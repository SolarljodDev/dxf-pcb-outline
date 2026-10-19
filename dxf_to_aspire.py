#!/usr/bin/env python3
"""Convert a vector EasyEDA PCB PDF into closed DXF copper vectors for Aspire."""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

try:
    import ezdxf
    from ezdxf.entities import DXFGraphic, Hatch, LWPolyline
    from ezdxf.path import from_hatch, make_path
    from ezdxf.render import TraceBuilder
    from shapely import make_valid, union_all
    from shapely.geometry import GeometryCollection, LineString, Point, Polygon
    from shapely.geometry.base import BaseGeometry
    from shapely.geometry.polygon import orient
except ModuleNotFoundError as exc:  # pragma: no cover - friendly CLI error
    print(
        f"Missing Python package: {exc.name}.\n"
        "Install dependencies with:\n"
        "  python -m pip install -r requirements.txt\n"
        "or drag the PDF onto convert_dxf.bat.",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc


DEFAULT_TOLERANCE = 0.0005
DEFAULT_JOIN_TOLERANCE = 0.001
DEFAULT_BACKGROUND_RATIO = 0.90
DEFAULT_POUR_AREA_RATIO = 0.02
DEFAULT_MIN_TRACK_WIDTH = 0.10
DEFAULT_TEARDROP_MAX_WIDTH = 0.01


@dataclass
class LayerResult:
    source_layer: str
    track_layer: str
    pour_layer: str
    pad_hatch_count: int = 0
    pour_hatch_count: int = 0
    stroke_count: int = 0
    teardrop_count: int = 0
    thermal_count: int = 0
    skipped_thin_strokes: int = 0
    skipped_closed_strokes: int = 0
    removed_background_handles: list[str] = field(default_factory=list)
    track_polygons: list[Polygon] = field(default_factory=list)
    pour_polygons: list[Polygon] = field(default_factory=list)


def polygon_parts(geometry: BaseGeometry) -> list[Polygon]:
    """Return only area components from any Shapely geometry."""
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]  # type: ignore[list-item]
    parts: list[Polygon] = []
    for child in getattr(geometry, "geoms", ()):  # MultiPolygon/collection
        parts.extend(polygon_parts(child))
    return parts


def clean_area(geometry: BaseGeometry) -> BaseGeometry:
    """Repair an area and discard line/point leftovers."""
    if geometry.is_empty:
        return GeometryCollection()
    if not geometry.is_valid:
        geometry = make_valid(geometry)
    parts = polygon_parts(geometry)
    return union_all(parts) if parts else GeometryCollection()


def points_to_polygon(points: Iterable[object]) -> BaseGeometry:
    coords: list[tuple[float, float]] = []
    for point in points:
        x = float(getattr(point, "x", point[0]))
        y = float(getattr(point, "y", point[1]))
        if not coords or not (math.isclose(x, coords[-1][0]) and math.isclose(y, coords[-1][1])):
            coords.append((x, y))
    if len(coords) < 3:
        return GeometryCollection()
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    return clean_area(Polygon(coords))


def regular_circle_from_hatch_boundary(boundary: object) -> BaseGeometry | None:
    """Restore a true round pad from an exported straight-sided regular polygon.

    EasyEDA writes some 16-sided circular pads as straight HATCH vertices, even
    though the corresponding pad is round.  Do this only for near-perfect
    regular polygons; all other boundaries keep their source shape.
    """
    vertices = list(getattr(boundary, "vertices", ()))
    if not 8 <= len(vertices) <= 32:
        return None
    if any(abs(float(vertex[2])) > 1e-12 for vertex in vertices):
        return None
    xy = [(float(vertex[0]), float(vertex[1])) for vertex in vertices]
    center_x = sum(x for x, _ in xy) / len(xy)
    center_y = sum(y for _, y in xy) / len(xy)
    radii = [math.hypot(x - center_x, y - center_y) for x, y in xy]
    radius = sum(radii) / len(radii)
    if radius <= 0 or max(abs(r - radius) for r in radii) > radius * 0.005:
        return None
    angles = sorted(math.atan2(y - center_y, x - center_x) for x, y in xy)
    gaps = [
        (angles[(i + 1) % len(angles)] - angles[i]) % (2 * math.pi)
        for i in range(len(angles))
    ]
    ideal_gap = 2 * math.pi / len(angles)
    if max(abs(gap - ideal_gap) for gap in gaps) > ideal_gap * 0.02:
        return None
    return Point(center_x, center_y).buffer(radius, quad_segs=64)


