"""Local evidence packages on the existing version and correction contract."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat

from .materials import material_profile
from .preservation import Store, PreservationError, canonical, digest, guard_no_secrets, timestamp

MAX_ARTIFACT_BYTES = 512 * 1024
MAX_SOURCE_BYTES = 64 * 1024**2
MAX_CHECK_BYTES = 256 * 1024**2
MAX_REGISTRY = 1000
KINDS = {"research_package", "learning_case", "learning_proposal", "learning_activation"}
POLICY_FIELDS = {"schema", "require_source_dates", "min_distinct_originals", "allow_whitespace_normalization"}
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z")
VERSION_PATTERN = re.compile(r"olv-[0-9a-f]{64}\Z")


def identifier(value) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise PreservationError("invalid_evidence_identifier")
    return value


def version_id(value) -> str:
    if not isinstance(value, str) or not VERSION_PATTERN.fullmatch(value):
        raise PreservationError("invalid_evidence_version")
    return value


def exact_shape(data, required: set[str], optional: set[str] = frozenset()) -> None:
    if not isinstance(data, dict) or not required <= data.keys() or data.keys() - required - optional:
        raise PreservationError("invalid_evidence_shape")


def nonempty(value, limit=4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise PreservationError("invalid_evidence_text")
    return value


def load_input(path: Path, *, limit=MAX_ARTIFACT_BYTES) -> dict:
    """Read a bounded regular JSON input without executing or following it."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise PreservationError("evidence_input_not_regular_or_too_large")
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        if (before.st_size != len(raw) or (before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_ino, after.st_size, after.st_mtime_ns)):
            raise PreservationError("evidence_input_changed")
        guard_no_secrets(raw)
        result = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        raise PreservationError("invalid_evidence_input") from None
    if not isinstance(result, dict):
        raise PreservationError("invalid_evidence_input")
    return result


def assert_ready(store: Store, scope: str) -> None:
    # This uses the existing global recovery/retraction barriers; membership in
    # the model index is not required to verify a locally preserved original.
    store.active_documents(scope)


class Sources:
    def __init__(self, store: Store):
        self.store, self.cache, self.bytes = store, {}, 0

    def get(self, vid: str) -> dict:
        version_id(vid)
        if vid in self.cache:
            return self.cache[vid]
        with self.store.connect() as db:
            row = db.execute("""SELECT v.active,s.forgotten_at FROM versions v
                JOIN sources s ON s.id=v.source_id WHERE v.id=?""", (vid,)).fetchone()
        if not row or not row["active"] or row["forgotten_at"]:
            raise PreservationError("evidence_version_not_active")
        folder = self.store.versions / vid
        try:
            size = sum((folder / name).stat().st_size for name in ("original", "text.txt"))
        except OSError:
            raise PreservationError("evidence_version_unavailable") from None
        if size > MAX_SOURCE_BYTES or self.bytes + size > MAX_CHECK_BYTES:
            raise PreservationError("evidence_check_size_limit")
        data = self.store.read_version(vid)
        self.bytes += size
        self.cache[vid] = data
        return data


def record_key(kind: str, scope: str, record_id: str) -> str:
    if kind not in KINDS:
        raise PreservationError("invalid_evidence_record_kind")
    identifier(scope)
    identifier(record_id)
    return kind + ":" + digest(canonical([scope, record_id]))


def save_record(store: Store, kind: str, data: dict, *, role="artifact") -> dict:
    key = record_key(kind, data["scope"], data["id"])
    raw = canonical(data)
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise PreservationError("evidence_artifact_too_large")
    receipt = store.capture(source_key=key, scope=data["scope"], kind="document",
        title=kind + ": " + data["id"], original=raw, text=raw.decode(),
        locator="olympus-record://" + kind + "/" + data["id"], archive_only=True,
        metadata={"material_role": role, "artifact_type": kind})
    store.set_setting(key, receipt.version_id)
    return asdict(receipt)


def read_record(store: Store, vid: str, kind: str, *, sources: Sources | None = None,
                require_current=False) -> tuple[dict, dict]:
    source = (sources or Sources(store)).get(vid)
    if (source["metadata"].get("artifact_type") != kind or
            source["metadata"].get("delivery_mode") != "archive_only"):
        raise PreservationError("wrong_evidence_record_type")
    try:
        data = json.loads(source["original"])
        key = record_key(kind, data["scope"], data["id"])
    except (ValueError, KeyError, TypeError, RecursionError):
        raise PreservationError("invalid_evidence_record") from None
    if source["source_key"] != key or source["scope"] != data["scope"]:
        raise PreservationError("evidence_record_identity_mismatch")
    if require_current and store.setting(key) != vid:
        raise PreservationError("evidence_record_replaced")
    return data, source


