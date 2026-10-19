"""Read the actual filled/stroked copper geometry from an EasyEDA PCB PDF.

This is intentionally a vector parser, not an image trace. PDF stroke widths
are in points and remain available even when CAD importers show only their
centerlines. The supported source is a single-page EasyEDA PCB-layer print;
translations, rotations, reflections and uniform scaling are supported.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from pypdf import PdfReader
from pypdf.generic import ContentStream
from shapely import affinity, union_all
from shapely.geometry import GeometryCollection, LineString, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize


MM_PER_POINT = 25.4 / 72.0


@dataclass
class PdfCopper:
    copper: BaseGeometry
    board: BaseGeometry
    holes: BaseGeometry
    stroke_widths_mm: Counter[float]
    stroke_count: int
    fill_count: int
    hole_count: int


@dataclass
class Paint:
    operator: str
    paths: list[list[tuple[float, float]]]
    width: float
    cap: int
    join: int
    fill_gray: float
    stroke_gray: float


def _distance_to_chord(
    point: tuple[float, float], start: tuple[float, float], end: tuple[float, float]
) -> float:
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    if length < 1e-12:
        return math.dist(point, start)
    return abs(dx * (start[1] - point[1]) - dy * (start[0] - point[0])) / length


def _flatten_cubic(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    tolerance: float,
    depth: int = 0,
) -> list[tuple[float, float]]:
    if depth >= 16 or max(
        _distance_to_chord(p1, p0, p3), _distance_to_chord(p2, p0, p3)
    ) <= tolerance:
        return [p3]
    p01 = ((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2)
    p12 = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
    p23 = ((p2[0] + p3[0]) / 2, (p2[1] + p3[1]) / 2)
    p012 = ((p01[0] + p12[0]) / 2, (p01[1] + p12[1]) / 2)
    p123 = ((p12[0] + p23[0]) / 2, (p12[1] + p23[1]) / 2)
    middle = ((p012[0] + p123[0]) / 2, (p012[1] + p123[1]) / 2)
    return _flatten_cubic(p0, p01, p012, middle, tolerance, depth + 1) + _flatten_cubic(
        middle, p123, p23, p3, tolerance, depth + 1
    )


def _append_point(path: list[tuple[float, float]], point: tuple[float, float]) -> None:
    if not path or point != path[-1]:
        path.append(point)


def _read_paints(pdf_path: Path, tolerance_mm: float) -> list[Paint]:
    reader = PdfReader(pdf_path)
    if len(reader.pages) != 1:
        raise ValueError("PDF input must contain exactly one PCB page")
    page = reader.pages[0]
    if int(page.get("/Rotate", 0)) % 360 or float(page.get("/UserUnit", 1)) != 1:
        raise ValueError("Rotated or rescaled PDF pages are not supported")
    state = {
        "w": 1.0, "J": 0, "j": 0, "G": 0.0, "g": 0.0,
        "ctm": (1.0, 0.0, 0.0, 1.0, 0.0, 0.0),
    }
    stack: list[dict[str, float | int | tuple[float, ...]]] = []
    paths: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    paints: list[Paint] = []
    tolerance_pt = tolerance_mm / MM_PER_POINT
    ext_states = page.get("/Resources", {}).get("/ExtGState", {})

    def transformed(x: float, y: float) -> tuple[float, float]:
        a, b, c, d, e, f = state["ctm"]
        return (a * x + c * y + e, b * x + d * y + f)

    def color_class(red: float, green: float, blue: float) -> float:
        """Keep only black copper and white cutouts; ignore colored annotations."""
        if max(red, green, blue) <= 0.1:
            return 0.0
        if min(red, green, blue) >= 0.9:
            return 1.0
        return 0.5

    def finish_path() -> None:
        nonlocal current
        if current:
            paths.append(current)
            current = []

    for operands, raw_operator in ContentStream(page.get_contents(), reader).operations:
        op = raw_operator.decode("latin1")
        if op == "q":
            stack.append(state.copy())
        elif op == "Q":
            if not stack:
                raise ValueError("Unbalanced PDF graphics state")
            state = stack.pop()
        elif op in ("w", "J", "j"):
            state[op] = float(operands[0])
        elif op in ("G", "g"):
            gray = float(operands[0])
            state[op] = color_class(gray, gray, gray)
        elif op in ("RG", "rg"):
            red, green, blue = (float(value) for value in operands)
            state["G" if op == "RG" else "g"] = color_class(red, green, blue)
        elif op in ("K", "k"):
            cyan, magenta, yellow, black = (float(value) for value in operands)
            state["G" if op == "K" else "g"] = color_class(
                (1 - cyan) * (1 - black),
                (1 - magenta) * (1 - black),
                (1 - yellow) * (1 - black),
            )
        elif op == "cm":
            a, b, c, d, e, f = state["ctm"]
            u, v, w, z, tx, ty = (float(x) for x in operands)
            state["ctm"] = (
                a * u + c * v, b * u + d * v,
                a * w + c * z, b * w + d * z,
                a * tx + c * ty + e, b * tx + d * ty + f,
            )
            aa, bb, cc, dd, _, _ = state["ctm"]
            sx, sy = math.hypot(aa, bb), math.hypot(cc, dd)
            if (
                sx <= 0 or not math.isclose(sx, sy, rel_tol=1e-9, abs_tol=1e-9)
                or not math.isclose(aa * cc + bb * dd, 0, abs_tol=1e-9 * sx * sy)
            ):
                raise ValueError("PDF uses a non-uniform or skewed transform")
        elif op == "gs":
            graphics_state = ext_states.get(operands[0])
            if graphics_state is None:
                raise ValueError(f"Unknown PDF graphics state: {operands[0]}")
            for key, value in graphics_state.get_object().items():
                if key not in ("/CA", "/ca") or float(value) != 1.0:
                    raise ValueError(f"Unsupported PDF graphics state: {key}")
        elif op == "Do":
            raise ValueError(f"Unsupported PDF drawing operator: {op}")
        elif op == "m":
            finish_path()
            current = [transformed(float(operands[0]), float(operands[1]))]
        elif op == "l":
            if not current:
                raise ValueError("PDF line without a starting point")
            _append_point(current, transformed(float(operands[0]), float(operands[1])))
        elif op in ("c", "v", "y"):
            if not current:
                raise ValueError("PDF curve without a starting point")
            p0 = current[-1]
            coords = tuple(float(x) for x in operands)
            if op == "c":
                p1, p2, p3 = (
                    transformed(*coords[0:2]), transformed(*coords[2:4]),
                    transformed(*coords[4:6]),
                )
            elif op == "v":
                p1, p2, p3 = (p0, transformed(*coords[0:2]), transformed(*coords[2:4]))
            else:
                p1, p2, p3 = (
                    transformed(*coords[0:2]), transformed(*coords[2:4]),
                    transformed(*coords[2:4]),
                )
            for point in _flatten_cubic(p0, p1, p2, p3, tolerance_pt):
                _append_point(current, point)
        elif op == "h":
            if current:
                _append_point(current, current[0])
                finish_path()
        elif op == "re":
            finish_path()
            x, y, width, height = (float(value) for value in operands)
            paths.append(
                [transformed(x, y), transformed(x + width, y),
                 transformed(x + width, y + height), transformed(x, y + height),
                 transformed(x, y)]
            )
        elif op in ("S", "s", "B", "b", "f", "F", "n"):
            if op in ("s", "b") and current:
                _append_point(current, current[0])
            finish_path()
            if op != "n" and paths:
                paints.append(
                    Paint(
                        op, paths,
                        float(state["w"]) * math.hypot(*state["ctm"][:2]),
                        int(state["J"]),
                        int(state["j"]), float(state["g"]), float(state["G"])
                    )
                )
            paths = []
        elif op in ("f*", "B*", "b*"):
            raise ValueError("Even-odd PDF fills are not supported")
    if stack:
        raise ValueError("Unbalanced PDF graphics state")
    return paints


def _winding_number(
    path: Sequence[tuple[float, float]], point: tuple[float, float]
) -> int:
    """Nonzero PDF fill winding, including self-intersecting paths."""
    px, py = point
    winding = 0
    for a, b in zip(path, (*path[1:], path[0])):
        cross = (b[0] - a[0]) * (py - a[1]) - (px - a[0]) * (b[1] - a[1])
        if a[1] <= py < b[1] and cross > 0:
            winding += 1
        elif b[1] <= py < a[1] and cross < 0:
            winding -= 1
    return winding


def _filled_area(paths: Sequence[Sequence[tuple[float, float]]]) -> BaseGeometry:
    """Use PDF's nonzero winding rule, including compound clearance holes."""
    loops = [path for path in paths if len(path) >= 3]
    if not loops:
        return GeometryCollection()
    if len(loops) == 1:
        polygon = Polygon(loops[0])
        if polygon.is_valid:
            return polygon
    linework = union_all([
        LineString([*path, path[0]]) if path[0] != path[-1] else LineString(path)
        for path in loops
    ])
    faces = list(polygonize(linework))
    selected = []
    for face in faces:
        point = face.representative_point()
        if sum(_winding_number(path, (point.x, point.y)) for path in loops):
            selected.append(face)
    return union_all(selected) if selected else GeometryCollection()


