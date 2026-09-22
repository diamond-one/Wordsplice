"""Wordsplice: loopback-only local application. No API keys required."""
import argparse
import ast
import html
import json
import mimetypes
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import numpy as np
from audio_engine import words, find_span, decode_recording, save_wav, assemble, RATE

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('WORDSPLICE_DATA', str(ROOT / 'data'))).resolve()
DATA.mkdir(parents=True, exist_ok=True)
MODEL = None
MODEL_LOCK = threading.Lock()
MODEL_STATE = {'app': 'wordsplice', 'state': 'idle', 'message': 'Preparing local speech alignment'}
SEARCH_LOCK = threading.Lock()
SEARCH_CACHE = {}
LAST_SEARCH = 0
SESSIONS = {}
SESSION_LOCK = threading.Lock()

def write_snapshot(folder, kind, data):
    # Projected Windows folders can reject replacing an existing file. Publish
    # immutable snapshots instead; readers ignore an incomplete concurrent write.
    path = folder / f'{kind}-{time.time_ns()}-{uuid.uuid4().hex}.json'
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    with path.open('x', encoding='utf-8') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return path

def snapshots(folder, kind):
    revisions = sorted(folder.glob(f'{kind}-*.json'), key=lambda p: p.name, reverse=True)
    legacy = folder / f'{kind}.json'
    return revisions + ([legacy] if legacy.is_file() else [])

def read_snapshot(folder, kind):
    for path in snapshots(folder, kind):
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
    return None

def save_session(sid):
    with SESSION_LOCK:
        write_snapshot(DATA / sid, 'session', SESSIONS[sid])

def get_session(sid):
    if not re.fullmatch(r'[a-f0-9]{32}', sid):
        return None
    if sid not in SESSIONS:
        saved_session = read_snapshot(DATA / sid, 'session')
        saved = read_snapshot(DATA / sid, 'sources')
        if saved_session:
            SESSIONS[sid] = saved_session
        elif saved:
            SESSIONS[sid] = {'sentence': saved['sentence'], 'words': words(saved['sentence']),
                             'clips': {c['id']: c for c in saved['clips']}}
        if saved and sid in SESSIONS:
            # A completed mix may have been saved before a legacy session-file
            # replacement failed. Recover its wording and any missing clip records.
            SESSIONS[sid]['sentence'] = saved['sentence']
            for clip in saved.get('clips', []):
                SESSIONS[sid]['clips'].setdefault(clip['id'], clip)
    return SESSIONS.get(sid)

