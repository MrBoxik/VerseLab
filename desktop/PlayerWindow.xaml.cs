using System.ComponentModel;
using System.IO;
using System.Text.Json;
using System.Windows;
using Microsoft.Web.WebView2.Core;
using Microsoft.Win32;

namespace VerseLab;

public partial class PlayerWindow : Window
{
    private const string Origin = "https://verse-lab.invalid/";
    private const string Page = Origin + "index.html";
    internal const string AudioFilter = "Audio files|*.mp3;*.mp2;*.mpa;*.wav;*.flac;*.m4a;*.m4b;*.mp4;*.aac;*.alac;*.ogg;*.oga;*.opus;*.spx;*.aif;*.aiff;*.ape;*.wv;*.wma;*.asf;*.mpc;*.tta;*.tak;*.dsf;*.dff;*.mka;*.ac3;*.eac3;*.dts;*.caf;*.au;*.amr|All files|*.*";
    private readonly TaskCompletionSource pageReady = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private Backend? backend;
    private LocalAudioResources? localAudio;
    private CancellationTokenSource? startupCancellation;
    private string? startupLog;
    private string projectRoot = "";
    private bool closing, closed;

    public PlayerWindow() => InitializeComponent();

    private void MinimizeWindow(object sender, RoutedEventArgs e) => SystemCommands.MinimizeWindow(this);

    private void ToggleMaximized(object sender, RoutedEventArgs e)
    {
        if (WindowState == WindowState.Maximized)
            SystemCommands.RestoreWindow(this);
        else
            SystemCommands.MaximizeWindow(this);
    }

    private void CloseWindow(object sender, RoutedEventArgs e) => SystemCommands.CloseWindow(this);

    private async void OpenPlayer(object? sender, EventArgs e)
    {
        ContentRendered -= OpenPlayer;
        using var startup = new CancellationTokenSource(TimeSpan.FromSeconds(15));
        startupCancellation = startup;
        var stage = "finding the player files";
        try
        {
            var root = new DirectoryInfo(AppContext.BaseDirectory);
            while (!File.Exists(Path.Combine(root.FullName, "desktop_bridge.py")))
                root = root.Parent ?? throw new FileNotFoundException("Open Verse Lab.exe in its project folder.");
            projectRoot = root.FullName;
            startupLog = Path.Combine(root.FullName, "data", "player-startup.log");
            LogStartup("Opening the embedded player", reset: true);
            stage = "starting WebView2";
            var environment = await CoreWebView2Environment.CreateAsync(
                userDataFolder: Path.Combine(root.FullName, "runtime", "webview"),
                options: new CoreWebView2EnvironmentOptions("--autoplay-policy=no-user-gesture-required --disable-features=HardwareMediaKeyHandling"))
                .WaitAsync(startup.Token);
            if (closing)
                return;
            stage = "initializing the embedded view";
            LogStartup("WebView2 environment ready");
            await View.EnsureCoreWebView2Async(environment).WaitAsync(startup.Token);
            if (closing)
                return;
            stage = "starting the lyrics worker";
            LogStartup("Embedded view ready");
            backend = ((App)Application.Current).Bridge;
            var web = View.CoreWebView2;
            web.Settings.AreDevToolsEnabled = false;
            web.Settings.AreDefaultContextMenusEnabled = false;
            web.Settings.AreBrowserAcceleratorKeysEnabled = false;
            web.Settings.IsStatusBarEnabled = false;
            web.SetVirtualHostNameToFolderMapping("verse-lab.invalid", Path.Combine(root.FullName, "dist"),
                CoreWebView2HostResourceAccessKind.Deny);
            localAudio = new LocalAudioResources(web);
            web.WebMessageReceived += Receive;
            web.NavigationStarting += (_, navigation) => navigation.Cancel = navigation.Uri != Page;
            web.NewWindowRequested += (_, requested) => requested.Handled = true;
            web.NavigationCompleted += (_, result) =>
            {
                if (!result.IsSuccess)
                    pageReady.TrySetException(new InvalidOperationException($"Navigation failed ({result.WebErrorStatus})."));
            };
            stage = "loading the local player page";
            LogStartup("Loading the local page");
            web.Navigate(Page);
            await pageReady.Task.WaitAsync(startup.Token);
            if (!closing)
            {
                StartupStatus.Visibility = Visibility.Collapsed;
                LogStartup("Player page ready");
            }
        }
        catch (OperationCanceledException)
        {
            if (!closing)
                OpenFallback($"Startup timed out while {stage}.");
        }
        catch (Exception error)
        {
            if (!closing)
                OpenFallback($"Failed while {stage}. {error.Message}");
        }
        finally
        {
            startupCancellation = null;
        }
    }

    private void LogStartup(string message, bool reset = false)
    {
        if (startupLog == null)
            return;
        try
        {
            var line = $"{DateTimeOffset.UtcNow:O} {message}{Environment.NewLine}";
            if (reset)
                File.WriteAllText(startupLog, line);
            else
                File.AppendAllText(startupLog, line);
        }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }

