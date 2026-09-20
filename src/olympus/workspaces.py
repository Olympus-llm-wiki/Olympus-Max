"""Prepare and resume explicit local work contexts; execution stays with the agent."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import uuid

from . import projects as p
from .preservation import PreservationError, canonical, digest, timestamp

OLYMPUS_ROOT = Path(__file__).resolve().parents[2]
MAX_CHANGED = 1000
MAX_FILE_BYTES = 8 * 1024**2
MAX_SNAPSHOT_BYTES = 128 * 1024**2


def _fingerprint(path, budget):
    """Hash bounded ordinary files; credential paths never have their body read."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"state": "missing"}
    result = {"size": info.st_size, "mtime_ns": info.st_mtime_ns, "mode": info.st_mode}
    if path.is_symlink():
        return {**result, "state": "symlink", "target_hash": digest(os.fsencode(os.readlink(path)))}
    if path.is_dir():
        return {**result, "state": "directory"}
    if not path.is_file():
        raise PreservationError("workspace_file_not_regular")
    if p.credential_path(path):
        return {**result, "state": "credential_metadata_only"}
    if info.st_size > MAX_FILE_BYTES or budget[0] + info.st_size > MAX_SNAPSHOT_BYTES:
        raise PreservationError("workspace_snapshot_limit")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(data) != info.st_size or (info.st_ino, info.st_mtime_ns) != (after.st_ino, after.st_mtime_ns):
        raise PreservationError("workspace_file_changed_during_probe")
    budget[0] += len(data)
    return {**result, "state": "file", "sha256": digest(data)}


def _scope(root, target):
    path = Path(p.string(target))
    if ".." in path.parts or ".git" in path.parts:
        raise PreservationError("workspace_target_outside_resource")
    path = path if path.is_absolute() else root / path
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or p.credential_path(path):
        raise PreservationError("workspace_target_outside_resource")
    return resolved


def _git_snapshot(root, budget):
    raw = p.git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all")
    entries = raw.split(b"\0")
    changed, i = [], 0
    while i < len(entries) and entries[i]:
        item = entries[i].decode("utf-8")
        code, name = item[:2], item[3:]
        row = {"status": code, "path": name, "fingerprint": _fingerprint(root / name, budget)}
        if "R" in code or "C" in code:
            i += 1
            row["original_path"] = entries[i].decode("utf-8")
        changed.append(row)
        if len(changed) > MAX_CHANGED:
            raise PreservationError("workspace_changed_files_limit")
        i += 1
    try:
        head = p.git(root, "rev-parse", "--verify", "HEAD").decode().strip()
    except PreservationError:
        head = None  # An unborn Git repository has no HEAD yet.
    try:
        branch = p.git(root, "symbolic-ref", "--short", "HEAD").decode().strip()
    except PreservationError:
        branch = None
    return {"head": head, "branch": branch, "changes": changed, "status_sha256": digest(raw)}


def _instructions(root, targets, extras, budget):
    dirs = {root}
    for target in targets:
        directory = target if target.is_dir() else target.parent
        dirs.update(parent for parent in [directory, *directory.parents] if parent.is_relative_to(root))
    files = []
    for directory in sorted(dirs, key=lambda d: (len(d.parts), str(d))):
        for name in ("AGENTS.override.md", "AGENTS.md"):
            path = directory / name
            if path.exists():
                fp = _fingerprint(path, budget)
                if fp["state"] != "file":
                    raise PreservationError("workspace_instruction_not_regular")
                if fp["size"]:
                    files.append({"path": str(path), "sha256": fp["sha256"], "scope": str(directory)})
                    break
    for extra in extras:
        path = p.local_path(extra)
        fp = _fingerprint(path, budget)
        if fp["state"] != "file":
            raise PreservationError("workspace_instruction_unavailable")
        if str(path) not in [row["path"] for row in files]:
            files.append({"path": str(path), "sha256": fp["sha256"], "scope": "explicit_project_pointer"})
    return files


def _tool_scopes(root):
    return [{"name": name, "bound_root": str(OLYMPUS_ROOT),
             "scope_matches": root == OLYMPUS_ROOT,
             "availability": "not_probed" if root == OLYMPUS_ROOT else "bound_to_other_project"}
            for name in ("olympus_serena", "olympus_openspec_wrapper")]


