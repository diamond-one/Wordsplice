import json
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import wave
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio_engine import RATE, words, find_span, save_wav, read_wav, assemble, decode_recording
from server import parse_results
import server

class AudioTests(unittest.TestCase):
    def test_exact_words_not_near_matches(self):
        recognized = [{'word': 'we', 'start': 1, 'end': 1.2}, {'word': 'think', 'start': 1.2, 'end': 1.5}]
        with self.assertRaises(ValueError):
            find_span('we thing', recognized)
        self.assertAlmostEqual(find_span('we think', recognized)[0], .965)

    def test_repeat_prefers_confident_take(self):
        heard = [{'word': 'boat', 'start': 1, 'end': 2, 'probability': .1},
                 {'word': 'boat', 'start': 4, 'end': 5, 'probability': .9}]
        self.assertAlmostEqual(find_span('boat', heard)[0], 3.965)

    def test_punctuation_and_contractions(self):
        self.assertEqual(words("There’s a boat—we're here!"), ["there's", 'a', 'boat', "we're", 'here'])

    def test_order_pauses_and_all_textures(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            a = .3 * np.sin(np.arange(RATE) * 2 * np.pi * 220 / RATE)
            b = .2 * np.sin(np.arange(RATE) * 2 * np.pi * 880 / RATE)
            save_wav(p/'a.wav', a); save_wav(p/'b.wav', b)
            for texture in ('clean', 'warm', 'radio', 'radio40', 'tape70', 'sampler90'):
                duration = assemble([(p/'a.wav', .1, .6, 150), (p/'b.wav', .1, .4, 0)], p/'out.wav', texture=texture)
                self.assertAlmostEqual(duration, .95, places=4)
                result = read_wav(p/'out.wav')
                self.assertLessEqual(float(np.max(np.abs(result))), .941)
                self.assertLess(float(np.max(np.abs(result[int(.52*RATE):int(.63*RATE)]))), .001)
            assemble([(p/'a.wav', 0, .5), (p/'b.wav', 0, .5)], p/'out.wav', gap_ms=0)
            result = read_wav(p/'out.wav')
            for segment, expected in [(result[:RATE//2], 220), (result[RATE//2:], 880)]:
                peak = np.argmax(np.abs(np.fft.rfft(segment))) * RATE / len(segment)
                self.assertAlmostEqual(peak, expected, delta=2)

    def test_reject_silence_and_bad_trims(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'silent.wav'; save_wav(path, np.zeros(RATE))
            with self.assertRaisesRegex(ValueError, 'silent'):
                decode_recording(path.read_bytes())
            for start, end in [(1,0), (-1,.1), (0,5), (0,float('nan'))]:
                with self.assertRaises(ValueError):
                    assemble([(path,start,end)], Path(tmp)/'out.wav')

    def test_search_filters_noncontiguous_and_invalid_sources(self):
        payload = {'results': [
            {'vid':'emHAoQGoQic','start':'180','end':'183','display':'There are times when you stop.'},
            {'vid':'emHAoQGoQic','start':'180','end':'183','display':'There sometimes are times.'},
            {'vid':'bad','start':'180','end':'183','display':'There are times.'}]}
        page = 'params.jsonData = ' + repr(json.dumps(payload)) + ';'
        found = parse_results(page, 'there are times')
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['start'], 178)
        self.assertEqual(parse_results('params.jsonData = \'{"results": []}\';', 'nothing'), [])

    def test_saved_collage_migrates_and_keeps_dash_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, 'DATA', Path(tmp)), patch.object(server, 'SESSIONS', {}):
            sid = 'a' * 32; cid = 'b' * 32; folder = Path(tmp) / sid; folder.mkdir()
            save_wav(folder/(cid+'.wav'), np.sin(np.arange(RATE)*.03)*.2)
            clip = {'id': cid, 'phrase': 'i want', 'start': 0, 'end': .4, 'duration': 1, 'audio': f'/audio/{sid}/{cid}.wav'}
            (folder/'sources.json').write_text(json.dumps({'sentence':'i want','clips':[clip],'mix':'mix-'+cid+'.wav'}))
            project = server.latest_project()
            self.assertEqual(project['session']['id'], sid)
            server.save_session(sid); server.SESSIONS.clear()
            self.assertEqual(server.get_session(sid)['clips'][cid]['phrase'], 'i want')
            # Restored sessions can render again; separators never become spoken words.
            result = server.Handler.render(None, {'session':sid,'sentence':'i - want','clips':[clip]})
            self.assertTrue((folder/Path(result['url']).name).exists())
            self.assertEqual(server.latest_project()['session']['sentence'], 'i - want')
            with self.assertRaisesRegex(ValueError, 'sentence words'):
                server.Handler.render(None, {'session':sid,'sentence':'different words','clips':[clip]})

    def test_export_rejects_missing_or_reordered_phrases(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server,'DATA',Path(tmp)), patch.object(server,'SESSIONS',{}):
            sid='c'*32; folder=Path(tmp)/sid; folder.mkdir()
            clips=[]
            for i, phrase in enumerate(['i want','to live','on a boat']):
                cid=str(i)*32;save_wav(folder/(cid+'.wav'),np.ones(RATE)*.1)
                clips.append({'id':cid,'phrase':phrase,'start':0,'end':.1})
            server.SESSIONS[sid]={'sentence':'i want - to live - on a boat','words':words('i want to live on a boat'),'clips':{c['id']:c for c in clips}}
            for selection in [clips[:2], list(reversed(clips)), [clips[0],clips[0],clips[2]]]:
                with self.assertRaisesRegex(ValueError, 'entire sentence in order'):
                    server.Handler.render(None,{'session':sid,'clips':selection})

    def test_snapshots_work_when_windows_denies_replacement(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server,'DATA',Path(tmp)), patch.object(server,'SESSIONS',{}), patch.object(Path,'replace',side_effect=PermissionError(5,'Access is denied')):
            sid='d'*32; folder=Path(tmp)/sid; folder.mkdir()
            legacy={'sentence':'hello','words':['hello'],'clips':{}}
            (folder/'session.json').write_text(json.dumps(legacy))
            server.SESSIONS[sid]=legacy.copy()
            for i in range(4):
                server.SESSIONS[sid]['revision']=i
                server.save_session(sid)
            self.assertEqual(len(list(folder.glob('session-*.json'))),4)
            self.assertEqual(json.loads((folder/'session.json').read_text()),legacy)
            (folder/'session-9999999999999999999-incomplete.json').write_text('{')
            server.SESSIONS.clear()
            self.assertEqual(server.get_session(sid)['revision'],3)

    def test_save_failure_does_not_delete_an_aligned_recording(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server,'DATA',Path(tmp)), patch.object(server,'SESSIONS',{}):
            sid='e'*32; folder=Path(tmp)/sid; folder.mkdir()
            server.SESSIONS[sid]={'sentence':'hello','words':['hello'],'clips':{}}
            speech=SimpleNamespace(words=[SimpleNamespace(word='hello',start=.1,end=.5,probability=.9)])
            recognizer=SimpleNamespace(transcribe=lambda *args,**kwargs:([speech],None))
            with patch.object(server,'model',return_value=recognizer), patch.object(server,'decode_recording',return_value=np.ones(RATE)*.1), patch.object(server,'save_session',side_effect=PermissionError(5,'Access is denied')):
                with self.assertRaises(PermissionError):
                    server.Handler.clip(None,{'session':[sid],'phrase':['hello']},b'test')
            self.assertEqual(len(list(folder.glob('*.wav'))),1)
            self.assertEqual(len(server.SESSIONS[sid]['clips']),1)

    def test_voice_filters_have_separate_caches_and_source_links(self):
        requests=[]
        def respond(request, **kwargs):
            requests.append(request.full_url)
            voice='male' if '%3Am' in request.full_url else 'female' if '%3Af' in request.full_url else ''
            payload={'results':[{'vid':'emHAoQGoQic','start':'10','end':'12','display':'there are times'}]}
            page=f'<title>Gender: {voice}</title>params.jsonData = '+repr(json.dumps(payload))+';'
            return io.BytesIO(page.encode())
        with patch.object(server,'SEARCH_CACHE',{}), patch.object(server.time,'sleep'), patch.object(server.urllib.request,'urlopen',side_effect=respond):
            male=server.search('there are','uk','male')
            server.search('there are','uk','male')
            female=server.search('there are','uk','female')
            any_voice=server.search('there are','uk','')
            self.assertEqual(len(requests),3)
            self.assertIn('there%20are%20%3Am/english/uk',male[0]['youglish'])
            self.assertIn('there%20are%20%3Af/english/uk',female[0]['youglish'])
            self.assertEqual(any_voice[0]['voice'],'')
            with self.assertRaisesRegex(ValueError,'Choose Any'):
                server.search('there are','','invalid')

    def test_voice_filter_never_silently_returns_unfiltered_results(self):
        page='params.jsonData = '+repr(json.dumps({'results':[{'vid':'emHAoQGoQic','start':'10','end':'12','display':'hello'}]}))+';'
        with patch.object(server,'SEARCH_CACHE',{}),patch.object(server.time,'sleep'),patch.object(server.urllib.request,'urlopen',return_value=io.BytesIO(page.encode())):
            with self.assertRaisesRegex(ValueError,'did not confirm'):
                server.search('hello','','female')

    def test_topics_combine_with_voice_without_changing_spoken_words(self):
        requests=[]
        def respond(request,**kwargs):
            requests.append(request.full_url)
            topic='programming' if 'programming' in request.full_url else 'travel'
            payload={'results':[{'vid':'emHAoQGoQic','start':'10','end':'12','display':'Java is useful'}]}
            return io.BytesIO((f'<title>Gender: male #tag:{topic}</title>params.jsonData = '+repr(json.dumps(payload))+';').encode())
        with patch.object(server,'SEARCH_CACHE',{}),patch.object(server.time,'sleep'),patch.object(server.urllib.request,'urlopen',side_effect=respond):
            first=server.search('java','uk','male',' #programming ')
            server.search('java','uk','male','programming')
            other=server.search('java','uk','male','travel')
            self.assertEqual(len(requests),2)
            self.assertIn('java%20%3Am%20%23programming/english/uk',first[0]['youglish'])
            self.assertEqual(first[0]['caption'],'Java is useful')
            self.assertEqual(other[0]['topic'],'travel')
        self.assertEqual(server.normalize_topic('science fiction'),'sciencefiction')
        with self.assertRaises(ValueError):server.normalize_topic('history #travel')

    def test_topic_filter_cannot_silently_drop_its_restriction(self):
        page='params.jsonData = '+repr(json.dumps({'results':[{'vid':'emHAoQGoQic','start':'10','end':'12','display':'hello'}]}))+';'
        with patch.object(server,'SEARCH_CACHE',{}),patch.object(server.time,'sleep'),patch.object(server.urllib.request,'urlopen',return_value=io.BytesIO(page.encode())):
            with self.assertRaisesRegex(ValueError,'topic/source filter'):
                server.search('hello','','','history')

if __name__ == '__main__':
    unittest.main()
