using System.IO;
using System.Runtime.InteropServices;
using Windows.Media.Control;

namespace VerseLab;

internal sealed class WindowsPlayback
{
    private GlobalSystemMediaTransportControlsSessionManager? manager;
    private string selected = "";

    internal async Task<Playback?> Read(CancellationToken lifetime)
    {
        try
        {
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(lifetime);
            timeout.CancelAfter(750);
            manager ??= await GlobalSystemMediaTransportControlsSessionManager.RequestAsync().AsTask(timeout.Token);
            var current = manager.GetCurrentSession();
            var active = manager.GetSessions().Where(session => !IsVerseLab(session.SourceAppUserModelId))
                .Select(session => (Session: session, Info: session.GetPlaybackInfo()))
                .Where(item => item.Info?.PlaybackStatus is GlobalSystemMediaTransportControlsSessionPlaybackStatus.Playing
                    or GlobalSystemMediaTransportControlsSessionPlaybackStatus.Paused
                    or GlobalSystemMediaTransportControlsSessionPlaybackStatus.Changing)
                .OrderByDescending(item => item.Info!.PlaybackStatus == GlobalSystemMediaTransportControlsSessionPlaybackStatus.Playing)
                .ThenByDescending(item => item.Session.SourceAppUserModelId == selected)
                .ThenByDescending(item => item.Session == current)
                .ThenByDescending(item => item.Session.SourceAppUserModelId == current?.SourceAppUserModelId);
            foreach (var (session, info) in active)
            {
                var media = await session.TryGetMediaPropertiesAsync().AsTask(timeout.Token);
                if (string.IsNullOrWhiteSpace(media.Title)) continue;
                selected = session.SourceAppUserModelId;
                var timeline = session.GetTimelineProperties();
                bool playing = info!.PlaybackStatus == GlobalSystemMediaTransportControlsSessionPlaybackStatus.Playing;
                double rate = info.PlaybackRate is double speed && speed > 0 ? Math.Clamp(speed, .1, 8) : 1;
                double seconds = (timeline.Position - timeline.StartTime).TotalSeconds;
                int duration = Milliseconds((timeline.EndTime - timeline.StartTime).TotalSeconds);
                if (playing && (timeline.LastUpdatedTime > DateTimeOffset.UnixEpoch))
                    seconds += Math.Max(0, (DateTimeOffset.UtcNow - timeline.LastUpdatedTime).TotalSeconds) * rate;
                int position = Milliseconds(seconds);
                if (duration > 0) position = Math.Min(position, duration);
                return new Playback("", media.Title, media.Artist, position, duration, playing,
                    info.PlaybackStatus.ToString(), [], false, selected, PlayerName(selected), rate);
            }
        }
        catch (Exception error) when (error is OperationCanceledException or COMException or InvalidOperationException)
        {
            if (error is COMException) manager = null;
        }
        return null;
    }

    private static int Milliseconds(double seconds) => (int)Math.Clamp(seconds * 1000, 0, int.MaxValue);

    private static bool IsVerseLab(string id)
    {
        var stem = Path.GetFileNameWithoutExtension(id);
        return id.Equals("VerseLab.Desktop", StringComparison.OrdinalIgnoreCase) ||
               stem.Equals("Verse Lab", StringComparison.OrdinalIgnoreCase) ||
               stem.Equals("Taskbar Lyrics", StringComparison.OrdinalIgnoreCase);
    }

    private static string PlayerName(string id)
    {
        var stem = Path.GetFileNameWithoutExtension(id);
        if (id.Contains("MusicBee", StringComparison.OrdinalIgnoreCase)) return "MusicBee";
        if (id.Contains("Spotify", StringComparison.OrdinalIgnoreCase)) return "Spotify";
        if (id.Contains("foobar2000", StringComparison.OrdinalIgnoreCase)) return "foobar2000";
        return id.Contains('!') || stem.Length == 0 ? "Windows media player" : stem;
    }
}
