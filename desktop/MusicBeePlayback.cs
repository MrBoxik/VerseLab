using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using Microsoft.Win32.SafeHandles;

namespace VerseLab;

internal sealed record Playback(string File, string Title, string Artist, int PositionMs, int DurationMs,
                                bool Playing, string State, string[] UpNext, bool AutoDj,
                                string SourceId = "", string Player = "MusicBee", double Rate = 1)
{
    public bool CanDisplay => (File.Length > 0 || SourceId.Length > 0 && Title.Length > 0) &&
                              State is "Playing" or "Paused" or "Loading" or "Changing";

    public double Seconds(double sinceSample) =>
        Math.Max(0, Math.Min((PositionMs / 1000.0) + (Playing ? sinceSample * Rate : 0),
                            DurationMs > 0 ? DurationMs / 1000.0 : double.PositiveInfinity));
}

internal static class MusicBeePlayback
{
    private const string PreviewPipe = "TaskbarMediaPresence.MusicBeeVolume.v1";

    internal static async Task<(Playback? State, string Status)> Read(CancellationToken lifetime)
    {
        try
        {
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(lifetime);
            timeout.CancelAfter(950);
            using var pipe = new NamedPipeClientStream(".", PreviewPipe, PipeDirection.InOut,
                PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
            await pipe.ConnectAsync(timeout.Token);
            if (!GetNamedPipeServerProcessId(pipe.SafePipeHandle, out uint processId))
                return (null, "Waiting for the MusicBee companion");
            using var process = Process.GetProcessById(checked((int)processId));
            if (!string.Equals(process.ProcessName, "MusicBee", StringComparison.OrdinalIgnoreCase))
                return (null, "Waiting for the MusicBee companion");
            var playback = await Request(pipe, timeout.Token);
            return playback == null ? (null, "Install the bundled MusicBee companion")
                : (playback, "MusicBee connected");
        }
        catch (Exception error) when (error is IOException or TimeoutException or OperationCanceledException
                                      or ArgumentException or InvalidOperationException or UnauthorizedAccessException)
        {
            return (null, "Waiting for the MusicBee companion");
        }
    }

    internal static async Task<Playback?> Request(Stream pipe, CancellationToken cancellation)
    {
        await pipe.WriteAsync("PLAYBACK\n"u8.ToArray(), cancellation);
        await pipe.FlushAsync(cancellation);
        using var reader = new StreamReader(pipe, Encoding.UTF8, leaveOpen: true);
        var response = await reader.ReadLineAsync(cancellation);
        if (response == null || !response.StartsWith('{'))
            return null;
        try
        {
            var state = JsonSerializer.Deserialize<Playback>(response, Backend.Json);
            return state?.File == null || state.UpNext == null ? null : state;
        }
        catch (JsonException) { return null; }
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetNamedPipeServerProcessId(SafePipeHandle pipe, out uint serverProcessId);
}
