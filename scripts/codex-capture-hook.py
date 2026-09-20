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
import time
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from olympus.codex_capture import CODEX_VERSION, SUPPORTED_CODEX_VERSIONS, CaptureRegistration
from olympus.codex_metadata import read_task_metadata
from olympus.preservation import Store, PreservationError, canonical, digest
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


def _metadata(path: Path | None, thread_id: str, project_root: Path) -> str | None:
    """Read only the first session metadata record, never a transcript tail."""
    if path is None:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise HookError("transcript_not_regular")
            first = stream.readline(MAX_HEADER + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise HookError("transcript_unavailable") from None
    if len(first) > MAX_HEADER:
        raise HookError("session_metadata_size_limit")
    if not first.endswith(b"\n"):
        return None
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
    if meta.get("cli_version") not in SUPPORTED_CODEX_VERSIONS:
        raise HookError("unsupported_codex_version")
    return meta["cli_version"]


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
        } or value["schema"] != 1 or value["codex_version"] not in SUPPORTED_CODEX_VERSIONS or value["scope"] != SCOPE
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


def _record_lifecycle(directory, value, payload, status, observed_at):
    events = directory.parent/'events'
    if events.is_symlink():
        raise HookError('hook_state_symlink')
    events.mkdir(exist_ok=True, mode=0o700)
    with Store(directory.parent.parent).connect() as db:
        registration = db.execute('SELECT started_at FROM registrations WHERE thread_id=?',(value['thread_id'],)).fetchone()
    record = {'schema':1,'thread_id':value['thread_id'],'event':payload['hook_event_name'],
              'source':payload.get('source') if payload['hook_event_name']=='SessionStart' else None,
              'turn_id':payload.get('turn_id') if payload['hook_event_name']=='Stop' else None,
              'observed_at':observed_at,'status':status,'signal_sha256':digest(canonical(value)),
              'transcript_path':value['transcript_path'],'registration_boundary':registration[0] if registration else None,
              'first_signal_boundary':value['started_at'],
              'origin':'hook_envelope','body_saved':False}
    path=events/(value['thread_id']+'.jsonl')
    fd=os.open(path,os.O_WRONLY|os.O_APPEND|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'ab') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise HookError('hook_lifecycle_not_regular')
        stream.write(canonical(record)+b'\n');stream.flush();os.fsync(stream.fileno())
    _sync_dir(events)


def _register_saved(value: dict, *, state_root: Path, project_root: Path, sessions_root: Path) -> str:
    _exact_project(value["project_root"], project_root)
    path = _transcript(value["transcript_path"], sessions_root)
    version = _metadata(path, value["thread_id"], project_root)
    if not version:
        return "pending_metadata"
    store = Store(state_root)
    with store.connect() as db:
        prior = db.execute("SELECT * FROM registrations WHERE thread_id=?", (value["thread_id"],)).fetchone()
    if prior:
        if (prior["transcript_path"], prior["codex_version"], prior["scope"]) != (str(path), version, SCOPE):
            raise HookError("existing_registration_mismatch")
        boundary = prior["started_at"]
    else:
        boundary = value["started_at"]
    register_task(store, CaptureRegistration(value["thread_id"], path, boundary, version), SCOPE)
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
    version = _metadata(path, thread_id, project_root)
    value = {"schema": 1, "thread_id": thread_id,
             "transcript_path": str(path) if path else None, "project_root": project,
             "started_at": _when(received_at or _now()), "codex_version": version or CODEX_VERSION,
             "scope": SCOPE, "first_event": event}
    directory = _spool_directory(state_root)
    file = directory / (thread_id + ".json")
    with _signal_lock(directory, thread_id):
        if file.exists() or file.is_symlink():
            existing = _read_signal(file)
            for key in ("thread_id", "project_root", "scope"):
                if existing[key] != value[key]:
                    raise HookError("existing_signal_mismatch")
            if existing["transcript_path"] is not None and value["transcript_path"] not in (None, existing["transcript_path"]):
                raise HookError("signal_transcript_changed")
            if existing["transcript_path"] is None and value["transcript_path"] is not None:
                existing["transcript_path"] = value["transcript_path"]
                existing["codex_version"] = value["codex_version"]
                _write_signal(file, existing)
            value = existing
        else:
            _write_signal(file, value)
        # Keep the lock until registration finishes. Stop cannot race SessionStart
        # and replace its earliest boundary between spool and SQLite.
        try:
            status = _register_saved(value, state_root=state_root, project_root=project_root, sessions_root=sessions_root)
        except (HookError, OSError, PreservationError) as exc:
            code = str(exc) if isinstance(exc,(HookError,PreservationError)) else 'signal_registration_pending'
            _record_lifecycle(directory,value,payload,code,_when(received_at or _now()))
            raise
        _record_lifecycle(directory,value,payload,status,_when(received_at or _now()))
        if status != "pending_metadata":
            file.unlink()
            _sync_dir(directory)
    return {"status": status, "thread_id": thread_id}


