# Third-party components

The MIT license in this repository covers Verse Lab. Bundled components and the installer’s downloads retain their own terms.

- [CPython](https://docs.python.org/3/license.html) and its official Windows embeddable package. The package includes its license text.
- [.NET](https://github.com/dotnet/runtime/blob/main/LICENSE.TXT), [WPF](https://github.com/dotnet/wpf/blob/main/LICENSE.TXT), and [WebView2 SDK](https://www.nuget.org/packages/Microsoft.Web.WebView2/).
- [Whisper.net](https://github.com/sandrohanea/whisper.net) and [whisper.cpp](https://github.com/ggml-org/whisper.cpp) supply the Vulkan transcription worker. [The optional Whisper Large-v3 weights](https://huggingface.co/ggerganov/whisper.cpp) are fetched and verified separately.
- [Taskbar Media Presence](https://github.com/MrBoxik/Taskbar-Media-Presence) ships with its MIT license in `setup/taskbar/LICENSE.txt`.
- Setup fetches [Mutagen](https://pypi.org/project/mutagen/), [NumPy](https://pypi.org/project/numpy/), and [FFmpeg’s Windows LGPL shared build](https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/tag/autobuild-2026-09-30-13-08) directly from their publishers and verifies their checksums. The dependencies are installed locally with their upstream notices.
