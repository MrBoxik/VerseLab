using System.Diagnostics;
using System.Text.Json;
using System.Windows.Threading;

namespace VerseLab;

internal sealed class TaskbarLyricsWorker
{
    private readonly Backend backend;
    private readonly LyricPublication publication;
    private readonly Action<string> showStatus;
    private readonly WindowsPlayback windows = new();
    private string connectionStatus = "Looking for a media player…";
    private string previousStatus = "";
    private readonly CancellationTokenSource lifetime = new();
    private readonly DispatcherTimer pollTimer = new() { Interval = TimeSpan.FromMilliseconds(550) };
    private readonly DispatcherTimer renderTimer = new() { Interval = TimeSpan.FromMilliseconds(100) };
    private readonly Dictionary<string, string> registered = new(StringComparer.OrdinalIgnoreCase);
    private Playback? playback;
    private string currentFile = "";
    private string queuedFiles = "";
    private string? currentKey;
    private string? jobKey;
    private string status = "";
    private double offset, scale = 1;
    private long sampled;
    private List<LyricLine> lines = [];
    private Dictionary<LyricLine, TimedWord[]> words = [];
    private string source = "";
    private string longestLine = "";
    private string? track;
    private int revision;
    private Task? streamLookup;
    private Task? poll;
    private bool stopping;
    private bool indexed;

    internal Playback? Current => playback;
    internal string Status => previousStatus.Length > 0 ? previousStatus : connectionStatus;

    internal object Follow(int knownRevision) => new
    {
        playback, status = Status, lyricStatus = status, revision, source, offset, scale,
        seconds = playback?.Seconds(Stopwatch.GetElapsedTime(sampled).TotalSeconds) ?? 0,
        lines = knownRevision == revision ? null : lines,
    };

    internal TaskbarLyricsWorker(Backend backend, Action<string> showStatus)
    {
        this.showStatus = showStatus;
        publication = new LyricPublication();
        this.backend = backend;
        pollTimer.Tick += (_, _) => SchedulePoll();
        renderTimer.Tick += (_, _) => RenderLine();
        pollTimer.Start();
        renderTimer.Start();
        SchedulePoll();
    }

    private void SchedulePoll()
    {
        if (!stopping && (poll == null || poll.IsCompleted))
            poll = Poll();
    }

    private async Task Poll()
    {
        try
        {
            var local = MusicBeePlayback.Read(lifetime.Token);
            var other = await windows.Read(lifetime.Token);
            long windowsSampled = Stopwatch.GetTimestamp();
            var (bee, connection) = await local;
            var snapshot = bee?.CanDisplay == true && (other == null || other.Player == "MusicBee" ||
                           bee.Playing && !other.Playing) ? bee : other;
            connectionStatus = snapshot == null && bee == null && connection.StartsWith("Install", StringComparison.Ordinal)
                ? connection : "Waiting for playback";
            if (stopping)
                return;
            if (snapshot?.CanDisplay != true)
            {
                playback = null;
                if (track != null)
                {
                    bool hadLocal = currentFile.Length > 0;
                    Reset(null, "");
                    if (hadLocal) await backend.Send(new { action = "prefetch", keys = Array.Empty<string>() });
                }
                return;
            }
            string identity = snapshot.File.Length > 0 ? snapshot.File :
                $"{snapshot.SourceId}\n{snapshot.Title}\n{snapshot.Artist}\n{snapshot.DurationMs > 0}";
            bool changed = !string.Equals(track, identity, StringComparison.OrdinalIgnoreCase);
            bool leavingLocal = changed && currentFile.Length > 0 && snapshot.File.Length == 0;
            if (changed) Reset(identity, snapshot.File);
            playback = snapshot;
            sampled = ReferenceEquals(snapshot, other) ? windowsSampled : Stopwatch.GetTimestamp();
            if (snapshot.File.Length == 0)
            {
                if (leavingLocal) await backend.Send(new { action = "prefetch", keys = Array.Empty<string>() });
                if (status.Length == 0 && lines.Count == 0 && (streamLookup == null || streamLookup.IsCompleted))
                    streamLookup = LookupStream(snapshot, identity);
                return;
            }
            if (currentKey == null)
            {
                status = "Preparing lyrics…";
                if (!indexed)
                {
                    await backend.Send(new { action = "index" });
                    indexed = true;
                }
                var key = await Register(currentFile);
                var lyrics = await backend.Send(new { action = "lyricsState", key });
                if (lyrics.ValueKind == JsonValueKind.Object)
                    SetLyrics(lyrics);
                else
                {
                    var request = await backend.Send(new { action = "prepare", key });
                    if (request.TryGetProperty("result", out var result))
                        SetLyrics(result);
                    else
                        jobKey = request.GetProperty("key").GetString();
                }
                currentKey = key;
            }
            if (jobKey != null)
                await ReadJob();
            string upcoming = string.Join("\n", snapshot.UpNext);
            if (upcoming != queuedFiles)
            {
                var keys = new List<string>();
                foreach (var path in snapshot.UpNext)
                {
                    try { keys.Add(await Register(path)); }
                    catch (InvalidOperationException) { }
                }
                await backend.Send(new { action = "prefetch", key = currentKey, keys });
                queuedFiles = upcoming;
            }
        }
        catch (Exception error)
        {
            Debug.WriteLine(error);
            status = "Lyrics unavailable";
        }
    }

