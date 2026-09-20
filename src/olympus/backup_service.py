"""Independent daily object snapshots with a bounded native submission hold.

The hold drains current work without suppressing polls. Only the native/SQLite
cutover owns maintenance; capture and revocations may continue during native
export. Encryption runs after the hold is released and eligible workers resume.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time

from .admission import inflight_count, new_submission_hold, permission
from .backup import CheckpointError, _age, _recipient
from .backup_job import _native_backup
from .jobs import PipelineJobs
from .preservation import Store, PreservationError, canonical, digest, timestamp
from .snapshot_objects import prepare_snapshot, encrypt_snapshot, RESERVE_BYTES

JOB_KIND = "backup.snapshot"
MAINTENANCE = "backup_snapshot"
HOLD_KEY = "backup_submission_hold"
OWNER_KEY = "backup_maintenance_owner"
LEASE_SECONDS = 120
HOLD_SECONDS = 60
SPACE_ESTIMATE_KEY = "backup_space_estimate"
SPACE_RETRY_SECONDS = 3600


def _json(value):
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}


def _setting(db, key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _save(db, key, value):
    db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, value if isinstance(value, str) else canonical(value).decode()))


def _owned(value, lease):
    return (value.get("schema") == 1 and value.get("job_id") == lease["id"]
            and value.get("token") == lease["token"])


def _fence_db(db, lease):
    row = db.execute("SELECT kind,state,token,lease_until FROM pipeline_jobs WHERE id=?", (lease["id"],)).fetchone()
    if (row is None or row["kind"] != JOB_KIND or row["state"] != "running"
            or row["token"] != lease["token"] or row["lease_until"] <= time.time()):
        raise PreservationError("backup_lease_lost")
    return row


def _report(store, state, *, lease=None, **details):
    value = {"state": state, "checked_at": timestamp(), **details}
    if lease is None:
        store.set_setting("backup_status", canonical(value).decode())
    else:
        with store.connect(write=True) as db:
            row = db.execute("SELECT attempts,token,state FROM pipeline_jobs WHERE id=?", (lease["id"],)).fetchone()
            if (row and row["attempts"] == lease.get("attempt")
                    and (row["token"] == lease["token"] or row["state"] != "running")):
                _save(db, "backup_status", value)
    return value


class _LeaseKeeper:
    def __init__(self, store, jobs, lease, cancelled):
        self.store, self.jobs, self.lease, self.cancelled = store, jobs, lease, cancelled
        self.stopped = threading.Event()
        self.lost = threading.Event()
        self.phase = "reservation"
        self.last_detail = None
        self.last_report_phase = None

    def fence(self):
        if self.cancelled():
            raise PreservationError("backup_cancelled")
        if self.lost.is_set():
            raise PreservationError("backup_lease_lost")
        with self.store.connect() as db:
            _fence_db(db, self.lease)

    def _refresh_markers(self):
        with self.store.connect(write=True) as db:
            row = _fence_db(db, self.lease)
            for key in (HOLD_KEY, OWNER_KEY):
                value = _json(_setting(db, key))
                if _owned(value, self.lease):
                    value["expires_at"] = min(time.time() + HOLD_SECONDS, row["lease_until"])
                    _save(db, key, value)

    def renew(self):
        self.fence()
        if not self.jobs.renew(self.lease):
            self.lost.set()
            raise PreservationError("backup_lease_lost")
        self._refresh_markers()

    def progress(self, detail=None):
        self.fence()
        value = {"phase": self.phase, **(detail or {})}
        signature = canonical(value)
        if signature == self.last_detail:
            self.renew()
            return
        if not self.jobs.progress(self.lease, value):
            self.lost.set()
            raise PreservationError("backup_lease_lost")
        self.last_detail = signature
        self._refresh_markers()
        phase_key = (self.phase, value.get("stage"), value.get("state"))
        if phase_key != self.last_report_phase:
            _report(self.store, self.phase, lease=self.lease, job_id=self.lease["id"], detail=value)
            self.last_report_phase = phase_key

    def _run(self):
        while not self.stopped.wait(10):
            try:
                self.renew()
            except Exception:
                self.lost.set()
                return

    def __enter__(self):
        self.progress()
        self.thread = threading.Thread(target=self._run, name="olympus-backup-lease", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopped.set()
        self.thread.join(timeout=1)


def _reserve(store, lease):
    with store.exclusive():
        other = new_submission_hold(store)
        if other and other["job_id"] != lease["id"]:
            raise PreservationError("backup_submission_hold_owned_elsewhere")
        with store.connect(write=True) as db:
            row = _fence_db(db, lease)
            if _setting(db, "maintenance", "off") != "off":
                raise PreservationError("backup_foreign_maintenance")
            _save(db, HOLD_KEY, {"schema": 1, "job_id": lease["id"], "token": lease["token"],
                "reason": "backup_snapshot", "expires_at": min(time.time() + HOLD_SECONDS, row["lease_until"])})


def _enter_quiet(store, lease):
    with store.exclusive(), store.connect(write=True) as db:
        row = _fence_db(db, lease)
        hold = _json(_setting(db, HOLD_KEY))
        if not _owned(hold, lease) or hold.get("expires_at", 0) <= time.time():
            raise PreservationError("backup_submission_hold_lost")
        if _setting(db, "maintenance", "off") != "off":
            raise PreservationError("backup_foreign_maintenance")
        _save(db, OWNER_KEY, {"schema": 1, "job_id": lease["id"], "token": lease["token"],
            "phase": "native_snapshot", "started_at": timestamp(),
            "expires_at": min(time.time() + HOLD_SECONDS, row["lease_until"])})
        _save(db, "maintenance", MAINTENANCE)


def _release(store, lease):
    """Clear only this still-owned epoch; another maintenance is never changed."""
    with store.exclusive(), store.connect(write=True) as db:
        _fence_db(db, lease)
        maintenance = _setting(db, "maintenance", "off")
        owner = _json(_setting(db, OWNER_KEY))
        if maintenance not in {"off", MAINTENANCE}:
            raise PreservationError("backup_foreign_maintenance")
        if maintenance == MAINTENANCE:
            if not _owned(owner, lease):
                raise PreservationError("backup_maintenance_owner_changed")
            _save(db, "maintenance", "off")
            db.execute("DELETE FROM settings WHERE key=?", (OWNER_KEY,))
        hold = _json(_setting(db, HOLD_KEY))
        if _owned(hold, lease):
            db.execute("DELETE FROM settings WHERE key=?", (HOLD_KEY,))


def _recover_stale(store, runtime):
    """No inference from a dead PID or expired marker without its matching job."""
    if store.setting("maintenance", "off") == "off":
        return None
    if store.setting("maintenance") != MAINTENANCE:
        return {"state": "waiting_for_maintenance"}
    owner = _json(store.setting(OWNER_KEY))
    expiry = owner.get("expires_at")
    if (owner.get("schema") != 1 or not owner.get("job_id") or not owner.get("token")
            or type(expiry) not in (int, float) or not math.isfinite(expiry)):
        return {"state": "backup_maintenance_owner_unknown"}
    with store.connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_jobs'").fetchone()
        row = db.execute("SELECT * FROM pipeline_jobs WHERE id=?", (owner["job_id"],)).fetchone() if exists else None
    if (row is None or row["kind"] != JOB_KIND or row["state"] != "running" or row["token"] != owner["token"]):
        return {"state": "backup_maintenance_owner_unknown"}
    if row["lease_until"] > time.time() or owner.get("expires_at", 0) > time.time():
        return {"state": "backup_in_progress"}
    quiet = runtime.assert_quiet()
    with store.exclusive(), store.connect(write=True) as db:
        current = db.execute("SELECT * FROM pipeline_jobs WHERE id=?", (owner["job_id"],)).fetchone()
        if (_json(_setting(db, OWNER_KEY)) != owner or _setting(db, "maintenance") != MAINTENANCE
                or current["token"] != owner["token"] or current["state"] != "running"
                or current["lease_until"] > time.time()):
            return {"state": "backup_owner_changed_during_recovery"}
        _save(db, "maintenance", "off")
        db.execute("DELETE FROM settings WHERE key=?", (OWNER_KEY,))
        hold = _json(_setting(db, HOLD_KEY))
        if hold.get("job_id") == owner["job_id"] and hold.get("token") == owner["token"]:
            db.execute("DELETE FROM settings WHERE key=?", (HOLD_KEY,))
        _save(db, "backup_stale_recovery", {"checked_at": timestamp(), "job_id": owner["job_id"],
            "quiet": {k: quiet.get(k) for k in ("running", "workers_stopped", "writers_stopped", "container_id")}})
    return None


def _resume_eligible(store, runtime):
    access = permission(store)
    if not access["allowed"]:
        return {"state": "not_resumed", "reason": access["reason"]}
    try:
        observed = runtime.set_mode("continuous" if access["mode"] == "continuous" else "pilot")
        return {"state": "resumed", "workers_stopped": observed.get("workers_stopped")}
    except PreservationError as exc:
        return {"state": "resume_failed", "error": str(exc)}


def _readiness(store):
    folder = store.root / "recovery"
    if not (folder / "readiness.json").exists():
        return None
    try:
        ready = json.loads((folder / "readiness.json").read_text())
        recipient = (folder / "recipient.txt").read_text().strip()
        if (ready.get("status") != "verified" or ready.get("recipient") != recipient
                or ready.get("bws_delivery_verified") is not True or ready.get("age_roundtrip_verified") is not True):
            raise PreservationError("recovery_key_not_verified")
        return recipient
    except (OSError, ValueError):
        raise PreservationError("recovery_key_readiness_invalid") from None


def _space_context(store, output, recipient):
    """Cheap inputs that can reduce the previous missing-object estimate."""
    def directory(path):
        if path.is_symlink():
            raise PreservationError("backup_output_parent_unavailable")
        try:
            info = path.stat()
        except FileNotFoundError:
            return None
        return [info.st_dev, info.st_ino, info.st_mtime_ns]
    recipient_id = digest(recipient.encode())
    with store.connect() as db:
        table = db.execute("SELECT 1 FROM sqlite_master WHERE name='payload_deletions'").fetchone()
        deleted = db.execute("SELECT count(*) FROM payload_deletions").fetchone()[0] if table else 0
    return {"output": str(output.resolve()), "recipient": recipient_id, "deleted_payloads": deleted,
            "output_directory": directory(output), "cache_directory": directory(output / "cache" / recipient_id),
            "snapshots_directory": directory(output / "snapshots")}


def _space_gate(store, output, recipient, *, force):
    estimate = _json(store.setting(SPACE_ESTIMATE_KEY))
    if (estimate.get("schema") != 1 or type(estimate.get("minimum_required_bytes")) is not int
            or estimate["minimum_required_bytes"] <= 0):
        return None
    with store.connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_jobs'").fetchone()
        row = db.execute("SELECT * FROM pipeline_jobs WHERE id=?", (estimate.get("job_id"),)).fetchone() if exists else None
    if (row is None or row["kind"] != JOB_KIND or row["state"] != "failed"
            or row["error_code"] != "snapshot_disk_space_low" or row["attempts"] != estimate.get("attempt")):
        return None
    free = shutil.disk_usage(output if output.exists() else output.parent).free
    if not force and _space_context(store, output, recipient) == estimate.get("context") and free < estimate["minimum_required_bytes"]:
        return {"state": "waiting_for_disk_space", "error": "snapshot_disk_space_low", "free_bytes": free,
                "lease": {"id": estimate["job_id"], "attempt": estimate["attempt"], "token": None},
                "minimum_required_bytes": estimate["minimum_required_bytes"], "estimate_scope": "previous_exact_missing_objects",
                "cached_estimate": True, "estimated_at": estimate["checked_at"], "retry_at": estimate["retry_at"],
                "replan_requires": "enough_space_or_changed_inputs_or_force"}
    # An explicit force or changed capacity/input may retry this exact failure.
    # Neither another failure nor a newer/running ownership epoch is altered.
    with store.connect(write=True) as db:
        db.execute("""UPDATE pipeline_jobs SET next_attempt=0 WHERE id=? AND kind=? AND state='failed'
            AND attempts=? AND error_code='snapshot_disk_space_low' AND lease_until<=?""",
            (estimate["job_id"], JOB_KIND, estimate["attempt"], time.time()))
    return None


def _remember_space_failure(store, output, recipient, lease, error):
    free, minimum = getattr(error, "free_bytes", None), getattr(error, "minimum_required_bytes", None)
    if type(free) is not int or type(minimum) is not int or not 0 <= free < minimum:
        return
    value = {"schema": 1, "job_id": lease["id"], "attempt": lease["attempt"], "checked_at": timestamp(),
             "retry_at": time.time() + SPACE_RETRY_SECONDS, "free_bytes": free, "minimum_required_bytes": minimum,
             "context": _space_context(store, output, recipient)}
    with store.connect(write=True) as db:
        _fence_db(db, lease)
        _save(db, SPACE_ESTIMATE_KEY, value)


def tick(store: Store, runtime, client, force: bool = False, *, cancelled=lambda: False,
         drain_seconds: float = 30) -> dict:
    """One independent attempt; caller runs this in its own daemon, never delivery."""
    if type(force) is not bool:
        raise PreservationError("invalid_backup_force")
    if not 0 <= drain_seconds <= 60:
        raise PreservationError("invalid_backup_drain_deadline")
    if cancelled():
        return _report(store, "cancelled")
    try:
        # Cleanup an expired owned barrier even when a later operator disabled
        # backups or the recovery-key readiness file became unavailable.
        recovered = _recover_stale(store, runtime)
        if recovered:
            return _report(store, **recovered)
        if not force and store.setting("backup_service_enabled", "off") != "on":
            return _report(store, "disabled")
        recipient = _readiness(store)
        if recipient is None:
            return _report(store, "waiting_for_recovery_key")
        if store.setting("recovery_state", "ready") != "ready":
            return _report(store, "waiting_for_recovery")
        if not force and time.time() - float(store.setting("backup_last_completed", "0")) < 86400:
            return _report(store, "not_due")
        _recipient(_age(), recipient)
        output = Path(store.setting("backup_object_root", str(store.root / "backups-v2"))).expanduser()
        if not output.is_absolute() or output.is_symlink() or not output.parent.is_dir():
            raise PreservationError("backup_output_parent_unavailable")
        waiting_for_space = _space_gate(store, output, recipient, force=force)
        if waiting_for_space:
            return _report(store, **waiting_for_space)
        # Exact new ciphertext cost is determined after the immutable chunk plan.
        # This cheap preflight reserves only the mutable inputs and small overhead.
        native_estimate = int(store.setting("backup_last_native_bytes", str(32 * 1024**2)))
        minimum = store.db_path.stat().st_size * 2 + native_estimate * 2 + RESERVE_BYTES
        free = shutil.disk_usage(store.root).free
        if free < minimum:
            return _report(store, "waiting_for_disk_space", error="backup_disk_space_low", free_bytes=free,
                           minimum_required_bytes=minimum, estimate_scope="mutable_inputs_only")
        observed = runtime.snapshot()
        if not observed.get("running"):
            return _report(store, "waiting_for_runtime")
        if observed.get("runtime_layout") != "separate_api_worker":
            return _report(store, "waiting_for_separate_runtime")
    except Exception as exc:
        code = str(exc) if isinstance(exc, (PreservationError, CheckpointError)) else "backup_preflight_failed"
        return _report(store, "error", error=code)
    jobs = PipelineJobs(store)
    lease = jobs.begin(JOB_KIND, lease_seconds=LEASE_SECONDS)
    if lease is None:
        current = next((row for row in jobs.snapshot() if row["kind"] == JOB_KIND and row["object_id"] == "service"), {})
        return _report(store, "backup_in_progress" if current.get("state") == "running" else "waiting_to_retry",
                       retry_at=current.get("next_attempt"), lease_until=current.get("lease_until"),
                       error=current.get("error_code"))
    with store.connect() as db:
        attempt = db.execute("SELECT attempts FROM pipeline_jobs WHERE id=? AND token=? AND state='running'",
                             (lease["id"], lease["token"])).fetchone()
    if attempt is None:
        return _report(store, "error", lease=lease, error="backup_lease_lost")
    lease["attempt"] = attempt[0]
    owned_maintenance = False
    released = False
    resume = None
    try:
        with _LeaseKeeper(store, jobs, lease, cancelled) as keeper:
            _reserve(store, lease)
            keeper.phase = "draining"
            keeper.progress()
            deadline = time.monotonic() + drain_seconds
            while True:
                keeper.renew()
                native_processing = int(client.list_operations(status="processing", limit=1)["total"])
                local_inflight = inflight_count(store)
                keeper.progress({"native_processing": native_processing, "local_inflight": local_inflight})
                if not native_processing and not local_inflight or time.monotonic() >= deadline:
                    break
                _report(store, "draining", lease=lease, native_processing=native_processing, local_inflight=local_inflight,
                        new_submissions_held=True, polls_allowed=True)
                time.sleep(min(1, max(0, deadline - time.monotonic())))
            keeper.phase = "native_snapshot"
            keeper.progress()
            _enter_quiet(store, lease)
            owned_maintenance = True
            runtime.set_mode("safe")
            quiet = runtime.assert_quiet()
            keeper.fence()
            native_started = timestamp()
            stage_root = store.root / "snapshot-staging"
            stage_root.mkdir(exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(prefix="native-", dir=stage_root) as temporary:
                native = Path(temporary) / "hindsight.zip"
                keeper.progress({"state": "exporting_native"})
                _native_backup(runtime, native, cancelled=cancelled, progress=keeper.fence)
                keeper.progress({"state": "native_exported", "bytes": native.stat().st_size})
                quiet = runtime.assert_quiet()
                proof = {**quiet, "writers_stopped": True, "writer_scope": "native_submissions_and_workers",
                         "native_export_started_at": native_started, "native_export_finished_at": timestamp(),
                         "local_capture_continues": True, "local_revocations_continue": True}
                with prepare_snapshot(store, native, runtime_receipt=proof, fence=keeper.fence) as prepared:
                    keeper.progress({"state": "sqlite_snapshotted", "versions": len(prepared["local"]["version_ids"]),
                                     "changes_max_id": prepared["local"]["changes_max_id"]})
                    _release(store, lease)
                    owned_maintenance = False
                    released = True
                    keeper.phase = "resuming_workers"
                    keeper.progress()
                    resume = _resume_eligible(store, runtime)
                    keeper.phase = "encrypting"
                    def publication(commit):
                        # A new epoch cannot be claimed while the canonical
                        # envelope/creation ledger is being published. A crash
                        # can leave an unregistered envelope, never a false proof.
                        with store.exclusive(), store.connect(write=True) as db:
                            _fence_db(db, lease)
                            keeper.fence()
                            commit(db)
                            keeper.fence()
                            _fence_db(db, lease)
                    receipt = encrypt_snapshot(prepared, recipient, output, progress=keeper.progress,
                                               fence=keeper.fence, publication=publication)
                    keeper.fence()
                    envelope = output / "snapshots" / (receipt["checkpoint_id"] + ".json")
                    if envelope.read_bytes() != canonical(receipt):
                        raise PreservationError("backup_envelope_not_published")
                    full_receipt = {**receipt, "path": str(envelope), "object_root": str(output),
                                    "size": sum(obj["size"] for obj in receipt["objects"]),
                                    "sha256": digest(canonical(receipt)), "native_bytes": native.stat().st_size}
                    # All service completion writers use the same short lock.
                    # A new reservation cannot race this successful projection.
                    with store.exclusive():
                        keeper.fence()
                        if not jobs.succeed(lease, {"checkpoint_id": receipt["checkpoint_id"],
                            "objects": len(receipt["objects"]), "versions": receipt["versions"],
                            "remote_state": "unconfirmed", "restore_verified": False}):
                            raise PreservationError("backup_lease_lost")
                        with store.connect(write=True) as db:
                            _save(db, "backup_last_completed", str(time.time()))
                            _save(db, "backup_last_receipt", full_receipt)
                            _save(db, "backup_last_native_bytes", str(native.stat().st_size))
                            db.execute("DELETE FROM settings WHERE key=?", (SPACE_ESTIMATE_KEY,))
                    return _report(store, "created", lease=lease, checkpoint_id=receipt["checkpoint_id"], path=str(envelope),
                                   remote_state="unconfirmed", restore_verified=False, runtime_resume=resume)
    except Exception as exc:
        code = str(exc) if isinstance(exc, (PreservationError, CheckpointError)) else "backup_attempt_failed"
        if not re.fullmatch(r"[a-z_]{1,120}", code):
            code = "backup_attempt_failed"
        cleanup_failed = False
        if not released:
            try:
                if owned_maintenance:
                    runtime.assert_quiet()
                _release(store, lease)
                released = True
                resume = _resume_eligible(store, runtime) if owned_maintenance else None
            except (PreservationError, OSError):
                cleanup_failed = True
        # Retain a running expiring epoch if its barrier cannot be cleared safely;
        # stale recovery then has the exact token and expiry needed for its CAS.
        if not cleanup_failed:
            if code == "snapshot_disk_space_low":
                try:
                    _remember_space_failure(store, output, recipient, lease, exc)
                except (PreservationError, OSError):
                    pass  # An expired/replaced attempt cannot publish a new gate.
            jobs.fail(lease, code, scope="recovery" if code == "backup_lease_lost" else "stage",
                      retry_after=SPACE_RETRY_SECONDS if code == "snapshot_disk_space_low" else 30)
        details = {"error": code, "error_type": type(exc).__name__, "stale_recovery_required": cleanup_failed,
                   "runtime_resume": resume}
        if code == "snapshot_disk_space_low":
            details.update(free_bytes=getattr(exc, "free_bytes", None),
                minimum_required_bytes=getattr(exc, "minimum_required_bytes", None),
                estimate_scope="exact_missing_objects")
        state = "cancelled" if code == "backup_cancelled" else "waiting_for_disk_space" if code == "snapshot_disk_space_low" else "error"
        return _report(store, state, lease=lease, **details)
