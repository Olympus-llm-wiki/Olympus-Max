"""Delivery admission and reconciliation around Hindsight's native operations."""
from __future__ import annotations

import time
import json

from .preservation import PreservationError, Store
from .materials import material_profile, package_versions, source_provenance, discussion_matches
from .admission import permission, inflight_count, hold, new_submission_hold, MAX_NATIVE_RETRIES
from .native_proof import CompletionProofs, completion_error, completion_readiness, profile_fingerprint, observe_native_progress
from .hindsight import UNCONFIRMED_RESPONSE_CODES


def grant_budget(store: Store, operations: int, expires_in: int = 900, max_text_chars: int = 50000) -> None:
    """Bound a pilot series, not a claim about billing or a quota percentage."""
    if not 1 <= operations <= 20 or not 1 <= expires_in <= 3600 or not 1 <= max_text_chars <= 1_000_000:
        raise PreservationError("invalid_pilot_budget")
    with store.connect(write=True) as db:
        for key, value in {"budget_remaining": str(operations), "budget_expires": str(time.time() + expires_in),
                           "budget_max_text_chars": str(max_text_chars), "delivery_mode": "pilot"}.items():
            db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _admit(store: Store, job: dict, text: str) -> dict | None:
    """Admit once or return a safe, structured refusal without spending budget."""
    access = permission(store)
    drain = new_submission_hold(store)
    if drain is not None:
        return {"error": drain["reason"], "retry_at": drain["retry_at"]}
    if access["mode"] != "pilot":
        if not access["allowed"]:
            return {"error": access["reason"]}
        if inflight_count(store, excluding=job["version_id"]) >= access["max_inflight"]:
            return {"error": "waiting_for_inflight"}
        return None
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