def registry(store: Store, kind: str, scope: str | None = None) -> list[str]:
    if kind not in KINDS:
        raise PreservationError("invalid_evidence_record_kind")
    with store.connect() as db:
        prefix = kind + ":"
        rows = db.execute("SELECT key,value FROM settings WHERE substr(key,1,?)=? ORDER BY key LIMIT ?",
                          (len(prefix), prefix, MAX_REGISTRY + 1)).fetchall()
    if len(rows) > MAX_REGISTRY:
        raise PreservationError("evidence_registry_limit")
    result = []
    for row in rows:
        vid = version_id(row["value"])
        # Scope can be selected without opening raw bodies or all source files.
        with store.connect() as db:
            meta = db.execute("SELECT s.scope FROM versions v JOIN sources s ON s.id=v.source_id WHERE v.id=?", (vid,)).fetchone()
        if not meta:
            raise PreservationError("evidence_registry_unresolved")
        if scope is None or meta[0] == scope:
            result.append(vid)
    return result


def validate_policy(policy: dict) -> dict:
    exact_shape(policy, POLICY_FIELDS)
    if (type(policy["schema"]) is not int or policy["schema"] != 1
            or type(policy["require_source_dates"]) is not bool
            or type(policy["allow_whitespace_normalization"]) is not bool
            or type(policy["min_distinct_originals"]) is not int
            or not 1 <= policy["min_distinct_originals"] <= 5):
        raise PreservationError("invalid_research_policy")
    return dict(policy)


def base_policy() -> dict:
    path = Path(__file__).resolve().parents[2] / "config/research-policy.json"
    return validate_policy(load_input(path))


def validate_manifest(data: dict) -> None:
    exact_shape(data, {"schema", "id", "scope", "report_version", "claims"})
    if type(data["schema"]) is not int or data["schema"] != 1:
        raise PreservationError("unsupported_research_schema")
    identifier(data["id"])
    identifier(data["scope"])
    version_id(data["report_version"])
    claims = data["claims"]
    if not isinstance(claims, list) or not 1 <= len(claims) <= 100:
        raise PreservationError("invalid_research_claims")
    ids, total = set(), 0
    for claim in claims:
        exact_shape(claim, {"id", "type", "statement", "evidence"})
        cid = identifier(claim["id"])
        if cid in ids or not isinstance(claim["type"], str) or claim["type"] not in {"factual", "inference", "hypothesis", "editorial"}:
            raise PreservationError("invalid_research_claim")
        ids.add(cid)
        nonempty(claim["statement"])
        if not isinstance(claim["evidence"], list) or len(claim["evidence"]) > 20:
            raise PreservationError("invalid_claim_evidence")
        for entry in claim["evidence"]:
            exact_shape(entry, {"source_version", "quote"}, {"start"})
            version_id(entry["source_version"])
            nonempty(entry["quote"], 2000)
            if "start" in entry and (type(entry["start"]) is not int or entry["start"] < 0):
                raise PreservationError("invalid_quote_position")
            total += 1
    if total > 200:
        raise PreservationError("too_many_evidence_entries")


def quote_match(text: str, quote: str, *, start: int | None = None, normalize=True) -> dict:
    if start is not None:
        if text[start:start + len(quote)] != quote:
            return {"status": "quote_position_mismatch"}
        position = start
    else:
        position = text.find(quote)
        if position >= 0 and text.find(quote, position + 1) >= 0:
            return {"status": "quote_ambiguous"}
    if position >= 0:
        end = position + len(quote)
        return {"status": "matched_exact", "start": position, "end": end,
                "line_start": text.count("\n", 0, position) + 1,
                "line_end": text.count("\n", 0, end - 1) + 1}
    if normalize:
        hay, needle = " ".join(text.split()), " ".join(quote.split())
        position = hay.find(needle)
        if position >= 0:
            if hay.find(needle, position + 1) >= 0:
                return {"status": "quote_ambiguous"}
            return {"status": "matched_whitespace", "start": None, "end": None,
                    "line_start": None, "line_end": None}
    return {"status": "quote_not_found"}


def declared_date(source: dict) -> str | None:
    for key in ("source_published_at", "source_date", "published_at"):
        value = source["metadata"].get(key)
        if not isinstance(value, str):
            continue
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return value
        except ValueError:
            continue
    return None


