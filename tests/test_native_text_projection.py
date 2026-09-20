"""Canonical/source proof versus the pinned native sanitizer representation."""
import json
from pathlib import Path
import tempfile
import unittest

from olympus.admission import resume_models
from olympus.delivery import run_one
from olympus.hindsight import HindsightClient
from olympus.native_proof import CompletionProofs
from olympus.native_text_projection import project_text, validate_projection, MAX_REMOVED_SPANS
from olympus.preservation import Store, PreservationError, canonical, digest
from olympus.representations import Representations, native_document_map
from test_representations import PROFILE


class SanitizingNative(HindsightClient):
    """Deterministic transport using the real adapter, no native/model execution."""
    def __init__(self):
        super().__init__('http://127.0.0.1:1', 'fixture')
        self.documents, self.operations, self.submissions = {}, {}, []

    def retain_profile(self, strategy=None):
        return dict(PROFILE)

    def _request(self, method, path, body=None):
        if method == 'POST' and path == '/memories':
            item = body['items'][0]
            self.submissions.append(body)
            self.documents[item['document_id']] = item['content']
            self.operations[body['operation_id']] = item['document_id']
            return {'success': True, 'async': True, 'bank_id': self.bank_id,
                    'operation_id': body['operation_id'], 'items_count': 1}
        if path.startswith('/operations/'):
            operation = path.rsplit('/', 1)[1]
            if operation not in self.operations:
                return {'operation_id': operation, 'status': 'not_found'}
            return {'operation_id': operation, 'status': 'completed', 'result_metadata': {
                    'document_id': self.operations[operation], 'extraction_errors_count': 0, 'unit_ids_count': 1}}
        if path.startswith('/documents/'):
            doc = path.rsplit('/', 1)[1]
            if doc not in self.documents:
                from olympus.hindsight import HindsightError
                raise HindsightError('http_error', 404)
            return {'id': doc, 'bank_id': self.bank_id, 'original_text': self.documents[doc], 'memory_unit_count': 1}
        if path.startswith('/memories/list?'):
            from urllib.parse import parse_qs, urlsplit
            doc = parse_qs(urlsplit(path).query)['document_id'][0]
            return {'total': 1, 'items': [{'id': 'fixture-fact', 'document_id': doc}]}
        raise AssertionError((method, path))


