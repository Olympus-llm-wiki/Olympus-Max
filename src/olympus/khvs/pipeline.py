"""Local orchestration: capture, cache, profile, outputs and bounded receipts."""
from __future__ import annotations

from dataclasses import asdict
from importlib.metadata import version
import json
import os
from pathlib import Path
import shutil
import time
import zipfile

from ..preservation import Store
from . import PROFILE_VERSION
from .extract import WorkflowError, canonical, complete_text, extract_pdf, sha256, write_json
from .ned import parse_ned
from .template import inspect_template, map_schedule
from .report import render_review
from .workbook import render_workbook


def in_git(path: Path) -> bool:
    return any((parent / ".git").exists() for parent in (path, *path.parents))


def resolve_input(text: str) -> Path:
    path = Path(text).expanduser()
    if not path.is_absolute() and len(path.parts) == 1 and not text.startswith("."):
        downloaded = Path.home() / "Downloads" / path
        if downloaded.is_file():
            path = downloaded
    path = path.resolve()
    if not path.is_file():
        raise WorkflowError("input_not_found", {"path": str(path)})
    return path


def _capture(store, *, key, title, raw, role, locator="", text="", parents=()):
    metadata = {"material_role": role}
    if parents:
        metadata["parent_sources"] = json.dumps([{"version_id": p} for p in parents])
    return asdict(store.capture(source_key=key, scope="engineering-ai", title=title, original=raw,
                                text=text, locator=locator, metadata=metadata, archive_only=True))


