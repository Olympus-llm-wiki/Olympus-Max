"""Reviewed cases and bounded policy changes; no model-driven self-modification."""
from __future__ import annotations

import json
from pathlib import Path

from . import evidence as ev
from .materials import material_profile
from .preservation import Store, PreservationError, canonical, digest, timestamp


def policy_key(scope: str) -> str:
    return "research_policy:" + digest(ev.identifier(scope).encode())


def case_token(data: dict) -> str:
    return "review-case:" + digest(canonical({k: v for k, v in data.items() if k != "owner_review"}))


def approval_binding(scope: str, tokens: list[str]) -> str:
    return digest(canonical({"scope": scope, "tokens": tokens}))


def owner_confirmation(store: Store, vid: str, scope: str, tokens: list[str],
                       sources: ev.Sources | None = None) -> dict:
    source = (sources or ev.Sources(store)).get(vid)
    if (not material_profile(source)["current_owner_decision"] or source["scope"] != scope
            or source["metadata"].get("decision_scope") != scope
            or source["metadata"].get("decision_action") != "approve"
            or source["metadata"].get("approval_binding") != approval_binding(scope, tokens)
            or any(token not in source["text"] for token in tokens)):
        raise PreservationError("exact_owner_confirmation_required")
    return source


def validate_case(data: dict) -> None:
    ev.exact_shape(data, {"schema", "id", "scope", "summary", "observations", "evidence"},
                   {"benchmark", "owner_review"})
    if type(data["schema"]) is not int or data["schema"] != 1:
        raise PreservationError("unsupported_learning_schema")
    ev.identifier(data["id"])
    ev.identifier(data["scope"])
    ev.nonempty(data["summary"])
    if not isinstance(data["evidence"], list) or not 1 <= len(data["evidence"]) <= 20:
        raise PreservationError("learning_evidence_required")
    for entry in data["evidence"]:
        ev.exact_shape(entry, {"source_version", "quote"}, {"start"})
        ev.version_id(entry["source_version"])
        ev.nonempty(entry["quote"], 2000)
        if "start" in entry and (type(entry["start"]) is not int or entry["start"] < 0):
            raise PreservationError("invalid_quote_position")
    if not isinstance(data["observations"], list) or not 1 <= len(data["observations"]) <= 20:
        raise PreservationError("learning_observations_required")
    for observation in data["observations"]:
        ev.exact_shape(observation, {"outcome", "statement", "evidence"})
        if not isinstance(observation["outcome"], str) or observation["outcome"] not in {"success", "failure", "improvement", "observation"}:
            raise PreservationError("invalid_learning_outcome")
        ev.nonempty(observation["statement"])
        refs = observation["evidence"]
        if (not isinstance(refs, list) or not refs or
                any(type(n) is not int or not 0 <= n < len(data["evidence"]) for n in refs)):
            raise PreservationError("learning_observation_evidence_required")
    if "benchmark" in data:
        bench = data["benchmark"]
        ev.exact_shape(bench, {"package_version", "expected_state", "expected_issue_codes"})
        ev.version_id(bench["package_version"])
        codes = bench["expected_issue_codes"]
        if (not isinstance(bench["expected_state"], str) or bench["expected_state"] not in {"ready_for_review", "held"}
                or not isinstance(codes, list) or len(codes) > 30
                or any(not isinstance(c, str) or not c or len(c) > 100 for c in codes)
                or (bench["expected_state"] == "ready_for_review" and codes)
                or (bench["expected_state"] == "held" and not codes)):
            raise PreservationError("invalid_learning_benchmark")
    if "owner_review" in data:
        ev.version_id(data["owner_review"])


def checked_case(store: Store, vid: str, *, reviewed=False, current=True,
                 sources: ev.Sources | None = None) -> tuple[dict, dict]:
    cache = sources or ev.Sources(store)
    data, source = ev.read_record(store, vid, "learning_case", sources=cache, require_current=current)
    check_case_data(store, data, cache, reviewed=reviewed)
    return data, source


