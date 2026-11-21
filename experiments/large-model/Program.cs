// Isolated benchmark only. Does not replace the player's Small engine or write music files.
using System.Diagnostics;
using System.Text.Json;
using Whisper.net;
using Whisper.net.LibraryLoader;
using Whisper.net.Logger;
using System.Runtime.Versioning;

[assembly: SupportedOSPlatform("windows")]

if (args.Length == 2 && args[0] == "--worker")
    return await RunWorker(args[1]);

if (args.Length < 3)
{
    Console.Error.WriteLine("Usage: LargeModelBenchmark MODEL MANIFEST OUTPUT [--dtw] [--beam]");
    return 2;
}
var model = Path.GetFullPath(args[0]);
var manifest = JsonSerializer.Deserialize<TrackInput[]>(File.ReadAllText(args[1]))!;
var destination = Path.GetFullPath(args[2]);
var dtw = args.Contains("--dtw");
var beam = args.Contains("--beam");
var sectional = dtw || args.Contains("--sections");
if (!OperatingSystem.IsWindows()) throw new PlatformNotSupportedException("This experiment measures Windows GPU counters.");
Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
RuntimeOptions.RuntimeLibraryOrder = [RuntimeLibrary.Vulkan]; // Never silently benchmark CPU.
using var logger = LogProvider.AddConsoleLogging(WhisperLogLevel.Info);
var samples = new List<object>();
var peaks = new long[3]; // dedicated VRAM, shared GPU memory, process working set
using var monitorStop = new CancellationTokenSource();
var monitor = Task.Run(async () =>
{
    var category = new PerformanceCounterCategory("GPU Process Memory");
    var prefix = $"pid_{Environment.ProcessId}_";
    while (!monitorStop.IsCancellationRequested)
    {
        try
        {
            long dedicated = 0, shared = 0;
            foreach (var name in category.GetInstanceNames().Where(name => name.StartsWith(prefix,StringComparison.OrdinalIgnoreCase)))
            {
                using var d = new PerformanceCounter("GPU Process Memory","Dedicated Usage",name,true);
                using var s = new PerformanceCounter("GPU Process Memory","Shared Usage",name,true);
                dedicated += d.RawValue; shared += s.RawValue;
            }
            using var process = Process.GetCurrentProcess();
            var ram = process.WorkingSet64;
            peaks[0] = Math.Max(peaks[0],dedicated); peaks[1] = Math.Max(peaks[1],shared); peaks[2] = Math.Max(peaks[2],ram);
            samples.Add(new {utc=DateTimeOffset.UtcNow,dedicated,shared,ram});
            if (dedicated > 8L*1024*1024*1024)
            {
                File.WriteAllText(destination+".memory-limit.json",JsonSerializer.Serialize(new {peaks,samples}));
                Console.Error.WriteLine("8 GiB per-process dedicated VRAM target exceeded; stopping benchmark.");
                Environment.Exit(8);
            }
        }
        catch (Exception exc) { Console.Error.WriteLine("Memory measurement: "+exc.Message); }
        try { await Task.Delay(250,monitorStop.Token); } catch (OperationCanceledException) { break; }
    }
});
var total = Stopwatch.StartNew();
var options = WhisperFactoryOptions.Default;
options.UseGpu = true;
options.UseFlashAttention = !dtw;
options.UseDtwTimeStamps = dtw;
options.HeadsPreset = dtw ? (model.Contains("small") ? WhisperAlignmentHeadsPreset.Small : WhisperAlignmentHeadsPreset.LargeV3) : WhisperAlignmentHeadsPreset.None;
var load = Stopwatch.StartNew();
using var factory = WhisperFactory.FromPath(model, options);
var loadSeconds = load.Elapsed.TotalSeconds;
Console.WriteLine($"MODEL_READY pid={Environment.ProcessId} load={loadSeconds:F2}s dtw={dtw}");
var results = new List<object>();
try
{
    foreach (var input in manifest)
    {
        var clock = Stopwatch.StartNew();
        var segments = await Decode(factory, input, dtw, beam, sectional, 4);
        var seconds = clock.Elapsed.TotalSeconds;
        results.Add(new {input.id,input.wav,input.duration,input.language,seconds,segments});
        File.WriteAllText(destination,JsonSerializer.Serialize(new {model,runtime="Vulkan",dtw,beam,sectional,loadSeconds,results},new JsonSerializerOptions {WriteIndented=true}));
        Console.WriteLine($"TRACK_READY {input.id} {seconds:F2}s segments={segments.Count}");
    }
}
finally
{
    monitorStop.Cancel(); await monitor;
    var report = new {model,runtime="Vulkan",dtw,beam,sectional,loadSeconds,totalSeconds=total.Elapsed.TotalSeconds,
        peakDedicatedBytes=peaks[0],peakSharedBytes=peaks[1],peakRamBytes=peaks[2],memorySamples=samples,results};
    File.WriteAllText(destination,JsonSerializer.Serialize(report,new JsonSerializerOptions {WriteIndented=true}));
}
return 0;

