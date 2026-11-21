import {activeLine, audioTime, clock, exportLrc, lyricTime, parseLyrics, rankCandidates, stamp, wordSegments} from './lyrics.mjs';

const pipelineVersion = 6;
const ipc = window.chrome.webview;
const pending = new Map();
let requestId = 0;
ipc.addEventListener('message', ({data}) => {
  const request = pending.get(data.requestId);
  if (!request) return;
  pending.delete(data.requestId);
  if (data.error) request.reject(new Error(data.error));
  else request.resolve(data.value);
});

function call(action, fields = {}) {
  const id = ++requestId;
  return new Promise((resolve, reject) => {
    pending.set(id, {resolve, reject});
    ipc.postMessage({requestId: id, request: {action, ...fields}});
  });
}

const $ = id => document.getElementById(id);
const audio = $('audio');
const seek = $('seek');
const pane = $('lyrics-scroll');
const lookupDialog = $('find-dialog');
const taskbarDialog = $('taskbar-dialog');
const trackList = $('track-list');
const trackRows = $('visible-tracks');
const trackSpacer = $('track-spacer');
const rowHeight = 48;
const overscan = 6;
let library = [];
let results = [];
let selectedLibraryRow;
let visibleStart = -1, visibleEnd = -1;
let libraryFrame, searchFrame;
let queue = [];
let followed;
let external;
let sidebar = localStorage.getItem('verse-sidebar');
let queueSignature;
let song;
let lookup;
let jobState;
let pollBusy = false;
let toastTimer;
let scrollTimeout;
let readUntil = 0;
let frame;

function notify(message) {
  const toast = $('toast');
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 4500);
}

function run(work) {
  return async (...args) => {
    try { return await work(...args); }
    catch (error) { notify(error.message); }
  };
}

function text(tag, value, className) {
  const node = document.createElement(tag);
  node.textContent = value;
  if (className) node.className = className;
  return node;
}

function button(label, className, handler) {
  const node = text('button', label, className);
  node.type = 'button';
  node.addEventListener('click', run(handler));
  return node;
}

function filterLibrary() {
  const words = $('library-search').value.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
  results = library.filter(track => words.every(word => track.searchName.includes(word)));
  renderLibrary();
  if (jobState) renderBatch(jobState.batch);
}

function renderLibrary() {
  $('library-count').textContent = results.length.toLocaleString() + ' of ' + library.length.toLocaleString();
  trackSpacer.style.height = (results.length * rowHeight) + 'px';
  trackList.scrollTop = 0;
  visibleStart = visibleEnd = -1;
  renderVisibleTracks();
}

function renderVisibleTracks() {
  const start = Math.max(0, Math.floor(trackList.scrollTop / rowHeight) - overscan);
  const end = Math.min(results.length, Math.ceil((trackList.scrollTop + trackList.clientHeight) / rowHeight) + overscan);
  if (start === visibleStart && end === visibleEnd) return;
  visibleStart = start;
  visibleEnd = end;
  trackRows.style.transform = 'translateY(' + (start * rowHeight) + 'px)';
  const rows = document.createDocumentFragment();
  for (let index = start; index < end; index++) {
    const track = results[index];
    const row = text('button', '', 'track-row');
    row.type = 'button';
    row.id = 'library-track-' + track.key;
    row.dataset.trackIndex = index;
    const icon = text('span', '\u266a', 'track-icon');
    icon.setAttribute('aria-hidden', 'true');
    row.append(icon, text('span', track.name, 'track-name'));
    row.title = track.name;
    rows.append(row);
  }
  if (!results.length) rows.append(text('p', 'No songs found', 'hint'));
  trackRows.replaceChildren(rows);
  markCurrentTrack();
}

function scheduleLibraryRender() {
  if (libraryFrame != null) return;
  libraryFrame = requestAnimationFrame(() => {
    libraryFrame = undefined;
    renderVisibleTracks();
  });
}

