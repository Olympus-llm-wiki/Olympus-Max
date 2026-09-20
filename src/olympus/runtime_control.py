"""Observe the owned Compose service and bound when its workers may run."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import time

from .preservation import PreservationError, Store, timestamp
from .admission import permission


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
            separated = inspected['Config'].get('Labels', {}).get('io.olympus.runtime-role') == 'read-api'
            if separated:
                worker_id = self._run([*self.compose, '--profile', 'workers', 'ps', '-q', 'worker']).stdout.strip()
                if '\n' in worker_id:
                    raise PreservationError('unexpected_runtime_replicas')
                worker = json.loads(self._run(['docker', 'inspect', worker_id]).stdout)[0] if worker_id else None
                worker_running = bool(worker and worker['State']['Running'])
                worker_env = dict(item.split('=', 1) for item in worker['Config']['Env'] if '=' in item) if worker else {}
                api_safe = variables.get('HINDSIGHT_API_WORKER_ENABLED') == 'false' and variables.get('HINDSIGHT_API_LLM_PROVIDER') == 'none'
                if not api_safe:
                    raise PreservationError('read_api_model_execution_enabled')
                return {'checked_at': timestamp(), 'running': bool(inspected['State']['Running']),
                        'container_id': container, 'runtime_layout': 'separate_api_worker',
                        'adapter_sha256': inspected['Config'].get('Labels', {}).get('io.olympus.adapter-sha256'),
                        'api_ready': inspected['State'].get('Health', {}).get('Status') == 'healthy',
                        'worker_ready': worker_running and worker['State'].get('Health', {}).get('Status') == 'healthy',
                        'worker_container_id': worker_id or None, 'workers_stopped': not worker_running,
                        'observations_stopped': not worker_running, 'provider_disabled': not worker_running,
                        'api_model_provider_disabled': True,
                        'worker_slots': int(worker_env.get('HINDSIGHT_API_WORKER_MAX_SLOTS', '1')) if worker_running else 1,
                        'consolidation_reserved_slots': int(worker_env.get('HINDSIGHT_API_WORKER_CONSOLIDATION_RESERVED_SLOTS', '0')) if worker_running else 0}
            # Only nonsecret assertions leave this method.
            return {
                "checked_at": timestamp(), "running": bool(inspected["State"]["Running"]),
                "container_id": container,
                "workers_stopped": variables.get("HINDSIGHT_API_WORKER_ENABLED") == "false",
                "observations_stopped": variables.get("HINDSIGHT_API_ENABLE_OBSERVATIONS") == "false",
                "provider_disabled": variables.get("HINDSIGHT_API_LLM_PROVIDER") == "none",
                "worker_slots": int(variables.get("HINDSIGHT_API_WORKER_MAX_SLOTS", "1")),
                "consolidation_reserved_slots": int(variables.get("HINDSIGHT_API_WORKER_CONSOLIDATION_RESERVED_SLOTS", "0")),
            }
        except (KeyError, IndexError, ValueError, TypeError):
            raise PreservationError("invalid_runtime_observation") from None

    def set_mode(self, mode: str) -> dict:
        if mode not in {"safe", "pilot", "continuous"}:
            raise PreservationError("invalid_runtime_mode")
        current = self.snapshot()
        expected_stopped = mode == "safe"
        expected_slots = 2 if mode == "continuous" else 1
        expected_reserved = 1 if mode == "continuous" else 0
        if (current["running"] and current.get("workers_stopped") == expected_stopped
                and current.get('api_ready', True)
                and (expected_stopped or current.get('worker_ready', True))
                and current.get("provider_disabled") == expected_stopped
                and current.get("worker_slots") == expected_slots
                and current.get("consolidation_reserved_slots") == expected_reserved):
            return current
        try:
            self._run(["bash", str(self.root / "scripts/runtime-up.sh"), mode], timeout=240)
        except PreservationError:
            # A failed attempt to stop model processing must fail closed.
            if mode == "safe":
                if current.get('runtime_layout') == 'separate_api_worker':
                    self._run([*self.compose, '--profile', 'workers', 'stop', 'worker'], timeout=45)
                else:
                    self._run([*self.compose, "stop", "hindsight"], timeout=45)
            raise
        observed = self.snapshot()
        if (not observed["running"] or observed.get("workers_stopped") != expected_stopped
                or not observed.get('api_ready', True)
                or (not expected_stopped and not observed.get('worker_ready', True))
                or observed.get("provider_disabled") != expected_stopped
                or observed.get("worker_slots") != expected_slots
                or observed.get("consolidation_reserved_slots") != expected_reserved):
            raise PreservationError("runtime_mode_not_observed")
        return observed

    def assert_quiet(self) -> dict:
        result = self.snapshot()
        if not result["running"] or not result.get("workers_stopped") or not result.get("provider_disabled"):
            raise PreservationError("runtime_not_quiet")
        # The coordinator owns maintenance and rechecks this observation around
        # snapshot work. Native UI and bypassing Coding Agents writes are disabled.
        return {**result, "writers_stopped": True}

    def supervise(self, store: Store) -> dict:
        if store.setting("runtime_supervision", "off") != "on":
            return {"managed": False}
        access = permission(store)
        if store.setting('maintenance', 'off') == 'backup_snapshot':
            # The snapshot owner performs stop/resume under the lifecycle lock.
            # Do not race its finite quiet phase with another mode transition.
            try:
                owner = json.loads(store.setting('backup_maintenance_owner', '{}'))
                with store.connect() as db:
                    row = db.execute('SELECT state,token,lease_until FROM pipeline_jobs WHERE id=? AND kind=?',
                                     (owner.get('job_id'), 'backup.snapshot')).fetchone()
                live_owner = (owner.get('schema') == 1 and row is not None and row['state'] == 'running'
                              and row['token'] == owner.get('token') and row['lease_until'] > time.time()
                              and owner.get('expires_at', 0) > time.time())
            except (ValueError, TypeError, AttributeError, sqlite3.Error):
                live_owner = False
            if live_owner:
                result = self.snapshot()
                store.set_setting('runtime_last_observation', json.dumps(result))
                return {'managed': True, 'mode': 'backup_snapshot', 'reason': 'runtime_owned_by_backup', **result}
            # A dead owner cannot keep models running indefinitely. The snapshot
            # service alone clears its stale marker after observing quiet.
        mode = ("continuous" if access["mode"] == "continuous" else "pilot") if access["allowed"] else "safe"
        result = self.set_mode(mode)
        store.set_setting("runtime_last_observation", json.dumps(result))
        return {"managed": True, "mode": mode, **result}
