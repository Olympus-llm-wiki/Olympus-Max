"""Local project maps and versioned pointers; never execute commands from a map."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from urllib.parse import urlsplit

from .preservation import Store, PreservationError, canonical, digest, guard_no_secrets, timestamp

MAX_BYTES = 512 * 1024
MAX_RECORDS = 1000
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,79}\Z")
VERSION = re.compile(r"olv-[a-f0-9]{64}\Z")
ROLES = {"code", "knowledge", "design", "reference"}
STATUSES = {"candidate", "active", "retired"}


def shape(data, required, optional=()):
    if not isinstance(data, dict) or not set(required) <= data.keys() or data.keys() - set(required) - set(optional):
        raise PreservationError("invalid_workspace_shape")


def string(value, limit=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise PreservationError("invalid_workspace_string")
    return value


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise PreservationError("invalid_workspace_id")
    return value


def sequence(value, limit=100):
    if not isinstance(value, list) or len(value) > limit:
        raise PreservationError("invalid_workspace_list")
    return value


def date(value):
    try:
        datetime.fromisoformat(string(value).replace("Z", "+00:00"))
    except ValueError:
        raise PreservationError("invalid_workspace_date") from None


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise PreservationError("duplicate_workspace_key")
        result[key] = value
    return result


def load_input(path):
    """Read one bounded regular JSON file, without following a final symlink."""
    fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
            raise PreservationError("workspace_input_not_regular_or_too_large")
        raw = stream.read(MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(raw) != before.st_size or (before.st_ino, before.st_mtime_ns) != (after.st_ino, after.st_mtime_ns):
        raise PreservationError("workspace_input_changed")
    guard_no_secrets(raw)
    try:
        data = json.loads(raw, object_pairs_hook=_pairs)
    except (ValueError, RecursionError):
        raise PreservationError("invalid_workspace_json") from None
    if not isinstance(data, dict):
        raise PreservationError("invalid_workspace_shape")
    return data


def locator(value):
    value = string(value)
    guard_no_secrets(value.encode())
    if value.startswith(("http://", "https://")):
        url = urlsplit(value)
        if not url.hostname or url.username or url.password:
            raise PreservationError("invalid_workspace_locator")
    return value


def local_path(value):
    path = Path(string(value)).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise PreservationError("workspace_absolute_path_required")
    return path


def credential_path(path):
    return any(p in {"auth.json", "credentials.json", ".env"} or p.startswith(".env.") or
               p.endswith((".key", ".pem")) for p in Path(path).parts)


def git(path, *args):
    # Inherited Git selectors must not silently redirect an explicit target.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(["git", "-C", str(path), *args], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        raise PreservationError("workspace_git_unavailable") from None
    if result.returncode:
        raise PreservationError("workspace_git_probe_failed")
    if len(result.stdout) > 4 * 1024**2:
        raise PreservationError("workspace_git_output_too_large")
    return result.stdout


def identity(path, *, code=False):
    path = local_path(str(path))
    try:
        real = path.resolve(strict=True)
        info = real.stat()
    except OSError:
        raise PreservationError("workspace_path_unavailable") from None
    if not (real.is_dir() or real.is_file()) or credential_path(real):
        raise PreservationError("workspace_resource_not_supported")
    value = {"real_path": str(real), "device": info.st_dev, "inode": info.st_ino}
    if code:
        root = Path(git(real, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        common = Path(git(real, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()).resolve()
        cstat = common.stat()
        value.update(git_root=str(root), git_common_dir=str(common),
                     git_device=cstat.st_dev, git_inode=cstat.st_ino)
    return value


def validate_project(data):
    shape(data, {"schema", "id", "name", "aliases", "resources", "requirements"})
    if type(data["schema"]) is not int or data["schema"] != 1:
        raise PreservationError("unsupported_workspace_schema")
    identifier(data["id"])
    string(data["name"], 160)
    aliases = [string(a, 160).casefold() for a in sequence(data["aliases"])]
    if len(set(aliases)) != len(aliases):
        raise PreservationError("duplicate_workspace_alias")
    for value in sequence(data["requirements"]):
        locator(value)
    ids = []
    for resource in sequence(data["resources"]):
        shape(resource, {"id", "role", "purpose", "status", "provenance"},
              {"path", "url", "binding", "actions", "instructions"})
        ids.append(identifier(resource["id"]))
        string(resource["purpose"])
        if resource["role"] not in ROLES or resource["status"] not in STATUSES:
            raise PreservationError("invalid_workspace_resource_status")
        if ("path" in resource) == ("url" in resource):
            raise PreservationError("workspace_one_locator_required")
        if "path" in resource:
            local_path(resource["path"])
            if credential_path(resource["path"]):
                raise PreservationError("workspace_credential_resource")
        else:
            if not locator(resource["url"]).startswith(("https://", "http://")) or resource["role"] == "code":
                raise PreservationError("workspace_local_code_required")
        provenance = resource["provenance"]
        shape(provenance, {"source", "checked_at", "basis"})
        locator(provenance["source"])
        date(provenance["checked_at"])
        string(provenance["basis"])
        # Binding is generated on registration, never trusted from input.
        if "binding" in resource and not isinstance(resource["binding"], dict):
            raise PreservationError("invalid_workspace_binding")
        for extra in sequence(resource.get("instructions", [])):
            local_path(extra)
        for action in sequence(resource.get("actions", [])):
            shape(action, {"name", "argv", "source"})
            identifier(action["name"])
            locator(action["source"])
            if not sequence(action["argv"], 50):
                raise PreservationError("empty_workspace_command")
            for arg in action["argv"]:
                string(arg)
    if len(ids) != len(set(ids)):
        raise PreservationError("duplicate_workspace_resource")
    guard_no_secrets(canonical(data))
    if len(canonical(data)) > MAX_BYTES:
        raise PreservationError("workspace_record_too_large")
    return data


def _key(kind, record_id):
    if kind not in {"project", "workspace"}:
        raise PreservationError("invalid_workspace_record_kind")
    return f"routing.{kind}.{identifier(record_id)}"


def read_record(store, kind, record_id, *, version=None):
    version = version or store.setting(_key(kind, record_id))
    if not version or not VERSION.fullmatch(version):
        raise PreservationError("workspace_record_not_found")
    for name in ("original", "text.txt"):
        if (store.versions / version / name).stat().st_size > MAX_BYTES:
            raise PreservationError("workspace_record_too_large")
    saved = store.read_version(version)
    if saved["source_key"] != _key(kind, record_id) or saved["metadata"].get("artifact_type") != kind:
        raise PreservationError("workspace_record_identity_mismatch")
    with store.connect() as db:
        row = db.execute("SELECT v.active,s.forgotten_at FROM versions v JOIN sources s ON s.id=v.source_id WHERE v.id=?", (version,)).fetchone()
    if not row or not row["active"] or row["forgotten_at"]:
        raise PreservationError("workspace_record_withdrawn")
    data = json.loads(saved["original"])
    shape(data, {"schema", "id", "kind", "previous_version", "payload"})
    if data["schema"] != 1 or data["kind"] != kind or data["id"] != record_id:
        raise PreservationError("workspace_record_identity_mismatch")
    guard_no_secrets(canonical(data))
    return {"version_id": version, "previous_version": data["previous_version"], "record": data["payload"],
            "receipt": asdict(store.receipt(version))}


def write_record(store, kind, record_id, payload, *, expected):
    """CAS under the existing process lock; an interrupted capture is not latest."""
    key = _key(kind, record_id)
    if expected != "new" and (not isinstance(expected, str) or not VERSION.fullmatch(expected)):
        raise PreservationError("workspace_expected_version_required")
    guard_no_secrets(canonical(payload))
    with store.exclusive():
        current = store.setting(key)
        previous = read_record(store, kind, record_id) if current else None
        if previous and previous["record"] == payload and expected in {current, previous["previous_version"] or "new"}:
            return previous
        if current != (None if expected == "new" else expected):
            raise PreservationError("workspace_version_conflict")
        body = canonical({"schema": 1, "id": record_id, "kind": kind, "previous_version": current, "payload": payload})
        if len(body) > MAX_BYTES:
            raise PreservationError("workspace_record_too_large")
        receipt = store.capture(source_key=key, scope="project-workspaces", title=f"{kind}: {record_id}",
                                original=body, text=body.decode(), archive_only=True,
                                metadata={"material_role": "artifact", "artifact_type": kind})
        store.set_setting(key, receipt.version_id)
        return read_record(store, kind, record_id)


def records(store, kind):
    _key(kind, "check")
    with store.connect() as db:
        rows = db.execute("SELECT key FROM settings WHERE key LIKE ? ORDER BY key LIMIT ?",
                          (f"routing.{kind}.%", MAX_RECORDS + 1)).fetchall()
    if len(rows) > MAX_RECORDS:
        raise PreservationError("workspace_registry_limit")
    return [read_record(store, kind, row["key"].split(".", 2)[2]) for row in rows]


def register(store, data, *, expected="new"):
    data = json.loads(canonical(validate_project(data)))
    for resource in data["resources"]:
        resource.pop("binding", None)
        if "path" in resource and resource["status"] == "active":
            resource["binding"] = identity(resource["path"], code=resource["role"] == "code")
    return write_record(store, "project", data["id"], data, expected=expected)


def resolve(store, query):
    query = string(query, 160).strip().casefold()
    items = records(store, "project")
    exact = [row for row in items if row["record"]["id"] == query]
    matches = exact or [row for row in items if query in [row["record"]["name"].casefold(),
                                                       *[a.casefold() for a in row["record"]["aliases"]]]]
    if len(matches) != 1:
        return {"state": "ambiguous" if matches else "not_found", "candidates": [
            {"id": row["record"]["id"], "name": row["record"]["name"]} for row in matches]}
    return {"state": "resolved", **matches[0]}


def check(store, query):
    result = resolve(store, query)
    if result["state"] != "resolved":
        return result
    observed = []
    for resource in result["record"]["resources"]:
        row = {"id": resource["id"], "role": resource["role"], "status": resource["status"], "issues": []}
        if "url" in resource:
            row.update(url=resource["url"], observation="not_fetched")
        else:
            try:
                now = identity(resource["path"], code=resource["role"] == "code")
                row["observed_binding"] = now
                if resource.get("binding") != now:
                    row["issues"].append("binding_changed" if resource.get("binding") else "binding_unconfirmed")
            except (OSError, PreservationError):
                row["issues"].append("resource_unavailable")
        if resource["status"] != "active":
            row["issues"].append("resource_not_active")
        observed.append(row)
    return {**result, "checked_at": timestamp(), "resources": observed,
            "recovery_state": store.setting("recovery_state", "ready"),
            "execution_performed": False}
