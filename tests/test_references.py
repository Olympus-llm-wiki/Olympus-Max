import io
import json
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch

from olympus.cli import main
from olympus.evidence import register_package
from olympus.preservation import Store, PreservationError, digest
from olympus.references import reference, ReferenceStore


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name)

    def capture(self,key,text,**kwargs):
        return self.store.capture(source_key=key,scope='reference-fixture',title=key,
            text=text,original=kwargs.pop('original',text.encode()),**kwargs)

    def json_source(self):
        records=[{'id':17,'title':'Начало','content':'Код ЛАЗУРЬ-91','flag':False,'media':['one','two']},
                 {'id':17,'title':'Middle needle','content':'English exact quote'},
                 {'id':99,'title':'End anchor','content':'尾部 \"literal\"'}]
        text=json.dumps(records,ensure_ascii=False,indent=2)
        return self.capture('json',text),records,text

    def repo_source(self):
        files={'first.txt':'Russian текст\n===== FILE: invented.txt =====\nnot a boundary\n',
               'empty.txt':'','last.ts':'export const exactEnd = 731;\n','image.png':b'\x89PNG\xff'}
        output=io.BytesIO()
        with tarfile.open(fileobj=output,mode='w') as tar:
            for name,value in files.items():
                raw=value.encode() if isinstance(value,str) else value
                member=tarfile.TarInfo(name);member.size=len(raw);tar.addfile(member,io.BytesIO(raw))
        raw=output.getvalue();meta={'upstream_repository':'https://example.invalid/repo','upstream_commit':'a'*40,'archive_sha256':digest(raw)}
        archive=self.capture('repo:archive','',original=raw,metadata=meta,archive_only=True)
        text='Repository: https://example.invalid/repo\nCommit: '+'a'*40+'\nFull UTF-8 text files; binary files are preserved in archive.\n\n'
        for name,value in files.items():
            if isinstance(value,str):text+='\n===== FILE: '+name+' =====\n'+value+'\n'
        source=self.capture('repo:text',text,metadata={**meta,'material_role':'extracted'})
        return source,archive,files

    def test_json_read_preserves_every_field_bytes_and_unicode_anchors(self):
        r,records,text=self.json_source();before=asdict(self.store.receipt(r.version_id))
        for i,expected in enumerate(records):
            row=reference(self.store,r.version_id,'read',pointer='/'+str(i))['results'][0]
            self.assertEqual(row['record'],expected)
            anchor=row['anchor'];raw=text.encode()[anchor['byte_start']:anchor['byte_start']+anchor['byte_length']]
            self.assertEqual(raw.decode(),row['raw']);self.assertEqual(digest(raw),anchor['sha256'])
            self.assertEqual(text[anchor['start']:anchor['end']],row['raw'])
        self.assertEqual(before,asdict(self.store.receipt(r.version_id)))

    def test_public_list_search_read_use_no_native_client(self):
        r,records,text=self.json_source()
        for args in [('list','--offset','1','--limit','1'),('search','Middle needle'),('read','--pointer','/1')]:
            output=io.StringIO()
            with redirect_stdout(output),patch('olympus.cli._client',side_effect=AssertionError('native called')):
                self.assertEqual(main(['--state',str(self.store.root),'reference',args[0],r.version_id,*args[1:]]),0)
            result=json.loads(output.getvalue());self.assertEqual(result['results'][0]['selector'],{'pointer':'/1'})
            self.assertFalse(result['profile_selected'])

    def test_cli_does_not_bootstrap_schema_and_bounds_writer_fence(self):
        r,_,_=self.json_source()
        with patch('olympus.cli.Store',side_effect=AssertionError('schema bootstrap')):
            output=io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(['--state',str(self.store.root),'reference','list',r.version_id]),0)
        with self.store.exclusive():
            started=__import__('time').monotonic()
            with self.assertRaisesRegex(PreservationError,'reference_time_budget_exceeded'):
                reference(ReferenceStore(self.store.root,.03),r.version_id,'list')
            self.assertLess(__import__('time').monotonic()-started,.5)

    def test_readonly_registry_cannot_write(self):
        store=ReferenceStore(self.store.root)
        with self.assertRaisesRegex(PreservationError,'reference_store_read_only'):
            with store.connect(write=True):pass
        with self.assertRaisesRegex(PreservationError,'reference_registry_unavailable'):
            with store.connect() as db:db.execute('DELETE FROM sources')

    def test_output_budget_refuses_entire_record_instead_of_truncation(self):
        r,_,_=self.json_source()
        with self.assertRaisesRegex(PreservationError,'reference_output_budget_exceeded'):
            reference(self.store,r.version_id,'read',pointer='/0',max_chars=5)
        self.assertFalse(reference(self.store,r.version_id,'list')['results'][0]['full_payload'])

    def test_invalid_json_duplicate_keys_and_nonfinite_do_not_return_partial(self):
        for n,text in enumerate(['[{"x":1,"x":2}]','[{"x":NaN}]','[{},]','[{}] junk','[42]','[{},',
                                 '[{}\u00a0,{}]','[{}]\u00a0','[{"x":1e400}]']):
            r=self.capture(str(n),text)
            with self.assertRaises(PreservationError):reference(self.store,r.version_id,'list')

    def test_short_exact_id_and_path_search_preserve_duplicate_memberships(self):
        r,_,_=self.json_source()
        for query in ('17','ID 17'):
            result=reference(self.store,r.version_id,'search',query=query)
            self.assertEqual([x['selector']for x in result['results']],[{'pointer':'/0'},{'pointer':'/1'}])
        result=reference(self.store,r.version_id,'search',query='/2')
        self.assertEqual(result['results'][0]['selector'],{'pointer':'/2'})

    def test_input_size_and_time_budgets(self):
        r,_,_=self.json_source()
        with patch('olympus.references.MAX_TEXT_BYTES',1):
            with self.assertRaisesRegex(PreservationError,'reference_input_budget_exceeded'):reference(self.store,r.version_id,'list')
        with patch('olympus.references.time.monotonic',side_effect=[0,20]):
            with self.assertRaisesRegex(PreservationError,'reference_time_budget_exceeded'):reference(self.store,r.version_id,'list')

    def test_adversarial_scan_prefixes_obey_between_window_budget(self):
        text=json.dumps([{'content':'-----BEGIN PRIVATE KEY-----\n'*8000}])
        # Capture scanner is outside this read-only module; provision a fixture
        # that exercises its own streaming policy without timing that old path.
        with patch('olympus.preservation.guard_no_secrets'),patch('olympus.text_index.guard_no_secrets'):
            r=self.capture('scan-bound',text)
        started=__import__('time').monotonic()
        with self.assertRaisesRegex(PreservationError,'reference_time_budget_exceeded'):
            reference(self.store,r.version_id,'list',seconds=.02)
        self.assertLess(__import__('time').monotonic()-started,1)

    def test_corrupt_or_symlink_source_is_rejected_without_rebuild_side_effect(self):
        r,_,_=self.json_source();path=self.store.versions/r.version_id/'text.txt'
        path.write_text('[{"bad":true}]')
        with self.assertRaisesRegex(PreservationError,'hash_mismatch'):reference(self.store,r.version_id,'list')
        path.unlink();path.symlink_to('/dev/zero')
        with self.assertRaises(PreservationError):reference(self.store,r.version_id,'list')

    def test_repo_files_exact_with_nested_fake_delimiter_and_binary_inventory(self):
        r,archive,files=self.repo_source()
        listed=reference(self.store,r.version_id,'list')
        self.assertEqual(listed['units_total'],3);self.assertEqual(listed['coverage']['binary_files'],1)
        self.assertEqual(listed['coverage']['archive_version_id'],archive.version_id)
        for path in ('first.txt','empty.txt','last.ts'):
            result=reference(self.store,r.version_id,'read',path=path)
            self.assertEqual(result['results'][0]['raw'],files[path])
        with self.assertRaisesRegex(PreservationError,'reference_unit_not_found'):
            reference(self.store,r.version_id,'read',path='invented.txt')

    def test_repository_requires_canonical_matching_archive_and_exact_content(self):
        r,archive,files=self.repo_source()
        self.store.forget(archive.source_id,'synthetic archive withdrawal')
        with self.assertRaisesRegex(PreservationError,'reference_archive_unavailable'):reference(self.store,r.version_id,'list')

    def test_archive_corruption_and_unmapped_text_never_return_files(self):
        r,archive,_=self.repo_source();path=self.store.versions/archive.version_id/'original'
        path.write_bytes(b'bad')
        with self.assertRaisesRegex(PreservationError,'hash_mismatch'):reference(self.store,r.version_id,'list')

    def test_repository_extra_text_and_file_mismatch_are_not_guessed(self):
        r,archive,_=self.repo_source();source=self.store.read_text_version(r.version_id)
        for n,text in enumerate([source['text']+'unmapped tail',source['text'].replace('exactEnd = 731','exactEnd = 732')]):
            altered=self.capture('altered'+str(n),text,metadata=source['metadata'])
            with self.assertRaises(PreservationError):reference(self.store,altered.version_id,'list')

    def test_archive_revoked_after_read_is_fenced(self):
        r,archive,_=self.repo_source()
        import olympus.references as module
        original=module.report_links
        def revoke(*args):
            result=original(*args);self.store.forget(archive.source_id,'synthetic archive race');return result
        with patch('olympus.references.report_links',side_effect=revoke):
            with self.assertRaisesRegex(PreservationError,'reference_source_withheld'):reference(self.store,r.version_id,'read',path='last.ts')

    def test_unrelated_withdrawal_allowed_but_selected_source_is_fenced(self):
        r,_,_=self.json_source();other=self.capture('other','Other source')
        self.store.forget(other.source_id,'synthetic unrelated withdrawal')
        self.assertEqual(reference(self.store,r.version_id,'list')['units_total'],3)
        import olympus.references as module
        original=module.report_links
        def revoke(*args):
            result=original(*args);self.store.forget(r.source_id,'synthetic concurrent withdrawal');return result
        with patch('olympus.references.report_links',side_effect=revoke):
            with self.assertRaisesRegex(PreservationError,'reference_source_withheld'):reference(self.store,r.version_id,'read',pointer='/0')

    def test_dependency_and_research_change_guard_are_checked(self):
        base=self.capture('base','Basis statement')
        report=self.capture('report','[{"title":"derived","content":"Basis statement"}]',metadata={'material_role':'synthesis','parent_sources':json.dumps([base.version_id])})
        self.store.forget(base.source_id,'synthetic source withdrawal')
        with self.assertRaisesRegex(PreservationError,'reference_source_withheld'):reference(self.store,report.version_id,'list')

    def test_research_package_change_after_check_cannot_release_reference(self):
        base=self.capture('base','Basis statement')
        report=self.capture('report','[{"title":"derived","content":"Basis statement"}]',metadata={'material_role':'synthesis'})
        data={'schema':1,'scope':'reference-fixture','id':'reference-package','report_version':report.version_id,
            'claims':[{'id':'claim','type':'factual','statement':'Basis statement','evidence':[{'source_version':base.version_id,'quote':'Basis statement'}]}]}
        register_package(self.store,data)
        import olympus.references as module
        original=module.report_links
        def changed(*args):
            result=original(*args);data['claims'][0]['evidence'][0]['quote']='Absent quote';register_package(self.store,data);return result
        with patch('olympus.references.report_links',side_effect=changed):
            with self.assertRaisesRegex(PreservationError,'reference_source_withheld'):reference(self.store,report.version_id,'read',pointer='/0')

    def test_expensive_reference_reads_do_not_hold_writer_lock(self):
        r,_,_=self.json_source();entered=threading.Event();release=threading.Event();captured=threading.Event();errors=[]
        import olympus.references as module
        original=module._json_units
        def delayed(*args):entered.set();release.wait(2);return original(*args)
        def read():
            try:reference(self.store,r.version_id,'list')
            except Exception as exc:errors.append(exc)
        with patch('olympus.references._json_units',side_effect=delayed):
            worker=threading.Thread(target=read);worker.start();self.assertTrue(entered.wait(1))
            writer=threading.Thread(target=lambda:(self.capture('new','New input'),captured.set()));writer.start()
            progressed=captured.wait(1);release.set();writer.join();worker.join()
        self.assertTrue(progressed);self.assertEqual(errors,[])
