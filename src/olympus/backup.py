"""Encrypted, verifiable checkpoints; native database restore remains external."""

from __future__ import annotations

from contextlib import contextmanager, closing
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import select
import time
import sqlite3
import stat
import struct
import subprocess
import tarfile
import tempfile
from typing import BinaryIO
import uuid
import zipfile

from .preservation import canonical, guard_no_secrets


SCHEMA_VERSION = 1
AGE_VERSION = "v1.3.1"
MAX_FILE_BYTES = 8 * 1024**3
MAX_TOTAL_BYTES = 32 * 1024**3
MAX_MANIFEST_BYTES = 8 * 1024**2
MAX_MEMBERS = 100_000
COPY_SIGNATURE = b"PGCOPY\n\xff\r\n\x00"
NATIVE_TABLES = (
    "banks", "documents", "entities", "chunks", "memory_units", "invalidated_memory_units",
    "unit_entities", "entity_cooccurrences", "memory_links", "observation_history",
    "mental_models", "mental_model_history", "knowledge_pages", "directives", "async_operations",
    "webhooks", "file_storage", "audit_log", "llm_requests", "graph_maintenance_queue",
    "entity_maintenance_queue",
)
LOCAL_TABLES = {"sources", "versions", "locations", "delivery", "changes", "settings",
                "registrations", "captured_events", "manifest_integrity", "superseded_content"}
VERSION_FILES = ("original", "text.txt", "manifest.json", "manifest.sha256")
VERSION_ID = re.compile(r"olv-[a-f0-9]{64}")
HASH = re.compile(r"[a-f0-9]{64}")


