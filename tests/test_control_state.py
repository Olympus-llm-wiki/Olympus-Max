from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from olympus.control_state import export_control_state
from olympus.preservation import Store, PreservationError
from olympus.bounded_io import FileUnavailable


class ControlStateTests(unittest.TestCase):
    def test_control_unavailable_read_is_typed_and_does_not_change_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, library = Store(Path(tmp) / "state"), Path(tmp) / "library"
            export_control_state(store, library)
            original = (library / "Control/latest.json").read_bytes()
            with patch("olympus.control_state.read_file", side_effect=FileUnavailable("materialization_pending")):
                with self.assertRaisesRegex(FileUnavailable, "materialization_pending"):
                    export_control_state(store, library)
            self.assertEqual((library / "Control/latest.json").read_bytes(), original)

    def test_lost_lease_fence_prevents_control_pointer_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, library = Store(Path(tmp) / "state"), Path(tmp) / "library"
            def lost(_):
                raise PreservationError("stage_lease_lost")
            with self.assertRaisesRegex(PreservationError, "stage_lease_lost"):
                export_control_state(store, library, progress=lost)
            self.assertFalse((library / "Control/latest.json").exists())
            self.assertIsNone(store.setting("control_staged_sha"))

    def test_forget_and_original_identity_are_in_versioned_control_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state")
            library = Path(tmp) / "library"
            receipt = store.capture(source_key="synthetic", scope="test", title="test", original=b"original", text="original")
            first = export_control_state(store, library)
            store.forget(receipt.source_id, "Synthetic withdrawal")
            second = export_control_state(store, library)
            self.assertNotEqual(first["sha256"], second["sha256"])
            self.assertFalse(second["remote_verified"])
            data = json.loads((library / "Control/states" / (second["sha256"] + ".json")).read_text())
            self.assertEqual(data["changes"][0]["kind"], "forget")
            self.assertEqual(data["sources"][0]["source_key"], "synthetic")
            self.assertEqual(export_control_state(store, library)["sha256"], second["sha256"])


if __name__ == "__main__":
    unittest.main()
