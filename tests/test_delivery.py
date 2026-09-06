from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from olympus.delivery import grant_budget, run_one, recall_active
from olympus.preservation import PreservationError, Store, digest


class TransportFailure(Exception):
    code = "network_error"
    status = None


class NativeContractDouble:
    def __init__(self):
        self.ops = {}
        self.documents = {}
        self.submits = []
        self.lose_response = False
        self.recall_data = {"results": []}

    def operation(self, operation_id):
        return {"status": self.ops.get(operation_id, "not_found")}

    def verify_document(self, document_id, expected):
        row = self.documents.get(document_id)
        if row is None:
            return {"exists": False, "searchable": False}
        text, count = row
        matches = digest(text.encode()) == expected
        return {"exists": True, "text_matches": matches, "memory_unit_count": count, "searchable": matches and count > 0}

    def submit(self, **kwargs):
        self.submits.append(kwargs)
        self.ops[kwargs["operation_id"]] = "pending"
        if self.lose_response:
            raise TransportFailure()
        return {"operation_id": kwargs["operation_id"]}

    def recall(self, query, tags):
        return self.recall_data


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state")
        self.receipt = self.store.capture(source_key="source-one", scope="scope-one", title="Synthetic", original=b"input", text="BLUE-731")
        self.client = NativeContractDouble()

    def tearDown(self):
        self.temp.cleanup()

    def due(self):
        with self.store.connect(write=True) as db:
            db.execute("UPDATE delivery SET next_attempt=0")

    def test_unknown_budget_keeps_complete_input_and_does_not_submit(self):
        self.assertEqual(run_one(self.store, self.client)["state"], "awaiting_budget")
        self.assertEqual(self.client.submits, [])
        self.assertEqual(self.store.read_version(self.receipt.version_id)["original"], b"input")

    def test_expired_budget_cannot_admit_new_work(self):
        grant_budget(self.store, 1)
        self.store.set_setting("budget_expires", "1")
        self.assertEqual(run_one(self.store, self.client)["state"], "awaiting_budget")

    def test_explicit_large_grant_submits_complete_text_under_same_operation(self):
        store = Store(Path(self.temp.name) / "large-state")
        text = "START\n" + "Полный текст 🙂\\\n" * 40000 + "\nEND-731"
        self.assertGreater(len(text), 488547)
        receipt = store.capture(source_key="large", scope="scope-one", title="Large synthetic",
                                original=text.encode("utf-8"), text=text)
        grant_budget(store, 1, max_text_chars=1_000_000)
        result = run_one(store, self.client)
        self.assertEqual(result["state"], "submitted")
        self.assertEqual(len(self.client.submits), 1)
        submitted = self.client.submits[0]
        self.assertEqual(submitted["text"], text)
        self.assertEqual(submitted["document_id"], receipt.version_id)
        self.assertEqual(submitted["operation_id"], receipt.operation_id)
        self.assertEqual(store.setting("budget_remaining"), "0")
        self.client.ops[receipt.operation_id] = "completed"
        self.client.documents[receipt.version_id] = (text, 12)
        with store.connect(write=True) as db:
            db.execute("UPDATE delivery SET next_attempt=0")
        self.assertEqual(run_one(store, self.client)["state"], "searchable")
        self.assertEqual(len(self.client.submits), 1)
        self.assertEqual(store.read_version(receipt.version_id)["original"], text.encode("utf-8"))

    def test_oversized_document_does_not_starve_later_small_document(self):
        small = self.store.capture(source_key="small", scope="scope-one", title="Small synthetic",
                                   original=b"fit", text="fit")
        grant_budget(self.store, 1, max_text_chars=3)
        result = run_one(self.store, self.client)
        self.assertEqual(result, {"version_id": self.receipt.version_id, "state": "pending",
                                  "error": "text_exceeds_budget", "text_chars": 8, "max_text_chars": 3})
        self.assertEqual(self.store.setting("budget_remaining"), "1")
        self.assertEqual(self.client.submits, [])
        with self.store.connect() as db:
            row = db.execute("SELECT state,last_error,attempts FROM delivery WHERE version_id=?",
                             (self.receipt.version_id,)).fetchone()
        self.assertEqual(tuple(row), ("pending", "text_exceeds_budget", 0))
        self.assertEqual(run_one(self.store, self.client)["state"], "submitted")
        self.assertEqual(self.client.submits[0]["document_id"], small.version_id)
        self.assertEqual(self.store.setting("budget_remaining"), "0")
        # Raising the explicit grant retries the preserved large job with its UUID.
        grant_budget(self.store, 1, max_text_chars=8)
        self.due()
        self.assertEqual(run_one(self.store, self.client)["state"], "submitted")
        self.assertEqual(self.client.submits[1]["operation_id"], self.receipt.operation_id)
        self.assertEqual(self.client.submits[1]["text"], "BLUE-731")

    def test_grant_default_and_upper_bound_remain_explicit(self):
        grant_budget(self.store, 1)
        self.assertEqual(self.store.setting("budget_max_text_chars"), "50000")
        with self.assertRaisesRegex(PreservationError, "invalid_pilot_budget"):
            grant_budget(self.store, 1, max_text_chars=1_000_001)

    def test_delivery_cycle_continues_after_oversized_document(self):
        from olympus.cli import _deliver

        small = self.store.capture(source_key="small", scope="scope-one", title="Small synthetic",
                                   original=b"fit", text="fit")
        grant_budget(self.store, 1, max_text_chars=3)
        with patch("olympus.runtime_control.RuntimeControl.supervise", return_value={}), \
                patch("olympus.backup_job.maybe_backup", return_value={"state": "not_due"}):
            result = _deliver(self.store, self.client, limit=2)
        self.assertEqual(len(result["delivery"]), 2)
        self.assertEqual(result["delivery"][0]["version_id"], self.receipt.version_id)
        self.assertEqual(result["delivery"][0]["error"], "text_exceeds_budget")
        self.assertEqual(result["delivery"][1], {"version_id": small.version_id, "state": "submitted"})
        self.assertEqual(len(self.client.submits), 1)
        self.assertEqual(self.client.submits[0]["operation_id"], small.operation_id)

    def test_submission_then_native_completion_requires_readback(self):
        grant_budget(self.store, 1)
        self.assertEqual(run_one(self.store, self.client)["state"], "submitted")
        self.assertEqual(self.store.receipt(self.receipt.version_id).memory, "submitted")
        self.client.ops[self.receipt.operation_id] = "completed"
        self.client.documents[self.receipt.version_id] = ("BLUE-731", 1)
        self.due()
        self.assertEqual(run_one(self.store, self.client)["state"], "searchable")

    def test_lost_response_resumes_existing_operation(self):
        grant_budget(self.store, 1)
        self.client.lose_response = True
        self.assertEqual(run_one(self.store, self.client)["state"], "pending")
        self.due()
        self.assertEqual(run_one(self.store, self.client)["state"], "submitted")
        self.assertEqual(len(self.client.submits), 1)
        self.assertEqual(self.client.submits[0]["operation_id"], self.receipt.operation_id)

    def test_expired_operation_history_uses_exact_document_proof(self):
        self.client.documents[self.receipt.version_id] = ("BLUE-731", 2)
        self.assertEqual(run_one(self.store, self.client)["state"], "searchable")
        self.assertEqual(self.client.submits, [])

    def test_completed_zero_units_is_visible_failure(self):
        self.client.ops[self.receipt.operation_id] = "completed"
        self.client.documents[self.receipt.version_id] = ("BLUE-731", 0)
        self.assertEqual(run_one(self.store, self.client)["state"], "empty")
        self.assertEqual(self.store.active_documents("scope-one"), set())

    def test_wrong_remote_content_is_not_overwritten(self):
        self.client.documents[self.receipt.version_id] = ("OTHER", 2)
        grant_budget(self.store, 1)
        self.assertEqual(run_one(self.store, self.client)["state"], "blocked")
        self.assertEqual(self.client.submits, [])

    def test_terminal_failure_does_not_generate_a_fresh_operation(self):
        self.client.ops[self.receipt.operation_id] = "failed"
        self.assertEqual(run_one(self.store, self.client)["state"], "failed")
        self.assertEqual(self.client.submits, [])

    def test_corrupt_local_input_never_reaches_network_write(self):
        (self.store.versions / self.receipt.version_id / "text.txt").write_text("corrupted")
        grant_budget(self.store, 1)
        self.assertEqual(run_one(self.store, self.client)["state"], "blocked")
        self.assertEqual(self.client.submits, [])

    def test_scope_filter_excludes_unregistered_and_derived_results(self):
        self.client.documents[self.receipt.version_id] = ("BLUE-731", 1)
        run_one(self.store, self.client)
        self.client.recall_data = {"results": [{"document_id": self.receipt.version_id, "text": "yes"},
                                               {"document_id": "legacy", "text": "no"}, {"text": "unattributed observation"}]}
        result = recall_active(self.store, self.client, "code?", "scope-one")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["text"], "yes")

    def test_native_chunks_are_filtered_via_allowed_units(self):
        self.client.documents[self.receipt.version_id] = ("BLUE-731", 1)
        run_one(self.store, self.client)
        self.client.recall_data = {
            "results": [{"document_id": self.receipt.version_id, "chunk_id": "good"},
                        {"document_id": "legacy", "chunk_id": "other"}],
            "chunks": {"good": {"id": "good", "text": "full exact source", "chunk_index": 0},
                       "other": {"id": "other", "text": "forbidden", "chunk_index": 0}},
        }
        result = recall_active(self.store, self.client, "code?", "scope-one")
        self.assertEqual(list(result["chunks"]), ["good"])

    def test_forget_during_recall_blocks_the_result(self):
        self.client.documents[self.receipt.version_id] = ("BLUE-731", 1)
        run_one(self.store, self.client)
        def recall(query, tags):
            self.store.forget(self.receipt.source_id, "Owner withdrawal during request")
            return {"results": [{"document_id": self.receipt.version_id}]}
        self.client.recall = recall
        from olympus.preservation import PreservationError
        with self.assertRaisesRegex(PreservationError, "correction_reconciliation_pending"):
            recall_active(self.store, self.client, "code?", "scope-one")


if __name__ == "__main__":
    unittest.main()