def prepare(store, project, *, goal, selection=None, intent="read", exclude_work=None):
    p.string(goal)
    if intent not in {"read", "edit"}:
        raise PreservationError("invalid_workspace_intent")
    found = p.check(store, project)
    if found["state"] != "resolved":
        return found
    resources = {r["id"]: r for r in found["record"]["resources"]}
    if selection is None:
        selection = {key: [] for key, r in resources.items() if r["status"] == "active"}
    if not isinstance(selection, dict) or not selection or selection.keys() - resources.keys():
        raise PreservationError("invalid_workspace_selection")
    observations = {r["id"]: r for r in found["resources"]}
    context, issues, budget = [], [], [0]
    for key, targets in selection.items():
        for target in p.sequence(targets):
            p.string(target)
        resource = resources[key]
        observed = observations[key]
        row = {"id": key, "role": resource["role"], "purpose": resource["purpose"],
               "status": resource["status"], "provenance": resource["provenance"],
               "issues": list(observed["issues"]), "instructions": [], "targets": [],
               "actions": [{**action, "verification": "not_executed"} for action in resource.get("actions", [])]}
        if "url" in resource:
            row.update(url=resource["url"], observation="not_fetched", execution_root=None)
            if targets:
                row["issues"].append("url_not_local_target")
        elif "observed_binding" in observed:
            root = Path(observed["observed_binding"]["real_path"])
            execution = root if root.is_dir() else root.parent
            row.update(binding=observed["observed_binding"], execution_root=str(execution))
            try:
                if root.is_file() and targets:
                    raise PreservationError("workspace_file_resource_has_targets")
                selected = [_scope(root, target) for target in targets]
                row["targets"] = [str(x) for x in selected]
                row["target_fingerprints"] = {str(x): _fingerprint(x, budget) for x in selected}
                if resource["role"] == "code":
                    git_root = Path(row["binding"]["git_root"])
                    row["git"] = _git_snapshot(git_root, budget)
                    row["instructions"] = _instructions(git_root, [root, *selected], resource.get("instructions", []), budget)
                elif targets or resource.get("instructions"):
                    row["instructions"] = _instructions(execution, selected, resource.get("instructions", []), budget)
                elif root.is_file():
                    row["fingerprint"] = _fingerprint(root, budget)
                row["tool_scopes"] = _tool_scopes(execution)
            except (OSError, UnicodeError, PreservationError) as exc:
                row["issues"].append(str(exc) if isinstance(exc, PreservationError) else "resource_probe_failed")
            if intent == "edit" and targets and resource["role"] == "reference":
                row["issues"].append("reference_not_edit_target")
        if intent == "edit" and targets and (resource["status"] != "active" or row.get("execution_root") is None):
            row["issues"].append("workspace_target_not_active")
        issues.extend({"resource": key, "code": code} for code in row["issues"])
        context.append(row)
    if intent == "edit" and not any(selection.values()):
        issues.append({"code": "workspace_edit_paths_required"})
    if found["recovery_state"] != "ready":
        issues.append({"code": "workspace_recovery_pending"})
    overlaps = []
    selected_roots = {r.get("binding", {}).get("git_root") or r.get("execution_root") for r in context} - {None}
    for work in p.records(store, "workspace"):
        item = work["record"]
        if item["id"] == exclude_work or item["status"] == "complete":
            continue
        roots = {r.get("binding", {}).get("git_root") or r.get("execution_root") for r in item["context"]["resources"]} - {None}
        if roots & selected_roots:
            overlaps.append({"work_id": item["id"], "roots": sorted(roots & selected_roots)})
    payload = {"project_id": found["record"]["id"], "project_version": found["version_id"],
               "intent": intent, "selection": selection, "resources": context,
               "requirements": found["record"]["requirements"], "issues": issues}
    p.guard_no_secrets(canonical(payload))
    return {"state": "needs_attention" if issues else "ready", "goal": goal, "prepared_at": timestamp(),
            "context": payload, "context_sha256": digest(canonical(payload)), "overlaps": overlaps,
            "instructions_require_reading": True, "commands_executed": False,
            "permissions_granted": False, "native_session_reconfigured": False}


def _thread(value):
    if value is None:
        return None
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise PreservationError("invalid_workspace_thread_id") from None
    return value


def start(store, work_id, project, *, goal, selection=None, intent="read", thread_id=None, references=None):
    p.identifier(work_id)
    _thread(thread_id)
    for reference in p.sequence(references or []):
        p.locator(reference)
    context = prepare(store, project, goal=goal, selection=selection, intent=intent, exclude_work=work_id)
    if context["state"] != "ready":
        return context
    data = {"schema": 1, "id": work_id, "project_id": context["context"]["project_id"],
            "goal": goal, "thread_id": thread_id, "status": "in-progress", "context": context["context"],
            "initial_context": context["context"], "context_sha256": context["context_sha256"],
            "references": references or [], "done": [], "pending": [goal], "next_step": goal,
            "checks": [], "artifacts": [], "commits": [], "limitations": [],
            "assessment_role": "assistant_assessment"}
    return p.write_record(store, "workspace", work_id, data, expected="new")