    private void OpenFallback(string error)
    {
        LogStartup($"{error} Opening the desktop fallback.");
        ((App)Application.Current).OpenWindow(new MainWindow());
        Close();
    }

    private async void Receive(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        if (closing || e.Source != Page || backend == null)
            return;
        int requestId = 0;
        try
        {
            using var document = JsonDocument.Parse(e.WebMessageAsJson);
            var envelope = document.RootElement;
            if (envelope.Flag("ready"))
            {
                pageReady.TrySetResult();
                return;
            }
            requestId = envelope.GetProperty("requestId").GetInt32();
            var request = envelope.GetProperty("request");
            var result = await Dispatch(request);
            Reply(new { requestId, value = result });
        }
        catch (Exception error)
        {
            Reply(new { requestId, error = error.Message });
        }
    }

    private void Reply(object reply)
    {
        if (!closing)
            View.CoreWebView2.PostWebMessageAsJson(JsonSerializer.Serialize(reply, Backend.Json));
    }

    private async Task<object?> Dispatch(JsonElement request)
    {
        var bridge = backend!;
        var action = request.String("action");
        switch (action)
        {
            case "musicBee":
                var musicBee = ((App)Application.Current).MusicBee;
                return musicBee?.Follow(request.Get("revision").ValueKind == JsonValueKind.Number
                    ? request.Get("revision").GetInt32() : -1);
            case "closeToTray":
                var app = (App)Application.Current;
                if (request.TryGetProperty("enabled", out var enabled))
                    app.SetCloseToTray(enabled.GetBoolean());
                return app.CloseToTray;
            case "taskbarMode":
                _ = Dispatcher.BeginInvoke(new Action(async () => await ((App)Application.Current).EnterTaskbarMode()));
                return true;
            case "taskbarBundle":
                TaskbarSetup.OpenFolder(projectRoot);
                return true;
            case "copyTaskbarSource":
                TaskbarSetup.CopySource(projectRoot);
                return true;
            case "openWindhawk":
                TaskbarSetup.OpenWindhawk();
                return true;
            case "chooseFolder":
                var folder = new OpenFolderDialog { Title = "Choose your music folder" };
                if (folder.ShowDialog(this) != true)
                    return null;
                return await bridge.Send(new { action = "library", folder = folder.FolderName });
            case "openFile":
                var file = new OpenFileDialog { Title = "Open audio", Filter = AudioFilter };
                if (file.ShowDialog(this) != true)
                    return null;
                return await bridge.Send(new { action = "file", path = file.FileName });
            case "import":
                var import = new OpenFileDialog { Title = "Import lyrics", Filter = "Lyrics|*.lrc;*.txt" };
                if (import.ShowDialog(this) != true)
                    return null;
                if (new FileInfo(import.FileName).Length > 2_000_000)
                    throw new IOException("The lyric file is too large.");
                return await File.ReadAllTextAsync(import.FileName);
            case "export":
                var export = new SaveFileDialog
                {
                    Filter = "LRC lyric file|*.lrc", DefaultExt = ".lrc",
                    FileName = request.String("title", "Lyrics") + ".lrc",
                };
                if (export.ShowDialog(this) != true)
                    return false;
                await File.WriteAllTextAsync(export.FileName, request.String("lyrics"));
                return true;
            case "track":
                var track = await bridge.Send(request);
                var metadata = track.Get("metadata");
                return new
                {
                    metadata, saved = track.Get("saved"), prepared = track.Get("prepared"),
                    audioUrl = localAudio!.Register(request.String("key"), metadata.String("path")),
                };
            case "decodePlayback":
                var decoded = await bridge.Send(request);
                return new { audioUrl = localAudio!.Register(decoded.String("revision"), decoded.String("path")) };
            case "library":
                return await bridge.Send(new { action = "library" });
            case "save":
            case "prepare":
            case "result":
            case "cancel":
            case "status":
            case "batch":
            case "search":
                return await bridge.Send(request);
            case "prefetch":
                if (((App)Application.Current).MusicBee?.Current?.CanDisplay == true)
                    return await bridge.Send(new { action = "status" });
                return await bridge.Send(request);
            default:
                throw new InvalidOperationException("Unknown player command.");
        }
    }

    private void ClosePlayer(object? sender, CancelEventArgs e)
    {
        if (closed)
            return;
        e.Cancel = true;
        if (closing)
            return;
        if (((App)Application.Current).HideOnClose(this))
            return;
        closing = true;
        startupCancellation?.Cancel();
        try
        {
            View.Dispose();
        }
        catch (Exception error)
        {
            LogStartup($"Shutdown failed. {error.Message}");
        }
        finally
        {
            closed = true;
            _ = Dispatcher.BeginInvoke(new Action(Close));
        }
    }
}