function focusTrack(index) {
  if (index < 0 || index >= results.length) return;
  const top = index * rowHeight;
  if (top < trackList.scrollTop) trackList.scrollTop = top;
  else if (top + rowHeight > trackList.scrollTop + trackList.clientHeight) {
    trackList.scrollTop = top + rowHeight - trackList.clientHeight;
  }
  renderVisibleTracks();
  $('library-track-' + results[index].key)?.focus({preventScroll: true});
}

function markCurrentTrack() {
  selectedLibraryRow?.classList.remove('selected');
  selectedLibraryRow?.removeAttribute('aria-current');
  selectedLibraryRow = song ? $('library-track-' + song.item.key) : null;
  selectedLibraryRow?.classList.add('selected');
  selectedLibraryRow?.setAttribute('aria-current', 'true');
}

function useLibrary(state) {
  if (!state) return;
  library = state.tracks.map(track => ({...track, searchName: track.name.toLocaleLowerCase()}));
  const folder = $('library-root');
  folder.textContent = state.folder;
  folder.title = state.folder;
  filterLibrary();
}

function lyricsOf(data) {
  if (data.lines?.length) return data.lines;
  return parseLyrics(data.raw || data.lyrics || '').lines;
}

function installLyrics(current, state, label = state.source) {
  current.lines = lyricsOf(state);
  current.offset = Number(state.offset) || 0;
  current.scale = Math.min(4, Math.max(.25, Number(state.scale) || 1));
  current.source = label || '';
  current.rows = [];
  current.active = -2;
  $('word-timing-note').hidden = true;
  const duration = current.metadata?.duration > 0 ? lyricTime(current.metadata.duration, current.offset, current.scale) : 0;
  const nodes = current.lines.map((line, index) => {
    const time = line.time;
    const row = button('', 'lyric-line', () => {
      if (!current.external && Number.isFinite(time)) {
        readUntil = 0;
        jump(audioTime(time, current.offset, current.scale));
      }
    });
    if (Number.isFinite(time)) row.append(text('span', stamp(audioTime(time, current.offset, current.scale)), 'line-timestamp'));
    const segments = wordSegments(line, current.lines[index + 1]?.time, duration);
    const words = segments.map((word, index) => {
      const prefixed = index > 0 && !/\s$/.test(segments[index - 1].text) && !/^\s/.test(word.text);
      return text('span', (prefixed ? ' ' : '') + word.text, 'word');
    });
    row.append(...(words.length ? words : [text('span', line.text, 'line-text')]));
    row.classList.toggle('word-progress', words.length > 0);
    if (current.external || !Number.isFinite(time)) row.disabled = true;
    current.rows.push({row, words, segments, estimated: !!segments.length && !line.words?.length});
    return row;
  });
  $('lyrics-lines').replaceChildren(...nodes);
  $('empty-state').hidden = !!nodes.length;
  $('export-button').disabled = current.external || !nodes.length;
  $('source-badge').hidden = !current.source;
  $('source-badge').textContent = current.source;
  updateFrame(true);
}

function upcomingQueue() {
  const position = queue.findIndex(track => track.key === song?.item.key);
  return position < 0 ? [] : queue.slice(position + 1);
}

function showArtwork(artwork, title = '') {
  $('art-letter').textContent = [...title][0]?.toLocaleLowerCase() || 'v';
  for (const id of ['cover-art', 'mini-cover-art']) {
    const image = $(id);
    image.hidden = !artwork;
    image.parentElement.classList.toggle('has-cover', !!artwork);
    if (artwork) image.src = artwork;
    else image.removeAttribute('src');
  }
}

