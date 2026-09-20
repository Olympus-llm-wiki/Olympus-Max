"""Persist sanitized observed events, then deliver complete turn documents."""
from __future__ import annotations

from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import re
import stat

from .codex_capture import CaptureRegistration, read_registered_rollout
from .codex_metadata import read_task_metadata
from .preservation import Store, PreservationError, canonical, guard_no_secrets, redact_secrets, timestamp


def drain_status(stdout: str, returncode: int) -> dict:
    """Project the helper's bounded structured response into safe health fields."""
    failure = {"state": "degraded", "errors": [{"code": "hook_drain_invalid_response"}]}
    if returncode != 0:
        return {"state": "degraded", "errors": [{"code": "hook_drain_process_failed"}]}
    try:
        if not isinstance(stdout, str) or len(stdout) > 65536:
            return failure
        raw = json.loads(stdout)
        if not isinstance(raw, dict) or not isinstance(raw.get("errors"), list):
            return failure
        errors = raw["errors"]
        if len(errors) > 1000 or any(not isinstance(e, dict) or not isinstance(e.get("code"), str)
                or not re.fullmatch(r"[a-z_]{1,100}", e["code"]) for e in errors):
            return failure
        safe_errors = []
        for entry in errors[:20]:
            error = {'code':entry['code']}
            identity = entry.get('thread_id')
            if isinstance(identity,str) and re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',identity):
                error['thread_id'] = identity
            safe_errors.append(error)
        result = {"errors": safe_errors, "errors_truncated": len(errors) > 20}
        # A helper interrupted before inventory may report only errors.
        if not errors and any(key not in raw for key in ("registered", "pending", "checked")):
            return failure
        for key in ("registered", "pending", "checked", "pending_total", 'deferred', 'held', 'held_total'):
            if key in raw:
                if type(raw[key]) is not int or not 0 <= raw[key] <= 1000000:
                    return failure
                result[key] = raw[key]
        for key in ("oldest_pending_at", "pending_age_coverage"):
            if raw.get(key) is not None:
                value = raw[key]
                if not isinstance(value, str) or len(value) > 80:
                    return failure
                if key == "oldest_pending_at":
                    from datetime import datetime
                    if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
                        return failure
                elif value not in {"complete", "bounded"}:
                    return failure
                result[key] = value
        age = raw.get("oldest_pending_age_seconds")
        if age is not None:
            if type(age) not in (int, float) or not math.isfinite(age) or age < 0:
                return failure
            result["oldest_pending_age_seconds"] = age
        details = []
        for entry in raw.get('pending_details', [])[:20]:
            if not isinstance(entry,dict):
                continue
            identity,reason = entry.get('thread_id'),entry.get('reason')
            if not isinstance(reason,str) or not re.fullmatch(r'[a-z_]{1,100}',reason):
                continue
            detail = {'reason':reason}
            if isinstance(identity,str) and re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',identity):
                detail['thread_id'] = identity
            for key in ['attempts','next_attempt']:
                value = entry.get(key)
                if type(value) in (int,float) and math.isfinite(value) and value >= 0:
                    detail[key] = value
            details.append(detail)
        result['pending_details'] = details
        result["state"] = "degraded" if errors or result.get("pending_total", result.get("pending", 0)) else "healthy"
        return result
    except (ValueError, TypeError):
        return failure


def register_task(store: Store, registration: CaptureRegistration, scope: str) -> dict:
    if not scope:
        raise PreservationError("scope_required")
    with store.connect(write=True) as db:
        prior = db.execute("SELECT * FROM registrations WHERE thread_id=?", (registration.thread_id,)).fetchone()
        identity = (str(registration.transcript_path), registration.started_at, registration.codex_version, scope)
        if prior and tuple(prior[k] for k in ("transcript_path", "started_at", "codex_version", "scope")) != identity:
            raise PreservationError("registration_already_has_different_scope")
        db.execute("INSERT OR IGNORE INTO registrations(thread_id,transcript_path,started_at,codex_version,scope) VALUES(?,?,?,?,?)",
                   (registration.thread_id, *identity))
    return {"thread_id": registration.thread_id, "registered": True, "scope": scope}


