using NetTopologySuite.Algorithm.Distance;
using NetTopologySuite.Geometries;
using netDxf;
using netDxf.Entities;
using netDxf.Header;
using netDxf.Tables;
using netDxf.Units;

namespace PcbPdfOutline;

internal sealed record OutputCounts(int Copper, int Holes, int Board);

internal static class DxfOutput
{
    private static readonly GeometryFactory Factory = new();

    internal static OutputCounts Write(BoardGeometry source, string output, ConversionOptions options)
    {
        var document = new DxfDocument(DxfVersion.AutoCad2007);
        document.DrawingVariables.InsUnits = DrawingUnits.Millimeters;
        var copperLayer = new Layer("COPPER_OUTLINE_Top-Layer") { Color = new AciColor(1) };
        var holeLayer = new Layer("DRILL_OUTLINE") { Color = new AciColor(8) };
        var boardLayer = new Layer("BOARD_OUTLINE") { Color = new AciColor(6) };
        int copperCount = 0, holeCount = 0, boardCount = 0;

        foreach (Polygon region in PdfGeometry.Polygons(source.Copper))
        {
            if (AddRing(document, region.ExteriorRing, copperLayer, options.ArcToleranceMm)) copperCount++;
            foreach (LineString interior in region.InteriorRings)
            {
                var voidArea = Factory.CreatePolygon((LinearRing)interior);
                bool isDrill = !source.Holes.IsEmpty && voidArea.Area > 0
                    && voidArea.Intersection(source.Holes).Area / voidArea.Area > 0.5;
                if (!isDrill && AddRing(document, interior, copperLayer, options.ArcToleranceMm)) copperCount++;
            }
        }
        if (options.WriteHoles)
            foreach (Polygon hole in PdfGeometry.Polygons(source.Holes))
                if (AddRing(document, hole.ExteriorRing, holeLayer, options.ArcToleranceMm)) holeCount++;
        if (options.WriteBoard)
            foreach (Polygon board in PdfGeometry.Polygons(source.Board))
            {
                if (AddRing(document, board.ExteriorRing, boardLayer, options.ArcToleranceMm)) boardCount++;
                foreach (LineString interior in board.InteriorRings)
                    if (AddRing(document, interior, boardLayer, options.ArcToleranceMm)) boardCount++;
            }

        string target = Path.GetFullPath(output);
        string directory = Path.GetDirectoryName(target)!;
        Directory.CreateDirectory(directory);
        string temporary = Path.Combine(directory,
            $".{Path.GetFileNameWithoutExtension(target)}.{Guid.NewGuid():N}.tmp.dxf");
        try
        {
            if (!document.Save(temporary)) throw new IOException("DXF could not be saved.");
            File.Move(temporary, target, true);
        }
        finally
        {
            if (File.Exists(temporary)) File.Delete(temporary);
        }
        return new OutputCounts(copperCount, holeCount, boardCount);
    }

    private static bool AddRing(DxfDocument document, LineString ring, Layer layer, double tolerance)
    {
        var points = ring.Coordinates.ToList();
        if (points.Count > 1 && points[0].Equals2D(points[^1])) points.RemoveAt(points.Count - 1);
        for (int i = points.Count - 1; i > 0; i--)
            if (points[i].Equals2D(points[i - 1])) points.RemoveAt(i);
        if (points.Count < 3) return false;

        List<(Coordinate Point, double Bulge)> fitted = [];
        foreach (double trial in new[] { tolerance, tolerance / 10, tolerance / 100 })
        {
            fitted = Fit(points, trial);
            if (IsFaithful(points, fitted, Math.Max(tolerance, 0.001))) break;
        }
        if (!IsFaithful(points, fitted, Math.Max(tolerance, 0.001)))
            fitted = points.Select(p => (p, 0.0)).ToList();
        var vertices = fitted.Select(p => new Polyline2DVertex(p.Point.X, p.Point.Y, p.Bulge));
        document.Entities.Add(new Polyline2D(vertices, true) { Layer = layer });
        return true;
    }