async function openTrack(item) {
  externalButtons(false);
  closeLyricsSearch();
  audio.pause();
  audio.removeAttribute('src');
  audio.load();
  const current = {item, metadata: null, revision: 0, lines: [], rows: [], active: -2};
  song = current;
  readUntil = 0;
  pane.scrollTop = 0;
  $('track-title').textContent = item.name;
  showArtwork('', item.name);
  $('track-artist').textContent = '';
  $('import-button').disabled = true;
  $('find-lyrics').disabled = true;
  $('empty-title').textContent = 'Opening song…';
  showPreparation(current, '');
  installLyrics(current, {});
  markCurrentTrack();
  renderQueue();

  const {metadata, saved, prepared, audioUrl} = await call('track', {key: item.key});
  if (song !== current) return;
  current.metadata = metadata;
  current.audioUrl = audioUrl;
  for (const id of ['track-title', 'mini-title']) $(id).textContent = metadata.title;
  for (const id of ['track-artist', 'mini-artist']) $(id).textContent = metadata.artist;
  showArtwork(metadata.artwork, metadata.title);
  $('import-button').disabled = false;
  $('find-lyrics').disabled = false;
  showPreparation(current, '');

  const hasAdjustment = (Number(saved.offset) || 0) !== 0 || (Number(saved.scale) || 1) !== 1 ||
    saved.lines?.some(line => line.timing === 'manual');
  const generated = saved.timingKind === 'model' || saved.selection?.kind === 'reference_aligned';
  const stale = generated && !hasAdjustment &&
    ((saved.pipelineVersion || 0) < pipelineVersion || saved.fingerprint !== metadata.fingerprint);
  const localLyrics = metadata.sidecar || metadata.embedded;
  if (metadata.embedded) {
    const rich = [saved, prepared].find(state => state?.embeddedDigest && state.embeddedDigest === metadata.embeddedDigest);
    installLyrics(current, rich || {raw: metadata.embedded}, 'Embedded lyrics');
  } else if (!stale && lyricsOf(saved).length) installLyrics(current, saved, saved.source || 'Saved lyrics');
  else if (localLyrics) installLyrics(current, {raw: localLyrics}, 'Local lyric file');
  else if (prepared) installLyrics(current, prepared);
  else if (stale) installLyrics(current, saved, 'Previous lyric draft');
  $('empty-title').textContent = 'Looking for lyrics…';

  audio.src = audioUrl;
  playRecording(current);
  if (!metadata.embedded && (!current.lines.length || (stale && !prepared && !localLyrics))) {
    await prepare(current, false);
  }
  if (song === current) {
    jobState = await call('prefetch', {key: item.key, keys: upcomingQueue().map(track => track.key)});
    renderQueue();
  }
}

function playRecording(current) {
  return audio.play().catch(async error => {
    if (song !== current || error.name === 'AbortError') return;
    if (current.decoding) return;
    if (!current.decoded && (error.name === 'NotSupportedError' || [3, 4].includes(audio.error?.code))) {
      current.decoding = true;
      const elapsed = audio.currentTime;
      try {
        const {audioUrl} = await call('decodePlayback', {key: current.item.key});
        if (song !== current) return;
        current.decoded = true;
        current.audioUrl = audioUrl;
        audio.src = audioUrl;
        audio.currentTime = elapsed;
        await audio.play();
        return;
      } catch (decodeError) {
        if (song === current && decodeError.name !== 'AbortError') notify(decodeError.message);
        return;
      } finally {
        current.decoding = false;
      }
    }
    notify(error.message);
  });
}

function updateRecordingVersion(current, result) {
  if (!result?.fingerprint || result.fingerprint === current.metadata.fingerprint) return;
  current.metadata.fingerprint = result.fingerprint;
  if (song !== current || !audio.src) return;
  const elapsed = audio.currentTime;
  const playing = !audio.paused;
  audio.src = current.audioUrl + '?revision=' + encodeURIComponent(result.fingerprint);
  audio.currentTime = elapsed;
  if (playing) playRecording(current);
}

function showPreparation(current, message) {
  if (current !== song) return;
  $('asr-status').textContent = message;
  $('asr-cancel').hidden = !current.job;
  const generate = $('asr-generate');
  generate.hidden = !current.metadata;
  generate.disabled = !!(current.preparing || current.job);
  generate.textContent = current.failed ? 'Retry lyrics' : 'Prepare lyrics';
  if (!current.lines.length && current.failed) $('empty-title').textContent = 'Lyrics unavailable';
}

