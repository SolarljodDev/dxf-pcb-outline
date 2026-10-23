using System.Globalization;
using System.Windows.Forms;

namespace PcbPdfOutline;

internal sealed record ConversionOptions(
    bool WriteHoles = true,
    bool WriteBoard = true,
    bool SubtractHoles = true,
    double CurveToleranceMm = 0.0005,
    double ArcToleranceMm = 0.0001);

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        // Explorer passes a PDF dropped onto the EXE as the sole argument.
        // With an argument, convert directly without opening the main window.
        if (args.Length == 0)
        {
            ApplicationConfiguration.Initialize();
            Application.Run(new MainForm());
            return 0;
        }

        try
        {
            if (args[0] is "--help" or "-h")
            {
                Console.WriteLine("Usage: PcbPdfOutline.exe input.pdf [--convert] [-o output.dxf] [--no-holes] [--no-board] [--no-subtract-holes] [--tolerance mm] [--arc-fit-tolerance mm]");
                return 0;
            }
            string input = Path.GetFullPath(args[0]);
            string? output = null;
            bool holes = true, board = true, subtract = true;
            double curveTolerance = 0.0005, arcTolerance = 0.0001;
            for (int i = 1; i < args.Length; i++)
            {
                switch (args[i])
                {
                    case "-o" or "--output":
                        output = Path.GetFullPath(args[++i]);
                        break;
                    case "--no-holes": holes = false; break;
                    case "--no-board": board = false; break;
                    case "--no-subtract-holes": subtract = false; break;
                    case "--convert": break;
                    case "--tolerance": curveTolerance = ParsePositive(args[++i]); break;
                    case "--arc-fit-tolerance": arcTolerance = ParsePositive(args[++i]); break;
                    default: throw new ArgumentException($"Unknown option: {args[i]}");
                }
            }
            output ??= Path.Combine(Path.GetDirectoryName(input)!,
                Path.GetFileNameWithoutExtension(input) + "_fromPDF.dxf");
            var result = Convert(input, output,
                new ConversionOptions(holes, board, subtract, curveTolerance, arcTolerance));
            Console.WriteLine(result);
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine("Error: " + error.Message);
            MessageBox.Show(error.Message, "Не удалось создать DXF",
                MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    private static double ParsePositive(string value)
    {
        double number = double.Parse(value, CultureInfo.InvariantCulture);
        if (!double.IsFinite(number) || number <= 0)
            throw new ArgumentException("Tolerance must be greater than zero.");
        return number;
    }

    internal static string Convert(string input, string output, ConversionOptions options)
    {
        if (!File.Exists(input)) throw new FileNotFoundException("PDF file not found.", input);
        if (!input.EndsWith(".pdf", StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException("Input must be a PDF file.");
        if (!output.EndsWith(".dxf", StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException("Output must be a DXF file.");
        var geometry = PdfGeometry.Read(input, options.CurveToleranceMm,
            subtractHoles: options.SubtractHoles);
        var result = DxfOutput.Write(geometry, output, options);
        return $"Saved: {output}\nCopper contours: {result.Copper}; holes: {result.Holes}; board contours: {result.Board}";
    }
}
