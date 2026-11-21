using System.ComponentModel;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using Microsoft.Win32;

namespace VerseLab;

public sealed record LibraryTrack(string Key, string Name);

public partial class MainWindow : Window
{
    private const int PipelineVersion = 6;
    private const string AudioFiles = PlayerWindow.AudioFilter;
    private static readonly Brush UpcomingWord = new SolidColorBrush(Color.FromRgb(158, 140, 176));
    private static readonly Brush SungWord = new SolidColorBrush(Color.FromRgb(232, 210, 255));
    private readonly DispatcherTimer clock = new() { Interval = TimeSpan.FromMilliseconds(85) };
    private readonly DispatcherTimer statusTimer = new() { Interval = TimeSpan.FromSeconds(1) };
    private Backend? backend;
    private MediaPlayer? audio;
    private List<LibraryTrack> library = [];
    private List<LibraryTrack> filtered = [];
    private List<LibraryTrack> playQueue = [];
    private List<LyricLine> lines = [];
    private LibraryTrack? track;
    private JsonElement metadata;
    private JsonElement selection;
    private string source = "", timingKind = "line";
    private double offset, scale = 1, duration;
    private bool playing, opened, updatingPosition, closing, closed, polling;
    private int trackVersion, editVersion, activeIndex = -1, lastWord = -1;
    private (string Key, int Edit, string Job)? wanted;
    private Task? pendingSave;

    public MainWindow()
    {
        InitializeComponent();
        clock.Tick += (_, _) => UpdateClock();
        statusTimer.Tick += async (_, _) => await Run(Poll);
    }

    private async void WindowLoaded(object sender, RoutedEventArgs e)
    {
        await Run(async () =>
        {
            var root = new DirectoryInfo(AppContext.BaseDirectory);
            while (!File.Exists(Path.Combine(root.FullName, "desktop_bridge.py")))
                root = root.Parent ?? throw new FileNotFoundException("Keep Verse Lab.exe in its project folder.");
            backend = ((App)Application.Current).Bridge;
            await LoadLibrary();
            var state = await Call(new { action = "status" });
            UpdateBatch(state.Get("batch"));
            clock.Start();
            statusTimer.Start();
        });
    }

    private async void WindowClosing(object? sender, CancelEventArgs e)
    {
        if (closed)
            return;
        e.Cancel = true;
        if (closing)
            return;
        if (((App)Application.Current).HideOnClose(this))
            return;
        closing = true;
        Activity.Text = "Closing Verse Lab…";
        clock.Stop();
        statusTimer.Stop();
        audio?.Close();
        try
        {
            if (pendingSave != null)
                await pendingSave;
        }
        catch (Exception error)
        {
            MessageBox.Show(this, error.Message, "Could not save lyrics");
        }
        finally
        {
            closed = true;
            Close();
        }
    }

    private Task<JsonElement> Call(object message) =>
        backend?.Send(message) ?? throw new InvalidOperationException("The lyrics engine is unavailable.");

    private async Task Run(Func<Task> work)
    {
        if (closing)
            return;
        try { await work(); }
        catch (Exception error) { if (!closing) Activity.Text = error.Message; }
    }

    private async Task LoadLibrary(string? folder = null)
    {
        LibraryStatus.Text = "Scanning music…";
        var result = await Call(new { action = "library", folder });
        library = result.Get("tracks").Deserialize<List<LibraryTrack>>(Backend.Json) ?? [];
        LibraryStatus.Text = result.String("folder");
        RefreshLibraryFilter();
    }

    private void FilterLibrary(object sender, TextChangedEventArgs e)
    {
        if (Library != null)
            RefreshLibraryFilter();
    }