async function prepare(current, force) {
  if (!current?.metadata || current.preparing || current.job) return;
  const revision = current.revision;
  current.preparing = true;
  current.failed = false;
  showPreparation(current, 'Preparing lyrics…');
  try {
    const job = await call('prepare', {key: current.item.key, reason: force ? 'manual' : 'current', force});
    if (song !== current || current.revision !== revision) return;
    current.preparing = false;
    if (job.result) await accept(current, job.result);
    else {
      current.job = job.key;
      showPreparation(current, 'Preparing lyrics…');
    }
  } catch (error) {
    if (song === current && current.revision === revision) {
      current.preparing = false;
      current.failed = true;
      showPreparation(current, error.message);
    }
  }
}

async function saveLyrics(current, selection, timingKind, embed = false) {
  const result = await call('save', {key: current.item.key, embed, state: {
    lines: current.lines, offset: current.offset, scale: current.scale,
    source: current.source, selection, timingKind, pipelineVersion,
    fingerprint: current.metadata.fingerprint, embeddedDigest: current.metadata.embeddedDigest,
    updatedAt: new Date().toISOString(),
  }});
  updateRecordingVersion(current, result);
  if (result?.embeddedDigest) current.metadata.embeddedDigest = result.embeddedDigest;
  if (result?.embedError && song === current) {
    showPreparation(current, 'Lyrics cached. File update failed.');
    notify(result.embedError);
  }
}

async function accept(current, result) {
  current.job = undefined;
  installLyrics(current, result);
  current.failed = !current.lines.length;
  updateRecordingVersion(current, result);
  current.metadata.embeddedDigest = result.embeddedDigest;
  showPreparation(current, result.embedError ? 'Lyrics cached. File update failed.'
    : current.lines.length ? '' : 'No usable lyrics found');
  if (result.embedError) notify(result.embedError);
  await saveLyrics(current, {
    kind: result.kind, model: result.model, alignment: result.alignment, pipelineVersion: result.pipelineVersion,
  }, result.timingKind || 'line');
}

async function useLyrics(current, raw, source, selection = null) {
  const {lines} = parseLyrics(raw);
  if (!lines.length) throw new Error('No usable lyrics found.');
  current.revision++;
  current.preparing = false;
  current.job = undefined;
  current.failed = false;
  installLyrics(current, {lines, source});
  showPreparation(current, '');
  current.metadata.embeddedDigest = undefined;
  await saveLyrics(current, selection, 'line', true);
}

function closeLyricsSearch() {
  lookup = undefined;
  if (lookupDialog.open) lookupDialog.close();
}

function openLyricsSearch() {
  if (!song?.metadata) return;
  const {title, artist} = song.metadata;
  const duration = Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : song.metadata.duration;
  lookup = {song, duration, pending: false};
  $('query-title').value = title;
  $('query-artist').value = artist;
  $('search-recording').textContent = duration > 0 ? `Current recording · ${clock(duration)}` : '';
  lookupDialog.showModal();
  return searchLyrics();
}

async function searchLyrics() {
  const current = lookup;
  const title = $('query-title').value.trim();
  const artist = $('query-artist').value.trim();
  const status = $('search-status');
  if (!current || current.pending) return;
  if (!title) {
    status.textContent = 'Enter a song title.';
    return;
  }
  current.pending = true;
  const search = $('search-lyrics');
  search.disabled = true;
  $('candidates').replaceChildren();
  status.textContent = 'Searching LRCLIB…';
  try {
    const rows = await call('search', {title, artist, duration: current.duration});
    if (lookup !== current || !lookupDialog.open || song !== current.song) return;
    const available = rows.filter(row => row.syncedLyrics?.trim() || row.plainLyrics?.trim()).map(row => ({
      ...row, trackName: row.trackName || title, artistName: row.artistName || '', duration: Number(row.duration) || 0,
    }));
    const versions = rankCandidates(available, {title, artist, duration: current.duration});
    $('candidates').replaceChildren(...versions.map(row => lyricCandidate(row, current)));
    status.textContent = versions.length ? `${versions.length} ${versions.length === 1 ? 'version' : 'versions'} found`
      : 'No lyrics match this recording’s length.';
  } catch (error) {
    if (lookup === current) status.textContent = `Search failed. ${error.message}`;
  } finally {
    current.pending = false;
    if (lookup === current) search.disabled = false;
  }
}

