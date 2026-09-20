"""Read retained extractor outputs without reopening media or invoking a model."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .common import ReelError, canonical, digest, file_hash


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class SourceItem(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")
    path: str
    profile: Literal["lesson", "reel"]
    title: str = Field(min_length=1, max_length=500)


class Corpus(StrictModel):
    schema_version: Literal[1]
    corpus_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")
    items: list[SourceItem] = Field(min_length=1, max_length=1000)
    selected_ids: list[str] | None = None


def json_bytes(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ReelError("content_source_file_required")
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ReelError("content_json_too_large")
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def load_corpus(path):
    path = Path(path).expanduser().absolute()
    value, _ = json_bytes(path)
    result = Corpus.model_validate(value).model_dump()
    ids = [x["id"] for x in result["items"]]
    selected = result["selected_ids"] if result["selected_ids"] is not None else ids
    if len(set(ids)) != len(ids) or len(set(selected)) != len(selected) or not set(selected) <= set(ids):
        raise ReelError("content_invalid_selection")
    result["selected_ids"] = selected
    for item in result["items"]:
        p = Path(item["path"]).expanduser()
        item["path"] = str((path.parent / p if not p.is_absolute() else p).absolute())
    return result


def child(base, name):
    base = Path(base).absolute()
    part = Path(name)
    if part.is_absolute() or ".." in part.parts or not part.parts:
        raise ReelError("content_path_outside_source")
    target = base / part
    current = target
    while current != base.parent:
        if current.is_symlink():
            raise ReelError("content_symlink_not_supported")
        if current == base:
            break
        current = current.parent
    if base.resolve() not in target.resolve().parents:
        raise ReelError("content_path_outside_source")
    return target


def output_root(path):
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise ReelError("content_symlink_not_supported")
    # Standard OS aliases (macOS /tmp -> /private/tmp) are valid ancestors.
    path = path.resolve()
    if any((p / ".git").exists() for p in [path, *path.parents]):
        raise ReelError("content_output_must_be_outside_git")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


class Reader:
    def __init__(self):
        self.dependencies = {}

    def read(self, path, expected=None):
        path = Path(path).absolute()
        value, actual = json_bytes(path)
        if expected is not None and actual != expected:
            raise ReelError("content_parent_hash_mismatch")
        self.dependencies[str(path)] = actual
        return value

    def verify(self, base, hashes):
        if not isinstance(hashes, dict) or not hashes:
            raise ReelError("content_missing_parent_hashes")
        for name, expected in hashes.items():
            path = child(base, name)
            if not path.is_file() or file_hash(path) != expected:
                raise ReelError("content_parent_hash_mismatch")
            self.dependencies[str(path)] = expected


def interval(start, end, duration, offset=0):
    if start is None or end is None:
        if start is not None or end is not None:
            raise ReelError("content_invalid_interval")
        return None, None
    if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in (start, end)):
        raise ReelError("content_invalid_interval")
    if start < 0 or end < start or end > duration + .05:
        raise ReelError("content_interval_outside_source")
    return start + offset, end + offset


def add_event(out, event_id, track, value, duration, *, offset=0, variant="primary"):
    if not isinstance(value, dict):
        raise ReelError("content_invalid_event")
    start, end = interval(value.get("start_s"), value.get("end_s"), duration, offset)
    out["events"].append({
        "id": event_id, "track": track, "start_s": start, "end_s": end,
        "variant": variant, "observation": value,
    })


def add_transcript(out, text, start, end, label):
    if not isinstance(text, str):
        raise ReelError("content_invalid_transcript")
    if text:
        out["transcript"].append({"text": text, "start_s": start, "end_s": end, "label": label})


def reel_tracks(out, data, duration, prefix="", variant="primary"):
    speech = data.get("speech", {})
    for index, event in enumerate(speech.get("segments", [])):
        event_id = prefix + "speech:" + str(event.get("id", index))
        add_event(out, event_id, "speech", event, duration, variant=variant)
        if variant == "primary":
            add_transcript(out, event["text"], event.get("start_s"), event.get("end_s"), event_id)
    visual = data.get("visual", {})
    for key, track in (("scenes", "scene"), ("text_events", "screen_text"), ("visual_events", "visual")):
        for index, event in enumerate(visual.get(key, [])):
            add_event(out, prefix + track + ":" + str(event.get("id", index)), track, event, duration, variant=variant)
    out["gaps"].extend(speech.get("gaps", []))
    out["gaps"].extend(visual.get("gaps", []))


def load_reel(path, reader, out):
    manifest = reader.read(child(path, "manifest.json"))
    hashes = manifest.get("files", {})
    if "analysis.json" not in hashes:
        raise ReelError("content_missing_parent_hashes")
    reader.verify(path, hashes)
    data = reader.read(child(path, "analysis.json"), hashes["analysis.json"])
    if data.get("schema_version") != 1 or data.get("execution", {}).get("state") != "completed":
        raise ReelError("content_reel_not_complete")
    duration = data["source"]["duration_s"]
    out.update(format="reel", source_sha256=data["source"]["sha256"], duration_s=duration)
    if data.get("speech", {}).get("segments") and data.get("speech_basis") in ("verified_digital_silence", "no_audio_track"):
        raise ReelError("content_speech_modality_contradiction")
    reel_tracks(out, data, duration)
    for index, refinement in enumerate(data.get("refinements", [])):
        kind = refinement["track"]
        if kind in ("speech", "visual"):
            reel_tracks(out, {kind: refinement["observations"]}, duration, f"refine{index}:", "refinement")
    out["gaps"].extend(data.get("quality", {}).get("gaps", []))
    out["quality_notes"].append(data.get("quality", {}))
    out["quality_notes"].append({"speech_basis": data.get("speech_basis", "unknown")})


def load_raw(path, reader, out):
    from .raw_batch import validate_raw
    value = reader.read(path)
    if value.get("schema_version") != 1 or value.get("provider") != "gemini-antigravity":
        raise ReelError("content_unsupported_raw_result")
    duration = value["input"]["duration_s"]
    data = validate_raw(value["data"], duration)
    if data["speech"] and data["modality_status"]["speech"] != "detected":
        raise ReelError("content_speech_modality_contradiction")
    out.update(format="raw", source_sha256=value["input"]["sha256"], duration_s=duration)
    for key, track in (("speech", "speech"), ("screen_text", "screen_text"), ("scenes", "scene"), ("visible_events", "visual")):
        for index, event in enumerate(data[key]):
            event_id = f"{track}:{index:05d}"
            add_event(out, event_id, track, event, duration)
            if track == "speech":
                add_transcript(out, event["text"], event["start_s"], event["end_s"], event_id)
    out["gaps"].extend(data.get("limitations", []))
    out["quality_notes"].append({"quality": value.get("quality"), "modality_status": data["modality_status"]})
    for name, status in data["modality_status"].items():
        if status == "unavailable":
            out["gaps"].append({"track": name, "reason": "source_modality_unavailable"})


def load_media(path, reader, out):
    plan = reader.read(child(path, "manifest.json"))
    receipt = reader.read(child(path, "receipt.json"))
    spans, parts = plan["spans"], receipt["parts"]
    if len(parts) != len(spans) or not spans or plan.get("profile", "standard") != "standard":
        raise ReelError("content_unsupported_media_job")
    duration = plan["media"]["duration"]
    out.update(format="media", source_sha256=plan["source_sha256"], duration_s=duration)
    for index, span in enumerate(spans):
        part = parts[index]
        if part["index"] != index or part["span"] != span:
            raise ReelError("content_part_manifest_mismatch")
        selected = part.get("accepted_attempt")
        if not selected:
            out["gaps"].append({"part": index, "span": span, "reason": "source_part_unavailable"})
            continue
        attempt = child(path, selected)
        expected_parent = path / f"part-{index:04d}"
        if attempt.parent != expected_parent or not attempt.name.startswith("attempt-"):
            raise ReelError("content_part_manifest_mismatch")
        proof = reader.read(child(attempt, "receipt.json"))
        if proof.get("quality", {}).get("reusable") is not True:
            raise ReelError("content_parent_not_accepted")
        hashes = proof.get("hashes", {})
        if not {"result.json", "input.json", "request.json"} <= set(hashes):
            raise ReelError("content_missing_parent_hashes")
        reader.verify(attempt, hashes)
        request = reader.read(child(attempt, "request.json"), hashes["request.json"])
        # media.py predates the package's compact JSON canonicalization.
        plan_digest = hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if request != {"plan_digest": plan_digest, "index": index, "span": span}:
            raise ReelError("content_part_manifest_mismatch")
        data = reader.read(child(attempt, "result.json"), hashes["result.json"])
        media = reader.read(child(attempt, "input.json"), hashes["input.json"])["media"]
        part_duration = media["duration"]
        start, end = interval(span["start"], span["end"], duration)
        text = data.get("transcript", "")
        if text and (not media.get("audio") or media.get("digital_silence") or data.get("speech_present") is not True or data.get("audio_access") is not True):
            raise ReelError("content_speech_modality_contradiction")
        label = f"part-{index:04d}"
        add_transcript(out, text, start, end, label)
        # Full chunk text is retained even when model segment annotations omit words.
        if text:
            out["events"].append({
                "id": label + ":speech-full", "track": "speech", "start_s": start, "end_s": end,
                "variant": "primary", "observation": {"text": text, "timing": "chunk_bounds_not_speech_alignment"},
            })
        for n, event in enumerate(data.get("speech_segments", [])):
            add_event(out, f"{label}:speech:{n:05d}", "speech", event, part_duration, offset=start, variant="annotation")
        for n, event in enumerate(data.get("visual_events", [])):
            add_event(out, f"{label}:visual:{n:05d}", "visual", event, part_duration, offset=start)
            if event.get("visible_text"):
                add_event(out, f"{label}:screen_text:{n:05d}", "screen_text",
                          {"start_s": event.get("start_s"), "end_s": event.get("end_s"), "text": event["visible_text"]},
                          part_duration, offset=start)
        out["gaps"].extend(data.get("uncertainties", []))
        out["quality_notes"].append({"part": index, "quality": proof["quality"]})
    out["quality_notes"].append({"overlap_seconds": plan.get("overlap_seconds", 0), "overlap_reconciliation": "not_performed_parts_preserved"})


def normalize(item):
    path = Path(item["path"]).absolute()
    if path.is_symlink():
        raise ReelError("content_symlink_not_supported")
    reader = Reader()
    out = {"schema_version": 1, "id": item["id"], "title": item["title"], "events": [], "transcript": [], "gaps": [], "quality_notes": []}
    if path.is_dir() and (path / "analysis.json").exists():
        load_reel(path, reader, out)
    elif path.is_dir() and (path / "receipt.json").exists():
        load_media(path, reader, out)
    elif path.is_file():
        load_raw(path, reader, out)
    else:
        raise ReelError("content_source_not_found")
    if not isinstance(out["source_sha256"], str) or len(out["source_sha256"]) != 64:
        raise ReelError("content_invalid_source_hash")
    duration = out["duration_s"]
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise ReelError("content_invalid_duration")
    ids = [e["id"] for e in out["events"]]
    if len(ids) != len(set(ids)):
        raise ReelError("content_duplicate_event_id")
    out["dependencies"] = [{"path": p, "sha256": h} for p, h in sorted(reader.dependencies.items())]
    out["quality"] = "draft_unverified"
    out["coverage"] = "partial" if out["gaps"] else "unverified"
    out["snapshot_sha256"] = digest(out)
    return out


def dependencies_changed(snapshot):
    issues = []
    for dep in snapshot["dependencies"]:
        path = Path(dep["path"])
        if not path.is_file() or path.is_symlink():
            issues.append({"reason": "source_missing", "path": str(path)})
        elif file_hash(path) != dep["sha256"]:
            issues.append({"reason": "source_changed", "path": str(path)})
    return issues


def groups(snapshot, max_bytes=48000):
    if not 1000 <= max_bytes <= 100000:
        raise ReelError("content_invalid_part_budget")
    result, batch, size = [], [], 2
    for event in snapshot["events"]:
        weight = len(canonical(event).encode()) + 1
        if weight > max_bytes:
            raise ReelError("content_event_exceeds_part_budget:" + event["id"])
        if batch and size + weight > max_bytes:
            result.append(batch)
            batch, size = [], 2
        batch.append(event)
        size += weight
    if batch:
        result.append(batch)
    return result
