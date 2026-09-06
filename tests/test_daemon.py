import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from olympus.preservation import Store


class DaemonTests(unittest.TestCase):
    def test_collector_runs_without_codex_or_credentials_and_persists_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            env = dict(os.environ)
            for key in list(env):
                if "KEY" in key or "TOKEN" in key:
                    env.pop(key, None)
            process = subprocess.Popen([sys.executable, "-m", "olympus", "--state", str(state),
                                        "daemon", "--mode", "collect", "--interval", "5"],
                                       env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 8
                observed = None
                while time.monotonic() < deadline and process.poll() is None:
                    if (state / "registry.sqlite3").exists():
                        observed = Store(state).setting("heartbeat:collect")
                        if observed:
                            break
                    time.sleep(0.1)
                self.assertIsNotNone(observed)
            finally:
                process.terminate()
                stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(Store(state).setting("heartbeat:collect"), observed)


if __name__ == "__main__":
    unittest.main()
