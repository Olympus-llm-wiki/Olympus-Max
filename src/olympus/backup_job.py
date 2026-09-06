"""Daily key-gated checkpoint creation; Drive publication is a separate job."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

from .backup import create_checkpoint, CheckpointError
from .preservation import Store, PreservationError, digest, timestamp
from .runtime_control import RuntimeControl


def _hash_file(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _native_backup(runtime: RuntimeControl, destination: Path) -> None:
    key = os.environ.get("OLYMPUS_HINDSIGHT_API_KEY")
    if not key:
        raise PreservationError("backup_api_key_unavailable")
    path = "/tmp/olympus-checkpoint-" + uuid.uuid4().hex + ".zip"
    program = (
        'import os,sys; key=sys.stdin.buffer.read().decode(); '
        'os.environ["HINDSIGHT_API_TENANT_API_KEY"]=key; '
        'os.execvp("hindsight-admin",["hindsight-admin","backup",sys.argv[1],"--schema","public"])'
    )
    try:
        process = subprocess.run([*runtime.compose, "exec", "-T", "hindsight", "python", "-c", program, path],
                                 input=key.encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        if process.returncode:
            raise PreservationError("native_backup_failed")
        copied = subprocess.run([*runtime.compose, "cp", "hindsight:" + path, str(destination)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        if copied.returncode:
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


def publish_backups(store: Store, library_root: Path) -> dict:
    output = store.root / "backups"
    if not output.is_dir():
        return {"staged": 0, "remote_verified": 0}
    target = library_root / "Backups"
    target.mkdir(exist_ok=True)
    staged = 0
    for source in sorted(output.glob("*.age")):
        if source.is_symlink():
            raise PreservationError("backup_source_symlink")
        destination = target / source.name
        if destination.exists():
            if destination.is_symlink() or _hash_file(source) != _hash_file(destination):
                raise PreservationError("backup_destination_conflict")
            continue
        fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=target)
        try:
            with os.fdopen(fd, "wb") as stream, source.open("rb") as original:
                shutil.copyfileobj(original, stream)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if destination.is_symlink() or _hash_file(source) != _hash_file(destination):
                    raise PreservationError("backup_destination_conflict") from None
            store.set_setting("backup_staged:" + source.name, timestamp())
            staged += 1
        finally:
            Path(temporary).unlink(missing_ok=True)
    # Retention never removes a copy before verified off-device recovery exists.
    return {"staged": staged, "remote_verified": 0}
