"""Vector PDF copper extraction, including stroke widths and fill voids."""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

import ezdxf
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject
from shapely import affinity
from shapely.geometry import Point

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dxf_to_aspire as converter
from pdf_geometry import MM_PER_POINT, _filled_area, read_easyeda_pdf


def make_pdf(
    path: Path, bridge_width_pt: float,
    with_drill: bool = False, with_rgb_annotation: bool = False,
    transform: str | None = None,
) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=10, height=10)
    content = f"""
        0 G 0.2 w
        1 1 m 9 1 l S
        9 1 m 9 9 l S
        9 9 m 1 9 l S
        1 9 m 1 1 l S
        0 g 0 G 0.58 w
        1.5 1.5 m 8.5 1.5 l 8.5 8.5 l 1.5 8.5 l h
        4 4 m 4 6 l 6 6 l 6 4 l h
        B
        {bridge_width_pt} w 1 J 5.5 5 m 6.5 5 l S
    """
    if with_drill:
        content += """
            1 g 5.2 4.7 m 5.8 4.7 l 5.8 5.3 l 5.2 5.3 l h f
            0 g 5.4 4.9 m 5.6 4.9 l 5.6 5.1 l 5.4 5.1 l h f
        """
    if with_rgb_annotation:
        content += """
            0.6 0.4 1 RG 0.6 0.4 1 rg
            2 w 2 2 m 3 3 l S
            2 2 m 3 2 l 3 3 l 2 3 l h f
        """
    if transform:
        content = f"q {transform} cm {content} Q"
    stream = DecodedStreamObject()
    stream.set_data(content.encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


class PdfConversionTest(unittest.TestCase):
    def test_default_output_uses_from_pdf_marker_and_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "board.pdf"
            output = Path(temp) / "board_fromPDF.dxf"
            unrelated = Path(temp) / "board.dxf"
            make_pdf(source, 2.0)
            unrelated.write_bytes(b"original EasyEDA DXF")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(converter.main([str(source)]), 0)
            self.assertTrue(output.is_file())
            self.assertFalse((Path(temp) / "board_aspire_tracks.dxf").exists())
            output.write_bytes(b"previous result")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(converter.main([str(source)]), 0)
            self.assertNotEqual(output.read_bytes(), b"previous result")
            self.assertEqual(unrelated.read_bytes(), b"original EasyEDA DXF")

    def test_pdf_stroke_width_and_compound_fill(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "board.pdf"
            make_pdf(path, 2.0)
            result = read_easyeda_pdf(path, 0.0005, 0.1)

            def at(x: float, y: float) -> Point:
                return Point(x * MM_PER_POINT, y * MM_PER_POINT)

            self.assertEqual(result.stroke_count, 2)
            self.assertEqual(result.stroke_widths_mm[round(2 * MM_PER_POINT, 4)], 1)
            self.assertEqual(result.stroke_widths_mm[round(0.58 * MM_PER_POINT, 4)], 1)
            self.assertTrue(result.board.covers(at(1, 1)))
            self.assertTrue(result.copper.covers(at(3, 5)))
            self.assertTrue(result.copper.covers(at(4.2, 5)))
            self.assertFalse(result.copper.covers(at(4.4, 5)))
            self.assertTrue(result.copper.covers(at(5.5, 5)))
            self.assertTrue(result.copper.covers(at(1.4, 3)))
            self.assertFalse(result.copper.covers(at(1.1, 3)))

    def test_changed_pdf_width_changes_copper_and_cli_writes_closed_vectors(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            wide = Path(temp) / "wide.pdf"
            narrow = Path(temp) / "narrow.pdf"
            output = Path(temp) / "aspire.dxf"
            make_pdf(wide, 2.0)
            make_pdf(narrow, 1.0)
            wide_area = read_easyeda_pdf(wide, 0.0005, 0.1).copper
            narrow_area = read_easyeda_pdf(narrow, 0.0005, 0.1).copper
            self.assertGreater(wide_area.area, narrow_area.area)

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(converter.main([str(wide), "-o", str(output)]), 0)
            document = ezdxf.readfile(output)
            self.assertEqual(document.units, 4)
            self.assertEqual(len(document.audit().errors), 0)
            self.assertEqual(
                {entity.dxf.layer for entity in document.modelspace()},
                {"COPPER_OUTLINE_Top-Layer", "BOARD_OUTLINE"},
            )
            self.assertTrue(all(
                entity.dxftype() == "LWPOLYLINE" and entity.closed and not entity.has_width
                for entity in document.modelspace()
            ))

    def test_white_drill_cutout_is_exported_and_black_mark_is_not_copper(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "drilled.pdf"
            output = Path(temp) / "drilled.dxf"
            make_pdf(source, 2.0, with_drill=True)
            result = read_easyeda_pdf(source, 0.0005, 0.1)
            center = Point(5.5 * MM_PER_POINT, 5 * MM_PER_POINT)

            self.assertEqual(result.hole_count, 1)
            self.assertEqual(result.fill_count, 1)
            self.assertTrue(result.holes.covers(center))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(converter.main([str(source), "-o", str(output)]), 0)
            document = ezdxf.readfile(output)
            self.assertEqual(len(document.modelspace().query("LWPOLYLINE[layer=='DRILL_OUTLINE']")), 1)
            self.assertEqual(len(document.audit().errors), 0)

    def test_rgb_annotations_are_not_copper(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            plain = Path(temp) / "plain.pdf"
            annotated = Path(temp) / "annotated.pdf"
            make_pdf(plain, 2.0)
            make_pdf(annotated, 2.0, with_rgb_annotation=True)
            first = read_easyeda_pdf(plain, 0.0005, 0.1)
            second = read_easyeda_pdf(annotated, 0.0005, 0.1)
            self.assertEqual(first.stroke_count, second.stroke_count)
            self.assertAlmostEqual(first.copper.symmetric_difference(second.copper).area, 0)

    def test_mirrored_pdf_transforms_fill_strokes_and_drill_holes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            plain = Path(temp) / "plain.pdf"
            mirrored = Path(temp) / "mirrored.pdf"
            make_pdf(plain, 2.0, with_drill=True)
            make_pdf(mirrored, 2.0, with_drill=True, transform="-1 0 0 1 10 0")
            original = read_easyeda_pdf(plain, 0.0005, 0.1)
            reflected = read_easyeda_pdf(mirrored, 0.0005, 0.1)
            matrix = [-1, 0, 0, 1, 10 * MM_PER_POINT, 0]
            for name in ("copper", "board", "holes"):
                expected = affinity.affine_transform(getattr(original, name), matrix)
                actual = getattr(reflected, name)
                self.assertLess(expected.symmetric_difference(actual).area, 1e-9)
            self.assertEqual(original.stroke_widths_mm, reflected.stroke_widths_mm)

    def test_self_intersecting_compound_fill_uses_pdf_winding_rule(self) -> None:
        outer = [(0, 0), (10, 0), (10, 10), (0, 10)]
        crossing = [(2, 2), (8, 8), (2, 8), (8, 2)]
        filled = _filled_area([outer, crossing])
        self.assertTrue(filled.is_valid)
        self.assertGreater(filled.area, 0)
        self.assertLess(filled.area, 100)

    def test_dxf_input_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "board.dxf"
            source.write_text("not read", encoding="ascii")
            error = io.StringIO()
            with contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as raised:
                converter.main([str(source)])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("PDF", error.getvalue())


if __name__ == "__main__":
    unittest.main()