static async Task<int> RunWorker(string model)
{
    RuntimeOptions.RuntimeLibraryOrder = [RuntimeLibrary.Vulkan];
    var options = WhisperFactoryOptions.Default;
    options.UseGpu = true;
    options.UseDtwTimeStamps = true;
    options.HeadsPreset = WhisperAlignmentHeadsPreset.LargeV3;
    using var factory = WhisperFactory.FromPath(model, options);
    Send(new { type = "ready" });
    while (await Console.In.ReadLineAsync() is { } request)
    {
        try
        {
            var track = JsonSerializer.Deserialize<TrackInput>(request)
                ?? throw new InvalidDataException("Missing request");
            var segments = await Decode(factory, track, true, true, true, track.threads,
                (current, total) => Send(new { type = "progress", current, total }));
            Send(new { type = "result", segments });
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error);
            Send(new { type = "error", message = error.Message });
        }
    }
    return 0;
}

static void Send(object message)
{
    Console.WriteLine(JsonSerializer.Serialize(message));
    Console.Out.Flush();
}

static async Task<List<object>> Decode(WhisperFactory factory, TrackInput input, bool dtw,
    bool beam, bool sectional, int threads, Action<int,int>? progress = null)
{
    var builder = factory.CreateBuilder().WithThreads(threads).WithLanguage(input.language)
        .WithNoContext().WithMaxLastTextTokens(0).WithTokenTimestamps()
        .WithMaxSegmentLength(80).SplitOnWord()
        .WithTemperature(0).WithTemperatureInc(0).WithProbabilities();
    if (beam) builder.WithBeamSearchSamplingStrategy(s => s.WithBeamSize(5));
    else builder.WithGreedySamplingStrategy(s => s.WithBestOf(1));

    using var processor = builder.Build();
    var pcm = ReadPcm16(input.wav);
    var duration = pcm.Length / 16000.0;
    var coreLength = sectional ? 24.0 : duration;
    var segments = new List<object>();
    var section = 0;
    for (double coreStart = 0; coreStart < duration; coreStart += coreLength)
    {
        progress?.Invoke(++section, (int)Math.Ceiling(duration / coreLength));
        var coreEnd = Math.Min(duration, coreStart + coreLength);
        var windowStart = sectional ? Math.Max(0, coreStart - 2) : 0;
        var windowEnd = sectional ? Math.Min(duration, coreEnd + 2) : duration;
        var first = (int)Math.Round(windowStart * 16000);
        var count = Math.Min(pcm.Length - first, (int)Math.Round((windowEnd - windowStart) * 16000));
        await foreach (var segment in processor.ProcessAsync(pcm.AsMemory(first, count)))
        {
            segments.Add(new
            {
                time = windowStart + segment.Start.TotalSeconds,
                end = windowStart + segment.End.TotalSeconds,
                text = segment.Text,
                coreStart, coreEnd, windowStart, windowEnd,
                probability = segment.Probability,
                tokens = segment.Tokens.Select(token => new
                {
                    text = token.Text, id = token.Id,
                    time = windowStart + token.Start / 100.0,
                    end = windowStart + token.End / 100.0,
                    dtw = dtw && token.DtwTimestamp >= 0 ? windowStart + token.DtwTimestamp / 100.0 : -1,
                    probability = token.Probability
                }).ToArray()
            });
        }
    }
    return segments;
}

static float[] ReadPcm16(string path)
{
    using var reader = new BinaryReader(File.OpenRead(path));
    if (new string(reader.ReadChars(4)) != "RIFF") throw new InvalidDataException("Expected RIFF WAV.");
    reader.ReadUInt32();
    if (new string(reader.ReadChars(4)) != "WAVE") throw new InvalidDataException("Expected WAVE.");
    while (reader.BaseStream.Position < reader.BaseStream.Length)
    {
        var id = new string(reader.ReadChars(4));
        var size = reader.ReadUInt32();
        if (id == "fmt ")
        {
            if (reader.ReadUInt16()!=1 || reader.ReadUInt16()!=1 || reader.ReadUInt32()!=16000)
                throw new InvalidDataException("Expected mono PCM16 at 16 kHz.");
            reader.ReadUInt32(); reader.ReadUInt16();
            if (reader.ReadUInt16()!=16) throw new InvalidDataException("Expected PCM16.");
            reader.BaseStream.Seek(size-16,SeekOrigin.Current);
        }
        else if (id == "data")
        {
            var values = new float[size/2];
            for (int i=0;i<values.Length;i++) values[i]=reader.ReadInt16()/32768f;
            return values;
        }
        else reader.BaseStream.Seek(size,SeekOrigin.Current);
        if (size%2!=0) reader.BaseStream.Seek(1,SeekOrigin.Current);
    }
    throw new InvalidDataException("WAV has no audio data.");
}

record TrackInput(string id,string wav,double duration,string language,int threads = 4);
