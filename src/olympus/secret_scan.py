"""Bounded streaming implementation of the current complete credential policy.

Only counters, literal matcher states and a short in-memory boundary are kept.
Checkpoint state excludes source text; the boundary is reconstructed from source.
Unknown future policies fail closed until their streaming state is implemented.
"""
from __future__ import annotations
import re

from .preservation import PreservationError, _SECRET_PATTERNS, canonical, digest

SUPPORTED_POLICY = "8bcf7b547c09537fdfc5c2395348fda6a2120e5c156a8fddcf6c6c64a1e2c5fe"
_PREFIX = re.compile(
    r"(?P<begin>-----BEGIN )|(?P<end>-----END )|"
    r"(?P<token>\b(?:sk-|gh[pousr]_|github_pat_))|(?P<bearer>(?i:\bBearer))|"
    r"(?P<key>(?i:access_token|refresh_token|id_token|client_secret|BWS_ACCESS_TOKEN|password))|"
    r"(?P<url>(?i:\b(?:https?|postgres(?:ql)?|mysql|redis|amqp|mongodb(?:\+srv)?)://))|"
    r"(?P<age>(?i:\bAGE-SECRET-KEY-))")
_SPACE = re.compile(r"\s*")
_TOKEN = re.compile(r"[A-Za-z0-9_\-]*")
_AGE = re.compile(r"[A-Z0-9]*", re.I)
_BEARER = re.compile(r"[A-Za-z0-9._~+/-]*")
_VALUE = re.compile(r"[^\s\"',}]*")
_USER_END = re.compile(r"[\s/@:]")
_PASSWORD_END = re.compile(r"[\s/@]")
_UPPER = re.compile(r"[A-Z]*")
_DASH_BOUNDARY = re.compile(r"[A-Za-z0-9_]-")


def _word(char):
    return bool(char) and (char.isalnum() or char == "_")


def _consume(kind, state, text, eof=False):
    pos, phase = 0, state.get("phase", 0)
    if kind in {"begin", "end"}:
        if phase == 0:
            run = _UPPER.match(text).group()
            word = state.get("word", "")
            candidate = word + run
            state["word"] = candidate if "PRIVATE".startswith(candidate) else "other"
            state["count"] = state.get("count", 0) + len(run)
            pos = len(run)
            if pos == len(text):
                return "pending"
            if text[pos] != " " or not state["count"]:
                return "discard"
            state["suffixes"] = ["PRIVATE KEY-----"] + (["KEY-----"] if state["word"] == "PRIVATE" else [])
            state["phase"] = 1
            pos += 1
        suffixes = []
        for suffix in state["suffixes"]:
            body = text[pos:pos + len(suffix)]
            if suffix.startswith(body):
                if len(body) == len(suffix):
                    return "match"
                suffixes.append(suffix[len(body):])
        state["suffixes"] = suffixes
        return "pending" if suffixes else "discard"
    if kind == "key":
        while True:
            phase = state.get("phase", 0)
            if phase in (0, 3):
                if pos == len(text):
                    return "pending"
                if text[pos] in "\"'":
                    pos += 1
                state["phase"] = phase + 1
            elif phase in (1, 2):
                pos = _SPACE.match(text, pos).end()
                if pos == len(text):
                    return "pending"
                if phase == 1:
                    if text[pos] not in ":=":
                        return "discard"
                    pos += 1
                state["phase"] = phase + 1
            else:
                end = _VALUE.match(text, pos).end()
                state["count"] = state.get("count", 0) + end - pos
                if state["count"] >= 12:
                    return "match"
                return "pending" if end == len(text) else "discard"
    if kind == "url":
        while True:
            phase = state.get("phase", 0)
            match = (_USER_END if phase == 0 else _PASSWORD_END).search(text, pos)
            end = match.start() if match else len(text)
            state["count"] = state.get("count", 0) + end - pos
            if not match:
                return "pending"
            if not state["count"]:
                return "discard"
            if phase == 0 and text[end] == ":":
                state.update(phase=1, count=0); pos = end + 1
            else:
                return "match" if phase == 1 and text[end] == "@" else "discard"
    if kind == "bearer":
        if phase < 2:
            pos = _SPACE.match(text).end()
            if phase == 0 and not pos:
                return "pending" if not text else "discard"
            if pos == len(text):
                state["phase"] = 1
                return "pending"
            state["phase"] = 2
        end = _BEARER.match(text, pos).end()
        state["count"] = state.get("count", 0) + end - pos
        return "match" if state["count"] >= 20 else "pending" if end == len(text) else "discard"
    minimum = 20 if kind == "token" else 30
    run = (_TOKEN if kind == "token" else _AGE).match(text).group()
    count = state.get("count", 0)
    if kind == "token":
        if count >= minimum and state.get("last_word") and run.startswith("-"):
            return "match"
        if _DASH_BOUNDARY.search(run, max(0, minimum - count - 1)):
            return "match"
    count += len(run)
    last_word = _word(run[-1]) if run else state.get("last_word", False)
    state.update(count=count, last_word=last_word)
    if len(run) < len(text):
        return "match" if count >= minimum and last_word and not _word(text[len(run)]) else "discard"
    if eof:
        return "match" if count >= minimum and last_word else "discard"
    return "pending"


class StreamingSecretGuard:
    def __init__(self, state=None, *, previous_text="", before=""):
        if digest(canonical([[p.pattern, p.flags] for p in _SECRET_PATTERNS])) != SUPPORTED_POLICY:
            raise PreservationError("credential_scan_policy_unsupported")
        state = state or {}
        self.pending = state.get("pending", [])
        self.private_active = state.get("private_active", False)
        self.tail = previous_text[-64:]
        self.before = previous_text[-65] if len(previous_text) > 64 else before

    def _result(self, kind, state, text, eof=False):
        result = _consume(kind, state, text, eof)
        if result == "match":
            if kind == "begin":
                self.private_active = True
            elif kind != "end" or self.private_active:
                raise PreservationError("credential_pattern_detected")
        return result

    def feed(self, text):
        combined = self.tail + text
        # Complete within-window matches retain the exact original regex policy.
        for index, pattern in enumerate(_SECRET_PATTERNS):
            for match in pattern.finditer(combined):
                if match.start() == 0 and index in (1, 2, 4, 5) and _word(self.before):
                    continue
                if index in (1, 5) and match.end() == len(combined):
                    continue  # EOF is not a word boundary until finish().
                raise PreservationError("credential_pattern_detected")
        pending = []
        for kind, state in self.pending:
            if self._result(kind, state, text) == "pending":
                pending.append([kind, state])
        for match in _PREFIX.finditer(combined):
            if match.end() <= len(self.tail):
                continue
            kind = match.lastgroup
            if match.start() == 0 and kind in {"token", "bearer", "url", "age"} and _word(self.before):
                continue
            state = {}
            if self._result(kind, state, combined[match.end():]) == "pending":
                pending.append([kind, state])
        if len(pending) > 128:
            raise PreservationError("credential_scan_complexity_limit")
        self.pending = pending
        if len(combined) > 64:
            self.before = combined[-65]
        self.tail = combined[-64:]

    def finish(self):
        for kind, state in self.pending:
            self._result(kind, state, "", eof=True)

    def checkpoint(self):
        # Contains only counters, flags and known matcher literals, never tail.
        return {"pending": self.pending, "private_active": self.private_active}
