using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace VerseLab;

internal sealed class LyricSearch : Window
{
    private readonly TextBox title;
    private readonly TextBox artist;
    private readonly TextBlock status;
    private readonly ListBox results;
    private readonly Func<string,string,Task<JsonElement>> search;
    private readonly double duration;
    public string? SelectedLyrics { get; private set; }

    public LyricSearch(Window owner, string song, string performer, double length,
                       Func<string,string,Task<JsonElement>> lookup)
    {
        Owner = owner;
        Title = "Find lyrics";
        WindowStartupLocation = WindowStartupLocation.CenterOwner;
        Width = 640;
        Height = 470;
        MinWidth = 480;
        MinHeight = 320;
        Background = owner.Background;
        Foreground = Brushes.White;
        search = lookup;
        duration = length;
        var root = new DockPanel { Margin = new Thickness(16) };
        var fields = new StackPanel();
        DockPanel.SetDock(fields, Dock.Top);
        root.Children.Add(fields);
        fields.Children.Add(new TextBlock { Text = "Song title" });
        title = new TextBox { Text = song };
        fields.Children.Add(title);
        fields.Children.Add(new TextBlock { Text = "Artist", Margin = new Thickness(0,6,0,0) });
        artist = new TextBox { Text = performer };
        fields.Children.Add(artist);
        var buttons = new WrapPanel { Margin = new Thickness(0,8,0,0) };
        fields.Children.Add(buttons);
        var find = new Button { Content = "Search", IsDefault = true };
        find.Click += async (_, _) => await Search(find);
        buttons.Children.Add(find);
        var use = new Button { Content = "Use selected lyrics", IsEnabled = false };
        use.Click += (_, _) => Choose();
        buttons.Children.Add(use);
        status = new TextBlock { TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0,0,0,6) };
        fields.Children.Add(status);
        results = new ListBox { DisplayMemberPath = nameof(Candidate.Label) };
        results.SelectionChanged += (_, _) => use.IsEnabled = results.SelectedItem is Candidate;
        results.MouseDoubleClick += (_, _) => Choose();
        root.Children.Add(results);
        Content = root;
    }

    private async Task Search(Button button)
    {
        button.IsEnabled = false;
        results.ItemsSource = null;
        status.Text = "Searching lyric versions…";
        try
        {
            var rows = await search(artist.Text.Trim(), title.Text.Trim());
            var matches = rows.Items().Where(row => row.String("syncedLyrics").Length > 0 ||
                                                  row.String("plainLyrics").Length > 0)
                .OrderBy(row => Math.Abs(row.Number("duration", 0) - duration))
                .Select(row => new Candidate(
                    $"{row.String("trackName")} · {row.String("artistName")} · " +
                    $"{Math.Floor(row.Number("duration", 0)):0}s · {(row.String("syncedLyrics").Length > 0 ? "timed" : "text")}",
                    row.String("syncedLyrics", row.String("plainLyrics")))).ToList();
            results.ItemsSource = matches;
            status.Text = matches.Count > 0 ? $"{matches.Count} versions found. Compare the recording lengths."
                : "No lyrics found. Try a shorter title or a different artist.";
        }
        catch (Exception error) { status.Text = error.Message; }
        finally { button.IsEnabled = true; }
    }

    private void Choose()
    {
        if (results.SelectedItem is Candidate choice)
        {
            SelectedLyrics = choice.Raw;
            DialogResult = true;
        }
    }

    private sealed record Candidate(string Label, string Raw);
}
