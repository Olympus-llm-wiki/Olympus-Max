"""Correction reconciliation for the explicitly scoped no-pages pilot bank.

Workers and every writer must already be stopped by the caller. This module
does not change runtime modes, run models, retry jobs, or erase local originals.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re
import time
from typing import Callable
import uuid

from .hindsight import HindsightError
from .preservation import Store, PreservationError, canonical, digest, timestamp

QUIET_PROOF_MAX_AGE_SECONDS = 90
MAX_OPERATIONS = 1000
MAX_CHANGES = 100
MAX_DOCUMENTS = 100
MAX_DRAIN_PASSES = 4
MAX_WALL_SECONDS = 120
_DERIVATIVE_TASKS = frozenset({"consolidation", "graph_maintenance", "vector_index_maintenance"})
_SOURCE_TASKS = frozenset({"retain", "batch_retain"})
_TERMINAL = frozenset({"completed", "failed", "cancelled"})
_COUNTS = ("observations", "mental_models", "knowledge_pages", "pending_operations", "processing_operations")


class _Barrier(Exception):
    pass


def _uuid(value: object) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise _Barrier("invalid_native_operation_id") from None
    return value


def reconcile_no_pages(store: Store, client, *, assert_quiet: Callable[[], dict]) -> dict:
    """Apply only the captured correction IDs after final absence readback.

    ``assert_quiet`` is called afresh under ``store.exclusive``. It must be a
    read-only callback, must not acquire that same lock, and must return exact
    booleans ``workers_stopped``/``writers_stopped`` plus a recent ``checked_at``
    (ISO timestamp with timezone or Unix seconds). A supplied timestamp is not
    itself proof: the caller owns genuine runtime observation.
    """
    result = {
        "state": "blocked", "reason": None, "applied_change_ids": [],
        "target_document_ids": [], "cancelled_operation_ids": [],
        "deleted_operation_ids": [], "deleted_document_ids": [],
        "observations_cleared": 0, "current_read_barrier": True,
    }
    deadline = time.monotonic() + MAX_WALL_SECONDS

    def bounded() -> None:
        if time.monotonic() >= deadline:
            raise _Barrier("reconciliation_time_limit")

    def recovery_ready() -> None:
        if store.setting("recovery_state", "ready") != "ready":
            raise _Barrier("recovery_verification_required")

    def quiet() -> str:
        bounded()
        recovery_ready()
        if not callable(assert_quiet):
            raise _Barrier("quiet_callback_required")
        try:
            proof = assert_quiet()
        except Exception:
            raise _Barrier("quiet_observation_failed") from None
        if (not isinstance(proof, dict) or proof.get("workers_stopped") is not True
                or proof.get("writers_stopped") is not True):
            raise _Barrier("runtime_not_quiet")
        checked = proof.get("checked_at")
        try:
            if isinstance(checked, str):
                moment = datetime.fromisoformat(checked.replace("Z", "+00:00"))
                if moment.tzinfo is None:
                    raise ValueError
                checked_epoch = moment.timestamp()
            elif type(checked) in (float, int) and math.isfinite(checked):
                checked_epoch = float(checked)
            else:
                raise ValueError
            age = time.time() - checked_epoch
            if age < -5 or age > QUIET_PROOF_MAX_AGE_SECONDS:
                raise ValueError
        except (ValueError, OverflowError, OSError):
            raise _Barrier("quiet_observation_not_fresh") from None
        recovery_ready()
        bounded()
        return datetime.fromtimestamp(checked_epoch, timezone.utc).isoformat()

    def call(method: str, *args, write: bool = False, **kwargs):
        bounded()
        if write:
            quiet()
        answer = getattr(client, method)(*args, **kwargs)
        bounded()
        return answer

    def state() -> dict:
        observed = call("reconciliation_state")
        if not isinstance(observed, dict) or any(type(observed.get(k)) is not int or observed[k] < 0 for k in _COUNTS):
            raise _Barrier("invalid_reconciliation_counts")
        safe = {k: observed[k] for k in _COUNTS}
        if safe["mental_models"] or safe["knowledge_pages"]:
            raise _Barrier("knowledge_pages_require_reconciliation")
        return safe

    def inventory() -> list[dict]:
        rows = []
        expected_total = None
        bank = getattr(client, "bank_id", None)
        seen = set()
        offset = 0
        while expected_total is None or offset < expected_total:
            page = call("list_operations", limit=100, offset=offset)
            if (not isinstance(page, dict) or type(page.get("total")) is not int or page["total"] < 0
                    or not isinstance(page.get("operations"), list) or page.get("limit") != 100
                    or page.get("offset") != offset or bank is not None and page.get("bank_id") != bank):
                raise _Barrier("invalid_native_inventory")
            total = page["total"]
            if total > MAX_OPERATIONS:
                raise _Barrier("native_operation_inventory_limit")
            if expected_total is not None and total != expected_total:
                raise _Barrier("native_inventory_changed")
            expected_total = total
            if len(page["operations"]) != min(100, max(0, total - offset)):
                raise _Barrier("incomplete_native_inventory")
            for row in page["operations"]:
                if (not isinstance(row, dict) or row.get("status") not in _TERMINAL | {"pending", "processing"}
                        or not isinstance(row.get("task_type"), str)
                        or type(row.get("items_count")) is not int or row["items_count"] < 0):
                    raise _Barrier("invalid_native_inventory")
                identifier = _uuid(row.get("id"))
                if identifier in seen:
                    raise _Barrier("duplicate_native_operation")
                seen.add(identifier)
                rows.append(row)
            offset += len(page["operations"])
        return rows

    def operation_status(identifier: str) -> str:
        op = call("operation", identifier)
        if (not isinstance(op, dict) or op.get("status") not in _TERMINAL | {"pending", "processing", "not_found"}
                or op.get("operation_id", identifier) != identifier):
            raise _Barrier("unknown_operation_state")
        return op["status"]

    def verify_absent(document_id: str) -> bool:
        proof = call("verify_deleted_document", document_id)
        if not isinstance(proof, dict) or proof.get("document_id") != document_id:
            raise _Barrier("invalid_document_absence_proof")
        return all(proof.get(k) is True for k in ("document_absent", "memory_units_absent", "deleted"))

    try:
        with store.exclusive():
            recovery_ready()
            changes = store.pending_changes()
            if not changes:
                return {**result, "state": "idle", "reason": "no_pending_changes", "current_read_barrier": False}
            if len(changes) > MAX_CHANGES:
                raise _Barrier("change_batch_limit")
            if any(c.get("kind") not in {"forget", "supersede"} or type(c.get("id")) is not int for c in changes):
                raise _Barrier("unsupported_change_contract")
            change_ids = [c["id"] for c in changes]
            documents = sorted({document for change in changes for document in store.change_documents(change)})
            if not documents or len(documents) > MAX_DOCUMENTS:
                raise _Barrier("changed_document_inventory_limit")
            if any(not re.fullmatch(r"olv-[a-f0-9]{64}", doc) for doc in documents):
                raise _Barrier("invalid_changed_document_id")
            result["target_document_ids"] = documents
            with store.connect() as db:
                rows = [dict(db.execute("""SELECT v.id,v.source_id,v.active,d.operation_id
                    FROM versions v JOIN delivery d ON d.version_id=v.id WHERE v.id=?""", (doc,)).fetchone() or {}) for doc in documents]
            if any(not r or r["active"] != 0 for r in rows):
                raise _Barrier("changed_version_not_withdrawn")
            target_ops = {_uuid(row["operation_id"]): row["id"] for row in rows}
            target_set = set(documents)
            snapshot_hash = digest(canonical(changes))
            initial_quiet_at = quiet()
            initial = state()

            def exact_target(row: dict) -> bool:
                return (row["id"] in target_ops
                        and row.get("document_id") in (None, target_ops[row["id"]])
                        and row["items_count"] <= 1)

            def terminal_target(row: dict) -> bool:
                # v0.9.2 also records a separate native retain child for a
                # single-item batch. Its explicit document binding is ownership
                # evidence even though that child UUID was not client-supplied.
                source = row["task_type"] in _SOURCE_TASKS and row["items_count"] <= 1
                return source and (exact_target(row) or row.get("document_id") in target_set)

            def operations_settle() -> list[dict]:
                entries = inventory()
                # Validate the complete inventory before making any cancellation.
                for row in entries:
                    if row["status"] == "processing":
                        raise _Barrier("native_operation_processing")
                    if row["id"] in target_ops and not (
                        exact_target(row) and row["task_type"] in _SOURCE_TASKS
                    ):
                        # A known source UUID with contradictory native metadata
                        # cannot fall through into the broader derivative allowlist.
                        raise _Barrier("unknown_or_unrelated_pending_operation" if row["status"] == "pending"
                                       else "target_operation_ownership_unresolved")
                    if row["status"] != "pending":
                        continue
                    owned_source = terminal_target(row)
                    derivative = row["task_type"] in _DERIVATIVE_TASKS and not row.get("mental_model_id")
                    if not (owned_source or derivative):
                        raise _Barrier("unknown_or_unrelated_pending_operation")
                for row in entries:
                    if row["status"] != "pending":
                        continue
                    identifier = row["id"]
                    current = operation_status(identifier)
                    if current == "processing":
                        raise _Barrier("native_operation_processing")
                    if current == "pending":
                        call("cancel_operation", identifier, write=True)
                        result["cancelled_operation_ids"].append(identifier)
                        current = operation_status(identifier)
                    if current not in _TERMINAL | {"not_found"}:
                        raise _Barrier("cancellation_not_terminal")
                entries = inventory()
                if any(r["status"] in {"pending", "processing"} for r in entries):
                    # The outer bounded pass may inspect newly queued maintenance.
                    return entries
                for row in entries:
                    relevant = row["id"] in target_ops or row.get("document_id") in target_set
                    if relevant and not terminal_target(row):
                        raise _Barrier("target_operation_ownership_unresolved")
                    if not terminal_target(row):
                        continue
                    identifier = row["id"]
                    current = operation_status(identifier)
                    if current in {"pending", "processing"}:
                        raise _Barrier("native_operation_changed")
                    if current != "not_found":
                        call("delete_operation", identifier, write=True)
                        result["deleted_operation_ids"].append(identifier)
                    if operation_status(identifier) != "not_found":
                        raise _Barrier("target_operation_record_remaining")
                return inventory()

            def settle_to_quiet() -> list[dict]:
                for _ in range(MAX_DRAIN_PASSES):
                    quiet()
                    counts = state()
                    entries = operations_settle()
                    if any(r["status"] in {"pending", "processing"} for r in entries):
                        continue
                    counts = state()
                    if counts["pending_operations"] == 0 and counts["processing_operations"] == 0:
                        return entries
                raise _Barrier("native_jobs_did_not_settle")

            # Readback counts are cross-checked against a complete operation view.
            entries = inventory()
            if (sum(r["status"] == "pending" for r in entries) != initial["pending_operations"]
                    or sum(r["status"] == "processing" for r in entries) != initial["processing_operations"]):
                raise _Barrier("native_inventory_changed")
            settle_to_quiet()
            for document_id in documents:
                if not verify_absent(document_id):
                    try:
                        call("delete_document", document_id, write=True)
                        result["deleted_document_ids"].append(document_id)
                    except HindsightError as exc:
                        if exc.code != "http_error" or exc.status != 404:
                            raise
                    if not verify_absent(document_id):
                        raise _Barrier("document_absence_not_verified")

            cleared = call("clear_observations", write=True)
            if (not isinstance(cleared, dict) or cleared.get("success") is not True
                    or type(cleared.get("deleted_count")) is not int or cleared["deleted_count"] < 0):
                raise _Barrier("invalid_observation_clear_ack")
            result["observations_cleared"] = cleared["deleted_count"]
            settle_to_quiet()
            final_counts = state()
            if any(final_counts[k] != 0 for k in _COUNTS):
                raise _Barrier("derived_state_not_empty")
            for document_id in documents:
                if not verify_absent(document_id):
                    raise _Barrier("document_reappeared")
            final_entries = inventory()
            if any(r["status"] in {"pending", "processing"} for r in final_entries):
                raise _Barrier("native_jobs_reappeared")
            if any(r["id"] in target_ops or r.get("document_id") in target_set for r in final_entries):
                raise _Barrier("target_operation_record_remaining")
            final_quiet_at = quiet()
            receipt = {
                "schema": 1, "mode": "no-pages-bank-reset", "change_ids": change_ids,
                "change_snapshot_sha256": snapshot_hash, "document_ids": documents,
                "counts": final_counts, "quiet_checked_at": final_quiet_at,
                "initial_quiet_checked_at": initial_quiet_at, "verified_at": timestamp(),
            }
            with store.connect(write=True) as db:
                bounded()
                recovery = db.execute("SELECT value FROM settings WHERE key='recovery_state'").fetchone()
                if recovery and recovery[0] != "ready":
                    raise _Barrier("recovery_verification_required")
                current = [dict(db.execute("SELECT * FROM changes WHERE id=?", (identifier,)).fetchone() or {}) for identifier in change_ids]
                if digest(canonical(current)) != snapshot_hash:
                    raise _Barrier("change_snapshot_changed")
                for identifier in change_ids:
                    db.execute("UPDATE changes SET applied=1 WHERE id=? AND applied=0", (identifier,))
                    db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               ("correction_receipt:" + str(identifier), canonical(receipt).decode()))
                pending = db.execute("SELECT count(*) FROM changes WHERE applied=0").fetchone()[0]
            result.update(state="reconciled" if not pending else "partial", reason=None if not pending else "new_changes_pending",
                          applied_change_ids=change_ids, current_read_barrier=bool(pending), final_counts=final_counts)
            return result
    except _Barrier as exc:
        result["reason"] = str(exc)
    except HindsightError as exc:
        result["reason"] = exc.code if re.fullmatch(r"[a-z_]{1,100}", exc.code or "") else "native_api_failed"
    except Exception:
        result["reason"] = "reconciliation_failed"
    return result
