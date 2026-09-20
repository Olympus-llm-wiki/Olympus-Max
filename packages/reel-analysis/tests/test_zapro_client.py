import json

import httpx
import pytest

from reel_analysis.zapro_client import ZaproClient, ZaproError


def reply(model="gemini-3.8-flash", content="OK", finish="stop"):
    return {"model": model, "choices": [{"finish_reason": finish, "message": {"content": content}}]}


def client(monkeypatch, handler):
    monkeypatch.setenv("GEMINI_ZAPRO", "private-test-token")
    monkeypatch.delenv("POZAPROSU_API_TOKEN", raising=False)
    return ZaproClient(transport=httpx.MockTransport(handler), retry_delay=0)


def test_chat_contract(monkeypatch):
    def handler(req):
        assert str(req.url) == "https://po.zapro.su/v1/chat/completions"
        assert req.headers["Authorization"] == "Bearer private-test-token"
        assert json.loads(req.content)["stream"] is False
        return httpx.Response(200, json=reply(), headers={"x-request-id": "req-123"})
    result = client(monkeypatch, handler).chat("hello")
    assert result["text"] == "OK"
    assert result["receipt"]["request_id"] == "req-123"
    assert result["receipt"]["attempts"] == 1


@pytest.mark.parametrize("status,code,category", [(503, "request_duplicate", "upstream_duplicate"), (400, "invalid_channel_identity", "protocol_unavailable"), (401, "bad_key", "authentication"), (429, "limited", "rate_limited")])
def test_http_errors_keep_cause_without_retry_or_key(monkeypatch, status, code, category):
    calls = []
    def handler(req):
        calls.append(1)
        return httpx.Response(status, json={"error": {"code": code, "message": "private-test-token"}})
    with pytest.raises(ZaproError) as caught:
        client(monkeypatch, handler).chat("hello")
    assert caught.value.receipt["category"] == category
    assert caught.value.receipt["upstream_code"] == code
    assert "private-test-token" not in json.dumps(caught.value.receipt)
    assert len(calls) == 1


@pytest.mark.parametrize("data,code", [(reply(model="gemini-3.5-flash"), "model_mismatch"), (reply(finish="length"), "incomplete_response"), (reply(content=""), "empty_response"), ([], "invalid_response")])
def test_false_success_rejected(monkeypatch, data, code):
    with pytest.raises(ZaproError, match=code):
        client(monkeypatch, lambda r: httpx.Response(200, json=data)).chat("hello")


def test_read_timeout_never_retried(monkeypatch):
    calls = []
    def handler(req):
        calls.append(1)
        raise httpx.ReadTimeout("private-test-token")
    with pytest.raises(ZaproError) as caught:
        client(monkeypatch, handler).chat("hello")
    assert caught.value.receipt["outcome"] == "unknown"
    assert "private-test-token" not in str(caught.value)
    assert len(calls) == 1


def test_connect_failure_retried_once(monkeypatch):
    calls = []
    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectTimeout("before send")
        return httpx.Response(200, json=reply())
    assert client(monkeypatch, handler).chat("hello")["receipt"]["attempts"] == 2


def test_redirect_never_followed(monkeypatch):
    calls = []
    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(307, headers={"Location": "https://example.com"})
    with pytest.raises(ZaproError):
        client(monkeypatch, handler).chat("hello")
    assert len(calls) == 1


def test_conflicting_credentials_fail_before_network(monkeypatch):
    monkeypatch.setenv("GEMINI_ZAPRO", "first")
    monkeypatch.setenv("POZAPROSU_API_TOKEN", "second")
    with pytest.raises(ZaproError, match="credential_conflict"):
        ZaproClient().chat("hello")


def test_catalog_failure_not_treated_as_empty_catalog(monkeypatch):
    from reel_analysis.zapro_cli import doctor
    result = doctor(client(monkeypatch, lambda r: httpx.Response(401, json={})))
    assert result["state"] == "failed"
    assert result["probes"] == []


