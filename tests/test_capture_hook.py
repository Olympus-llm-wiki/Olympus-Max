"""Official SessionStart/Stop payload shapes; only synthetic session headers."""
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from olympus.preservation import Store

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "scripts/codex-capture-hook.py"
spec = importlib.util.spec_from_file_location("olympus_capture_hook_test", SCRIPT)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)

THREAD = "019abcde-0000-7000-8000-000000000001"
OTHER = "019abcde-0000-7000-8000-000000000002"
TURN = "019abcde-0000-7000-8000-000000000003"


class CaptureHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.sessions = self.root / "codex/sessions"
        self.sessions.mkdir(parents=True)
        self.state = self.root / "state"
        self.path = self.sessions / ("rollout-synthetic-" + THREAD + ".jsonl")
        self.header()
        self.start = "2026-09-05T10:00:00+00:00"

    def tearDown(self):
        self.temp.cleanup()

    def header(self, **changes):
        fields = {"id": THREAD, "cwd": str(self.project), "cli_version": "0.153.1",
                  "base_instructions": "SYNTHETIC_INTERNAL_NEVER_SAVED"}
        fields.update(changes)
        self.path.write_text(json.dumps({"ordinal": 0, "type": "session_meta", "timestamp": self.start if hasattr(self, "start") else "2026-09-05T10:00:00Z", "payload": fields}) + "\n")

    def payload(self, event="SessionStart", **changes):
        value = {"session_id": THREAD, "transcript_path": str(self.path), "cwd": str(self.project),
                 "hook_event_name": event, "model": "synthetic-model", "permission_mode": "default"}
        if event == "SessionStart":
            value["source"] = "startup"
        elif event == "Stop":
            value.update(turn_id=TURN, stop_hook_active=False,
                         last_assistant_message="SYNTHETIC_BODY_NEVER_SAVED")
        value.update(changes)
        return value

    def receive(self, payload=None, **kwargs):
        return hook.receive_signal(payload if payload is not None else self.payload(),
                                   state_root=self.state, project_root=self.project, sessions_root=self.sessions,
                                   received_at=kwargs.pop("received_at", self.start), **kwargs)

    def drain(self, **kwargs):
        return hook.drain_signals(state_root=self.state, project_root=self.project,
                                  sessions_root=self.sessions, **kwargs)

    def registration(self):
        with Store(self.state).connect() as db:
            row = db.execute("SELECT * FROM registrations WHERE thread_id=?", (THREAD,)).fetchone()
        return dict(row) if row else None

    def test_session_start_registers_only_matching_metadata_and_no_sources(self):
        result = self.receive()
        self.assertEqual(result["status"], "registered")
        row = self.registration()
        self.assertEqual(row["scope"], "olympus")
        self.assertEqual(row["transcript_path"], str(self.path))
        self.assertEqual(row["started_at"], self.start)
        self.assertEqual(Store(self.state).status()["versions"], 0)
        self.assertFalse(list((self.state / "hook-signals/pending").glob("*.json")))

    def test_repeat_stop_resume_and_compaction_never_slide_start_boundary(self):
        self.receive()
        for payload in (self.payload("Stop"), self.payload(source="resume"), self.payload(source="compact"), self.payload("Stop", stop_hook_active=True)):
            result = self.receive(payload, received_at="2026-09-05T13:00:00Z")
            self.assertEqual(result["status"], "already_registered")
            self.assertEqual(self.registration()["started_at"], self.start)

    def test_first_stop_uses_first_signal_time_without_backfilling_history(self):
        self.receive(self.payload("Stop"))
        self.assertEqual(self.registration()["started_at"], self.start)

    def test_null_transcript_on_start_retains_boundary_until_stop_supplies_it(self):
        result = self.receive(self.payload(transcript_path=None))
        self.assertEqual(result["status"], "pending_metadata")
        self.assertIsNone(self.registration())
        saved = next((self.state / "hook-signals/pending").glob("*.json"))
        self.assertEqual(json.loads(saved.read_text())["started_at"], self.start)
        result = self.receive(self.payload("Stop"), received_at="2026-09-05T12:00:00Z")
        self.assertEqual(result["status"], "registered")
        self.assertEqual(self.registration()["started_at"], self.start)

    def test_missing_or_partial_header_is_pending_and_daemon_can_finish(self):
        for partial in (False, True):
            with self.subTest(partial=partial):
                if partial:
                    self.path.write_bytes(b'{"type":"session_meta"')
                else:
                    self.path.unlink(missing_ok=True)
                result = self.receive()
                self.assertEqual(result["status"], "pending_metadata")
                self.header()
                result = self.drain()
                self.assertEqual(result["registered"], 1)
                self.assertEqual(self.registration()["started_at"], self.start)

    def test_last_assistant_message_and_internal_header_do_not_enter_state(self):
        with patch.object(hook, "_register_saved", return_value="pending_metadata"):
            self.receive(self.payload("Stop"))
        for file in self.state.rglob("*"):
            if file.is_file():
                raw = file.read_bytes()
                self.assertNotIn(b"SYNTHETIC_BODY_NEVER_SAVED", raw)
                self.assertNotIn(b"SYNTHETIC_INTERNAL_NEVER_SAVED", raw)

    def test_metadata_id_cwd_or_version_mismatch_cannot_register(self):
        for changes, code in (({"id": OTHER}, "session_metadata_id_mismatch"),
                              ({"cwd": str(self.root)}, "project_cwd_mismatch"),
                              ({"cli_version": "0.154.0"}, "unsupported_codex_version")):
            self.header(**changes)
            with self.assertRaisesRegex(hook.HookError, code):
                self.receive()
            self.assertFalse(self.state.exists())

    def test_outside_project_or_sessions_path_is_rejected_before_spool(self):
        for payload in (self.payload(cwd=str(self.root)), self.payload(transcript_path=str(self.root / "rollout-private.jsonl")),
                        self.payload(transcript_path=str(self.sessions / "../rollout-private.jsonl")),
                        self.payload(transcript_path="relative.jsonl"), self.payload(cwd="\x00")):
            with self.assertRaises(hook.HookError):
                self.receive(payload)
        self.assertFalse(self.state.exists())

    def test_symlink_leaf_and_parent_cannot_escape_sessions(self):
        outside = self.root / "elsewhere"
        outside.mkdir()
        external = outside / "rollout-other.jsonl"
        external.write_bytes(self.path.read_bytes())
        leaf = self.sessions / "rollout-link.jsonl"
        leaf.symlink_to(external)
        parent = self.sessions / "linked"
        parent.symlink_to(outside, target_is_directory=True)
        for path in (leaf, parent / "rollout-other.jsonl"):
            with self.assertRaisesRegex(hook.HookError, "transcript_symlink"):
                self.receive(self.payload(transcript_path=str(path)))

    def test_other_hook_events_and_malformed_payload_are_rejected(self):
        for value in ([], self.payload(session_id="not-an-id"), self.payload(source="future-source"),
                      self.payload("SubagentStop"), self.payload("Stop", stop_hook_active="true"),
                      self.payload("Stop", turn_id=None)):
            with self.assertRaises(hook.HookError):
                self.receive(value)

    def test_registration_failure_leaves_durable_signal_for_daemon(self):
        with patch.object(hook, "register_task", side_effect=OSError("synthetic database busy")):
            with self.assertRaises(OSError):
                self.receive()
        file = next((self.state / "hook-signals/pending").glob("*.json"))
        self.assertEqual(json.loads(file.read_text())["started_at"], self.start)
        self.assertEqual(self.drain()["registered"], 1)
        self.assertEqual(self.registration()["started_at"], self.start)

    def test_drain_cursor_does_not_starve_later_signal_behind_missing_metadata(self):
        self.receive(self.payload(transcript_path=None))
        self.header(id=OTHER)
        with patch.object(hook, "register_task", side_effect=OSError("synthetic temporary failure")):
            with self.assertRaises(OSError):
                self.receive(self.payload(session_id=OTHER))
        one = self.drain(limit=1)
        two = self.drain(limit=1)
        self.assertEqual(one["pending"], 1)
        self.assertEqual(two["registered"], 1)

    def test_real_sigterm_after_spool_preserves_signal_and_does_not_block_codex(self):
        # The child uses a synthetic header but the script's real project root.
        self.header(cwd=str(PROJECT))
        payload = self.payload(cwd=str(PROJECT))
        child = """import importlib.util,os,signal,sys
spec=importlib.util.spec_from_file_location('hook',sys.argv[1]); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
def stop(*args,**kwargs): os.kill(os.getpid(),signal.SIGTERM)
module.register_task=stop
raise SystemExit(module.main(sys.argv[2:]))
"""
        result = subprocess.run([sys.executable, "-c", child, str(SCRIPT), "--state-root", str(self.state),
                                 "--sessions-root", str(self.sessions)], input=json.dumps(payload).encode(),
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        self.assertTrue(output["continue"])
        self.assertIn("signal_registration_pending", output["systemMessage"])
        self.assertEqual(result.stderr, b"")
        self.assertEqual(len(list((self.state / "hook-signals/pending").glob("*.json"))), 1)
        result = hook.drain_signals(state_root=self.state, project_root=PROJECT, sessions_root=self.sessions)
        self.assertEqual(result["registered"], 1)

    def test_project_hook_config_uses_only_native_events_and_synchronous_small_timeout(self):
        output = self.state.parent / "hooks-preview.json"
        generated = subprocess.run([sys.executable, str(PROJECT / "scripts/generate-capture-hooks.py"),
            "--state-root", str(self.state), "--sessions-root", str(self.sessions), "--output", str(output)],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(generated.returncode, 0, generated.stderr)
        self.assertFalse(json.loads(generated.stdout)["activated"])
        config = json.loads(output.read_text())
        self.assertEqual(set(config["hooks"]), {"SessionStart", "Stop"})
        for groups in config["hooks"].values():
            for group in groups:
                for item in group["hooks"]:
                    self.assertEqual(item["type"], "command")
                    self.assertEqual(item["timeout"], 3)
                    self.assertFalse(item.get("async", False))
                    self.assertIn(str(SCRIPT), item["command"])
                    self.assertNotIn("with-runtime-secrets", item["command"])


if __name__ == "__main__":
    unittest.main()
