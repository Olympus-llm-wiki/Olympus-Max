"""Contract tests for evidence preservation, resumability and report lineage."""
import json
import hashlib
import shutil
from pathlib import Path

import pytest

from reel_analysis.antigravity import UnknownOutcome
from reel_analysis.card_sources import groups, normalize, output_root
from reel_analysis.common import ReelError, digest, file_hash, read_json, write_json
from reel_analysis.content_cards import plan, run, validate_draft
from reel_analysis.card_reports import export_report, check_report, search_cards


def raw_file(tmp_path, name="raw.json", text="Не меняйте 15 на 50.", silent=False, count=1):
    data = {
        "modality_status": {"speech": "not_detected" if silent else "detected", "screen_text": "detected", "visual": "detected"},
        "speech": [] if silent else [{"start_s": float(i), "end_s": float(i + 1), "text": text, "uncertainty": ""} for i in range(count)],
        "screen_text": [{"start_s": 0., "end_s": 1., "text": "Текст только на экране", "role": "heading", "uncertainty": ""}],
        "scenes": [{"start_s": 0., "end_s": float(max(2, count)), "description": "Сцена", "uncertainty": ""}],
        "visible_events": [], "limitations": ["Времена приблизительны"],
    }
    path = tmp_path / name
    write_json(path, {"schema_version": 1, "provider": "gemini-antigravity", "model": "fixture",
                      "input": {"sha256": "a" * 64, "duration_s": float(max(2, count))},
                      "data": data, "quality": "draft_unverified"})
    return path


def corpus(tmp_path, sources, selected=None):
    items = [{"id": f"item{i}", "path": str(path), "profile": profile, "title": f"Материал {i}"} for i, (path, profile) in enumerate(sources)]
    path = tmp_path / "corpus.json"
    value = {"schema_version": 1, "corpus_id": "test", "items": items}
    if selected is not None:
        value["selected_ids"] = selected
    write_json(path, value)
    return path


class Backend:
    def __init__(self):
        self.calls = 0

    def invoke(self, **kw):
        self.calls += 1
        assert len(kw["files"]) == 1 and kw["files"][0].suffix == ".json"
        data = read_json(kw["files"][0])
        kind = "concept" if data["profile"] == "lesson" else "hook"
        return {"data": {"points": [{"kind": kind, "title": "Проверенный термин", "text": "Черновой вывод с основанием",
                                     "evidence_ids": [data["events"][0]["id"]]}], "gaps": []},
                "usage": {"input_tokens": 20, "output_tokens": 10}}


def test_raw_profile_links_and_no_source_changes(tmp_path):
    source = raw_file(tmp_path)
    original = source.read_bytes()
    manifest = corpus(tmp_path, [(source, "lesson")])
    output = tmp_path / "cards"
    result = plan(manifest, output)
    assert result["selected_ids"] == ["item0"]
    assert result["new_calls"] == 0
    backend = Backend()
    first = run(manifest, output, backend=backend, max_parts=10)
    assert first["items"][0]["state"] == "draft"
    assert source.read_bytes() == original
    assert backend.calls == 1
    again = run(manifest, output, backend=backend, max_parts=10)
    assert again["new_calls"] == 0 and backend.calls == 1
    matches = search_cards(output, "ТЕРМИН")["matches"]
    assert matches[0]["point"]["kind"] == "concept"
    assert matches[0]["evidence"][0]["observation"]["text"] == "Не меняйте 15 на 50."


def test_silence_caption_does_not_become_transcript(tmp_path):
    source = raw_file(tmp_path, silent=True)
    manifest = corpus(tmp_path, [(source, "reel")])
    output = tmp_path / "cards"
    run(manifest, output, backend=Backend())
    target = tmp_path / "transcript"
    export_report(output, target, "transcript")
    card = read_json(target / "cards/item0.json")
    assert not card["source"]["transcript"]
    assert card["points"][0]["evidence_tracks"] == ["screen_text"]
    assert "Текст только на экране" not in (target / "report.md").read_text()


def test_wrong_kind_fake_reference_and_unknown_time():
    events = [{"id": "sp", "track": "speech", "start_s": None, "end_s": None}]
    draft = {"points": [{"kind": "concept", "title": "a", "text": "b", "evidence_ids": ["sp"]}], "gaps": []}
    assert validate_draft(draft, events, "lesson")["points"][0]["start_s"] is None
    with pytest.raises(ReelError, match="wrong_profile"):
        validate_draft(draft, events, "reel")
    draft["points"][0]["evidence_ids"] = ["invented"]
    with pytest.raises(ReelError, match="unknown_evidence"):
        validate_draft(draft, events, "lesson")
    with pytest.raises(ReelError, match="empty_without"):
        validate_draft({"points": [], "gaps": []}, events, "lesson")


