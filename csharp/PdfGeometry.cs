using NetTopologySuite.Geometries;
using NetTopologySuite.Geometries.Utilities;
using NetTopologySuite.Operation.Buffer;
using NetTopologySuite.Operation.Polygonize;
using NetTopologySuite.Operation.Union;
using UglyToad.PdfPig;
using UglyToad.PdfPig.Core;
using UglyToad.PdfPig.Graphics;
using UglyToad.PdfPig.Graphics.Colors;

namespace PcbPdfOutline;

internal sealed record BoardGeometry(
    Geometry Copper, Geometry Board, Geometry Holes,
    int StrokeCount, int FillCount, int HoleCount,
    SortedDictionary<double, int> Widths);

internal static class PdfGeometry
{
    internal const double MmPerPoint = 25.4 / 72.0;
    private static readonly GeometryFactory Factory = new();

    internal static BoardGeometry Read(string file, double toleranceMm = 0.0005,
        double minTrackWidthMm = 0.1, double joinToleranceMm = 0.001,
        bool subtractHoles = true)
    {
        using var document = PdfDocument.Open(file);
        if (document.NumberOfPages != 1)
            throw new InvalidDataException("PDF must contain exactly one PCB page.");
        var paths = document.GetPage(1).Paths.ToArray();
        int firstCopper = Array.FindIndex(paths,
            p => p.IsFilled && ColorClass(p.FillColor) == 0);
        if (firstCopper < 0)
            throw new InvalidDataException("No filled black copper found in the PDF.");

        var boardLines = paths.Take(firstCopper)
            .Where(p => p.IsStroked && ColorClass(p.StrokeColor) == 0)
            .SelectMany(p => Flatten(p, toleranceMm))
            .Where(p => p.Count >= 2)
            .Select(p => (Geometry)Factory.CreateLineString(p.ToArray()))
            .ToList();
        var boardFaces = Polygonize(boardLines);
        if (boardFaces.Count != 1)
            throw new InvalidDataException("Could not identify one board outline in the PDF.");
        Geometry board = boardFaces[0];

        var copperParts = new List<Geometry>();
        var blackFills = new List<Geometry>();
        var whiteFills = new List<Geometry>();
        var widths = new SortedDictionary<double, int>();
        int strokes = 0;

        foreach (var path in paths.Skip(firstCopper))
        {
            var lines = Flatten(path, toleranceMm);
            if (path.IsFilled)
            {
                Geometry area = FilledArea(lines);
                if (!area.IsEmpty)
                {
                    int color = ColorClass(path.FillColor);
                    if (color == 0) blackFills.Add(area);
                    else if (color == 1) whiteFills.Add(area);
                }
            }
            if (path.IsStroked && ColorClass(path.StrokeColor) == 0)
            {
                double width = path.LineWidth * MmPerPoint;
                if (width < minTrackWidthMm && !path.IsFilled) continue;
                Geometry area = StrokeArea(path, lines, width);
                if (area.IsEmpty) continue;
                copperParts.Add(area);
                strokes++;
                double key = Math.Round(width, 4);
                widths[key] = widths.GetValueOrDefault(key) + 1;
            }
        }

        Geometry holes = Union(whiteFills);
        int fills = 0;
        foreach (Geometry area in blackFills)
        {
            // Some EasyEDA PDFs redraw a black drafting mark inside a white
            // drill hole. It is not a tiny island of physical copper.
            if (!holes.IsEmpty && area.Difference(holes).Area <= 1e-8)
                continue;
            copperParts.Add(area);
            fills++;
        }
        if (strokes == 0 || copperParts.Count == 0)
            throw new InvalidDataException("PDF has no usable width-bearing copper strokes.");

        Geometry copper = Clean(Union(copperParts));
        if (joinToleranceMm > 0 && !copper.IsEmpty)
        {
            var parameters = new BufferParameters(4)
            {
                JoinStyle = JoinStyle.Mitre,
                EndCapStyle = EndCapStyle.Flat
            };
            copper = Clean(copper.Buffer(joinToleranceMm, parameters)
                .Buffer(-joinToleranceMm, parameters));
        }
        if (subtractHoles && !holes.IsEmpty) copper = Clean(copper.Difference(holes));
        return new BoardGeometry(copper, board, holes, strokes, fills,
            whiteFills.Count, widths);
    }

    internal static List<Polygon> Polygons(Geometry geometry)
    {
        var result = new List<Polygon>();
        if (geometry is Polygon polygon) result.Add(polygon);
        else if (geometry is GeometryCollection collection)
            foreach (Geometry part in collection.Geometries)
                result.AddRange(Polygons(part));
        return result;
    }

    private static Geometry Clean(Geometry geometry)
    {
        if (!geometry.IsValid) geometry = GeometryFixer.Fix(geometry);
        return Union(Polygons(geometry).Cast<Geometry>().ToList());
    }

    private static Geometry Union(IEnumerable<Geometry> pieces)
    {
        Geometry[] items = pieces.Where(g => !g.IsEmpty).ToArray();
        return items.Length == 0 ? Factory.CreateGeometryCollection()
            : UnaryUnionOp.Union(items);
    }

    private static int ColorClass(IColor? color)
    {
        if (color is null) return -1;
        var (r, g, b) = color.ToRGBValues();
        if (Math.Max(r, Math.Max(g, b)) <= 0.1) return 0;
        if (Math.Min(r, Math.Min(g, b)) >= 0.9) return 1;
        return -1;
    }

