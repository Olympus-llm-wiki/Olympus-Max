"""Bounded local library staging and note capture through the durable Store.

There is no remote Drive API here. Files on a sync mount are staged copies,
never proof of a remote backup. No live database is copied to the library.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Iterator, Sequence
import uuid

from .preservation import Store, PreservationError, canonical, digest, guard_no_secrets

_SELECTABLE = frozenset({"Inbox", "Library/Personal"})
_TEXT_EXTENSIONS = frozenset({".md", ".markdown", ".txt", ".text"})
_CONFLICT_NAME = re.compile(r"conflict(?:ed)? copy|sync conflict|конфликтная копия|копия с конфликтом", re.I)


def _limit(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 10000:
        raise PreservationError("invalid_library_limit")


def _root(path: Path, *, create: bool) -> Path:
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise PreservationError("library_root_must_be_absolute")
    if path.is_symlink():
        raise PreservationError("library_root_symlink")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise PreservationError("library_root_unavailable")
    resolved = path.resolve()
    guard_no_secrets(str(resolved).encode())
    return resolved


def _directory(parent: Path, name: str) -> Path:
    path = parent / name
    if path.is_symlink():
        raise PreservationError("library_directory_symlink")
    path.mkdir(exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise PreservationError("library_directory_conflict")
    return path


@contextmanager
def _lock(store: Store, key: str) -> Iterator[None]:
    path = store.root / (".library-" + digest(key.encode())[:32] + ".lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PreservationError("library_lock_not_regular")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _same_file(path: Path, expected: bytes) -> bool:
    if path.is_symlink():
        raise PreservationError("library_file_symlink")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return False
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise PreservationError("library_file_not_regular")
        if info.st_size != len(expected) or stream.read(len(expected) + 1) != expected:
            raise PreservationError("library_existing_bytes_conflict")
    return True


def _publish_files(directory: Path, files: dict[str, bytes], *, strict: bool) -> bool:
    """Publish complete files by hardlink, manifest last; never clobber a name.

    A crash can leave a subset of complete files. The next call compares every
    existing byte and fills missing names. A manifest is the commit marker, and
    the staging receipt is written only after all files have been verified.
    """
    if directory.is_symlink():
        raise PreservationError("library_directory_symlink")
    directory.mkdir(exist_ok=True, mode=0o700)
    if not directory.is_dir():
        raise PreservationError("library_directory_conflict")
    if strict and set(p.name for p in directory.iterdir()) - set(files) - {".DS_Store"}:
        raise PreservationError("library_unexpected_version_files")
    existing = [_same_file(directory / name, data) for name, data in files.items()]
    existed = all(existing)
    if existed:
        return False
    stage = Path(tempfile.mkdtemp(prefix=".olympus-export-", dir=directory.parent))
    try:
        for name, data in files.items():
            file = stage / name
            fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        _sync_directory(stage)
        for name, data in files.items():
            target = directory / name
            if _same_file(target, data):
                continue
            try:
                os.link(stage / name, target, follow_symlinks=False)
            except FileExistsError:
                if not _same_file(target, data):
                    raise PreservationError("library_publish_race") from None
        _sync_directory(directory)
        _sync_directory(directory.parent)
        for name, data in files.items():
            if not _same_file(directory / name, data):
                raise PreservationError("library_publish_incomplete")
    finally:
        shutil.rmtree(stage)
    return True


def _safe_code(exc: Exception, fallback: str) -> str:
    code = str(exc)
    return code if isinstance(exc, PreservationError) and re.fullmatch(r"[a-z_]{1,100}", code) else fallback


def _card(source: dict) -> bytes:
    sid = source["source_id"]
    text = "\n".join([
        "# Источник " + sid, "",
        "Область: " + json.dumps(source["scope"], ensure_ascii=False),
        "Тип: " + json.dumps(source["source_kind"], ensure_ascii=False),
        "Идентичность: " + json.dumps(source["source_key"], ensure_ascii=False), "",
        f"[Сохранённые версии и их манифесты](../../Corpus/{sid}/)", "",
        "Каждая версия содержит original, text.txt и manifest.json.",
        "Эта карточка описывает происхождение; она не назначает версию актуальной истиной.",
        "Поздняя загрузка и время изменения файла сами по себе не отменяют прежнее решение.",
        "Применимость, замены и отзывы проверяются по журналу Olympus.", "",
    ])
    return text.encode()


def export_versions(store: Store, library_root: Path, *, limit: int = 100) -> dict:
    """Stage at most ``limit`` registered versions and short source cards.

    A persisted cursor rotates through non-forgotten versions, including those
    already staged, so subsequent passes also detect tampering and sync conflicts.
    No existing bytes are overwritten or deleted, including inactive history.
    """
    _limit(limit)
    root = _root(library_root, create=True)
    if root == store.root or root in store.root.parents or store.root in root.parents:
        raise PreservationError("library_and_state_must_be_separate")
    key = "library_export_cursor:" + digest(str(root).encode())
    result = {"checked": 0, "exported": 0, "already_present": 0, "errors": [],
              "staged": [], "remote_verified": 0, "cycle_complete": False}
    with _lock(store, "export:" + str(root)):
        corpus = _directory(root, "Corpus")
        cards = _directory(_directory(root, "Library"), "Sources")
        cursor = store.setting(key, "")
        with store.connect() as db:
            rows = [dict(r) for r in db.execute("""SELECT v.id AS version_id,v.source_id,
                s.source_key,s.scope,s.kind AS source_kind FROM versions v
                JOIN sources s ON s.id=v.source_id WHERE s.forgotten_at IS NULL AND v.id>?
                ORDER BY v.id LIMIT ?""", (cursor, limit + 1))]
            if not rows and cursor:
                rows = [dict(r) for r in db.execute("""SELECT v.id AS version_id,v.source_id,
                    s.source_key,s.scope,s.kind AS source_kind FROM versions v
                    JOIN sources s ON s.id=v.source_id WHERE s.forgotten_at IS NULL
                    ORDER BY v.id LIMIT ?""", (limit + 1,))]
        result["cycle_complete"] = len(rows) <= limit
        for row in rows[:limit]:
            vid = row["version_id"]
            result["checked"] += 1
            try:
                value = store.read_version(vid)
                if value["source_id"] != row["source_id"]:
                    raise PreservationError("library_source_id_mismatch")
                manifest = canonical({k: v for k, v in value.items() if k not in {"original", "text"}})
                card = _card(row)
                for exported_bytes in (value["original"], value["text"].encode(), manifest, card):
                    guard_no_secrets(exported_bytes)
                source_dir = _directory(corpus, value["source_id"])
                version_dir = source_dir / vid
                published = _publish_files(version_dir, {
                    "original": value["original"], "text.txt": value["text"].encode(),
                    "manifest.json": manifest,
                }, strict=True)
                _publish_files(cards, {value["source_id"] + ".md": card}, strict=False)
                staged = {"path": str(version_dir), "manifest_sha256": digest(manifest),
                          "status": "local_staged", "remote_copy": "unconfirmed"}
                store.set_setting("drive_staged:" + vid, canonical(staged).decode())
                result["exported" if published else "already_present"] += 1
                result["staged"].append({"source_id": value["source_id"], "version_id": vid,
                                         "path": str(version_dir), "manifest_sha256": digest(manifest)})
            except (PreservationError, OSError, ValueError) as exc:
                code = _safe_code(exc, "library_export_io_error")
                result["errors"].append({"version_id": vid, "code": code})
                store.set_setting("drive_staged:" + vid, canonical({
                    "status": "error", "code": code, "remote_copy": "unconfirmed",
                }).decode())
        next_cursor = "" if result["cycle_complete"] else rows[limit - 1]["version_id"]
        store.set_setting(key, next_cursor)
    return result


def _file_identity(info: os.stat_result) -> str:
    birth = getattr(info, "st_birthtime_ns", None)
    if birth is None and hasattr(info, "st_birthtime"):
        birth = int(info.st_birthtime * 1_000_000_000)
    return f"{info.st_dev}:{info.st_ino}:{birth if birth is not None else 'unknown'}"


def _issue(path: str, code: str) -> dict:
    # Filenames can themselves contain credentials or private text.
    return {"file_ref": digest(path.encode())[:24], "code": code}


def _inventory(root: Path, selected: Sequence[str], limit: int) -> tuple[list[tuple[str, os.stat_result]], set[str], list[dict], bool]:
    candidates: list[tuple[str, os.stat_result]] = []
    seen: set[str] = set()
    issues: list[dict] = []
    pending = list(reversed(sorted(selected)))
    inspected = 0
    complete = True
    while pending:
        relative = pending.pop()
        directory = root / relative
        chain = root
        symlink = False
        for part in Path(relative).parts:
            chain = chain / part
            symlink = symlink or chain.is_symlink()
        if symlink or not directory.is_dir():
            issues.append(_issue(relative, "selected_directory_unavailable"))
            complete = False
            continue
        try:
            # Bound enumeration itself, not only subsequent file reads.
            with os.scandir(directory) as iterator:
                entries = []
                for entry in iterator:
                    if inspected >= limit:
                        complete = False
                        break
                    entries.append(entry)
                    inspected += 1
            for entry in sorted(entries, key=lambda e: e.name):
                rel = str(Path(relative) / entry.name)
                seen.add(rel)
                if entry.is_symlink():
                    issues.append(_issue(rel, "note_symlink_not_followed"))
                    continue
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(rel)
                elif stat.S_ISREG(info.st_mode):
                    candidates.append((rel, info))
                else:
                    issues.append(_issue(rel, "note_not_regular"))
            if not complete and inspected >= limit:
                break
        except OSError:
            complete = False
            issues.append(_issue(relative, "directory_scan_failed"))
    if pending or inspected >= limit and not complete:
        complete = False
    if not complete and inspected >= limit:
        issues.append({"code": "scan_limit_reached"})
    return sorted(candidates), seen, issues, complete


def _read_note(path: Path, expected: os.stat_result, max_bytes: int) -> tuple[bytes, str]:
    if expected.st_size > max_bytes:
        raise PreservationError("note_size_limit")
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or _file_identity(before) != _file_identity(expected):
            raise PreservationError("note_changed_during_scan")
        raw = stream.read(max_bytes + 1)
        after = os.fstat(stream.fileno())
    current = path.stat(follow_symlinks=False)
    observed = lambda st: (_file_identity(st), st.st_size, st.st_mtime_ns)
    if len(raw) > max_bytes:
        raise PreservationError("note_size_limit")
    if observed(before) != observed(after) or observed(after) != observed(current):
        raise PreservationError("note_changed_during_scan")
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise PreservationError("note_encoding_unsupported") from None
    if "\x00" in text:
        raise PreservationError("binary_note_unsupported")
    if not text.strip():
        raise PreservationError("empty_note_pending")
    if re.search(r"(?m)^<<<<<<<(?: |$)", text) and re.search(r"(?m)^>>>>>>>(?: |$)", text):
        raise PreservationError("note_merge_conflict")
    guard_no_secrets(raw)
    return raw, text


def scan_notes(store: Store, library_root: Path, *, selected: Sequence[str],
               scope: str, limit: int = 100, max_bytes: int = 8 * 1024 * 1024) -> dict:
    """Capture text versions only from explicitly selected approved subtrees.

    File identities and atomic replacement at the same full path preserve a
    source. A copied file, cross-volume move, missing/reappeared path, or ambiguous
    conflict is never silently treated as an owner-approved supersession.
    """
    _limit(limit)
    if (not selected or isinstance(selected, str) or not all(isinstance(item, str) for item in selected)
            or len(set(selected)) != len(selected)
            or any(item not in _SELECTABLE for item in selected)):
        raise PreservationError("explicit_note_directories_required")
    if not scope or not isinstance(scope, str):
        raise PreservationError("note_scope_required")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 1 <= max_bytes <= 64 * 1024 * 1024:
        raise PreservationError("invalid_note_size_limit")
    root = _root(library_root, create=False)
    if root == store.root or root in store.root.parents or store.root in root.parents:
        raise PreservationError("library_and_state_must_be_separate")
    registry_key = "library_files:" + digest(canonical([str(root), scope]))
    result = {"captured": 0, "unchanged": 0, "renamed": 0, "missing": [],
              "issues": [], "versions": [], "complete_scan": False}
    with _lock(store, registry_key):
        try:
            registry = json.loads(store.setting(registry_key, '{"schema":1,"entries":{}}'))
            if registry["schema"] != 1 or not isinstance(registry["entries"], dict):
                raise ValueError
            entries = registry["entries"]
            required = {"identity", "path", "title", "sha256", "source_id", "version_id", "missing"}
            if any(not isinstance(v, dict) or not required <= set(v) for v in entries.values()):
                raise ValueError
            for source_key, entry in entries.items():
                if not isinstance(source_key, str) or any(
                    not isinstance(entry[field], str) for field in required - {"missing"}
                ) or not isinstance(entry["missing"], bool):
                    raise ValueError
                relative = Path(entry["path"])
                if relative.is_absolute() or ".." in relative.parts or not any(
                    relative.is_relative_to(Path(area)) for area in _SELECTABLE
                ):
                    raise ValueError
        except (ValueError, KeyError, TypeError):
            raise PreservationError("invalid_note_identity_registry") from None
        candidates, seen, issues, complete = _inventory(root, selected, limit)
        result["issues"].extend(issues)
        result["complete_scan"] = complete
        identity_counts = Counter(_file_identity(info) for _, info in candidates)
        current_identities = set(identity_counts)
        with store.connect() as db:
            forgotten = {r[0] for r in db.execute("SELECT id FROM sources WHERE forgotten_at IS NOT NULL")}
        processed_keys: set[str] = set()
        for relative, info in candidates:
            path = root / relative
            try:
                guard_no_secrets(path.as_uri().encode())
                if _CONFLICT_NAME.search(path.name):
                    raise PreservationError("note_sync_conflict")
                if path.suffix.lower() not in _TEXT_EXTENSIONS:
                    raise PreservationError("note_type_pending_extraction")
                identity = _file_identity(info)
                if identity_counts[identity] > 1:
                    raise PreservationError("note_identity_has_multiple_paths")
                raw, text = _read_note(path, info, max_bytes)
                raw_hash = digest(raw)
                if any(e["source_id"] in forgotten and e["sha256"] == raw_hash for e in entries.values()):
                    raise PreservationError("forgotten_note_content_requires_review")
                matches = [(k, e) for k, e in entries.items() if e["identity"] == identity]
                if len(matches) > 1:
                    raise PreservationError("ambiguous_note_identity")
                existing = matches[0] if matches else None
                if existing:
                    _, entry = existing
                    if entry["missing"] and identity.endswith(":unknown"):
                        raise PreservationError("reappeared_note_identity_requires_review")
                    if entry["path"] != relative and identity.endswith(":unknown") and entry["sha256"] != raw_hash:
                        raise PreservationError("renamed_changed_note_requires_review")
                else:
                    paths = [(k, e) for k, e in entries.items() if e["path"] == relative]
                    if len(paths) > 1:
                        raise PreservationError("ambiguous_note_path")
                    if paths:
                        _, entry = paths[0]
                        if entry["missing"]:
                            raise PreservationError("reappeared_note_path_requires_review")
                        if not complete:
                            raise PreservationError("incomplete_note_identity_reconciliation")
                        if entry["identity"] not in current_identities:
                            existing = paths[0]  # Editor's atomic replacement at the same path.
                    if not existing and any(
                        e["sha256"] == raw_hash and e["path"] not in seen
                        for e in entries.values()
                    ):
                        raise PreservationError("possible_note_move_requires_review")
                source_key, prior = existing if existing else ("library-file:" + uuid.uuid4().hex, None)
                if source_key in processed_keys:
                    raise PreservationError("note_identity_reused_in_scan")
                title = prior["title"] if prior else path.stem
                if prior is None:
                    # Reserve the source identity before capture. A process exit
                    # after Store.capture cannot create a new UUID on the retry.
                    entries[source_key] = {
                        "identity": identity, "path": relative, "title": title,
                        "sha256": raw_hash, "source_id": "ols-" + digest(canonical([scope, source_key])),
                        "version_id": "", "missing": False,
                    }
                    store.set_setting(registry_key, canonical(registry).decode())
                receipt = store.capture(
                    source_key=source_key, scope=scope, title=title,
                    original=raw, text=text, locator=path.as_uri(), kind="note",
                    metadata={"capture_origin": "selected_library_file", "text_encoding": "utf-8"},
                )
                if prior and prior["version_id"] == receipt.version_id:
                    result["unchanged"] += 1
                else:
                    result["captured"] += 1
                if prior and prior["path"] != relative:
                    result["renamed"] += 1
                entries[source_key] = {
                    "identity": identity, "path": relative, "title": title,
                    "sha256": raw_hash, "source_id": receipt.source_id,
                    "version_id": receipt.version_id, "missing": False,
                }
                processed_keys.add(source_key)
                result["versions"].append({"source_id": receipt.source_id, "version_id": receipt.version_id,
                                            "file_ref": digest(relative.encode())[:24]})
            except (PreservationError, OSError) as exc:
                result["issues"].append(_issue(relative, _safe_code(exc, "note_read_failed")))
        if complete:
            for entry in entries.values():
                if any(Path(entry["path"]).is_relative_to(Path(area)) for area in selected):
                    if entry["path"] not in seen:
                        entry["missing"] = True
                        result["missing"].append(entry["source_id"])
        store.set_setting(registry_key, canonical(registry).decode())
    return result
