using System.Drawing;
using System.Windows.Forms;

namespace PcbPdfOutline;

internal sealed class MainForm : Form
{
    private readonly TextBox sourcePath = new() { Dock = DockStyle.Fill };
    private readonly TextBox outputPath = new() { Dock = DockStyle.Fill };
    private readonly TextBox sourceFilter = new() { Dock = DockStyle.Fill,
        PlaceholderText = "Поиск PDF по имени…" };
    private readonly TextBox outputFilter = new() { Dock = DockStyle.Fill,
        PlaceholderText = "Поиск DXF по имени…" };
    private readonly ListView sourceList = MakeList();
    private readonly ListView outputList = MakeList();
    private readonly Label chosenPdf = new() { Dock = DockStyle.Fill, AutoEllipsis = true,
        Text = "Выберите PDF в списке слева." };
    private readonly TextBox nameBox = new() { Dock = DockStyle.Fill, Text = "board_fromPDF.dxf" };
    private readonly CheckBox holesBox = new() { Text = "Контуры отверстий", Checked = true, AutoSize = true };
    private readonly CheckBox boardBox = new() { Text = "Контур платы", Checked = true, AutoSize = true };
    private readonly CheckBox subtractBox = new() { Text = "Вычитать отверстия из меди", Checked = true, AutoSize = true };
    private readonly Button convertButton = new() { Text = "Создать DXF", Width = 160, Height = 42 };
    private readonly Label status = new() { Text = "Выберите PDF слева и папку результата справа.",
        AutoSize = true };
    private readonly ProgressBar progress = new() { Style = ProgressBarStyle.Marquee,
        Dock = DockStyle.Fill, Height = 8, Visible = false };

    private string sourceFolder = "";
    private string resultFolder = "";
    private string? selectedPdf;
    private bool converting;

    private sealed record BrowserEntry(string Path, bool IsDirectory);