class NativeProjectionTests(unittest.TestCase):
    def test_ascii_table_preserves_tab_lf_cr_deliberately(self):
        text = ''.join(chr(i) for i in range(128)) + ' Русский🙂\u00a0\u200b'
        kept = ''.join(chr(i) for i in [9, 10, 13, *range(32, 127)]) + ' Русский🙂\u00a0\u200b'
        native, proof = project_text(text)
        self.assertEqual(native, kept)
        self.assertEqual(proof['removed_chars'], 30)
        self.assertEqual(proof['removed_bytes'], 30)
        self.assertEqual(proof['canonical_sha256'], digest(text.encode()))
        validate_projection(proof, digest(text.encode()))

    def test_offsets_reconstruct_multibyte_canonical_without_guessing(self):
        text = 'Я🙂\f\fX\x01Y\n \f'
        native, proof = project_text(text)
        self.assertEqual(native, 'Я🙂XY\n ')
        restored = native
        for span in reversed(proof['removed_spans']):
            offset = span['native_char_start']
            removed = chr(span['codepoint']) * (span['char_end'] - span['char_start'])
            restored = restored[:offset] + removed + restored[offset:]
            self.assertEqual(text.encode()[span['byte_start']:span['byte_end']], removed.encode())
        self.assertEqual(restored, text)
        self.assertEqual(proof['removed_spans'][0]['byte_start'], 6)
        self.assertEqual(proof['removed_spans'][0]['char_start'], 2)

    def test_mapping_budget_is_explicit_and_long_runs_are_coalesced(self):
        native, proof = project_text('x' + '\f' * 100000)
        self.assertEqual(native, 'x')
        self.assertEqual(len(proof['removed_spans']), 1)
        with self.assertRaisesRegex(PreservationError, 'native_projection_mapping_limit'):
            project_text('x\f' * (MAX_REMOVED_SPANS + 1))
        with self.assertRaisesRegex(PreservationError, 'invalid_canonical_text'):
            project_text('invalid\ud800')

    def test_projection_counter_tampering_is_rejected(self):
        _, proof = project_text('Я\fX')
        for key in ('native_chars', 'native_bytes', 'removed_chars', 'removed_bytes'):
            with self.subTest(key=key), self.assertRaisesRegex(PreservationError, 'native_projection_proof_invalid'):
                validate_projection({**proof, key: proof[key] + 1}, proof['canonical_sha256'])

    def test_completed_legacy_pdf_retains_canonical_anchor_and_explicit_native_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)); resume_models(store)
            text = 'Exact PDF content\n\f'
            source = store.capture(source_key='pdf', scope='fixture', title='PDF', original=b'fixture original', text=text)
            client = SanitizingNative()
            # Existing operation/document from before the adapter fix.
            client.operations[source.operation_id] = source.version_id
            client.documents[source.version_id] = text[:-1]
            with store.connect(write=True) as db:
                db.execute("UPDATE delivery SET state='submitted',attempts=1 WHERE version_id=?", (source.version_id,))
            before = (store.versions/source.version_id/'manifest.json').read_bytes()
            self.assertEqual(run_one(store, client, version_id=source.version_id)['state'], 'searchable')
            self.assertEqual(client.submissions, [])
            self.assertEqual(store.read_text_version(source.version_id)['text'], text)
            self.assertEqual((store.versions/source.version_id/'manifest.json').read_bytes(), before)
            with store.connect() as db:
                row = db.execute('SELECT * FROM native_completion_receipts WHERE operation_id=?', (source.operation_id,)).fetchone()
                proof = json.loads(row['proof_json'])
            self.assertEqual(proof['text_sha256'], digest(text.encode()))
            self.assertFalse(proof['readback']['canonical_text_matches'])
            self.assertEqual(proof['readback']['native_text_projection']['removed_spans'][0]['char_start'], len(text)-1)
            self.assertEqual(proof['readback']['native_text_sha256'], digest(text[:-1].encode()))
            self.assertEqual(native_document_map(store, [source.version_id])[source.version_id]['native_text_projection'],
                             proof['readback']['native_text_projection'])
            # An unanchored zero counter must not hide the required map.
            proof['readback']['native_text_projection']['removed_chars'] = 0
            with store.connect(write=True) as db:
                db.execute('UPDATE native_completion_receipts SET proof_json=?', (canonical(proof).decode(),))
            with self.assertRaisesRegex(PreservationError, 'native_completion_receipt_mismatch'):
                native_document_map(store, [source.version_id])
            proof['readback']['native_text_projection']['removed_chars'] = 1
            # Re-signing internally inconsistent counters still cannot certify it.
            proof['readback']['native_text_projection']['removed_chars'] += 1
            with store.connect(write=True) as db:
                encoded = canonical(proof)
                db.execute('UPDATE native_completion_receipts SET proof_json=?,proof_sha256=?', (encoded.decode(), digest(encoded)))
            with self.assertRaisesRegex(PreservationError, 'native_projection_proof_invalid'):
                CompletionProofs(store).verified(version_id=source.version_id, document_id=source.version_id,
                    operation_id=source.operation_id, text_sha256=digest(text.encode()), profile=row['profile_fingerprint'])

    def test_parts_submit_and_readback_share_projection_without_changing_offsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)); resume_models(store)
            text = 'First\f page. Next\f page.'
            source = store.capture(source_key='parts', scope='fixture', title='Parts', original=b'original', text=text)
            reps = Representations(store)
            plan = reps.enable(reps.prepare(source.version_id, profile=PROFILE, max_part_chars=16)['id'])
            client = SanitizingNative()
            for _ in range(20):
                result = run_one(store, client, version_id=source.version_id)
                if result['state'] == 'searchable':
                    break
                with store.connect(write=True) as db:
                    db.execute('UPDATE delivery SET next_attempt=0')
                    db.execute('UPDATE native_representation_parts SET next_attempt=0')
            self.assertEqual(result['state'], 'searchable')
            self.assertEqual(''.join(body['items'][0]['content'] for body in client.submissions), text.replace('\f',''))
            self.assertEqual(reps.get(plan['id'])['manifest'], plan['manifest'])
            self.assertEqual(store.read_text_version(source.version_id)['text'], text)