def hatch_to_area(entity: Hatch, tolerance: float) -> BaseGeometry:
    """Convert HATCH loops using the even-odd fill rule."""
    result: BaseGeometry = GeometryCollection()
    for raw_boundary, boundary in zip(entity.paths, from_hatch(entity)):
        loop = regular_circle_from_hatch_boundary(raw_boundary)
        if loop is None:
            loop = points_to_polygon(boundary.flattening(tolerance, segments=8))
        if not loop.is_empty:
            result = result.symmetric_difference(loop)
    return clean_area(result)


def polyline_center_area(entity: LWPolyline, tolerance: float) -> BaseGeometry:
    """Turn a closed polyline center path into an area, ignoring its width."""
    if not entity.closed:
        return GeometryCollection()
    path = make_path(entity)
    return points_to_polygon(path.flattening(tolerance, segments=8))


def wide_polyline_areas(entity: LWPolyline, arc_segments: int) -> list[Polygon]:
    """Expand the DXF start/end widths into their exact trace faces."""
    points = list(entity.get_points("xyseb"))
    constant_width = float(entity.dxf.const_width)
    if (
        len(points) == 2
        and constant_width > 0
        and all(abs(float(point[2])) < 1e-12 for point in points)
        and all(abs(float(point[3])) < 1e-12 for point in points)
        and all(abs(float(point[4])) < 1e-12 for point in points)
    ):
        centerline = [(float(point[0]), float(point[1])) for point in points]
        if LineString(centerline).length > 0:
            # Every PCB trace in the supplied DXF is one straight segment.
            # Round caps merge into smooth turns where segments meet.
            rounded = round_centerline_stroke(centerline, constant_width)
            return polygon_parts(rounded)
    areas: list[Polygon] = []
    builder = TraceBuilder.from_polyline(entity, segments=arc_segments)
    for trace_polygon in builder.polygons():
        area = points_to_polygon(trace_polygon)
        areas.extend(polygon_parts(area))
    return areas


def round_centerline_stroke(
    points: Sequence[tuple[float, float]], width: float, closed: bool = False
) -> BaseGeometry:
    """Expand an open or closed DXF centerline by half its pen width."""
    coords = list(points)
    if closed and coords and coords[0] != coords[-1]:
        coords.append(coords[0])
    return LineString(coords).buffer(
        width / 2, quad_segs=64, cap_style="round", join_style="round"
    )


def layer_names(modelspace: object) -> list[str]:
    return sorted({entity.dxf.layer for entity in modelspace})


def match_layer(requested: str | None, available: Sequence[str]) -> str | None:
    if requested is None:
        return None
    by_lower = {name.casefold(): name for name in available}
    return by_lower.get(requested.casefold())


def find_default_layer(available: Sequence[str], exact: str, words: Sequence[str]) -> str | None:
    match = match_layer(exact, available)
    if match:
        return match
    for name in available:
        lowered = name.casefold()
        if all(word in lowered for word in words):
            return name
    return None


def build_board_reference(
    modelspace: object, board_layer: str | None, tolerance: float
) -> BaseGeometry:
    if board_layer is None:
        return GeometryCollection()
    candidates: list[Polygon] = []
    for entity in modelspace:
        if entity.dxf.layer != board_layer:
            continue
        if entity.dxftype() == "LWPOLYLINE":
            candidates.extend(polygon_parts(polyline_center_area(entity, tolerance)))
        elif entity.dxftype() == "HATCH":
            candidates.extend(polygon_parts(hatch_to_area(entity, tolerance)))
    return max(candidates, key=lambda item: item.area) if candidates else GeometryCollection()


def is_board_background(
    hatch_area: BaseGeometry,
    board_area: BaseGeometry,
    minimum_ratio: float,
    tolerance: float,
) -> bool:
    if hatch_area.is_empty or board_area.is_empty or board_area.area <= 0:
        return False
    ratio = hatch_area.area / board_area.area
    if ratio < minimum_ratio:
        return False
    # A real full-board copper pour is possible, so this filter is exposed as
    # --keep-board-fill.  EasyEDA's export artifact is just inside the board.
    return bool(board_area.buffer(tolerance * 2).covers(hatch_area))