def test_budget_continuation_full_inventory_and_profile_versions(tmp_path):
    one = raw_file(tmp_path, count=20, text="Длинный пример. " * 12)
    two = raw_file(tmp_path, "other.json")
    manifest = corpus(tmp_path, [(one, "lesson"), (two, "reel")], ["item0"])
    output = tmp_path / "cards"
    backend = Backend()
    first = run(manifest, output, part_bytes=1500, max_parts=1, backend=backend)
    assert first["hold"] == "part_budget_reached"
    assert first["items"][0]["state"] == "partial"
    assert first["items"][1]["state"] == "not_selected"
    old = read_json(output / "items/item0/current.json")["fingerprint"]
    rest = run(manifest, output, part_bytes=1500, max_parts=100, backend=backend)
    assert rest["items"][0]["state"] == "draft"
    assert backend.calls == rest["items"][0]["total_parts"]
    data = read_json(manifest)
    data["items"][0]["profile"] = "reel"
    write_json(manifest, data)
    run(manifest, output, part_bytes=1500, max_parts=100, backend=backend)
    new = read_json(output / "items/item0/current.json")["fingerprint"]
    assert old != new
    assert (output / "items/item0/versions" / old).is_dir()


def test_unknown_holds_even_new_selection_and_explicit_retry(tmp_path):
    class Unknown(Backend):
        def invoke(self, **kw):
            self.calls += 1
            write_json(kw["attempt"] / "submitted.json", {"test": True})
            raise UnknownOutcome("inference_outcome_unknown")

        def recover(self, attempt):
            raise UnknownOutcome("inference_outcome_unknown")
    manifest = corpus(tmp_path, [(raw_file(tmp_path), "lesson")])
    output = tmp_path / "cards"
    backend = Unknown()
    assert run(manifest, output, backend=backend)["hold"] == "inference_outcome_unknown"
    again = run(manifest, output, retry_failed=True, backend=backend)
    assert again["hold"] == "inference_outcome_unknown" and backend.calls == 1


def test_recovery_after_raw_before_accept_does_not_resubmit(tmp_path):
    class Interrupted(Backend):
        def invoke(self, **kw):
            result = super().invoke(**kw)
            write_json(kw["attempt"] / "envelope.json", result)
            write_json(kw["attempt"] / "submitted.json", {"test": True})
            raise UnknownOutcome("inference_outcome_unknown")
    manifest = corpus(tmp_path, [(raw_file(tmp_path), "lesson")])
    output = tmp_path / "cards"
    backend = Interrupted()
    run(manifest, output, backend=backend)
    recovered = run(manifest, output, backend=backend)
    assert recovered["items"][0]["state"] == "draft"
    assert recovered["new_calls"] == 0 and backend.calls == 1


def test_failed_result_requires_explicit_retry(tmp_path):
    class Invalid(Backend):
        def invoke(self, **kw):
            envelope = super().invoke(**kw)
            envelope["data"]["points"][0]["evidence_ids"] = ["fake"]
            return envelope
    manifest = corpus(tmp_path, [(raw_file(tmp_path), "lesson")])
    output = tmp_path / "cards"
    invalid = Invalid()
    assert run(manifest, output, backend=invalid)["hold"] == "content_unknown_evidence_id"
    good = Backend()
    assert run(manifest, output, backend=good)["hold"] == "retry_failed_required"
    assert good.calls == 0
    assert run(manifest, output, backend=good, retry_failed=True)["items"][0]["state"] == "draft"
    assert len(list(output.glob("items/*/versions/*/parts/*/attempt-*"))) == 2


def test_local_views_staleness_selection_integrity_and_portability(tmp_path):
    source = raw_file(tmp_path)
    other = raw_file(tmp_path, "other.json", text="Другой текст")
    manifest = corpus(tmp_path, [(source, "lesson"), (other, "reel")])
    output = tmp_path / "cards"
    backend = Backend()
    run(manifest, output, backend=backend, max_parts=20)
    for view in ("transcript", "notes", "editing", "comparison"):
        target = tmp_path / view
        export_report(output, target, view)
        assert check_report(target)["state"] == "current"
        assert (target / "sources/item0.md").exists()
    assert backend.calls == 2
    copied = tmp_path / "copied"
    shutil.copytree(tmp_path / "notes", copied)
    assert check_report(copied)["state"] == "current"
    data = read_json(manifest)
    data["selected_ids"] = ["item0"]
    write_json(manifest, data)
    assert check_report(copied)["state"] == "stale"
    (copied / "report.md").write_text("broken")
    assert check_report(copied)["state"] == "corrupt"
    old_text = (tmp_path / "notes/report.md").read_bytes()
    changed = read_json(source)
    changed["data"]["speech"][0]["text"] = "Изменённый источник"
    write_json(source, changed)
    checked = check_report(tmp_path / "notes")
    assert any(x["reason"] == "source_changed" for x in checked["issues"])
    run(manifest, output, backend=backend, max_parts=20)
    assert any(x["reason"] == "card_version_changed" for x in check_report(tmp_path / "notes")["issues"])
    assert (tmp_path / "notes/report.md").read_bytes() == old_text
    source.unlink()
    assert check_report(tmp_path / "notes")["state"] == "missing"


