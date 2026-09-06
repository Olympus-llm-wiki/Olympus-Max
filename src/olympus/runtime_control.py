"""Observe the owned Compose service and bound when its workers may run."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from .preservation import PreservationError, Store, timestamp


class RuntimeControl:
    def __init__(self, project_root: Path):
        self.root = project_root.resolve()
        self.compose = ["docker", "compose", "-f", str(self.root / "config/compose.yaml")]

    def _run(self, command: list[str], *, timeout: float = 15) -> subprocess.CompletedProcess:
        process = None
        try:
            process = subprocess.Popen(
                command, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True,
            )
            stdout, stderr = process.communicate(timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            if process is not None:
                # The shell may exit before descendants, even if they closed their
                # pipes. Always escalate against the entire session-owned group.
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(process.pid, sig)
                    except OSError:
                        pass
                    try:
                        process.wait(timeout=1)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                # Do not communicate() again: a descendant could retain a pipe.
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except OSError:
                            pass
            raise PreservationError("runtime_command_unavailable") from None
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        if result.returncode:
            raise PreservationError("runtime_command_failed")
        return result

    def snapshot(self) -> dict:
        container = self._run([*self.compose, "ps", "-q", "hindsight"]).stdout.strip()
        if not container:
            return {"running": False, "workers_stopped": True, "checked_at": timestamp()}
        if "\n" in container:
            raise PreservationError("unexpected_runtime_replicas")
        raw = self._run(["docker", "inspect", container]).stdout
        try:
            inspected = json.loads(raw)[0]
            variables = dict(item.split("=", 1) for item in inspected["Config"]["Env"] if "=" in item)
            # Only nonsecret assertions leave this method.
            return {
                "checked_at": timestamp(), "running": bool(inspected["State"]["Running"]),
                "container_id": container,
                "workers_stopped": variables.get("HINDSIGHT_API_WORKER_ENABLED") == "false",
                "observations_stopped": variables.get("HINDSIGHT_API_ENABLE_OBSERVATIONS") == "false",
                "provider_disabled": variables.get("HINDSIGHT_API_LLM_PROVIDER") == "none",
            }
        except (KeyError, IndexError, ValueError, TypeError):
            raise PreservationError("invalid_runtime_observation") from None

    def set_mode(self, mode: str) -> dict:
        if mode not in {"safe", "pilot"}:
            raise PreservationError("invalid_runtime_mode")
        current = self.snapshot()
        expected_stopped = mode == "safe"
        if (current["running"] and current.get("workers_stopped") == expected_stopped
                and current.get("provider_disabled") == expected_stopped):
            return current
        try:
            self._run(["bash", str(self.root / "scripts/runtime-up.sh"), mode], timeout=240)
        except PreservationError:
            # A failed attempt to stop model processing must fail closed.
            if mode == "safe":
                self._run([*self.compose, "stop", "hindsight"], timeout=45)
            raise
        observed = self.snapshot()
        if not observed["running"] or observed.get("workers_stopped") != expected_stopped:
            raise PreservationError("runtime_mode_not_observed")
        return observed

    def assert_quiet(self) -> dict:
        result = self.snapshot()
        if not result["running"] or not result.get("workers_stopped") or not result.get("provider_disabled"):
            raise PreservationError("runtime_not_quiet")
        # Caller holds Store.exclusive; the local delivery contract is the sole
        # writer. Native UI and bypassing Coding Agents mutations are disabled.
        return {**result, "writers_stopped": True}

    def supervise(self, store: Store) -> dict:
        if store.setting("runtime_supervision", "off") != "on":
            return {"managed": False}
        permit = (float(store.setting("budget_expires", "0")) > time.time()
                  and store.setting("maintenance", "off") == "off"
                  and store.setting("recovery_state", "ready") == "ready")
        mode = "pilot" if permit else "safe"
        result = self.set_mode(mode)
        store.set_setting("runtime_last_observation", json.dumps(result))
        return {"managed": True, "mode": mode, **result}
