"""Bounded parallel review with immutable attempts and explicit reuse/retry."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import json
import re
import statistics
import threading
import time

from .antigravity import UnknownOutcome
from .common import ReelError, digest, file_hash, read_json, write_json
from .contracts import Profile
from .gemini_gateway import Zapro
from .raw_batch import batch_lock
from .review_contract import PROMPT, SCHEMA, validate_review
from .review_antigravity import ReviewAntigravity

SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def caption_mentions(text):
    handles = dict.fromkeys(match.rstrip(".") for match in re.findall(r"(?<![\w@])@([A-Za-z0-9._]{1,30})", text))
    return [{"handle": "@" + handle, "url": "https://www.instagram.com/" + handle + "/", "basis": "caption", "is_cta": False} for handle in handles if handle]


def configuration(backend="antigravity", thinking="low", timeout_s=180):
    if backend not in ("zapro", "antigravity") or thinking not in ("low", "medium", "high"):
        raise ReelError("review_configuration_invalid")
    if not 30 <= timeout_s <= 900:
        raise ReelError("review_timeout_invalid")
    return {"backend": backend, "thinking": thinking, "model": "gemini-3.8-flash" + ("-" + thinking if backend == "antigravity" else ""), "timeout_s": timeout_s, "prompt_sha256": digest(PROMPT), "schema_sha256": digest(SCHEMA), "contract": "commercial-review-v1"}


def load_inputs(manifest_path):
    manifest_path = Path(manifest_path).resolve()
    source = read_json(manifest_path)
    if not isinstance(source, dict) or source.get("schema_version") != 1 or not isinstance(source.get("corpus_id"), str) or not isinstance(source.get("items"), list) or not source["items"]:
        raise ReelError("review_manifest_invalid")
    items, ids = [], set()
    for original in source["items"]:
        if not isinstance(original, dict) or not isinstance(original.get("path"), str):
            raise ReelError("review_manifest_item_invalid")
        identifier = original.get("id", "")
        if not isinstance(identifier, str) or not SAFE_ID.fullmatch(identifier) or identifier in ids:
            raise ReelError("review_item_id_invalid")
        ids.add(identifier)
        path = (manifest_path.parent / original.get("path", "")).resolve()
        duration = original.get("duration_s")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 0 < duration <= 300:
            raise ReelError("review_duration_invalid")
        if not path.is_file() or path.stat().st_size > 100 * 1024 * 1024 or file_hash(path) != original.get("sha256"):
            raise ReelError("review_source_hash_mismatch")
        item = {"id": identifier, "path": str(path), "sha256": original["sha256"], "duration_s": duration}
        context = {"caption": "", "transcript": "", "metadata": ""}
        if original.get("context_path"):
            context_path = (manifest_path.parent / original["context_path"]).resolve()
            if not context_path.is_file() or context_path.stat().st_size > 60000 or file_hash(context_path) != original.get("context_sha256"):
                raise ReelError("review_context_hash_mismatch")
            raw = read_json(context_path)
            for name in context:
                value = raw.get(name, "")
                if not isinstance(value, str):
                    raise ReelError("review_context_invalid")
                context[name] = value
            item.update(context_path=str(context_path), context_sha256=original["context_sha256"])
        item["context"] = context
        items.append(item)
    return {"schema_version": 1, "corpus_id": source["corpus_id"], "items": items}


def item_fingerprint(item, config):
    return digest({"input_sha256": item["sha256"], "duration_s": item["duration_s"], "context": item["context"], "config": config})


def prepare(manifest_path, output, config):
    manifest = load_inputs(manifest_path)
    output = Path(output).resolve()
    for item in manifest["items"]:
        protected = [Path(item["path"])]
        if item.get("context_path"):
            protected.append(Path(item["context_path"]))
        if any(path.is_relative_to(output) for path in protected):
            raise ReelError("review_output_overlaps_source")
    stable = {"corpus_id": manifest["corpus_id"], "config": config, "items": [{"id": x["id"], "fingerprint": item_fingerprint(x, config)} for x in manifest["items"]]}
    fingerprint = digest(stable)
    existing = output / "batch.json"
    if existing.exists() and read_json(existing).get("fingerprint") != fingerprint:
        raise ReelError("review_batch_fingerprint_mismatch")
    if not existing.exists():
        write_json(existing, {"schema_version": 1, "manifest": str(Path(manifest_path).resolve()), **stable, "fingerprint": fingerprint, "created_at": time.time()})
    return manifest, output


def accepted(folder, item, config):
    result_file = folder / "result.json"
    if not result_file.exists():
        return None
    seal_file = folder / "integrity.json"
    if not seal_file.exists():
        return None  # Interrupted publication can be rebuilt from the saved terminal response.
    if read_json(seal_file).get("result_sha256") != file_hash(result_file):
        raise ReelError("review_result_hash_mismatch")
    result = read_json(result_file)
    if result.get("fingerprint") != item_fingerprint(item, config):
        raise ReelError("review_result_fingerprint_mismatch")
    validate_review(result["data"], item["duration_s"], item["context"])
    return result


def publish(folder, attempt, item, config, envelope, elapsed_s, recovered=False):
    data = validate_review(envelope["data"], item["duration_s"], item["context"])
    result = {"schema_version": 1, "id": item["id"], "fingerprint": item_fingerprint(item, config), "config": config, "input": {k:v for k,v in item.items() if k != "context"}, "attempt": attempt.name, "elapsed_s": elapsed_s, "usage": envelope.get("usage", {}), "usage_basis": envelope.get("usage_basis", "provider_reported"), "quality": "draft_unverified", "recovered": recovered, "data": data}
    write_json(folder / "result.json", result)
    write_json(folder / "integrity.json", {"result_sha256": file_hash(folder / "result.json")})
    write_json(folder / "state.json", {"state": "ready", "attempt": attempt.name, "updated_at": time.time()})
    return result


def execute_item(item, output, config, backend, retry_failed):
    folder = output / "items" / item["id"]
    if accepted(folder, item, config):
        return {"id": item["id"], "state": "ready", "reused": True, "new_calls": 0}
    folder.mkdir(parents=True, exist_ok=True)
    attempts = sorted(folder.glob("attempt-*"))
    if attempts:
        last = attempts[-1]
        # Every persisted terminal response is recoverable without another paid request.
        try:
            envelope = backend.recover(last)
            elapsed = read_json(last / "execution.json").get("elapsed_s", 0) if (last / "execution.json").exists() else 0
            publish(folder, last, item, config, envelope, elapsed, recovered=True)
            return {"id": item["id"], "state": "ready", "reused": True, "new_calls": 0, "recovered": True}
        except (ReelError, ValueError, OSError, KeyError):
            if not retry_failed:
                state = read_json(folder / "state.json") if (folder / "state.json").exists() else {"state": "unknown", "error": "interrupted_attempt"}
                if state.get("state") == "running":
                    state = {"state": "unknown", "error": "interrupted_attempt", "attempt": last.name, "updated_at": time.time()}
                    write_json(folder / "state.json", state)
                return {"id": item["id"], "state": state.get("state", "unknown"), "error": state.get("error"), "reused": False, "new_calls": 0, "retry_required": True}
    number = max([int(p.name.split("-")[-1]) for p in attempts] or [0]) + 1
    attempt = folder / f"attempt-{number:04d}"
    attempt.mkdir()
    started = time.time()
    monotonic = time.monotonic()
    write_json(attempt / "started.json", {"started_at": started})
    write_json(folder / "state.json", {"state": "running", "attempt": attempt.name, "updated_at": started})
    profile = Profile(timeout_s=config["timeout_s"], target_output_tokens=4096, min_quota=.1)
    prompt = PROMPT + f"\nMeasured duration: {item['duration_s']:.6f} seconds.\nAttributed source context:\n" + json.dumps(item["context"], ensure_ascii=False)
    state, error = "ready", None
    try:
        envelope = backend.invoke(attempt=attempt, files=[Path(item["path"])], prompt=prompt, schema=SCHEMA, model=config["model"], profile=profile)
        publish(folder, attempt, item, config, envelope, time.monotonic() - monotonic)
    except Exception as exc:
        state = "unknown" if isinstance(exc, UnknownOutcome) else "failed"
        error = str(exc) if isinstance(exc, ReelError) else type(exc).__name__
        write_json(folder / "state.json", {"state": state, "attempt": attempt.name, "error": error, "updated_at": time.time()})
    elapsed = time.monotonic() - monotonic
    new_call = int((attempt / "submitted.json").exists())
    write_json(attempt / "attempt.json", {"started_at": started, "ended_at": time.time(), "elapsed_s": elapsed, "state": state, "error": error, "new_call": bool(new_call), "usage": attempt_usage(attempt)})
    return {"id": item["id"], "state": state, "error": error, "reused": False, "new_calls": new_call, "elapsed_s": elapsed}


def attempt_usage(attempt):
    if (attempt / "response.json").exists():
        return read_json(attempt / "response.json").get("usage", {})
    if (attempt / "raw-response.json").exists():
        u = read_json(attempt / "raw-response.json").get("usageMetadata", {})
        return {"input_tokens": u.get("promptTokenCount"), "output_tokens": u.get("candidatesTokenCount"), "total_tokens": u.get("totalTokenCount"), "thinking_tokens": None if u.get("billing_usage") else u.get("thoughtsTokenCount")}
    if (attempt / "raw.ndjson").exists():
        terminal = {}
        for line in (attempt / "raw.ndjson").read_text().splitlines():
            try:
                row = json.loads(line)
                if row.get("event") == "result":
                    terminal = row.get("result", {})
            except ValueError:
                continue
        return terminal.get("usage", {})
    return {}


def summarize(manifest, output, config, execution=None):
    rows = []
    for item in manifest["items"]:
        folder = output / "items" / item["id"]
        state = read_json(folder / "state.json") if (folder / "state.json").exists() else {"state": "pending"}
        result = accepted(folder, item, config) if state["state"] == "ready" else None
        row = {"id": item["id"], "state": "ready" if result else state["state"]}
        if result:
            row.update(elapsed_s=result["elapsed_s"], usage=result["usage"], speech_access=result["data"]["speech_access"], visual_access=result["data"]["visual_access"], result=str(folder / "result.json"), caption_mentions=caption_mentions(item["context"]["caption"]))
        elif state.get("error"):
            row["error"] = state["error"]
        rows.append(row)
    counts = {name:sum(row["state"] == name for row in rows) for name in ("ready", "failed", "unknown", "pending", "running")}
    summary = {"schema_version": 1, "corpus_id": manifest["corpus_id"], "config": config, "counts": counts, "items": rows, "updated_at": time.time(), "execution": execution, "quality": "draft_unverified"}
    write_json(output / "summary.json", summary)
    return summary


def write_report(output, summary):
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")
    text = ["# Коммерческий review без субтитров", "", "Наблюдения модели — draft; речь и оплата не объявляются независимо проверенными. Упоминание профиля из подписи не является CTA.", "", "| Ролик | Статус | Формат / содержание | Товары | CTA | Упоминания из подписи |", "|---|---|---|---|---|---|"]
    for row in summary["items"]:
        if row["state"] != "ready":
            text.append(f"| {cell(row['id'])} | {row['state']} | {cell(row.get('error',''))} | | | |")
            continue
        data = read_json(row["result"])["data"]
        mentions = ", ".join(f"[{m['handle']}]({m['url']})" for m in row.get("caption_mentions", []))
        text.append(f"| [{cell(row['id'])}](<{row['result']}>) | draft | {cell(data['format'] + ': ' + data['summary'])} | {cell(', '.join(p['name'] for p in data['products']))} | {cell('; '.join(c['action'] for c in data['ctas']))} | {mentions} |")
    (output / "report.md").write_text("\n".join(text) + "\n")


def plan(manifest_path, output, config=None):
    config = config or configuration()
    output = Path(output).resolve()
    with batch_lock(output):
        manifest, output = prepare(manifest_path, output, config)
        return summarize(manifest, output, config)


def run(manifest_path, output, config=None, workers=8, retry_failed=False, backend=None, format_retries=1):
    config = config or configuration()
    if isinstance(workers, bool) or not 1 <= workers <= 8:
        raise ReelError("review_workers_invalid")
    if isinstance(format_retries, bool) or format_retries not in (0, 1):
        raise ReelError("review_format_retries_invalid")
    output = Path(output).resolve()
    with batch_lock(output):
        manifest, output = prepare(manifest_path, output, config)
        # Check every existing result before starting any remote work.
        for item in manifest["items"]:
            accepted(output / "items" / item["id"], item, config)
        backend = backend or (Zapro(config["thinking"]) if config["backend"] == "zapro" else ReviewAntigravity())
        runs = output / "executions"
        execution_id = f"{time.time_ns()}"
        begun = time.time()
        completed = []
        summarize(manifest, output, config, {"id": execution_id, "started_at": begun, "state": "running", "workers": workers})
        paused = threading.Event()

        def perform(item):
            if paused.is_set():
                folder = output / "items" / item["id"]
                if accepted(folder, item, config):
                    return {"id": item["id"], "state": "ready", "reused": True, "new_calls": 0}
                # Preserve any existing accepted artifact or failure; only untouched jobs remain pending.
                if not (folder / "state.json").exists():
                    write_json(folder / "state.json", {"state": "pending", "error": "provider_paused", "updated_at": time.time()})
                state = read_json(folder / "state.json")
                return {"id": item["id"], "state": state["state"], "error": state.get("error", "provider_paused"), "reused": False, "new_calls": 0}
            row = execute_item(item, output, config, backend, retry_failed)
            repairable = {"antigravity_completed_response_invalid", "gateway_review_not_json", "review_schema_invalid", "review_context_quote_not_found"}
            if format_retries and row.get("new_calls") and row.get("state") == "failed" and row.get("error") in repairable:
                first = row
                row = execute_item(item, output, config, backend, True)
                row["new_calls"] += first["new_calls"]
                row["elapsed_s"] = row.get("elapsed_s", 0) + first.get("elapsed_s", 0)
                row["reused"] = False
                row["format_retries_used"] = 1
            if not row.get("retry_required") and row.get("error") in ("gateway_http_401", "gateway_http_402", "gateway_http_403", "zapro_token_unavailable", "zapro_credential_conflict", "zapro_protocol_unavailable", "zapro_model_unavailable", "zapro_upstream_duplicate", "antigravity_quota_below_floor", "antigravity_auth_or_quota_unavailable", "subscription_configuration_required"):
                paused.set()
            return row

        with ThreadPoolExecutor(max_workers=workers) as pool:
            jobs = {pool.submit(perform, item):item for item in manifest["items"]}
            for future in as_completed(jobs):
                item = jobs[future]
                row = future.result()
                completed.append(row)
                print(json.dumps(row), flush=True)
                summarize(manifest, output, config, {"id": execution_id, "started_at": begun, "state": "running", "workers": workers, "finished_items": len(completed)})
        durations = [r["elapsed_s"] for r in completed if r.get("new_calls") and "elapsed_s" in r]
        execution = {"id": execution_id, "started_at": begun, "ended_at": time.time(), "wall_s": time.time()-begun, "workers": workers, "format_retries": format_retries, "format_retries_used": sum(r.get("format_retries_used", 0) for r in completed), "new_calls": sum(r["new_calls"] for r in completed), "reused": sum(r["reused"] for r in completed), "attempt_elapsed_sum_s": sum(durations), "attempt_median_s": statistics.median(durations) if durations else None, "items": completed}
        ledger = []
        for attempt in sorted((output / "items").glob("*/attempt-*")):
            attempt_file = attempt / "attempt.json"
            if attempt_file.exists():
                row = read_json(attempt_file)
            else:
                started = read_json(attempt / "started.json") if (attempt / "started.json").exists() else {}
                row = {"started_at": started.get("started_at", 0), "state": "interrupted_attempt", "new_call": (attempt / "submitted.json").exists(), "usage": attempt_usage(attempt)}
            ledger.append({"path": str(attempt), **row})
        execution["reported_usage_new_calls"] = {key:sum((r.get("usage") or {}).get(key) or 0 for r in ledger if r.get("new_call") and r["started_at"] >= begun) for key in ("input_tokens", "output_tokens", "total_tokens")}
        execution["new_calls_with_unknown_usage"] = sum(r.get("new_call") and r["started_at"] >= begun and not (r.get("usage") or {}).get("total_tokens") for r in ledger)
        write_json(output / "attempt-ledger.json", {"attempts": ledger, "note": "Provider-reported usage; thinking may be unavailable and is not added to output twice."})
        write_json(runs / (execution_id + ".json"), execution)
        summary = summarize(manifest, output, config, execution)
        write_report(output, summary)
        return summary


def status(output):
    output = Path(output).resolve()
    batch = read_json(output / "batch.json")
    try:
        with batch_lock(output):
            manifest, _ = prepare(batch["manifest"], output, batch["config"])
            previous = read_json(output / "summary.json").get("execution") if (output / "summary.json").exists() else None
            return summarize(manifest, output, batch["config"], previous)
    except ReelError as exc:
        if str(exc) == "raw_batch_busy" and (output / "summary.json").exists():
            return read_json(output / "summary.json")
        raise
