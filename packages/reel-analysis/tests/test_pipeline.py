import json
import multiprocessing
import shutil
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from reel_analysis.common import ReelError, read_json, write_json, file_hash
from reel_analysis.contracts import Profile, validate_observation
from reel_analysis.pipeline import Worker, shifted
from reel_analysis.store import Store
from reel_analysis.server import create_app
from reel_analysis.antigravity import UnknownOutcome
from reel_analysis import media


@pytest.fixture
def clip(tmp_path):
    def make(name="input.mp4", sound=True, color="red"):
        p=tmp_path/name;p.parent.mkdir(exist_ok=True,parents=True)
        args=["ffmpeg","-v","error","-y","-f","lavfi","-i",f"color=c={color}:s=160x240:r=10:d=2","-f","lavfi","-i","sine=frequency=400:sample_rate=16000:duration=2" if sound else "anullsrc=r=16000:cl=stereo","-t","2","-c:v","libx264","-c:a","aac",str(p)]
        media.execute(args)
        return p
    return make


class FixtureBackend:
    """Protocol fixture, never presented as a Gemini accuracy result."""
    def __init__(self):self.calls=[]
    def invoke(self,**kw):
        name=kw['attempt'].parent.name;self.calls.append(name)
        kw['attempt'].mkdir(parents=True,exist_ok=True)
        if name=='speech':
            value={'status':'observed','segments':[{'id':'sp1','start_s':0,'end_s':2,'timing_uncertainty_s':.2,'text':'fixture phrase'}],'gaps':[]}
        elif name=='visual':
            value={'summary':'fixture','layers':[{'id':'l1','role':'captions','description':'text'}],'scenes':[{'id':'s1','start_s':0,'end_s':2,'timing_uncertainty_s':.2,'description':'fixture'}],'text_events':[{'id':'t1','layer_id':'l1','start_s':0,'end_s':1,'timing_uncertainty_s':.2,'text':'different caption','appearance':'white','bbox':None}],'visual_events':[{'id':'v1','layer_id':'l1','start_s':.5,'end_s':1.2,'timing_uncertainty_s':.2,'kind':'entrance','description':'appears'}],'gaps':[]}
        elif name=='reconcile':
            value={'summary':'fixture linked','links':[],'gaps':[]}
        elif name=='course':
            value={'interpretations':[{'event_id':'v1','source_id':'lesson1','explanation':'interpretation only'}],'gaps':[]}
        else:raise AssertionError(name)
        response={'data':value,'usage':{'input_tokens':10,'output_tokens':20},'status':'SUCCESS','conversation_id':'fixture'}
        write_json(kw['attempt']/'response.json',response)
        return response


def test_full_bundle_reuse_and_restore(tmp_path,clip):
    store=Store(tmp_path/'data');p=Profile(max_refinements=0)
    asset=store.ingest(clip(),p);job=store.submit(asset['id'],p)
    backend=FixtureBackend();out=Worker(store,backend).run_once()
    assert out['state']=='completed';assert backend.calls==['speech','visual','reconcile']
    bundle=store.result(job['id']);data=read_json(bundle/'analysis.json')
    assert data['speech']['segments'][0]['text']!=data['visual']['text_events'][0]['text']
    assert data['quality']['state']=='needs_review'
    assert '/Users/' not in (bundle/'review.html').read_text()
    assert store.submit(asset['id'],p)['id']==job['id']
    assert Worker(store,backend).run_once() is None
    store.backup(tmp_path/'snapshot')
    restored=Store.restore(tmp_path/'snapshot',tmp_path/'restored')
    assert read_json(restored.result(job['id'])/'analysis.json')==data
    assert Worker(restored,backend).run_once() is None


def test_silent_video_skips_speech(tmp_path,clip):
    store=Store(tmp_path/'data');asset=store.ingest(clip(sound=False))
    assert asset['digital_silence'] is True
    job=store.submit(asset['id']);backend=FixtureBackend();Worker(store,backend).run_once()
    result=read_json(store.result(job['id'])/'analysis.json')
    assert 'speech' not in backend.calls
    assert result['speech']['segments']==[]
    assert result['speech_basis']=='verified_digital_silence'
    assert result['visual']['text_events']


