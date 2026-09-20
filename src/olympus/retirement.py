"""Bounded retirement of obsolete derived documents after replacement readback.

This does not forget a canonical source, clear a bank, or create model work.
Only recorded, terminal source operations and obsolete document IDs are removed.
"""
from __future__ import annotations

import copy
import json
import time

from .preservation import Store, PreservationError, canonical, digest, timestamp
from .representations import Representations


def retire_obsolete(store: Store, client, version_id: str, *, kind: str = "native_index",
                    limit: int = 10, max_seconds: float = 45) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100 or not 0 < max_seconds <= 60:
        raise PreservationError("invalid_retirement_budget")
    reps = Representations(store)
    with store.exclusive(), store.connect(write=True) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS native_retirements (
            replacement_id TEXT NOT NULL REFERENCES native_representations(id),
            document_id TEXT NOT NULL, phase TEXT NOT NULL, schema_version INTEGER NOT NULL,
            state TEXT NOT NULL, proof_json TEXT NOT NULL, proof_sha256 TEXT NOT NULL, checked_at REAL NOT NULL,
            PRIMARY KEY(replacement_id,document_id,phase)
        )""")
        ready = db.execute("""SELECT id FROM native_representations WHERE version_id=? AND kind=?
            AND state='complete' AND published=1""", (version_id, kind)).fetchall()
    if len(ready) != 1:
        return {"state": "blocked", "reason": "retirement_requires_complete_replacement", "retired": []}
    replacement = reps.get(ready[0][0])
    # Validate every durable part receipt before remote destructive work.
    try:
        reps.complete(replacement["id"])
    except PreservationError as exc:
        return {"state": "blocked", "reason": str(exc), "retired": []}
    deadline = time.monotonic() + max_seconds
    adapter = copy.copy(client)
    remaining = limit
    retired = []

    def gate():
        with store.connect() as db:
            row = db.execute("""SELECT r.state,r.published,v.active,s.forgotten_at FROM native_representations r
                JOIN versions v ON v.id=r.version_id JOIN sources s ON s.id=v.source_id WHERE r.id=?""", (replacement["id"],)).fetchone()
            controls = dict(db.execute("SELECT key,value FROM settings WHERE key IN ('recovery_state','maintenance')"))
        if controls.get("recovery_state", "ready") != "ready" or controls.get("maintenance", "off") != "off":
            raise PreservationError("retirement_recovery_or_maintenance")
        if not row or row["state"] != "complete" or not row["published"] or not row["active"] or row["forgotten_at"]:
            raise PreservationError("retirement_replacement_changed")

    def call(method, *args, **kwargs):
        left = deadline - time.monotonic()
        if left <= 0:
            raise PreservationError("retirement_deadline")
        if hasattr(adapter, "timeout"):
            adapter.timeout = min(getattr(client, "timeout"), left)
        return getattr(adapter, method)(*args, **kwargs)

    def recorded(doc, phase, *, fresh=False):
        with store.connect() as db:
            row = db.execute("SELECT * FROM native_retirements WHERE replacement_id=? AND document_id=? AND phase=?",
                             (replacement["id"], doc, phase)).fetchone()
        if not row:
            return False
        if row["schema_version"] != 1 or digest(row["proof_json"].encode()) != row["proof_sha256"]:
            raise PreservationError("retirement_receipt_invalid")
        try:
            proof = json.loads(row["proof_json"])
        except (ValueError, TypeError):
            raise PreservationError("retirement_receipt_invalid") from None
        if proof.get("manifest_sha256", proof.get("replacement_manifest_sha256")) != replacement["manifest_sha256"]:
            raise PreservationError("retirement_receipt_invalid")
        return bool(row["state"] == "complete" and (not fresh or row["checked_at"] >= time.time() - 3600))

    def record(doc, phase, proof):
        with store.exclusive():
            gate()
            with store.connect(write=True) as db:
                encoded = canonical(proof)
                db.execute("""INSERT INTO native_retirements VALUES(?,?,?,1,'complete',?,?,?)
                    ON CONFLICT(replacement_id,document_id,phase) DO UPDATE SET
                    proof_json=excluded.proof_json,proof_sha256=excluded.proof_sha256,checked_at=excluded.checked_at,state='complete'""",
                    (replacement["id"], doc, phase, encoded.decode(), digest(encoded), time.time()))

    try:
        gate()
        for part in replacement["parts"]:
            if not part["requires_native"] or recorded(part["document_id"], "replacement_readback", fresh=True):
                continue
            if remaining <= 0:
                return {"state": "verifying_replacement", "retired": retired, "replacement_id": replacement["id"]}
            proof = call("verify_document", part["document_id"], part["text_sha256"], expected_text=reps.read_part(part))
            remaining -= 1
            if not proof.get("searchable") or not proof.get("text_matches"):
                return {"state": "blocked", "reason": "replacement_readback_failed", "retired": retired}
            record(part["document_id"], "replacement_readback", {"text_sha256": part["text_sha256"],
                "manifest_sha256": replacement["manifest_sha256"], "memory_unit_count": proof["memory_unit_count"]})

        with store.connect() as db:
            old_ids = [r[0] for r in db.execute("""SELECT id FROM native_representations WHERE version_id=? AND kind=?
                AND id!=? AND published=0 AND created_at<?""",
                (version_id, kind, replacement["id"], replacement["created_at"]))]
            original = db.execute("SELECT operation_id,attempts FROM delivery WHERE version_id=?", (version_id,)).fetchone()
            legacy_proof = db.execute("SELECT 1 FROM native_completion_receipts WHERE version_id=? AND document_id=?", (version_id, version_id)).fetchone()
        older = [reps.get(identifier) for identifier in old_ids]
        targets = [{"document_id": p["document_id"], "operation_id": p["operation_id"]}
                   for plan in older for p in plan["parts"] if p["requires_native"] and (p["attempts"] > 0 or p["proof_json"])]
        if kind == "native_index" and (original["attempts"] > 0 or legacy_proof):
            targets.append({"document_id": version_id, "operation_id": original["operation_id"]})
        live_ids = {p["document_id"] for p in replacement["parts"]}
        if any(t["document_id"] in live_ids for t in targets):
            raise PreservationError("retirement_target_is_replacement")

        for target in targets:
            doc, operation_id = target["document_id"], target["operation_id"]
            if recorded(doc, "obsolete_retired"):
                continue
            if remaining <= 0:
                return {"state": "pending", "retired": retired, "replacement_id": replacement["id"], "targets_total": len(targets)}
            with store.exclusive():
                gate()
                operation = call("operation", operation_id)
                if operation.get("status") in {"pending", "processing"}:
                    return {"state": "waiting", "reason": "obsolete_operation_inflight", "retired": retired}
                if operation.get("document_id") not in (None, doc):
                    raise PreservationError("obsolete_operation_identity_mismatch")
                children = operation.get("child_operations", [])
                if any(child.get("status") not in {"completed", "failed", "cancelled"} for child in children):
                    return {"state": "waiting", "reason": "obsolete_operation_inflight", "retired": retired}
                identifiers = [child["operation_id"] for child in children] + [operation_id]
                # Lost parent history requires an inventory: an orphan child
                # must not recreate the document after its retirement.
                if operation.get("status") == "not_found":
                    offset = 0
                    total = None
                    while total is None or offset < total:
                        page = call("list_operations", limit=100, offset=offset)
                        if total is not None and total != page["total"] or page["total"] > 100000:
                            raise PreservationError("retirement_inventory_changed")
                        total = page["total"]
                        rows = page["operations"]
                        if not rows and offset < total:
                            raise PreservationError("retirement_inventory_incomplete")
                        for row in rows:
                            if row.get("document_id") == doc:
                                if row.get("task_type") not in {"retain", "batch_retain"} or row.get("items_count") != 1:
                                    raise PreservationError("obsolete_operation_identity_mismatch")
                                if row["status"] in {"pending", "processing"}:
                                    return {"state": "waiting", "reason": "obsolete_operation_inflight", "retired": retired}
                                identifiers.append(row["id"])
                            elif (row.get("document_id") is None and row.get("task_type") in {"retain", "batch_retain"}
                                  and row["status"] in {"pending", "processing"}):
                                raise PreservationError("retirement_unknown_source_operation")
                        offset += len(rows)
                deleted_operations = []
                for identifier in dict.fromkeys(identifiers):
                    current = call("operation", identifier)
                    if current.get("document_id") not in (None, doc):
                        raise PreservationError("obsolete_operation_identity_mismatch")
                    if current.get("status") == "not_found":
                        continue
                    if current.get("status") not in {"completed", "failed", "cancelled"}:
                        raise PreservationError("obsolete_operation_inflight")
                    call("delete_operation", identifier)
                    if call("operation", identifier).get("status") != "not_found":
                        raise PreservationError("obsolete_operation_not_deleted")
                    deleted_operations.append(identifier)
                absence = call("verify_deleted_document", doc)
                if not absence.get("deleted"):
                    call("delete_document", doc)
                    absence = call("verify_deleted_document", doc)
                if not all(absence.get(key) is True for key in ("document_absent", "memory_units_absent", "deleted")):
                    raise PreservationError("obsolete_document_absence_unproven")
                record(doc, "obsolete_retired", {"document_id": doc, "operation_id": operation_id,
                    "deleted_operations": deleted_operations, "verified_at": timestamp(),
                    "replacement_manifest_sha256": replacement["manifest_sha256"]})
            retired.append(doc)
            remaining -= 1
        for plan in older:
            if all(recorded(p["document_id"], "obsolete_retired") for p in plan["parts"]
                   if p["requires_native"] and (p["attempts"] > 0 or p["proof_json"])):
                with store.exclusive(), store.connect(write=True) as db:
                    gate()
                    db.execute("UPDATE native_representations SET state='retired',enabled=0,published=0 WHERE id=? AND published=0", (plan["id"],))
        return {"state": "complete", "retired": retired, "replacement_id": replacement["id"], "targets_total": len(targets), "canonical_forgotten": False}
    except Exception as exc:
        code = str(exc) if isinstance(exc, PreservationError) else getattr(exc, "code", "retirement_failed")
        return {"state": "pending" if code == "retirement_deadline" else "blocked",
                "reason": code if isinstance(code, str) and code.replace("_", "").isalnum() else "retirement_failed",
                "retired": retired}