def check_case_data(store: Store, data: dict, cache: ev.Sources, *, reviewed=False) -> None:
    validate_case(data)
    for entry in data["evidence"]:
        original = cache.get(entry["source_version"])
        match = ev.quote_match(original["text"], entry["quote"], start=entry.get("start"), normalize=False)
        if match["status"] != "matched_exact":
            raise PreservationError("learning_evidence_no_longer_matches")
    if "benchmark" in data:
        package, _ = ev.read_record(store, data["benchmark"]["package_version"], "research_package", sources=cache)
        ev.validate_manifest(package)
        if package["scope"] != data["scope"]:
            raise PreservationError("learning_benchmark_scope_mismatch")
        cache.get(package["report_version"])
        for claim in package["claims"]:
            for entry in claim["evidence"]:
                cache.get(entry["source_version"])
    if reviewed and ("owner_review" not in data or "benchmark" not in data):
        raise PreservationError("reviewed_benchmark_required")
    if "owner_review" in data:
        owner_confirmation(store, data["owner_review"], data["scope"], [case_token(data)], cache)


def record_case(store: Store, data: dict) -> dict:
    validate_case(data)
    with store.exclusive():
        ev.assert_ready(store, data["scope"])
        cache = ev.Sources(store)
        check_case_data(store, data, cache)
        receipt = ev.save_record(store, "learning_case", data, role="assessment")
        return {"receipt": receipt, "status": "owner_reviewed" if "owner_review" in data else "assistant_assessment",
                "confirmation_token": case_token(data),
                "approval_binding": approval_binding(data["scope"], [case_token(data)]),
                "automatic_policy_change": False}


def from_migration(store: Store, plan: dict, package_id: str, *, record_id: str, scope: str) -> dict:
    from .migration import render_package_report, validate_plan
    with store.exclusive():
        ev.assert_ready(store, scope)
        validate_plan(store, plan)
        history = json.loads(store.setting("migration_assessment:" + plan["plan_id"] + ":" + package_id, "[]"))
        if not history:
            raise PreservationError("migration_assessment_required")
        rendered = render_package_report(store, plan, package_id)
        vid = rendered["receipt"]["version_id"]
        report = store.read_version(vid)
        latest = history[-1]
        entries = [("observation", item) for item in latest["observations"]]
        entries += [("improvement", item) for item in latest["proposed_improvements"]]
        if len(entries) > 20:
            raise PreservationError("learning_case_observation_limit")
        evidence, observations = [], []
        for outcome, item in entries:
            statement = item["statement"]
            start = report["text"].find(statement)
            if start < 0:
                raise PreservationError("migration_assessment_quote_missing")
            evidence.append({"source_version": vid, "quote": statement[:2000], "start": start})
            observations.append({"outcome": outcome, "statement": statement, "evidence": [len(evidence) - 1]})
        data = {"schema": 1, "id": record_id, "scope": scope,
                "summary": "Оценка переноса " + package_id + "; подтверждение владельца не переносится.",
                "observations": observations, "evidence": evidence}
        return record_case(store, data)


def raw_head(store: Store, scope: str) -> tuple[str | None, dict | None]:
    vid = store.setting(policy_key(scope))
    if vid is None:
        return None, None
    version = store.read_version(ev.version_id(vid))
    try:
        head = json.loads(version["original"])
        if (version["metadata"].get("artifact_type") != "learning_activation"
                or head["scope"] != scope or head["action"] not in {"activate", "reset"}):
            raise PreservationError("invalid_active_learning_policy")
        ev.validate_policy(head["policy"])
    except (ValueError, KeyError, TypeError):
        raise PreservationError("invalid_active_learning_policy") from None
    return vid, head


def effective_policy(store: Store, scope: str) -> dict:
    with store.exclusive():
        return _effective_policy(store, scope)


def _effective_policy(store: Store, scope: str) -> dict:
    ev.identifier(scope)
    ev.assert_ready(store, scope)
    baseline = ev.base_policy()
    vid, head = raw_head(store, scope)
    if head is None:
        return {"policy": baseline, "policy_sha256": digest(canonical(baseline)), "activation_version": None}
    cache = ev.Sources(store)
    ev.read_record(store, vid, "learning_activation", sources=cache)
    if head["baseline_sha256"] != digest(canonical(baseline)):
        raise PreservationError("learning_baseline_configuration_changed")
    owner_confirmation(store, head["approval_version"], scope, head["confirmation_tokens"], cache)
    if head["action"] == "activate":
        ev.read_record(store, head["proposal_version"], "learning_proposal", sources=cache)
        for case in head["case_versions"]:
            checked_case(store, case, reviewed=True, sources=cache)
    return {"policy": head["policy"], "policy_sha256": digest(canonical(head["policy"])),
            "activation_version": vid}


