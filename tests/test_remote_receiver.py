import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/receive-remote-file.py"


class ReceiverTests(unittest.TestCase):
    def run_receiver(self, root, raw, *, expected=None, file_id="synthetic-file", truncate=False):
        meta = {"file_id": file_id, "bytes": len(raw), "expected_sha256": expected or hashlib.sha256(raw).hexdigest()}
        encoded = base64.b64encode(raw)
        if truncate:
            encoded = encoded[:-4]
        return subprocess.run([sys.executable, str(SCRIPT), str(root)],
                              input=json.dumps(meta).encode() + b"\n" + encoded,
                              capture_output=True, timeout=10)

    def test_large_binary_and_empty_file_are_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for raw in (bytes(range(256)) * 512, b""):
                result = self.run_receiver(root, raw)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual((root / "synthetic-file.bin").read_bytes(), raw)
                if raw:
                    self.assertNotIn(base64.b64encode(raw[:1024]), result.stdout)

    def test_bad_hash_and_partial_input_never_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for options in ({"expected": "0" * 64}, {"truncate": True}):
                result = self.run_receiver(root, b"complete synthetic payload", **options)
                self.assertEqual(result.returncode, 2)
                self.assertFalse((root / "synthetic-file.bin").exists())
                self.assertFalse(list(root.glob(".download-*")))

    def test_remote_id_cannot_escape_download_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_receiver(Path(tmp), b"sample", file_id="../escape")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout), {"error": "invalid_download_header"})


if __name__ == "__main__":
    unittest.main()
