// Pure timing helpers shared by the player and focused verification.
export function clock(value) {
  const n = Math.max(0, Number(value) || 0);
  return `${Math.floor(n / 60)}:${String(Math.floor(n % 60)).padStart(2, '0')}`;
}

export function stamp(value) {
  const cs = Math.round(Math.max(0, value) * 100);
  return `${String(Math.floor(cs / 6000)).padStart(2, '0')}:${String(Math.floor(cs / 100) % 60).padStart(2, '0')}.${String(cs % 100).padStart(2, '0')}`;
}

function seconds(m, s) { return Number(m) * 60 + Number(s); }

export function parseLyrics(raw) {
  let offset = 0;
  const offsetTag = raw.match(/\[offset:([+-]?\d+)\]/i);
  if (offsetTag) offset = Number(offsetTag[1]) / 1000;
  const lines = [];
  let enhanced = false;
  for (const row of raw.replace(/^\uFEFF/, '').split(/\r?\n/)) {
    const tags = [...row.matchAll(/\[(\d{1,3}):(\d{2}(?:[.,]\d+)?)\]/g)];
    if (tags.length) {
      const body = row.replace(/\[\d{1,3}:\d{2}(?:[.,]\d+)?\]/g, '').trim();
      const markers = [...body.matchAll(/<(\d{1,3}):(\d{2}(?:[.,]\d+)?)>/g)];
      const words = [];
      for (let i = 0; i < markers.length; i++) {
        const marker = markers[i];
        const text = body.slice(marker.index + marker[0].length, markers[i + 1]?.index ?? body.length);
        if (text.trim()) words.push({text, time: seconds(marker[1], marker[2].replace(',', '.')) + offset});
      }
      const text = body.replace(/<\d{1,3}:\d{2}(?:[.,]\d+)?>/g, '').trim();
      if (!text) continue;
      for (const tag of tags) {
        // Repeated line timestamps shift enhanced-word timestamps by the same amount.
        const delta = seconds(tag[1], tag[2].replace(',', '.')) - seconds(tags[0][1], tags[0][2].replace(',', '.'));
        lines.push({time: seconds(tag[1], tag[2].replace(',', '.')) + offset, text,
                    words: words.map(w => ({...w, time: w.time + delta}))});
      }
      enhanced ||= words.length > 0;
    } else if (row.trim() && !/^\[[a-z][a-z0-9_-]*:.*\]\s*$/i.test(row)) {
      lines.push({time: null, text: row.trim(), words: []});
    }
  }
  const timed = lines.some(l => l.time !== null);
  // Provider files occasionally include a plain heading among timestamped lines.
  const result = timed ? lines.filter(l => l.time !== null).sort((a, b) => a.time - b.time) : lines;
  return {lines: result, timed, enhanced};
}

export function activeLine(lines, time) {
  let index = -1, latest = -Infinity;
  lines.forEach((line, i) => {
    if (line.time !== null && line.time <= time && line.time >= latest) { index = i; latest = line.time; }
  });
  return index>=0&&Number.isFinite(lines[index].end)&&time>=lines[index].end?-1:index;
}

export function lyricTime(audioTime, offset = 0, scale = 1) { return (audioTime - offset) / scale; }
export function audioTime(lyric, offset = 0, scale = 1) { return lyric * scale + offset; }

export function fitAnchors(a, b) {
  if (!a || !b || Math.abs(b.lyric - a.lyric) < 1) throw new Error('Choose two different lyric lines, at least one second apart.');
  const scale = (b.audio - a.audio) / (b.lyric - a.lyric);
  if (!Number.isFinite(scale) || scale < .25 || scale > 4) throw new Error('Those points produce an unlikely timing change. Check both points.');
  return {scale, offset: a.audio - a.lyric * scale};
}

