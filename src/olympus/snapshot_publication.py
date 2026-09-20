"""Bounded ciphertext-only publication to a Drive Desktop sync directory.

Local staging receipts are historical byte proofs, never remote receipts. A
generation's discovery envelope follows all of its immutable encrypted objects.
Only clonefile(2) success establishes copy-on-write; ordinary copies need space.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from .bounded_io import FileUnavailable, fingerprint, hash_file, publish_file
from .library import _directory, _root
from .preservation import PreservationError, canonical, digest, timestamp
from .snapshot_objects import MAX_OBJECT_BYTES, RESERVE_BYTES, _object_path


def _clone_file(source: Path, target: Path) -> bool:
    if sys.platform != "darwin":
        return False
    library = ctypes.CDLL(None, use_errno=True)
    clone = library.clonefile
    clone.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int)
    clone.restype = ctypes.c_int
    if clone(os.fsencode(source), os.fsencode(target), 0) == 0:
        return True
    code = ctypes.get_errno()
    if code in (errno.ENOTSUP, errno.EXDEV, errno.ENOSYS):
        return False
    raise OSError(code, "snapshot_clone_failed")


def _copy_candidate(request: dict) -> dict:
    """Child-only bounded operation; the caller owns and removes the candidate."""
    source, target = Path(request["source"]), Path(request["target"])
    observed = fingerprint(source)
    if observed != request["source_fingerprint"] or not 0 < observed["bytes"] <= MAX_OBJECT_BYTES:
        raise PreservationError("snapshot_publication_source_changed")
    method = "clone"
    if not _clone_file(source, target):
        method = "copy"
        if shutil.disk_usage(target.parent).free < observed["bytes"] + RESERVE_BYTES:
            raise PreservationError("snapshot_publication_space_required")
        with source.open("rb") as incoming, target.open("xb") as outgoing:
            remaining = observed["bytes"]
            while remaining:
                block = incoming.read(min(1024**2, remaining))
                if not block:
                    raise PreservationError("snapshot_publication_source_changed")
                outgoing.write(block)
                remaining -= len(block)
            if incoming.read(1):
                raise PreservationError("snapshot_publication_source_changed")
            outgoing.flush()
            os.fsync(outgoing.fileno())
    if target.stat().st_size != observed["bytes"]:
        raise PreservationError("snapshot_publication_source_changed")
    checksum = hashlib.sha256()
    with target.open("rb") as incoming:
        for block in iter(lambda: incoming.read(1024**2), b""):
            checksum.update(block)
        os.fsync(incoming.fileno())
    if (fingerprint(source) != observed or target.stat().st_size != observed["bytes"]
            or checksum.hexdigest() != request["sha256"]):
        raise PreservationError("snapshot_publication_hash_mismatch")
    return {"method": method, "sha256": checksum.hexdigest()}


def _transfer(source: Path, target: Path, source_fp: dict, checksum: str, timeout: float) -> dict:
    request = {"source": str(source), "target": str(target), "source_fingerprint": source_fp, "sha256": checksum}
    try:
        result = subprocess.run([sys.executable, "-m", "olympus.snapshot_publication"],
            input=canonical(request), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise FileUnavailable("snapshot_publication_timeout") from None
    try:
        value = json.loads(result.stdout)
        if value.get("error"):
            raise FileUnavailable(value["error"], value.get("errno"))
        if result.returncode or value["sha256"] != checksum or value["method"] not in ("clone", "copy"):
            raise ValueError
        return value
    except (KeyError, ValueError, TypeError):
        raise PreservationError("snapshot_publication_worker_failed") from None


def _remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise PreservationError("snapshot_publication_budget_exhausted")
    return min(5, value)


def _observed(path: Path) -> dict | None:
    # lstat also rejects dangling symlinks; placeholders fail without hydration.
    try:
        return fingerprint(path)
    except FileNotFoundError:
        return None


def _matches(receipt: dict, source_fp: dict | None, destination_fp: dict | None, checksum: str) -> bool:
    return (source_fp is not None and destination_fp is not None
        and receipt.get("status") == "local_staged" and receipt.get("sha256") == checksum
        and receipt.get("source") == source_fp and receipt.get("destination") == destination_fp)


def _stage_file(source: Path, target: Path, checksum: str, size: int, prior: dict,
                *, deadline: float, audit: bool, fence) -> dict:
    fence()
    source_fp, target_fp = fingerprint(source), _observed(target)
    if source_fp["bytes"] != size or not 0 < size <= MAX_OBJECT_BYTES:
        raise PreservationError("snapshot_publication_size_mismatch")
    if not audit and _matches(prior, source_fp, target_fp, checksum):
        return {**prior, "cached": True}
    if target_fp is not None:
        if target_fp["bytes"] != size:
            raise PreservationError("snapshot_publication_destination_conflict")
        if hash_file(source, max_bytes=MAX_OBJECT_BYTES, timeout=_remaining(deadline)) != checksum:
            raise PreservationError("snapshot_publication_hash_mismatch")
        method = "existing"
    else:
        with tempfile.TemporaryDirectory(prefix=".object-stage-", dir=target.parent) as temporary:
            candidate = Path(temporary) / "payload"
            method = _transfer(source, candidate, source_fp, checksum, _remaining(deadline))["method"]
            fence()
            if fingerprint(source) != source_fp:
                raise PreservationError("snapshot_publication_source_changed")
            try:
                publish_file(candidate, target, checksum, timeout=_remaining(deadline))
            except FileUnavailable as exc:
                if str(exc) != "copy_target_exists":
                    raise
            candidate.unlink()
    # Link/unlink changes ctime. Bind the receipt to a fresh, bounded target
    # read after those changes, then compare that exact fingerprint at commit.
    fence()
    target_fp = fingerprint(target)
    if (target_fp["bytes"] != size
            or hash_file(target, max_bytes=MAX_OBJECT_BYTES, timeout=_remaining(deadline)) != checksum):
        raise PreservationError("snapshot_publication_destination_conflict")
    fence()
    if fingerprint(source) != source_fp:
        raise PreservationError("snapshot_publication_source_changed")
    if fingerprint(target) != target_fp:
        raise PreservationError("snapshot_publication_destination_changed")
    return {"schema": 2, "status": "local_staged", "source": source_fp, "destination": target_fp,
            "sha256": checksum, "bytes": size, "checked_at": timestamp(), "method": method,
            "remote_state": "unconfirmed"}


def _created(value: str, checkpoint_id: str) -> dict:
    try:
        record = json.loads(value)
        receipt = record["receipt"]
        allowed = {"schema", "format", "checkpoint_id", "created_at", "manifest_object", "manifest_sha256", "objects",
                   "files", "versions", "version_ids", "changes_max_id", "plain_bytes", "created_parts", "reused_parts",
                   "remote_state", "restore_verified"}
        if (set(receipt) != allowed or not re.fullmatch(r"[a-f0-9-]{36}", checkpoint_id)
                or receipt["checkpoint_id"] != checkpoint_id or receipt["schema"] != 2
                or receipt["format"] != "olympus-age-objects"
                or digest(canonical(receipt)) != record["receipt_sha256"]):
            raise ValueError
        root = Path(record["object_root"])
        if not root.is_absolute() or root.is_symlink():
            raise ValueError
        objects, seen = receipt["objects"], set()
        if not isinstance(objects, list) or not 0 < len(objects) <= 100000:
            raise ValueError
        for obj in objects:
            if (set(obj) != {"id", "size"} or not re.fullmatch(r"[a-f0-9]{64}", obj["id"])
                    or type(obj["size"]) is not int or not 0 < obj["size"] <= MAX_OBJECT_BYTES
                    or obj["id"] in seen):
                raise ValueError
            seen.add(obj["id"])
        if receipt["manifest_object"] not in seen or len(canonical(receipt)) > MAX_OBJECT_BYTES:
            raise ValueError
        return record
    except (KeyError, ValueError, TypeError):
        raise PreservationError("snapshot_publication_receipt_invalid") from None


def publish_object_snapshots(store, library_root: Path, *, limit: int = 32, time_budget: float = 10,
                             progress=None, audit: bool = False) -> dict:
    """Stage at most limit objects/envelopes, with durable rotation and byte proofs.

    The source inventory comes only from registered complete generations. Its
    private cache, temporary preparation inputs and unregistered objects stay out.
    """
    if type(limit) is not int or not 1 <= limit <= 10000 or not 0 < time_budget <= 300:
        raise PreservationError("snapshot_publication_budget_invalid")
    target = _directory(_directory(_root(library_root, create=False), "Backups"), "objects-v2")
    objects_root, envelopes = _directory(target, "objects"), _directory(target, "snapshots")
    prefix = "snapshot_staging:" + digest(str(target).encode()) + ":"
    cursor_key = prefix + "cursor"
    result = {"checked": 0, "staged": 0, "cached": 0, "deferred": 0, "snapshots_staged": 0,
              "bytes_staged": 0, "metadata_checked": 0, "cloned": 0, "copied": 0, "errors": [], "remote_verified": 0}
    deadline, plan, generations = time.monotonic() + time_budget, [], {}

    def fence():
        if progress and progress({k: v for k, v in result.items() if k != "errors"}) is False:
            raise PreservationError("stage_lease_lost")

    with store.connect() as db:
        created = list(db.execute("SELECT key,value FROM settings WHERE key GLOB 'snapshot_created:*' ORDER BY key"))
        records = {row[0]: json.loads(row[1]) for row in db.execute(
            "SELECT key,value FROM settings WHERE key GLOB ?", (prefix + "*",)) if row[0] != cursor_key}
    for key, value in created:
        identifier = key.removeprefix("snapshot_created:")
        try:
            record = _created(value, identifier)
            generations[identifier] = record
            for obj in record["receipt"]["objects"]:
                relative = "objects/" + obj["id"][:2] + "/" + obj["id"] + ".age"
                plan.append((identifier + ":" + relative, identifier, relative, obj["id"], obj["size"]))
            envelope = canonical(record["receipt"])
            plan.append((identifier + ":snapshots/" + identifier + ".json", identifier,
                         "snapshots/" + identifier + ".json", digest(envelope), len(envelope)))
        except PreservationError as exc:
            result["errors"].append({"checkpoint_id": identifier, "code": str(exc)})
            result["deferred"] += 1
    cursor = store.setting(cursor_key, "")
    tokens = [entry[0] for entry in plan]
    start = tokens.index(cursor) + 1 if cursor in tokens else 0
    plan = plan[start:] + plan[:start]
    for token, identifier, relative, checksum, size in plan:
        if result["checked"] >= limit or time.monotonic() >= deadline:
            break
        record = generations[identifier]
        source_root = Path(record["object_root"])
        receipt_key = prefix + relative
        is_envelope = relative.startswith("snapshots/")
        fence()
        try:
            if is_envelope:
                # Receipts are shared by immutable object ID across generations;
                # verify their current metadata before advertising this envelope.
                last_fence = time.monotonic()
                for obj in record["receipt"]["objects"]:
                    _remaining(deadline)
                    if time.monotonic() - last_fence >= 0.5:
                        fence()
                        last_fence = time.monotonic()
                    object_relative = "objects/" + obj["id"][:2] + "/" + obj["id"] + ".age"
                    if not _matches(records.get(prefix + object_relative, {}),
                            _observed(_object_path(source_root, obj["id"])),
                            _observed(_object_path(target, obj["id"])), obj["id"]):
                        raise PreservationError("snapshot_publication_objects_pending")
                    result["metadata_checked"] += 1
                source = source_root / relative
                if (source_root / "snapshots").is_symlink():
                    raise PreservationError("snapshot_publication_source_symlink")
                destination = envelopes / (identifier + ".json")
            else:
                source = _object_path(source_root, checksum)
                destination = _directory(objects_root, checksum[:2]) / (checksum + ".age")
            staged = _stage_file(source, destination, checksum, size, records.get(receipt_key, {}),
                                 deadline=deadline, audit=audit, fence=fence)
            fence()
            if staged.pop("cached", False):
                result["cached"] += 1
            else:
                store.set_setting(receipt_key, canonical(staged).decode())
                records[receipt_key] = staged
                result["staged"] += 1
                result["bytes_staged"] += size
                result["cloned"] += staged["method"] == "clone"
                result["copied"] += staged["method"] == "copy"
            if is_envelope:
                store.set_setting("snapshot_publication:" + identifier, canonical({"schema": 2,
                    "status": "local_staged", "creation_sha256": record["receipt_sha256"],
                    "path": str(destination), "object_root": str(target), "checked_at": timestamp(),
                    "objects": len(record["receipt"]["objects"]), "remote_state": "unconfirmed"}).decode())
                result["snapshots_staged"] += 1
        except (PreservationError, OSError) as exc:
            if isinstance(exc, PreservationError) and str(exc) == "stage_lease_lost":
                raise
            fence()
            code = str(exc) if isinstance(exc, PreservationError) else "snapshot_publication_io_error"
            error = {"checkpoint_id": identifier, "file": relative, "code": code, "errno": getattr(exc, "errno", None)}
            result["errors"].append(error)
            result["deferred"] += 1
            store.set_setting("snapshot_publication:" + identifier, canonical({"schema": 2, "status": "pending",
                "creation_sha256": record["receipt_sha256"], **error, "remote_state": "unconfirmed"}).decode())
        result["checked"] += 1
        fence()
        store.set_setting(cursor_key, token)
    return result


if __name__ == "__main__":
    try:
        print(json.dumps(_copy_candidate(json.loads(sys.stdin.buffer.read(16384)))))
    except (PreservationError, OSError) as exc:
        print(json.dumps({"error": str(exc) if isinstance(exc, PreservationError) else "snapshot_publication_io_error",
                          "errno": getattr(exc, "errno", None)}))
