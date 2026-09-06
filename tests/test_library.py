from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from olympus.library import export_versions, scan_notes
from olympus.preservation import Store, PreservationError, digest


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.store = Store(self.base / "state")
        self.library = self.base / "HindSight"
        (self.library / "Inbox").mkdir(parents=True)
        (self.library / "Library/Personal").mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def capture(self, **kwargs):
        options = dict(source_key="synthetic:source", scope="personal", title="Synthetic source",
                       original=b"original \x00 bytes", text="Exact synthetic extracted text.",
                       locator="https://example.invalid/source")
        options.update(kwargs)
        return self.store.capture(**options)

    def note(self, name="Inbox/note.md", text="Original synthetic note."):
        path = self.library / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def scan(self, **kwargs):
        return scan_notes(self.store, self.library,
                          selected=kwargs.pop("selected", ["Inbox", "Library/Personal"]),
                          scope=kwargs.pop("scope", "personal"), **kwargs)

    def codes(self, result):
        return {item["code"] for item in result["issues"]}

    def test_export_contains_complete_original_text_manifest_and_one_source_card(self):
        first = self.capture()
        second = self.capture(text="Changed extracted text.")
        result = export_versions(self.store, self.library)
        self.assertEqual(result["exported"], 2)
        self.assertFalse(result["errors"])
        for receipt in (first, second):
            folder = self.library / "Corpus" / receipt.source_id / receipt.version_id
            captured = self.store.read_version(receipt.version_id)
            self.assertEqual((folder / "original").read_bytes(), captured["original"])
            self.assertEqual((folder / "text.txt").read_text(), captured["text"])
            manifest = json.loads((folder / "manifest.json").read_text())
            self.assertEqual(manifest["original_sha256"], digest(captured["original"]))
            self.assertEqual(manifest["version_id"], receipt.version_id)
            staged = json.loads(self.store.setting("drive_staged:" + receipt.version_id))
            self.assertEqual(staged["status"], "local_staged")
            self.assertEqual(staged["remote_copy"], "unconfirmed")
            self.assertEqual(self.store.receipt(receipt.version_id).remote_copy, "unconfirmed")
        self.assertEqual(len(list((self.library / "Library/Sources").glob("*.md"))), 1)
        self.assertFalse(list(self.library.rglob("*.sqlite3")))
        self.assertFalse(list(self.library.rglob("*.db")))

    def test_repeat_export_checks_existing_bytes_without_changing_them(self):
        receipt = self.capture()
        export_versions(self.store, self.library)
        path = self.library / "Corpus" / receipt.source_id / receipt.version_id / "original"
        before = path.stat()
        result = export_versions(self.store, self.library)
        after = path.stat()
        self.assertEqual(result["already_present"], 1)
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))

    def test_export_conflict_preserves_existing_bytes_and_revokes_local_staging_claim(self):
        receipt = self.capture()
        export_versions(self.store, self.library)
        folder = self.library / "Corpus" / receipt.source_id / receipt.version_id
        (folder / "original").write_bytes(b"synthetic conflicting sync bytes")
        result = export_versions(self.store, self.library)
        self.assertEqual(result["errors"][0]["code"], "library_existing_bytes_conflict")
        self.assertEqual((folder / "original").read_bytes(), b"synthetic conflicting sync bytes")
        self.assertEqual(json.loads(self.store.setting("drive_staged:" + receipt.version_id))["status"], "error")
        self.assertEqual(self.store.receipt(receipt.version_id).remote_copy, "unconfirmed")

    def test_manual_card_edit_is_not_overwritten(self):
        receipt = self.capture()
        export_versions(self.store, self.library)
        card = self.library / "Library/Sources" / (receipt.source_id + ".md")
        card.write_text("Owner's synthetic edit")
        result = export_versions(self.store, self.library)
        self.assertTrue(result["errors"])
        self.assertEqual(card.read_text(), "Owner's synthetic edit")

    def test_partial_publication_recovers_after_process_failure_without_overwrite(self):
        receipt = self.capture()
        real_link = os.link
        calls = 0
        def interrupt(src, dst, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic interrupted filesystem")
            return real_link(src, dst, **kwargs)
        with patch("olympus.library.os.link", side_effect=interrupt):
            result = export_versions(self.store, self.library)
        self.assertTrue(result["errors"])
        folder = self.library / "Corpus" / receipt.source_id / receipt.version_id
        self.assertTrue((folder / "original").is_file())
        self.assertFalse((folder / "manifest.json").exists())
        before = (folder / "original").stat().st_ino
        result = export_versions(Store(self.store.root), self.library)
        self.assertFalse(result["errors"])
        self.assertEqual((folder / "original").stat().st_ino, before)
        self.assertTrue((folder / "manifest.json").is_file())

    def test_parallel_exporters_never_clobber_or_duplicate(self):
        receipt = self.capture()
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: export_versions(Store(self.store.root), self.library), range(3)))
        self.assertTrue(all(not r["errors"] for r in results))
        self.assertEqual(sum(r["exported"] for r in results), 1)
        self.assertEqual(len(list((self.library / "Corpus" / receipt.source_id).iterdir())), 1)

    def test_export_limit_advances_cursor_and_eventually_rechecks(self):
        receipts = [self.capture(text="Synthetic version " + str(i)) for i in range(3)]
        results = [export_versions(self.store, self.library, limit=1) for _ in range(3)]
        self.assertEqual(sum(r["exported"] for r in results), 3)
        self.assertTrue(results[-1]["cycle_complete"])
        self.assertEqual(export_versions(self.store, self.library, limit=1)["already_present"], 1)
        for receipt in receipts:
            self.assertIsNotNone(self.store.setting("drive_staged:" + receipt.version_id))

    def test_forgotten_sources_not_newly_exported_old_copies_not_deleted(self):
        first = self.capture()
        export_versions(self.store, self.library)
        self.store.forget(first.source_id, "Explicit synthetic owner decision")
        result = export_versions(self.store, self.library)
        self.assertEqual(result["checked"], 0)
        self.assertTrue((self.library / "Corpus" / first.source_id / first.version_id / "original").exists())

    def test_superseded_originals_are_retained_as_history(self):
        old = self.capture()
        new = self.capture(text="Explicit replacement")
        self.store.supersede(old.version_id, new.version_id, "Explicit synthetic decision")
        result = export_versions(self.store, self.library)
        self.assertEqual(result["exported"], 2)
        self.assertEqual(self.store.receipt(old.version_id).memory, "superseded")

    def test_export_symlinks_and_unexpected_sync_copies_are_visible(self):
        receipt = self.capture()
        export_versions(self.store, self.library)
        folder = self.library / "Corpus" / receipt.source_id / receipt.version_id
        (folder / "text (conflicted copy).txt").write_text("synthetic")
        result = export_versions(self.store, self.library)
        self.assertEqual(result["errors"][0]["code"], "library_unexpected_version_files")
        (folder / "text (conflicted copy).txt").unlink()
        (folder / "text.txt").unlink()
        (folder / "text.txt").symlink_to(self.base / "outside")
        result = export_versions(self.store, self.library)
        self.assertEqual(result["errors"][0]["code"], "library_file_symlink")

    def test_note_capture_repeat_rename_and_edit_keep_source_and_versions(self):
        original = self.note()
        first = self.scan()
        receipt = first["versions"][0]
        self.assertEqual(first["captured"], 1)
        self.assertEqual(self.scan()["unchanged"], 1)
        renamed = original.with_name("renamed.md")
        original.rename(renamed)
        second = self.scan()
        self.assertEqual(second["renamed"], 1)
        self.assertEqual(second["versions"][0]["source_id"], receipt["source_id"])
        self.assertEqual(second["versions"][0]["version_id"], receipt["version_id"])
        renamed.write_text("Edited synthetic note with new evidence.")
        third = self.scan()
        self.assertEqual(third["captured"], 1)
        self.assertEqual(third["versions"][0]["source_id"], receipt["source_id"])
        self.assertNotEqual(third["versions"][0]["version_id"], receipt["version_id"])
        self.assertEqual(self.store.status()["versions"], 2)
        self.assertFalse(self.store.pending_changes())
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM versions WHERE active=1").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM locations").fetchone()[0], 2)

    def test_editor_atomic_replacement_keeps_source_identity(self):
        path = self.note()
        first = self.scan()["versions"][0]
        temp = path.with_name(".editor-tmp")
        temp.write_text("Synthetic edited note saved atomically")
        temp.replace(path)
        result = self.scan()
        self.assertEqual(result["versions"][0]["source_id"], first["source_id"])
        self.assertEqual(result["captured"], 1)

    def test_rename_with_new_file_at_old_path_does_not_steal_old_source(self):
        path = self.note()
        old = self.scan()["versions"][0]
        path.rename(path.with_name("moved.md"))
        path.write_text("New unrelated synthetic note at old path")
        result = self.scan()
        self.assertEqual(len({v["source_id"] for v in result["versions"]}), 2)
        self.assertIn(old["source_id"], {v["source_id"] for v in result["versions"]})
        self.assertEqual(self.store.status()["versions"], 2)

    def test_identity_reservation_survives_failure_after_store_capture(self):
        self.note()
        real_capture = self.store.capture
        def fail_after_capture(**kwargs):
            real_capture(**kwargs)
            raise OSError("Synthetic lost response")
        with patch.object(self.store, "capture", side_effect=fail_after_capture):
            first = self.scan()
        self.assertIn("note_read_failed", self.codes(first))
        second = self.scan()
        self.assertEqual(len(second["versions"]), 1)
        self.assertEqual(self.store.status()["versions"], 1)

    def test_disappearance_is_recorded_but_never_forgets_or_deletes(self):
        path = self.note()
        first = self.scan()["versions"][0]
        path.unlink()
        result = self.scan()
        self.assertEqual(result["missing"], [first["source_id"]])
        self.assertFalse(self.store.pending_changes())
        self.assertEqual(self.store.read_version(first["version_id"])["text"], "Original synthetic note.")
        self.assertEqual(self.store.receipt(first["version_id"]).memory, "pending")

    def test_explicit_selection_never_reads_other_subtrees_or_whole_drive(self):
        self.note("Inbox/allowed.md", "Allowed synthetic text")
        self.note("Library/Personal/unselected.md", "Unselected private text")
        self.note("Library/Research/unselected.md", "Unselected research")
        self.note("Other/unselected.md", "Unselected other folder")
        result = self.scan(selected=["Inbox"])
        self.assertEqual(result["captured"], 1)
        self.assertEqual(self.store.status()["versions"], 1)
        for selected in ([], ["."], ["../"], ["Library"], ["Library/Research"], "Inbox", [["Inbox"]]):
            with self.assertRaises(PreservationError):
                self.scan(selected=selected)

    def test_scan_bound_is_visible_and_never_marks_unseen_paths_missing(self):
        for i in range(3):
            self.note(f"Inbox/{i}.md", f"Synthetic note {i}")
        self.scan()
        result = self.scan(limit=1)
        self.assertFalse(result["complete_scan"])
        self.assertIn("scan_limit_reached", self.codes(result))
        self.assertFalse(result["missing"])

    def test_binary_pdf_bad_encoding_empty_and_sync_conflicts_are_pending(self):
        self.note("Inbox/document.pdf", "Not a PDF extractor")
        self.note("Inbox/empty.md", "")
        self.note("Inbox/note (conflicted copy).md", "Unmerged branch")
        self.note("Inbox/merge.md", "<<<<<<< branch-a\none\n=======\ntwo\n>>>>>>> branch-b")
        self.note("Inbox/binary.txt", "\x00\x01")
        (self.library / "Inbox/encoding.txt").write_bytes(b"\xff\xfe")
        result = self.scan()
        self.assertEqual(result["captured"], 0)
        self.assertTrue({"note_type_pending_extraction", "empty_note_pending", "note_sync_conflict",
                         "note_merge_conflict", "binary_note_unsupported", "note_encoding_unsupported"} <= self.codes(result))
        self.assertEqual(self.store.status()["versions"], 0)

    def test_known_secrets_never_enter_store_or_diagnostics(self):
        secret = "github_pat_" + "a" * 40
        self.note(text="Synthetic credential " + secret)
        result = self.scan()
        self.assertIn("credential_pattern_detected", self.codes(result))
        self.assertEqual(self.store.status()["versions"], 0)
        self.assertNotIn(secret, json.dumps(result))
        self.assertFalse(list(self.store.versions.iterdir()))

    def test_size_limit_missing_selected_folder_and_symlink_are_visible(self):
        path = self.note(text="Too large for selected test bound")
        self.assertIn("note_size_limit", self.codes(self.scan(max_bytes=2)))
        link = path.with_name("link.md")
        link.symlink_to(path)
        self.assertIn("note_symlink_not_followed", self.codes(self.scan()))
        (self.library / "Library/Personal").rmdir()
        result = self.scan()
        self.assertFalse(result["complete_scan"])
        self.assertIn("selected_directory_unavailable", self.codes(result))

    def test_symlink_parent_cannot_escape_explicit_personal_subtree(self):
        outside = self.base / "outside/Personal"
        outside.mkdir(parents=True)
        (outside / "private.md").write_text("must not read")
        (self.library / "Library/Personal").rmdir()
        (self.library / "Library").rmdir()
        (self.library / "Library").symlink_to(outside.parent, target_is_directory=True)
        result = self.scan(selected=["Library/Personal"])
        self.assertEqual(result["captured"], 0)
        self.assertIn("selected_directory_unavailable", self.codes(result))

    def test_hardlinks_are_ambiguous_not_two_sources(self):
        path = self.note()
        os.link(path, path.with_name("alias.md"))
        result = self.scan()
        self.assertEqual(result["captured"], 0)
        self.assertIn("note_identity_has_multiple_paths", self.codes(result))

    def test_forgotten_note_and_exact_copy_do_not_reenter_under_new_identity(self):
        path = self.note()
        first = self.scan()["versions"][0]
        self.store.forget(first["source_id"], "Explicit synthetic owner decision")
        self.note("Inbox/copy.md", path.read_text())
        result = self.scan()
        self.assertEqual(result["captured"], 0)
        self.assertIn("forgotten_note_content_requires_review", self.codes(result))
        self.assertEqual(self.store.status()["versions"], 1)

    def test_scope_isolates_source_identity(self):
        self.note()
        one = self.scan(scope="project-one")["versions"][0]
        two = self.scan(scope="project-two")["versions"][0]
        self.assertNotEqual(one["source_id"], two["source_id"])

    def test_live_store_and_library_must_be_separate(self):
        with self.assertRaises(PreservationError):
            export_versions(self.store, self.store.root)
        with self.assertRaises(PreservationError):
            scan_notes(self.store, self.store.root, selected=["Inbox"], scope="one")


if __name__ == "__main__":
    unittest.main()