    private void RefreshLibraryFilter()
    {
        var terms = SearchBox.Text.Split(' ', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        filtered = library.Where(item => terms.All(term => item.Name.Contains(term, StringComparison.CurrentCultureIgnoreCase))).ToList();
        Library.ItemsSource = filtered;
        LibraryCount.Text = $"{filtered.Count:N0} of {library.Count:N0} tracks";
    }

    private async void Rescan(object sender, RoutedEventArgs e) => await Run(() => LoadLibrary());
    private async void ChooseFolder(object sender, RoutedEventArgs e)
    {
        var chooser = new OpenFolderDialog { Title = "Choose your music folder" };
        if (chooser.ShowDialog(this) == true)
            await Run(() => LoadLibrary(chooser.FolderName));
    }

    private async void OpenFile(object sender, RoutedEventArgs e)
    {
        var chooser = new OpenFileDialog { Title = "Open audio", Filter = AudioFiles };
        if (chooser.ShowDialog(this) != true)
            return;
        await Run(async () =>
        {
            var item = (await Call(new { action = "file", path = chooser.FileName })).Deserialize<LibraryTrack>(Backend.Json)!;
            playQueue = [item];
            await LoadTrack(item);
        });
    }

    private async void OpenSelected(object sender, MouseButtonEventArgs e) => await Run(PlaySelected);
    private async void OpenSelectedKey(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.Enter)
        {
            e.Handled = true;
            await Run(PlaySelected);
        }
    }

    private Task PlaySelected()
    {
        if (Library.SelectedItem is not LibraryTrack item)
            return Task.CompletedTask;
        playQueue = filtered.ToList();
        return LoadTrack(item);
    }

    private async Task LoadTrack(LibraryTrack next)
    {
        if (pendingSave != null)
            await pendingSave;
        int version = ++trackVersion;
        wanted = null;
        track = null;
        audio?.Close();
        audio = null;
        opened = playing = false;
        duration = 0;
        offset = 0;
        scale = 1;
        PlayButton.Content = "Play";
        TrackTitle.Text = next.Name;
        TrackArtist.Text = "";
        SetLyrics([], "");
        Activity.Text = "Opening audio…";

        var loaded = await Call(new { action = "track", key = next.Key });
        if (closing || version != trackVersion)
            return;
        track = next;
        metadata = loaded.Get("metadata");
        TrackTitle.Text = metadata.String("title", next.Name);
        TrackArtist.Text = metadata.String("artist");
        duration = metadata.Number("duration", 0);
        Position.Maximum = Math.Max(1, duration);

        var saved = loaded.Get("saved");
        var prepared = loaded.Get("prepared");
        var savedSelection = saved.Get("selection");
        bool manual = saved.Number("offset", 0) != 0 || saved.Number("scale", 1) != 1 ||
            saved.Get("lines").Items().Any(line => line.String("timing") == "manual");
        var savedKind = saved.String("timingKind", "line");
        bool generated = savedKind == "model" || savedSelection.String("kind") == "reference_aligned";
        bool previousDraft = generated && !manual &&
            (saved.Number("pipelineVersion", 0) < PipelineVersion ||
             saved.String("fingerprint") != metadata.String("fingerprint"));
        var savedLyrics = ReadLyrics(saved);
        selection = default;
        timingKind = "line";
        if (metadata.String("embedded") is { Length: > 0 } embedded)
            SetLyrics(Lyrics.Parse(embedded), "Embedded lyrics");
        else if (!previousDraft && savedLyrics.Count > 0)
        {
            offset = saved.Number("offset", 0);
            scale = Math.Clamp(saved.Number("scale", 1), .25, 4);
            selection = savedSelection;
            timingKind = savedKind;
            SetLyrics(savedLyrics, saved.String("source", "Saved lyrics"));
        }
        else if (metadata.String("sidecar") is { Length: > 0 } sidecar)
            SetLyrics(Lyrics.Parse(sidecar), "Local lyric file");
        else if (prepared.ValueKind == JsonValueKind.Object)
            ApplyPrepared(prepared);
        else if (previousDraft)
        {
            timingKind = savedKind;
            SetLyrics(savedLyrics, "Previous lyric draft");
        }

        var player = new MediaPlayer { Volume = Volume.Value };
        bool triedDecode = false;
        audio = player;
        player.MediaOpened += (_, _) =>
        {
            if (audio != player || closing)
                return;
            if (player.NaturalDuration.HasTimeSpan)
                duration = player.NaturalDuration.TimeSpan.TotalSeconds;
            Position.Maximum = Math.Max(1, duration);
            opened = true;
            SetPlaying(true);
            if (wanted == null)
                Activity.Text = "Playing";
        };
        player.MediaFailed += async (_, error) =>
        {
            if (audio != player)
                return;
            SetPlaying(false);
            if (!triedDecode)
            {
                triedDecode = true;
                await Run(async () =>
                {
                    Activity.Text = "Decoding local audio…";
                    var decoded = await Call(new { action = "decodePlayback", key = next.Key });
                    if (audio == player && !closing)
                        player.Open(new Uri(decoded.String("path"), UriKind.Absolute));
                });
                return;
            }
            Activity.Text = $"Windows could not play this recording. {error.ErrorException?.Message}";
        };
        player.MediaEnded += async (_, _) =>
        {
            if (audio == player)
            {
                SetPlaying(false);
                await Run(() => Move(1));
            }
        };
        player.Open(new Uri(metadata.String("path"), UriKind.Absolute));

        bool awaitingUpgrade = previousDraft && prepared.ValueKind != JsonValueKind.Object &&
            string.IsNullOrEmpty(metadata.String("sidecar")) && string.IsNullOrEmpty(metadata.String("embedded"));
        if (string.IsNullOrEmpty(metadata.String("embedded")) && (lines.Count == 0 || awaitingUpgrade))
            await QueueLyrics(false);
        if (version == trackVersion)
            await QueueUpcoming();
    }

