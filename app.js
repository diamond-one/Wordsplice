'use strict';
const $ = id => document.getElementById(id);
const tokens = text => text.toLowerCase().replaceAll('’', "'").match(/[a-z0-9]+(?:'[a-z0-9]+)*/g) || [];
function phrasePlan(text) {
  const parts = text.trim().split(/\s+[-–—]\s+/);
  let offset = 0;
  return {explicit: parts.length > 1, groups: parts.map(part => {
    const group = {words: tokens(part), offset}; offset += group.words.length; return group;
  })};
}
let session = null, clips = [], running = false, cancelled = false, capture = null;
let player = null, playerPromise = null, playerError = null, activeStop = null;
let modelReady = false, previewAudio = null, selectedClip = -1;
let previousSettings = '', requestController = null;
let undoStack = [], restoring = true;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
function checkStop() { if (cancelled) throw new Error('Stopped. Your completed clips are kept. Click Resume to continue.'); }
function status(message, detail = '') { $('status').textContent = message; if (detail) $('status-detail').textContent = detail; }
function showError(message) {
  $('error').textContent = message; $('error').hidden = !message;
  if (message) $('error').scrollIntoView?.({block: 'nearest', behavior: 'smooth'});
}
function invalidateMix() {
  $('download').removeAttribute('href'); $('download').classList.add('disabled'); $('download').setAttribute('aria-disabled', 'true');
  $('mix-audio').pause(); $('mix-audio').hidden = true;
  if (clips.length && isComplete()) {
    $('mix-title').textContent = 'Changes ready to apply';
    $('mix-detail').textContent = 'Update the mix to hear and save your edits.';
    $('remix').hidden = false;
  }
}
function setBusy(value) {
  running = value;
  for (const id of ['sentence', 'accent', 'voice', 'topic', 'phrase-size', 'mode', 'texture', 'gap']) $(id).disabled = value;
  if (phrasePlan($('sentence').value).explicit) $('phrase-size').disabled = true;
  $('create').hidden = value; $('create').disabled = !modelReady || restoring; $('stop').hidden = !value;
  $('activity-dot').classList.toggle('active', value);
  $('remix').disabled = value;
  if ($('clip-tools')) $('clip-tools').disabled = value;
  if ($('undo')) $('undo').disabled = value || !undoStack.length;
}
const copyClips = list => JSON.parse(JSON.stringify(list));
const sourceKey = source => source ? `${source.video}:${source.start}:${source.end}` : '';
const isComplete = () => session && clips.flatMap(c => tokens(c.phrase)).join(' ') === session.words.join(' ');
function projectSettings() {
  return Object.fromEntries(['accent', 'voice', 'topic', 'phrase-size', 'mode', 'texture', 'gap'].map(id => [id, $(id).value]));
}
function updatePhraseHints() {
  const plan = phrasePlan($('sentence').value);
  $('phrase-size').disabled = running || plan.explicit;
  $('phrase-preview').replaceChildren();
  if (plan.explicit) {
    $('phrase-hint').textContent = `${plan.groups.length} phrases · Your dash breaks take priority over automatic grouping.`;
    plan.groups.forEach(group => {
      const chip = document.createElement('span'); chip.className = 'phrase-chip'; chip.textContent = group.words.join(' ') || 'Add words here'; $('phrase-preview').append(chip);
    });
  } else $('phrase-hint').textContent = 'Set your own phrase breaks with spaced dashes: I want - to live - on a boat';
}
function syncPhraseText() {
  const completed = clips.reduce((sum, c) => sum + tokens(c.phrase).length, 0);
  const groups = clips.map(c => c.phrase);
  if (completed < session.words.length) groups.push(session.words.slice(completed).join(' '));
  $('sentence').value = groups.join(' - ');
  session.sentence = $('sentence').value;
  previousSettings = $('sentence').value + '|' + $('accent').value + '|' + $('phrase-size').value + '|' + $('voice').value + '|' + $('topic').value.trim();
  updatePhraseHints();
}
async function openCapture() {
  if (!navigator.mediaDevices?.getDisplayMedia || !window.MediaRecorder) throw new Error('Open this local address in Chrome or Edge to capture tab audio.');
  // This call must happen directly in the button handler, before any network requests.
  capture = await navigator.mediaDevices.getDisplayMedia({video: {displaySurface: 'browser'}, audio: {suppressLocalAudioPlayback: false, echoCancellation: false, noiseSuppression: false}, preferCurrentTab: true, selfBrowserSurface: 'include', systemAudio: 'exclude', surfaceSwitching: 'exclude'});
  checkStop();
  if (!capture.getAudioTracks().length) throw new Error('No tab audio was shared. Choose this Wordsplice tab and enable Share tab audio, then retry.');
  const surface = capture.getVideoTracks()[0]?.getSettings().displaySurface;
  if (surface && surface !== 'browser') throw new Error('Choose a browser tab rather than a window or screen, so only the source audio is captured.');
  capture.getTracks().forEach(track => track.addEventListener('ended', () => {if (running) stop();}, {once: true}));
  status('Connecting the source player…');
  await ensurePlayer(); checkStop();
}
async function capturePhrase(phrase, excluded = [], preferredNewVoices = []) {
  checkStop();
  $('current-phrase').textContent = '“' + phrase + '”'; $('source-state').textContent = 'SEARCHING YOUGLISH';
  status(`Finding “${phrase}”…`, 'Looking for an exact phrase. Your existing cuts stay in place.');
  const found = await api('/api/search', {query: phrase, accent: $('accent').value, voice: $('voice').value, topic: $('topic').value.trim()}); checkStop();
  const used = new Set([...clips.map(c => c.source?.video), ...preferredNewVoices]);
  const candidates = found.results.filter(s => !excluded.includes(sourceKey(s)))
    .sort((a,b) => Number(used.has(a.video)) - Number(used.has(b.video))).slice(0, 3);
  let lastError = candidates.length ? '' : `No unused exact-match ${$('voice').value ? $('voice').value + ' voice ' : ''}source was found.${$('topic').value.trim() ? ' Context: ' + $('topic').value.trim() + '. Try a broader topic or clear the filter.' : ''}`;
  for (let i = 0; i < candidates.length; i++) {
    checkStop(); const source = candidates[i];
    $('caption').textContent = source.caption; $('source-link').href = source.youglish; $('source-link').hidden = false;
    $('source-state').textContent = '● RECORDING';
    status(`Capturing “${phrase}” · take ${i + 1}`, 'Keep this tab open. Source audio is being captured in real time.');
    try {
      const blob = await recordCandidate(source); checkStop();
      $('source-state').textContent = 'TRIMMING'; status(`Locating the words “${phrase}”…`, 'Matching the exact words and trimming this take.');
      const result = await api(`/api/clip?session=${session.id}&phrase=${encodeURIComponent(phrase)}`, blob, true); checkStop();
      return {...result, source, pause: Number($('gap').value), takeHistory: [...new Set([...excluded, sourceKey(source)])]};
    } catch (error) {
      checkStop(); lastError = error.message;
      if (/silent|No tab audio|sharing ended|select.*tab/i.test(lastError)) throw error;
      status('Trying another voice…', lastError);
    }
  }
  return {unavailable: true, reason: lastError};
}
async function api(path, body, raw = false) {
  requestController = new AbortController();
  const response = await fetch(path, { method: 'POST', headers: {'X-Wordsplice': '1', ...(raw ? {} : {'Content-Type': 'application/json'})},
    body: raw ? body : JSON.stringify(body), signal: requestController.signal });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'The local server could not finish the request.');
  return data;
}
function ensurePlayer() {
  if (playerPromise) return playerPromise;
  playerPromise = new Promise((resolve, reject) => {
    const timeout = setTimeout(() => { playerPromise = null; reject(new Error('The YouTube player could not load. Check your connection or content blocker.')); }, 20000);
    const create = () => {
      player = new YT.Player('player', {width: '100%', height: '100%', playerVars: {origin: location.origin, playsinline: 1, controls: 1, rel: 0},
        events: {onReady: () => {clearTimeout(timeout); resolve(player);}, onError: event => { playerError = event.data; }} });
    };
    if (window.YT?.Player) create();
    else {
      window.onYouTubeIframeAPIReady = create;
      const script = document.createElement('script'); script.src = 'https://www.youtube.com/iframe_api';
      script.onerror = () => {clearTimeout(timeout); playerPromise = null; reject(new Error('YouTube could not be reached.'));};
      document.head.append(script);
    }
  });
  return playerPromise;
}
async function recordCandidate(source) {
  checkStop();
  playerError = null;
  $('player-placeholder').hidden = true;
  const audioTrack = capture?.getAudioTracks()[0];
  if (!audioTrack || audioTrack.readyState !== 'live') throw new Error('Tab audio sharing ended. Resume and select this Wordsplice tab again.');
  const mimeType = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'].find(t => MediaRecorder.isTypeSupported(t));
  if (!mimeType) throw new Error('Audio recording is not supported in this browser. Use Chrome or Edge.');
  const recorder = new MediaRecorder(new MediaStream([audioTrack]), {mimeType});
  const chunks = [];
  let recorderError = null;
  const stopped = new Promise(resolve => {recorder.onstop = resolve;});
  recorder.ondataavailable = e => {if (e.data.size) chunks.push(e.data);};
  recorder.onerror = e => {recorderError = e.error || new Error('The audio recorder failed.');};
  activeStop = () => {if (recorder.state !== 'inactive') recorder.stop();};
  recorder.start(200);
  player.unMute(); player.setVolume(100); player.setPlaybackRate(1);
  player.loadVideoById({videoId: source.video, startSeconds: source.start, endSeconds: source.end});
  const deadline = Date.now() + 55000;
  let began = false;
  try {
    while (Date.now() < deadline) {
      await sleep(100); checkStop();
      if (recorderError) throw recorderError;
      if (playerError) throw new Error(`This source cannot play here (YouTube ${playerError}). Trying another.`);
      const state = player.getPlayerState();
      const currentVideo = player.getVideoData()?.video_id;
      if (state === 1 && currentVideo === source.video) began = true;
      if (began && (player.getCurrentTime() >= source.end - .06 || state === 0)) break;
      if (!began && Date.now() > deadline - 40000) status('Waiting for playback…', 'If playback is paused, click Play in the video. Unavailable sources will be skipped.');
    }
    if (!began || Date.now() >= deadline) throw new Error('This source did not finish playing. Trying another clip.');
  } finally {
    player.pauseVideo();
    if (recorder.state !== 'inactive') recorder.stop();
    await stopped; activeStop = null;
  }
  return new Blob(chunks, {type: mimeType});
}
function updateTimeline() {
  $('timeline').replaceChildren();
  clips.forEach((clip, index) => {
    const button = document.createElement('button'); button.className = 'clip' + (index === selectedClip ? ' selected' : '');
    const head = document.createElement('span'); head.className = 'clip-head';
    const number = document.createElement('span'); number.textContent = String(index + 1).padStart(2, '0');
    const duration = document.createElement('span'); duration.textContent = (clip.end - clip.start).toFixed(2) + 's';
    head.append(number, duration);
    const label = document.createElement('b'); label.textContent = clip.phrase;
    const wave = document.createElement('span'); wave.className = 'wave'; wave.setAttribute('aria-hidden', 'true');
    (clip.waveform || []).forEach(value => {const bar = document.createElement('i'); bar.style.height = Math.max(2, value * 23) + 'px'; wave.append(bar);});
    button.append(head, label, wave); button.setAttribute('aria-label', `Edit clip ${index + 1}: ${clip.phrase}`);
    button.onclick = () => {if (!running) editClip(index);};
    $('timeline').append(button);
  });
  const covered = clips.reduce((sum, clip) => sum + tokens(clip.phrase).length, 0);
  $('clip-count').textContent = clips.length + (clips.length === 1 ? ' clip' : ' clips');
  $('progress').style.width = (session ? covered / session.words.length * 100 : 0) + '%';
  if (!isComplete()) {
    $('mix-title').textContent = `${covered} of ${session?.words.length || 0} words captured`;
    $('mix-detail').textContent = 'Select a phrase to replace, split, join or trim it.';
  }
}
function editClip(index) {
  if (running || !clips[index]) return;
  previewAudio?.pause();
  selectedClip = index;
  Array.from($('timeline').children || []).forEach((button, i) => {
    button.classList.toggle('selected', i === index);
    button.setAttribute('aria-pressed', String(i === index));
  });
  const clip = clips[index]; $('trim-panel').hidden = false;
  const tools = document.createElement('fieldset'); tools.id = 'clip-tools'; tools.className = 'clip-tools';
  const heading = document.createElement('div'); heading.className = 'editor-heading';
  const title = document.createElement('b'); title.textContent = `Cut ${index + 1} · ${clip.phrase}`;
  const close = document.createElement('button'); close.className = 'secondary'; close.textContent = 'Close editor';
  close.onclick = () => {
    previewAudio?.pause(); selectedClip = -1; $('trim-panel').hidden = true;
    Array.from($('timeline').children || []).forEach(button => {button.classList.remove('selected'); button.setAttribute('aria-pressed', 'false');});
    $('timeline').children?.[index]?.focus();
  };
  heading.append(title, close);
  const inner = document.createElement('div'); inner.className = 'trim-inner';
  for (const [key, name, max] of [['start', 'In (s)', clip.duration], ['end', 'Out (s)', clip.duration], ['pause', 'Pause after (ms)', 2000]]) {
    const label = document.createElement('label'); label.textContent = name;
    const input = document.createElement('input'); input.type = 'number'; input.min = '0'; input.max = String(max); input.step = key === 'pause' ? '10' : '.01'; input.value = key === 'pause' ? clip.pause : clip[key].toFixed(3);
    input.onchange = () => {
      const value = Number(input.value);
      if (!Number.isFinite(value) || value < 0 || value > max || (key === 'start' && value >= clip.end) || (key === 'end' && value <= clip.start)) {
        input.value = clip[key]; showError('Keep the cut inside the recording, with Out later than In.'); return;
      }
      clip[key] = value; showError(''); invalidateMix(); updateTimeline();
      $('remix').hidden = tokens(session.words.join(' ')).length !== clips.reduce((n,c) => n + tokens(c.phrase).length, 0);
    };
    label.append(input); inner.append(label);
  }
  const preview = document.createElement('button'); preview.className = 'secondary'; preview.textContent = '▷ Audition cut';
  preview.onclick = async () => {
    previewAudio?.pause(); previewAudio = new Audio(clip.audio);
    previewAudio.currentTime = clip.start;
    previewAudio.ontimeupdate = () => {if (previewAudio.currentTime >= clip.end) previewAudio.pause();};
    try { await previewAudio.play(); } catch { showError('Could not play this recording. Check that the local server is still running.'); }
  };
  inner.append(preview);
  const downloadCut = document.createElement('button'); downloadCut.className = 'secondary'; downloadCut.textContent = '↓ Download this cut';
  downloadCut.onclick = async () => {
    if (running) return;
    setBusy(true); showError('');
    try {
      const result = await api('/api/export-clip', {session: session.id, clip: {id: clip.id, start: clip.start, end: clip.end}, texture: $('texture').value});
      const link = document.createElement('a'); link.href = result.url; link.download = 'wordsplice-cut-' + tokens(clip.phrase).join('-') + '.wav'; document.body.append(link); link.click(); link.remove();
    } catch (error) {showError(error.message);}
    finally {setBusy(false);}
  };
  inner.append(downloadCut);
  const info = document.createElement('small'); info.textContent = `Recognized: ${clip.transcript} · Recording: ${clip.duration.toFixed(2)}s`;
  if (clip.source?.url) {
    const link = document.createElement('a'); link.href = clip.source.url; link.target = '_blank'; link.rel = 'noopener'; link.className = 'text-link'; link.textContent = 'Original source ↗'; inner.append(link);
  }
  const transcript = document.createElement('details'); transcript.className = 'source-transcript';
  const summary = document.createElement('summary'); summary.textContent = 'Recording transcript';
  transcript.append(summary, info); inner.append(transcript);
  const actions = document.createElement('div'); actions.className = 'phrase-actions';
  const replace = document.createElement('button'); replace.className = 'secondary'; replace.textContent = '↻ Find another take';
  replace.onclick = () => changePhrase(index, 'replace'); actions.append(replace);
  const splitGroup = document.createElement('div'); splitGroup.className = 'phrase-action-group';
  const phraseWords = tokens(clip.phrase);
  const splitLabel = document.createElement('label'); splitLabel.textContent = 'Split after';
  const boundary = document.createElement('select'); boundary.setAttribute('aria-label', 'Split phrase after word');
  for (let i = 1; i < phraseWords.length; i++) {
    const option = document.createElement('option'); option.value = String(i);
    option.textContent = phraseWords.slice(0, i).join(' ') + ' | ' + phraseWords.slice(i).join(' '); boundary.append(option);
  }
  boundary.value = String(Math.max(1, Math.floor(phraseWords.length / 2))); boundary.disabled = phraseWords.length < 2;
  if (phraseWords.length < 2) {
    const option = document.createElement('option'); option.textContent = 'Single word'; boundary.append(option);
  }
  splitLabel.append(boundary);
  const split = document.createElement('button'); split.className = 'secondary'; split.textContent = 'Split into two takes'; split.disabled = phraseWords.length < 2;
  split.onclick = () => changePhrase(index, 'split', Number(boundary.value)); splitGroup.append(splitLabel, split); actions.append(splitGroup);
  const joinGroup = document.createElement('div'); joinGroup.className = 'phrase-action-group';
  const join = document.createElement('button'); join.className = 'secondary'; join.textContent = 'Join with next'; join.disabled = index === clips.length - 1;
  join.onclick = () => changePhrase(index, 'join');
  const next = document.createElement('small'); next.textContent = clips[index + 1] ? `Next: “${clips[index + 1].phrase}”` : 'This is the last phrase.';
  joinGroup.append(join, next); actions.append(joinGroup);
  const note = document.createElement('p'); note.className = 'edit-note';
  note.textContent = 'These actions record new takes for the same words. Split creates two clips; Join searches for one continuous phrase. Your current cuts stay until every replacement is ready.';
  tools.append(heading, actions, inner, note); $('trim-panel').replaceChildren(tools);
}
async function changePhrase(index, operation, splitAt) {
  if (running || !modelReady || !session || !clips[index]) return;
  if (tokens($('sentence').value).join(' ') !== session.words.join(' ')) return showError('Create audio for your changed sentence before editing these cuts.');
  const original = copyClips(clips);
  const current = original[index]; const phraseWords = tokens(current.phrase);
  if (operation === 'split' && (!Number.isInteger(splitAt) || splitAt < 1 || splitAt >= phraseWords.length)) return showError('Choose a split between two words.');
  if (operation === 'join' && !original[index + 1]) return;
  const phrases = operation === 'split' ? [phraseWords.slice(0, splitAt).join(' '), phraseWords.slice(splitAt).join(' ')]
    : operation === 'join' ? [current.phrase + ' ' + original[index + 1].phrase] : [current.phrase];
  const exclude = operation === 'replace' ? [...new Set([...(current.takeHistory || []), sourceKey(current.source)])] : [];
  let committed = false;
  showError(''); cancelled = false; setBusy(true); previewAudio?.pause(); $('mix-audio').pause();
  try {
    await openCapture();
    const replacements = [];
    for (const phrase of phrases) {
      const result = await capturePhrase(phrase, exclude, replacements.map(c => c.source.video));
      checkStop();
      if (result.unavailable) throw new Error(`Could not find a usable take for “${phrase}”. ${result.reason}`);
      replacements.push(result);
    }
    replacements[replacements.length - 1].pause = operation === 'join' ? original[index + 1].pause : current.pause;
    checkStop();
    undoStack.push({clips: original, sentence: $('sentence').value}); if (undoStack.length > 20) undoStack.shift();
    clips.splice(index, operation === 'join' ? 2 : 1, ...replacements);
    if (operation !== 'replace') syncPhraseText();
    committed = true; invalidateMix(); selectedClip = index; updateTimeline(); releaseCapture();
    if (isComplete()) await renderMix();
    else status('The phrase edit is ready.', 'Continue recording to finish the sentence.');
  } catch (error) {
    const reason = cancelled || error.name === 'AbortError' ? 'Edit cancelled.' : error.name === 'NotAllowedError' ? 'Tab sharing was cancelled.' : error.message;
    showError(reason + (committed ? ' The new cuts are kept. Use Update mix to retry.' : ' Your original cuts and mix are unchanged.'));
    status(committed ? 'New cuts ready; mix needs attention.' : 'Original cuts kept.');
  } finally {
    setBusy(false); releaseCapture(); player?.pauseVideo?.(); editClip(index); updateUndo();
  }
}
function updateUndo() {
  $('undo').hidden = !undoStack.length; $('undo').disabled = running || !undoStack.length;
}
async function undoEdit() {
  if (running || !undoStack.length) return;
  const previous = undoStack.pop(); clips = previous.clips; $('sentence').value = previous.sentence;
  session.sentence = previous.sentence; previousSettings = previous.sentence + '|' + $('accent').value + '|' + $('phrase-size').value + '|' + $('voice').value + '|' + $('topic').value.trim();
  updatePhraseHints(); selectedClip = Math.min(selectedClip, clips.length - 1); invalidateMix(); updateTimeline(); updateUndo();
  if (isComplete()) {try {await renderMix();} catch (error) {showError(error.message);}}
  if (selectedClip >= 0) editClip(selectedClip);
}
async function renderMix() {
  if (!session || tokens($('sentence').value).join(' ') !== session.words.join(' ')) throw new Error('The sentence has changed. Create audio for the new sentence before exporting.');
  const wasRunning = running;
  setBusy(true);
  previewAudio?.pause(); $('remix').disabled = true; showError('');
  status('Splicing your statement…', 'Leveling voices, shaping the sound and smoothing the cuts.');
  try {
    const result = await api('/api/render', {session: session.id, sentence: $('sentence').value, clips: clips.map(c => ({id: c.id, start: c.start, end: c.end, pause: c.pause, source: c.source, takeHistory: c.takeHistory})), gap: Number($('gap').value), texture: $('texture').value, settings: projectSettings()});
    $('mix-audio').src = result.url; $('mix-audio').hidden = false;
    $('download').href = result.url; $('download').download = 'wordsplice-' + session.words.slice(0, 5).join('-') + '.wav';
    $('download').classList.remove('disabled'); $('download').setAttribute('aria-disabled', 'false');
    $('mix-title').textContent = 'Your statement is ready';
    $('mix-detail').textContent = `${result.duration.toFixed(2)} seconds · ${clips.length} cuts · 48 kHz WAV`;
    $('source-state').textContent = 'ASSEMBLED'; $('remix').hidden = true;
    status('Ready to listen.', 'Review the joins. Click any phrase to adjust its cut or the pause after it.');
  } finally { setBusy(wasRunning); }
}
async function start() {
  if (running || !modelReady) return;
  const sentence = $('sentence').value.trim();
  if (!tokens(sentence).length || tokens(sentence).length > 60) return showError('Enter between 1 and 60 English words.');
  if (phrasePlan(sentence).groups.some(group => !group.words.length)) return showError('Add words between each pair of dash separators.');
  if (!navigator.mediaDevices?.getDisplayMedia || !window.MediaRecorder) return showError('Open this local address in Chrome or Edge to capture tab audio.');
  showError(''); cancelled = false; setBusy(true); $('mix-audio').pause(); previewAudio?.pause(); $('trim-panel').hidden = true;
  const settings = sentence + '|' + $('accent').value + '|' + $('phrase-size').value + '|' + $('voice').value + '|' + $('topic').value.trim();
  try {
    await openCapture();
    if (!session || settings !== previousSettings || clips.reduce((n,c) => n + tokens(c.phrase).length, 0) === session.words.length) {
      session = await api('/api/session', {sentence}); clips = []; undoStack = []; selectedClip = -1; previousSettings = settings; invalidateMix(); $('remix').hidden = true; updateTimeline(); updateUndo();
    }
    let position = clips.reduce((n,c) => n + tokens(c.phrase).length, 0);
    const plan = phrasePlan(sentence);
    while (position < session.words.length) {
      checkStop(); let accepted = null; let lastError = '';
      const manual = plan.explicit ? plan.groups.find(group => group.offset === position) : null;
      if (plan.explicit && !manual) throw new Error('The saved cuts do not match these phrase breaks. Start a new sentence.');
      const sizes = manual ? [manual.words.length] : Array.from({length: Math.min(Number($('phrase-size').value), session.words.length - position)}, (_, i) => Math.min(Number($('phrase-size').value), session.words.length - position) - i);
      for (const size of sizes) {
        const phrase = session.words.slice(position, position + size).join(' ');
        const result = await capturePhrase(phrase);
        if (!result.unavailable) {accepted = result; clips.push(accepted); position += size; updateTimeline(); break;}
        lastError = result.reason;
      }
      if (!accepted) throw new Error(manual
        ? `No usable take for your phrase “${manual.words.join(' ')}”. ${lastError} Your phrase breaks are kept. Try Resume or add another dash to shorten this phrase.`
        : `Could not capture the exact word “${session.words[position]}”. ${lastError} Completed cuts are kept; Resume retries this word.`);
    }
    // End capture before the mix can be auditioned, avoiding feedback into a recording.
    releaseCapture(); await renderMix();
  } catch (error) {
    const message = error.name === 'NotAllowedError' ? 'Tab sharing was cancelled. Click Create audio and share this Wordsplice tab with audio to begin.' : error.name === 'AbortError' ? 'Stopped. Your completed clips are kept. Click Resume to continue.' : error.message;
    showError(message); status(cancelled ? 'Session stopped.' : 'The session needs your attention.', message);
    $('source-state').textContent = cancelled ? 'STOPPED' : 'PAUSED';
  } finally {
    setBusy(false); releaseCapture(); player?.pauseVideo?.();
    const incomplete = session && clips.length && clips.reduce((n,c) => n + tokens(c.phrase).length, 0) < session.words.length;
    $('create').textContent = incomplete ? 'Resume recording ↗' : 'Create audio ↗';
  }
}
function releaseCapture() {const stream = capture; capture = null; stream?.getTracks().forEach(track => track.stop());}
function stop() {cancelled = true; requestController?.abort(); activeStop?.(); player?.pauseVideo?.(); releaseCapture();}
$('create').onclick = start;
$('stop').onclick = stop;
$('remix').onclick = () => renderMix().catch(error => showError(error.message));
$('undo').onclick = undoEdit;
$('topic').onchange = () => {
  status($('topic').value.trim() ? 'Source context: ' + $('topic').value.trim() : 'All source topics selected.', 'Applies to new takes. Topics describe context; they do not verify the recording date.');
};
$('voice').onchange = () => {
  const label = $('voice').value ? $('voice').value[0].toUpperCase() + $('voice').value.slice(1) : 'Any voice';
  status(label + ' selected for new takes.', 'Create a new collage or use Find another take on a phrase. Existing audio stays as recorded.');
};
$('sentence').oninput = () => {
  $('word-count').textContent = tokens($('sentence').value).length + ' words'; showError('');
  updatePhraseHints();
  if (session && tokens($('sentence').value).join(' ') !== session.words.join(' ')) {
    invalidateMix(); $('remix').hidden = true; $('mix-title').textContent = 'Previous cuts — create audio for your new sentence';
  }
};
$('sentence').oninput();
$('mode').onchange = () => {
  const narrative = $('mode').value === 'narrative';
  $('phrase-size').value = narrative ? '6' : '2'; $('gap').value = narrative ? '150' : '25';
  $('gap').onchange?.();
  $('mode-description').textContent = narrative ? 'Longer fragments. Room between voices. Let the context carry the mood.' : 'Separate words, deliberate joins. Make different voices deliver one statement.';
};
for (const id of ['texture', 'gap']) $(id).onchange = () => {
  if (id === 'gap') clips.forEach(c => {c.pause = Number($('gap').value);});
  if (clips.length) {invalidateMix(); if (selectedClip >= 0) editClip(selectedClip);}
};
window.addEventListener('beforeunload', event => {if (running) {event.preventDefault(); event.returnValue = ''; } releaseCapture();});
async function pollReady() {
  try {
    const response = await fetch('/api/status'); const data = await response.json();
    modelReady = data.state === 'ready'; $('create').disabled = !modelReady || restoring;
    if (!running) status(modelReady ? (clips.length ? 'Your saved collage is ready to edit.' : 'Ready to find your first voice.') : data.message, modelReady ? 'No microphone is used. Only the tab audio is recorded.' : 'First-time setup may take a few minutes.');
    if (!modelReady && data.state !== 'error') setTimeout(pollReady, 2500);
    if (data.state === 'error') showError(data.message);
  } catch {status('The local studio is offline.', 'Open Start Wordsplice to start the tool.'); setTimeout(pollReady, 5000);}
}
async function restoreProject() {
  try {
    const response = await fetch('/api/latest');
    if (!response.ok) return;
    const saved = await response.json();
    if (!saved.session) return;
    session = saved.session; clips = saved.clips.map(c => ({...c, pause: c.pause ?? 25}));
    $('sentence').value = session.sentence;
    for (const [id, value] of Object.entries(saved.settings || {})) if (['accent', 'voice', 'topic', 'phrase-size', 'mode', 'texture', 'gap'].includes(id)) $(id).value = String(value);
    $('mode-description').textContent = $('mode').value === 'narrative' ? 'Longer fragments. Room between voices. Let the context carry the mood.' : 'Separate words, deliberate joins. Make different voices deliver one statement.';
    previousSettings = $('sentence').value + '|' + $('accent').value + '|' + $('phrase-size').value + '|' + $('voice').value + '|' + $('topic').value.trim();
    $('sentence').oninput(); updateTimeline();
    if (saved.mix) {
      $('mix-audio').src = saved.mix; $('mix-audio').hidden = false;
      $('download').href = saved.mix; $('download').download = 'wordsplice-' + session.words.slice(0,5).join('-') + '.wav';
      $('download').classList.remove('disabled'); $('download').setAttribute('aria-disabled','false'); $('remix').hidden = true;
      $('mix-title').textContent = 'Your saved collage'; $('mix-detail').textContent = 'Click any phrase to replace, split or join it.';
      $('source-state').textContent = 'SAVED';
      if (modelReady) status('Your saved collage is ready to edit.', 'Select a phrase to replace, split, join or trim it.');
    }
  } catch {showError('Your last collage could not be reopened. Its audio files remain saved on this computer.');}
  finally {restoring = false; $('create').disabled = !modelReady;}
}
$('create').disabled = true; pollReady(); restoreProject();
