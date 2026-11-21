"""Recording-clock boundaries and conservative matching to known lyric text."""
from collections import Counter
import difflib
import itertools
import math
import re
import unicodedata

PIPELINE_VERSION = 6
FIXED_MODEL = "large-v3"
LYRICS_DURATION_TOLERANCE = .050


def matches_lyric_duration(record, duration):
    try:
        candidate = float(record.get("duration") or 0)
        duration = float(duration)
    except (TypeError, ValueError):
        return False
    if (not math.isfinite(duration) or not math.isfinite(candidate)
            or min(duration, candidate) <= 0 or int(duration) != int(candidate)
            or abs(duration - candidate) > LYRICS_DURATION_TOLERANCE + 1e-9):
        return False
    raw = record.get("syncedLyrics") or ""
    if not raw:
        return True
    cues = timed_reference(raw)
    offset = re.search(r"\[offset:([+-]?\d+)\]", raw, re.I)
    shift = int(offset[1]) / 1000 if offset else 0
    word_times = (int(stamp[1]) * 60 + float(stamp[2].replace(",", ".")) + shift
                  for stamp in re.finditer(r"<(\d+):(\d+(?:[.,]\d+)?)>", raw))
    limit = min(duration, candidate) + LYRICS_DURATION_TOLERANCE
    return bool(cues) and cues[-1]["time"] <= limit and all(time <= limit for time in word_times)


def tokens(text):
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.findall(r"[^\W_]+", text, re.UNICODE)


def song_query(metadata):
    title, artist = metadata.get("title", ""), metadata.get("artist", "")
    # This narrow repair covers the observed downloader's reversed Nightcore tags.
    if re.fullmatch(r"\s*[\[(]?(?:lyrics?|official\s*(?:video|audio))[\])]?\s*", title, re.I):
        match = re.search(r"nightcore[^→]*→\s*(.+?)\s*\(([^()]+)\)\s*$", artist, re.I)
        if match:
            return {"title": match[1].strip(), "artist": match[2].strip(), "repaired": True}
    title = re.sub(r"\([^)]*(?:lyric video|official.*?video)[^)]*\)|\bClosed-Captioned\b", "", title, flags=re.I).strip()
    return {"title": title, "artist": artist, "repaired": False}


def reference_queries(metadata):
    exact = song_query(metadata)
    yield {**exact, "alternate": bool(exact["repaired"] or metadata.get("metadataCorrected"))}
    title = re.sub(
        r"[\s–—-]+(?:[\[(]\s*)?(?:(?:hardstyle|techno|dance|edm|club|bass|extended)\s+)?"
        r"(?:remix|rework|nightcore|sped[\s-]+up|slowed(?:\s*\+\s*reverb)?|radio edit)"
        r"(?:\s*[\[(].*?[\])])?\s*[\])]?\s*$",
        "", exact["title"], flags=re.I,
    ).strip()
    if title and title != exact["title"]:
        yield {**exact, "title": title, "alternate": True}


def timed_reference(raw):
    entries = []
    offset = re.search(r"\[offset:([+-]?\d+)\]", raw, re.I)
    shift = int(offset[1]) / 1000 if offset else 0
    for row in raw.splitlines():
        stamps = list(re.finditer(r"\[(\d+):(\d+(?:[.,]\d+)?)\]", row))
        body = re.sub(r"\[\d+:\d+(?:[.,]\d+)?\]|<\d+:\d+(?:[.,]\d+)?>", "", row).strip()
        for tag in stamps:
            entries.append({"time": int(tag[1]) * 60 + float(tag[2].replace(",", ".")) + shift, "text": body})
    entries.sort(key=lambda line: line["time"])
    for i, line in enumerate(entries):
        line["end"] = next((x["time"] for x in entries[i+1:] if x["time"] > line["time"]), line["time"] + 5)
    return [line for line in entries if line["text"]]