def test_same_basename_and_corruption(tmp_path,clip):
    store=Store(tmp_path/'data')
    a=store.ingest(clip('one/01_video.mp4'));b=store.ingest(clip('two/01_video.mp4',color='blue'))
    assert a['id']!=b['id']
    bad=tmp_path/'bad.mp4';bad.write_bytes(b'not video')
    with pytest.raises(ReelError):store.ingest(bad)
    assert not list((store.root/'incoming').iterdir())


def test_unknown_outcome_holds_new_inference(tmp_path,clip):
    class Unknown(FixtureBackend):
        def invoke(self,**kw):raise UnknownOutcome('inference_outcome_unknown')
    store=Store(tmp_path/'data');a=store.ingest(clip());b=store.ingest(clip('b.mp4',color='blue'))
    one=store.submit(a['id']);store.submit(b['id'])
    assert Worker(store,Unknown()).run_once()['state']=='needs_attention'
    assert store.next_job() is None
    assert store.get(one['id'])['error']=='inference_outcome_unknown'


def test_targeted_rerun_preserves_visual(tmp_path,clip):
    store=Store(tmp_path/'data');asset=store.ingest(clip());job=store.submit(asset['id'])
    backend=FixtureBackend();Worker(store,backend).run_once()
    new=store.rerun(job['id'],'speech');backend.calls=[];Worker(store,backend).run_once()
    assert backend.calls==['speech','reconcile']
    assert store.result(job['id']).exists() and store.result(new['id']).exists()
    assert read_json(store.job_dir(new['id'])/'stages/visual/accepted.json')['reused_from']==job['id']


def test_invalid_reference_rejected_and_nullable_span():
    with pytest.raises(ReelError,match='unknown_link'):
        validate_observation('reconcile',{'summary':'','links':[{'speech_id':'bad','event_id':'bad','relation':'x'}],'gaps':[]},2)
    good={'status':'unavailable','segments':[],'gaps':[{'track':'speech','start_s':None,'end_s':None,'reason':'unknown'}]}
    assert validate_observation('speech',good,2)==good
    shifted_value=shifted({'segments':[{'id':'x','start_s':.2,'end_s':.7}],'gaps':[]},9,'r_')
    assert shifted_value['segments'][0]['start_s']==9.2
    assert shifted_value['segments'][0]['local_start_s']==.2


def test_course_cannot_create_visual_event(tmp_path,clip):
    store=Store(tmp_path/'data');asset=store.ingest(clip());job=store.submit(asset['id'],course=[{'source_id':'lesson1','text':'An interpretation'}])
    Worker(store,FixtureBackend()).run_once();data=read_json(store.result(job['id'])/'analysis.json')
    assert len(data['visual']['visual_events'])==1 and len(data['course']['interpretations'])==1
    with pytest.raises(ReelError,match='unknown_course'):
        validate_observation('course',{'interpretations':[{'event_id':'invented','source_id':'lesson1','explanation':'bad'}],'gaps':[]},2,{'visual':data['visual'],'course_ids':['lesson1']})


def test_http_authorization_upload_and_client_disconnect(tmp_path,clip):
    store=Store(tmp_path/'data');token='test-token-'+'x'*30;app=create_app(store,token,allowed_hosts=['testserver'])
    source=clip();body=source.read_bytes();headers={'Authorization':'Bearer '+token,'Content-Type':'application/octet-stream','X-Content-SHA256':file_hash(source)}
    with TestClient(app) as client:
        assert client.get('/limits').status_code==401
        assert client.post('/assets',content=body,headers={**headers,'X-Content-SHA256':'0'*64}).status_code==400
        response=client.post('/assets',content=body,headers=headers);assert response.status_code==201,response.text
        asset_id=response.json()['asset_id']
        auth={'Authorization':'Bearer '+token}
        response=client.post('/jobs',json={'asset_id':asset_id},headers=auth);assert response.status_code==202
        job=response.json()
        assert client.post('/jobs',json={'asset_id':'/Users/someone/file.mp4'},headers=auth).status_code==400
    # The disconnected client never owned worker lifetime.
    Worker(store,FixtureBackend()).run_once()
    with TestClient(create_app(store,token,allowed_hosts=['testserver'])) as client:
        assert client.get('/jobs/'+job['id']).status_code==401
        assert client.get('/jobs/'+job['id']+'/artifacts/review.html',headers=auth).status_code==200
        assert client.get('/jobs/'+job['id']+'/artifacts/analysis.json',headers=auth).json()['execution']['state']=='completed'
    assert not list((store.root/'incoming').iterdir())


