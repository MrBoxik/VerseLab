"""Align an alternate recording to a copy of the original audio."""
from bisect import bisect_right
from dataclasses import dataclass
import math
import shutil
import statistics
import subprocess


@dataclass(frozen=True)
class AudioSpan:
    start: float
    end: float
    source_times: tuple
    offsets: tuple

    def time_at(self, source_time):
        index = bisect_right(self.source_times, source_time)
        if index == 0:
            return source_time + self.offsets[0]
        if index == len(self.source_times):
            return source_time + self.offsets[-1]
        before, after = self.source_times[index-1:index+1]
        fraction = (source_time-before) / (after-before)
        return source_time + self.offsets[index-1] + fraction * (self.offsets[index]-self.offsets[index-1])


def spectral_frames(path, cancel, np):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    samples = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-t", "600", "-ar", "8000", "-ac", "1", "-f", "f32le", "-"],
        capture_output=True, check=True, timeout=25,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if cancel.is_set():
        return None
    samples = np.frombuffer(samples.stdout, dtype="<f4")
    fft_size = 1024
    frames = np.lib.stride_tricks.sliding_window_view(np.pad(samples, fft_size // 2), fft_size)[::400]
    power = np.abs(np.fft.rfft(frames * np.hanning(fft_size)))
    bands = 110 * 2 ** (np.arange(124) / 24) / (8000 / fft_size)
    bands = bands[bands < power.shape[1] - 2]
    features = np.stack([
        power[:, max(1, int(bin / 1.022)):max(int(bin * 1.022) + 1, int(bin / 1.022) + 2)].mean(axis=1)
        for bin in bands
    ], axis=1)
    return np.log(features + .025).astype(np.float32)


def normalize(frames, np):
    frames = frames - frames.mean(axis=-2, keepdims=True)
    frames -= frames.mean(axis=-1, keepdims=True)
    return frames / np.maximum(np.linalg.norm(frames, axis=-1, keepdims=True), 1.e-6)


def compare_patches(source, target, starts, np):
    """Each row finds a five-second audio patch anywhere in the source."""
    patches = normalize(target[starts[:, None] + np.arange(50)], np)
    windows = np.lib.stride_tricks.sliding_window_view(normalize(source, np), 50, axis=0)
    windows = windows.transpose(0, 2, 1).reshape(len(windows), -1).copy()
    return patches.reshape(len(patches), -1) @ windows.T / 50


def match_audio(source_path, edited_path, fit, cancel):
    """Locate independently matching audio sections, including reordered repeats."""
    try:
        import numpy as np
    except ImportError:
        return [], 0
    try:
        original = spectral_frames(source_path, cancel, np)
        edited = spectral_frames(edited_path, cancel, np)
    except (subprocess.SubprocessError, OSError, ValueError):
        return [], 0
    if original is None or edited is None or min(len(original), len(edited)) < 150:
        return [], 0

    scale = fit["scale"]
    # Stretch the original's timeline to the estimated tempo, then search its pitch shift.
    indices = np.arange(0, len(original) - 1, 2 / scale)
    indices = np.minimum(indices, len(original)-2)
    lower = indices.astype(int)
    fraction = (indices - lower).astype(np.float32)[:, None]
    source = original[lower] * (1 - fraction) + original[lower + 1] * fraction
    edited = edited[::2]
    seed = fit["offset"]
    anchors = fit["anchors"]
    markers = np.array(sorted({
        int((anchor["audio"] - 1.5) * 10) for anchor in anchors if 3 <= anchor["audio"] <= len(edited)/10 - 4
    }))
    if len(markers) < 3:
        return [], 0

    def bands(shift):
        if shift == 0:
            return source, edited
        if shift > 0:
            return source[:, :-shift], edited[:, shift:]
        return source[:, -shift:], edited[:, :shift]

    best = (-1, 0, 0)
    pitch = round(24 * math.log2(1 / scale))
    for shift in sorted(set(range(-7, 8)) | set(range(pitch-6, pitch+7))):
        if cancel.is_set():
            return [], 0
        src, dst = bands(shift)
        # Only compare patches near the text fit while estimating pitch.
        src_norm = normalize(src, np)
        dst_patches = normalize(dst[markers[:, None] + np.arange(50)], np)
        max_start = len(src) - 50
        if max_start <= 0:
            return [], 0
        for change in range(-27, 28):
            starts = np.clip(markers - round(seed*10) + change, 0, max_start)
            samples = src_norm[starts[:, None] + np.arange(50)]
            score = float(np.mean(np.sum(samples * dst_patches, axis=-1)))
            if score > best[0]:
                best = (score, shift, change)
    quality, shift, change = best
    if quality < .25 or cancel.is_set():
        return [], 0
    initial_offset = seed - change / 10
    src, dst = bands(shift)
    centers = np.arange(25, len(dst)-24, 10)
    scores = compare_patches(src, dst, centers-25, np)
    matches = select_spans(scores, centers, initial_offset, cancel, np)
    # Convert a timestamp from the lyric provider into the local original's clock.
    reference_adjustment = (seed - initial_offset) / scale
    return matches, reference_adjustment


def select_spans(scores, centers, initial_offset, cancel, np):
    prior = {None: (0., ())}
    for center, row in zip(centers, scores):
        if cancel.is_set():
            return []
        peaks = []
        remaining = row.copy()
        for _ in range(8):
            index = int(remaining.argmax())
            value = float(remaining[index])
            if value < .19:
                break
            peaks.append((int(center) - (index + 25), value))
            remaining[max(0, index-4):index+5] = -1
        candidates = {**dict(peaks), None: .235}
        step = {}
        for offset, quality in candidates.items():
            bonus = .025 if offset is not None and abs(offset - initial_offset * 10) <= 5 else 0
            def penalty(previous):
                if previous is None or offset is None:
                    return 0 if previous == offset else .17
                return .025 * abs(offset-previous) if abs(offset-previous) <= 4 else .38
            match = max(prior, key=lambda previous: prior[previous][0] - penalty(previous))
            total, path = prior[match]
            step[offset] = (total + quality + bonus - penalty(match), (*path, offset))
        prior = step
    _, path = max(prior.values(), key=lambda route: route[0])
    groups = []
    for frame, offset in zip(centers, path):
        if offset is None:
            continue
        time = float(frame) / 10
        shift = offset / 10
        if groups and time - groups[-1][-1][0] <= 1.1 and abs(shift - statistics.median(x[1] for x in groups[-1])) <= .4:
            groups[-1].append((time, shift))
        else:
            groups.append([(time, shift)])
    spans = []
    for run in groups:
        if len(run) < 5:
            continue
        offsets = tuple(statistics.median(shift for _, shift in run[max(0, i-2):i+3]) for i in range(len(run)))
        source_times = tuple(time - offset for (time, _), offset in zip(run, offsets))
        spans.append(AudioSpan(run[0][0]-1, run[-1][0]+1, source_times, offsets))
    return spans
