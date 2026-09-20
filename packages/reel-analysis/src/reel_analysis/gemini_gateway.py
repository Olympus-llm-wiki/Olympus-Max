"""Selected Zapro Gemini-native transport; response content never executes tools."""
from __future__ import annotations

import base64
from pathlib import Path
import time

from .antigravity import UnknownOutcome, parse_terminal_json
from .common import ReelError, file_hash, read_json, write_json
from .zapro_client import BASE_URL, ZaproClient, ZaproError, credential


class Zapro:
    def __init__(self, thinking="low", *, transport=None):
        if thinking not in ("low", "medium", "high"):
            raise ReelError("review_thinking_invalid")
        self.thinking = thinking
        self.transport = transport

    def identity(self):
        return {"backend": "zapro", "endpoint": BASE_URL, "protocol": "gemini_generate_content_v1beta", "thinking": self.thinking}

    @staticmethod
    def recover(attempt):
        metadata_file, body_file = attempt / "http.json", attempt / "raw-response.json"
        if not metadata_file.exists() or not body_file.exists():
            raise UnknownOutcome("gateway_outcome_unknown")
        metadata = read_json(metadata_file)
        if file_hash(body_file) != metadata["response_sha256"]:
            raise ReelError("gateway_response_hash_mismatch")
        if metadata["http_status"] != 200:
            if metadata.get("category") in ("protocol_unavailable", "model_unavailable", "upstream_duplicate"):
                raise ReelError("zapro_" + metadata["category"])
            raise ReelError("gateway_http_" + str(metadata["http_status"]))
        try:
            response = read_json(body_file)
        except ValueError:
            raise ReelError("gateway_response_not_json") from None
        if not isinstance(response, dict):
            raise ReelError("gateway_response_not_json")
        candidates = response.get("candidates") or []
        if len(candidates) != 1:
            raise ReelError("gateway_candidates_invalid")
        candidate = candidates[0]
        if not isinstance(candidate, dict) or not isinstance(candidate.get("content"), dict):
            raise ReelError("gateway_candidates_invalid")
        parts = candidate.get("content", {}).get("parts") or []
        if any(any(key in part for key in ("functionCall", "toolCall", "executableCode")) for part in parts):
            raise ReelError("gateway_unexpected_tool_call")
        if candidate.get("finishReason") not in ("STOP", "stop"):
            raise ReelError("gateway_incomplete_response")
        content = "\n".join(part["text"] for part in parts if isinstance(part.get("text"), str) and not part.get("thought"))
        if not content.strip():
            raise ReelError("gateway_empty_response")
        request = read_json(attempt / "input.json")
        try:
            data = parse_terminal_json(content, request["schema"].get("required"), request["schema"].get("properties"))
        except (ValueError, TypeError):
            raise ReelError("gateway_review_not_json") from None
        usage = response.get("usageMetadata") or {}
        wrapped = bool(usage.get("billing_usage"))
        normalized = {
            "input_tokens": usage.get("promptTokenCount"),
            "output_tokens": usage.get("candidatesTokenCount"),
            "total_tokens": usage.get("totalTokenCount"),
            "thinking_tokens": None if wrapped else usage.get("thoughtsTokenCount"),
            "cache_read_tokens": usage.get("cachedContentTokenCount"),
        }
        envelope = {"data": data, "usage": normalized, "usage_basis": "gateway_reported_wrapped" if wrapped else "gateway_reported", "backend": "zapro", "status": "SUCCESS", "timing": metadata}
        write_json(attempt / "response.json", envelope)
        return envelope

    def invoke(self, *, attempt, files, prompt, schema, model, profile):
        if (attempt / "submitted.json").exists():
            return self.recover(attempt)
        try:
            credential()
        except ZaproError as exc:
            raise ReelError(exc.code) from None
        if not model.startswith("gemini-") or not all(c.isalnum() or c in "-_." for c in model):
            raise ReelError("gateway_model_invalid")
        if len(files) != 1:
            raise ReelError("gateway_requires_one_video")
        source = Path(files[0])
        if source.suffix.lower() not in (".mp4", ".mov", ".webm") or source.stat().st_size > 100 * 1024 * 1024:
            raise ReelError("gateway_media_unsupported")
        attempt.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        mime = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}[source.suffix.lower()]
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}, {"inlineData": {"mimeType": mime, "data": base64.b64encode(source.read_bytes()).decode()}}]}],
            "generationConfig": {"thinkingConfig": {"thinkingLevel": self.thinking}, "maxOutputTokens": profile.target_output_tokens, "responseMimeType": "application/json", "responseJsonSchema": schema},
        }
        # Byte-identical originals already live in the corpus; never store auth headers or duplicate base64.
        write_json(attempt / "input.json", {"files": [{"path": str(source), "sha256": file_hash(source)}], "prompt": prompt, "model": model, "schema": schema, "thinking": self.thinking, "max_output_tokens_requested": profile.target_output_tokens, "output_limit_enforced": "unverified"})
        prepared_s = time.monotonic() - started
        write_json(attempt / "submitted.json", {"started_at": time.time(), **self.identity(), "model": model})
        try:
            client = ZaproClient(transport=self.transport, timeout=profile.timeout_s)
            payload, metadata = client.request("native", model=model, body=body)
        except ZaproError as exc:
            write_json(attempt / "diagnostic.json", {"error": exc.code, **exc.receipt})
            if exc.payload is None:
                write_json(attempt / "transport-error.json", exc.receipt)
                raise UnknownOutcome("gateway_transport_unknown") from None
            payload, metadata = exc.payload, exc.receipt
        finally:
            write_json(attempt / "execution.json", {"elapsed_s": time.monotonic() - started})
        write_json(attempt / "raw-response.json", payload)
        metadata.update(prepare_s=prepared_s, response_sha256=file_hash(attempt / "raw-response.json"), ended_at=time.time())
        write_json(attempt / "http.json", metadata)
        return self.recover(attempt)
