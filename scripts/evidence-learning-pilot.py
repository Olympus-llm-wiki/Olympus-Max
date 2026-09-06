#!/usr/bin/env python3
"""Run a synthetic, network-disabled CLI workflow in a new isolated directory."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from olympus.evidence import base_policy
from olympus.learning import approval_binding
from olympus.preservation import canonical, digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    out = args.output.expanduser().resolve()
    if out.exists():
        parser.error("output must be a new directory")
    out.mkdir(parents=True, mode=0o700)
    state, inputs, blocker = out / "state", out / "inputs", out / "no-network"
    inputs.mkdir()
    blocker.mkdir()
    (blocker / "sitecustomize.py").write_text(
        "import socket\n"
        "def deny(*args, **kwargs): raise RuntimeError('network_disabled_for_pilot')\n"
        "socket.socket.connect = deny\nsocket.socket.connect_ex = deny\n"
        "socket.create_connection = deny\nsocket.getaddrinfo = deny\n")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(blocker), str(PROJECT / "src")]))
    scope = "evidence-learning-pilot"

    def run(*parts):
        result = subprocess.run([sys.executable, "-m", "olympus.cli", "--state", str(state), *map(str, parts)],
            cwd=PROJECT, env=env, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return json.loads(result.stdout)

    def data_file(name, data):
        path = inputs / (name + ".json")
        path.write_bytes(canonical(data))
        return path

    def capture(name, body, role, metadata=None):
        path = inputs / (name + ".md")
        path.write_text(body)
        meta = data_file(name + "-metadata", {"synthetic": "true", **(metadata or {})})
        return run("capture-file", path, "--source-key", "pilot:" + name, "--scope", scope,
                   "--title", "Synthetic pilot: " + name, "--role", role, "--archive-only",
                   "--text-file", path, "--metadata", meta)["version_id"]

    def approval(name, tokens):
        return capture(name, "Synthetic approval fixture, not a real owner decision.\n" + "\n".join(tokens), "decision",
            {"decision_status": "confirmed", "owner_confirmed_at": "2026-09-06T00:00:00Z",
             "decision_scope": scope, "owner_confirmation_evidence": "synthetic isolated CLI pilot",
             "decision_action": "approve", "approval_binding": approval_binding(scope, tokens)})

    run("init")
    source_quote = "A saved original remains available for verification."
    packages, cases = {}, []
    for name, metadata, expected, codes in (
        ("dated", {"source_date": "2026-09-01"}, "ready_for_review", []),
        ("undated", {}, "held", ["source_date_missing"]),
    ):
        source = capture(name + "-original", source_quote, "primary", metadata)
        claim = "Attributed synthetic assertion for " + name + "."
        report = capture(name + "-report", claim, "synthesis")
        quote = {"source_version": source, "quote": source_quote}
        manifest = {"schema": 1, "id": name, "scope": scope, "report_version": report,
                    "claims": [{"id": "c1", "type": "factual", "statement": claim, "evidence": [quote]}]}
        manifest_path = data_file(name + "-package", manifest)
        package = run("research", "register", manifest_path)
        repeated = run("research", "register", manifest_path)
        assert package["receipt"]["version_id"] == repeated["receipt"]["version_id"]
        packages[name] = package["receipt"]["version_id"]
        case = {"schema": 1, "id": name + "-case", "scope": scope,
                "summary": "Synthetic policy benchmark: " + name,
                "observations": [{"outcome": "observation", "statement": "Check the date requirement.", "evidence": [0]}],
                "evidence": [quote], "benchmark": {"package_version": packages[name], "expected_state": expected,
                                                    "expected_issue_codes": codes}}
        unreviewed = run("learning", "record", data_file(name + "-case", case))
        assert unreviewed["status"] == "assistant_assessment"
        case["owner_review"] = approval(name + "-owner", [unreviewed["confirmation_token"]])
        reviewed = run("learning", "record", data_file(name + "-reviewed-case", case))
        cases.append(reviewed["receipt"]["version_id"])
    candidate = {"id": "require-dates", "scope": scope, "policy": {**base_policy(), "require_source_dates": True},
                 "support_cases": cases}
    proposed = run("learning", "propose", data_file("candidate", candidate))
    pid, checked = proposed["receipt"]["version_id"], proposed["evaluation"]
    assert checked["state"] == "ready_for_owner_review" and checked["improved"] == 1 and checked["regressed"] == 0
    evaluated = run("learning", "check", pid)
    assert evaluated["evaluation_sha256"] == checked["evaluation_sha256"]
    owner = approval("activation-owner", ["activate-research-policy", pid, checked["evaluation_sha256"]])
    active = run("learning", "activate", pid, "--expected-evaluation", checked["evaluation_sha256"], "--approval-version", owner)
    again = run("learning", "activate", pid, "--expected-evaluation", checked["evaluation_sha256"], "--approval-version", owner)
    assert again["state"] == "already_active"
    after = run("research", "check", packages["undated"])
    assert after["state"] == "held"
    current = run("learning", "policy", "--scope", scope)
    reset_owner = approval("reset-owner", ["reset-research-policy", scope, current["activation_version"], current["policy_sha256"]])
    reset = run("learning", "reset", "--scope", scope, "--expected-policy", current["policy_sha256"], "--approval-version", reset_owner)
    assert reset["state"] == "reset"
    assert run("research", "check", packages["undated"])["state"] == "ready_for_review"
    status = run("status")
    summary = {"schema": 1, "synthetic_only": True, "network_disabled": True, "native_hindsight_calls": 0,
               "human_approvals_simulated": True, "scope": scope, "source_versions": status["versions"],
               "pending_model_operations": status["delivery"].get("pending", 0),
               "package_repeat_stable": True, "reviewed_cases": len(cases), "improved": checked["improved"],
               "regressions": checked["regressed"], "activation_repeat_stable": True, "reset_roundtrip": True,
               "model_quality_improvement_proven": False}
    (out / "result.json").write_bytes(canonical(summary))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