function lyricCandidate(row, current) {
  const timed = row.syncedLyrics?.trim();
  const item = text('div', '', 'candidate');
  const info = text('div', '');
  const details = [row.artistName, row.albumName, row.duration > 0 ? clock(row.duration) : ''].filter(Boolean);
  info.append(text('h3', row.trackName), text('p', details.join(' · ')),
    text('span', timed ? 'Timed lyrics' : 'Lyrics text', 'badge'));
  const use = button('Use lyrics', 'secondary-button', async () => {
    if (lookup !== current || song !== current.song) return;
    closeLyricsSearch();
    await useLyrics(current.song, timed || row.plainLyrics, 'Selected lyrics · LRCLIB', {id: row.id});
  });
  item.append(info, use);
  return item;
}

function upcoming() {
  const position = queue.findIndex(track => track.key === song?.item.key);
  return position < 0 ? [] : queue.slice(position + 1, position + 25);
}

function showSidebar(which) {
  const showQueue = which === 'queue';
  $('queue-pane').hidden = !showQueue;
  $('library-pane').hidden = showQueue;
  $('show-queue').setAttribute('aria-pressed', showQueue);
  $('show-library').setAttribute('aria-pressed', !showQueue);
  if (!showQueue) scheduleLibraryRender();
}

function renderQueue() {
  const source = followed?.playback;
  showSidebar(sidebar || (source ? 'queue' : 'library'));
  if (source) {
    const label = source.autoDj ? 'MusicBee · Auto-DJ' : source.player;
    const hint = source.upNext.length ? label : label + (source.file ? ' · No upcoming local tracks' : ' · Queue unavailable');
    $('queue-hint').textContent = hint;
    const signature = JSON.stringify([label, source.upNext]);
    if (signature === queueSignature) return;
    queueSignature = signature;
    $('up-next').replaceChildren(...source.upNext.map(path => {
      const name = path.split(/[\\/]/).at(-1).replace(/\.[^.]+$/, '');
      const row = text('div', '', 'queue-song');
      row.title = name;
      row.append(text('span', name));
      return row;
    }));
    return;
  }
  const tracks = upcoming();
  $('queue-hint').textContent = tracks.length ? '' : song ? 'No more songs queued.' : 'Play a song to start a queue.';
  const entries = tracks.map(track => {
    const status = jobState?.jobs.filter(job => job.trackId === track.key).at(-1);
    const sub = status?.status === 'complete' ? 'Lyrics ready'
      : status?.status === 'running' ? 'Preparing lyrics'
      : status?.status === 'queued' ? 'Queued' : '';
    return {track, sub};
  });
  const signature = JSON.stringify(entries.map(({track, sub}) => [track.key, sub]));
  if (signature === queueSignature) return;
  queueSignature = signature;
  $('up-next').replaceChildren(...entries.map(({track, sub}) => {
    const row = button('', 'queue-song', () => openTrack(track));
    row.append(text('span', track.name));
    if (sub) row.append(text('small', sub));
    return row;
  }));
}

function externalButtons(disabled) {
  for (const id of ['play', 'previous', 'next', 'back', 'forward', 'seek']) $(id).disabled = disabled;
}

function renderExternal(snapshot) {
  const playback = snapshot?.playback;
  $('external-track').hidden = !playback;
  $('follow-player').hidden = !song || !playback;
  if (!playback) {
    externalButtons(false);
    if (!song && external) {
      external = undefined;
      $('track-title').textContent = 'Choose a song';
      $('track-artist').textContent = '';
      $('mini-title').textContent = 'Nothing playing yet';
      $('mini-artist').textContent = '';
      $('empty-title').textContent = 'Lyrics appear when you play a song';
      installLyrics({}, {});
    }
    return;
  }
  $('external-title').textContent = playback.title;
  $('external-artist').textContent = playback.artist;
  const duration = playback.durationMs / 1000;
  $('external-time').textContent = [playback.state, clock(snapshot.seconds), duration > 0 ? clock(duration) : ''].filter(Boolean).join(' · ');
  if (song) return;
  externalButtons(true);
  $('track-title').textContent = playback.title;
  $('track-artist').textContent = playback.artist;
  $('mini-title').textContent = playback.title;
  $('mini-artist').textContent = playback.player + ' · ' + playback.state;
  $('play').textContent = playback.playing ? '\u275a\u275a' : '\u25b6';
  $('empty-title').textContent = snapshot.lyricStatus || 'Lyrics appear when playback reaches the first line';
  if (external?.revision !== snapshot.revision) {
    external = {external: true, revision: snapshot.revision, metadata: {duration}};
    showArtwork('', playback.title);
    installLyrics(external, snapshot);
  }
  updateFrame();
}

