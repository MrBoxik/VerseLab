using System.IO.MemoryMappedFiles;
using System.Text;

namespace VerseLab;

internal sealed class LyricPublication : IDisposable
{
    internal const string MappingName = @"Local\TaskbarMediaPresence.Lyrics.v3";
    private const string WordName = @"Local\TaskbarMediaPresence.Lyrics.v2";
    private const string LegacyName = @"Local\TaskbarMediaPresence.Lyrics.v1";
    private readonly MemoryMappedFile mapping;
    private readonly MemoryMappedViewAccessor view;
    private readonly MemoryMappedFile wordMapping;
    private readonly MemoryMappedViewAccessor wordView;
    private readonly MemoryMappedFile legacyMapping;
    private readonly MemoryMappedViewAccessor legacyView;
    private readonly byte[] payload = new byte[3208];
    private int sequence;

    internal LyricPublication()
    {
        mapping = MemoryMappedFile.CreateOrOpen(MappingName, 3224, MemoryMappedFileAccess.ReadWrite);
        view = mapping.CreateViewAccessor(0, 3224, MemoryMappedFileAccess.ReadWrite);
        wordMapping = MemoryMappedFile.CreateOrOpen(WordName, 2200, MemoryMappedFileAccess.ReadWrite);
        wordView = wordMapping.CreateViewAccessor(0, 2200, MemoryMappedFileAccess.ReadWrite);
        legacyMapping = MemoryMappedFile.CreateOrOpen(LegacyName, 1680, MemoryMappedFileAccess.ReadWrite);
        legacyView = legacyMapping.CreateViewAccessor(0, 1680, MemoryMappedFileAccess.ReadWrite);
    }

    internal void Update(string title, string artist, string text, string source = "", TimedWord word = default, string longestLine = "")
    {
        Array.Clear(payload);
        WriteText(title, 0, 160);
        WriteText(artist, 320, 160);
        int textLength = WriteText(text, 640, 512);
        WriteText(source, 1672, 256);
        WriteText(longestLine, 2184, 512);
        if (word.Length > 0 && word.Start >= 0 && word.Start + word.Length <= textLength)
        {
            System.Buffers.Binary.BinaryPrimitives.WriteInt32LittleEndian(payload.AsSpan(1664), word.Start);
            System.Buffers.Binary.BinaryPrimitives.WriteInt32LittleEndian(payload.AsSpan(1668), word.Length);
        }
        var tick = text.Length > 0 ? Environment.TickCount64 : 0;
        sequence += 2;
        Publish(view, 3, tick, payload.Length);
        Publish(wordView, 2, tick, 2184);
        Publish(legacyView, 1, source.Length == 0 ? tick : 0, 1664);
    }

    private void Publish(MemoryMappedViewAccessor target, int version, long tick, int count)
    {
        target.Write(0, sequence - 1);
        Thread.MemoryBarrier();
        target.Write(4, version);
        target.Write(8, tick);
        target.WriteArray(16, payload, 0, count);
        Thread.MemoryBarrier();
        target.Write(0, sequence);
    }

    private int WriteText(string text, int start, int capacity)
    {
        int length = Math.Min(text.Length, capacity - 1);
        if (length > 0 && char.IsHighSurrogate(text[length - 1]))
            length--;
        Encoding.Unicode.GetBytes(text.AsSpan(0, length), payload.AsSpan(start, capacity * 2));
        return length;
    }

    public void Dispose()
    {
        Update("", "", "");
        view.Dispose();
        mapping.Dispose();
        wordView.Dispose();
        wordMapping.Dispose();
        legacyView.Dispose();
        legacyMapping.Dispose();
    }
}
