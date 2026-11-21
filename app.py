"""Throwaway local lyrics player. Python stdlib + an existing ffprobe installation."""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from transcription import Preparation
from lyric_pipeline import song_query, tokens, PIPELINE_VERSION
from audio_tags import AUDIO_EXTENSIONS, lyric_digest, read_audio_tags

BASE = Path(__file__).resolve().parent
CONFIG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
MUSIC_ROOT = Path(CONFIG["music_root"]).expanduser().resolve()
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)
(DATA / "cache").mkdir(exist_ok=True)
(DATA / "tracks").mkdir(exist_ok=True)
LIBRARY: dict[str, Path] = {}
LIB_LOCK = threading.RLock()
STATE_LOCK = threading.Lock()
REMOTE_LOCK = threading.Lock()
LAST_REMOTE = 0.0
PREPARATION = None


def library_path(key):
    with LIB_LOCK:
        path = LIBRARY.get(key)
    if not path or not path.is_file():
        raise ValueError("Choose a song from the selected music folder for local preparation.")
    return path


def scan_library():
    found = {}
    if MUSIC_ROOT.is_dir():
        for path in MUSIC_ROOT.rglob("*"):
            if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
                resolved = path.resolve()
                if not resolved.is_relative_to(MUSIC_ROOT):
                    continue
                key = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:24]
                found[key] = resolved
    with LIB_LOCK:
        LIBRARY.clear()
        LIBRARY.update(found)


def original_recording(metadata, record):
    """Resolve an existing library copy by the provider's title and artist."""
    title = tokens(record["trackName"])
    artist = tokens(record["artistName"])
    if not title or not artist:
        return None
    edited = Path(metadata["path"])
    with LIB_LOCK:
        copies = tuple(LIBRARY.values())
    for path in copies:
        if path != edited and tokens(path.stem) in (artist + title, title + artist):
            return path
    return None


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def state_path(key):
    if not re.fullmatch(r"[a-f0-9]{24,64}", key):
        raise ValueError("Invalid track ID")
    return DATA / "tracks" / (key + ".json")


def read_state(key):
    path = state_path(key)
    with STATE_LOCK:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def local_sidecar(path):
    for sidecar in (path.with_name(path.name + ".lrc"), path.with_suffix(".lrc"), path.with_suffix(".txt")):
        if sidecar.is_file() and sidecar.stat().st_size < 2_000_000:
            return sidecar.read_text(encoding="utf-8-sig", errors="replace"), sidecar.name
    return "", ""


def track_metadata(key, include_art=False):
    with LIB_LOCK:
        path = LIBRARY.get(key)
    if not path or not path.is_file():
        raise FileNotFoundError("Song not found. Rescan the library if it moved.")
    stat = path.stat()
    result = {"id": key, "filename": path.name, "path": str(path), "title": path.stem,
              "fingerprint": f"{stat.st_size}:{stat.st_mtime_ns}",
              "artist": "", "album": "", "duration": 0, "embedded": "", "sidecar": ""}
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        command = [ffprobe, "-v", "error", "-show_entries", "format=duration:format_tags", "-of", "json", str(path)]
        proc = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace", timeout=20,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode == 0:
            info = json.loads(proc.stdout).get("format", {})
            tags = {k.lower(): v for k, v in info.get("tags", {}).items()}
            result.update(title=tags.get("title", path.stem), artist=tags.get("artist", ""),
                          album=tags.get("album", ""), duration=float(info.get("duration", 0)))
            result["embedded"] = next((v for k, v in tags.items() if "lyric" in k and v), "")
    else:
        result["metadata_warning"] = "ffprobe not found; edit artist and title in Find lyrics."
    embedded, artwork = read_audio_tags(path, include_art)
    if embedded:
        result["embedded"] = embedded
    result["embeddedDigest"] = lyric_digest(result["embedded"])
    if artwork:
        result["artwork"] = artwork
    result["sidecar"], result["sidecar_name"] = local_sidecar(path)
    query = song_query(result)
    if query["repaired"]:
        result.update(originalTitle=result["title"],originalArtist=result["artist"],title=query["title"],artist=query["artist"],metadataCorrected=True)
    return result


