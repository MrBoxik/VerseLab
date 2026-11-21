"""Local Whisper workers sharing a bounded queue and per-recording cache."""
from __future__ import annotations
import atexit
from collections import deque
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import queue
import shutil
import subprocess
import threading
import time
import uuid
import wave
from audio_alignment import match_audio
from audio_tags import TAG_LOCK, fingerprint, lyric_digest, lyrics_text, read_audio_tags, save_audio_lyrics
from gpu_budget import MAX_AGENTS, available_agents
from lyric_pipeline import (PIPELINE_VERSION, FIXED_MODEL, gpu_lyric_lines, match_reference, matches_lyric_duration,
                            place_audio_reference, reference_queries, suspicious_repetition, tokens)

MODELS = {
    "large-v3": {"file": "ggml-large-v3.bin", "bytes": 3095033483, "sha256": "64d182b440b98d5203c4f9bd541544d84c605196c4f7b845dfa11fb23594d1e2"},
}
DEFAULTS = {"model": FIXED_MODEL, "mode": "on_demand", "threads": 4, "language": "auto",
            "automatic": True, "prefetch": True, "lookahead": 2, "maxTrackMinutes": 20, "agents": 1}


def validate_settings(values, previous=None):
    result = {**DEFAULTS, **(previous or {}), **{k: v for k, v in values.items() if k in DEFAULTS}}
    if result["model"] not in {"tiny", "base", "small", FIXED_MODEL} or result["mode"] not in {"on_demand", "resident", "disabled"}:
        raise ValueError("Invalid local engine or loading mode.")
    result["model"] = FIXED_MODEL
    if type(result["threads"]) is not int or result["threads"] not in {2, 4, 8}:
        raise ValueError("CPU threads must be 2, 4 or 8.")
    if result["language"] not in {"auto", "en", "cs", "ja", "de", "fr", "es", "it", "ko", "zh", "ru", "pt"}:
        raise ValueError("Unsupported language selection.")
    if type(result["lookahead"]) is not int or not 1 <= result["lookahead"] <= 5:
        raise ValueError("Prepare between 1 and 5 upcoming songs.")
    if any(type(result[k]) is not bool for k in ("automatic", "prefetch")):
        raise ValueError("Automatic and prefetch settings must be true or false.")
    if type(result["maxTrackMinutes"]) is not int or not 0 <= result["maxTrackMinutes"] <= 1440:
        raise ValueError("Maximum track length must be a whole number of minutes from 0 to 1440.")
    if type(result["agents"]) is not int or not 1 <= result["agents"] <= MAX_AGENTS:
        raise ValueError("Choose between one and four transcription agents.")
    return result


def write_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def has_manual_timing(saved):
    return (saved.get("offset", 0) != 0 or saved.get("scale", 1) != 1
            or any(line.get("timing") == "manual" for line in saved.get("lines", [])))


def recording_key(path, settings, version=PIPELINE_VERSION):
    stat = path.stat()
    signature = [str(path.resolve()), stat.st_size, stat.st_mtime_ns, FIXED_MODEL, settings["language"], f"pipeline-{version}"]
    return hashlib.sha256(json.dumps(signature).encode()).hexdigest()


def legacy_anchor_key(path, settings):
    """Locate only this exact recording's old Small draft, never display its text."""
    stat = path.stat()
    signature = [str(path.resolve()), stat.st_size, stat.st_mtime_ns, "small", settings["language"], "decoder-v1"]
    return hashlib.sha256(json.dumps(signature).encode()).hexdigest()


def decoder_lines(payload):
    """Join subword tokens, preserving model-derived times rather than inventing timing."""
    lines = []
    for segment in payload.get("segments", []):
        start, end = float(segment.get("start", 0)), float(segment.get("end", 0))
        body = str(segment.get("text", "")).strip()
        if not body or end <= start or segment.get("no_speech_prob", 0) > .85:
            continue
        if re.fullmatch(r"[\[(].*[\])]", body) and len(body) < 80:
            continue
        words = []
        for token in segment.get("words", []):
            piece = token.get("word", "")
            ts = float(token.get("start", start))
            te = float(token.get("end", ts))
            if not piece or piece.startswith("[_") or not math.isfinite(ts) or ts < 0:
                continue
            if words and not piece[0].isspace():
                words[-1]["text"] += piece
                words[-1]["end"] = max(words[-1]["end"], te)
            else:
                words.append({"text": piece.lstrip(), "time": ts, "end": max(ts, te),
                              "probability": float(token.get("probability", 0))})
        words = [w for w in words if w["text"].strip()]
        for i, word in enumerate(words):
            if i < len(words) - 1:
                word["text"] += " "
        # If a decoder returned zero/invalid timings, keep honest line-only timing.
        if not words or any(w["time"] > end + 1 for w in words) or (len(words) > 1 and words[-1]["time"] <= words[0]["time"]):
            words = []
        lines.append({"time": start, "end": end, "text": body, "words": words})
    return lines