def _turn_text(thread_id: str, turn_id: str, messages: list[dict]) -> str:
    lines = [f"# Codex conversation: {thread_id}", f"Turn: {turn_id}",
             "Derived text capture. Roles and original language preserved; known credentials redacted.", ""]
    for m in messages:
        lines.extend([f"## {m['role']} — {m['timestamp']}", f"Event: {m['event_id']}",
                      f"Kind: {m['kind']}; phase: {m.get('phase') or 'unspecified'}", "", m["text"], ""])
    return "\n".join(lines)


def _rebind_registered_path(store, row, *, metadata_reader):
    """Follow only a registered ID through native metadata after its path moved."""
    project = Path(__file__).resolve().parents[2]
    metadata = metadata_reader(row["thread_id"], project)
    candidate = Path(metadata.get("transcript_path") or "")
    roots = (Path.home() / ".codex/sessions", Path.home() / ".codex/archived_sessions")
    if (not candidate.is_absolute() or ".." in candidate.parts or candidate.suffix != ".jsonl"
            or not candidate.name.startswith("rollout-") or not any(candidate.is_relative_to(base) for base in roots)):
        raise PreservationError("registration_rebind_path_not_allowed")
    if any(parent.is_symlink() for parent in (candidate, *candidate.parents)):
        raise PreservationError("registration_rebind_symlink")
    fd = os.open(candidate, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise PreservationError("registration_rebind_not_regular")
        header = stream.readline(1024 * 1024 + 1)
    if len(header) > 1024 * 1024 or not header.endswith(b"\n"):
        raise PreservationError("registration_rebind_invalid_metadata")
    try:
        record = json.loads(header)
        meta = record["payload"]
        if (record.get("type") != "session_meta" or meta.get("id") != row["thread_id"]
                or meta.get("cwd") != str(project) or meta.get("cli_version") != row["codex_version"]):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise PreservationError("registration_rebind_invalid_metadata") from None
    registration = CaptureRegistration(row["thread_id"], candidate, row["started_at"], row["codex_version"])
    batch = read_registered_rollout(registration, redactor=redact_secrets)
    if batch.source_sha256 is None:
        raise PreservationError("registration_rebind_unreadable")
    with store.connect(write=True) as db:
        changed = db.execute("UPDATE registrations SET transcript_path=? WHERE thread_id=? AND transcript_path=?",
                             (str(candidate), row["thread_id"], row["transcript_path"])).rowcount
    if not changed:
        raise PreservationError("registration_rebind_conflict")
    store.set_setting("capture_rebind:" + row["thread_id"], json.dumps({
        "previous_path": row["transcript_path"], "current_path": str(candidate),
        "started_at": row["started_at"], "checked_at": timestamp(), "source_sha256": batch.source_sha256}))
    return batch


def sync_registered_tasks(store: Store, *, limit: int = 50, metadata_reader=read_task_metadata) -> dict:
    if not 1 <= limit <= 100:
        raise PreservationError("invalid_registration_limit")
    with store.connect() as db:
        cursor = db.execute("SELECT value FROM settings WHERE key='registration_cursor'").fetchone()
        after = cursor[0] if cursor else ""
        registrations = [dict(r) for r in db.execute("SELECT * FROM registrations WHERE thread_id>? ORDER BY thread_id LIMIT ?", (after, limit))]
        if not registrations:
            registrations = [dict(r) for r in db.execute("SELECT * FROM registrations ORDER BY thread_id LIMIT ?", (limit,))]
        total = db.execute("SELECT count(*) FROM registrations").fetchone()[0]
    results = []
    for row in registrations:
        registration = CaptureRegistration(row["thread_id"], row["transcript_path"], row["started_at"], row["codex_version"])
        batch = read_registered_rollout(registration, redactor=redact_secrets)
        rebind_error = None
        if any(g.code == "transcript_unavailable" for g in batch.gaps):
            try:
                batch = _rebind_registered_path(store, row, metadata_reader=metadata_reader)
            except PreservationError as exc:
                rebind_error = str(exc)
            except (OSError, ValueError, TypeError):
                rebind_error = "registration_rebind_unavailable"
        gaps = sorted({g.code for g in batch.gaps})
        if rebind_error:
            gaps.append(rebind_error)
        inserted = 0
        with store.connect(write=True) as db:
            for message in batch.messages:
                fields = asdict(message)
                # Check the sanitized values before JSON escaping can join
                # separate lines into a false credential match.
                for value in fields.values():
                    if isinstance(value, str):
                        guard_no_secrets(value.encode())
                payload = canonical(fields).decode()
                previous = db.execute("SELECT message_json FROM captured_events WHERE thread_id=? AND event_id=?", (row["thread_id"], message.event_id)).fetchone()
                if previous and previous[0] != payload:
                    gaps.append("captured_event_changed")
                    continue
                inserted += db.execute("INSERT OR IGNORE INTO captured_events VALUES(?,?,?,?,?)",
                                       (row["thread_id"], message.event_id, message.turn_id, message.timestamp, payload)).rowcount
            db.execute("UPDATE registrations SET last_source_hash=?,last_gap=? WHERE thread_id=?",
                       (batch.source_sha256, json.dumps(sorted(set(gaps))), row["thread_id"]))
        # Local events remain durable even when source/schema coverage is partial.
        coverage = {"source_sha256": batch.source_sha256,
                    "segments": [asdict(segment) for segment in batch.segments],
                    "gaps": sorted(set(gaps)), "checked_at": timestamp()}
        store.set_setting("capture_coverage:" + row["thread_id"], json.dumps(coverage))
        completed = batch.completed_turn_ids if "captured_event_changed" not in gaps else ()
        receipts = []
        for turn_id in sorted(completed):
            # A source version is this observed snapshot, not a union with text
            # from older revisions retained in the local event journal.
            messages = [asdict(m) for m in batch.messages if m.turn_id == turn_id]
            if not messages:
                continue
            text = _turn_text(row["thread_id"], turn_id, messages)
            try:
                receipt = store.capture(
                    source_key=f"codex:{row['thread_id']}:turn:{turn_id}", scope=row["scope"],
                    kind="conversation", title=f"Codex turn {turn_id}", original=text.encode(), text=text,
                    locator=f"codex-task:{row['thread_id']}", observed_at=messages[0]["timestamp"],
                    metadata={"thread_id": row["thread_id"], "turn_id": turn_id, "capture_format": "derived-sanitized",
                              "codex_version": registration.codex_version, "event_at": messages[0]["timestamp"],
                              "original_is_transformed": "true", "source_capture_separate": "required"},
                )
                version_key = "captured_turn:" + row["thread_id"] + ":" + turn_id
                previous_version = store.setting(version_key)
                if previous_version and previous_version != receipt.version_id:
                    store.supersede(previous_version, receipt.version_id, "Observed revision of the same completed transcript turn")
                store.set_setting(version_key, receipt.version_id)
                receipts.append(asdict(store.receipt(receipt.version_id)))
            except PreservationError as exc:
                if str(exc) != "source_is_forgotten":
                    raise
                receipts.append({"turn_id": turn_id, "memory": "forgotten", "reimported": False})
        results.append({"thread_id": row["thread_id"], "new_local_events": inserted,
                        "coverage_gaps": sorted(set(gaps)), "coverage_segments": coverage["segments"],
                        "turn_receipts": receipts})
        store.set_setting("registration_cursor", row["thread_id"])
    return {"tasks": results, "registrations_total": total, "bounded_pass": len(registrations) < total}
