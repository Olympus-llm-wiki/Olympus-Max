"""Verify complete connector-fetched bytes before recording remote receipts.

The caller supplies authenticated Drive download results, not sync-mount files.
This module verifies integrity and provenance metadata; it does not authenticate
the connector or claim continued remote availability after the observed read.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from .control_state import control_payload
from .preservation import Store, PreservationError, canonical, digest, timestamp


@dataclass(frozen=True)
class RemoteFile:
    content: bytes
    file_id: str
    parent_ids: tuple[str, ...]
    fetched_at: str


def _file_proof(remote: RemoteFile, expected: bytes, parent_id: str) -> dict:
    if not isinstance(remote, RemoteFile) or not isinstance(remote.content, bytes):
        raise PreservationError("remote_complete_bytes_required")
    if not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value)
               for value in (remote.file_id, parent_id)) or parent_id not in remote.parent_ids:
        raise PreservationError("remote_file_identity_invalid")
    try:
        observed = datetime.fromisoformat(remote.fetched_at.replace("Z", "+00:00"))
        if observed.tzinfo is None or (observed - datetime.now(timezone.utc)).total_seconds() > 60:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise PreservationError("remote_observed_time_invalid") from None
    if remote.content != expected:
        raise PreservationError("remote_bytes_mismatch")
    return {"drive_id": remote.file_id, "parent_id": parent_id, "fetched_at": remote.fetched_at,
            "bytes": len(remote.content), "sha256": digest(remote.content)}


def _save(db, key: str, value) -> None:
    db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, canonical(value).decode() if not isinstance(value, str) else value))


def verify_remote_version(store: Store, version_id: str, files: dict[str, RemoteFile], *, folder_id: str) -> dict:
    """Record one source version only after all three exported files match."""
    if set(files) != {"original", "text.txt", "manifest.json"}:
        raise PreservationError("remote_version_inventory_mismatch")
    if any(not isinstance(value, RemoteFile) for value in files.values()):
        raise PreservationError("remote_complete_bytes_required")
    if len({value.file_id for value in files.values()}) != 3:
        raise PreservationError("remote_duplicate_file_id")
    with store.exclusive():
        value = store.read_version(version_id)  # checks independent manifest anchor
        expected = {"original": value["original"], "text.txt": value["text"].encode(),
                    "manifest.json": canonical({k: v for k, v in value.items() if k not in {"original", "text"}})}
        proof = {"schema": 1, "checked_at": timestamp(), "version_id": version_id,
                 "source_id": value["source_id"], "folder_id": folder_id,
                 "remote_state": "verified", "files": {
                     name: _file_proof(files[name], data, folder_id) for name, data in expected.items()}}
        with store.connect(write=True) as db:
            row = db.execute("SELECT v.source_id,m.sha256 FROM versions v JOIN manifest_integrity m ON m.version_id=v.id WHERE v.id=?",
                             (version_id,)).fetchone()
            if row is None or row["source_id"] != value["source_id"] or row["sha256"] != digest(expected["manifest.json"]):
                raise PreservationError("remote_registered_version_mismatch")
            _save(db, "drive_remote:" + version_id, proof)
            db.execute("UPDATE delivery SET remote_state='verified',remote_id=?,updated_at=? WHERE version_id=?",
                       (folder_id, timestamp(), version_id))
        return proof


def verify_remote_control(store: Store, pointer: RemoteFile, state: RemoteFile, *,
                          control_folder_id: str, states_folder_id: str) -> dict:
    """Reject an old snapshot even when the latest local staging is also old."""
    if pointer.file_id == state.file_id:
        raise PreservationError("remote_duplicate_file_id")
    with store.exclusive(), store.connect(write=True) as db:
        payload = control_payload(db)
        checksum = digest(payload)
        changes = json.loads(payload)["changes"]
        expected_pointer = canonical({"schema": 1, "sha256": checksum,
            "changes_max_id": changes[-1]["id"] if changes else 0, "state_file": "states/" + checksum + ".json"})
        proof = {"schema": 1, "checked_at": timestamp(), "sha256": checksum,
                 "remote_state": "verified", "files": {
                     "latest.json": _file_proof(pointer, expected_pointer, control_folder_id),
                     "state.json": _file_proof(state, payload, states_folder_id)}}
        _save(db, "control_remote_receipt", proof)
        _save(db, "control_remote_sha", checksum)
        return proof


def verify_remote_backup(store: Store, filename: str, remote: RemoteFile, *, folder_id: str) -> dict:
    """Confirm the latest ciphertext, preserving its immutable creation receipt."""
    if not isinstance(filename, str) or not re.fullmatch(r"olympus-[a-f0-9-]{36}\.tar\.age", filename):
        raise PreservationError("remote_backup_name_invalid")
    with store.exclusive():
        saved = json.loads(store.setting("backup_last_receipt", "{}"))
        local = store.root / "backups" / filename
        if (local.is_symlink() or not local.is_file() or Path(saved.get("path", "")).name != filename
                or not saved.get("checkpoint_id")):
            raise PreservationError("remote_backup_receipt_required")
        content = local.read_bytes()
        if saved.get("sha256") != digest(content) or saved.get("size") != len(content):
            raise PreservationError("local_backup_hash_mismatch")
        proof = {"schema": 1, "checked_at": timestamp(), "checkpoint_id": saved["checkpoint_id"],
                 "filename": filename, "remote_state": "verified", "restore_verified": False,
                 "file": _file_proof(remote, content, folder_id)}
        with store.connect(write=True) as db:
            _save(db, "backup_remote:" + filename, proof)
        return proof


def _snapshot_created(store: Store, checkpoint_id: str) -> dict:
    if not re.fullmatch(r"[a-f0-9-]{36}", checkpoint_id):
        raise PreservationError("snapshot_id_invalid")
    value = json.loads(store.setting("snapshot_created:" + checkpoint_id, "{}"))
    if not value or digest(canonical(value.get("receipt"))) != value.get("receipt_sha256"):
        raise PreservationError("snapshot_creation_receipt_required")
    return value


def verify_remote_snapshot_envelope(store: Store, checkpoint_id: str, remote: RemoteFile, *, folder_id: str) -> dict:
    """Verify the discovery envelope as well as its encrypted manifest object."""
    created = _snapshot_created(store, checkpoint_id)
    proof = _file_proof(remote, canonical(created["receipt"]), folder_id)
    result = {"schema": 2, "checkpoint_id": checkpoint_id, "checked_at": timestamp(), "file": proof}
    with store.connect(write=True) as db:
        _save(db, "snapshot_envelope_remote:" + checkpoint_id, result)
    return result


def verify_remote_snapshot_object(store: Store, checkpoint_id: str, object_id: str,
                                  remote: RemoteFile, *, folder_id: str) -> dict:
    """Persist one complete authenticated object; callers may release downloads.

    Each object is bounded to 32 MiB. No global writer lock spans file reads; the
    immutable creation receipt binds the object ID, size and snapshot generation.
    """
    from .snapshot_objects import _object_path, MAX_OBJECT_BYTES
    created = _snapshot_created(store, checkpoint_id)
    expected = next((obj for obj in created["receipt"]["objects"] if obj["id"] == object_id), None)
    if not expected or expected["size"] > MAX_OBJECT_BYTES:
        raise PreservationError("snapshot_object_not_in_manifest")
    local = _object_path(Path(created["object_root"]), object_id)
    if local.stat().st_size != expected["size"]:
        raise PreservationError("snapshot_local_object_mismatch")
    content = local.read_bytes()
    if digest(content) != object_id:
        raise PreservationError("snapshot_local_object_mismatch")
    proof = _file_proof(remote, content, folder_id)
    result = {"schema": 2, "checkpoint_id": checkpoint_id, "object_id": object_id,
              "creation_sha256": created["receipt_sha256"], "checked_at": timestamp(), "file": proof}
    with store.connect(write=True) as db:
        _save(db, "snapshot_object_remote:" + checkpoint_id + ":" + object_id, result)
        _save(db, "snapshot_object_remote_global:" + object_id, result)
    return result


def finish_remote_snapshot(store: Store, checkpoint_id: str) -> dict:
    """Aggregate durable object proofs without retaining downloaded payloads."""
    created = _snapshot_created(store, checkpoint_id)
    with store.connect(write=True) as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", ("snapshot_envelope_remote:" + checkpoint_id,)).fetchone()
        if not row:
            raise PreservationError("snapshot_remote_envelope_missing")
        envelope = json.loads(row[0])
        if envelope["file"]["sha256"] != created["receipt_sha256"]:
            raise PreservationError("snapshot_remote_envelope_mismatch")
        files = [envelope["file"]]
        reused = 0
        for expected in created["receipt"]["objects"]:
            row = db.execute("SELECT value FROM settings WHERE key=?", ("snapshot_object_remote:" + checkpoint_id + ":" + expected["id"],)).fetchone()
            specific = row is not None
            if row is None:
                row = db.execute("SELECT value FROM settings WHERE key=?", ("snapshot_object_remote_global:" + expected["id"],)).fetchone()
                reused += row is not None
            if not row:
                raise PreservationError("snapshot_remote_objects_incomplete")
            proof = json.loads(row[0])
            if ((specific and proof.get("creation_sha256") != created["receipt_sha256"])
                    or proof.get("object_id") != expected["id"]
                    or proof["file"]["sha256"] != expected["id"] or proof["file"]["bytes"] != expected["size"]):
                raise PreservationError("snapshot_remote_object_mismatch")
            files.append(proof["file"])
        if len({f["drive_id"] for f in files}) != len(files):
            raise PreservationError("remote_duplicate_file_id")
        result = {"schema": 2, "checkpoint_id": checkpoint_id, "checked_at": timestamp(),
                  "creation_sha256": created["receipt_sha256"], "objects": len(files) - 1,
                  "remote_state": "verified", "restore_verified": False,
                  "transport": "verified_immutable_objects", "envelope": envelope["file"],
                  "reused_object_proofs": reused, "oldest_fetched_at": min(f["fetched_at"] for f in files)}
        _save(db, "snapshot_remote:" + checkpoint_id, result)
    return result
