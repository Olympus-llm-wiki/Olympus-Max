"""Small, explicit-bank HTTP adapter for the pinned Hindsight v0.9.2 API."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from typing import Any


class HindsightError(Exception):
    """A log-safe error: never includes URLs, credentials or upstream bodies."""

    def __init__(self, code: str, status: int | None = None):
        self.code = code
        self.status = status
        super().__init__(code if status is None else f"{code} (HTTP {status})")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", value):
        raise HindsightError("invalid_identifier")
    return value


def _operation_id(value: str) -> str:
    try:
        result = str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise HindsightError("invalid_operation_id") from None
    return result


def _tags(value: list[str]) -> list[str]:
    if not isinstance(value, list) or len(value) > 256:
        raise HindsightError("invalid_tags")
    if any(not isinstance(tag, str) or not tag.strip() or len(tag) > 512 or
           any(ord(c) < 32 or ord(c) == 127 for c in tag) for tag in value):
        raise HindsightError("invalid_tags")
    return list(dict.fromkeys(value))


def _nonnegative_int(value: Any) -> bool:
    return type(value) is int and value >= 0


def _reject_constant(value: str):
    raise ValueError


def _base_url(value: str) -> str:
    if not isinstance(value, str) or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise HindsightError("invalid_base_url")
    try:
        parts = urllib.parse.urlsplit(value)
        host = parts.hostname
        port = parts.port
        if (parts.scheme not in {"http", "https"} or not host or parts.username is not None
                or parts.password is not None or parts.path not in {"", "/"}
                or parts.query or parts.fragment or "%" in host):
            raise ValueError
        # Resolve localhost deterministically; do not use DNS or environment proxies.
        host = "127.0.0.1" if host == "localhost" else host
        address = ipaddress.ip_address(host)
        if not address.is_loopback or port == 0:
            raise ValueError
        netloc = f"[{host}]" if address.version == 6 else host
        if port is not None:
            netloc += f":{port}"
    except ValueError:
        raise HindsightError("invalid_base_url") from None
    return f"{parts.scheme}://{netloc}"


class HindsightClient:
    """No implicit bank, retries, model configuration, logging or secret storage."""

    def __init__(self, base_url: str, bank_id: str, *, api_key_env: str | None = None,
                 timeout: float = 15.0, max_response_bytes: int = 8_388_608,
                 max_request_bytes: int = 8_388_608):
        self.base_url = _base_url(base_url)
        self.bank_id = _identifier(bank_id)
        if (type(timeout) not in {int, float} or not math.isfinite(timeout)
                or not 0 < timeout <= 300):
            raise HindsightError("invalid_timeout")
        for limit in (max_response_bytes, max_request_bytes):
            if type(limit) is not int or not 1 <= limit <= 67_108_864:
                raise HindsightError("invalid_body_limit")
        if api_key_env is not None and (not isinstance(api_key_env, str) or
                not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env)):
            raise HindsightError("invalid_credential_environment")
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.max_request_bytes = max_request_bytes
        self._prefix = "/v1/default/banks/" + urllib.parse.quote(self.bank_id, safe="")
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        payload = None
        if body is not None:
            try:
                payload = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError, UnicodeError):
                raise HindsightError("invalid_request_body") from None
            if len(payload) > self.max_request_bytes:
                raise HindsightError("request_too_large")
            headers["Content-Type"] = "application/json"
        if self.api_key_env is not None:
            token = os.environ.get(self.api_key_env)
            if not token:
                raise HindsightError("credential_unavailable")
            if any(ord(c) < 33 or ord(c) > 126 for c in token):
                raise HindsightError("invalid_credential")
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(self.base_url + self._prefix + path,
                                         data=payload, headers=headers, method=method)
        try:
            started = time.monotonic()
            with self._opener.open(request, timeout=self.timeout) as response:
                status = response.status
                if not 200 <= status < 300:
                    raise HindsightError("http_error", status)
                if response.headers.get_content_type() != "application/json":
                    raise HindsightError("unexpected_content_type", status)
                if response.headers.get("Content-Encoding", "identity").lower() not in {"identity", ""}:
                    raise HindsightError("unexpected_content_encoding", status)
                length = response.headers.get("Content-Length")
                if length is not None:
                    if not length.isdigit():
                        raise HindsightError("invalid_content_length", status)
                    if int(length) > self.max_response_bytes:
                        raise HindsightError("response_too_large", status)
                chunks = []
                size = 0
                while True:
                    if time.monotonic() - started >= self.timeout:
                        raise HindsightError("timeout")
                    chunk = response.read1(min(65_536, self.max_response_bytes - size + 1))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > self.max_response_bytes:
                        raise HindsightError("response_too_large", status)
                raw = b"".join(chunks)
                if length is not None and len(raw) != int(length):
                    raise HindsightError("incomplete_response", status)
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise HindsightError("redirect_refused" if 300 <= status < 400 else "http_error", status) from None
        except urllib.error.URLError as error:
            code = "timeout" if isinstance(error.reason, (TimeoutError, socket.timeout)) else "transport_error"
            raise HindsightError(code) from None
        except (TimeoutError, socket.timeout):
            raise HindsightError("timeout") from None
        except (OSError, ValueError, http.client.HTTPException):
            raise HindsightError("transport_error") from None
        try:
            result = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeError, ValueError, RecursionError):
            raise HindsightError("invalid_json", status) from None
        if not isinstance(result, dict):
            raise HindsightError("invalid_response", status)
        return result

    def submit(self, document_id: str, operation_id: str, text: str, timestamp: str,
               metadata: dict[str, str], tags: list[str], strategy: str | None = None) -> dict:
        document_id = _identifier(document_id)
        operation_id = _operation_id(operation_id)
        if not isinstance(text, str) or not text.strip():
            raise HindsightError("invalid_text")
        try:
            if not isinstance(timestamp, str):
                raise ValueError
            if timestamp.lower() != "unset":
                datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            raise HindsightError("invalid_timestamp") from None
        if not isinstance(metadata, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                 for k, v in metadata.items()):
            raise HindsightError("invalid_metadata")
        item = {"content": text, "document_id": document_id, "timestamp": timestamp,
                "metadata": metadata, "tags": _tags(tags), "update_mode": "replace"}
        if strategy is not None:
            item["strategy"] = _identifier(strategy)
        result = self._request("POST", "/memories", {
            "items": [item], "async": True, "operation_id": operation_id})
        if (result.get("success") is not True or result.get("async") is not True
                or result.get("bank_id") != self.bank_id or result.get("operation_id") != operation_id
                or type(result.get("items_count")) is not int or result["items_count"] != 1
                or result.get("operation_ids") not in (None, [operation_id])):
            raise HindsightError("invalid_submit_response")
        return {"success": True, "bank_id": self.bank_id, "items_count": 1,
                "async": True, "operation_id": operation_id}

    def operation(self, operation_id: str) -> dict:
        operation_id = _operation_id(operation_id)
        result = self._request("GET", "/operations/" + operation_id)
        states = {"pending", "processing", "completed", "failed", "cancelled", "not_found"}
        if (result.get("operation_id") != operation_id or not isinstance(result.get("status"), str)
                or result["status"] not in states):
            raise HindsightError("invalid_operation_response")
        safe = {"operation_id": operation_id, "status": result["status"]}
        if _nonnegative_int(result.get("retry_count")):
            safe["retry_count"] = result["retry_count"]
        if result["status"] == "failed":
            safe["error_code"] = "operation_failed"
        return safe

    def retry_operation(self, operation_id: str) -> dict:
        """Explicit native retry of failed/cancelled work; preserves its UUID."""
        operation_id = _operation_id(operation_id)
        result = self._request("POST", "/operations/" + operation_id + "/retry")
        if result.get("success") is not True or result.get("operation_id") != operation_id:
            raise HindsightError("invalid_retry_response")
        return {"success": True, "operation_id": operation_id}

    def cancel_operation(self, operation_id: str) -> dict:
        """Cancel pending work. This is not a kill switch for a running worker."""
        operation_id = _operation_id(operation_id)
        result = self._request("DELETE", "/operations/" + operation_id)
        if result.get("success") is not True or result.get("operation_id") != operation_id:
            raise HindsightError("invalid_cancel_response")
        return {"success": True, "operation_id": operation_id}

    def delete_operation(self, operation_id: str) -> dict:
        """Explicitly delete a terminal operation, including its retained payload."""
        operation_id = _operation_id(operation_id)
        result = self._request("DELETE", "/operations/" + operation_id + "/delete")
        if result.get("success") is not True or result.get("operation_id") != operation_id:
            raise HindsightError("invalid_operation_delete_response")
        return {"success": True, "operation_id": operation_id}

    def list_operations(self, *, status: str | None = None, limit: int = 100, offset: int = 0) -> dict:
        """One native page, without raw payloads, error messages or filenames."""
        states = {"pending", "processing", "completed", "failed", "cancelled"}
        if status is not None and (not isinstance(status, str) or status not in states):
            raise HindsightError("invalid_operation_status")
        if type(limit) is not int or not 1 <= limit <= 100 or not _nonnegative_int(offset):
            raise HindsightError("invalid_pagination")
        query = {"limit": limit, "offset": offset}
        if status is not None:
            query["status"] = status
        result = self._request("GET", "/operations?" + urllib.parse.urlencode(query))
        operations = result.get("operations")
        if (result.get("bank_id") != self.bank_id or not _nonnegative_int(result.get("total"))
                or not isinstance(operations, list) or len(operations) > limit
                or result.get("limit") != limit or result.get("offset") != offset):
            raise HindsightError("invalid_operations_response")
        safe = []
        for operation in operations:
            if (not isinstance(operation, dict) or not isinstance(operation.get("status"), str)
                    or operation["status"] not in states
                    or status is not None and operation["status"] != status
                    or not _nonnegative_int(operation.get("items_count"))):
                raise HindsightError("invalid_operations_response")
            row = {"id": _operation_id(operation.get("id")), "status": operation["status"],
                   "task_type": _identifier(operation.get("task_type")),
                   "items_count": operation["items_count"]}
            for key in ("document_id", "mental_model_id"):
                if operation.get(key) is not None:
                    row[key] = _identifier(operation[key])
            safe.append(row)
        if len(safe) != min(limit, max(0, result["total"] - offset)):
            raise HindsightError("inconsistent_operations_page")
        return {"bank_id": self.bank_id, "total": result["total"], "limit": limit,
                "offset": offset, "operations": safe}

    def clear_observations(self) -> dict:
        """Explicit bank-wide removal of derived observations, never source documents."""
        result = self._request("DELETE", "/observations")
        if result.get("success") is not True or not _nonnegative_int(result.get("deleted_count")):
            raise HindsightError("invalid_clear_observations_response")
        return {"success": True, "deleted_count": result["deleted_count"]}

    def reconciliation_state(self) -> dict:
        """Read native counts; caller must independently quiesce workers/writers.

        The reads are not a transaction and do not certify a completed revocation.
        No underlying document, mental-model text or operation payload is returned.
        """
        counts = {}
        for key, path in (("observations", "/memories/list?type=observation&limit=1"),
                          ("mental_models", "/mental-models?detail=metadata&limit=1&offset=0")):
            result = self._request("GET", path)
            if (not _nonnegative_int(result.get("total")) or not isinstance(result.get("items"), list)
                    or len(result["items"]) != min(1, result["total"])
                    or any(not isinstance(item, dict) for item in result["items"])):
                raise HindsightError("invalid_reconciliation_response")
            counts[key] = result["total"]
        tree = self._request("GET", "/knowledge-base/tree")
        if not isinstance(tree.get("roots"), list):
            raise HindsightError("invalid_knowledge_tree_response")
        pending_nodes = list(tree["roots"])
        seen = set()
        pages = 0
        while pending_nodes:
            node = pending_nodes.pop()
            if (not isinstance(node, dict) or not isinstance(node.get("id"), str)
                    or not node["id"] or node["id"] in seen or len(seen) >= 10_000
                    or node.get("kind") not in ("folder", "page")
                    or not isinstance(node.get("children"), list)):
                raise HindsightError("invalid_knowledge_tree_response")
            seen.add(node["id"])
            pages += node["kind"] == "page"
            pending_nodes.extend(node["children"])
        counts["knowledge_pages"] = pages
        for status in ("pending", "processing"):
            counts[status + "_operations"] = self.list_operations(status=status, limit=1)["total"]
        return counts

    def _list_document_memories(self, document_id: str) -> dict:
        query = urllib.parse.urlencode({"document_id": document_id, "limit": 1})
        listed = self._request("GET", "/memories/list?" + query)
        if (not _nonnegative_int(listed.get("total")) or not isinstance(listed.get("items"), list)
                or len(listed["items"]) > 1 or any(not isinstance(i, dict) for i in listed["items"])):
            raise HindsightError("invalid_memory_list_response")
        return listed

    def verify_document(self, document_id: str, expected_text_sha256: str) -> dict:
        document_id = _identifier(document_id)
        if not isinstance(expected_text_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_text_sha256):
            raise HindsightError("invalid_expected_hash")
        verification = {"document_id": document_id, "exists": False, "text_sha256": None,
                        "expected_text_sha256": expected_text_sha256, "text_matches": False,
                        "memory_unit_count": 0, "listed_memory_unit_count": 0,
                        "listed_unit_present": False, "searchable": False}
        try:
            document = self._request("GET", "/documents/" + urllib.parse.quote(document_id, safe=""))
        except HindsightError as error:
            if error.code == "http_error" and error.status == 404:
                return verification
            raise
        if (document.get("id") != document_id or document.get("bank_id") != self.bank_id
                or not _nonnegative_int(document.get("memory_unit_count"))
                or "original_text" not in document
                or (document["original_text"] is not None and not isinstance(document["original_text"], str))):
            raise HindsightError("invalid_document_response")
        verification.update(exists=True, memory_unit_count=document["memory_unit_count"])
        if document["original_text"] is not None:
            try:
                digest = hashlib.sha256(document["original_text"].encode("utf-8")).hexdigest()
            except UnicodeError:
                raise HindsightError("invalid_document_response") from None
            verification.update(text_sha256=digest, text_matches=digest == expected_text_sha256)
        listed = self._list_document_memories(document_id)
        present = bool(listed["items"] and isinstance(listed["items"][0].get("id"), str)
                       and listed["items"][0]["id"]
                       and listed["items"][0].get("document_id", document_id) == document_id)
        verification.update(listed_memory_unit_count=listed["total"], listed_unit_present=present)
        verification["searchable"] = bool(verification["text_matches"] and
            document["memory_unit_count"] > 0 and listed["total"] > 0 and present)
        return verification

    def recall(self, query: str, tags: list[str], *, budget: str = "mid", max_tokens: int = 4096) -> dict:
        if not isinstance(query, str) or not query.strip():
            raise HindsightError("invalid_query")
        if not isinstance(budget, str) or budget not in {"low", "mid", "high"}:
            raise HindsightError("invalid_budget")
        if type(max_tokens) is not int or not 1 <= max_tokens <= 32_768:
            raise HindsightError("invalid_token_limit")
        tags = _tags(tags)
        result = self._request("POST", "/memories/recall", {
            "query": query, "tags": tags, "tags_match": "all_strict" if tags else "exact",
            "budget": budget, "max_tokens": max_tokens,
            "include": {"entities": None, "chunks": {"max_tokens": max_tokens},
                        "source_facts": {"max_tokens": max_tokens}}})
        if (not isinstance(result.get("results"), list)
                or any(not isinstance(item, dict) for item in result["results"])
                or result.get("chunks") is not None and not isinstance(result["chunks"], dict)
                or result.get("source_facts") is not None and not isinstance(result["source_facts"], dict)):
            raise HindsightError("invalid_recall_response")
        # Keep native document/chunk IDs and source facts; callers must treat text as data.
        return result

    def delete_document(self, document_id: str) -> dict:
        document_id = _identifier(document_id)
        result = self._request("DELETE", "/documents/" + urllib.parse.quote(document_id, safe=""))
        if (result.get("success") is not True or result.get("document_id") != document_id
                or not _nonnegative_int(result.get("memory_units_deleted"))):
            raise HindsightError("invalid_delete_response")
        return {"success": True, "document_id": document_id,
                "memory_units_deleted": result["memory_units_deleted"]}

    def verify_deleted_document(self, document_id: str) -> dict:
        """Observe document/unit absence; does not certify derived-data reconciliation."""
        document_id = _identifier(document_id)
        absent = False
        try:
            document = self._request("GET", "/documents/" + urllib.parse.quote(document_id, safe=""))
            if document.get("id") != document_id or document.get("bank_id") != self.bank_id:
                raise HindsightError("invalid_document_response")
        except HindsightError as error:
            if error.code == "http_error" and error.status == 404:
                absent = True
            else:
                raise
        listed = self._list_document_memories(document_id)
        units_absent = listed["total"] == 0 and listed["items"] == []
        return {"document_id": document_id, "document_absent": absent,
                "memory_units_absent": units_absent, "deleted": absent and units_absent}