    private static List<LyricLine> ReadLyrics(JsonElement data)
    {
        var cached = data.Get("lines");
        if (cached.ValueKind == JsonValueKind.Array && cached.GetArrayLength() > 0)
            return cached.Deserialize<List<LyricLine>>(Backend.Json) ?? [];
        return Lyrics.Parse(data.String("raw", data.String("lyrics")));
    }

    private void ApplyPrepared(JsonElement result)
    {
        offset = 0;
        scale = 1;
        var kind = result.String("kind");
        timingKind = result.String("timingKind", "line");
        selection = JsonSerializer.SerializeToElement(new
        {
            kind, model = result.String("model"), alignment = result.Get("alignment").OrNull(),
            pipelineVersion = result.Number("pipelineVersion", PipelineVersion)
        }, Backend.Json);
        var timeCount = result.Get("alignment").Number("locallyTimedLines", 0);
        var label = result.String("source", "Prepared lyrics");
        if (timeCount > 0 && result.Get("alignment").Number("audioLines", 0) == 0)
            label += $" · audio timing on {timeCount:0} lines";
        SetLyrics(ReadLyrics(result), label);
    }

    private void SetLyrics(List<LyricLine> next, string label)
    {
        if (activeIndex >= 0 && activeIndex < lines.Count)
            lines[activeIndex].IsActive = false;
        lines = next;
        source = label;
        LyricsList.ItemsSource = null;
        LyricsList.ItemsSource = lines;
        LyricsList.SelectedIndex = -1;
        activeIndex = -1;
        lastWord = -1;
        ActiveWords.Inlines.Clear();
        SourceLabel.Text = lines.Count == 0 ? "Lyrics will appear here" : source;
        UpdateClock();
    }

    private void SetPlaying(bool value)
    {
        if (value) audio?.Play();
        else audio?.Pause();
        playing = value;
        PlayButton.Content = value ? "Pause" : "Play";
    }

