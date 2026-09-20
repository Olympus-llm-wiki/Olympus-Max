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
import time
from typing import Callable, Iterator, Sequence
import uuid

from .preservation import Store, PreservationError, canonical, digest, guard_no_secrets, _SECRET_PATTERNS
from .bounded_io import fingerprint, hash_file, read_file, guard_file, scan_guard_file, copy_window, publish_file

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
        info = fingerprint(path)
    except FileNotFoundError:
        return False
    if info["bytes"] != len(expected) or hash_file(path, max_bytes=len(expected)) != digest(expected):
        raise PreservationError("library_existing_bytes_conflict")
    return True


def _publish_files(directory: Path, files: dict[str, bytes], *, strict: bool,
                   allowed_aliases: dict[str, dict] | None = None, fence=None) -> bool:
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
    if strict:
        _check_inventory(directory, set(files), allowed_aliases or {})
    existing = [_same_file(directory / name, data) for name, data in files.items()]
    existed = all(existing)
    if existed:
        return False
    if fence:
        fence()
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
            if fence:
                fence()
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


def _check_inventory(directory: Path, names: set[str], aliases: dict[str, dict]) -> None:
    if directory.is_symlink():
        raise PreservationError("library_directory_symlink")
    extras = set(p.name for p in directory.iterdir()) - names - {".DS_Store"}
    if extras - set(aliases):
        raise PreservationError("library_unexpected_version_files")
    for name in extras:
        pending = aliases[name].get("verification") == "pending"
        observed = fingerprint(directory / name, allow_dataless=pending)
        # Explicit pending aliases are excluded from canonical proof. They remain
        # visible warnings; no unknown extra, symlink or alias byte is trusted.
        if not pending and observed != aliases[name].get("fingerprint"):
            raise PreservationError("library_alias_changed")


def _guard_policy() -> str:
    return digest(canonical(["complete-utf8-ignore-stream-v2", [[p.pattern, p.flags] for p in _SECRET_PATTERNS]]))


def _fingerprints(folder: Path, names: Sequence[str]) -> dict:
    return {name: fingerprint(folder / name) for name in names}


def _existing_fingerprints(folder: Path, names: Sequence[str]) -> dict | None:
    try:
        return _fingerprints(folder, names)
    except FileNotFoundError:
        return None


def _existing_fingerprint(path: Path) -> dict | None:
    try:
        return fingerprint(path)
    except FileNotFoundError:
        return None


def _saved_json(store: Store, key: str) -> dict:
    try:
        value = json.loads(store.setting(key, "{}"))
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


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


def _validated_sources(store: Store, manifest: dict, source_files: dict, *, audit: bool, fence=None) -> None:
    vid = manifest["version_id"]
    semantic = {k: manifest[k] for k in ("source_key", "scope", "kind", "title", "metadata")}
    sid = "ols-" + digest(canonical([manifest["scope"], manifest["source_key"]]))
    expected = "olv-" + digest(canonical([sid, manifest["original_sha256"], manifest["text_sha256"], semantic]))
    if sid != manifest["source_id"] or expected != vid:
        raise PreservationError("source_manifest_mismatch")
    key = "library_source_validation:" + vid
    prior = _saved_json(store, key)
    if not audit and prior.get("source_files") == source_files:
        return
    for name, hash_key in (("original", "original_sha256"), ("text.txt", "text_sha256")):
        if fence:
            fence()
        if hash_file(store.versions / vid / name, timeout=5) != manifest[hash_key]:
            raise PreservationError("source_hash_mismatch")
    if fence:
        fence()
    store.set_setting(key, canonical({"source_files": source_files, "manifest_sha256": digest(canonical(manifest))}).decode())


