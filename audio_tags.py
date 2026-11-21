"""Read and update lyrics in native audio tags without re-encoding the track."""
import base64
from functools import lru_cache
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent / "runtime" / "python-deps"))
from mutagen import File, MutagenError
from mutagen.id3 import ID3, ID3NoHeaderError, SYLT, USLT
from mutagen.aiff import AIFF
from mutagen.asf import ASF
from mutagen.dsf import DSF
from mutagen.flac import FLAC, Picture
from mutagen.monkeysaudio import MonkeysAudio
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.musepack import Musepack
from mutagen.oggflac import OggFLAC
from mutagen.oggopus import OggOpus
from mutagen.oggspeex import OggSpeex
from mutagen.oggvorbis import OggVorbis
from mutagen.optimfrog import OptimFROG
from mutagen.trueaudio import TrueAudio
from mutagen.wave import WAVE
from mutagen.wavpack import WavPack

from lyric_pipeline import timed_reference

TAG_LOCK = threading.RLock()
LYRICS_FIELDS = {"lyrics", "unsyncedlyrics", "syncedlyrics", "lyrics_sync", "lyrics_synced"}
TIMESTAMP = re.compile(r"\[\d+:\d+(?:[.,]\d+)?\]")
AUDIO_EXTENSIONS = frozenset("""
    .mp3 .mp2 .mpa .wav .wave .w64 .flac .m4a .m4b .mp4 .aac .alac
    .ogg .oga .opus .spx .aif .aiff .aifc .ape .wv .wma .asf .mpc .mpp
    .tta .tak .dsf .dff .ofr .ofs .mka .ac3 .eac3 .dts .caf .au .amr .ra
""".split())
ID3_FORMATS = (AIFF, DSF, MP3, TrueAudio, WAVE)
COMMENT_FORMATS = (FLAC, OggFLAC, OggOpus, OggSpeex, OggVorbis)
APE_FORMATS = (MonkeysAudio, Musepack, OptimFROG, WavPack)


class UnsupportedTags(ValueError):
    pass