class CheckpointError(Exception):
    """Stable diagnostic code, without private content or subprocess stderr."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _sha(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _json(data: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    try:
        result = json.loads(data, object_pairs_hook=pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise CheckpointError("invalid_manifest_json") from None
    if not isinstance(result, dict):
        raise CheckpointError("invalid_manifest_json")
    return result


def _count(value) -> bool:
    return type(value) is int and value >= 0


def _age() -> str:
    binary = shutil.which("age")
    if not binary:
        raise CheckpointError("age_unavailable")
    try:
        result = subprocess.run([binary, "--version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise CheckpointError("age_unavailable") from None
    if result.returncode or result.stdout.strip() != AGE_VERSION.encode():
        raise CheckpointError("age_version_mismatch")
    return binary


def _recipient(binary: str, value: str) -> None:
    # Permit only native X25519 recipients, never plugin/SSH/passphrase identities.
    if not isinstance(value, str) or not re.fullmatch(r"age1[023456789acdefghjklmnpqrstuvwxyz]{58}", value):
        raise CheckpointError("invalid_age_recipient")
    try:
        checked = subprocess.run([binary, "--encrypt", "--recipient", value], input=b"",
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        raise CheckpointError("age_recipient_validation_failed") from None
    if checked.returncode:
        raise CheckpointError("invalid_age_recipient")


def _safe_receipt(value: dict, *, require_frozen: bool) -> dict:
    if not isinstance(value, dict):
        raise CheckpointError("invalid_runtime_receipt")
    def walk(item, depth=0):
        if depth > 12:
            raise CheckpointError("invalid_runtime_receipt")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise CheckpointError("invalid_runtime_receipt")
                if re.search(r"secret|password|credential|api.?key|access.?token|refresh.?token|private.?key|^env$", key, re.I):
                    if child is not None and type(child) is not bool:
                        raise CheckpointError("runtime_receipt_contains_credentials")
                walk(child, depth + 1)
        elif isinstance(item, list):
            if len(item) > 1000:
                raise CheckpointError("invalid_runtime_receipt")
            for child in item:
                walk(child, depth + 1)
        elif not isinstance(item, (str, int, float, bool, type(None))):
            raise CheckpointError("invalid_runtime_receipt")
    walk(value)
    try:
        data = json.dumps(value, allow_nan=False).encode()
        guard_no_secrets(data)
    except Exception:
        raise CheckpointError("unsafe_runtime_receipt") from None
    if len(data) > MAX_MANIFEST_BYTES or b"AGE-SECRET-KEY-" in data:
        raise CheckpointError("unsafe_runtime_receipt")
    if require_frozen and (value.get("writers_stopped") is not True or value.get("workers_stopped") is not True):
        raise CheckpointError("runtime_freeze_receipt_required")
    return _json(data)


def _copy_regular(source: Path, target: Path) -> dict:
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise CheckpointError("source_not_regular") from None
    with os.fdopen(fd, "rb") as incoming:
        before = os.fstat(incoming.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            raise CheckpointError("source_not_regular")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        outgoing_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        hasher = hashlib.sha256()
        size = 0
        with os.fdopen(outgoing_fd, "wb") as outgoing:
            for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise CheckpointError("source_too_large")
                hasher.update(chunk)
                outgoing.write(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        after = os.fstat(incoming.fileno())
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise CheckpointError("source_changed_during_checkpoint")
    return {"size": size, "sha256": hasher.hexdigest()}


def _exact(stream: BinaryIO, count: int) -> bytes:
    data = stream.read(count)
    if len(data) != count:
        raise CheckpointError("invalid_native_copy_stream")
    return data


def _validate_copy(stream: BinaryIO, columns: int, expected_rows: int) -> None:
    if _exact(stream, 11) != COPY_SIGNATURE:
        raise CheckpointError("invalid_native_copy_stream")
    flags, extension = struct.unpack("!II", _exact(stream, 8))
    if flags != 0 or extension != 0:
        raise CheckpointError("unsupported_native_copy_header")
    rows = 0
    while True:
        fields = struct.unpack("!h", _exact(stream, 2))[0]
        if fields == -1:
            break
        if fields != columns or rows >= expected_rows:
            raise CheckpointError("native_copy_shape_mismatch")
        for _ in range(fields):
            length = struct.unpack("!i", _exact(stream, 4))[0]
            if length < -1 or length > MAX_FILE_BYTES:
                raise CheckpointError("invalid_native_copy_field")
            remaining = max(0, length)
            while remaining:
                chunk = _exact(stream, min(1024 * 1024, remaining))
                remaining -= len(chunk)
        rows += 1
    if rows != expected_rows or stream.read(1):
        raise CheckpointError("native_copy_shape_mismatch")


def validate_native_backup(path: Path) -> dict:
    """Validate v2 inventory/COPY framing. This does not execute a PostgreSQL restore."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise CheckpointError("native_backup_not_regular")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            expected = {"manifest.json", *(table + ".bin" for table in NATIVE_TABLES)}
            if len(names) != len(set(names)) or set(names) != expected:
                raise CheckpointError("native_backup_inventory_mismatch")
            if sum(item.file_size for item in infos) > MAX_TOTAL_BYTES:
                raise CheckpointError("native_backup_too_large")
            for item in infos:
                mode = item.external_attr >> 16
                if item.is_dir() or stat.S_ISLNK(mode) or item.flag_bits & 1 or item.file_size > MAX_FILE_BYTES:
                    raise CheckpointError("invalid_native_backup_member")
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    raise CheckpointError("invalid_native_backup_member")
            if archive.getinfo("manifest.json").file_size > MAX_MANIFEST_BYTES:
                raise CheckpointError("native_manifest_too_large")
            manifest = _json(archive.read("manifest.json"))
            if manifest.get("version") != "2":
                raise CheckpointError("unsupported_native_backup_schema")
            if not isinstance(manifest.get("schema"), str) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", manifest["schema"]):
                raise CheckpointError("invalid_native_manifest")
            try:
                created = datetime.fromisoformat(manifest["created_at"])
                if created.tzinfo is None:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise CheckpointError("invalid_native_manifest") from None
            tables = manifest.get("tables")
            if not isinstance(tables, dict) or set(tables) != set(NATIVE_TABLES):
                raise CheckpointError("native_backup_inventory_mismatch")
            total_rows = 0
            for table in NATIVE_TABLES:
                entry = tables[table]
                if (not isinstance(entry, dict) or not _count(entry.get("rows"))
                        or not _count(entry.get("size_bytes")) or not isinstance(entry.get("columns"), list)
                        or not 1 <= len(entry["columns"]) <= 1600):
                    raise CheckpointError("invalid_native_manifest")
                columns = entry["columns"]
                if any(not isinstance(c, dict) or not isinstance(c.get("name"), str)
                       or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", c["name"])
                       or not isinstance(c.get("type_name"), str) or not c["type_name"] for c in columns):
                    raise CheckpointError("invalid_native_manifest")
                if len({c["name"] for c in columns}) != len(columns):
                    raise CheckpointError("invalid_native_manifest")
                name = table + ".bin"
                if entry["size_bytes"] != archive.getinfo(name).file_size:
                    raise CheckpointError("native_backup_size_mismatch")
                with archive.open(name) as stream:
                    _validate_copy(stream, len(columns), entry["rows"])
                total_rows += entry["rows"]
            return {"format_version": "2", "schema": manifest["schema"], "created_at": manifest["created_at"],
                    "table_count": len(NATIVE_TABLES), "row_count": total_rows,
                    "structure_verified": True, "restore_verified": False}
    except CheckpointError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, struct.error, EOFError):
        raise CheckpointError("invalid_native_backup") from None


