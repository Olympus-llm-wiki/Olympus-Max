"""Small filesystem primitives shared by the durable job implementation."""
import hashlib
import json
import os
from pathlib import Path
import tempfile


class ReelError(Exception):
    """A safe, static error code suitable for a caller or a log."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w") as target:
            target.write(canonical(value))
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp, path)
        sync_dir(path.parent)
    finally:
        Path(temp).unlink(missing_ok=True)


def read_json(path):
    return json.loads(Path(path).read_text())


def code_version():
    root = Path(__file__).parent
    return digest({p.name: file_hash(p) for p in sorted(root.glob("*.py"))})


def identifier(value, prefix):
    if not value.startswith(prefix) or len(value) > 90 or not all(c.isalnum() or c in "_-" for c in value):
        raise ReelError("invalid_identifier")
    return value
