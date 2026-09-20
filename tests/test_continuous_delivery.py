from pathlib import Path
import tempfile
import json
import time
import unittest
from unittest.mock import patch

from olympus.admission import resume_models, pause_models, permission, hold, inflight_count, new_submission_hold, MAX_NATIVE_RETRIES
from olympus.delivery import grant_budget, run_one
from olympus.hindsight import HindsightError
from olympus.preservation import Store, digest
from olympus.runtime_control import RuntimeControl


class Native:
    def __init__(self):
        self.operations = {}
        self.documents = {}
        self.submissions = []
        self.retries = []
        self.lose_response = False

    def operation(self, oid):
        result = self.operations.get(oid, {"status": "not_found"})
        if result["status"] == "completed":
            return {"extraction_errors_count": 0, **result}
        return result

    def submit(self, **kwargs):
        self.submissions.append(kwargs)
        self.operations[kwargs["operation_id"]] = {"status": "pending"}
        if self.lose_response:
            raise HindsightError("transport_error")

    def retry_operation(self, oid):
        self.retries.append(oid)
        self.operations[oid] = {"status": "pending"}

    def verify_document(self, vid, sha, *, expected_text=None):
        text = self.documents.get(vid)
        return {"exists": text is not None, "searchable": text is not None and digest(text.encode()) == sha,
                "text_matches": text is not None and digest(text.encode()) == sha, "memory_unit_count": 1}

    def list_operations(self, **kwargs):
        return {"total": 0}


class ContinuousTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state")
        self.client = Native()
        self.first = self.capture("first")
        resume_models(self.store)

    def capture(self, name, text=None):
        return self.store.capture(source_key=name, scope="test", title=name,
                                  original=(text or name).encode(), text=text or name)

    def due(self):
        with self.store.connect(write=True) as db:
            db.execute("UPDATE delivery SET next_attempt=0")
        self.store.set_setting("delivery_hold_until", "0")

    def complete(self, receipt, text):
        self.client.operations[receipt.operation_id] = {"status": "completed"}
        self.client.documents[receipt.version_id] = text
        self.due()

    def test_continuous_survives_expired_zero_pilot_and_processes_more_than_twenty(self):
        self.store.set_setting("budget_expires", "0")
        self.store.set_setting("budget_remaining", "0")
        for i in range(25):
            receipt = self.first if i == 0 else self.capture(str(i))
            text = "first" if i == 0 else str(i)
            self.assertEqual(run_one(self.store, self.client)["state"], "submitted")
            self.complete(receipt, text)
            self.assertEqual(run_one(self.store, self.client)["state"], "searchable")
        self.assertEqual(len(self.client.submissions), 25)
        self.assertEqual(self.store.setting("budget_remaining"), "0")

    def test_pause_blocks_new_submission_and_resume_preserves_source(self):
        pause_models(self.store)
        self.assertEqual(run_one(self.store, self.client)["error"], "models_paused")
        self.assertFalse(self.client.submissions)
        resume_models(self.store)
        self.due()
        run_one(self.store, self.client)
        self.assertEqual(self.client.submissions[0]["operation_id"], self.first.operation_id)

    def test_one_inflight_and_polling_before_new_submissions(self):
        second = self.capture("second")
        run_one(self.store, self.client)
        result = run_one(self.store, self.client)
        self.assertEqual(result["reason"], "waiting_for_inflight")
        self.assertNotIn("error", result)
        self.assertEqual(inflight_count(self.store), 1)
        self.due()
        self.assertEqual(run_one(self.store, self.client)["version_id"], self.first.version_id)
        self.assertEqual(len(self.client.submissions), 1)
        self.complete(self.first, "first")
        self.assertEqual(run_one(self.store, self.client)["state"], "searchable")
        self.assertEqual(run_one(self.store, self.client)["version_id"], second.version_id)

    def test_lost_response_occupies_slot_and_reuses_original_operation(self):
        self.capture("second")
        self.client.lose_response = True
        run_one(self.store, self.client)
        self.assertEqual(inflight_count(self.store), 1)
        self.due()
        self.assertEqual(run_one(self.store, self.client)["state"], "submitted")
        self.assertEqual(len(self.client.submissions), 1)

    def test_accepted_write_with_unusable_response_keeps_unknown_operation_inflight(self):
        submit = self.client.submit
        def invalid_ack(**kwargs):
            submit(**kwargs)
            raise HindsightError("unexpected_content_type", 200)
        self.client.submit = invalid_ack
        self.assertEqual(run_one(self.store, self.client)["state"], "pending")
        self.assertEqual(inflight_count(self.store), 1)
        self.due()
        self.assertEqual(run_one(self.store, self.client)["state"], "submitted")
        self.assertEqual(len(self.client.submissions), 1)

    def test_process_death_after_submit_keeps_inflight_slot_until_reconciliation(self):
        self.capture("second")
        submit = self.client.submit
        def crash(**kwargs):
            submit(**kwargs)
            raise SystemExit("simulated process loss")
        self.client.submit = crash
        with self.assertRaises(SystemExit):
            run_one(self.store, self.client)
        self.assertEqual(inflight_count(self.store), 1)
        self.assertEqual(run_one(self.store, self.client)["reason"], "waiting_for_inflight")
        with self.store.connect(write=True) as db:
            db.execute("UPDATE delivery SET lease_until=0,next_attempt=0")
        self.assertEqual(run_one(self.store, self.client)["state"], "submitted")
        self.assertEqual(len(self.client.submissions), 1)

    def test_large_text_complete_until_terminal_readback(self):
        text = "Full 🙂\\\n" * 150000
        self.assertGreater(len(text), 1_000_000)
        large = self.capture("large", text)
        self.complete(self.first, "first")
        run_one(self.store, self.client)
        run_one(self.store, self.client)
        sent = self.client.submissions[-1]
        self.assertEqual(sent["text"], text)
        self.assertEqual(sent["operation_id"], large.operation_id)
        # Partial native document never outruns an unfinished operation.
        self.client.documents[large.version_id] = text
        self.due()
        self.assertEqual(run_one(self.store, self.client)["state"], "submitted")
        self.complete(large, text)
        self.assertEqual(run_one(self.store, self.client)["state"], "searchable")

    def test_rate_limit_waits_then_retries_same_native_operation(self):
        run_one(self.store, self.client)
        self.client.operations[self.first.operation_id] = {"status": "failed", "error_code": "provider_rate_limited"}
        self.due()
        result = run_one(self.store, self.client)
        self.assertEqual(result["error"], "provider_rate_limited")
        self.assertGreater(permission(self.store)["retry_at"], time.time())
        self.assertFalse(self.client.retries)
        # A different eligible source cannot bypass the shared wait.
        self.capture("second")
        self.assertEqual(run_one(self.store, self.client)["error"], "provider_rate_limited")
        self.due()
        self.assertEqual(run_one(self.store, self.client)["state"], "submitted")
        self.assertEqual(self.client.retries, [self.first.operation_id])
        self.assertEqual(len(self.client.submissions), 1)

    def test_unknown_terminal_failure_is_not_retried(self):
        second = self.capture("second")
        self.client.operations[self.first.operation_id] = {"status": "failed", "error_code": "operation_failed"}
        self.assertEqual(run_one(self.store, self.client)["state"], "failed")
        self.assertFalse(self.client.retries)
        self.assertTrue(permission(self.store)["allowed"])
        self.assertEqual(run_one(self.store, self.client)["version_id"], second.version_id)

    def test_completed_with_extraction_errors_is_partial_and_does_not_hold_others(self):
        second = self.capture("second")
        self.client.operations[self.first.operation_id] = {"status": "completed", "extraction_errors_count": 2}
        self.client.documents[self.first.version_id] = "first"
        result = run_one(self.store, self.client)
        self.assertEqual(result["state"], "partial")
        self.assertEqual(result["error"], "native_extraction_partial")
        self.assertTrue(permission(self.store)["allowed"])
        self.assertEqual(run_one(self.store, self.client)["version_id"], second.version_id)

    def test_missing_completion_counters_do_not_certify_complete(self):
        self.client.operation = lambda oid: {"status": "completed"}
        self.client.documents[self.first.version_id] = "first"
        self.assertEqual(run_one(self.store, self.client)["error"], "native_completion_counters_missing")

    def test_native_child_defer_is_observed_without_inventing_quota_or_full_coverage(self):
        import json
        child = "fcf717c0-ea93-454e-a0f7-ef523159a8a7"
        self.client.operations[self.first.operation_id] = {"status": "pending",
            "child_operations": [{"operation_id": child, "status": "pending"}]}
        self.client.operations[child] = {"status": "pending", "next_retry_at": "2026-09-10T00:00:00+00:00",
            "progress": {"stage": "storing", "processed": 10, "total": 10}}
        result = run_one(self.store, self.client)
        observed = json.loads(self.store.setting("native_progress:" + self.first.version_id))
        self.assertEqual(observed["current"]["next_retry_at"], "2026-09-10T00:00:00+00:00")
        self.assertFalse(observed["full_source_coverage"])
        self.assertEqual(result["native_progress"], observed)
        self.assertTrue(permission(self.store)["allowed"])

    def test_authentication_failure_holds_provider_scope(self):
        self.client.operations[self.first.operation_id] = {"status": "failed", "error_code": "provider_authentication_required"}
        self.assertEqual(run_one(self.store, self.client)["state"], "failed")
        self.assertEqual(permission(self.store)["reason"], "provider_authentication_required")

    def test_timed_out_source_remains_failed_while_next_source_progresses(self):
        second = self.capture("second")
        self.client.operations[self.first.operation_id] = {"status": "failed", "error_code": "source_processing_timeout"}
        result = run_one(self.store, self.client)
        self.assertEqual(result["error"], "source_processing_timeout")
        self.assertEqual(self.store.receipt(self.first.version_id).memory, "failed")
        self.assertTrue(permission(self.store)["allowed"])
        self.assertEqual(run_one(self.store, self.client)["version_id"], second.version_id)
        self.assertEqual(self.client.retries, [])

    def test_backup_cannot_mask_a_delivery_hold(self):
        from olympus.backup_job import continuous_backup_window
        (self.store.root / "recovery").mkdir()
        (self.store.root / "recovery/readiness.json").write_text("{}")
        self.store.set_setting("delivery_attention", "operation_failed")
        self.client.list_operations = lambda **kwargs: self.fail("must report hold before waiting on native work")
        self.assertIsNone(continuous_backup_window(self.store, RuntimeControl(Path(self.temp.name)), self.client))

    def test_insufficient_backup_space_is_visible_without_stopping_delivery(self):
        from olympus.cli import _deliver
        from types import SimpleNamespace
        (self.store.root / "recovery").mkdir()
        (self.store.root / "recovery/readiness.json").write_text("{}")
        self.store.set_setting('backup_status', json.dumps({'state': 'waiting_for_disk_space',
            'error': 'backup_disk_space_low', 'minimum_required_bytes': 1024}))
        self.client.list_operations = lambda **kwargs: self.fail("must not wait on native backup when disk cannot fit it")
        with patch("olympus.runtime_control.RuntimeControl.supervise", return_value={}), \
                patch("olympus.backup_job.maybe_backup", side_effect=AssertionError('backup must run independently')):
            result = _deliver(self.store, self.client, limit=1)
        self.assertEqual(result["backup"]["state"], "waiting_for_disk_space")
        self.assertGreater(result["backup"]["minimum_required_bytes"], 0)
        self.assertEqual(result["delivery"][0]["state"], "submitted")
        self.assertEqual(len(self.client.submissions), 1)

    def test_pause_does_not_finalize_a_recoverable_native_failure(self):
        run_one(self.store, self.client)
        pause_models(self.store)
        self.client.operations[self.first.operation_id] = {"status": "failed", "error_code": "provider_unavailable"}
        self.due()
        self.assertEqual(run_one(self.store, self.client)["error"], "models_paused")
        self.assertEqual(self.store.receipt(self.first.version_id).memory, "submitted")
        resume_models(self.store)
        self.assertEqual(run_one(self.store, self.client)["error"], "provider_unavailable")
        self.due()
        self.assertEqual(run_one(self.store, self.client)["state"], "submitted")
        self.assertEqual(self.client.retries, [self.first.operation_id])

    def test_native_retry_limit_is_visible_without_permanent_global_stop(self):
        self.client.operations[self.first.operation_id] = {"status": "failed", "error_code": "provider_rate_limited"}
        self.store.set_setting("native_retry:" + self.first.operation_id, str(MAX_NATIVE_RETRIES))
        self.assertEqual(run_one(self.store, self.client)["error"], "provider_retry_exhausted")
        self.assertTrue(permission(self.store)["allowed"])

    def test_http_retry_after_is_shared_across_the_queue(self):
        self.capture("second")
        def limited(**kwargs):
            raise HindsightError("http_error", 429, retry_after=7200)
        self.client.submit = limited
        started = time.time()
        self.assertEqual(run_one(self.store, self.client)["state"], "pending")
        self.assertGreaterEqual(permission(self.store)["retry_at"], started + 7200)
        self.assertEqual(run_one(self.store, self.client)["error"], "provider_rate_limited")
        self.assertEqual(inflight_count(self.store), 1)

    def snapshot_hold(self):
        import json
        from olympus.jobs import PipelineJobs
        jobs = PipelineJobs(self.store)
        lease = jobs.begin("backup.snapshot")
        self.store.set_setting("backup_submission_hold", json.dumps({"job_id": lease["id"], "token": lease["token"],
            "expires_at": time.time() + 60, "reason": "backup_snapshot"}))
        return jobs, lease

    def test_snapshot_hold_blocks_only_new_submission_and_ends_with_owner_lease(self):
        jobs, lease = self.snapshot_hold()
        self.assertTrue(permission(self.store)["allowed"])
        result = run_one(self.store, self.client)
        self.assertEqual(result["reason"], "backup_snapshot")
        self.assertNotIn("error", result)
        self.assertFalse(self.client.submissions)
        self.assertNotIn("token", new_submission_hold(self.store))
        jobs.succeed(lease, {"snapshot": "synthetic"})
        self.assertIsNone(new_submission_hold(self.store))
        self.due()
        self.assertEqual(run_one(self.store, self.client)["state"], "submitted")

    def test_snapshot_hold_keeps_polling_and_full_readback_running(self):
        run_one(self.store, self.client)
        self.snapshot_hold(); self.due()
        self.assertEqual(run_one(self.store, self.client)["state"], "submitted")
        self.complete(self.first, "first")
        self.assertEqual(run_one(self.store, self.client)["state"], "searchable")
        self.assertEqual(len(self.client.submissions), 1)

    def test_snapshot_hold_blocks_native_retry_without_spending_retry_budget(self):
        run_one(self.store, self.client)
        self.client.operations[self.first.operation_id] = {"status": "failed", "error_code": "provider_unavailable"}
        self.snapshot_hold(); self.due()
        self.assertEqual(run_one(self.store, self.client)["reason"], "backup_snapshot")
        self.assertFalse(self.client.retries)
        self.assertIsNone(self.store.setting("native_retry:" + self.first.operation_id))

    def test_expired_or_wrong_snapshot_token_cannot_strand_delivery(self):
        import json
        self.snapshot_hold()
        data = json.loads(self.store.setting("backup_submission_hold"))
        for updates in ({"token": "not-the-current-lease"}, {"expires_at": 0}, {"expires_at": True}):
            self.store.set_setting("backup_submission_hold", json.dumps({**data, **updates}))
            self.assertIsNone(new_submission_hold(self.store))

    def test_local_input_error_does_not_hold_other_sources(self):
        second = self.capture("second")
        submit = self.client.submit
        def invalid_first(**kwargs):
            if kwargs["document_id"] == self.first.version_id:
                raise HindsightError("invalid_metadata")
            return submit(**kwargs)
        self.client.submit = invalid_first
        self.assertEqual(run_one(self.store, self.client)["state"], "blocked")
        self.assertTrue(permission(self.store)["allowed"])
        self.assertEqual(run_one(self.store, self.client)["version_id"], second.version_id)

    def test_supervisor_obeys_permission_pause_cooldown_and_recovery(self):
        self.store.set_setting("runtime_supervision", "on")
        runtime = RuntimeControl(Path(self.temp.name))
        modes = []
        runtime.set_mode = lambda mode: modes.append(mode) or {"running": True}
        runtime.supervise(self.store)
        pause_models(self.store)
        runtime.supervise(self.store)
        resume_models(self.store)
        hold(self.store, "provider_rate_limited", 60)
        runtime.supervise(self.store)
        self.due()
        self.store.set_setting("recovery_state", "blocked")
        runtime.supervise(self.store)
        self.assertEqual(modes, ["continuous", "safe", "safe", "safe"])

    def test_daily_backup_gets_quiet_window_and_keeps_permission(self):
        from olympus.backup_job import continuous_backup_window
        recovery = self.store.root / "recovery"
        recovery.mkdir()
        (recovery / "readiness.json").write_text("{}")
        runtime = RuntimeControl(Path(self.temp.name))
        modes = []
        runtime.set_mode = lambda mode: modes.append(mode)
        def backup(store, rt):
            self.assertEqual(store.setting("maintenance"), "off")
            self.assertEqual(modes, ["safe"])
            return {"state": "created"}
        with patch("olympus.backup_job.maybe_backup", side_effect=backup), \
                patch("olympus.backup_job.backup_space_status", return_value=None):
            self.assertEqual(continuous_backup_window(self.store, runtime, self.client)["state"], "created")
        self.assertEqual(permission(self.store)["mode"], "continuous")
        self.assertTrue(permission(self.store)["allowed"])

    def test_backup_waits_for_active_source(self):
        from olympus.backup_job import continuous_backup_window
        (self.store.root / "recovery").mkdir()
        (self.store.root / "recovery/readiness.json").write_text("{}")
        run_one(self.store, self.client)
        runtime = RuntimeControl(Path(self.temp.name))
        runtime.set_mode = lambda _: self.fail("must not interrupt active source")
        self.assertIsNone(continuous_backup_window(self.store, runtime, self.client))


if __name__ == "__main__":
    unittest.main()