def _attempt_state(store, value):
    try:
        data = json.loads(store.setting('hook_attempt:' + value['thread_id'], '{}'))
        if isinstance(data, dict) and data.get('signal_sha256') == digest(canonical(value)):
            return data
    except (ValueError, TypeError):
        pass
    return {}


def _save_attempt(store, value, code, *, terminal=False):
    previous = _attempt_state(store, value)
    attempts = int(previous.get('attempts', 0)) + 1
    now = time.time()
    record = {'schema': 1, 'thread_id': value['thread_id'], 'signal_sha256': digest(canonical(value)),
              'state': 'held' if terminal else 'retry', 'reason': code, 'attempts': attempts,
              'last_attempt': now, 'next_attempt': None if terminal else now + min(21600, 60 * 2**min(attempts-1, 9))}
    store.set_setting('hook_attempt:' + value['thread_id'], json.dumps(record))
    return record


def _hold_signal(directory, file, value, code):
    held = directory.parent/'held'
    if held.is_symlink():
        raise HookError('hook_state_symlink')
    held.mkdir(exist_ok=True, mode=0o700)
    path = held/(value['thread_id']+'-'+digest(canonical(value))[:16]+'.json')
    _write_signal(path, {'schema': 1, 'signal': value, 'reason': code, 'checked_at': _now(),
                         'native_metadata_checked': True, 'history_read': False})
    file.unlink()
    _sync_dir(directory)


def _refresh_registration_path(store, value, project_root, sessions_root, metadata_reader):
    with store.connect() as db:
        row = db.execute('SELECT * FROM registrations WHERE thread_id=?', (value['thread_id'],)).fetchone()
    if row is None or row['transcript_path'] == value['transcript_path']:
        return False
    native = metadata_reader(value['thread_id'], project_root)
    path = _transcript(native.get('transcript_path'), sessions_root)
    version = _metadata(path, value['thread_id'], project_root)
    if not version:
        raise HookError('registration_metadata_unavailable')
    if row['scope'] != SCOPE:
        raise HookError('existing_registration_mismatch')
    with store.connect(write=True) as db:
        changed = db.execute('''UPDATE registrations SET transcript_path=?,codex_version=?
            WHERE thread_id=? AND transcript_path=? AND started_at=? AND scope=?''',
            (str(path),version,value['thread_id'],row['transcript_path'],row['started_at'],SCOPE)).rowcount
    if not changed:
        raise HookError('registration_rebind_conflict')
    value['transcript_path'], value['codex_version'] = str(path), version
    store.set_setting('capture_rebind:' + value['thread_id'], json.dumps({
        'previous_path':row['transcript_path'],'current_path':str(path),'started_at':row['started_at'],
        'checked_at':_now(),'native_metadata_checked':True,'body_capture_confirmed':False}))
    return True


