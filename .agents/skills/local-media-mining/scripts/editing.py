#!/usr/bin/env python3
"""Short-video editing breakdown using the existing scoped media runner."""
import argparse
import array
import bisect
import fcntl
import html
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys

import media as m
from editing_report import render

VERSION = '1'
MODEL = 'gemini-3.8-flash-high'
AUDIO_MODEL = 'gemini-3.1-pro-high'


def inventory(source):
    info = m.probe(source)
    video = [s for s in info['streams'] if s['codec_type'] == 'video']
    audio = [s for s in info['streams'] if s['codec_type'] == 'audio']
    if len(video) != 1 or len(audio) > 1:
        raise m.MediaError('requires_one_video_and_at_most_one_audio_track')
    if info['duration'] > 90:
        raise m.MediaError('editing_v1_requires_selected_clip_at_most_90_seconds')
    if audio and int(audio[0]['sample_rate']) * audio[0]['channels'] * info['duration'] * 4 > 256_000_000:
        raise m.MediaError('decoded_audio_limit')
    return info


def envelope(samples, rate, channels, window=.01):
    """Presentation-aligned mixed-track amplitude; no semantic sound classification."""
    hop = max(1, round(rate * window))
    bins = []
    for start in range(0, len(samples), hop * channels):
        values = samples[start:start+hop*channels]
        bins.append({'time_s':start/channels/rate,
                     'end_s':(start+len(values))/channels/rate,
                     'rms':math.sqrt(math.fsum(v*v for v in values)/len(values)),
                     'peak':max(abs(v) for v in values)})
    rises = [max(0, b['rms']-(bins[i-1]['rms'] if i else 0)) for i,b in enumerate(bins)]
    floor = sorted(rises)[int(.95*(len(rises)-1))] if rises else 0
    candidates = [{'bin':i, 'time_s':bins[i]['time_s'], 'rise':r}
                  for i,r in enumerate(rises) if r > max(floor, .005)
                  and (i == 0 or r >= rises[i-1]) and (i+1 == len(rises) or r >= rises[i+1])]
    return {'sample_rate':rate, 'channels':channels, 'window_s':hop/rate,
            'bins':bins, 'energy_rises':candidates,
            'meaning':'Mixed-track energy changes; may be speech, music or effects. Not SFX detections.'}


def capture_measurements(source, info, out):
    out.mkdir(exist_ok=True)
    probe=json.loads(m.run_local(['ffprobe','-v','error','-show_entries',
        'format=start_time:frame=best_effort_timestamp_time','-select_streams','v:0','-of','json',str(source)]))
    origin=float(probe['format'].get('start_time',0))
    pts=[float(f['best_effort_timestamp_time'])-origin for f in probe['frames']]
    raw=subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(source),'-map','0:v:0',
        '-vf','scale=64:112,format=gray','-fps_mode','passthrough','-f','rawvideo','-'],
        capture_output=True,timeout=120,check=True).stdout
    size=64*112
    if len(raw)//size != len(pts) or len(raw)%size:
        raise m.MediaError('decoded_frame_pts_count_mismatch')
    changes=[]
    for i in range(1,len(pts)):
        prev=raw[(i-1)*size:i*size];cur=raw[i*size:(i+1)*size]
        changes.append({'frame':i,'time_s':pts[i],
                        'luma_difference':sum(abs(a-b) for a,b in zip(prev,cur))/size/255})
    result={'source_sha256':m.sha(source),'source_origin_s':origin,'frame_pts':pts,
            'visual_changes':changes,'audio':None,
            'method':'ffprobe decoded-frame PTS; 64x112 luma MAD per adjacent original frame; audio PCM float32 with timestamp gaps padded by aresample; no semantic detections'}
    if info['audio']:
        stream=next(s for s in info['streams'] if s['codec_type']=='audio')
        cmd=['ffmpeg','-nostdin','-v','error','-copyts','-start_at_zero','-i',str(source),
             '-map','0:a:0','-af','aresample=async=1:first_pts=0','-c:a','pcm_f32le','-f','f32le','-']
        raw=subprocess.run(cmd,capture_output=True,timeout=120,check=True).stdout
        samples=array.array('f');samples.frombytes(raw)
        if sys.byteorder != 'little':samples.byteswap()
        if not samples or not all(math.isfinite(x) for x in samples):
            raise m.MediaError('invalid_audio_decode')
        result['audio']=envelope(samples,int(stream['sample_rate']),stream['channels'])
    m.save(out/'measurements.json',result)
    return result