@contextmanager
def _database(path: Path):
    try:
        db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA trusted_schema=OFF")
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise CheckpointError("unsupported_local_schema")
        checks = db.execute("PRAGMA integrity_check").fetchall()
        if len(checks) != 1 or checks[0][0] != "ok":
            raise CheckpointError("local_database_integrity_failed")
        schema = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        if {row["name"] for row in schema if row["type"] == "table"} != LOCAL_TABLES:
            raise CheckpointError("unsupported_local_schema")
        if any(row["type"] in ("view", "trigger") for row in schema) or db.execute("PRAGMA foreign_key_check").fetchone():
            raise CheckpointError("local_database_integrity_failed")
        yield db
    finally:
        if "db" in locals():
            db.close()


def _local_inventory(local: Path) -> dict:
    try:
        with _database(local / "registry.sqlite3") as db:
            versions = [dict(row) for row in db.execute("SELECT id,source_id FROM versions ORDER BY id")]
            tables = {name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in sorted(LOCAL_TABLES)}
            changes_max_id = db.execute("SELECT coalesce(max(id),0) FROM changes").fetchone()[0]
            for row in versions:
                if not isinstance(row["id"], str) or not VERSION_ID.fullmatch(row["id"]):
                    raise CheckpointError("invalid_source_version")
            return {"version_ids": [row["id"] for row in versions], "tables": tables,
                    "changes_max_id": changes_max_id, "schema": 1}
    except CheckpointError:
        raise
    except (sqlite3.Error, OSError, ValueError):
        raise CheckpointError("local_database_integrity_failed") from None