def matching_closed_outline(
    modelspace: object,
    hatch_entity: Hatch,
    hatch_area: BaseGeometry,
    tolerance: float,
) -> LWPolyline | None:
    """Find the DXF centerline drawn on top of a filled HATCH boundary."""
    for entity in modelspace:
        if entity.dxf.layer != hatch_entity.dxf.layer:
            continue
        if entity.dxftype() != "LWPOLYLINE" or not entity.closed:
            continue
        width = float(entity.dxf.const_width)
        if not 0 < width < 0.1:
            continue
        candidate = polyline_center_area(entity, tolerance)
        if candidate.is_empty:
            continue
        if abs(candidate.area - hatch_area.area) > hatch_area.area * 1e-5:
            continue
        if candidate.symmetric_difference(hatch_area).area <= hatch_area.area * 1e-5:
            return entity
    return None


def pour_outline_stroke(entity: LWPolyline, tolerance: float) -> BaseGeometry:
    """Give a pour's closed outline the same rounded stroke as a track.

    The HATCH is already the interior copper; only the outer half of this
    stroke survives the union with it.
    """
    centerline = [
        (point.x, point.y)
        for point in make_path(entity).flattening(tolerance, segments=8)
    ]
    return round_centerline_stroke(centerline, float(entity.dxf.const_width), closed=True)


def merge_areas(
    areas: Sequence[BaseGeometry], snap_grid: float, join_tolerance: float
) -> BaseGeometry:
    if not areas:
        return GeometryCollection()
    merged = union_all(areas, grid_size=snap_grid if snap_grid > 0 else None)
    merged = clean_area(merged)
    if join_tolerance > 0 and not merged.is_empty:
        merged = merged.buffer(
            join_tolerance, quad_segs=4, join_style="mitre"
        ).buffer(-join_tolerance, quad_segs=4, join_style="mitre")
        merged = clean_area(merged)
    return merged


def collect_holes(
    modelspace: object,
    hole_layer: str | None,
    tolerance: float,
    snap_grid: float,
) -> BaseGeometry:
    if hole_layer is None:
        return GeometryCollection()
    hatch_areas: list[BaseGeometry] = []
    fallback_areas: list[BaseGeometry] = []
    for entity in modelspace:
        if entity.dxf.layer != hole_layer:
            continue
        if entity.dxftype() == "HATCH":
            hatch_areas.append(hatch_to_area(entity, tolerance))
        elif entity.dxftype() == "LWPOLYLINE":
            fallback_areas.append(polyline_center_area(entity, tolerance))
    # EasyEDA emits a HATCH and a duplicate outline for each hole.  Prefer the
    # filled boundaries; fall back to closed polylines for other exporters.
    source = hatch_areas if any(not item.is_empty for item in hatch_areas) else fallback_areas
    return merge_areas(source, snap_grid, 0.0)