    private async Task TogglePlayback()
    {
        if (opened)
            SetPlaying(!playing);
        else if (track == null && Library.SelectedItem != null)
            await PlaySelected();
    }
    private async void PlayPause(object sender, RoutedEventArgs e) => await Run(TogglePlayback);
    private async void Previous(object sender, RoutedEventArgs e) => await Run(() => Move(-1));
    private async void Next(object sender, RoutedEventArgs e) => await Run(() => Move(1));
    private Task Move(int delta)
    {
        int index = playQueue.FindIndex(item => item.Key == track?.Key) + delta;
        return index >= 0 && index < playQueue.Count ? LoadTrack(playQueue[index]) : Task.CompletedTask;
    }

    private void SeekChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (opened && !updatingPosition && audio != null)
            audio.Position = TimeSpan.FromSeconds(e.NewValue);
    }

    private void VolumeChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (audio != null)
            audio.Volume = e.NewValue;
    }

    private void UpdateClock()
    {
        var now = opened ? audio?.Position.TotalSeconds ?? 0 : 0;
        if (Position == null)
            return;
        updatingPosition = true;
        if (!Position.IsMouseCaptureWithin)
            Position.Value = Math.Clamp(now, Position.Minimum, Position.Maximum);
        updatingPosition = false;
        PositionLabel.Text = Lyrics.Stamp(now)[..^3];
        DurationLabel.Text = Lyrics.Stamp(duration)[..^3];

        var lyricTime = (now - offset) / scale;
        int next = -1;
        double latest = double.NegativeInfinity;
        for (int i = 0; opened && i < lines.Count; i++)
            if (lines[i].Time is double start && start <= lyricTime && start >= latest)
                (next, latest) = (i, start);
        if (next >= 0 && lines[next].End is double end && lyricTime >= end)
            next = -1;
        if (next != activeIndex)
        {
            if (activeIndex >= 0)
                lines[activeIndex].IsActive = false;
            activeIndex = next;
            lastWord = -1;
            ActiveWords.Inlines.Clear();
            if (next >= 0)
            {
                lines[next].IsActive = true;
                if (!LyricsList.IsMouseOver)
                    LyricsList.ScrollIntoView(lines[next]);
            }
        }
        if (next < 0)
            return;
        var current = lines[next];
        var words = current.Words;
        int sung = words.Count(word => word.Time <= lyricTime);
        if (sung == lastWord)
            return;
        lastWord = sung;
        ActiveWords.Inlines.Clear();
        if (words.Count == 0)
            ActiveWords.Inlines.Add(new Run(current.Text) { Foreground = SungWord });
        else
            for (int i = 0; i < words.Count; i++)
            {
                string word = words[i].Text;
                if (i > 0 && !char.IsWhiteSpace(words[i - 1].Text.LastOrDefault()) &&
                    !char.IsWhiteSpace(word.FirstOrDefault()))
                    word = " " + word;
                ActiveWords.Inlines.Add(new Run(word) { Foreground = i < sung ? SungWord : UpcomingWord });
            }
    }

    private async void KeyboardShortcut(object sender, KeyEventArgs e)
    {
        if (e.OriginalSource is TextBox or Button or ComboBox or ComboBoxItem)
            return;
        if (e.Key == Key.Space)
        {
            e.Handled = true;
            await Run(TogglePlayback);
        }
        else if (opened && audio != null && e.Key is Key.J or Key.L)
        {
            e.Handled = true;
            audio.Position = TimeSpan.FromSeconds(Math.Clamp(
                audio.Position.TotalSeconds + (e.Key == Key.J ? -5 : 5), 0, duration));
        }
    }

    private async Task QueueLyrics(bool force)
    {
        if (track == null)
            return;
        var key = track.Key;
        int edit = editVersion;
        Activity.Text = "Preparing lyrics…";
        var result = await Call(new { action = "prepare", key, reason = force ? "manual" : "current", force });
        if (track?.Key != key || closing || editVersion != edit)
            return;
        if (result.Get("result").ValueKind == JsonValueKind.Object)
            await AcceptPrepared(result.Get("result"));
        else
            wanted = (key, edit, result.String("key"));
    }

    private async Task QueueUpcoming()
    {
        if (track == null)
            return;
        int index = playQueue.FindIndex(item => item.Key == track.Key);
        if (index < 0)
            return;
        var keys = playQueue.Skip(index + 1).Select(item => item.Key).ToArray();
        await Call(new { action = "prefetch", key = track.Key, keys });
    }

    private async Task Poll()
    {
        if (polling)
            return;
        polling = true;
        try
        {
            var status = await Call(new { action = "status" });
            UpdateBatch(status.Get("batch"));
            CancelPreparation.Visibility = wanted.HasValue ? Visibility.Visible : Visibility.Collapsed;
            if (wanted is not { } request || request.Key != track?.Key)
                return;
            var job = status.Get("jobs").Items().LastOrDefault(item => item.String("key") == request.Job);
            switch (job.String("status"))
            {
                case "complete":
                    wanted = null;
                    var result = await Call(new { action = "result", key = request.Key });
                    if (track?.Key == request.Key && editVersion == request.Edit && !closing)
                        await AcceptPrepared(result);
                    break;
                case "failed":
                case "cancelled":
                case "skipped":
                    wanted = null;
                    Activity.Text = job.String("phase", "Preparation stopped");
                    break;
                default:
                    Activity.Text = job.String("phase", "Queued") + $" · {job.Number("elapsed", 0):0}s";
                    break;
            }
        }
        finally { polling = false; }
    }

    private async Task AcceptPrepared(JsonElement result)
    {
        if (result.ValueKind != JsonValueKind.Object)
            return;
        ApplyPrepared(result);
        Activity.Text = $"Lyrics ready · {result.Number("seconds", 0):0.#} s";
        await Save();
    }

    private void UpdateBatch(JsonElement batch)
    {
        string status = batch.String("status", "idle");
        bool active = status is "running" or "stopping";
        StartBatchButton.IsEnabled = !active;
        PauseBatchButton.IsEnabled = status == "running";
        BatchStatus.Text = status == "idle" ? "" :
            $"{char.ToUpperInvariant(status[0]) + status[1..]} · {batch.Number("prepared", 0):0} ready · " +
            $"{batch.Number("skipped", 0):0} skipped · {batch.Number("failed", 0):0} failed / {batch.Number("total", 0):0}";
        if (batch.String("lastError") is { Length: > 0 } error)
            BatchStatus.Text += Environment.NewLine + error;
    }

    private async void StartBatch(object sender, RoutedEventArgs e) => await Run(async () =>
    {
        var keys = BatchScope.SelectedIndex == 1 ? null : filtered.Take(10).Select(t => t.Key).ToArray();
        if (keys is { Length: 0 })
            throw new InvalidOperationException("Search for songs to include in the batch.");
        UpdateBatch(await Call(new { action = "batch", keys }));
    });

    private async void PauseBatch(object sender, RoutedEventArgs e) => await Run(async () =>
        UpdateBatch(await Call(new { action = "batch", pause = true })));
    private async void PrepareLyrics(object sender, RoutedEventArgs e) => await Run(() => QueueLyrics(true));
    private async void CancelLyrics(object sender, RoutedEventArgs e) => await Run(async () =>
    {
        if (track != null)
            await Call(new { action = "cancel", key = track.Key });
        wanted = null;
        Activity.Text = "Preparation cancelled";
    });

    private async Task<JsonElement> Save(bool embed = false)
    {
        if (track == null)
            return default;
        var writing = Call(new
        {
            action = "save", key = track.Key, embed,
            state = new
            {
                lines, source, offset, scale, timingKind, pipelineVersion = PipelineVersion,
                selection = selection.OrNull(), fingerprint = metadata.String("fingerprint"),
                updatedAt = DateTimeOffset.UtcNow
            }
        });
        pendingSave = writing;
        return await writing;
    }

    private void JumpToLine(object sender, MouseButtonEventArgs e)
    {
        if (opened && audio != null && LyricsList.SelectedItem is LyricLine { Time: double time })
            audio.Position = TimeSpan.FromSeconds(Math.Clamp(time * scale + offset, 0, duration));
    }

    private async void ImportLyrics(object sender, RoutedEventArgs e) => await Run(async () =>
    {
        if (track == null)
            return;
        var dialog = new OpenFileDialog { Filter = "Lyrics|*.lrc;*.txt|All files|*.*" };
        if (dialog.ShowDialog(this) != true)
            return;
        var imported = Lyrics.Parse(await File.ReadAllTextAsync(dialog.FileName));
        if (imported.Count == 0)
            throw new InvalidOperationException("The lyric file is empty.");
        await UseLyrics(imported, "Imported lyrics");
    });

    private async Task UseLyrics(List<LyricLine> replacement, string label)
    {
        (offset, scale) = (0, 1);
        timingKind = "line";
        selection = default;
        editVersion++;
        wanted = null;
        SetLyrics(replacement, label);
        var result = await Save(embed: true);
        Activity.Text = result.String("embedError") is { Length: > 0 } reason
            ? "Lyrics cached. MP3 update failed. " + reason
            : "Lyrics saved";
    }

    private async void ExportLyrics(object sender, RoutedEventArgs e) => await Run(async () =>
    {
        if (track == null || lines.Count == 0)
            return;
        var dialog = new SaveFileDialog { Filter = "LRC lyric file|*.lrc", FileName = track.Name + ".lrc", DefaultExt = ".lrc" };
        if (dialog.ShowDialog(this) != true)
            return;
        var exported = lines.Select(line => line.Time is double start ?
            $"[{Lyrics.Stamp(start * scale + offset)}]" +
            (line.Words.Count == 0 ? line.Text : string.Concat(line.Words.Select(word =>
                $"<{Lyrics.Stamp(word.Time * scale + offset)}>{word.Text}"))) : line.Text);
        await File.WriteAllLinesAsync(dialog.FileName, exported);
        Activity.Text = "Lyric file exported";
    });

    private async void FindLyrics(object sender, RoutedEventArgs e) => await Run(async () =>
    {
        if (track == null)
            return;
        var search = new LyricSearch(this, TrackTitle.Text, TrackArtist.Text, duration,
            (artist, title) => Call(new { action = "search", artist, title, duration }));
        if (search.ShowDialog() == true && search.SelectedLyrics is { } raw)
            await UseLyrics(Lyrics.Parse(raw), "Selected lyrics · LRCLIB");
    });
}

internal static class JsonReading
{
    public static JsonElement Get(this JsonElement data, string key) =>
        data.ValueKind == JsonValueKind.Object && data.TryGetProperty(key, out var value) ? value : default;
    public static string String(this JsonElement data, string key, string fallback = "") =>
        data.Get(key) is { ValueKind: JsonValueKind.String } value ? value.GetString() ?? fallback : fallback;
    public static double Number(this JsonElement data, string key, double fallback) =>
        data.Get(key) is { ValueKind: JsonValueKind.Number } value && value.TryGetDouble(out var number) &&
        double.IsFinite(number) ? number : fallback;
    public static bool Flag(this JsonElement data, string key, bool fallback = false) =>
        data.Get(key).ValueKind switch { JsonValueKind.True => true, JsonValueKind.False => false, _ => fallback };
    public static IEnumerable<JsonElement> Items(this JsonElement value) =>
        value.ValueKind == JsonValueKind.Array ? value.EnumerateArray() : [];
    public static JsonElement? OrNull(this JsonElement value) => value.ValueKind == JsonValueKind.Undefined ? null : value;
}
