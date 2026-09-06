"""Synthetic compatibility and loss-detection tests; no personal session reads."""

from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from olympus.codex_capture import (
    CODEX_VERSION, CaptureRegistration, export_markdown,
    parse_rollout, read_registered_rollout,
)

FIXTURE = Path(__file__).parent / "fixtures/codex/completed-0.153.1.jsonl"


def keep(text):
    # Only used with synthetic data. Production must pass its actual guard.
    return text


def encoded(records):
    return b"".join(json.dumps(r, ensure_ascii=False).encode() + b"\n" for r in records)


class CodexCaptureTests(unittest.TestCase):
    def setUp(self):
        self.data = FIXTURE.read_bytes()
        self.records = [json.loads(line) for line in self.data.splitlines()]
        self.registration = CaptureRegistration(
            "synthetic-thread", FIXTURE.resolve(), "2026-09-05T10:00:00Z", CODEX_VERSION,
        )

    def parse(self, records=None, **kwargs):
        return parse_rollout(
            encoded(records) if records is not None else self.data,
            kwargs.pop("registration", self.registration),
            redactor=kwargs.pop("redactor", keep), **kwargs,
        )

    def codes(self, batch):
        return {gap.code for gap in batch.gaps}

    def next_turn(self, name="next-turn", offset=60, complete=True):
        records = json.loads(json.dumps(self.records[1:]))
        for record in records:
            record["timestamp"] = (datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00")) + timedelta(seconds=offset)).isoformat()
            payload = record["payload"]
            if "turn_id" in payload:
                payload["turn_id"] = name
            if "call_id" in payload:
                payload["call_id"] = name + "-call"
        return records if complete else records[:-1]

    def test_complete_preserves_roles_times_tool_outputs_and_correction_order(self):
        result = read_registered_rollout(self.registration, redactor=keep)
        self.assertTrue(result.complete, result.gaps)
        self.assertEqual([m.role for m in result.messages],
                         ["user", "assistant", "assistant", "tool", "user", "assistant"])
        output = result.messages[3]
        self.assertEqual(output.text, "color=red\nrevision=synthetic-42")
        self.assertEqual(output.timestamp, "2026-09-05T10:00:07Z")
        self.assertEqual(output.tool_name, "read_file")
        self.assertEqual(output.call_id, "synthetic-call")
        self.assertIn("прежний выбор отменён", result.messages[4].text)
        self.assertEqual(result.messages[-1].phase, "final_answer")

    def test_repeat_read_is_stable_without_collapsing_real_repeated_messages(self):
        first = self.parse()
        second = self.parse()
        self.assertEqual(first, second)
        self.assertEqual(len({m.event_id for m in first.messages}), len(first.messages))
        repeated = self.records.copy()
        repeated.insert(7, self.records[6])
        result = self.parse(repeated)
        same_text = [m for m in result.messages if m.text == "Проверь цвет: красный."]
        self.assertEqual(len(same_text), 2)
        self.assertNotEqual(same_text[0].event_id, same_text[1].event_id)

    def test_hidden_reasoning_and_instructions_never_reach_redactor_or_export(self):
        seen = []
        def recorder(text):
            seen.append(text)
            return text
        result = self.parse(redactor=recorder)
        exported = export_markdown(result)
        for marker in ("SYNTHETIC_PRIVATE_REASONING", "SYNTHETIC_ENCRYPTED",
                       "SYNTHETIC_INTERNAL_INSTRUCTIONS", "SYNTHETIC_STARTUP"):
            self.assertNotIn(marker, exported)
            self.assertFalse(any(marker in text for text in seen))
            self.assertNotIn(marker, repr(result))

    def test_redactor_required_and_rejection_never_contains_exception_or_input(self):
        with self.assertRaises(TypeError):
            parse_rollout(self.data, self.registration)
        def reject(text):
            raise RuntimeError("SYNTHETIC_SECRET:" + text)
        result = self.parse(redactor=reject)
        self.assertFalse(result.complete)
        self.assertFalse(result.messages)
        self.assertEqual(self.codes(result), {"redaction_rejected"})
        self.assertNotIn("SYNTHETIC_SECRET", repr(result) + export_markdown(result))

    def test_redaction_is_visible_and_repr_does_not_expose_message_text(self):
        result = self.parse(redactor=lambda text: text.replace("revision=synthetic-42", "[REDACTED]"))
        self.assertTrue(result.redacted)
        self.assertTrue(result.messages[3].redacted)
        self.assertNotIn("revision=synthetic-42", export_markdown(result))
        self.assertIn("Редактор изменил содержимое: да", export_markdown(result))
        self.assertNotIn("color=red", repr(result))

    def test_redactor_invalid_return_rejects_record(self):
        result = self.parse(redactor=lambda text: b"unsafe return type")
        self.assertFalse(result.messages)
        self.assertIn("redaction_rejected", self.codes(result))

    def test_unknown_registration_or_transcript_version_fails_closed(self):
        result = self.parse(registration=replace(self.registration, codex_version="0.154.0"))
        self.assertFalse(result.messages)
        self.assertEqual(self.codes(result), {"unsupported_codex_version"})
        self.records[0]["payload"]["cli_version"] = "0.144.6"
        result = self.parse(self.records)
        self.assertFalse(result.messages)
        self.assertEqual(self.codes(result), {"unsupported_transcript_version"})

    def test_thread_mismatch_cannot_export_any_text_even_after_matching_metadata(self):
        alien = {"timestamp": "2026-09-05T10:00:11Z", "type": "session_meta",
                 "payload": {"id": "other-thread", "cli_version": CODEX_VERSION}}
        result = self.parse(self.records + [alien])
        self.assertFalse(result.messages)
        self.assertEqual(self.codes(result), {"thread_id_mismatch"})

    def test_registration_boundary_does_not_import_earlier_messages(self):
        result = self.parse(registration=replace(self.registration, started_at="2026-09-05T10:00:08Z"))
        self.assertTrue(result.complete, result.gaps)
        self.assertEqual([m.role for m in result.messages], ["user", "assistant"])
        self.assertNotIn("color=red", export_markdown(result))
        self.assertGreater(result.excluded["before_registration"], 0)

    def test_partial_line_is_deferred_then_recovered_after_newline_without_replaying_ids(self):
        partial = self.data.rstrip(b"\n")
        first = parse_rollout(partial, self.registration, redactor=keep)
        second = self.parse()
        self.assertTrue(first.trailing_partial)
        self.assertIn("trailing_partial_line", self.codes(first))
        self.assertIn("turn_incomplete", self.codes(first))
        self.assertTrue(second.complete)
        self.assertEqual([m.event_id for m in first.messages], [m.event_id for m in second.messages])

    def test_malformed_complete_line_and_invalid_utf8_are_visible(self):
        for bad in (b"{broken}\n", b'\xff\n', b"[]\n"):
            with self.subTest(bad=bad):
                result = parse_rollout(self.data + bad, self.registration, redactor=keep)
                self.assertFalse(result.complete)
                self.assertTrue(self.codes(result) & {"invalid_json_line", "invalid_record"})

    def test_unknown_schema_and_role_are_gaps_and_unknown_text_is_not_exported(self):
        for payload in (
            {"type": "future_item", "text": "SYNTHETIC_UNSUPPORTED"},
            {"type": "message", "role": "future_role", "content": [{"type": "input_text", "text": "SYNTHETIC_UNSUPPORTED"}]},
            {"type": "message", "role": "assistant", "phase": "analysis", "content": [{"type": "output_text", "text": "SYNTHETIC_UNSUPPORTED"}]},
        ):
            with self.subTest(payload=payload):
                record = {"timestamp": "2026-09-05T10:00:11Z", "type": "response_item", "payload": payload}
                result = self.parse(self.records + [record])
                self.assertFalse(result.complete)
                self.assertNotIn("SYNTHETIC_UNSUPPORTED", export_markdown(result))

    def test_new_field_on_known_schema_is_not_silent_success(self):
        self.records[6]["payload"]["future_content"] = "SYNTHETIC_NOT_EXPORTED"
        result = self.parse(self.records)
        self.assertIn("unknown_response_field", self.codes(result))
        self.assertNotIn("SYNTHETIC_NOT_EXPORTED", export_markdown(result))

    def test_analysis_channel_is_excluded(self):
        self.records[8]["payload"]["channel"] = "analysis"
        result = self.parse(self.records)
        self.assertNotIn("Проверяю сохранённый файл", export_markdown(result))
        self.assertEqual(result.excluded["internal_channel"], 1)

    def test_missing_tool_output_and_missing_response_mirror_are_visible(self):
        result = self.parse([r for i, r in enumerate(self.records) if i not in (6, 10)])
        self.assertIn("tool_output_missing", self.codes(result))
        self.assertIn("display_message_without_response_item", self.codes(result))

    def test_compaction_and_media_are_not_misrepresented_as_full_originals(self):
        self.records[6]["payload"]["content"].append({"type": "input_image", "image_url": "data:synthetic"})
        self.records.insert(-1, {"timestamp": "2026-09-05T10:00:10Z", "type": "compacted",
                                 "payload": {"message": "SYNTHETIC_COMPACTED_CONTEXT"}})
        result = self.parse(self.records)
        self.assertIn("media_not_captured", self.codes(result))
        self.assertIn("compacted_history_requires_reconciliation", self.codes(result))
        self.assertNotIn("SYNTHETIC_COMPACTED_CONTEXT", export_markdown(result))
        self.assertIn("не является побайтовым оригиналом", export_markdown(result))

    def test_timestamp_and_missing_metadata_errors_fail_visibly(self):
        self.records[6]["timestamp"] = "no time"
        self.assertIn("invalid_timestamp", self.codes(self.parse(self.records)))
        result = self.parse(self.records[1:])
        self.assertFalse(result.messages)
        self.assertIn("missing_session_metadata", self.codes(result))

    def test_unknown_event_and_timestamp_regression_are_visible(self):
        self.records[-1]["payload"]["type"] = "new_control_event"
        self.records[-1]["timestamp"] = "2026-09-05T10:00:01Z"
        result = self.parse(self.records)
        self.assertTrue({"unknown_event_type", "timestamp_regression", "turn_incomplete"} <= self.codes(result))

    def test_structured_tool_output_preserves_text_and_omits_encrypted_content(self):
        self.records[10]["payload"]["output"] = [
            {"type": "input_text", "text": "exact output"},
            {"type": "encrypted_content", "encrypted_content": "SYNTHETIC_PRIVATE"},
        ]
        result = self.parse(self.records)
        self.assertEqual(result.messages[3].text, "exact output")
        self.assertNotIn("SYNTHETIC_PRIVATE", export_markdown(result))

    def test_custom_tool_call_and_result(self):
        call = self.records[9]["payload"]
        call["type"] = "custom_tool_call"
        call["input"] = call.pop("arguments")
        self.records[10]["payload"]["type"] = "custom_tool_call_output"
        result = self.parse(self.records)
        self.assertTrue(result.complete, result.gaps)
        self.assertEqual(result.messages[3].role, "tool")

    def test_tool_display_event_without_persisted_output_is_visible(self):
        self.records.insert(-1, {
            "timestamp": "2026-09-05T10:00:10Z", "type": "event_msg",
            "payload": {"type": "exec_command_end", "call_id": "missing-call",
                        "aggregated_output": "SYNTHETIC_NOT_PERSISTED"},
        })
        result = self.parse(self.records)
        self.assertIn("tool_event_without_response_output", self.codes(result))
        self.assertNotIn("SYNTHETIC_NOT_PERSISTED", export_markdown(result))

    def test_invalid_discriminator_shapes_report_gaps_without_crashing(self):
        for record_type in ("response_item", "event_msg"):
            for invalid_type in ([], {}, None, 7):
                record = {"timestamp": "2026-09-05T10:00:11Z", "type": record_type,
                          "payload": {"type": invalid_type}}
                result = self.parse(self.records + [record])
                self.assertFalse(result.complete)

    def test_exact_path_only_missing_symlink_and_size_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            missing = replace(self.registration, transcript_path=directory / "missing.jsonl")
            self.assertEqual(self.codes(read_registered_rollout(missing, redactor=keep)), {"transcript_unavailable"})
            link = directory / "link.jsonl"
            link.symlink_to(FIXTURE.resolve())
            self.assertEqual(self.codes(read_registered_rollout(replace(self.registration, transcript_path=link), redactor=keep)), {"transcript_symlink"})
            with patch("olympus.codex_capture.MAX_ROLLOUT_BYTES", 1):
                self.assertEqual(self.codes(read_registered_rollout(self.registration, redactor=keep)), {"transcript_size_limit"})
            # A FIFO cannot cause the read-only adapter to hang.
            import os
            fifo = directory / "fifo"
            os.mkfifo(fifo)
            self.assertEqual(self.codes(read_registered_rollout(replace(self.registration, transcript_path=fifo), redactor=keep)), {"transcript_not_regular"})

    def test_registration_requires_absolute_path_and_timezone(self):
        with self.assertRaisesRegex(ValueError, "absolute"):
            CaptureRegistration("synthetic-thread", "relative.jsonl", "2026-09-05T10:00:00Z")
        with self.assertRaisesRegex(ValueError, "timezone"):
            replace(self.registration, started_at="2026-09-05T10:00:00")

    def test_observed_desktop_envelope_metadata_and_agent_messages(self):
        data = (FIXTURE.parent / "desktop-0.153.1.jsonl").read_bytes()
        seen = []
        def redactor(text):
            seen.append(text)
            return text
        result = parse_rollout(data, self.registration, redactor=redactor)
        self.assertTrue(result.complete, result.gaps)
        self.assertEqual(result.completed_turn_ids, ("synthetic-turn",))
        self.assertEqual(len(result.messages), 3)
        agent = result.messages[1]
        self.assertEqual(agent.kind, "agent_message")
        self.assertEqual((agent.author, agent.recipient), ("synthetic-child", "synthetic-parent"))
        self.assertIsNotNone(agent.created_at)
        self.assertEqual(result.excluded["private_or_usage_metadata"], 3)
        for forbidden in ("SYNTHETIC_PRIVATE_WORLD_STATE", "SYNTHETIC_ENCRYPTED_AGENT_CONTEXT"):
            self.assertFalse(any(forbidden in text for text in seen))
            self.assertNotIn(forbidden, export_markdown(result))

    def test_unknown_content_kind_or_extra_metadata_remains_fail_closed(self):
        for metadata in (
            {"turn_id": "synthetic-turn", "content_item_kinds": ["future.hidden"]},
            {"turn_id": "synthetic-turn", "create_time": "bad"},
            {"turn_id": "synthetic-turn", "unknown_field": True},
        ):
            self.records[6]["payload"]["internal_chat_message_metadata_passthrough"] = metadata
            result = self.parse(self.records)
            self.assertFalse(result.complete)
            self.assertNotIn("Проверь цвет: красный.", [m.text for m in result.messages])

    def test_internal_roles_are_excluded_before_metadata_validation(self):
        for role in ("system", "developer"):
            for metadata in (
                {"content_item_kinds": ["multi_agent.mode_instructions"]},
                {"content_item_kinds": "future-internal-shape"},
                {"unknown_field": "SYNTHETIC_PRIVATE_METADATA"},
            ):
                with self.subTest(role=role, metadata=metadata):
                    seen = []
                    def recorder(text):
                        seen.append(text)
                        return text
                    record = {
                        "timestamp": "2026-09-05T10:00:11Z", "type": "response_item",
                        "payload": {
                            "type": "message", "role": role,
                            "content": [{"type": "input_text", "text": "SYNTHETIC_PRIVATE_INSTRUCTIONS"}],
                            "internal_chat_message_metadata_passthrough": metadata,
                        },
                    }
                    result = self.parse(self.records + [record] + self.next_turn(), redactor=recorder)
                    self.assertTrue(result.complete, result.gaps)
                    self.assertEqual(set(result.completed_turn_ids), {"synthetic-turn", "next-turn"})
                    self.assertEqual(len(result.messages), 12)
                    for marker in ("SYNTHETIC_PRIVATE_INSTRUCTIONS", "SYNTHETIC_PRIVATE_METADATA"):
                        self.assertFalse(any(marker in text for text in seen))
                        self.assertNotIn(marker, export_markdown(result))

    def test_known_platform_insertions_preserve_mixed_user_text(self):
        for kinds in (["agents_md.instructions"], ["plugins.recommendations"],
                      ["agents_md.instructions", "user.text", "plugins.recommendations"]):
            with self.subTest(kinds=kinds):
                seen = []
                record = {"timestamp": "2026-09-05T10:00:11Z", "type": "response_item", "payload": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "VISIBLE-USER-421" if kind == "user.text" else "PRIVATE-PLATFORM-773"} for kind in kinds],
                    "internal_chat_message_metadata_passthrough": {"turn_id": "synthetic-turn", "content_item_kinds": kinds}}}
                result = self.parse(self.records + [record] + self.next_turn(), redactor=lambda text: seen.append(text) or text)
                self.assertTrue(result.complete, result.gaps)
                self.assertNotIn("PRIVATE-PLATFORM-773", export_markdown(result))
                self.assertFalse(any("PRIVATE-PLATFORM-773" in item for item in seen))
                if "user.text" in kinds:
                    self.assertIn("VISIBLE-USER-421", export_markdown(result))
                self.assertEqual(set(result.completed_turn_ids), {"synthetic-turn", "next-turn"})

    def test_non_string_content_kind_remains_a_gap(self):
        for kind in ({"unexpected": "object"}, ["user.text"], None):
            record = {"timestamp": "2026-09-05T10:00:11Z", "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "UNSUPPORTED-823"}],
                "internal_chat_message_metadata_passthrough": {"content_item_kinds": [kind]}}}
            result = self.parse(self.records + [record])
            self.assertIn("unknown_content_item_kind", self.codes(result))
            self.assertNotIn("UNSUPPORTED-823", export_markdown(result))

    def test_unknown_content_kinds_on_captured_roles_still_block_all_turns(self):
        for role in ("user", "assistant", "tool"):
            with self.subTest(role=role):
                record = {
                    "timestamp": "2026-09-05T10:00:11Z", "type": "response_item",
                    "payload": {
                        "type": "message", "role": role,
                        "content": [{"type": "input_text", "text": "SYNTHETIC_UNSUPPORTED_CONTENT"}],
                        "internal_chat_message_metadata_passthrough": {
                            "turn_id": "synthetic-turn",
                            "content_item_kinds": ["multi_agent.mode_instructions"],
                        },
                    },
                }
                result = self.parse(self.records + [record] + self.next_turn())
                self.assertEqual(result.completed_turn_ids, ())
                self.assertEqual(set(result.closed_turn_ids), {"synthetic-turn", "next-turn"})
                self.assertTrue(any(g.code == "unknown_content_item_kind" and g.turn_id is None for g in result.gaps))
                self.assertNotIn("SYNTHETIC_UNSUPPORTED_CONTENT", export_markdown(result))

    def test_open_turn_missing_output_does_not_block_prior_completed_turn(self):
        active = self.next_turn(complete=False)
        active = [r for r in active if r["payload"].get("type") != "function_call_output"]
        result = self.parse(self.records + active)
        self.assertFalse(result.complete)
        self.assertEqual(result.completed_turn_ids, ("synthetic-turn",))
        self.assertEqual(result.closed_turn_ids, ("synthetic-turn",))
        self.assertTrue({"tool_output_missing", "turn_incomplete"} <= self.codes(result))
        self.assertTrue(all(g.turn_id == "next-turn" for g in result.gaps))

    def test_bad_closed_turn_does_not_block_later_clean_turn(self):
        broken = [r for r in self.records if r["payload"].get("type") != "function_call_output"]
        result = self.parse(broken + self.next_turn())
        self.assertEqual(result.completed_turn_ids, ("next-turn",))
        self.assertEqual(set(result.closed_turn_ids), {"synthetic-turn", "next-turn"})
        self.assertEqual(next(g for g in result.gaps if g.code == "tool_output_missing").turn_id, "synthetic-turn")

    def test_aborted_or_abandoned_turn_keeps_local_events_without_blocking_later_turn(self):
        for aborted in (True, False):
            with self.subTest(aborted=aborted):
                first = json.loads(json.dumps(self.records))
                if aborted:
                    first[-1]["payload"]["type"] = "turn_aborted"
                else:
                    first = first[:-1]
                result = self.parse(first + self.next_turn())
                self.assertEqual(result.completed_turn_ids, ("next-turn",))
                self.assertTrue(any(m.turn_id == "synthetic-turn" for m in result.messages))
                self.assertIn("turn_aborted" if aborted else "turn_incomplete", self.codes(result))

    def test_global_unknown_schema_blocks_all_closed_turns(self):
        records = self.records + self.next_turn()
        records[-2]["payload"]["future_field"] = "unknown contract"
        # Put the unknown field on a response item, not ignored control metadata.
        next(r for r in reversed(records) if r["type"] == "response_item")["payload"]["future_field"] = "unknown contract"
        result = self.parse(records)
        self.assertEqual(result.completed_turn_ids, ())
        self.assertEqual(len(result.closed_turn_ids), 2)
        self.assertTrue(any(g.code == "unknown_response_field" and g.turn_id is None for g in result.gaps))

    def test_partial_line_in_open_turn_does_not_block_prior_complete_turn(self):
        raw = encoded(self.records + self.next_turn(complete=False)) + b'{"timestamp":'
        result = parse_rollout(raw, self.registration, redactor=keep)
        self.assertEqual(result.completed_turn_ids, ("synthetic-turn",))
        self.assertTrue(result.trailing_partial)
        self.assertEqual(next(g for g in result.gaps if g.code == "trailing_partial_line").turn_id, "next-turn")

    def test_identical_display_messages_cannot_cover_a_missing_message_in_other_turn(self):
        first = [r for i, r in enumerate(self.records) if i != 6]
        result = self.parse(first + self.next_turn())
        self.assertEqual(result.completed_turn_ids, ("next-turn",))
        self.assertEqual(next(g for g in result.gaps if g.code == "display_message_without_response_item").turn_id, "synthetic-turn")


if __name__ == "__main__":
    unittest.main()
