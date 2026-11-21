import unittest
from lyric_pipeline import (song_query, reference_queries, match_reference, refine_reference,
                            gpu_lyric_lines, bounded_lines, suspicious_repetition)
from transcription import DEFAULTS, validate_settings


class LyricPipelineTests(unittest.TestCase):
    def test_one_engine_replaces_legacy_model_choices(self):
        self.assertEqual(DEFAULTS['model'],'large-v3')
        self.assertEqual(validate_settings({'model':'base'})['model'],'large-v3')
        self.assertEqual(validate_settings({'model':'tiny'})['model'],'large-v3')

    def test_repairs_the_reported_nightcore_tags_without_changing_cover_identity(self):
        result=song_query({'title':'(Lyrics)','artist':'「Nightcore」→ More Than A Friend (GIRLI)'})
        self.assertEqual(result,{'title':'More Than A Friend','artist':'GIRLI','repaired':True})
        cover=song_query({'title':'Song (English Cover)','artist':'Cover Artist'})
        self.assertEqual(cover['artist'],'Cover Artist');self.assertFalse(cover['repaired'])

    def test_reference_fit_recovers_after_an_invented_phrase(self):
        phrases=['We begin another quiet morning','A red bird waits beside water','There is a light across town',
                 'The train is leaving this station','We walk beneath the summer rain','The final page is turning slowly']
        raw='\n'.join(f'[{i//60:02}:{i%60:05.2f}]{p}' for i,p in zip([5,20,40,60,80,100],phrases))
        observed=[{'time':t*.85+2,'text':p} for t,p in zip([5,20,60,80,100],[phrases[i] for i in [0,1,3,4,5]])]
        observed.insert(2,{'time':30,'text':'invented phrase repeated over and over'})
        result=match_reference(observed,{'id':1,'artistName':'Test','duration':120,'syncedLyrics':raw},104)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result['alignment']['scale'],.85)
        self.assertAlmostEqual(result['lines'][2]['time'],36)
        self.assertEqual(result['lines'][2]['text'],phrases[2])
        self.assertAlmostEqual(result['lines'][5]['time'],87)

    def test_a_repeated_chorus_alone_cannot_establish_reference_alignment(self):
        phrase='We repeat this chorus again'
        raw='\n'.join(f'[00:{i:02}.00]{phrase}' for i in [0,10,20,30,40,50])
        self.assertIsNone(match_reference([{'time':i,'text':phrase} for i in [0,9,18,27,36,45]],{'syncedLyrics':raw,'duration':60},54))
    def test_phrase_anchors_do_not_depend_on_how_the_recognizer_wraps_lines(self):
        phrases=['Amber birds cross the morning river','Blue flowers grow beside this road','Cold winter winds shake our windows',
                 'Distant trains arrive at the station','Every silver cloud covers a mountain','Fresh summer rain fills the garden']
        raw='\n'.join(f'[{t//60:02}:{t%60:05.2f}]{p}' for t,p in zip([5,20,40,60,80,100],phrases))
        observed=[]
        for t,phrase in zip([5,20,40,60,80,100],phrases):
            words=phrase.split()
            for start in (0,3):
                part=words[start:start+3]
                observed.append({'time':t*.85+2+start*.1,'text':' '.join(part),'words':[{'text':word+' ','time':t*.85+2+(start+i)*.1} for i,word in enumerate(part)]})
        self.assertIsNotNone(match_reference(observed,{'syncedLyrics':raw,'duration':120},104))

    def test_local_audio_match_refines_one_line_without_stealing_a_later_repeat(self):
        phrase = "The night begins to glow"
        lines = [
            {"time": 10.0, "end": 14.0, "text": phrase, "words": []},
            {"time": 14.0, "end": 16.0, "text": "An unmatched line", "words": []},
        ]
        def heard(at):
            return {"timing": "dtw", "words": [
                {"text": word, "time": at+i*.4, "end": at+(i+1)*.4}
                for i, word in enumerate(phrase.split())
            ]}
        self.assertEqual(refine_reference(lines,[heard(10.5),heard(45)],60),1)
        self.assertEqual(lines[0]["time"],10.5)
        self.assertEqual(lines[0]["end"],12.5)
        self.assertEqual(len(lines[0]["words"]),5)
        self.assertEqual(lines[1]["time"],14)

    def test_remix_uses_the_original_title_only_as_a_reference(self):
        query, original = reference_queries({
            "title": "I Kissed A Girl Hardstyle Remix (ZYZZCORE)", "artist": "Katy Perry"
        })
        self.assertFalse(query["alternate"])
        self.assertEqual(original["title"], "I Kissed A Girl")
        self.assertTrue(original["alternate"])

    def test_accented_word_is_decoded_whole_and_keeps_its_anchor(self):
        segment = {
            "text": " \u0159\u00edkaj\u00ed, \u017ee",
            "time": 1, "end": 2, "coreStart": 0, "coreEnd": 5, "windowStart": 0, "windowEnd": 5,
            "tokens": [{"text": text, "dtw": start} for text, start in
                       [(" \ufffd", 1.1), ("\ufffd", 1.12), ("\u00edkaj\u00ed,", 1.2), (" \u017ee", 1.5)]],
        }
        line, = gpu_lyric_lines([segment], 5)
        self.assertEqual(line["text"], "\u0159\u00edkaj\u00ed, \u017ee")
        self.assertEqual(line["words"][0]["time"], 1.1)
        self.assertEqual(line["words"][1]["time"], 1.5)

    def test_a_misheard_interior_preserves_independent_line_boundaries(self):
        line = {"time": 10.0, "end": 14.0, "text": "The night begins to glow", "words": []}
        heard = [{"timing": "dtw", "words": [
            {"time": 10.4 + i*.4, "end": 10.8 + i*.4, "text": word}
            for i, word in enumerate("The night wants to glow".split())
        ]}]
        self.assertEqual(refine_reference([line], heard, 20), 1)
        self.assertAlmostEqual(line["time"], 10.4)
        self.assertAlmostEqual(line["end"], 12.4)
        self.assertEqual(line["text"], "The night begins to glow")
        self.assertEqual(line["words"], [])
        self.assertEqual([word["index"] for word in line["wordAnchors"]], [0, 1, 3, 4])
        self.assertAlmostEqual(line["wordAnchors"][-1]["time"], 12.0)

    def test_cut_is_anchored_again_after_the_restarted_verse(self):
        times = [5, 20, 40, 60, 75, 105, 115, 125]
        phrases = ["Amber birds cross the morning river", "Blue flowers grow beside this road",
                   "Cold winter winds shake our windows", "Distant trains arrive at the station",
                   "Every silver cloud covers a mountain", "Fresh summer rain fills the garden",
                   "Grass begins to grow beneath our feet", "High above us the sun is setting"]
        raw = "\n".join(f"[{t//60:02}:{t%60:05.2f}]{phrase}" for t, phrase in zip(times, phrases))
        observed = [{"time": t*.85 + (24 if t >= 105 else 2), "text": phrase}
                    for t, phrase in zip(times, phrases)]
        result = match_reference(observed, {"syncedLyrics": raw, "duration": 135}, 140)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["lines"][5]["time"], 113.25)
        self.assertAlmostEqual(result["lines"][-1]["time"], 130.25)

    def test_bad_recognition_in_one_section_does_not_move_later_audio_times(self):
        first=bounded_lines([{'time':1,'end':50,'text':'bad timing','words':[]}],0,0,24,26)
        later=bounded_lines([{'time':3,'end':5,'text':'Later words','words':[{'text':'Later ', 'time':3,'end':4},{'text':'words','time':4,'end':5}]}],22,24,48,28)
        self.assertTrue(all(line['end']<=24 for line in first))
        self.assertEqual(later[0]['time'],25);self.assertEqual(later[0]['end'],27)

    def test_loop_detection_flags_dense_runs_without_deleting_real_refrains(self):
        dense=[{'time':i*2,'text':'A phrase keeps repeating'} for i in range(7)]
        sparse=[{'time':i*50,'text':'A phrase keeps repeating'} for i in range(3)]
        self.assertEqual(len(suspicious_repetition(dense)),7)
        self.assertTrue(any(l.get('review') for l in dense))
        self.assertFalse(any(l.get('review') for l in suspicious_repetition(sparse)))
    def test_degenerate_word_loop_is_shown_once_as_uncertain_without_losing_original(self):
        loop='up '*16
        result=suspicious_repetition([{'time':0,'end':2,'section':0,'text':loop,'words':[]},{'time':2,'end':4,'section':0,'text':loop,'words':[]},{'time':24,'end':27,'section':24,'text':'A clear later phrase','words':[]}])
        self.assertEqual(len(result),2)
        self.assertTrue(result[0]['uncertain']);self.assertEqual(result[0]['end'],4)
        self.assertIn(loop,result[0]['heardText']);self.assertEqual(result[1]['time'],24)


if __name__=='__main__':unittest.main()
