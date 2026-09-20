"""Version 2 checkpoints: immutable age objects and an encrypted file manifest.

Preparation freezes only mutable SQLite/native inputs. Originals remain anchored
immutable files, read outside the writer lock. A new generation reuses verified
objects; restore needs one bounded plaintext object plus the final target, never
a second full plaintext tar. Network transport and owner retention are separate.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid

from .backup import (CheckpointError, HASH, MAX_FILE_BYTES, MAX_MANIFEST_BYTES,
                     _age, _recipient, _safe_receipt, _copy_regular, _local_inventory,
                     _database, _validate_versions, _version_files, _member_name,
                     _decrypted, _sha, validate_native_backup)
from .bounded_io import fingerprint
from .preservation import canonical, digest, guard_no_secrets

SCHEMA = 2
DEFAULT_CHUNK_BYTES = 16 * 1024**2
MAX_OBJECT_BYTES = 32 * 1024**2
RESERVE_BYTES = 64 * 1024**2


def _atomic_json(path: Path, value: dict, *, fence=None) -> None:
    if fence:
        fence()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value)); stream.flush(); os.fsync(stream.fileno())
        if fence:
            fence()
        os.replace(temporary, path)
        _sync(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _sync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _object_path(root: Path, object_id: str) -> Path:
    if not isinstance(object_id, str) or not HASH.fullmatch(object_id):
        raise CheckpointError("invalid_snapshot_object_id")
    path = root / "objects" / object_id[:2] / (object_id + ".age")
    if any(p.is_symlink() for p in (root, root / "objects", path.parent, path)):
        raise CheckpointError("snapshot_object_symlink")
    return path


def _register_created(store, root: Path, receipt: dict, *, connection=None, fence=None) -> None:
    key = "snapshot_created:" + receipt["checkpoint_id"]
    value = {"receipt": receipt, "object_root": str(root), "receipt_sha256": digest(canonical(receipt))}
    guard_no_secrets(canonical(value))
    if fence:
        fence()
    row = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone() if connection else None
    prior = row[0] if row else store.setting(key) if connection is None else None
    if prior and json.loads(prior) != value:
        raise CheckpointError("snapshot_receipt_conflict")
    if connection is not None:
        connection.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, canonical(value).decode()))
    else:
        store.set_setting(key, canonical(value).decode())


@contextmanager
def prepare_snapshot(store, native_backup: Path, *, runtime_receipt: dict,
                     temporary_root: Path | None = None, fence=None):
    """Yield a frozen SQLite/native inventory; release writer lock before yield.

    Caller may resume native workers after this function yields and before
    encryption. The context owns only its small mutable-input staging directory.
    """
    runtime = _safe_receipt(runtime_receipt, require_frozen=True)
    parent = Path(temporary_root or store.root / "snapshot-staging")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="prepared-", dir=parent) as temporary:
        staging = Path(temporary)
        (staging / "local").mkdir(mode=0o700)
        with store.exclusive():
            if fence:
                fence()
            path = staging / "local/registry.sqlite3"
            target = sqlite3.connect(path)
            try:
                with store.connect() as db:
                    db.backup(target)
                target.execute("PRAGMA journal_mode=DELETE")
            finally:
                target.close()
            path.chmod(0o600)
        native_file = staging / "native/hindsight.zip"
        native_info = _copy_regular(Path(native_backup), native_file, fence=fence)
        local = _local_inventory(staging / "local")
        native = validate_native_backup(native_file)
        if runtime.get("native_backup_sha256", native_info["sha256"]) != native_info["sha256"]:
            raise CheckpointError("native_backup_hash_mismatch")
        files = {"local/registry.sqlite3": {"path": str(path), "size": path.stat().st_size, "sha256": _sha(path)},
                 "native/hindsight.zip": {"path": str(native_file), **native_info}}
        with _database(path) as db:
            anchors = dict(db.execute("SELECT version_id,sha256 FROM manifest_integrity"))
        for vid in local["version_ids"]:
            if fence:
                fence()
            folder = store.versions / vid
            manifest_raw = (folder / "manifest.json").read_bytes()
            manifest = json.loads(manifest_raw)
            if digest(manifest_raw) != anchors.get(vid) or (folder / "manifest.sha256").read_text() != anchors[vid]:
                raise CheckpointError("source_manifest_anchor_mismatch")
            for name in _version_files(local, vid):
                source = folder / name
                info = fingerprint(source)
                expected = manifest["original_sha256"] if name == "original" else manifest["text_sha256"] if name == "text.txt" else digest(manifest_raw) if name == "manifest.json" else digest(anchors[vid].encode())
                files[f"local/versions/{vid}/{name}"] = {"path": str(source), "size": info["bytes"], "sha256": expected}
        for entry in files.values():
            entry["fingerprint"] = fingerprint(Path(entry["path"]))
        yield {"schema": SCHEMA, "checkpoint_id": str(uuid.uuid4()), "_store": store,
               "created_at": datetime.now(timezone.utc).isoformat(), "runtime_receipt": runtime,
               "local": local, "native": native, "files": files}


def _cached(root: Path, recipient_id: str, plain_hash: str, plain_size: int) -> dict | None:
    path = root / "cache" / recipient_id / (plain_hash + ".json")
    try:
        value = json.loads(path.read_text())
        object_file = _object_path(root, value["id"])
        if value["plain_sha256"] != plain_hash or value["plain_size"] != plain_size:
            return None
        if fingerprint(object_file) != value.get("fingerprint"):
            if _sha(object_file) != value["id"]:
                raise CheckpointError("snapshot_object_hash_mismatch")
            value["fingerprint"] = fingerprint(object_file)
            _atomic_json(path, value)
        return value
    except FileNotFoundError:
        return None
    except (KeyError, ValueError, TypeError):
        raise CheckpointError("invalid_snapshot_object_cache") from None


def _encrypt_object(root: Path, recipient: str, data: bytes, *, cache: bool = True, fence=None) -> dict:
    if fence:
        fence()
    recipient_id, plain_hash = digest(recipient.encode()), digest(data)
    if cache:
        prior = _cached(root, recipient_id, plain_hash, len(data))
        if prior:
            return prior
    fd, temporary = tempfile.mkstemp(prefix=".object-", dir=root)
    try:
        with os.fdopen(fd, "wb") as stream:
            process = subprocess.Popen([_age(), "--encrypt", "--recipient", recipient], stdin=subprocess.PIPE,
                stdout=stream, stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                remaining_input = data
                deadline = time.monotonic() + 60
                while True:
                    if fence:
                        fence()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CheckpointError("age_encryption_timeout")
                    try:
                        process.communicate(input=remaining_input, timeout=min(0.2, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        remaining_input = None
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=1)
            if process.returncode:
                raise CheckpointError("age_encryption_failed")
            stream.flush(); os.fsync(stream.fileno())
        temp = Path(temporary)
        object_id = _sha(temp)
        target = _object_path(root, object_id)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if fence:
            fence()
        try:
            os.link(temp, target)
        except FileExistsError:
            if _sha(target) != object_id:
                raise CheckpointError("snapshot_object_hash_mismatch")
        _sync(target.parent)
        temp.unlink()  # Capture the stable one-link fingerprint, after publication.
        value = {"id": object_id, "size": target.stat().st_size,
                 "plain_sha256": plain_hash, "plain_size": len(data), "fingerprint": fingerprint(target)}
        if cache:
            _atomic_json(root / "cache" / recipient_id / (plain_hash + ".json"), value, fence=fence)
        return value
    finally:
        Path(temporary).unlink(missing_ok=True)


def encrypt_snapshot(prepared: dict, recipient: str, output_dir: Path, *,
                     chunk_bytes: int = DEFAULT_CHUNK_BYTES, progress=None, fence=None, publication=None) -> dict:
    """Plan exact missing chunks, preflight space, then publish manifest last."""
    if prepared.get("schema") != SCHEMA or not 1 <= chunk_bytes <= DEFAULT_CHUNK_BYTES:
        raise CheckpointError("invalid_snapshot_plan")
    binary = _age(); _recipient(binary, recipient)
    root = Path(output_dir).expanduser()
    if not root.is_absolute() or root.is_symlink():
        raise CheckpointError("invalid_snapshot_directory")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(root / ".snapshot.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        lock_deadline = time.monotonic() + 30
        while True:
            if fence:
                fence()
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= lock_deadline:
                    raise CheckpointError("snapshot_lock_busy")
                time.sleep(0.1)
        saved_receipt = root / "snapshots" / (prepared["checkpoint_id"] + ".json")
        if saved_receipt.exists():
            previous = json.loads(saved_receipt.read_text())
            if previous.get("checkpoint_id") != prepared["checkpoint_id"]:
                raise CheckpointError("snapshot_receipt_conflict")
            for obj in previous["objects"]:
                if fence:
                    fence()
                if _sha(_object_path(root, obj["id"])) != obj["id"]:
                    raise CheckpointError("snapshot_object_hash_mismatch")
            def reuse(connection=None):
                _register_created(prepared["_store"], root, previous, connection=connection, fence=fence)
            if publication:
                publication(reuse)
            else:
                reuse()
            return previous
        planned, unique_missing = {}, {}
        planned_bytes = 0
        total_plain_bytes = sum(e["size"] for e in prepared["files"].values())
        recipient_id = digest(recipient.encode())
        for name, entry in prepared["files"].items():
            if fence:
                fence()
            source = Path(entry["path"])
            if fingerprint(source) != entry["fingerprint"]:
                raise CheckpointError("source_changed_during_checkpoint")
            pieces, offset, hasher = [], 0, hashlib.sha256()
            last_chunk_report = 0
            with source.open("rb") as stream:
                while block := stream.read(chunk_bytes):
                    if fence:
                        fence()
                    plain_hash = digest(block)
                    hasher.update(block)
                    cached = _cached(root, recipient_id, plain_hash, len(block))
                    pieces.append({"offset": offset, "size": len(block), "sha256": plain_hash, "cached": cached})
                    if cached is None:
                        unique_missing[plain_hash] = len(block)
                    offset += len(block)
                    if progress and offset - last_chunk_report >= 64 * 1024**2:
                        progress({"stage": "planned", "files": len(planned),
                                  "planned_bytes": planned_bytes + offset, "total_bytes": total_plain_bytes})
                        last_chunk_report = offset
            if offset != entry["size"] or hasher.hexdigest() != entry["sha256"] or fingerprint(source) != entry["fingerprint"]:
                raise CheckpointError("source_hash_mismatch")
            planned[name] = pieces
            planned_bytes += offset
            if progress:
                progress({"stage": "planned", "files": len(planned), "planned_bytes": planned_bytes,
                          "total_bytes": total_plain_bytes})
        # age overhead plus manifest and a small reserve. All final objects remain
        # immutable; this bounds transient staging, not total retained generations.
        missing_bytes = sum(unique_missing.values())
        required = missing_bytes + missing_bytes // 100 + len(unique_missing) * 1024 + MAX_MANIFEST_BYTES + RESERVE_BYTES
        free = shutil.disk_usage(root).free
        if free < required:
            error = CheckpointError("snapshot_disk_space_low")
            error.free_bytes = free
            error.minimum_required_bytes = required
            raise error
        manifest = {k: prepared[k] for k in ("schema", "checkpoint_id", "created_at", "runtime_receipt", "local", "native")}
        manifest.update({"format": "olympus-age-objects", "recipient_sha256": recipient_id,
                         "chunk_bytes": chunk_bytes, "files": {}})
        objects, created, reused = {}, 0, 0
        processed_bytes, created_bytes = 0, 0
        for name, pieces in planned.items():
            entry = prepared["files"][name]
            parts = []
            file_processed, last_chunk_report = 0, 0
            with Path(entry["path"]).open("rb") as stream:
                for piece in pieces:
                    if fence:
                        fence()
                    block = stream.read(piece["size"])
                    if digest(block) != piece["sha256"]:
                        raise CheckpointError("source_changed_during_checkpoint")
                    cached = _cached(root, recipient_id, piece["sha256"], piece["size"])
                    obj = cached or _encrypt_object(root, recipient, block, fence=fence)
                    reused += bool(cached)
                    created += not bool(cached)
                    created_bytes += obj["size"] if not cached else 0
                    objects[obj["id"]] = {"id": obj["id"], "size": obj["size"]}
                    parts.append({"object_id": obj["id"], "offset": piece["offset"],
                                  "size": piece["size"], "sha256": piece["sha256"]})
                    file_processed += piece["size"]
                    if progress and file_processed - last_chunk_report >= 64 * 1024**2:
                        progress({"stage": "encrypted", "files": len(manifest["files"]), "created": created,
                            "reused": reused, "processed_plain_bytes": processed_bytes + file_processed,
                            "created_ciphertext_bytes": created_bytes, "total_bytes": total_plain_bytes})
                        last_chunk_report = file_processed
            if fingerprint(Path(entry["path"])) != entry["fingerprint"]:
                raise CheckpointError("source_changed_during_checkpoint")
            manifest["files"][name] = {"size": entry["size"], "sha256": entry["sha256"], "parts": parts}
            processed_bytes += entry["size"]
            if progress:
                progress({"stage": "encrypted", "files": len(manifest["files"]), "created": created, "reused": reused,
                          "processed_plain_bytes": processed_bytes, "created_ciphertext_bytes": created_bytes,
                          "total_bytes": total_plain_bytes})
        data = canonical(manifest)
        if len(data) > MAX_MANIFEST_BYTES:
            raise CheckpointError("snapshot_manifest_too_large")
        manifest_object = _encrypt_object(root, recipient, data, cache=False, fence=fence)
        objects[manifest_object["id"]] = {"id": manifest_object["id"], "size": manifest_object["size"]}
        receipt = {"schema": SCHEMA, "format": "olympus-age-objects", "checkpoint_id": prepared["checkpoint_id"],
                   "created_at": prepared["created_at"], "manifest_object": manifest_object["id"],
                   "manifest_sha256": digest(data), "objects": sorted(objects.values(), key=lambda x: x["id"]),
                   "files": len(manifest["files"]), "versions": len(prepared["local"]["version_ids"]),
                   "version_ids": prepared["local"]["version_ids"], "changes_max_id": prepared["local"]["changes_max_id"],
                   "plain_bytes": sum(e["size"] for e in prepared["files"].values()),
                   "created_parts": created, "reused_parts": reused,
                   "remote_state": "unconfirmed", "restore_verified": False}
        def commit(connection=None):
            _atomic_json(root / "snapshots" / (prepared["checkpoint_id"] + ".json"), receipt, fence=fence)
            _register_created(prepared["_store"], root, receipt, connection=connection, fence=fence)
        if fence:
            fence()
        if publication:
            publication(commit)
        else:
            commit()
        return receipt
    finally:
        os.close(lock)


def _identity_bytes(fd: int) -> bytes:
    if type(fd) is not int or fd < 0:
        raise CheckpointError("invalid_identity_descriptor")
    identity, deadline = bytearray(), time.monotonic() + 5
    while len(identity) <= 8192:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise CheckpointError("invalid_identity_format")
        block = os.read(fd, 8193 - len(identity))
        if not block:
            break
        identity.extend(block)
    keys = re.findall(rb"(?<![A-Z0-9-])AGE-SECRET-KEY-1[A-Z0-9]+(?![A-Z0-9-])", identity)
    if len(identity) > 8192 or len(keys) != 1:
        raise CheckpointError("invalid_identity_format")
    return keys[0] + b"\n"


def _decrypt_object(root: Path, object_id: str, identity: bytes, size_limit: int) -> bytes:
    path = _object_path(root, object_id)
    try:
        size = path.stat().st_size
    except OSError:
        raise CheckpointError("snapshot_object_unavailable") from None
    if size > MAX_OBJECT_BYTES or _sha(path) != object_id:
        raise CheckpointError("snapshot_object_hash_mismatch")
    incoming, outgoing = os.pipe()
    try:
        os.write(outgoing, identity)
    finally:
        os.close(outgoing)
    try:
        with _decrypted(path, incoming) as plaintext:
            data = plaintext.read(size_limit + 1)
            if len(data) > size_limit:
                raise CheckpointError("snapshot_plaintext_limit")
            return data
    finally:
        os.close(incoming)


def _validate_manifest(receipt: dict, manifest: dict) -> dict:
    """Validate complete inventory and coverage before creating output members."""
    try:
        if (manifest.get("schema") != SCHEMA or manifest.get("checkpoint_id") != receipt.get("checkpoint_id")
                or manifest.get("format") != "olympus-age-objects"
                or type(manifest.get("chunk_bytes")) is not int
                or not 1 <= manifest["chunk_bytes"] <= DEFAULT_CHUNK_BYTES
                or str(uuid.UUID(manifest["checkpoint_id"])) != manifest["checkpoint_id"]):
            raise CheckpointError("invalid_snapshot_manifest")
        if datetime.fromisoformat(manifest["created_at"]).tzinfo is None:
            raise CheckpointError("invalid_snapshot_manifest")
        _safe_receipt(manifest.get("runtime_receipt"), require_frozen=True)
        files = manifest.get("files", {})
        if (not isinstance(files, dict) or len(files) > 100000
                or not {"local/registry.sqlite3", "native/hindsight.zip"} <= set(files)):
            raise CheckpointError("snapshot_inventory_mismatch")
        if any(not _member_name(name) or name == "checkpoint.json" for name in files):
            raise CheckpointError("unsafe_checkpoint_member")
        object_ids = set()
        for obj in receipt["objects"]:
            if (set(obj) != {"id", "size"} or not HASH.fullmatch(obj["id"])
                    or type(obj["size"]) is not int or not 0 < obj["size"] <= MAX_OBJECT_BYTES
                    or obj["id"] in object_ids):
                raise CheckpointError("snapshot_object_inventory_mismatch")
            object_ids.add(obj["id"])
        needed = {receipt["manifest_object"]}
        for entry in files.values():
            if (set(entry) != {"size", "sha256", "parts"} or type(entry["size"]) is not int
                    or not 0 <= entry["size"] <= MAX_FILE_BYTES or not HASH.fullmatch(entry["sha256"])
                    or not isinstance(entry["parts"], list)):
                raise CheckpointError("invalid_snapshot_member")
            count = 0
            for part in entry["parts"]:
                if (set(part) != {"offset", "size", "sha256", "object_id"}
                        or type(part["offset"]) is not int or part["offset"] != count
                        or type(part["size"]) is not int or not 0 < part["size"] <= manifest["chunk_bytes"]
                        or not HASH.fullmatch(part["sha256"]) or not HASH.fullmatch(part["object_id"])):
                    raise CheckpointError("snapshot_part_coverage_mismatch")
                count += part["size"]
                needed.add(part["object_id"])
            if count != entry["size"]:
                raise CheckpointError("snapshot_part_coverage_mismatch")
        if needed != object_ids:
            raise CheckpointError("snapshot_object_inventory_mismatch")
        return files
    except (AttributeError, KeyError, TypeError, ValueError):
        raise CheckpointError("invalid_snapshot_manifest") from None


def restore_object_snapshot(receipt: dict, object_root: Path, target_dir: Path, *, identity_fd: int) -> dict:
    """Authenticate all objects into a new target; leave native recovery blocked."""
    if receipt.get("schema") != SCHEMA or receipt.get("format") != "olympus-age-objects":
        raise CheckpointError("unsupported_checkpoint_schema")
    root, target = Path(object_root), Path(target_dir).expanduser()
    if target.is_symlink() or target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise CheckpointError("restore_target_not_empty")
    if not target.is_absolute() or not target.parent.is_dir():
        raise CheckpointError("restore_parent_missing")
    identity = _identity_bytes(identity_fd)
    raw = _decrypt_object(root, receipt["manifest_object"], identity, MAX_MANIFEST_BYTES)
    if digest(raw) != receipt["manifest_sha256"]:
        raise CheckpointError("snapshot_manifest_hash_mismatch")
    try:
        manifest = json.loads(raw)
    except ValueError:
        raise CheckpointError("invalid_snapshot_manifest") from None
    files = _validate_manifest(receipt, manifest)
    total = sum(e["size"] for e in files.values())
    if shutil.disk_usage(target.parent).free < total + RESERVE_BYTES:
        raise CheckpointError("restore_disk_space_low")
    if shutil.disk_usage(tempfile.gettempdir()).free < MAX_OBJECT_BYTES + RESERVE_BYTES:
        raise CheckpointError("restore_temporary_space_low")
    with tempfile.TemporaryDirectory(prefix=".object-restore-", dir=target.parent) as temporary:
        staging = Path(temporary)
        used = {receipt["manifest_object"]}
        for name, entry in files.items():
            destination = staging / name
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            count, hasher = 0, hashlib.sha256()
            with os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
                for part in entry["parts"]:
                    if part["offset"] != count or not 0 < part["size"] <= manifest["chunk_bytes"]:
                        raise CheckpointError("snapshot_part_coverage_mismatch")
                    data = _decrypt_object(root, part["object_id"], identity, part["size"])
                    if len(data) != part["size"] or digest(data) != part["sha256"]:
                        raise CheckpointError("snapshot_part_hash_mismatch")
                    stream.write(data); hasher.update(data); count += len(data); used.add(part["object_id"])
                stream.flush(); os.fsync(stream.fileno())
            if count != entry["size"] or hasher.hexdigest() != entry["sha256"]:
                raise CheckpointError("snapshot_member_hash_mismatch")
        if used != {obj["id"] for obj in receipt["objects"]}:
            raise CheckpointError("snapshot_object_inventory_mismatch")
        inventory = _local_inventory(staging / "local")
        if inventory != manifest["local"]:
            raise CheckpointError("local_inventory_mismatch")
        expected = {"local/registry.sqlite3", "native/hindsight.zip"}
        expected.update(f"local/versions/{vid}/{name}" for vid in inventory["version_ids"] for name in _version_files(inventory, vid))
        if set(files) != expected:
            raise CheckpointError("snapshot_inventory_mismatch")
        _validate_versions(staging / "local", inventory)
        if validate_native_backup(staging / "native/hindsight.zip") != manifest["native"]:
            raise CheckpointError("native_inventory_mismatch")
        db = sqlite3.connect(staging / "local/registry.sqlite3")
        try:
            for key, value in {"recovery_state": "blocked", "recovery_reason": "native_restore_and_latest_revocations_required",
                               "recovery_checkpoint": receipt["checkpoint_id"]}.items():
                db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            db.commit()
        finally:
            db.close()
        _atomic_json(staging / "checkpoint.json", receipt)
        if target.exists():
            target.rmdir()
        os.rename(staging, target)
        _sync(target.parent)
    return {"store_root": str(target / "local"), "native_backup": str(target / "native/hindsight.zip"),
            "checkpoint_id": receipt["checkpoint_id"], "recovery_state": "blocked", "native_restore_required": True,
            "latest_revocations_required": True, "remote_state": receipt.get("remote_state", "unconfirmed")}


def snapshot_rotation_plan(store, checkpoint_id: str, *, keep: list[str]) -> dict:
    """Return guarded eligibility only; never unlink a local or remote object.

    The external recovery workflow records snapshot_restore only after isolated
    native restore and current revocations. Historical remote proof alone cannot
    make a generation disposable. Actual retention policy remains owner-owned.
    """
    from .remote_readback import _snapshot_created
    candidate = _snapshot_created(store, checkpoint_id)["receipt"]
    result = {"checkpoint_id": checkpoint_id, "eligible": False, "unreferenced_objects": []}
    if not keep or checkpoint_id in keep:
        return {**result, "reason": "last_snapshot_must_be_preserved"}
    from .control_state import control_payload
    with store.connect() as db:
        current_control = digest(control_payload(db))
    def recovered(identifier):
        remote = json.loads(store.setting("snapshot_remote:" + identifier, "{}"))
        restore = json.loads(store.setting("snapshot_restore:" + identifier, "{}"))
        created = _snapshot_created(store, identifier)
        return (remote.get("remote_state") == "verified"
                and remote.get("creation_sha256") == created["receipt_sha256"]
                and restore.get("checkpoint_id") == identifier
                and restore.get("creation_sha256") == created["receipt_sha256"]
                and restore.get("local_restore_verified") is True
                and restore.get("native_restore_verified") is True
                and restore.get("current_revocations_verified") is True
                and restore.get("current_revocations_sha256") == current_control
                and store.setting("control_remote_sha") == current_control)
    if not recovered(checkpoint_id):
        return {**result, "reason": "candidate_recovery_unverified"}
    protected = [_snapshot_created(store, identifier)["receipt"] for identifier in keep]
    verified = [r for r in protected if recovered(r["checkpoint_id"])]
    if not any(set(candidate["version_ids"]) <= set(r["version_ids"])
               and r["changes_max_id"] >= candidate["changes_max_id"] for r in verified):
        return {**result, "reason": "replacement_recovery_unverified"}
    # Include every other registered generation, not just the requested keep set.
    with store.connect() as db:
        records = [json.loads(r[0])["receipt"] for r in db.execute("SELECT value FROM settings WHERE key LIKE 'snapshot_created:%'")]
    referenced = {obj["id"] for r in records if r["checkpoint_id"] != checkpoint_id for obj in r["objects"]}
    return {**result, "eligible": True, "reason": "recovery_and_references_verified",
            "unreferenced_objects": [obj["id"] for obj in candidate["objects"] if obj["id"] not in referenced]}
