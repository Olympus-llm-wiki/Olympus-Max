import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from olympus.preservation import Store
from olympus.text_index import TextIndex
from olympus.search import search_corpus
from olympus.evidence import register_package
from olympus.payload_deletion import delete_archived_payloads
from olympus.withholding import eligible_versions


class TextIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def capture(self, key, text, scope='test', **kwargs):
        return self.store.capture(source_key=key, title='Neutral', scope=scope,
                                  original=text.encode(), text=text, **kwargs)

    def test_unicode_marker_crosses_chunk_boundary_with_exact_span(self):
        source = self.capture('boundary', 'я'*15996 + 'uniquemarker' + ' конец')
        result = search_corpus(self.store, 'uniquemarker')
        item = result['results'][0]
        self.assertEqual(item['version_id'], source.version_id)
        text = self.store.read_text_version(source.version_id)['text']
        anchor = item['anchor']
        self.assertEqual(text[anchor['start']:anchor['end']], item['excerpt'])
        self.assertIn('uniquemarker', item['excerpt'])

    def test_scope_filter_precedes_hit_limit(self):
        for i in range(5):
            self.capture(str(i), 'needle '*10, scope='other')
        wanted = self.capture('wanted', 'long surrounding content needle', scope='selected')
        with patch('olympus.text_index.MAX_HITS', 2):
            result = search_corpus(self.store, 'needle', scopes=['selected'])
        self.assertEqual([x['version_id'] for x in result['results']], [wanted.version_id])

    def test_large_text_hit_verifies_only_matched_span_and_detects_tampering(self):
        source = self.capture('large', 'padding '*30000 + 'uniquebodymarker' + ' tail')
        with patch.object(self.store, 'read_text_version', side_effect=AssertionError('full text reread')):
            first = search_corpus(self.store, 'uniquebodymarker')
        self.assertEqual(first['results'][0]['version_id'], source.version_id)
        path = self.store.versions/source.version_id/'text.txt'
        raw = path.read_bytes().replace(b'uniquebodymarker', b'changedtextmarkr')
        path.write_bytes(raw)
        self.assertEqual(search_corpus(self.store, 'uniquebodymarker')['results'], [])

    def test_rebuild_skips_explicitly_removed_archival_payload(self):
        archived = self.capture('removed', 'archived artifact', archive_only=True,
                                metadata={'material_role':'artifact'})
        retained = self.capture('retained', 'usable needle')
        delete_archived_payloads(self.store, [archived.version_id], owner_wording='Delete this synthetic archive')
        result = TextIndex(self.store).rebuild(reset=True)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['remaining'], 0)
        self.assertEqual([x['version_id'] for x in search_corpus(self.store,'needle')['results']], [retained.version_id])
        self.assertFalse((self.store.versions/archived.version_id/'original').exists())

    def test_corrupt_index_metadata_isolated_from_another_source(self):
        bad = self.capture('bad', 'needle corrupted index')
        good = self.capture('good', 'needle valid index')
        with self.store.connect(write=True) as db:
            db.execute('UPDATE local_text_documents SET metadata_json=? WHERE version_id=?', ('not-json',bad.version_id))
        result = search_corpus(self.store,'needle')
        self.assertEqual([x['version_id'] for x in result['results']], [good.version_id])
        self.assertEqual(result['coverage']['withheld_versions'][0]['reasons'], ['text_index_metadata_invalid'])

    def test_current_package_only_and_package_withdrawal_preserve_dependency_guard(self):
        original = self.capture('original', 'Original assertion.')
        report = self.capture('report', 'Report claim.', metadata={'material_role':'synthesis'})
        data = {'schema':1,'scope':'test','id':'package','report_version':report.version_id,
                'claims':[{'id':'claim','type':'factual','statement':'Report claim.',
                           'evidence':[{'source_version':original.version_id,'quote':'Original assertion.'}]}]}
        saved = register_package(self.store,data)
        package_id = saved['receipt']['version_id']
        with self.store.connect() as db:
            rows = {r[0] for r in db.execute('SELECT source_version FROM local_text_dependencies WHERE report_version=?',(report.version_id,))}
        self.assertEqual(rows, {original.version_id, package_id})
        self.store.forget(saved['receipt']['source_id'], 'Withdraw synthetic evidence package')
        status = eligible_versions(self.store, diagnostics=True)
        self.assertNotIn(report.version_id, status['eligible'])
        self.assertTrue(next(x for x in status['withheld'] if x['version_id']==report.version_id)['research_report'])

    def test_unknown_derivative_stays_held_after_applied_historical_correction(self):
        old = self.capture('old', 'Old basis.')
        synthesis = self.capture('unknown', 'Historical interpretation.', metadata={'material_role':'synthesis'})
        extracted = self.capture('extracted', 'Direct extracted original.', metadata={'material_role':'extracted'})
        independent = self.capture('independent', 'Independent original.')
        self.store.forget(old.source_id, 'Semantic withdrawal')
        with self.store.connect(write=True) as db:
            db.execute('UPDATE changes SET applied=1')
        reopened = Store(self.temp.name)
        status = eligible_versions(reopened, diagnostics=True)
        self.assertNotIn(synthesis.version_id, status['eligible'])
        self.assertIn(extracted.version_id, status['eligible'])
        self.assertIn(independent.version_id, status['eligible'])
        self.assertEqual(next(x for x in status['withheld'] if x['version_id']==synthesis.version_id)['reasons'],
                         ['unknown_derivative_dependencies'])

    def test_proven_archival_payload_removal_does_not_infer_semantic_withdrawal(self):
        archive = self.capture('archive', 'Opaque archive.', archive_only=True, metadata={'material_role':'artifact'})
        unknown = self.capture('unknown', 'Historical synthesis.', metadata={'material_role':'synthesis'})
        dependent = self.capture('dependent', 'Synthesis of selected archive.',
            metadata={'material_role':'synthesis', 'parent_sources':json.dumps([archive.version_id])})
        delete_archived_payloads(self.store,[archive.version_id],owner_wording='Remove website archive bytes only')
        change = self.store.pending_changes()[0]
        self.store.set_setting('correction_receipt:'+str(change['id']),json.dumps({
            'mode':'never-submitted-archive-deletion','change_ids':[change['id']],
            'document_ids':[archive.version_id],'native_document_absent':True,'native_operation_absent':True}))
        with self.store.connect(write=True) as db:
            db.execute('UPDATE changes SET applied=1')
        status=eligible_versions(self.store)
        self.assertIn(unknown.version_id,status)
        self.assertNotIn(dependent.version_id,status)


if __name__ == '__main__':
    unittest.main()
