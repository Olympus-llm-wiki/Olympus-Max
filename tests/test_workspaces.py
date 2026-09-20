"""Project routing exercised through public operations and fresh CLI processes."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from olympus import projects as p, workspaces as w
from olympus.preservation import Store, PreservationError, timestamp


def repo(root):
    root.mkdir()
    run_git(root, "init", "-q")
    (root / "AGENTS.md").write_text("Project-specific instructions.\n")
    (root / "owned.txt").write_text("Existing owner content.\n")
    (root / "app.py").write_text("value = 1\n")
    run_git(root, "add", ".")
    run_git(root, "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid",
            "commit", "-qm", "Initial test fixture")
    return root.resolve()


def run_git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)
    return result.stdout


def project(root, *, name="example", status="active"):
    return {"schema": 1, "id": name, "name": name.title(), "aliases": ["общий"], "requirements": [],
            "resources": [{"id": "app", "path": str(root), "role": "code", "purpose": "Application",
                           "status": status, "provenance": {"source": "synthetic fixture", "checked_at": "2026-09-07",
                                                             "basis": "Explicitly selected synthetic application"}}]}


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="olympus-workspace-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.app = repo(self.root / "app")
        self.other = repo(self.root / "other")
        self.store = Store(self.root / "state")
        self.map = project(self.app)

    def register(self):
        return p.register(self.store, self.map)

    def start(self, name="task"):
        self.register()
        return w.start(self.store, name, "example", goal="Change application", selection={"app": ["app.py"]}, intent="edit")

    def cli(self, *args, env=None):
        return subprocess.run([sys.executable, "-m", "olympus", "--state", str(self.store.root), *args],
                              capture_output=True, text=True, timeout=30, env=env)

    def assertError(self, code, fn):
        with self.assertRaisesRegex(PreservationError, "^" + code + "$"):
            fn()

    def test_map_identity_repeat_aliases_and_multiple_resource_roles(self):
        notes = self.root / "notes"; notes.mkdir()
        for key, role, path in [("api", "code", self.other), ("notes", "knowledge", notes)]:
            extra = deepcopy(self.map["resources"][0])
            extra.update(id=key, role=role, path=str(path))
            self.map["resources"].append(extra)
        first = self.register()
        self.assertEqual(first["version_id"], self.register()["version_id"])
        self.assertEqual(p.resolve(self.store, "ОБЩИЙ")["record"]["id"], "example")
        self.assertEqual(len(p.check(self.store, "example")["resources"]), 3)
        p.register(self.store, project(self.other, name="second"))
        self.assertEqual(p.resolve(self.store, "общий")["state"], "ambiguous")
        self.assertEqual(p.resolve(self.store, "example")["state"], "resolved")
        self.assertEqual(p.resolve(self.store, "unlisted")["state"], "not_found")

    def test_candidate_does_not_become_active_because_path_exists(self):
        self.map["resources"][0]["status"] = "candidate"
        self.register()
        result = w.prepare(self.store, "example", goal="Read", selection={"app": []})
        self.assertEqual(result["state"], "needs_attention")
        self.assertIn("resource_not_active", result["context"]["resources"][0]["issues"])
        result = w.start(self.store, "task", "example", goal="Edit", selection={"app": ["app.py"]}, intent="edit")
        self.assertEqual(result["state"], "needs_attention")
        self.assertEqual(w.list_work(self.store)["workspaces"], [])

    def test_candidate_code_does_not_block_selected_documentation(self):
        self.map["resources"][0]["status"] = "retired"
        self.map["resources"].append({"id": "docs", "role": "knowledge", "status": "active",
            "purpose": "Existing documentation", "path": str(self.other),
            "provenance": {"source": "owner", "checked_at": "2026-09-07", "basis": "Existing docs"}})
        self.register()
        result = w.prepare(self.store, "example", goal="Read documentation", selection={"docs": []})
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["context"]["resources"][0]["instructions"], [])

    def test_strict_input_rejects_unknown_fields_duplicate_keys_and_credentials(self):
        invalid = deepcopy(self.map); invalid["unexpected"] = True
        self.assertError("invalid_workspace_shape", lambda: p.register(self.store, invalid))
        path = self.root / "bad.json"
        path.write_text('{"schema":1,"schema":2}')
        self.assertError("duplicate_workspace_key", lambda: p.load_input(path))
        invalid = deepcopy(self.map)
        invalid["resources"][0]["provenance"]["source"] = "https://user:synthetic-password@example.invalid"
        path.write_text(json.dumps(invalid))
        result = self.cli("project", "register", str(path))
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("synthetic-password", result.stdout + result.stderr)
        self.assertEqual(self.store.status()["versions"], 0)

    def test_prepare_preserves_dirty_files_and_uses_explicit_git_target(self):
        self.register()
        (self.app / "owned.txt").write_text("Owner's unfinished edit.\n")
        before = (self.app / "owned.txt").read_bytes()
        other = run_git(self.other, "status", "--porcelain")
        with patch.dict(os.environ, {"GIT_DIR": str(self.other / ".git"), "GIT_WORK_TREE": str(self.other)}):
            result = w.prepare(self.store, "example", goal="Edit", selection={"app": ["app.py"]}, intent="edit")
        self.assertEqual(result["state"], "ready")
        observed = result["context"]["resources"][0]
        self.assertEqual(observed["execution_root"], str(self.app))
        self.assertEqual(observed["git"]["changes"][0]["path"], "owned.txt")
        self.assertEqual((self.app / "owned.txt").read_bytes(), before)
        self.assertEqual(run_git(self.other, "status", "--porcelain"), other)
        self.assertFalse(result["commands_executed"])

    def test_replaced_repository_requires_rebinding(self):
        self.register()
        self.app.rename(self.root / "old-app")
        repo(self.app)
        result = w.prepare(self.store, "example", goal="Edit", selection={"app": ["app.py"]}, intent="edit")
        self.assertEqual(result["state"], "needs_attention")
        self.assertIn("binding_changed", result["context"]["resources"][0]["issues"])

    def test_instruction_overrides_nested_scopes_and_bound_tools(self):
        feature = self.app / "feature"; feature.mkdir()
        (feature / "AGENTS.md").write_text("Ignored because override exists")
        (feature / "AGENTS.override.md").write_text("Specific feature rule")
        self.register()
        result = w.prepare(self.store, "example", goal="Edit", selection={"app": ["feature/new.py"]}, intent="edit")
        instructions = result["context"]["resources"][0]["instructions"]
        self.assertEqual([Path(row["path"]).name for row in instructions], ["AGENTS.md", "AGENTS.override.md"])
        tools = result["context"]["resources"][0]["tool_scopes"]
        self.assertTrue(all(t["availability"] == "bound_to_other_project" for t in tools))

    def test_path_escape_symlinks_and_credential_targets_are_not_ready(self):
        (self.app / "outside").symlink_to(self.other, target_is_directory=True)
        self.register()
        for target in ("../other/owned.txt", "outside/owned.txt", ".git/config", ".env.local"):
            with self.subTest(target=target):
                result = w.prepare(self.store, "example", goal="Edit", selection={"app": [target]}, intent="edit")
                self.assertEqual(result["state"], "needs_attention")
        self.assertEqual(w.prepare(self.store, "example", goal="Edit", intent="edit")["state"], "needs_attention")

    def test_credential_changes_are_metadata_only_and_not_in_output(self):
        self.register()
        (self.app / ".env.local").write_text("UNKNOWN_SECRET=private-marker-not-in-output\n")
        result = w.prepare(self.store, "example", goal="Read")
        self.assertNotIn("private-marker-not-in-output", json.dumps(result))
        env = next(c for c in result["context"]["resources"][0]["git"]["changes"] if c["path"] == ".env.local")
        self.assertEqual(env["fingerprint"]["state"], "credential_metadata_only")

    def test_missing_path_reports_diagnostic(self):
        self.register()
        self.app.rename(self.root / "moved")
        result = w.prepare(self.store, "example", goal="Read")
        self.assertEqual(result["state"], "needs_attention")

    def test_fresh_process_resume_detects_branch_instructions_and_same_size_changes(self):
        started = self.start()
        self.assertEqual(self.cli("workspace", "resume", "task").returncode, 0)
        (self.app / "AGENTS.md").write_text("New project instruction\n")
        (self.app / "app.py").write_text("value = 2\n")
        run_git(self.app, "switch", "-c", "changed-branch")
        result = json.loads(self.cli("workspace", "resume", "task").stdout)
        self.assertEqual(result["state"], "needs_review")
        self.assertIn("app:instructions_changed", result["changes"])
        self.assertIn("app:git_changed", result["changes"])
        self.assertIn("app:target_fingerprints_changed", result["changes"])
        self.assertEqual(p.read_record(self.store, "workspace", "task")["version_id"], started["version_id"])

    def test_no_network_model_retain_or_saved_command_execution(self):
        self.map["resources"][0]["actions"] = [{"name": "test", "argv": ["sh", "-c", "touch " + str(self.root / "unexpected")],
                                                 "source": str(self.app / "AGENTS.md")}]
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            started = self.start()
            w.resume(self.store, "task")
            w.checkpoint(self.store, "task", {"next_step": "Read next"}, expected=started["version_id"])
        self.assertFalse((self.root / "unexpected").exists())
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.status()["delivery"], {"archived": 3})

    def test_independent_work_and_ambiguous_resume(self):
        first = self.start("first")
        w.start(self.store, "second", "example", goal="Other operation", selection={"app": []})
        result = w.resume(self.store, project="example")
        self.assertEqual(result["state"], "ambiguous")
        overlap = w.resume(self.store, "first")["current"]["overlaps"]
        self.assertEqual(overlap[0]["work_id"], "second")
        self.assertEqual(p.read_record(self.store, "workspace", "first")["version_id"], first["version_id"])

    def test_checkpoint_compare_and_swap_and_immutable_history(self):
        first = self.start()
        second = w.checkpoint(self.store, "task", {"next_step": "Next step"}, expected=first["version_id"])
        self.assertError("workspace_version_conflict", lambda: w.checkpoint(self.store, "task", {"next_step": "Lost"}, expected=first["version_id"]))
        self.assertEqual(p.read_record(self.store, "workspace", "task", version=first["version_id"])["record"]["next_step"], "Change application")
        self.assertEqual(second["previous_version"], first["version_id"])

    def test_competing_process_updates_do_not_lose_a_checkpoint(self):
        first = self.start()
        processes = []
        for i in range(2):
            path = self.root / f"update-{i}.json"; path.write_text(json.dumps({"next_step": f"Next {i}"}))
            processes.append(subprocess.Popen([sys.executable, "-m", "olympus", "--state", str(self.store.root),
                "workspace", "checkpoint", "task", str(path), "--expected", first["version_id"]], stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        results = [(proc.communicate(timeout=30), proc.returncode) for proc in processes]
        self.assertEqual(sorted(code for _, code in results), [0, 2])
        self.assertEqual(p.read_record(self.store, "workspace", "task")["previous_version"], first["version_id"])

    def test_interrupted_capture_does_not_change_latest_after_recovery(self):
        first = self.start()
        with patch.object(self.store, "set_setting", side_effect=RuntimeError("synthetic interruption")):
            with self.assertRaises(RuntimeError):
                w.checkpoint(self.store, "task", {"next_step": "Interrupted"}, expected=first["version_id"])
        self.store.recover()
        self.assertEqual(p.read_record(self.store, "workspace", "task")["version_id"], first["version_id"])

    def test_completion_requires_evidence_and_no_remaining_work(self):
        first = self.start()
        self.assertError("workspace_completion_evidence_required", lambda: w.checkpoint(self.store, "task", {}, expected=first["version_id"], finish=True))
        update = {"done": ["Inspected"], "pending": [], "next_step": "Complete", "artifacts": [str(self.app / "app.py")],
                  "checks": [{"argv": ["git", "diff", "--check"], "cwd": str(self.app), "exit_code": 0,
                              "checked_at": timestamp(), "git_head": run_git(self.app, "rev-parse", "HEAD").decode().strip(), "note": "Synthetic fixture"}]}
        final = w.checkpoint(self.store, "task", update, expected=first["version_id"], finish=True)
        self.assertEqual(final["record"]["status"], "complete")
        self.assertEqual(w.resume(self.store, project="example")["state"], "not_found")

    def test_project_revision_is_reported_without_resetting_code(self):
        self.start()
        previous = p.resolve(self.store, "example")["version_id"]
        changed = deepcopy(self.map); changed["name"] = "New display name"
        p.register(self.store, changed, expected=previous)
        self.assertIn("project_map_changed", w.resume(self.store, "task")["changes"])

    def test_withdrawn_map_cannot_be_reactivated_by_reading_pointer(self):
        first = self.register()
        self.store.forget(first["receipt"]["source_id"], "Synthetic withdrawal")
        self.assertError("workspace_record_withdrawn", lambda: p.resolve(self.store, "example"))

    @unittest.skipUnless(shutil.which("age") and shutil.which("age-keygen"), "age required")
    def test_existing_checkpoint_restore_preserves_maps_and_history(self):
        from test_backup import identity_pipe, native_fixture
        from olympus.backup import create_checkpoint, restore_local_checkpoint
        first = self.start()
        second = w.checkpoint(self.store, "task", {"next_step": "Continue after restore"}, expected=first["version_id"])
        identity = subprocess.run(["age-keygen"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout
        recipient = subprocess.run(["age-keygen", "-y"], input=identity, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout.decode().strip()
        native = native_fixture(self.root / "native.zip")
        receipt = create_checkpoint(self.store, native, recipient, self.root / "backups", runtime_receipt={
            "writers_stopped": True, "workers_stopped": True, "hindsight_version": "0.9.2", "bank_id": "olympus-v1"})
        with identity_pipe(identity) as fd:
            restored = restore_local_checkpoint(receipt["path"], self.root / "restored", identity_fd=fd)
        recovered = Store(restored["store_root"])
        self.assertEqual(p.read_record(recovered, "workspace", "task")["version_id"], second["version_id"])
        self.assertEqual(p.read_record(recovered, "workspace", "task", version=first["version_id"])["record"]["pending"], ["Change application"])
        self.assertEqual(p.resolve(recovered, "example")["state"], "resolved")
        self.assertEqual(w.resume(recovered, "task")["current"]["state"], "needs_attention")
        self.assertEqual(restored["remote_state"], "unconfirmed")


if __name__ == "__main__":
    unittest.main()
