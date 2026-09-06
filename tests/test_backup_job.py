from pathlib import Path
import tempfile
import unittest

from olympus.backup_job import maybe_backup
from olympus.preservation import Store


class BackupJobTests(unittest.TestCase):
    def test_missing_owner_key_never_calls_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state")
            class Unavailable:
                def snapshot(self):
                    raise AssertionError("no runtime access before key readiness")
            self.assertEqual(maybe_backup(store, Unavailable())["state"], "waiting_for_recovery_key")

    def test_same_store_writer_lock_is_reentrant_for_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state")
            with store.exclusive():
                with store.exclusive():
                    store.set_setting("nested_checkpoint_lock", "ok")
            self.assertEqual(store.setting("nested_checkpoint_lock"), "ok")


if __name__ == "__main__":
    unittest.main()
