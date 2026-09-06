#!/usr/bin/env python3
"""Receive one authenticated connector result as data on stdin, never as code."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sys
import tempfile


def main():
    root = Path(sys.argv[1])
    if not root.is_absolute() or root.is_symlink():
        raise ValueError("invalid_download_root")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    line = sys.stdin.buffer.readline(16385)
    if not line.endswith(b"\n") or len(line) > 16384:
        raise ValueError("invalid_download_header")
    meta = json.loads(line)
    if (not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", meta.get("file_id", ""))
            or type(meta.get("bytes")) is not int or not 0 <= meta["bytes"] <= 32 * 1024**2
            or not re.fullmatch(r"[a-f0-9]{64}", meta.get("expected_sha256", ""))):
        raise ValueError("invalid_download_header")
    remaining = ((meta["bytes"] + 2) // 3) * 4
    fd, temporary = tempfile.mkstemp(prefix=".download-", dir=root)
    size = 0
    checksum = hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as stream:
            while remaining:
                block = sys.stdin.buffer.read(min(65536, remaining))
                if not block:
                    raise ValueError("incomplete_remote_download")
                # read() on a pipe can return short data; preserve base64 groups.
                while len(block) % 4:
                    extra = sys.stdin.buffer.read(4 - len(block) % 4)
                    if not extra:
                        raise ValueError("incomplete_remote_download")
                    block += extra
                remaining -= len(block)
                decoded = base64.b64decode(block, validate=True)
                size += len(decoded)
                checksum.update(decoded)
                stream.write(decoded)
            stream.flush()
            os.fsync(stream.fileno())
        if size != meta["bytes"] or checksum.hexdigest() != meta["expected_sha256"]:
            raise ValueError("remote_bytes_mismatch")
        target = root / meta["file_id"]
        os.replace(temporary, str(target) + ".bin")
        metadata = {**meta, "sha256": checksum.hexdigest()}
        fd, sidecar = tempfile.mkstemp(prefix=".metadata-", dir=root)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(metadata, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(sidecar, str(target) + ".json")
        finally:
            if os.path.exists(sidecar):
                os.unlink(sidecar)
        print(json.dumps({"file_id": meta["file_id"], "bytes": size, "sha256": checksum.hexdigest()}), flush=True)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    def timeout(*_):
        raise ValueError("remote_receive_timeout")
    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(90)
    try:
        main()
    except Exception as exc:
        known = {"invalid_download_root", "invalid_download_header", "incomplete_remote_download",
                 "remote_bytes_mismatch", "remote_receive_timeout"}
        code = str(exc) if str(exc) in known else "remote_receive_failed"
        print(json.dumps({"error": code}), flush=True)
        sys.exit(2)
