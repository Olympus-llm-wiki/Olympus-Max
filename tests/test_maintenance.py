"""Synthetic native-state transitions, never a real Hindsight/model call."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from olympus.hindsight import HindsightError
from olympus.maintenance import reconcile_no_pages
from olympus.preservation import Store, PreservationError, timestamp


class NativeDouble:
    bank_id = "synthetic-no-pages"

    def __init__(self):
        self.ops = {}
        self.documents = set()
        self.orphan_units = set()
        self.observations = 3
        self.mental_models = 0
        self.knowledge_pages = 0
        self.calls = []
        self.keep_cancelled_pending = False
        self.keep_deleted_operation = False
        self.keep_document = False
        self.keep_observations = False
        self.enqueue_on_delete = False
        self.enqueue_on_clear = None

    def add_operation(self, *, identifier=None, document_id=None, status="completed", task_type="retain"):
        identifier = identifier or str(uuid.uuid4())
        self.ops[identifier] = {"id": identifier, "status": status, "task_type": task_type,
                                "document_id": document_id, "items_count": 1}
        return identifier

    def list_operations(self, *, status=None, limit=100, offset=0):
        self.calls.append(("list_operations", offset))
        rows = sorted((dict(r) for r in self.ops.values() if status is None or r["status"] == status), key=lambda r: r["id"])
        return {"bank_id": self.bank_id, "operations": rows[offset:offset + limit],
                "total": len(rows), "limit": limit, "offset": offset}

    def reconciliation_state(self):
        self.calls.append(("reconciliation_state", None))
        return {"observations": self.observations, "mental_models": self.mental_models,
                "knowledge_pages": self.knowledge_pages,
                "pending_operations": sum(r["status"] == "pending" for r in self.ops.values()),
                "processing_operations": sum(r["status"] == "processing" for r in self.ops.values())}

    def operation(self, identifier):
        self.calls.append(("operation", identifier))
        return {"operation_id": identifier, "status": self.ops.get(identifier, {}).get("status", "not_found")}

    def cancel_operation(self, identifier):
        self.calls.append(("cancel_operation", identifier))
        if not self.keep_cancelled_pending:
            self.ops[identifier]["status"] = "cancelled"
        return {"success": True, "operation_id": identifier}

    def delete_operation(self, identifier):
        self.calls.append(("delete_operation", identifier))
        if self.ops[identifier]["status"] not in {"completed", "failed", "cancelled"}:
            raise HindsightError("http_error", 409)
        if not self.keep_deleted_operation:
            del self.ops[identifier]
        return {"success": True, "operation_id": identifier}

    def delete_document(self, identifier):
        self.calls.append(("delete_document", identifier))
        if identifier not in self.documents:
            raise HindsightError("http_error", 404)
        if not self.keep_document:
            self.documents.remove(identifier)
        if self.enqueue_on_delete:
            for kind in ("graph_maintenance", "vector_index_maintenance"):
                self.add_operation(status="pending", task_type=kind)
        return {"success": True, "document_id": identifier, "memory_units_deleted": 1}

    def verify_deleted_document(self, identifier):
        self.calls.append(("verify_deleted_document", identifier))
        absent = identifier not in self.documents
        units_absent = absent and identifier not in self.orphan_units
        return {"document_id": identifier, "document_absent": absent,
                "memory_units_absent": units_absent, "deleted": absent and units_absent}

    def clear_observations(self):
        self.calls.append(("clear_observations", None))
        count = self.observations
        if not self.keep_observations:
            self.observations = 0
        if self.enqueue_on_clear:
            self.add_operation(status="pending", task_type=self.enqueue_on_clear)
        return {"success": True, "deleted_count": count}


def quiet_proof():
    return {"workers_stopped": True, "writers_stopped": True, "checked_at": timestamp()}


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state")
        self.old = self.capture("old", "Synthetic old text")
        self.other = self.capture("other", "Synthetic unrelated active text")
        self.client = NativeDouble()
        self.client.documents.update({self.old.version_id, self.other.version_id})
        self.client.add_operation(identifier=self.old.operation_id, document_id=self.old.version_id)
        self.client.add_operation(identifier=self.other.operation_id, document_id=self.other.version_id)

    def tearDown(self):
        self.temp.cleanup()

    def capture(self, key, text):
        return self.store.capture(source_key="synthetic:" + key, scope="one", title=key,
                                  original=text.encode(), text=text)

    def forget(self):
        return self.store.forget(self.old.source_id, "Explicit synthetic owner withdrawal")

    def run_reconciliation(self, proof=quiet_proof):
        return reconcile_no_pages(self.store, self.client, assert_quiet=proof)

    def writes(self):
        return [(name, value) for name, value in self.client.calls
                if name in {"cancel_operation", "delete_operation", "delete_document", "clear_observations"}]

    def test_complete_reconciliation_preserves_originals_tombstone_and_unrelated_data(self):
        change = self.forget()
        self.store.set_setting("maintenance", "on")
        before = self.store.read_version(self.old.version_id)["original"]
        result = self.run_reconciliation()
        self.assertEqual(result["state"], "reconciled", result)
        self.assertEqual(result["applied_change_ids"], [change])
        self.assertFalse(result["current_read_barrier"])
        self.assertEqual(self.store.read_version(self.old.version_id)["original"], before)
        self.assertEqual(self.store.receipt(self.old.version_id).memory, "forgotten")
        self.assertIn(self.other.version_id, self.client.documents)
        self.assertIn(self.other.operation_id, self.client.ops)
        self.assertNotIn(self.old.operation_id, self.client.ops)
        self.assertEqual(self.store.setting("maintenance"), "on")
        self.assertEqual(self.store.setting("recovery_state"), None)
        receipt = json.loads(self.store.setting("correction_receipt:" + str(change)))
        self.assertTrue(all(value == 0 for value in receipt["counts"].values()))
        self.assertFalse(self.store.pending_changes())
        with self.assertRaises(PreservationError):
            self.store.retry(self.old.version_id)
        with self.assertRaisesRegex(PreservationError, "source_is_forgotten"):
            self.capture("old", "Late old text")

    def test_replacement_searchability_is_a_separate_receipt(self):
        new = self.capture("old", "Synthetic replacement")
        self.client.documents.add(new.version_id)
        self.store.supersede(self.old.version_id, new.version_id, "Explicit replacement")
        result = self.run_reconciliation()
        self.assertEqual(result["state"], "reconciled", result)
        self.assertEqual(self.store.receipt(self.old.version_id).memory, "superseded")
        self.assertEqual(self.store.receipt(new.version_id).memory, "pending")
        self.assertIn(new.version_id, self.client.documents)

    def test_no_changes_does_not_clear_the_bank_or_call_callback(self):
        result = self.run_reconciliation(lambda: self.fail("quiet callback not needed for idle"))
        self.assertEqual(result["state"], "idle")
        self.assertFalse(self.client.calls)

    def test_restore_barrier_is_not_reenabled_or_cleared(self):
        self.forget()
        self.store.set_setting("recovery_state", "blocked")
        result = self.run_reconciliation()
        self.assertEqual(result["reason"], "recovery_verification_required")
        self.assertTrue(result["current_read_barrier"])
        self.assertFalse(self.client.calls)
        self.assertEqual(self.store.setting("recovery_state"), "blocked")

    def test_missing_false_stale_future_or_untyped_quiet_proof_blocks_before_writes(self):
        self.forget()
        for proof in (
            {}, {"workers_stopped": True, "writers_stopped": True},
            {**quiet_proof(), "workers_stopped": False},
            {**quiet_proof(), "writers_stopped": 1},
            {**quiet_proof(), "checked_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()},
            {**quiet_proof(), "checked_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()},
        ):
            with self.subTest(proof=proof):
                result = self.run_reconciliation(lambda: proof)
                self.assertEqual(result["state"], "blocked")
                self.assertFalse(self.writes())
                self.assertEqual(len(self.store.pending_changes()), 1)

    def test_quiet_callback_failure_does_not_expose_exception_text(self):
        self.forget()
        def fail():
            raise RuntimeError("SYNTHETIC_PRIVATE_RUNTIME_OUTPUT")
        result = self.run_reconciliation(fail)
        self.assertEqual(result["reason"], "quiet_observation_failed")
        self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result))
        self.assertFalse(self.writes())

    def test_existing_pages_or_mental_models_forbid_bankwide_reset(self):
        self.forget()
        for field in ("knowledge_pages", "mental_models"):
            setattr(self.client, field, 1)
            result = self.run_reconciliation()
            self.assertEqual(result["reason"], "knowledge_pages_require_reconciliation")
            self.assertFalse(self.writes())
            setattr(self.client, field, 0)

    def test_processing_anywhere_blocks_before_even_owned_pending_cancel(self):
        self.forget()
        self.client.ops[self.old.operation_id]["status"] = "pending"
        self.client.ops[self.other.operation_id]["status"] = "processing"
        result = self.run_reconciliation()
        self.assertEqual(result["reason"], "native_operation_processing")
        self.assertFalse(self.writes())

    def test_unrelated_unknown_or_mismatched_pending_job_is_not_cancelled(self):
        self.forget()
        for task_type in ("retain", "future_task", "knowledge_page_refresh"):
            self.client.ops[self.other.operation_id].update(status="pending", task_type=task_type)
            result = self.run_reconciliation()
            self.assertEqual(result["reason"], "unknown_or_unrelated_pending_operation")
            self.assertFalse(self.writes())
        self.client.ops[self.other.operation_id]["status"] = "completed"
        self.client.ops[self.old.operation_id].update(status="pending", document_id=self.other.version_id)
        result = self.run_reconciliation()
        self.assertEqual(result["reason"], "unknown_or_unrelated_pending_operation")
        self.assertFalse(self.writes())

    def test_owned_pending_and_new_allowlisted_maintenance_are_cancelled_without_running(self):
        self.forget()
        self.client.ops[self.old.operation_id]["status"] = "pending"
        original_maintenance = self.client.add_operation(status="pending", task_type="consolidation")
        self.client.enqueue_on_delete = True
        self.client.enqueue_on_clear = "consolidation"
        result = self.run_reconciliation()
        self.assertEqual(result["state"], "reconciled", result)
        self.assertIn(self.old.operation_id, result["cancelled_operation_ids"])
        self.assertIn(original_maintenance, result["cancelled_operation_ids"])
        self.assertNotIn(original_maintenance, result["deleted_operation_ids"])
        self.assertEqual(self.client.ops[original_maintenance]["status"], "cancelled")
        self.assertTrue(all(r["status"] not in {"pending", "processing"} for r in self.client.ops.values()))
        self.assertFalse(any(name in {"submit", "retry_operation", "reflect"} for name, _ in self.client.calls))

    def test_cancel_ack_without_terminal_state_cannot_remove_record_or_apply(self):
        self.forget()
        self.client.ops[self.old.operation_id]["status"] = "pending"
        self.client.keep_cancelled_pending = True
        result = self.run_reconciliation()
        self.assertEqual(result["reason"], "cancellation_not_terminal")
        self.assertFalse(any(name == "delete_operation" for name, _ in self.writes()))
        self.assertEqual(len(self.store.pending_changes()), 1)

    def test_terminal_delete_ack_is_not_absence(self):
        self.forget()
        self.client.keep_deleted_operation = True
        result = self.run_reconciliation()
        self.assertEqual(result["reason"], "target_operation_record_remaining")
        self.assertFalse(any(name == "delete_document" for name, _ in self.writes()))

    def test_document_delete_ack_requires_document404_and_units0(self):
        self.forget()
        self.client.keep_document = True
        result = self.run_reconciliation()
        self.assertEqual(result["reason"], "document_absence_not_verified")
        self.assertEqual(len(self.store.pending_changes()), 1)
        self.assertFalse(any(name == "clear_observations" for name, _ in self.writes()))
        self.client.keep_document = False
        self.client.documents.discard(self.old.version_id)
        self.client.orphan_units.add(self.old.version_id)
        result = self.run_reconciliation()
        self.assertEqual(result["reason"], "document_absence_not_verified")

    def test_clear_observations_ack_is_not_final_counts_proof(self):
        self.forget()
        self.client.keep_observations = True
        result = self.run_reconciliation()
        self.assertEqual(result["reason"], "derived_state_not_empty")
        self.assertEqual(len(self.store.pending_changes()), 1)

    def test_unknown_job_enqueued_by_delete_path_keeps_barrier(self):
        self.forget()
        self.client.enqueue_on_clear = "unknown_maintenance"
        result = self.run_reconciliation()
        self.assertEqual(result["reason"], "unknown_or_unrelated_pending_operation")
        unknown = [r for r in self.client.ops.values() if r["task_type"] == "unknown_maintenance"]
        self.assertEqual(unknown[0]["status"], "pending")
        self.assertEqual(len(self.store.pending_changes()), 1)

    def test_midway_failure_then_retry_is_idempotent_and_does_not_restart_workers(self):
        change = self.forget()
        def failure_after_mutation():
            if any(name == "clear_observations" for name, _ in self.client.calls):
                raise RuntimeError("SYNTHETIC_FAILED_RUNTIME_OBSERVATION")
            return quiet_proof()
        first = self.run_reconciliation(failure_after_mutation)
        self.assertEqual(first["state"], "blocked")
        self.assertEqual(len(self.store.pending_changes()), 1)
        prior_deletes = sum(name == "delete_document" for name, _ in self.client.calls)
        second = self.run_reconciliation()
        self.assertEqual(second["state"], "reconciled", second)
        self.assertEqual(second["applied_change_ids"], [change])
        self.assertEqual(sum(name == "delete_document" for name, _ in self.client.calls), prior_deletes)
        self.assertEqual(self.run_reconciliation()["state"], "idle")

    def test_full_pagination_includes_target_beyond_first_page(self):
        self.forget()
        for i in range(105):
            self.client.add_operation(identifier=str(uuid.UUID(int=i + 1)), document_id=self.other.version_id)
        result = self.run_reconciliation()
        self.assertEqual(result["state"], "reconciled", result)
        self.assertTrue(any(name == "list_operations" and offset == 100 for name, offset in self.client.calls))
        self.assertEqual(len(self.client.ops), 106)

    def test_changing_inventory_or_inventory_limit_blocks_before_writes(self):
        self.forget()
        with patch("olympus.maintenance.MAX_OPERATIONS", 1):
            result = self.run_reconciliation()
        self.assertEqual(result["reason"], "native_operation_inventory_limit")
        self.assertFalse(self.writes())

    def test_known_single_item_operation_without_document_reference_is_owned(self):
        self.forget()
        self.client.ops[self.old.operation_id]["document_id"] = None
        result = self.run_reconciliation()
        self.assertEqual(result["state"], "reconciled", result)
        self.assertIn(self.old.operation_id, result["deleted_operation_ids"])

    def test_known_pending_single_item_operation_without_document_reference_is_owned(self):
        self.forget()
        self.client.ops[self.old.operation_id].update(document_id=None, status="pending", task_type="batch_retain")
        result = self.run_reconciliation()
        self.assertEqual(result["state"], "reconciled", result)
        self.assertIn(self.old.operation_id, result["cancelled_operation_ids"])
        self.assertIn(self.old.operation_id, result["deleted_operation_ids"])

    def test_contradictory_document_reference_blocks_pending_and_terminal_operations(self):
        self.forget()
        for status in ("pending", "completed", "failed", "cancelled"):
            with self.subTest(status=status):
                self.client.ops[self.old.operation_id].update(document_id=self.other.version_id, status=status)
                result = self.run_reconciliation()
                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["reason"], "unknown_or_unrelated_pending_operation" if status == "pending"
                                 else "target_operation_ownership_unresolved")
                self.assertFalse(self.writes())

    def test_unregistered_child_reference_is_not_deleted_by_guessing(self):
        self.forget()
        self.client.add_operation(document_id=self.old.version_id, task_type="unknown_source_task")
        result = self.run_reconciliation()
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason"], "target_operation_ownership_unresolved")
        self.assertEqual(len(self.store.pending_changes()), 1)

    def test_native_single_item_retain_child_is_owned_by_exact_document(self):
        self.forget()
        child = self.client.add_operation(document_id=self.old.version_id, task_type="retain")
        result = self.run_reconciliation()
        self.assertEqual(result["state"], "reconciled")
        self.assertEqual(self.store.pending_changes(), [])

    def test_known_operation_conflicting_item_count_or_source_type_is_not_owned(self):
        self.forget()
        for status, change in (("completed", {"items_count": 2}), ("pending", {"items_count": 2}),
                               ("completed", {"task_type": "future_source_job"}),
                               ("pending", {"task_type": "consolidation"})):
            self.client.ops[self.old.operation_id].update(document_id=None, status=status, items_count=1, task_type="retain")
            self.client.ops[self.old.operation_id].update(change)
            result = self.run_reconciliation()
            self.assertEqual(result["state"], "blocked")
            self.assertFalse(self.writes())

    def test_only_snapshot_change_ids_are_marked_applied(self):
        initial = self.forget()
        inserted = []
        def concurrent_change():
            if not inserted and any(name == "clear_observations" for name, _ in self.client.calls):
                # Simulates another restored/administrative ledger write without
                # recursively acquiring Store.exclusive from this callback.
                with self.store.connect(write=True) as db:
                    db.execute("UPDATE sources SET forgotten_at=? WHERE id=?", (timestamp(), self.other.source_id))
                    db.execute("UPDATE versions SET active=0 WHERE source_id=?", (self.other.source_id,))
                    db.execute("UPDATE delivery SET state='forgotten' WHERE version_id=?", (self.other.version_id,))
                    inserted.append(db.execute("INSERT INTO changes(kind,source_id,reason,created_at) VALUES('forget',?,?,?)",
                                               (self.other.source_id, "new explicit synthetic change", timestamp())).lastrowid)
            return quiet_proof()
        result = self.run_reconciliation(concurrent_change)
        self.assertEqual(result["state"], "partial", result)
        self.assertEqual(result["applied_change_ids"], [initial])
        self.assertEqual([c["id"] for c in self.store.pending_changes()], inserted)
        self.assertTrue(result["current_read_barrier"])
        self.assertIn(self.other.version_id, self.client.documents)

    def test_new_recovery_barrier_before_final_commit_remains(self):
        self.forget()
        def restored_while_quiet():
            if any(name == "clear_observations" for name, _ in self.client.calls):
                self.store.set_setting("recovery_state", "blocked")
            return quiet_proof()
        result = self.run_reconciliation(restored_while_quiet)
        self.assertEqual(result["reason"], "recovery_verification_required")
        self.assertEqual(len(self.store.pending_changes()), 1)

    def test_native_exception_is_safe_and_keeps_barrier(self):
        self.forget()
        with patch.object(self.client, "verify_deleted_document", side_effect=RuntimeError("SYNTHETIC_PRIVATE_NATIVE_BODY")):
            result = self.run_reconciliation()
        self.assertEqual(result["reason"], "reconciliation_failed")
        self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result))
        self.assertEqual(len(self.store.pending_changes()), 1)

    def test_runtime_proof_can_use_unix_time_without_copying_extra_private_fields(self):
        self.forget()
        import time
        result = self.run_reconciliation(lambda: {
            "workers_stopped": True, "writers_stopped": True, "checked_at": time.time(),
            "unrelated_details": "SYNTHETIC_PRIVATE_RUNTIME_DETAILS",
        })
        self.assertEqual(result["state"], "reconciled", result)
        self.assertNotIn("SYNTHETIC_PRIVATE_RUNTIME_DETAILS", json.dumps(result))
        with self.store.connect() as db:
            receipts = [r[0] for r in db.execute("SELECT value FROM settings WHERE key LIKE 'correction_receipt:%'")]
        self.assertTrue(all("SYNTHETIC_PRIVATE_RUNTIME_DETAILS" not in value for value in receipts))

    def test_callback_is_observed_under_actual_writer_lock(self):
        self.forget()
        import fcntl
        import os
        def proof_under_lock():
            descriptor = os.open(self.store.root / "writer.lock", os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)
            return quiet_proof()
        self.assertEqual(self.run_reconciliation(proof_under_lock)["state"], "reconciled")

    def test_processing_that_appears_during_cancel_is_never_deleted(self):
        self.forget()
        self.client.ops[self.old.operation_id]["status"] = "pending"
        original = self.client.cancel_operation
        def cancel(identifier):
            result = original(identifier)
            self.client.ops[identifier]["status"] = "processing"
            return result
        with patch.object(self.client, "cancel_operation", side_effect=cancel):
            result = self.run_reconciliation()
        self.assertEqual(result["state"], "blocked")
        self.assertFalse(any(name == "delete_operation" for name, _ in self.writes()))

    def test_recovery_change_inside_quiet_observation_stops_the_next_mutation(self):
        self.forget()
        count = 0
        def state_changes():
            nonlocal count
            count += 1
            if count == 3:
                self.store.set_setting("recovery_state", "blocked")
            return quiet_proof()
        result = self.run_reconciliation(state_changes)
        self.assertEqual(result["reason"], "recovery_verification_required")
        self.assertFalse(self.writes())

    def test_continuously_enqueued_maintenance_is_bounded_and_keeps_barrier(self):
        self.forget()
        self.client.add_operation(status="pending", task_type="consolidation")
        original = self.client.cancel_operation
        def always_more(identifier):
            result = original(identifier)
            self.client.add_operation(status="pending", task_type="consolidation")
            return result
        with patch.object(self.client, "cancel_operation", side_effect=always_more):
            result = self.run_reconciliation()
        self.assertEqual(result["reason"], "native_jobs_did_not_settle")
        self.assertEqual(len(result["cancelled_operation_ids"]), 4)
        self.assertEqual(len(self.store.pending_changes()), 1)

    def test_document_reappearing_after_deletion_is_detected_by_final_readback(self):
        self.forget()
        original = self.client.verify_deleted_document
        def reappears(identifier):
            if any(name == "clear_observations" for name, _ in self.client.calls):
                self.client.documents.add(identifier)
            return original(identifier)
        with patch.object(self.client, "verify_deleted_document", side_effect=reappears):
            result = self.run_reconciliation()
        self.assertEqual(result["reason"], "document_reappeared")
        self.assertEqual(len(self.store.pending_changes()), 1)


if __name__ == "__main__":
    unittest.main()
