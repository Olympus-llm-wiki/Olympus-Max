import array
import copy
import importlib.util
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS=Path(__file__).resolve().parents[1]/'.agents/skills/local-media-mining/scripts'
sys.path.insert(0,str(SCRIPTS))
import editing as e
import editing_contract as c
import media as m


def sample(profile):
    d={'audio_access':True,'video_access':True,'uncertainties':[],
       'coverage':{'uninspected':[],'notes':[]},'style_summary':['A style']}
    if profile=='editing-map':d['scenes']=[{'id':'s1','start_s':0,'end_s':2,'description':'Scene','layout':'split'}]
    if profile=='editing-visual':
        d['layers']=[{'id':'card','role':'graphic','description':'One card','style':{}}]
        d['events']=[{'id':'v1','layer_id':'card','start_s':1,'end_s':1.2,'description':'Move','text':'<script>alert(1)</script>','bbox':[0,0,.5,.5],'animation':'slide','easing':'unknown'},
                     {'id':'v2','layer_id':'card','start_s':1.2,'end_s':2,'description':'Hold','bbox':None}]
    if profile=='editing-audio':
        d['video_access']=False
        d['perception_basis']='audio_only';d['sound_status']={'speech':'not_detected','music':'not_detected','sfx':'observed','ambience':'not_detected'}
        d['audio_events']=[{'id':'a1','kind':'sfx','start_s':1,'end_s':1.05,'description':'Possible click','text':'','character':'impulse','confidence':'low'}]
        d['links']=[{'id':'link1','audio_event_id':'a1','visual_event_id':'v1','relation':'coincides','explanation':'Candidate'}]
    return d


class ContractTests(unittest.TestCase):
    def test_profiles_and_invalid_numeric_values(self):
        info={'audio':True,'video':True,'duration':2,'editing_visual_ids':['v1']}
        for profile in c.PROFILES:
            self.assertTrue(c.assess(sample(profile),info,True,profile)['reusable'])
        for v in (True,float('nan'),float('inf'),-1,3):
            d=sample('editing-visual');d['events'][0]['end_s']=v
            self.assertFalse(c.assess(d,info,True,'editing-visual')['reusable'])

    def test_dangling_duplicate_ids_and_invalid_boxes(self):
        info={'audio':True,'video':True,'duration':2}
        for change in ('reference','duplicate','box'):
            d=sample('editing-visual')
            if change=='reference':d['events'][0]['layer_id']='missing'
            if change=='duplicate':d['events'][1]['id']='v1'
            if change=='box':d['events'][0]['bbox']=[.8,.2,.3,.4]
            self.assertFalse(c.assess(d,info,True,'editing-visual')['reusable'])
        d=sample('editing-audio')
        self.assertIn('unknown_visual_event_id',c.assess(d,info,True,'editing-audio')['issues'])

    def test_null_is_unknown_and_model_cannot_promote_itself(self):
        d=sample('editing-visual');d['events'][0].update(start_s=None,end_s=None,status='measured')
        info={'audio':True,'video':True,'duration':2}
        self.assertTrue(c.assess(d,info,True,'editing-visual')['reusable'])
        data=e.assemble({'media':info},{'visual':{'data':d,'job':'job','state':'draft'}},{})
        self.assertEqual(data['visual_events'][0]['status'],'model_observation')
        self.assertIsNone(data['visual_events'][0]['start_s'])
        self.assertEqual({v['layer_id'] for v in data['visual_events']},{'card'})
        self.assertEqual(data['coverage']['computed_map_gaps'],[{'start_s':0.,'end_s':2}])
        self.assertEqual(data['status'],'partial')

    def test_profile_rejects_incompatible_mode_before_work(self):
        args=type('Args',(),{'profile':'editing-audio','mode':'transcribe','representation':'json'})()
        with self.assertRaises(m.MediaError):m.build_plan(args)

    def test_waveform_peak_does_not_classify_sfx(self):
        samples=array.array('f',[0.] * 4000);samples[2000]=1
        result=e.envelope(samples,1000,2)
        self.assertAlmostEqual(result['energy_rises'][0]['time_s'],1)
        self.assertNotIn('kind',result['energy_rises'][0])
        self.assertEqual(result['channels'],2)

    def test_missing_times_are_rejected_but_explicit_null_is_valid(self):
        d=sample('editing-visual');d['events'][0].pop('start_s');d['events'][0].pop('end_s')
        self.assertFalse(c.assess(d,{'audio':True,'video':True,'duration':2},True,'editing-visual')['reusable'])

    def test_review_needs_source_and_observed_evidence(self):
        d={'source':{'source_sha256':'a'},'visual_events':[{'id':'v1'}],'audio_events':[],'links':[],
           'measurements':{'frame_pts':[0,1],'audio':None}}
        review={'source_sha256':'a','reviews':[{'event_id':'v1','method':'frame_inspection','status':'measured','aspect':'appearance','reviewer':'test','note':'Frame check','evidence':[{'kind':'frame','index':1}]}]}
        e.apply_reviews(d,review);self.assertEqual(len(d['reviews']),1)
        review['reviews'][0]['evidence'][0]['index']=2
        with self.assertRaises(m.MediaError):e.apply_reviews(d,review)

    def test_html_embeds_data_without_executable_source_markup(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);info={'audio':False,'video':True,'duration':2}
            d=e.assemble({'source':'/tmp/source.mp4','source_sha256':'a','model':'test','media':info},
                         {'visual':{'data':sample('editing-visual'),'job':'x','state':'draft'}},
                         {'frame_pts':[0,1],'audio':None})
            e.render(p,d);text=(p/'review.html').read_text()
            self.assertNotIn('<script>alert(1)</script>',text)
            self.assertIn('\\u003cscript\\u003e',text)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'ffmpeg required')