def run(pdf: Path, template: Path, out: Path, state: Path, cache: Path | None = None, *, profile=PROFILE_VERSION, preview=True) -> dict:
    start = time.perf_counter()
    if profile != PROFILE_VERSION:
        raise WorkflowError("needs_profile", {"requested": profile, "supported": [PROFILE_VERSION]})
    pdf, template, out, state = (Path(p).expanduser().resolve() for p in (pdf, template, out, state))
    if pdf.suffix.lower() != ".pdf" or template.suffix.lower() != ".xlsx":
        raise WorkflowError("pdf_and_xlsx_required")
    if any(in_git(p) for p in (pdf.parent, template.parent, out, state)):
        raise WorkflowError("private_documents_require_non_git_paths")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise WorkflowError("output_directory_not_empty", {"path": str(out)})
    for path in (pdf, template):
        if not path.is_file():
            raise WorkflowError("input_not_found", {"path": str(path)})
        if path.stat().st_size > 64 * 1024 * 1024:
            raise WorkflowError("input_size_limit", {"path": str(path), "limit_mb": 64})
    cache = Path(cache).expanduser().resolve() if cache else state / "khvs/cache"
    if in_git(cache):
        raise WorkflowError("cache_requires_non_git_path")
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    work, inputs = out / ".work", out / "inputs"
    work.mkdir(mode=0o700)
    inputs.mkdir(mode=0o700)
    store = Store(state)
    receipts = {}
    stage = time.perf_counter()
    snapshots = {}
    for kind, path in (("pdf", pdf), ("xlsx", template)):
        raw = path.read_bytes()
        receipts[kind] = _capture(store, key=f"khvs:{kind}:{sha256(raw)}", title=f"ХОВС — оригинал {kind.upper()}",
                                  raw=raw, role="primary", locator=path.as_uri())
        snapshot = inputs / ("source.pdf" if kind == "pdf" else "template.xlsx")
        snapshot.write_bytes(raw)
        snapshots[kind] = {"path": str(snapshot.relative_to(out)), "sha256": sha256(raw), "input_locator": path.as_uri(), "version_id": receipts[kind]["version_id"]}
    timings = {"capture_seconds": time.perf_counter() - stage}
    write_json(out / "manifest.json", {"schema": 1, "status": "captured", "profile": profile, "inputs": snapshots, "receipts": receipts})
    try:
        stage = time.perf_counter()
        extraction, cache_info = extract_pdf(inputs / "source.pdf", cache)
        timings["extraction_seconds"] = time.perf_counter() - stage
        text = complete_text(extraction)
        (out / "source-text.txt").write_text(text)
        receipts["extraction"] = _capture(store, key=f"khvs:extraction:{cache_info['key']}", title="ХОВС — текст и координаты PDF",
                                          raw=canonical(extraction), text=text, role="extracted", parents=(receipts["pdf"]["version_id"],))
        stage = time.perf_counter()
        parsed = parse_ned(extraction)
        template_info = inspect_template(inputs / "template.xlsx")
        schedule = map_schedule(parsed, template_info)
        schedule["source_version"] = receipts["pdf"]["version_id"]
        schedule["template_version"] = receipts["xlsx"]["version_id"]
        schedule["extraction_version"] = receipts["extraction"]["version_id"]
        write_json(out / "values.json", schedule)
        write_json(out / "issues.json", {"issues": schedule["issues"], "unfilled": schedule["unfilled"]})
        timings["mapping_seconds"] = time.perf_counter() - stage
        stage = time.perf_counter()
        workbook_check = render_workbook(schedule, inputs / "template.xlsx", out / "result.xlsx", work, preview=preview)
        timings["workbook_seconds"] = time.perf_counter() - stage
        stage = time.perf_counter()
        pdf_check = render_review(schedule, inputs / "source.pdf", out / "review.pdf", work)
        timings["review_pdf_seconds"] = time.perf_counter() - stage
        summary = {
            "status": schedule["status"], "profile": PROFILE_VERSION, "systems": len(schedule["systems"]),
            "values": len(schedule["updates"]), "issues": len(schedule["issues"]), "unfilled": len(schedule["unfilled"]),
            "cache": cache_info, "model_calls": 0, "model_tokens": 0,
            "timings": {key: round(value, 4) for key, value in timings.items()},
            "outputs": {"xlsx": str(out / "result.xlsx"), "review": str(out / "review.pdf"), "data": str(out / "values.json"), "issues": str(out / "issues.json")},
            "visual_review": "previews_available" if preview else "not_requested", "engineering_acceptance": False,
        }
        report = f"# ХОВС — результат локальной обработки\n\nСтатус: {summary['status']}. Систем: {summary['systems']}, значений: {summary['values']}, замечаний: {summary['issues']}.\n\n[Excel](result.xlsx) · [Проверка и исходные страницы](review.pdf) · [Замечания](issues.json).\n\nПрофиль {PROFILE_VERSION}; Д1–Д3 остаются рабочими допущениями. Неизвлечённые значения не равны нулю и не доказывают отсутствие оборудования. Сверка сохранности XLSX и ссылок PDF выполнена программно. Инженерная приёмка не выполнялась.\n\nКоманда выполнила 0 модельных вызовов. Это не стоимость разработки или внешней работы агента. Кеш: {cache_info['state']}.\n"
        (out / "summary.md").write_text(report)
        stage = time.perf_counter()
        parents = (receipts["pdf"]["version_id"], receipts["xlsx"]["version_id"], receipts["extraction"]["version_id"])
        for name in ("values.json", "issues.json", "result.xlsx", "review.pdf", "summary.md"):
            path = out / name
            raw = path.read_bytes()
            receipts[name] = _capture(store, key=f"khvs:output:{sha256(raw)}", title="ХОВС — " + name,
                                      raw=raw, text=raw.decode() if path.suffix in (".json", ".md") else "", role="artifact", parents=parents)
        timings["output_capture_seconds"] = time.perf_counter() - stage
        summary["outputs"]["package"] = str(out / "package.zip")
        summary["outputs"]["metrics"] = str(out / "metrics.json")
        manifest = {"schema": 1, "status": schedule["status"], "profile": PROFILE_VERSION, "inputs": snapshots, "receipts": receipts,
                    "cache": cache_info, "checks": {"workbook": workbook_check, "pdf": pdf_check},
                    "versions": {name: version(name) for name in ("pdfplumber", "pypdf", "openpyxl", "reportlab", "lxml")}}
        write_json(out / "manifest.json", manifest)
        stage = time.perf_counter()
        with zipfile.ZipFile(out / "package.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for name in ("result.xlsx", "review.pdf", "summary.md", "values.json", "issues.json", "manifest.json", "inputs/source.pdf", "inputs/template.xlsx"):
                archive.write(out / name, name)
        timings["package_seconds"] = time.perf_counter() - stage
        summary["timings"] = {key: round(value, 4) for key, value in timings.items()}
        summary["elapsed_seconds"] = round(time.perf_counter() - start, 4)
        write_json(out / "metrics.json", summary)
        return summary
    except Exception as error:
        failure = {"status": "failed", "code": getattr(error, "code", type(error).__name__), "details": getattr(error, "details", None), "receipts": receipts}
        write_json(out / "failure.json", failure)
        write_json(out / "manifest.json", {"schema": 1, "profile": profile, "inputs": snapshots, **failure})
        raise


def render_page(job: Path, number: int, bbox=None) -> dict:
    import pdfplumber
    job = Path(job).expanduser().resolve()
    manifest = json.loads((job / "manifest.json").read_bytes())
    record = manifest["inputs"]["pdf"]
    source = (job / record["path"]).resolve()
    if not source.is_relative_to(job) or sha256(source.read_bytes()) != record["sha256"]:
        raise WorkflowError("source_snapshot_mismatch")
    with pdfplumber.open(source) as pdf:
        if not 1 <= number <= len(pdf.pages):
            raise WorkflowError("invalid_page_number")
        page = pdf.pages[number - 1]
        if bbox:
            if len(bbox) != 4 or not (0 <= bbox[0] < bbox[2] <= page.width and 0 <= bbox[1] < bbox[3] <= page.height):
                raise WorkflowError("invalid_bbox")
            page = page.crop(tuple(bbox))
        folder = job / "evidence"
        if not folder.resolve().is_relative_to(job):
            raise WorkflowError("evidence_directory_outside_job")
        folder.mkdir(exist_ok=True)
        suffix = "-" + sha256(canonical(bbox))[:8] if bbox else ""
        output = folder / f"page-{number:03d}{suffix}.png"
        page.to_image(resolution=140).save(output)
    return {"page": number, "bbox": bbox, "image": str(output), "source_sha256": record["sha256"]}
