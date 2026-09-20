#!/usr/bin/env python3
"""Local project/workspace commands, available in both Olympus editions."""
import argparse
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'src'))
from olympus.preservation import Store,PreservationError
from olympus import workspace_cli
import environment

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state',type=Path)
    workspace_cli.add_parsers(p.add_subparsers(dest='command',required=True))
    args=p.parse_args(argv)
    try:
        state=args.state or Path(environment.load(ROOT)['state'])/'operations'
        result=workspace_cli.run(Store(state),args)
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except (PreservationError,environment.EnvironmentError) as e:
        print(json.dumps({'error':str(e)},ensure_ascii=False));return 2
if __name__=='__main__': raise SystemExit(main())