def latest_project():
    manifests = sorted(DATA.glob('*/sources*.json'), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    for path in manifests:
        try:
            sid = path.parent.name
            saved = json.loads(path.read_text(encoding='utf-8'))
            session = get_session(sid)
            if not session or not saved.get('clips'):
                continue
            if not all(re.fullmatch(r'[a-f0-9]{32}', c['id']) and (path.parent / (c['id'] + '.wav')).is_file() for c in saved['clips']):
                continue
            return {'session': {'id': sid, 'words': session['words'], 'sentence': saved['sentence']},
                    'clips': saved['clips'], 'settings': saved.get('settings', {}),
                    'mix': f'/audio/{sid}/{saved["mix"]}' if saved.get('mix') else None}
        except (ValueError, KeyError, OSError, TypeError):
            continue
    return {'session': None}

def model():
    global MODEL
    if MODEL is None:
        MODEL_STATE.update(state='loading', message='Preparing speech alignment. The first launch downloads a 150 MB model.')
        try:
            from faster_whisper import WhisperModel
            MODEL = WhisperModel('base.en', device='cpu', compute_type='int8', cpu_threads=4,
                                 download_root=str(DATA / 'models'))
            MODEL_STATE.update(state='ready', message='Local speech alignment ready')
        except Exception:
            MODEL_STATE.update(state='error', message='Could not prepare the speech model. Check your connection and restart Wordsplice.')
            raise
    return MODEL

def warmup():
    try:
        with MODEL_LOCK:
            model()
    except Exception as error:
        print('Speech model:', error, flush=True)

def normalize_topic(topic):
    topic = re.sub(r'\s+', '', topic.strip().lstrip('#'))
    if topic and not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', topic):
        raise ValueError('Enter one topic using letters, numbers, spaces, underscores or hyphens, or a YouTube video/channel ID.')
    return topic

def source_query(query, voice='', topic=''):
    return query + {'male': ' :m', 'female': ' :f'}.get(voice, '') + (f' #{topic}' if topic else '')

def parse_results(page, query, accent='', voice='', topic=''):
    match = re.search(r"params\.jsonData\s*=\s*('(?:\\.|[^'\\])*')", page)
    if not match:
        if re.search(r'0 pronunciations|No results found|No results for', page, re.I):
            return []
        raise ValueError('YouGlish did not return readable results. It may be temporarily unavailable or its page format has changed.')
    payload = json.loads(ast.literal_eval(match.group(1)))
    result = []
    wanted = words(query)
    for item in payload.get('results', []):
        video = str(item.get('vid', ''))
        caption = html.unescape(re.sub('<[^>]+>', '', str(item.get('display', ''))))
        caption_words = words(caption)
        if not any(caption_words[i:i + len(wanted)] == wanted for i in range(len(caption_words))):
            continue
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video) or item.get('src', 'YT') != 'YT':
            continue
        try:
            start, end = float(item['start']), float(item['end'])
        except (KeyError, ValueError, TypeError):
            continue
        if not (0 <= start < end and end - start <= 30):
            continue
        search_query = source_query(query, voice, topic)
        result.append({'video': video, 'start': max(0, start - 2), 'end': end + 2, 'voice': voice, 'topic': topic,
                       'caption': caption, 'url': f'https://www.youtube.com/watch?v={video}&t={int(start)}',
                       'youglish': 'https://youglish.com/pronounce/' + urllib.parse.quote(search_query, safe='') + '/english' + ('/' + accent if accent else '')})
    return result

