#!/bin/bash
# Notebook audio preparation. Preserve originals and record an input/output map.
set -euo pipefail
exec python3 - "$@" <<'PY'
import hashlib,json,subprocess,sys
from pathlib import Path
if len(sys.argv)!=3:
    raise SystemExit('usage: to-audio.sh SOURCE_FOLDER OUTPUT_FOLDER')
src,out=map(lambda s:Path(s).expanduser().resolve(),sys.argv[1:])
if src==out or src in out.parents:
    raise SystemExit('output must be outside source tree')
out.mkdir(parents=True,exist_ok=True)
if (out/'manifest.json').is_symlink():raise SystemExit('symlink manifest refused')
def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1048576),b''):h.update(b)
    return h.hexdigest()
records=[]
for path in sorted(src.rglob('*')):
    if not path.is_file() or path.suffix.lower() not in {'.mp4','.mov','.mkv','.webm','.m4a','.mp3','.wav','.flac'}:continue
    identity=sha(path);target=out/(identity+'.m4a');receipt=out/(identity+'.json')
    record={'source':str(path),'source_sha256':identity,'output':str(target),'transformation':'aac32k_mono_22050'}
    if target.is_symlink() or receipt.is_symlink():raise SystemExit('symlink output refused')
    if target.exists():
        if not receipt.exists() or json.loads(receipt.read_text()).get('output_sha256')!=sha(target):
            raise SystemExit('existing output lacks matching receipt: '+str(target))
        record.update(status='reused',output_sha256=sha(target))
    else:
        p=subprocess.run(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-i',str(path),'-vn',
            '-c:a','aac','-b:a','32k','-ac','1','-ar','22050',str(target)])
        if p.returncode:raise SystemExit('conversion failed; partial file preserved: '+str(target))
        record.update(status='prepared',output_sha256=sha(target))
        receipt.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
    records.append(record)
(out/'manifest.json').write_text(json.dumps(records,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'files':len(records),'manifest':str(out/'manifest.json')},ensure_ascii=False))
PY
