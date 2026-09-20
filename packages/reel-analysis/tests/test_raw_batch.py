import json
from pathlib import Path

import pytest

from reel_analysis.antigravity import UnknownOutcome, parse_terminal_json
from reel_analysis.common import ReelError, file_hash, read_json, write_json
from reel_analysis.raw_batch import COMPACT_PROMPT, RAW_PROMPT, RAW_SCHEMA, batch_fingerprint, run_batch, validate_raw


def valid_data(duration=2):
    return {
        "modality_status": {"speech": "detected", "screen_text": "detected", "visual": "detected"},
        "speech": [{"start_s": 0, "end_s": duration, "text": "hello", "uncertainty": ""}],
        "screen_text": [{"start_s": 0, "end_s": duration, "text": "HELLO", "role": "heading", "uncertainty": ""}],
        "scenes": [{"start_s": 0, "end_s": duration, "description": "speaker", "uncertainty": ""}],
        "visible_events": [{"start_s": 0, "end_s": .5, "description": "cut", "uncertainty": ""}],
        "limitations": [],
    }


def manifest(tmp_path, count=2):
    items = []
    for number in range(count):
        source = tmp_path / f"{number}.mp4"
        source.write_bytes(("video" + str(number)).encode())
        items.append({"id": f"r{number}", "path": str(source), "sha256": file_hash(source), "duration_s": 2})
    path = tmp_path / "manifest.json"
    write_json(path, {"schema_version": 1, "corpus_id": "test", "items": items})
    return path, items


class FixtureBackend:
    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at

    def invoke(self, **kwargs):
        self.calls.append(kwargs["files"][0].name)
        if len(self.calls) == self.fail_at:
            raise UnknownOutcome("inference_outcome_unknown")
        return {"data": valid_data(), "usage": {"input_tokens": 10, "output_tokens": 20}}


def test_validate_rejects_boolean_and_out_of_range_interval():
    data = valid_data()
    data["speech"][0]["start_s"] = True
    with pytest.raises(ReelError, match="raw_interval_invalid"):
        validate_raw(data, 2)


def test_terminal_json_selects_required_object_from_reasoning_prose():
    schema_properties = {"ok": {"type": "boolean"}}
    text = 'thought schema {"ok":{"type":"boolean"}}\nresult {"ok":true}\nfinished'
    assert parse_terminal_json(text, ["ok"], schema_properties) == {"ok": True}
    with pytest.raises(ValueError, match="multiple_terminal"):
        parse_terminal_json('{"ok":true}\nthen {"ok":false}', ["ok"])


def test_compact_prompt_has_a_distinct_batch_fingerprint(tmp_path):
    source, _ = manifest(tmp_path, 1)
    value = read_json(source)
    assert batch_fingerprint(value, "gemini-3.8-flash-high", RAW_PROMPT) != batch_fingerprint(value, "gemini-3.8-flash-high", COMPACT_PROMPT)
    data = valid_data()
    data["scenes"][0]["end_s"] = 3
    with pytest.raises(ReelError, match="raw_interval_invalid"):
        validate_raw(data, 2)


def test_batch_runs_sequentially_and_reuses_results(tmp_path):
    source, items = manifest(tmp_path)
    backend = FixtureBackend()
    summary = run_batch(source, tmp_path / "out", backend=backend)
    assert summary["counts"] == {"ready": 2, "failed": 0, "unknown": 0, "pending": 0}
    assert summary["modality_coverage"]["speech"] == {"detected": 2, "not_detected": 0, "unavailable": 0}
    assert summary["incomplete_results"] == []
    assert backend.calls == ["0.mp4", "1.mp4"]
    assert summary["usage"]["input_tokens"] == 20
    again = FixtureBackend()
    run_batch(source, tmp_path / "out", backend=again)
    assert again.calls == []
    assert read_json(tmp_path / "out/runs/r0/result.json")["quality"] == "draft_unverified"


def test_unknown_stops_batch_and_requires_explicit_retry(tmp_path):
    source, _ = manifest(tmp_path, 3)
    backend = FixtureBackend(fail_at=2)
    with pytest.raises(ReelError, match="stopped_after_unknown"):
        run_batch(source, tmp_path / "out", backend=backend)
    summary = read_json(tmp_path / "out/summary.json")
    assert summary["counts"] == {"ready": 1, "failed": 0, "unknown": 1, "pending": 1}
    with pytest.raises(ReelError, match="raw_retry_required"):
        run_batch(source, tmp_path / "out", backend=FixtureBackend())
    resumed = FixtureBackend()
    summary = run_batch(source, tmp_path / "out", retry_failed=True, backend=resumed)
    assert summary["counts"]["ready"] == 3
    assert resumed.calls == ["1.mp4", "2.mp4"]


def test_continue_on_error_records_unprocessed_and_finishes_batch(tmp_path):
    source, _ = manifest(tmp_path, 3)
    backend = FixtureBackend(fail_at=2)
    summary = run_batch(source, tmp_path / "out", continue_on_error=True, backend=backend)
    assert summary["counts"] == {"ready": 2, "failed": 0, "unknown": 1, "pending": 0}
    assert summary["unprocessed"] == [{"id": "r1", "state": "unknown", "error": "inference_outcome_unknown"}]
    assert backend.calls == ["0.mp4", "1.mp4", "2.mp4"]
    again = FixtureBackend()
    summary = run_batch(source, tmp_path / "out", continue_on_error=True, backend=again)
    assert summary["counts"]["unknown"] == 1
    assert again.calls == []


def test_prior_result_is_imported_without_provider_call(tmp_path):
    source, items = manifest(tmp_path, 1)
    prior = tmp_path / "prior.json"
    write_json(prior, {
        "provider": "gemini-antigravity", "model": "gemini-3.8-flash-high",
        "input": items[0], "elapsed_s": 1, "usage": {}, "data": valid_data(),
    })
    value = read_json(source)
    value["items"][0]["prior_result"] = str(prior)
    write_json(source, value)
    backend = FixtureBackend()
    run_batch(source, tmp_path / "out", backend=backend)
    assert backend.calls == []
    result = read_json(tmp_path / "out/runs/r0/result.json")
    assert result["provenance"]["state"] == "reused_prior_result"
    assert result["config"]["prompt_sha256"]
    assert RAW_PROMPT and RAW_SCHEMA
