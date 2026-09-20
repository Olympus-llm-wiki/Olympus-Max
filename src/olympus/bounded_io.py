"""Deadline-bound reads of files which may be backed by a cloud provider.

O_NONBLOCK does not bound FileProvider materialization. A disposable subprocess
owns each read; only bounded bytes or a digest cross the pipe. No provider
credentials, cache eviction, or remote deletion are involved.
"""
from __future__ import annotations

import hashlib
import codecs
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

from olympus.preservation import PreservationError, guard_no_secrets

DATALESS = 0x40000000  # Darwin SF_DATALESS; absent flags are zero elsewhere.


class FileUnavailable(PreservationError):
    def __init__(self, code: str, errno: int | None = None):
        super().__init__(code)
        self.errno = errno


def fingerprint(path: Path, *, allow_dataless: bool = False) -> dict:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise FileUnavailable("library_file_symlink")
    if not stat.S_ISREG(info.st_mode):
        raise FileUnavailable("library_file_not_regular")
    flags = getattr(info, "st_flags", 0)
    if flags & DATALESS and not allow_dataless:
        raise FileUnavailable("materialization_pending")
    return {"device": info.st_dev, "inode": info.st_ino, "bytes": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns, "flags": flags}


def _request(path: Path, mode: str, *, max_bytes: int, timeout: float, extra: dict | None = None) -> tuple[dict, bytes]:
    if max_bytes < 0 or not 0 < timeout <= 300:
        raise ValueError("invalid_file_read_limit")
    # Avoid even asking FileProvider to hydrate known placeholders.
    observed = fingerprint(path)
    if observed["bytes"] > max_bytes:
        raise FileUnavailable("file_size_limit")
    request = {"path": str(path), "mode": mode, "max_bytes": max_bytes, "expected": observed}
    request.update(extra or {})
    try:
        result = subprocess.run([sys.executable, "-m", "olympus.bounded_io"],
            input=json.dumps(request).encode(), stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise FileUnavailable("file_read_timeout") from None
    except OSError as exc:
        raise FileUnavailable("file_reader_unavailable", exc.errno) from None
    try:
        header, _, content = result.stdout.partition(b"\n")
        response = json.loads(header)
        if response.get("error"):
            raise FileUnavailable(response["error"], response.get("errno"))
        if result.returncode or response["fingerprint"] != observed:
            raise FileUnavailable("file_changed_during_read")
        if mode == "read" and (len(content) != observed["bytes"] or len(content) > max_bytes):
            raise FileUnavailable("file_read_incomplete")
        return response, content
    except (KeyError, ValueError, TypeError):
        raise FileUnavailable("file_reader_failed") from None


def read_file(path: Path, *, max_bytes: int = 8 * 1024**2, timeout: float = 2) -> bytes:
    return _request(Path(path), "read", max_bytes=max_bytes, timeout=timeout)[1]


def hash_file(path: Path, *, max_bytes: int = 8 * 1024**3, timeout: float = 5) -> str:
    return _request(Path(path), "hash", max_bytes=max_bytes, timeout=timeout)[0]["sha256"]


def guard_file(path: Path, *, max_bytes: int = 8 * 1024**3, timeout: float = 60) -> None:
    _request(Path(path), "guard", max_bytes=max_bytes, timeout=timeout)


def scan_guard_file(path: Path, *, checkpoint: dict | None = None,
                    window_bytes: int = 32 * 1024**2, timeout: float = 5) -> dict:
    if not 1 <= window_bytes <= 64 * 1024**2:
        raise ValueError("invalid_guard_window")
    return _request(Path(path), "guard", max_bytes=8 * 1024**3, timeout=timeout,
                    extra={"checkpoint": checkpoint or {}, "window_bytes": window_bytes})[0]


def copy_window(source: Path, target: Path, *, offset: int = 0, window_bytes: int = 32 * 1024**2,
                timeout: float = 5) -> dict:
    if not 1 <= window_bytes <= 64 * 1024**2 or offset < 0:
        raise ValueError("invalid_copy_window")
    return _request(Path(source), "copy", max_bytes=8 * 1024**3, timeout=timeout,
                    extra={"destination": str(target), "offset": offset, "window_bytes": window_bytes})[0]


def publish_file(source: Path, target: Path, expected_sha256: str, *, timeout: float = 5) -> dict:
    return _request(Path(source), "publish", max_bytes=8 * 1024**3, timeout=timeout,
                    extra={"destination": str(target), "expected_sha256": expected_sha256})[0]


def _worker() -> None:
    """Private wire protocol: JSON header, then bytes only for bounded read."""
    request = json.loads(sys.stdin.buffer.read(16384))
    path = Path(request["path"])
    try:
        before = fingerprint(path)
        if before != request["expected"]:
            raise FileUnavailable("file_changed_during_read")
        if request["mode"] == "publish":
            hasher = hashlib.sha256()
            with path.open("rb") as incoming:
                for block in iter(lambda: incoming.read(1024**2), b""):
                    hasher.update(block)
            if hasher.hexdigest() != request["expected_sha256"] or fingerprint(path) != before:
                raise FileUnavailable("copy_hash_mismatch")
            target = Path(request["destination"])
            try:
                os.link(path, target, follow_symlinks=False)
            except FileExistsError:
                raise FileUnavailable("copy_target_exists") from None
            fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            sys.stdout.buffer.write(json.dumps({"fingerprint": before, "published": True}).encode() + b"\n")
            return
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (info.st_dev, info.st_ino) != (before["device"], before["inode"]):
                raise FileUnavailable("file_changed_during_read")
            if request["mode"] == "copy":
                target = Path(request["destination"])
                offset = request["offset"]
                if target.is_symlink() or not 0 <= offset <= before["bytes"]:
                    raise FileUnavailable("invalid_copy_checkpoint")
                flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                outfd = os.open(target, flags, 0o600)
                with os.fdopen(outfd, "r+b") as output:
                    if not stat.S_ISREG(os.fstat(output.fileno()).st_mode) or os.fstat(output.fileno()).st_size < offset:
                        raise FileUnavailable("invalid_copy_checkpoint")
                    output.truncate(offset); output.seek(offset); stream.seek(offset)
                    written = 0
                    while written < request["window_bytes"]:
                        block = stream.read(min(1024**2, request["window_bytes"] - written))
                        if not block:
                            break
                        output.write(block); written += len(block)
                    output.flush(); os.fsync(output.fileno())
                if fingerprint(path) != before:
                    raise FileUnavailable("file_changed_during_read")
                sys.stdout.buffer.write(json.dumps({"fingerprint": before, "offset": offset + written,
                    "complete": offset + written == before["bytes"]}).encode() + b"\n")
                return
            checksum = hashlib.sha256()
            content = bytearray()
            size = 0
            scanner = decoder = None
            checkpoint = request.get("checkpoint", {})
            offset = checkpoint.get("offset", 0)
            if request["mode"] == "guard":
                from olympus.secret_scan import StreamingSecretGuard
                decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
                if not 0 <= offset <= before["bytes"]:
                    raise FileUnavailable("invalid_guard_checkpoint")
                if offset and checkpoint.get("fingerprint") != before:
                    raise FileUnavailable("file_changed_during_read")
                # Reconstruct boundary from the immutable input, not a persisted
                # source-text fragment. Incremental decoder retains partial UTF-8.
                stream.seek(max(0, offset - 512))
                previous = decoder.decode(stream.read(offset - stream.tell()))
                scanner = StreamingSecretGuard(checkpoint.get("state"), previous_text=previous)
                stream.seek(offset)
            window = request.get("window_bytes", request["max_bytes"])
            for block in iter(lambda: stream.read(min(1024**2, window - size)), b""):
                size += len(block)
                if size > request["max_bytes"]:
                    raise FileUnavailable("file_size_limit")
                checksum.update(block)
                if request["mode"] == "read":
                    content.extend(block)
                if scanner:
                    scanner.feed(decoder.decode(block))
                    if size >= request.get("window_bytes", request["max_bytes"]):
                        break
            if scanner and offset + size == before["bytes"]:
                scanner.feed(decoder.decode(b"", final=True)); scanner.finish()
        if fingerprint(path) != before or (not scanner and size != before["bytes"]):
            raise FileUnavailable("file_changed_during_read")
        response = {"fingerprint": before, "sha256": checksum.hexdigest()}
        if scanner:
            response.update({"complete": offset + size == before["bytes"], "offset": offset + size,
                             "state": scanner.checkpoint()})
        sys.stdout.buffer.write(json.dumps(response).encode() + b"\n")
        if request["mode"] == "read":
            sys.stdout.buffer.write(content)
    except (PreservationError, OSError) as exc:
        code = str(exc) if isinstance(exc, PreservationError) else "file_read_io_error"
        sys.stdout.buffer.write(json.dumps({"error": code, "errno": getattr(exc, "errno", None)}).encode() + b"\n")


if __name__ == "__main__":
    _worker()
