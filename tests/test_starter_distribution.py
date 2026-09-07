import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/verify-starter-distribution.py"
if not SCRIPT.exists():
    SCRIPT = ROOT / "scripts/verify-distribution.py"
spec = importlib.util.spec_from_file_location("distribution_verifier", SCRIPT)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.skill = self.root / ".agents/skills/example/SKILL.md"
        self.skill.parent.mkdir(parents=True)
        self.skill.write_text("synthetic skill")
        (self.root / ".claude").mkdir()
        (self.root / ".claude/skills").symlink_to("../.agents/skills")
        self.relative = self.skill.relative_to(self.root).as_posix()
        (self.root / "DISTRIBUTION.json").write_text(json.dumps({"schema": 1, "files": {
            self.relative: hashlib.sha256(self.skill.read_bytes()).hexdigest()}}))

    def tearDown(self):
        self.temp.cleanup()

    def test_pristine_and_python_cache(self):
        cache = self.skill.parent / "__pycache__"
        cache.mkdir(); (cache / "example.pyc").write_bytes(b"synthetic")
        self.assertEqual(verifier.verify(self.root)["state"], "verified")

    def test_changed_missing_and_added_files_are_distinct(self):
        self.skill.write_text("changed")
        self.assertEqual(verifier.verify(self.root)["issues"][0]["code"], "changed")
        self.skill.unlink()
        extra = self.skill.parent / "instructions.md"; extra.write_text("extra")
        self.assertEqual({i["code"] for i in verifier.verify(self.root)["issues"]},
                         {"missing", "unexpected_skill_file"})

    def test_symlink_cannot_substitute_a_packaged_file(self):
        other = self.root / "outside.txt"; other.write_text("synthetic skill")
        self.skill.unlink(); self.skill.symlink_to(other)
        with self.assertRaisesRegex(ValueError, "symlink"):
            verifier.verify(self.root)

    def test_manifest_cannot_read_outside_checkout(self):
        (self.root / "DISTRIBUTION.json").write_text(json.dumps({"schema": 1, "files": {"../private": "x"}}))
        with self.assertRaisesRegex(ValueError, "invalid_manifest_path"):
            verifier.verify(self.root)

    def test_claude_link_must_stay_relative_and_internal(self):
        (self.root / ".claude/skills").unlink()
        (self.root / ".claude/skills").symlink_to(self.root / ".agents/skills")
        self.assertEqual(verifier.verify(self.root)["issues"][0]["code"], "claude_skill_link_changed")