function renderBatch(batch) {
  const active = batch.status === 'running' || batch.status === 'stopping';
  const candidates = $('batch-size').value === '0' ? library : results;
  $('batch-start').disabled = active || !candidates.length;
  $('batch-pause').hidden = !active;
  $('batch-pause').disabled = batch.status === 'stopping';
  $('batch-size').disabled = $('batch-duration').disabled = active;
  $('batch-status').textContent = batch.status === 'idle' ? '' :
    [batch.status[0].toUpperCase() + batch.status.slice(1), batch.prepared + ' ready',
      batch.skipped + ' skipped', batch.failed + ' failed', batch.total + ' total'].join(' · ');
  if (batch.lastError) $('batch-status').append(document.createElement('br'), batch.lastError);
  if (active && batch.endsAt) {
    const minutes = Math.max(1, Math.ceil((batch.endsAt * 1000 - Date.now()) / 60000));
    $('batch-status').append(document.createElement('br'), minutes + ' min left');
  } else if (batch.stopReason) {
    $('batch-status').append(document.createElement('br'), batch.stopReason);
  }
}

async function poll() {
  if (pollBusy) return;
  pollBusy = true;
  try {
    const snapshot = await call('musicBee', {revision: followed?.revision ?? -1});
    followed = snapshot?.lines || !snapshot ? snapshot : {...snapshot, lines: followed?.lines || []};
    if (followed) followed.sampled = performance.now();
    $('taskbar-connection').textContent = followed?.status || 'Waiting for playback';
    renderExternal(followed);
    jobState = await call('status');
    renderQueue();
    renderBatch(jobState.batch);
    const current = song;
    if (!current?.job) return;
    const key = current.job;
    const job = jobState.jobs.find(job => job.key === key);
    if (job?.status === 'complete') {
      const result = await call('result', {key: current.item.key});
      if (song === current && current.job === key && result) await accept(current, result);
    } else if (job?.status === 'failed' || job?.status === 'cancelled') {
      current.job = undefined;
      current.failed = true;
      showPreparation(current, job.phase);
    } else if (job) {
      showPreparation(current, job.phase + ' · ' + Math.round(job.elapsed || 0) + 's');
    }
  } catch (error) {
    if (song?.job) showPreparation(song, error.message);
  } finally {
    pollBusy = false;
  }
}

function jump(seconds) {
  if (song?.metadata && Number.isFinite(seconds)) {
    audio.currentTime = Math.max(0, Math.min(seconds, audio.duration || song.metadata.duration));
    updateFrame(true);
  }
}

function move(step) {
  if (queue.length < 2) return;
  const index = queue.findIndex(track => track.key === song?.item.key);
  const next = queue[index + step];
  if (next) return openTrack(next);
}

function updateFrame(force = false) {
  const current = song || external;
  const playback = !song && followed?.playback;
  const duration = playback ? playback.durationMs / 1000 : (Number.isFinite(audio.duration) ? audio.duration : current?.metadata?.duration || 0);
  const position = playback
    ? Math.min(duration || Infinity, followed.seconds + (playback.playing ? (performance.now() - followed.sampled) * playback.rate / 1000 : 0))
    : audio.currentTime;
  seek.max = duration || 100;
  if (!seek.matches(':active')) seek.value = position;
  $('elapsed').textContent = clock(position);
  $('duration').textContent = clock(duration);
  if (!current || !playback && !audio.src) return;
  const time = lyricTime(position, current.offset, current.scale);
  const index = activeLine(current.lines, time);
  if (force || index !== current.active) {
    current.active = index;
    $('word-timing-note').hidden = !current.rows[index]?.estimated;
    current.rows.forEach(({row}, i) => {
      row.classList.toggle('active', index === i);
      row.classList.toggle('past', index !== i && current.lines[i].time != null && current.lines[i].time < time);
      if (index === i) row.setAttribute('aria-current', 'true');
      else row.removeAttribute('aria-current');
    });
    if (index >= 0 && Date.now() > readUntil) scrollToLine();
  }
  const row = current.rows[index];
  if (row) row.segments.forEach((word, i) => {
    const progress = Math.max(0, Math.min(1, (time - word.time) / Math.max(.04, word.end - word.time)));
    row.words[i].style.setProperty('--fill', (progress * 100) + '%');
  });
}

