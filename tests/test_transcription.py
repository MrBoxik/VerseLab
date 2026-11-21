import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import wave
from unittest.mock import patch
from transcription import DEFAULTS, Cancelled, Engine, Preparation, TrackTooLong, decoder_lines, recording_key, legacy_anchor_key, validate_settings


def wait_until(predicate, timeout=5):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(.01)
    raise AssertionError('Condition was not reached')


class FakeEngine:
    def __init__(self):self.loaded=False;self.started=[];self.release=threading.Event();self.model=None
    def requirements(self):pass
    def ready(self,settings,cancel):self.loaded=True;self.model=settings['model'];return 0
    def stop(self):self.loaded=False
    def status(self):return {'loaded':self.loaded,'loading':False,'model':self.model if self.loaded else None}
    def run(self,path,settings,cancel,phase):
        self.ready(settings,cancel);self.started.append(path.stem);phase('Transcribing locally')
        while not self.release.wait(.01):
            if cancel.is_set():raise Cancelled()
        if cancel.is_set():raise Cancelled()
        return {'lines':[{'time':0,'text':'Test','words':[]}],'source':'AI draft','kind':'transcription','duration':120,'seconds':.01,'timingKind':'model'}


class TranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);(self.base/'data').mkdir()
        self.paths={x:self.base/(x+'.mp3') for x in ['a','b','c']}
        for p in self.paths.values():p.write_bytes(b'original audio')
        metadata=lambda key:{'title':key,'artist':'Artist','duration':120,'sidecar':'','embedded':''}
        self.manager=Preparation(self.base,lambda key:self.paths[key],metadata,lambda a,t:{'results':[]},lambda key:{})
        self.fake=FakeEngine();self.manager.engine=self.fake
    def tearDown(self):
        self.manager.close()
        for thread in self.manager.threads:
            thread.join(timeout=2)
        self.temp.cleanup()
    def test_cache_avoids_reprocessing_and_music_is_unchanged(self):
        self.fake.release.set();job=self.manager.prepare('a','manual')
        wait_until(lambda:self.manager.jobs[job['key']]['status']=='complete')
        cached=self.manager.prepare('a','current')
        self.assertTrue(cached['cached']);self.assertEqual(self.fake.started,['a'])
        self.assertEqual(self.paths['a'].read_bytes(),b'original audio')
        wait_until(lambda:not self.fake.loaded)
    def test_batch_pause_resume_and_reprocess_changed_recording(self):
        self.manager.start_batch(['a','b'])
        wait_until(lambda:self.fake.started==['a'])
        self.manager.cancel_batch()
        wait_until(lambda:self.manager.status()['batch']['status']=='paused')
        self.fake.release.set()
        self.manager.start_batch(['a','b'])
        wait_until(lambda:self.manager.status()['batch']['status']=='complete')
        self.assertEqual(self.manager.status()['batch']['prepared'],2)
        self.paths['b'].write_bytes(b'changed audio file')
        self.manager.start_batch(['a','b'])
        wait_until(lambda:self.manager.status()['batch']['status']=='complete')
        self.assertEqual(self.manager.status()['batch']['skipped'],1)
        self.assertEqual(self.manager.status()['batch']['prepared'],1)
        self.assertEqual(self.fake.started,['a','a','b','b'])

    def test_length_limit_skips_long_tracks_and_continues_batch(self):
        original_metadata = self.manager.get_metadata
        self.manager.get_metadata = lambda key: {
            **original_metadata(key), "duration": 9 * 3600 if key != "c" else 120,
        }
        self.manager.configure({"maxTrackMinutes": 2})
        manual = self.manager.prepare("a", "manual", force=True)
        wait_until(lambda: self.manager.jobs[manual["key"]]["status"] == "skipped")
        self.fake.release.set()
        self.manager.start_batch(["a", "b", "c"])
        wait_until(lambda: self.manager.status()["batch"]["status"] == "complete")
        batch = self.manager.status()["batch"]
        self.assertEqual((batch["prepared"], batch["skippedLong"], batch["failed"]), (1, 2, 0))
        self.assertEqual(self.fake.started, ["c"])

    def test_over_limit_prefetch_leaves_capacity_for_next_track(self):
        original_metadata = self.manager.get_metadata
        self.manager.get_metadata = lambda key: {
            **original_metadata(key), "duration": 540 * 60 if key == "a" else 120,
        }
        self.manager.embed_audio = True
        self.manager.configure({"maxTrackMinutes": 5, "lookahead": 1})
        self.fake.release.set()
        with patch("transcription.save_audio_lyrics", return_value={"embeddedDigest": "saved"}):
            self.manager.prefetch(["a", "b", "c"])
            wait_until(lambda: any(job["trackId"] == "b" and job["status"] == "complete"
                                   for job in self.manager.status()["jobs"]))
        self.assertEqual(self.fake.started, ["b"])

    def test_desktop_reloads_length_limit_and_keeps_its_engine_defaults(self):
        self.manager.configure({"maxTrackMinutes": 45, "threads": 8})
        restored = Preparation(self.base, self.manager.get_path, self.manager.get_metadata,
                               self.manager.find_online, self.manager.get_saved, defaults_only=True)
        try:
            self.assertEqual(restored.settings["maxTrackMinutes"], 45)
            self.assertEqual(restored.settings["threads"], DEFAULTS["threads"])
        finally:
            restored.close()
            restored.thread.join(timeout=2)

    def test_unknown_duration_is_checked_before_the_model_loads(self):
        def decode(command, **kwargs):
            self.assertEqual(command[command.index("-t") + 1], "61")
            with wave.open(command[-1], "wb") as audio:
                audio.setparams((1, 2, 1, 61, "NONE", "not compressed"))
                audio.writeframes(bytes(122))
        engine = Engine(self.base)
        with patch("transcription.shutil.which", return_value="ffmpeg"), \
                patch("transcription.subprocess.run", side_effect=decode), \
                patch.object(engine, "ready") as ready:
            with self.assertRaises(TrackTooLong):
                engine.run(self.paths["a"], {**DEFAULTS, "maxTrackMinutes": 1}, threading.Event(), lambda _: None)
            ready.assert_not_called()

    def test_current_track_preempts_prefetch_then_runs_before_remaining_queue(self):
        self.manager.prepare('a','prefetch');wait_until(lambda:self.fake.started==['a'])
        self.manager.prepare('b','prefetch');current=self.manager.prepare('c','current')
        wait_until(lambda:self.fake.started==['a','c'])
        self.fake.release.set();wait_until(lambda:self.manager.jobs[current['key']]['status']=='complete')
        wait_until(lambda:self.fake.started==['a','c','b'])

    def test_timed_batch_stops_without_cancelling_playback_preparation(self):
        self.manager.start_batch(['a', 'b'], minutes=1)
        wait_until(lambda: self.fake.started == ['a'])
        batch = self.manager.batch
        current = self.manager.prepare('c', 'current')
        self.assertGreater(batch['endsAt'], time.time())
        self.manager._stop_timed_batch(batch)
        wait_until(lambda: self.fake.started == ['a', 'c'])
        self.assertEqual(self.manager.batch['stopReason'], 'Time limit reached')
        self.assertEqual(self.manager.batch['status'], 'paused')
        self.assertIsNone(self.manager.batch_timer)
        self.fake.release.set()
        wait_until(lambda: self.manager.jobs[current['key']]['status'] == 'complete')

    def test_two_agents_process_distinct_recordings_and_batch_waits_for_both(self):
        second = FakeEngine()
        self.manager.extra_engines[0] = second
        with patch('transcription.available_agents', return_value=2):
            self.manager.configure({'agents': 4})
            self.manager.start_batch(['a', 'a', 'b'])
            wait_until(lambda: self.fake.started and second.started)
            self.assertEqual(set(self.fake.started + second.started), {'a', 'b'})
            self.assertEqual(self.manager.status()['agents']['allowed'], 2)
            track = self.fake.started[0]
            expected = next(job['key'] for job in self.manager.status()['jobs'] if job['trackId'] == track)
            self.assertEqual(self.manager.prepare(track, 'manual', force=True)['key'], expected)
            self.fake.release.set()
            wait_until(lambda: self.manager.status()['batch']['prepared'] == 1)
            self.assertEqual(self.manager.status()['batch']['status'], 'running')
            second.release.set()
            wait_until(lambda: self.manager.status()['batch']['status'] == 'complete')
        self.assertEqual(self.manager.status()['batch']['prepared'], 2)

    def test_stop_batch_leaves_other_agents_current_song_running(self):
        second = FakeEngine()
        self.manager.extra_engines[0] = second
        with patch('transcription.available_agents', return_value=2):
            self.manager.configure({'agents': 2})
            current = self.manager.prepare('a')
            self.manager.start_batch(['b', 'c'])
            wait_until(lambda: self.fake.started and second.started)
            self.manager.cancel_batch()
            wait_until(lambda: self.manager.status()['batch']['status'] == 'paused')
            self.assertFalse(self.manager.running[current['key']][1].is_set())
            self.fake.release.set()
            second.release.set()
            wait_until(lambda: self.manager.jobs[current['key']]['status'] == 'complete')

    def test_one_allowed_agent_stays_serial_and_desktop_saves_agent_count(self):
        second = FakeEngine()
        self.manager.extra_engines[0] = second
        with patch('transcription.available_agents', return_value=1):
            self.manager.configure({'agents': 4})
            self.manager.start_batch(['a', 'b'])
            wait_until(lambda: self.fake.started == ['a'])
            self.assertEqual(second.started, [])
            self.fake.release.set()
            wait_until(lambda: self.manager.status()['batch']['status'] == 'complete')
            restored = Preparation(self.base, self.manager.get_path, self.manager.get_metadata,
                                   self.manager.find_online, self.manager.get_saved, defaults_only=True)
            try:
                self.assertEqual(restored.settings['agents'], 4)
            finally:
                restored.close()
                for thread in restored.threads:
                    thread.join(timeout=2)
        self.assertEqual(self.fake.started, ['a', 'b'])

    def test_batch_embeds_a_cached_draft_without_loading_the_model(self):
        self.manager.embed_audio = True
        cache = self.manager.folder / (recording_key(self.paths['a'], DEFAULTS) + '.json')
        cache.write_text(json.dumps({'raw': '[00:00.00]Hello', 'kind': 'transcription'}), encoding='utf-8')
        with patch('transcription.read_audio_tags', return_value=('', '')), \
                patch('transcription.save_audio_lyrics', return_value={'embeddedDigest': 'saved'}) as save:
            self.manager.start_batch(['a'])
            wait_until(lambda: self.manager.batch['status'] == 'complete')
            save.assert_called_once_with(self.paths['a'], '[00:00.00]Hello')
        self.assertEqual(self.manager.batch['prepared'], 1)
        self.assertEqual(self.fake.started, [])
    def test_starting_a_prefetched_song_promotes_it_without_cancel(self):
        job=self.manager.prepare('a','prefetch');wait_until(lambda:self.fake.started==['a'])
        self.manager.prefetch(['b'],current_id='a')
        self.assertEqual(self.manager.jobs[job['key']]['reason'],'current')
        self.assertFalse(self.manager.running[job['key']][1].is_set())
        self.fake.release.set();wait_until(lambda:self.manager.jobs[job['key']]['status']=='complete')
    def test_resident_keeps_model_ready_and_disabled_unloads_it(self):
        self.manager.configure({'mode':'resident'})
        wait_until(lambda:self.fake.loaded)
        self.fake.release.set();job=self.manager.prepare('a','manual')
        wait_until(lambda:self.manager.jobs[job['key']]['status']=='complete')
        self.assertTrue(self.fake.loaded)
        self.manager.configure({'mode':'disabled'});wait_until(lambda:not self.fake.loaded)
        with self.assertRaises(ValueError):self.manager.prepare('b')
    def test_cache_changes_with_audio_or_language_but_uses_only_one_model(self):
        first=recording_key(self.paths['a'],DEFAULTS)
        self.assertEqual(first,recording_key(self.paths['a'],{**DEFAULTS,'model':'tiny'}))
        self.assertNotEqual(first,recording_key(self.paths['a'],{**DEFAULTS,'language':'cs'}))
        self.paths['a'].write_bytes(b'changed recording')
        self.assertNotEqual(first,recording_key(self.paths['a'],DEFAULTS))

    def test_legacy_draft_is_used_only_as_reference_alignment_evidence_for_identical_audio(self):
        path=self.paths['a'];cache=self.manager.folder/(legacy_anchor_key(path,DEFAULTS)+'.json')
        cache.write_text(json.dumps({'lines':[{'time':1,'text':'Wrong old draft'}]}),encoding='utf-8')
        fit={'lines':[{'time':1,'text':'Verified reference words'}],'alignment':{'anchors':[1,2,3],'meanError':.2}}
        with patch('transcription.match_reference',return_value=fit) as matcher:
            result=self.manager._reuse_anchors(path,DEFAULTS,[{'id':1}],120)
            self.assertTrue(result['reusedAnchors']);self.assertEqual(result['lines'],fit['lines'])
            self.assertEqual(matcher.call_count,1)
            path.write_bytes(b'different audio file')
            self.assertIsNone(self.manager._reuse_anchors(path,DEFAULTS,[{'id':1}],120))
            self.assertIsNone(self.manager._reuse_anchors(path,DEFAULTS,[],120))
            self.assertEqual(matcher.call_count,1)
    def test_cancelled_worker_transport_reset_is_cancelled_not_failed(self):
        def reset_run(path,settings,cancel,phase):
            self.fake.started.append(path.stem)
            cancel.wait(3)
            raise ConnectionResetError('Native worker was terminated')
        self.fake.run=reset_run
        job=self.manager.prepare('a','manual')
        wait_until(lambda:self.fake.started==['a'])
        self.manager.cancel('a')
        wait_until(lambda:self.manager.jobs[job['key']]['status']!='running')
        self.assertEqual(self.manager.jobs[job['key']]['status'],'cancelled')
    def test_invalid_settings_and_unbounded_queues_are_rejected(self):
        for value in [{'threads':True},{'threads':16},{'model':'huge'},{'language':'anything'},{'lookahead':99}]:
            with self.subTest(value=value),self.assertRaises(ValueError):validate_settings(value)
        with self.assertRaises(ValueError):self.manager.prefetch(['a']*6)
    def test_subword_tokens_become_words_with_model_timestamps(self):
        result=decoder_lines({'segments':[{'start':1,'end':5,'text':'Hello world','words':[
            {'word':' Hello','start':1,'end':1.3,'probability':.8},
            {'word':' wor','start':2,'end':2.2,'probability':.7},
            {'word':'ld','start':2.2,'end':2.5,'probability':.9}]}]})
        self.assertEqual([w['text'] for w in result[0]['words']],['Hello ','world'])
        self.assertEqual(result[0]['words'][1]['time'],2)
        self.assertEqual(result[0]['words'][1]['end'],2.5)
    def test_unusable_silence_and_zero_word_timings_do_not_get_fake_word_sync(self):
        self.assertEqual(decoder_lines({'segments':[{'start':0,'end':2,'text':'[Music]'}]}),[])
        result=decoder_lines({'segments':[{'start':0,'end':2,'text':'Hello world','words':[
            {'word':' Hello','start':0,'end':0},{'word':' world','start':0,'end':0}]}]})
        self.assertEqual(result[0]['words'],[])


if __name__=='__main__':unittest.main()
