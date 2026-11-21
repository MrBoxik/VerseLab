using System.ComponentModel;
using System.Globalization;
using System.Text.RegularExpressions;

namespace VerseLab;

public sealed class LyricLine : INotifyPropertyChanged
{
    public double? Time { get; set; }
    public double? End { get; set; }
    public string Text { get; set; } = "";
    public string? Timing { get; set; }
    public string? Review { get; set; }
    public string? HeardText { get; set; }
    public List<LyricWord> Words { get; set; } = [];
    private bool active;

    [System.Text.Json.Serialization.JsonIgnore]
    public bool IsActive
    {
        get => active;
        set
        {
            if (active == value)
                return;
            active = value;
            PropertyChanged?.Invoke(this, new(nameof(IsActive)));
        }
    }

    [System.Text.Json.Serialization.JsonIgnore]
    public string Timestamp => (Time is double time ? Lyrics.Stamp(time) : "Untimed") +
                               (Review == null ? "" : "   ·   Review");

    public event PropertyChangedEventHandler? PropertyChanged;
}

public sealed record LyricWord(string Text, double Time, double? End);

internal readonly record struct TimedWord(int Start, int Length, double Time, double End);

internal static class Lyrics
{
    private static readonly Regex lineStamp = new(@"\[(\d+):(\d+(?:[.,]\d+)?)\]");
    private static readonly Regex wordStamp = new(@"<(\d+):(\d+(?:[.,]\d+)?)>");
    private static readonly Regex metadata = new(@"^\[[a-z][a-z0-9_-]*:.*\]$", RegexOptions.IgnoreCase);
    private static readonly Regex wordToken = new(@"\S+");
    private static double Seconds(Match match) => int.Parse(match.Groups[1].Value) * 60 +
        double.Parse(match.Groups[2].Value.Replace(',', '.'), CultureInfo.InvariantCulture);

    public static List<LyricLine> Parse(string raw)
    {
        var shiftTag = Regex.Match(raw, @"\[offset:([+-]?\d+)\]", RegexOptions.IgnoreCase);
        var shift = shiftTag.Success && long.TryParse(shiftTag.Groups[1].Value, out var milliseconds)
            ? milliseconds / 1000.0 : 0;
        var timed = new List<LyricLine>();
        var plain = new List<LyricLine>();
        foreach (var source in raw.TrimStart('\uFEFF').Split('\n'))
        {
            var row = source.Trim();
            var tags = lineStamp.Matches(row);
            if (tags.Count == 0)
            {
                if (row.Length > 0 && !metadata.IsMatch(row))
                    plain.Add(new() { Text = row });
                continue;
            }
            var body = lineStamp.Replace(row, "").Trim();
            var markers = wordStamp.Matches(body);
            foreach (Match tag in tags)
            {
                var delta = Seconds(tag) - Seconds(tags[0]);
                var line = new LyricLine { Time = Seconds(tag) + shift, Text = wordStamp.Replace(body, "").Trim() };
                for (int i = 0; i < markers.Count; i++)
                {
                    var marker = markers[i];
                    var next = i + 1 < markers.Count ? markers[i + 1] : null;
                    var word = body[(marker.Index + marker.Length)..(next?.Index ?? body.Length)];
                    if (!string.IsNullOrWhiteSpace(word))
                        line.Words.Add(new(word, Seconds(marker) + delta + shift,
                            next == null ? null : Seconds(next) + delta + shift));
                }
                timed.Add(line);
            }
        }
        if (timed.Count == 0)
            return plain;
        timed.Sort((a, b) => Nullable.Compare(a.Time, b.Time));
        for (int i = 0; i + 1 < timed.Count; i++)
            if (timed[i + 1].Time > timed[i].Time)
                timed[i].End = timed[i + 1].Time;
        return timed.Where(line => line.Text.Length > 0).ToList();
    }

    public static string Stamp(double seconds)
    {
        var hundredths = (long)Math.Round(Math.Max(0, seconds) * 100);
        return $"{hundredths / 6000:00}:{hundredths / 100 % 60:00}.{hundredths % 100:00}";
    }

    internal static TimedWord[] TaskbarWords(LyricLine line, double? next)
    {
        if (line.Time is not double start || line.Review != null) return [];
        var limit = next is double cue && cue > start ? cue : double.PositiveInfinity;
        if (line.End is double end && double.IsFinite(end) && end > start)
            limit = Math.Min(limit, end);
        var result = new List<TimedWord>();
        int cursor = 0;
        for (int i = 0; i < line.Words.Count; i++)
        {
            var word = line.Words[i];
            var text = word.Text.Trim();
            if (text.Length == 0 || !double.IsFinite(word.Time)) continue;
            int position = line.Text.IndexOf(text, cursor, StringComparison.OrdinalIgnoreCase);
            if (position < 0) continue;
            cursor = position + text.Length;
            double boundary = i + 1 < line.Words.Count ? line.Words[i + 1].Time : word.Time + 1.2;
            if (word.End is double wordEnd && wordEnd > word.Time)
                boundary = Math.Min(boundary, wordEnd);
            boundary = Math.Min(limit, boundary);
            if (double.IsFinite(boundary) && boundary > word.Time)
                result.Add(new(position, text.Length, Math.Max(start, word.Time), boundary));
        }
        if (result.Count > 0) return [.. result];
        var tokens = wordToken.Matches(line.Text);
        if (tokens.Count == 0) return [];
        var duration = Math.Min(limit - start, Math.Clamp(tokens.Count * .45, 1.4, 5));
        var weight = tokens.Sum(token => Math.Pow(token.Length, .65));
        foreach (Match token in tokens)
        {
            double endTime = start + duration * Math.Pow(token.Length, .65) / weight;
            result.Add(new(token.Index, token.Length, start, endTime));
            start = endTime;
        }
        return [.. result];
    }
}