def convert_copper_layer(
    modelspace: object,
    source_layer: str,
    board_area: BaseGeometry,
    hole_area: BaseGeometry,
    args: argparse.Namespace,
) -> LayerResult:
    result = LayerResult(
        source_layer=source_layer,
        track_layer=f"TRACK_OUTLINE_{source_layer}",
        pour_layer=f"POUR_OUTLINE_{source_layer}",
    )
    track_areas: list[BaseGeometry] = []
    pour_areas: list[BaseGeometry] = []
    for entity in modelspace:
        if entity.dxf.layer != source_layer:
            continue
        if entity.dxftype() == "HATCH":
            hatch_area = hatch_to_area(entity, args.tolerance)
            if (
                not args.keep_board_fill
                and is_board_background(
                    hatch_area,
                    board_area,
                    args.background_ratio,
                    args.tolerance,
                )
            ):
                result.removed_background_handles.append(entity.dxf.handle or "?")
                continue
            if not hatch_area.is_empty:
                if (
                    not board_area.is_empty
                    and hatch_area.area / board_area.area >= args.pour_area_ratio
                ):
                    outline = matching_closed_outline(
                        modelspace, entity, hatch_area, args.tolerance
                    )
                    if outline is not None:
                        hatch_area = clean_area(
                            union_all((hatch_area, pour_outline_stroke(outline, args.tolerance)))
                        )
                    pour_areas.append(hatch_area)
                    result.pour_hatch_count += 1
                else:
                    track_areas.append(hatch_area)
                    result.pad_hatch_count += 1
        elif entity.dxftype() == "LWPOLYLINE" and entity.has_width:
            width = float(entity.dxf.const_width)
            if entity.closed:
                # The source's teardrops are filled closed paths drawn with a
                # hairline pen (0.000254 mm).  The pad/pour boundaries have a
                # thicker cosmetic pen and duplicate their HATCH geometry.
                center_area = polyline_center_area(entity, args.tolerance)
                if (
                    0 < width <= args.teardrop_max_width
                    and not center_area.is_empty
                    and (
                        board_area.is_empty
                        or center_area.area / board_area.area < args.pour_area_ratio
                    )
                ):
                    track_areas.append(center_area)
                    result.teardrop_count += 1
                elif args.include_closed_strokes and not center_area.is_empty:
                    track_areas.extend(wide_polyline_areas(entity, args.arc_segments))
                    result.stroke_count += 1
                else:
                    result.skipped_closed_strokes += 1
                continue
            if width < args.min_track_width:
                result.skipped_thin_strokes += 1
                continue
            stroke_areas = wide_polyline_areas(entity, args.arc_segments)
            if stroke_areas:
                track_areas.extend(stroke_areas)
                result.stroke_count += 1

    tracks = merge_areas(track_areas, args.snap_grid, args.join_tolerance)
    pours = merge_areas(pour_areas, args.snap_grid, args.join_tolerance)
    if args.merge_pours and not pours.is_empty:
        tracks = merge_areas([tracks, pours], args.snap_grid, args.join_tolerance)
        pours = GeometryCollection()
    if args.subtract_holes and not hole_area.is_empty:
        if not tracks.is_empty:
            tracks = clean_area(tracks.difference(hole_area))
        if not pours.is_empty:
            pours = clean_area(pours.difference(hole_area))
    result.track_polygons = sorted(
        polygon_parts(tracks), key=lambda item: item.area, reverse=True
    )
    result.pour_polygons = sorted(
        polygon_parts(pours), key=lambda item: item.area, reverse=True
    )
    return result


def reconstruct_filled_copper_layer(
    modelspace: object,
    source_layer: str,
    board_area: BaseGeometry,
    hole_area: BaseGeometry,
    args: argparse.Namespace,
) -> LayerResult:
    """Rebuild an EasyEDA copper plane from its fill, reliefs and pad spokes.

    In this DXF export the board-sized HATCH is the plane, the HATCHes before
    the narrow radial guides are its clearance voids, and the later HATCHes
    are actual pad/region copper.  The guides store only a cosmetic DXF pen
    width; their real copper width has to be supplied by the caller.
    """
    entities = [e for e in modelspace if e.dxf.layer == source_layer]
    hatches = [e for e in entities if e.dxftype() == "HATCH"]
    if not hatches:
        raise ValueError("No copper HATCH entities on the selected layer")
    hatch_areas = {e.dxf.handle: hatch_to_area(e, args.tolerance) for e in hatches}
    background = max(hatches, key=lambda e: hatch_areas[e.dxf.handle].area)
    background_area = hatch_areas[background.dxf.handle]
    if (
        board_area.is_empty
        or background_area.area / board_area.area < args.background_ratio
    ):
        raise ValueError("No board-sized copper fill found for thermal reconstruction")

    guide_indices = [
        index for index, e in enumerate(entities)
        if e.dxftype() == "LWPOLYLINE"
        and not e.closed
        and len(e) == 2
        and 0 < float(e.dxf.const_width) < args.min_track_width
    ]
    if not guide_indices:
        raise ValueError("No narrow thermal guide lines found in the DXF")
    first_guide = min(guide_indices)
    clearance_hatches = [
        e for index, e in enumerate(entities)
        if e.dxftype() == "HATCH" and e is not background and index < first_guide
    ]
    pad_hatches = [
        e for index, e in enumerate(entities)
        if e.dxftype() == "HATCH" and index > first_guide
    ]
    if not clearance_hatches or not pad_hatches:
        raise ValueError("Could not separate clearance and copper HATCH groups")

    def stroked_hatch_area(entity: Hatch) -> BaseGeometry:
        area = hatch_areas[entity.dxf.handle]
        outline = matching_closed_outline(modelspace, entity, area, args.tolerance)
        if outline is None:
            return area
        return clean_area(union_all((area, pour_outline_stroke(outline, args.tolerance))))

    plane = stroked_hatch_area(background)
    clearances = union_all([stroked_hatch_area(e) for e in clearance_hatches])
    plane = clean_area(plane.difference(clearances))
    pads = [hatch_areas[e.dxf.handle] for e in pad_hatches]
    pad_centers = [pad.centroid for pad in pads if not pad.is_empty]
    result = LayerResult(
        source_layer=source_layer,
        track_layer=f"COPPER_OUTLINE_{source_layer}",
        pour_layer=f"POUR_OUTLINE_{source_layer}",
        pad_hatch_count=len(pad_hatches),
        pour_hatch_count=1,
    )
    copper: list[BaseGeometry] = [plane, *pads]
    for entity in entities:
        if entity.dxftype() != "LWPOLYLINE":
            continue
        width = float(entity.dxf.const_width)
        if entity.closed:
            if 0 < width <= args.teardrop_max_width:
                area = polyline_center_area(entity, args.tolerance)
                if not area.is_empty and (
                    board_area.is_empty
                    or area.area / board_area.area < args.pour_area_ratio
                ):
                    copper.append(area)
                    result.teardrop_count += 1
            continue
        if width >= args.min_track_width:
            copper.extend(wide_polyline_areas(entity, args.arc_segments))
            result.stroke_count += 1
            continue
        if not 0 < width < args.min_track_width or len(entity) != 2:
            continue
        points = [(float(x), float(y)) for x, y in entity.get_points("xy")]
        if any(Point(points[0]).distance(center) <= 0.005 for center in pad_centers):
            copper.append(round_centerline_stroke(points, args.thermal_width))
            result.thermal_count += 1
        else:
            result.skipped_thin_strokes += 1

    if not result.thermal_count:
        raise ValueError("No thermal guides matched pad centers; refusing to guess")
    merged = merge_areas(copper, args.snap_grid, args.join_tolerance)
    if args.subtract_holes and not hole_area.is_empty:
        merged = clean_area(merged.difference(hole_area))
    result.track_polygons = sorted(
        polygon_parts(merged), key=lambda item: item.area, reverse=True
    )
    return result