    private static List<(Coordinate Point, double Bulge)> Fit(List<Coordinate> points, double tolerance)
    {
        int count = points.Count;
        if (count >= 16)
        {
            double cx = points.Average(p => p.X), cy = points.Average(p => p.Y);
            double radius = points.Average(p => Hypot(p.X - cx, p.Y - cy));
            double maxError = points.Max(p => Math.Abs(Hypot(p.X - cx, p.Y - cy) - radius));
            double signedArea = SignedArea(points);
            if (radius > 0 && maxError <= Math.Max(tolerance, 0.001)
                && Math.Abs(Math.PI * radius * radius - Math.Abs(signedArea))
                <= Math.Max(0.02 * radius * radius, tolerance * radius * 8))
            {
                var first = points[0];
                double sign = signedArea > 0 ? 1 : -1;
                return [(first, sign), (new Coordinate(2 * cx - first.X, 2 * cy - first.Y), sign)];
            }
        }

        int start = Enumerable.Range(0, count).MaxBy(i => CornerStrength(points, i));
        var ordered = points.Skip(start).Concat(points.Take(start)).ToList();
        ordered.Add(ordered[0]);
        var result = new List<(Coordinate, double)>();
        int index = 0;
        while (index < count)
        {
            int lineEnd = index + 1;
            for (int end = index + 2; end <= count; end++)
            {
                if (Enumerable.Range(index + 1, end - index - 1)
                    .All(k => LineDistance(ordered[k], ordered[index], ordered[end]) <= tolerance))
                    lineEnd = end;
                else break;
            }
            int arcEnd = index;
            double arcSweep = 0;
            foreach (var seed in new[] { (Middle: 2, End: 4, MinEdges: 4, MinSweep: 5.0),
                                         (Middle: 1, End: 3, MinEdges: 3, MinSweep: 0.25) })
            {
                if (index + seed.End > count ||
                    !Circle(ordered[index], ordered[index + seed.Middle], ordered[index + seed.End],
                        out double cx, out double cy, out double radius) || radius < 0.01 || radius > 1000)
                    continue;
                double previous = Math.Atan2(ordered[index].Y - cy, ordered[index].X - cx);
                double sweep = 0;
                int direction = 0;
                for (int end = index + 1; end <= count; end++)
                {
                    var p = ordered[end];
                    if (Math.Abs(Hypot(p.X - cx, p.Y - cy) - radius) > tolerance) break;
                    double angle = Math.Atan2(p.Y - cy, p.X - cx);
                    double delta = angle - previous;
                    while (delta <= -Math.PI) delta += 2 * Math.PI;
                    while (delta > Math.PI) delta -= 2 * Math.PI;
                    if (Math.Abs(delta) < 1e-9) continue;
                    if (radius * (1 - Math.Cos(Math.Abs(delta) / 2)) > Math.Max(tolerance, 0.001)) break;
                    int sign = delta > 0 ? 1 : -1;
                    if (direction != 0 && sign != direction) break;
                    if (Math.Abs(sweep + delta) > Math.PI + 1e-6) break;
                    direction = sign;
                    sweep += delta;
                    previous = angle;
                    if (end - index >= seed.MinEdges && Math.Abs(sweep) >= seed.MinSweep * Math.PI / 180
                        && end > arcEnd)
                    { arcEnd = end; arcSweep = sweep; }
                }
            }
            if (arcEnd - index >= 3 && arcEnd >= lineEnd)
            { result.Add((ordered[index], Math.Tan(arcSweep / 4))); index = arcEnd; }
            else
            { result.Add((ordered[index], 0)); index = lineEnd; }
        }
        return result;
    }

