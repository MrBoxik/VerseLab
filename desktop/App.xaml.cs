using System.IO;
using System.Runtime.InteropServices;
using System.Windows;
using Forms = System.Windows.Forms;

namespace VerseLab;

public partial class App : System.Windows.Application
{
    private Mutex? instance;
    private string root = "";
    private bool switching;
    private Forms.NotifyIcon? tray;
    private Forms.ToolStripItem? statusItem;
    private TaskbarLyricsWorker? taskbar;
    private Backend? backend;
    private bool exiting;

    internal Backend Bridge => backend ??= new Backend(root);
    internal TaskbarLyricsWorker? MusicBee => taskbar;
    internal bool CloseToTray => !File.Exists(Path.Combine(root, "data", "close-to-tray-off"));

    protected override async void OnStartup(StartupEventArgs e)
    {
        _ = SetCurrentProcessExplicitAppUserModelID("VerseLab.Desktop");
        instance = new Mutex(true, @"Local\VerseLabDesktop", out bool first);
        if (!first)
        {
            System.Windows.MessageBox.Show("Verse Lab is already open. Use its window or system tray icon.", "Verse Lab");
            Shutdown();
            return;
        }
        base.OnStartup(e);
        var folder = new DirectoryInfo(AppContext.BaseDirectory);
        while (!File.Exists(Path.Combine(folder.FullName, "desktop_bridge.py")) && folder.Parent != null)
            folder = folder.Parent;
        root = folder.FullName;
        try { taskbar = new TaskbarLyricsWorker(Bridge, UpdateStatus); }
        catch (Exception error) { UpdateStatus(error.Message); }
        var launcher = Path.GetFileNameWithoutExtension(Environment.ProcessPath);
        if (e.Args.Contains("--taskbar") || (launcher?.Equals("Taskbar Lyrics", StringComparison.OrdinalIgnoreCase) ?? false) ||
            File.Exists(ModeFile))
            await EnterTaskbarMode();
        else
            OpenWindow(new PlayerWindow());
    }

    private string ModeFile => Path.Combine(root, "data", "taskbar-mode");

    internal void OpenWindow(Window window)
    {
        MainWindow = window;
        window.Closed += (_, _) =>
        {
            if (!switching && !exiting && (MainWindow == null || ReferenceEquals(MainWindow, window)))
            {
                MainWindow = null;
                _ = ExitVerseLab();
            }
        };
        window.Show();
    }

    internal async Task EnterTaskbarMode()
    {
        if (switching)
            return;
        switching = true;
        try
        {
            await ClosePlayer();
            ShowTray();
            Directory.CreateDirectory(Path.GetDirectoryName(ModeFile)!);
            File.WriteAllText(ModeFile, "enabled");
        }
        catch (Exception error)
        {
            DisposeTray();
            System.Windows.MessageBox.Show(error.Message, "Taskbar mode");
            OpenWindow(new PlayerWindow());
        }
        finally { switching = false; }
    }

    internal void SetCloseToTray(bool enabled)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(ModeFile)!);
        string preference = Path.Combine(root, "data", "close-to-tray-off");
        if (enabled) File.Delete(preference);
        else File.WriteAllText(preference, "");
    }

    internal bool HideOnClose(Window window)
    {
        if (switching || exiting || !CloseToTray || !ReferenceEquals(MainWindow, window))
            return false;
        ShowTray();
        window.Hide();
        return true;
    }

    private void ShowTray()
    {
        if (tray != null) return;
        var menu = new Forms.ContextMenuStrip();
        statusItem = menu.Items.Add(taskbar?.Status ?? "Looking for MusicBee…");
        statusItem.Enabled = false;
        menu.Items.Add("Open Verse Lab player", null, (_, _) => Restore());
        menu.Items.Add("Open bundled setup files", null, (_, _) => TaskbarSetup.OpenFolder(root));
        menu.Items.Add("Exit Verse Lab", null, async (_, _) => await ExitVerseLab());
        tray = new Forms.NotifyIcon
        {
            Icon = new System.Drawing.Icon(Path.Combine(root, "dist", "verse-lab.ico")),
            Text = "Verse Lab",
            ContextMenuStrip = menu,
            Visible = true,
        };
        tray.DoubleClick += (_, _) => Restore();
        UpdateStatus(taskbar?.Status ?? "Looking for MusicBee…");
    }

    private void UpdateStatus(string text)
    {
        if (statusItem != null) statusItem.Text = text;
        if (tray != null) tray.Text = text.Length > 63 ? text[..63] : text;
    }

    private void Restore()
    {
        if (switching || exiting)
            return;
        DisposeTray();
        File.Delete(ModeFile);
        if (MainWindow is Window window)
        {
            window.Show();
            if (window.WindowState == WindowState.Minimized) window.WindowState = WindowState.Normal;
            window.Activate();
        }
        else OpenWindow(new PlayerWindow());
    }

    private async Task ExitVerseLab()
    {
        if (exiting) return;
        exiting = true;
        try
        {
            await ClosePlayer();
            if (taskbar != null) await taskbar.Stop();
            if (backend != null) await backend.DisposeAsync();
        }
        finally { Shutdown(); }
    }

    private async Task ClosePlayer()
    {
        if (MainWindow is not Window window) return;
        var closed = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        window.Closed += (_, _) => closed.TrySetResult();
        window.Close();
        await closed.Task;
        MainWindow = null;
    }

    private void DisposeTray()
    {
        if (tray == null)
            return;
        tray.Visible = false;
        tray.ContextMenuStrip?.Dispose();
        var icon = tray.Icon;
        tray.Dispose();
        icon?.Dispose();
        tray = null;
        statusItem = null;
    }

    protected override void OnExit(ExitEventArgs e)
    {
        DisposeTray();
        instance?.Dispose();
        base.OnExit(e);
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
    private static extern int SetCurrentProcessExplicitAppUserModelID(string id);
}