def hold_lock(root,ready):
    with Store(root).worker_lock():
        ready.set();time.sleep(1)


def test_worker_lock_across_processes(tmp_path):
    store=Store(tmp_path/'data');ctx=multiprocessing.get_context('spawn');event=ctx.Event();p=ctx.Process(target=hold_lock,args=(str(store.root),event));p.start()
    try:
        assert event.wait(5)
        with pytest.raises(ReelError,match='worker_busy'):
            Worker(store,FixtureBackend()).run_once()
    finally:p.join(5)


def test_scope_gate_and_corrupted_backup(tmp_path):
    from reel_analysis.scope_hook import decide
    assert decide({'toolCall':{'name':'run_command'}},set())=='deny'
    assert decide({'toolCall':{'name':'view_file','args':{'AbsolutePath':'/secret'}}},{'/allowed'})=='deny'
    assert decide({'toolCall':{'name':'view_file','args':{'AbsolutePath':'/allowed'}}},{'/allowed'})=='allow'
    s=Store(tmp_path/'data');s.backup(tmp_path/'copy');(tmp_path/'copy/jobs.sqlite3').write_bytes(b'bad')
    with pytest.raises(ReelError,match='backup_hash_mismatch'):Store.restore(tmp_path/'copy',tmp_path/'restore')