def preserves_limits(before: dict, after: dict) -> bool:
    ev.validate_policy(before)
    ev.validate_policy(after)
    return (not before["require_source_dates"] or after["require_source_dates"]) and (
        after["min_distinct_originals"] >= before["min_distinct_originals"]) and (
        before["allow_whitespace_normalization"] or not after["allow_whitespace_normalization"])


def list_cases(store: Store, scope: str) -> list[dict]:
    with store.exclusive():
        ev.assert_ready(store, scope)
        rows = []
        for vid in ev.registry(store, "learning_case", scope):
            try:
                data, _ = checked_case(store, vid)
                rows.append({"version_id": vid, "id": data["id"], "summary": data["summary"],
                             "status": "owner_reviewed" if "owner_review" in data else "assistant_assessment",
                             "benchmark": data.get("benchmark"), "confirmation_token": case_token(data)})
            except PreservationError as exc:
                rows.append({"version_id": vid, "status": "unavailable", "reason": str(exc)})
        return rows


def propose_policy(store: Store, data: dict) -> dict:
    ev.exact_shape(data, {"id", "scope", "policy", "support_cases"})
    ev.identifier(data["id"])
    ev.identifier(data["scope"])
    ev.validate_policy(data["policy"])
    if not isinstance(data["support_cases"], list) or not 2 <= len(data["support_cases"]) <= 50:
        raise PreservationError("two_distinct_learning_cases_required")
    for vid in data["support_cases"]:
        ev.version_id(vid)
    if len(set(data["support_cases"])) != len(data["support_cases"]):
        raise PreservationError("two_distinct_learning_cases_required")
    with store.exclusive():
        current = effective_policy(store, data["scope"])
        proposal = {"schema": 1, **data, "base_policy": current["policy"],
                    "base_policy_sha256": current["policy_sha256"],
                    "base_activation": current["activation_version"],
                    "baseline_sha256": digest(canonical(ev.base_policy()))}
        receipt = ev.save_record(store, "learning_proposal", proposal)
        return {"receipt": receipt, "evaluation": evaluate_policy(store, receipt["version_id"])}


def evaluate_policy(store: Store, vid: str) -> dict:
    with store.exclusive():
        proposal, _ = ev.read_record(store, vid, "learning_proposal", require_current=True)
        scope = proposal["scope"]
        current = effective_policy(store, scope)
        issues, results, reviewed = [], [], {}
        if (current["policy_sha256"] != proposal["base_policy_sha256"]
                or current["activation_version"] != proposal["base_activation"]
                or proposal["baseline_sha256"] != digest(canonical(ev.base_policy()))):
            issues.append("learning_base_changed")
        if not preserves_limits(proposal["base_policy"], proposal["policy"]):
            issues.append("learning_policy_relaxes_limits")
        for case_vid in ev.registry(store, "learning_case", scope):
            try:
                data, source = checked_case(store, case_vid)
            except PreservationError:
                with store.connect() as db:
                    active = db.execute("SELECT v.active,s.forgotten_at FROM versions v JOIN sources s ON s.id=v.source_id WHERE v.id=?", (case_vid,)).fetchone()
                if case_vid in proposal["support_cases"] or (active and active[0] and not active[1]):
                    issues.append("learning_support_unavailable")
                continue
            if "owner_review" in data and "benchmark" in data:
                reviewed[case_vid] = (data, source)
        if len(reviewed) > 200:
            raise PreservationError("learning_benchmark_limit")
        support = [reviewed[x][0] for x in proposal["support_cases"] if x in reviewed]
        if len(support) != len(proposal["support_cases"]) or len(support) < 2:
            issues.append("reviewed_support_cases_required")
        else:
            decisions = {ev.Sources(store).get(x["owner_review"])["source_id"] for x in support}
            if len(decisions) < 2:
                issues.append("independent_owner_reviews_required")
            if {x["benchmark"]["expected_state"] for x in support} != {"ready_for_review", "held"}:
                issues.append("positive_and_negative_cases_required")
        improved = regressed = mismatches = 0
        for case_vid, (data, _) in sorted(reviewed.items()):
            bench = data["benchmark"]
            before = ev.check_package(store, bench["package_version"], policy=proposal["base_policy"])
            after = ev.check_package(store, bench["package_version"], policy=proposal["policy"])
            expected = (bench["expected_state"], sorted(set(bench["expected_issue_codes"])))
            def verdict(result):
                return result["state"], sorted({x["code"] for x in result["issues"]})
            before_ok, after_ok = verdict(before) == expected, verdict(after) == expected
            improved += not before_ok and after_ok
            regressed += before_ok and not after_ok
            mismatches += not after_ok
            results.append({"case_version": case_vid, "package_version": bench["package_version"],
                "owner_review": data["owner_review"], "expected": expected,
                "before": verdict(before), "after": verdict(after),
                "before_matches": before_ok, "after_matches": after_ok,
                "before_verification": before["verification_sha256"], "after_verification": after["verification_sha256"]})
        if not improved:
            issues.append("learning_no_improvement")
        if regressed:
            issues.append("learning_regression")
        if mismatches:
            issues.append("learning_expected_results_not_met")
        proof = {"schema": 1, "proposal_version": vid, "scope": scope,
                 "base_policy_sha256": proposal["base_policy_sha256"],
                 "candidate_policy_sha256": digest(canonical(proposal["policy"])),
                 "case_versions": sorted(reviewed), "results": results,
                 "improved": improved, "regressed": regressed, "mismatches": mismatches,
                 "issues": sorted(set(issues)), "state": "blocked" if issues else "ready_for_owner_review"}
        evaluation_sha = digest(canonical(proof))
        return {**proof, "evaluation_sha256": evaluation_sha, "checked_at": timestamp(),
                "approval_binding": approval_binding(scope, ["activate-research-policy", vid, evaluation_sha]),
                "model_quality_improvement_proven": False}


