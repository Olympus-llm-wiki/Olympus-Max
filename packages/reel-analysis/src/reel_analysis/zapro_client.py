"""Shared Zapro transport. No implicit model fallback or ambiguous POST retries."""
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
import time

import httpx

BASE_URL = "https://po.zapro.su"
DEFAULT_MODEL = "gemini-3.8-flash"
MODELS = tuple(f"gemini-3.{n}-flash" for n in range(5, 9))
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class ZaproError(Exception):
    def __init__(self, code, receipt=None, payload=None):
        super().__init__(code)
        self.code = code
        self.receipt = receipt or {"category": "configuration", "outcome": "not_sent"}
        self.payload = payload


def credential():
    current = os.environ.get("GEMINI_ZAPRO", "").strip()
    legacy = os.environ.get("POZAPROSU_API_TOKEN", "").strip()
    if current and legacy and current != legacy:
        raise ZaproError("zapro_credential_conflict")
    key = current or legacy
    if not key or not key.isascii() or any(c.isspace() for c in key):
        raise ZaproError("zapro_token_unavailable")
    return key


def valid_model(model):
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", model):
        raise ZaproError("zapro_invalid_model")
    return model


class ZaproClient:
    def __init__(self, *, transport=None, timeout=60, retry_delay=0.25):
        if not math.isfinite(timeout) or not 1 <= timeout <= 900:
            raise ZaproError("zapro_invalid_timeout")
        self.transport = transport
        self.timeout = timeout
        self.retry_delay = retry_delay

    def request(self, protocol, *, model=None, body=None):
        """Return decoded body plus a redacted receipt; never execute returned tools."""
        if protocol == "models":
            method, path = "GET", "/v1/models"
        elif protocol == "chat":
            method, path = "POST", "/v1/chat/completions"
            valid_model(model)
        elif protocol == "native":
            method, path = "POST", f"/v1beta/models/{valid_model(model)}:generateContent"
        else:
            raise ZaproError("zapro_invalid_protocol")
        key = credential()
        def clean(value):
            return str(value).replace(key, "[REDACTED]")
        headers = {"Accept": "application/json", "User-Agent": "Olympus-Zapro/1.0"}
        headers["x-goog-api-key" if protocol == "native" else "Authorization"] = key if protocol == "native" else "Bearer " + key
        start = time.monotonic()
        receipt = {"observed_at": datetime.now(timezone.utc).isoformat(), "protocol": protocol,
                   "endpoint": BASE_URL + path, "requested_model": model, "attempts": 0,
                   "http_status": None, "request_id": None, "first_byte_s": None,
                   "outcome": "unknown", "category": "transport"}
        receipt["request_sha256"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        # The same client pools connections across the bounded connect retry.
        with httpx.Client(transport=self.transport, trust_env=False, follow_redirects=False,
                          timeout=self.timeout) as client:
            for attempt in range(2):
                receipt["attempts"] += 1
                try:
                    with client.stream(method, BASE_URL + path, json=body, headers=headers) as response:
                        receipt["http_status"] = response.status_code
                        receipt["request_id"] = clean(response.headers.get("x-request-id") or response.headers.get("request-id") or "")[:200] or None
                        receipt["retry_after"] = clean(response.headers.get("retry-after", ""))[:100] or None
                        size, chunks = 0, []
                        for chunk in response.iter_bytes():
                            if receipt["first_byte_s"] is None:
                                receipt["first_byte_s"] = round(time.monotonic() - start, 3)
                            size += len(chunk)
                            if size > MAX_RESPONSE_BYTES:
                                raise ZaproError("zapro_response_too_large", receipt.copy())
                            if time.monotonic() - start > self.timeout:
                                raise ZaproError("zapro_deadline_exceeded", receipt.copy())
                            chunks.append(chunk)
                        receipt.update(response_bytes=size, elapsed_s=round(time.monotonic() - start, 3), outcome="completed")
                        raw = clean(b"".join(chunks).decode("utf-8", errors="replace"))
                        receipt["response_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
                        try:
                            payload = json.loads(raw)
                        except ValueError:
                            payload = {"unparsed_body": raw}
                        if response.status_code != 200:
                            err = payload.get("error", {}) if isinstance(payload, dict) else {}
                            if not isinstance(err, dict):
                                err = {"message": str(err)}
                            code = str(err.get("code", ""))[:150]
                            category = {401: "authentication", 402: "quota", 403: "permission", 429: "rate_limited"}.get(response.status_code, "upstream_error")
                            if code == "request_duplicate":
                                category = "upstream_duplicate"
                            elif code == "invalid_channel_identity":
                                category = "protocol_unavailable"
                            elif code == "get_channel_failed":
                                category = "model_unavailable"
                            receipt.update(category=category, upstream_code=code or None,
                                           upstream_message=str(err.get("message", ""))[:600])
                            raise ZaproError(f"gateway_http_{response.status_code}", receipt.copy(), payload)
                        receipt["category"] = "success"
                        receipt["reported_model"] = payload.get("model") or payload.get("modelVersion") if isinstance(payload, dict) else None
                        return payload, receipt
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    receipt.update(transport_error=type(exc).__name__, outcome="not_sent", elapsed_s=round(time.monotonic() - start, 3))
                    if attempt == 0 and time.monotonic() - start + self.retry_delay < self.timeout:
                        time.sleep(self.retry_delay)
                        continue
                    raise ZaproError("zapro_connect_failed", receipt.copy()) from None
                except httpx.HTTPError as exc:
                    receipt.update(transport_error=type(exc).__name__, outcome="unknown", elapsed_s=round(time.monotonic() - start, 3))
                    raise ZaproError("zapro_transport_unknown", receipt.copy()) from None
        raise AssertionError("unreachable")

    def models(self):
        payload, receipt = self.request("models")
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ZaproError("zapro_invalid_catalog", receipt)
        return payload["data"], receipt

    def chat(self, prompt, *, model=DEFAULT_MODEL, max_tokens=1024):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ZaproError("zapro_empty_prompt")
        if not isinstance(max_tokens, int) or not 1 <= max_tokens <= 65536:
            raise ZaproError("zapro_invalid_token_limit")
        body = {"model": valid_model(model), "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens, "stream": False}
        payload, receipt = self.request("chat", model=model, body=body)
        def fail(code):
            receipt["category"] = code
            raise ZaproError("zapro_" + code, receipt, payload)
        if not isinstance(payload, dict):
            fail("invalid_response")
        receipt["reported_model"] = payload.get("model")
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            fail("invalid_response")
        choice = choices[0]
        receipt["finish_reason"] = choice.get("finish_reason")
        receipt["usage"] = payload.get("usage")
        message = choice.get("message")
        if not isinstance(message, dict):
            fail("invalid_response")
        if message.get("tool_calls") or message.get("function_call"):
            fail("unexpected_tool_call")
        if choice.get("finish_reason") != "stop":
            fail("incomplete_response")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            fail("empty_response")
        if payload.get("model") != model:
            fail("model_mismatch")
        return {"text": content, "receipt": receipt}