def verify_measurements(bundle):
    receipt=json.loads((bundle/'measurements-receipt.json').read_text())
    data=json.loads((bundle/'measurements/measurements.json').read_text())
    if m.sha(bundle/'measurements/measurements.json') != receipt['sha256']:
        raise m.MediaError('measurement_hash_mismatch')
    if data['source_sha256'] != json.loads((bundle/'manifest.json').read_text())['source_sha256']:
        raise m.MediaError('measurement_source_mismatch')
    return data


def pass_data(path):
    report=m.verify(path)
    if report['completed_parts'] != 1 or report['total_parts'] != 1:
        return None,report
    attempt=path/report['parts'][0]['accepted_attempt']
    return json.loads((attempt/'result.json').read_text()),report


def assemble(manifest, passes, measurements):
    data={'schema_version':VERSION, 'source':manifest, 'scenes':[], 'layers':[],
          'visual_events':[], 'audio_events':[], 'links':[], 'style_summary':[],
          'coverage':{}, 'uncertainties':[], 'passes':{}, 'measurements':measurements,
          'status':'partial', 'reviews':[], 'evidence':[]}
    for name,entry in passes.items():
        result=entry.get('data');data['passes'][name]={'job':entry['job'],'state':entry['state']}
        if not result:continue
        data['coverage'][name]=dict(result['coverage']);
        if name=='audio':
            data['coverage'][name].update(perception_basis=result.get('perception_basis'),sound_status=result.get('sound_status'))
        data['uncertainties']+=result['uncertainties']
        data['style_summary']+=result.get('style_summary',[])
        for src,dst in [('scenes','scenes'),('layers','layers'),('events','visual_events'),('audio_events','audio_events')]:
            for value in result.get(src,[]):
                data[dst].append({**value,'status':'model_observation','origin_pass':name,'origin_job':entry['job']})
        data['links'] += [{**value,'status':'hypothesis','origin_job':entry['job']} for value in result.get('links',[])]
    for event in data['visual_events']+data['audio_events']:
        a,b=event.get('start_s'),event.get('end_s')
        event['scene_ids']=[s['id'] for s in data['scenes'] if a is not None and s.get('start_s') is not None and ((a==b and s['start_s']<=a<s['end_s']) or (a<b and a<s['end_s'] and b>s['start_s']))]
        event['scene_association_basis']='overlap of model intervals; not verified boundaries'
    vids={e['id']:e for e in data['visual_events']};aids={e['id']:e for e in data['audio_events']}
    for link in data['links']:
        if link.get('visual_event_id') not in vids or link.get('audio_event_id') not in aids:
            raise m.MediaError('cross_pass_event_reference_missing')
        v,a=vids[link['visual_event_id']],aids[link['audio_event_id']]
        link['estimated_start_delta_ms']=None if v.get('start_s') is None or a.get('start_s') is None else round((a['start_s']-v['start_s'])*1000,1)
        link['delta_basis']='model timestamps; not measured synchronization'
    expected={'map','visual'}|({'audio'} if manifest['media']['audio'] else set())
    if all(passes.get(n,{}).get('data') is not None for n in expected):data['status']='draft'
    # Unreported map gaps remain explicit; timestamps in bounds do not imply full coverage.
    intervals=sorted((s['start_s'],s['end_s']) for s in data['scenes'] if s.get('start_s') is not None)
    cursor=0.;gaps=[]
    for a,b in intervals:
        if a>cursor+.1:gaps.append({'start_s':cursor,'end_s':a})
        cursor=max(cursor,b)
    if manifest['media']['duration']>cursor+.1:gaps.append({'start_s':cursor,'end_s':manifest['media']['duration']})
    data['coverage']['computed_map_gaps']=gaps
    data['coverage']['semantic_completeness']='unverified'
    return data