def test_doctor_rejects_model_mismatch_and_wrong_marker(monkeypatch):
    from reel_analysis.zapro_cli import doctor
    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "gemini-3.5-flash"}, {"id": "gemini-3.8-flash"}]})
        return httpx.Response(200, json=reply())
    result = doctor(client(monkeypatch, handler), models=["gemini-3.5-flash", "gemini-3.8-flash"])
    assert result["state"] == "degraded"
    assert [p["error"] for p in result["probes"]] == ["zapro_model_mismatch", "zapro_marker_mismatch"]


def test_cli_reserves_output_and_prevents_replay(monkeypatch, tmp_path):
    from reel_analysis import zapro_cli
    from reel_analysis.cli import parser
    from reel_analysis.common import ReelError
    prompt, output = tmp_path / "prompt.txt", tmp_path / "result.json"
    prompt.write_text("hello")
    args = parser().parse_args(["zapro", "chat", "--prompt-file", str(prompt), "--output", str(output)])
    calls = []
    def handler(req):
        calls.append(1)
        assert json.loads(output.read_text())["state"] == "unknown"
        return httpx.Response(200, json=reply())
    instance = client(monkeypatch, handler)
    monkeypatch.setattr(zapro_cli, "ZaproClient", lambda **kwargs: instance)
    assert zapro_cli.dispatch(args)["state"] == "ready"
    with pytest.raises(ReelError, match="output_exists"):
        zapro_cli.dispatch(args)
    assert calls == [1]
    assert output.stat().st_mode & 0o077 == 0


def test_tool_calls_never_accepted(monkeypatch):
    data = reply()
    data["choices"][0]["message"]["tool_calls"] = [{"function": {"name": "run"}}]
    with pytest.raises(ZaproError, match="unexpected_tool_call"):
        client(monkeypatch, lambda r: httpx.Response(200, json=data)).chat("hello")


def test_oversize_response_is_unknown_and_not_retried(monkeypatch):
    from reel_analysis.zapro_client import MAX_RESPONSE_BYTES
    with pytest.raises(ZaproError) as caught:
        client(monkeypatch, lambda r: httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))).chat("hello")
    assert caught.value.code == "zapro_response_too_large"
    assert caught.value.receipt["outcome"] == "unknown"


def test_failed_connect_retry_keeps_bounded_attempt_count(monkeypatch):
    def handler(req):
        raise httpx.ConnectError("failed")
    with pytest.raises(ZaproError) as caught:
        client(monkeypatch, handler).chat("hello")
    assert caught.value.receipt["attempts"] == 2
    assert caught.value.receipt["outcome"] == "not_sent"


def test_legacy_alias_still_supported(monkeypatch):
    from reel_analysis.zapro_client import credential
    monkeypatch.delenv("GEMINI_ZAPRO", raising=False)
    monkeypatch.setenv("POZAPROSU_API_TOKEN", "legacy-test-token")
    assert credential() == "legacy-test-token"


@pytest.mark.parametrize("native_case,expected", [("valid", "ready"), ("invalid", "degraded"), ("tool", "degraded")])
def test_native_doctor_validates_shape_and_tools(monkeypatch, native_case, expected):
    from reel_analysis.zapro_cli import doctor
    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "gemini-3.8-flash"}]})
        body = json.loads(req.content)
        if "messages" in body:
            return httpx.Response(200, json=reply(content=body["messages"][0]["content"].split()[-1]))
        marker = body["contents"][0]["parts"][0]["text"].split()[-1]
        parts = [{"text": marker}]
        if native_case == "tool":
            parts.append({"functionCall": {"name": "run"}})
        data = {"candidates": None} if native_case == "invalid" else {"candidates": [{"finishReason": "STOP", "content": {"parts": parts}}]}
        return httpx.Response(200, json=data)
    result = doctor(client(monkeypatch, handler), models=["gemini-3.8-flash"], native=True)
    assert result["state"] == expected
