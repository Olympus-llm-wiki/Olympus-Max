#!/usr/bin/env python3
"""Bind explicit verification commands to an Olympus workspace snapshot."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT / "src"))

from olympus import projects as p, workspaces as w
from olympus.preservation import Store, PreservationError, canonical, timestamp


def current(store, work_id):
    saved = w.resume(store, work_id)
    if saved["record"]["status"] == "complete":
        raise PreservationError("workspace_already_complete")
    prepared = saved.get("current")
    if not prepared or prepared["state"] != "ready":
        raise PreservationError("development_context_not_ready")
    return saved, prepared


def code_resource(context, cwd):
    matches = [r for r in context["resources"] if r["role"] == "code"
               and r.get("execution_root") and cwd.is_relative_to(Path(r["execution_root"]))]
    if not matches:
        raise PreservationError("development_check_cwd_outside_code")
    return max(matches, key=lambda r: len(r["execution_root"]))


def check(store, work_id, *, cwd, argv, output, timeout=120):
    """Run only caller-supplied argv; never execute a saved map action or note."""
    cwd, output = Path(cwd).resolve(), Path(output).absolute()
    saved, before = current(store, work_id)
    resource = code_resource(before["context"], cwd)
    for r in before["context"]["resources"]:
        if r.get("execution_root") and output.resolve().is_relative_to(Path(r["execution_root"])):
            raise PreservationError("development_receipt_inside_resource")
    if not argv or not 1 <= timeout <= 3600:
        raise PreservationError("development_invalid_check")
    for arg in p.sequence(argv, 50):
        p.string(arg)
    p.guard_no_secrets(canonical(argv))
    # Reserve the exact receipt before executing the command; do not overwrite it.
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as receipt_file:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        try:
            result = subprocess.run(argv, cwd=cwd, env=env, timeout=timeout,
                                    stdout=sys.stderr, stderr=sys.stderr)
            exit_code, error = result.returncode, None
        except subprocess.TimeoutExpired:
            exit_code, error = None, "development_check_timeout"
        except OSError:
            exit_code, error = None, "development_check_launch_failed"
        try:
            latest, after = current(store, work_id)
            stable = (saved["version_id"] == latest["version_id"]
                      and before["context_sha256"] == after["context_sha256"])
        except (PreservationError, OSError):
            stable = False
            error = error or "development_context_unavailable_after_check"
        receipt = {"schema": 1, "work_id": work_id, "workspace_version": saved["version_id"],
                   "context_sha256": before["context_sha256"], "stable": stable, "error": error,
                   "check": {"argv": argv, "cwd": str(cwd), "exit_code": exit_code,
                             "checked_at": timestamp(), "git_head": resource["git"]["head"],
                             "note": "Executed by olympus-development; bounded workspace snapshot before/after."}}
        p.guard_no_secrets(canonical(receipt))
        receipt_file.write(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    return receipt


def finish(store, work_id, *, update, receipts):
    """Reuse workspace's version/finish gates after checking fresh receipts."""
    if not receipts or "checks" in update:
        raise PreservationError("development_fresh_receipts_required")
    if "selection" in update or "intent" in update:
        raise PreservationError("development_scope_update_requires_checkpoint")
    saved, prepared = current(store, work_id)
    checks = []
    for receipt in receipts:
        p.shape(receipt, {"schema", "work_id", "workspace_version", "context_sha256",
                          "stable", "error", "check"})
        if receipt["schema"] != 1 or receipt["work_id"] != work_id:
            raise PreservationError("development_receipt_wrong_work")
        if receipt["workspace_version"] != saved["version_id"]:
            raise PreservationError("workspace_version_conflict")
        if receipt["context_sha256"] != prepared["context_sha256"]:
            raise PreservationError("development_checks_stale")
        if receipt["stable"] is not True or receipt["error"] is not None:
            raise PreservationError("development_check_unstable")
        w._validate_update({"checks": [receipt["check"]]})
        if receipt["check"]["exit_code"] != 0:
            raise PreservationError("development_check_failed")
        resource = code_resource(prepared["context"], Path(receipt["check"]["cwd"]).resolve())
        if receipt["check"]["git_head"] != resource["git"]["head"]:
            raise PreservationError("development_checks_stale")
        checks.append(receipt["check"])
    return w.checkpoint(store, work_id, {**update, "checks": checks},
                        expected=saved["version_id"], finish=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    commands = parser.add_subparsers(dest="action", required=True)
    run = commands.add_parser("check", epilog="Pass the verification executable and arguments after --.")
    run.add_argument("work_id")
    run.add_argument("--cwd", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path)
    run.add_argument("--timeout", type=int, default=120)
    done = commands.add_parser("finish")
    done.add_argument("work_id")
    done.add_argument("--update", required=True, type=Path)
    done.add_argument("--receipt", action="append", required=True, type=Path)
    raw = sys.argv[1:]
    boundary = raw.index("--") if "--" in raw else len(raw)
    argv = raw[boundary + 1:]
    args = parser.parse_args(raw[:boundary])
    if args.action != "check" and argv:
        parser.error("Only check accepts a command after --")
    try:
        store = Store(args.state)
        if args.action == "check":
            result = check(store, args.work_id, cwd=args.cwd, argv=argv,
                           output=args.output, timeout=args.timeout)
            ok = result["stable"] and result["error"] is None and result["check"]["exit_code"] == 0
        else:
            result = finish(store, args.work_id, update=p.load_input(args.update),
                            receipts=[p.load_input(path) for path in args.receipt])
            ok = True
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    except (PreservationError, OSError) as exc:
        print(json.dumps({"error": str(exc) if isinstance(exc, PreservationError)
                          else "development_receipt_io_error"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
