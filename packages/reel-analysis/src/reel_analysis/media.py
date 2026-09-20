"""Deterministic preparation; never infers speech or graphic meaning."""
import array
import json
import math
from pathlib import Path
import subprocess

from .common import ReelError, file_hash, write_json


def execute(args, timeout=180, binary=False):
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout, check=True)
    except (subprocess.SubprocessError, OSError):
        raise ReelError("media_command_failed") from None
    return result.stdout if binary else result.stdout.decode()


def versions():
    return {x: execute([x, "-version"]).splitlines()[0] for x in ("ffmpeg", "ffprobe")}


def inspect(path, profile):
    if path.stat().st_size > profile.max_bytes:
        raise ReelError("file_too_large")
    # Do not let ffprobe treat an uploaded playlist as instructions to fetch
    # network URLs or read unrelated local files. Reels use MP4/MOV or WebM.
    with path.open("rb") as source:
        magic = source.read(16)
    if not (magic[4:8] == b"ftyp" or magic[:4] == b"\x1aE\xdf\xa3"):
        raise ReelError("mp4_mov_or_webm_required")
    info = json.loads(execute(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]))
    video = [x for x in info["streams"] if x["codec_type"] == "video"]
    audio = [x for x in info["streams"] if x["codec_type"] == "audio"]
    duration = float(info["format"].get("duration", 0))
    if len(video) != 1 or len(audio) > 1 or not math.isfinite(duration) or not 0 < duration <= profile.max_duration_s:
        raise ReelError("unsupported_short_video")
    if video[0]["width"] * video[0]["height"] > 4096 * 4096:
        raise ReelError("video_resolution_limit")
    raw = json.loads(execute(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(path)]))
    origin = float(info["format"].get("start_time", 0))
    pts = [float(x["best_effort_timestamp_time"]) - origin for x in raw["frames"]]
    if not pts or any(not math.isfinite(x) for x in pts) or pts != sorted(pts) or pts[0] < -.01:
        raise ReelError("invalid_frame_timestamps")
    # Force full video decoding, including the end; ffprobe metadata alone is insufficient.
    execute(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path), "-map", "0:v:0", "-f", "null", "-"])
    silence = None
    if audio:
        # Decode channels at native rate before checking silence; no cancellation by downmix.
        expected_pcm = int(audio[0].get("sample_rate", 0)) * int(audio[0].get("channels", 0)) * duration * 4
        if not 0 < expected_pcm <= 256_000_000:
            raise ReelError("decoded_audio_limit")
        pcm = execute(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path), "-map", "0:a:0", "-c:a", "pcm_f32le", "-f", "f32le", "-"], binary=True)
        samples = array.array("f")
        samples.frombytes(pcm)
        if not samples or not all(math.isfinite(x) for x in samples):
            raise ReelError("invalid_audio_decode")
        silence = all(x == 0 for x in samples)
    fps_n, fps_d = map(int, video[0]["r_frame_rate"].split("/"))
    if fps_d == 0 or not 0 < fps_n / fps_d <= 240 or len(pts) > 50_000:
        raise ReelError("video_frame_limit")
    return {"sha256": file_hash(path), "bytes": path.stat().st_size, "duration_s": duration, "origin_s": origin, "frame_pts": pts, "fps": fps_n / fps_d, "width": video[0]["width"], "height": video[0]["height"], "has_audio": bool(audio), "digital_silence": silence, "tools": versions()}


def prepare(original, info, out, fps=4):
    out.mkdir(parents=True, exist_ok=True)
    manifest = out / "manifest.json"
    if manifest.exists():
        result = json.loads(manifest.read_text())
        if result["source_sha256"] == info["sha256"] and result.get("prepared_video_fps") == fps and all((out / f).exists() and file_hash(out / f) == h for f, h in result["files"].items()):
            return result
    execute(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(original), "-map", "0:v:0", "-an", "-vf", f"fps={fps},scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-fps_mode", "passthrough", "-movflags", "+faststart", str(out / "visual.mp4")])
    files = ["visual.mp4"]
    if info["has_audio"] and not info["digital_silence"]:
        execute(["ffmpeg", "-nostdin", "-v", "error", "-y", "-copyts", "-start_at_zero", "-i", str(original), "-map", "0:a:0", "-af", "aresample=16000:async=1:first_pts=0", "-c:a", "pcm_s16le", str(out / "speech.wav")])
        files.append("speech.wav")
    # Choose actual original frames spread across the entire video, including the tail.
    indices = sorted({round(i * (len(info["frame_pts"]) - 1) / 5) for i in range(6)})
    frames = []
    for n in indices:
        name = f"frame-{n:06d}.jpg"
        execute(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(original), "-vf", f"select=eq(n\\,{n})", "-frames:v", "1", "-q:v", "2", str(out / name)])
        files.append(name)
        frames.append({"file": name, "frame_index": n, "pts_s": info["frame_pts"][n]})
    if (out / "visual.mp4").stat().st_size > 18 * 1024 * 1024:
        raise ReelError("prepared_video_exceeds_native_limit")
    result = {"source_sha256": info["sha256"], "frames": frames, "files": {f: file_hash(out / f) for f in files}, "offset_s": 0, "prepared_video_fps": fps, "sampling_note": "Derived video uses FFmpeg fps sampling; stills retain original frame indices and PTS. Native model sampling is not controlled."}
    write_json(manifest, result)
    return result


def subclip(original, out, start, end, audio=False):
    if end <= start:
        raise ReelError("invalid_clip_window")
    args = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", str(start), "-i", str(original), "-t", str(end - start)]
    args += ["-map", "0:a:0", "-vn", "-c:a", "pcm_s16le"] if audio else ["-map", "0:v:0", "-an", "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-fps_mode", "passthrough"]
    execute(args + [str(out)])
    return {"file": out.name, "source_sha256": file_hash(original), "offset_s": start, "end_s": end, "sha256": file_hash(out)}


def crop_frame(original, out, info, index, box):
    """A crop from an explicitly indexed original frame, never a guessed FPS label."""
    from .contracts import Box
    box = Box.model_validate(box)
    if not 0 <= index < len(info["frame_pts"]):
        raise ReelError("invalid_frame_index")
    width, height = info["width"], info["height"]
    x, y = int(box.x * width), int(box.y * height)
    w = min(width - x, max(2, math.ceil(box.width * width)))
    h = min(height - y, max(2, math.ceil(box.height * height)))
    if w < 2 or h < 2:
        raise ReelError("empty_crop")
    execute(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(original), "-vf", f"select=eq(n\\,{index}),crop={w}:{h}:{x}:{y}", "-frames:v", "1", "-q:v", "2", str(out)])
    return {"file": out.name, "frame_index": index, "pts_s": info["frame_pts"][index], "crop_pixels": {"x": x, "y": y, "width": w, "height": h}, "source_sha256": info["sha256"], "sha256": file_hash(out)}