def evidence_packets(bundle,data,maximum=6):
    source=Path(data['source']['source']);pts=data['measurements']['frame_pts']
    visuals={v['id']:v for v in data['visual_events']}
    picks=[]
    for link in data['links']:
        v=visuals[link['visual_event_id']]
        if v.get('start_s') is not None and v['id'] not in {x['id'] for x in picks}:picks.append(v)
    for v in data['visual_events']:
        if v.get('animation') and v.get('start_s') is not None and v['id'] not in {x['id'] for x in picks}:picks.append(v)
    for v in picks[:maximum]:
        folder=bundle/'evidence'/v['id'];folder.mkdir(parents=True,exist_ok=True)
        center=v['start_s'];start=max(0,center-.6);end=min(data['source']['media']['duration'],center+1)
        frames=[]
        for offset in (-.12,0,.12):
            ix=min(len(pts)-1,max(0,bisect.bisect_left(pts,max(0,center+offset))))
            target=folder/f'frame-{ix:06d}.jpg'
            if not target.exists():m.run_local(['ffmpeg','-nostdin','-v','error','-i',str(source),'-vf',f'select=eq(n\\,{ix})','-frames:v','1','-q:v','2','-y',str(target)])
            frames.append({'frame':ix,'pts_s':pts[ix],'path':str(target.relative_to(bundle)),'sha256':m.sha(target)})
        clip=folder/'clip.mp4'
        if not clip.exists():m.run_local(['ffmpeg','-nostdin','-v','error','-ss',str(start),'-i',str(source),'-t',str(end-start),'-c:v','libx264','-crf','18','-c:a','aac','-b:a','192k','-y',str(clip)])
        data['evidence'].append({'visual_event_id':v['id'],'start_s':start,'end_s':end,'frames':frames,
                                 'clip':str(clip.relative_to(bundle)),'clip_sha256':m.sha(clip),
                                 'meaning':'Source frames around model candidate; derived clip; event timing not verified'})


def apply_reviews(data, review):
    valid={e['id'] for k in ('visual_events','audio_events','links') for e in data[k]}
    if review.get('source_sha256') != data['source']['source_sha256']:
        raise m.MediaError('review_source_mismatch')
    result=[]
    for item in review.get('reviews',[]):
        if item.get('event_id') not in valid or item.get('method') not in ('frame_inspection','listening','signal_measurement'):
            raise m.MediaError('invalid_review_reference_or_method')
        if item.get('aspect') not in ('timing','appearance','mixed_signal','audible_identity','synchronization'):
            raise m.MediaError('review_aspect_required')
        if item.get('status') not in ('measured','hypothesis','unknown') or not item.get('note'):
            raise m.MediaError('invalid_review_status')
        if not item.get('reviewer') or not isinstance(item.get('evidence'),list) or not item['evidence']:
            raise m.MediaError('review_evidence_required')
        for evidence in item['evidence']:
            if evidence.get('kind')=='frame':
                n=evidence.get('index')
                if type(n) is not int or not 0<=n<len(data['measurements']['frame_pts']):raise m.MediaError('review_frame_out_of_bounds')
            elif evidence.get('kind')=='audio_bin':
                n=evidence.get('index');a=data['measurements']['audio']
                if a is None or type(n) is not int or not 0<=n<len(a['bins']):raise m.MediaError('review_bin_out_of_bounds')
            else:raise m.MediaError('invalid_review_evidence')
        if item['status']=='measured' and item['method']=='listening' and not item.get('timing_note'):
            raise m.MediaError('listening_is_not_automatic_precise_alignment')
        if item['status']=='measured' and item['aspect']=='timing':
            interval=item.get('corrected_interval',{})
            if not m.profile_contract().valid_time(interval,data['source']['media']['duration']) or interval.get('start_s') is None:
                raise m.MediaError('measured_timing_interval_required')
        if item['status']=='measured' and item['aspect']=='synchronization':
            if item['method']!='listening' or {x['kind'] for x in item['evidence']}!={'frame','audio_bin'}:
                raise m.MediaError('synchronization_requires_listening_and_both_timebases')
        if item['aspect']=='audible_identity' and item['method']!='listening':
            raise m.MediaError('sound_identity_requires_listening')
        result.append(item)
    data['reviews']=result