def _guard_sources(store: Store, manifest: dict, source_files: dict, policy: str, *, fence=None) -> None:
    vid = manifest["version_id"]
    key = "library_guard:" + vid
    prior = _saved_json(store, key)
    if prior.get("policy") == policy and prior.get("source_files") == source_files:
        return
    work_key = "library_guard_progress:" + vid
    work = _saved_json(store, work_key)
    if work.get("policy") != policy or work.get("source_files") != source_files:
        work = {"policy": policy, "source_files": source_files, "files": {}}
    for name in ("original", "text.txt"):
        if work["files"].get(name, {}).get("complete"):
            continue
        source = store.versions / vid / name
        if fence:
            fence()
        if source_files[name]["bytes"] <= 1024**2:
            guard_no_secrets(read_file(source, max_bytes=1024**2))
            work["files"][name] = {"complete": True}
        else:
            work["files"][name] = scan_guard_file(source, checkpoint=work["files"].get(name))
        if fence:
            fence()
        store.set_setting(work_key, canonical(work).decode())
        if not work["files"][name]["complete"]:
            raise PreservationError("library_guard_pending")
    store.set_setting(key, canonical({"policy": policy, "source_files": source_files,
        "manifest_sha256": digest(canonical(manifest)), "original_sha256": manifest["original_sha256"],
        "text_sha256": manifest["text_sha256"]}).decode())
    with store.connect(write=True) as db:
        db.execute("DELETE FROM settings WHERE key=?", (work_key,))


def _copy_source_path(store: Store, root: Path, vid: str, name: str, target: Path,
                      expected_hash: str, source_info: dict, *, audit: bool, fence=None) -> bool:
    namespace = digest(str(root).encode()) + ":" + vid + ":" + name
    key = "library_copy:" + namespace
    completed_key = "library_file:" + namespace
    copy = _saved_json(store, key)
    saved = _saved_json(store, completed_key)
    existing = _existing_fingerprint(target)
    if existing:
        if not (not audit and saved.get("fingerprint") == existing and saved.get("sha256") == expected_hash):
            if existing["bytes"] != source_info["bytes"] or hash_file(target, timeout=5) != expected_hash:
                raise PreservationError("library_existing_bytes_conflict")
        if fence:
            fence()
        store.set_setting(completed_key, canonical({"fingerprint": existing, "sha256": expected_hash}).decode())
        return False
    staging_root = _directory(root, ".olympus-staging")
    if not copy:
        if fence:
            fence()
        folder = Path(tempfile.mkdtemp(prefix="copy-", dir=staging_root))
        copy = {"path": str(folder / name), "source": source_info, "offset": 0, "sha256": expected_hash}
        store.set_setting(key, canonical(copy).decode())
    stage = Path(copy["path"])
    if (stage.parent.parent != staging_root or stage.parent.is_symlink() or stage.name != name
            or copy.get("source") != source_info or copy.get("sha256") != expected_hash):
        raise PreservationError("library_copy_checkpoint_conflict")
    if copy["offset"] < source_info["bytes"]:
        if fence:
            fence()
        copied = copy_window(store.versions / vid / name, stage, offset=copy["offset"])
        if fence:
            fence()
        copy["offset"] = copied["offset"]
        store.set_setting(key, canonical(copy).decode())
        if not copied["complete"]:
            raise PreservationError("library_copy_pending")
    elif not stage.exists():
        # Empty files still require a complete owned staging file.
        stage.touch(mode=0o600, exist_ok=False)
    if fence:
        fence()
    publish_file(stage, target, expected_hash)
    if fence:
        fence()
    store.set_setting(completed_key, canonical({"fingerprint": fingerprint(target), "sha256": expected_hash}).decode())
    stage.unlink(); stage.parent.rmdir()
    with store.connect(write=True) as db:
        db.execute("DELETE FROM settings WHERE key=?", (key,))
    return True


def _publish_version(store: Store, root: Path, folder: Path, manifest: dict, source_files: dict,
                     aliases: dict, *, audit: bool, fence=None) -> bool:
    _validated_sources(store, manifest, source_files, audit=audit, fence=fence)
    _guard_sources(store, manifest, source_files, _guard_policy(), fence=fence)
    metadata = canonical(manifest)
    guard_no_secrets(metadata)
    # Small bundles retain the same no-clobber publication path. Large payloads
    # never become Python bytes in the parent; each copy attempt is at most 32MiB.
    if max(source_files[name]["bytes"] for name in ("original", "text.txt")) <= 1024**2:
        return _publish_files(folder, {"original": read_file(store.versions / manifest["version_id"] / "original", max_bytes=1024**2),
            "text.txt": read_file(store.versions / manifest["version_id"] / "text.txt", max_bytes=1024**2),
            "manifest.json": metadata}, strict=True, allowed_aliases=aliases, fence=fence)
    folder.mkdir(exist_ok=True, mode=0o700)
    _check_inventory(folder, {"original", "text.txt", "manifest.json"}, aliases)
    published = False
    for name, hash_key in (("original", "original_sha256"), ("text.txt", "text_sha256")):
        published = _copy_source_path(store, root, manifest["version_id"], name, folder / name,
            manifest[hash_key], source_files[name], audit=audit, fence=fence) or published
    return _publish_files(folder, {"manifest.json": metadata}, strict=False, fence=fence) or published


