"""Standalone Antigravity PreToolUse gate. Does not import the parent application."""
import json
import os
from pathlib import Path
import sys


def decide(payload, allowed):
    tool = payload.get("toolCall", {})
    if tool.get("name") == "finish":
        return "allow"
    if tool.get("name") != "view_file":
        return "deny"
    value = tool.get("args", {}).get("AbsolutePath")
    return "allow" if isinstance(value, str) and Path(value).is_absolute() and str(Path(value).resolve()) in allowed else "deny"


def main():
    decision, name = "deny", "unknown"
    try:
        payload = json.loads(sys.stdin.read(1_000_000))
        name = payload.get("toolCall", {}).get("name", "unknown")
        allowed = json.loads(os.environ["REEL_ALLOWED_FILES"])
        decision = decide(payload, allowed)
    except (ValueError, KeyError, OSError, TypeError):
        pass
    try:
        with open(os.environ["REEL_SCOPE_LOG"], "a") as log:
            log.write(json.dumps({"tool": name, "decision": decision}) + "\n")
            log.flush()
            os.fsync(log.fileno())
    except (KeyError, OSError):
        decision = "deny"
    print(json.dumps({"decision": decision, "reason": "Assigned analysis inputs only"}))


if __name__ == "__main__":
    main()
