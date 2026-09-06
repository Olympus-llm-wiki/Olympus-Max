from dataclasses import replace
from pathlib import Path
import json
import tempfile
import unittest

from olympus.backup_parts import RemotePart, stage_backup_parts, verify_remote_backup_parts
from olympus.preservation import Store, PreservationError, digest, timestamp
from olympus.remote_readback import RemoteFile


class BackupPartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "state")
        self.library = self.root / "library"
        self.library.mkdir()
        self.checkpoint = "00000000-0000-0000-0000-000000000123"
        path = self.store.root / "backups" / ("olympus-" + self.checkpoint + ".tar.age")
        path.parent.mkdir()
        path.write_bytes(b"synthetic encrypted checkpoint bytes with several parts")
        self.store.set_setting("backup_last_receipt", json.dumps({"path": str(path), "checkpoint_id": self.checkpoint,
            "size": path.stat().st_size, "sha256": digest(path.read_bytes())}))
        self.staged = stage_backup_parts(self.store, self.library, part_bytes=16)
        folder = Path(self.staged["path"])
        self.manifest = RemoteFile((folder / "manifest.json").read_bytes(), "manifest", ("folder",), timestamp())
        self.parts = {p["name"]: RemotePart(folder / p["name"], "id-" + str(p["index"]), ("folder",), timestamp())
                      for p in self.staged["manifest"]["parts"]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_complete_parts_verify_full_ciphertext_without_claiming_restore(self):
        proof = verify_remote_backup_parts(self.store, self.manifest, self.parts, folder_id="folder")
        self.assertEqual(proof["file"]["sha256"], self.staged["manifest"]["sha256"])
        self.assertEqual(proof["transport"], "verified_parts")
        self.assertFalse(proof["restore_verified"])
        self.assertEqual(stage_backup_parts(self.store, self.library, part_bytes=16)["manifest"], self.staged["manifest"])

    def test_missing_or_mismatched_part_never_confirms(self):
        incomplete = dict(self.parts)
        incomplete.pop(next(iter(incomplete)))
        with self.assertRaisesRegex(PreservationError, "backup_parts_inventory_mismatch"):
            verify_remote_backup_parts(self.store, self.manifest, incomplete, folder_id="folder")
        part = next(iter(self.parts.values()))
        part.path.write_bytes(b"wrong-part-bytes!")
        with self.assertRaises(PreservationError):
            verify_remote_backup_parts(self.store, self.manifest, self.parts, folder_id="folder")
        self.assertIsNone(self.store.setting("backup_remote:" + self.staged["manifest"]["filename"]))

    def test_wrong_parent_or_manifest_is_rejected(self):
        with self.assertRaises(PreservationError):
            verify_remote_backup_parts(self.store, replace(self.manifest, content=b"{}"), self.parts, folder_id="folder")
        parts = dict(self.parts)
        name = next(iter(parts))
        parts[name] = replace(parts[name], parent_ids=("wrong",))
        with self.assertRaisesRegex(PreservationError, "remote_file_identity_invalid"):
            verify_remote_backup_parts(self.store, self.manifest, parts, folder_id="folder")


if __name__ == "__main__":
    unittest.main()