def drain_signals(*, state_root: Path, project_root: Path, sessions_root: Path, limit: int = 100,
                  metadata_reader=read_task_metadata) -> dict:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise HookError("invalid_signal_limit")
    directory = _spool_directory(state_root)
    result = {"registered": 0, "pending": 0, "errors": [], "checked": 0, 'deferred': 0, 'held': 0}
    # Leave room for a final metadata read (bounded to 2 s) and diagnostics
    # before the helper's 4.5 s outer deadline.
    work_deadline = time.monotonic() + 2
    with _signal_lock(directory):
        store = Store(state_root)
        cursor = store.setting("hook_signal_cursor", "")
        # Only our small metadata spool is inventoried; no sessions directory walk.
        files = sorted(p for p in directory.iterdir() if p.suffix == ".json")
        after = [p for p in files if p.name > cursor]
        selected = (after or files)[:limit]
        for file in selected:
            if time.monotonic() >= work_deadline:
                break
            # Persist each claim; a timed-out metadata resolver must not starve
            # all later signals on every daemon pass.
            store.set_setting("hook_signal_cursor", file.name)
            value = None
            identity = None
            try:
                identity = _id(file.stem)
                with _signal_lock(directory, file.stem):
                    if not file.exists():
                        continue  # A normal hook registered it after inventory.
                    value = _read_signal(file)
                    if file.stem != value["thread_id"]:
                        raise HookError("signal_filename_mismatch")
                    attempt = _attempt_state(store, value)
                    if attempt.get('state') == 'retry' and (attempt.get('next_attempt') or 0) > time.time():
                        result['deferred'] += 1
                        continue
                    result['checked'] += 1
                    failure = None
                    if value["transcript_path"] is None:
                        try:
                            metadata = metadata_reader(value["thread_id"], project_root)
                            if metadata and metadata.get("transcript_path"):
                                candidate = _transcript(metadata["transcript_path"], sessions_root)
                                version = _metadata(candidate, value["thread_id"], project_root)
                                if version:
                                    value["transcript_path"] = str(candidate)
                                    value["codex_version"] = version
                                    _write_signal(file, value)
                        except PreservationError as exc:
                            failure = str(exc)
                    if failure in {'codex_metadata_project_mismatch', 'codex_metadata_subagent'}:
                        _save_attempt(store, value, failure, terminal=True)
                        _hold_signal(directory,file,value,failure)
                        result['held'] += 1
                        continue
                    if failure:
                        _save_attempt(store,value,failure)
                        result['errors'].append({'code':failure,'thread_id':value['thread_id']})
                        result['pending'] += 1
                        continue
                    try:
                        if _refresh_registration_path(store,value,project_root,sessions_root,metadata_reader):
                            _write_signal(file,value)
                    except PreservationError as exc:
                        failure = str(exc)
                        terminal = failure in {'codex_metadata_project_mismatch','codex_metadata_subagent'}
                        _save_attempt(store,value,failure,terminal=terminal)
                        if terminal:
                            _hold_signal(directory,file,value,failure)
                            result['held'] += 1
                        else:
                            result['errors'].append({'code':failure,'thread_id':value['thread_id']})
                            result['pending'] += 1
                        continue
                    status = _register_saved(value, state_root=state_root, project_root=project_root, sessions_root=sessions_root)
                    if status == "pending_metadata":
                        result["pending"] += 1
                        _save_attempt(store,value,'transcript_path_missing' if value['transcript_path'] is None else 'registration_metadata_unavailable')
                    else:
                        file.unlink()
                        _sync_dir(directory)
                        result["registered"] += 1
                        store.set_setting('hook_attempt:' + value['thread_id'], json.dumps({
                            'schema':1,'thread_id':value['thread_id'],'state':'registered','checked_at':_now()}))
            except HookError as exc:
                result["errors"].append({"code": str(exc), **({'thread_id':identity} if identity else {})})
                if value is not None and value.get('thread_id') == file.stem:
                    _save_attempt(store,value,str(exc))
            except (OSError, PreservationError):
                result["errors"].append({"code": "signal_registration_pending", **({'thread_id':identity} if identity else {})})
                if value is not None and value.get('thread_id') == file.stem:
                    _save_attempt(store,value,'signal_registration_pending')
        if not selected:
            store.set_setting("hook_signal_cursor", "")
        pending_files = sorted(p for p in directory.iterdir() if p.suffix == ".json")
        pending_details, oldest = [], None
        now = datetime.now(timezone.utc)
        for file in pending_files[:1000]:
            try:
                value = _read_signal(file)
                attempt = _attempt_state(store,value)
                started = _when(value["started_at"])
                oldest = min(oldest, started) if oldest else started
                if len(pending_details) < 20:
                    pending_details.append({"thread_id": value["thread_id"], "started_at": started,
                        "reason": attempt.get('reason') or ('transcript_path_missing' if value['transcript_path'] is None else 'registration_pending'),
                        'attempts':attempt.get('attempts',0),'next_attempt':attempt.get('next_attempt')})
            except HookError:
                if len(pending_details) < 20:
                    pending_details.append({"reason": "invalid_saved_signal"})
        result.update(pending_total=len(pending_files), oldest_pending_at=oldest,
            oldest_pending_age_seconds=max(0, (now - datetime.fromisoformat(oldest)).total_seconds()) if oldest else None,
            pending_details=pending_details, pending_details_truncated=len(pending_files) > 20,
            pending_age_coverage="bounded" if len(pending_files) > 1000 else "complete")
        held = directory.parent/'held'
        result['held_total'] = len(list(held.glob('*.json'))) if held.is_dir() else 0
    return result


