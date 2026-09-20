"""Durable stage attempts. Additive schema; source/delivery identities stay in Store."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from .preservation import PreservationError, canonical, guard_no_secrets


def _revision():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[2],
            stderr=subprocess.DEVNULL, text=True, timeout=2).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


# Captured when the process imports its code, never presented as the current checkout.
LOADED_REVISION = _revision()
STARTED_AT = time.time()


class PipelineJobs:
    SCHEMA = 1

    def __init__(self, store):
        self.store = store
        with store.connect(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS pipeline_jobs (
                id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
                kind TEXT NOT NULL, object_id TEXT NOT NULL,
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                token TEXT, lease_until REAL NOT NULL DEFAULT 0,
                lease_seconds REAL NOT NULL DEFAULT 120, next_attempt REAL NOT NULL DEFAULT 0,
                last_attempt REAL, last_progress REAL, last_success REAL,
                error_code TEXT, failure_scope TEXT, detail_json TEXT, proof_json TEXT,
                pid INTEGER, process_started REAL, loaded_revision TEXT,
                UNIQUE(kind, object_id))""")
            if db.execute("SELECT 1 FROM pipeline_jobs WHERE schema_version!=? LIMIT 1", (self.SCHEMA,)).fetchone():
                raise PreservationError("unsupported_pipeline_job_schema")

    def begin(self, kind, object_id="service", *, lease_seconds=120):
        if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,120}", kind) or not 0 < lease_seconds <= 86400:
            raise PreservationError("invalid_pipeline_job")
        guard_no_secrets(str(object_id).encode())
        now, token = time.time(), str(uuid.uuid4())
        identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, "olympus:job:" + kind + ":" + str(object_id)))
        with self.store.connect(write=True) as db:
            db.execute("""INSERT OR IGNORE INTO pipeline_jobs
                (id,schema_version,kind,object_id,state) VALUES(?,?,?,?, 'pending')""",
                (identifier, self.SCHEMA, kind, str(object_id)))
            changed = db.execute("""UPDATE pipeline_jobs SET state='running', attempts=attempts+1,
                token=?,lease_until=?,lease_seconds=?,last_attempt=?,pid=?,process_started=?,loaded_revision=?
                WHERE id=? AND lease_until<=? AND next_attempt<=?""",
                (token, now+lease_seconds, lease_seconds, now, os.getpid(), STARTED_AT,
                 LOADED_REVISION, identifier, now, now)).rowcount
        return {"id": identifier, "token": token} if changed else None

    def _finish(self, lease, state, data, *, code=None, scope=None, retry_after=0):
        payload = canonical(data).decode()
        guard_no_secrets(payload.encode())
        if code and not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,120}", code):
            raise PreservationError("unsafe_error_code")
        if scope not in (None, "source", "stage", "provider", "recovery") or retry_after < 0:
            raise PreservationError("invalid_pipeline_failure")
        now = time.time()
        with self.store.connect(write=True) as db:
            if state == "running":
                return db.execute("""UPDATE pipeline_jobs SET last_progress=?,detail_json=?,
                    lease_until=?+lease_seconds WHERE id=? AND token=? AND state='running' AND lease_until>?""",
                    (now, payload, now, lease['id'], lease['token'], now)).rowcount == 1
            return db.execute("""UPDATE pipeline_jobs SET state=?,token=NULL,lease_until=0,
                next_attempt=?,error_code=?,failure_scope=?,detail_json=?,
                proof_json=CASE WHEN ?='succeeded' THEN ? ELSE proof_json END,
                last_success=CASE WHEN ?='succeeded' THEN ? ELSE last_success END,
                last_progress=CASE WHEN ?='succeeded' THEN ? ELSE last_progress END
                WHERE id=? AND token=? AND state='running' AND lease_until>?""",
                (state, now+retry_after, code, scope, payload, state, payload, state, now,
                 state, now, lease['id'], lease['token'], now)).rowcount == 1

    def progress(self, lease, detail):
        return self._finish(lease, "running", detail)

    def renew(self, lease):
        """Liveness renews ownership without claiming useful work progressed."""
        now = time.time()
        with self.store.connect(write=True) as db:
            return db.execute('''UPDATE pipeline_jobs SET lease_until=?+lease_seconds
                WHERE id=? AND token=? AND state='running' AND lease_until>?''',
                (now, lease['id'], lease['token'], now)).rowcount == 1

    def succeed(self, lease, proof):
        return self._finish(lease, "succeeded", proof)

    def fail(self, lease, code, scope="source", retry_after=0, *, detail=None):
        return self._finish(lease, "failed", detail or {}, code=code, scope=scope, retry_after=retry_after)

    def snapshot(self):
        with self.store.connect() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM pipeline_jobs ORDER BY kind,object_id")]
        now = time.time()
        for row in rows:
            row.pop('token', None)
            row['lease_expired'] = row['state'] == 'running' and row['lease_until'] <= now
            for key in ('detail_json', 'proof_json'):
                row[key.removesuffix('_json')] = json.loads(row.pop(key) or '{}')
        return rows
