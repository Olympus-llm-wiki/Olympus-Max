import json
from pathlib import Path
import threading
import time

import httpx
import pytest

from reel_analysis.antigravity import UnknownOutcome
from reel_analysis.common import ReelError, file_hash, read_json, write_json
from reel_analysis.contracts import Profile
from reel_analysis.gemini_gateway import BASE_URL, Zapro
from reel_analysis.review_batch import caption_mentions, configuration, plan, run, status
from reel_analysis.review_contract import SCHEMA, validate_review


def valid():
    return {"summary": "Product demonstration", "format": "routine", "speech_access": "not_detected", "visual_access": "observed", "products": [{"name": "BrandA", "evidence": [{"basis": "caption", "quote": "BrandA", "time_s": None}]}], "commercial_signals": [{"kind": "sponsorship_disclosed", "evidence": [{"basis": "caption", "quote": "AD", "time_s": None}]}], "ctas": [], "beats": [{"start_s": 0., "end_s": 2., "description": "Uses the product"}], "limitations": []}


def inputs(tmp_path, count=3):
    items = []
    for i in range(count):
        video, context = tmp_path / f"v{i}.mp4", tmp_path / f"v{i}.json"
        video.write_bytes(b"synthetic-mock-input")
        write_json(context, {"caption": "AD BrandA", "transcript": "", "metadata": ""})
        items.append({"id": f"v{i}", "path": str(video), "sha256": file_hash(video), "duration_s": 2, "context_path": str(context), "context_sha256": file_hash(context)})
    path = tmp_path / "manifest.json"
    write_json(path, {"schema_version": 1, "corpus_id": "review-test", "items": items})
    return path


class Fixture:
    def __init__(self, unknown=None, reject=None):
        self.unknown, self.reject = unknown, reject
        self.calls, self.active, self.peak = 0, 0, 0
        self.lock = threading.Lock()

    def invoke(self, **kw):
        attempt = kw["attempt"]
        with self.lock:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        write_json(attempt / "submitted.json", {"mock": True})
        try:
            time.sleep(.025)
            if kw["files"][0].stem == self.unknown:
                raise UnknownOutcome("fixture_unknown")
            result = valid()
            if kw["files"][0].stem == self.reject:
                result["products"][0]["evidence"][0]["quote"] = "invented"
            envelope = {"data": result, "usage": {"input_tokens": 20, "output_tokens": 30, "total_tokens": 50}}
            write_json(attempt / "response.json", envelope)
            return envelope
        finally:
            with self.lock:
                self.active -= 1

    @staticmethod
    def recover(attempt):
        if not (attempt / "response.json").exists():
            raise UnknownOutcome("fixture_unknown")
        return read_json(attempt / "response.json")


def test_contract_rejects_invented_quote_subtitles_and_audio_contradiction():
    context = {"caption": "AD BrandA"}
    assert validate_review(valid(), 2, context)["products"][0]["name"] == "BrandA"
    bad = valid()
    bad["subtitles"] = []
    with pytest.raises(ReelError, match="schema_invalid"):
        validate_review(bad, 2, context)


    bad = valid()
    bad["products"][0]["evidence"][0]["quote"] = "not in source"
    with pytest.raises(ReelError, match="quote_not_found"):
        validate_review(bad, 2, context)
    bad = valid()
    bad["products"][0]["evidence"][0]["basis"] = "speech"
    with pytest.raises(ReelError, match="speech_access_contradiction"):
        validate_review(bad, 2, context)
    bad = valid()
    bad["beats"][0]["end_s"] = 4
    with pytest.raises(ReelError, match="interval_invalid"):
        validate_review(bad, 2, context)


def test_caption_mentions_are_source_links_not_invented_cta():
    result = caption_mentions("See @lovable.dev and @brand. @lovable.dev Contact user@example.com")
    assert [r["handle"] for r in result] == ["@lovable.dev", "@brand"]
    assert all(r["is_cta"] is False and r["basis"] == "caption" for r in result)


def test_single_evidence_shape_is_losslessly_normalized_without_editing_raw():
    data = valid()
    data["products"][0]["evidence"] = data["products"][0]["evidence"][0]
    original = json.dumps(data)
    result = validate_review(data, 2, {"caption": "AD BrandA"})
    assert isinstance(result["products"][0]["evidence"], list)
    assert json.dumps(data) == original


def test_parallel_limit_reuse_and_report(tmp_path):
    manifest, output = inputs(tmp_path, 5), tmp_path / "out"
    backend = Fixture()
    first = run(manifest, output, workers=2, backend=backend)
    assert backend.peak == 2
    assert first["counts"]["ready"] == 5
    assert first["execution"]["new_calls"] == 5
    assert first["execution"]["reported_usage_new_calls"]["total_tokens"] == 250
    assert (output / "report.md").is_file()
    second = run(manifest, output, workers=4, backend=backend)
    assert second["execution"]["reused"] == 5 and second["execution"]["new_calls"] == 0
    assert backend.calls == 5
    assert status(output)["counts"]["ready"] == 5


