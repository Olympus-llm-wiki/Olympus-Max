import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from olympus.preservation import Store, PreservationError
from olympus.hindsight import HindsightError
from olympus.search import search_corpus


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def source(self, text, scope='ugc', role='primary', archive=False):
        return self.store.capture(source_key=text, scope=scope, title=text, text=text,
                                  original=text.encode(), metadata={'material_role': role}, archive_only=archive)

    def catalog(self):
        rows = [dict(id='I002', title='Small Zero AI Influencers', category='UGC и производство', description='Уроки ComfyUI по AI-инфлюенсерам'),
                dict(id='I019', title='LoRAtech / ComfyUI Hub', category='UGC и производство', description='Видео о LoRA и ComfyUI'),
                dict(id='I020', title='OFM OnlyFans', category='OFM', description='Подкасты')]
        r = self.store.capture(source_key='catalog', scope='legacy-catalog', title='catalog', original=json.dumps({'rows': rows}).encode(), text='', archive_only=True, metadata={'material_role': 'catalog'})
        self.store.set_setting('legacy_catalog', json.dumps({'version_id': r.version_id}))

    def test_original_question_discovers_neighbors_without_author_hints(self):
        self.catalog()
        r = self.source('Identity LoRA training dataset with different clothing', archive=True)
        result = search_corpus(self.store, 'Вот у нас был корпус Онлифанса. Рассказывали про генерацию изображений livestyle, купальники, косплей?')
        self.assertTrue({'I002', 'I019'} <= {c['id'] for c in result['catalog']})
        self.assertIn(r.version_id, {m['version_id'] for m in result['results']})
        self.assertEqual(result['results'][0]['delivery_state'], 'archived')
        self.assertFalse(result['coverage']['absence_proven'])

    def test_scope_is_explicit_and_pending_is_readable(self):
        a = self.source('lora dataset'); self.source('lora dataset elsewhere', 'other')
        result = search_corpus(self.store, 'лора', scopes=['ugc'])
        self.assertEqual([r['version_id'] for r in result['results']], [a.version_id])
        self.assertEqual(result['results'][0]['delivery_state'], 'pending')

    def test_roles_are_separated(self):
        self.source('lora discussion', role='discussion')
        self.source('lora decision', role='decision')
        self.assertEqual(search_corpus(self.store, 'lora')['results'], [])
        result = search_corpus(self.store, 'lora', include_discussions=True)
        self.assertEqual(len(result['discussions']), 1)
        self.assertEqual(result['results'], [])

    def test_corruption_is_a_gap(self):
        r = self.source('lora')
        (self.store.versions / r.version_id / 'text.txt').write_text('changed')
        result = search_corpus(self.store, 'lora')
        self.assertEqual(result['results'], [])
        self.assertEqual(len(result['coverage']['skipped_versions']), 1)

    def test_recovery_and_correction_barriers(self):
        r = self.source('lora')
        self.store.set_setting('recovery_state', 'pending')
        with self.assertRaises(PreservationError): search_corpus(self.store, 'lora')
        self.store.set_setting('recovery_state', 'ready')
        self.store.forget(r.source_id, 'test')
        with self.assertRaises(PreservationError): search_corpus(self.store, 'lora')

    def test_native_failure_does_not_hide_local(self):
        r = self.source('lora')
        self.store.update_delivery(self.store.claim(), 'searchable', units=1)
        class Client:
            def recall(self, *args): raise HindsightError('network_error')
        result = search_corpus(self.store, 'lora', client=Client())
        self.assertEqual(result['results'][0]['version_id'], r.version_id)
        self.assertEqual(result['coverage']['semantic'][0]['state'], 'unavailable')

    def test_revoked_during_native_query_is_not_returned(self):
        r = self.source('lora')
        self.store.update_delivery(self.store.claim(), 'searchable', units=1)
        store = self.store
        class Client:
            def recall(self, *args):
                store.forget(r.source_id, 'test revoke')
                # Simulate completed reconciliation before final read.
                with store.connect(write=True) as db: db.execute('UPDATE changes SET applied=1')
                return {'results': [{'document_id': r.version_id, 'text': 'lora', 'chunk_id': 'c'}], 'chunks': {'c': {'text': 'lora'}}}
        result = search_corpus(store, 'lora', client=Client())
        self.assertEqual(result['results'], [])
        self.assertEqual(result['semantic_results'][0]['results'], [])
        self.assertEqual(result['semantic_results'][0]['chunks'], {})

    def test_size_limit_is_visible(self):
        self.source('lora')
        with patch('olympus.search.MAX_SCAN_BYTES', 1):
            result = search_corpus(self.store, 'lora')
        self.assertEqual(result['coverage']['skipped_versions'][0]['reason'], 'byte_limit')

    def test_held_synthesis_does_not_bypass_evidence(self):
        r = self.source('lora synthesis', role='synthesis')
        with patch('olympus.search.report_links', return_value={r.version_id: [{'state': 'held'}]}):
            result = search_corpus(self.store, 'lora')
        self.assertEqual(result['results'], [])
        self.assertIn(r.version_id, result['coverage']['withheld_reports'])

    def test_package_filters_both_local_and_catalog(self):
        self.catalog()
        a = self.source('lora dataset', 'legacy'); self.source('lora elsewhere')
        self.store.set_setting('legacy_package:I002', json.dumps({'version_ids': [a.version_id]}))
        result = search_corpus(self.store, 'lora', package_id='I002')
        self.assertEqual([r['version_id'] for r in result['results']], [a.version_id])
        self.assertEqual([c['id'] for c in result['catalog']], ['I002'])
        self.assertEqual(search_corpus(self.store, 'lora', scopes=['ugc'])['catalog'], [])

    def test_native_search_covers_scopes_and_removes_foreign_results(self):
        a = self.source('lora a', 'a'); b = self.source('lora b', 'b')
        for _ in range(2): self.store.update_delivery(self.store.claim(), 'searchable', units=1)
        calls = []
        class Client:
            def recall(self, query, tags):
                calls.append(tags)
                vid = a.version_id if 'scope:a' in tags else b.version_id
                return {'results': [{'document_id': vid, 'chunk_id': vid, 'text': 'lora'},
                                    {'document_id': 'foreign', 'chunk_id': 'foreign', 'text': 'bad'}],
                        'chunks': {vid: {'text': 'lora'}, 'foreign': {'text': 'bad'}}}
        result = search_corpus(self.store, 'lora', client=Client())
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(result['semantic_results']), 2)
        self.assertTrue(all(len(r['results']) == 1 for r in result['semantic_results']))
        limited = search_corpus(self.store, 'lora', client=Client(), max_semantic_scopes=1)
        self.assertIn('scope_limit', [c['state'] for c in limited['coverage']['semantic']])

    def test_non_visual_expansion_and_empty_query(self):
        self.source('audio transcript extraction', 'media')
        result = search_corpus(self.store, 'расшифровка')
        self.assertEqual(len(result['results']), 1)
        self.assertEqual(result['expansion']['groups'], ['speech'])
        with self.assertRaises(PreservationError): search_corpus(self.store, '  ')
