"""One bounded native part attempt; the caller owns the canonical source lease."""
from __future__ import annotations

from .admission import MAX_NATIVE_RETRIES, hold, inflight_count, permission, new_submission_hold
from .native_proof import completion_error, observe_native_progress
from .preservation import PreservationError, Store
from .representations import Representations
from .hindsight import UNCONFIRMED_RESPONSE_CODES


def run_part(store: Store, client, source_job: dict | None, version: dict, reps: Representations, plan: dict) -> dict:
    access = permission(store)
    if not access["allowed"]:
        return {"version_id": version["version_id"], "state": "waiting", "error": access["reason"]}
    if access["mode"] != "continuous":
        return {"version_id": version["version_id"], "state": "waiting", "error": "representation_requires_continuous"}
    part = reps.claim(plan["id"])

    def source_state(state: str, error: str | None = None, *, delay: float = 0, units=None) -> dict:
        if source_job is not None:
            store.update_delivery(source_job, state, error=error, delay=delay, units=units)
        return {"version_id": version["version_id"], "representation_id": plan["id"],
                "state": state, **({"error": error} if error else {})}

    def refresh_source() -> dict:
        fresh = reps.get(plan["id"])
        states = {p["state"] for p in fresh["parts"]}
        if states == {"complete"}:
            proof = reps.complete(plan["id"])
            return source_state("searchable", units=proof["memory_unit_count"])
        if states & {"partial", "blocked", "failed"}:
            failed = next(p for p in fresh["parts"] if p["state"] in {"partial", "blocked", "failed"})
            return source_state("partial", failed["last_error"])
        inflight = any(p["state"] == "submitted" or p["state"] == "pending" and p["attempts"] > 0 for p in fresh["parts"])
        return source_state("submitted" if inflight else "pending", delay=3 if inflight else 0)

    if part is None:
        return refresh_source()

    def finish(state: str, *, error=None, delay=0, proof=None) -> dict:
        reps.finish(part, state, error=error, delay=delay, proof=proof)
        result = refresh_source()
        result["part_index"] = part["part_index"]
        result["part_state"] = state
        if state == "pending" and error:
            result["pending_reason"] = error
        return result

    binding = {"version_id": plan["version_id"], "document_id": part["document_id"],
               "operation_id": part["operation_id"], "text_sha256": part["text_sha256"],
               "profile": plan["profile_fingerprint"]}
    native_may_be_running = part["state"] == "submitted" or part["attempts"] > 0
    try:
        text = reps.read_part(part)
        if not part["requires_native"]:
            if text.strip():
                raise PreservationError("representation_whitespace_mismatch")
            return finish("complete", proof={"kind": "local_whitespace", "text_sha256": part["text_sha256"]})
        operation = client.operation(part["operation_id"])
        state = operation["status"]
        native_may_be_running = state in {"pending", "processing"}
        if state in {"pending", "processing"}:
            observation = observe_native_progress(store, client, version["version_id"], part["operation_id"], operation)
            return {**finish("submitted", delay=3), "native_progress": observation}
        if state in {"failed", "cancelled"}:
            code = operation.get("error_code", "operation_" + state)
            if part["last_error"] == "retry_requested":
                with store.exclusive():
                    drain = new_submission_hold(store)
                    if drain is not None:
                        return {**finish("pending", error="retry_requested", delay=3), **drain}
                    access = permission(store)
                    if not access["allowed"]:
                        return finish("pending", error="retry_requested", delay=30)
                    if inflight_count(store, excluding=plan["version_id"]) >= (access["max_inflight"] or 1):
                        return finish("pending", error="retry_requested", delay=3)
                    if not reps.reserve(part):
                        return {"version_id": plan["version_id"], "state": "withdrawn"}
                    native_may_be_running = True
                    client.retry_operation(part["operation_id"])
                return finish("submitted", delay=3)
            if code == "provider_authentication_required":
                store.set_setting("delivery_attention", code)
            if code in {"provider_rate_limited", "provider_unavailable"}:
                drain = new_submission_hold(store)
                if drain is not None:
                    return {**finish("pending", error=part["last_error"], delay=3), **drain}
                key = "native_retry:" + part["operation_id"]
                retries = int(store.setting(key, "0"))
                if retries >= MAX_NATIVE_RETRIES:
                    return finish("failed", error="provider_retry_exhausted")
                if part["last_error"] != code:
                    delay = min(1800, 300 * 2 ** retries)
                    hold(store, code, delay)
                    return finish("pending", error=code, delay=delay)
                with store.exclusive():
                    drain = new_submission_hold(store)
                    if drain is not None:
                        return {**finish("pending", error=part["last_error"], delay=3), **drain}
                    if not permission(store)["allowed"]:
                        return finish("pending", error=code, delay=30)
                    store.set_setting(key, str(retries + 1))
                    if not reps.reserve(part):
                        return {"version_id": plan["version_id"], "state": "withdrawn"}
                    native_may_be_running = True
                    client.retry_operation(part["operation_id"])
                return finish("submitted", delay=3)
            return finish("failed", error=code)
        if state not in {"not_found", "completed"}:
            raise PreservationError("unsupported_operation_status")
        if state == "completed":
            error = completion_error(operation)
            if error:
                return finish("partial" if error == "native_extraction_partial" else "blocked", error=error)
        verification = client.verify_document(part["document_id"], part["text_sha256"], expected_text=text)
        if verification["searchable"]:
            if state == "completed":
                proof = reps.proofs.record(**binding, operation=operation, verification=verification)
            else:
                proof = reps.proofs.verified(**binding)
                if proof is None:
                    return finish("blocked", error="document_without_completion_proof")
            return finish("complete", proof=proof)
        if verification.get("exists"):
            if not verification.get("text_matches"):
                return finish("blocked", error="remote_document_hash_mismatch")
            if state == "not_found":
                return finish("blocked", error="document_without_completion_proof")
        if state == "completed":
            return finish("blocked", error="no_searchable_units" if verification.get("exists") else "completed_document_missing")
        policy = plan["manifest"]["profile"]
        drain = new_submission_hold(store)
        if drain is not None:
            return {**finish("pending", delay=3), **drain}
        profile = policy["native"]
        strategy = profile.get("strategy")
        # No silent strategy fallback or config drift between different parts.
        if hasattr(client, "retain_profile"):
            current = client.retain_profile(strategy)
            if any(current.get(key) != profile.get(key) for key in ("config_fingerprint", "effective_strategy", "mode", "chunk_size")):
                return finish("blocked", error="representation_profile_changed")
            if profile["mode"] == "chunks" and current.get("bank_auto_consolidation") is not False and current.get("bank_observations") is not False:
                return finish("blocked", error="chunks_consolidation_not_disabled")
        with store.exclusive():
            drain = new_submission_hold(store)
            if drain is not None:
                return {**finish("pending", delay=3), **drain}
            access = permission(store)
            if not access["allowed"]:
                return finish("pending", error=access["reason"], delay=30)
            if inflight_count(store, excluding=plan["version_id"]) >= (access["max_inflight"] or 1):
                return finish("pending", delay=3)
            if not reps.reserve(part):
                return {"version_id": plan["version_id"], "state": "withdrawn"}
            native_may_be_running = True
            client.submit(document_id=part["document_id"], operation_id=part["operation_id"], text=text,
                timestamp=version["metadata"].get("event_at", version["observed_at"]),
                metadata={**version["metadata"], "olympus_source_id": version["source_id"],
                    "olympus_version_id": version["version_id"], "olympus_scope": version["scope"],
                    "olympus_representation_id": plan["id"], "olympus_profile": plan["profile_fingerprint"],
                    "olympus_part_index": str(part["part_index"]), "olympus_part_count": str(len(plan["parts"])),
                    "olympus_char_start": str(part["char_start"]), "olympus_char_end": str(part["char_end"]),
                    "source_text_sha256": version["text_sha256"], "text_sha256": part["text_sha256"],
                    "original_sha256": version["original_sha256"], "title": version["title"], "locator": version["locator"]},
                tags=["olympus", "scope:" + version["scope"], "source:" + version["source_id"]], strategy=strategy)
        return finish("submitted", delay=3)
    except Exception as exc:
        code = str(exc) if isinstance(exc, PreservationError) else getattr(exc, "code", "delivery_error")
        if not isinstance(code, str) or not code.replace("_", "").isalnum():
            code = "delivery_error"
        status = getattr(exc, "status", None)
        transient = (not isinstance(exc, PreservationError) and status in (None, 408, 429, 500, 502, 503, 504)
                     and code in {"transport_error", "timeout", "http_error", "network_error", "delivery_error"})
        if native_may_be_running and code in UNCONFIRMED_RESPONSE_CODES:
            transient = True
        if status in (401, 403) or code in {"credential_unavailable", "invalid_credential"}:
            store.set_setting("delivery_attention", "provider_authentication_required")
        elif status == 429:
            hold(store, "provider_rate_limited", max(300, getattr(exc, "retry_after", 0) or 0))
        elif transient and code in {"transport_error", "timeout", "http_error", "network_error", "delivery_error"}:
            hold(store, "provider_unavailable", 60)
        return finish("pending" if transient else "blocked", error=code,
                      delay=min(1800, 15 * 2 ** min(part["attempts"], 7)) if transient else 0)


def run_representation_one(store: Store, client, representation_id: str) -> dict:
    """Explicit enrichment runner; does not downgrade the canonical source state."""
    reps = Representations(store)
    plan = reps.get(representation_id)
    version = store.read_manifest(plan["version_id"])
    return run_part(store, client, None, version, reps, plan)