def test_mcp_list_tools_and_submit(tmp_path,clip):
    store=Store(tmp_path/'data');asset=store.ingest(clip())
    app=create_app(store,'x'*32,allowed_hosts=['testserver'])
    headers={'Authorization':'Bearer '+'x'*32,'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-11-25'}
    def call(client,method,params,n):
        r=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':n,'method':method,'params':params})
        assert r.status_code==200,r.text
        if r.headers['content-type'].startswith('text/event-stream'):
            return json.loads(next(l[6:] for l in r.text.splitlines() if l.startswith('data: ')))
        return r.json()
    with TestClient(app) as c:
        init=call(c,'initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'test','version':'1'}},1)
        assert init['result']['serverInfo']['name']=='reel-analysis'
        tools=call(c,'tools/list',{},2)
        assert {x['name'] for x in tools['result']['tools']}=={'submit_reel','reel_status','reel_result','rerun_reel'}
        submitted=call(c,'tools/call',{'name':'submit_reel','arguments':{'asset_id':asset['id']}},3)
        assert not submitted['result'].get('isError',False)
        assert store.next_job()['state']=='queued'


def test_refinement_budget_and_report_escaping(tmp_path,clip):
    class Uncertain(FixtureBackend):
        def invoke(self,**kw):
            name=kw['attempt'].parent.name
            if name.startswith('refine-'):
                self.calls.append(name)
                return {'data':{'summary':'','layers':[],'scenes':[],'text_events':[],'visual_events':[],'gaps':[{'track':'text','start_s':0,'end_s':2,'reason':'still unreadable'}]},'usage':{}}
            result=super().invoke(**kw)
            if name=='visual':
                result['data']['gaps']=[{'track':'text','start_s':0,'end_s':2,'reason':'<script>alert(1)</script>'}]
                result['data']['summary']='</script><img src=x onerror=alert(1)>'
            return result
    store=Store(tmp_path/'data');job=store.submit(store.ingest(clip())['id'],Profile(max_refinements=1));backend=Uncertain()
    Worker(store,backend).run_once();result=store.result(job['id']);d=read_json(result/'analysis.json')
    assert backend.calls.count('refine-0')==1
    assert len(d['refinements'])==1 and d['quality']['state']=='partial'
    page=(result/'review.html').read_text()
    assert '<script>alert(1)</script>' not in page
    assert '</script><img' not in page


def test_raw_recovery_without_fresh_native_call(tmp_path):
    from reel_analysis.antigravity import Antigravity
    d=tmp_path/'attempt';d.mkdir()
    write_json(d/'submitted.json',{'sent':True});write_json(d/'input.json',{'files':[]})
    result={'status':'SUCCESS','structured_output':{'ok':True},'usage':{'input_tokens':10},'conversation_id':'sample'}
    (d/'raw.ndjson').write_text(json.dumps({'event':'result','result':result})+'\n')
    assert Antigravity().recover(d)['data']=={'ok':True}
    (d/'raw.ndjson').write_text(json.dumps({'event':'init','conversation_id':'sample'})+'\n')
    with pytest.raises(UnknownOutcome):Antigravity().recover(d)


class PowerLoss(BaseException):
    pass


def test_restart_after_raw_before_stage_commit(tmp_path,clip):
    class Interrupted(FixtureBackend):
        lost=False
        def invoke(self,**kw):
            response=kw['attempt']/'response.json'
            if response.exists():return read_json(response)
            result=super().invoke(**kw)
            if not self.lost:
                self.lost=True
                raise PowerLoss()
            return result
    store=Store(tmp_path/'data');job=store.submit(store.ingest(clip())['id'])
    backend=Interrupted()
    with pytest.raises(PowerLoss):Worker(store,backend).run_once()
    assert store.get(job['id'])['state']=='waiting_provider'
    Worker(Store(store.root),backend).run_once()
    assert backend.calls==['speech','visual','reconcile']
    assert store.get(job['id'])['state']=='completed'


def test_rerun_speech_model_does_not_invalidate_visual(tmp_path,clip):
    store=Store(tmp_path/'data');job=store.submit(store.ingest(clip())['id']);backend=FixtureBackend()
    Worker(store,backend).run_once();backend.calls=[]
    new=store.rerun(job['id'],'speech',Profile(speech_model='gemini-3.1-pro-high'))
    Worker(store,backend).run_once()
    assert backend.calls==['speech','reconcile']
    assert store.get(new['id'])['state']=='completed'


def test_native_scope_is_required_for_media_result(tmp_path):
    from reel_analysis.antigravity import Antigravity
    attempt=tmp_path/'attempt';attempt.mkdir()
    write_json(attempt/'input.json',{'files':[{'path':'/assigned'}]})
    (attempt/'raw.ndjson').write_text(json.dumps({'event':'result','result':{'status':'SUCCESS','structured_output':{'ok':True}}})+'\n')
    with pytest.raises(ReelError,match='media_not_read'):Antigravity.recover(attempt)
    (attempt/'scope.jsonl').write_text(json.dumps({'tool':'view_file','decision':'allow'})+'\n')
    with pytest.raises(ReelError,match='primary_media_not_read'):Antigravity.recover(attempt)
    terminal=(attempt/'raw.ndjson').read_text()
    observed={'event':'step_update','step_update':{'state':'DONE','tool_info':{'name':'view_file','parameters':{'AbsolutePath':'/assigned'}}}}
    (attempt/'raw.ndjson').write_text(json.dumps(observed)+'\n'+terminal)
    assert Antigravity.recover(attempt)['data']=={'ok':True}


def test_denied_tool_attempt_can_recover_but_executed_cannot(tmp_path):
    from reel_analysis.antigravity import Antigravity
    d=tmp_path/'attempt';d.mkdir();write_json(d/'input.json',{'files':[]})
    events=[{'event':'step_update','step_update':{'state':'DONE','tool_info':{'name':'manage_task','parameters':{}}}},{'event':'result','result':{'status':'SUCCESS','structured_output':{'ok':True}}}]
    (d/'raw.ndjson').write_text('\n'.join(json.dumps(x) for x in events)+'\n')
    with pytest.raises(ReelError,match='scope_not_verified'):Antigravity.recover(d)
    (d/'scope.jsonl').write_text(json.dumps({'tool':'manage_task','decision':'deny'})+'\n')
    assert Antigravity.recover(d)['data']=={'ok':True}


def test_backup_of_queued_job_can_continue(tmp_path,clip):
    s=Store(tmp_path/'data');job=s.submit(s.ingest(clip())['id']);s.backup(tmp_path/'snapshot')
    restored=Store.restore(tmp_path/'snapshot',tmp_path/'restored')
    assert restored.get(job['id'])['state']=='queued'
    Worker(restored,FixtureBackend()).run_once()
    assert restored.get(job['id'])['state']=='completed'


def test_network_playlist_is_not_a_video_input(tmp_path):
    s=Store(tmp_path/'data');f=tmp_path/'pretend.mp4';f.write_text('#EXTM3U\nhttps://example.invalid/private\n')
    with pytest.raises(ReelError,match='mp4_mov_or_webm_required'):s.ingest(f)


def test_crop_uses_original_frame_identity(tmp_path,clip):
    path=clip();info=media.inspect(path,Profile());out=tmp_path/'crop.jpg'
    receipt=media.crop_frame(path,out,info,3,{'x':.2,'y':.2,'width':.4,'height':.4})
    assert receipt['frame_index']==3 and receipt['pts_s']==info['frame_pts'][3]
    assert receipt['sha256']==file_hash(out)
    dims=json.loads(media.execute(['ffprobe','-v','error','-show_entries','stream=width,height','-of','json',str(out)]))['streams'][0]
    assert dims['width']==64 and dims['height']==96


def test_restore_rejects_unlisted_files(tmp_path):
    s=Store(tmp_path/'data');s.backup(tmp_path/'snapshot');(tmp_path/'snapshot/unlisted').write_text('unexpected')
    with pytest.raises(ReelError,match='backup_hash_mismatch'):Store.restore(tmp_path/'snapshot',tmp_path/'copy')


def test_scene_links_and_revalidation_do_not_spend_again(tmp_path,clip):
    s=Store(tmp_path/'data');job=s.submit(s.ingest(clip())['id']);backend=FixtureBackend();Worker(s,backend).run_once()
    old=s.result(job['id']);before=file_hash(old/'analysis.json')
    context=read_json(old/'analysis.json')
    data={'summary':'linked to actual scene','links':[{'speech_id':'sp1','event_id':'s1','relation':'during scene'}],'gaps':[]}
    assert validate_observation('reconcile',data,2,{'speech':context['speech'],'visual':context['visual']})['links']
    write_json(s.job_dir(job['id'])/'stages/reconcile/attempt/response.json',{'data':data,'usage':{'output_tokens':11}})
    version=s.revalidate(job['id']);backend.calls=[];Worker(s,backend).run_once()
    result=read_json(s.result(version['id'])/'analysis.json')
    assert result['reconciliation']['links'][0]['event_id']=='s1'
    assert not backend.calls
    assert file_hash(old/'analysis.json')==before
    assert result['revalidated_from']==job['id']


def test_failed_native_usage_is_not_discarded(tmp_path):
    from reel_analysis.pipeline import collect_usage
    stage=tmp_path/'stages/refine-1/attempt';stage.mkdir(parents=True)
    write_json(stage/'submitted.json',{'sent':True})
    (stage/'raw.ndjson').write_text(json.dumps({'event':'result','result':{'status':'ERROR','usage':{'output_tokens':123}}})+'\n')
    result=collect_usage(tmp_path)
    assert result[0]['outcome']=='native_ERROR'
    assert result[0]['usage']['output_tokens']==123


def test_visual_audio_disclaimer_is_not_a_speech_gap():
    value={'summary':'','layers':[],'scenes':[],'text_events':[],'visual_events':[],'gaps':[{'track':'speech','start_s':0,'end_s':2,'reason':'audio not assessed in visual pass'},{'track':'text','start_s':0,'end_s':2,'reason':'caption unreadable'}]}
    result=validate_observation('visual',value,2)
    assert [x['track'] for x in result['gaps']]==['text']
    assert len(value['gaps'])==2


def test_default_state_does_not_live_in_checkout(tmp_path,monkeypatch):
    from reel_analysis.cli import parser
    monkeypatch.delenv('REEL_DATA_DIR',raising=False)
    monkeypatch.setenv('XDG_STATE_HOME',str(tmp_path/'state'))
    assert Path(parser().parse_args(['doctor']).data_dir)==tmp_path/'state/gemini-reel-analysis'