class MediaIntegrationTests(unittest.TestCase):
    def make_source(self,p):
        # 2s: black -> white at 1s; stereo 48kHz impulse starts at 1s.
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',"color=black:s=128x224:r=25:d=2,drawbox=color=white:t=fill:enable='gte(t,1)'",
                        '-f','lavfi','-i',"aevalsrc=if(between(t\\,1\\,1.01)\\,0.8\\,0)|if(between(t\\,1\\,1.01)\\,0.4\\,0):s=48000:d=2",
                        '-c:v','libx264','-c:a','pcm_s16le','-y',str(p)],check=True)

    def test_source_rate_channels_and_known_transition_timing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);src=root/'input.mov';self.make_source(src);info=e.inventory(src)
            out=root/'audio';out.mkdir();wav,transform=m.prepare(src,out,info,{'start':0,'end':info['duration']},'analyze','editing-audio')
            probe=m.probe(wav);a=next(s for s in probe['streams'] if s['codec_type']=='audio')
            self.assertEqual(a['sample_rate'],'48000');self.assertEqual(a['channels'],2)
            data=e.capture_measurements(src,info,root/'measurements')
            peak=max(data['visual_changes'],key=lambda x:x['luma_difference'])
            self.assertAlmostEqual(peak['time_s'],1,places=3)
            attack=max(data['audio']['energy_rises'],key=lambda x:x['rise'])
            self.assertAlmostEqual(attack['time_s'],1,places=2)

    def test_delayed_audio_retains_presentation_alignment_and_selected_span(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);src=root/'delayed.mov'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=black:s=128x224:r=25:d=3',
                            '-itsoffset','1','-f','lavfi','-i','sine=frequency=500:sample_rate=48000:duration=2',
                            '-c:v','libx264','-c:a','pcm_s16le','-y',str(src)],check=True)
            info=e.inventory(src)
            for start,end,expected in [(0,3,1),(.5,2.5,.5)]:
                folder=root/str(start);folder.mkdir()
                wav,_=m.prepare(src,folder,info,{'start':start,'end':end},'analyze','editing-audio')
                raw=subprocess.check_output(['ffmpeg','-v','error','-i',str(wav),'-af','aresample=async=1:first_pts=0','-f','f32le','-c:a','pcm_f32le','-'])
                samples=array.array('f');samples.frombytes(raw)
                first=next(i for i,x in enumerate(samples) if abs(x)>.001)/48000
                self.assertAlmostEqual(first,expected,places=2)
                self.assertAlmostEqual(m.probe(wav)['duration'],end-start,places=3)

    def test_runner_reuse_and_corruption_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);src=root/'input.mov';self.make_source(src)
            options=type('Args',(),dict(input=str(src),output=str(root/'runs'),source_version=None,
                                        model='test',timeout=30,min_quota=.1,retry_failed=False))()
            calls=[]
            def execute(agy,workspace,input_path,prompt,attempt,model,timeout,representation):
                profile='editing-map' if 'Return scenes:' in prompt else 'editing-visual' if 'Return layers:' in prompt else 'editing-audio'
                calls.append(profile);d=sample(profile)
                raw=[{'event':'init','init':{'tools':['view_file']}},
                     {'event':'step_update','step_update':{'tool_info':{'name':'view_file','parameters':{'AbsolutePath':str(input_path)}}}},
                     {'event':'result','result':{'status':'SUCCESS','structured_output':d}}]
                (attempt/'raw.ndjson').write_text('\n'.join(json.dumps(x) for x in raw));(attempt/'boundary.jsonl').write_text(json.dumps({'tool':'view_file','decision':'allow'}))
                result={'process_exit':0,'error':None,'initialized':True};m.save(attempt/'execution.json',result);return result
            # sha(agy) needs a real file, but the fake never launches it.
            with patch.object(m,'quota',return_value=1),patch.object(m,'execute',side_effect=execute),patch.object(m.shutil,'which',return_value=shutil.which('python3')):
                self.assertEqual(e.run(options),0)
                self.assertEqual(e.run(options),0)
                self.assertEqual(calls,['editing-map','editing-visual','editing-audio'])
                bundle=next((root/'runs').glob('*/manifest.json')).parent
                state=json.loads((bundle/'passes.json').read_text());old_audio=Path(state['audio'])
                accepted=json.loads((old_audio/'receipt.json').read_text())['parts'][0]['accepted_attempt']
                (old_audio/accepted/'result.json').write_text('{}')
                options.only_pass='audio';options.retry_failed=True
                self.assertEqual(e.run(options),0)
                self.assertEqual(calls,['editing-map','editing-visual','editing-audio','editing-audio'])
                options.only_pass='audio';options.retry_failed=False
                options.output=str(root/'mixed');options.audio_model='alternate-model';options.reuse_bundle=str(bundle)
                self.assertEqual(e.run(options),0)
                mixed=next((root/'mixed').glob('*/breakdown.json'));md=json.loads(mixed.read_text())
                self.assertEqual(md['source']['audio_model'],'alternate-model')
                self.assertEqual(calls[-1],'editing-audio');self.assertEqual(len(calls),5)
                self.assertEqual(md['passes']['map']['job'],state['map'])
                options.only_pass=None;options.audio_model=None;options.reuse_bundle=None;options.retry_failed=False
                # A new bundle, interrupted before the visual pass, must still render map + reason.
                options.output=str(root/'interrupted')
                with patch.object(m,'quota',side_effect=[1,1,m.MediaError('insufficient_quota')]):
                    self.assertEqual(e.run(options),2)
                partial=next((root/'interrupted').glob('*/breakdown.json'))
                pd=json.loads(partial.read_text());self.assertEqual(pd['status'],'partial')
                self.assertEqual(pd['stop']['reason'],'insufficient_quota')
                self.assertTrue((partial.parent/'review.html').exists())
            bundle=next((root/'runs').glob('*/manifest.json')).parent
            d=json.loads((bundle/'breakdown.json').read_text());self.assertEqual(d['links'][0]['status'],'hypothesis')
            self.assertEqual(d['links'][0]['estimated_start_delta_ms'],0)
            (bundle/'measurements/measurements.json').write_text('{}')
            with self.assertRaises(m.MediaError):e.build(bundle)


if __name__=='__main__':unittest.main()
