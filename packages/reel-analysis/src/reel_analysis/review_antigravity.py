"""Distinguish completed unusable output from genuinely unknown remote execution."""
import json

from .antigravity import Antigravity, UnknownOutcome
from .common import ReelError


class ReviewAntigravity(Antigravity):
    @staticmethod
    def recover(attempt):
        try:
            return Antigravity.recover(attempt)
        except UnknownOutcome:
            raw = attempt / "raw.ndjson"
            if raw.exists():
                terminal = None
                for line in raw.read_text().splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("event") == "result":
                        terminal = event.get("result")
                # A full terminal envelope proves the request ended, not that its content is correct.
                if isinstance(terminal, dict) and terminal.get("status") == "SUCCESS":
                    raise ReelError("antigravity_completed_response_invalid") from None
            raise