def search_lyrics(artist, title, cached_only=False):
    global LAST_REMOTE
    if not title.strip():
        raise ValueError("Enter a song title first.")
    query = urllib.parse.urlencode({"track_name": title.strip(), **({"artist_name": artist.strip()} if artist.strip() else {})})
    cache_path = DATA / "cache" / (hashlib.sha256(query.encode()).hexdigest() + ".json")
    with REMOTE_LOCK:
        if cache_path.exists() and (cached_only or time.time() - cache_path.stat().st_mtime < 7 * 86400):
            return {"results": json.loads(cache_path.read_text(encoding="utf-8")), "cached": True}
        if cached_only:
            return {"results": [], "cached": False}
        time.sleep(max(0, .6 - (time.monotonic() - LAST_REMOTE)))
        request = urllib.request.Request("https://lrclib.net/api/search?" + query,
            headers={"User-Agent": "CodexLocalLyricsPrototype/0.1 (https://openai.com/codex/)"})
        LAST_REMOTE = time.monotonic()
        with urllib.request.urlopen(request, timeout=9) as response:
            records = json.load(response)
        # Keep only the fields needed by the test player (no remote YAML payload).
        fields = {"id", "trackName", "artistName", "albumName", "duration", "instrumental", "plainLyrics", "syncedLyrics", "hasWordSync"}
        records = [{k: v for k, v in row.items() if k in fields} for row in records]
        atomic_json(cache_path, records)
        return {"results": records, "cached": False}


