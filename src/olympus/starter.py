"""Portable connection profile; no credentials, personal runtime or corpus are bundled."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .hindsight import HindsightClient, HindsightError
from .preservation import Store, PreservationError, canonical, guard_no_secrets

PROJECT = Path(__file__).resolve().parents[2]
PROFILE = PROJECT / "starter.local.json"
SUPPORTED = {"capture-file", "capture-url", "status", "receipt", "recall", "search", "grant-budget",
             "pause-models", "work", "research", "learning", "retry", "sync-tasks", "register-task"}


def profile() -> dict:
    if not PROFILE.is_file() or PROFILE.is_symlink():
        raise PreservationError("run_starter_setup_first")
    data = json.loads(PROFILE.read_text())
    if set(data) != {"schema", "state", "api_url", "bank"} or data["schema"] != 1:
        raise PreservationError("invalid_starter_profile")
    if not all(isinstance(data[k], str) and data[k] for k in ("state", "api_url", "bank")):
        raise PreservationError("invalid_starter_profile")
    if Path(data["state"]).resolve().is_relative_to(PROJECT.resolve()):
        raise PreservationError("state_must_be_outside_repository")
    HindsightClient(data["api_url"], data["bank"], api_key_env="OLYMPUS_HINDSIGHT_API_KEY")
    return data


def main(argv=None) -> int:
    from . import cli
    import sys
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv == ["--help"] or argv == ["-h"]:
        print("Olympus Max starter — connect to an existing Hindsight API.")
        print("First: python3 starter.py setup; then python3 starter.py doctor --live")
        print("Commands: " + ", ".join(sorted(SUPPORTED)))
        print("Use python3 max.py COMMAND --help for command arguments.")
        return 0
    try:
        data = profile()
        # Inherited settings from another Olympus must not redirect this starter.
        os.environ.update(OLYMPUS_STATE_DIR=data["state"], OLYMPUS_API_URL=data["api_url"],
                          OLYMPUS_BANK_ID=data["bank"], OLYMPUS_API_KEY_ENV="OLYMPUS_HINDSIGHT_API_KEY")
        args = cli.parser().parse_args(argv)
        if args.command not in SUPPORTED:
            raise PreservationError("command_requires_managed_runtime_not_in_connection_starter")
        store = Store(args.state)
        if store.setting("starter_profile") != "olympus-max" or store.setting("runtime_supervision", "off") != "off":
            raise PreservationError("starter_state_profile_mismatch")
        if args.command == "work" and (store.root / "recovery/readiness.json").exists():
            raise PreservationError("managed_backup_state_not_supported_by_connection_starter")
        return cli.main(argv)
    except (PreservationError, HindsightError) as exc:
        print(json.dumps({"error": str(exc) if isinstance(exc, PreservationError) else exc.code}))
        return 2
    except (OSError, ValueError, TypeError):
        print(json.dumps({"error": "starter_configuration_error"}))
        return 2


def setup_main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Olympus Max starter setup; credentials stay in the process environment")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("setup")
    p.add_argument("--state", type=Path, default=Path.home() / ".local/state/olympus-max")
    p.add_argument("--api-url", default="http://127.0.0.1:8888")
    p.add_argument("--bank", default="olympus-max")
    p = sub.add_parser("doctor")
    p.add_argument("--live", action="store_true", help="Read-only check against the explicitly configured API/bank")
    args = parser.parse_args(argv)
    try:
        if args.command == "setup":
            state = args.state.expanduser().resolve()
            if state.is_relative_to(PROJECT.resolve()):
                raise PreservationError("state_must_be_outside_repository")
            data = {"schema": 1, "state": str(state), "api_url": args.api_url, "bank": args.bank}
            guard_no_secrets(canonical(data))
            HindsightClient(args.api_url, args.bank, api_key_env="OLYMPUS_HINDSIGHT_API_KEY")
            if PROFILE.exists():
                if profile() != data:
                    raise PreservationError("existing_starter_profile_differs")
            else:
                if state.exists() and any(state.iterdir()):
                    raise PreservationError("new_empty_state_directory_required")
                store = Store(state)
                store.set_setting("starter_profile", "olympus-max")
                store.set_setting("runtime_supervision", "off")
                fd = os.open(PROFILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(canonical(data))
                    stream.flush()
                    os.fsync(stream.fileno())
            result = {"status": "configured", **data, "api_contacted": False, "credentials_saved": False}
        else:
            data = profile()
            result = {"profile": "valid", "bank": data["bank"], "api_url": data["api_url"],
                      "api_key_available": bool(os.environ.get("OLYMPUS_HINDSIGHT_API_KEY")),
                      "api_contacted": args.live}
            if args.live:
                client = HindsightClient(data["api_url"], data["bank"], api_key_env="OLYMPUS_HINDSIGHT_API_KEY")
                client.list_operations(limit=1)
                result.update(api_health="reachable", bank_access="verified", model_access="not_tested")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (PreservationError, HindsightError) as exc:
        print(json.dumps({"error": str(exc) if isinstance(exc, PreservationError) else exc.code,
                          **({"http_status": exc.status} if isinstance(exc, HindsightError) else {})}))
        return 2
    except (OSError, ValueError, TypeError):
        print(json.dumps({"error": "starter_configuration_error"}))
        return 2
