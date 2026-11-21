# Verse Lab

Verse Lab is a Windows lyrics player and optional companion to [Taskbar Media Presence](https://github.com/MrBoxik/Taskbar-Media-Presence). It plays a local music folder, follows compatible Windows media players, and sends timed lyrics to the taskbar widget. MusicBee’s companion adds the local file and the upcoming queue, including Auto-DJ.

## Install

1. Download and run [VerseLab-Setup-win-x64.exe](https://github.com/MrBoxik/VerseLab/releases/latest/download/VerseLab-Setup-win-x64.exe) on Windows 11 x64. Setup uses your Windows account and downloads the audio tools over the internet.
2. Select **Offline transcription model** in Setup if you want Verse Lab to generate missing lyrics. This adds a roughly 3.1 GB download and uses a Vulkan-capable GPU. You can run Setup again to add it later.
3. Open **Verse Lab** from Start and select your music folder. The desktop view uses the Windows WebView2 runtime.

## Lyrics and the taskbar

Verse Lab prefers embedded lyrics. Online matches must show the same duration to the second and differ by at most 50 milliseconds. If no strict match exists, Verse Lab can prepare a draft from a local audio file. Timed preparation can process a folder in the tray and embed results in supported audio tags. Formats without a writable lyrics tag use a companion LRC file.

In **Library preparation**, set **Maximum transcription length** to skip local transcription for longer tracks, including audiobooks. The default is 20 minutes. Use 0 for no limit. The setting applies as tracks are picked up for transcription, and embedded or matching online lyrics remain available.

**Number of agents** sets the maximum from one to four for normal playback and library preparation. Each agent transcribes a different recording. Verse Lab uses current dedicated GPU memory to decide how many workers to load. It uses one worker when it cannot identify the GPU budget.

Open **Taskbar setup** in Verse Lab and follow [the local setup instructions](setup/taskbar/SETUP.md). [Taskbar Media Presence](https://github.com/MrBoxik/Taskbar-Media-Presence) runs in Windhawk. With both apps running, enable **Show lyrics** on the widget. **Automatically fit lyrics** sizes the strip from the longest line and the available taskbar space.

The app sends the song title and artist to LRCLIB when looking up lyrics. Local transcription runs on your PC.

## Build

Install the .NET 10 SDK, Python 3.11 or newer, and FFmpeg. For a development checkout, install the Python dependencies with `python -m pip install -r requirements.txt`. Run `python setup_transcription.py` for the model and development worker.

```powershell
dotnet publish desktop/VerseLab.csproj -c Release -r win-x64 --self-contained false -p:PublishSingleFile=true -o desktop/release
Copy-Item -LiteralPath 'desktop/release/Verse Lab.exe' -Destination 'Verse Lab.exe'
python -m unittest discover -s tests
node --test tests/lyrics.test.mjs
```

To build the Windows installer, download the [NSIS compiler](https://sourceforge.net/projects/nsis/files/NSIS%203/3.11/) and run `setup/build-installer.ps1 -NsisCompiler path/to/makensis.exe`. The build writes the executable to `runtime/installer-build`.

## License and support

Verse Lab is available under the [MIT license](LICENSE). See the [third-party notices](THIRD_PARTY.md) for bundled and downloaded components. [Support MrBoxik on Buy Me a Coffee](https://buymeacoffee.com/mrboxik).
