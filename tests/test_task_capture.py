from pathlib import Path
import json
import tempfile
import subprocess
import unittest
from unittest.mock import patch

from olympus.codex_capture import CaptureRegistration, CaptureBatch
from olympus.preservation import Store, PreservationError
from olympus.task_capture import register_task, sync_registered_tasks
from olympus.cli import _capture_local


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

    def _set_tool_output(self, text):
        lines = [json.loads(line) for line in self.fixture.splitlines()]
        for row in lines:
            if row.get("payload", {}).get("type") == "function_call_output":
                row["payload"]["output"] = text
        self.path.write_text("".join(json.dumps(row) + "\n" for row in lines))

    def test_multiline_tool_output_is_captured_without_false_credential_rejection(self):
        text = "password=short\ncapture-regression-marker"
        self._set_tool_output(text)
        first = sync_registered_tasks(self.store)["tasks"][0]
        self.assertEqual(first["new_local_events"], 6)
        self.assertEqual(first["coverage_gaps"], [])
        receipt = first["turn_receipts"][0]
        self.assertIn(text, self.store.read_version(receipt["version_id"])["text"])
        with self.store.connect() as db:
            events = [json.loads(row[0]) for row in db.execute("SELECT message_json FROM captured_events")]
        output = next(event for event in events if event["kind"] == "tool_output")
        self.assertEqual(output["text"], text)
        self.assertFalse(output["redacted"])
        second = sync_registered_tasks(Store(self.root / "state"))["tasks"][0]
        self.assertEqual(second["new_local_events"], 0)
        self.assertEqual(first["turn_receipts"], second["turn_receipts"])

    def test_credential_is_redacted_in_both_event_and_completed_turn(self):
        credential = "synthetic-credential-for-regression"
        self._set_tool_output("password=" + credential)
        result = sync_registered_tasks(self.store)["tasks"][0]
        text = self.store.read_version(result["turn_receipts"][0]["version_id"])["text"]
        self.assertNotIn(credential, text)
        self.assertIn("[REDACTED:credential]", text)
        with self.store.connect() as db:
            events = [json.loads(row[0]) for row in db.execute("SELECT message_json FROM captured_events")]
        self.assertNotIn(credential, json.dumps(events))
        output = next(event for event in events if event["kind"] == "tool_output")
        self.assertTrue(output["redacted"])
        self.assertEqual(output["text"], "[REDACTED:credential]")

    def test_unredacted_credential_is_rejected_before_persistence(self):
        self._set_tool_output("password=synthetic-credential-for-regression")
        with patch("olympus.task_capture.redact_secrets", side_effect=lambda text: text):
            with self.assertRaisesRegex(PreservationError, "^credential_pattern_detected$"):
                sync_registered_tasks(self.store)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM captured_events").fetchone()[0], 0)
        self.assertEqual(self.store.status()["versions"], 0)

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

    def test_drain_pending_and_exit_zero_errors_survive_collector(self):
        payload = {"registered": 0, "pending": 1, "checked": 1,
                   "errors": [{"code": "signal_registration_pending"}],
                   "pending_total": 1, "oldest_pending_age_seconds": 600,
                   "oldest_pending_at": "2026-09-05T10:00:00Z"}
        process = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with patch("olympus.cli.subprocess.run", return_value=process):
            result = _capture_local(self.store)
        self.assertFalse(result["hook_signal_drain_ok"])
        self.assertEqual(result["hook_signals"]["state"], "degraded")
        self.assertEqual(result["hook_signals"]["pending_total"], 1)
        self.assertEqual(result["hook_signals"]["oldest_pending_age_seconds"], 600)
        self.assertEqual(json.loads(self.store.setting("hook_signal_status"))["state"], "degraded")

    def test_drain_malformed_and_timeout_remain_visible(self):
        cases = [(subprocess.CompletedProcess([], 0, '{"errors": "bad"}', ""), "hook_drain_invalid_response"),
                 (subprocess.TimeoutExpired("synthetic", 8), "hook_drain_timeout"),
                 (subprocess.CompletedProcess([], 1, "", "not forwarded"), "hook_drain_process_failed")]
        for value, code in cases:
            with self.subTest(code=code), patch("olympus.cli.subprocess.run", **(
                    {"side_effect": value} if isinstance(value, Exception) else {"return_value": value})):
                result = _capture_local(self.store)
            self.assertFalse(result["hook_signal_drain_ok"])
            self.assertEqual(result["hook_signals"]["errors"], [{"code": code}])

    def test_new_segment_is_published_once_with_historical_gap_preserved(self):
        lines = [json.loads(line) for line in self.fixture.splitlines()]
        boundary = {"timestamp": "2026-09-05T10:00:11Z", "type": "compacted", "payload": {}}
        later = json.loads(json.dumps(lines[1:]).replace("synthetic-turn", "later-turn").replace("synthetic-call", "later-call"))
        for record in later:
            record["timestamp"] = record["timestamp"].replace("10:00:", "10:01:")
        self.path.write_text(''.join(json.dumps(row)+'\n' for row in lines+[boundary]+later))
        first = sync_registered_tasks(self.store)["tasks"][0]
        self.assertIn("compacted_history_requires_reconciliation", first["coverage_gaps"])
        self.assertEqual(len(first["turn_receipts"]), 2)
        self.assertEqual(len(first["coverage_segments"]), 2)
        second = sync_registered_tasks(self.store)["tasks"][0]
        self.assertEqual(first["turn_receipts"], second["turn_receipts"])
        self.assertEqual(second["new_local_events"], 0)
        self.assertEqual(self.store.status()["versions"], 2)

    def test_registered_rollout_rebind_preserves_boundary_and_events(self):
        lines = [json.loads(line) for line in self.fixture.splitlines()]
        lines[0]["payload"]["cwd"] = str(Path(__file__).resolve().parents[1])
        self.path.write_text(''.join(json.dumps(row)+'\n' for row in lines))
        first = sync_registered_tasks(self.store)["tasks"][0]
        moved = self.root.resolve() / '.codex/archived_sessions/rollout-synthetic.jsonl'
        moved.parent.mkdir(parents=True)
        self.path.rename(moved)
        calls=[]
        def reader(identity, project):
            calls.append(identity)
            return {"transcript_path":str(moved),"codex_version":"0.153.1"}
        with patch('olympus.task_capture.Path.home', return_value=self.root.resolve()):
            second = sync_registered_tasks(self.store, metadata_reader=reader)["tasks"][0]
        self.assertEqual(calls,['synthetic-thread'])
        self.assertEqual(second['new_local_events'],0)
        self.assertEqual(first['turn_receipts'],second['turn_receipts'])
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM registrations').fetchone()
        self.assertEqual(row['transcript_path'],str(moved))
        self.assertEqual(row['started_at'],self.registration.started_at)
        self.assertIsNotNone(self.store.setting('capture_rebind:synthetic-thread'))

    def test_rebind_does_not_follow_path_outside_codex_roots(self):
        self.path.unlink()
        reader=lambda *_:{'transcript_path':str(self.root/'private.jsonl'),'codex_version':'0.153.1'}
        with patch('olympus.task_capture.Path.home', return_value=self.root):
            result=sync_registered_tasks(self.store,metadata_reader=reader)['tasks'][0]
        self.assertIn('registration_rebind_path_not_allowed',result['coverage_gaps'])
        self.assertEqual(result['turn_receipts'],[])

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
