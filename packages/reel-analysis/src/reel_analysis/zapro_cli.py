"""Reproducible, bounded diagnostics and text requests through the shared client."""
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import uuid

from .common import ReelError, write_json
from .zapro_client import DEFAULT_MODEL, MODELS, ZaproClient, ZaproError


def add_parser(commands):
    root = commands.add_parser("zapro", help="standard Zapro client and live health checks")
    sub = root.add_subparsers(dest="zapro_command", required=True)
    doctor = sub.add_parser("doctor", help="live unique-marker probes; makes billed model requests")
    doctor.add_argument("--model", action="append", choices=MODELS)
    doctor.add_argument("--rounds", type=int, choices=range(1, 4), default=1)
    doctor.add_argument("--native", action="store_true", help="also probe Gemini-native text; does not verify video")
    chat = sub.add_parser("chat")
    chat.add_argument("--prompt-file", required=True)
    chat.add_argument("--model", default=DEFAULT_MODEL, choices=MODELS)
    chat.add_argument("--max-tokens", type=int, default=1024)
    for command in (doctor, chat):
        command.add_argument("--timeout", type=float, default=60)
        command.add_argument("--output", required=True, help="new private JSON receipt file; no overwrite")


def doctor(client, *, models=MODELS, rounds=1, native=False):
    rows = []
    try:
        catalog, catalog_receipt = client.models()
    except ZaproError as exc:
        return {"state": "failed", "error": exc.code, "receipt": exc.receipt, "probes": []}
    ids = {row.get("id") for row in catalog if isinstance(row, dict)}
    for model in models:
        for round_number in range(1, rounds + 1):
            for protocol in (["chat", "native"] if native else ["chat"]):
                marker = "PING_" + uuid.uuid4().hex[:12].upper()
                row = {"model": model, "protocol": protocol, "round": round_number, "listed": model in ids}
                try:
                    if protocol == "chat":
                        result = client.chat("Reply with exactly this token: " + marker, model=model, max_tokens=256)
                        text, receipt = result["text"], result["receipt"]
                    else:
                        payload, receipt = client.request("native", model=model, body={
                            "contents": [{"role": "user", "parts": [{"text": "Reply with exactly this token: " + marker}]}],
                            "generationConfig": {"maxOutputTokens": 256}})
                        candidates = payload.get("candidates", []) if isinstance(payload, dict) else []
                        if not isinstance(candidates, list) or len(candidates) != 1 or not isinstance(candidates[0], dict):
                            raise ZaproError("zapro_invalid_response", receipt)
                        candidate = candidates[0]
                        content = candidate.get("content")
                        parts = content.get("parts") if isinstance(content, dict) else None
                        if not isinstance(parts, list) or not all(isinstance(p, dict) for p in parts):
                            raise ZaproError("zapro_invalid_response", receipt)
                        if any(any(k in p for k in ("functionCall", "toolCall", "executableCode")) for p in parts):
                            raise ZaproError("zapro_unexpected_tool_call", receipt)
                        text = "".join(p["text"] for p in parts if isinstance(p.get("text"), str) and not p.get("thought"))
                        if candidate.get("finishReason") != "STOP":
                            raise ZaproError("zapro_incomplete_response", receipt)
                    row.update(receipt=receipt, exact_marker=text.strip() == marker,
                               state="ready" if text.strip() == marker else "failed")
                    if not row["exact_marker"]:
                        row["error"] = "zapro_marker_mismatch"
                except ZaproError as exc:
                    row.update(state="failed", error=exc.code, receipt=exc.receipt)
                rows.append(row)
    return {"observed_at": datetime.now(timezone.utc).isoformat(),
            "state": "ready" if all(r["state"] == "ready" for r in rows) else "degraded",
            "catalog": {"models": sorted(ids - {None}), "receipt": catalog_receipt}, "probes": rows,
            "scope": "Text marker only; no streaming, tools, image or video readiness claim"}


def dispatch(args):
    output = Path(args.output).expanduser()
    if output.exists():
        raise ReelError("zapro_output_exists")
    prompt = Path(args.prompt_file).read_text() if args.zapro_command == "chat" else None
    # Reserve an immutable attempt before any network operation. A crash leaves a
    # visible unknown outcome; running the same command cannot send the POST again.
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ReelError("zapro_output_exists") from None
    with os.fdopen(fd, "w") as stream:
        json.dump({"state": "unknown", "reason": "attempt_started_no_terminal_receipt"}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        client = ZaproClient(timeout=args.timeout)
        if args.zapro_command == "doctor":
            result = doctor(client, models=args.model or MODELS, rounds=args.rounds, native=args.native)
        else:
            result = {"state": "ready", **client.chat(prompt, model=args.model, max_tokens=args.max_tokens)}
    except ZaproError as exc:
        result = {"state": "unknown" if exc.receipt.get("outcome") == "unknown" else "failed",
                  "error": exc.code, "receipt": exc.receipt}
    write_json(output, result)
    return result
