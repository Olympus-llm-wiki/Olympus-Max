"""Daily key-gated checkpoint creation; Drive publication is a separate job."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import uuid

from .backup import create_checkpoint, CheckpointError
from .preservation import Store, PreservationError, digest, timestamp
from .runtime_control import RuntimeControl
from .admission import inflight_count, permission


def backup_space_status(store: Store) -> dict | None:
    """Conservative local staging/ciphertext floor, excluding native ZIP growth."""
    local_bytes = store.db_path.stat().st_size
    for folder in store.versions.iterdir():
        if folder.is_dir() and not folder.is_symlink():
            for name in ("original", "text.txt", "manifest.json", "manifest.sha256"):
                path = folder / name
                if path.is_file():
                    local_bytes += path.stat().st_size
    # create_checkpoint copies the full corpus into staging before streaming age.
    minimum = 2 * local_bytes + 1024 ** 3
    free = shutil.disk_usage(store.root).free
    if free < minimum:
        return {"state": "waiting_for_disk_space", "error": "backup_disk_space_low",
                "free_bytes": free, "minimum_required_bytes": minimum}
    return None


def continuous_backup_window(store: Store, runtime: RuntimeControl, client) -> dict | None:
    """Reserve a quiet gap between sources for an already configured daily backup."""
    if (store.setting("delivery_mode", "pilot") != "continuous"
            or not permission(store)["allowed"]
            or not (store.root / "recovery/readiness.json").exists()
            or time.time() - float(store.setting("backup_last_completed", "0")) < 86400
            or store.setting("maintenance", "off") != "off"
            or store.setting("recovery_state", "ready") != "ready"
            or inflight_count(store)):
        return None
    space = backup_space_status(store)
    if space:
        return {**space, "allow_delivery": True}
    # Let a running consolidation finish; do not interrupt it to take a copy.
    if client.list_operations(status="processing", limit=1)["total"]:
        return {"state": "waiting_for_native_completion"}
    with store.exclusive():
        if inflight_count(store) or store.setting("maintenance", "off") != "off":
            return None
        store.set_setting("maintenance", "backup_window")
    try:
        runtime.set_mode("safe")
        with store.exclusive():
            store.set_setting("maintenance", "off")
            return maybe_backup(store, runtime)
    finally:
        if store.setting("maintenance", "off") == "backup_window":
            store.set_setting("maintenance", "off")


def _hash_file(path: Path, *, timeout: float = 5) -> str:
    from .bounded_io import hash_file
    return hash_file(path, max_bytes=64 * 1024**3, timeout=timeout)


def _native_backup(runtime: RuntimeControl, destination: Path, *, cancelled=lambda: False, progress=None) -> None:
    key = os.environ.get("OLYMPUS_HINDSIGHT_API_KEY")
    if not key:
        raise PreservationError("backup_api_key_unavailable")
    if len(key.encode()) > 8192:
        raise PreservationError("backup_api_key_invalid")
    path = "/tmp/olympus-checkpoint-" + uuid.uuid4().hex + ".zip"
    program = (
        'import os,sys,signal; signal.alarm(300); key=sys.stdin.buffer.read().decode(); '
        'os.environ["HINDSIGHT_API_TENANT_API_KEY"]=key; '
        'os.execvp("hindsight-admin",["hindsight-admin","backup",sys.argv[1],"--schema","public"])'
    )
    def run(command, *, input_data=None, timeout):
        process = subprocess.Popen(command, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            if input_data is not None:
                process.stdin.write(input_data); process.stdin.close()
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if cancelled():
                    raise PreservationError("backup_cancelled")
                if time.monotonic() >= deadline:
                    raise PreservationError("native_backup_timeout")
                if progress:
                    progress()
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
            return process.returncode
        finally:
            if process.poll() is None:
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(process.pid, sig)
                    except ProcessLookupError:
                        break
                    try:
                        process.wait(timeout=1)
                        break
                    except subprocess.TimeoutExpired:
                        continue
    try:
        code = run([*runtime.compose, "exec", "-T", "hindsight", "python", "-c", program, path],
                   input_data=key.encode(), timeout=300)
        if code:
            raise PreservationError("native_backup_failed")
        copied = run([*runtime.compose, "cp", "hindsight:" + path, str(destination)], timeout=120)
        if copied:
            raise PreservationError("native_backup_copy_failed")
        destination.chmod(0o600)
    finally:
        subprocess.run([*runtime.compose, "exec", "-T", "hindsight", "rm", "-f", path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)


def maybe_backup(store: Store, runtime: RuntimeControl, *, force: bool = False) -> dict:
    recovery = store.root / "recovery"
    readiness = recovery / "readiness.json"
    if not readiness.exists():
        return {"state": "waiting_for_recovery_key"}
    try:
        ready = json.loads(readiness.read_text())
        recipient = (recovery / "recipient.txt").read_text().strip()
        if (ready.get("status") != "verified" or ready.get("recipient") != recipient
                or ready.get("bws_delivery_verified") is not True or ready.get("age_roundtrip_verified") is not True):
            raise PreservationError("recovery_key_not_verified")
    except (OSError, ValueError):
        raise PreservationError("recovery_key_readiness_invalid") from None
    if not force and time.time() - float(store.setting("backup_last_completed", "0")) < 86400:
        return {"state": "not_due"}
    if store.setting("maintenance", "off") != "off" or store.setting("recovery_state", "ready") != "ready":
        return {"state": "waiting_for_maintenance"}
    space = backup_space_status(store)
    if space:
        return space
    observed = runtime.snapshot()
    if not observed.get("running") or not observed.get("workers_stopped"):
        return {"state": "waiting_for_quiet_runtime"}
    output = store.root / "backups"
    output.mkdir(exist_ok=True, mode=0o700)
    with store.exclusive():
        if store.setting("maintenance", "off") != "off":
            return {"state": "waiting_for_maintenance"}
        store.set_setting("maintenance", "backup")
        try:
            proof = runtime.assert_quiet()
            with tempfile.TemporaryDirectory(prefix=".native-", dir=output) as temporary:
                native = Path(temporary) / "hindsight.zip"
                _native_backup(runtime, native)
                runtime.assert_quiet()
                receipt = create_checkpoint(store, native, recipient, output, runtime_receipt=proof)
            store.set_setting("backup_last_completed", str(time.time()))
            store.set_setting("backup_last_receipt", json.dumps(receipt))
            return {"state": "created", "path": receipt["path"], "remote_state": "unconfirmed"}
        finally:
            store.set_setting("maintenance", "off")


def publish_backups(store: Store, library_root: Path, *, audit: bool = False,
                    time_budget: float = 10, progress=None) -> dict:
    from .bounded_io import fingerprint
    def checksum_file(path):
        return _hash_file(path, timeout=60 if audit else 5)
    output = store.root / "backups"
    if not output.is_dir():
        return {"staged": 0, "remote_verified": 0}
    target = library_root / "Backups"
    target.mkdir(exist_ok=True)
    result = {"staged": 0, "already_present": 0, "cached": 0, "deferred": 0, "errors": [], "remote_verified": 0}
    deadline = time.monotonic() + time_budget
    cursor_key = "backup_publish_cursor:" + digest(str(library_root).encode())
    cursor = store.setting(cursor_key, "")
    files = sorted(output.glob("*.age"))
    files = [f for f in files if f.name > cursor] + [f for f in files if f.name <= cursor]
    def fence():
        if progress:
            progress({k: result[k] for k in ("staged", "already_present", "cached", "deferred")})
    for source in files:
        fence()
        receipt_key = "backup_publication:" + source.name
        destination = target / source.name
        source_fp = destination_fp = None
        try:
            prior = json.loads(store.setting(receipt_key, "{}"))
        except ValueError:
            prior = {}
        try:
            if (not audit and prior.get("status") != "audit_required"
                    and prior.get("next_attempt_at", 0) > time.time()):
                result["deferred"] += 1
                continue
            if source.is_symlink():
                raise PreservationError("backup_source_symlink")
            source_fp = fingerprint(source)
            destination_fp = fingerprint(destination) if destination.exists() else None
            if (not audit and prior.get("status") == "audit_required"
                    and prior.get("path") == str(destination)
                    and prior.get("source") == source_fp and prior.get("destination") == destination_fp):
                # Metadata anchors only suppress another expensive failed attempt;
                # they never establish integrity or turn this into a staged copy.
                result["errors"].append({"filename": source.name, "code": "backup_audit_required", "errno": None})
                result["deferred"] += 1
                continue
            if destination_fp is not None:
                if (not audit and prior.get("status") == "local_staged"
                        and prior.get("path") == str(destination)
                        and prior.get("source") == source_fp and prior.get("destination") == destination_fp):
                    result["already_present"] += 1
                    result["cached"] += 1
                    continue
                checksum = checksum_file(source)
                if source_fp["bytes"] != destination_fp["bytes"] or checksum != checksum_file(destination):
                    raise PreservationError("backup_destination_conflict") from None
                result["already_present"] += 1
            else:
                fence()
                fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=target)
                try:
                    with os.fdopen(fd, "wb") as stream, source.open("rb") as original:
                        shutil.copyfileobj(original, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    checksum = checksum_file(source)
                    if checksum != checksum_file(Path(temporary)):
                        raise PreservationError("backup_copy_mismatch")
                    try:
                        fence()
                        os.link(temporary, destination)
                    except FileExistsError:
                        if destination.is_symlink() or checksum != checksum_file(destination):
                            raise PreservationError("backup_destination_conflict") from None
                    result["staged"] += 1
                    store.set_setting("backup_staged:" + source.name, timestamp())
                finally:
                    Path(temporary).unlink(missing_ok=True)
            fence()
            store.set_setting(receipt_key, json.dumps({"status": "local_staged", "path": str(destination),
                "source": fingerprint(source), "destination": fingerprint(destination), "sha256": checksum,
                "checked_at": timestamp(), "remote_state": "unconfirmed"}))
        except (PreservationError, OSError) as exc:
            if isinstance(exc, PreservationError) and str(exc) == "stage_lease_lost":
                raise
            fence()
            code = str(exc) if isinstance(exc, PreservationError) else "backup_publication_io_error"
            error = {"filename": source.name, "code": code, "errno": getattr(exc, "errno", None)}
            result["errors"].append(error)
            result["deferred"] += 1
            receipt = {"status": "pending", **error, "next_attempt_at": time.time() + 60}
            if code == "file_read_timeout" and source_fp is not None:
                receipt.update({"status": "audit_required", "path": str(destination),
                                "source": source_fp, "destination": destination_fp,
                                "remote_state": "unconfirmed"})
            store.set_setting(receipt_key, json.dumps(receipt))
        finally:
            fence()
            store.set_setting(cursor_key, source.name)
            if progress:
                progress({k: result[k] for k in ("staged", "already_present", "cached", "deferred")})
        if time.monotonic() >= deadline:
            break
    # Retention never removes a copy before verified off-device recovery exists.
    return result
