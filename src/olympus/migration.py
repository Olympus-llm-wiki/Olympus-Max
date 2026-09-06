"""Frozen, resumable import of explicitly selected legacy packages.

No source code is executed. Personal manifests and reports belong in Store state,
not in the repository. Machine checks and assistant assessments remain distinct.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import urllib.parse

from .materials import material_profile, source_provenance
from .preservation import Store, PreservationError, canonical, digest, guard_no_secrets, redact_secrets, timestamp


MAX_FILE_BYTES = 512 * 1024**2
TEXT_SUFFIXES = {".md", ".txt", ".json", ".html", ".htm", ".py", ".csv", ".tsv", ".yaml", ".yml"}


def read_regular(path: Path, *, expected: str | None = None) -> bytes:
    if path.name in {"auth.json", "credentials.json"} or path.name.startswith(".env") or path.suffix in {".key", ".pem"}:
        raise PreservationError("credential_file_not_a_source")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
                raise PreservationError("migration_source_not_regular_or_too_large")
            raw = stream.read(MAX_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or len(raw) != before.st_size):
            raise PreservationError("migration_source_changed_while_reading")
    except OSError:
        raise PreservationError("migration_source_unavailable") from None
    if expected is not None and digest(raw) != expected:
        raise PreservationError("migration_source_changed_since_plan")
    return raw


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = canonical(value)
    guard_no_secrets(data)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def file_key(path: Path) -> str:
    return "legacy-file:" + digest(str(path.absolute()).encode())


def under(root: Path, value: str) -> Path:
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise PreservationError("migration_path_outside_package")
    path = root.joinpath(*relative.parts)
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise PreservationError("migration_path_outside_package")
    return path


def _sanitized(raw: bytes, path: Path) -> tuple[bytes, bool]:
    try:
        guard_no_secrets(raw)
        return raw, False
    except PreservationError:
        if path.suffix.lower() not in TEXT_SUFFIXES:
            raise
        try:
            safe = redact_secrets(raw.decode("utf-8")).encode("utf-8")
        except UnicodeError:
            raise PreservationError("credential_pattern_detected") from None
        guard_no_secrets(safe)
        return safe, True


def _role(path: Path, package: str, root: Path | None) -> str:
    if package == "I001":
        if path.suffix.lower() != ".md":
            return "artifact"
        if path.name.lower() == "readme.md" or "контекст" in path.name.casefold():
            return "discussion"
        return "synthesis"
    relative = path.relative_to(root).as_posix() if root and path.is_relative_to(root) else ""
    if relative.startswith("derived/html/"):
        return "extracted"
    if (relative.startswith("derived/transcripts/") and path.suffix == ".txt" and "-timestamped" not in path.stem):
        return "extracted"
    if relative.startswith("supplement-2026-09-05/transcripts/") and path.suffix == ".txt":
        return "extracted"
    if path.suffix == ".md" and path.name not in {"SOURCE-INVENTORY.md"}:
        return "synthesis"
    if path.name in {"manifest.json", "verification.json", "SOURCE-INVENTORY.md", "index.json"}:
        return "catalog"
    return "artifact"


def build_plan(inventory_path: Path, small_zero_root: Path, preview: Store,
               *, packages: tuple[str, ...] = ("I001", "I002")) -> dict:
    if not packages or set(packages) - {"I001", "I002"}:
        raise PreservationError("unsupported_migration_selection")
    inventory_raw = read_regular(inventory_path)
    inventory = json.loads(inventory_raw)
    rows = inventory.get("rows")
    members = inventory.get("members")
    if (not isinstance(rows, list) or not isinstance(members, list) or
            len({r["id"] for r in rows}) != len(rows)):
        raise PreservationError("invalid_legacy_catalog")
    row_map = {row["id"]: row for row in rows}
    if not set(packages) <= row_map.keys():
        raise PreservationError("missing_selected_package")
    root = small_zero_root.absolute()
    if "I002" in packages and str(root) not in row_map["I002"]["locations"]:
        raise PreservationError("unregistered_small_zero_root")
    candidates: dict[Path, dict] = {}
    excluded = []

    def add(path: Path, package: str, **details):
        path = path.absolute()
        record = candidates.setdefault(path, {"packages": [], "parents": [], "details": {}})
        if package not in record["packages"]:
            record["packages"].append(package)
        record["details"].update(details)

    if "I001" in packages:
        for member in members:
            if member.get("id") != "I001":
                continue
            path = Path(member["path"])
            if path.is_dir():
                excluded.append({"path": str(path), "reason": "catalog_location_not_recursive_input"})
            else:
                add(path, "I001")
    if "I002" in packages:
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise PreservationError("migration_source_symlink")
            if path.is_file():
                if path.name == ".DS_Store":
                    excluded.append({"path": str(path), "reason": "operating_system_metadata"})
                else:
                    add(path, "I002")
        main = json.loads(read_regular(root / "manifest.json"))
        supplement = json.loads(read_regular(root / "supplement-2026-09-05/manifest.json"))
        source_to_archive = {}
        for item in main["source_items"]:
            path = under(root, item["archived_path"])
            source_to_archive[item["source_path"]] = path
            add(path, "I002", legacy_expected_sha256=item["archived_sha256"],
                legacy_source_sha256=item["source_sha256"], legacy_original_path=item["source_path"],
                legacy_redactions=item.get("redactions", 0))
        for page in main["html_pages"]:
            path = under(root, page["extracted_path"])
            add(path, "I002", extraction_method="legacy_html_text", coverage="legacy_extraction_not_revalidated")
            candidates[path]["parents"].append(under(root, page["archived_path"]))
        for transcript in main["video_transcripts"]:
            path = under(root, transcript["text"])
            add(path, "I002", legacy_expected_sha256=transcript["text_sha256"],
                extraction_method=transcript["model"], coverage="legacy_asr_not_revalidated")
            candidates[path]["parents"].append(under(root, transcript["video"]))
        for video in supplement["videos"]:
            original = Path(video["original_path"]).absolute()
            if (original.suffix.lower() != ".mp4"
                    or original.parent.resolve() != (Path.home() / "Downloads").resolve()
                    or not re.fullmatch(r"[a-f0-9]{64}", video.get("sha256_original", ""))):
                raise PreservationError("unapproved_supplement_original")
            add(original, "I002", legacy_expected_sha256=video["sha256_original"],
                legacy_source_sha256=video["sha256_original"])
            transcript = Path(video["transcript_path"]).absolute()
            if not transcript.is_relative_to(root):
                raise PreservationError("migration_path_outside_package")
            add(transcript, "I002", legacy_expected_sha256=video["sha256_transcript"],
                extraction_method=supplement["method"], coverage="legacy_end_check_passed_not_new_audio_verification")
            candidates[transcript]["parents"].append(original)
            audio = Path(video["audio_path"]).absolute()
            if not audio.is_relative_to(root):
                raise PreservationError("migration_path_outside_package")
            add(audio, "I002", legacy_expected_sha256=video["sha256_audio"])
            candidates[audio]["parents"].append(original)
        index = json.loads(read_regular(under(root, main["normalized_prompt_library"]["index_path"])))
        for item in index:
            path = under(root, item["normalized_path"])
            add(path, "I002", legacy_expected_sha256=item["normalized_sha256"],
                transformation=item["repair_method"])
            candidates[path]["parents"].append(under(root, item["source_path"]))
        for archive in main["zip_analysis"]:
            if archive.get("duplicate"):
                continue
            parent = source_to_archive[archive["source_path"]]
            extracted_root = under(root, archive["extracted_path"])
            for path in candidates:
                if path.is_relative_to(extracted_root):
                    candidates[path]["parents"].append(parent)
                    candidates[path]["details"]["archive_member"] = str(path.relative_to(extracted_root))
        for artifact in main["analysis_artifacts"]:
            add(under(root, artifact["path"]), "I002", legacy_expected_sha256=artifact["sha256"])
        cross = Path(main["cross_archive_synthesis"]).absolute()
        if cross.parent != root.parent or cross.suffix != ".md":
            raise PreservationError("unapproved_related_synthesis")
        add(cross, "I002", covers_multiple_archives=True, coverage="mixed_historical_synthesis")
    if len(candidates) > 10_000:
        raise PreservationError("migration_candidate_limit")

    items, issues = [], []
    for path, record in sorted(candidates.items(), key=lambda pair: str(pair[0])):
        role = _role(path, record["packages"][0], root)
        try:
            raw = read_regular(path)
            safe, redacted = _sanitized(raw, path)
            metadata = {"material_role": role, "legacy_locator": path.as_uri(),
                        "legacy_input_sha256": digest(raw), "migration_generation": "1",
                        "event_at": "unset", "source_date_status": "historical_not_current_ingestion_date",
                        "coverage": str(record["details"].get("coverage", "not_independently_verified"))}
            metadata.update({k: str(v) for k, v in record["details"].items()})
            if redacted:
                metadata["original_is_transformed"] = "true"
                metadata["transformation"] = "known_credentials_redacted_on_import"
            expected = record["details"].get("legacy_expected_sha256")
            if expected and digest(raw) != expected:
                issues.append({"path": str(path), "packages": record["packages"], "code": "legacy_hash_differs_from_current_file"})
                metadata["legacy_hash_verification"] = "mismatch_current_variant"
                if role in {"artifact", "extracted", "primary"}:
                    metadata["delivery_mode"] = "archive_only"
            elif expected:
                metadata["legacy_hash_verification"] = "matched"
            text = safe.decode("utf-8") if role in {"primary", "extracted", "synthesis", "discussion", "assessment", "decision"} else ""
            if not text.strip():
                metadata["delivery_mode"] = "archive_only"
                metadata["text_availability"] = "absent"
            title = path.stem
            key = file_key(path)
            if path.suffix == ".zip" and record["details"].get("legacy_source_sha256"):
                key = "legacy-archive:" + digest(safe)
                title = "Small Zero source archive " + digest(safe)[:12]
                # Aliases are locations/views, not new versions of identical bytes.
                metadata.pop("legacy_locator", None)
                metadata.pop("legacy_original_path", None)
            receipt = preview.capture(source_key=key, scope="legacy-preview", title=title,
                                      original=safe, text=text, locator=path.as_uri(), kind=role,
                                      metadata=metadata, archive_only=True)
            items.append({"item_id": "item-" + digest(str(path).encode())[:20], "source_key": key,
                          "path": str(path), "packages": sorted(record["packages"]), "title": title,
                          "role": role, "input_sha256": digest(raw), "captured_sha256": digest(safe),
                          "bytes": len(raw), "text_chars": len(text), "redacted": redacted,
                          "preview_version_id": receipt.version_id, "metadata": metadata,
                          "parent_paths": [str(p) for p in record["parents"]]})
        except (PreservationError, UnicodeError) as exc:
            code = str(exc) if isinstance(exc, PreservationError) else "text_not_utf8"
            issues.append({"path": str(path), "packages": record["packages"], "code": code, "blocking": True})
    by_path = {name: item for item in items for name in (item["path"], str(Path(item["path"]).resolve()))}
    by_stem = {}
    for item in items:
        by_stem.setdefault(Path(item["path"]).stem, []).append(item)
    # Preserve raw/normalized links and Markdown links without rewriting originals.
    for item in items:
        parents, unresolved = [], []
        explicit_parents = set(item["parent_paths"])
        external = []
        if item["role"] in {"synthesis", "discussion", "extracted"}:
            text = preview.read_version(item["preview_version_id"])["text"]
            if text.startswith("---\n") and "\n---" in text[4:]:
                front = text.split("---", 2)[1]
                fields = dict(re.findall(r"(?m)^(source|created|updated|verified|confidence|contested|status|model):\s*([^\n]+)", front))
                item["metadata"]["legacy_frontmatter"] = json.dumps(fields, ensure_ascii=False, sort_keys=True)
                if fields.get("source", "").startswith(("http://", "https://")):
                    external.append(fields["source"])
            for target in re.findall(r"\[\[([^\]\n]+)\]\]", text):
                name = target.split("|", 1)[0].split("#", 1)[0]
                matches = by_stem.get(Path(name).stem, [])
                if len(matches) == 1:
                    item["parent_paths"].append(matches[0]["path"])
                else:
                    unresolved.append("wiki:" + name)
            for bracketed, plain in re.findall(r"\[[^\]]*\]\((?:<([^>]+)>|([^\n)]+))\)", text):
                target = (bracketed or plain).strip()
                if target.startswith(("https://", "http://")):
                    external.append(target)
                    continue
                if not target or target.startswith("#"):
                    continue
                target = urllib.parse.unquote(target.split("#", 1)[0])
                if target.startswith("file://"):
                    target = urllib.parse.urlsplit(target).path
                local = Path(target)
                if not local.is_absolute():
                    local = Path(item["path"]).parent / local
                item["parent_paths"].append(str(local.resolve()))
        for name in item["parent_paths"]:
            parent = by_path.get(name)
            if parent:
                if parent["source_key"] != item["source_key"]:
                    link = {"source_key": parent["source_key"], "original_sha256": parent["captured_sha256"],
                            "relation": "derived_from" if name in explicit_parents else "references"}
                    if link not in parents:
                        parents.append(link)
            else:
                unresolved.append(name)
        for parent in parents:
            candidate = next(v for v in items if v["source_key"] == parent["source_key"])
            if candidate["metadata"].get("legacy_hash_verification") == "mismatch_current_variant" and item["role"] == "extracted":
                item["metadata"]["delivery_mode"] = "archive_only"
                unresolved.append("declared_parent_hash_mismatch")
        item["metadata"]["parent_sources"] = json.dumps(parents, ensure_ascii=False, sort_keys=True)
        item["metadata"]["provenance_gaps"] = json.dumps(sorted(set(unresolved)), ensure_ascii=False)
        item["metadata"]["external_references"] = json.dumps(sorted(set(external)), ensure_ascii=False)
    groups = {}
    for item in items:
        groups.setdefault(item["captured_sha256"], []).append(item["item_id"])
    plan = {"schema": 1, "created_at": timestamp(), "inventory_path": str(inventory_path.absolute()),
            "inventory_sha256": digest(inventory_raw), "catalog_count": len(rows),
            "packages": list(packages), "preview_state": str(preview.root), "scope": "legacy",
            "items": items, "issues": issues, "excluded_locations": excluded,
            "intended_files": {package: [str(path) for path, record in candidates.items() if package in record["packages"]] for package in packages},
            "duplicate_byte_groups": [values for values in groups.values() if len(values) > 1]}
    plan["plan_id"] = "migration-" + digest(canonical({k: v for k, v in plan.items() if k != "created_at"}))[:24]
    return plan


def register_catalog(store: Store, inventory_path: Path, *, expected_sha256: str) -> dict:
    raw = read_regular(inventory_path, expected=expected_sha256)
    catalog = json.loads(raw)
    receipt = store.capture(source_key="legacy-catalog:olympus", scope="legacy-catalog",
                            kind="catalog", title="Каталог прежнего корпуса Olympus", original=raw, text="",
                            metadata={"material_role": "catalog", "coverage": "inventory_only_not_source_completeness"},
                            locator=inventory_path.as_uri(), archive_only=True)
    store.set_setting("legacy_catalog", json.dumps({"version_id": receipt.version_id, "cards": len(catalog["rows"])}))
    return asdict(receipt)


def save_plan(store: Store, plan: dict) -> Path:
    expected = "migration-" + digest(canonical({k: v for k, v in plan.items() if k not in {"created_at", "plan_id"}}))[:24]
    if plan.get("plan_id") != expected:
        raise PreservationError("migration_plan_hash_mismatch")
    receipt = store.capture(source_key="migration-plan:" + expected, scope="legacy-catalog", kind="catalog",
                            title="План переноса " + expected, original=canonical(plan), text="", archive_only=True,
                            metadata={"material_role": "catalog"})
    store.set_setting("migration_plan:" + expected, json.dumps({"version_id": receipt.version_id, "sha256": digest(canonical(plan))}))
    path = store.root / "imports" / expected / "plan.json"
    write_json(path, plan)
    return path


def validate_plan(store: Store, plan: dict) -> None:
    if (not isinstance(plan, dict) or plan.get("schema") != 1 or plan.get("scope") != "legacy"
            or not isinstance(plan.get("plan_id"), str) or not re.fullmatch(r"migration-[a-f0-9]{24}", plan["plan_id"])):
        raise PreservationError("unsupported_migration_plan")
    registered = json.loads(store.setting("migration_plan:" + plan.get("plan_id", ""), "{}"))
    if registered.get("sha256") != digest(canonical(plan)):
        raise PreservationError("migration_plan_not_registered_or_changed")
    archived_plan = store.read_version(registered["version_id"])
    if archived_plan["original"] != canonical(plan):
        raise PreservationError("migration_plan_archive_mismatch")


def apply_plan(store: Store, plan: dict, *, package_id: str | None = None) -> dict:
    validate_plan(store, plan)
    selected = [package_id] if package_id else plan["packages"]
    if set(selected) - set(plan["packages"]):
        raise PreservationError("package_not_in_plan")
    preview = Store(plan["preview_state"])
    catalog = register_catalog(store, Path(plan["inventory_path"]), expected_sha256=plan["inventory_sha256"])
    results = []
    for item in plan["items"]:
        if not set(item["packages"]) & set(selected):
            continue
        try:
            read_regular(Path(item["path"]), expected=item["input_sha256"])
            saved = preview.read_version(item["preview_version_id"])
            if digest(saved["original"]) != item["captured_sha256"]:
                raise PreservationError("preview_source_mismatch")
            profile = material_profile({"kind": item["role"], "metadata": item["metadata"]})
            receipt = store.capture(source_key=item["source_key"], scope="legacy", kind=item["role"],
                                    title=item["title"], original=saved["original"], text=saved["text"],
                                    locator=Path(item["path"]).as_uri(), metadata=item["metadata"],
                                    archive_only=not profile["indexable"])
            result = {"item_id": item["item_id"], "packages": item["packages"], "role": item["role"],
                      "receipt": asdict(receipt), "local_verified": True}
        except PreservationError as exc:
            result = {"item_id": item["item_id"], "packages": item["packages"], "role": item["role"],
                      "error": str(exc), "local_verified": False}
        results.append(result)
        with store.exclusive(), store.connect(write=True) as db:
            for package in set(item["packages"]) & set(selected):
                key = "legacy_package:" + package
                previous = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
                view = json.loads(previous[0]) if previous else {"package_id": package, "scope": "legacy", "version_ids": [], "items": {}}
                view["items"][item["item_id"]] = result
                view["version_ids"] = sorted({r["receipt"]["version_id"] for r in view["items"].values() if r.get("local_verified")})
                view["plan_id"] = plan["plan_id"]
                db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, canonical(view).decode()))
    run = {"schema": 1, "checked_at": timestamp(), "plan_id": plan["plan_id"], "packages": selected,
           "catalog": catalog, "results": results, "planning_issues": plan["issues"]}
    store.set_setting("migration_run:" + plan["plan_id"], json.dumps(run))
    return run


def package_quality(store: Store, plan: dict, package_id: str) -> dict:
    validate_plan(store, plan)
    if package_id not in plan["packages"]:
        raise PreservationError("package_not_in_plan")
    view = json.loads(store.setting("legacy_package:" + package_id, '{"items":{},"version_ids":[]}'))
    probe = json.loads(store.setting("migration_probe:" + plan["plan_id"] + ":" + package_id, "{}"))
    probe_valid = (probe.get("passed") is True and bool(probe.get("document_ids"))
                   and set(probe["document_ids"]) <= set(view["version_ids"]))
    items = [item for item in plan["items"] if package_id in item["packages"]]
    intended = plan.get("intended_files", {}).get(package_id, [item["path"] for item in items])
    missing = set(intended) - {item["path"] for item in items}
    counts = {"planned_files": len(intended), "local_verified": 0, "searchable": 0, "archived": 0,
              "in_progress": 0, "remote_verified": 0, "errors": 0, "text_files": 0,
              "input_bytes": sum(item["bytes"] for item in items), "unique_versions": len(view["version_ids"]),
              "legacy_hash_mismatches": sum(item["metadata"].get("legacy_hash_verification") == "mismatch_current_variant" for item in items),
              "redacted_on_import": sum(item["redacted"] for item in items),
              "unresolved_provenance_links": sum(len(json.loads(item["metadata"].get("provenance_gaps", "[]"))) for item in items)}
    records = [{"original_path": name, "error": "planning_source_unavailable"} for name in sorted(missing)]
    counts["errors"] = len(missing)
    for item in items:
        record = view["items"].get(item["item_id"])
        entry = {"item_id": item["item_id"], "title": item["title"], "role": item["role"],
                 "original_path": item["path"], "metadata": item["metadata"]}
        counts["text_files"] += item["text_chars"] > 0
        if not record or not record.get("local_verified"):
            entry["error"] = record.get("error", "not_imported") if record else "not_imported"
            counts["errors"] += 1
        else:
            vid = record["receipt"]["version_id"]
            version = store.read_version(vid)
            receipt = store.receipt(vid)
            counts["local_verified"] += version["original_sha256"] == item["captured_sha256"]
            counts["searchable"] += receipt.memory == "searchable"
            counts["archived"] += receipt.memory == "archived"
            counts["in_progress"] += receipt.memory in {"pending", "submitted"}
            counts["errors"] += receipt.memory in {"failed", "blocked", "empty"}
            counts["remote_verified"] += receipt.remote_copy == "verified"
            entry.update(receipt=asdict(receipt), source=source_provenance(version),
                         local_original=str(store.versions / vid / "original"),
                         local_text=str(store.versions / vid / "text.txt"))
        records.append(entry)
    return {"schema": 1, "checked_at": timestamp(), "package_id": package_id, "plan_id": plan["plan_id"],
            "counts": counts, "records": records,
            "checks": {"complete_local_copy": counts["local_verified"] == counts["planned_files"] and counts["errors"] == 0,
                       "all_delivery_terminal": counts["in_progress"] == 0 and counts["errors"] == 0,
                       "all_remote_verified": counts["remote_verified"] == counts["planned_files"],
                       "legacy_declared_hashes_match": counts["legacy_hash_mismatches"] == 0,
                       "input_variants_copied_exactly": counts["redacted_on_import"] == 0 and counts["errors"] == 0,
                       "retrieval_probe": probe_valid,
                       "subscription_completeness": "not_verified", "source_claim_truth": "not_automatically_verified"}}


def record_assessment(store: Store, plan: dict, package_id: str, *, observations: list[dict],
                      improvements: list[dict]) -> dict:
    """Persist visible conclusions/evidence, never hidden reasoning or new rules."""
    if package_id not in plan["packages"] or not observations:
        raise PreservationError("invalid_migration_assessment")
    quality = package_quality(store, plan, package_id)
    evidence_ids = {"item:" + item["item_id"] for item in plan["items"] if package_id in item["packages"]}
    evidence_ids |= {"check:" + name for name in quality["checks"]}
    for entry in observations + improvements:
        if (not isinstance(entry, dict) or not isinstance(entry.get("statement"), str)
                or not entry["statement"].strip() or not isinstance(entry.get("evidence"), list)
                or not entry["evidence"] or any(not isinstance(v, str) or v not in evidence_ids for v in entry["evidence"])):
            raise PreservationError("assessment_evidence_required")
    record = {"schema": 1, "created_at": timestamp(), "author": "assistant", "status": "assessment_not_owner_decision",
              "package_id": package_id, "plan_id": plan["plan_id"], "observations": observations,
              "proposed_improvements": improvements, "automatic_policy_changes": False,
              "observed_counts": quality["counts"], "observed_checks": quality["checks"]}
    guard_no_secrets(canonical(record))
    key = "migration_assessment:" + plan["plan_id"] + ":" + package_id
    history = json.loads(store.setting(key, "[]"))
    if not history or {k: v for k, v in history[-1].items() if k != "created_at"} != {k: v for k, v in record.items() if k != "created_at"}:
        history.append(record)
        store.set_setting(key, json.dumps(history, ensure_ascii=False))
    return history[-1]


def render_package_report(store: Store, plan: dict, package_id: str) -> dict:
    """Create a readable milestone and archive it without feeding an LLM loop."""
    quality = package_quality(store, plan, package_id)
    history = json.loads(store.setting("migration_assessment:" + plan["plan_id"] + ":" + package_id, "[]"))
    latest = history[-1] if history else None
    folder = store.root / "imports" / plan["plan_id"]
    counts = quality["counts"]
    lines = [f"# {package_id}: перенос и качество", "", f"Проверено: {quality['checked_at']}", "",
             f"Сохранено {counts['local_verified']} из {counts['planned_files']} принятых файлов; "
             f"уникальных версий {counts['unique_versions']}. Поиск: {counts['searchable']}; "
             f"архив: {counts['archived']}; обрабатывается: {counts['in_progress']}.", "",
             f"Удалённо подтверждено {counts['remote_verified']} файловых записей. "
             "Сохранение, извлечение, качество содержания и восстановление проверяются отдельно.", "",
             "## Результаты проверок", "", "| Проверка | Результат |", "|---|---|",
             f"| Принятые варианты сохранены и проверены | {counts['local_verified']}/{counts['planned_files']} |",
             f"| Расхождения со старыми hashes | {counts['legacy_hash_mismatches']} |",
             f"| Дополнительное очищение при импорте | {counts['redacted_on_import']} |",
             f"| Не разрешённые локальные ссылки | {counts['unresolved_provenance_links']} |",
             f"| Ошибки текущей доставки | {counts['errors']} |", "",
             "Проверка байтов не подтверждает истинность утверждений источника. Полнота подписки не проверялась.", ""]
    if latest:
        lines += ["## Оценка ассистента", "", f"Дата оценки: {latest['created_at']}. Это оценка, не решение владельца.", ""]
        evidence_items = {"item:" + r["item_id"]: r for r in quality["records"] if "item_id" in r}
        def evidence_link(reference):
            item = evidence_items.get(reference)
            target = item.get("local_original") if item else str(folder / ("quality-" + package_id + ".json"))
            return f"[{reference}](<{target}>)" if target else reference
        for entry in latest["observations"]:
            lines.append("- " + entry["statement"] + " Основания: " + ", ".join(evidence_link(e) for e in entry["evidence"]) + ".")
        lines += ["", "## Предложения улучшений", ""]
        for entry in latest["proposed_improvements"]:
            lines.append("- " + entry["statement"] + " Основания: " + ", ".join(evidence_link(e) for e in entry["evidence"]) + ".")
        lines += ["", "Эти предложения не изменяют правила и настройки автоматически.", ""]
    else:
        lines += ["Оценка ассистента ещё не записана.", ""]
    lines += ["## Материалы", "", "| Материал | Роль | Стадия | Сохранённый вариант |", "|---|---|---|---|"]
    for row in quality["records"]:
        title = row.get("title", Path(row.get("original_path", "unknown")).name).replace("|", "\\|").replace("\n", " ")
        receipt = row.get("receipt", {})
        target = row.get("local_original")
        link = f"[original](<{target}>)" if target else "не сохранён"
        lines.append(f"| {title} | {row.get('role', 'unknown')} | {receipt.get('memory', row.get('error', 'unknown'))} | {link} |")
    lines += ["", f"[Полные проверки и происхождение](<{folder / ('quality-' + package_id + '.json')}>)", ""]
    raw = ("\n".join(lines)).encode()
    guard_no_secrets(raw)
    report_path = folder / ("review-" + package_id + ".md")
    fd, temporary = tempfile.mkstemp(prefix=".review-", dir=folder)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, report_path)
    write_json(folder / ("quality-" + package_id + ".json"), quality)
    snapshot = {"quality": quality, "assessment_history": history}
    receipt = store.capture(source_key="migration-review:" + plan["plan_id"] + ":" + package_id,
                            scope="migration-quality", kind="assessment", title="Качество переноса " + package_id,
                            original=canonical(snapshot), text=raw.decode(), locator=report_path.as_uri(), archive_only=True,
                            metadata={"material_role": "assessment", "plan_id": plan["plan_id"], "package_id": package_id})
    store.set_setting("migration_review:" + plan["plan_id"] + ":" + package_id,
                      json.dumps({"version_id": receipt.version_id, "path": str(report_path)}))
    return {"report_path": str(report_path), "receipt": asdict(receipt), "counts": counts}
