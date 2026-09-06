from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from olympus.codex_capture import CaptureRegistration, CaptureBatch
from olympus.preservation import Store, PreservationError
from olympus.task_capture import register_task, sync_registered_tasks


class TaskCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = (Path(__file__).parent / "fixtures/codex/completed-0.153.1.jsonl").read_bytes()
        self.path = self.root / "registered.jsonl"
        self.path.write_bytes(self.fixture)
        self.store = Store(self.root / "state")
        self.registration = CaptureRegistration("synthetic-thread", self.path, "2026-09-05T00:00:00Z")
        register_task(self.store, self.registration, "synthetic")

    def tearDown(self):
        self.temp.cleanup()

    def test_local_events_and_complete_turn_survive_reopen_without_duplicates(self):
        first = sync_registered_tasks(self.store)["tasks"][0]
        self.assertEqual(first["new_local_events"], 6)
        self.assertEqual(len(first["turn_receipts"]), 1)
        second = sync_registered_tasks(Store(self.root / "state"))["tasks"][0]
        self.assertEqual(second["new_local_events"], 0)
        self.assertEqual(first["turn_receipts"], second["turn_receipts"])
        self.assertEqual(self.store.status()["versions"], 1)

    def test_unfinished_producer_keeps_events_without_claiming_complete_turn(self):
        lines = [json.loads(line) for line in self.fixture.splitlines()]
        partial = [line for line in lines if not (line.get("type") == "event_msg" and line.get("payload", {}).get("type") in {"task_complete", "turn_complete"})]
        self.path.write_text("".join(json.dumps(row) + "\n" for row in partial))
        result = sync_registered_tasks(self.store)["tasks"][0]
        self.assertEqual(result["new_local_events"], 6)
        self.assertIn("turn_incomplete", result["coverage_gaps"])
        self.assertEqual(result["turn_receipts"], [])
        with Store(self.root / "state").connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM captured_events").fetchone()[0], 6)
        self.path.write_bytes(self.fixture)
        result = sync_registered_tasks(Store(self.root / "state"))["tasks"][0]
        self.assertEqual(result["new_local_events"], 0)
        self.assertEqual(len(result["turn_receipts"]), 1)

    def test_forgotten_turn_is_not_resubmitted_or_a_global_sync_failure(self):
        receipt = sync_registered_tasks(self.store)["tasks"][0]["turn_receipts"][0]
        self.store.forget(receipt["source_id"], "Explicit synthetic withdrawal")
        result = sync_registered_tasks(self.store)["tasks"][0]
        self.assertEqual(result["turn_receipts"][0]["memory"], "forgotten")
        self.assertFalse(result["turn_receipts"][0]["reimported"])

    def test_registration_scope_cannot_change_silently(self):
        with self.assertRaises(PreservationError):
            register_task(self.store, self.registration, "other-project")

    def test_rewritten_turn_does_not_union_old_and_new_user_text(self):
        lines = [json.loads(line) for line in self.fixture.splitlines()]
        def replace_user(marker):
            for row in lines:
                value = row.get("payload", {})
                if row.get("type") == "response_item" and value.get("type") == "message" and value.get("role") == "user":
                    value["content"] = [{"type": "input_text", "text": marker}]
                if row.get("type") == "event_msg" and value.get("type") == "user_message":
                    value["message"] = marker
            self.path.write_text("".join(json.dumps(row) + "\n" for row in lines))
        replace_user("ORIGINAL-REQUEST-483")
        first = sync_registered_tasks(self.store)["tasks"][0]["turn_receipts"][0]
        replace_user("REVISED-REQUEST-926")
        second = sync_registered_tasks(self.store)["tasks"][0]["turn_receipts"][0]
        self.assertNotEqual(first["version_id"], second["version_id"])
        text = self.store.read_version(second["version_id"])["text"]
        self.assertIn("REVISED-REQUEST-926", text)
        self.assertNotIn("ORIGINAL-REQUEST-483", text)
        self.assertIn("ORIGINAL-REQUEST-483", self.store.read_version(first["version_id"])["text"])
        self.assertEqual(self.store.receipt(first["version_id"]).memory, "superseded")

    def test_bounded_registration_scan_eventually_visits_every_task(self):
        for i in range(51):
            register_task(self.store, CaptureRegistration(f"thread-{i:02d}", self.path, "2026-09-05T00:00:00Z"), "many")
        visited = set()
        def reader(registration, **kwargs):
            visited.add(registration.thread_id)
            return CaptureBatch(registration.thread_id)
        with patch("olympus.task_capture.read_registered_rollout", side_effect=reader):
            sync_registered_tasks(self.store)
            sync_registered_tasks(Store(self.root / "state"))
        self.assertEqual(len(visited), 52)


if __name__ == "__main__":
    unittest.main()