def _stroke_area(paint: Paint) -> BaseGeometry:
    if paint.width <= 0:
        return GeometryCollection()
    cap_style = {0: "flat", 1: "round", 2: "square"}[paint.cap]
    join_style = {0: "mitre", 1: "round", 2: "bevel"}[paint.join]
    pieces = [
        LineString(path).buffer(
            paint.width / 2, quad_segs=64, cap_style=cap_style, join_style=join_style
        )
        for path in paint.paths if len(path) >= 2
    ]
    return union_all(pieces) if pieces else GeometryCollection()


def read_easyeda_pdf(
    pdf_path: Path, tolerance_mm: float, min_track_width_mm: float,
) -> PdfCopper:
    """Return one physical copper area and board outline, in millimetres."""
    paints = _read_paints(pdf_path, tolerance_mm)
    first_copper = next(
        (i for i, paint in enumerate(paints)
         if paint.operator in ("B", "b", "f", "F") and paint.fill_gray < 0.5),
        None,
    )
    if first_copper is None:
        raise ValueError("No filled copper found in the PDF")
    board_lines = [
        LineString(path) for paint in paints[:first_copper]
        if paint.operator in ("S", "s") and paint.stroke_gray < 0.5
        for path in paint.paths if len(path) >= 2
    ]
    board_faces = list(polygonize(union_all(board_lines))) if board_lines else []
    if len(board_faces) != 1:
        raise ValueError("Could not identify the PDF board outline")
    board_points = board_faces[0]
    copper_parts: list[BaseGeometry] = []
    black_fills: list[BaseGeometry] = []
    white_fills: list[BaseGeometry] = []
    widths: Counter[float] = Counter()
    stroke_count = 0
    fill_count = 0
    for paint in paints[first_copper:]:
        if paint.operator in ("B", "b", "f", "F"):
            area = _filled_area(paint.paths)
            if not area.is_empty:
                if paint.fill_gray < 0.5:
                    black_fills.append(area)
                elif paint.fill_gray > 0.5:
                    white_fills.append(area)
        # PDF's B/b operators paint both the fill and the centered outline.
        # On this EasyEDA export the 0.2 mm outline extends the copper by
        # about 0.1 mm on each side; omitting it creates visible cusps near
        # thermals and leaves the outer plane short of the board edge.
        if paint.operator in ("S", "s", "B", "b") and paint.stroke_gray < 0.5:
            width_mm = paint.width * MM_PER_POINT
            if paint.operator in ("B", "b") or width_mm >= min_track_width_mm:
                area = _stroke_area(paint)
                if not area.is_empty:
                    copper_parts.append(area)
                    stroke_count += 1
                    widths[round(width_mm, 4)] += 1
    holes_pt = union_all(white_fills) if white_fills else GeometryCollection()
    for area in black_fills:
        # The re-export draws narrow black outlines *inside* each white drill
        # circle. They are drafting marks, not islands of copper in a hole.
        if not holes_pt.is_empty and area.difference(holes_pt).area <= 1e-8:
            continue
        copper_parts.append(area)
        fill_count += 1
    if not copper_parts or not stroke_count:
        raise ValueError("PDF has no usable copper strokes")
    # PDF's native coordinates already have Y increasing upwards, like DXF.
    transform = (MM_PER_POINT, 0, 0, MM_PER_POINT, 0, 0)
    copper = affinity.affine_transform(union_all(copper_parts), transform)
    board = affinity.affine_transform(board_points, transform)
    holes = affinity.affine_transform(holes_pt, transform)
    return PdfCopper(copper, board, holes, widths, stroke_count, fill_count, len(white_fills))
