import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SKILL = Path(__file__).resolve().parents[1]/'.agents/skills/local-media-mining'


def load(name):
    spec = importlib.util.spec_from_file_location(name, SKILL/'scripts'/f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = load('media')
boundary = load('boundary')


class MediaContractTests(unittest.TestCase):
    def data(self, **kw):
        return dict(audio_access=True, video_access=True, speech_present=True,
                    transcript='Test words', speech_segments=[], visual_events=[dict(start_s=0,end_s=1,description='A frame',visible_text='')], **kw)

    def seed_attempt(self, job, index=0):
        if not (job/'manifest.json').exists():
            m.save(job/'manifest.json',{'spans':[{'start':0,'end':10}], 'mode':'analyze','representation':'json'})
        plan=json.loads((job/'manifest.json').read_text())
        a=job/f'part-{index:04d}/attempt-0001';a.mkdir(parents=True)
        media=a/'workspace/input.mp4';media.parent.mkdir();media.write_bytes(b'synthetic fixture, not playable media')
        data=self.data()
        raw=[{'event':'init','init':{'tools':['view_file']}},
             {'event':'step_update','step_update':{'tool_info':{'name':'view_file','parameters':{'AbsolutePath':str(media)}}}},
             {'event':'result','result':{'status':'SUCCESS','structured_output':data}}]
        (a/'raw.ndjson').write_text('\n'.join(json.dumps(x) for x in raw))
        m.save(a/'input.json',{'path':str(media),'sha256':m.sha(media),'media':{'duration':10,'audio':True,'video':True}})
        m.save(a/'result.json',data);m.save(a/'execution.json',{'process_exit':0,'error':None,'initialized':True})
        (a/'boundary.jsonl').write_text(json.dumps({'tool':'view_file','decision':'allow'})+'\n')
        (a/'prompt.txt').write_text('Synthetic test')
        m.save(a/'request.json',{'plan_digest':m.digest(plan),'index':index,'span':plan['spans'][index]})
        names=['raw.ndjson','input.json','result.json','execution.json','boundary.jsonl','prompt.txt','request.json','workspace/input.mp4']
        m.save(a/'receipt.json',{'quality':{'reusable':True},'hashes':{n:m.sha(a/n) for n in names}})
        return a

    def quality(self, data, **kw):
        return m.assessment(data, dict(duration=43, audio=True, video=True), True, 'analyze', **kw)

    def test_exit_zero_does_not_hide_terminal_error(self):
        raw=json.dumps({'event':'result','result':{'status':'ERROR','structured_output':self.data()}})
        _,data,success=m.parse_response(raw,0)
        self.assertFalse(success)
        self.assertFalse(m.assessment(data,dict(duration=43,audio=True,video=True),success,'analyze')['reusable'])

    def test_success_without_audio_preserves_failure(self):
        data=self.data();data.update(audio_access=False,transcript='',speech_present=False)
        self.assertIn('audio_unavailable',self.quality(data)['issues'])

    def test_wav_58_seconds_for_43_second_file(self):
        data=self.data();data['speech_segments']=[dict(start_s=40,end_s=58,text='end')]
        result=self.quality(data)
        self.assertEqual(result['text'],'draft_unverified')
        self.assertEqual(result['timestamps'],'invalid')
        self.assertFalse(result['reusable'])

    def test_boolean_or_nan_timestamps_fail(self):
        for end in (True,float('nan'),float('inf')):
            data=self.data();data['visual_events']=[dict(start_s=0,end_s=end)]
            self.assertEqual(self.quality(data)['timestamps'],'invalid')

    def test_null_time_keeps_uncertainty(self):
        data=self.data();data['visual_events']=[dict(start_s=None,end_s=None,description='uncertain')]
        q=self.quality(data)
        self.assertEqual(q['timestamps'],'not_provided')
        self.assertTrue(q['review_required'])

    def test_no_track_cannot_have_transcript(self):
        q=m.assessment(self.data(),dict(duration=6,audio=False,video=True),True,'analyze')
        self.assertIn('speech_modality_contradiction',q['issues'])

    def test_silent_video_can_be_visual_draft(self):
        data=self.data();data.update(audio_access=False,speech_present=False,transcript='')
        q=m.assessment(data,dict(duration=6,audio=False,video=True),True,'analyze')
        self.assertTrue(q['reusable']);self.assertEqual(q['text'],'reported_no_speech')

    def test_missing_modality_flags_rejected(self):
        q=self.quality({'transcript':'Something','speech_segments':[],'visual_events':[]})
        self.assertFalse(q['reusable'])

    def test_truncated_or_multiple_terminal_not_accepted(self):
        with self.assertRaises(m.MediaError):m.parse_response('{"event":"init"}\n')
        result=json.dumps({'event':'result','result':{'status':'SUCCESS'}})
        with self.assertRaises(m.MediaError):m.parse_response(result+'\n'+result)

    def test_json_response_without_cli_schema(self):
        raw=json.dumps({'event':'result','result':{'status':'SUCCESS','response':json.dumps(self.data())}})
        self.assertEqual(m.parse_response(raw)[1]['transcript'],'Test words')

    def test_plain_text_is_unstructured(self):
        raw=json.dumps({'event':'result','result':{'status':'SUCCESS','response':'Hello world'}})
        _,data,ok=m.parse_response(raw,representation='text')
        q=m.assessment(data,dict(duration=1,audio=True,video=False),ok,'transcribe','text')
        self.assertIn('unstructured_response',q['issues'])

    def test_chunks_cover_end_and_overlap(self):
        parts=m.spans(943,chunk=180,overlap=2)
        self.assertEqual(parts[0]['start'],0)
        self.assertEqual(parts[-1]['end'],943)
        for a,b in zip(parts,parts[1:]):self.assertEqual(a['end']-b['start'],2)
        with self.assertRaises(m.MediaError):m.spans(30,chunk=2,overlap=2)
        with self.assertRaises(m.MediaError):m.spans(30,end=31)

    def test_billing_missing_true_or_wrong_provider_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'settings.json'
            for data in ({'useG1Credits':True},{'useG1Credits':False,'modelProvider':'gemini'}):
                m.save(p,data)
                with self.assertRaises(m.MediaError):m.billing_check(p)
            m.save(p,{'useG1Credits':False});m.billing_check(p)
            m.save(p,{});m.billing_check(p)  # Native sparse false default.

    def test_boundary_only_exact_file_including_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);ok=root/'input';ok.write_text('a')
            other=root/'other';other.write_text('b')
            link=root/'link';link.symlink_to(other)
            def request(p):return {'toolCall':{'name':'view_file','args':{'AbsolutePath':str(p)}}}
            self.assertEqual(boundary.decide(request(ok),ok),'allow')
            self.assertEqual(boundary.decide(request(link),ok),'deny')
            self.assertEqual(boundary.decide({'toolCall':{'name':'run_command','args':{}}},ok),'deny')
            self.assertEqual(boundary.decide({},ok),'deny')

    def test_unexpected_scope_events(self):
        with self.assertRaises(m.MediaError):
            m.scope_event({'event':'init','init':{'tools':['run_command']}},Path('/input'))
        with self.assertRaises(m.MediaError):
            m.scope_event({'step_update':{'tool_info':{'name':'run_command'}}},Path('/input'))
        with self.assertRaises(m.MediaError):
            m.scope_event({'step_update':{'tool_info':{'name':'view_file','parameters':{'AbsolutePath':'/other'}}}},Path('/input'))

    def test_cached_result_requires_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=self.seed_attempt(Path(tmp))
            self.assertTrue(m.intact(p))
            (p/'result.json').write_text('changed')
            self.assertFalse(m.intact(p))

    def test_verify_partial_job_preserves_completed_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            job=Path(tmp);m.save(job/'manifest.json',{'spans':[{'start':0,'end':10},{'start':8,'end':20}],'mode':'analyze','representation':'json'})
            self.seed_attempt(job)
            result=m.verify(job)
            self.assertEqual(result['state'],'partial');self.assertEqual(result['completed_parts'],1)
            self.assertIn('Test words',(job/'transcript.txt').read_text())

    def test_empty_and_malformed_hashes_do_not_accept(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=Path(tmp)
            for receipt in ({'quality':{'reusable':True},'hashes':{}}, {'quality':None,'hashes':[]}, []):
                m.save(a/'receipt.json',receipt)
                self.assertFalse(m.intact(a))

    def test_copied_request_from_other_job_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            job=Path(tmp);a=self.seed_attempt(job)
            plan=json.loads((job/'manifest.json').read_text());plan['focus']='different request'
            m.save(job/'manifest.json',plan)
            self.assertFalse(m.intact(a))

    def test_symlink_output_does_not_overwrite_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            job=Path(tmp)/'job';job.mkdir();other=Path(tmp)/'keep.txt';other.write_text('preserve')
            (job/'transcript.txt').symlink_to(other)
            with self.assertRaises(m.MediaError):m.verify(job)
            self.assertEqual(other.read_text(),'preserve')

    def test_missing_or_empty_visual_observations_fail(self):
        for events in ([],[{}]):
            data=self.data();data['visual_events']=events
            self.assertFalse(self.quality(data)['reusable'])

    def test_plain_text_refusal_not_accepted(self):
        q=m.assessment({'transcript':'I cannot access the audio'},dict(duration=1,audio=True,video=False),True,'transcribe','text')
        self.assertFalse(q['reusable'])

    def test_error_without_json_keeps_terminal_failure(self):
        raw=json.dumps({'event':'result','result':{'status':'ERROR','response':'malformed response','error':'native failure'}})
        result,data,ok=m.parse_response(raw)
        self.assertFalse(ok);self.assertEqual(result['error'],'native failure');self.assertEqual(data,{})

    def test_digital_silence_allows_visual_result_not_fabricated_speech(self):
        data=self.data();data.update(transcript='',speech_present=False,audio_access=False)
        meta=dict(duration=4,audio=True,video=True,digital_silence=True)
        q=m.assessment(data,meta,True,'analyze')
        self.assertTrue(q['reusable']);self.assertEqual(q['text'],'verified_digital_silence')
        data.update(transcript='Fabricated',speech_present=True,audio_access=True)
        self.assertFalse(m.assessment(data,meta,True,'analyze')['reusable'])


if __name__=='__main__':unittest.main()