def check_manifest(store: Store, manifest: dict, policy: dict, *, sources: Sources | None = None) -> dict:
    validate_manifest(manifest)
    validate_policy(policy)
    cache = sources or Sources(store)
    issues, claims, dependency_ids = [], [], {manifest["report_version"]}
    try:
        report = cache.get(manifest["report_version"])
        if material_profile(report)["material_role"] != "synthesis" or report["scope"] != manifest["scope"]:
            issues.append({"code": "report_role_or_scope_mismatch"})
    except PreservationError as exc:
        report = None
        issues.append({"code": "report_unavailable", "reason": str(exc)})
    for claim in manifest["claims"]:
        cid, entries, originals = claim["id"], [], set()
        if report and claim["statement"] not in report["text"]:
            issues.append({"code": "claim_not_in_report", "claim_id": cid})
        if claim["type"] in {"factual", "inference"} and not claim["evidence"]:
            issues.append({"code": "claim_evidence_missing", "claim_id": cid})
        for index, entry in enumerate(claim["evidence"]):
            vid = entry["source_version"]
            dependency_ids.add(vid)
            item = {"source_version": vid, "quote": entry["quote"]}
            if vid == manifest["report_version"]:
                issues.append({"code": "report_is_context_only", "claim_id": cid, "evidence_index": index})
            try:
                source = cache.get(vid)
                role = material_profile(source)["material_role"]
                matched = quote_match(source["text"], entry["quote"], start=entry.get("start"),
                                      normalize=policy["allow_whitespace_normalization"])
                item.update(matched, source_role=role, original_sha256=source["original_sha256"],
                            text_sha256=source["text_sha256"], source_title=source["title"],
                            source_date=declared_date(source))
                if not matched["status"].startswith("matched_"):
                    issues.append({"code": matched["status"], "claim_id": cid, "evidence_index": index})
                factual_ok = role in {"primary", "extracted"} and vid != manifest["report_version"]
                if claim["type"] == "factual" and not factual_ok:
                    issues.append({"code": "factual_source_role_ineligible", "claim_id": cid, "evidence_index": index})
                if claim["type"] == "factual" and policy["require_source_dates"] and not item["source_date"]:
                    issues.append({"code": "source_date_missing", "claim_id": cid, "evidence_index": index})
                if matched["status"].startswith("matched_") and factual_ok:
                    originals.add(source["original_sha256"])
            except PreservationError as exc:
                item.update(status="source_unavailable", reason=str(exc))
                issues.append({"code": "source_unavailable", "claim_id": cid, "evidence_index": index, "reason": str(exc)})
            entries.append(item)
        if claim["type"] == "factual" and len(originals) < policy["min_distinct_originals"]:
            issues.append({"code": "insufficient_distinct_originals", "claim_id": cid})
        claims.append({"id": cid, "type": claim["type"], "statement": claim["statement"],
                       "evidence": entries, "distinct_originals": len(originals), "verified_fact": False})
    stable = {"schema": 1, "package_id": manifest["id"], "scope": manifest["scope"],
              "report_version": manifest["report_version"], "state": "held" if issues else "ready_for_review",
              "policy_sha256": digest(canonical(policy)), "issues": issues, "claims": claims,
              "dependency_versions": sorted(dependency_ids), "coverage": "declared_claims_only",
              "semantic_support_verified": False, "verified_fact": False}
    return {**stable, "verification_sha256": digest(canonical(stable)), "checked_at": timestamp()}


def register_package(store: Store, data: dict) -> dict:
    validate_manifest(data)
    with store.exclusive():
        assert_ready(store, data["scope"])
        # Capture precedes citation analysis; even a held package is reviewable.
        receipt = save_record(store, "research_package", data)
        return {"receipt": receipt, "verification": check_package(store, receipt["version_id"])}


def check_package(store: Store, vid: str, *, policy: dict | None = None,
                  require_current=False) -> dict:
    with store.exclusive():
        cache = Sources(store)
        data, source = read_record(store, vid, "research_package", sources=cache, require_current=require_current)
        assert_ready(store, data["scope"])
        if policy is None:
            from .learning import effective_policy
            policy = effective_policy(store, data["scope"])["policy"]
        return {**check_manifest(store, data, policy, sources=cache), "package_version": vid}


def report_links(store: Store, reports: set[str], scope: str) -> dict[str, list[dict]]:
    """Caller holds the writer lock across this check and final result shaping."""
    result = {}
    for vid in registry(store, "research_package", scope):
        # Read the immutable manifest first so a withdrawn package can still
        # withhold its known report instead of silently removing the guard.
        version = store.read_version(vid)
        try:
            data = json.loads(version["original"])
            report = data["report_version"]
        except (ValueError, KeyError, TypeError):
            raise PreservationError("invalid_research_registry") from None
        if report not in reports:
            continue
        try:
            checked = check_package(store, vid, require_current=True)
            summary = {"package_version": vid, "package_id": data["id"], "state": checked["state"],
                       "verification_sha256": checked["verification_sha256"],
                       "dependency_versions": checked["dependency_versions"],
                       "issue_codes": sorted({x["code"] for x in checked["issues"]}),
                       "semantic_support_verified": False, "coverage": "declared_claims_only"}
        except PreservationError as exc:
            summary = {"package_version": vid, "state": "held", "issue_codes": [str(exc)],
                       "semantic_support_verified": False}
        result.setdefault(report, []).append(summary)
    return result
