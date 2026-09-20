"""Local review UI; model content is data and never executable markup."""
import html
import json
from pathlib import Path

PAGE = r'''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Разбор монтажа</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#101215;color:#e8ebef;font:16px/1.5 system-ui,sans-serif}header{padding:24px 32px;border-bottom:1px solid #353941}h1{margin:0 0 8px;font-size:30px}h2{font-size:20px}.muted{color:#a8b2bf;font-size:14px}.layout{display:grid;grid-template-columns:minmax(300px,390px) 1fr;gap:28px;padding:24px 32px;max-width:1500px;margin:auto}.player{position:sticky;top:16px;align-self:start}video{width:100%;max-height:58vh;background:#000;border-radius:12px}canvas{display:block;width:100%;height:120px;background:#191d24;cursor:crosshair;border:1px solid #3c4755;border-radius:8px;margin:12px 0}button,select{padding:8px 12px;background:#242c36;border:1px solid #516074;color:#eee;border-radius:6px;cursor:pointer}button:hover{background:#374558}.controls{display:flex;flex-wrap:wrap;gap:7px;align-items:center}article{padding:16px;border:1px solid #39424f;background:#191e26;border-radius:10px;margin:12px 0}article.active{border-color:#70c0a5}article h3{font-size:17px;margin:0 0 7px}.tag{display:inline-block;font-size:12px;border:1px solid #556170;padding:2px 7px;border-radius:8px;margin:0 6px 5px 0}.evidence{display:flex;gap:8px;overflow:auto}.evidence img{height:170px}.evidence a{color:#9be5c7}a{color:#9be5c7}.small{font-size:13px}details{margin:10px 0}summary{cursor:pointer}.legend{color:#ffd493}.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}.empty{padding:24px;color:#a8b2bf}@media(max-width:760px){.layout{display:block;padding:16px}.player{position:static}header{padding:20px}video{max-height:45vh}.grid{display:block}}
</style><header><h1>Разбор монтажа</h1><div id="summary"></div><div id="quality" class="legend"></div><div class="muted">Наблюдения модели и гипотезы связей требуют проверки. Волна показывает общую дорожку: её пик может принадлежать речи, музыке или эффекту.</div></header>
<main class="layout"><aside class="player"><video id="video" controls preload="metadata"></video><div class="controls"><button id="prev">← кадр</button><button id="next">кадр →</button><select id="speed" aria-label="Скорость"><option value="1">1×</option><option value=".5">0,5×</option><option value=".25">0,25×</option></select><span id="time">0,000 с</span></div><canvas id="wave" width="900" height="240" aria-label="Амплитуда звука; нажмите для перехода"></canvas><div class="muted">RMS и амплитуда, окно 10 мс. Голос, музыка и эффекты сведены.</div><div class="controls"><a href="analysis.md">Паспорт стиля</a><a href="breakdown.json">JSON</a></div><details><summary>Покрытие и ограничения</summary><pre id="coverage"></pre></details><details><summary>Слои и оформление</summary><div id="layers"></div></details><details><summary>Проверки по источнику</summary><div id="reviews"></div></details></aside>
<section><div class="controls"><select id="kind" aria-label="Тип событий"><option value="all">Все события</option><option value="scenes">Сцены</option><option value="visual">Картинка</option><option value="audio">Звук</option><option value="links">Связи</option></select><select id="layer" aria-label="Слой"><option value="">Все слои</option></select><select id="audioKind" aria-label="Тип звука"><option value="">Все звуки</option><option value="speech">Речь</option><option value="music">Музыка</option><option value="sfx">Эффекты</option><option value="ambience">Атмосфера</option><option value="silence">Тишина</option><option value="unknown">Не определено</option></select></div><div id="events"></div></section></main>
<script id="data" type="application/json">__DATA__</script><script>
const D=JSON.parse(document.getElementById('data').textContent), $=id=>document.getElementById(id),V=$('video');V.src=D.source.source_uri;
const dur=D.source.media.duration,pts=D.measurements.frame_pts,visual=new Map(D.visual_events.map(e=>[e.id,e])),audio=new Map(D.audio_events.map(e=>[e.id,e])),layers=new Map(D.layers.map(e=>[e.id,e]));
const fmt=x=>x==null?'не определено':x.toFixed(3)+' с';
function el(tag,text,cls){const n=document.createElement(tag);if(text!=null)n.textContent=String(text);if(cls)n.className=cls;return n}
function seek(t){if(t==null)return;V.currentTime=Math.max(0,Math.min(dur,t));V.pause();draw();}
$('summary').textContent=`${dur.toFixed(2)} с · ${D.layers.length} слоёв · ${D.visual_events.length} визуальных событий · ${D.audio_events.length} аудиособытий · ${D.links.length} гипотез связей · ${D.status}`;
for(const n of D.quality_notes||[])$('quality').append(el('p',n.note));
$('coverage').textContent=JSON.stringify({coverage:D.coverage,uncertainties:D.uncertainties,passes:D.passes},null,2);
for(const l of D.layers){const o=el('option',l.id+' · '+l.role);o.value=l.id;$('layer').append(o);const x=el('article');x.append(el('h3',l.id+' · '+l.role),el('p',l.description),el('pre',JSON.stringify(l.style,null,2)));$('layers').append(x)}
for(const r of D.reviews){const x=el('article');x.append(el('h3',r.event_id+' · '+r.aspect+' · '+r.status),el('p',r.note),el('pre',JSON.stringify(r,null,2)));$('reviews').append(x)}
if(!D.reviews.length)$('reviews').append(el('p','Проверенные аннотации ещё не добавлены.','muted'));
function draw(){const c=$('wave'),ctx=c.getContext('2d'),w=c.width,h=c.height;ctx.fillStyle='#191d24';ctx.fillRect(0,0,w,h);const bins=D.measurements.audio?.bins||[];let max=.01;for(const b of bins)max=Math.max(max,b.peak);ctx.strokeStyle='#638994';ctx.beginPath();for(const b of bins){const x=b.time_s/dur*w,a=b.peak/max*h*.44;ctx.moveTo(x,h/2-a);ctx.lineTo(x,h/2+a)}ctx.stroke();ctx.strokeStyle='#98e4c1';ctx.beginPath();for(const b of bins){const x=b.time_s/dur*w,a=b.rms/max*h*.44;ctx.moveTo(x,h/2-a);ctx.lineTo(x,h/2+a)}ctx.stroke();ctx.strokeStyle='#ffbd72';ctx.beginPath();ctx.moveTo(V.currentTime/dur*w,0);ctx.lineTo(V.currentTime/dur*w,h);ctx.stroke();$('time').textContent=fmt(V.currentTime)}
$('wave').onclick=e=>{const r=$('wave').getBoundingClientRect();seek((e.clientX-r.left)/r.width*dur)};
$('speed').onchange=()=>V.playbackRate=Number($('speed').value);
function frame(step){const t=V.currentTime;let i=step>0?pts.findIndex(x=>x>t+.0001):pts.findLastIndex(x=>x<t-.0001);if(i<0)i=step>0?pts.length-1:0;seek(pts[i])}$('prev').onclick=()=>frame(-1);$('next').onclick=()=>frame(1);V.ontimeupdate=draw;V.onloadedmetadata=draw;
const entries=[...D.scenes.map(e=>({...e,type:'scenes'})),...D.visual_events.map(e=>({...e,type:'visual'})),...D.audio_events.map(e=>({...e,type:'audio'})),...D.links.map(e=>({...e,type:'links',start_s:visual.get(e.visual_event_id)?.start_s,end_s:visual.get(e.visual_event_id)?.end_s}))].sort((a,b)=>(a.start_s??Infinity)-(b.start_s??Infinity));
function show(){const root=$('events');root.replaceChildren();let count=0;for(const e of entries){if($('kind').value!=='all'&&e.type!==$('kind').value)continue;if($('layer').value&&e.layer_id!==$('layer').value&&visual.get(e.visual_event_id)?.layer_id!==$('layer').value)continue;if($('audioKind').value&&e.kind!==$('audioKind').value&&audio.get(e.audio_event_id)?.kind!==$('audioKind').value)continue;count++;const a=el('article');a.append(el('span',e.type,'tag'),el('span',e.status,'tag'),el('h3',e.id+' · '+(e.kind||layers.get(e.layer_id)?.role||e.layout||e.relation||'')));const b=el('button',fmt(e.start_s)+' → '+fmt(e.end_s));b.disabled=e.start_s==null;b.onclick=()=>seek(e.start_s);a.append(b,el('p',e.description||e.explanation));if(e.text)a.append(el('p',e.text,'legend'));if(e.animation)a.append(el('p','Движение: '+e.animation));if(e.easing)a.append(el('p','Характер движения: '+e.easing));if(e.type==='links'){a.append(el('p',`${e.audio_event_id} ↔ ${e.visual_event_id}`),el('p','Разность стартов по оценке модели: '+(e.estimated_start_delta_ms??'?')+' мс. Это не измеренная синхронизация.','muted'));const x=audio.get(e.audio_event_id);if(x)a.append(el('p','Звук: '+x.description))}const ev=D.evidence.find(x=>x.visual_event_id===(e.visual_event_id||e.id));if(ev){const details=el('details');details.append(el('summary','Кадры исходника и фрагмент'));const row=el('div',null,'evidence');for(const f of ev.frames){const anchor=el('a');anchor.href=f.path;anchor.target='_blank';const img=el('img');img.src=f.path;img.loading='lazy';img.alt='Кадр '+f.frame+' · '+fmt(f.pts_s);anchor.append(img,el('div',fmt(f.pts_s)));row.append(anchor)}details.append(row);const link=el('a','Открыть фрагмент со звуком');link.href=ev.clip;details.append(link);a.append(details)}root.append(a)}if(!count)root.append(el('p','Нет событий для выбранного фильтра.','empty'))}
for(const id of ['kind','layer','audioKind'])$(id).onchange=show;show();draw();
</script></html>'''


