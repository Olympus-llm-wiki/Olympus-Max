"""Persistent delivery permission, separate from pilot counters and workload."""
from __future__ import annotations

import time
import json
import math

from .preservation import Store, PreservationError

CONTINUOUS_BODY_BYTES = 64 * 1024 * 1024
# One outer requeue in addition to the worker's configured task retry budget.
# With worker_max_retries=3 this bounds generic task executions to 4 * 2;
# provider quota deferrals and consolidation have separate native semantics.
MAX_NATIVE_RETRIES = 1


def new_submission_hold(store: Store) -> dict | None:
    """A live snapshot owner can drain writers without blocking readback.

    Tokens stay internal. An expired or replaced job cannot strand the queue.
    The snapshot service separately verifies that it still owns this hold.
    """
    with store.connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key='backup_submission_hold'").fetchone()
        if row is None:
            return None
        try:
            hold = json.loads(row[0])
        except (ValueError, TypeError):
            return None
        if not isinstance(hold, dict) or hold.get("reason") != "backup_snapshot":
            return None
        expires = hold.get("expires_at")
        if type(expires) not in (int, float) or not math.isfinite(expires) or expires <= time.time():
            return None
        if not isinstance(hold.get("job_id"), str) or not isinstance(hold.get("token"), str):
            return None
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pipeline_jobs'").fetchone() is None:
            return None
        job = db.execute("SELECT schema_version,kind,state,token,lease_until FROM pipeline_jobs WHERE id=?", (hold["job_id"],)).fetchone()
        if (job is None or job["schema_version"] != 1 or job["kind"] != "backup.snapshot"
                or job["state"] != "running" or job["token"] != hold["token"] or job["lease_until"] <= time.time()):
            return None
        return {"reason": "backup_snapshot", "job_id": hold["job_id"], "retry_at": min(expires, job["lease_until"])}


def resume_models(store: Store) -> None:
    with store.exclusive(), store.connect(write=True) as db:
        for key, value in {"delivery_mode": "continuous", "delivery_hold_until": "0",
                           "delivery_hold_reason": "", "delivery_attention": ""}.items():
            db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def pause_models(store: Store) -> None:
    with store.exclusive(), store.connect(write=True) as db:
        for key, value in {"delivery_mode": "paused", "budget_expires": "0"}.items():
            db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def permission(store: Store) -> dict:
    with store.connect() as db:
        settings = dict(db.execute("SELECT key,value FROM settings WHERE key LIKE 'delivery_%' OR key IN ('budget_expires','maintenance','recovery_state')"))
    mode = settings.get("delivery_mode", "pilot")
    reason = None
    until = float(settings.get("delivery_hold_until", "0"))
    if settings.get("recovery_state", "ready") != "ready":
        reason = "recovery_blocked"
    elif settings.get("maintenance", "off") != "off":
        reason = "maintenance"
    elif mode == "paused":
        reason = "models_paused"
    elif mode not in {"continuous", "pilot"}:
        reason = "invalid_delivery_mode"
    elif mode == "continuous" and settings.get("delivery_attention"):
        reason = settings["delivery_attention"]
    elif mode == "continuous" and until > time.time():
        reason = settings.get("delivery_hold_reason") or "provider_backoff"
    elif mode == "pilot" and float(settings.get("budget_expires", "0")) <= time.time():
        reason = "awaiting_pilot_budget"
    return {"mode": mode, "allowed": reason is None, "reason": reason,
            "retry_at": until if until > time.time() else None,
            "max_inflight": 1 if mode == "continuous" else None}


def inflight_count(store: Store, *, excluding: str = "") -> int:
    with store.connect() as db:
        has_parts = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_representations'").fetchone() is not None
        represented = ("AND NOT EXISTS (SELECT 1 FROM native_representations r WHERE r.version_id=d.version_id AND r.kind='native_index' AND r.enabled=1)"
                       if has_parts else "")
        legacy = db.execute(f"""SELECT count(*) FROM delivery d JOIN versions v ON v.id=d.version_id
            JOIN sources s ON s.id=v.source_id WHERE v.active=1 AND s.forgotten_at IS NULL
            AND d.version_id!=? AND (d.state='submitted' OR (d.state='pending' AND d.attempts>0)) {represented}""",
            (excluding,)).fetchone()[0]
        parts = db.execute("""SELECT count(*) FROM native_representation_parts p
            JOIN native_representations r ON r.id=p.representation_id
            JOIN versions v ON v.id=r.version_id JOIN sources s ON s.id=v.source_id
            WHERE r.enabled=1 AND v.active=1 AND s.forgotten_at IS NULL AND v.id!=?
              AND (p.state='submitted' OR (p.state='pending' AND p.attempts>0))""", (excluding,)).fetchone()[0] if has_parts else 0
        return legacy + parts


def hold(store: Store, reason: str, seconds: float) -> None:
    if reason not in {"provider_rate_limited", "provider_unavailable", "provider_authentication_required"}:
        raise PreservationError("invalid_delivery_hold")
    with store.connect(write=True) as db:
        previous = db.execute("SELECT value FROM settings WHERE key='delivery_hold_until'").fetchone()
        until = max(float(previous[0]) if previous else 0, time.time() + seconds)
        for key, value in {"delivery_hold_until": str(until), "delivery_hold_reason": reason}.items():
            db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
