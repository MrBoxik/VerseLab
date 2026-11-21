# Verse Lab

Verse Lab is a Windows lyrics player and optional companion to [Taskbar Media Presence](https://github.com/MrBoxik/Taskbar-Media-Presence). It plays a local music folder, follows compatible Windows media players, and sends timed lyrics to the taskbar widget. MusicBee’s companion adds the local file and the upcoming queue, including Auto-DJ.

## Install

1. Download and extract `VerseLab-win-x64.zip` from [Releases](https://github.com/MrBoxik/VerseLab/releases).
2. Install the [.NET 10 Desktop Runtime](https://dotnet.microsoft.com/download/dotnet/10.0), [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/), Python 3.11 or newer, and FFmpeg on your Windows PATH.
3. In the extracted folder, run `python -m pip install -r requirements.txt`, then open **Verse Lab.exe**. Select your music folder in the player.
4. For offline lyric generation, install the .NET 10 SDK and run `python setup_transcription.py` from the same folder. It downloads and checks the roughly 3.1 GB Whisper Large-v3 model and builds the Vulkan worker.

## Lyrics and the taskbar

Verse Lab prefers embedded lyrics. Online matches must show the same duration to the second and differ by at most 50 milliseconds. If no strict match exists, Verse Lab can prepare a draft from a local audio file. Timed preparation can process a folder in the tray and embed results in supported audio tags. Formats without a writable lyrics tag use a companion LRC file.

Open **Taskbar setup** in Verse Lab and follow [the local setup instructions](setup/taskbar/SETUP.md). [Taskbar Media Presence](https://github.com/MrBoxik/Taskbar-Media-Presence) runs in Windhawk. With both apps running, enable **Show lyrics** on the widget. **Automatically fit lyrics** sizes the strip from the longest line and the available taskbar space.

The app sends the song title and artist to LRCLIB when looking up lyrics. Local transcription runs on your PC.

## Build

```powershell
dotnet publish desktop/VerseLab.csproj -c Release -r win-x64 --self-contained false -p:PublishSingleFile=true -o desktop/release
Copy-Item -LiteralPath 'desktop/release/Verse Lab.exe' -Destination 'Verse Lab.exe'
python -m unittest discover -s tests
node --test tests/lyrics.test.mjs
```