def render(bundle, data):
    from media import write_text
    payload={**data,'source':{**data['source'],'source_uri':Path(data['source']['source']).as_uri()}}
    encoded=json.dumps(payload,ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    write_text(bundle/'review.html',PAGE.replace('__DATA__',encoded))
    lines=['# Разбор монтажа','',f'Статус: **{data["status"]}**. Наблюдения модели и предложенные связи требуют проверки.',
           f'Источник: [{Path(data["source"]["source"]).name}]({data["source"]["source"]})',
           f'SHA-256: `{data["source"]["source_sha256"]}`',
           f'Модель визуала: `{data["source"]["model"]}`; аудио: `{data["source"].get("audio_model",data["source"]["model"])}`. Длительность: {data["source"]["media"]["duration"]:.3f} с.',
           f'[Интерактивный таймлайн]({bundle}/review.html) · [JSON]({bundle}/breakdown.json)','',
           '## Паспорт стиля — наблюдения модели','']
    lines += ['**Проверка качества:** '+x['note'] for x in data.get('quality_notes',[])]
    lines += ['- '+str(x) for x in data['style_summary']]
    lines += ['', '## Слои', '', '| ID | Роль | Наблюдение |','|---|---|---|']
    clean=lambda x:str(x).replace('|','\\|').replace('\n',' ')
    lines += [f'| {l["id"]} | {l["role"]} | {clean(l["description"])} |' for l in data['layers']]
    lines += ['','## Звуковой ряд','']
    for e in data['audio_events']:
        lines.append(f'- **{e["id"]} / {e["kind"]}**, {e.get("start_s")}–{e.get("end_s")} с: {e["description"]}')
    lines += ['','## Предложенные связи','']
    for e in data['links']:
        lines.append(f'- {e["audio_event_id"]} ↔ {e["visual_event_id"]}: {e["explanation"]} Статус: hypothesis.')
    lines += ['','## Проверки источника','']
    lines += [f'- {r["event_id"]}: {r["note"]} ({r["aspect"]}; {r["method"]}; {r["status"]}).' for r in data['reviews']]
    if not data['reviews']:lines.append('Проверенные аннотации ещё не добавлены.')
    lines += ['','## Ограничения','',
              'RMS/амплитуда и PTS кадров измерены локально; энергетический пик не классифицирует звук. Времена событий модели приблизительные. Исходные дорожки и монтажные слои не восстановлены. Полнота семантического покрытия не подтверждена.',
              '', '```json',json.dumps({'coverage':data['coverage'],'uncertainties':data['uncertainties'],'passes':data['passes']},ensure_ascii=False,indent=2),'```']
    write_text(bundle/'analysis.md','\n'.join(lines)+'\n')