def safe_layer_name(name: str) -> str:
    forbidden = '<>/\\":;?*|='
    clean = "".join("_" if char in forbidden else char for char in name).strip()
    return clean[:255] or "OUTLINE"


def add_layer(document: object, name: str, color: int) -> str:
    name = safe_layer_name(name)
    if name not in document.layers:
        document.layers.add(name, color=color)
    return name


def normalized_ring_coords(ring: object) -> list[tuple[float, float]]:
    coords = [(float(x), float(y)) for x, y, *_ in ring.coords]
    compact: list[tuple[float, float]] = []
    for point in coords:
        if not compact or point != compact[-1]:
            compact.append(point)
    if len(compact) > 1 and compact[0] == compact[-1]:
        compact.pop()
    return compact


def circle_through_three(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
) -> tuple[float, float, float] | None:
    ax, ay = a
    bx, by = b
    cx, cy = c
    denominator = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(denominator) < 1e-12:
        return None
    aa, bb, cc = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    center_x = (aa * (by - cy) + bb * (cy - ay) + cc * (ay - by)) / denominator
    center_y = (aa * (cx - bx) + bb * (ax - cx) + cc * (bx - ax)) / denominator
    radius = math.hypot(ax - center_x, ay - center_y)
    return center_x, center_y, radius


