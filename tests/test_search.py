import json
from pathlib import Path
import tempfile
import threading
import time
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
        other = self.source('lora unaffected', scope='other')
        self.store.forget(r.source_id, 'test')
        result = search_corpus(self.store, 'lora')
        self.assertEqual([x['version_id'] for x in result['results']], [other.version_id])

    def test_body_only_pending_archive_search_never_reads_large_original(self):
        ids = set()
        for archive in (False, True):
            r = self.store.capture(source_key=str(archive), scope='test', title='Neutral',
                original=b'opaque-binary'*10000, text='Rarebodymarker', archive_only=archive)
            ids.add(r.version_id)
        with (patch.object(self.store, 'read_version', side_effect=AssertionError('binary read in search')),
              patch('olympus.search.MAX_VERSION_BYTES', 32)):
            result = search_corpus(self.store, 'Rarebodymarker')
        self.assertEqual({x['version_id'] for x in result['results']}, ids)
        self.assertEqual(result['coverage']['skipped_version_count'], 0)

    def test_native_deadline_is_one_global_budget_and_preserves_local(self):
        source = self.source('deadline marker')
        self.store.update_delivery(self.store.claim(), 'searchable', units=1)
        for scope in ['a','b']:
            self.source('deadline marker '+scope, scope=scope)
            self.store.update_delivery(self.store.claim(), 'searchable', units=1)
        release = threading.Event()
        calls=[]
        class Client:
            def recall(self, *args):
                calls.append(args)
                release.wait(1)
                return {'results': [], 'chunks': {}}
        started = time.monotonic()
        try:
            result = search_corpus(self.store, 'deadline', client=Client(), native_deadline_seconds=.04)
            elapsed = time.monotonic() - started
        finally:
            release.set()
        self.assertLess(elapsed, .5)
        self.assertIn(source.version_id, {r['version_id'] for r in result['results']})
        self.assertEqual(len(calls), 1)
        self.assertEqual(sum(x['state']=='deadline_exceeded' for x in result['coverage']['semantic']), 2)
        self.assertIn('deadline_exceeded', [x.get('reason') for x in result['coverage']['semantic']])

    def test_package_source_read_does_not_hold_writer_lock(self):
        from olympus.evidence import register_package
        original = self.source('Original assertion.', scope='test')
        report = self.source('Needle report.', scope='test', role='synthesis')
        package = register_package(self.store, {'schema':1,'scope':'test','id':'package','report_version':report.version_id,
            'claims':[{'id':'claim','type':'factual','statement':'Needle report.',
                       'evidence':[{'source_version':original.version_id,'quote':'Original assertion.'}]}]})
        vid = package['receipt']['version_id']
        reading, release, captured = threading.Event(), threading.Event(), threading.Event()
        real_read = self.store.read_version
        errors = []
        def blocked_read(value):
            if value == vid:
                reading.set()
                release.wait(2)
            return real_read(value)
        def search():
            try: search_corpus(self.store, 'Needle')
            except Exception as exc: errors.append(exc)
        def capture():
            try: self.source('Concurrent capture.'); captured.set()
            except Exception as exc: errors.append(exc)
        with patch.object(self.store, 'read_version', side_effect=blocked_read):
            worker = threading.Thread(target=search); worker.start()
            self.assertTrue(reading.wait(1))
            writer = threading.Thread(target=capture); writer.start()
            progressed = captured.wait(1)
            release.set(); writer.join(2); worker.join(2)
        self.assertTrue(progressed, 'source verification blocked unrelated durable capture')
        self.assertEqual(errors, [])

    def test_package_dependency_withdrawal_keeps_unrelated_original_visible(self):
        from olympus.evidence import register_package
        original = self.source('Needle original.', scope='test')
        report = self.source('Needle report.', scope='test', role='synthesis')
        unrelated = self.source('Needle unaffected.', scope='other')
        register_package(self.store, {'schema':1,'scope':'test','id':'package','report_version':report.version_id,
            'claims':[{'id':'claim','type':'factual','statement':'Needle report.',
                       'evidence':[{'source_version':original.version_id,'quote':'Needle original.'}]}]})
        self.store.forget(original.source_id, 'test withdrawal')
        result = search_corpus(self.store, 'Needle')
        self.assertEqual({x['version_id'] for x in result['results']}, {unrelated.version_id})

    def test_package_change_after_citation_check_invalidates_final_result(self):
        from olympus.evidence import register_package, report_links
        original = self.source('Original assertion.', scope='test')
        report = self.source('Needle report.', scope='test', role='synthesis')
        data = {'schema':1,'scope':'test','id':'package','report_version':report.version_id,
            'claims':[{'id':'claim','type':'factual','statement':'Needle report.',
                       'evidence':[{'source_version':original.version_id,'quote':'Original assertion.'}]}]}
        register_package(self.store, data)
        def changed_after_read(store, ids, scope):
            links = report_links(store, ids, scope)
            data['claims'][0]['evidence'][0]['quote'] = 'Absent quotation.'
            register_package(store,data)
            return links
        with patch('olympus.search.report_links', side_effect=changed_after_read):
            result = search_corpus(self.store,'Needle')
        self.assertEqual(result['results'], [])
        self.assertIn(report.version_id, result['coverage']['withheld_reports'])

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

    def test_natural_question_keeps_short_answer_above_generic_long_documents(self):
        useful=self.source('During experimentation a creator should use one to three content formats.',scope='creators')
        market=self.source('Total addressable market is the total revenue opportunity if a business captures the entire market.',scope='business')
        for number in range(30):
            self.source('How many content formats should a creator choose during experimentation? '
                        'How is total addressable market defined in the source? '
                        + 'Unrelated background discussion. '*200 + str(number),scope='generic')
        formats=search_corpus(self.store,'How many content formats should a creator choose during experimentation?')
        tam=search_corpus(self.store,'How is total addressable market defined in the source?')
        self.assertIn(useful.version_id,{row['version_id']for row in formats['results']})
        self.assertIn(market.version_id,{row['version_id']for row in tam['results']})
        self.assertIn('one to three',next(row['excerpt']for row in formats['results']if row['version_id']==useful.version_id))

    def test_excerpt_covers_answer_terms_instead_of_first_alphabetic_match(self):
        body='Butter automatically finds top performing content across each Instagram account and repurposes it with metadata changes. '
        body+='Background filler. '*50+' Instagram accounts appear here. '+'Later unrelated material. '*100
        source=self.source(body)
        result=search_corpus(self.store,'What does Butter automatically find and repurpose across Instagram accounts?')
        excerpt=next(row['excerpt']for row in result['results']if row['version_id']==source.version_id)
        self.assertIn('top performing content',excerpt)
        self.assertIn('metadata changes',excerpt)
