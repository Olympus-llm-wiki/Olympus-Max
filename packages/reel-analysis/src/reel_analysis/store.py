"""Durable catalogue and immutable job versions, owned by a single installation."""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import uuid

from .common import ReelError, canonical, code_version, digest, file_hash, identifier, read_json, sync_dir, write_json
from .contracts import Profile
from . import media


@contextmanager
def file_lock(path, blocking=True):
    with Path(path).open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise ReelError("worker_busy") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in ("assets", "jobs", "incoming"):
            (self.root / directory).mkdir(exist_ok=True, mode=0o700)
        self.db_path = self.root / "jobs.sqlite3"
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY, metadata TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL, asset_id TEXT NOT NULL,
                profile TEXT NOT NULL, course TEXT NOT NULL, parent_id TEXT, rerun_track TEXT,
                state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, error TEXT,
                runtime TEXT NOT NULL);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def worker_lock(self, blocking=False):
        return file_lock(self.root / "worker.lock", blocking)

    def ingest(self, source, profile=None):
        """Copy first, then validate those exact bytes, never the mutable caller path."""
        source = Path(source)
        if not source.is_file() or source.is_symlink():
            raise ReelError("video_file_required")
        profile = profile or Profile()
        fd, temp = tempfile.mkstemp(dir=self.root / "incoming", suffix=".video")
        temp = Path(temp)
        try:
            with os.fdopen(fd, "wb") as target, source.open("rb") as stream:
                size = 0
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    size += len(block)
                    if size > profile.max_bytes:
                        raise ReelError("file_too_large")
                    target.write(block)
                target.flush()
                os.fsync(target.fileno())
            info = media.inspect(temp, profile)
            asset_id = "a_" + info["sha256"]
            info["id"] = asset_id
            # No client paths survive in public source metadata.
            info["original_name"] = source.name
            with file_lock(self.root / "catalog.lock"):
                folder = self.root / "assets" / asset_id
                folder.mkdir(exist_ok=True)
                original = folder / "original.bin"
                if original.exists():
                    if file_hash(original) != info["sha256"]:
                        raise ReelError("asset_hash_mismatch")
                else:
                    os.replace(temp, original)
                    sync_dir(folder)
                with self.db() as db:
                    db.execute("INSERT OR IGNORE INTO assets VALUES (?,?)", (asset_id, canonical(info)))
            return self.asset(asset_id)
        finally:
            temp.unlink(missing_ok=True)

    def asset(self, asset_id):
        identifier(asset_id, "a_")
        with self.db() as db:
            row = db.execute("SELECT metadata FROM assets WHERE id=?", (asset_id,)).fetchone()
        if not row:
            raise ReelError("asset_not_found")
        import json
        return json.loads(row[0])

    def original(self, asset_id):
        info = self.asset(asset_id)
        path = self.root / "assets" / asset_id / "original.bin"
        if not path.is_file() or file_hash(path) != info["sha256"]:
            raise ReelError("asset_hash_mismatch")
        return path

    def job_dir(self, job_id):
        return self.root / "jobs" / identifier(job_id, "j_")

    def submit(self, asset_id, profile=None, course=None, *, parent_id=None, rerun_track=None):
        profile = profile or Profile()
        info = self.asset(asset_id)
        if info["duration_s"] > profile.max_duration_s or info["bytes"] > profile.max_bytes:
            raise ReelError("profile_input_limit")
        course = course or []
        if not isinstance(course, list) or len(course) > 10:
            raise ReelError("course_limit")
        for item in course:
            if not isinstance(item, dict) or set(item) != {"source_id", "text"} or not all(isinstance(v, str) for v in item.values()):
                raise ReelError("invalid_course_source")
        if len(canonical(course).encode()) > 100_000 or len({x["source_id"] for x in course}) != len(course):
            raise ReelError("course_limit")
        from .antigravity import Antigravity
        runtime = {"code": code_version(), "media_tools": media.versions(), "executor": Antigravity().identity()}
        identity = digest({"asset": asset_id, "profile": profile.model_dump(), "course": course, "runtime": runtime, "rerun": str(uuid.uuid4()) if parent_id else None})
        with file_lock(self.root / "catalog.lock"), self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM jobs WHERE identity=?", (identity,)).fetchone()
            if row:
                return self.get(row["id"])
            job_id = "j_" + uuid.uuid4().hex
            now = time.time()
            folder = self.job_dir(job_id)
            folder.mkdir()
            write_json(folder / "request.json", {"asset_id": asset_id, "profile": profile.model_dump(), "course": course, "parent_id": parent_id, "rerun_track": rerun_track, "runtime": runtime})
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (job_id, identity, asset_id, canonical(profile.model_dump()), canonical(course), parent_id, rerun_track, "queued", now, now, None, canonical(runtime)))
        return self.get(job_id)

    def get(self, job_id):
        import json
        identifier(job_id, "j_")
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ReelError("job_not_found")
        result = dict(row)
        for field in ("profile", "course", "runtime"):
            result[field] = json.loads(result[field])
        return result

    def state(self, job_id, state, error=None):
        with self.db() as db:
            db.execute("UPDATE jobs SET state=?,error=?,updated=? WHERE id=?", (state, error, time.time(), job_id))

    def next_job(self):
        with self.db() as db:
            # An unknown inference may still be active remotely. Hold all new model work.
            if db.execute("SELECT 1 FROM jobs WHERE state='needs_attention' AND error='inference_outcome_unknown'").fetchone():
                return None
            row = db.execute("SELECT id FROM jobs WHERE state IN ('running','waiting_provider','queued') ORDER BY CASE WHEN state='queued' THEN 1 ELSE 0 END, created, id LIMIT 1").fetchone()
        return self.get(row[0]) if row else None

    def retry(self, job_id):
        job = self.get(job_id)
        if job["state"] not in ("needs_attention", "failed"):
            raise ReelError("job_not_retryable")
        # Does not clear the attempt journal: worker can recover raw, never blind-resubmit.
        self.state(job_id, "queued")
        return self.get(job_id)

    def rerun(self, job_id, track, profile=None):
        if track not in ("speech", "visual", "all"):
            raise ReelError("invalid_rerun_track")
        parent = self.get(job_id)
        if parent["state"] != "completed":
            raise ReelError("parent_not_complete")
        return self.submit(parent["asset_id"], profile or Profile(**parent["profile"]), parent["course"], parent_id=job_id, rerun_track=track)

    def revalidate(self, job_id):
        parent = self.get(job_id)
        self.result(job_id)
        if parent["state"] != "completed":
            raise ReelError("parent_not_complete")
        return self.submit(parent["asset_id"], Profile(**parent["profile"]), parent["course"], parent_id=job_id, rerun_track="revalidate")

    def result(self, job_id):
        self.get(job_id)
        folder = self.job_dir(job_id) / "result"
        if not folder.is_dir():
            raise ReelError("result_not_ready")
        manifest = read_json(folder / "manifest.json")
        for name, expected in manifest["files"].items():
            path = folder / name
            if Path(name).is_absolute() or ".." in Path(name).parts or path.is_symlink() or folder.resolve() not in path.resolve().parents or not path.is_file() or file_hash(path) != expected:
                raise ReelError("result_hash_mismatch")
        return folder

    def backup(self, target):
        target = Path(target).resolve()
        if target == self.root or self.root in target.parents or target.exists():
            raise ReelError("backup_requires_new_external_directory")
        with self.worker_lock(), file_lock(self.root / "catalog.lock"):
            shutil.copytree(self.root, target, ignore=shutil.ignore_patterns("*.lock", "incoming"))
            with self.db() as db, sqlite3.connect(target / "jobs.sqlite3") as copy:
                db.backup(copy)
            entries = {str(x.relative_to(target)): file_hash(x) for x in target.rglob("*") if x.is_file()}
            write_json(target / "backup-manifest.json", {"schema": 1, "files": entries})
        return target

    @staticmethod
    def restore(source, target):
        source, target = Path(source), Path(target)
        if target.exists() or source.resolve() in target.resolve().parents:
            raise ReelError("restore_target_exists")
        manifest = read_json(source / "backup-manifest.json")
        actual = {str(p.relative_to(source)) for p in source.rglob("*") if p.is_file() and p.name != "backup-manifest.json"}
        if actual != set(manifest["files"]) or any(p.is_symlink() for p in source.rglob("*")):
            raise ReelError("backup_hash_mismatch")
        for name, expected in manifest["files"].items():
            path = source / name
            if Path(name).is_absolute() or ".." in Path(name).parts or path.is_symlink() or not path.is_file() or file_hash(path) != expected:
                raise ReelError("backup_hash_mismatch")
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("backup-manifest.json"))
        return Store(target)
