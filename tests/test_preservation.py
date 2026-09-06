from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
import tempfile
import time
import unittest
import json
from unittest.mock import patch

from olympus.preservation import Store, PreservationError, redact_secrets, digest


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "state"
        self.store = Store(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def capture(self, **kwargs):
        values = dict(source_key="test:original", scope="project-one", title="Source",
                      original=b"original bytes", text="The exact code is BLUE-731.",
                      locator="file:///old-name.txt")
        values.update(kwargs)
        return self.store.capture(**values)

    def test_receipt_survives_reopen_with_exact_original(self):
        first = self.capture()
        reopened = Store(self.root)
        self.assertEqual(first, reopened.receipt(first.version_id))
        self.assertEqual(reopened.read_version(first.version_id)["original"], b"original bytes")
        self.assertEqual(first.local_capture, "durable")
        self.assertEqual(first.memory, "pending")
        self.assertEqual(first.remote_copy, "unconfirmed")

    def test_repeat_and_rename_keep_one_version_and_operation(self):
        first = self.capture(observed_at="2026-01-01T00:00:00Z")
        second = self.capture(locator="file:///new-name.txt", observed_at="2026-09-05T00:00:00Z")
        self.assertEqual(asdict(first), asdict(second))
        self.assertEqual(self.store.status()["versions"], 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM locations").fetchone()[0], 2)

    def test_changed_content_and_scope_are_distinct(self):
        first = self.capture()
        changed = self.capture(text="The exact code is GREEN-912.")
        other = self.capture(scope="project-two")
        self.assertNotEqual(first.version_id, changed.version_id)
        self.assertEqual(first.source_id, changed.source_id)
        self.assertNotEqual(first.source_id, other.source_id)

    def test_concurrent_capture_does_not_duplicate_or_corrupt(self):
        def capture(_):
            store = Store(self.root)
            return store.capture(source_key="concurrent", scope="one", title="same",
                                 original=b"same", text="same").version_id
        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(capture, range(12)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(self.store.status()["versions"], 1)
        self.assertEqual(self.store.read_version(ids[0])["text"], "same")

    def test_failure_after_files_before_registry_is_recoverable(self):
        with patch.object(self.store, "_register", side_effect=OSError("simulated disk/registry interruption")):
            with self.assertRaises(OSError):
                self.capture()
        self.assertEqual(self.store.status()["versions"], 0)
        reopened = Store(self.root)
        self.assertEqual(reopened.recover(), {"recovered": 1, "invalid": []})
        self.assertEqual(reopened.status()["delivery"], {"pending": 1})
        self.assertEqual(reopened.recover()["recovered"], 0)

    def test_corruption_blocks_reads_and_recovery(self):
        receipt = self.capture()
        (self.root / "versions" / receipt.version_id / "original").write_bytes(b"damaged")
        with self.assertRaises(PreservationError):
            self.store.read_version(receipt.version_id)
        self.assertEqual(self.store.recover()["invalid"], [receipt.version_id])

    def test_provenance_tampering_is_detected_independently_of_semantic_id(self):
        receipt = self.capture()
        folder = self.store.versions / receipt.version_id
        p = folder / "manifest.json"
        metadata = json.loads(p.read_text())
        metadata["locator"] = "https://different.invalid/source"
        metadata["observed_at"] = "2040-01-01T00:00:00Z"
        p.write_text(json.dumps(metadata))
        with self.assertRaises(PreservationError):
            self.store.read_version(receipt.version_id)
        (folder / "manifest.sha256").write_text(digest(p.read_bytes()))
        with self.assertRaises(PreservationError):
            self.store.read_version(receipt.version_id)

    def test_restore_barrier_preserves_queue_but_blocks_reads_and_claims(self):
        receipt = self.capture()
        self.store.set_setting("recovery_state", "blocked")
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.receipt(receipt.version_id).memory, "pending")
        with self.assertRaisesRegex(PreservationError, "recovery_verification_required"):
            self.store.active_documents("project-one")

    def test_one_lease_and_recovery_after_worker_death(self):
        receipt = self.capture()
        first = self.store.claim(now=100, lease_seconds=10)
        self.assertIsNotNone(first)
        self.assertIsNone(Store(self.root).claim(now=109))
        replacement = Store(self.root).claim(now=111)
        self.assertEqual(first["operation_id"], replacement["operation_id"])
        self.assertNotEqual(first["lease_id"], replacement["lease_id"])
        self.assertFalse(self.store.update_delivery(first, "searchable", units=1))
        self.assertTrue(self.store.update_delivery(replacement, "submitted"))
        self.assertEqual(self.store.receipt(receipt.version_id).memory, "submitted")

    def test_submission_is_not_searchability(self):
        receipt = self.capture()
        job = self.store.claim()
        self.store.update_delivery(job, "submitted", attempted=True)
        self.assertEqual(self.store.active_documents("project-one"), set())
        job = self.store.claim(now=time.time() + 1)
        self.store.update_delivery(job, "searchable", units=2)
        self.assertEqual(self.store.active_documents("project-one"), {receipt.version_id})
        self.assertEqual(self.store.active_documents("project-two"), set())

    def test_known_credential_is_rejected_before_any_source_write(self):
        secret = "sk-proj-" + "a" * 40
        with self.assertRaisesRegex(PreservationError, "credential_pattern_detected"):
            self.capture(text=secret)
        self.assertEqual(list(self.store.versions.iterdir()), [])
        self.assertEqual(self.store.status()["versions"], 0)
        self.assertNotIn(secret, redact_secrets("A token " + secret))

    def test_secret_in_metadata_and_locator_is_rejected(self):
        secret = "github_pat_" + "a" * 40
        for kwargs in [{"metadata": {"token": secret}}, {"locator": "https://example.invalid/" + secret}]:
            with self.assertRaises(PreservationError):
                self.capture(**kwargs)

    def test_forget_survives_reopen_retry_capture_and_recovery(self):
        first = self.capture()
        job = self.store.claim()
        self.store.forget(first.source_id, "Owner's explicit synthetic request")
        self.assertFalse(self.store.update_delivery(job, "searchable", units=1))
        with self.assertRaisesRegex(PreservationError, "source_is_forgotten"):
            Store(self.root).capture(source_key="test:original", scope="project-one", title="New title", original=b"new", text="new")
        with self.assertRaises(PreservationError):
            self.store.retry(first.version_id)
        self.store.recover()
        self.assertEqual(self.store.receipt(first.version_id).memory, "forgotten")
        self.assertIsNone(self.store.claim())
        with self.assertRaisesRegex(PreservationError, "correction_reconciliation_pending"):
            self.store.active_documents("project-one")

    def test_supersession_does_not_apply_to_another_scope(self):
        first = self.capture()
        other = self.capture(scope="other", text="corrected")
        with self.assertRaises(PreservationError):
            self.store.supersede(first.version_id, other.version_id, "wrong scope")
        changed = self.capture(text="corrected")
        change_id = self.store.supersede(first.version_id, changed.version_id, "accepted correction")
        self.assertGreater(change_id, 0)
        self.assertEqual(self.store.receipt(first.version_id).memory, "superseded")
        self.store.recover()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT active FROM versions WHERE id=?", (first.version_id,)).fetchone()[0], 0)

    def test_late_old_content_with_new_metadata_cannot_become_active(self):
        first = self.capture()
        changed = self.capture(text="New applicable correction", original=b"corrected")
        self.store.supersede(first.version_id, changed.version_id, "Accepted new source version")
        late = self.capture(title="Renamed old source", metadata={"uploaded_at": "2040-01-01"})
        self.assertNotEqual(first.version_id, late.version_id)
        self.assertEqual(late.memory, "superseded")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT active FROM versions WHERE id=?", (late.version_id,)).fetchone()[0], 0)

    def test_no_unsafe_error_text_can_enter_status(self):
        self.capture()
        job = self.store.claim()
        with self.assertRaisesRegex(PreservationError, "unsafe_error_code"):
            self.store.update_delivery(job, "blocked", error="upstream body with private text")

    def test_state_cannot_be_in_drive(self):
        with self.assertRaisesRegex(PreservationError, "state_requires_local_directory"):
            Store(Path(self.temp.name) / "CloudStorage" / "queue")


if __name__ == "__main__":
    unittest.main()
