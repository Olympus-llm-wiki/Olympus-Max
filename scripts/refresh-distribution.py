#!/usr/bin/env python3
"""Refresh hashes from the reviewed Git index, excluding machine-local files."""
import hashlib
import json
from pathlib import Path
import subprocess
ROOT=Path(__file__).resolve().parents[1]
EXCLUDE={'DISTRIBUTION.json','VERIFICATION.json','.claude/skills'}

def main():
    names=subprocess.check_output(['git','-C',str(ROOT),'ls-files','-z']).decode().split('\0')[:-1]
    files={}
    for name in sorted(names):
        if name in EXCLUDE:continue
        parts=Path(name).parts
        if name.startswith(('.codex/','.serena/')) or any(p in {'.env','starter.local.json','.olympus-local.json','.mcp.json','.venv','node_modules','__pycache__','.pytest_cache'} or p.startswith('.env.') for p in parts):
            raise SystemExit('private_file_in_index:'+name)
        blob=subprocess.check_output(['git','-C',str(ROOT),'show',':'+name])
        if (ROOT/name).is_symlink() or (ROOT/name).read_bytes()!=blob:raise SystemExit('index_worktree_mismatch:'+name)
        files[name]=hashlib.sha256(blob).hexdigest()
    template=json.loads((ROOT/'TEMPLATE.json').read_text())
    (ROOT/'DISTRIBUTION.json').write_text(json.dumps({'schema':1,'version':template['distribution_version'],'files':files},indent=2)+'\n')
    print(json.dumps({'files':len(files),'version':template['distribution_version']}))
if __name__=='__main__':main()