def _validate_versions(local: Path, inventory: dict) -> None:
    try:
        with _database(local / "registry.sqlite3") as db:
            for version_id in inventory["version_ids"]:
                folder = local / "versions" / version_id
                if (folder / "manifest.json").stat().st_size > MAX_MANIFEST_BYTES:
                    raise CheckpointError("source_manifest_too_large")
                if (folder / "manifest.sha256").stat().st_size != 64:
                    raise CheckpointError("source_manifest_anchor_mismatch")
                manifest = _json((folder / "manifest.json").read_bytes())
                manifest_hash = _sha(folder / "manifest.json")
                anchor = (folder / "manifest.sha256").read_text()
                recorded = db.execute("SELECT sha256 FROM manifest_integrity WHERE version_id=?", (version_id,)).fetchone()
                if anchor != manifest_hash or recorded is None or recorded[0] != manifest_hash:
                    raise CheckpointError("source_manifest_anchor_mismatch")
                semantic = {k: manifest[k] for k in ("source_key", "scope", "kind", "title", "metadata")}
                source_id = "ols-" + hashlib.sha256(canonical([manifest["scope"], manifest["source_key"]])).hexdigest()
                original_hash = _sha(folder / "original")
                text_hash = _sha(folder / "text.txt")
                expected = "olv-" + hashlib.sha256(canonical([source_id, original_hash, text_hash, semantic])).hexdigest()
                row = db.execute("SELECT source_id FROM versions WHERE id=?", (version_id,)).fetchone()
                if (manifest.get("schema") != 1 or manifest.get("version_id") != version_id
                        or manifest.get("source_id") != source_id or expected != version_id
                        or manifest.get("original_sha256") != original_hash or manifest.get("text_sha256") != text_hash
                        or row is None or row[0] != source_id):
                    raise CheckpointError("source_version_hash_mismatch")
                source = db.execute("SELECT source_key,scope,kind FROM sources WHERE id=?", (source_id,)).fetchone()
                if source is None or any(source[k] != manifest[k] for k in ("source_key", "scope", "kind")):
                    raise CheckpointError("source_registry_mismatch")
    except CheckpointError:
        raise
    except (OSError, KeyError, TypeError, ValueError, sqlite3.Error):
        raise CheckpointError("invalid_source_version") from None


def _member_name(name: str) -> bool:
    if name in {"checkpoint.json", "local/registry.sqlite3", "native/hindsight.zip"}:
        return True
    parts = PurePosixPath(name).parts
    return (len(parts) == 4 and parts[:2] == ("local", "versions") and bool(VERSION_ID.fullmatch(parts[2]))
            and parts[3] in VERSION_FILES and str(PurePosixPath(name)) == name)