def activate_policy(store: Store, vid: str, *, expected_evaluation: str, approval_version: str) -> dict:
    with store.exclusive():
        proposal, _ = ev.read_record(store, vid, "learning_proposal", require_current=True)
        scope = proposal["scope"]
        ev.assert_ready(store, scope)
        tokens = ["activate-research-policy", vid, expected_evaluation]
        owner_confirmation(store, approval_version, scope, tokens)
        current_vid, head = raw_head(store, scope)
        if head and head.get("proposal_version") == vid and head.get("evaluation_sha256") == expected_evaluation:
            effective_policy(store, scope)
            return {"state": "already_active", "activation_version": current_vid, "policy": head["policy"]}
        evaluation = evaluate_policy(store, vid)
        if evaluation["evaluation_sha256"] != expected_evaluation:
            raise PreservationError("learning_evaluation_changed")
        if evaluation["state"] != "ready_for_owner_review":
            raise PreservationError("learning_evaluation_not_ready")
        record = {"schema": 1, "id": "activate-" + digest(canonical([vid, expected_evaluation, approval_version]))[:40],
                  "scope": scope, "action": "activate", "policy": proposal["policy"],
                  "baseline_sha256": proposal["baseline_sha256"], "proposal_version": vid,
                  "evaluation_sha256": expected_evaluation, "case_versions": evaluation["case_versions"],
                  "approval_version": approval_version, "confirmation_tokens": tokens,
                  "previous_activation": current_vid}
        receipt = ev.save_record(store, "learning_activation", record)
        store.set_setting(policy_key(scope), receipt["version_id"])
        return {"state": "active", "receipt": receipt, "policy": record["policy"]}


def reset_policy(store: Store, scope: str, *, expected_policy: str, approval_version: str) -> dict:
    with store.exclusive():
        ev.assert_ready(store, scope)
        baseline = ev.base_policy()
        old_vid, head = raw_head(store, scope)
        old_policy = head["policy"] if head else baseline
        if head and head["action"] == "reset" and head.get("reset_from_sha256") == expected_policy and head["approval_version"] == approval_version:
            owner_confirmation(store, approval_version, scope, head["confirmation_tokens"])
            effective_policy(store, scope)
            return {"state": "already_reset", "activation_version": old_vid, "policy": baseline}
        tokens = ["reset-research-policy", scope, old_vid or "base", expected_policy]
        owner_confirmation(store, approval_version, scope, tokens)
        if digest(canonical(old_policy)) != expected_policy:
            raise PreservationError("learning_reset_policy_changed")
        record = {"schema": 1, "id": "reset-" + digest(canonical([scope, old_vid, approval_version]))[:40],
                  "scope": scope, "action": "reset", "policy": baseline,
                  "baseline_sha256": digest(canonical(baseline)), "reset_from_sha256": expected_policy,
                  "approval_version": approval_version, "confirmation_tokens": tokens,
                  "previous_activation": old_vid}
        receipt = ev.save_record(store, "learning_activation", record)
        store.set_setting(policy_key(scope), receipt["version_id"])
        return {"state": "reset", "receipt": receipt, "policy": baseline}