def fingerprint(path):
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def lyric_digest(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else ""


def stamp(seconds):
    ticks = round(max(0, seconds) * 100)
    return f"{ticks//6000:02}:{ticks//100%60:02}.{ticks%100:02}"


def load_tags(path):
    try:
        return ID3(path, translate=False)
    except ID3NoHeaderError:
        return ID3()


def embedded_lyrics(tags):
    unsynced = [f.text for f in tags.getall("USLT") if f.text.strip()]
    custom = [text for f in tags.getall("TXXX") if f.desc.casefold() in LYRICS_FIELDS
              for text in f.text if text.strip()]
    for raw in unsynced + custom:
        if TIMESTAMP.search(raw):
            return raw
    synced = [f for f in tags.getall("SYLT") if f.type == 1 and f.text]
    for frame in synced:
        if frame.format == 2:
            return "\n".join(f"[{stamp(ms/1000)}]{text}" for text, ms in frame.text if text.strip())
    if unsynced or custom:
        return (unsynced + custom)[0]
    return "\n".join(text for text, _ in synced[0].text) if synced else ""


def tag_format(audio):
    if isinstance(audio, ID3_FORMATS):
        return "id3"
    if isinstance(audio, MP4):
        return "mp4"
    if isinstance(audio, ASF):
        return "asf"
    if isinstance(audio, COMMENT_FORMATS + APE_FORMATS):
        return "comments"
    return ""


def text_from_tags(audio, tags, layout):
    if not tags:
        return ""
    if layout == "id3":
        return embedded_lyrics(tags)
    fields = {"mp4": {"©lyr"}, "asf": {"wm/lyrics", "wm/lyrics_synchronised"}}.get(layout, LYRICS_FIELDS)
    found = []
    for key in tags.keys():
        if key.casefold() not in fields:
            continue
        values = tags[key]
        if not isinstance(values, list):
            values = [values]
        for value in values:
            value = getattr(value, "value", value)
            if not isinstance(value, bytes) and str(value).strip():
                found.append(str(value))
    return next((raw for raw in found if TIMESTAMP.search(raw)), found[0] if found else "")


def pictures_from_tags(audio, tags, layout):
    if layout == "id3" and tags:
        for picture in sorted(tags.getall("APIC"), key=lambda frame: frame.type != 3):
            yield picture.mime, picture.data
    elif isinstance(audio, FLAC):
        for picture in sorted(audio.pictures, key=lambda frame: frame.type != 3):
            yield picture.mime, picture.data
    elif isinstance(audio, COMMENT_FORMATS) and tags:
        pictures = []
        for encoded in tags.get("metadata_block_picture", []):
            try:
                pictures.append(Picture(base64.b64decode(encoded)))
            except (MutagenError, ValueError, EOFError):
                continue
        for picture in sorted(pictures, key=lambda frame: frame.type != 3):
            yield picture.mime, picture.data
    elif layout == "mp4" and tags:
        for picture in tags.get("covr", []):
            yield ("image/png" if picture.imageformat == 14 else "image/jpeg"), picture
    elif isinstance(audio, APE_FORMATS) and tags:
        cover = tags.get("Cover Art (Front)")
        if cover is not None and isinstance(cover.value, bytes):
            _, _, image = cover.value.partition(b"\x00")
            yield ("image/png" if image.startswith(b"\x89PNG") else "image/jpeg"), image


def open_audio_tags(path):
    if path.suffix.lower() == ".mp3":
        return None, load_tags(path), "id3"
    audio = File(path)
    return audio, audio.tags if audio is not None else None, tag_format(audio)


def read_audio_tags(path, include_art=False):
    try:
        if not include_art:
            return cached_embedded_lyrics(path, fingerprint(path)), ""
        audio, tags, layout = open_audio_tags(path)
        raw = text_from_tags(audio, tags, layout)
        for mime, data in pictures_from_tags(audio, tags, layout):
            mime = mime.lower().replace("image/jpg", "image/jpeg")
            if mime in {"image/jpeg", "image/png", "image/webp", "image/gif"} and 0 < len(data) <= 5_000_000:
                return raw, f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        return raw, ""
    except (MutagenError, OSError, ValueError, EOFError):
        return "", ""


@lru_cache(maxsize=8192)
def cached_embedded_lyrics(path, file_version):
    return text_from_tags(*open_audio_tags(path))


def lyrics_text(state):
    """Serialize actual timestamps. Display-only estimates stay out of the tag."""
    lines = state.get("lines") or []
    offset, scale = state.get("offset", 0), state.get("scale", 1)
    output = []
    for line in lines:
        text = line.get("text", "").strip()
        if not text or line.get("uncertain"):
            continue
        time = line.get("time")
        if not isinstance(time, (int, float)) or not math.isfinite(time):
            output.append(text)
            continue
        words = line.get("words") or []
        if words and " ".join("".join(word["text"] for word in words).split()) == " ".join(text.split()):
            text = "".join(f"<{stamp(word['time']*scale+offset)}>{word['text']}" for word in words)
        output.append(f"[{stamp(time*scale+offset)}]{text}")
    return "\n".join(output) if output else (state.get("raw") or state.get("lyrics") or "").strip()


def replace_while_reading(source, target):
    try:
        os.replace(source, target)
        return
    except PermissionError:
        if os.name != "nt":
            raise

    # Windows normally rejects replacement while even a shared-delete reader has the file open.
    import ctypes as ct
    from ctypes import wintypes as win

    class RenameInfo(ct.Structure):
        _fields_ = [("flags", win.DWORD), ("root", win.HANDLE), ("name_length", win.DWORD),
                    ("name", ct.c_wchar * 1)]

    kernel = ct.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [win.LPCWSTR, win.DWORD, win.DWORD, win.LPVOID, win.DWORD, win.DWORD, win.HANDLE]
    kernel.CreateFileW.restype = win.HANDLE
    kernel.SetFileInformationByHandle.argtypes = [win.HANDLE, ct.c_int, win.LPVOID, win.DWORD]
    kernel.SetFileInformationByHandle.restype = win.BOOL
    kernel.CloseHandle.argtypes = [win.HANDLE]
    delete_access, share_all, open_existing = 0x10000, 7, 3
    handle = kernel.CreateFileW(str(source), delete_access, share_all, None, open_existing, 0, None)
    if handle == ct.c_void_p(-1).value:
        raise ct.WinError(ct.get_last_error())
    try:
        name = str(target).encode("utf-16-le")
        buffer = ct.create_string_buffer(max(ct.sizeof(RenameInfo), RenameInfo.name.offset + len(name) + 2))
        info = RenameInfo.from_buffer(buffer)
        info.flags = 0x01 | 0x02  # Replace the name while allowing existing shared readers to finish.
        info.name_length = len(name)
        ct.memmove(ct.addressof(buffer) + RenameInfo.name.offset, name, len(name))
        file_rename_info_ex = 22
        if not kernel.SetFileInformationByHandle(handle, file_rename_info_ex, buffer, len(buffer)):
            raise ct.WinError(ct.get_last_error())
    finally:
        kernel.CloseHandle(handle)


def update_id3(tags, raw):
    version = 3 if tags.version[1] == 3 else 4
    encoding = 1 if version == 3 else 3
    tags.delall("USLT")
    tags.delall("SYLT")
    for frame in tags.getall("TXXX"):
        if frame.desc.casefold() in LYRICS_FIELDS:
            del tags[frame.HashKey]
    tags.add(USLT(encoding=encoding, lang="und", desc="", text=raw))
    synced = [(line["text"], round(max(0, line["time"]) * 1000)) for line in timed_reference(raw)]
    if synced:
        tags.add(SYLT(encoding=encoding, lang="und", format=2, type=1, desc="", text=synced))
    return version


def write_tags(path, raw):
    audio, tags, layout = open_audio_tags(path)
    if not layout:
        raise UnsupportedTags("This audio format has no supported writable lyrics tag.")
    if tags is None:
        audio.add_tags()
        tags = audio.tags
    if layout == "id3":
        version = update_id3(tags, raw)
        if audio is None:
            tags.save(path, v2_version=version)
        elif isinstance(audio, (MP3, TrueAudio, AIFF, WAVE)):
            audio.save(v2_version=version)
        else:
            audio.save()
    else:
        field = {"mp4": "©lyr", "asf": "WM/Lyrics", "comments": "LYRICS"}[layout]
        aliases = {"WM/Lyrics_Synchronised".casefold()} if layout == "asf" else LYRICS_FIELDS if layout == "comments" else set()
        for key in list(tags.keys()):
            if key.casefold() in aliases:
                del tags[key]
        tags[field] = raw if isinstance(audio, APE_FORMATS) else [raw]
        audio.save()


def write_lyrics(path, raw, expected):
    """Stage edits beside the file and replace only after verifying the tags."""
    with TAG_LOCK:
        before = fingerprint(path)
        if expected and before != expected:
            raise ValueError("The audio file changed before lyrics could be embedded.")
        audio, tags, layout = open_audio_tags(path)
        if not layout:
            raise UnsupportedTags("This audio format has no supported writable lyrics tag.")
        if text_from_tags(audio, tags, layout) == raw:
            return before
        fd, name = tempfile.mkstemp(prefix=".verse-lyrics-", suffix=path.suffix, dir=path.parent)
        os.close(fd)
        temporary = Path(name)
        try:
            shutil.copy2(path, temporary)
            write_tags(temporary, raw)
            if text_from_tags(*open_audio_tags(temporary)) != raw:
                raise ValueError("The new lyric tags could not be verified.")
            for attempt in range(3):
                if fingerprint(path) != before:
                    raise ValueError("The audio file changed while lyrics were being saved.")
                try:
                    replace_while_reading(temporary, path)
                    return fingerprint(path)
                except PermissionError:
                    if attempt == 2:
                        raise
                    time.sleep(.12)
        finally:
            temporary.unlink(missing_ok=True)


def save_audio_lyrics(path, raw):
    with TAG_LOCK:
        try:
            version = write_lyrics(path, raw, fingerprint(path))
            return {"fingerprint": version, "embeddedDigest": lyric_digest(raw)}
        except (UnsupportedTags, MutagenError):
            sidecar = path.with_name(path.name + ".lrc")
            marker = "[by:Verse Lab]\n"
            if sidecar.exists() and not sidecar.read_text(encoding="utf-8-sig").startswith(marker):
                raise ValueError("This format uses sidecars and already has a separate LRC file.")
            fd, name = tempfile.mkstemp(prefix=".verse-lyrics-", suffix=".tmp", dir=path.parent)
            temporary = Path(name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as document:
                    document.write(marker + raw + "\n")
                os.replace(temporary, sidecar)
            finally:
                temporary.unlink(missing_ok=True)
            return {"sidecarSaved": True}