def export_versions(store: Store, library_root: Path, *, limit: int = 100,
                    audit: bool = False, time_budget: float = 10,
                    progress: Callable[[dict], None] | None = None) -> dict:
    """Stage at most ``limit`` registered versions and short source cards.

    A persisted cursor rotates through non-forgotten versions, including those
    already staged, so subsequent passes also detect tampering and sync conflicts.
    No existing bytes are overwritten or deleted, including inactive history.
    """
    _limit(limit)
    if not 0 < time_budget <= 300:
        raise PreservationError("invalid_library_time_budget")
    deadline = time.monotonic() + time_budget
    root = _root(library_root, create=True)
    if root == store.root or root in store.root.parents or store.root in root.parents:
        raise PreservationError("library_and_state_must_be_separate")
    key = "library_export_cursor:" + digest(str(root).encode())
    result = {"checked": 0, "exported": 0, "already_present": 0, "errors": [],
              "staged": [], "remote_verified": 0, "cycle_complete": False, "deferred": 0,
              "cached": 0, "bytes_checked": 0, "warnings": [], "pending": [], "hot_resumed": 0}
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
            hot = db.execute("""SELECT v.id AS version_id,v.source_id,s.source_key,
                s.scope,s.kind AS source_kind FROM versions v JOIN sources s ON s.id=v.source_id
                JOIN settings st ON st.key='drive_staged:'||v.id
                WHERE s.forgotten_at IS NULL AND
                CASE WHEN json_valid(st.value) THEN json_extract(st.value,'$.status') END='in_progress'
                ORDER BY coalesce(json_extract(st.value,'$.last_attempt_at'),0),v.id LIMIT 1""").fetchone()
        # One resume slot shares the existing item/time budget. The normal cursor
        # only advances for normal inventory rows, so hot work cannot skip sources.
        normal = rows[:limit - bool(hot)]
        work_rows = ([{**dict(hot), "_hot": True}] if hot else []) + [r for r in normal if not hot or r["version_id"] != hot["version_id"]]
        fresh_ids = {r["version_id"] for r in rows}
        visited = set()
        result["cycle_complete"] = not rows
        for row in work_rows:
            if result["checked"] and time.monotonic() >= deadline:
                break
            vid = row["version_id"]
            result["hot_resumed"] += bool(row.get("_hot"))
            result["checked"] += 1
            prior = _saved_json(store, "drive_staged:" + vid)
            def fence():
                if progress:
                    progress({"version_id": vid, "checked": result["checked"], "deferred": result["deferred"]})
            try:
                fence()
                if not audit and prior.get("next_attempt_at", 0) > time.time():
                    result["deferred"] += 1
                    continue
                source_folder = store.versions / vid
                captured_manifest = store.read_manifest(vid)
                if (captured_manifest["source_id"] != row["source_id"]
                        or any(captured_manifest[k] != row[r] for k, r in
                               (("source_key", "source_key"), ("scope", "scope"), ("kind", "source_kind")))):
                    raise PreservationError("library_source_id_mismatch")
                source_files = _fingerprints(source_folder, ("original", "text.txt", "manifest.json", "manifest.sha256"))
                version_dir = _directory(corpus, row["source_id"]) / vid
                policy = _guard_policy()
                aliases = _saved_json(store, "library_names:" + digest(str(root).encode()) + ":" + vid).get("aliases", {})
                pending_aliases = [name for name, proof in aliases.items() if proof.get("verification") == "pending"]
                if pending_aliases:
                    result["warnings"].append({"version_id": vid, "code": "library_alias_verification_pending", "aliases": pending_aliases})
                card_path = cards / (row["source_id"] + ".md")
                if version_dir.exists():
                    _check_inventory(version_dir, {"original", "text.txt", "manifest.json"}, aliases)
                if (not audit and prior.get("status") == "local_staged"
                        and prior.get("path") == str(version_dir) and prior.get("guard_policy") == policy
                        and prior.get("source_files") == source_files
                        and prior.get("files") == _existing_fingerprints(version_dir, ("original", "text.txt", "manifest.json"))
                        and prior.get("card") == _existing_fingerprint(card_path)):
                    result["already_present"] += 1
                    result["cached"] += 1
                    continue
                manifest = canonical(captured_manifest)
                card = _card(row)
                guard_no_secrets(card)
                published = _publish_version(store, root, version_dir, captured_manifest, source_files, aliases, audit=audit, fence=fence)
                _publish_files(cards, {captured_manifest["source_id"] + ".md": card}, strict=False, fence=fence)
                fence()
                staged = {"path": str(version_dir), "manifest_sha256": digest(manifest),
                          "status": "local_staged", "remote_copy": "unconfirmed", "guard_policy": policy,
                          "source_files": source_files,
                          "files": _fingerprints(version_dir, ("original", "text.txt", "manifest.json")),
                          "card": fingerprint(card_path), "checked_at": time.time()}
                store.set_setting("drive_staged:" + vid, canonical(staged).decode())
                result["exported" if published else "already_present"] += 1
                result["bytes_checked"] += source_files["original"]["bytes"] + source_files["text.txt"]["bytes"]
                result["staged"].append({"source_id": captured_manifest["source_id"], "version_id": vid,
                                         "path": str(version_dir), "manifest_sha256": digest(manifest)})
            except (PreservationError, OSError, ValueError) as exc:
                if isinstance(exc, PreservationError) and str(exc) == "stage_lease_lost":
                    raise
                fence()
                code = _safe_code(exc, "library_export_io_error")
                ongoing = code in {"library_guard_pending", "library_copy_pending"}
                result["pending" if ongoing else "errors"].append({"version_id": vid, "code": code})
                deferred = code in {"materialization_pending", "file_read_timeout", "file_read_io_error"}
                if deferred:
                    result["deferred"] += 1
                if ongoing:
                    result["deferred"] += 1
                store.set_setting("drive_staged:" + vid, canonical({
                    "status": "materialization_pending" if deferred else "in_progress" if ongoing else "error", "code": code,
                    "remote_copy": "unconfirmed", "errno": getattr(exc, "errno", None),
                    "last_attempt_at": time.time(),
                    "next_attempt_at": time.time() + 60 if deferred else 0,
                }).decode())
            finally:
                fence()
                visited.add(vid)
                result["cycle_complete"] = fresh_ids <= visited
                if result["cycle_complete"]:
                    store.set_setting(key, "")
                elif not row.get("_hot"):
                    store.set_setting(key, vid)
                if progress:
                    progress({k: result[k] for k in ("checked", "exported", "cached", "deferred", "bytes_checked")})
    return result


