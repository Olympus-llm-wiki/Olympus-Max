#!/usr/bin/env python3
"""Inventory a media folder before extraction.

Reports duration, size, audio-track presence and container health per file, plus totals
by top-level subfolder. Catches the failures that silently waste hours: files with no
audio track, truncated downloads, and a "course" that is mostly low-value call recordings.

Usage:
    python3 inventory.py "<folder>" [--ext .mp4,.mov,.mkv,.m4a,.mp3]
"""
import argparse
import json
import pathlib
import subprocess
import sys
from collections import defaultdict

DEFAULT_EXT = ".mp4,.mov,.mkv,.webm,.m4a,.mp3,.wav"


def probe(path: pathlib.Path) -> dict:
    """Return duration, stream types and container health for one file."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration,format_name",
        "-show_entries", "stream=index,codec_type,codec_name",
        "-of", "json", str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ffprobe timeout — likely truncated"}
    if out.returncode != 0:
        msg = (out.stderr or "").strip().splitlines()
        return {"ok": False, "error": msg[-1] if msg else "ffprobe failed"}

    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "unparseable ffprobe output"}

    streams = data.get("streams", [])
    fmt = data.get("format", {})
    try:
        duration = float(fmt.get("duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0.0

    return {
        "ok": True,
        "duration": duration,
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
        "has_video": any(s.get("codec_type") == "video" for s in streams),
        "codecs": ",".join(s.get("codec_name", "?") for s in streams),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--ext", default=DEFAULT_EXT)
    args = ap.parse_args()

    root = pathlib.Path(args.folder).expanduser()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    exts = {e.strip().lower() for e in args.ext.split(",") if e.strip()}
    files = sorted(p for p in root.rglob("*") if p.suffix.lower() in exts)
    if not files:
        print("no media files found")
        return 1

    total = 0.0
    problems: list[str] = []
    by_group: dict[str, list[float]] = defaultdict(list)

    print(f"{'dur':>8}  {'size':>8}  {'A/V':<5} file")
    print("-" * 78)
    for f in files:
        rel = f.relative_to(root)
        info = probe(f)
        size_mb = f.stat().st_size / 1048576

        if not info["ok"]:
            problems.append(f"BROKEN   {rel} — {info['error']}")
            print(f"{'—':>8}  {size_mb:7.0f}M  {'??':<5} {rel}")
            continue

        dur = info["duration"]
        total += dur
        by_group[rel.parts[0] if len(rel.parts) > 1 else "."].append(dur)

        av = ("A" if info["has_audio"] else "-") + "/" + ("V" if info["has_video"] else "-")
        if not info["has_audio"]:
            problems.append(f"NO AUDIO {rel} — video-only stream, any ASR returns silence")
        if dur == 0:
            problems.append(f"ZERO DUR {rel} — container may be truncated")

        print(f"{dur/60:7.1f}m  {size_mb:7.0f}M  {av:<5} {rel}")

    print("-" * 78)
    print(f"TOTAL: {len(files)} files, {total/3600:.2f} hours\n")

    if len(by_group) > 1:
        print("By folder:")
        for group, durs in sorted(by_group.items()):
            print(f"  {sum(durs)/3600:6.2f} h  ({len(durs):2d} files)  {group}")
        print()

    if problems:
        print("PROBLEMS — fix these before extraction:")
        for p in problems:
            print(f"  {p}")
    else:
        print("No problems found.")

    pdfs = list(root.rglob("*.pdf"))
    if pdfs:
        print(f"\n{len(pdfs)} PDF(s) present — extract them first, they are usually the")
        print("author's own lesson summaries and give the course skeleton in seconds:")
        print(f'  find "{root}" -name "*.pdf" -exec pdftotext -layout {{}} \\;')

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