def match_reference(recognized, record, duration):
    """Use distinctive, agreeing audio phrases; never align by counting lyric rows."""
    reference = timed_reference(record.get("syncedLyrics") or "")
    if len(reference) < 6 or duration <= 0:
        return None
    observed = [line for line in recognized if line.get("text") and line.get("time") is not None]
    ref_counts = Counter(tuple(tokens(line["text"])) for line in reference)
    obs_counts = Counter(tuple(tokens(line["text"])) for line in observed)
    anchors = []
    for line in observed:
        words = tokens(line["text"])
        if len(words) < 4 or obs_counts[tuple(words)] != 1:
            continue
        choices = []
        for target in reference:
            expected = tokens(target["text"])
            if len(expected) < 4 or ref_counts[tuple(expected)] != 1:
                continue
            similarity = difflib.SequenceMatcher(None, words, expected, autojunk=False).ratio()
            if similarity >= .8:
                choices.append((similarity, target))
        if choices:
            quality, target = max(choices, key=lambda x:x[0])
            anchors.append({"reference": target["time"], "audio": line["time"], "quality": quality})
    # Match distinctive four-word starts across recognition row boundaries as well.
    reference_words = [word for line in reference for word in tokens(line["text"])]
    ref_grams = Counter(tuple(reference_words[i:i+4]) for i in range(max(0,len(reference_words)-3)))
    heard, heard_times = [], []
    for line in observed:
        for word in line.get("words",[]):
            values=tokens(word["text"])
            heard.extend(values);heard_times.extend([word["time"]]*len(values))
    heard_grams = {}
    for i in range(max(0,len(heard)-3)):
        heard_grams.setdefault(tuple(heard[i:i+4]),[]).append(i)
    for line in reference:
        values=tokens(line["text"])
        phrase=tuple(values[:4])
        matches=heard_grams.get(phrase,[])
        if len(values)>=4 and ref_grams[phrase]==1 and len(matches)==1:
            anchors.append({"reference":line["time"],"audio":heard_times[matches[0]],"quality":1.0})
    # Several observed fragments must not vote repeatedly for the same reference line.
    unique = {}
    ratio = duration / max(1, float(record.get("duration") or duration))
    for anchor in anchors:
        key = anchor["reference"]
        if key not in unique or abs(anchor["audio"] - ratio*key) < abs(unique[key]["audio"] - ratio*key):
            unique[key] = anchor
    anchors = list(unique.values())
    best = []
    for first, last in itertools.combinations(anchors, 2):
        delta = last["reference"] - first["reference"]
        if abs(delta) < 25:
            continue
        scale = (last["audio"] - first["audio"]) / delta
        offset = first["audio"] - scale * first["reference"]
        if not .5 <= scale <= 1.6 or not .7*ratio <= scale <= 1.3*ratio or abs(offset) > 30:
            continue
        agreeing = [a for a in anchors if abs(a["audio"] - (scale*a["reference"] + offset)) <= 2.2]
        if len(agreeing) > len(best):
            best = agreeing
    if len(best) < 3:
        return None
    if max(a["reference"] for a in best) - min(a["reference"] for a in best) < .4 * float(record.get("duration") or duration):
        return None
    mean_x = sum(a["reference"] for a in best) / len(best)
    mean_y = sum(a["audio"] for a in best) / len(best)
    denominator = sum((a["reference"]-mean_x)**2 for a in best)
    if denominator <= 0:
        return None
    scale = sum((a["reference"]-mean_x)*(a["audio"]-mean_y) for a in best) / denominator
    offset = mean_y - scale * mean_x
    errors = [abs(a["audio"]-(scale*a["reference"]+offset)) for a in best]
    if sum(errors)/len(errors) > 1.5:
        return None
    regions = phrase_regions(anchors, best, scale, offset)
    lines = []
    for region in regions:
        lower, upper, points = region
        for line in reference:
            if not lower <= line["time"] <= upper:
                continue
            start = max(0, phrase_time(line["time"], scale, points))
            end = min(duration, phrase_time(line["end"], scale, points))
            if start < duration and end > start:
                lines.append({"time": start, "end": end, "text": line["text"], "words": [], "timing": "reference-fit"})
    if len(regions) > 1:
        lines = fill_with_draft(lines, observed)
    lines.sort(key=lambda line: line["time"])
    refined = refine_reference(lines, observed, duration)
    source = "Matched lyrics · includes draft sections" if len(regions) > 1 else "Matched lyrics"
    return {"lines": lines, "source": source, "kind": "reference_aligned",
            "timingKind": "reference", "alignment": {"scale": scale, "offset": offset, "anchors": best,
            "meanError": round(sum(errors)/len(errors), 3), "locallyTimedLines": refined}, "referenceId": record.get("id"), "pipelineVersion": PIPELINE_VERSION}


