import json
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contextlib import redirect_stdout
from olympus import starter
from olympus.hindsight import HindsightError
from olympus.preservation import Store, PreservationError


class StarterProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / "starter"
        self.project.mkdir()
        self.state = self.root / "state"
        self.a = patch.object(starter, "PROJECT", self.project)
        self.b = patch.object(starter, "PROFILE", self.project / "starter.local.json")
        self.a.start(); self.b.start()
        self.output = patch("sys.stdout", new_callable=io.StringIO)
        self.output.start()

    def tearDown(self):
        self.output.stop(); self.b.stop(); self.a.stop(); self.tmp.cleanup()

    def test_setup_is_local_and_idempotent(self):
        with patch("socket.create_connection", side_effect=AssertionError("no network")):
            self.assertEqual(starter.setup_main(["setup", "--state", str(self.state)]), 0)
            self.assertEqual(starter.setup_main(["setup", "--state", str(self.state)]), 0)
        self.assertEqual(Store(self.state).setting("starter_profile"), "olympus-max")
        self.assertEqual(set(json.loads(starter.PROFILE.read_text())), {"schema", "state", "api_url", "bank"})

    def test_state_cannot_be_inside_publishable_repository(self):
        self.assertEqual(starter.setup_main(["setup", "--state", str(self.project / "state")]), 2)
        self.assertFalse(starter.PROFILE.exists())

    def test_existing_foreign_state_is_preserved(self):
        self.state.mkdir();(self.state / "personal.txt").write_text("keep")
        self.assertEqual(starter.setup_main(["setup", "--state", str(self.state)]), 2)
        self.assertEqual((self.state / "personal.txt").read_text(), "keep")

    def test_inherited_main_olympus_settings_do_not_redirect_starter(self):
        starter.setup_main(["setup", "--state", str(self.state), "--bank", "new-bank"])
        with patch.dict(os.environ, {"OLYMPUS_STATE_DIR": "/unrelated", "OLYMPUS_BANK_ID": "unrelated"}), \
             patch("olympus.cli.main", return_value=0) as main:
            self.assertEqual(starter.main(["status"]), 0)
            self.assertEqual(os.environ["OLYMPUS_STATE_DIR"], str(self.state.resolve()))
            self.assertEqual(os.environ["OLYMPUS_BANK_ID"], "new-bank")
            main.assert_called_once()

    def test_admin_commands_do_not_touch_external_runtime(self):
        starter.setup_main(["setup", "--state", str(self.state)])
        with patch("olympus.cli.main", side_effect=AssertionError("not delegated")):
            self.assertEqual(starter.main(["reconcile"]), 2)

    def test_local_search_uses_profile_without_network_or_api_key(self):
        starter.setup_main(["setup", "--state", str(self.state)])
        store = Store(self.state)
        captured = store.capture(source_key="synthetic", scope="personal", title="Orchid plan",
            text="Orchid plans stay local", original=b"Orchid plans stay local", metadata={"material_role": "primary"})
        output = io.StringIO()
        with patch.dict(os.environ, {"OLYMPUS_STATE_DIR": "/unrelated", "OLYMPUS_HINDSIGHT_API_KEY": ""}), \
             patch("socket.create_connection", side_effect=AssertionError("network forbidden")), redirect_stdout(output):
            self.assertEqual(starter.main(["search", "orchid", "--local-only"]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["results"][0]["version_id"], captured.version_id)
        self.assertEqual(result["results"][0]["delivery_state"], "pending")
        self.assertEqual(result["coverage"]["catalog"], "unavailable")
        self.assertFalse(result["coverage"]["absence_proven"])

    def test_search_keeps_local_result_when_hindsight_fails(self):
        starter.setup_main(["setup", "--state", str(self.state)])
        store = Store(self.state)
        captured = store.capture(source_key="synthetic", scope="personal", title="Orchid plan",
            text="Orchid plans stay local", original=b"Orchid plans stay local", metadata={"material_role": "primary"})
        store.update_delivery(store.claim(), "searchable", units=1)
        output = io.StringIO()
        with patch.dict(os.environ, {}), \
             patch.object(starter.HindsightClient, "recall", side_effect=HindsightError("network_error")), redirect_stdout(output):
            self.assertEqual(starter.main(["search", "orchid"]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["results"][0]["version_id"], captured.version_id)
        self.assertEqual(result["coverage"]["semantic"][0]["state"], "unavailable")

    def test_live_doctor_uses_authenticated_bank_endpoint(self):
        starter.setup_main(["setup", "--state", str(self.state)])
        with patch.object(starter, "HindsightClient") as client:
            client.return_value._request.side_effect = AssertionError("no bank-relative /health request")
            client.return_value.list_operations.return_value = {"operations": []}
            self.assertEqual(starter.setup_main(["doctor", "--live"]), 0)
            client.return_value.list_operations.assert_called_once_with(limit=1)


if __name__ == "__main__":
    unittest.main()
