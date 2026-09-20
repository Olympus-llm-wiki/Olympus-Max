#!/usr/bin/env python3
"""Exercise the installed Serena MCP and Python symbols without calling a model."""
import argparse
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]

def verify(root,output):
    cfg=json.loads((root/'.olympus-local.json').read_text())
    log=Path(cfg['state'])/'logs/serena-verification.log'
    with log.open('w') as errors:
        process=subprocess.Popen([cfg['python'],str(root/'environment.py'),'tool','serena'],
                                 stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=errors,
                                 text=True,bufsize=1,start_new_session=True,cwd=root)
        def send(method,params=None,ident=None):
            message={'jsonrpc':'2.0','method':method}
            if params is not None:message['params']=params
            if ident is not None:message['id']=ident
            process.stdin.write(json.dumps(message)+'\n');process.stdin.flush()
        def receive(ident,timeout=100):
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                ready,_,_=select.select([process.stdout],[],[],min(2,deadline-time.monotonic()))
                if not ready:continue
                line=process.stdout.readline()
                if not line:raise RuntimeError('mcp_process_exited')
                message=json.loads(line)
                if message.get('id')==ident:
                    if 'error' in message:raise RuntimeError('mcp_error')
                    return message['result']
            raise RuntimeError('mcp_timeout')
        try:
            send('initialize',{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'olympus-portable-check','version':'0.3.0'}},1)
            initialized=receive(1)
            send('notifications/initialized')
            send('tools/list',{},2)
            catalog=receive(2);names=[t['name'] for t in catalog['tools']]
            if 'get_symbols_overview' not in names:raise RuntimeError('symbol_tool_missing')
            send('tools/call',{'name':'get_symbols_overview','arguments':{'relative_path':'environment.py','depth':0}},3)
            result=receive(3)
            if result.get('isError') or 'EnvironmentError' not in json.dumps(result):raise RuntimeError('python_symbols_not_observed')
            receipt={'schema':1,'state':'verified','protocol':initialized['protocolVersion'],'tool_count':len(names),
                     'python_symbols_observed':True,'model_calls':0,'checked_at':time.time()}
            output.parent.mkdir(parents=True,exist_ok=True)
            with output.open('x') as f:json.dump(receipt,f,indent=2);f.write('\n')
            return receipt
        finally:
            try:os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',default=ROOT,type=Path);parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();print(json.dumps(verify(args.root.resolve(),args.output),indent=2))
