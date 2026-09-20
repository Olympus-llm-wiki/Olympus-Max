from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from olympus.backup_job import maybe_backup, publish_backups
from olympus.bounded_io import FileUnavailable
from olympus.preservation import Store


class BackupJobTests(unittest.TestCase):
    def test_hash_deadline_requires_audit_until_publication_inputs_change(self):
        for changed in (None, "source", "destination", "audit"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp:
                store = Store(Path(tmp) / "state")
                library = Path(tmp) / "library"
                destination = library / "Backups" / "synthetic.age"
                destination.parent.mkdir(parents=True)
                source = store.root / "backups" / destination.name
                source.parent.mkdir()
                source.write_bytes(b"synthetic encrypted bytes")
                destination.write_bytes(source.read_bytes())
                key = "backup_publication:" + source.name
                with patch("olympus.backup_job._hash_file", side_effect=FileUnavailable("file_read_timeout")):
                    self.assertEqual(publish_backups(store, library)["deferred"], 1)
                receipt = json.loads(store.setting(key))
                self.assertEqual(receipt["status"], "audit_required")
                self.assertEqual(receipt["remote_state"], "unconfirmed")
                self.assertNotIn("sha256", receipt)
                receipt["next_attempt_at"] = 0
                store.set_setting(key, json.dumps(receipt))
                if changed in ("source", "destination"):
                    path = source if changed == "source" else destination
                    path.write_bytes(b"different encrypted bytes")
                if changed is None:
                    with patch("olympus.backup_job._hash_file", side_effect=AssertionError("ordinary rehash")):
                        result = publish_backups(store, library)
                    self.assertEqual(result["deferred"], 1)
                    self.assertEqual(result["errors"][0]["code"], "backup_audit_required")
                    self.assertEqual(json.loads(store.setting(key))["status"], "audit_required")
                else:
                    from olympus.backup_job import _hash_file
                    with patch("olympus.backup_job._hash_file", wraps=_hash_file) as hashed:
                        result = publish_backups(store, library, audit=changed == "audit")
                    self.assertGreaterEqual(hashed.call_count, 1)
                    if changed == "audit":
                        self.assertFalse(result["errors"])
                        self.assertEqual(hashed.call_count, 2)
                        self.assertEqual(json.loads(store.setting(key))["status"], "local_staged")
                    else:
                        self.assertEqual(result["errors"][0]["code"], "backup_destination_conflict")

    def test_hash_deadline_before_publish_does_not_repeat_temporary_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state")
            library = Path(tmp) / "library"
            library.mkdir()
            source = store.root / "backups" / "synthetic.age"
            source.parent.mkdir()
            source.write_bytes(b"synthetic encrypted bytes")
            with patch("olympus.backup_job._hash_file", side_effect=FileUnavailable("file_read_timeout")):
                self.assertEqual(publish_backups(store, library)["deferred"], 1)
            key = "backup_publication:" + source.name
            receipt = json.loads(store.setting(key))
            self.assertEqual(receipt["status"], "audit_required")
            self.assertIsNone(receipt["destination"])
            receipt["next_attempt_at"] = 0
            store.set_setting(key, json.dumps(receipt))
            with patch("olympus.backup_job._hash_file", side_effect=AssertionError("ordinary rehash")), \
                 patch("olympus.backup_job.shutil.copyfileobj", side_effect=AssertionError("ordinary recopy")):
                self.assertEqual(publish_backups(store, library)["deferred"], 1)
            self.assertFalse(list((library / "Backups").iterdir()))

    def test_idle_publication_does_not_hash_old_backups_but_audit_does(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state")
            library = Path(tmp) / "library"
            library.mkdir()
            (store.root / "backups").mkdir()
            source = store.root / "backups" / "synthetic.age"
            source.write_bytes(b"synthetic encrypted bytes")
            self.assertEqual(publish_backups(store, library)["staged"], 1)
            with patch("olympus.backup_job._hash_file", side_effect=AssertionError("idle rehash")):
                self.assertEqual(publish_backups(store, library)["cached"], 1)
            from olympus.backup_job import _hash_file
            with patch("olympus.backup_job._hash_file", wraps=_hash_file) as hashed:
                result = publish_backups(store, library, audit=True)
            self.assertEqual(hashed.call_count, 2)
            self.assertFalse(result["errors"])

    def test_late_backup_failure_does_not_hide_export_or_prevent_control(self):
        from olympus.cli import _library
        from olympus.jobs import PipelineJobs
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state")
            store.set_setting("library_root", str(Path(tmp) / "library"))
            def exported(*args, **kwargs):
                store.set_setting("synthetic_export_cursor", "advanced")
                return {"checked": 1, "errors": []}
            with patch("olympus.library.scan_notes", return_value={"issues": []}), \
                 patch("olympus.library.export_versions", side_effect=exported), \
                 patch("olympus.backup_job.publish_backups", side_effect=OSError(5, "synthetic")), \
                 patch("olympus.snapshot_publication.publish_object_snapshots", return_value={"staged": 1, "errors": []}) as objects, \
                 patch("olympus.control_state.export_control_state", return_value={"sha256": "synthetic"}) as control:
                result = _library(store)
            self.assertEqual(store.setting("synthetic_export_cursor"), "advanced")
            self.assertEqual(result["backups"]["errors"][0]["errno"], 5)
            control.assert_called_once()
            objects.assert_called_once()
            states = {row["kind"]: row for row in PipelineJobs(store).snapshot()}
            self.assertEqual(states["library.export"]["state"], "succeeded")
            self.assertEqual(states["library.backups"]["state"], "failed")
            self.assertEqual(states["library.snapshot_objects"]["state"], "succeeded")
            self.assertEqual(states["library.control"]["state"], "succeeded")

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
