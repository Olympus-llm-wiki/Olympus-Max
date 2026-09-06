"""Bounded, version-pinned reader of one explicitly registered Codex rollout.

This is an adapter for an internal persisted format, not a public Codex API.
It never discovers sessions, starts Codex, writes files, or delivers to memory.
Only sanitized user/assistant/tool text is returned; diagnostics contain codes
and record numbers, never raw input or exception messages.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import stat
from typing import Callable, Literal

CODEX_VERSION = "0.153.1"
MAX_ROLLOUT_BYTES = 128 * 1024 * 1024
Redactor = Callable[[str], str]
_CURRENT_TURN = object()


@dataclass(frozen=True)
class CaptureRegistration:
    thread_id: str
    transcript_path: Path | str
    started_at: str
    codex_version: str = CODEX_VERSION

    def __post_init__(self) -> None:
        if not self.thread_id or any(c in self.thread_id for c in "\r\n\x00"):
            raise ValueError("invalid_thread_id")
        if not Path(self.transcript_path).is_absolute():
            raise ValueError("transcript_path_must_be_absolute")
        _time(self.started_at)


@dataclass(frozen=True)
class CoverageGap:
    code: str
    record_number: int | None = None
    turn_id: str | None = None


@dataclass(frozen=True)
class CapturedMessage:
    event_id: str
    role: Literal["user", "assistant", "tool"]
    timestamp: str
    turn_id: str | None
    text: str = field(repr=False)
    kind: str = "message"
    tool_name: str | None = None
    call_id: str | None = None
    phase: str | None = None
    redacted: bool = False
    author: str | None = None
    recipient: str | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class CaptureBatch:
    thread_id: str
    messages: tuple[CapturedMessage, ...] = ()
    gaps: tuple[CoverageGap, ...] = ()
    excluded: dict[str, int] = field(default_factory=dict)
    source_sha256: str | None = None
    bytes_read: int = 0
    trailing_partial: bool = False
    adapter_version: str = CODEX_VERSION
    completed_turn_ids: tuple[str, ...] = ()
    closed_turn_ids: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """Coverage of the registered text scope, not delivery or source capture."""
        return not self.gaps and not self.trailing_partial

    @property
    def redacted(self) -> bool:
        return any(message.redacted for message in self.messages)


def _time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid_timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid_timestamp") from None
    if result.tzinfo is None:
        raise ValueError("timestamp_requires_timezone")
    return result.astimezone(timezone.utc)


def _canonical_time(value: str) -> str:
    return _time(value).isoformat().replace("+00:00", "Z")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _startup(text: str) -> bool:
    return text.lstrip().startswith(("# AGENTS.md instructions for ", "<environment_context>"))


def read_registered_rollout(
    registration: CaptureRegistration, *, redactor: Redactor
) -> CaptureBatch:
    """Read only the exact registered absolute file, including when Codex is off.

    ``redactor`` is mandatory. It must return safe text or raise to reject it.
    The adapter never logs input or redactor exceptions. A final line without a
    newline is deferred even if its JSON currently parses: the writer may still
    be appending it. Files larger than the limit are not silently truncated.
    """
    if not callable(redactor):
        raise TypeError("redactor_required")
    if registration.codex_version != CODEX_VERSION:
        return _failure(registration, "unsupported_codex_version")
    path = Path(registration.transcript_path)
    # No directory walks, globbing, thread DB reads, or resolution by basename.
    try:
        if path.is_symlink():
            return _failure(registration, "transcript_symlink")
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                return _failure(registration, "transcript_not_regular")
            if before.st_size > MAX_ROLLOUT_BYTES:
                return _failure(registration, "transcript_size_limit")
            data = stream.read(MAX_ROLLOUT_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(data) > MAX_ROLLOUT_BYTES:
            return _failure(registration, "transcript_size_limit")
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return _failure(registration, "transcript_changed_during_read")
    except OSError:
        return _failure(registration, "transcript_unavailable")
    return parse_rollout(data, registration, redactor=redactor)


def _failure(registration: CaptureRegistration, code: str) -> CaptureBatch:
    return CaptureBatch(thread_id=registration.thread_id, gaps=(CoverageGap(code),))


# Known duplicate display events and non-content lifecycle/telemetry. Their
# payloads are never exported. Unknown event types are gaps, not ignored success.
_IGNORED_EVENTS = frozenset({
    "token_count", "agent_reasoning", "agent_reasoning_delta",
    "agent_reasoning_raw_content", "agent_reasoning_raw_content_delta",
    "agent_reasoning_section_break", "agent_message_delta", "user_message_delta",
    "item_started", "item_completed", "turn_diff", "plan_update",
    "exec_command_begin", "exec_command_end", "exec_command_output_delta",
    "mcp_tool_call_begin", "mcp_tool_call_end", "web_search_begin", "web_search_end",
    "patch_apply_begin", "patch_apply_end", "context_compacted",
    "session_configured", "warning", "error", "shutdown_complete",
    "background_event", "view_image_tool_call", "stream_error",
    "terminal_interaction", "skills_update_available", "mcp_startup_update",
    "mcp_startup_complete", "hook_started", "hook_completed",
    "thread_settings_applied",
})

_RESPONSE_FIELDS = {
    "message": {"type", "id", "role", "content", "phase", "channel",
                "internal_chat_message_metadata_passthrough"},
    "function_call": {"type", "id", "name", "namespace", "arguments", "call_id",
                      "encrypted_function_args", "internal_chat_message_metadata_passthrough"},
    "custom_tool_call": {"type", "id", "name", "namespace", "input", "call_id", "status",
                         "internal_chat_message_metadata_passthrough"},
    "function_call_output": {"type", "id", "name", "namespace", "output", "call_id",
                             "internal_chat_message_metadata_passthrough"},
    "custom_tool_call_output": {"type", "id", "name", "output", "call_id",
                                "internal_chat_message_metadata_passthrough"},
    "agent_message": {"type", "id", "author", "recipient", "content",
                      "internal_chat_message_metadata_passthrough"},
}


def parse_rollout(
    data: bytes, registration: CaptureRegistration, *, redactor: Redactor
) -> CaptureBatch:
    """Pure parser; useful for synthetic tests and an already bounded snapshot."""
    if not callable(redactor):
        raise TypeError("redactor_required")
    if not isinstance(data, bytes):
        raise TypeError("rollout_bytes_required")
    if registration.codex_version != CODEX_VERSION:
        return _failure(registration, "unsupported_codex_version")
    if len(data) > MAX_ROLLOUT_BYTES:
        return _failure(registration, "transcript_size_limit")

    start = _time(registration.started_at)
    messages: list[CapturedMessage] = []
    gaps: list[CoverageGap] = []
    excluded: Counter[str] = Counter()
    mirror_events: Counter[tuple[str | None, str, str]] = Counter()
    response_messages: Counter[tuple[str | None, str, str]] = Counter()
    pending_calls: dict[str, tuple[str, str | None, int]] = {}
    tool_event_ids: dict[str, str | None] = {}
    tool_output_ids: set[str] = set()
    open_turns: set[str] = set()
    observed_turns: set[str] = set()
    closed_turns: set[str] = set()
    current_turn: str | None = None
    record_turn: str | None = None
    blocked_turns: set[str] = set()
    global_gap = False
    metadata_seen = False
    last_timestamp: datetime | None = None
    partial = bool(data) and not data.endswith(b"\n")
    lines = data.splitlines(keepends=True)
    if partial:
        lines = lines[:-1]

    def gap(code: str, line: int | None = None, *, turn_id: str | None | object = _CURRENT_TURN) -> None:
        nonlocal global_gap
        global_code = code.startswith(("unknown_", "unsupported_")) or code in {
            "invalid_json_line", "invalid_record", "invalid_record_ordinal",
            "duplicate_session_metadata", "record_before_session_metadata",
            "missing_session_metadata", "invalid_timestamp", "timestamp_regression",
            "missing_turn_id", "compacted_history_requires_reconciliation",
        }
        affected = None if global_code else (record_turn if turn_id is _CURRENT_TURN else turn_id)
        safe_turn = None
        if not isinstance(affected, str) or not affected:
            global_gap = True
        else:
            blocked_turns.add(affected)
            try:
                safe_turn = redactor(affected)
                if not isinstance(safe_turn, str):
                    raise ValueError
            except Exception:
                global_gap = True
                safe_turn = None
        gaps.append(CoverageGap(code, line, safe_turn))

    def content_text(content: object, line: int) -> str | None:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            gap("invalid_content", line)
            return None
        texts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                gap("invalid_content_block", line)
                continue
            kind = block.get("type")
            if kind in ("input_text", "output_text"):
                if set(block) - {"type", "text"}:
                    gap("unknown_text_block_field", line)
                if isinstance(block.get("text"), str):
                    texts.append(block["text"])
                else:
                    gap("invalid_text_block", line)
            elif kind == "encrypted_content":
                excluded["encrypted_content"] += 1
            elif kind in ("input_image", "input_audio"):
                gap("media_not_captured", line)
            else:
                gap("unknown_content_type", line)
        return "\n".join(texts)

    def emit(
        role: Literal["user", "assistant", "tool"], text: str | None,
        timestamp: str, line: int, *, kind: str = "message",
        tool_name: str | None = None, call_id: str | None = None,
        phase: str | None = None, turn_id: str | None = None,
        author: str | None = None, recipient: str | None = None,
        created_at: str | None = None,
    ) -> None:
        if text is None:
            return
        if role == "user" and _startup(text):
            excluded["startup_instructions"] += 1
            return
        # Identifiers also came from an untrusted file. Sanitize them before
        # returning or interpolating into Markdown; never put them in diagnostics.
        values = (text, tool_name, call_id, turn_id, author, recipient)
        try:
            safe = tuple(redactor(v) if v is not None else None for v in values)
            if any(v is not None and not isinstance(v, str) for v in safe):
                raise ValueError("invalid_redactor_result")
        except Exception:
            gap("redaction_rejected", line)
            return
        if turn_id is None:
            gap("missing_turn_id", line)
        else:
            observed_turns.add(turn_id)
        changed = safe != values
        safe_text, safe_name, safe_call, safe_turn, safe_author, safe_recipient = safe
        identity = json.dumps([registration.thread_id, line, role, timestamp,
                               kind, safe_text, safe_name, safe_call, safe_turn,
                               safe_author, safe_recipient, created_at],
                              ensure_ascii=False, separators=(",", ":"))
        messages.append(CapturedMessage(
            event_id="codex:" + _digest(identity), role=role,
            timestamp=timestamp, turn_id=safe_turn, text=safe_text or "",
            kind=kind, tool_name=safe_name, call_id=safe_call,
            phase=phase, redacted=changed,
            author=safe_author, recipient=safe_recipient, created_at=created_at,
        ))

    for line, raw in enumerate(lines, 1):
        record_turn = current_turn
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            gap("invalid_json_line", line)
            continue
        if not isinstance(record, dict):
            gap("invalid_record", line)
            continue
        record_type = record.get("type")
        if record_type in ("world_state", "token_usage_record", "inter_agent_communication_metadata"):
            # Version 0.153.1 Desktop persistence metadata, not user evidence.
            # Deliberately do not access their payload, especially world_state.
            excluded["private_or_usage_metadata"] += 1
            continue
        if not isinstance(record.get("payload"), dict):
            gap("invalid_record", line)
            continue
        payload = record["payload"]
        if record_type == "session_meta":
            if set(record) - {"timestamp", "type", "payload", "ordinal"}:
                gap("unknown_record_field", line)
            if payload.get("id") != registration.thread_id:
                return _failure(registration, "thread_id_mismatch")
            if payload.get("cli_version") != CODEX_VERSION:
                return _failure(registration, "unsupported_transcript_version")
            if metadata_seen:
                gap("duplicate_session_metadata", line)
                continue
            metadata_seen = True
            excluded["session_metadata"] += 1
            continue
        if not metadata_seen:
            gap("record_before_session_metadata", line)
            continue
        try:
            observed = _time(record.get("timestamp"))
            timestamp = _canonical_time(record["timestamp"])
        except (TypeError, ValueError):
            gap("invalid_timestamp", line)
            continue
        if last_timestamp is not None and observed < last_timestamp:
            gap("timestamp_regression", line)
        last_timestamp = observed

        if observed >= start:
            if set(record) - {"timestamp", "type", "payload", "ordinal"}:
                gap("unknown_record_field", line)
            if "ordinal" in record and (not isinstance(record["ordinal"], int) or isinstance(record["ordinal"], bool)):
                gap("invalid_record_ordinal", line)

        if record_type == "turn_context":
            value = payload.get("turn_id")
            current_turn = value if isinstance(value, str) and value else None
            excluded["turn_context"] += 1
            continue
        if observed < start:
            excluded["before_registration"] += 1
            continue

        if record_type == "event_msg":
            event_type = payload.get("type")
            if not isinstance(event_type, str):
                gap("unknown_event_type", line)
            elif event_type == "task_started":
                value = payload.get("turn_id")
                if isinstance(value, str) and value:
                    current_turn = value
                    record_turn = value
                    open_turns.add(value)
                else:
                    gap("missing_turn_id", line)
            elif event_type in ("task_complete", "turn_aborted"):
                value = payload.get("turn_id") or current_turn
                if isinstance(value, str):
                    open_turns.discard(value)
                    closed_turns.add(value)
                    if event_type == "turn_aborted":
                        gap("turn_aborted", line, turn_id=value)
                    if current_turn == value:
                        current_turn = None
                else:
                    gap("missing_turn_id", line)
            elif event_type in ("user_message", "agent_message"):
                text = payload.get("message")
                if isinstance(text, str):
                    role = "user" if event_type == "user_message" else "assistant"
                    if not (role == "user" and _startup(text)):
                        mirror_events[(record_turn, role, _digest(text))] += 1
                else:
                    gap("invalid_display_message", line)
            elif event_type in ("exec_command_end", "mcp_tool_call_end"):
                call_id = payload.get("call_id")
                if isinstance(call_id, str) and call_id:
                    tool_event_ids[call_id] = record_turn
                else:
                    gap("tool_event_without_call_id", line)
            elif event_type not in _IGNORED_EVENTS:
                gap("unknown_event_type", line)
            excluded["display_or_control_event"] += 1
            continue

        if record_type == "compacted":
            # Never export compacted/replacement model context as user evidence.
            excluded["compacted_context"] += 1
            gap("compacted_history_requires_reconciliation", line)
            continue
        if record_type != "response_item":
            gap("unknown_record_type", line)
            continue

        item_type = payload.get("type")
        if not isinstance(item_type, str):
            gap("unknown_response_item", line)
            continue
        if item_type in ("reasoning", "compaction", "context_compaction", "compaction_trigger"):
            # Do not inspect summary, content, or encrypted payload fields.
            excluded["reasoning_or_model_context"] += 1
            continue
        if item_type == "message" and payload.get("role") in ("system", "developer"):
            # These roles are outside the capture scope, including their
            # metadata. New internal content kinds must not block public turns.
            excluded["instructions"] += 1
            continue
        if item_type in _RESPONSE_FIELDS and set(payload) - _RESPONSE_FIELDS[item_type]:
            gap("unknown_response_field", line)
        metadata = payload.get("internal_chat_message_metadata_passthrough")
        if metadata is not None and (not isinstance(metadata, dict) or set(metadata) - {"turn_id", "content_item_kinds", "create_time"}):
            gap("unknown_message_metadata", line)
            continue
        if isinstance(metadata, dict) and "content_item_kinds" in metadata:
            kinds = metadata["content_item_kinds"]
            controls = {"generic.turn_aborted", "environments.environment_context",
                        "agents_md.instructions", "plugins.recommendations"}
            if (not isinstance(kinds, list) or any(not isinstance(k, str) or k not in {"unknown", "user.text"} | controls for k in kinds)
                    or not isinstance(payload.get("content"), list) or len(kinds) != len(payload["content"])):
                gap("unknown_content_item_kind", line)
                continue
            public_content = [item for kind, item in zip(kinds, payload["content"]) if kind not in controls]
            excluded["platform_control_message"] += len(kinds) - len(public_content)
            if not public_content:
                continue
            payload = {**payload, "content": public_content}
        created_at = None
        if isinstance(metadata, dict) and "create_time" in metadata:
            value = metadata["create_time"]
            try:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError
                created_at = datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
            except (ValueError, OverflowError, OSError):
                gap("invalid_message_create_time", line)
                continue
        turn_id = metadata.get("turn_id") if isinstance(metadata, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            turn_id = current_turn
        record_turn = turn_id
        phase = payload.get("phase")
        if phase not in (None, "commentary", "final_answer"):
            gap("unknown_message_phase", line)
            continue
        if payload.get("channel") in ("analysis", "summary"):
            excluded["internal_channel"] += 1
            continue
        if payload.get("channel") not in (None, "commentary", "final"):
            gap("unknown_message_channel", line)
            continue
        if item_type == "message":
            role = payload.get("role")
            if role not in ("user", "assistant", "tool"):
                gap("unknown_message_role", line)
                continue
            text = content_text(payload.get("content"), line)
            if text is not None:
                response_messages[(turn_id, role, _digest(text))] += 1
            emit(role, text, timestamp, line, phase=phase, turn_id=turn_id, created_at=created_at)
        elif item_type == "agent_message":
            author, recipient = payload.get("author"), payload.get("recipient")
            if not all(isinstance(v, str) and v for v in (author, recipient)):
                gap("invalid_agent_message_route", line)
                continue
            emit("assistant", content_text(payload.get("content"), line), timestamp, line,
                 kind="agent_message", turn_id=turn_id, author=author, recipient=recipient,
                 created_at=created_at)
        elif item_type in ("function_call", "custom_tool_call"):
            name, call_id = payload.get("name"), payload.get("call_id")
            text = payload.get("arguments" if item_type == "function_call" else "input")
            if not all(isinstance(v, str) and v for v in (name, call_id)) or not isinstance(text, str):
                gap("invalid_tool_call", line)
                continue
            if call_id in pending_calls:
                gap("duplicate_tool_call_id", line)
            pending_calls[call_id] = (name, turn_id, line)
            emit("assistant", text, timestamp, line, kind="tool_call",
                 tool_name=name, call_id=call_id, turn_id=turn_id)
        elif item_type in ("function_call_output", "custom_tool_call_output"):
            call_id = payload.get("call_id")
            name = payload.get("name")
            if call_id is not None and not isinstance(call_id, str):
                gap("invalid_tool_call_id", line)
                continue
            if call_id:
                tool_output_ids.add(call_id)
            if call_id in pending_calls:
                name, call_turn, _ = pending_calls.pop(call_id)
                if call_turn != turn_id:
                    gap("tool_output_turn_mismatch", line, turn_id=call_turn)
                    gap("tool_output_turn_mismatch", line, turn_id=turn_id)
            elif name is None:
                gap("orphan_tool_output", line)
            if name is not None and not isinstance(name, str):
                gap("invalid_tool_name", line)
                continue
            emit("tool", content_text(payload.get("output"), line), timestamp, line,
                 kind="tool_output", tool_name=name, call_id=call_id, turn_id=turn_id)
        else:
            gap("unknown_response_item", line)

    if not metadata_seen:
        gap("missing_session_metadata")
    for _, turn_id, line in pending_calls.values():
        gap("tool_output_missing", line, turn_id=turn_id)
    for call_id in tool_event_ids.keys() - tool_output_ids:
        gap("tool_event_without_response_output", turn_id=tool_event_ids[call_id])
    for turn_id in open_turns | (observed_turns - closed_turns):
        gap("turn_incomplete", turn_id=turn_id)
    for turn_id, _, _ in mirror_events - response_messages:
        gap("display_message_without_response_item", turn_id=turn_id)
    if partial:
        record_turn = current_turn
        gap("trailing_partial_line", len(lines) + 1, turn_id=current_turn)
    safe_closed: dict[str, str] = {}
    for turn_id in sorted(closed_turns):
        try:
            safe_turn = redactor(turn_id)
            if not isinstance(safe_turn, str):
                raise ValueError
            safe_closed[turn_id] = safe_turn
        except Exception:
            gap("redaction_rejected", turn_id=turn_id)
    safe_completed = () if global_gap else tuple(
        safe_closed[turn_id] for turn_id in sorted(closed_turns - blocked_turns)
        if turn_id in safe_closed
    )
    return CaptureBatch(
        thread_id=registration.thread_id, messages=tuple(messages), gaps=tuple(gaps),
        excluded=dict(excluded), source_sha256=hashlib.sha256(data).hexdigest(),
        bytes_read=len(data), trailing_partial=partial,
        completed_turn_ids=safe_completed, closed_turn_ids=tuple(safe_closed.values()),
    )


def export_markdown(batch: CaptureBatch) -> str:
    """Render sanitized messages explicitly as a derived transcript.

    Do not print this document to a diagnostic log. The caller owns durable
    atomic capture, deduplication, and delivery receipts. Source documents
    mentioned by a tool still need their own full-source capture.
    """
    lines = [
        "# Разговор Codex",
        "",
        f"Адаптер: {batch.adapter_version}. Покрытие: {'полное' if batch.complete else 'есть пробелы'}.",
        "Это производная текстовая копия; она не является побайтовым оригиналом rollout.",
        f"Редактор изменил содержимое: {'да' if batch.redacted else 'нет'}.",
    ]
    if batch.gaps:
        lines.extend(["", "Пробелы:"])
        for item in batch.gaps:
            suffix = f" (строка {item.record_number})" if item.record_number else ""
            if item.turn_id:
                suffix += " (ход " + json.dumps(item.turn_id, ensure_ascii=False) + ")"
            lines.append(f"- {item.code}{suffix}")
    for message in batch.messages:
        lines.extend(["", f"## {message.role} · {message.timestamp}", "",
                      f"Событие: {message.event_id}; вид: {message.kind}."])
        if message.turn_id is not None:
            lines.append("Ход: " + json.dumps(message.turn_id, ensure_ascii=False))
        if message.tool_name is not None:
            lines.append("Инструмент: " + json.dumps(message.tool_name, ensure_ascii=False))
        if message.call_id is not None:
            lines.append("Вызов: " + json.dumps(message.call_id, ensure_ascii=False))
        if message.phase:
            lines.append("Фаза: " + message.phase)
        if message.author is not None:
            lines.append("Отправитель: " + json.dumps(message.author, ensure_ascii=False))
        if message.recipient is not None:
            lines.append("Получатель: " + json.dumps(message.recipient, ensure_ascii=False))
        if message.created_at is not None:
            lines.append("Время создания сообщения: " + message.created_at)
        if message.redacted:
            lines.append("Содержимое изменено редактором.")
        lines.extend(["", message.text])
    return "\n".join(lines) + "\n"