def build(bundle, packets=True):
    m.guard_tree(bundle)
    manifest=json.loads((bundle/'manifest.json').read_text())
    if m.sha(manifest['source'])!=manifest['source_sha256']:raise m.MediaError('source_changed')
    passes={}
    state=json.loads((bundle/'passes.json').read_text()) if (bundle/'passes.json').exists() else {}
    for name,path in state.items():
        content,receipt=pass_data(Path(path));passes[name]={'job':path,'data':content,'state':receipt['state']}
    data=assemble(manifest,passes,verify_measurements(bundle))
    if (bundle/'stop.json').exists():
        data['stop']=json.loads((bundle/'stop.json').read_text())
        data['uncertainties'].append('Execution stopped: '+data['stop']['reason'])
    if packets:evidence_packets(bundle,data)
    data['quality_notes']=[]
    if (bundle/'quality-notes.json').exists():
        quality=json.loads((bundle/'quality-notes.json').read_text())
        if quality.get('source_sha256')!=manifest['source_sha256']:raise m.MediaError('quality_notes_source_mismatch')
        for note in quality.get('notes',[]):
            if not note.get('reviewer') or not note.get('note') or note.get('status') not in ('needs_review','checked_samples'):
                raise m.MediaError('invalid_quality_note')
        data['quality_notes']=quality['notes']
        if any(x['status']=='needs_review' for x in quality['notes']):data['status']='needs_review'
    if (bundle/'reviews.json').exists():apply_reviews(data,json.loads((bundle/'reviews.json').read_text()))
    m.save(bundle/'breakdown.json',data);render(bundle,data)
    m.save(bundle/'outputs.json',{'source_sha256':manifest['source_sha256'],'hashes':{n:m.sha(bundle/n) for n in ('breakdown.json','review.html','analysis.md')}})
    return data


