#!/usr/bin/env python3
"""Generate a local preview; never activate Codex hooks automatically."""
import argparse
import json
import os
from pathlib import Path
import shlex
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--sessions-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    script = Path(__file__).resolve().parent / "codex-capture-hook.py"
    command = shlex.join([sys.executable, str(script), "--state-root", str(args.state_root.expanduser().resolve()),
                         "--sessions-root", str(args.sessions_root.expanduser().resolve())])
    hook = {"type": "command", "command": command, "timeout": 3}
    config = {"description": "Preview for one explicitly registered Codex task; no model calls.",
              "hooks": {"SessionStart": [{"matcher": "startup|resume|clear|compact", "hooks": [hook]}],
                        "Stop": [{"hooks": [hook]}]}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(config, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"preview": str(args.output), "activated": False}))


if __name__ == "__main__":
    main()
