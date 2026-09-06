#!/usr/bin/env python3
"""Quick project-local Codex signals; no transcript bodies or network calls.

The native payload contract is Codex 0.153.1 SessionStart/Stop. Only normalized
registration hints enter the spool. The daemon invokes --drain to recover a
signal if shutdown happened before SQLite registration completed.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from olympus.codex_capture import CODEX_VERSION, CaptureRegistration
from olympus.preservation import Store, PreservationError, canonical
from olympus.task_capture import register_task

MAX_PAYLOAD = 1024 * 1024
MAX_HEADER = 1024 * 1024
SCOPE = "olympus"


class HookError(Exception):
    """Static code only: payload text never becomes an exception message."""


class HookInterrupted(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _when(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise HookError("invalid_signal_time") from None
    return parsed.astimezone(timezone.utc).isoformat()


def _id(value: object) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise HookError("invalid_session_id") from None
    return value


def _exact_project(value: object, expected: Path) -> str:
    if (not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value)
            or not Path(value).is_absolute()):
        raise HookError("project_cwd_mismatch")
    if Path(value).resolve() != expected.resolve():
        raise HookError("project_cwd_mismatch")
    return str(expected.resolve())


def _transcript(value: object, sessions_root: Path) -> Path | None:
    if value is None:
        return None
    if (not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value)
            or not Path(value).is_absolute()):
        raise HookError("invalid_transcript_path")
    candidate = Path(value)
    base = Path(sessions_root).expanduser()
    if not base.is_absolute() or base.is_symlink():
        raise HookError("invalid_sessions_root")
    base = base.resolve()
    if ".." in candidate.parts or not candidate.is_relative_to(base):
        raise HookError("transcript_outside_sessions")
    current = base
    for part in candidate.relative_to(base).parts:
        current = current / part
        if current.is_symlink():
            raise HookError("transcript_symlink")
    if candidate.suffix != ".jsonl" or not candidate.name.startswith("rollout-"):
        raise HookError("invalid_transcript_path")
    return candidate


def _metadata(path: Path | None, thread_id: str, project_root: Path) -> bool:
    """Read only the first session metadata record, never a transcript tail."""
    if path is None:
        return False
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise HookError("transcript_not_regular")
            first = stream.readline(MAX_HEADER + 1)
    except FileNotFoundError:
        return False
    except OSError:
        raise HookError("transcript_unavailable") from None
    if len(first) > MAX_HEADER:
        raise HookError("session_metadata_size_limit")
    if not first.endswith(b"\n"):
        return False
    try:
        row = json.loads(first)
        if not isinstance(row, dict) or row.get("type") != "session_meta" or not isinstance(row.get("payload"), dict):
            raise ValueError
        meta = row["payload"]
    except (ValueError, UnicodeError, RecursionError):
        raise HookError("invalid_session_metadata") from None
    if meta.get("id") != thread_id:
        raise HookError("session_metadata_id_mismatch")
    _exact_project(meta.get("cwd"), project_root)
    if meta.get("cli_version") != CODEX_VERSION:
        raise HookError("unsupported_codex_version")
    return True


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _spool_directory(state_root: Path) -> Path:
    state_root = Path(state_root).expanduser()
    if not state_root.is_absolute() or "CloudStorage" in state_root.parts or ".git" in state_root.parts:
        raise HookError("hook_state_requires_local_directory")
    if state_root.is_symlink():
        raise HookError("hook_state_symlink")
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = state_root / "hook-signals"
    for directory in (parent, parent / "pending"):
        if directory.is_symlink():
            raise HookError("hook_state_symlink")
        directory.mkdir(exist_ok=True, mode=0o700)
        _sync_dir(directory.parent)
    return parent / "pending"


@contextmanager
def _signal_lock(directory: Path, identity: str = "queue"):
    fd = os.open(directory.parent / ("signal-" + identity + ".lock"), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise HookError("invalid_signal_lock")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read_signal(path: Path) -> dict:
    if path.is_symlink():
        raise HookError("signal_symlink")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {
            "schema", "thread_id", "transcript_path", "project_root", "started_at", "codex_version", "scope", "first_event"
        } or value["schema"] != 1 or value["codex_version"] != CODEX_VERSION or value["scope"] != SCOPE
                or value["first_event"] not in {"SessionStart", "Stop"}):
            raise ValueError
        _id(value["thread_id"])
        _when(value["started_at"])
        return value
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise HookError("invalid_saved_signal") from None


def _write_signal(path: Path, value: dict) -> None:
    # Control-state update; preserves first signal time while adding a path that
    # SessionStart may initially report as null. No conversation body is stored.
    fd, temporary = tempfile.mkstemp(prefix=".signal-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _register_saved(value: dict, *, state_root: Path, project_root: Path, sessions_root: Path) -> str:
    _exact_project(value["project_root"], project_root)
    path = _transcript(value["transcript_path"], sessions_root)
    if not _metadata(path, value["thread_id"], project_root):
        return "pending_metadata"
    store = Store(state_root)
    with store.connect() as db:
        prior = db.execute("SELECT * FROM registrations WHERE thread_id=?", (value["thread_id"],)).fetchone()
    if prior:
        if (prior["transcript_path"], prior["codex_version"], prior["scope"]) != (str(path), CODEX_VERSION, SCOPE):
            raise HookError("existing_registration_mismatch")
        boundary = prior["started_at"]
    else:
        boundary = value["started_at"]
    register_task(store, CaptureRegistration(value["thread_id"], path, boundary, CODEX_VERSION), SCOPE)
    return "already_registered" if prior else "registered"


def receive_signal(payload: object, *, state_root: Path, project_root: Path,
                   sessions_root: Path, received_at: str | None = None) -> dict:
    if not isinstance(payload, dict):
        raise HookError("invalid_hook_payload")
    event = payload.get("hook_event_name")
    if event not in ("SessionStart", "Stop"):
        raise HookError("unsupported_hook_event")
    if event == "SessionStart" and payload.get("source") not in ("startup", "resume", "clear", "compact"):
        raise HookError("unsupported_session_start_source")
    if event == "Stop" and not isinstance(payload.get("stop_hook_active"), bool):
        raise HookError("invalid_stop_hook_payload")
    if event == "Stop":
        _id(payload.get("turn_id"))
    thread_id = _id(payload.get("session_id"))
    project = _exact_project(payload.get("cwd"), project_root)
    path = _transcript(payload.get("transcript_path"), sessions_root)
    # Reject a known mismatch before it can reserve a source identity. A file
    # absent/incomplete at SessionStart is a pending signal, not registration.
    _metadata(path, thread_id, project_root)
    value = {"schema": 1, "thread_id": thread_id,
             "transcript_path": str(path) if path else None, "project_root": project,
             "started_at": _when(received_at or _now()), "codex_version": CODEX_VERSION,
             "scope": SCOPE, "first_event": event}
    directory = _spool_directory(state_root)
    file = directory / (thread_id + ".json")
    with _signal_lock(directory, thread_id):
        if file.exists() or file.is_symlink():
            existing = _read_signal(file)
            for key in ("thread_id", "project_root", "codex_version", "scope"):
                if existing[key] != value[key]:
                    raise HookError("existing_signal_mismatch")
            if existing["transcript_path"] is not None and value["transcript_path"] not in (None, existing["transcript_path"]):
                raise HookError("signal_transcript_changed")
            if existing["transcript_path"] is None and value["transcript_path"] is not None:
                existing["transcript_path"] = value["transcript_path"]
                _write_signal(file, existing)
            value = existing
        else:
            _write_signal(file, value)
        # Keep the lock until registration finishes. Stop cannot race SessionStart
        # and replace its earliest boundary between spool and SQLite.
        status = _register_saved(value, state_root=state_root, project_root=project_root, sessions_root=sessions_root)
        if status != "pending_metadata":
            file.unlink()
            _sync_dir(directory)
    return {"status": status, "thread_id": thread_id}


def drain_signals(*, state_root: Path, project_root: Path, sessions_root: Path, limit: int = 100) -> dict:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise HookError("invalid_signal_limit")
    directory = _spool_directory(state_root)
    result = {"registered": 0, "pending": 0, "errors": [], "checked": 0}
    with _signal_lock(directory):
        store = Store(state_root)
        cursor = store.setting("hook_signal_cursor", "")
        # Only our small metadata spool is inventoried; no sessions directory walk.
        files = sorted(p for p in directory.iterdir() if p.suffix == ".json")
        after = [p for p in files if p.name > cursor]
        selected = (after or files)[:limit]
        for file in selected:
            result["checked"] += 1
            try:
                _id(file.stem)
                with _signal_lock(directory, file.stem):
                    if not file.exists():
                        continue  # A normal hook registered it after inventory.
                    value = _read_signal(file)
                    if file.stem != value["thread_id"]:
                        raise HookError("signal_filename_mismatch")
                    status = _register_saved(value, state_root=state_root, project_root=project_root, sessions_root=sessions_root)
                    if status == "pending_metadata":
                        result["pending"] += 1
                    else:
                        file.unlink()
                        _sync_dir(directory)
                        result["registered"] += 1
            except HookError as exc:
                result["errors"].append({"code": str(exc)})
            except (OSError, PreservationError):
                result["errors"].append({"code": "signal_registration_pending"})
        store.set_setting("hook_signal_cursor", selected[-1].name if selected else "")
    return result


def _interrupted(signum, frame):
    raise HookInterrupted


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Project-local Codex capture signal")
    parser.add_argument("--state-root", type=Path, default=Path.home() / ".local/state/olympus")
    parser.add_argument("--sessions-root", type=Path, default=Path.home() / ".codex/sessions")
    parser.add_argument("--drain", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    started = _now()
    signal.signal(signal.SIGTERM, _interrupted)
    signal.signal(signal.SIGINT, _interrupted)
    signal.signal(signal.SIGALRM, _interrupted)
    signal.setitimer(signal.ITIMER_REAL, 2.5 if not args.drain else 4.5)
    try:
        if args.drain:
            result = drain_signals(state_root=args.state_root, project_root=PROJECT_ROOT,
                                   sessions_root=args.sessions_root, limit=args.limit)
            print(json.dumps(result))
        else:
            raw = sys.stdin.buffer.read(MAX_PAYLOAD + 1)
            if len(raw) > MAX_PAYLOAD:
                raise HookError("hook_payload_size_limit")
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                raise HookError("invalid_hook_payload") from None
            result = receive_signal(payload, state_root=args.state_root, project_root=PROJECT_ROOT,
                                    sessions_root=args.sessions_root, received_at=started)
            # Stop needs JSON. No additionalContext, continuation, or body output.
            output = {"continue": True}
            if result["status"] == "pending_metadata":
                output["systemMessage"] = "Olympus: сигнал сохранён, регистрация ожидает метаданных задачи."
            print(json.dumps(output, ensure_ascii=False))
        return 0
    except (HookError, HookInterrupted, OSError, PreservationError) as exc:
        code = str(exc) if isinstance(exc, HookError) else "signal_registration_pending"
        if args.drain:
            print(json.dumps({"errors": [{"code": code}]}))
        else:
            print(json.dumps({"continue": True, "systemMessage": "Olympus capture: " + code}))
        return 0
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == "__main__":
    raise SystemExit(main())
