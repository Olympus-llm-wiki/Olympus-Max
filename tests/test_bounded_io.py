import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

from olympus.bounded_io import DATALESS, FileUnavailable, fingerprint, guard_file, hash_file, read_file, scan_guard_file


class BoundedFileTests(unittest.TestCase):
    def test_reader_returns_complete_bytes_and_hash_without_following_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file"
            content = bytes(range(256)) * 655
            path.write_bytes(content)
            self.assertEqual(read_file(path, max_bytes=len(content)), content)
            self.assertEqual(hash_file(path), hashlib.sha256(content).hexdigest())
            alias = path.with_name("alias")
            alias.symlink_to(path)
            with self.assertRaisesRegex(FileUnavailable, "library_file_symlink"):
                hash_file(alias)

    def test_dataless_is_deferred_before_reader_process_or_hydration(self):
        info = types.SimpleNamespace(st_mode=0o100600, st_flags=DATALESS, st_size=16126)
        with patch.object(Path, "lstat", return_value=info), patch("olympus.bounded_io.subprocess.run") as child:
            with self.assertRaisesRegex(FileUnavailable, "materialization_pending"):
                read_file(Path("/synthetic/dataless"))
        child.assert_not_called()

    def test_timeout_is_typed_and_next_read_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file"
            path.write_bytes(b"complete synthetic bytes")
            with patch("olympus.bounded_io.subprocess.run", side_effect=subprocess.TimeoutExpired("reader", 0.01)):
                with self.assertRaisesRegex(FileUnavailable, "file_read_timeout"):
                    read_file(path, timeout=0.01)
            self.assertEqual(read_file(path), path.read_bytes())

    def test_complete_guard_detects_pattern_across_worker_read_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file"
            path.write_bytes(b" " * (1024**2 - 5) + b"github_pat_" + b"x" * 40)
            with self.assertRaisesRegex(FileUnavailable, "credential_pattern_detected"):
                guard_file(path)

    def test_guard_resumes_long_gap_without_persisting_source_fragments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file"
            path.write_bytes(b"password" + b" " * (2 * 1024**2) + b"=abcdefghijkl")
            first = scan_guard_file(path, window_bytes=1024**2)
            self.assertFalse(first["complete"])
            second = scan_guard_file(path, checkpoint=first, window_bytes=1024**2)
            self.assertFalse(second["complete"])
            with self.assertRaisesRegex(FileUnavailable, "credential_pattern_detected"):
                scan_guard_file(path, checkpoint=second, window_bytes=1024**2)


if __name__ == "__main__":
    unittest.main()