def byte_range(value, size):
    """Return an inclusive range; reject malformed/multiple/unsatisfiable ranges."""
    if not value:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
    if not match or not size:
        raise ValueError("Invalid byte range")
    a, b = match.groups()
    if not a:
        if not b or int(b) <= 0:
            raise ValueError("Invalid suffix range")
        start, end = max(0, size - int(b)), size - 1
    else:
        start, end = int(a), min(int(b), size - 1) if b else size - 1
    if start >= size or start > end:
        raise ValueError("Range outside file")
    return start, end


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def local_request(self):
        authority = self.headers.get("Host", "")
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        origin = self.headers.get("Origin")
        return authority in allowed and (not origin or origin in {"http://" + h for h in allowed})

    def send_bytes(self, value, status=200, content_type="application/json; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(value)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(value)

    def json(self, value, status=200, headers=None):
        self.send_bytes(json.dumps(value, ensure_ascii=False).encode(), status, headers=headers)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.local_request():
            return self.json({"error": "Local requests only"}, 403)
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        arg = lambda name, default="": query.get(name, [default])[0]
        try:
            if parsed.path == "/api/health":
                return self.json({"app": "Verse Lab", "version": "0.4", "root": str(MUSIC_ROOT), "count": len(LIBRARY), "ffprobe": bool(shutil.which("ffprobe"))})
            if parsed.path == "/api/asr/status" and PREPARATION:
                return self.json(PREPARATION.status())
            if parsed.path == "/api/asr/result" and PREPARATION:
                return self.json({"result": PREPARATION.cached(arg("id"))})
            if parsed.path == "/api/library":
                if arg("refresh") == "1":
                    scan_library()
                with LIB_LOCK:
                    tracks = [{"id": k, "name": p.stem, "folder": str(p.parent.relative_to(MUSIC_ROOT))} for k, p in LIBRARY.items()]
                tracks.sort(key=lambda t: t["name"].casefold())
                return self.json({"tracks": tracks, "root": str(MUSIC_ROOT)})
            if parsed.path == "/api/track":
                return self.json(track_metadata(arg("id")))
            if parsed.path == "/api/state":
                return self.json(read_state(arg("id")))
            if parsed.path == "/api/lyrics":
                return self.json(search_lyrics(arg("artist")[:250], arg("title")[:500]))
            if parsed.path == "/api/audio":
                with LIB_LOCK:
                    path = LIBRARY.get(arg("id"))
                if not path or not path.is_file():
                    raise FileNotFoundError("Audio file not found")
                return self.stream_audio(path)
            static = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/lyrics.mjs": "lyrics.mjs", "/styles.css": "styles.css"}
            if parsed.path not in static:
                return self.json({"error": "Not found"}, 404)
            path = BASE / "dist" / static[parsed.path]
            kind = "text/javascript" if path.suffix in {".js", ".mjs"} else mimetypes.guess_type(path)[0] or "text/plain"
            return self.send_bytes(path.read_bytes(), content_type=kind + "; charset=utf-8")
        except urllib.error.HTTPError as exc:
            headers = {"Retry-After": exc.headers.get("Retry-After", "30")} if exc.code in {429, 503} else None
            return self.json({"error": "Lyrics service is busy. Try again later." if headers else f"Lyrics service returned {exc.code}."}, 503 if headers else 502, headers)
        except (TimeoutError, urllib.error.URLError, subprocess.TimeoutExpired):
            return self.json({"error": "The lookup timed out. You can still play music or import an LRC file."}, 504)
        except FileNotFoundError as exc:
            return self.json({"error": str(exc)}, 404)
        except ValueError as exc:
            return self.json({"error": str(exc)}, 400)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as exc:
            self.log_error("%s", exc)
            return self.json({"error": "Local server error. Check the server log."}, 500)

    def stream_audio(self, path):
        size = path.stat().st_size
        try:
            selected = byte_range(self.headers.get("Range"), size)
        except ValueError:
            return self.send_bytes(b"", 416, headers={"Content-Range": f"bytes */{size}"})
        start, end = selected if selected else (0, size - 1)
        self.send_response(206 if selected else 200)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "audio/mpeg")
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        self.send_header("Accept-Ranges", "bytes")
        if selected:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as source:
            source.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = source.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_POST(self):
        if not self.local_request():
            return self.json({"error": "Local requests only"}, 403)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 2_000_000:
                return self.json({"error": "Request too large or empty"}, 413)
            data = json.loads(self.rfile.read(size))
            if self.path.startswith("/api/asr/") and PREPARATION:
                if self.path == "/api/asr/batch":
                    if data.get("action") == "pause":
                        return self.json(PREPARATION.cancel_batch())
                    if data.get("action") != "start":
                        raise ValueError("Choose start or pause.")
                    requested = data.get("ids")
                    with LIB_LOCK:
                        if requested is None:
                            ids = list(LIBRARY)
                        elif isinstance(requested, list) and all(isinstance(key,str) and key in LIBRARY for key in requested):
                            ids = requested
                        else:
                            raise ValueError("Batch tracks must be in the indexed library.")
                    return self.json(PREPARATION.start_batch(ids))
                if self.path == "/api/asr/settings":
                    return self.json(PREPARATION.configure(data))
                if self.path == "/api/asr/prepare":
                    return self.json(PREPARATION.prepare(data["id"], data.get("reason", "current"), bool(data.get("force", False))))
                if self.path == "/api/asr/prefetch":
                    return self.json(PREPARATION.prefetch(data.get("ids", []), data.get("currentId")))
                if self.path == "/api/asr/cancel":
                    return self.json(PREPARATION.cancel(data.get("id")))
            if self.path == "/api/state":
                path = state_path(data["id"])
                if not isinstance(data.get("state"), dict):
                    raise ValueError("Invalid state")
                with STATE_LOCK:
                    if path.exists() and data["state"].get("pipelineVersion",0) >= PIPELINE_VERSION:
                        previous = json.loads(path.read_text(encoding="utf-8"))
                        if previous.get("timingKind") == "model" and previous.get("pipelineVersion",0) < PIPELINE_VERSION:
                            history = DATA / "history"
                            history.mkdir(exist_ok=True)
                            backup = history / (data["id"]+f"-before-v{PIPELINE_VERSION}.json")
                            if not backup.exists():
                                atomic_json(backup,previous)
                    atomic_json(path, data["state"])
                return self.json({"saved": True})
            if self.path == "/api/export":
                text = data.get("text", "")
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("No lyrics to export")
                name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", data.get("name", "lyrics"))[:160].strip(" .") or "lyrics"
                name = re.sub(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?=\.|$)", r"_\1", name, flags=re.I)
                folder = BASE / "exports"
                folder.mkdir(exist_ok=True)
                path = folder / (name + ".lrc")
                path.write_text(text, encoding="utf-8")
                return self.json({"path": str(path)})
            return self.json({"error": "Not found"}, 404)
        except (KeyError, TypeError, ValueError) as exc:
            return self.json({"error": str(exc)}, 400)

    def log_message(self, message, *args):
        if self.path == "/api/asr/status":
            return
        print(f"{self.address_string()} {message % args}", flush=True)


def main():
    global PREPARATION
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=CONFIG.get("port", 8768))
    args = parser.parse_args()
    scan_library()
    PREPARATION = Preparation(BASE, library_path, track_metadata, search_lyrics, read_state,
                              lambda artist, title: search_lyrics(artist, title, cached_only=True))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Verse Lab: http://127.0.0.1:{args.port} | {len(LIBRARY)} tracks | {MUSIC_ROOT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        PREPARATION.close()
        server.server_close()


if __name__ == "__main__":
    main()