function scrollToLine() {
  const current = song || external;
  const line = current?.rows[current.active]?.row;
  if (line) pane.scrollTo({top: line.offsetTop + line.offsetHeight / 2 - pane.clientHeight * .42, behavior: 'smooth'});
}

function playState() {
  $('play').textContent = audio.paused ? '\u25b6' : '\u275a\u275a';
  $('play').setAttribute('aria-label', audio.paused ? 'Play' : 'Pause');
  cancelAnimationFrame(frame);
  function animate() {
    updateFrame();
    if (!audio.paused) frame = requestAnimationFrame(animate);
  }
  animate();
}

async function toggle() {
  if (!audio.src) return;
  if (audio.paused) await playRecording(song);
  else audio.pause();
}

$('library-search').addEventListener('input', () => {
  if (searchFrame != null) return;
  searchFrame = requestAnimationFrame(() => {
    searchFrame = undefined;
    filterLibrary();
  });
});
trackList.addEventListener('scroll', scheduleLibraryRender, {passive: true});
new ResizeObserver(scheduleLibraryRender).observe(trackList);
$('library-search').addEventListener('keydown', event => {
  if (event.key === 'ArrowDown' && results.length) {
    event.preventDefault();
    focusTrack(0);
  }
});
trackList.addEventListener('keydown', event => {
  const row = event.target.closest('button.track-row');
  if (!row || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  const index = Number(row.dataset.trackIndex);
  focusTrack(event.key === 'Home' ? 0 : event.key === 'End' ? results.length - 1 : index + (event.key === 'ArrowDown' ? 1 : -1));
});
trackList.addEventListener('click', run(event => {
  const row = event.target.closest('button.track-row');
  if (!row) return;
  queue = results.slice();
  return openTrack(queue[Number(row.dataset.trackIndex)]);
}));
$('rescan').onclick = run(async () => useLibrary(await call('library')));
$('follow-player').onclick = () => {
  closeLyricsSearch();
  audio.pause();
  audio.removeAttribute('src');
  audio.load();
  song = external = undefined;
  markCurrentTrack();
  for (const id of ['find-lyrics', 'import-button']) $(id).disabled = true;
  $('asr-generate').hidden = $('asr-cancel').hidden = true;
  $('asr-status').textContent = '';
  renderExternal(followed);
};
$('choose-folder').onclick = run(async () => {
  const state = await call('chooseFolder');
  if (!state) return;
  closeLyricsSearch();
  audio.pause();
  audio.removeAttribute('src');
  audio.load();
  song = undefined;
  external = undefined;
  queue = [];
  installLyrics({lines: [], rows: []}, {});
  $('track-title').textContent = 'Choose a song';
  showArtwork('');
  $('track-artist').textContent = '';
  $('mini-title').textContent = 'Nothing playing yet';
  $('mini-artist').textContent = '';
  $('import-button').disabled = true;
  $('find-lyrics').disabled = true;
  $('asr-cancel').hidden = $('asr-generate').hidden = true;
  $('asr-status').textContent = '';
  $('empty-title').textContent = 'Lyrics appear when you play a song';
  useLibrary(state);
  renderQueue();
});
$('open-file').onclick = run(async () => {
  const item = await call('openFile');
  if (!item) return;
  queue = [item];
  await openTrack(item);
});
$('find-lyrics').onclick = run(openLyricsSearch);
$('find-close').onclick = closeLyricsSearch;
$('taskbar-setup').onclick = run(async () => {
  $('close-to-tray').checked = await call('closeToTray');
  taskbarDialog.showModal();
});
$('close-to-tray').onchange = run(async event => {
  event.target.checked = await call('closeToTray', {enabled: event.target.checked});
});
for (const which of ['queue', 'library']) {
  $('show-' + which).onclick = () => {
    localStorage.setItem('verse-sidebar', which);
    sidebar = which;
    showSidebar(which);
  };
}
$('taskbar-close').onclick = () => taskbarDialog.close();
for (const [id, action] of [['taskbar-copy', 'copyTaskbarSource'], ['taskbar-files', 'taskbarBundle'],
                            ['taskbar-windhawk', 'openWindhawk'], ['taskbar-start', 'taskbarMode']]) {
  $(id).onclick = async () => {
    $('taskbar-status').textContent = '';
    try {
      await call(action);
      if (action === 'copyTaskbarSource') $('taskbar-status').textContent = 'Windhawk source copied.';
    } catch (error) {
      $('taskbar-status').textContent = error.message;
    }
  };
}
lookupDialog.addEventListener('close', () => {
  if (!lookupDialog.open) lookup = undefined;
});
$('find-form').addEventListener('submit', event => {
  event.preventDefault();
  void searchLyrics();
});
$('import-button').onclick = run(async () => {
  const current = song;
  const raw = await call('import');
  if (song !== current || raw == null) return;
  await useLyrics(current, raw, 'Imported lyrics');
});
$('export-button').onclick = run(async () => {
  const {lines, offset, scale, metadata} = song;
  const saved = await call('export', {title: metadata.title, lyrics: exportLrc(lines, offset, scale, metadata.title, metadata.artist)});
  if (saved) notify('Lyrics exported');
});
$('asr-generate').onclick = run(() => prepare(song, true));
$('asr-cancel').onclick = run(async () => {
  const current = song;
  current.revision++;
  current.job = undefined;
  current.failed = true;
  await call('cancel', {key: current.item.key});
  showPreparation(current, 'Preparation cancelled');
});
$('batch-start').onclick = run(async () => renderBatch(await call('batch', {
  keys: $('batch-size').value === '0' ? null : results.slice(0, 10).map(track => track.key),
  minutes: Number($('batch-duration').value),
})));
$('batch-size').onchange = () => { if (jobState) renderBatch(jobState.batch); };
$('batch-pause').onclick = run(async () => renderBatch(await call('batch', {pause: true})));
$('play').onclick = run(toggle);
$('previous').onclick = run(() => move(-1));
$('next').onclick = run(() => move(1));
$('back').onclick = () => jump(audio.currentTime - 5);
$('forward').onclick = () => jump(audio.currentTime + 5);
$('volume').oninput = () => {audio.volume = Number($('volume').value);};
$('mute').onclick = () => {
  audio.muted = !audio.muted;
  $('mute').setAttribute('aria-label', audio.muted ? 'Unmute' : 'Mute');
};
audio.volume = .7;
seek.oninput = () => jump(Number(seek.value));
audio.onplay = audio.onpause = playState;
audio.onloadedmetadata = audio.ontimeupdate = () => updateFrame();
audio.onended = run(() => move(1));
pane.addEventListener('wheel', () => {
  readUntil = Date.now() + 4000;
  clearTimeout(scrollTimeout);
  scrollTimeout = setTimeout(scrollToLine, 4100);
}, {passive: true});
document.addEventListener('keydown', run(async event => {
  if (event.target.closest('input, select, textarea, button') || event.altKey || event.ctrlKey) return;
  if (event.code === 'Space') {
    event.preventDefault();
    await toggle();
  } else if (event.code === 'KeyJ') jump(audio.currentTime - 5);
  else if (event.code === 'KeyL') jump(audio.currentTime + 5);
}));

ipc.postMessage({ready: true});
await run(async () => useLibrary(await call('library')))();
await poll();
setInterval(poll, 1000);
setInterval(() => { if (!song && external) updateFrame(); }, 100);