def phrase_regions(anchors, best, scale, offset):
    """Use corroborated local phrase starts and detect a cut with several unique lines."""
    points = [(a["reference"], a["audio"] - scale*a["reference"]) for a in best]
    main = sorted((t, local if abs(local-offset) <= .7 else offset) for t, local in points)
    lower, upper = main[0][0], main[-1][0]
    outliers = [(a["reference"], a["audio"] - scale*a["reference"]) for a in anchors
                if a["quality"] >= .9 and abs(a["audio"] - scale*a["reference"] - offset) > 2.5]
    for before in (False, True):
        candidates = [(t, local) for t, local in outliers if (t < lower if before else t > upper)]
        if len(candidates) < 3:
            continue
        matches = max(
            ([point for point in candidates if abs(point[1]-local) <= .9] for _, local in candidates),
            key=len,
        )
        if len(matches) < 3:
            continue
        matches.sort()
        first, last = matches[0][0], matches[-1][0]
        if last-first < 5:
            continue
        if before and phrase_time(last, scale, matches) < phrase_time(lower, scale, main):
            return [(-math.inf, last+4, matches), (lower-4, math.inf, main)]
        if not before and phrase_time(first, scale, matches) > phrase_time(upper, scale, main):
            return [(-math.inf, upper+4, main), (first-4, math.inf, matches)]
    return [(-math.inf, math.inf, main)]


def phrase_time(time, scale, points):
    # Interpolate locally, keeping independent corrections on either side of an edit.
    before = points[0]
    for after in points[1:]:
        if time <= after[0]:
            fraction = min(1, max(0, (time-before[0]) / (after[0]-before[0])))
            return scale*time + before[1] + fraction*(after[1]-before[1])
        before = after
    return scale*time + before[1]


def refine_reference(lines, observed, duration):
    """Look for independent start and end evidence in each line's audio neighborhood."""
    heard = sorted((word for line in observed if line.get("timing") == "dtw"
                    and not line.get("review") for word in line.get("words", [])
                    if tokens(word["text"]) and word.get("end", 0) > word["time"]),
                   key=lambda word: word["time"])
    keys = [tuple(tokens(word["text"])) for word in heard]
    predicted = [line["time"] for line in lines]
    refined = 0
    for index, line in enumerate(lines):
        if line.get("timing") not in {"reference-fit", "audio-match", None}:
            continue
        spellings = re.findall(r"\S+\s*", line["text"])
        expected = [tuple(tokens(word)) for word in spellings]
        if len(expected) < 3 or not all(expected):
            continue
        start = predicted[index]
        lower = max(0, start - 1.1)
        upper = start + 1.1
        if index:
            lower = max(lower, (predicted[index-1] + start) / 2)
        following = predicted[index+1] if index+1 < len(lines) else duration
        upper = min(upper, (start + following) / 2)
        best = None
        for first, word in enumerate(heard):
            if not lower <= word["time"] <= upper:
                continue
            for length in range(max(3, len(expected)-2), len(expected)+3):
                portion = heard[first:first+length]
                if len(portion) != length or portion[-1]["time"] >= following:
                    continue
                if any(not 0 < right["time"]-left["time"] <= 1.6 for left, right in zip(portion, portion[1:])):
                    continue
                matcher = difflib.SequenceMatcher(None, expected, keys[first:first+length], autojunk=False)
                ratio = matcher.ratio()
                if ratio < .7:
                    continue
                reliable = matcher.get_matching_blocks()[:-1]
                score = ratio - .05*abs(word["time"]-start)
                if best is None or score > best[0]:
                    best = (score, ratio, portion, reliable)
        if best is None:
            continue
        _, ratio, portion, blocks = best
        begin, finish = blocks[0], blocks[-1]
        start_match = begin.a == 0 and begin.b == 0
        end_match = finish.a+finish.size == len(expected) and finish.b+finish.size == len(portion)
        start = portion[0]["time"] if start_match else line["time"]
        end = min(following, portion[-1]["end"]) if end_match else line["end"]
        if end <= start:
            continue
        line["time"], line["end"] = start, end
        if ratio == 1:
            line.update(timing="reference-dtw", words=[
                {**word, "text": text, "end": min(word["end"], line["end"])}
                for text, word in zip(spellings, portion)
            ])
        else:
            line["wordAnchors"] = [
                {"index": block.a + i, "time": word["time"], "end": min(word["end"], end)}
                for block in blocks
                for i, word in enumerate(portion[block.b:block.b+block.size])
                if start <= word["time"] < end
            ]
        if start_match or end_match:
            refined += 1
    bound_timeline(lines)
    return refined