def register_current_task(thread_id: str, *, state_root: Path, project_root: Path,
                          sessions_root: Path, metadata_reader=read_task_metadata) -> dict:
    """Explicit operator registration of this task from now; never fake a hook."""
    thread_id = _id(thread_id)
    boundary = _now()
    metadata = metadata_reader(thread_id, project_root)
    path = _transcript(metadata.get("transcript_path"), sessions_root)
    version = _metadata(path, thread_id, project_root)
    if not version:
        raise HookError("registration_metadata_unavailable")
    store = Store(state_root)
    with store.connect() as db:
        prior = db.execute("SELECT * FROM registrations WHERE thread_id=?", (thread_id,)).fetchone()
    if prior:
        boundary = prior["started_at"]
    result = register_task(store, CaptureRegistration(thread_id, path, boundary, version), SCOPE)
    store.set_setting("capture_registration_origin:" + thread_id, json.dumps({
        "method": "explicit_current_task_native_metadata", "started_at": boundary,
        "history_before_boundary_imported": False, "checked_at": _now()}))
    return {**result, "started_at": boundary, "codex_version": version, "turn_capture_confirmed": False}


def _interrupted(signum, frame):
    raise HookInterrupted


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Project-local Codex capture signal")
    parser.add_argument("--state-root", type=Path, default=Path.home() / ".local/state/olympus")
    parser.add_argument("--sessions-root", type=Path, default=Path.home() / ".codex/sessions")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--drain", action="store_true")
    mode.add_argument("--register-current", metavar="THREAD_ID")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    started = _now()
    signal.signal(signal.SIGTERM, _interrupted)
    signal.signal(signal.SIGINT, _interrupted)
    signal.signal(signal.SIGALRM, _interrupted)
    signal.setitimer(signal.ITIMER_REAL, 4.5 if args.drain or args.register_current else 2.5)
    try:
        if args.register_current:
            result = register_current_task(args.register_current, state_root=args.state_root,
                project_root=PROJECT_ROOT, sessions_root=args.sessions_root)
            print(json.dumps(result))
        elif args.drain:
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
        code = str(exc) if isinstance(exc, (HookError, PreservationError)) else "signal_registration_pending"
        if args.drain or args.register_current:
            print(json.dumps({"errors": [{"code": code}]}))
        else:
            print(json.dumps({"continue": True, "systemMessage": "Olympus capture: " + code}))
        return 1 if args.register_current else 0
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == "__main__":
    raise SystemExit(main())
