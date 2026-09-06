from dataclasses import replace
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from olympus.control_state import export_control_state
from olympus.cli import main
from olympus.library import export_versions
from olympus.preservation import Store, PreservationError, canonical, digest, timestamp
from olympus.remote_readback import RemoteFile, verify_remote_version, verify_remote_control, verify_remote_backup


class RemoteReadbackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.store = Store(self.base / "state")
        self.library = self.base / "library"
        self.source = self.store.capture(source_key="synthetic", scope="test", title="source",
                                         original=b"exact original\x00", text="Exact extracted text")
        export_versions(self.store, self.library)

    def tearDown(self):
        self.temporary.cleanup()

    def remote(self, content, file_id="remote-id", parent="folder"):
        return RemoteFile(content, file_id, (parent,), timestamp())

    def source_files(self):
        root = self.library / "Corpus" / self.source.source_id / self.source.version_id
        return {name: self.remote((root / name).read_bytes(), "id-" + str(index))
                for index, name in enumerate(("original", "text.txt", "manifest.json"))}

    def status(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--state", str(self.store.root), "status"]), 0)
        return json.loads(output.getvalue())

    def test_complete_version_records_proof_but_preserves_memory_state(self):
        before = self.store.receipt(self.source.version_id)
        proof = verify_remote_version(self.store, self.source.version_id, self.source_files(), folder_id="folder")
        after = self.store.receipt(self.source.version_id)
        self.assertEqual(after.remote_copy, "verified")
        self.assertEqual(after.memory, before.memory)
        self.assertEqual(proof["files"]["original"]["sha256"], digest(b"exact original\x00"))
        self.assertEqual(json.loads(self.store.setting("drive_remote:" + self.source.version_id)), proof)

    def test_missing_corrupt_or_wrong_parent_never_confirms(self):
        for failure in ("missing", "corrupt", "parent", "duplicate"):
            with self.subTest(failure=failure):
                files = self.source_files()
                if failure == "missing":
                    del files["text.txt"]
                elif failure == "corrupt":
                    files["original"] = replace(files["original"], content=b"truncated")
                elif failure == "parent":
                    files["original"] = replace(files["original"], parent_ids=("wrong",))
                else:
                    files["text.txt"] = replace(files["text.txt"], file_id=files["original"].file_id)
                with self.assertRaises(PreservationError):
                    verify_remote_version(self.store, self.source.version_id, files, folder_id="folder")
                self.assertEqual(self.store.receipt(self.source.version_id).remote_copy, "unconfirmed")
                self.assertIsNone(self.store.setting("drive_remote:" + self.source.version_id))

    def test_local_manifest_tamper_cannot_be_blessed_by_matching_remote_bytes(self):
        files = self.source_files()
        folder = self.store.versions / self.source.version_id
        manifest = json.loads((folder / "manifest.json").read_bytes())
        manifest["locator"] = "https://example.invalid/replaced"
        changed = canonical(manifest)
        (folder / "manifest.json").write_bytes(changed)
        (folder / "manifest.sha256").write_text(digest(changed))
        files["manifest.json"] = replace(files["manifest.json"], content=changed)
        with self.assertRaisesRegex(PreservationError, "source_manifest_hash_mismatch"):
            verify_remote_version(self.store, self.source.version_id, files, folder_id="folder")
        self.assertEqual(self.store.receipt(self.source.version_id).remote_copy, "unconfirmed")

    def control_files(self):
        exported = export_control_state(self.store, self.library)
        return (self.remote((self.library / "Control/latest.json").read_bytes(), "pointer", "control"),
                self.remote((self.library / "Control/states" / (exported["sha256"] + ".json")).read_bytes(), "state", "states"))

    def test_control_requires_current_registry_even_before_next_staging(self):
        pointer, state = self.control_files()
        self.store.forget(self.source.source_id, "Synthetic withdrawal")
        with self.assertRaisesRegex(PreservationError, "remote_bytes_mismatch"):
            verify_remote_control(self.store, pointer, state, control_folder_id="control", states_folder_id="states")
        self.assertIsNone(self.store.setting("control_remote_sha"))
        pointer, state = self.control_files()
        proof = verify_remote_control(self.store, pointer, state, control_folder_id="control", states_folder_id="states")
        self.assertEqual(self.store.setting("control_remote_sha"), proof["sha256"])

    def test_control_pointer_cannot_name_different_state(self):
        pointer, state = self.control_files()
        state = replace(state, content=b"{}")
        with self.assertRaises(PreservationError):
            verify_remote_control(self.store, pointer, state, control_folder_id="control", states_folder_id="states")
        self.assertIsNone(self.store.setting("control_remote_receipt"))

    def test_status_detects_new_change_after_successful_control_readback(self):
        pointer, state = self.control_files()
        verify_remote_control(self.store, pointer, state, control_folder_id="control", states_folder_id="states")
        self.assertTrue(self.status()["revocations_remote_verified"])
        self.store.forget(self.source.source_id, "Synthetic subsequent withdrawal")
        self.assertFalse(self.status()["revocations_remote_verified"])

    def test_backup_proof_is_separate_from_creation_and_restoration(self):
        filename = "olympus-00000000-0000-0000-0000-000000000001.tar.age"
        path = self.store.root / "backups" / filename
        path.parent.mkdir()
        path.write_bytes(b"synthetic ciphertext bytes")
        saved = {"path": str(path), "sha256": digest(path.read_bytes()), "size": path.stat().st_size,
                 "checkpoint_id": "synthetic-checkpoint", "remote_state": "unconfirmed", "restore_verified": False}
        self.store.set_setting("backup_last_receipt", json.dumps(saved))
        with self.assertRaisesRegex(PreservationError, "remote_bytes_mismatch"):
            verify_remote_backup(self.store, filename, self.remote(b"partial"), folder_id="folder")
        self.assertIsNone(self.store.setting("backup_remote:" + filename))
        proof = verify_remote_backup(self.store, filename, self.remote(path.read_bytes()), folder_id="folder")
        self.assertEqual(proof["remote_state"], "verified")
        self.assertFalse(proof["restore_verified"])
        self.assertEqual(json.loads(self.store.setting("backup_last_receipt")), saved)
        self.store.set_setting("backup_status", json.dumps({"state": "not_due"}))
        status = self.status()
        self.assertEqual(status["backup"]["state"], "not_due")
        self.assertEqual(status["latest_backup"]["remote_state"], "verified")
        self.assertFalse(status["latest_backup"]["restore_verified"])
        path.write_bytes(b"local corruption")
        with self.assertRaisesRegex(PreservationError, "local_backup_hash_mismatch"):
            verify_remote_backup(self.store, filename, self.remote(path.read_bytes()), folder_id="folder")


if __name__ == "__main__":
    unittest.main()