def _run_one(store: Store, client, *, version_id: str | None = None) -> dict:
    """One bounded attempt; repeated invocation continues the same operation."""
    if store.setting("recovery_state", "ready") != "ready":
        return {"state": "recovery_blocked"}
    if store.setting("maintenance", "off") != "off":
        return {"state": "maintenance"}
    access = permission(store)
    if access["mode"] != "pilot" and not access["allowed"]:
        return {"state": "waiting", "error": access["reason"]}
    if version_id is not None and store.setting('processing_profile:'+version_id) is not None:
        from .reference_profiles import processing_profile
        return {'state':'profile_selected','version_id':version_id,
                'processing_profile':processing_profile(store,version_id)}
    job = store.claim(version_id=version_id)
    if job is None:
        return {"state": "idle"}
    native_may_be_running = job["state"] == "submitted" or job["attempts"] > 0
    try:
        version = store.read_manifest(job["version_id"])
        from .representations import Representations, _exists
        part_chars = int(store.setting("native_part_chars", "0"))
        if _exists(store) or part_chars:
            reps = Representations(store)
            plan = reps.for_version(job["version_id"])
            if plan is None and part_chars and access["mode"] == "continuous" and job["attempts"] == 0:
                drain = new_submission_hold(store)
                if drain is not None:
                    store.update_delivery(job, "pending", delay=3)
                    return {"version_id": job["version_id"], "state": "waiting", **drain}
                from .delivery_formats import classify_delivery_format
                try:
                    with (store.versions / job["version_id"] / "text.txt").open("rb") as stream:
                        prefix = stream.read(8192).decode("utf-8", errors="replace")
                except OSError:
                    raise PreservationError("source_format_unavailable") from None
                source_format = classify_delivery_format(version, prefix)
                store.set_setting("delivery_format:" + job["version_id"], json.dumps(source_format))
                if not source_format["auto_profile_allowed"]:
                    store.update_delivery(job, "pending", error="waiting_profile_review", delay=3600)
                    return {"version_id": job["version_id"], "state": "pending", "reason": "waiting_profile_review",
                            "source_format": source_format["format"]}
                version = store.read_text_version(job["version_id"])
                if len(version["text"]) > part_chars:
                    profile = client.retain_profile(version["metadata"].get("retain_strategy"))
                    plan = reps.prepare(job["version_id"], profile=profile, max_part_chars=part_chars)
                    plan = reps.enable(plan["id"])
            if plan is not None:
                from .part_delivery import run_part
                return run_part(store, client, job, version, reps, plan)
        if "text" not in version:
            version = store.read_text_version(job["version_id"])
        operation = client.operation(job["operation_id"])
        state = operation["status"]
        native_may_be_running = state in {"pending", "processing"}
        continuous = store.setting("delivery_mode", "pilot") == "continuous"
        if state == "failed" and continuous and operation.get("error_code") in {"provider_rate_limited", "provider_unavailable"}:
            code = operation["error_code"]
            key = "native_retry:" + job["operation_id"]
            with store.exclusive():
                drain = new_submission_hold(store)
                if drain is not None:
                    store.update_delivery(job, "pending", error=job["last_error"], delay=3)
                    return {"version_id": job["version_id"], "state": "waiting", **drain}
                access = permission(store)
                if not access["allowed"]:
                    store.update_delivery(job, "pending", error=access["reason"], delay=30)
                    return {"version_id": job["version_id"], "state": "waiting", "error": access["reason"]}
                retries = int(store.setting(key, "0"))
                if retries >= MAX_NATIVE_RETRIES:
                    store.update_delivery(job, "failed", error="provider_retry_exhausted")
                    return {"version_id": job["version_id"], "state": "failed", "error": "provider_retry_exhausted"}
                # First observation records the wait before invoking native retry.
                if job["last_error"] != code:
                    delay = min(1800, 300 * 2 ** retries)
                    hold(store, code, delay)
                    store.update_delivery(job, "pending", error=code, delay=delay)
                    return {"version_id": job["version_id"], "state": "waiting", "error": code}
                # Reserve before the request; lost responses never create a new ID.
                store.set_setting(key, str(retries + 1))
                if not store.reserve_delivery_attempt(job):
                    return {"version_id": job["version_id"], "state": "withdrawn"}
                native_may_be_running = True
                client.retry_operation(job["operation_id"])
                store.update_delivery(job, "submitted", delay=3)
                return {"version_id": job["version_id"], "state": "submitted"}
        if state in {"failed", "cancelled"}:
            code = operation.get("error_code", "operation_" + state)
            if continuous and code == "provider_authentication_required":
                store.set_setting("delivery_attention", code)
            store.update_delivery(job, "failed", error=code)
            return {"version_id": job["version_id"], "state": "failed", "error": code}
        if state in {"pending", "processing"}:
            observation = observe_native_progress(store, client, job["version_id"], job["operation_id"], operation)
            store.update_delivery(job, "submitted", delay=3)
            return {"version_id": job["version_id"], "state": "submitted", "native_progress": observation}
        if state not in {"not_found", "completed"}:
            raise PreservationError("unsupported_operation_status")
        if not material_profile(version)["indexable"]:
            store.update_delivery(job, "archived")
            return {"version_id": job["version_id"], "state": "archived"}
        proofs = CompletionProofs(store)
        proof_binding = {"version_id": job["version_id"], "document_id": job["version_id"],
                         "operation_id": job["operation_id"], "text_sha256": version["text_sha256"],
                         "profile": profile_fingerprint(version["metadata"].get("retain_strategy"))}
        if state == "completed":
            error = completion_error(operation)
            if error:
                terminal = "partial" if error == "native_extraction_partial" else "blocked"
                store.update_delivery(job, terminal, error=error)
                return {"version_id": job["version_id"], "state": terminal, "error": error}
        verification = client.verify_document(job["version_id"], version["text_sha256"], expected_text=version['text'])
        if verification["searchable"]:
            if state == "completed":
                proofs.record(**proof_binding, operation=operation, verification=verification)
            elif proofs.verified(**proof_binding) is None:
                store.update_delivery(job, "blocked", error="document_without_completion_proof")
                return {"version_id": job["version_id"], "state": "blocked", "error": "document_without_completion_proof"}
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
                if refusal["error"] == "backup_snapshot":
                    store.update_delivery(job, "pending", delay=3)
                    return {"version_id": job["version_id"], "state": "waiting", "reason": "backup_snapshot",
                            "retry_at": refusal["retry_at"]}
                if refusal["error"] == "waiting_for_inflight":
                    store.update_delivery(job, "pending", delay=3)
                    return {"version_id": job["version_id"], "state": "waiting", "reason": "waiting_for_inflight"}
                store.update_delivery(job, "pending", delay=30, error=refusal["error"])
                # A document-specific limit must not stop the caller's queue loop:
                # later documents can still fit the same active grant.
                state = {"text_exceeds_budget": "pending", "awaiting_pilot_budget": "awaiting_budget"}.get(refusal["error"], "waiting")
                return {"version_id": job["version_id"], "state": state, **refusal}
            with store.connect() as db:
                active = db.execute("SELECT active FROM versions WHERE id=?", (job["version_id"],)).fetchone()
                if active is None or not active[0]:
                    return {"version_id": job["version_id"], "state": "withdrawn"}
            if not store.reserve_delivery_attempt(job):
                return {"version_id": job["version_id"], "state": "withdrawn"}
            native_may_be_running = True
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
            store.update_delivery(job, "submitted", delay=3)
        return {"version_id": job["version_id"], "state": "submitted"}
    except Exception as exc:
        # Only locally defined stable codes/statuses are allowed into receipts.
        code = str(exc) if isinstance(exc, PreservationError) else getattr(exc, "code", "delivery_error")
        if not isinstance(code, str) or not code.replace("_", "").replace("-", "").isalnum():
            code = "delivery_error"
        http_status = getattr(exc, "status", None)
        transient = http_status in (None, 408, 429, 500, 502, 503, 504) and not isinstance(exc, PreservationError)
        if code in {"invalid_text", "invalid_metadata", "invalid_timestamp", "request_too_large",
                    "response_too_large", "invalid_request_body", "credential_unavailable", "invalid_credential"}:
            transient = False
        if native_may_be_running and code in UNCONFIRMED_RESPONSE_CODES:
            transient = True
        if store.setting("delivery_mode", "pilot") == "continuous":
            if http_status in (401, 403) or code in {"credential_unavailable", "invalid_credential"}:
                store.set_setting("delivery_attention", "provider_authentication_required")
            elif http_status == 429:
                hold(store, "provider_rate_limited", max(300, getattr(exc, "retry_after", 0) or 0))
            elif transient and code in {"transport_error", "timeout", "http_error", "network_error"}:
                hold(store, "provider_unavailable", 60)
        state = "pending" if transient else "blocked"
        delay = min(1800, 15 * 2 ** min(job["attempts"], 7)) if transient else 0
        store.update_delivery(job, state, error=code[:100], delay=delay)
        return {"version_id": job["version_id"], "state": state, "error": code[:100]}