def bound_timeline(lines):
    for line, following in zip(lines, lines[1:]):
        line["end"] = min(line["end"], following["time"])
        line["words"] = [{**word, "end": min(word["end"], line["end"])}
                         for word in line.get("words", []) if word["time"] < line["end"]]
        if "wordAnchors" in line:
            line["wordAnchors"] = [{**word, "end": min(word["end"], line["end"])}
                                   for word in line["wordAnchors"] if word["time"] < line["end"]]


def fill_with_draft(lines, observed):
    return lines + [line.copy() for line in observed if not any(
        line["time"] < other["end"] + .2 and line.get("end", line["time"]) > other["time"] - .2 for other in lines
    )]


def place_audio_reference(record, recognized, duration, alignment, spans, adjustment):
    """Transfer verified words only where reference audio fits the recording."""
    if sum(span.end-span.start for span in spans) < .55*duration:
        return None
    reference = timed_reference(record["syncedLyrics"])
    lines = []
    for span in spans:
        for line in reference:
            start = span.time_at((line["time"]+adjustment)*alignment["scale"])
            if not 0 <= start < min(duration, span.end):
                continue
            if start < span.start:
                # A patch may straddle an edit. Require local words before extending its edge.
                if span.start-start > 1.8 or not any(
                    abs(heard["time"]-start) < .7 and
                    difflib.SequenceMatcher(None, tokens(heard["text"]), tokens(line["text"]), autojunk=False).ratio() >= .65
                    for heard in recognized
                ):
                    continue
            end = min(duration, span.end + .7, span.time_at((line["end"]+adjustment)*alignment["scale"]))
            if end > start:
                lines.append({**line, "time": start, "end": end, "words": [], "timing": "audio-match"})
    if len(lines) < 6:
        return None
    placed = []
    for line in sorted(lines, key=lambda line: line["time"]):
        if placed and line["time"]-placed[-1]["time"] < 1 and tokens(line["text"]) == tokens(placed[-1]["text"]):
            continue
        placed.append(line)
    # Keep a draft only in uncovered stretches. A remix may add new vocals to the original.
    uncovered = [line for line in recognized
                 if not any(span.start - .5 <= line["time"] <= span.end + .5 for span in spans)]
    placed = sorted(fill_with_draft(placed, uncovered), key=lambda line: line["time"])
    refined = refine_reference(placed, recognized, duration)
    return {
        "lines": placed, "source": "Lyrics matched to this recording", "kind": "reference_aligned",
        "timingKind": "reference", "referenceId": record.get("id"), "pipelineVersion": PIPELINE_VERSION,
        "alignment": {**alignment, "locallyTimedLines": refined, "audioLines": len(lines),
                      "audioSections": len(spans), "audioAnchors": sum(len(span.source_times) for span in spans)},
    }


def segment_words(segment, body):
    """Align decoded words to token timing without decoding split UTF-8 bytes twice."""
    groups = []
    boundary = True
    for token in segment["tokens"]:
        piece = token["text"] or ""
        if piece.startswith("[_"):
            continue
        for fragment in re.findall(r"\s+|\S+", piece):
            if fragment.isspace():
                boundary = True
                continue
            if boundary:
                groups.append(["", []])
                boundary = False
            groups[-1][0] += fragment
            start = token["dtw"]
            if segment["windowStart"] <= start < segment["windowEnd"]:
                groups[-1][1].append(start)

    decoded = body.split()
    if len(decoded) != len(groups) or "\ufffd" in body:
        return []
    words = []
    for text, (assembled, positions) in zip(decoded, groups):
        # Native token strings may contain replacement characters for partial bytes.
        if text != assembled:
            pattern = ".+?".join(re.escape(part) for part in re.split(r"\ufffd+", assembled))
            if not re.fullmatch(pattern, text):
                return []
        if not positions or (words and min(positions) < words[-1]["time"]):
            return []
        words.append({"text": text, "time": min(positions), "lastToken": max(positions)})
    return words


