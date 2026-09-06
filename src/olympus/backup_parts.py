"""Transport large encrypted checkpoints as fully verified immutable parts."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from .library import _root, _directory, _publish_files
from .preservation import Store, PreservationError, canonical, digest, timestamp
from .remote_readback import RemoteFile, _file_proof, _save


@dataclass(frozen=True)
class RemotePart:
    path: Path
    file_id: str
    parent_ids: tuple[str, ...]
    fetched_at: str


def _latest(store: Store) -> tuple[dict, Path]:
    receipt = json.loads(store.setting("backup_last_receipt", "{}"))
    path = Path(receipt.get("path", ""))
    if (not isinstance(receipt.get("checkpoint_id"), str)
            or path.name != "olympus-" + receipt["checkpoint_id"] + ".tar.age"
            or not re.fullmatch(r"olympus-[a-f0-9-]{36}\.tar\.age", path.name)
            or path.parent != store.root / "backups" or path.is_symlink() or not path.is_file()):
        raise PreservationError("backup_parts_receipt_required")
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    if path.stat().st_size != receipt.get("size") or checksum.hexdigest() != receipt.get("sha256"):
        raise PreservationError("local_backup_hash_mismatch")
    return receipt, path


def stage_backup_parts(store: Store, library_root: Path, *, part_bytes: int = 16 * 1024**2) -> dict:
    if type(part_bytes) is not int or not 1 <= part_bytes <= 32 * 1024**2:
        raise PreservationError("invalid_backup_part_size")
    with store.exclusive():
        receipt, path = _latest(store)
        root = _root(library_root, create=False)
        folder = _directory(_directory(_directory(root, "Backups"), "Parts"), receipt["checkpoint_id"])
        parts, offset = [], 0
        with path.open("rb") as stream:
            while block := stream.read(part_bytes):
                if len(parts) >= 10000:
                    raise PreservationError("backup_part_count_limit")
                name = f"part-{len(parts) + 1:05d}.bin"
                _publish_files(folder, {name: block}, strict=False)
                parts.append({"name": name, "index": len(parts), "offset": offset,
                              "bytes": len(block), "sha256": digest(block)})
                offset += len(block)
        manifest = {"schema": 1, "checkpoint_id": receipt["checkpoint_id"], "filename": path.name,
                    "bytes": receipt["size"], "sha256": receipt["sha256"], "part_bytes": part_bytes, "parts": parts}
        _publish_files(folder, {"manifest.json": canonical(manifest)}, strict=False)
        record = {"manifest": manifest, "manifest_sha256": digest(canonical(manifest)),
                  "path": str(folder), "status": "local_staged"}
        store.set_setting("backup_parts:" + path.name, json.dumps(record))
        return record


def verify_remote_backup_parts(store: Store, manifest: RemoteFile, parts: dict[str, RemotePart],
                               *, folder_id: str) -> dict:
    with store.exclusive():
        receipt, local = _latest(store)
        staged = json.loads(store.setting("backup_parts:" + local.name, "{}"))
        spec = staged.get("manifest")
        if not spec or digest(canonical(spec)) != staged.get("manifest_sha256"):
            raise PreservationError("backup_parts_not_staged")
        manifest_proof = _file_proof(manifest, canonical(spec), folder_id)
        if set(parts) != {part["name"] for part in spec["parts"]}:
            raise PreservationError("backup_parts_inventory_mismatch")
        if len({manifest.file_id, *(part.file_id for part in parts.values())}) != len(parts) + 1:
            raise PreservationError("remote_duplicate_file_id")
        checksum, count, proofs = hashlib.sha256(), 0, []
        with local.open("rb") as original:
            for part in spec["parts"]:
                source = parts[part["name"]]
                if source.path.is_symlink() or source.path.stat().st_size != part["bytes"]:
                    raise PreservationError("backup_part_size_mismatch")
                data = source.path.read_bytes()
                expected = original.read(part["bytes"])
                proof = _file_proof(RemoteFile(data, source.file_id, source.parent_ids, source.fetched_at), expected, folder_id)
                if proof["sha256"] != part["sha256"] or part["offset"] != count:
                    raise PreservationError("backup_part_hash_mismatch")
                checksum.update(data)
                count += len(data)
                proofs.append({"name": part["name"], **proof})
            if original.read(1):
                raise PreservationError("backup_parts_incomplete")
        if count != receipt["size"] or checksum.hexdigest() != receipt["sha256"]:
            raise PreservationError("backup_parts_full_hash_mismatch")
        result = {"schema": 1, "checked_at": timestamp(), "checkpoint_id": receipt["checkpoint_id"],
                  "filename": local.name, "remote_state": "verified", "restore_verified": False,
                  "transport": "verified_parts", "manifest": manifest_proof, "parts": proofs,
                  "file": {"bytes": count, "sha256": checksum.hexdigest()}}
        with store.connect(write=True) as db:
            _save(db, "backup_remote:" + local.name, result)
        return result