def distance_to_line(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    if length < 1e-12:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    return abs(dx * (start[1] - point[1]) - dy * (start[0] - point[0])) / length


def fit_closed_ring_arcs(
    coords: list[tuple[float, float]], tolerance: float
) -> list[tuple[float, float, float]]:
    """Represent polygonized round sections as DXF polyline bulge arcs.

    Each returned vertex has (x, y, bulge-to-next).  Straight runs are also
    simplified.  Endpoints are retained; only the segments between them change.
    """
    count = len(coords)
    if count < 3:
        return []

    # An isolated complete circle has no corner at which to begin the walk.
    if count >= 16:
        center_x = sum(point[0] for point in coords) / count
        center_y = sum(point[1] for point in coords) / count
        radii = [math.hypot(x - center_x, y - center_y) for x, y in coords]
        radius = sum(radii) / count
        if (
            radius > 0
            and max(abs(r - radius) for r in radii) <= max(tolerance, 0.001)
            and abs(math.pi * radius * radius - abs(Polygon(coords).area))
            <= max(0.02 * radius * radius, tolerance * radius * 8)
        ):
            signed_area = sum(
                coords[i][0] * coords[(i + 1) % count][1]
                - coords[(i + 1) % count][0] * coords[i][1]
                for i in range(count)
            )
            sign = 1.0 if signed_area > 0 else -1.0
            first = coords[0]
            opposite = (2 * center_x - first[0], 2 * center_y - first[1])
            return [(first[0], first[1], sign), (opposite[0], opposite[1], sign)]

    # Begin at the sharpest corner so a round section does not straddle the
    # cyclic boundary of the coordinate list.
    def corner_strength(index: int) -> float:
        before = coords[(index - 1) % count]
        at = coords[index]
        after = coords[(index + 1) % count]
        u = (at[0] - before[0], at[1] - before[1])
        v = (after[0] - at[0], after[1] - at[1])
        return abs(math.atan2(u[0] * v[1] - u[1] * v[0], u[0] * v[0] + u[1] * v[1]))

    start_index = max(range(count), key=corner_strength)
    ordered = coords[start_index:] + coords[:start_index]
    ordered.append(ordered[0])
    fitted: list[tuple[float, float, float]] = []
    index = 0
    while index < count:
        # Long straight runs often contain unnecessary vertices after unions.
        line_end = index + 1
        for end in range(index + 2, count + 1):
            if all(
                distance_to_line(ordered[k], ordered[index], ordered[end]) <= tolerance
                for k in range(index + 1, end)
            ):
                line_end = end
            else:
                break

        arc_end = index
        arc_sweep = 0.0
        # The wider seed survives noisy polygonized long curves, while the
        # compact seed can still recover a three-edge shallow/short curve.
        for middle, seed_end, minimum_edges, minimum_sweep in (
            (2, 4, 4, 5.0),
            (1, 3, 3, 0.25),
        ):
            if index + seed_end > count:
                continue
            circle = circle_through_three(
                ordered[index], ordered[index + middle], ordered[index + seed_end]
            )
            if circle is not None:
                center_x, center_y, radius = circle
                if 0.01 <= radius <= 1000:
                    previous_angle = math.atan2(
                        ordered[index][1] - center_y,
                        ordered[index][0] - center_x,
                    )
                    sweep = 0.0
                    direction = 0
                    for end in range(index + 1, count + 1):
                        x, y = ordered[end]
                        if abs(math.hypot(x - center_x, y - center_y) - radius) > tolerance:
                            break
                        angle = math.atan2(y - center_y, x - center_x)
                        delta = (angle - previous_angle + math.pi) % (2 * math.pi) - math.pi
                        if abs(delta) < 1e-9:
                            continue
                        # Sparse vertices can make an almost straight source
                        # edge look circular. The arc must stay within the
                        # geometric tolerance of *each* original chord, not
                        # merely pass near its endpoints.
                        if radius * (1 - math.cos(abs(delta) / 2)) > max(tolerance, 0.001):
                            break
                        this_direction = 1 if delta > 0 else -1
                        if direction and this_direction != direction:
                            break
                        if abs(sweep + delta) > math.pi + 1e-6:
                            break
                        direction = this_direction
                        sweep += delta
                        previous_angle = angle
                        if (
                            end - index >= minimum_edges
                            and abs(sweep) >= math.radians(minimum_sweep)
                            and end > arc_end
                        ):
                            arc_end, arc_sweep = end, sweep

        if arc_end - index >= 3 and arc_end >= line_end:
            fitted.append(
                (ordered[index][0], ordered[index][1], math.tan(arc_sweep / 4))
            )
            index = arc_end
        else:
            fitted.append((ordered[index][0], ordered[index][1], 0.0))
            index = line_end
    return fitted


def add_ring(
    modelspace: object, ring: object, layer: str, arc_fit_tolerance: float
) -> bool:
    coords = normalized_ring_coords(ring)
    if len(coords) < 3:
        return False
    original_area = Polygon(coords).area
    for tolerance in (
        arc_fit_tolerance,
        arc_fit_tolerance / 10,
        arc_fit_tolerance / 100,
    ):
        bulged = fit_closed_ring_arcs(coords, tolerance)
        if not bulged:
            continue
        entity = modelspace.add_lwpolyline(
            bulged,
            format="xyb",
            close=True,
            dxfattribs={"layer": layer, "const_width": 0.0},
        )
        flattened = [
            (point.x, point.y)
            for point in make_path(entity).flattening(0.00005, segments=8)
        ]
        fitted_area = Polygon(flattened)
        original_boundary = Polygon(coords).boundary
        if (
            fitted_area.is_valid
            and not fitted_area.is_empty
            and abs(fitted_area.area - original_area) <= max(0.01 * original_area, 1e-5)
            and fitted_area.boundary.hausdorff_distance(original_boundary)
            <= max(arc_fit_tolerance, 0.001)
        ):
            return True
        modelspace.delete_entity(entity)
    modelspace.add_lwpolyline(
        coords,
        format="xy",
        close=True,
        dxfattribs={"layer": layer, "const_width": 0.0},
    )
    return True


def write_board_contours(
    output_modelspace: object,
    board_area: BaseGeometry,
    output_layer: str,
    arc_fit_tolerance: float,
) -> int:
    count = 0
    for polygon in polygon_parts(board_area):
        polygon = orient(polygon, sign=1.0)
        count += int(add_ring(output_modelspace, polygon.exterior, output_layer, arc_fit_tolerance))
        for interior in polygon.interiors:
            count += int(add_ring(output_modelspace, interior, output_layer, arc_fit_tolerance))
    return count


def write_result(
    input_document: object,
    output_path: Path,
    layer_results: Sequence[LayerResult],
    hole_area: BaseGeometry,
    board_area: BaseGeometry,
    args: argparse.Namespace,
) -> tuple[int, int, int, int]:
    output = ezdxf.new("R2007", setup=True)
    output.units = input_document.units
    output.header["$MEASUREMENT"] = input_document.header.get("$MEASUREMENT", 1)
    modelspace = output.modelspace()

    track_count = 0
    pour_count = 0
    drill_count = 0
    for index, result in enumerate(layer_results):
        for kind, polygons, layer_name, color in (
            ("track", result.track_polygons, result.track_layer, 1 + (index % 6)),
            ("pour", result.pour_polygons, result.pour_layer, 3 + (index % 4)),
        ):
            if not polygons:
                continue
            layer = add_layer(output, layer_name, color)
            for polygon in polygons:
                polygon = orient(polygon, sign=1.0)
                count = int(add_ring(modelspace, polygon.exterior, layer, args.arc_fit_tolerance))
                for interior in polygon.interiors:
                    void = Polygon(interior)
                    is_drill = (
                        not hole_area.is_empty
                        and void.area > 0
                        and void.intersection(hole_area).area / void.area > 0.5
                    )
                    if not is_drill:
                        count += int(add_ring(modelspace, interior, layer, args.arc_fit_tolerance))
                if kind == "track":
                    track_count += count
                else:
                    pour_count += count

    if args.write_holes and not hole_area.is_empty:
        drill_layer = add_layer(output, "DRILL_OUTLINE", 8)
        for hole in polygon_parts(hole_area):
            drill_count += int(
                add_ring(modelspace, orient(hole, sign=1.0).exterior, drill_layer, args.arc_fit_tolerance)
            )

    board_count = 0
    if args.write_board and not board_area.is_empty:
        board_layer = add_layer(output, "BOARD_OUTLINE", 6)
        board_count = write_board_contours(
            modelspace, board_area, board_layer, args.arc_fit_tolerance
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output.saveas(output_path)
    return track_count, pour_count, drill_count, board_count


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert a vector EasyEDA PCB PDF to closed DXF outlines for Aspire."
    )
    parser.add_argument("input", type=Path, help="source EasyEDA PCB PDF (not DXF)")
    parser.add_argument("-o", "--output", type=Path, help="output DXF path")
    parser.add_argument(
        "--tolerance",
        type=positive_float,
        default=DEFAULT_TOLERANCE,
        help="maximum PDF curve flattening error in mm (default: %(default)s)",
    )
    parser.add_argument(
        "--join-tolerance",
        type=non_negative_float,
        default=DEFAULT_JOIN_TOLERANCE,
        help="close gaps this small before union; 0 disables (default: %(default)s)",
    )
    parser.add_argument(
        "--snap-grid",
        type=non_negative_float,
        default=1e-6,
        help="coordinate grid used during union; 0 disables (default: %(default)g)",
    )
    parser.add_argument(
        "--arc-fit-tolerance",
        type=positive_float,
        default=0.0001,
        help="maximum radial/line fit error when restoring DXF arcs (default: %(default)s)",
    )
    parser.add_argument(
        "--min-track-width",
        type=non_negative_float,
        default=DEFAULT_MIN_TRACK_WIDTH,
        help="ignore PDF strokes thinner than this (default: %(default)s mm)",
    )
    parser.add_argument(
        "--no-subtract-holes",
        dest="subtract_holes",
        action="store_false",
        help="leave copper solid under drill holes",
    )
    parser.add_argument(
        "--no-holes",
        dest="write_holes",
        action="store_false",
        help="do not write the DRILL_OUTLINE layer",
    )
    parser.add_argument(
        "--no-board",
        dest="write_board",
        action="store_false",
        help="do not write the BOARD_OUTLINE layer",
    )
    parser.set_defaults(subtract_holes=True, write_holes=True, write_board=True)
    return parser


def resolve_layers(
    modelspace: object, args: argparse.Namespace
) -> tuple[list[str], str | None, str | None]:
    available = layer_names(modelspace)
    if args.copper_layer:
        copper_layers: list[str] = []
        for requested in args.copper_layer:
            match = match_layer(requested, available)
            if match is None:
                raise ValueError(
                    f"Copper layer '{requested}' not found. Available: {', '.join(available)}"
                )
            copper_layers.append(match)
    else:
        default = find_default_layer(available, "Top-Layer", ("top", "layer"))
        if default is None:
            excluded = ("hole", "drill", "board", "outline")
            default = next(
                (name for name in available if not any(word in name.casefold() for word in excluded)),
                None,
            )
        if default is None:
            raise ValueError(f"No copper layer found. Available: {', '.join(available)}")
        copper_layers = [default]

    if args.hole_layer:
        hole_layer = match_layer(args.hole_layer, available)
        if hole_layer is None:
            raise ValueError(f"Hole layer '{args.hole_layer}' not found")
    else:
        hole_layer = find_default_layer(available, "Hole-Layer", ("hole",))
        if hole_layer is None:
            hole_layer = find_default_layer(available, "Drill", ("drill",))

    if args.board_layer:
        board_layer = match_layer(args.board_layer, available)
        if board_layer is None:
            raise ValueError(f"Board layer '{args.board_layer}' not found")
    else:
        board_layer = find_default_layer(
            available, "Board-Outline-Layer", ("board", "outline")
        )
    return copper_layers, hole_layer, board_layer


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    input_path = args.input.expanduser().resolve()
    if input_path.suffix.casefold() != ".pdf":
        parser.error("input must be a vector EasyEDA PCB PDF; DXF is output only")
    if not input_path.is_file():
        parser.error(f"input file not found: {input_path}")
    output_path = (
        args.output.expanduser().resolve()
        if args.output
        else input_path.with_name(f"{input_path.stem}_aspire_tracks.dxf")
    )
    if output_path.suffix.casefold() != ".dxf":
        parser.error("output must be a .dxf file for Aspire")

    try:
        from pdf_geometry import read_easyeda_pdf

        pdf = read_easyeda_pdf(
            input_path, args.tolerance, args.min_track_width,
        )
        copper = merge_areas([pdf.copper], args.snap_grid, args.join_tolerance)
        hole_area = clean_area(pdf.holes)
        if args.subtract_holes and not hole_area.is_empty:
            copper = clean_area(copper.difference(hole_area))
        result = LayerResult(
            source_layer="Top-Layer",
            track_layer="COPPER_OUTLINE_Top-Layer",
            pour_layer="POUR_OUTLINE_Top-Layer",
            track_polygons=sorted(
                polygon_parts(copper), key=lambda item: item.area, reverse=True
            ),
        )
        document = ezdxf.new("R2007")
        document.units = 4  # PDF geometry and line widths were converted to mm.
        counts = write_result(
            document, output_path, [result], hole_area,
            clean_area(pdf.board), args,
        )
    except (ezdxf.DXFError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Input : {input_path}")
    print(f"Output: {output_path}")
    print("Units : millimetres (converted from PDF points)")
    print(
        f"PDF copper: {pdf.stroke_count} width-bearing strokes + "
        f"{pdf.fill_count} filled paths -> {len(result.track_polygons)} regions"
    )
    print("Stroke widths (mm, rounded from PDF points): " + ", ".join(
        f"{width:g}: {count}" for width, count in sorted(pdf.stroke_widths_mm.items())
    ))
    print(
        f"Written closed vectors: copper={counts[0]}, board={counts[3]}, "
        f"drill={counts[2]} ({pdf.hole_count} white drill shapes in PDF)"
    )
    if not pdf.hole_count:
        print("Warning: this PDF has no drill-hole geometry")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