    internal MainForm()
    {
        Text = "PDF платы → контуры DXF для Aspire";
        MinimumSize = new Size(920, 620);
        Size = new Size(1080, 720);
        StartPosition = FormStartPosition.CenterScreen;
        Font = new Font("Segoe UI", 10);
        BackColor = Color.FromArgb(246, 248, 251);

        var root = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(18),
            ColumnCount = 2, RowCount = 4, BackColor = BackColor };
        root.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));
        root.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 52));
        root.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 68));
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 62));
        Controls.Add(root);

        var title = new Label { Text = "Контуры дорожек и заливки из PDF", Dock = DockStyle.Fill,
            Font = new Font(Font.FontFamily, 16, FontStyle.Bold),
            ForeColor = Color.FromArgb(28, 45, 65) };
        root.Controls.Add(title, 0, 0);
        root.SetColumnSpan(title, 2);

        root.Controls.Add(MakeBrowserPanel(true), 0, 1);
        root.Controls.Add(MakeBrowserPanel(false), 1, 1);

        var options = new FlowLayoutPanel { Dock = DockStyle.Fill,
            Padding = new Padding(8, 14, 8, 0), WrapContents = true };
        options.Controls.Add(new Label { Text = "Включить в результат:", AutoSize = true,
            Font = new Font(Font, FontStyle.Bold), Margin = new Padding(4, 4, 20, 8) });
        foreach (var checkbox in new[] { holesBox, boardBox, subtractBox })
        {
            checkbox.Margin = new Padding(8, 4, 16, 8);
            options.Controls.Add(checkbox);
        }
        root.Controls.Add(options, 0, 2);
        root.SetColumnSpan(options, 2);

        var footer = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 2 };
        footer.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        footer.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 170));
        footer.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        footer.RowStyles.Add(new RowStyle(SizeType.Absolute, 8));
        status.Anchor = AnchorStyles.Left;
        footer.Controls.Add(status, 0, 0);
        convertButton.Anchor = AnchorStyles.Right;
        convertButton.Click += ConvertClicked;
        footer.Controls.Add(convertButton, 1, 0);
        footer.Controls.Add(progress, 0, 1);
        footer.SetColumnSpan(progress, 2);
        root.Controls.Add(footer, 0, 3);
        root.SetColumnSpan(footer, 2);

        sourceList.SelectedIndexChanged += (_, _) =>
        {
            if (sourceList.SelectedItems.Count == 1 &&
                sourceList.SelectedItems[0].Tag is BrowserEntry { IsDirectory: false } entry)
                SelectPdf(entry.Path, false);
        };
        sourceList.ItemActivate += (_, _) => ActivateEntry(sourceList, true);
        outputList.ItemActivate += (_, _) => ActivateEntry(outputList, false);
        outputList.SelectedIndexChanged += (_, _) =>
        {
            if (outputList.SelectedItems.Count == 1 &&
                outputList.SelectedItems[0].Tag is BrowserEntry { IsDirectory: false } entry)
                nameBox.Text = Path.GetFileName(entry.Path);
        };
        sourcePath.KeyDown += (_, e) =>
        {
            if (e.KeyCode != Keys.Enter) return;
            e.SuppressKeyPress = true;
            Navigate(sourcePath.Text, true);
        };
        outputPath.KeyDown += (_, e) =>
        {
            if (e.KeyCode != Keys.Enter) return;
            e.SuppressKeyPress = true;
            Navigate(outputPath.Text, false);
        };
        sourceFilter.TextChanged += (_, _) =>
        {
            if (sourceFolder.Length > 0) Navigate(sourceFolder, true);
        };
        outputFilter.TextChanged += (_, _) =>
        {
            if (resultFolder.Length > 0) Navigate(resultFolder, false);
        };
        FormClosing += (_, e) =>
        {
            if (!converting) return;
            e.Cancel = true;
            MessageBox.Show(this, "Дождитесь завершения конвертации.", "Идёт обработка",
                MessageBoxButtons.OK, MessageBoxIcon.Information);
        };

        string downloads = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            "Downloads");
        if (!Directory.Exists(downloads)) downloads = Environment.GetFolderPath(
            Environment.SpecialFolder.MyDocuments);
        string projectResults = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "results"));
        Navigate(downloads, true);
        Navigate(Directory.Exists(projectResults) ? projectResults : downloads, false);
        RegisterDrop(this);
    }

    private Panel MakeBrowserPanel(bool source)
    {
        var shell = new Panel { Dock = DockStyle.Fill, Margin = new Padding(5),
            Padding = new Padding(12), BackColor = Color.White, BorderStyle = BorderStyle.FixedSingle };
        var content = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 5 };
        content.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        content.RowStyles.Add(new RowStyle(SizeType.Absolute, 36));
        content.RowStyles.Add(new RowStyle(SizeType.Absolute, 46));
        content.RowStyles.Add(new RowStyle(SizeType.Absolute, 38));
        content.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        content.RowStyles.Add(new RowStyle(SizeType.Absolute, 48));
        shell.Controls.Add(content);

        content.Controls.Add(new Label { Text = source ? "1  Исходный PDF" : "2  Папка результата",
            Dock = DockStyle.Fill, Font = new Font(Font, FontStyle.Bold) }, 0, 0);
        var nav = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 3, RowCount = 1 };
        nav.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 39));
        nav.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        nav.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 108));
        var up = new Button { Text = "↑", Dock = DockStyle.Fill };
        up.Click += (_, _) =>
        {
            string? parent = Directory.GetParent(source ? sourceFolder : resultFolder)?.FullName;
            if (parent != null) Navigate(parent, source);
        };
        nav.Controls.Add(up, 0, 0);
        nav.Controls.Add(source ? sourcePath : outputPath, 1, 0);
        var browse = new Button { Text = source ? "PDF…" : "Папка…", Dock = DockStyle.Fill };
        browse.Click += (_, _) =>
        {
            if (source) ChoosePdf(); else ChooseFolder();
        };
        nav.Controls.Add(browse, 2, 0);
        content.Controls.Add(nav, 0, 1);
        content.Controls.Add(source ? sourceFilter : outputFilter, 0, 2);
        content.Controls.Add(source ? sourceList : outputList, 0, 3);

        if (source)
        {
            chosenPdf.Padding = new Padding(3, 13, 0, 0);
            content.Controls.Add(chosenPdf, 0, 4);
        }
        else
        {
            var nameRow = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2,
                RowCount = 1 };
            nameRow.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 105));
            nameRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            nameRow.Controls.Add(new Label { Text = "Имя DXF:", Anchor = AnchorStyles.Left,
                AutoSize = true }, 0, 0);
            nameRow.Controls.Add(nameBox, 1, 0);
            content.Controls.Add(nameRow, 0, 4);
        }
        return shell;
    }

    private static ListView MakeList()
    {
        var list = new ListView { Dock = DockStyle.Fill, View = View.Details,
            FullRowSelect = true, HideSelection = false, MultiSelect = false,
            BorderStyle = BorderStyle.FixedSingle };
        list.Columns.Add("Имя", 260);
        list.Columns.Add("Тип", 85);
        list.Columns.Add("КБ", 70, HorizontalAlignment.Right);
        list.Resize += (_, _) => list.Columns[0].Width = Math.Max(120, list.ClientSize.Width - 160);
        return list;
    }

    private void Navigate(string folder, bool source)
    {
        try
        {
            folder = Path.GetFullPath(folder);
            if (!Directory.Exists(folder)) throw new DirectoryNotFoundException(folder);
            var directories = Directory.EnumerateDirectories(folder).OrderBy(x => x,
                StringComparer.CurrentCultureIgnoreCase).ToList();
            var files = Directory.EnumerateFiles(folder)
                .Where(x => x.EndsWith(source ? ".pdf" : ".dxf",
                    StringComparison.OrdinalIgnoreCase))
                .Where(x => Path.GetFileName(x).Contains(source ? sourceFilter.Text : outputFilter.Text,
                    StringComparison.CurrentCultureIgnoreCase))
                .OrderBy(x => x, StringComparer.CurrentCultureIgnoreCase).ToList();
            var list = source ? sourceList : outputList;
            list.BeginUpdate();
            try
            {
                list.Items.Clear();
                foreach (string path in files)
                {
                    long size = new FileInfo(path).Length;
                    list.Items.Add(new ListViewItem([Path.GetFileName(path), source ? "PDF" : "DXF",
                        Math.Ceiling(size / 1024.0).ToString("0")])
                    { Tag = new BrowserEntry(path, false) });
                }
                foreach (string path in directories)
                    list.Items.Add(new ListViewItem([Path.GetFileName(path), "Папка", ""])
                    { Tag = new BrowserEntry(path, true) });
            }
            finally { list.EndUpdate(); }
            if (source)
            {
                sourceFolder = folder;
                sourcePath.Text = folder;
                selectedPdf = null;
                chosenPdf.Text = "Выберите PDF в списке слева.";
            }
            else
            {
                resultFolder = folder;
                outputPath.Text = folder;
            }
            status.Text = source ? "Выберите PDF в левом списке." : "Папка результата: " + folder;
        }
        catch (Exception error)
        {
            MessageBox.Show(this, error.Message, "Не удалось открыть папку",
                MessageBoxButtons.OK, MessageBoxIcon.Error);
            if (source) sourcePath.Text = sourceFolder;
            else outputPath.Text = resultFolder;
        }
    }

    private void ActivateEntry(ListView list, bool source)
    {
        if (list.SelectedItems.Count != 1 || list.SelectedItems[0].Tag is not BrowserEntry entry)
            return;
        if (entry.IsDirectory) Navigate(entry.Path, source);
        else if (source) SelectPdf(entry.Path, false);
        else nameBox.Text = Path.GetFileName(entry.Path);
    }

    private void SelectPdf(string file, bool navigate)
    {
        if (!File.Exists(file) || !file.EndsWith(".pdf", StringComparison.OrdinalIgnoreCase)) return;
        if (navigate) Navigate(Path.GetDirectoryName(file)!, true);
        selectedPdf = file;
        chosenPdf.Text = "Выбран: " + Path.GetFileName(file);
        nameBox.Text = Path.GetFileNameWithoutExtension(file) + "_fromPDF.dxf";
        status.Text = "PDF выбран. Справа укажите папку результата.";
        if (!navigate) return;
        foreach (ListViewItem item in sourceList.Items)
            if (item.Tag is BrowserEntry entry && entry.Path.Equals(file,
                StringComparison.OrdinalIgnoreCase))
            { item.Selected = true; item.EnsureVisible(); break; }
    }

    private void ChoosePdf()
    {
        using var dialog = new OpenFileDialog { Title = "Выберите экспорт платы из EasyEDA",
            Filter = "PDF платы (*.pdf)|*.pdf|Все файлы (*.*)|*.*", InitialDirectory = sourceFolder };
        if (dialog.ShowDialog(this) == DialogResult.OK) SelectPdf(dialog.FileName, true);
    }

    private void ChooseFolder()
    {
        using var dialog = new FolderBrowserDialog { Description = "Куда сохранить готовый DXF",
            SelectedPath = resultFolder };
        if (dialog.ShowDialog(this) == DialogResult.OK) Navigate(dialog.SelectedPath, false);
    }

    private void RegisterDrop(Control control)
    {
        control.AllowDrop = true;
        control.DragEnter += (_, e) =>
        {
            string[]? files = e.Data?.GetData(DataFormats.FileDrop) as string[];
            e.Effect = files?.Any(IsPdfFile) == true ? DragDropEffects.Copy : DragDropEffects.None;
        };
        control.DragDrop += (_, e) =>
        {
            if (e.Data?.GetData(DataFormats.FileDrop) is string[] files)
            {
                string? pdf = files.FirstOrDefault(IsPdfFile);
                if (pdf != null) SelectPdf(pdf, true);
            }
        };
        foreach (Control child in control.Controls) RegisterDrop(child);
    }

    private static bool IsPdfFile(string path) =>
        path.EndsWith(".pdf", StringComparison.OrdinalIgnoreCase) && File.Exists(path);

    private async void ConvertClicked(object? sender, EventArgs e)
    {
        if (selectedPdf == null || !File.Exists(selectedPdf) || !Directory.Exists(resultFolder))
        {
            MessageBox.Show(this, "Выберите PDF слева и папку результата справа.",
                "Не все поля заполнены", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }
        string name = nameBox.Text.Trim();
        if (!name.EndsWith(".dxf", StringComparison.OrdinalIgnoreCase)) name += ".dxf";
        if (name != Path.GetFileName(name) || name.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0)
        {
            MessageBox.Show(this, "Укажите только имя файла DXF без пути.", "Неверное имя",
                MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }
        string output = Path.Combine(resultFolder, name);
        string generatedName = Path.GetFileNameWithoutExtension(selectedPdf) + "_fromPDF.dxf";
        bool mayOverwriteGenerated = name.Equals(generatedName, StringComparison.OrdinalIgnoreCase);
        if (File.Exists(output) && !mayOverwriteGenerated &&
            MessageBox.Show(this, $"Файл уже существует:\n{output}\n\nЗаменить?",
            "Подтверждение", MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return;

        string input = selectedPdf;
        var options = new ConversionOptions(holesBox.Checked, boardBox.Checked, subtractBox.Checked);
        converting = true;
        convertButton.Enabled = false;
        progress.Visible = true;
        status.Text = "Читаю PDF и строю контуры…";
        try
        {
            string result = await Task.Run(() => Program.Convert(input, output, options));
            status.Text = "Готово: " + Path.GetFileName(output);
            Navigate(resultFolder, false);
            MessageBox.Show(this, result + "\n\nПроверьте контуры перед фрезеровкой.", "DXF готов",
                MessageBoxButtons.OK, MessageBoxIcon.Information);
        }
        catch (Exception error)
        {
            status.Text = "Ошибка конвертации.";
            MessageBox.Show(this, error.Message, "Не удалось создать DXF",
                MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
        finally
        {
            progress.Visible = false;
            convertButton.Enabled = true;
            converting = false;
        }
    }
}
