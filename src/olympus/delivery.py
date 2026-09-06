"""Delivery admission and reconciliation around Hindsight's native operations."""
from __future__ import annotations

import time

from .preservation import PreservationError, Store
from .materials import material_profile, package_versions, source_provenance, discussion_matches


def grant_budget(store: Store, operations: int, expires_in: int = 900, max_text_chars: int = 50000) -> None:
    """Bound a pilot series, not a claim about billing or a quota percentage."""
    if not 1 <= operations <= 20 or not 1 <= expires_in <= 3600 or not 1 <= max_text_chars <= 1_000_000:
        raise PreservationError("invalid_pilot_budget")
    with store.connect(write=True) as db:
        for key, value in {"budget_remaining": str(operations), "budget_expires": str(time.time() + expires_in),
                           "budget_max_text_chars": str(max_text_chars)}.items():
            db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _admit(store: Store, job: dict, text: str) -> dict | None:
    """Admit once or return a safe, structured refusal without spending budget."""
    with store.connect(write=True) as db:
        recovery = db.execute("SELECT value FROM settings WHERE key='recovery_state'").fetchone()
        maintenance = db.execute("SELECT value FROM settings WHERE key='maintenance'").fetchone()
        if (recovery and recovery[0] != "ready") or (maintenance and maintenance[0] != "off"):
            return {"error": "awaiting_pilot_budget"}
        settings = {r[0]: r[1] for r in db.execute("SELECT key,value FROM settings WHERE key LIKE 'budget_%'")}
        if time.time() >= float(settings.get("budget_expires", "0")):
            return {"error": "awaiting_pilot_budget"}
        max_text_chars = int(settings.get("budget_max_text_chars", "0"))
        if len(text) > max_text_chars:
            return {"error": "text_exceeds_budget", "text_chars": len(text), "max_text_chars": max_text_chars}
        key = "admitted:" + job["operation_id"]
        if db.execute("SELECT 1 FROM settings WHERE key=?", (key,)).fetchone():
            return None
        remaining = int(settings.get("budget_remaining", "0"))
        if remaining <= 0:
            return {"error": "awaiting_pilot_budget"}
        db.execute("UPDATE settings SET value=? WHERE key='budget_remaining'", (str(remaining - 1),))
        db.execute("INSERT INTO settings VALUES(?,?)", (key, str(time.time())))
        return None


def _run_one(store: Store, client) -> dict:
    """One bounded attempt; repeated invocation continues the same operation."""
    if store.setting("recovery_state", "ready") != "ready":
        return {"state": "recovery_blocked"}
    if store.setting("maintenance", "off") != "off":
        return {"state": "maintenance"}
    job = store.claim()
    if job is None:
        return {"state": "idle"}
    submitted = False
    try:
        version = store.read_version(job["version_id"])
        operation = client.operation(job["operation_id"])
        state = operation["status"]
        if state in {"failed", "cancelled"}:
            store.update_delivery(job, "failed", error="operation_" + state)
            return {"version_id": job["version_id"], "state": "failed"}
        if state in {"pending", "processing"}:
            store.update_delivery(job, "submitted", delay=3)
            return {"version_id": job["version_id"], "state": "submitted"}
        if state not in {"not_found", "completed"}:
            raise PreservationError("unsupported_operation_status")
        if not material_profile(version)["indexable"]:
            store.update_delivery(job, "archived")
            return {"version_id": job["version_id"], "state": "archived"}
        verification = client.verify_document(job["version_id"], version["text_sha256"])
        if verification["searchable"]:
            store.update_delivery(job, "searchable", units=verification["memory_unit_count"])
            return {"version_id": job["version_id"], "state": "searchable"}
        if verification.get("exists", False):
            if not verification["text_matches"]:
                store.update_delivery(job, "blocked", error="remote_document_hash_mismatch")
                return {"version_id": job["version_id"], "state": "blocked"}
            # An existing matching document with unknown operation may still be
            # processing. Do not create more model work until that is resolved.
            if state == "not_found":
                store.update_delivery(job, "blocked", error="document_without_operation")
                return {"version_id": job["version_id"], "state": "blocked"}
        if state == "completed":
            terminal = "empty" if verification.get("exists") else "blocked"
            store.update_delivery(job, terminal, error="no_searchable_units" if terminal == "empty" else "completed_document_missing",
                                  units=verification.get("memory_unit_count"))
            return {"version_id": job["version_id"], "state": terminal}
        # The source may have been forgotten while the read-only calls ran.
        with store.exclusive():
            if store.setting("maintenance", "off") != "off" or store.setting("recovery_state", "ready") != "ready":
                store.update_delivery(job, "pending", delay=30, error="maintenance")
                return {"version_id": job["version_id"], "state": "pending"}
            refusal = _admit(store, job, version["text"])
            if refusal is not None:
                store.update_delivery(job, "pending", delay=30, error=refusal["error"])
                # A document-specific limit must not stop the caller's queue loop:
                # later documents can still fit the same active grant.
                state = "pending" if refusal["error"] == "text_exceeds_budget" else "awaiting_budget"
                return {"version_id": job["version_id"], "state": state, **refusal}
            with store.connect() as db:
                active = db.execute("SELECT active FROM versions WHERE id=?", (job["version_id"],)).fetchone()
                if active is None or not active[0]:
                    return {"version_id": job["version_id"], "state": "withdrawn"}
            submitted = True
            client.submit(
                document_id=job["version_id"], operation_id=job["operation_id"], text=version["text"],
                timestamp=version["metadata"].get("event_at", version["observed_at"]),
                metadata={**version["metadata"], "olympus_source_id": version["source_id"],
                          "olympus_version_id": version["version_id"], "olympus_scope": version["scope"],
                          "original_sha256": version["original_sha256"], "text_sha256": version["text_sha256"],
                          "title": version["title"], "locator": version["locator"]},
                tags=["olympus", "scope:" + version["scope"], "source:" + version["source_id"]],
                strategy=version["metadata"].get("retain_strategy"),
            )
            store.update_delivery(job, "submitted", delay=3, attempted=True)
        return {"version_id": job["version_id"], "state": "submitted"}
    except Exception as exc:
        # Only locally defined stable codes/statuses are allowed into receipts.
        code = str(exc) if isinstance(exc, PreservationError) else getattr(exc, "code", "delivery_error")
        if not isinstance(code, str) or not code.replace("_", "").replace("-", "").isalnum():
            code = "delivery_error"
        http_status = getattr(exc, "status", None)
        transient = http_status in (None, 408, 429, 500, 502, 503, 504) and not isinstance(exc, PreservationError)
        state = "pending" if transient else "blocked"
        delay = min(1800, 15 * 2 ** min(job["attempts"], 7)) if transient else 0
        store.update_delivery(job, state, error=code[:100], delay=delay, attempted=submitted)
        return {"version_id": job["version_id"], "state": state, "error": code[:100]}


