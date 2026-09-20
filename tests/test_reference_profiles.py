from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from olympus.admission import resume_models
from olympus.delivery import run_one
from olympus.preservation import Store,PreservationError,canonical,digest
from olympus.reference_profiles import (activate_reference_profiles,processing_profile,
    processing_profile_summary,native_not_required_ids,PROFILE,PREFIX,_binding)
import test_references as fixtures
from test_continuous_delivery import Native


class ReferenceProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name)
        text='[{"id":17,"title":"Reference entry","content":"Exact value"}]'
        self.source=self.store.capture(source_key='reference',scope='test',title='reference.json',original=text.encode(),text=text)
        self.wording='Да, точный локальный профиль'

    def approval(self,ids=None):
        binding=_binding(ids or [self.source.version_id])
        text=self.wording+'\n'+canonical(binding).decode()
        return self.store.capture(source_key='decision'+digest(text.encode()),scope='test',title='Owner decision',
            original=text.encode(),text=text,kind='owner-decision',archive_only=True,
            metadata={'material_role':'decision','decision_status':'confirmed','decision_scope':'exact profile versions',
                'owner_confirmed_at':'2026-09-09T00:00:00Z','owner_confirmation_evidence':self.wording,
                'profile_approval':canonical(binding).decode(),'profile_approval_binding':digest(canonical(binding))})

    def activate(self):
        self.decision=self.approval()
        return activate_reference_profiles(self.store,[self.source.version_id],self.decision.version_id,self.wording)

    def test_selected_profile_completes_without_changing_delivery_history(self):
        before=asdict(self.store.receipt(self.source.version_id));result=self.activate()
        self.assertEqual(result['state'],'profile_complete')
        self.assertEqual(before,asdict(self.store.receipt(self.source.version_id)))
        view=processing_profile(self.store,self.source.version_id)
        self.assertEqual(view['native_requirement'],'not_required_by_selected_profile')
        self.assertEqual(view['unit_count'],1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM settings WHERE key LIKE ?',('processing_profile:%',)).fetchone()[0],1)

    def test_exact_binding_rejects_subset_or_unconfirmed_decision(self):
        other=self.store.capture(source_key='other',scope='test',title='other',original=b'[]',text='[]')
        approval=self.approval([self.source.version_id,other.version_id])
        with self.assertRaisesRegex(PreservationError,'approval_mismatch'):
            activate_reference_profiles(self.store,[self.source.version_id],approval.version_id,self.wording)
        fake=self.store.capture(source_key='fake',scope='test',title='fake',original=self.wording.encode(),text=self.wording)
        with self.assertRaises(PreservationError):activate_reference_profiles(self.store,[self.source.version_id],fake.version_id,self.wording)
        self.assertFalse(processing_profile(self.store,self.source.version_id)['selected'])

    def test_scheduler_skips_valid_profile_and_keeps_next_source_fair(self):
        self.activate();resume_models(self.store)
        text='Next narrative source'
        other=self.store.capture(source_key='other',scope='test',title='other',original=text.encode(),text=text)
        native=Native();result=run_one(self.store,native)
        self.assertEqual(result['version_id'],other.version_id)
        self.assertEqual(native.submissions[0]['document_id'],other.version_id)
        self.assertEqual(self.store.receipt(self.source.version_id).memory,'pending')
        explicit=run_one(self.store,native,version_id=self.source.version_id)
        self.assertEqual(explicit['state'],'pending')
        self.assertEqual(explicit['processing_profile']['state'],'profile_complete')

    def test_original_mutation_invalidates_without_rehashing_or_native_complete(self):
        self.activate();path=self.store.versions/self.source.version_id/'original'
        path.write_bytes(path.read_bytes()+b' ')
        view=processing_profile(self.store,self.source.version_id)
        self.assertEqual(view['state'],'profile_invalid');self.assertEqual(native_not_required_ids(self.store),set())

    def test_index_body_tampering_and_missing_span_invalidate(self):
        self.activate()
        with self.store.connect(write=True) as db:
            db.execute('UPDATE local_text_fts SET body=? WHERE version_id=?',('tampered',self.source.version_id))
        self.assertEqual(processing_profile(self.store,self.source.version_id)['state'],'profile_invalid')

    def test_activation_requires_all_exact_textindex_spans(self):
        decision=self.approval()
        with self.store.connect(write=True) as db:db.execute('DELETE FROM local_text_spans WHERE version_id=?',(self.source.version_id,))
        with self.assertRaises(PreservationError):activate_reference_profiles(self.store,[self.source.version_id],decision.version_id,self.wording)
        self.assertIsNone(self.store.setting(PREFIX+self.source.version_id))

    def test_revoked_source_or_approval_invalidates(self):
        self.activate();self.store.forget(self.decision.source_id,'synthetic revoke approval')
        self.assertEqual(processing_profile(self.store,self.source.version_id)['reason'],'reference_profile_dependency_withheld')

    def test_parser_version_change_and_receipt_corruption_are_fail_closed(self):
        self.activate()
        with patch('olympus.reference_profiles.REFERENCE_SCHEMA','future-parser'):
            self.assertEqual(processing_profile(self.store,self.source.version_id)['state'],'profile_invalid')
        self.store.set_setting(PREFIX+self.source.version_id,'{}')
        self.assertEqual(processing_profile(self.store,self.source.version_id)['state'],'profile_invalid')

    def test_native_started_cannot_be_relabelled_not_required(self):
        decision=self.approval();job=self.store.claim();self.store.reserve_delivery_attempt(job)
        with self.assertRaisesRegex(PreservationError,'native_history_requires_review'):
            activate_reference_profiles(self.store,[self.source.version_id],decision.version_id,self.wording)

    def test_large_multispan_reference_certifies_every_span(self):
        text=json.dumps([{'title':'large','content':'x'*17000},{'title':'end','content':'z'*20000}])
        self.source=self.store.capture(source_key='large',scope='test',title='large.json',original=text.encode(),text=text)
        result=self.activate();self.assertEqual(result['receipts'][0]['index']['span_count'],3)

    def test_archive_dependency_mutation_and_revocation_are_not_false_complete(self):
        fixture=fixtures.ReferenceTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.store=fixture.store;self.source,archive,_=fixture.repo_source();self.activate()
        self.assertEqual(processing_profile(self.store,self.source.version_id)['state'],'profile_complete')
        self.store.forget(archive.source_id,'synthetic archive withdrawal')
        self.assertEqual(processing_profile(self.store,self.source.version_id)['state'],'profile_invalid')

    def test_fast_status_does_not_read_full_canonical_source(self):
        self.activate()
        with patch('pathlib.Path.read_bytes',side_effect=AssertionError('fullsource status read')):
            self.assertEqual(processing_profile_summary(self.store)['counts'],{'profile_complete':1})

    def test_corruption_after_full_index_scan_invalidates_same_public_read(self):
        self.activate()
        import olympus.reference_profiles as profiles
        original=profiles._index_proof
        def corrupt(*args,**kwargs):
            result=original(*args,**kwargs)
            with self.store.connect(write=True) as db:db.execute('UPDATE local_text_fts SET body=? WHERE version_id=?',('changed after check',self.source.version_id))
            return result
        with patch('olympus.reference_profiles._index_proof',side_effect=corrupt):
            view=processing_profile(self.store,self.source.version_id)
        self.assertEqual(view['state'],'profile_invalid')
        self.assertEqual(view['reason'],'reference_profile_snapshot_changed')

    def test_unrelated_heartbeat_after_index_scan_does_not_invalidate_profile(self):
        self.activate()
        import olympus.reference_profiles as profiles
        original=profiles._index_proof
        def heartbeat(*args,**kwargs):
            result=original(*args,**kwargs)
            self.store.set_setting('heartbeat:delivery','2026-09-09T15:00:00Z')
            return result
        with patch('olympus.reference_profiles._index_proof',side_effect=heartbeat):
            view=processing_profile(self.store,self.source.version_id)
        self.assertEqual(view['state'],'profile_complete')

    def test_unrelated_capture_during_activation_keeps_exact_approved_profile(self):
        decision=self.approval()
        import olympus.reference_profiles as profiles
        original=profiles._index_proof
        calls=[]
        def capture(*args,**kwargs):
            result=original(*args,**kwargs)
            calls.append(1)
            self.store.capture(source_key='unrelated-'+str(len(calls)),scope='other',title='unrelated',
                               original=b'independent capture',text='independent capture')
            return result
        with patch('olympus.reference_profiles._index_proof',side_effect=capture):
            result=activate_reference_profiles(self.store,[self.source.version_id],decision.version_id,self.wording)
        self.assertEqual(result['state'],'profile_complete')

    def test_changed_selected_index_does_not_invalidate_other_selected_source(self):
        text='[{"id":18,"title":"Other","content":"Independent"}]'
        other=self.store.capture(source_key='second',scope='test',title='other.json',original=text.encode(),text=text)
        decision=self.approval([self.source.version_id,other.version_id])
        activate_reference_profiles(self.store,[self.source.version_id,other.version_id],decision.version_id,self.wording)
        import olympus.reference_profiles as profiles
        original=profiles._index_proof
        def corrupt(*args,**kwargs):
            result=original(*args,**kwargs)
            if args[1]==self.source.version_id:
                with self.store.connect(write=True) as db:
                    db.execute('UPDATE local_text_fts SET body=? WHERE version_id=?',('x'*17000,self.source.version_id))
            return result
        with patch('olympus.reference_profiles._index_proof',side_effect=corrupt):
            rows={r['version_id']:r for r in processing_profile_summary(self.store)['versions']}
        self.assertEqual(rows[self.source.version_id]['state'],'profile_invalid')
        self.assertEqual(rows[other.version_id]['state'],'profile_complete')

    def test_actual_index_hash_does_not_hold_writer_transaction(self):
        self.activate()
        import olympus.reference_profiles as profiles
        original=profiles.digest
        writes=[]
        def hashing(raw):
            if b'Exact value' in raw:
                self.store.set_setting('independent_writer_progress','yes')
                writes.append(True)
            return original(raw)
        with patch('olympus.reference_profiles.digest',side_effect=hashing):
            view=processing_profile(self.store,self.source.version_id)
        self.assertTrue(writes)
        self.assertEqual(view['state'],'profile_complete')

    def test_selected_invalid_receipt_still_blocks_native_without_full_audit(self):
        self.activate();self.store.set_setting(PREFIX+self.source.version_id,'{}');resume_models(self.store)
        with patch('olympus.reference_profiles._index_proof',side_effect=AssertionError('scheduler must not scan index')):
            self.assertIsNone(self.store.claim(version_id=self.source.version_id))
            self.assertEqual(run_one(self.store,Native())['state'],'idle')
        self.assertEqual(processing_profile(self.store,self.source.version_id)['state'],'profile_invalid')

    def test_activation_between_scheduler_preflight_and_claim_cannot_submit(self):
        decision=self.approval();resume_models(self.store);original=self.store.claim
        def activated(**kwargs):
            activate_reference_profiles(self.store,[self.source.version_id],decision.version_id,self.wording)
            return original(**kwargs)
        native=Native()
        with patch.object(self.store,'claim',side_effect=activated):
            self.assertEqual(run_one(self.store,native)['state'],'idle')
        self.assertEqual(native.submissions,[])

    def test_out_of_band_native_attempt_invalidates_completion(self):
        self.activate()
        with self.store.connect(write=True) as db:db.execute('UPDATE delivery SET attempts=1 WHERE version_id=?',(self.source.version_id,))
        view=processing_profile(self.store,self.source.version_id)
        self.assertEqual(view['state'],'profile_invalid')
        self.assertEqual(view['reason'],'reference_profile_native_history_requires_review')

    def test_expired_claim_cannot_reserve_after_profile_activation(self):
        decision=self.approval();job=self.store.claim(version_id=self.source.version_id)
        with self.store.connect(write=True) as db:
            db.execute('UPDATE delivery SET lease_until=0 WHERE version_id=?',(self.source.version_id,))
        activate_reference_profiles(self.store,[self.source.version_id],decision.version_id,self.wording)
        self.assertFalse(self.store.reserve_delivery_attempt(job))
        self.assertEqual(processing_profile(self.store,self.source.version_id)['state'],'profile_complete')

    def test_archive_mutation_between_parser_and_receipt_anchor_is_rejected(self):
        fixture=fixtures.ReferenceTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.store=fixture.store;self.source,archive,_=fixture.repo_source();decision=self.approval()
        import olympus.reference_profiles as profiles
        original=profiles.reference
        def changed(*args,**kwargs):
            result=original(*args,**kwargs)
            (self.store.versions/archive.version_id/'original').write_bytes(b'tampered after reference read')
            return result
        with patch('olympus.reference_profiles.reference',side_effect=changed):
            with self.assertRaisesRegex(PreservationError,'hash_mismatch'):
                activate_reference_profiles(self.store,[self.source.version_id],decision.version_id,self.wording)
        self.assertIsNone(self.store.setting(PREFIX+self.source.version_id))