def test_unknown_and_invalid_are_not_accepted_or_silently_retried(tmp_path):
    manifest, output = inputs(tmp_path), tmp_path / "out"
    first = Fixture(unknown="v1", reject="v2")
    result = run(manifest, output, workers=2, backend=first)
    assert result["counts"] == {"ready": 1, "failed": 1, "unknown": 1, "pending": 0, "running": 0}
    assert result["execution"]["new_calls_with_unknown_usage"] == 1
    second = Fixture()
    run(manifest, output, backend=second)
    assert second.calls == 0
    retried = run(manifest, output, backend=second, retry_failed=True)
    assert second.calls == 2 and retried["counts"]["ready"] == 3
    assert len(list((output / "items/v1").glob("attempt-*"))) == 2


def test_source_context_config_and_result_integrity_before_calls(tmp_path):
    manifest, output = inputs(tmp_path, 1), tmp_path / "out"
    backend = Fixture()
    run(manifest, output, backend=backend)
    with pytest.raises(ReelError, match="fingerprint_mismatch"):
        plan(manifest, output, configuration(thinking="high"))
    result = output / "items/v0/result.json"
    result.write_text("{}")
    with pytest.raises(ReelError, match="hash_mismatch"):
        run(manifest, output, backend=backend)
    assert backend.calls == 1
    (tmp_path / "v0.json").write_text("{}")
    with pytest.raises(ReelError, match="context_hash_mismatch"):
        plan(manifest, tmp_path / "other")


def test_interrupted_publication_recovers_from_terminal_without_call(tmp_path):
    manifest, output = inputs(tmp_path, 1), tmp_path / "out"
    run(manifest, output, backend=Fixture())
    folder = output / "items/v0"
    (folder / "integrity.json").unlink()
    (folder / "attempt-0001/attempt.json").unlink()
    write_json(folder / "state.json", {"state": "running"})
    backend = Fixture()
    result = run(manifest, output, backend=backend)
    assert backend.calls == 0 and result["counts"]["ready"] == 1
    assert read_json(folder / "result.json")["recovered"]
    ledger = read_json(output / "attempt-ledger.json")["attempts"]
    assert len(ledger) == 1 and ledger[0]["usage"]["total_tokens"] == 50
    assert ledger[0]["state"] == "interrupted_attempt"


def test_live_status_reads_incremental_snapshot_and_second_run_is_busy(tmp_path):
    from reel_analysis.raw_batch import batch_lock
    output = tmp_path / "out"
    plan(inputs(tmp_path, 1), output)
    with batch_lock(output):
        assert status(output)["counts"]["pending"] == 1
        with pytest.raises(ReelError, match="busy"):
            run(tmp_path / "manifest.json", output, backend=Fixture())


def test_auth_failure_pauses_unstarted_work(tmp_path):
    class Unauthorized(Fixture):
        def invoke(self, **kw):
            self.calls += 1
            write_json(kw["attempt"] / "submitted.json", {"mock": True})
            raise ReelError("gateway_http_401")
    backend = Unauthorized()
    result = run(inputs(tmp_path, 4), tmp_path / "out", backend=backend, workers=1)
    assert backend.calls == 1
    assert result["counts"]["failed"] == 1 and result["counts"]["pending"] == 3


def gateway_call(tmp_path, monkeypatch, handler):
    monkeypatch.delenv("GEMINI_ZAPRO", raising=False)
    monkeypatch.setenv("POZAPROSU_API_TOKEN", "test-private-token")
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"synthetic mock bytes")
    gateway = Zapro(transport=httpx.MockTransport(handler))
    return gateway, dict(attempt=tmp_path / "attempt", files=[source], prompt="Review fixture", schema=SCHEMA, model="gemini-3.8-flash", profile=Profile())


def test_native_gateway_video_auth_and_recovery(tmp_path, monkeypatch):
    def handler(request):
        assert str(request.url) == BASE_URL + "/v1beta/models/gemini-3.8-flash:generateContent"
        assert request.headers["x-goog-api-key"] == "test-private-token"
        body = json.loads(request.content)
        assert body["contents"][0]["parts"][1]["inlineData"]["mimeType"] == "video/mp4"
        assert body["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "low"
        return httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "```json\n" + json.dumps(valid()) + "\n```"}]}}], "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 20, "totalTokenCount": 30, "thoughtsTokenCount": 0, "billing_usage": {"source": "oai_chat"}}})
    gateway, args = gateway_call(tmp_path, monkeypatch, handler)
    envelope = gateway.invoke(**args)
    assert envelope["data"] == valid() and envelope["usage"]["thinking_tokens"] is None
    assert gateway.recover(args["attempt"])["data"] == valid()
    assert all("test-private-token" not in p.read_text() for p in args["attempt"].glob("*.json"))