class Cancelled(Exception):
    pass


class TrackTooLong(Exception):
    def __init__(self, minutes):
        super().__init__(f"Track exceeds the {minutes}-minute transcription limit.")


class AgentCapacityChanged(Exception):
    pass


class Engine:
    def __init__(self, base, index=0, can_start=None):
        self.base = base
        self.index = index
        self.can_start = can_start
        (base / "data" / "asr-cache").mkdir(parents=True, exist_ok=True)
        self.proc = None
        self.signature = None
        self.messages = None
        self.log = None
        self.worker_slot = None
        self.lock = threading.RLock()
        self.warming = False
        atexit.register(self.stop)

    def stop(self):
        with self.lock:
            proc, self.proc = self.proc, None
            self.signature = None
            self.warming = False
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            if proc and proc.stdin:
                proc.stdin.close()
            if self.log:
                self.log.close()
                self.log = None
            if self.worker_slot:
                self.worker_slot.close()
                self.worker_slot = None

    def status(self):
        with self.lock:
            loaded = bool(self.proc and self.proc.poll() is None)
            return {"loaded": loaded and not self.warming, "loading": loaded and self.warming,
                    "model": self.signature[0] if loaded and self.signature else None,
                    "pid": self.proc.pid if loaded else None}

    def requirements(self):
        folder = self.base / "runtime" / "lyrics-gpu"
        bundled = folder / "LargeModelBenchmark.exe"
        binary = folder / "LargeModelBenchmark.dll"
        model = self.base / "models" / MODELS[FIXED_MODEL]["file"]
        if not model.is_file():
            raise ValueError("Run Verse Lab Setup and select the offline transcription model.")
        if bundled.is_file():
            return [str(bundled)], model
        dotnet = shutil.which("dotnet")
        if not dotnet or not binary.is_file():
            raise ValueError("The local transcription worker is missing. Run Verse Lab Setup or setup_transcription.py.")
        return [dotnet, str(binary)], model

    @staticmethod
    def _receive(messages, cancel, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel.is_set():
                raise Cancelled()
            try:
                message = messages.get(timeout=.2)
            except queue.Empty:
                continue
            if message is None:
                raise RuntimeError("Large-v3 worker exited. See data/transcription-worker.log.")
            if message["type"] == "error":
                raise RuntimeError(message["message"])
            return message
        raise TimeoutError("Large-v3 stopped responding. See data/transcription-worker.log.")

    def _reserve_worker(self, cancel):
        try:
            import msvcrt
        except ImportError:
            return None
        name = f"large-v3-{self.index + 1}.lock" if self.index else "large-v3.lock"
        slot = (self.base / "data" / name).open("a+b")
        try:
            while not cancel.is_set():
                try:
                    slot.seek(0)
                    msvcrt.locking(slot.fileno(), msvcrt.LK_NBLCK, 1)
                    return slot
                except PermissionError:
                    cancel.wait(.15)
            raise Cancelled()
        except BaseException:
            slot.close()
            raise

    def ready(self, settings, cancel):
        signature = (FIXED_MODEL, settings["threads"])
        with self.lock:
            if cancel.is_set():
                raise Cancelled()
            if self.proc and self.proc.poll() is None and self.signature == signature and not self.warming:
                return 0
            self.stop()
            command, model = self.requirements()
        if self.can_start is not None and not self.can_start():
            raise AgentCapacityChanged()
        slot = self._reserve_worker(cancel)
        with self.lock:
            self.worker_slot = slot
            try:
                if cancel.is_set():
                    raise Cancelled()
                name = f"transcription-worker-{self.index + 1}.log" if self.index else "transcription-worker.log"
                self.log = (self.base / "data" / name).open("w", encoding="utf-8")
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
                started = time.monotonic()
                self.proc = subprocess.Popen([*command, "--worker", str(model)], cwd=self.base,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True, encoding="utf-8", creationflags=flags)
            except BaseException:
                self.stop()
                raise
            proc = self.proc
            messages = self.messages = queue.Queue()
            self.signature = signature
            self.warming = True

            def read_messages():
                try:
                    with proc.stdout:
                        for line in proc.stdout:
                            try:
                                messages.put(json.loads(line))
                            except ValueError:
                                continue
                finally:
                    messages.put(None)

            threading.Thread(target=read_messages, name="large-v3-results", daemon=True).start()
        if self._receive(messages, cancel, 90)["type"] != "ready":
            raise RuntimeError("Large-v3 failed to initialize")
        with self.lock:
            if cancel.is_set() or self.proc is not proc:
                raise Cancelled()
            self.warming = False
        return time.monotonic() - started

    def run(self, path, settings, cancel, phase):
        started = time.monotonic()
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise ValueError("ffmpeg is required to decode audio for local transcription.")
        wav = self.base / "data" / "asr-cache" / ("audio-" + uuid.uuid4().hex + ".wav")
        try:
            phase("Decoding audio")
            limit = settings["maxTrackMinutes"] * 60
            # Read just past the cutoff when metadata is missing or inaccurate.
            bound = ["-t", str(limit + 1)] if limit else []
            subprocess.run([ffmpeg, "-v", "error", "-i", str(path), *bound, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
                           check=True, capture_output=True, timeout=90, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with wave.open(str(wav), "rb") as decoded:
                duration = decoded.getnframes() / decoded.getframerate()
            if duration <= 0:
                raise ValueError("The recording has no decodable audio.")
            if limit and duration > limit:
                raise TrackTooLong(settings["maxTrackMinutes"])
            phase("Loading Large-v3")
            load_seconds = self.ready(settings, cancel)
            infer_start = time.monotonic()
            with self.lock:
                if cancel.is_set() or self.proc is None:
                    raise Cancelled()
                messages = self.messages
                self.proc.stdin.write(json.dumps({"id": path.stem, "wav": str(wav),
                    "duration": duration, "language": settings["language"], "threads": settings["threads"]}) + "\n")
                self.proc.stdin.flush()
            while True:
                message = self._receive(messages, cancel, 120)
                if message["type"] == "result":
                    break
                if message["type"] == "progress":
                    phase(f"Recognizing section {message['current']}/{message['total']}")
            lines = gpu_lyric_lines(message["segments"], duration)
            if not lines:
                raise ValueError("No usable vocals recognized.")
            return {"lines": lines, "source": "AI draft · Whisper Large-v3", "timingKind": "model",
                    "model": FIXED_MODEL, "language": settings["language"], "duration": duration,
                    "pipelineVersion": PIPELINE_VERSION, "reviewSections": sum(bool(l.get("review")) for l in lines),
                    "seconds": round(time.monotonic()-started,2), "loadSeconds": round(load_seconds,2),
                    "inferenceSeconds": round(time.monotonic()-infer_start,2), "kind": "transcription"}
        finally:
            wav.unlink(missing_ok=True)


class Preparation:
    def __init__(self, base, get_path, get_metadata, find_online, get_saved, find_cached=None, *, defaults_only=False, find_original=None, embed_audio=False):
        self.base, self.get_path, self.get_metadata = base, get_path, get_metadata
        self.find_online, self.get_saved = find_online, get_saved
        self.find_original = find_original
        self.embed_audio = embed_audio
        self.find_cached = find_cached or (lambda artist, title: {"results": []})
        self.folder = base / "data" / "asr-cache"
        self.folder.mkdir(exist_ok=True)
        self.settings_file = base / "data" / "transcription-settings.json"
        stored = json.loads(self.settings_file.read_text(encoding="utf-8")) if self.settings_file.exists() else {}
        if defaults_only:
            stored = {name: stored.get(name, DEFAULTS[name]) for name in ("maxTrackMinutes", "agents")}
        self.settings = validate_settings(stored)
        self.engine = Engine(base)
        self.extra_engines = [
            Engine(base, index, lambda index=index: self._can_start(index)) for index in range(1, MAX_AGENTS)
        ]
        self.condition = threading.Condition(threading.RLock())
        self.jobs, self.pending = {}, []
        self.batch_pending = deque()
        self.prefetch_pending = deque()
        self.prefetch_remaining = 0
        self.prefetch_revision = 0
        self.batch = {"status": "idle"}
        self.batch_timer = None
        self.running = {}
        self.agent_limit = 1
        self.budget_sample_time = 0
        self.closed = False
        self.warm_error = None
        self.threads = [threading.Thread(target=self._loop, args=(index,), name=f"lyrics-agent-{index + 1}", daemon=True)
                        for index in range(MAX_AGENTS)]
        self.thread = self.threads[0]
        for thread in self.threads:
            thread.start()
        atexit.register(self.close)

    def _engines(self):
        return [self.engine, *self.extra_engines]

    def _agent_limit(self):
        if self.settings["agents"] == 1 or self.settings["mode"] == "disabled":
            return 1
        if time.monotonic() >= self.budget_sample_time + 1:
            pids = [pid for engine in self._engines() if (pid := engine.status().get("pid")) is not None]
            self.agent_limit = available_agents(self.settings["agents"], pids)
            self.budget_sample_time = time.monotonic()
        return min(self.settings["agents"], self.agent_limit)

    def _can_start(self, index):
        with self.condition:
            return index < self._agent_limit()

    def _cancel_running(self, matches):
        for key, (engine, cancel) in self.running.items():
            if matches(self.jobs[key]):
                cancel.set()
                engine.stop()

    def close(self):
        with self.condition:
            self.closed = True
            if self.batch_timer:
                self.batch_timer.cancel()
            for _, cancel in self.running.values():
                cancel.set()
            self.condition.notify_all()
        for engine in self._engines():
            engine.stop()

    def configure(self, values):
        with self.condition:
            updated = validate_settings(values, self.settings)
            changed = any(updated[k] != self.settings[k] for k in ("model", "threads", "language", "mode"))
            previous_lookahead = max(self.settings["lookahead"], self._agent_limit()) if self.prefetch_pending else 0
            write_json(self.settings_file, updated)
            self.settings = updated
            self.budget_sample_time = 0
            if self.prefetch_pending:
                lookahead = max(updated["lookahead"], self._agent_limit())
                self.prefetch_remaining = max(0, self.prefetch_remaining + lookahead - previous_lookahead)
            self.warm_error = None
            if changed:
                self.cancel_batch()
                self._cancel_running(lambda job: True)
                for key in self.pending:
                    self.jobs[key].update(status="cancelled", phase="Settings changed")
                self.pending.clear()
                for engine in self._engines():
                    engine.stop()
            self.condition.notify_all()
        return self.status()

    def cached(self, track_id):
        path = self.get_path(track_id)
        cache = self.folder / (recording_key(path, self.settings) + ".json")
        return json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else None

    def prepare(self, track_id, reason="current", force=False):
        if reason not in {"current", "prefetch", "manual"}:
            raise ValueError("Invalid preparation reason")
        path = self.get_path(track_id)
        with self.condition:
            if self.settings["mode"] == "disabled":
                raise ValueError("Local transcription is disabled. Choose a loading mode in AI settings.")
            key = recording_key(path, self.settings)
            prior_key = next((active for active in self.running if self.jobs[active]["trackId"] == track_id), key)
            prior = self.jobs.get(prior_key)
            if prior and prior["status"] in {"queued", "running"}:
                if reason != "prefetch" and prior["reason"] != "batch":
                    prior["reason"] = reason
                    if prior_key in self.pending:
                        self.pending.remove(prior_key)
                        self.pending.insert(0, prior_key)
                return dict(prior)
            embedded, _ = read_audio_tags(path) if self.embed_audio and not force else ("", "")
            cached = None if force else self.cached(track_id)
            if embedded and (not cached or cached.get("embeddedDigest") != lyric_digest(embedded)):
                cached = {"raw": embedded, "source": "Embedded lyrics", "kind": "saved",
                          "embeddedDigest": lyric_digest(embedded), "fingerprint": fingerprint(path)}
            if cached:
                self.jobs[key] = {"key": key, "trackId": track_id, "reason": reason, "status": "complete", "phase": "Cached", "cached": True, "seconds": 0}
                return {"status": "complete", "cached": True, "result": cached}
            job = {"key": key, "trackId": track_id, "status": "queued", "reason": reason, "phase": "Queued", "createdAt": time.time(), "force": force}
            self.jobs[key] = job
            if reason == "prefetch":
                self.pending.append(key)
            else:
                self.pending.insert(0, key)
                if len(self.running) >= self._agent_limit():
                    victim = next((self.jobs[active] for active in self.running
                                   if self.jobs[active]["reason"] == "prefetch"), None)
                    if victim is not None:
                        self._cancel_running(lambda item: item is victim)
            # Bound in-memory history independently of the disk result cache.
            if len(self.jobs) > 80:
                for old in list(self.jobs):
                    if old != key and old not in self.running and self.jobs[old]["status"] not in {"running", "queued"}:
                        del self.jobs[old]
                        if len(self.jobs) <= 60:
                            break
            self.condition.notify_all()
            return dict(job)

    def prefetch(self, ids, current_id=None):
        if not isinstance(ids, list) or (not self.embed_audio and len(ids) > 5):
            raise ValueError("At most five upcoming songs can be prepared.")
        wanted = dict.fromkeys(track_id for track_id in ids if track_id != current_id)
        if not self.embed_audio:
            wanted = dict.fromkeys(list(wanted)[:self.settings["lookahead"]])
        with self.condition:
            # A track that has just started playing must keep its in-flight work.
            for key in list(self.pending):
                if self.jobs[key]["trackId"] == current_id:
                    self.jobs[key]["reason"] = "current"
                    self.pending.remove(key)
                    self.pending.insert(0, key)
            for active in self.running:
                job = self.jobs[active]
                if job["trackId"] == current_id and job["reason"] != "batch":
                    job["reason"] = "current"
            for key in list(self.pending):
                if self.jobs[key]["reason"] == "prefetch" and self.jobs[key]["trackId"] not in wanted:
                    self.pending.remove(key)
                    self.jobs[key].update(status="cancelled", phase="Removed from upcoming queue")
            self._cancel_running(lambda job: job["reason"] == "prefetch" and job["trackId"] not in wanted)
            if self.embed_audio:
                self.prefetch_pending = deque(wanted)
                self.prefetch_remaining = max(self.settings["lookahead"], self._agent_limit())
                self.prefetch_revision += 1
                self.condition.notify_all()
        if not self.embed_audio and self.settings["prefetch"] and self.settings["mode"] != "disabled":
            for track_id in wanted:
                self.prepare(track_id, "prefetch")
        return self.status()

    def start_batch(self, track_ids, minutes=0):
        if type(minutes) is not int or not 0 <= minutes <= 1440:
            raise ValueError("Preparation time must be between 0 and 1440 minutes.")
        with self.condition:
            if self.settings["mode"] == "disabled":
                raise ValueError("Enable Large-v3 in Local engine settings.")
            if self.batch["status"] in {"running", "stopping"}:
                raise ValueError("A library batch is already active.")
            if not self.embed_audio:
                self.engine.requirements()
            self.batch_pending = deque(dict.fromkeys(track_ids))
            self.batch = {"status": "running", "total": len(self.batch_pending),
                          "prepared": 0, "skipped": 0, "skippedLong": 0, "failed": 0, "current": None}
            if minutes:
                self.batch["endsAt"] = time.time() + minutes * 60
                self.batch_timer = threading.Timer(minutes * 60, self._stop_timed_batch, [self.batch])
                self.batch_timer.daemon = True
                self.batch_timer.start()
            self.condition.notify_all()
            return dict(self.batch)

    def _stop_timed_batch(self, batch):
        with self.condition:
            if self.batch is batch and not self.closed:
                self.cancel_batch("Time limit reached")

    def _clear_batch_timer(self):
        if self.batch_timer:
            self.batch_timer.cancel()
            self.batch_timer = None

    def cancel_batch(self, reason=None):
        with self.condition:
            if self.batch["status"] != "running":
                return dict(self.batch)
            self.batch_pending.clear()
            self._clear_batch_timer()
            if reason:
                self.batch["stopReason"] = reason
            self.batch["status"] = "stopping" if self.batch["current"] else "paused"
            self._cancel_running(lambda job: job["reason"] == "batch")
            self.condition.notify_all()
            return dict(self.batch)

    def _next_batch_job(self, settings):
        if self.batch["status"] != "running":
            return None
        for _ in range(min(12, len(self.batch_pending))):
            track_id = self.batch_pending.popleft()
            if any(self.jobs[key]["trackId"] == track_id for key in self.running):
                self.batch_pending.append(track_id)
                continue
            try:
                path = self.get_path(track_id)
                key = recording_key(path, settings)
                saved = self.get_saved(track_id)
                has_lyrics = saved.get("lines") or saved.get("lyrics")
                preserve_saved = saved.get("timingKind") != "model" or has_manual_timing(saved)
                cache = self.folder / (key + ".json")
                if self.embed_audio:
                    skip = bool(read_audio_tags(path)[0])
                    if not skip and cache.exists() and path.with_name(path.name + ".lrc").is_file():
                        skip = bool(self.cached(track_id).get("sidecarSaved"))
                else:
                    skip = cache.exists() or (has_lyrics and preserve_saved)
                if skip:
                    self.batch["skipped"] += 1
                    continue
            except (OSError, ValueError, KeyError):
                self.batch["failed"] += 1
                continue
            self.batch["current"] = track_id
            self.jobs[key] = {"key": key, "trackId": track_id, "status": "queued", "reason": "batch"}
            return key
        if not self.batch_pending and not self.batch["current"]:
            self.batch["status"] = "complete"
            self._clear_batch_timer()
        return None

    def cancel(self, track_id=None):
        with self.condition:
            if track_id is None:
                self.cancel_batch()
                self.prefetch_pending.clear()
                self.prefetch_revision += 1
            for key in list(self.pending):
                if track_id is None or self.jobs[key]["trackId"] == track_id:
                    self.pending.remove(key)
                    self.jobs[key].update(status="cancelled", phase="Cancelled")
            self._cancel_running(lambda job: track_id is None or job["trackId"] == track_id)
        return self.status()

    def status(self):
        with self.condition:
            jobs = [dict(j, elapsed=round(time.time() - j["startedAt"], 1) if j.get("status") == "running" else j.get("seconds", 0))
                    for j in self.jobs.values()]
            models = {k: {**{f: v for f, v in spec.items() if f != "sha256"}, "installed": (self.base / "models" / spec["file"]).is_file()} for k, spec in MODELS.items()}
            workers = [engine.status() for engine in self._engines()]
            return {"settings": dict(self.settings), "worker": workers[0], "models": models, "jobs": jobs, "batch": dict(self.batch),
                    "agents": {"allowed": self._agent_limit(), "loaded": sum(bool(worker["loaded"]) for worker in workers)},
                    "warmError": self.warm_error, "pipelineVersion": PIPELINE_VERSION, "backend": "Whisper Large-v3 · Vulkan GPU"}

    def _references(self, metadata, cached_only=False):
        lookup = self.find_cached if cached_only else self.find_online
        matches = {}
        for query in reference_queries(metadata):
            try:
                records = lookup(query["artist"], query["title"])["results"]
            except (OSError, ValueError):
                continue
            for record in records:
                if (record.get("syncedLyrics") and tokens(record.get("artistName", "")) == tokens(query["artist"])
                        and tokens(record.get("trackName", "")) == tokens(query["title"])
                        and matches_lyric_duration(record, metadata["duration"])):
                    matches.setdefault(record["id"], {**record, "alternate": query["alternate"]})
        return sorted(matches.values(), key=lambda r: (r["alternate"], abs(r["duration"]-metadata["duration"])))[:6]

    def _match(self, recognized, references, metadata, cancel):
        duration = metadata["duration"]
        fits = [(record, fit) for record in references
                if (fit := match_reference(recognized, record, duration))]
        if not fits:
            return None
        record, fit = max(fits, key=lambda item: (
            len(item[1]["alignment"]["anchors"]), -item[1]["alignment"]["meanError"]
        ))
        if record["alternate"] and self.find_original:
            original = self.find_original(metadata, record)
            if original:
                alignment = fit["alignment"]
                spans, adjustment = match_audio(original, metadata["path"], alignment, cancel)
                if spans:
                    placed = place_audio_reference(record, recognized, duration, alignment, spans, adjustment)
                    if placed:
                        return placed
        return fit

    def _reuse_draft(self, path, settings, references, metadata, cancel):
        # Reuse text evidence for alternate versions that were already processed before audio matching.
        if not any(record["alternate"] for record in references):
            return None
        cache = self.folder / (recording_key(path, settings, version=4) + ".json")
        try:
            old = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if old.get("kind") != "transcription":
            return None
        fit = self._match(old.get("lines", []), references, metadata, cancel)
        if fit:
            return {**fit, "duration": metadata["duration"], "reusedAnchors": True}
        return None

    def _reuse_anchors(self, path, settings, references, duration):
        if not references:
            return None
        cache = self.folder / (legacy_anchor_key(path, settings) + ".json")
        try:
            old = json.loads(cache.read_text(encoding="utf-8"))
            fits = [fit for record in references if (fit := match_reference(old.get("lines",[]),record,duration))]
        except (OSError, ValueError, TypeError, KeyError):
            return None
        if not fits:
            return None
        result = max(fits,key=lambda fit:(len(fit["alignment"]["anchors"]),-fit["alignment"]["meanError"]))
        return {**result, "duration": duration, "reusedAnchors": True}

    def _schedule_prefetch(self, track_id, revision, settings):
        queued = False
        try:
            path = self.get_path(track_id)
            if read_audio_tags(path)[0] or (self.folder / (recording_key(path, settings) + ".json")).exists():
                return
            with self.condition:
                if revision != self.prefetch_revision or self.closed:
                    return
                key = recording_key(path, settings)
                if key in self.pending or any(self.jobs[active]["trackId"] == track_id for active in self.running):
                    return
                job = self.prepare(track_id, "prefetch")
                if job["status"] != "complete" and job["reason"] == "prefetch":
                    self.jobs[job["key"]]["prefetchRevision"] = revision
                    queued = True
        except (ValueError, OSError, KeyError):
            return
        finally:
            with self.condition:
                if not queued and revision == self.prefetch_revision:
                    self.prefetch_remaining += 1
                self.condition.notify_all()

    def _loop(self, index):
        sleep_blocked = False
        while True:
            with self.condition:
                if self.closed:
                    return
                engine = self.engine if index == 0 else self.extra_engines[index - 1]
                keep_awake = index == 0 and self.embed_audio and self.batch["status"] in {"running", "stopping"}
                if os.name == "nt" and keep_awake != sleep_blocked:
                    continuous, system_required = 0x80000000, 0x00000001
                    ctypes.windll.kernel32.SetThreadExecutionState(continuous | (system_required if keep_awake else 0))
                    sleep_blocked = keep_awake
                settings = dict(self.settings)
                candidate = None
                key = None
                has_work = bool(self.pending or self.batch_pending or
                                (self.batch["status"] == "running" and not self.batch["current"]) or
                                (self.prefetch_pending and self.prefetch_remaining > 0 and settings["prefetch"]))
                if has_work and settings["mode"] != "disabled":
                    limit = self._agent_limit()
                    if index < limit and len(self.running) < limit:
                        if self.pending:
                            key = self.pending.pop(0)
                        elif self.prefetch_pending and self.prefetch_remaining > 0 and settings["prefetch"]:
                            candidate = (self.prefetch_pending.popleft(), self.prefetch_revision)
                            self.prefetch_remaining -= 1
                        else:
                            key = self._next_batch_job(settings)
                if key:
                    is_batch = self.jobs[key]["reason"] == "batch"
                    cancel = threading.Event()
                    self.running[key] = (engine, cancel)
                    job = self.jobs[key]
                    job.update(status="running", phase="Checking saved lyrics", startedAt=time.time())
            if candidate:
                self._schedule_prefetch(*candidate, settings)
                continue
            if not key:
                if index == 0 and settings["mode"] == "resident" and not engine.status()["loaded"] and not self.warm_error:
                    try:
                        engine.ready(settings, threading.Event())
                    except Exception as exc:
                        self.warm_error = str(exc)
                        engine.stop()
                elif index != 0 or settings["mode"] != "resident":
                    engine.stop()
                with self.condition:
                    self.condition.wait(timeout=.15 if has_work else 1)
                continue
            try:
                started = time.monotonic()
                path = self.get_path(job["trackId"])
                result = None
                metadata = self.get_metadata(job["trackId"])
                references = []
                if not job.get("force") and metadata.get("embedded"):
                    raw = metadata["embedded"]
                    result = {"raw": raw, "source": "Embedded lyrics", "kind": "saved",
                              "embeddedDigest": lyric_digest(raw)}
                if result is None and job["reason"] in {"prefetch", "batch"}:
                    saved = self.get_saved(job["trackId"])
                    stale_ai = saved.get("timingKind") == "model" and not has_manual_timing(saved)
                    if (saved.get("lines") or saved.get("lyrics")) and not stale_ai:
                        result = {"lines": saved.get("lines", []), "raw": saved.get("lyrics", ""), "source": saved.get("source", "Saved lyrics"), "kind": "saved", "timingKind": saved.get("timingKind", "line"), "offset": saved.get("offset", 0), "scale": saved.get("scale", 1)}
                    elif is_batch and self.embed_audio:
                        result = self.cached(job["trackId"])
                        if result:
                            result.pop("embedError", None)
                    if result is None and (metadata.get("sidecar") or metadata.get("embedded")):
                        result = {"raw": metadata.get("sidecar") or metadata["embedded"], "source": "Local lyrics", "kind": "saved", "timingKind": "line"}
                if result is None:
                    job["phase"] = "Finding reference lyrics"
                    try:
                        references = self._references(metadata)
                        close = next((r for r in references if not r["alternate"]), None)
                        if close and not job.get("force"):
                            result = {"raw":close["syncedLyrics"],"source":"LRCLIB · "+close["artistName"],"kind":"online",
                                      "duration":metadata["duration"],"timingKind":"line","pipelineVersion":PIPELINE_VERSION}
                    except (OSError, ValueError):
                        pass
                if cancel.is_set():
                    raise Cancelled()
                if result is None and not job.get("force"):
                    job["phase"] = "Checking saved audio timing"
                    result = self._reuse_draft(path, settings, references, metadata, cancel)
                if result is None and not cancel.is_set():
                    with self.condition:
                        limit = self.settings["maxTrackMinutes"]
                    if limit and float(metadata.get("duration") or 0) > limit * 60:
                        raise TrackTooLong(limit)
                    settings["maxTrackMinutes"] = limit
                    result = engine.run(path, settings, cancel, lambda phase: job.update(phase=phase))
                    job["phase"] = "Matching lyrics to the audio"
                    fit = self._match(result["lines"], references, {**metadata, "duration": result["duration"]}, cancel)
                    if fit:
                        result.update(fit)
                if cancel.is_set():
                    raise Cancelled()
                with TAG_LOCK:
                    if key != recording_key(path, settings):
                        raise ValueError("The audio changed while it was being prepared. Try again.")
                    if self.embed_audio and not metadata.get("embedded") and (is_batch or result["kind"] != "saved"):
                        raw = lyrics_text(result)
                        if raw:
                            try:
                                result.update(save_audio_lyrics(path, raw))
                            except Exception as error:
                                result["embedError"] = str(error)
                    result["seconds"] = round(time.monotonic() - started, 2)
                    result["trackId"] = job["trackId"]
                    saved_key = recording_key(path, settings)
                    write_json(self.folder / (saved_key + ".json"), result)
                duration = result.get("duration", 0)
                job.update(status="complete", phase="Ready", seconds=result["seconds"],
                           underMinute=result["seconds"] <= 60, fasterThanSong=bool(duration and result["seconds"] < duration))
            except Cancelled:
                job.update(status="cancelled", phase="Cancelled")
            except TrackTooLong as exc:
                job.update(status="skipped", phase=str(exc))
            except AgentCapacityChanged:
                job.update(status="cancelled" if cancel.is_set() else "queued",
                           phase="Cancelled" if cancel.is_set() else "Waiting for GPU memory")
            except Exception as exc:
                job.update(status="cancelled" if cancel.is_set() else "failed", phase="Cancelled" if cancel.is_set() else str(exc), seconds=round(time.time() - job["startedAt"], 2))
                engine.stop()
            finally:
                with self.condition:
                    self.running.pop(key, None)
                    if job["status"] == "queued":
                        if is_batch and self.batch["status"] == "running":
                            self.batch_pending.appendleft(job["trackId"])
                        elif not is_batch:
                            self.pending.insert(0, key)
                        else:
                            job.update(status="cancelled", phase="Cancelled")
                    if (job["status"] == "skipped" and job["reason"] == "prefetch"
                            and job.get("prefetchRevision") == self.prefetch_revision):
                        self.prefetch_remaining += 1
                    if is_batch:
                        self.batch["current"] = next((self.jobs[active]["trackId"] for active in self.running
                                                      if self.jobs[active]["reason"] == "batch"), None)
                        if job["status"] == "complete":
                            self.batch["consecutiveFailures"] = 0
                            if result.get("embedError"):
                                self.batch["lastError"] = "Lyrics cached but not embedded: " + result["embedError"]
                            persisted = result.get("embeddedDigest") or result.get("sidecarSaved")
                            if self.embed_audio:
                                counter = "skipped" if metadata.get("embedded") else "prepared" if persisted else "failed"
                            else:
                                counter = "skipped" if result["kind"] in {"saved", "online"} else "prepared"
                            self.batch[counter] += 1
                        elif job["status"] == "skipped":
                            self.batch["skipped"] += 1
                            self.batch["skippedLong"] += 1
                            self.batch["consecutiveFailures"] = 0
                        elif job["status"] == "failed":
                            self.batch["failed"] += 1
                            self.batch["lastError"] = job["phase"]
                            self.batch["consecutiveFailures"] = self.batch.get("consecutiveFailures", 0) + 1
                            if self.batch["consecutiveFailures"] >= 3:
                                self.batch["status"] = "stopping"
                                self.batch_pending.clear()
                        if self.batch["status"] == "stopping" and not self.batch["current"]:
                            self.batch["status"] = "paused"
                        elif self.batch["status"] == "running" and not self.batch_pending and not self.batch["current"]:
                            self.batch["status"] = "complete"
                        if self.batch["status"] in {"complete", "paused"}:
                            self._clear_batch_timer()
                        if len(self.jobs) > 80:
                            for old in list(self.jobs):
                                if old not in self.running and self.jobs[old]["status"] not in {"running", "queued"}:
                                    del self.jobs[old]
                                    if len(self.jobs) <= 60:
                                        break
                    self.condition.notify_all()
