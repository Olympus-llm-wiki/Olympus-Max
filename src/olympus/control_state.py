"""Stage versioned revocation metadata; local staging is not remote readback."""
import json
import os
from pathlib import Path
import tempfile

from .preservation import Store, canonical, digest, guard_no_secrets, PreservationError
from .library import _publish_files, _lock, _root, _directory
from .bounded_io import read_file


def control_payload(db) -> bytes:
    """Read the complete control state inside the caller's SQLite transaction."""
    changes = [dict(r) for r in db.execute("SELECT id,kind,source_id,version_id,replacement_id,reason,created_at FROM changes ORDER BY id")]
    sources = [dict(r) for r in db.execute("SELECT id,source_key,scope,kind,created_at,forgotten_at FROM sources ORDER BY id")]
    withdrawn = [dict(r) for r in db.execute("SELECT * FROM superseded_content ORDER BY source_id,original_sha256,text_sha256")]
    return canonical({"schema": 1, "changes": changes, "sources": sources, "superseded_content": withdrawn})


def export_control_state(store: Store, library_root: Path, *, progress=None) -> dict:
    root = _root(library_root, create=True)
    if root == store.root or root in store.root.parents or store.root in root.parents:
        raise PreservationError("library_and_state_must_be_separate")
    with _lock(store, "control:" + str(root)):
        return _export_control_state(store, root, progress=progress)


def _export_control_state(store: Store, library_root: Path, *, progress=None) -> dict:
    def fence():
        if progress:
            progress({"state": "publishing_control"})
    fence()
    with store.connect(write=True) as db:
        payload = control_payload(db)
    changes = json.loads(payload)["changes"]
    guard_no_secrets(payload)
    checksum = digest(payload)
    control = _directory(library_root, "Control")
    states = _directory(control, "states")
    destination = states / (checksum + ".json")
    if destination.exists():
        if destination.is_symlink() or read_file(destination, max_bytes=max(len(payload), 8 * 1024**2)) != payload:
            raise PreservationError("control_state_conflict")
    else:
        _publish_files(states, {destination.name: payload}, strict=False, fence=fence)
    pointer = canonical({"schema": 1, "sha256": checksum, "changes_max_id": changes[-1]["id"] if changes else 0,
                         "state_file": "states/" + destination.name})
    latest = control / "latest.json"
    prior = store.setting("control_pointer_sha")
    observed_pointer = read_file(latest, max_bytes=4096) if latest.exists() else None
    if latest.exists() and (latest.is_symlink() or prior and digest(observed_pointer) != prior):
        raise PreservationError("control_pointer_conflict")
    if observed_pointer != pointer:
        fence()
        fd, temporary = tempfile.mkstemp(prefix=".control-", dir=control)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(pointer); stream.flush(); os.fsync(stream.fileno())
            fence()
            os.replace(temporary, latest)
        finally:
            Path(temporary).unlink(missing_ok=True)
    fence()
    store.set_setting("control_pointer_sha", digest(pointer))
    store.set_setting("control_staged_sha", checksum)
    return {"sha256": checksum, "changes": len(changes), "remote_verified": False}