def run_one(store: Store, client, *, version_id: str | None = None) -> dict:
    result = _run_one(store, client, version_id=version_id)
    if "version_id" in result:
        state = store.receipt(result["version_id"]).memory
        if state != "pending" or result["state"] not in {"awaiting_budget", "waiting"}:
            result["state"] = state
    return result


def recall_active(store: Store, client, query: str, scope: str, *, package_id: str | None = None,
                  include_discussions: bool = False) -> dict:
    from .representations import native_document_map
    from .evidence import report_links, report_bindings_current
    from .withholding import eligible_versions
    allowed = store.active_documents(scope)
    permitted = package_versions(store, package_id) if package_id else None
    if permitted is not None:
        allowed &= permitted
    # Provenance lives in verified manifests; candidate discovery does not need
    # to read every binary original (or even every full text) before native recall.
    versions = {vid: store.read_manifest(vid) for vid in allowed}
    allowed = {vid for vid in allowed if material_profile(versions[vid])["indexable"]}
    historical = discussion_matches(store, query, scope, permitted=permitted) if include_discussions else []
    if not allowed:
        diagnostic = eligible_versions(store, scope=scope, delivered_only=True, diagnostics=True)
        held_reports = {item["version_id"] for item in diagnostic["withheld"] if item["research_report"]}
        if permitted is not None:
            held_reports &= permitted
        held_bindings = report_links(store, held_reports, scope) if held_reports else {}
        with store.exclusive():
            current_history = eligible_versions(store, scope=scope)
            historical = [item for item in historical if item["version_id"] in current_history]
        return {"results": [], "chunks": {}, "scope": scope, "discussions": historical,
                "withheld_reports": [{"version_id": vid, "packages": held_bindings.get(vid, [])} for vid in sorted(held_reports)],
                "interpretation": "Source claims with provenance; discussions are separate, nonbinding context."}
    initial_mapping = native_document_map(store, allowed)
    result = client.recall(query, ["olympus", "scope:" + scope])
    candidates = {initial_mapping[item["document_id"]]["version_id"] for item in result.get("results", [])
                  if item.get("document_id") in initial_mapping}
    # Potentially expensive source validation happens before the final short
    # withdrawal snapshot. report_links also verifies its own registry snapshot.
    bindings = report_links(store, candidates, scope)
    withheld = {vid for vid, packages in bindings.items()
                if any(p["state"] != "ready_for_review" for p in packages)}
    # Legacy searchable is an older transport/document proof until independently
    # reconciled; never imply that it has a new completion/profile certificate.
    readiness = {}
    with store.connect() as db:
        original_ops = {r[0]: r[1] for r in db.execute("SELECT version_id,operation_id FROM delivery")}
    for vid in candidates:
        readiness[vid] = completion_readiness(store, version_id=vid, document_id=vid,
            operation_id=original_ops[vid], text_sha256=versions[vid]["text_sha256"],
            profile=profile_fingerprint(versions[vid]["metadata"].get("retain_strategy")))
    with store.exclusive():
        if not report_bindings_current(store, bindings):
            withheld.update(candidates)
        allowed &= store.active_documents(scope)
        if package_id:
            allowed &= package_versions(store, package_id)
        allowed -= withheld
        final_mapping = native_document_map(store, allowed)
        mapping = {doc: binding for doc, binding in initial_mapping.items()
                   if final_mapping.get(doc) == binding}
        if historical:
            active_history = eligible_versions(store, scope=scope)
            historical = [item for item in historical if item["version_id"] in active_history]
        units = []
        accepted_docs = set()
        for item in result.get("results", []):
            doc = item.get("document_id")
            if doc not in mapping or item.get("type", item.get("fact_type", "world")) not in {"world", "experience"}:
                continue
            binding = mapping[doc]
            vid = binding["version_id"]
            provenance = source_provenance(versions[vid])
            if vid in bindings:
                provenance["research_packages"] = bindings[vid]
            if "representation_id" in binding:
                provenance["native_representation"] = binding
                provenance["native_completion"] = {"state": "native_complete", "terminal_receipt": True,
                    "profile_evidence": "representation_manifest", "enrichment_verified": False}
            else:
                provenance["native_completion"] = readiness[vid]
            accepted_docs.add(doc)
            units.append({**item, "document_id": vid, "native_document_id": doc, "source": provenance})
        output = {"results": units, "scope": scope, "discussions": historical,
                  "withheld_reports": [{"version_id": vid, "packages": bindings[vid]} for vid in sorted(withheld)],
                  "interpretation": "Source claims with provenance; citation matches do not prove truth; discussions are nonbinding context."}
        chunks = result.get("chunks", {})
        allowed_chunk_ids = {r.get("chunk_id") for r in units if r.get("chunk_id")}
        forbidden_chunk_ids = {r.get("chunk_id") for r in result.get("results", [])
                               if r.get("document_id") not in accepted_docs and r.get("chunk_id")}
        if isinstance(chunks, dict):
            output["chunks"] = {k: v for k, v in chunks.items()
                                if k in allowed_chunk_ids - forbidden_chunk_ids and isinstance(v, dict)}
        elif isinstance(chunks, list):
            output["chunks"] = [{**v, "document_id": mapping[v["document_id"]]["version_id"],
                                 "native_document_id": v["document_id"]}
                                for v in chunks if isinstance(v, dict) and v.get("document_id") in accepted_docs]
        return output
