from contextlib import redirect_stdout
from email.message import Message
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from olympus.cli import _client, main, parser
from olympus.preservation import Store


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "olympus", "--state", str(self.state), *args],
                              capture_output=True, text=True, timeout=10)

    def test_capture_receipt_is_usable_in_fresh_process(self):
        source = self.root / "source.txt"
        source.write_text("Синтетический код: CLI-729.")
        p = self.run_cli("capture-file", str(source), "--source-key", "cli-test", "--scope", "synthetic", "--title", "CLI source")
        self.assertEqual(p.returncode, 0, p.stderr)
        receipt = json.loads(p.stdout)
        source.unlink()
        p = self.run_cli("receipt", receipt["version_id"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["local_capture"], "durable")
        p = self.run_cli("status")
        self.assertEqual(json.loads(p.stdout)["versions"], 1)

    def test_search_local_default_and_explicit_scope(self):
        store = Store(self.state)
        for scope in ('ugc', 'other'):
            store.capture(source_key=scope, scope=scope, title='Identity LoRA',
                          original=b'lora training dataset', text='lora training dataset')
        p = self.run_cli('search', 'лора', '--local-only')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(json.loads(p.stdout)['results']), 2)
        p = self.run_cli('search', 'лора', '--local-only', '--scope', 'ugc')
        self.assertEqual(len(json.loads(p.stdout)['results']), 1)

    def test_no_implicit_model_budget(self):
        p = self.run_cli("status")
        self.assertEqual(json.loads(p.stdout)["budget"]["remaining"], 0)

    def test_recall_timeout_reaches_transport_without_bypassing_scope_filter(self):
        store = Store(self.state)
        receipt = store.capture(source_key="recall", scope="synthetic", title="Recall source",
                                original=b"synthetic source", text="synthetic source")
        store.update_delivery(store.claim(), "searchable", units=1)
        native = {"results": [
            {"document_id": receipt.version_id, "chunk_id": "allowed-chunk", "text": "allowed fact"},
            {"document_id": "foreign-document", "chunk_id": "foreign-chunk", "text": "foreign fact"},
        ], "chunks": {"allowed-chunk": {"text": "synthetic source"},
                        "foreign-chunk": {"text": "foreign source"}}}
        for flags, expected_timeout in (([], 120.0), (["--timeout", "45"], 45.0)):
            with self.subTest(flags=flags):
                response = io.BytesIO(json.dumps(native).encode())
                response.status = 200
                response.headers = Message()
                response.headers["Content-Type"] = "application/json"
                output = io.StringIO()
                with patch.dict("os.environ", {"OLYMPUS_API_KEY_ENV": "SYNTHETIC_API_KEY", "SYNTHETIC_API_KEY": "synthetic-key"}), \
                        patch("urllib.request.OpenerDirector.open", return_value=response) as transport, redirect_stdout(output):
                    status = main(["--state", str(self.state), "recall", "synthetic query", "--scope", "synthetic", *flags])
                self.assertEqual(status, 0)
                self.assertEqual(transport.call_args.kwargs["timeout"], expected_timeout)
                request = transport.call_args.args[0]
                body = json.loads(request.data)
                self.assertEqual(body["tags"], ["olympus", "scope:synthetic"])
                self.assertEqual(body["tags_match"], "all_strict")
                result = json.loads(output.getvalue())
                self.assertEqual([row["document_id"] for row in result["results"]], [receipt.version_id])
                self.assertEqual(set(result["chunks"]), {"allowed-chunk"})

    def test_recall_timeout_is_bounded_and_other_clients_keep_short_timeout(self):
        for value in ("0", "-1", "301", "nan", "inf"):
            with self.subTest(value=value):
                result = self.run_cli("recall", "synthetic query", "--scope", "synthetic", "--timeout", value)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stderr), {"error": "invalid_timeout"})
        for command in ("work", "reconcile", "daemon"):
            with self.subTest(command=command):
                self.assertEqual(_client(parser().parse_args([command])).timeout, 15.0)

    def test_credential_file_rejected_without_reading_or_printing_it(self):
        source = self.root / "auth.json"
        source.write_text('{"secret":"private-value-never-in-output"}')
        p = self.run_cli("capture-file", str(source), "--source-key", "bad", "--scope", "synthetic", "--title", "bad")
        self.assertEqual(p.returncode, 2)
        self.assertNotIn("private-value-never-in-output", p.stdout + p.stderr)

    def test_env_variant_and_embedded_uri_credentials_are_rejected(self):
        for name in [".env.local", "note.txt"]:
            source = self.root / name
            source.write_text("DATABASE_URL=postgresql://user:synthetic-password@host/db")
            p = self.run_cli("capture-file", str(source), "--source-key", name, "--scope", "synthetic", "--title", "bad")
            self.assertEqual(p.returncode, 2)
            self.assertNotIn("synthetic-password", p.stdout + p.stderr)
        self.assertEqual(json.loads(self.run_cli("status").stdout)["versions"], 0)


if __name__ == "__main__":
    unittest.main()