def run_one(store: Store, client) -> dict:
    result = _run_one(store, client)
    if "version_id" in result:
        state = store.receipt(result["version_id"]).memory
        if state != "pending" or result["state"] != "awaiting_budget":
            result["state"] = state
    return result


def recall_active(store: Store, client, query: str, scope: str, *, package_id: str | None = None,
                  include_discussions: bool = False) -> dict:
    allowed = store.active_documents(scope)
    permitted = package_versions(store, package_id) if package_id else None
    if permitted is not None:
        allowed &= permitted
    versions = {vid: store.read_version(vid) for vid in allowed}
    allowed = {vid for vid in allowed if material_profile(versions[vid])["indexable"]}
    historical = discussion_matches(store, query, scope, permitted=permitted) if include_discussions else []
    if not allowed:
        return {"results": [], "chunks": {}, "scope": scope, "discussions": historical,
                "interpretation": "Source claims with provenance; discussions are separate, nonbinding context."}
    result = client.recall(query, ["olympus", "scope:" + scope])
    # The source and dependency checks form one local snapshot after the
    # network call. A recalled report never outruns its registered originals.
    with store.exclusive():
        allowed &= store.active_documents(scope)
        from .evidence import report_links
        bindings = report_links(store, allowed, scope)
        withheld = {vid for vid, packages in bindings.items()
                    if any(p["state"] != "ready_for_review" for p in packages)}
        allowed -= withheld
        if historical:
            with store.connect() as db:
                active_history = {r[0] for r in db.execute("""SELECT v.id FROM versions v
                    JOIN sources s ON s.id=v.source_id WHERE s.scope=? AND v.active=1
                    AND s.forgotten_at IS NULL""", (scope,))}
            historical = [item for item in historical if item["version_id"] in active_history]
        units = []
        for item in result.get("results", []):
            vid = item.get("document_id")
            if vid not in allowed:
                continue
            provenance = source_provenance(versions[vid])
            if vid in bindings:
                provenance["research_packages"] = bindings[vid]
            units.append({**item, "source": provenance})
        output = {"results": units, "scope": scope, "discussions": historical,
                  "withheld_reports": [{"version_id": vid, "packages": bindings[vid]} for vid in sorted(withheld)],
                  "interpretation": "Source claims with provenance; citation matches do not prove truth; discussions are nonbinding context."}
        chunks = result.get("chunks", {})
        allowed_chunk_ids = {r.get("chunk_id") for r in units if r.get("chunk_id")}
        forbidden_chunk_ids = {r.get("chunk_id") for r in result.get("results", [])
                               if r.get("document_id") not in allowed and r.get("chunk_id")}
        if isinstance(chunks, dict):
            output["chunks"] = {k: v for k, v in chunks.items()
                                if k in allowed_chunk_ids - forbidden_chunk_ids and isinstance(v, dict)}
        elif isinstance(chunks, list):
            output["chunks"] = [v for v in chunks if isinstance(v, dict) and v.get("document_id") in allowed]
        return output