    private async Task LookupStream(Playback snapshot, string identity)
    {
        status = snapshot.DurationMs > 0 ? "Finding synced lyrics…" : "Waiting for track timing";
        if (snapshot.DurationMs <= 0) return;
        try
        {
            var result = await backend.Send(new { action = "streamLyrics", title = snapshot.Title,
                artist = snapshot.Artist, duration = snapshot.DurationMs / 1000.0 });
            if (stopping || track != identity) return;
            if (result.ValueKind == JsonValueKind.Object) SetLyrics(result);
            else status = "No matching synced lyrics";
        }
        catch (Exception error)
        {
            Debug.WriteLine(error);
            if (track == identity) status = "Lyrics unavailable";
        }
    }

    private void Reset(string? identity, string file)
    {
        track = identity;
        currentFile = file;
        currentKey = null;
        jobKey = null;
        status = "";
        queuedFiles = "\0";
        lines = [];
        words = [];
        source = "";
        longestLine = "";
        revision++;
    }

    private async Task<string> Register(string path)
    {
        if (registered.TryGetValue(path, out var key))
            return key;
        var file = await backend.Send(new { action = "file", path });
        key = file.GetProperty("key").GetString()!;
        if (registered.Count > 512)
            registered.Clear();
        registered[path] = key;
        return key;
    }

    private async Task ReadJob()
    {
        var state = await backend.Send(new { action = "status" });
        foreach (var job in state.GetProperty("jobs").EnumerateArray())
        {
            if (job.GetProperty("key").GetString() != jobKey)
                continue;
            var phase = job.GetProperty("status").GetString();
            if (phase == "complete")
            {
                jobKey = null;
                var result = await backend.Send(new { action = "lyricsState", key = currentKey });
                if (result.ValueKind == JsonValueKind.Object)
                    SetLyrics(result);
            }
            else if (phase is "failed" or "cancelled")
            {
                jobKey = null;
                status = "Lyrics unavailable";
            }
            break;
        }
    }

    private void SetLyrics(JsonElement value)
    {
        lines = value.TryGetProperty("lines", out var array) && array.ValueKind == JsonValueKind.Array && array.GetArrayLength() > 0
            ? array.Deserialize<List<LyricLine>>(Backend.Json) ?? []
            : Lyrics.Parse(value.TryGetProperty("raw", out var raw) || value.TryGetProperty("lyrics", out raw)
                ? raw.GetString() ?? "" : "");
        offset = value.TryGetProperty("offset", out var delay) && delay.TryGetDouble(out double seconds) ? seconds : 0;
        scale = value.TryGetProperty("scale", out var factor) && factor.TryGetDouble(out double rate) ? Math.Clamp(rate, .25, 4) : 1;
        lines = lines.Where(line => line.Time is double time && double.IsFinite(time) && !string.IsNullOrWhiteSpace(line.Text))
            .OrderBy(line => line.Time).ToList();
        longestLine = lines.MaxBy(line => line.Text.Length)?.Text ?? "";
        words = lines.Select((line, index) => (Line: line, Index: index)).ToDictionary(item => item.Line,
            item => Lyrics.TaskbarWords(item.Line, item.Index + 1 < lines.Count ? lines[item.Index + 1].Time : null));
        source = value.TryGetProperty("source", out var origin) ? origin.GetString() ?? "" : "";
        status = lines.Count > 0 ? "" : "Lyrics have no timing";
        revision++;
    }

    private void RenderLine()
    {
        if (stopping) return;
        string nextStatus = connectionStatus;
        if (playback?.CanDisplay == true)
        {
            var time = (playback.Seconds(Stopwatch.GetElapsedTime(sampled).TotalSeconds) - offset) / scale;
            var line = LineAt(lines, time);
            TimedWord word = line == null ? default : words[line].FirstOrDefault(word => word.Time <= time && time < word.End);
            publication.Update(playback.Title.Trim(), playback.Artist.Trim(), line?.Text ?? "", playback.SourceId, word, longestLine);
            nextStatus = status.Length > 0 ? status : playback.AutoDj ? "MusicBee · Auto-DJ connected" : playback.Player + " · synced lyrics";
        }
        else
        {
            publication.Update("", "", "");
        }
        if (nextStatus != previousStatus)
        {
            previousStatus = nextStatus;
            showStatus(nextStatus);
        }
    }

    internal static LyricLine? LineAt(IReadOnlyList<LyricLine> timeline, double time)
    {
        for (int index = timeline.Count - 1; index >= 0; index--)
        {
            var line = timeline[index];
            if (line.Time is double start && start <= time)
                return line;
        }
        return null;
    }

    internal async Task Stop()
    {
        stopping = true;
        lifetime.Cancel();
        pollTimer.Stop();
        renderTimer.Stop();
        try
        {
            if (poll != null)
                await poll;
            if (streamLookup != null)
                await streamLookup;
        }
        finally
        {
            publication.Dispose();
            lifetime.Dispose();
        }
    }

}
