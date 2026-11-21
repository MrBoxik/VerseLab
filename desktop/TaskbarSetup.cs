using System.Diagnostics;
using System.IO;
using System.Windows;

namespace VerseLab;

internal static class TaskbarSetup
{
    internal static string Bundle(string root) => Path.Combine(root, "setup", "taskbar");

    internal static void OpenFolder(string root) => Start(Bundle(root));

    internal static void CopySource(string root)
    {
        Clipboard.SetText(File.ReadAllText(Path.Combine(Bundle(root), "taskbar-media-presence.wh.cpp")));
    }

    internal static void OpenWindhawk()
    {
        string path = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Windhawk", "windhawk.exe");
        Start(File.Exists(path) ? path : "https://windhawk.net/");
    }

    private static void Start(string path)
    {
        Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
    }
}