def gpu_lyric_lines(segments, duration):
    """Turn DTW token positions into readable rows on the recording clock."""
    lines = []
    for segment in segments:
        body = segment["text"].strip()
        if not body or re.fullmatch(r"\[[^\]]+\]|\([^()]+\)", body):
            continue
        core_start, core_end = segment["coreStart"], segment["coreEnd"]
        words = segment_words(segment, body)
        groups = []
        group = []
        for index, word in enumerate(words):
            next_start = words[index+1]["time"] if index+1 < len(words) else word["lastToken"] + .25
            word["end"] = min(core_end, max(word["time"]+.02, min(next_start, word["lastToken"]+.65)))
            del word["lastToken"]
            if not core_start <= word["time"] < core_end:
                continue
            if group and (len(group) >= 9 or word["time"]-group[-1]["end"] > .8):
                groups.append(group)
                group = []
            group.append(word)
        if group:
            groups.append(group)
        for group in groups:
            start = group[0]["time"]
            end = max(word["end"] for word in group)
            line_text = " ".join(word["text"] for word in group)
            for word in group[:-1]:
                word["text"] += " "
            line = {"time": start, "end": end, "text": line_text, "words": group,
                    "section": core_start, "timing": "dtw"}
            if start >= .9 * duration and re.search(
                    r"thanks? for watching|for watching!?$|^subtitles? by|^titulky|^vytvořil ",
                    line_text, re.I):
                line["review"] = "Uncertain outro."
            lines.append(line)
        if not words and core_start <= (segment["time"]+segment["end"])/2 < core_end:
            start = max(core_start, segment["time"])
            end = min(core_end, segment["end"])
            if end > start:
                lines.append({"time": start, "end": end, "text": body, "words": [],
                              "section": core_start, "review": "Word timing unavailable for this line."})
    lines.sort(key=lambda line: line["time"])
    for line, following in zip(lines, lines[1:]):
        if following["time"] > line["time"]:
            line["end"] = min(line["end"], following["time"])
        line["words"] = [{**word, "end": min(word["end"], line["end"])}
                         for word in line["words"] if word["time"] < line["end"]]
    return suspicious_repetition(lines)


def bounded_lines(lines, offset, core_start, core_end, window_duration):
    """Assign a recognition window to its fixed audio-clock interval, including omissions."""
    result = []
    for source in lines:
        start = offset + source["time"]
        end = offset + source.get("end", source["time"] + 3)
        if not all(math.isfinite(v) for v in (start, end)) or end <= start:
            continue
        if not source.get("words"):
            if not core_start <= (start+end)/2 < core_end:
                continue
            result.append({**source, "time": max(core_start, start), "end": min(core_end, end), "section": core_start})
            continue
        words = []
        for original in source["words"]:
            t = offset + original["time"]
            e = offset + original.get("end", original["time"])
            if not all(math.isfinite(v) for v in (t,e)) or e < t or t < offset or e > offset+window_duration+.1:
                continue
            if core_start <= (t+e)/2 < core_end:
                words.append({**original, "time": max(core_start,t), "end": min(core_end,max(t+.01,e))})
        if words:
            result.append({**source, "time": words[0]["time"], "end": min(core_end, max(w["end"] for w in words)),
                           "text": "".join(w["text"] for w in words).strip(), "words": words, "section": core_start})
    return result


def suspicious_repetition(lines):
    """Flag dense loops for review, preserving real repeated choruses rather than deleting them."""
    result = []
    for original in lines:
        line = original
        phrase = tokens(line["text"])
        counts = Counter(phrase)
        if len(phrase) >= 8 and len(counts) <= 3 and max(counts.values()) / len(phrase) >= .65:
            line.update(uncertain=True, heardText=line["text"], text="Unclear vocals", words=[],
                        review="Possible recognition loop.")
        if (line.get("uncertain") and result and result[-1].get("uncertain")
                and line.get("section") == result[-1].get("section")
                and line["time"] <= result[-1].get("end",result[-1]["time"]) + .5):
            result[-1]["end"] = max(result[-1]["end"],line["end"])
            result[-1]["heardText"] += " " + line["heardText"]
        else:
            result.append(line)
    lines = result
    for i, line in enumerate(lines):
        phrase = tokens(line["text"])
        if not 3 <= len(phrase) <= 15:
            continue
        nearby = [x for x in lines[max(0,i-4):i+5] if abs(x["time"]-line["time"]) < 15 and tokens(x["text"]) == phrase]
        if len(nearby) >= 4:
            line["review"] = "Possible recognition loop."
    return lines
