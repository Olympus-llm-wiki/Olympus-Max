from pathlib import Path
import copy
import io
import json
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from olympus.migration import build_plan, apply_plan, save_plan, package_quality, record_assessment, render_package_report
from olympus.preservation import Store, PreservationError, digest


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.engineering = self.root / "engineering"
        self.engineering.mkdir()
        (self.engineering / "README.md").write_text("Может, создадим SaaS для инженеров?")
        (self.engineering / "research.md").write_text("---\nsource: https://example.invalid/research\nstatus: active\n---\nHistorical research [data](data.json). [[Unavailable source]]")
        (self.engineering / "data.json").write_text('{"source":"synthetic-source"}')
        self.small = self.root / "small-zero"
        self.small.mkdir()
        self.inventory = self.root / "inventory.json"
        self.refresh_inventory()
        self.store = Store(self.root / "state")
        self.preview = Store(self.root / "preview")

    def tearDown(self):
        self.tmp.cleanup()

    def refresh_inventory(self, *, missing=False):
        files = list(self.engineering.iterdir())
        if missing:
            files.append(self.engineering / "missing.md")
        self.inventory.write_text(json.dumps({"rows": [
            {"id": "I001", "title": "Engineering", "locations": [str(self.engineering)]},
            {"id": "I002", "title": "Small Zero", "locations": [str(self.small)]}],
            "members": [{"id": "I001", "path": str(p)} for p in [self.engineering, *files]]}))

    def prepare(self, packages=("I001",)):
        plan = build_plan(self.inventory, self.small, self.preview, packages=packages)
        save_plan(self.store, plan)
        return plan

    def test_selected_copy_roles_links_and_idempotent_replay(self):
        plan = self.prepare()
        first = apply_plan(self.store, plan)
        self.assertEqual(len(first["results"]), 3)
        before = self.store.status()["versions"]
        second = apply_plan(self.store, plan)
        self.assertEqual(self.store.status()["versions"], before)
        self.assertEqual([r["receipt"]["version_id"] for r in first["results"]],
                         [r["receipt"]["version_id"] for r in second["results"]])
        self.assertIsNone(self.store.setting("legacy_package:I002"))
        for row in first["results"]:
            v = self.store.read_version(row["receipt"]["version_id"])
            if row["role"] == "discussion":
                self.assertEqual(row["receipt"]["memory"], "archived")
                self.assertIn("Может", v["text"])
            elif row["role"] == "synthesis":
                self.assertEqual(row["receipt"]["memory"], "pending")
                self.assertEqual(v["metadata"]["event_at"], "unset")
                self.assertEqual(len(json.loads(v["metadata"]["parent_sources"])), 1)
                self.assertEqual(json.loads(v["metadata"]["legacy_frontmatter"])["status"], "active")
                self.assertIn("wiki:Unavailable source", json.loads(v["metadata"]["provenance_gaps"]))
        self.assertTrue(package_quality(self.store, plan, "I001")["checks"]["complete_local_copy"])

    def test_modified_source_is_visible_and_previous_preview_remains(self):
        plan = self.prepare()
        (self.engineering / "research.md").write_text("changed after plan")
        run = apply_plan(self.store, plan)
        self.assertTrue(any(r.get("error") == "migration_source_changed_since_plan" for r in run["results"]))
        report = package_quality(self.store, plan, "I001")
        self.assertFalse(report["checks"]["complete_local_copy"])
        original = next(item for item in plan["items"] if item["title"] == "research")
        self.assertIn("Historical research", self.preview.read_version(original["preview_version_id"])["text"])

    def test_missing_planned_file_is_not_silently_removed_from_denominator(self):
        self.refresh_inventory(missing=True)
        plan = self.prepare()
        self.assertEqual(len(plan["items"]), 3)
        self.assertEqual(len(plan["intended_files"]["I001"]), 4)
        apply_plan(self.store, plan)
        report = package_quality(self.store, plan, "I001")
        self.assertEqual(report["counts"]["planned_files"], 4)
        self.assertEqual(report["counts"]["errors"], 1)
        self.assertFalse(report["checks"]["complete_local_copy"])

    def test_modified_or_unregistered_plan_is_rejected(self):
        plan = self.prepare()
        changed = copy.deepcopy(plan)
        changed["items"][0]["role"] = "decision"
        with self.assertRaisesRegex(PreservationError, "migration_plan_not_registered_or_changed"):
            apply_plan(self.store, changed)
        changed["plan_id"] = "../../outside"
        with self.assertRaisesRegex(PreservationError, "unsupported_migration_plan"):
            render_package_report(self.store, changed, "I001")

    def test_assessment_requires_real_evidence_and_does_not_change_rules(self):
        plan = self.prepare()
        apply_plan(self.store, plan)
        observations = [{"statement": "Complete local copies verified", "evidence": ["check:complete_local_copy"]}]
        first = record_assessment(self.store, plan, "I001", observations=observations, improvements=[])
        second = record_assessment(self.store, plan, "I001", observations=observations, improvements=[])
        self.assertEqual(first, second)
        self.assertFalse(first["automatic_policy_changes"])
        self.assertEqual(first["status"], "assessment_not_owner_decision")
        report = render_package_report(self.store, plan, "I001")
        self.assertEqual(report["receipt"]["memory"], "archived")
        self.assertIn("Это оценка, не решение владельца", Path(report["report_path"]).read_text())
        with self.assertRaisesRegex(PreservationError, "assessment_evidence_required"):
            record_assessment(self.store, plan, "I001", observations=[{"statement": "unsupported", "evidence": ["imaginary-proof"]}], improvements=[])

    def test_assessment_can_enter_learning_without_inventing_success_or_owner_review(self):
        from olympus.learning import from_migration, checked_case
        plan = self.prepare()
        apply_plan(self.store, plan)
        record_assessment(self.store, plan, "I001",
            observations=[{"statement": "Remote copies are not verified yet.", "evidence": ["check:all_remote_verified"]}],
            improvements=[{"statement": "Verify remote bytes before claiming backup coverage.", "evidence": ["check:all_remote_verified"]}])
        result = from_migration(self.store, plan, "I001", record_id="migration-case", scope="migration-test")
        self.assertEqual(result["status"], "assistant_assessment")
        self.assertEqual(result["receipt"]["memory"], "archived")
        case, _ = checked_case(self.store, result["receipt"]["version_id"])
        self.assertEqual([x["outcome"] for x in case["observations"]], ["observation", "improvement"])
        self.assertNotIn("owner_review", case)
        self.assertNotIn("benchmark", case)

    def small_fixture(self):
        def write(name, raw):
            p = self.small / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(raw)
            return p
        html = write("raw/html/lesson.html", b"<p>Original lesson</p>")
        derived = write("derived/html/lesson.md", b"Full existing extracted lesson")
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("prompts/prompt.json", '{“prompt”:“example”}')
        z1 = write("raw/archives/one.zip", archive.getvalue())
        z2 = write("raw/archives/two.zip", archive.getvalue())
        raw_prompt = write("raw/extracted-zips/one/prompts/prompt.json", '{“prompt”:“example”}'.encode())
        normalized = write("derived/prompt-library/prompt.json", b'{"prompt":"example"}')
        index = [{"source_path": str(raw_prompt.relative_to(self.small)), "source_sha256": digest(raw_prompt.read_bytes()),
                  "normalized_path": str(normalized.relative_to(self.small)), "normalized_sha256": digest(normalized.read_bytes()),
                  "repair_method": "smart-quotes"}]
        write("derived/prompt-library/index.json", json.dumps(index).encode())
        report = write("REPORT.md", b"Historical synthesis [lesson](derived/html/lesson.md).")
        cross = self.root / "cross-synthesis.md"
        cross.write_text("Mixed historical synthesis")
        sources = [{"source_path": str(p), "archived_path": str(p.relative_to(self.small)),
                    "source_sha256": digest(p.read_bytes()), "archived_sha256": digest(p.read_bytes())} for p in (html, z1, z2)]
        main = {"source_items": sources, "html_pages": [{"source_path": str(html), "archived_path": str(html.relative_to(self.small)),
                                                          "extracted_path": str(derived.relative_to(self.small))}],
                "video_transcripts": [], "normalized_prompt_library": {"index_path": "derived/prompt-library/index.json"},
                "zip_analysis": [{"source_path": str(z1), "extracted_path": "raw/extracted-zips/one", "duplicate": False},
                                 {"source_path": str(z2), "duplicate": True}],
                "analysis_artifacts": [{"path": "REPORT.md", "sha256": digest(report.read_bytes())}],
                "cross_archive_synthesis": str(cross)}
        write("manifest.json", json.dumps(main).encode())
        write("supplement-2026-09-05/manifest.json", json.dumps({"videos": [], "method": "synthetic"}).encode())

    def test_small_zero_stages_and_duplicate_archives_remain_distinct_from_lessons(self):
        self.small_fixture()
        plan = self.prepare(("I002",))
        run = apply_plan(self.store, plan)
        self.assertTrue(all(r["local_verified"] for r in run["results"]))
        zip_items = [i for i in plan["items"] if i["path"].endswith(".zip")]
        self.assertEqual(len(zip_items), 2)
        self.assertEqual(zip_items[0]["source_key"], zip_items[1]["source_key"])
        versions = {r["item_id"]: r["receipt"]["version_id"] for r in run["results"]}
        self.assertEqual(versions[zip_items[0]["item_id"]], versions[zip_items[1]["item_id"]])
        normalized = next(i for i in plan["items"] if i["path"].endswith("derived/prompt-library/prompt.json"))
        self.assertEqual(normalized["metadata"]["transformation"], "smart-quotes")
        self.assertTrue(json.loads(normalized["metadata"]["parent_sources"]))
        source = self.store.read_version(versions[normalized["item_id"]])
        self.assertEqual(source["original"], b'{"prompt":"example"}')
        self.assertEqual(self.store.receipt(source["version_id"]).memory, "archived")

    def test_supplement_uses_declared_hash_not_truncated_author_name(self):
        self.small_fixture()
        downloads = self.root / "Downloads"
        downloads.mkdir()
        video = downloads / "Course clip - GUIDE Sma.mp4"
        video.write_bytes(b"synthetic complete media")
        transcript = self.small / "supplement-2026-09-05/transcripts/clip.txt"
        transcript.parent.mkdir()
        transcript.write_text("Synthetic existing transcript")
        audio = self.small / "supplement-2026-09-05/audio/clip.m4a"
        audio.parent.mkdir()
        audio.write_bytes(b"synthetic audio")
        supplement = {"method": "synthetic", "videos": [{"original_path": str(video),
            "sha256_original": digest(video.read_bytes()), "transcript_path": str(transcript),
            "sha256_transcript": digest(transcript.read_bytes()), "audio_path": str(audio),
            "sha256_audio": digest(audio.read_bytes())}]}
        (self.small / "supplement-2026-09-05/manifest.json").write_text(json.dumps(supplement))
        with patch("pathlib.Path.home", return_value=self.root):
            plan = self.prepare(("I002",))
        self.assertTrue(any(i["path"] == str(video) for i in plan["items"]))
        self.assertFalse(plan["issues"])


if __name__ == "__main__":
    unittest.main()
