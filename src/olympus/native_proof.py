"""Durable evidence of a native outcome, separate from stored source bytes."""
from __future__ import annotations

import json
import copy
import time

from .preservation import Store, PreservationError, canonical, digest, timestamp


def profile_fingerprint(strategy: str | None = None, *, config: dict | None = None) -> str:
    return digest(canonical({"schema": 1, "strategy": strategy, "config": config}))


def completion_error(operation: dict) -> str | None:
    """A positive unit count is not a substitute for this terminal evidence."""
    if operation.get("status") != "completed":
        return "native_completion_unproven"
    errors = operation.get("extraction_errors_count")
    if type(errors) is not int or errors < 0:
        return "native_completion_counters_missing"
    if errors:
        return "native_extraction_partial"
    children = operation.get("child_operations")
    if children is not None:
        if not children or any(child.get("status") != "completed" for child in children):
            return "native_children_incomplete"
        expected = operation.get("num_sub_batches")
        if expected is not None and expected != len(children):
            return "native_children_incomplete"
    return None


class CompletionProofs:
    """Schema is additive; construction is an explicit write/bootstrap boundary."""

    def __init__(self, store: Store):
        self.store = store
        with store.exclusive(), store.connect(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS native_completion_receipts (
                operation_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
                version_id TEXT NOT NULL REFERENCES versions(id), document_id TEXT NOT NULL,
                text_sha256 TEXT NOT NULL, profile_fingerprint TEXT NOT NULL,
                proof_json TEXT NOT NULL, proof_sha256 TEXT NOT NULL, observed_at TEXT NOT NULL
            )""")
            db.execute('CREATE INDEX IF NOT EXISTS native_completion_document ON native_completion_receipts(document_id)')

    def record(self, *, version_id: str, document_id: str, operation_id: str,
               text_sha256: str, profile: str, operation: dict, verification: dict) -> dict:
        error = completion_error(operation)
        if error:
            raise PreservationError(error)
        if (operation.get("operation_id", operation_id) != operation_id
                or operation.get("document_id", document_id) != document_id):
            raise PreservationError("native_completion_identity_mismatch")
        if not verification.get("searchable") or not verification.get("text_matches"):
            raise PreservationError("native_completion_readback_failed")
        projection = verification.get('native_text_projection')
        if projection is not None:
            from .native_text_projection import validate_projection
            validate_projection(projection, text_sha256)
            if (verification.get('text_sha256') != projection['native_sha256']
                    or verification.get('expected_text_sha256') != text_sha256
                    or verification.get('expected_native_text_sha256') != projection['native_sha256']):
                raise PreservationError('native_completion_readback_failed')
        proof = {"schema": 1, "version_id": version_id, "document_id": document_id,
                 "operation_id": operation_id, "text_sha256": text_sha256,
                 "profile_fingerprint": profile, "observed_at": timestamp(),
                 "profile_evidence": "representation_manifest" if document_id.startswith("olr-") else "request_only",
                 "native": {k: operation[k] for k in (
                     "status", "extraction_errors_count", "unit_ids_count", "completed_at",
                     "child_operations", "num_sub_batches") if k in operation},
                 "readback": {"text_matches": True, "memory_unit_count": verification["memory_unit_count"]}}
        if projection is not None:
            proof['readback'].update(native_text_projection=projection,
                                     native_text_sha256=verification['text_sha256'],
                                     canonical_text_matches=verification['text_sha256'] == text_sha256)
        encoded = canonical(proof)
        with self.store.connect(write=True) as db:
            previous = db.execute("SELECT version_id,document_id,text_sha256,profile_fingerprint FROM native_completion_receipts WHERE operation_id=?",
                                  (operation_id,)).fetchone()
            if previous and tuple(previous) != (version_id, document_id, text_sha256, profile):
                raise PreservationError("native_completion_identity_mismatch")
            db.execute("""INSERT INTO native_completion_receipts VALUES(?,1,?,?,?,?,?,?,?)
                ON CONFLICT(operation_id) DO NOTHING""",
                (operation_id, version_id, document_id, text_sha256, profile,
                 encoded.decode(), digest(encoded), proof["observed_at"]))
        return proof

    def verified(self, *, version_id: str, document_id: str, operation_id: str,
                 text_sha256: str, profile: str) -> dict | None:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM native_completion_receipts WHERE operation_id=?", (operation_id,)).fetchone()
        return self.verify_row(row, version_id=version_id, document_id=document_id,
                               operation_id=operation_id, text_sha256=text_sha256, profile=profile)

    @staticmethod
    def verify_row(row, *, version_id: str, document_id: str, operation_id: str,
                   text_sha256: str, profile: str) -> dict | None:
        """Verify a previously read row without another database query."""
        if row is None:
            return None
        if (row["schema_version"] != 1 or row["version_id"] != version_id or row["document_id"] != document_id
                or row["text_sha256"] != text_sha256 or row["profile_fingerprint"] != profile
                or digest(row["proof_json"].encode()) != row["proof_sha256"]):
            raise PreservationError("native_completion_receipt_mismatch")
        try:
            proof = json.loads(row["proof_json"])
        except (ValueError, TypeError):
            raise PreservationError("native_completion_receipt_invalid") from None
        if not isinstance(proof, dict) or not isinstance(proof.get("native"), dict) or not isinstance(proof.get("readback"), dict):
            raise PreservationError("native_completion_receipt_invalid")
        expected = {"version_id": version_id, "document_id": document_id, "operation_id": operation_id,
                    "text_sha256": text_sha256, "profile_fingerprint": profile}
        if any(proof.get(key) != value for key, value in expected.items()):
            raise PreservationError("native_completion_receipt_mismatch")
        if completion_error(proof["native"]):
            raise PreservationError("native_completion_receipt_invalid")
        projection = proof['readback'].get('native_text_projection')
        if projection is not None:
            from .native_text_projection import validate_projection
            validate_projection(projection, text_sha256)
            if (proof['readback'].get('native_text_sha256') != projection['native_sha256']
                    or proof['readback'].get('canonical_text_matches') != (projection['native_sha256'] == text_sha256)):
                raise PreservationError('native_projection_proof_invalid')
        return proof


def completion_readiness(store: Store, *, version_id: str, document_id: str,
                         operation_id: str, text_sha256: str, profile: str) -> dict:
    """Read-only status; old searchable rows are not retroactively certified."""
    with store.connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_completion_receipts'").fetchone()
    if not exists:
        return {"state": "legacy_document_proof_only", "terminal_receipt": False,
                "profile_evidence": "unknown", "enrichment_verified": False}
    reader = object.__new__(CompletionProofs)
    reader.store = store
    try:
        proof = reader.verified(version_id=version_id, document_id=document_id, operation_id=operation_id,
                                text_sha256=text_sha256, profile=profile)
    except PreservationError as exc:
        return {"state": "unknown", "terminal_receipt": False, "reason": str(exc), "enrichment_verified": False}
    if proof is None:
        return {"state": "legacy_document_proof_only", "terminal_receipt": False,
                "profile_evidence": "unknown", "enrichment_verified": False}
    return {"state": "native_complete", "terminal_receipt": True,
            "profile_evidence": proof.get("profile_evidence", "request_only"),
            "observed_at": proof["observed_at"], "enrichment_verified": False}


def audit_existing_completion(store: Store, client, *, limit: int = 10, after: str = "",
                              persist: bool = False, max_seconds: float = 45) -> dict:
    """Bounded GET/readback audit; never retries or submits model work.

    `persist` saves only freshly observed terminal evidence. Delivery states,
    original versions and unknown historical profiles are left unchanged.
    """
    if type(limit) is not int or not 1 <= limit <= 20 or not 0 < max_seconds <= 60:
        raise PreservationError("invalid_completion_audit_budget")
    deadline = time.monotonic() + max_seconds
    adapter = copy.copy(client)
    proofs = CompletionProofs(store) if persist else None
    with store.connect() as db:
        has_parts = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_representations'").fetchone()
        legacy = ("AND NOT EXISTS (SELECT 1 FROM native_representations r WHERE r.version_id=v.id AND r.kind='native_index' AND r.published=1 AND r.state='complete')"
                  if has_parts else "")
        rows = [dict(row) for row in db.execute(f"""SELECT d.version_id,d.operation_id FROM delivery d
            JOIN versions v ON v.id=d.version_id JOIN sources s ON s.id=v.source_id
            WHERE d.state='searchable' AND v.active=1 AND s.forgotten_at IS NULL AND d.version_id>?
            {legacy} ORDER BY d.version_id LIMIT ?""", (after, limit))]
    results = []
    cursor = after

    def read(method, *args, **kwargs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PreservationError("completion_audit_deadline")
        if hasattr(adapter, "timeout"):
            adapter.timeout = min(getattr(client, "timeout"), remaining)
        return getattr(adapter, method)(*args, **kwargs)

    for row in rows:
        if time.monotonic() >= deadline:
            break
        version = store.read_manifest(row["version_id"])
        binding = {"version_id": row["version_id"], "document_id": row["version_id"],
                   "operation_id": row["operation_id"], "text_sha256": version["text_sha256"],
                   "profile": profile_fingerprint(version["metadata"].get("retain_strategy"))}
        entry = {"version_id": row["version_id"], "operation_id": row["operation_id"],
                 "profile_evidence": "request_only", "enrichment_verified": False}
        try:
            operation = read("operation", row["operation_id"])
            error = completion_error(operation)
            if error:
                entry.update(state="partial" if error == "native_extraction_partial" else "unknown", reason=error)
            else:
                text = store.read_text_version(row['version_id'])['text']
                verification = read("verify_document", row["version_id"], version["text_sha256"], expected_text=text)
                if not verification.get("searchable"):
                    entry.update(state="unknown", reason="native_completion_readback_failed")
                else:
                    entry.update(state="native_complete", extraction_errors_count=0,
                                 memory_unit_count=verification["memory_unit_count"], receipt_persisted=False)
                    if proofs is not None:
                        with store.exclusive():
                            with store.connect() as db:
                                active = db.execute("SELECT active FROM versions WHERE id=?", (row["version_id"],)).fetchone()
                            if active is None or not active[0]:
                                raise PreservationError("version_not_active")
                            proofs.record(**binding, operation=operation, verification=verification)
                        entry["receipt_persisted"] = True
        except Exception as exc:
            code = str(exc) if isinstance(exc, PreservationError) else getattr(exc, "code", "completion_audit_failed")
            entry.update(state="unknown", reason=code if isinstance(code, str) and code.replace("_", "").isalnum() else "completion_audit_failed")
        results.append(entry)
        cursor = row["version_id"]
    return {"results": results, "next_after": cursor, "batch_exhausted": len(results) == limit,
            "deadline_reached": time.monotonic() >= deadline, "native_writes": False,
            "delivery_states_changed": False, "scope": "legacy_bindings"}


def observe_native_progress(store: Store, client, version_id: str, operation_id: str, operation: dict) -> dict:
    """Persist typed progress, including a single native child behind its parent.

    next_retry_at is a native schedule, not proof of a provider quota reason.
    Storing 10/10 may describe one sub-batch, never all source coverage.
    """
    current = operation
    children = operation.get("child_operations") or []
    if len(children) == 1:
        current = client.operation(children[0]["operation_id"])
    allowed = {"operation_id", "status", "retry_count", "created_at", "updated_at", "completed_at", "next_retry_at",
               "progress", "extraction_errors_count", "unit_ids_count", "num_sub_batches"}
    observation = {"schema": 1, "observed_at": timestamp(), "operation_id": operation_id,
                   "parent_status": operation["status"], "current": {k: v for k, v in current.items() if k in allowed},
                   "progress_scope": "native_operation_or_current_sub_batch", "full_source_coverage": False}
    store.set_setting("native_progress:" + version_id, canonical(observation).decode())
    return observation
