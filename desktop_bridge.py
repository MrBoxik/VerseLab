"""Private stdin/stdout interface for the native Windows player."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import app
from lyric_pipeline import PIPELINE_VERSION, matches_lyric_duration, tokens
from audio_tags import AUDIO_EXTENSIONS, fingerprint, lyric_digest, lyrics_text, read_audio_tags, save_audio_lyrics
from transcription import Preparation


REGISTERED_FILES = {}


def register_file(filename):
    path = Path(filename).resolve(strict=True)
    if not path.is_file():
        raise ValueError("Choose an audio file.")
    if path.suffix.lower() not in AUDIO_EXTENSIONS:
        probe = shutil.which("ffprobe")
        if not probe:
            raise ValueError("FFprobe is needed to identify this file.")
        check = subprocess.run([probe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=index",
                                "-of", "csv=p=0", str(path)], capture_output=True, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if check.returncode != 0 or not check.stdout.strip():
            raise ValueError("No readable audio stream was found in this file.")
    key = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:24]
    with app.LIB_LOCK:
        app.LIBRARY[key] = path
        REGISTERED_FILES[key] = path
    return {"key": key, "name": path.stem}


def decode_playback(key):
    path = app.library_path(key)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("FFmpeg is required to play this audio format.")
    folder = app.DATA / "playback-cache"
    folder.mkdir(exist_ok=True)
    signature = hashlib.sha256(f"{key}:{fingerprint(path)}".encode()).hexdigest()[:24]
    target = folder / (signature + ".flac")
    if not target.exists():
        temporary = target.with_suffix(".tmp")
        try:
            converted = subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(path), "-map", "0:a:0", "-vn", "-sn",
                                        "-map_metadata", "-1", "-ac", "2", "-ar", "48000", "-c:a", "flac", "-compression_level", "1",
                                        "-f", "flac", str(temporary)],
                                       capture_output=True, timeout=90,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if converted.returncode != 0:
                raise ValueError("FFmpeg could not decode this recording.")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    target.touch(exist_ok=True)
    for older in sorted(folder.glob("*.flac"), key=lambda item: item.stat().st_mtime_ns, reverse=True)[6:]:
        try:
            older.unlink()
        except OSError:
            pass
    return {"path": str(target), "revision": signature}


def reindex():
    app.scan_library()
    with app.LIB_LOCK:
        folder_files = dict(app.LIBRARY)
        app.LIBRARY.update(REGISTERED_FILES)
    return folder_files


def list_tracks():
    tracks = [{"key": key, "name": path.stem} for key, path in reindex().items()]
    return {"folder": str(app.MUSIC_ROOT), "tracks": sorted(tracks, key=lambda item: item["name"].casefold())}


def display_lyrics(manager, key):
    path = app.library_path(key)
    embedded, _ = read_audio_tags(path)
    saved = app.read_state(key)
    prepared = manager.cached(key)
    if embedded:
        digest = lyric_digest(embedded)
        for candidate in (saved, prepared):
            if candidate and candidate.get("embeddedDigest") == digest:
                return candidate
        return {"raw": embedded, "source": "Embedded lyrics"}
    if (saved.get("lines") or saved.get("lyrics")) and saved.get("fingerprint") == fingerprint(path):
        return saved
    sidecar, _ = app.local_sidecar(path)
    return {"raw": sidecar, "source": "Local lyrics"} if sidecar else prepared


def save_state(key, state):
    path = app.state_path(key)
    with app.STATE_LOCK:
        if path.exists():
            previous = json.loads(path.read_text(encoding="utf-8"))
            if previous.get("lines") and previous.get("pipelineVersion", 0) < PIPELINE_VERSION:
                history = app.DATA / "history"
                history.mkdir(exist_ok=True)
                backup = history / f"{key}-before-v{PIPELINE_VERSION}-desktop.json"
                if not backup.exists():
                    app.atomic_json(backup, previous)
        app.atomic_json(path, state)


def dispatch(manager, request):
    action = request["action"]
    key = request.get("key")
    if action == "library":
        if request.get("folder"):
            folder = Path(request["folder"]).resolve(strict=True)
            if not folder.is_dir():
                raise ValueError("Choose a music folder.")
            manager.cancel()
            app.MUSIC_ROOT = folder
            app.CONFIG["music_root"] = str(folder)
            app.atomic_json(app.BASE / "config.json", app.CONFIG)
        return list_tracks()
    if action == "index":
        reindex()
        return True
    if action == "file":
        return register_file(request["path"])
    if action == "track":
        return {"metadata": app.track_metadata(key, include_art=True), "saved": app.read_state(key),
                "prepared": manager.cached(key)}
    if action == "decodePlayback":
        return decode_playback(key)
    if action == "lyricsState":
        return display_lyrics(manager, key)
    if action == "streamLyrics":
        title, artist = request["title"], request["artist"]
        duration = request["duration"]
        if not tokens(title) or not tokens(artist) or duration <= 0:
            return None
        rows = app.search_lyrics(artist, title)["results"]
        matches = [row for row in rows if row.get("syncedLyrics")
                   and tokens(row.get("trackName") or "") == tokens(title)
                   and tokens(row.get("artistName") or "") == tokens(artist)
                   and matches_lyric_duration(row, duration)]
        if matches:
            best = min(matches, key=lambda row: abs(float(row["duration"]) - duration))
            return {"raw": best["syncedLyrics"], "source": "Synced lyrics · LRCLIB"}
        return None
    if action == "save":
        state = request["state"]
        response = {}
        if request.get("embed"):
            path = app.library_path(key)
            raw = lyrics_text(state)
            if raw:
                try:
                    response = save_audio_lyrics(path, raw)
                    state.update(response)
                except Exception as error:
                    response["embedError"] = str(error)
        save_state(key, state)
        return response
    if action == "prepare":
        return manager.prepare(key, request.get("reason", "current"), request.get("force", False))
    if action == "result":
        return manager.cached(key)
    if action == "prefetch":
        return manager.prefetch(request["keys"], key)
    if action == "cancel":
        return manager.cancel(key)
    if action == "status":
        return manager.status()
    if action == "transcriptionLimit":
        return manager.configure({"maxTrackMinutes": request["minutes"]})["settings"]["maxTrackMinutes"]
    if action == "transcriptionAgents":
        return manager.configure({"agents": request["count"]})["agents"]
    if action == "batch":
        if request.get("pause"):
            return manager.cancel_batch()
        keys = request.get("keys")
        if keys is None:
            keys = list(reindex())
        return manager.start_batch(keys, request.get("minutes", 0))
    if action == "search":
        duration = request.get("duration", 0)
        rows = app.search_lyrics(request["artist"], request["title"])["results"]
        return sorted((row for row in rows if matches_lyric_duration(row, duration)),
                      key=lambda row: abs(float(row["duration"]) - duration))
    raise ValueError("Unknown player command.")


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    manager = Preparation(app.BASE, app.library_path, app.track_metadata, app.search_lyrics, app.read_state,
                          lambda artist, title: app.search_lyrics(artist, title, cached_only=True),
                          defaults_only=True, find_original=app.original_recording, embed_audio=True)
    try:
        for line in sys.stdin:
            try:
                response = {"value": dispatch(manager, json.loads(line))}
            except Exception as error:
                response = {"error": str(error) or type(error).__name__}
            print(json.dumps(response, ensure_ascii=False))
    finally:
        manager.close()


if __name__ == "__main__":
    main()
