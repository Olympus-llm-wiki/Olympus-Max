"""Conservative automatic-profile boundary, not a semantic format classifier."""
from __future__ import annotations

import re
from urllib.parse import urlsplit


_STRUCTURED = {"json", "jsonl", "ndjson", "csv", "tsv", "yaml", "yml", "xml", "toml", "ini"}
_CODE = {"py", "js", "ts", "tsx", "jsx", "go", "rs", "java", "c", "h", "cpp", "hpp", "sh", "bash", "zsh", "sql", "swift"}


def classify_delivery_format(version: dict, text_prefix: str = "") -> dict:
    """Metadata and a bounded prefix can reject risky defaults without truncating data.

    A rejected source remains captured/indexed locally and awaits an explicit
    profile review. This does not archive it or accept a backlog exception.
    """
    metadata = version.get("metadata", {})
    prefix = text_prefix.lstrip("\ufeff \t\r\n")[:8192]
    signals = []
    names = [str(version.get("title", "")), str(version.get("locator", ""))]
    names.extend(str(metadata.get(key, "")) for key in ("filename", "original_filename", "source_path", "effective_url", "source_url"))
    extensions = set()
    for name in names:
        try:
            path = urlsplit(name).path if "://" in name else name
        except ValueError:
            path = name
        extensions.update(ext.lower() for ext in re.findall(r"\.([a-zA-Z0-9]+)(?=$|[\s?#/])", path))
    declared = " ".join(str(metadata.get(key, "")).lower() for key in
                        ("media_type", "content_type", "mime_type", "format", "source_format", "document_kind"))
    kind = "narrative_text"
    if extensions & _STRUCTURED or any(value in declared for value in ("application/json", "application/xml", "yaml", "text/csv", "ndjson")):
        kind = "structured_reference"
        signals.append("declared_structured_format")
    if prefix.startswith("{") or re.match(r'\[\s*(?:[\[{"]|-?\d+(?:[.,\]])|(?:true|false|null)\s*[,\]])', prefix):
        kind = "structured_json"
        signals.append("json_like_prefix")
    if extensions & _CODE or re.match(r"(?:#!|diff --git |```(?:python|javascript|typescript|json|sql|bash|sh|rust|go)\b)", prefix):
        kind = "code_reference"
        signals.append("declared_code_or_code_prefix")
    if any(value in declared for value in ("text/x-python", "javascript", "typescript", "shellscript", "api-reference", "api_reference")):
        kind = "code_reference"
        signals.append("declared_reference_type")
    if ((metadata.get("upstream_repository") and metadata.get("upstream_commit") and prefix.startswith("Repository:"))
            or re.match(r"Repository:\s*https?://[^\n]+\nCommit:", prefix)):
        kind = "repository_reference"
        signals.append("repository_aggregate")
    if re.match(r"(?i)(?:<!doctype\s+html|<html\b|<\?xml\b)", prefix):
        kind = "markup_reference"
        signals.append("unextracted_markup_prefix")
    if prefix.startswith("data:") or re.match(r"[A-Za-z0-9+/]{256}", prefix):
        kind = "encoded_reference"
        signals.append("encoded_payload_prefix")
    if not prefix and not signals:
        kind = "unknown_text"
        signals.append("no_text_prefix")
    allowed = kind == "narrative_text"
    if allowed:
        signals.append("plain_text_prefix_without_structured_signals")
    return {"schema": 1, "format": kind, "auto_profile_allowed": allowed,
            "reason": None if allowed else "waiting_profile_review", "signals": signals,
            "local_text_available": True, "owner_exception_accepted": False}
