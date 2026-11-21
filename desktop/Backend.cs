using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;

namespace VerseLab;

internal sealed class Backend : IAsyncDisposable
{
    private readonly Process process;
    private readonly SemaphoreSlim gate = new(1);
    internal static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web);

    public Backend(string root)
    {
        string bundled = Path.Combine(root, "runtime", "python", "python.exe");
        var info = new ProcessStartInfo(File.Exists(bundled) ? bundled : "python.exe")
        {
            WorkingDirectory = root,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardInputEncoding = new UTF8Encoding(false),
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8
        };
        string ffmpeg = Path.Combine(root, "runtime", "ffmpeg");
        if (File.Exists(Path.Combine(ffmpeg, "ffmpeg.exe")))
            info.Environment["PATH"] = ffmpeg + Path.PathSeparator + info.Environment["PATH"];
        info.ArgumentList.Add(Path.Combine(root, "desktop_bridge.py"));
        process = Process.Start(info) ?? throw new InvalidOperationException("Could not open the local lyrics engine.");
        process.ErrorDataReceived += (_, e) =>
        {
            if (e.Data != null)
                Debug.WriteLine(e.Data);
        };
        process.BeginErrorReadLine();
    }

    public async Task<JsonElement> Send(object request)
    {
        var body = JsonSerializer.Serialize(request, Json);
        await gate.WaitAsync();
        try
        {
            await process.StandardInput.WriteLineAsync(body);
            await process.StandardInput.FlushAsync();
            var response = await process.StandardOutput.ReadLineAsync();
            if (response == null)
                throw new InvalidOperationException("The lyrics engine closed. Reopen Verse Lab.");
            using var envelope = JsonDocument.Parse(response);
            if (envelope.RootElement.TryGetProperty("error", out var error))
                throw new InvalidOperationException(error.GetString());
            return envelope.RootElement.GetProperty("value").Clone();
        }
        finally { gate.Release(); }
    }

    public async ValueTask DisposeAsync()
    {
        process.StandardInput.Close();
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(8));
        try { await process.WaitForExitAsync(timeout.Token); }
        catch (OperationCanceledException)
        {
            if (!process.HasExited)
                process.Kill(entireProcessTree: true);
        }
        process.Dispose();
    }
}
