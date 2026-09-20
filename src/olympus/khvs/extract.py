"""Content-addressed, verifiable PDF extraction with page coordinates."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import tempfile

EXTRACTOR_VERSION = "pdf-lines-1"


class WorkflowError(ValueError):
    def __init__(self, code: str, details=None):
        super().__init__(code)
        self.code = code
        self.details = details


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = canonical(value)
    fd, temporary = tempfile.mkstemp(prefix=".khvs-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def lines_from_words(words: list[dict], tolerance=2.5) -> list[dict]:
    """Group by baseline rather than top, preserving superscript unit characters."""
    groups: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (w["bottom"], w["x0"])):
        if not groups or abs(word["bottom"] - groups[-1][0]["bottom"]) > tolerance:
            groups.append([])
        groups[-1].append(word)
    result = []
    for group in groups:
        group.sort(key=lambda w: w["x0"])
        result.append({
            "text": " ".join(w["text"] for w in group),
            "bbox": [round(min(w["x0"] for w in group), 3),
                     round(min(w["top"] for w in group), 3),
                     round(max(w["x1"] for w in group), 3),
                     round(max(w["bottom"] for w in group), 3)],
        })
    return sorted(result, key=lambda x: (x["bbox"][1], x["bbox"][0]))


def extract_pdf(path: Path, cache_root: Path, max_pages=500) -> tuple[dict, dict]:
    import pdfplumber
    raw = path.read_bytes()
    source_sha = sha256(raw)
    engine = {"extractor": EXTRACTOR_VERSION, "pdfplumber": importlib.metadata.version("pdfplumber")}
    key = sha256(canonical([source_sha, engine]))
    cache = cache_root / f"{key}.json"
    cache_state = "miss"
    if cache.exists():
        try:
            envelope = json.loads(cache.read_bytes())
            payload = envelope["payload"]
            if (sha256(canonical(payload)) != envelope["sha256"]
                    or payload["source_sha256"] != source_sha or payload["engine"] != engine):
                raise ValueError("mismatch")
            if len(payload["pages"]) > max_pages:
                raise WorkflowError("pdf_page_limit", {"pages": len(payload["pages"]), "limit": max_pages})
            return payload, {"state": "hit", "key": key, "extraction_calls": 0}
        except WorkflowError:
            raise
        except (ValueError, KeyError, TypeError, OSError):
            cache_state = "rebuilt_corrupt"
    pages = []
    with pdfplumber.open(path) as pdf:
        if len(pdf.pages) > max_pages:
            raise WorkflowError("pdf_page_limit", {"pages": len(pdf.pages), "limit": max_pages})
        for number, page in enumerate(pdf.pages, 1):
            clean = page.dedupe_chars(tolerance=1, extra_attrs=("fontname", "size"))
            words = clean.extract_words(x_tolerance=2, y_tolerance=3)
            mid = float(page.width) / 2
            sides = {"left": [], "right": []}
            for word in words:
                side = "left" if (word["x0"] + word["x1"]) / 2 < mid else "right"
                sides[side].append(word)
            pages.append({"number": number, "width": float(page.width), "height": float(page.height),
                          "text": clean.extract_text() or "", "lines": lines_from_words(words),
                          "left": lines_from_words(sides["left"]), "right": lines_from_words(sides["right"])})
    payload = {"source_sha256": source_sha, "engine": engine, "pages": pages}
    write_json(cache, {"sha256": sha256(canonical(payload)), "payload": payload})
    return payload, {"state": cache_state, "key": key, "extraction_calls": 1}


def complete_text(extraction: dict) -> str:
    return "\n\n".join(f"=== Physical PDF page {p['number']} ===\n{p['text']}" for p in extraction["pages"])
