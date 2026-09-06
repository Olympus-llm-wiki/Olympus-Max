from pathlib import Path
import json
import tempfile
import unittest

from olympus.control_state import export_control_state
from olympus.preservation import Store, PreservationError


class ControlStateTests(unittest.TestCase):
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
