"""Stage versioned revocation metadata; local staging is not remote readback."""
import json
import os
from pathlib import Path
import tempfile

from .preservation import Store, canonical, digest, guard_no_secrets, PreservationError
from .library import _publish_files


def control_payload(db) -> bytes:
    """Read the complete control state inside the caller's SQLite transaction."""
    changes = [dict(r) for r in db.execute("SELECT id,kind,source_id,version_id,replacement_id,reason,created_at FROM changes ORDER BY id")]
    sources = [dict(r) for r in db.execute("SELECT id,source_key,scope,kind,created_at,forgotten_at FROM sources ORDER BY id")]
    withdrawn = [dict(r) for r in db.execute("SELECT * FROM superseded_content ORDER BY source_id,original_sha256,text_sha256")]
    return canonical({"schema": 1, "changes": changes, "sources": sources, "superseded_content": withdrawn})


def export_control_state(store: Store, library_root: Path) -> dict:
    with store.connect(write=True) as db:
        payload = control_payload(db)
    changes = json.loads(payload)["changes"]
    guard_no_secrets(payload)
    checksum = digest(payload)
    control = library_root / "Control"
    states = control / "states"
    states.mkdir(parents=True, exist_ok=True)
    for path in (control, states):
        if path.is_symlink():
            raise PreservationError("control_directory_symlink")
    destination = states / (checksum + ".json")
    if destination.exists():
        if destination.is_symlink() or destination.read_bytes() != payload:
            raise PreservationError("control_state_conflict")
    else:
        _publish_files(states, {destination.name: payload}, strict=False)
    pointer = canonical({"schema": 1, "sha256": checksum, "changes_max_id": changes[-1]["id"] if changes else 0,
                         "state_file": "states/" + destination.name})
    latest = control / "latest.json"
    prior = store.setting("control_pointer_sha")
    if latest.exists() and (latest.is_symlink() or prior and digest(latest.read_bytes()) != prior):
        raise PreservationError("control_pointer_conflict")
    if not latest.exists() or latest.read_bytes() != pointer:
        fd, temporary = tempfile.mkstemp(prefix=".control-", dir=control)
        with os.fdopen(fd, "wb") as stream:
            stream.write(pointer); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, latest)
    store.set_setting("control_pointer_sha", digest(pointer))
    store.set_setting("control_staged_sha", checksum)
    return {"sha256": checksum, "changes": len(changes), "remote_verified": False}