export function wordSegments(line, nextTime, duration) {
  if (!Number.isFinite(line.time) || line.uncertain) return [];
  const boundary = Number.isFinite(nextTime) && nextTime > line.time ? nextTime : duration > line.time ? duration : line.time + 5;
  const next = Number.isFinite(line.end)&&line.end>line.time?Math.min(boundary,line.end):boundary;
  if (line.words?.length) return line.words.map((word, i) => {
    const boundary=line.words[i + 1]?.time ?? next;
    return {...word,end:Number.isFinite(word.end)&&word.end>word.time?Math.min(word.end,boundary):boundary};
  });
  const tokens = line.text.match(/\S+\s*/gu) || [];
  const segments = [];
  let cursor = line.time;
  let index = 0;
  for (const anchor of line.wordAnchors || []) {
    if (!Number.isInteger(anchor.index) || anchor.index < index || anchor.index >= tokens.length ||
        !Number.isFinite(anchor.time) || !Number.isFinite(anchor.end) ||
        anchor.time < cursor || anchor.time >= next || anchor.end <= anchor.time) continue;
    segments.push(...estimateWords(tokens.slice(index, anchor.index), cursor, anchor.time));
    cursor = Math.min(anchor.end, next);
    segments.push({text: tokens[anchor.index], time: anchor.time, end: cursor});
    index = anchor.index + 1;
  }
  // Avoid stretching the remaining words through a long instrumental gap.
  const rest = tokens.slice(index);
  segments.push(...estimateWords(rest, cursor, Math.min(next, cursor + Math.max(1.4, rest.length * .5))));
  return segments;
}

function estimateWords(tokens, start, end) {
  const weights = tokens.map(token => Math.max(1, [...token.trim()].length ** .65));
  const total = weights.reduce((sum, weight) => sum + weight, 0);
  let cursor = start;
  return tokens.map((text, index) => {
    const time = cursor;
    cursor += (end - start) * weights[index] / total;
    return {text, time, end: cursor};
  });
}

export function cleanTitle(value) {
  return value.replace(/\([^)]*(?:official\s*(?:music\s*)?video|lyric\s*video|official\s*audio)[^)]*\)/gi, '')
    .replace(/\[[^\]]*(?:official\s*(?:music\s*)?video|lyric\s*video|official\s*audio)[^\]]*\]/gi, '')
    .replace(/\b(?:closed[ -]captioned|official music video|official video|lyric video|official audio)\b/gi, '')
    .replace(/\s+/g, ' ').trim();
}

function normalized(value) { return value.normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase().replace(/[^\p{L}\p{N}]/gu, ''); }
function similarity(a, b) {
  a = normalized(a); b = normalized(b);
  if (!a || !b) return 0;
  if (a === b) return 1;
  const grams = s => new Set(Array.from({length: Math.max(1, s.length - 1)}, (_, i) => s.slice(i, i + 2)));
  const x = grams(a), y = grams(b);
  return 2 * [...x].filter(v => y.has(v)).length / (x.size + y.size);
}

export function rankCandidates(rows, track) {
  return rows.filter(row => row.duration > 0 && track.duration > 0 &&
    Math.floor(row.duration) === Math.floor(track.duration) &&
    Math.abs(row.duration - track.duration) <= .050000001).map(row => {
    const delta = row.duration - track.duration;
    const titleScore = similarity(cleanTitle(track.title), cleanTitle(row.trackName));
    const artistScore = similarity(track.artist, row.artistName);
    const safe = titleScore > .88 && artistScore > .82 && !!row.syncedLyrics;
    const score = titleScore * 45 + artistScore * 30 + (row.syncedLyrics ? 3 : 0);
    return {...row, delta, safe, score};
  }).sort((a, b) => Math.abs(a.delta) - Math.abs(b.delta) || b.score - a.score);
}

export function exportLrc(lines, offset, scale, title = '', artist = '') {
  const header = [`[ti:${title.replace(/[\r\n\]]/g, '')}]`, `[ar:${artist.replace(/[\r\n\]]/g, '')}]`, '[by:Verse Lab local prototype]'];
  const body = lines.filter(l => l.time !== null).map(line => {
    const words = line.words?.length ? line.words.map(w => `<${stamp(audioTime(w.time, offset, scale))}>${w.text}`).join('') : line.text;
    return `[${stamp(audioTime(line.time, offset, scale))}]${words}`;
  });
  return body.length ? [...header, ...body].join('\n') + '\n' : lines.map(l => l.text).join('\n');
}
