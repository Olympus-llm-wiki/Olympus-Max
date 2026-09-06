"""Native HTTP contract checks. These never contact Hindsight or call a model."""

import hashlib
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from olympus.hindsight import HindsightClient, HindsightError


OP = "2d32c79f-e108-42c4-8f08-9bd0b743d605"
DOC = "source:example-v1"
BANK = "olympus-test"
TEXT = "Источнику принадлежит точная деталь: 391.\nOriginal text."
DIGEST = hashlib.sha256(TEXT.encode()).hexdigest()
PREFIX = "/v1/default/banks/olympus-test"


class LocalServer:
    def __enter__(self):
        self.responses = []
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_request(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                owner.requests.append({"method": self.command, "path": self.path,
                                       "headers": dict(self.headers), "body": json.loads(raw) if raw else None})
                spec = owner.responses.pop(0) if owner.responses else {"status": 500, "body": {}}
                if spec.get("delay"):
                    time.sleep(spec["delay"])
                body = spec.get("body", {})
                raw_response = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
                self.send_response(spec.get("status", 200))
                headers = spec.get("headers", {})
                if "Content-Type" not in headers:
                    self.send_header("Content-Type", "application/json")
                if "Content-Length" not in headers and not spec.get("no_length"):
                    self.send_header("Content-Length", str(len(raw_response)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    self.wfile.write(raw_response)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_GET = handle_request
            do_POST = handle_request
            do_DELETE = handle_request

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)


class HindsightTests(unittest.TestCase):
    def setUp(self):
        self.server = LocalServer().__enter__()
        self.addCleanup(self.server.__exit__)
        self.client = HindsightClient(self.server.url, BANK)

    def reply(self, body, **kwargs):
        self.server.responses.append({"body": body, **kwargs})

    def error(self, code, call, status=None):
        with self.assertRaises(HindsightError) as raised:
            call()
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(raised.exception.status, status)
        return raised.exception

    def ack(self, **overrides):
        return {"success": True, "async": True, "bank_id": BANK, "items_count": 1,
                "operation_id": OP, **overrides}

    def document(self, **overrides):
        return {"id": DOC, "bank_id": BANK, "original_text": TEXT,
                "memory_unit_count": 2, **overrides}

    def memories(self, **overrides):
        return {"items": [{"id": "memory-1", "document_id": DOC}], "total": 2,
                "limit": 1, "offset": 0, **overrides}

    def test_submit_preserves_source_and_stable_operation_with_native_body(self):
        self.reply(self.ack())
        result = self.client.submit(DOC, OP, TEXT, "2026-09-05T11:00:00Z",
                                    {"source": "local:test", "text_sha256": DIGEST}, ["project:one"], "verbatim")
        request = self.server.requests[0]
        self.assertEqual((request["method"], request["path"]), ("POST", PREFIX + "/memories"))
        self.assertEqual(request["body"], {
            "items": [{"content": TEXT, "document_id": DOC, "timestamp": "2026-09-05T11:00:00Z",
                       "metadata": {"source": "local:test", "text_sha256": DIGEST},
                       "tags": ["project:one"], "strategy": "verbatim", "update_mode": "replace"}],
            "async": True, "operation_id": OP})
        self.assertEqual(result["operation_id"], OP)

    def test_submit_does_not_invent_strategy_or_accept_different_operation(self):
        self.reply(self.ack())
        self.client.submit(DOC, OP, TEXT, "unset", {}, [])
        self.assertNotIn("strategy", self.server.requests[0]["body"]["items"][0])
        self.reply(self.ack(operation_id="66e14902-803c-426d-a657-0a7e692f978a"))
        self.error("invalid_submit_response", lambda: self.client.submit(DOC, OP, TEXT, "unset", {}, []))

    def test_all_native_operation_states_and_upstream_diagnostics_are_safe(self):
        for state in ("pending", "processing", "completed", "failed", "cancelled", "not_found"):
            with self.subTest(state=state):
                self.reply({"operation_id": OP, "status": state, "retry_count": 2,
                            "error_message": "FAKE-SENSITIVE-DATA", "result_metadata": {"token": "FAKE"},
                            "task_payload": {"content": TEXT}})
                result = self.client.operation(OP)
                self.assertEqual(result["status"], state)
                self.assertNotIn("FAKE", repr(result))
                self.assertNotIn("task_payload", result)
        self.assertEqual(self.server.requests[0]["path"], PREFIX + "/operations/" + OP)
        self.reply({"operation_id": OP, "status": ["completed"]})
        self.error("invalid_operation_response", lambda: self.client.operation(OP))

    def test_searchable_needs_exact_text_and_document_filtered_real_unit(self):
        self.reply(self.document())
        self.reply(self.memories())
        result = self.client.verify_document(DOC, DIGEST)
        self.assertTrue(result["searchable"])
        self.assertEqual(result["text_sha256"], DIGEST)
        self.assertEqual(result["memory_unit_count"], 2)
        self.assertEqual(self.server.requests[0]["path"], PREFIX + "/documents/source%3Aexample-v1")
        self.assertEqual(self.server.requests[1]["path"], PREFIX + "/memories/list?document_id=source%3Aexample-v1&limit=1")

    def test_explicit_native_retry_preserves_identity_without_source_resubmission(self):
        self.reply({"success": True, "operation_id": OP, "message": "FAKE-SENSITIVE"})
        self.assertEqual(self.client.retry_operation(OP), {"success": True, "operation_id": OP})
        request = self.server.requests[-1]
        self.assertEqual((request["method"], request["path"], request["body"]),
                         ("POST", PREFIX + "/operations/" + OP + "/retry", None))
        for status in (404, 409):
            self.reply({"detail": "FAKE-SENSITIVE"}, status=status)
            self.error("http_error", lambda: self.client.retry_operation(OP), status)
        self.reply({"success": True, "operation_id": "wrong"})
        self.error("invalid_retry_response", lambda: self.client.retry_operation(OP))

    def test_failed_verification_cannot_claim_searchability(self):
        cases = [({"original_text": TEXT + " edited"}, {}), ({"original_text": None}, {}),
                 ({"memory_unit_count": 0}, {}), ({}, {"items": [], "total": 0}),
                 ({}, {"items": [{"id": "wrong", "document_id": "another"}]}),
                 ({}, {"items": [{"text": "missing identity"}]}), ({}, {"total": 0})]
        for doc_changes, list_changes in cases:
            with self.subTest(document=doc_changes, listed=list_changes):
                self.reply(self.document(**doc_changes))
                self.reply(self.memories(**list_changes))
                self.assertFalse(self.client.verify_document(DOC, DIGEST)["searchable"])

    def test_document_missing_is_negative_receipt_but_server_failure_is_error(self):
        self.reply({"detail": "not here"}, status=404)
        result = self.client.verify_document(DOC, DIGEST)
        self.assertFalse(result["exists"])
        self.assertFalse(result["searchable"])
        self.assertEqual(len(self.server.requests), 1)
        self.reply({"detail": "FAKE-SENSITIVE-DATA"}, status=500)
        self.error("http_error", lambda: self.client.verify_document(DOC, DIGEST), 500)

    def test_unexpected_document_or_count_is_rejected(self):
        for changes in ({"id": "other"}, {"bank_id": "other"}, {"memory_unit_count": True},
                        {"memory_unit_count": -1}, {"original_text": {"bad": "schema"}}):
            self.reply(self.document(**changes))
            self.error("invalid_document_response", lambda: self.client.verify_document(DOC, DIGEST))

    def test_recall_keeps_native_references_and_requests_strict_scope(self):
        response = {"results": [{"id": "fact", "document_id": DOC, "chunk_id": "chunk-1"}],
                    "chunks": {"chunk-1": {"id": "chunk-1", "text": TEXT, "chunk_index": 0, "truncated": False}},
                    "source_facts": {"fact": {"document_id": DOC}}}
        self.reply(response)
        result = self.client.recall("Which exact detail?", ["project:one", "user:anton"])
        self.assertEqual(result, response)
        body = self.server.requests[-1]["body"]
        self.assertEqual(body["tags_match"], "all_strict")
        self.assertEqual(body["tags"], ["project:one", "user:anton"])
        self.assertIn("chunks", body["include"])
        self.assertIn("source_facts", body["include"])
        self.reply({"results": []})
        self.client.recall("global", [])
        self.assertEqual(self.server.requests[-1]["body"]["tags_match"], "exact")

    def test_cancel_and_terminal_delete_use_distinct_native_paths(self):
        for method, suffix in ((self.client.cancel_operation, ""), (self.client.delete_operation, "/delete")):
            self.reply({"success": True, "operation_id": OP, "message": "FAKE-SENSITIVE"})
            self.assertEqual(method(OP), {"success": True, "operation_id": OP})
            request = self.server.requests[-1]
            self.assertEqual((request["method"], request["path"]),
                             ("DELETE", PREFIX + "/operations/" + OP + suffix))
            self.reply({"detail": "FAKE-SENSITIVE"}, status=409)
            self.error("http_error", lambda: method(OP), 409)

    def operation_page(self, status="pending", total=1, limit=1, offset=0):
        return {"bank_id": BANK, "total": total, "limit": limit, "offset": offset,
                "operations": [{"id": OP, "task_type": "batch_retain", "status": status,
                                "items_count": 1, "document_id": DOC, "filename": "private-file",
                                "error_message": "FAKE-SENSITIVE", "task_payload": {"content": TEXT}}]
                if total > offset else []}

    def test_operations_are_paged_and_sanitized_without_hiding_parent_jobs(self):
        self.reply(self.operation_page(total=7, offset=2))
        result = self.client.list_operations(status="pending", limit=1, offset=2)
        self.assertEqual(result["total"], 7)
        self.assertEqual(result["operations"], [{"id": OP, "task_type": "batch_retain", "status": "pending",
                                               "items_count": 1, "document_id": DOC}])
        self.assertEqual(self.server.requests[-1]["path"], PREFIX + "/operations?limit=1&offset=2&status=pending")
        self.assertNotIn("FAKE", repr(result))
        self.reply(self.operation_page(status="processing"))
        self.error("invalid_operations_response", lambda: self.client.list_operations(status="pending", limit=1))
        invalid = self.operation_page()
        invalid["operations"] = []
        self.reply(invalid)
        self.error("inconsistent_operations_page", lambda: self.client.list_operations(limit=1))

    def test_clear_observations_is_explicit_and_does_not_claim_readback(self):
        self.reply({"success": True, "deleted_count": 3, "message": "FAKE-SENSITIVE"})
        self.assertEqual(self.client.clear_observations(), {"success": True, "deleted_count": 3})
        self.assertEqual((self.server.requests[-1]["method"], self.server.requests[-1]["path"]),
                         ("DELETE", PREFIX + "/observations"))
        self.reply({"success": True, "deleted_count": True})
        self.error("invalid_clear_observations_response", self.client.clear_observations)

    def test_reconciliation_snapshot_reports_derivatives_and_jobs_without_claiming_closure(self):
        self.reply({"total": 4, "items": [{"id": "observation"}]})
        self.reply({"total": 3, "items": [{"id": "mental-model"}]})
        self.reply({"roots": [{"id": "folder", "kind": "folder", "children": [
            {"id": "page", "kind": "page", "children": []}]}]})
        self.reply(self.operation_page(status="pending", total=2))
        self.reply(self.operation_page(status="processing", total=1))
        self.assertEqual(self.client.reconciliation_state(), {
            "observations": 4, "mental_models": 3, "knowledge_pages": 1,
            "pending_operations": 2, "processing_operations": 1})
        self.assertEqual([r["path"] for r in self.server.requests], [
            PREFIX + "/memories/list?type=observation&limit=1",
            PREFIX + "/mental-models?detail=metadata&limit=1&offset=0",
            PREFIX + "/knowledge-base/tree", PREFIX + "/operations?limit=1&offset=0&status=pending",
            PREFIX + "/operations?limit=1&offset=0&status=processing"])

    def test_reconciliation_empty_is_observed_and_malformed_is_never_empty(self):
        self.reply({"total": 0, "items": []})
        self.reply({"total": 0, "items": []})
        self.reply({"roots": []})
        self.reply(self.operation_page(status="pending", total=0))
        self.reply(self.operation_page(status="processing", total=0))
        self.assertEqual(self.client.reconciliation_state(), {
            "observations": 0, "mental_models": 0, "knowledge_pages": 0,
            "pending_operations": 0, "processing_operations": 0})
        self.reply({"total": 1, "items": []})
        self.error("invalid_reconciliation_response", self.client.reconciliation_state)
        self.reply({"total": 0, "items": []})
        self.reply({"total": 0, "items": []})
        self.reply({"roots": [{"id": "page", "kind": "page"}]})
        self.error("invalid_knowledge_tree_response", self.client.reconciliation_state)

    def test_delete_uses_exact_document_endpoint_and_does_not_hide_404(self):
        self.reply({"success": True, "message": "FAKE-SENSITIVE", "document_id": DOC,
                    "memory_units_deleted": 2})
        result = self.client.delete_document(DOC)
        self.assertEqual(result, {"success": True, "document_id": DOC, "memory_units_deleted": 2})
        self.assertEqual(self.server.requests[0]["method"], "DELETE")
        self.assertEqual(self.server.requests[0]["path"], PREFIX + "/documents/source%3Aexample-v1")
        self.reply({}, status=404)
        self.error("http_error", lambda: self.client.delete_document(DOC), 404)

    def test_deletion_readback_requires_absence_of_both_document_and_units(self):
        for document_missing, units_missing in ((False, False), (False, True), (True, False), (True, True)):
            with self.subTest(document_missing=document_missing, units_missing=units_missing):
                self.reply({} if document_missing else self.document(), status=404 if document_missing else 200)
                self.reply(self.memories(items=[], total=0) if units_missing else self.memories())
                result = self.client.verify_deleted_document(DOC)
                self.assertEqual(result["document_absent"], document_missing)
                self.assertEqual(result["memory_units_absent"], units_missing)
                self.assertEqual(result["deleted"], document_missing and units_missing)
                self.assertNotIn("reconciled", result)
        self.reply({}, status=503)
        self.error("http_error", lambda: self.client.verify_deleted_document(DOC), 503)

    def test_loopback_only_and_no_path_injection(self):
        urls = ["http://example.com", "http://127.0.0.1.example.com", "http://0.0.0.0", "file:///tmp/test",
                "http://user:password@127.0.0.1", "http://127.0.0.1/path", "http://127.0.0.1?token=x",
                "http://127.0.0.1#x", "http://127.0.0.1:0", "http://[::1%25lo0]", " http://127.0.0.1"]
        for url in urls:
            self.error("invalid_base_url", lambda: HindsightClient(url, BANK))
        for identifier in ("", "../anton", "doc/path", "doc?x=1", "doc#x", "doc%2Fother", "doc\nother"):
            self.error("invalid_identifier", lambda: HindsightClient(self.server.url, identifier))
            self.error("invalid_identifier", lambda: self.client.delete_document(identifier))
        self.assertEqual(HindsightClient("http://localhost:8888/", BANK).base_url, "http://127.0.0.1:8888")
        self.assertEqual(HindsightClient("http://[::1]:8888", BANK).base_url, "http://[::1]:8888")
        self.assertFalse(self.server.requests)

    def test_auth_is_injected_per_request_and_environment_proxy_is_ignored(self):
        client = HindsightClient(self.server.url, BANK, api_key_env="OLYMPUS_TEST_TOKEN")
        self.reply({"operation_id": OP, "status": "pending"})
        with patch.dict(os.environ, {"OLYMPUS_TEST_TOKEN": "fake-test-token", "HTTP_PROXY": "http://192.0.2.1:1", "NO_PROXY": ""}):
            client.operation(OP)
        self.assertEqual(self.server.requests[0]["headers"]["Authorization"], "Bearer fake-test-token")
        self.assertNotIn("fake-test-token", repr(vars(client)))
        with patch.dict(os.environ, {}, clear=True):
            self.error("credential_unavailable", lambda: client.operation(OP))
        with patch.dict(os.environ, {"OLYMPUS_TEST_TOKEN": "bad\r\nheader"}):
            self.error("invalid_credential", lambda: client.operation(OP))

    def test_redirects_never_carry_credentials_or_request_body(self):
        with LocalServer() as destination:
            client = HindsightClient(self.server.url, BANK, api_key_env="OLYMPUS_TEST_TOKEN")
            for status in (301, 302, 303, 307, 308):
                self.reply({"detail": "FAKE-SENSITIVE"}, status=status, headers={"Location": destination.url + "/collect"})
                with patch.dict(os.environ, {"OLYMPUS_TEST_TOKEN": "fake-test-token"}):
                    self.error("redirect_refused", lambda: client.submit(DOC, OP, TEXT, "unset", {}, []), status)
            self.assertEqual(destination.requests, [])

    def test_http_failures_never_include_upstream_content_and_never_retry(self):
        for status in (401, 409, 429, 500):
            self.reply({"detail": "FAKE-SENSITIVE-DATA"}, status=status)
            error = self.error("http_error", lambda: self.client.operation(OP), status)
            self.assertNotIn("FAKE", repr(error))
        self.assertEqual(len(self.server.requests), 4)

    def test_body_limits_apply_with_and_without_content_length(self):
        client = HindsightClient(self.server.url, BANK, max_response_bytes=64)
        for no_length in (False, True):
            self.reply(b"{" + b" " * 100 + b"}", no_length=no_length)
            self.error("response_too_large", lambda: client.operation(OP), 200)
        tiny = HindsightClient(self.server.url, BANK, max_request_bytes=10)
        before = len(self.server.requests)
        self.error("request_too_large", lambda: tiny.submit(DOC, OP, TEXT, "unset", {}, []))
        self.assertEqual(len(self.server.requests), before)

    def test_malformed_responses_are_safe(self):
        for raw in (b"not json FAKE-SENSITIVE", b"\xff", b"{", b'{"value": NaN}'):
            self.reply(raw)
            self.error("invalid_json", lambda: self.client.operation(OP), 200)
        self.reply([])
        self.error("invalid_response", lambda: self.client.operation(OP), 200)
        self.reply(b"<html>FAKE-SENSITIVE</html>", headers={"Content-Type": "text/html"})
        self.error("unexpected_content_type", lambda: self.client.operation(OP), 200)
        self.reply({}, headers={"Content-Encoding": "gzip"})
        self.error("unexpected_content_encoding", lambda: self.client.operation(OP), 200)
        self.reply(b"{}", headers={"Content-Length": "30"})
        self.error("incomplete_response", lambda: self.client.operation(OP), 200)

    def test_timeout_and_connection_failure_are_bounded_and_safe(self):
        self.reply({}, delay=0.15)
        client = HindsightClient(self.server.url, BANK, timeout=0.03)
        self.error("timeout", lambda: client.operation(OP))
        with LocalServer() as closed:
            unavailable_url = closed.url
        unavailable = HindsightClient(unavailable_url, BANK, timeout=0.1)
        self.error("transport_error", lambda: unavailable.operation(OP))

    def test_bad_inputs_fail_before_network(self):
        self.error("invalid_operation_id", lambda: self.client.operation("../other"))
        self.error("invalid_timestamp", lambda: self.client.submit(DOC, OP, TEXT, "yesterday", {}, []))
        self.error("invalid_metadata", lambda: self.client.submit(DOC, OP, TEXT, "unset", {"n": 1}, []))
        self.error("invalid_tags", lambda: self.client.recall("query", [""]))
        self.error("invalid_expected_hash", lambda: self.client.verify_document(DOC, "bad"))
        self.error("invalid_timeout", lambda: HindsightClient(self.server.url, BANK, timeout=float("nan")))
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main()