    private static bool IsFaithful(List<Coordinate> source, List<(Coordinate Point, double Bulge)> fit,
        double tolerance)
    {
        if (fit.Count < 2) return false;
        var flattened = new List<Coordinate>();
        for (int i = 0; i < fit.Count; i++)
        {
            var a = fit[i].Point;
            var b = fit[(i + 1) % fit.Count].Point;
            double bulge = fit[i].Bulge;
            if (flattened.Count == 0) flattened.Add(a);
            if (Math.Abs(bulge) > 1e-10)
            {
                double chord = a.Distance(b);
                if (chord < 1e-12) return false;
                double radius = chord * (1 + bulge * bulge) / (4 * Math.Abs(bulge));
                double offset = chord * (1 - bulge * bulge) / (4 * bulge);
                double cx = (a.X + b.X) / 2 - (b.Y - a.Y) * offset / chord;
                double cy = (a.Y + b.Y) / 2 + (b.X - a.X) * offset / chord;
                double start = Math.Atan2(a.Y - cy, a.X - cx);
                double sweep = 4 * Math.Atan(bulge);
                double maxStep = radius > 0.00005 ?
                    2 * Math.Acos(Math.Max(-1, 1 - 0.00005 / radius)) : Math.PI / 8;
                int steps = Math.Clamp((int)Math.Ceiling(Math.Abs(sweep) / maxStep), 2, 8192);
                for (int j = 1; j < steps; j++)
                {
                    double angle = start + sweep * j / steps;
                    flattened.Add(new Coordinate(cx + radius * Math.Cos(angle),
                        cy + radius * Math.Sin(angle)));
                }
            }
            flattened.Add(b);
        }
        try
        {
            var original = Factory.CreatePolygon([.. source, source[0]]);
            var candidate = Factory.CreatePolygon(flattened.ToArray());
            return candidate.IsValid && Math.Abs(candidate.Area - original.Area)
                <= Math.Max(0.01 * original.Area, 1e-5)
                && DiscreteHausdorffDistance.Distance(original.Boundary, candidate.Boundary) <= tolerance;
        }
        catch { return false; }
    }

    private static double SignedArea(List<Coordinate> points)
    {
        double sum = 0;
        for (int i = 0; i < points.Count; i++)
        {
            var a = points[i]; var b = points[(i + 1) % points.Count];
            sum += a.X * b.Y - b.X * a.Y;
        }
        return sum / 2;
    }

    private static double CornerStrength(List<Coordinate> p, int i)
    {
        int n = p.Count;
        var a = p[(i + n - 1) % n]; var b = p[i]; var c = p[(i + 1) % n];
        double ux = b.X - a.X, uy = b.Y - a.Y, vx = c.X - b.X, vy = c.Y - b.Y;
        return Math.Abs(Math.Atan2(ux * vy - uy * vx, ux * vx + uy * vy));
    }

    private static double LineDistance(Coordinate p, Coordinate a, Coordinate b)
    {
        double dx = b.X - a.X, dy = b.Y - a.Y, length = Hypot(dx, dy);
        return length < 1e-12 ? p.Distance(a) :
            Math.Abs(dx * (a.Y - p.Y) - dy * (a.X - p.X)) / length;
    }

    private static bool Circle(Coordinate a, Coordinate b, Coordinate c,
        out double cx, out double cy, out double radius)
    {
        double d = 2 * (a.X * (b.Y - c.Y) + b.X * (c.Y - a.Y) + c.X * (a.Y - b.Y));
        cx = cy = radius = 0;
        if (Math.Abs(d) < 1e-12) return false;
        double aa = a.X * a.X + a.Y * a.Y, bb = b.X * b.X + b.Y * b.Y,
            cc = c.X * c.X + c.Y * c.Y;
        cx = (aa * (b.Y - c.Y) + bb * (c.Y - a.Y) + cc * (a.Y - b.Y)) / d;
        cy = (aa * (c.X - b.X) + bb * (a.X - c.X) + cc * (b.X - a.X)) / d;
        radius = Hypot(a.X - cx, a.Y - cy);
        return true;
    }

    private static double Hypot(double x, double y) => Math.Sqrt(x * x + y * y);
}