def media_job(tmp_path):
    job = tmp_path / "media-job"
    spans = [{"start": 0, "end": 3}, {"start": 2, "end": 5}, {"start": 4, "end": 6}]
    plan_data = {"source_sha256": "b" * 64, "media": {"duration": 6}, "spans": spans, "overlap_seconds": 1}
    write_json(job / "manifest.json", plan_data)
    parts = []
    for n, span in enumerate(spans):
        selected = None
        if n < 2:
            selected = f"part-{n:04d}/attempt-0001"
            part = job / selected
            legacy_digest = hashlib.sha256(json.dumps(plan_data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            write_json(part / "request.json", {"plan_digest": legacy_digest, "index": n, "span": span})
            write_json(part / "input.json", {"media": {"duration": span["end"] - span["start"], "audio": True}})
            write_json(part / "result.json", {"transcript": f"Часть {n}. Последние слова.", "speech_present": True, "audio_access": True,
                                             "speech_segments": [], "visual_events": [], "uncertainties": []})
            hashes = {name: file_hash(part / name) for name in ("request.json", "input.json", "result.json")}
            write_json(part / "receipt.json", {"quality": {"reusable": True}, "hashes": hashes})
        parts.append({"index": n, "span": span, "accepted_attempt": selected})
    write_json(job / "receipt.json", {"parts": parts})
    return job


def test_media_overlap_tail_and_missing_parts(tmp_path):
    job = media_job(tmp_path)
    item = {"id": "lesson", "path": str(job), "profile": "lesson", "title": "Урок"}
    source = normalize(item)
    assert [e["start_s"] for e in source["transcript"]] == [0, 2]
    assert all(e["text"].endswith("Последние слова.") for e in source["transcript"])
    assert source["coverage"] == "partial"
    assert source["gaps"] == [{"part": 2, "span": {"start": 4, "end": 6}, "reason": "source_part_unavailable"}]
    assert source["quality_notes"][-1]["overlap_reconciliation"] == "not_performed_parts_preserved"
    manifest = corpus(tmp_path, [(job, "lesson")])
    plan(manifest, tmp_path / "cards")
    export_report(tmp_path / "cards", tmp_path / "transcript", "transcript")
    text = (tmp_path / "transcript/report.md").read_text()
    assert "2/3" in text and "00:00.000–00:03.000" in text
    assert "content_pending" not in text
    (job / "part-0000/attempt-0001/result.json").write_text("{}")
    with pytest.raises(ReelError, match="parent_hash"):
        normalize(item)


def test_reel_bundle_and_parent_path_guard(tmp_path):
    bundle = tmp_path / "bundle"
    event = {"id": "sp1", "start_s": 0., "end_s": 1., "text": "Слова"}
    write_json(bundle / "analysis.json", {"schema_version": 1, "source": {"sha256": "c" * 64, "duration_s": 2.},
                                        "execution": {"state": "completed"}, "speech": {"segments": [event], "gaps": []},
                                        "visual": {"scenes": [], "text_events": [], "visual_events": [], "gaps": []},
                                        "quality": {"state": "partial", "gaps": ["не проверено"]}, "refinements": []})
    write_json(bundle / "manifest.json", {"files": {"analysis.json": file_hash(bundle / "analysis.json")}})
    item = {"id": "reel", "path": str(bundle), "profile": "reel", "title": "Рилс"}
    result = normalize(item)
    assert result["transcript"][0]["text"] == "Слова"
    assert result["events"][0]["id"] == "speech:sp1"
    write_json(bundle / "manifest.json", {"files": {"analysis.json": file_hash(bundle / "analysis.json"), "../outside.json": "x"}})
    with pytest.raises(ReelError, match="path_outside"):
        normalize(item)


def test_transcript_only_and_escaping(tmp_path):
    text = "<script>alert(1)</script>\n[link](https://example.com)"
    source = raw_file(tmp_path, text=text)
    manifest = corpus(tmp_path, [(source, "lesson")])
    output = tmp_path / "cards"
    plan(manifest, output)
    target = tmp_path / "report"
    export_report(output, target, "transcript")
    rendered = (target / "report.md").read_text()
    assert "    " + text.splitlines()[0] in rendered
    assert read_json(target / "cards/item0.json")["source"]["transcript"][0]["text"] == text


def test_single_oversized_event_rejected_without_truncation(tmp_path):
    path = raw_file(tmp_path, text="Я" * 3000)
    snapshot = normalize({"id": "x", "title": "x", "profile": "lesson", "path": str(path)})
    with pytest.raises(ReelError, match="exceeds_part_budget"):
        groups(snapshot, max_bytes=1000)


def test_output_accepts_os_alias_but_preserves_git_boundary(tmp_path):
    physical = tmp_path / "physical"
    physical.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(physical, target_is_directory=True)
    assert output_root(alias / "cards") == physical / "cards"
    (physical / ".git").mkdir()
    with pytest.raises(ReelError, match="outside_git"):
        output_root(alias / "other")
    with pytest.raises(ReelError, match="symlink"):
        output_root(alias)
