using System.Globalization;
using System.IO;
using Microsoft.Web.WebView2.Core;

namespace VerseLab;

internal sealed class LocalAudioResources
{
    private const string Origin = "https://verse-lab-audio.invalid/";
    private readonly CoreWebView2Environment environment;
    private readonly Dictionary<string, string> recordings = [];

    internal LocalAudioResources(CoreWebView2 web)
    {
        environment = web.Environment;
        // A virtual folder host bypasses resource interception. Audio has its own host.
        web.AddWebResourceRequestedFilter(Origin + "*", CoreWebView2WebResourceContext.All,
            CoreWebView2WebResourceRequestSourceKinds.Document);
        web.WebResourceRequested += ServeAudio;
    }

    internal string Register(string key, string path)
    {
        recordings[key] = path;
        return Origin + key;
    }

    private void ServeAudio(object? sender, CoreWebView2WebResourceRequestedEventArgs e)
    {
        var key = new Uri(e.Request.Uri).AbsolutePath.TrimStart('/');
        if (!recordings.TryGetValue(key, out var path))
        {
            e.Response = environment.CreateWebResourceResponse(null, 404, "Not Found", "");
            return;
        }

        FileStream input;
        try
        {
            input = File.Open(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            e.Response = environment.CreateWebResourceResponse(null, 404, "Not Found", "");
            return;
        }

        var length = input.Length;
        var range = e.Request.Headers.Contains("Range") ? e.Request.Headers.GetHeader("Range") : "";
        if (!TryGetRange(range, length, out long first, out long last))
        {
            input.Dispose();
            e.Response = environment.CreateWebResourceResponse(null, 416, "Range Not Satisfiable",
                $"Content-Range: bytes */{length}");
            return;
        }
        var type = Path.GetExtension(path).ToLowerInvariant() switch
        {
            ".mp3" or ".mp2" or ".mpa" => "audio/mpeg", ".wav" or ".wave" => "audio/wav", ".flac" => "audio/flac",
            ".ogg" or ".oga" or ".opus" or ".spx" => "audio/ogg", ".m4a" or ".m4b" or ".mp4" or ".alac" => "audio/mp4",
            ".aac" => "audio/aac", ".aiff" or ".aif" or ".aifc" => "audio/aiff", _ => "application/octet-stream",
        };
        bool partial = range.Length > 0;
        long count = last - first + 1;
        string headers = $"Content-Type: {type}\r\nContent-Length: {count}\r\nAccept-Ranges: bytes";
        if (partial)
            headers += $"\r\nContent-Range: bytes {first}-{last}/{length}";
        Stream content = new AudioRange(path, input, first, count);
        e.Response = environment.CreateWebResourceResponse(content, partial ? 206 : 200,
            partial ? "Partial Content" : "OK", headers);
    }

    private static bool TryGetRange(string value, long length, out long first, out long last)
    {
        first = 0;
        last = length - 1;
        if (value.Length == 0)
            return true;
        if (!value.StartsWith("bytes=", StringComparison.OrdinalIgnoreCase) || length == 0)
            return false;
        var parts = value[6..].Split('-', 2);
        if (parts.Length != 2)
            return false;
        static bool Number(string text, out long result) =>
            long.TryParse(text, NumberStyles.None, CultureInfo.InvariantCulture, out result);
        if (parts[0].Length == 0)
        {
            if (!Number(parts[1], out long suffix) || suffix <= 0)
                return false;
            first = Math.Max(0, length - suffix);
            return true;
        }
        if (!Number(parts[0], out first) || first >= length)
            return false;
        if (parts[1].Length == 0)
            return true;
        if (!Number(parts[1], out last) || last < first)
            return false;
        last = Math.Min(last, length - 1);
        return true;
    }

    private sealed class AudioRange(string path, FileStream input, long first, long length) : Stream
    {
        private FileStream? file = input;
        private long position;
        private bool disposed;

        public override bool CanRead => !disposed;
        public override bool CanSeek => !disposed;
        public override bool CanWrite => false;
        public override long Length => length;
        public override long Position
        {
            get => position;
            set => Seek(value, SeekOrigin.Begin);
        }

        public override int Read(byte[] buffer, int offset, int count)
        {
            ObjectDisposedException.ThrowIf(disposed, this);
            int available = (int)Math.Min(count, Math.Max(0, Length - position));
            if (available == 0)
                return 0;

            file ??= File.Open(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            file.Position = first + position;
            int read = file.Read(buffer, offset, available);
            position += read;
            // WebView2 does not dispose a managed response stream when it finishes reading.
            if (read == 0 || position == Length)
                CloseFile();
            return read;
        }

        public override long Seek(long offset, SeekOrigin origin)
        {
            ObjectDisposedException.ThrowIf(disposed, this);
            long target = origin switch
            {
                SeekOrigin.Begin => offset, SeekOrigin.Current => position + offset, SeekOrigin.End => Length + offset,
                _ => throw new ArgumentOutOfRangeException(nameof(origin)),
            };
            if (target < 0)
                throw new IOException("Invalid audio position.");
            position = target;
            return position;
        }

        private void CloseFile()
        {
            file?.Dispose();
            file = null;
        }

        public override void Flush() { }
        public override void SetLength(long value) => throw new NotSupportedException();
        public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                CloseFile();
                disposed = true;
            }
            base.Dispose(disposing);
        }
    }
}
