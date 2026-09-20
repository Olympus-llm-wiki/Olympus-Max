#!/usr/bin/env python3
"""Create a release from its verified allowlist, never from a recursive HOME scan."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tarfile
import tempfile

ROOT=Path(__file__).resolve().parents[1]

def pack(root,output):
    root=root.resolve(); output=output.resolve()
    if output.exists(): raise ValueError('output_exists')
    spec=importlib.util.spec_from_file_location('distribution',root/'scripts/verify-distribution.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    check=m.verify(root)
    if check['state']!='verified': raise ValueError('distribution_not_verified')
    manifest=json.loads((root/'DISTRIBUTION.json').read_text())
    forbidden={'.env','.olympus-local.json','starter.local.json','.mcp.json','.git','auth.json','registry.sqlite3','.venv','node_modules','__pycache__','.pytest_cache'}
    paths=sorted(set(manifest['files'])|{'DISTRIBUTION.json'})
    for name in paths:
        parts=Path(name).parts
        if any(p in forbidden or p.startswith('.env.') for p in parts) or name.startswith(('.codex/','.serena/')):
            raise ValueError('private_file_in_distribution')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent,prefix='.olympus-release-',delete=False) as f: temporary=Path(f.name)
    try:
        with tarfile.open(temporary,'w:gz',format=tarfile.USTAR_FORMAT) as tar:
            for name in paths+['.claude/skills']:
                p=root/name
                info=tar.gettarinfo(str(p),arcname=root.name+'/'+name)
                info.uid=info.gid=0;info.uname=info.gname='';info.mtime=0
                if info.isfile():
                    with p.open('rb') as f: tar.addfile(info,f)
                elif info.issym() and name=='.claude/skills' and info.linkname=='../.agents/skills': tar.addfile(info)
                else: raise ValueError('unsupported_distribution_member')
        os.link(temporary,output)
    finally: temporary.unlink(missing_ok=True)
    return {'archive':str(output),'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'bytes':output.stat().st_size,'files':len(paths)+1}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True,type=Path);p.add_argument('--root',default=ROOT,type=Path)
    a=p.parse_args();print(json.dumps(pack(a.root,a.output),indent=2))
if __name__=='__main__':main()