@pytest.mark.parametrize("status_code,body,error", [(302,{"error":"test-private-token"},"gateway_http_302"),(200,{"candidates":[{"finishReason":"STOP","content":{"parts":[{"functionCall":{"name":"untrusted"}}]}}]},"unexpected_tool"),(200,{"candidates":[{"finishReason":"MAX_TOKENS","content":{"parts":[{"text":"{}"}]}}]},"incomplete_response")])
def test_gateway_refuses_redirects_tools_and_truncation(tmp_path, monkeypatch, status_code, body, error):
    count = []
    def handler(request):
        count.append(str(request.url))
        return httpx.Response(status_code, json=body, headers={"Location":"https://example.invalid/other"})
    gateway, args = gateway_call(tmp_path, monkeypatch, handler)
    with pytest.raises(ReelError, match=error):
        gateway.invoke(**args)
    assert len(count) == 1
    assert "test-private-token" not in (args["attempt"] / "raw-response.json").read_text()


def test_gateway_timeout_is_unknown_and_does_not_retry(tmp_path, monkeypatch):
    count = []
    def handler(request):
        count.append(1)
        raise httpx.ReadTimeout("do not expose request headers")
    gateway, args = gateway_call(tmp_path, monkeypatch, handler)
    with pytest.raises(UnknownOutcome):
        gateway.invoke(**args)
    with pytest.raises(UnknownOutcome):
        gateway.invoke(**args)
    assert len(count) == 1


def test_native_channel_error_retains_shared_diagnostic(tmp_path, monkeypatch):
    gateway, args = gateway_call(tmp_path, monkeypatch, lambda r: httpx.Response(400, json={
        "error": {"code": "invalid_channel_identity", "message": "Invalid internal channel identity"}}))
    with pytest.raises(ReelError, match="zapro_protocol_unavailable"):
        gateway.invoke(**args)
    receipt = read_json(args["attempt"] / "http.json")
    assert receipt["category"] == "protocol_unavailable"
    assert receipt["upstream_code"] == "invalid_channel_identity"
    assert receipt["protocol"] == "native"


def test_native_protocol_failure_pauses_unstarted_work(tmp_path):
    class Unavailable(Fixture):
        def invoke(self, **kw):
            self.calls += 1
            write_json(kw["attempt"] / "submitted.json", {"mock": True})
            raise ReelError("zapro_protocol_unavailable")
    backend = Unavailable()
    result = run(inputs(tmp_path, 4), tmp_path / "out", backend=backend, workers=1)
    assert backend.calls == 1
    assert result["counts"]["failed"] == 1
    assert result["counts"]["pending"] == 3


def test_stale_running_attempt_becomes_unknown_without_paid_retry(tmp_path):
    manifest, output = inputs(tmp_path, 1), tmp_path / "out"
    plan(manifest, output)
    folder = output / "items/v0"
    write_json(folder / "state.json", {"state": "running", "attempt": "attempt-0001"})
    write_json(folder / "attempt-0001/started.json", {"started_at": 1})
    write_json(folder / "attempt-0001/submitted.json", {"mock": True})
    backend = Fixture()
    result = run(manifest, output, backend=backend)
    assert backend.calls == 0 and result["counts"]["unknown"] == 1
    assert result["counts"]["running"] == 0
    assert read_json(folder / "state.json")["state"] == "unknown"


def test_one_bounded_retry_of_new_completed_invalid_output(tmp_path):
    class InvalidOnce(Fixture):
        def invoke(self, **kw):
            result = super().invoke(**kw)
            if self.calls == 1:
                result["data"]["products"][0]["evidence"][0]["quote"] = "not a source quote"
                write_json(kw["attempt"] / "response.json", result)
            return result
    backend = InvalidOnce()
    result = run(inputs(tmp_path, 1), tmp_path / "out", backend=backend)
    assert result["counts"]["ready"] == 1 and backend.calls == 2
    assert result["execution"]["format_retries_used"] == 1
    assert result["execution"]["reported_usage_new_calls"]["total_tokens"] == 100
    assert result["execution"]["reused"] == 0


def test_review_classifies_complete_terminal_but_not_transport_unknown(tmp_path, monkeypatch):
    from reel_analysis.antigravity import Antigravity
    from reel_analysis.review_antigravity import ReviewAntigravity
    def unknown(attempt):
        raise UnknownOutcome("inference_outcome_unknown")
    monkeypatch.setattr(Antigravity, "recover", unknown)
    with pytest.raises(UnknownOutcome):
        ReviewAntigravity.recover(tmp_path)
    (tmp_path / "raw.ndjson").write_text(json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "invalid json"}}))
    with pytest.raises(ReelError, match="completed_response_invalid"):
        ReviewAntigravity.recover(tmp_path)


def test_output_cannot_overwrite_inputs_or_context(tmp_path):
    manifest = inputs(tmp_path, 1)
    before = (tmp_path / "v0.json").read_bytes()
    with pytest.raises(ReelError, match="output_overlaps_source"):
        plan(manifest, tmp_path)
    assert (tmp_path / "v0.json").read_bytes() == before
