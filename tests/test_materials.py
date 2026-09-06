from pathlib import Path
import json
import shutil
import tempfile
import unittest

from olympus.delivery import recall_active, run_one
from olympus.materials import material_profile
from olympus.preservation import Store, PreservationError


class Client:
    def __init__(self, result=None):
        self.calls = 0
        self.result = result or {"results": []}

    def recall(self, query, tags):
        self.calls += 1
        return self.result

    def operation(self, operation_id):
        self.calls += 1
        return {"status": "not_found"}


class MaterialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "state")

    def tearDown(self):
        self.tmp.cleanup()

    def capture(self, key, role="primary", text="Synthetic source claim", **kwargs):
        return self.store.capture(source_key=key, scope="test", title=key, original=text.encode(), text=text,
                                  metadata={"material_role": role, **kwargs.pop("metadata", {})}, **kwargs)

    def mark_indexed(self, receipt):
        # Simulate old data that predates the material policy.
        with self.store.connect(write=True) as db:
            db.execute("UPDATE delivery SET state='searchable',units=1 WHERE version_id=?", (receipt.version_id,))

    def test_binary_archive_preserves_bytes_without_fake_text_or_model(self):
        raw = b"\x00\xffsynthetic-video-bytes"
        receipt = self.store.capture(source_key="video", scope="test", title="video", original=raw,
                                     text="", kind="artifact", archive_only=True)
        self.assertEqual(receipt.memory, "archived")
        version = self.store.read_version(receipt.version_id)
        self.assertEqual(version["original"], raw)
        self.assertEqual(version["text"], "")
        self.assertEqual(material_profile(version)["text_availability"], "absent")
        client = Client()
        self.assertEqual(run_one(self.store, client), {"state": "idle"})
        self.assertEqual(client.calls, 0)
        other = Store(self.root / "recovered")
        shutil.copytree(self.store.versions / receipt.version_id, other.versions / receipt.version_id)
        self.assertEqual(other.recover()["recovered"], 1)
        self.assertEqual(other.receipt(receipt.version_id).memory, "archived")

    def test_archive_mode_does_not_bypass_secret_guard(self):
        with self.assertRaisesRegex(PreservationError, "credential_pattern_detected"):
            self.store.capture(source_key="bad", scope="test", title="bad", text="", archive_only=True,
                               original=b"access_token=synthetic-credential-123456789")
        self.assertEqual(self.store.status()["versions"], 0)

    def test_empty_semantic_capture_still_rejected(self):
        with self.assertRaisesRegex(PreservationError, "source_scope_and_text_required"):
            self.store.capture(source_key="empty", scope="test", title="empty", original=b"raw", text="")

    def test_unknown_role_is_not_silently_promoted_to_primary(self):
        with self.assertRaisesRegex(PreservationError, "unknown_material_role"):
            material_profile({"kind": "future-unclassified-kind", "metadata": {}})

    def test_conversations_are_archived_and_old_indexed_discussions_are_filtered(self):
        discussion = self.store.capture(source_key="chat", scope="test", title="chat", kind="conversation",
                                        original="Может, создадим агентство?".encode(), text="Может, создадим агентство?")
        self.assertEqual(discussion.memory, "archived")
        primary = self.capture("report", "synthesis")
        for item in (discussion, primary):
            self.mark_indexed(item)
        client = Client({"results": [{"document_id": discussion.version_id, "chunk_id": "idea", "text": "Agency exists"},
                                      {"document_id": primary.version_id, "chunk_id": "source", "text": "Source comparison"}],
                         "chunks": {"idea": {"text": "private thought"}, "source": {"text": "report"}}})
        result = recall_active(self.store, client, "агентство", "test", include_discussions=True)
        self.assertEqual([r["document_id"] for r in result["results"]], [primary.version_id])
        self.assertEqual(set(result["chunks"]), {"source"})
        self.assertEqual(result["results"][0]["source"]["epistemic_status"], "historical_synthesis")
        self.assertFalse(result["results"][0]["source"]["verified_fact"])
        self.assertEqual(len(result["discussions"]), 1)
        self.assertIn("Может", result["discussions"][0]["excerpt"])
        self.assertFalse(result["discussions"][0]["source"]["current_owner_decision"])

    def test_archive_history_needs_no_native_call(self):
        self.capture("idea", "discussion", text="Может, сделаем инженерное агентство")
        client = Client()
        result = recall_active(self.store, client, "агентство", "test", include_discussions=True)
        self.assertEqual(client.calls, 0)
        self.assertFalse(result["results"])
        self.assertEqual(len(result["discussions"]), 1)

    def test_decision_needs_explicit_confirmation_evidence(self):
        old = self.capture("old-decision", "decision")
        self.assertEqual(old.memory, "archived")
        with self.assertRaisesRegex(PreservationError, "owner_confirmation_evidence_required"):
            self.capture("unproven", "decision", metadata={"decision_status": "confirmed"})
        decision = self.capture("approved", "decision", metadata={"decision_status": "confirmed",
            "owner_confirmed_at": "2026-09-06T00:00:00Z", "decision_scope": "synthetic-project",
            "owner_confirmation_evidence": "synthetic-owner-confirmation-record"})
        self.assertEqual(decision.memory, "pending")
        self.assertTrue(material_profile(self.store.read_version(decision.version_id))["current_owner_decision"])

    def test_package_filter_excludes_other_package(self):
        first = self.capture("one")
        second = self.capture("two")
        for item in (first, second):
            self.mark_indexed(item)
        self.store.set_setting("legacy_package:I001", json.dumps({"version_ids": [first.version_id]}))
        client = Client({"results": [{"document_id": first.version_id, "text": "one"},
                                      {"document_id": second.version_id, "text": "two"}]})
        result = recall_active(self.store, client, "query", "test", package_id="I001")
        self.assertEqual([r["document_id"] for r in result["results"]], [first.version_id])
        with self.assertRaisesRegex(PreservationError, "unknown_package"):
            recall_active(self.store, client, "query", "test", package_id="I002")

    def test_old_pending_discussion_is_archived_without_new_submission(self):
        item = self.capture("old-pending", "discussion")
        with self.store.connect(write=True) as db:
            db.execute("UPDATE delivery SET state='pending' WHERE version_id=?", (item.version_id,))
        result = run_one(self.store, Client())
        self.assertEqual(result["state"], "archived")


if __name__ == "__main__":
    unittest.main()