def search(query, accent, voice='', topic=''):
    global LAST_SEARCH
    query = ' '.join(words(query))
    if not query or len(words(query)) > 60 or accent not in ('', 'us', 'uk', 'aus'):
        raise ValueError('Search for one to sixty English words.')
    if voice not in ('', 'male', 'female'):
        raise ValueError('Choose Any, Male or Female for the voice.')
    topic = normalize_topic(topic)
    key = (query, accent, voice, topic)
    with SEARCH_LOCK:
        if key in SEARCH_CACHE:
            return SEARCH_CACHE[key]
        time.sleep(max(0, 1.0 - (time.monotonic() - LAST_SEARCH)))
        search_query = source_query(query, voice, topic)
        url = 'https://youglish.com/pronounce/' + urllib.parse.quote(search_query, safe='') + '/english'
        if accent:
            url += '/' + accent
        request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; Wordsplice/1.0)', 'Accept': 'text/html'})
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                page = response.read(3_000_000).decode('utf-8')
            results = parse_results(page, query, accent, voice, topic)
            if voice and results and not re.search(r'Gender:\s*' + re.escape(voice) + r'\b', page, re.I):
                raise ValueError('YouGlish did not confirm the requested voice filter. Try again later or select Any.')
            if topic and results and not re.search(r'#tag:\s*' + re.escape(topic) + r'(?![A-Za-z0-9_-])', page, re.I):
                raise ValueError('YouGlish did not confirm the requested topic/source filter. Try another topic or clear it.')
        except urllib.error.HTTPError as error:
            if error.code == 404:
                SEARCH_CACHE[key] = []
                return []
            raise ValueError(f'YouGlish returned HTTP {error.code}. Please try again later; access checks are not bypassed.') from error
        finally:
            LAST_SEARCH = time.monotonic()
        SEARCH_CACHE[key] = results
        return results

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if args and str(args[0]).startswith('POST'):
            super().log_message(fmt, *args)

    def valid_origin(self):
        host = self.headers.get('Host', '')
        allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        return host in allowed and self.headers.get('Origin', f'http://{host}') == f'http://{host}'

    def reply(self, data, code=200):
        blob = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(blob)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self):
        if not self.valid_origin():
            return self.reply({'error': 'Local requests only'}, 403)
        route = urllib.parse.urlsplit(self.path).path
        if route == '/api/status':
            return self.reply(MODEL_STATE)
        if route == '/api/latest':
            return self.reply(latest_project())
        if route.startswith('/audio/'):
            parts = route.split('/')
            if len(parts) != 4 or not re.fullmatch('[a-f0-9]{32}', parts[2]) or not re.fullmatch(r'[a-f0-9]{32}\.wav|mix-[a-f0-9]{32}\.wav', parts[3]):
                return self.reply({'error': 'Not found'}, 404)
            path = DATA / parts[2] / parts[3]
        else:
            files = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css', '/favicon.svg': 'favicon.svg'}
            if route not in files:
                return self.reply({'error': 'Not found'}, 404)
            path = ROOT / files[route]
        if not path.is_file():
            return self.reply({'error': 'Not found'}, 404)
        blob = path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(len(blob)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'strict-origin-when-cross-origin')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(blob)

    def do_POST(self):
        if not self.valid_origin() or self.headers.get('X-Wordsplice') != '1':
            return self.reply({'error': 'Local application requests only'}, 403)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 12_000_000:
                return self.reply({'error': 'Upload too large or empty'}, 413)
            raw = self.rfile.read(length)
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == '/api/clip':
                params = urllib.parse.parse_qs(parsed.query)
                return self.reply(self.clip(params, raw))
            body = json.loads(raw)
            if parsed.path == '/api/session':
                sentence = str(body.get('sentence', ''))
                tokens = words(sentence)
                if not 1 <= len(tokens) <= 60:
                    raise ValueError('Enter a sentence of 1–60 English words.')
                sid = uuid.uuid4().hex
                with SESSION_LOCK:
                    SESSIONS[sid] = {'sentence': sentence, 'words': tokens, 'clips': {}}
                (DATA / sid).mkdir()
                save_session(sid)
                return self.reply({'id': sid, 'words': tokens})
            if parsed.path == '/api/search':
                return self.reply({'results': search(str(body.get('query', '')), str(body.get('accent', '')), str(body.get('voice', '')), str(body.get('topic', '')))})
            if parsed.path == '/api/render':
                return self.reply(self.render(body))
            if parsed.path == '/api/export-clip':
                return self.reply(self.export_clip(body))
            return self.reply({'error': 'Not found'}, 404)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            return self.reply({'error': str(error)}, 400)
        except OSError as error:
            print(type(error).__name__, str(error), flush=True)
            return self.reply({'error': 'The local save could not finish. Existing recordings are preserved. Retry the operation. ' + str(error)[:180]}, 500)
        except Exception as error:
            print(type(error).__name__, str(error), flush=True)
            return self.reply({'error': 'Processing failed. Check your connection, then retry. ' + str(error)[:180]}, 500)

    def clip(self, params, raw):
        sid = params.get('session', [''])[0]
        phrase = params.get('phrase', [''])[0]
        session = get_session(sid)
        if not session or not 1 <= len(words(phrase)) <= 60:
            raise ValueError('Invalid recording session')
        audio = decode_recording(raw)
        cid = uuid.uuid4().hex
        path = DATA / sid / (cid + '.wav')
        save_wav(path, audio)
        try:
            with MODEL_LOCK:
                segments, _ = model().transcribe(str(path), language='en', word_timestamps=True,
                    beam_size=5, vad_filter=True, condition_on_previous_text=False)
                recognized = [{'word': w.word, 'start': w.start, 'end': w.end, 'probability': w.probability}
                              for segment in segments for w in (segment.words or [])]
            start, end, confidence = find_span(phrase, recognized)
            duration = len(audio) / RATE
            result = {'id': cid, 'phrase': phrase, 'start': start, 'end': min(end, duration),
                      'duration': duration, 'confidence': round(confidence, 3),
                      'audio': f'/audio/{sid}/{cid}.wav', 'transcript': ' '.join(w['word'].strip() for w in recognized)}
            cut = audio[int(start * RATE):int(min(end, duration) * RATE)]
            peaks = [float(np.max(np.abs(chunk))) if len(chunk) else 0 for chunk in np.array_split(cut, 48)]
            result['waveform'] = [round(p / max(max(peaks), .001), 3) for p in peaks]
            SESSIONS[sid]['clips'][cid] = result
            save_session(sid)
            return result
        except Exception:
            if cid not in SESSIONS[sid]['clips']:
                path.unlink(missing_ok=True)
            raise

    def export_clip(self, body):
        sid = str(body.get('session', ''))
        session = get_session(sid)
        selected = body.get('clip', {})
        clip = session['clips'].get(selected.get('id')) if session else None
        if not clip:
            raise ValueError('This cut is not available in the current session.')
        texture = body.get('texture', 'clean')
        if texture not in ('clean', 'warm', 'radio', 'radio40', 'tape70', 'sampler90'):
            raise ValueError('Unknown sound texture')
        filename = 'mix-' + uuid.uuid4().hex + '.wav'
        duration = assemble([(DATA / sid / (clip['id'] + '.wav'), float(selected['start']), float(selected['end']))],
                            DATA / sid / filename, texture=texture)
        return {'url': f'/audio/{sid}/{filename}', 'duration': round(duration, 2)}

    def render(self, body):
        sid = str(body.get('session', ''))
        session = get_session(sid)
        if not session:
            raise ValueError('Session expired. Start a new sentence.')
        selections = body.get('clips', [])
        if not isinstance(selections, list) or not 1 <= len(selections) <= 60:
            raise ValueError('Select 1–60 clips.')
        pieces, joined = [], []
        for selected in selections:
            clip = session['clips'].get(selected.get('id'))
            if not clip:
                raise ValueError('Unknown clip')
            joined.extend(words(clip['phrase']))
            pause = float(selected.get('pause', body.get('gap', 25)))
            if not np.isfinite(pause) or not 0 <= pause <= 2000:
                raise ValueError('Pauses must be between 0 and 2000 ms.')
            pieces.append((DATA / sid / (clip['id'] + '.wav'), float(selected['start']), float(selected['end']), pause))
        if joined != session['words']:
            raise ValueError('The clips do not cover the entire sentence in order. Finish recording before exporting.')
        sentence = str(body.get('sentence', session['sentence']))
        if words(sentence) != session['words']:
            raise ValueError('The sentence words must match the clips before exporting.')
        filename = 'mix-' + uuid.uuid4().hex + '.wav'
        gap = float(body.get('gap', 25))
        if not 0 <= gap <= 200:
            raise ValueError('Choose a gap between 0 and 200 ms.')
        texture = body.get('texture', 'clean')
        if texture not in ('clean', 'warm', 'radio', 'radio40', 'tape70', 'sampler90'):
            raise ValueError('Unknown sound texture')
        duration = assemble(pieces, DATA / sid / filename, gap, texture)
        session['sentence'] = sentence
        manifest = {'sentence': sentence, 'clips': [{**session['clips'][s['id']], **{k: v for k, v in s.items() if k in ('start', 'end', 'pause', 'source', 'takeHistory')}} for s in selections], 'mix': filename,
                    'settings': body.get('settings', {})}
        with SESSION_LOCK:
            write_snapshot(DATA / sid, 'sources', manifest)
        save_session(sid)
        return {'url': f'/audio/{sid}/{filename}', 'duration': round(duration, 2)}

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--open', action='store_true')
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.daemon_threads = True
    threading.Thread(target=warmup, daemon=True).start()
    url = f'http://127.0.0.1:{args.port}'
    print(f'Wordsplice is running at {url}', flush=True)
    if args.open:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
