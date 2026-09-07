"""Antigravity PreToolUse hook: allow only the assigned file and terminal finish."""
import json
import os
from pathlib import Path
import sys


def decide(payload, allowed):
    tool = payload.get('toolCall', {})
    if tool.get('name') == 'finish':
        return 'allow'
    if tool.get('name') != 'view_file':
        return 'deny'
    path = tool.get('args', {}).get('AbsolutePath')
    if not isinstance(path, str) or not Path(path).is_absolute():
        return 'deny'
    return 'allow' if Path(path).resolve() == Path(allowed).resolve() else 'deny'


def main():
    decision = 'deny'
    name = 'unknown'
    try:
        payload = json.loads(sys.stdin.read(1_000_000))
        name = payload.get('toolCall', {}).get('name', 'unknown')
        decision = decide(payload, os.environ['MEDIA_ALLOWED_FILE'])
    except (ValueError, KeyError, TypeError, OSError):
        pass
    # Store only outcome, never arbitrary tool arguments or source content.
    try:
        with open(os.environ['MEDIA_BOUNDARY_LOG'], 'a') as log:
            log.write(json.dumps({'tool': name, 'decision': decision}) + '\n')
    except (KeyError, OSError):
        decision = 'deny'
    print(json.dumps({'decision': decision, 'reason': 'Scoped media reader boundary'}))


if __name__ == '__main__':
    main()
