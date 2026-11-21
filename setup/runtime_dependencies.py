"""Install pinned third-party runtime downloads next to Verse Lab."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from urllib.request import urlopen
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent.parent / "runtime"
DOWNLOADS = {
    "Mutagen": (
        "https://files.pythonhosted.org/packages/b0/7a/620f945b96be1f6ee357d211d5bf74ab1b7fe72a9f1525aafbfe3aee6875/mutagen-1.47.0-py3-none-any.whl",
        "edd96f50c5907a9539d8e5bba7245f62c9f520aef333d13392a79a4f70aca719",
    ),
    "NumPy": (
        "https://files.pythonhosted.org/packages/45/74/bb9161943fb65caf24e4bb04c788f5c7815e6cb78ac595e7f46781033c82/numpy-2.5.5-cp313-cp313-win_amd64.whl",
        "e0608d1585557b68dcb6a70a41654f7536567960afad79d91752d2863b242b68",
    ),
    "FFmpeg": (
        "https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/autobuild-2026-09-30-13-08/ffmpeg-n8.1.3-9-g29e619e767-win64-lgpl-shared-8.1.zip",
        "3e47bda1607740550141e37c0e49d1e5182b34699f15adfd137ee266d346811a",
    ),
}


def install():
    marker = ROOT / "installed-dependencies.json"
    stamp = json.dumps({name: value[1] for name, value in DOWNLOADS.items()})
    files = ("python-deps/mutagen/__init__.py", "python-deps/numpy/__init__.py",
             "ffmpeg/ffmpeg.exe", "ffmpeg/ffprobe.exe")
    if marker.is_file() and marker.read_text(encoding="utf-8") == stamp and all((ROOT / f).is_file() for f in files):
        return
    with tempfile.TemporaryDirectory(prefix="verse-dependencies-", dir=ROOT) as temp:
        staging = Path(temp)
        for name, (url, expected_hash) in DOWNLOADS.items():
            print("Downloading " + name, flush=True)
            package = staging / "download.zip"
            with urlopen(url, timeout=45) as source, package.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            with package.open("rb") as data:
                if hashlib.file_digest(data, "sha256").hexdigest() != expected_hash:
                    raise ValueError(name + " download did not match its published checksum.")
            with ZipFile(package) as archive:
                if name == "FFmpeg":
                    folder = staging / "ffmpeg"
                    folder.mkdir()
                    for item in archive.infolist():
                        parts = Path(item.filename).parts
                        if not item.is_dir() and len(parts) == 3 and parts[1] == "bin":
                            (folder / parts[-1]).write_bytes(archive.read(item))
                        elif not item.is_dir() and len(parts) == 2 and parts[-1].upper().startswith(("LICENSE", "COPYING")):
                            (folder / parts[-1]).write_bytes(archive.read(item))
                else:
                    archive.extractall(staging / "python-deps")
        for folder in ("python-deps", "ffmpeg"):
            shutil.copytree(staging / folder, ROOT / folder, dirs_exist_ok=True)
    marker.write_text(stamp, encoding="utf-8")


if __name__ == "__main__":
    install()
