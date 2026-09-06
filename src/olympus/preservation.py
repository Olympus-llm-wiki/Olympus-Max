"""Durable source versions and delivery leases; no semantic knowledge store."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from typing import Iterator
import uuid


class PreservationError(Exception):
    """A safe diagnostic code: never includes captured content."""


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z]+ )?PRIVATE KEY-----", re.S),
    re.compile(r"\b(?:sk-(?:proj-)?|gh[pousr]_|github_pat_)[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*"),
    re.compile(r'(?i)[\"\']?(?:access_token|refresh_token|id_token|client_secret|BWS_ACCESS_TOKEN|password)[\"\']?\s*[:=]\s*[\"\']?[^\s\"\',}]{12,}'),
    re.compile(r"(?i)\b(?:https?|postgres(?:ql)?|mysql|redis|amqp|mongodb(?:\+srv)?)://[^\s/@:]+:[^\s/@]+@"),
    re.compile(r"\bAGE-SECRET-KEY-[A-Z0-9]{30,}\b", re.I),
]


def redact_secrets(text: str) -> str:
    """Known credential patterns only. Not a guarantee for arbitrary private input."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED:credential]", text)
    return text


def guard_no_secrets(data: bytes) -> None:
    text = data.decode("utf-8", errors="ignore")
    if any(p.search(text) for p in _SECRET_PATTERNS):
        raise PreservationError("credential_pattern_detected")


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_file(path: Path, value: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


@dataclass(frozen=True)
class Receipt:
    source_id: str
    version_id: str
    document_id: str
    operation_id: str
    local_capture: str
    memory: str
    remote_copy: str
    correction: str


def serialized(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.exclusive():
            return method(self, *args, **kwargs)
    return wrapper


class Store:
    """One writer contract, safe to open from separate processes."""

    def __init__(self, root: str | Path):
        self._exclusive_local = threading.local()
        self.root = Path(root).expanduser().resolve()
        # A live queue must not reside in a known CloudStorage mount.
        if "CloudStorage" in self.root.parts or ".git" in self.root.parts:
            raise PreservationError("state_requires_local_directory")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.versions = self.root / "versions"
        self.versions.mkdir(exist_ok=True, mode=0o700)
        self.db_path = self.root / "registry.sqlite3"
        with self.connect() as db:
            current = db.execute("PRAGMA user_version").fetchone()[0]
            if current not in (0, 1):
                raise PreservationError("unsupported_registry_schema")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY, source_key TEXT NOT NULL, scope TEXT NOT NULL,
                    kind TEXT NOT NULL, created_at TEXT NOT NULL, forgotten_at TEXT,
                    UNIQUE(source_key, scope)
                );
                CREATE TABLE IF NOT EXISTS versions (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
                    observed_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS manifest_integrity (
                    version_id TEXT PRIMARY KEY REFERENCES versions(id), sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS superseded_content (
                    source_id TEXT NOT NULL REFERENCES sources(id), original_sha256 TEXT NOT NULL,
                    text_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(source_id,original_sha256,text_sha256)
                );
                CREATE TABLE IF NOT EXISTS locations (
                    source_id TEXT NOT NULL REFERENCES sources(id), locator TEXT NOT NULL,
                    observed_at TEXT NOT NULL, PRIMARY KEY(source_id, locator)
                );
                CREATE TABLE IF NOT EXISTS delivery (
                    version_id TEXT PRIMARY KEY REFERENCES versions(id), operation_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
                    lease_id TEXT, last_error TEXT, units INTEGER, verified_at TEXT,
                    remote_state TEXT NOT NULL DEFAULT 'unconfirmed', remote_id TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                    source_id TEXT NOT NULL REFERENCES sources(id), version_id TEXT,
                    replacement_id TEXT, reason TEXT NOT NULL, created_at TEXT NOT NULL,
                    applied INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS registrations (
                    thread_id TEXT PRIMARY KEY, transcript_path TEXT NOT NULL,
                    started_at TEXT NOT NULL, codex_version TEXT NOT NULL,
                    scope TEXT NOT NULL, last_source_hash TEXT, last_gap TEXT
                );
                CREATE TABLE IF NOT EXISTS captured_events (
                    thread_id TEXT NOT NULL REFERENCES registrations(thread_id), event_id TEXT NOT NULL,
                    turn_id TEXT, observed_at TEXT NOT NULL, message_json TEXT NOT NULL,
                    PRIMARY KEY(thread_id,event_id)
                );
                CREATE INDEX IF NOT EXISTS delivery_pending ON delivery(state, next_attempt, lease_until);
                PRAGMA user_version=1;
            """)
        self.db_path.chmod(0o600)

    @contextmanager
    def exclusive(self):
        """Serialize network writers with local revocations across processes."""
        if getattr(self._exclusive_local, "depth", 0):
            self._exclusive_local.depth += 1
            try:
                yield
            finally:
                self._exclusive_local.depth -= 1
            return
        fd = os.open(self.root / "writer.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._exclusive_local.depth = 1
            yield
        finally:
            self._exclusive_local.depth = 0
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=15, autocommit=True)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            if write:
                db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
            else:
                if db.in_transaction:
                    db.execute("COMMIT")
        finally:
            db.close()

    def capture(self, *, source_key: str, scope: str, title: str, original: bytes,
                text: str, locator: str = "", kind: str = "document",
                metadata: dict[str, str] | None = None, observed_at: str | None = None,
                archive_only: bool = False) -> Receipt:
        if not isinstance(original, bytes) or not isinstance(text, str):
            raise PreservationError("invalid_capture_type")
        if type(archive_only) is not bool:
            raise PreservationError("invalid_archive_mode")
        if not source_key or not scope or (not text.strip() and not archive_only):
            raise PreservationError("source_scope_and_text_required")
        if observed_at is not None:
            try:
                if datetime.fromisoformat(observed_at.replace("Z", "+00:00")).tzinfo is None:
                    raise ValueError
            except (AttributeError, ValueError):
                raise PreservationError("timestamp_requires_timezone") from None
        meta = dict(metadata or {})
        if archive_only:
            meta["delivery_mode"] = "archive_only"
            meta["text_availability"] = "present" if text.strip() else "absent"
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in meta.items()):
            raise PreservationError("metadata_must_be_strings")
        from .materials import material_profile
        material_profile({"kind": kind, "metadata": meta})
        semantic = {"source_key": source_key, "scope": scope, "kind": kind, "title": title, "metadata": meta}
        guard_no_secrets(original)
        guard_no_secrets(text.encode())
        guard_no_secrets(canonical(semantic))
        guard_no_secrets(locator.encode())
        source_id = "ols-" + digest(canonical([scope, source_key]))
        version_id = "olv-" + digest(canonical([source_id, digest(original), digest(text.encode()), semantic]))
        with self.connect() as db:
            row = db.execute("SELECT forgotten_at FROM sources WHERE id=?", (source_id,)).fetchone()
            if row and row[0]:
                raise PreservationError("source_is_forgotten")
        manifest = {
            "schema": 1, "source_id": source_id, "version_id": version_id,
            "original_sha256": digest(original), "text_sha256": digest(text.encode()),
            "observed_at": observed_at or timestamp(), "locator": locator, **semantic,
        }
        final = self.versions / version_id
        if not final.exists():
            staging = Path(tempfile.mkdtemp(prefix=".capture-", dir=self.versions))
            try:
                _write_file(staging / "original", original)
                _write_file(staging / "text.txt", text.encode())
                _write_file(staging / "manifest.json", canonical(manifest))
                _write_file(staging / "manifest.sha256", digest(canonical(manifest)).encode())
                _sync_dir(staging)
                try:
                    os.rename(staging, final)
                except OSError:
                    if not final.is_dir():
                        raise
                _sync_dir(self.versions)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        saved = self.read_version(version_id)
        self._register(saved, locator=locator)
        return self.receipt(version_id)

    def read_version(self, version_id: str) -> dict:
        if not re.fullmatch(r"olv-[a-f0-9]{64}", version_id):
            raise PreservationError("invalid_version_id")
        folder = self.versions / version_id
        if folder.is_symlink():
            raise PreservationError("unexpected_source_symlink")
        try:
            for name in ("original", "text.txt", "manifest.json"):
                if (folder / name).is_symlink():
                    raise PreservationError("unexpected_source_symlink")
            m = json.loads((folder / "manifest.json").read_text())
            manifest_bytes = (folder / "manifest.json").read_bytes()
            actual_manifest_hash = digest(manifest_bytes)
            anchor = (folder / "manifest.sha256").read_text()
            if actual_manifest_hash != anchor:
                raise PreservationError("source_manifest_hash_mismatch")
            with self.connect() as db:
                stored_anchor = db.execute("SELECT sha256 FROM manifest_integrity WHERE version_id=?", (version_id,)).fetchone()
                if stored_anchor and stored_anchor[0] != actual_manifest_hash:
                    raise PreservationError("source_manifest_hash_mismatch")
            original = (folder / "original").read_bytes()
            text_bytes = (folder / "text.txt").read_bytes()
            semantic = {k: m[k] for k in ("source_key", "scope", "kind", "title", "metadata")}
            expected_source = "ols-" + digest(canonical([m["scope"], m["source_key"]]))
            expected_version = "olv-" + digest(canonical([expected_source, digest(original), digest(text_bytes), semantic]))
            if m["schema"] != 1 or m["source_id"] != expected_source or m["version_id"] != version_id or expected_version != version_id:
                raise PreservationError("source_manifest_mismatch")
            if digest(original) != m["original_sha256"] or digest(text_bytes) != m["text_sha256"]:
                raise PreservationError("source_hash_mismatch")
            return {**m, "text": text_bytes.decode("utf-8"), "original": original}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise PreservationError("invalid_source_version") from None

    def _register(self, m: dict, *, locator: str | None = None) -> None:
        from .materials import material_profile
        initial_state = "pending" if material_profile(m)["indexable"] else "archived"
        operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "olympus:retain:" + m["version_id"]))
        with self.connect(write=True) as db:
            db.execute("INSERT OR IGNORE INTO sources(id,source_key,scope,kind,created_at) VALUES(?,?,?,?,?)",
                       (m["source_id"], m["source_key"], m["scope"], m["kind"], m["observed_at"]))
            forgotten = db.execute("SELECT forgotten_at FROM sources WHERE id=?", (m["source_id"],)).fetchone()[0]
            superseded = db.execute("SELECT 1 FROM superseded_content WHERE source_id=? AND original_sha256=? AND text_sha256=?",
                                    (m["source_id"], m["original_sha256"], m["text_sha256"])).fetchone()
            active = 0 if forgotten or superseded else 1
            db.execute("INSERT OR IGNORE INTO versions(id,source_id,observed_at,active) VALUES(?,?,?,?)",
                       (m["version_id"], m["source_id"], m["observed_at"], active))
            manifest_hash = digest((self.versions / m["version_id"] / "manifest.json").read_bytes())
            db.execute("INSERT OR IGNORE INTO manifest_integrity VALUES(?,?)", (m["version_id"], manifest_hash))
            db.execute("INSERT OR IGNORE INTO delivery(version_id,operation_id,state,updated_at) VALUES(?,?,?,?)",
                       (m["version_id"], operation_id, "forgotten" if forgotten else "superseded" if superseded else initial_state, timestamp()))
            location = m["locator"] if locator is None else locator
            if location:
                db.execute("INSERT INTO locations VALUES(?,?,?) ON CONFLICT(source_id,locator) DO UPDATE SET observed_at=excluded.observed_at",
                           (m["source_id"], location, timestamp()))

    def recover(self) -> dict:
        recovered, invalid = 0, []
        with self.connect() as db:
            known = {x[0] for x in db.execute("SELECT id FROM versions")}
        for folder in sorted(self.versions.iterdir()):
            if not folder.name.startswith("olv-"):
                continue
            try:
                m = self.read_version(folder.name)
                self._register(m)
                recovered += folder.name not in known
            except PreservationError:
                invalid.append(folder.name)
        return {"recovered": recovered, "invalid": invalid}

    def receipt(self, version_id: str) -> Receipt:
        with self.connect() as db:
            row = db.execute("SELECT v.source_id,d.* FROM versions v JOIN delivery d ON d.version_id=v.id WHERE v.id=?", (version_id,)).fetchone()
            if row is None:
                raise PreservationError("unknown_version")
            correction = "pending" if db.execute("SELECT 1 FROM changes WHERE source_id=? AND applied=0 LIMIT 1", (row["source_id"],)).fetchone() else "not_required"
            return Receipt(row["source_id"], version_id, version_id, row["operation_id"], "durable", row["state"], row["remote_state"], correction)

    def claim(self, *, lease_seconds: float = 90, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        with self.connect(write=True) as db:
            recovery = db.execute("SELECT value FROM settings WHERE key='recovery_state'").fetchone()
            maintenance = db.execute("SELECT value FROM settings WHERE key='maintenance'").fetchone()
            if (recovery and recovery[0] != "ready") or (maintenance and maintenance[0] != "off"):
                return None
            row = db.execute("""SELECT d.*,v.source_id,s.scope FROM delivery d
                JOIN versions v ON v.id=d.version_id JOIN sources s ON s.id=v.source_id
                WHERE d.state IN ('pending','submitted') AND d.next_attempt<=? AND d.lease_until<=?
                AND v.active=1 AND s.forgotten_at IS NULL ORDER BY v.observed_at,d.version_id LIMIT 1""", (now, now)).fetchone()
            if row is None:
                return None
            lease_id = str(uuid.uuid4())
            db.execute("UPDATE delivery SET lease_until=?,lease_id=? WHERE version_id=?", (now + lease_seconds, lease_id, row["version_id"]))
            return {**dict(row), "lease_id": lease_id, "lease_until": now + lease_seconds}

    def update_delivery(self, job: dict, state: str, *, error: str | None = None,
                        delay: float = 0, units: int | None = None, attempted: bool = False) -> bool:
        if state not in {"pending", "submitted", "searchable", "empty", "failed", "blocked", "archived"}:
            raise PreservationError("invalid_delivery_state")
        if error and not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,120}", error):
            raise PreservationError("unsafe_error_code")
        with self.connect(write=True) as db:
            result = db.execute("""UPDATE delivery SET state=?,last_error=?,next_attempt=?,
                lease_until=0,lease_id=NULL,attempts=attempts+?,units=?,verified_at=?,updated_at=?
                WHERE version_id=? AND lease_id=? AND state IN ('pending','submitted')""",
                (state, error, time.time() + delay, int(attempted), units, timestamp() if state == "searchable" else None,
                 timestamp(), job["version_id"], job["lease_id"]))
            return result.rowcount == 1

    def retry(self, version_id: str) -> None:
        with self.connect(write=True) as db:
            row = db.execute("SELECT active FROM versions WHERE id=?", (version_id,)).fetchone()
            if row is None or not row[0]:
                raise PreservationError("version_not_active")
            db.execute("UPDATE delivery SET state='pending',last_error=NULL,next_attempt=0,lease_until=0,lease_id=NULL WHERE version_id=? AND state IN ('failed','blocked','empty')", (version_id,))

    def status(self) -> dict:
        with self.connect() as db:
            counts = {row[0]: row[1] for row in db.execute("SELECT state,count(*) FROM delivery GROUP BY state")}
            pending_corrections = db.execute("SELECT count(*) FROM changes WHERE applied=0").fetchone()[0]
            oldest = db.execute("SELECT min(v.observed_at) FROM versions v JOIN delivery d ON d.version_id=v.id WHERE d.state IN ('pending','submitted','blocked','failed')").fetchone()[0]
            errors = [dict(r) for r in db.execute("SELECT version_id,state,last_error FROM delivery WHERE last_error IS NOT NULL ORDER BY updated_at DESC LIMIT 10")]
            return {"versions": sum(counts.values()), "delivery": counts, "oldest_pending": oldest,
                    "pending_corrections": pending_corrections, "errors": errors,
                    "remote_unconfirmed": db.execute("SELECT count(*) FROM delivery WHERE remote_state='unconfirmed'").fetchone()[0]}

    def active_documents(self, scope: str) -> set[str]:
        with self.connect() as db:
            recovery = db.execute("SELECT value FROM settings WHERE key='recovery_state'").fetchone()
            if recovery and recovery[0] != "ready":
                raise PreservationError("recovery_verification_required")
            if db.execute("SELECT 1 FROM changes WHERE applied=0 LIMIT 1").fetchone():
                raise PreservationError("correction_reconciliation_pending")
            return {r[0] for r in db.execute("""SELECT v.id FROM versions v JOIN sources s ON s.id=v.source_id
                JOIN delivery d ON d.version_id=v.id WHERE s.scope=? AND s.forgotten_at IS NULL AND v.active=1
                AND d.state='searchable'""", (scope,))}

    @serialized
    def forget(self, source_id: str, reason: str) -> int:
        guard_no_secrets(reason.encode())
        with self.connect(write=True) as db:
            source = db.execute("SELECT forgotten_at FROM sources WHERE id=?", (source_id,)).fetchone()
            if source is None:
                raise PreservationError("unknown_source")
            if source[0]:
                return 0
            db.execute("UPDATE sources SET forgotten_at=? WHERE id=?", (timestamp(), source_id))
            db.execute("UPDATE versions SET active=0 WHERE source_id=?", (source_id,))
            db.execute("UPDATE delivery SET state='forgotten',lease_id=NULL,lease_until=0 WHERE version_id IN (SELECT id FROM versions WHERE source_id=?)", (source_id,))
            return db.execute("INSERT INTO changes(kind,source_id,reason,created_at) VALUES('forget',?,?,?)", (source_id, reason, timestamp())).lastrowid

    @serialized
    def supersede(self, version_id: str, replacement_id: str, reason: str) -> int:
        guard_no_secrets(reason.encode())
        original = self.read_version(version_id)
        with self.connect(write=True) as db:
            rows = {r["id"]: r for r in db.execute("SELECT id,source_id,active FROM versions WHERE id IN (?,?)", (version_id, replacement_id))}
            if len(rows) != 2 or rows[version_id]["source_id"] != rows[replacement_id]["source_id"] or not rows[replacement_id]["active"]:
                raise PreservationError("invalid_replacement")
            if not rows[version_id]["active"]:
                return 0
            db.execute("UPDATE versions SET active=0 WHERE id=?", (version_id,))
            db.execute("INSERT OR IGNORE INTO superseded_content VALUES(?,?,?,?)",
                       (original["source_id"], original["original_sha256"], original["text_sha256"], timestamp()))
            db.execute("UPDATE delivery SET state='superseded',lease_id=NULL,lease_until=0 WHERE version_id=?", (version_id,))
            return db.execute("INSERT INTO changes(kind,source_id,version_id,replacement_id,reason,created_at) VALUES('supersede',?,?,?,?,?)",
                              (rows[version_id]["source_id"], version_id, replacement_id, reason, timestamp())).lastrowid

    def pending_changes(self) -> list[dict]:
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM changes WHERE applied=0 ORDER BY id")]

    def change_documents(self, change: dict) -> list[str]:
        if change["version_id"]:
            return [change["version_id"]]
        with self.connect() as db:
            return [r[0] for r in db.execute("SELECT id FROM versions WHERE source_id=?", (change["source_id"],))]

    def setting(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        guard_no_secrets(value.encode())
        with self.connect(write=True) as db:
            db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
