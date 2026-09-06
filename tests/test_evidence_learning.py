import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from olympus import evidence as ev
from olympus import learning as learn
from olympus.delivery import recall_active
from olympus.preservation import Store, PreservationError, canonical, digest


class EvidenceLearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.n = 0
        self.scope = "evidence-learning-test"

    def tearDown(self):
        self.tmp.cleanup()

    def source(self, text, role="primary", *, date=None, key=None, scope=None, metadata=None, archive=True):
        self.n += 1
        meta = {"material_role": role, **(metadata or {})}
        if date:
            meta["source_date"] = date
        return self.store.capture(source_key=key or "test-source-" + str(self.n),
            scope=scope or self.scope, title="Synthetic test source " + str(self.n),
            original=text.encode(), text=text, metadata=meta, archive_only=archive)

    def package(self, name, *, date=None, role="primary", text="Original assertion.",
                quote=None, claim_type="factual", start=None):
        original = self.source(text, role, date=date)
        statement = "Attributed test statement " + name + "."
        report = self.source(statement, "synthesis", archive=False)
        entry = {"source_version": original.version_id, "quote": quote or text}
        if start is not None:
            entry["start"] = start
        data = {"schema": 1, "id": name, "scope": self.scope, "report_version": report.version_id,
                "claims": [{"id": "c1", "type": claim_type, "statement": statement, "evidence": [entry]}]}
        registered = ev.register_package(self.store, data)
        return {"original": original, "report": report, "data": data,
                "version": registered["receipt"]["version_id"], "check": registered["verification"]}

    def owner(self, tokens, *, scope=None, action="approve", confirmed=True, binding=None):
        scope = scope or self.scope
        meta = {"decision_status": "confirmed" if confirmed else "historical",
                "owner_confirmed_at": "2026-09-06T00:00:00Z", "decision_scope": scope,
                "owner_confirmation_evidence": "synthetic-test-only; not a real user approval",
                "decision_action": action,
                "approval_binding": binding or learn.approval_binding(scope, tokens)}
        return self.source("Synthetic owner decision:\n" + "\n".join(tokens), "decision", scope=scope, metadata=meta)

    def learning_case(self, name, package, expected, codes, *, reviewed=True):
        data = {"schema": 1, "id": name, "scope": self.scope,
                "summary": "Synthetic observed behavior " + name,
                "observations": [{"outcome": "observation", "statement": "A recorded test case.", "evidence": [0]}],
                "evidence": [package["data"]["claims"][0]["evidence"][0]],
                "benchmark": {"package_version": package["version"], "expected_state": expected,
                              "expected_issue_codes": codes}}
        if reviewed:
            decision = self.owner([learn.case_token(data)])
            data["owner_review"] = decision.version_id
        saved = learn.record_case(self.store, data)
        return saved["receipt"]["version_id"], data

    def candidate(self):
        positive = self.package("dated", date="2026-09-01")
        negative = self.package("undated")
        a, _ = self.learning_case("dated-case", positive, "ready_for_review", [])
        b, _ = self.learning_case("undated-case", negative, "held", ["source_date_missing"])
        policy = {**ev.base_policy(), "require_source_dates": True}
        proposed = learn.propose_policy(self.store, {"id": "require-dates", "scope": self.scope,
            "policy": policy, "support_cases": [a, b]})
        return proposed, a, b

    def activate(self, candidate):
        vid = candidate["receipt"]["version_id"]
        evaluated = candidate["evaluation"]["evaluation_sha256"]
        owner = self.owner(["activate-research-policy", vid, evaluated])
        result = learn.activate_policy(self.store, vid, expected_evaluation=evaluated, approval_version=owner.version_id)
        return result, owner

    def make_searchable(self, vid):
        with self.store.connect(write=True) as db:
            db.execute("UPDATE delivery SET state='searchable' WHERE version_id=?", (vid,))

    def settle_retraction_for_local_test(self):
        # Simulates the existing reconciler's boundary in an isolated Store.
        with self.store.connect(write=True) as db:
            db.execute("UPDATE changes SET applied=1")

    def test_package_registration_archives_and_repeats(self):
        package = self.package("one")
        again = ev.register_package(self.store, package["data"])
        self.assertEqual(again["receipt"]["version_id"], package["version"])
        self.assertEqual(again["receipt"]["memory"], "archived")
        self.assertEqual(package["check"]["state"], "ready_for_review")
        self.assertFalse(package["check"]["verified_fact"])
        self.assertIsNone(self.store.setting("budget_remaining"))

    def test_matching_quote_has_real_positions(self):
        package = self.package("lines", text="Heading\nExact fragment.\nTail", quote="Exact fragment.")
        quote = package["check"]["claims"][0]["evidence"][0]
        self.assertEqual((quote["start"], quote["line_start"], quote["line_end"]), (8, 2, 2))

    def test_ambiguous_quote_requires_position(self):
        package = self.package("ambiguous", text="same\nsame", quote="same")
        self.assertIn("quote_ambiguous", {x["code"] for x in package["check"]["issues"]})
        package["data"]["claims"][0]["evidence"][0]["start"] = 5
        updated = ev.register_package(self.store, package["data"])
        self.assertEqual(updated["verification"]["state"], "ready_for_review")

    def test_whitespace_match_is_labelled_without_fake_positions(self):
        package = self.package("spaces", text="two\n  words", quote="two words")
        result = package["check"]["claims"][0]["evidence"][0]
        self.assertEqual(result["status"], "matched_whitespace")
        self.assertIsNone(result["start"])
        checked = ev.check_package(self.store, package["version"], policy={**ev.base_policy(), "allow_whitespace_normalization": False})
        self.assertEqual(checked["state"], "held")

    def test_discussion_and_synthesis_do_not_ground_factual_claims(self):
        for role in ("discussion", "synthesis", "decision"):
            with self.subTest(role=role):
                result = self.package(role, role=role)["check"]
                self.assertIn("factual_source_role_ineligible", {x["code"] for x in result["issues"]})

    def test_extracted_source_is_eligible(self):
        self.assertEqual(self.package("extracted", role="extracted")["check"]["state"], "ready_for_review")

    def test_report_cannot_cite_itself_even_as_inference(self):
        package = self.package("self")
        claim = package["data"]["claims"][0]
        claim["type"] = "inference"
        claim["evidence"] = [{"source_version": package["report"].version_id, "quote": claim["statement"]}]
        result = ev.register_package(self.store, package["data"])["verification"]
        self.assertIn("report_is_context_only", {x["code"] for x in result["issues"]})

    def test_declared_claim_must_appear_in_report(self):
        package = self.package("statement")
        package["data"]["claims"][0]["statement"] = "Not in the report."
        result = ev.register_package(self.store, package["data"])["verification"]
        self.assertIn("claim_not_in_report", {x["code"] for x in result["issues"]})

    def test_identical_copies_are_not_distinct_originals(self):
        package = self.package("copies")
        duplicate = self.source("Original assertion.")
        package["data"]["claims"][0]["evidence"].append({"source_version": duplicate.version_id, "quote": "Original assertion."})
        result = ev.check_manifest(self.store, package["data"], {**ev.base_policy(), "min_distinct_originals": 2})
        self.assertEqual(result["claims"][0]["distinct_originals"], 1)
        self.assertIn("insufficient_distinct_originals", {x["code"] for x in result["issues"]})

    def test_source_corruption_holds_package(self):
        package = self.package("corrupt")
        (self.store.versions / package["original"].version_id / "original").write_bytes(b"wrong bytes")
        result = ev.check_package(self.store, package["version"])
        self.assertIn("source_unavailable", {x["code"] for x in result["issues"]})

    def test_retraction_and_recovery_barriers_apply(self):
        package = self.package("withdraw")
        self.store.forget(package["original"].source_id, "Synthetic withdrawal")
        with self.assertRaisesRegex(PreservationError, "correction_reconciliation_pending"):
            ev.check_package(self.store, package["version"])
        self.settle_retraction_for_local_test()
        self.assertEqual(ev.check_package(self.store, package["version"])["state"], "held")
        self.store.set_setting("recovery_state", "blocked")
        with self.assertRaisesRegex(PreservationError, "recovery_verification_required"):
            ev.check_package(self.store, package["version"])

    def test_recall_includes_lineage_then_withholds_report_and_chunks(self):
        package = self.package("recall")
        vid = package["report"].version_id
        self.make_searchable(vid)

        class Client:
            def recall(_, query, tags):
                return {"results": [{"id": "m1", "document_id": vid, "chunk_id": "chunk1", "text": "Attribution"}],
                        "chunks": {"chunk1": {"text": "Report text"}}}

        first = recall_active(self.store, Client(), "query", self.scope)
        self.assertIn("research_packages", first["results"][0]["source"])
        self.store.forget(package["original"].source_id, "Synthetic withdrawal")
        self.settle_retraction_for_local_test()
        second = recall_active(self.store, Client(), "query", self.scope)
        self.assertEqual(second["results"], [])
        self.assertEqual(second["chunks"], {})
        self.assertEqual(second["withheld_reports"][0]["version_id"], vid)

    def test_withdrawal_during_recall_does_not_escape_barrier(self):
        package = self.package("race")
        self.make_searchable(package["report"].version_id)

        class Client:
            def recall(_, query, tags):
                self.store.forget(package["original"].source_id, "Synthetic withdrawal during recall")
                return {"results": [{"document_id": package["report"].version_id}]}

        with self.assertRaisesRegex(PreservationError, "correction_reconciliation_pending"):
            recall_active(self.store, Client(), "query", self.scope)

    def test_unregistered_report_keeps_existing_attribution(self):
        source = self.source("Historical report", "synthesis", archive=False)
        self.make_searchable(source.version_id)

        class Client:
            def recall(_, query, tags):
                return {"results": [{"document_id": source.version_id}], "chunks": {}}

        result = recall_active(self.store, Client(), "query", self.scope)
        self.assertEqual(len(result["results"]), 1)
        self.assertNotIn("research_packages", result["results"][0]["source"])
        self.assertFalse(result["results"][0]["source"]["verified_fact"])

    def test_assessment_does_not_become_reviewed_case(self):
        package = self.package("assessment")
        vid, data = self.learning_case("unreviewed", package, "ready_for_review", [], reviewed=False)
        self.assertEqual(learn.list_cases(self.store, self.scope)[0]["status"], "assistant_assessment")
        with self.assertRaisesRegex(PreservationError, "reviewed_benchmark_required"):
            learn.checked_case(self.store, vid, reviewed=True)

    def test_negative_decision_cannot_approve_even_when_tokens_match(self):
        package = self.package("reject")
        _, data = self.learning_case("reject-case", package, "ready_for_review", [], reviewed=False)
        decision = self.owner([learn.case_token(data)], action="reject")
        data["owner_review"] = decision.version_id
        with self.assertRaisesRegex(PreservationError, "exact_owner_confirmation_required"):
            learn.record_case(self.store, data)

    def test_general_or_historical_confirmation_is_insufficient(self):
        package = self.package("old-decision")
        _, data = self.learning_case("case", package, "ready_for_review", [], reviewed=False)
        for tokens, confirmed in [(["Sounds good"], True), ([learn.case_token(data)], False)]:
            decision = self.owner(tokens, confirmed=confirmed)
            data["owner_review"] = decision.version_id
            with self.assertRaisesRegex(PreservationError, "exact_owner_confirmation_required"):
                learn.record_case(self.store, data)

    def test_candidate_executes_behavior_and_improves_without_regression(self):
        candidate, _, _ = self.candidate()
        evaluation = candidate["evaluation"]
        self.assertEqual((evaluation["state"], evaluation["improved"], evaluation["regressed"]),
                         ("ready_for_owner_review", 1, 0))
        self.assertEqual(len(evaluation["results"]), 2)
        self.assertFalse(evaluation["model_quality_improvement_proven"])
        again = learn.evaluate_policy(self.store, candidate["receipt"]["version_id"])
        self.assertEqual(again["evaluation_sha256"], evaluation["evaluation_sha256"])

    def test_candidate_cannot_change_authority_fields(self):
        candidate, a, b = self.candidate()
        for key in ("bank", "autoReflect", "instructions", "command"):
            with self.subTest(key=key), self.assertRaises(PreservationError):
                learn.propose_policy(self.store, {"id": "bad", "scope": self.scope,
                    "policy": {**ev.base_policy(), key: "anything"}, "support_cases": [a, b]})

    def test_all_current_cases_are_evaluated_and_regression_blocks(self):
        candidate, a, b = self.candidate()
        extra = self.package("accepted-undated")
        self.learning_case("additional-positive", extra, "ready_for_review", [])
        result = learn.evaluate_policy(self.store, candidate["receipt"]["version_id"])
        self.assertEqual(len(result["results"]), 3)
        self.assertIn("learning_regression", result["issues"])
        self.assertNotEqual(result["evaluation_sha256"], candidate["evaluation"]["evaluation_sha256"])

    def test_activation_is_exact_and_idempotent(self):
        candidate, _, _ = self.candidate()
        result, owner = self.activate(candidate)
        self.assertEqual(result["state"], "active")
        self.assertTrue(learn.effective_policy(self.store, self.scope)["policy"]["require_source_dates"])
        repeated = learn.activate_policy(self.store, candidate["receipt"]["version_id"],
            expected_evaluation=candidate["evaluation"]["evaluation_sha256"], approval_version=owner.version_id)
        self.assertEqual(repeated["state"], "already_active")

    def test_changed_evaluation_rejects_stale_owner_approval(self):
        candidate, _, _ = self.candidate()
        vid = candidate["receipt"]["version_id"]
        sha = candidate["evaluation"]["evaluation_sha256"]
        decision = self.owner(["activate-research-policy", vid, sha])
        extra = self.package("third", date="2026-09-02")
        self.learning_case("third-case", extra, "ready_for_review", [])
        with self.assertRaisesRegex(PreservationError, "learning_evaluation_changed"):
            learn.activate_policy(self.store, vid, expected_evaluation=sha, approval_version=decision.version_id)

    def test_wrong_scope_owner_approval_rejected(self):
        candidate, _, _ = self.candidate()
        vid, sha = candidate["receipt"]["version_id"], candidate["evaluation"]["evaluation_sha256"]
        owner = self.owner(["activate-research-policy", vid, sha], scope="other-scope")
        with self.assertRaisesRegex(PreservationError, "exact_owner_confirmation_required"):
            learn.activate_policy(self.store, vid, expected_evaluation=sha, approval_version=owner.version_id)

    def test_withdrawn_learning_basis_requires_explicit_reset(self):
        candidate, a, _ = self.candidate()
        activated, _ = self.activate(candidate)
        current_sha = digest(canonical(activated["policy"]))
        self.store.forget(self.store.read_version(a)["source_id"], "Synthetic case retirement")
        self.settle_retraction_for_local_test()
        with self.assertRaises(PreservationError):
            learn.effective_policy(self.store, self.scope)
        owner = self.owner(["reset-research-policy", self.scope, activated["receipt"]["version_id"], current_sha])
        reset = learn.reset_policy(self.store, self.scope, expected_policy=current_sha, approval_version=owner.version_id)
        self.assertEqual(reset["state"], "reset")
        self.assertEqual(learn.effective_policy(self.store, self.scope)["policy"], ev.base_policy())
        self.assertEqual(learn.reset_policy(self.store, self.scope, expected_policy=current_sha,
                                          approval_version=owner.version_id)["state"], "already_reset")

    def test_old_reset_approval_cannot_override_later_activation(self):
        candidate, a, b = self.candidate()
        activated, _ = self.activate(candidate)
        sha = digest(canonical(activated["policy"]))
        old_owner = self.owner(["reset-research-policy", self.scope, activated["receipt"]["version_id"], sha])
        learn.reset_policy(self.store, self.scope, expected_policy=sha, approval_version=old_owner.version_id)
        next_candidate = learn.propose_policy(self.store, {"id": "later-require-dates", "scope": self.scope,
            "policy": activated["policy"], "support_cases": [a, b]})
        self.activate(next_candidate)
        with self.assertRaisesRegex(PreservationError, "exact_owner_confirmation_required"):
            learn.reset_policy(self.store, self.scope, expected_policy=sha, approval_version=old_owner.version_id)

    def test_unreviewed_support_does_not_activate(self):
        package = self.package("unreviewed-input", date="2026-09-01")
        a, _ = self.learning_case("unreviewed-a", package, "ready_for_review", [], reviewed=False)
        b, _ = self.learning_case("unreviewed-b", package, "ready_for_review", [], reviewed=False)
        candidate = learn.propose_policy(self.store, {"id": "unreviewed", "scope": self.scope,
            "policy": {**ev.base_policy(), "require_source_dates": True}, "support_cases": [a, b]})
        self.assertIn("reviewed_support_cases_required", candidate["evaluation"]["issues"])

    def test_registry_survives_reopening_store(self):
        candidate, _, _ = self.candidate()
        self.activate(candidate)
        reopened = Store(self.tmp.name)
        self.assertTrue(learn.effective_policy(reopened, self.scope)["policy"]["require_source_dates"])


if __name__ == "__main__":
    unittest.main()
