import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import unittest

from olympus.preservation import Store, PreservationError
from olympus.runtime_control import RuntimeControl


class RuntimeTests(unittest.TestCase):
    def test_run_preserves_successful_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = RuntimeControl(Path(tmp))._run([
                sys.executable, "-c", "import os; print(os.getcwd()); print('diagnostic', file=__import__('sys').stderr)",
            ])
            self.assertEqual(Path(result.stdout.strip()), Path(tmp).resolve())
            self.assertEqual(result.stderr, "diagnostic\n")
            self.assertEqual(result.returncode, 0)

    def test_run_nonzero_error_does_not_disclose_command_or_output(self):
        marker = "synthetic-sensitive-command-output"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PreservationError) as raised:
                RuntimeControl(Path(tmp))._run([
                    sys.executable, "-c",
                    f"import sys; print('{marker}'); print('{marker}', file=sys.stderr); sys.exit(3)",
                ])
            self.assertEqual(str(raised.exception), "runtime_command_failed")
            self.assertNotIn(marker, "".join(traceback.format_exception(raised.exception)))

    @unittest.skipUnless(os.name == "posix", "requires process groups")
    def test_run_timeout_kills_shell_and_term_ignoring_descendant(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "child.py").write_text(
                "import os, signal, time\n"
                "from pathlib import Path\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "os.close(1)\n"
                "os.close(2)\n"
                "Path('child.pid').write_text(str(os.getpid()))\n"
                "while True: time.sleep(1)\n"
            )
            (root / "parent.sh").write_text(
                "echo $$ > parent.pid\n"
                '"$1" child.py &\n'
                "wait\n"
            )
            pids = []
            started = time.monotonic()
            try:
                with self.assertRaises(PreservationError) as raised:
                    RuntimeControl(root)._run(["sh", "parent.sh", sys.executable], timeout=1)
                self.assertEqual(str(raised.exception), "runtime_command_unavailable")
                self.assertLess(time.monotonic() - started, 4)
                pids = [int((root / name).read_text()) for name in ("parent.pid", "child.pid")]
                for pid in pids:
                    # A killed orphan can briefly remain a zombie until init reaps
                    # it; neither an absent process nor a zombie can keep working.
                    deadline = time.monotonic() + 2
                    while True:
                        state = subprocess.run(
                            ["ps", "-o", "stat=", "-p", str(pid)],
                            capture_output=True, text=True, timeout=1,
                        ).stdout.strip()
                        if not state or state.startswith("Z"):
                            break
                        self.assertLess(time.monotonic(), deadline, f"process {pid} survived timeout")
                        time.sleep(0.05)
            finally:
                for name in ("parent.pid", "child.pid"):
                    path = root / name
                    if path.exists():
                        try:
                            os.kill(int(path.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_no_control_when_not_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state")
            runtime = RuntimeControl(Path(tmp))
            runtime._run = lambda *args, **kwargs: self.fail("unexpected process")
            self.assertFalse(runtime.supervise(store)["managed"])

    def test_unknown_or_expired_budget_selects_safe_even_when_remaining_positive(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state")
            store.set_setting("runtime_supervision", "on")
            store.set_setting("budget_remaining", "100")
            runtime = RuntimeControl(Path(tmp))
            modes = []
            def set_mode(mode):
                modes.append(mode)
                return {"running": True, "workers_stopped": mode == "safe"}
            runtime.set_mode = set_mode
            runtime.supervise(store)
            self.assertEqual(modes, ["safe"])

    def test_recovery_barrier_overrides_active_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            import time
            store = Store(Path(tmp) / "state")
            store.set_setting("runtime_supervision", "on")
            store.set_setting("budget_expires", str(time.time() + 3600))
            store.set_setting("recovery_state", "blocked")
            runtime = RuntimeControl(Path(tmp))
            modes = []
            runtime.set_mode = lambda mode: modes.append(mode) or {"running": True}
            runtime.supervise(store)
            self.assertEqual(modes, ["safe"])

    def test_quiet_assertion_requires_running_safe_process(self):
        runtime = RuntimeControl(Path("/tmp"))
        for value in [{"running": False, "workers_stopped": True},
                      {"running": True, "workers_stopped": False, "provider_disabled": False}]:
            runtime.snapshot = lambda: value
            with self.assertRaises(PreservationError):
                runtime.assert_quiet()


if __name__ == "__main__":
    unittest.main()