def list_work(store, project=None):
    if project:
        found = p.resolve(store, project)
        if found["state"] != "resolved":
            return found
        project = found["record"]["id"]
    rows = p.records(store, "workspace")
    return {"state": "listed", "workspaces": [
        {"version_id": row["version_id"], **{key: row["record"][key] for key in
         ("id", "project_id", "goal", "status", "next_step", "thread_id")}}
        for row in rows if project is None or row["record"]["project_id"] == project]}


def resume(store, work_id=None, *, project=None):
    if work_id is None:
        listed = list_work(store, project)
        if listed["state"] != "listed":
            return listed
        matches = [row for row in listed["workspaces"] if row["status"] != "complete"]
        if len(matches) != 1:
            return {"state": "ambiguous" if matches else "not_found", "candidates": matches}
        work_id = matches[0]["id"]
    saved = p.read_record(store, "workspace", work_id)
    data = saved["record"]
    if project:
        found = p.resolve(store, project)
        if found["state"] != "resolved" or found["record"]["id"] != data["project_id"]:
            raise PreservationError("workspace_project_mismatch")
    current = prepare(store, data["project_id"], goal=data["goal"], selection=data["context"]["selection"],
                      intent=data["context"]["intent"], exclude_work=work_id)
    if "context" not in current:
        return {**saved, "state": "needs_attention", "current": current, "changes": ["project_unresolved"]}
    changes = []
    if current["context"]["project_version"] != data["context"]["project_version"]:
        changes.append("project_map_changed")
    old = {r["id"]: r for r in data["context"]["resources"]}
    for row in current["context"]["resources"]:
        for key in ("binding", "git", "instructions", "targets", "target_fingerprints", "fingerprint", "issues"):
            if row.get(key) != old.get(row["id"], {}).get(key):
                changes.append(f"{row['id']}:{key}_changed")
    return {**saved, "state": "needs_review" if changes else current["state"], "changes": changes,
            "current": current, "commands_executed": False,
            "saved_summary_grants_permissions": False}


def _validate_update(update):
    p.shape(update, set(), {"done", "pending", "next_step", "references", "checks", "artifacts", "commits",
                          "limitations", "selection", "intent", "status", "thread_id"})
    for key in ("done", "pending", "references", "artifacts", "commits", "limitations"):
        if key in update:
            for value in p.sequence(update[key]):
                p.string(value)
                if key in {"references", "artifacts"}:
                    p.locator(value)
    if "next_step" in update:
        p.string(update["next_step"])
    if "thread_id" in update:
        _thread(update["thread_id"])
    if "status" in update and update["status"] not in {"in-progress", "paused", "complete"}:
        raise PreservationError("invalid_workspace_status")
    for check in p.sequence(update.get("checks", [])):
        p.shape(check, {"argv", "cwd", "exit_code", "checked_at", "git_head", "note"})
        if not p.sequence(check["argv"], 50):
            raise PreservationError("empty_workspace_command")
        for arg in check["argv"]:
            p.string(arg)
        p.local_path(check["cwd"])
        p.date(check["checked_at"])
        p.string(check["note"])
        if check["exit_code"] is not None and type(check["exit_code"]) is not int:
            raise PreservationError("invalid_workspace_exit_code")
        if check["git_head"] is not None and not isinstance(check["git_head"], str):
            raise PreservationError("invalid_workspace_git_head")
    p.guard_no_secrets(canonical(update))


def checkpoint(store, work_id, update, *, expected, finish=False):
    _validate_update(update)
    with store.exclusive():
        saved = p.read_record(store, "workspace", work_id)
        if saved["version_id"] != expected:
            raise PreservationError("workspace_version_conflict")
        data = saved["record"]
        if data["status"] == "complete":
            raise PreservationError("workspace_already_complete")
        selection = update.get("selection", data["context"]["selection"])
        intent = update.get("intent", data["context"]["intent"])
        context = prepare(store, data["project_id"], goal=data["goal"], selection=selection,
                          intent=intent, exclude_work=work_id)
        if "context" not in context:
            raise PreservationError("workspace_project_unresolved")
        for key, value in update.items():
            if key not in {"selection", "intent"}:
                data[key] = value
        if finish:
            data["status"] = "complete"
        if data["status"] == "complete":
            if data["pending"] or not data["done"] or not data["artifacts"] or not data["checks"]:
                raise PreservationError("workspace_completion_evidence_required")
            if context["state"] != "ready" or any(c["exit_code"] != 0 for c in data["checks"]):
                raise PreservationError("workspace_completion_has_unresolved_checks")
        data.update(context=context["context"], context_sha256=context["context_sha256"])
        result = p.write_record(store, "workspace", work_id, data, expected=expected)
        return {**result, "context_state": context["state"], "issues": context["context"]["issues"]}
