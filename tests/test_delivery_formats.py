from pathlib import Path
import tempfile
import unittest

from olympus.admission import resume_models
from olympus.delivery import run_one
from olympus.delivery_formats import classify_delivery_format
from olympus.preservation import Store
from olympus.representations import representation_status


class FormatTests(unittest.TestCase):
    def test_json_is_detected_despite_text_plain_transport(self):
        result = classify_delivery_format({"title": "prompts.json", "metadata": {"media_type": "text/plain"}}, '[\n {"prompt":"text"}')
        self.assertEqual(result["format"], "structured_json")
        self.assertFalse(result["auto_profile_allowed"])

    def test_repository_dump_is_not_mistaken_for_narrative(self):
        result = classify_delivery_format({"title": "Project text", "metadata": {"upstream_repository": "repo", "upstream_commit": "commit"}},
                                          "Repository: https://example.org/repo\nCommit: abc\nREADME and code")
        self.assertEqual(result["format"], "repository_reference")

    def test_captions_markdown_links_and_extracted_html_prose_remain_narrative(self):
        for prefix in ("0:00 This source talks about useful formats.", "[00:03] This is a caption.",
                       "[Introduction](https://example.org)\nA useful article.", "# Planning\nThis document explains the workflow."):
            self.assertTrue(classify_delivery_format({"title": "article", "metadata": {"media_type": "text/html"}}, prefix)["auto_profile_allowed"])

    def test_code_csv_and_unextracted_markup_require_review(self):
        for title, prefix in (("file.py", '"""Module docs"""'), ("table.csv", "name,value\na,1"),
                              ("page", "<!DOCTYPE html><html>"), ("snippet", "```sql\nSELECT")):
            self.assertFalse(classify_delivery_format({"title": title, "metadata": {}}, prefix)["auto_profile_allowed"])

    def test_format_wait_is_visible_and_does_not_fall_back_to_whole_source(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)/"state")
            source = store.capture(source_key="json", scope="test", title="prompts.json", original=b'[{"prompt":"example"}]', text='[{"prompt":"example"}]')
            resume_models(store);store.set_setting("native_part_chars", "12000")
            class NoNative:
                def __getattr__(self, name):
                    raise AssertionError("unreviewed structured source must not contact native APIs")
            result = run_one(store, NoNative())
            self.assertEqual(result["reason"], "waiting_profile_review")
            self.assertEqual(store.receipt(source.version_id).memory, "pending")
            self.assertEqual(store.read_version(source.version_id)["original"], b'[{"prompt":"example"}]')
            public = representation_status(store, source.version_id)
            self.assertEqual(public["waiting_reason"], "waiting_profile_review")
            self.assertFalse(public["format_review"]["owner_exception_accepted"])


if __name__ == "__main__":
    unittest.main()
