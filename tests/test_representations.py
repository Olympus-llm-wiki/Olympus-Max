"""Public delivery/representation contracts with deterministic native operations."""
from pathlib import Path
import tempfile
import unittest

from olympus.admission import inflight_count, resume_models
from olympus.delivery import run_one, recall_active
from olympus.hindsight import HindsightError
from olympus.part_delivery import run_representation_one
from olympus.preservation import PreservationError, Store, digest
from olympus.representations import Representations, native_targets_for_versions, native_document_map, representation_status


PROFILE = {"schema": 1, "strategy": None, "effective_strategy": None, "mode": "concise", "chunk_size": 3000,
           "config_fingerprint": "a" * 64, "bank_auto_consolidation": True, "bank_observations": True,
           "execution_profile_known": True}


class Native:
    def __init__(self):
        self.ops, self.documents, self.submissions, self.retries = {}, {}, [], []
        self.profile = dict(PROFILE)
        self.lose_response = False
        self.crash = False

    def retain_profile(self, strategy=None):
        return dict(self.profile)

    def operation(self, operation_id):
        return {"operation_id": operation_id, **self.ops.get(operation_id, {"status": "not_found"})}

    def verify_document(self, document_id, expected, *, expected_text=None):
        text = self.documents.get(document_id)
        matches = text is not None and digest(text.encode()) == expected
        return {"exists": text is not None, "text_matches": matches, "searchable": matches,
                "memory_unit_count": 1 if text is not None else 0}

    def submit(self, **kwargs):
        self.submissions.append(kwargs)
        self.ops[kwargs["operation_id"]] = {"status": "pending"}
        if self.crash:
            raise SystemExit("synthetic process loss")
        if self.lose_response:
            raise HindsightError("transport_error")

    def retry_operation(self, operation_id):
        self.retries.append(operation_id)
        self.ops[operation_id] = {"status": "pending"}

    def complete(self, submission=None, errors=0):
        submission = submission or self.submissions[-1]
        self.ops[submission["operation_id"]] = {"status": "completed", "extraction_errors_count": errors, "unit_ids_count": 1}
        self.documents[submission["document_id"]] = submission["text"]


class RepresentationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state")
        self.reps = Representations(self.store)
        self.client = Native()
        resume_models(self.store)

    def capture(self, name, text):
        return self.store.capture(source_key=name, scope="test", title=name, original=text.encode(), text=text)

    def prepare(self, receipt, **kwargs):
        plan = self.reps.prepare(receipt.version_id, profile=self.client.profile, max_part_chars=20, **kwargs)
        return self.reps.enable(plan["id"])

    def due(self):
        with self.store.connect(write=True) as db:
            db.execute("UPDATE delivery SET next_attempt=0,lease_until=0")
            db.execute("UPDATE native_representation_parts SET next_attempt=0,lease_until=0")
        self.store.set_setting("delivery_hold_until", "0")

    def drain(self, receipt, limit=500):
        for _ in range(limit):
            self.due()
            result = run_one(self.store, self.client)
            if result["state"] == "searchable":
                return result
            if result.get("part_state") == "submitted":
                self.client.complete()
        self.fail("representation did not finish")

    def test_large_unicode_manifest_is_deterministic_lossless_and_bounded(self):
        text = "начало🙂\n" + "Середина документа 🙂\n" * 55000 + "конец"
        self.assertGreater(len(text), 1_000_000)
        receipt = self.capture("large", text)
        plan = self.reps.prepare(receipt.version_id, profile=PROFILE, max_part_chars=12000, max_part_bytes=24000)
        second = self.reps.prepare(receipt.version_id, profile=PROFILE, max_part_chars=12000, max_part_bytes=24000)
        self.assertEqual(plan["manifest"], second["manifest"])
        restored = "".join(self.reps.read_part(p) for p in plan["parts"])
        self.assertEqual(restored, text)
        self.assertTrue(all(p["byte_end"] - p["byte_start"] <= 24000 for p in plan["parts"]))
        self.assertTrue(all(p["char_end"] - p["char_start"] <= 12000 for p in plan["parts"]))
        self.assertEqual(self.store.receipt(receipt.version_id).operation_id, receipt.operation_id)
        self.assertEqual(self.store.read_version(receipt.version_id)["original"], text.encode())

    def test_part_completion_yields_to_later_small_source_without_more_slots(self):
        large = self.capture("large", "large part with words " * 4)
        small = self.capture("small", "small")
        plan = self.prepare(large)
        self.assertEqual(run_one(self.store, self.client)["part_state"], "submitted")
        self.assertEqual(inflight_count(self.store), 1)
        self.client.complete(); self.due()
        self.assertEqual(run_one(self.store, self.client)["state"], "pending")
        self.assertEqual(inflight_count(self.store), 0)
        result = run_one(self.store, self.client)
        self.assertEqual(result["version_id"], small.version_id)
        self.assertEqual(self.client.submissions[-1]["document_id"], small.version_id)
        self.client.complete(); self.due(); run_one(self.store, self.client)
        self.drain(large)
        final = self.reps.get(plan["id"])
        self.assertEqual(final["state"], "complete")
        self.assertTrue(final["published"])
        self.assertEqual({p["operation_id"] for p in plan["parts"]},
                         {s["operation_id"] for s in self.client.submissions if s["document_id"] != small.version_id})

    def test_selected_cohort_does_not_admit_unselected_pending_sources(self):
        unrelated = self.capture("unrelated", "first but not selected")
        selected = self.capture("selected", "selected words " * 3)
        self.prepare(selected)
        result = run_one(self.store, self.client, version_id=selected.version_id)
        self.assertEqual(result["version_id"], selected.version_id)
        self.assertEqual(self.store.receipt(unrelated.version_id).memory, "pending")
        self.assertTrue(all(s["metadata"]["olympus_version_id"] == selected.version_id for s in self.client.submissions))

    def test_public_status_distinguishes_legacy_receipt_from_actual_declared_parts(self):
        source = self.capture("source", "source words " * 4)
        old = representation_status(self.store, source.version_id)
        self.assertTrue(old["legacy_binding"]["used_by_current_primary"])
        plan = self.prepare(source)
        status = representation_status(self.store, source.version_id)
        self.assertEqual(status["legacy_binding"]["operation_id"], source.operation_id)
        self.assertFalse(status["legacy_binding"]["used_by_current_primary"])
        current = status["representations"][0]
        self.assertEqual(current["part_counts"], {"pending": len(plan["parts"])})
        self.assertFalse(current["published"])
        self.assertEqual({p["operation_id"] for p in current["parts"]}, {p["operation_id"] for p in plan["parts"]})
        self.assertEqual(self.client.submissions, [])

    def test_snapshot_hold_skips_new_part_profile_lookup_but_allows_completion(self):
        import json,time
        from olympus.jobs import PipelineJobs
        source = self.capture("source", "some words " * 5)
        self.prepare(source)
        jobs = PipelineJobs(self.store); lease = jobs.begin("backup.snapshot")
        self.store.set_setting("backup_submission_hold", json.dumps({"job_id": lease["id"], "token": lease["token"],
            "expires_at": time.time()+60, "reason": "backup_snapshot"}))
        original = self.client.retain_profile
        self.client.retain_profile = lambda *args: self.fail("held source must not query a stopped model service")
        result = run_one(self.store, self.client)
        self.assertEqual(result["reason"], "backup_snapshot")
        self.assertFalse(self.client.submissions)
        jobs.succeed(lease, {})
        self.client.retain_profile = original
        self.due(); run_one(self.store, self.client)
        lease = jobs.begin("backup.snapshot")
        self.store.set_setting("backup_submission_hold", json.dumps({"job_id": lease["id"], "token": lease["token"],
            "expires_at": time.time()+60, "reason": "backup_snapshot"}))
        self.client.complete(); self.due()
        self.assertEqual(run_one(self.store, self.client)["part_state"], "complete")

    def test_lost_response_resumes_same_part_and_occupies_single_slot(self):
        source = self.capture("source", "long text repeated " * 3)
        self.prepare(source)
        self.client.lose_response = True
        run_one(self.store, self.client)
        self.assertEqual(inflight_count(self.store), 1)
        self.due()
        self.assertEqual(run_one(self.store, self.client)["part_state"], "submitted")
        self.assertEqual(len(self.client.submissions), 1)

    def test_invalid_ack_after_acceptance_keeps_part_slot_until_uuid_reconciliation(self):
        source = self.capture("source", "source data " * 4)
        self.prepare(source)
        submit = self.client.submit
        def accepted_but_invalid(**kwargs):
            submit(**kwargs)
            raise HindsightError("invalid_submit_response")
        self.client.submit = accepted_but_invalid
        self.assertEqual(run_one(self.store, self.client)["state"], "submitted")
        self.assertEqual(inflight_count(self.store), 1)
        self.due()
        self.assertEqual(run_one(self.store, self.client)["part_state"], "submitted")
        self.assertEqual(len(self.client.submissions), 1)

    def test_process_loss_after_submission_keeps_part_identity_and_completed_parts(self):
        source = self.capture("source", "original content " * 5)
        plan = self.prepare(source)
        run_one(self.store, self.client); self.client.complete(); self.due(); run_one(self.store, self.client)
        self.client.crash = True
        with self.assertRaises(SystemExit):
            run_one(self.store, self.client)
        second = self.client.submissions[-1]
        self.assertEqual(inflight_count(self.store), 1)
        self.client.crash = False; self.due()
        run_one(self.store, self.client)
        self.assertEqual(len(self.client.submissions), 2)
        self.client.complete(second); self.due(); run_one(self.store, self.client)
        self.drain(source)
        ids = [s["operation_id"] for s in self.client.submissions]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), len(plan["parts"]))

    def test_missing_operation_and_partial_extraction_never_publish_generation(self):
        source = self.capture("source", "source data " * 3)
        plan = self.prepare(source)
        first = plan["parts"][0]
        self.client.documents[first["document_id"]] = self.reps.read_part(first)
        result = run_one(self.store, self.client)
        self.assertEqual(result["state"], "partial")
        self.assertEqual(result["error"], "document_without_completion_proof")
        self.assertFalse(self.reps.get(plan["id"])["published"])
        second = self.capture("second", "another text")
        self.prepare(second)
        run_one(self.store, self.client); self.client.complete(errors=2); self.due()
        result = run_one(self.store, self.client)
        self.assertEqual(result["error"], "native_extraction_partial")
        self.assertEqual(result["state"], "partial")

    def test_explicit_retry_preserves_good_parts_and_requeues_only_failed_native_uuid(self):
        source = self.capture("source", "several words " * 5)
        plan = self.prepare(source)
        run_one(self.store, self.client); self.client.complete(); self.due(); run_one(self.store, self.client)
        run_one(self.store, self.client)
        failed = self.client.submissions[-1]
        self.client.ops[failed["operation_id"]] = {"status": "failed", "error_code": "source_processing_timeout"}
        self.due()
        self.assertEqual(run_one(self.store, self.client)["state"], "partial")
        self.reps.request_retry(plan["id"])
        self.assertEqual(run_one(self.store, self.client)["part_state"], "submitted")
        self.assertEqual(self.client.retries, [failed["operation_id"]])
        self.assertEqual(len(self.client.submissions), 2)
        self.client.complete(failed); self.due(); run_one(self.store, self.client)
        self.drain(source)
        self.assertEqual(len(self.client.submissions), len(plan["parts"]))

    def test_same_content_new_profile_has_distinct_ids_and_does_not_reuse_chunk_dedup(self):
        source = self.capture("source", "text " * 7)
        first = self.prepare(source)
        self.drain(source)
        self.client.profile = {**PROFILE, "mode": "verbose", "config_fingerprint": "b" * 64}
        second = self.reps.prepare(source.version_id, profile=self.client.profile, kind="enrichment", max_part_chars=20)
        self.reps.enable(second["id"])
        self.assertFalse({p["document_id"] for p in first["parts"]} & {p["document_id"] for p in second["parts"]})
        self.assertEqual(self.store.receipt(source.version_id).memory, "searchable")
        result = run_representation_one(self.store, self.client, second["id"])
        self.assertEqual(result["part_state"], "submitted")
        self.assertEqual(self.store.receipt(source.version_id).memory, "searchable")
        mapping = native_document_map(self.store, {source.version_id})
        self.assertEqual(set(mapping), {p["document_id"] for p in first["parts"]})

    def test_duplicate_old_completion_cannot_roll_back_new_published_generation(self):
        source = self.capture("source", "short text")
        old = self.prepare(source)
        self.drain(source)
        new = self.reps.prepare(source.version_id, profile=PROFILE, max_part_chars=20, generation="refresh-1")
        self.reps.enable(new["id"])
        run_representation_one(self.store, self.client, new["id"])
        self.client.complete(); self.due()
        run_representation_one(self.store, self.client, new["id"])
        self.reps.complete(old["id"])
        self.assertFalse(self.reps.get(old["id"])["published"])
        self.assertTrue(self.reps.get(new["id"])["published"])
        mapping = native_document_map(self.store, {source.version_id})
        self.assertEqual(set(mapping), {p["document_id"] for p in new["parts"]})

    def test_native_part_recall_maps_to_canonical_source_and_rechecks_withdrawal(self):
        source = self.capture("source", "some interesting words " * 3)
        plan = self.prepare(source)
        self.drain(source)
        doc = plan["parts"][0]["document_id"]
        self.client.recall = lambda *args: {"results": [
            {"document_id": doc, "chunk_id": "good", "text": "fact"},
            {"document_id": "unknown", "chunk_id": "bad", "text": "unrelated"}],
            "chunks": {"good": {"id": "good", "text": "context"}, "bad": {"id": "bad", "text": "other"}}}
        result = recall_active(self.store, self.client, "words", "test")
        self.assertEqual(result["results"][0]["document_id"], source.version_id)
        self.assertEqual(result["results"][0]["native_document_id"], doc)
        self.assertEqual(result["results"][0]["source"]["native_representation"]["char_start"], 0)
        self.assertEqual(set(result["chunks"]), {"good"})
        original = self.client.recall
        def revoke(*args):
            self.store.forget(source.source_id, "test revocation during query")
            return original(*args)
        self.client.recall = revoke
        result = recall_active(self.store, self.client, "words", "test")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["chunks"], {})

    def test_manifest_tampering_and_part_corruption_are_detected(self):
        source = self.capture("source", "some words " * 4)
        plan = self.prepare(source)
        with self.store.connect(write=True) as db:
            db.execute("UPDATE native_representation_parts SET byte_start=1 WHERE representation_id=? AND part_index=0", (plan["id"],))
        with self.assertRaisesRegex(PreservationError, "representation_manifest_mismatch"):
            self.reps.get(plan["id"])

    def test_forget_stops_all_generations_and_exposes_all_targets_for_reconciliation(self):
        source = self.capture("source", "some words " * 4)
        plan = self.prepare(source)
        self.store.forget(source.source_id, "synthetic withdrawal")
        self.assertIsNone(self.reps.claim(plan["id"]))
        self.assertEqual(native_document_map(self.store, set()), {})
        targets = native_targets_for_versions(self.store, {source.version_id})
        self.assertEqual({x["document_id"] for x in targets}, {source.version_id, *(p["document_id"] for p in plan["parts"])})

    def test_profile_drift_and_chunks_auto_consolidation_require_explicit_resolution(self):
        source = self.capture("source", "some words " * 4)
        plan = self.prepare(source)
        self.client.profile["config_fingerprint"] = "c" * 64
        self.assertEqual(run_one(self.store, self.client)["error"], "representation_profile_changed")
        self.assertFalse(self.client.submissions)
        with self.assertRaisesRegex(PreservationError, "chunks_consolidation_not_disabled"):
            self.reps.prepare(source.version_id, profile={**PROFILE, "mode": "chunks"})

    def test_whitespace_parts_preserve_coverage_without_invalid_native_requests(self):
        source = self.capture("whitespace", "start" + " " * 70 + "end")
        plan = self.prepare(source)
        self.drain(source)
        self.assertEqual("".join(self.reps.read_part(p) for p in plan["parts"]), "start" + " " * 70 + "end")
        self.assertTrue(all(s["text"].strip() for s in self.client.submissions))
        self.assertEqual(len(self.client.submissions), sum(p["requires_native"] for p in plan["parts"]))


if __name__ == "__main__":
    unittest.main()