def repair_original_names(store: Store, library_root: Path, version_ids: Sequence[str], *,
                          preserve_unverified_aliases: bool = False) -> dict:
    """Restore canonical names after exact verification, preserving every alias.

    Only the observed PDF aliases are supported. Unknown extras or different
    bytes stop this version before publication; no source or remote copy is
    deleted. The versioned receipt authorizes only these exact local aliases.
    """
    root = _root(library_root, create=False)
    repaired, errors, pending = [], [], []
    with _lock(store, "export:" + str(root)):
        for vid in version_ids:
            try:
                manifest = store.read_manifest(vid)
                with store.connect() as db:
                    row = db.execute("SELECT s.forgotten_at FROM versions v JOIN sources s ON s.id=v.source_id WHERE v.id=?", (vid,)).fetchone()
                if row is None or row["forgotten_at"]:
                    raise PreservationError("library_source_not_exportable")
                folder = root / "Corpus" / manifest["source_id"] / vid
                if folder.is_symlink() or folder.parent.is_symlink():
                    raise PreservationError("library_directory_symlink")
                names = {p.name for p in folder.iterdir()}
                extras = names - {"original", "text.txt", "manifest.json", ".DS_Store"}
                if not extras or extras - {"original.pdf", "original (1).pdf"}:
                    raise PreservationError("library_names_require_review")
                expected = manifest["original_sha256"]
                if hash_file(store.versions / vid / "original", timeout=60) != expected:
                    raise PreservationError("source_hash_mismatch")
                aliases = {}
                for name in sorted(extras):
                    try:
                        if hash_file(folder / name, timeout=5) != expected:
                            raise PreservationError("library_alias_bytes_conflict")
                        aliases[name] = {"verification": "verified", "sha256": expected}
                    except PreservationError as exc:
                        if not preserve_unverified_aliases or str(exc) not in {"materialization_pending", "file_read_timeout", "file_read_io_error"}:
                            raise
                        aliases[name] = {"verification": "pending", "code": str(exc)}
                original = folder / "original"
                if original.exists():
                    if hash_file(original, timeout=5) != expected:
                        raise PreservationError("library_existing_bytes_conflict")
                else:
                    verified_alias = next((name for name in sorted(extras) if aliases[name]["verification"] == "verified"), None)
                    if verified_alias:
                        os.link(folder / verified_alias, original, follow_symlinks=False)
                    else:
                        data = (store.versions / vid / "original").read_bytes()
                        if digest(data) != expected:
                            raise PreservationError("source_hash_mismatch")
                        guard_no_secrets(data)
                        _publish_files(folder, {"original": data}, strict=False)
                    _sync_directory(folder)
                for name in aliases:
                    aliases[name]["fingerprint"] = fingerprint(folder / name, allow_dataless=aliases[name]["verification"] == "pending")
                unresolved = [name for name in aliases if aliases[name]["verification"] == "pending"]
                if unresolved:
                    pending.append({"version_id": vid, "aliases": unresolved})
                receipt = {"schema": 2, "version_id": vid, "manifest_sha256": digest(canonical(manifest)),
                           "canonical": {"original": {"sha256": expected, "fingerprint": fingerprint(original)}},
                           "aliases": aliases, "status": "canonical_name_restored", "checked_at": time.time()}
                store.set_setting("library_names:" + digest(str(root).encode()) + ":" + vid, canonical(receipt).decode())
                repaired.append(vid)
            except (PreservationError, OSError) as exc:
                errors.append({"version_id": vid, "code": _safe_code(exc, "library_names_io_error"),
                               "errno": getattr(exc, "errno", None)})
    return {"repaired": repaired, "errors": errors, "aliases_pending": pending, "deleted": 0}


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
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or _file_identity(before) != _file_identity(expected):
        raise PreservationError("note_changed_during_scan")
    raw = read_file(path, max_bytes=max_bytes, timeout=2)
    after = path.stat(follow_symlinks=False)
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
               scope: str, limit: int = 100, max_bytes: int = 8 * 1024 * 1024,
               time_budget: float = 5, progress=None) -> dict:
    """Capture text versions only from explicitly selected approved subtrees.

    File identities and atomic replacement at the same full path preserve a
    source. A copied file, cross-volume move, missing/reappeared path, or ambiguous
    conflict is never silently treated as an owner-approved supersession.
    """
    _limit(limit)
    if not 0 < time_budget <= 300:
        raise PreservationError("invalid_library_time_budget")
    deadline = time.monotonic() + time_budget
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
        for candidate_index, (relative, info) in enumerate(candidates):
            if candidate_index and time.monotonic() >= deadline:
                complete = False
                result["complete_scan"] = False
                result["issues"].append({"code": "note_time_budget_reached"})
                break
            if progress:
                progress({"checked": candidate_index, "captured": result["captured"]})
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
                if progress:
                    progress({"checked": candidate_index, "captured": result["captured"], "state": "before_capture"})
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
                if isinstance(exc, PreservationError) and str(exc) == "stage_lease_lost":
                    raise
                result["issues"].append(_issue(relative, _safe_code(exc, "note_read_failed")))
        if complete:
            for entry in entries.values():
                if any(Path(entry["path"]).is_relative_to(Path(area)) for area in selected):
                    if entry["path"] not in seen:
                        entry["missing"] = True
                        result["missing"].append(entry["source_id"])
        if progress:
            progress({"captured": result["captured"], "state": "before_registry_commit"})
        store.set_setting(registry_key, canonical(registry).decode())
    return result