    private static List<List<Coordinate>> Flatten(PdfPath path, double toleranceMm)
    {
        var result = new List<List<Coordinate>>();
        foreach (PdfSubpath subpath in path)
        {
            var points = new List<Coordinate>();
            foreach (var command in subpath.Commands)
            {
                switch (command)
                {
                    case PdfSubpath.Move move:
                        Add(points, move.Location);
                        break;
                    case PdfSubpath.Line line:
                        Add(points, line.To);
                        break;
                    case PdfSubpath.CubicBezierCurve curve:
                        FlattenCubic(points, ToMm(curve.StartPoint),
                            ToMm(curve.FirstControlPoint), ToMm(curve.SecondControlPoint),
                            ToMm(curve.EndPoint), toleranceMm, 0);
                        break;
                    case PdfSubpath.Close:
                        if (points.Count > 0) Add(points, points[0]);
                        break;
                    default:
                        throw new InvalidDataException($"Unsupported PDF path command: {command.GetType().Name}");
                }
            }
            if (points.Count >= 2) result.Add(points);
        }
        return result;
    }

    private static Coordinate ToMm(PdfPoint point) =>
        new(point.X * MmPerPoint, point.Y * MmPerPoint);

    private static void Add(List<Coordinate> points, PdfPoint point) =>
        Add(points, ToMm(point));

    private static void Add(List<Coordinate> points, Coordinate point)
    {
        if (points.Count == 0 || !points[^1].Equals2D(point))
            points.Add(point);
    }

    private static double ChordDistance(Coordinate point, Coordinate a, Coordinate b)
    {
        double dx = b.X - a.X, dy = b.Y - a.Y;
        double length = Math.Sqrt(dx * dx + dy * dy);
        return length < 1e-12 ? point.Distance(a)
            : Math.Abs(dx * (a.Y - point.Y) - dy * (a.X - point.X)) / length;
    }

    private static Coordinate Mid(Coordinate a, Coordinate b) =>
        new((a.X + b.X) / 2, (a.Y + b.Y) / 2);

    private static void FlattenCubic(List<Coordinate> output, Coordinate a,
        Coordinate b, Coordinate c, Coordinate d, double tolerance, int depth)
    {
        if (depth >= 16 || Math.Max(ChordDistance(b, a, d), ChordDistance(c, a, d)) <= tolerance)
        {
            Add(output, d);
            return;
        }
        var ab = Mid(a, b); var bc = Mid(b, c); var cd = Mid(c, d);
        var abc = Mid(ab, bc); var bcd = Mid(bc, cd); var center = Mid(abc, bcd);
        FlattenCubic(output, a, ab, abc, center, tolerance, depth + 1);
        FlattenCubic(output, center, bcd, cd, d, tolerance, depth + 1);
    }

    private static Geometry FilledArea(List<List<Coordinate>> paths)
    {
        var loops = paths.Where(p => p.Count >= 3).ToList();
        if (loops.Count == 0) return Factory.CreateGeometryCollection();
        if (loops.Count == 1)
        {
            var polygon = Factory.CreatePolygon(Close(loops[0]));
            if (polygon.IsValid) return polygon;
        }
        var lines = loops.Select(p => (Geometry)Factory.CreateLineString(Close(p))).ToList();
        var faces = Polygonize(lines);
        var selected = new List<Geometry>();
        foreach (Polygon face in faces)
        {
            Coordinate point = face.InteriorPoint.Coordinate;
            if (loops.Sum(loop => WindingNumber(loop, point)) != 0)
                selected.Add(face);
        }
        return Union(selected);
    }

    private static List<Polygon> Polygonize(List<Geometry> lines)
    {
        var polygonizer = new Polygonizer();
        polygonizer.Add(Union(lines));
        return polygonizer.GetPolygons().Cast<Polygon>().ToList();
    }

    private static Coordinate[] Close(List<Coordinate> points) =>
        points[0].Equals2D(points[^1]) ? points.ToArray() : [.. points, points[0]];

    private static int WindingNumber(List<Coordinate> path, Coordinate point)
    {
        int winding = 0;
        for (int i = 0; i < path.Count; i++)
        {
            Coordinate a = path[i], b = path[(i + 1) % path.Count];
            double cross = (b.X - a.X) * (point.Y - a.Y)
                - (point.X - a.X) * (b.Y - a.Y);
            if (a.Y <= point.Y && point.Y < b.Y && cross > 0) winding++;
            else if (b.Y <= point.Y && point.Y < a.Y && cross < 0) winding--;
        }
        return winding;
    }

    private static Geometry StrokeArea(PdfPath path, List<List<Coordinate>> paths, double width)
    {
        if (width <= 0) return Factory.CreateGeometryCollection();
        var parameters = new BufferParameters(64)
        {
            EndCapStyle = path.LineCapStyle.ToString() switch
            {
                "Round" => EndCapStyle.Round,
                "ProjectingSquare" or "Square" => EndCapStyle.Square,
                _ => EndCapStyle.Flat
            },
            JoinStyle = path.LineJoinStyle.ToString() switch
            {
                "Round" => JoinStyle.Round,
                "Bevel" => JoinStyle.Bevel,
                _ => JoinStyle.Mitre
            }
        };
        return Union(paths.Where(p => p.Count >= 2).Select(p =>
            (Geometry)Factory.CreateLineString(p.ToArray()).Buffer(width / 2, parameters)));
    }
}