def create_checkpoint(store, native_backup: Path, recipient: str, output_dir: Path, *, runtime_receipt: dict) -> dict:
    """Capture consistent local SQLite state and encrypt an explicitly frozen native ZIP."""
    binary = _age()
    _recipient(binary, recipient)
    runtime_receipt = _safe_receipt(runtime_receipt, require_frozen=True)
    output = Path(output_dir).expanduser()
    if output.is_symlink():
        raise CheckpointError("output_directory_is_symlink")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = output.resolve()
    checkpoint_id = str(uuid.uuid4())
    final = output / ("olympus-" + checkpoint_id + ".tar.age")
    temporary_output = output / ("." + checkpoint_id + ".partial")
    try:
        with tempfile.TemporaryDirectory(prefix="olympus-checkpoint-") as temp:
            staging = Path(temp)
            (staging / "local").mkdir(mode=0o700)
            with store.exclusive():
                db_path = staging / "local/registry.sqlite3"
                target_db = sqlite3.connect(db_path)
                try:
                    with store.connect() as source_db:
                        source_db.backup(target_db)
                    target_db.execute("PRAGMA journal_mode=DELETE")
                finally:
                    target_db.close()
                db_path.chmod(0o600)
                inventory = _local_inventory(staging / "local")
                files = {"local/registry.sqlite3": {"size": db_path.stat().st_size, "sha256": _sha(db_path)}}
                for version in inventory["version_ids"]:
                    source_folder = store.versions / version
                    if source_folder.is_symlink() or not source_folder.is_dir():
                        raise CheckpointError("source_version_is_symlink")
                    for name in VERSION_FILES:
                        member = f"local/versions/{version}/{name}"
                        files[member] = _copy_regular(source_folder / name, staging / member)
                _validate_versions(staging / "local", inventory)
                files["native/hindsight.zip"] = _copy_regular(Path(native_backup), staging / "native/hindsight.zip")
            native = validate_native_backup(staging / "native/hindsight.zip")
            native_hash = files["native/hindsight.zip"]["sha256"]
            if runtime_receipt.get("native_backup_sha256", native_hash) != native_hash:
                raise CheckpointError("native_backup_hash_mismatch")
            if len(files) + 1 > MAX_MEMBERS or sum(item["size"] for item in files.values()) > MAX_TOTAL_BYTES:
                raise CheckpointError("checkpoint_too_large")
            manifest = {"schema": SCHEMA_VERSION, "checkpoint_id": checkpoint_id,
                        "created_at": datetime.now(timezone.utc).isoformat(), "age_version": AGE_VERSION,
                        "scope": "captured_versions_local_registry_and_native_backup", "remote_state": "unconfirmed",
                        "runtime_receipt": runtime_receipt, "local": inventory, "native": native, "files": files}
            manifest_bytes = canonical(manifest)
            if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                raise CheckpointError("checkpoint_manifest_too_large")
            with os.fdopen(os.open(temporary_output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as encrypted:
                process = subprocess.Popen([binary, "--encrypt", "--recipient", recipient], stdin=subprocess.PIPE,
                                           stdout=encrypted, stderr=subprocess.DEVNULL)
                try:
                    with tarfile.open(fileobj=process.stdin, mode="w|", format=tarfile.USTAR_FORMAT) as archive:
                        header = tarfile.TarInfo("checkpoint.json")
                        header.size = len(manifest_bytes)
                        header.mode = 0o600
                        archive.addfile(header, io.BytesIO(manifest_bytes))
                        for name in sorted(files):
                            header = tarfile.TarInfo(name)
                            header.size = files[name]["size"]
                            header.mode = 0o600
                            with (staging / name).open("rb") as contents:
                                archive.addfile(header, contents)
                    process.stdin.close()
                    if process.wait(timeout=60):
                        raise CheckpointError("age_encryption_failed")
                except BaseException:
                    process.kill()
                    process.wait()
                    raise
                encrypted.flush()
                os.fsync(encrypted.fileno())
            # Link is an atomic no-clobber publication; no output overwrite is permitted.
            os.link(temporary_output, final)
            temporary_output.unlink()
            fd = os.open(output, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            return {"checkpoint_id": checkpoint_id, "path": str(final), "sha256": _sha(final),
                    "size": final.stat().st_size, "encrypted": True, "remote_state": "unconfirmed",
                    "restore_verified": False, "manifest": manifest}
    except CheckpointError:
        raise
    except (OSError, ValueError, sqlite3.Error, tarfile.TarError, subprocess.SubprocessError):
        raise CheckpointError("checkpoint_creation_failed") from None
    finally:
        temporary_output.unlink(missing_ok=True)


@contextmanager
def _decrypted(checkpoint: Path, identity_fd: int):
    binary = _age()
    path = Path(checkpoint)
    if type(identity_fd) is not int or identity_fd < 0:
        raise CheckpointError("invalid_identity_descriptor")
    try:
        os.fstat(identity_fd)
    except OSError:
        raise CheckpointError("invalid_identity_descriptor") from None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_TOTAL_BYTES + MAX_MANIFEST_BYTES:
        raise CheckpointError("invalid_checkpoint_file")
    # Accept an age identity whose comment header was flattened by a secret form.
    # Bound both input size and waiting; never put the private key in argv or a file.
    identity = bytearray()
    deadline = time.monotonic() + 5
    try:
        while len(identity) <= 8192:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([identity_fd], [], [], remaining)[0]:
                raise CheckpointError("invalid_identity_format")
            chunk = os.read(identity_fd, 8193 - len(identity))
            if not chunk:
                break
            identity.extend(chunk)
    except OSError:
        raise CheckpointError("invalid_identity_descriptor") from None
    keys = re.findall(rb"(?<![A-Z0-9-])AGE-SECRET-KEY-1[A-Z0-9]+(?![A-Z0-9-])", identity)
    if len(identity) > 8192 or len(keys) != 1:
        raise CheckpointError("invalid_identity_format")
    with tempfile.TemporaryFile(mode="w+b") as plaintext:
        copied_fd, writer_fd = os.pipe()
        try:
            os.write(writer_fd, keys[0] + b"\n")
        finally:
            os.close(writer_fd)
        try:
            result = subprocess.run([binary, "--decrypt", "--identity", f"/dev/fd/{copied_fd}", str(path)],
                                    pass_fds=(copied_fd,), stdin=subprocess.DEVNULL, stdout=plaintext,
                                    stderr=subprocess.DEVNULL, timeout=300, start_new_session=True)
        except (OSError, subprocess.TimeoutExpired):
            raise CheckpointError("checkpoint_decryption_failed") from None
        finally:
            os.close(copied_fd)
        if result.returncode:
            raise CheckpointError("checkpoint_decryption_failed")
        if plaintext.tell() > MAX_TOTAL_BYTES + MAX_MANIFEST_BYTES + MAX_MEMBERS * 1024:
            raise CheckpointError("checkpoint_too_large")
        plaintext.seek(0)
        yield plaintext


def _unpack_verified(plaintext: BinaryIO, staging: Path) -> dict:
    try:
        with tarfile.open(fileobj=plaintext, mode="r:") as archive:
            seen = set()
            members = []
            total = 0
            manifest = None
            for item in archive:
                if (not item.isreg() or item.issparse() or item.pax_headers or not _member_name(item.name)
                        or item.name in seen or item.size < 0 or item.size > MAX_FILE_BYTES):
                    raise CheckpointError("unsafe_checkpoint_member")
                total += item.size
                if total > MAX_TOTAL_BYTES or len(seen) >= MAX_MEMBERS:
                    raise CheckpointError("checkpoint_too_large")
                seen.add(item.name)
                if item.name == "checkpoint.json":
                    if item.size > MAX_MANIFEST_BYTES:
                        raise CheckpointError("checkpoint_manifest_too_large")
                    manifest = _json(archive.extractfile(item).read())
                else:
                    members.append(item)
            if not manifest or type(manifest.get("schema")) is not int or manifest["schema"] != SCHEMA_VERSION:
                raise CheckpointError("unsupported_checkpoint_schema")
            if set(manifest) != {"schema", "checkpoint_id", "created_at", "age_version", "scope", "remote_state",
                                 "runtime_receipt", "local", "native", "files"}:
                raise CheckpointError("invalid_checkpoint_manifest")
            try:
                if str(uuid.UUID(manifest["checkpoint_id"])) != manifest["checkpoint_id"]:
                    raise ValueError
                if datetime.fromisoformat(manifest["created_at"]).tzinfo is None:
                    raise ValueError
            except (KeyError, TypeError, ValueError, AttributeError):
                raise CheckpointError("invalid_checkpoint_manifest") from None
            if (manifest.get("scope") != "captured_versions_local_registry_and_native_backup"
                    or manifest.get("remote_state") != "unconfirmed" or manifest.get("age_version") != AGE_VERSION):
                raise CheckpointError("invalid_checkpoint_manifest")
            _safe_receipt(manifest.get("runtime_receipt"), require_frozen=True)
            files = manifest.get("files")
            if not isinstance(files, dict) or set(files) != seen - {"checkpoint.json"}:
                raise CheckpointError("checkpoint_inventory_mismatch")
            if not {"local/registry.sqlite3", "native/hindsight.zip"} <= set(files):
                raise CheckpointError("checkpoint_inventory_mismatch")
            # Ignore no trailing payload, including data hidden after tar's end marker.
            plaintext.seek(archive.offset)
            for chunk in iter(lambda: plaintext.read(1024 * 1024), b""):
                if any(chunk):
                    raise CheckpointError("invalid_tar_trailer")
            for item in members:
                expected = files[item.name]
                if (not isinstance(expected, dict) or not _count(expected.get("size"))
                        or set(expected) != {"size", "sha256"}
                        or expected["size"] != item.size or not isinstance(expected.get("sha256"), str)
                        or not HASH.fullmatch(expected["sha256"])):
                    raise CheckpointError("invalid_checkpoint_manifest")
                destination = staging / item.name
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
                    source = archive.extractfile(item)
                    hasher = hashlib.sha256()
                    size = 0
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        hasher.update(chunk)
                        size += len(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if size != item.size or hasher.hexdigest() != expected["sha256"]:
                    raise CheckpointError("checkpoint_member_hash_mismatch")
        inventory = _local_inventory(staging / "local")
        if inventory != manifest.get("local"):
            raise CheckpointError("local_inventory_mismatch")
        expected_names = {"local/registry.sqlite3", "native/hindsight.zip"}
        expected_names.update(f"local/versions/{version}/{name}" for version in inventory["version_ids"]
                              for name in VERSION_FILES)
        if set(files) != expected_names:
            raise CheckpointError("checkpoint_inventory_mismatch")
        _validate_versions(staging / "local", inventory)
        if validate_native_backup(staging / "native/hindsight.zip") != manifest.get("native"):
            raise CheckpointError("native_inventory_mismatch")
        return manifest
    except CheckpointError:
        raise
    except (OSError, ValueError, TypeError, KeyError, tarfile.TarError, sqlite3.Error):
        raise CheckpointError("invalid_checkpoint_archive") from None


def inspect_checkpoint(checkpoint: Path, *, identity_fd: int) -> dict:
    """Authenticate/decrypt and verify every member; all extracted temporary data is removed."""
    with _decrypted(checkpoint, identity_fd) as plaintext:
        with tempfile.TemporaryDirectory(prefix="olympus-inspect-") as temp:
            manifest = _unpack_verified(plaintext, Path(temp))
    return {"checkpoint_id": manifest["checkpoint_id"], "authenticated": True, "hashes_verified": True,
            "remote_state": "unconfirmed", "native_restore_verified": False, "manifest": manifest}


def restore_local_checkpoint(checkpoint: Path, target_dir: Path, *, identity_fd: int) -> dict:
    """Restore into a new empty directory; keep native restore and current reads blocked."""
    target = Path(target_dir).expanduser()
    if target.is_symlink() or target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise CheckpointError("restore_target_not_empty")
    if not target.parent.is_dir():
        raise CheckpointError("restore_parent_missing")
    target = target.parent.resolve() / target.name
    with tempfile.TemporaryDirectory(prefix=".olympus-restore-", dir=target.parent) as temp:
        staging = Path(temp)
        with _decrypted(checkpoint, identity_fd) as plaintext:
            manifest = _unpack_verified(plaintext, staging)
        # All original queue states, captured events and revocations stay intact.
        # Store/readers must honour this explicit recovery contract before any use.
        db_path = staging / "local/registry.sqlite3"
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute("PRAGMA trusted_schema=OFF")
            for key, value in {
                "recovery_state": "blocked",
                "recovery_reason": "native_restore_and_latest_revocations_required",
                "recovery_checkpoint_id": manifest["checkpoint_id"],
            }.items():
                db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (key, value))
            db.commit()
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise CheckpointError("local_database_integrity_failed")
        with os.fdopen(os.open(staging / "checkpoint.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as saved:
            saved.write(canonical(manifest))
            saved.flush()
            os.fsync(saved.fileno())
        for directory in sorted((p for p in staging.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fd = os.open(staging, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        # Store root is target/local. The preserved native archive is never replayed here.
        try:
            if target.exists() and any(target.iterdir()):
                raise CheckpointError("restore_target_not_empty")
            os.rename(staging, target)
        except OSError:
            raise CheckpointError("restore_target_publication_failed") from None
        fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return {"checkpoint_id": manifest["checkpoint_id"], "target_dir": str(target),
            "store_root": str(target / "local"), "native_backup": str(target / "native/hindsight.zip"),
            "recovery_state": "blocked", "native_restore_required": True,
            "latest_revocations_required": True, "remote_state": "unconfirmed"}
