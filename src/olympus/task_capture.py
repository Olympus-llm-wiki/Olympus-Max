"""Persist sanitized observed events, then deliver complete turn documents."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path

from .codex_capture import CaptureRegistration, read_registered_rollout
from .preservation import Store, PreservationError, canonical, guard_no_secrets, redact_secrets


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


def sync_registered_tasks(store: Store, *, limit: int = 50) -> dict:
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
        gaps = sorted({g.code for g in batch.gaps})
        inserted = 0
        with store.connect(write=True) as db:
            for message in batch.messages:
                payload = canonical(asdict(message)).decode()
                guard_no_secrets(payload.encode())
                previous = db.execute("SELECT message_json FROM captured_events WHERE thread_id=? AND event_id=?", (row["thread_id"], message.event_id)).fetchone()
                if previous and previous[0] != payload:
                    gaps.append("captured_event_changed")
                    continue
                inserted += db.execute("INSERT OR IGNORE INTO captured_events VALUES(?,?,?,?,?)",
                                       (row["thread_id"], message.event_id, message.turn_id, message.timestamp, payload)).rowcount
            db.execute("UPDATE registrations SET last_source_hash=?,last_gap=? WHERE thread_id=?",
                       (batch.source_sha256, json.dumps(sorted(set(gaps))), row["thread_id"]))
        # Local events remain durable even when source/schema coverage is partial.
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
                        "coverage_gaps": sorted(set(gaps)), "turn_receipts": receipts})
        store.set_setting("registration_cursor", row["thread_id"])
    return {"tasks": results, "registrations_total": total, "bounded_pass": len(registrations) < total}