def run(args):
    source=Path(args.input).expanduser().resolve(strict=True);info=inventory(source)
    base=Path(args.output).expanduser().resolve()
    if any((p/'.git').exists() for p in [base,*base.parents]):raise m.MediaError('output_must_be_outside_git')
    manifest={'schema_version':VERSION,'source':str(source),'source_sha256':m.sha(source),
              'source_version':args.source_version,'media':info,'model':args.model}
    if getattr(args,'audio_model',None):manifest['audio_model']=args.audio_model
    bundle=base/m.digest(manifest)[:24];bundle.mkdir(parents=True,exist_ok=True);m.guard_tree(bundle)
    with (bundle/'.editing.lock').open('w') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise m.MediaError('editing_job_already_running')
        if not (bundle/'manifest.json').exists():m.save(bundle/'manifest.json',manifest)
        elif json.loads((bundle/'manifest.json').read_text())!=manifest:raise m.MediaError('bundle_manifest_mismatch')
        if not (bundle/'measurements-receipt.json').exists():
            capture_measurements(source,info,bundle/'measurements')
            m.save(bundle/'measurements-receipt.json',{'sha256':m.sha(bundle/'measurements/measurements.json')})
        verify_measurements(bundle)
        reuse=Path(args.reuse_bundle).expanduser().resolve() if getattr(args,'reuse_bundle',None) else bundle
        if reuse!=bundle and not getattr(args,'only_pass',None):raise m.MediaError('reuse_bundle_requires_only_pass')
        m.guard_tree(reuse)
        previous=json.loads((reuse/'passes.json').read_text()) if (reuse/'passes.json').exists() else {}
        state={};contexts={}
        if (bundle/'stop.json').exists():(bundle/'stop.json').unlink()
        for name in ('map','visual','audio'):
            if name=='audio' and not info['audio']:continue
            context=''
            if name=='visual':context=json.dumps({'scene_map':contexts.get('map',{}).get('scenes',[])},ensure_ascii=False)
            if name=='audio':context=json.dumps({'visual_events':[{k:e.get(k) for k in ('id','start_s','end_s','description','text','animation')} for e in contexts.get('visual',{}).get('events',[])]},ensure_ascii=False)
            options=argparse.Namespace(input=str(source),mode='analyze',profile='editing-'+name,
                focus=context,model=(getattr(args,'audio_model',None) or args.model) if name=='audio' else args.model,representation='json',start=None,end=None,chunk_seconds=90,
                overlap=0,output=str(bundle/'runs'),retry_failed=args.retry_failed,max_parts=1,
                timeout=args.timeout,min_quota=args.min_quota)
            try:
                if getattr(args,'only_pass',None) and name != args.only_pass:
                    if name not in previous:raise m.MediaError('requested_reuse_pass_missing:'+name)
                    prior=Path(previous[name]);prior_plan=json.loads((prior/'manifest.json').read_text())
                    expected=m.build_plan(options)
                    for key in ('source_sha256','model','profile','focus','spans','representation'):
                        if prior_plan.get(key)!=expected.get(key):raise m.MediaError('reuse_pass_parameters_changed:'+name+':'+key)
                    content,receipt=pass_data(prior)
                    if content is None:raise m.MediaError('requested_reuse_pass_not_intact:'+name)
                    contexts[name]=content;state[name]=str(prior);m.save(bundle/'passes.json',state)
                    continue
                receipt=m.work(options)
            except (m.MediaError,ValueError,OSError,subprocess.SubprocessError) as exc:
                m.save(bundle/'passes.json',state)
                m.save(bundle/'stop.json',{'pass':name,'reason':str(exc)[:500]})
                break
            state[name]=receipt['job'];m.save(bundle/'passes.json',state)
            content,_=pass_data(Path(receipt['job']))
            if content is None:
                m.save(bundle/'stop.json',{'pass':name,'reason':'pass_failed_or_incomplete','job':receipt['job']})
                break
            contexts[name]=content
        data=build(bundle)
        print(json.dumps({'bundle':str(bundle),'status':data['status'],'passes':data['passes'],
                          'visual_events':len(data['visual_events']),'audio_events':len(data['audio_events']),
                          'links':len(data['links'])},ensure_ascii=False),flush=True)
        return 0 if data['status']=='draft' else 2


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('run');p.add_argument('input');p.add_argument('--output',required=True)
    p.add_argument('--model',default=MODEL);p.add_argument('--source-version',default=None)
    p.add_argument('--audio-model',default=AUDIO_MODEL,help='Sound pass model; default Gemini 3.1 Pro High, chosen after the controlled audio pilot')
    p.add_argument('--reuse-bundle',default=None,help='With --only-pass, reuse compatible intact passes from an existing bundle')
    p.add_argument('--only-pass',choices=['map','visual','audio'],help='Explicitly regenerate one pass; reuse intact other passes with matching source/model/context, preserving old implementation provenance')
    p.add_argument('--retry-failed',action='store_true');p.add_argument('--timeout',type=int,default=300)
    p.add_argument('--min-quota',type=float,default=.1)
    p=sub.add_parser('build');p.add_argument('bundle');p.add_argument('--no-packets',action='store_true')
    args=parser.parse_args()
    try:
        if args.command=='run':
            if args.timeout<10 or not 0<=args.min_quota<=1:raise m.MediaError('invalid_run_limits')
            return run(args)
        d=build(Path(args.bundle).resolve(),not args.no_packets);print(d['status']);return 0
    except (m.MediaError,ValueError,OSError,KeyError,subprocess.SubprocessError) as exc:
        print(json.dumps({'error':str(exc)[:600]},ensure_ascii=False),file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
