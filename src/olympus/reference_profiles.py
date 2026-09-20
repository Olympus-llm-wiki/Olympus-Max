"""Explicit local reference profile receipts; native delivery history is intact."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from contextlib import contextmanager

from .materials import material_profile
from .preservation import PreservationError, canonical, digest, timestamp
from .references import reference, SCHEMA as REFERENCE_SCHEMA, _Budget, _verified_file
from .text_index import CHUNK_CHARS, OVERLAP
from .withholding import eligible_versions

PROFILE = 'local_reference_v1'
PREFIX = 'processing_profile:'
MAX_SELECTED = 100
MAX_AUDIT_BYTES = 128 * 1024**2
MAX_AUDIT_SECONDS = 10
INDEX_SCHEMA = 'fts5-trigram-span-v1'


class _IndexSnapshot:
    """Audit immutable row copies, then compare only those rows at the fence.

    Hashing and source-file reads happen without a writer transaction. The final
    transaction makes a bounded exact comparison of the selected index rows;
    unrelated SQLite writes cannot invalidate them. No schema changes are needed.
    """
    def __init__(self, db):
        self.db = db
        self.captured = {}
        self.changed = set()

    def _rows(self, ids, deadline):
        db = self.db
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        selected = json.dumps(sorted(ids))
        result = {vid: {'document': None, 'rows': [], 'spans': []} for vid in ids}
        try:
            for row in db.execute('SELECT * FROM local_text_documents WHERE version_id IN (SELECT value FROM json_each(?))', (selected,)):
                result[row['version_id']]['document'] = dict(row)
            readable = [vid for vid, value in result.items() if value['document'] is not None
                        and type(value['document']['text_bytes']) is int
                        and 0 <= value['document']['text_bytes'] <= MAX_AUDIT_BYTES]
            selected = json.dumps(sorted(readable))
            for row in db.execute('''SELECT f.version_id,f.start,f.title,f.body,p.byte_start,p.byte_length,p.sha256
                FROM local_text_fts f LEFT JOIN local_text_spans p
                ON p.version_id=f.version_id AND p.start=CAST(f.start AS INTEGER)
                WHERE f.version_id IN (SELECT value FROM json_each(?))
                ORDER BY f.version_id,CAST(f.start AS INTEGER),f.rowid''', (selected,)):
                if not isinstance(row['body'], str) or len(row['body']) > CHUNK_CHARS:
                    result[row['version_id']]['invalid'] = True
                    continue
                result[row['version_id']]['rows'].append(dict(row))
            for row in db.execute('''SELECT * FROM local_text_spans WHERE version_id IN
                (SELECT value FROM json_each(?)) ORDER BY version_id,start''', (selected,)):
                result[row['version_id']]['spans'].append(tuple(row))
            if time.monotonic() >= deadline:
                raise PreservationError('reference_profile_audit_budget')
            return result
        except sqlite3.Error:
            if time.monotonic() >= deadline:
                raise PreservationError('reference_profile_audit_budget') from None
            raise
        finally:
            db.set_progress_handler(None, 0)

    def capture(self, vid, deadline):
        if vid not in self.captured:
            if self.db.in_transaction:
                raise PreservationError('reference_profile_audit_inside_write_transaction')
            self.db.execute('BEGIN')
            try:
                value = self._rows([vid], deadline)[vid]
            finally:
                self.db.execute('ROLLBACK')
            self.captured[vid] = value
        return self.captured[vid]

    def fence(self):
        # A busy unrelated writer must not stall foreground operations for the
        # Store's normal 15-second busy timeout.
        self.db.execute('PRAGMA busy_timeout=500')
        try:
            self.db.execute('BEGIN IMMEDIATE')
        except sqlite3.Error:
            raise PreservationError('reference_profile_snapshot_busy') from None
        current = self._rows(self.captured, time.monotonic() + 0.5)
        self.changed = {vid for vid, rows in self.captured.items() if current[vid] != rows}
        return self.db

    def check(self, vid):
        if vid in self.changed:
            raise PreservationError('reference_profile_snapshot_changed')


@contextmanager
def _index_snapshot(store):
    with store.connect() as db:
        yield _IndexSnapshot(db)


def _binding(ids):
    return {'profile':PROFILE,'version_ids':sorted(ids),
            'native_requirement':'not_required_by_selected_profile',
            'preserve':['original','library','backup','cohort_membership']}


def _file_anchor(store, vid, name, expected):
    path=store.versions/vid/name
    if path.parent.is_symlink() or path.is_symlink():
        raise PreservationError('reference_profile_source_changed')
    value=path.stat()
    if not stat.S_ISREG(value.st_mode) or getattr(value,'st_flags',0)&getattr(stat,'SF_DATALESS',0x40000000):
        raise PreservationError('reference_profile_source_unavailable')
    return {'version_id':vid,'name':name,'sha256':expected,'bytes':value.st_size,
            'signature':[value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns]}


def _verified_anchor(store,vid,name,expected):
    with _verified_file(store.versions/vid/name,expected,256*1024**2,_Budget(30)):
        return _file_anchor(store,vid,name,expected)


def _approval(store, ids, approval_version, wording):
    if (not isinstance(wording,str) or not wording.strip() or len(wording)>2000
            or approval_version not in eligible_versions(store)):
        raise PreservationError('reference_profile_approval_required')
    approval=store.read_version(approval_version)
    metadata=approval['metadata'];binding=_binding(ids)
    try:
        saved=json.loads(metadata.get('profile_approval',''))
    except (ValueError,TypeError):
        raise PreservationError('reference_profile_approval_mismatch') from None
    if (not material_profile(approval)['current_owner_decision'] or saved!=binding
            or metadata.get('profile_approval_binding')!=digest(canonical(binding))
            or wording not in approval['text'] or wording not in metadata.get('owner_confirmation_evidence','')):
        raise PreservationError('reference_profile_approval_mismatch')
    return {'version_id':approval_version,'wording':wording,'binding':binding,
            'binding_sha256':digest(canonical(binding)),'text_sha256':approval['text_sha256'],
            'original_sha256':approval['original_sha256'],
            'manifest_sha256':digest((store.versions/approval_version/'manifest.json').read_bytes())}


def _index_proof(store, vid, *, text=None, manifest=None, deadline=None, snapshot=None):
    """Hash every copied indexed body outside writer transactions."""
    deadline = deadline or time.monotonic() + MAX_AUDIT_SECONDS
    if snapshot is None:
        with _index_snapshot(store) as own:
            captured = own.capture(vid, deadline)
    else:
        captured = snapshot.capture(vid, deadline)
    document = captured['document']
    if document is None:
        raise PreservationError('reference_profile_index_missing')
    if captured.get('invalid'):
        raise PreservationError('reference_profile_index_invalid')
    metadata = json.loads(document['metadata_json'])
    if (document['schema_version'] != 1 or document['text_bytes'] > MAX_AUDIT_BYTES
            or manifest is not None and metadata != manifest):
        raise PreservationError('reference_profile_index_invalid')
    fingerprint = hashlib.sha256(); count = 0; byte_start = 0
    for row in captured['rows']:
        if time.monotonic() >= deadline:
            raise PreservationError('reference_profile_audit_budget')
        start = int(row['start']); body = row['body']; raw = body.encode()
        if (row['title'] != metadata['title'] or start != count * (CHUNK_CHARS - OVERLAP)
                or row['byte_start'] != byte_start or row['byte_length'] != len(raw)
                or row['sha256'] != digest(raw)):
            raise PreservationError('reference_profile_index_invalid')
        if text is not None and body != text[start:start+CHUNK_CHARS]:
            raise PreservationError('reference_profile_index_invalid')
        fingerprint.update(canonical([start,row['title'],row['byte_start'],row['byte_length'],row['sha256']]))
        byte_start += len(body[:CHUNK_CHARS-OVERLAP].encode()); count += 1
    expected = len(range(0,max(1,len(text)),CHUNK_CHARS-OVERLAP)) if text is not None else None
    if (not count or len(captured['spans']) != count or (expected is not None and expected != count)
            or byte_start != document['text_bytes']):
        raise PreservationError('reference_profile_index_incomplete')
    return {'schema':INDEX_SCHEMA,'chunk_chars':CHUNK_CHARS,'overlap':OVERLAP,'span_count':count,
            'spans_sha256':fingerprint.hexdigest(),'document_sha256':digest(canonical(document)),
            'text_sha256':document['text_sha256'],'manifest_sha256':document['manifest_sha256'],
            'text_bytes':document['text_bytes']}


def _native_untouched(store, vid, *, transaction=None):
    @contextmanager
    def connection():
        if transaction is not None:yield transaction
        else:
            with store.connect() as db:yield db
    with connection() as db:
        row=db.execute('SELECT state,attempts,lease_until FROM delivery WHERE version_id=?',(vid,)).fetchone()
        represented=(db.execute("SELECT 1 FROM sqlite_master WHERE name='native_representations'").fetchone()
            and db.execute("SELECT 1 FROM native_representations WHERE version_id=? AND kind='native_index' AND enabled=1",(vid,)).fetchone())
        if row is None or row['state']!='pending' or row['attempts']!=0 or row['lease_until']>time.time() or represented:
            raise PreservationError('reference_profile_native_history_requires_review')


def _envelope(receipt):
    return canonical({'receipt':receipt,'sha256':digest(canonical(receipt))}).decode()


def activate_reference_profiles(store, version_ids, approval_version_id, owner_wording):
    """Verify exactly the owner-bound set, then atomically store profile receipts.

    No native operation, canonical delivery state, source role or cohort is
    changed. Expensive input/index checks happen before the final writer fence.
    """
    ids=sorted(set(version_ids))
    if (not ids or len(ids)>MAX_SELECTED or len(ids)!=len(version_ids)
            or any(not re.fullmatch(r'olv-[a-f0-9]{64}',v) for v in ids)):
        raise PreservationError('invalid_reference_profile_scope')
    approval=_approval(store,ids,approval_version_id,owner_wording)
    before={vid:store.setting(PREFIX+vid) for vid in ids}
    proofs=[]
    for vid in ids:
        _native_untouched(store,vid)
        inventory=reference(store,vid,'list',limit=1,seconds=30)
        version=store.read_manifest(vid);folder=store.versions/vid;budget=_Budget(30)
        files=[]
        for name,expected in [('original',version['original_sha256']),('text.txt',version['text_sha256'])]:
            with _verified_file(folder/name,expected,256*1024**2,budget) as stream:
                text=stream.read().decode() if name=='text.txt' else None
                anchor=_file_anchor(store,vid,name,expected)
            files.append(anchor)
        manifest_hash=digest((folder/'manifest.json').read_bytes())
        files.append(_file_anchor(store,vid,'manifest.json',manifest_hash))
        archive_id=inventory['coverage'].get('archive_version_id')
        dependencies=[approval_version_id]
        if archive_id:
            archive=store.read_manifest(archive_id)
            dependencies.append(archive_id)
            files.extend([_verified_anchor(store,archive_id,'original',archive['original_sha256']),
                _verified_anchor(store,archive_id,'manifest.json',digest((store.versions/archive_id/'manifest.json').read_bytes()))])
        files.extend([_verified_anchor(store,approval_version_id,'original',approval['original_sha256']),
                      _verified_anchor(store,approval_version_id,'text.txt',approval['text_sha256']),
                      _verified_anchor(store,approval_version_id,'manifest.json',approval['manifest_sha256'])])
        index=_index_proof(store,vid,text=text,manifest=version)
        if index['text_sha256']!=version['text_sha256'] or index['manifest_sha256']!=manifest_hash:
            raise PreservationError('reference_profile_index_invalid')
        proofs.append({'schema':1,'profile':PROFILE,'state':'profile_complete','version_id':vid,
            'native_requirement':'not_required_by_selected_profile','completed_at':timestamp(),
            'reference_schema':REFERENCE_SCHEMA,'inventory_sha256':inventory['inventory_sha256'],
            'unit_count':inventory['units_total'],'kind':inventory['kind'],'coverage':inventory['coverage'],
            'source':{'source_id':version['source_id'],'original_sha256':version['original_sha256'],
                      'text_sha256':version['text_sha256'],'manifest_sha256':manifest_hash},
            'index':index,'files':files,'dependencies':dependencies,'approval':approval})
    # Recheck copied selected index rows; unrelated writes remain independent.
    with _index_snapshot(store) as snapshot:
        allowed=eligible_versions(store)
        for proof in proofs:_validate(store,proof,allowed,snapshot=snapshot)
        with store.exclusive():
            db=snapshot.fence()
            allowed=eligible_versions(store)
            for proof in proofs:
                vid=proof['version_id']
                snapshot.check(vid)
                if store.setting(PREFIX+vid)!=before[vid]:
                    raise PreservationError('reference_profile_activation_race')
                _native_untouched(store,vid,transaction=db)
                _validate(store,proof,allowed,verify_index=False)
                db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                           (PREFIX+proof['version_id'],_envelope(proof)))
    return {'state':'profile_complete','profile':PROFILE,'version_ids':ids,'receipts':proofs,
            'native_writes':False,'delivery_states_changed':False,'cohort_membership_changed':False}


def _validate(store, receipt, allowed, *, verify_index=True, deadline=None, snapshot=None):
    if (receipt['schema']!=1 or receipt['profile']!=PROFILE or receipt['reference_schema']!=REFERENCE_SCHEMA
            or receipt['state']!='profile_complete' or receipt['native_requirement']!='not_required_by_selected_profile'
            or receipt['index']['schema']!=INDEX_SCHEMA):
        raise PreservationError('reference_profile_schema_changed')
    vid=receipt['version_id']
    approval=receipt['approval']
    archive_id=receipt['coverage'].get('archive_version_id')
    expected_files={(vid,'original'),(vid,'text.txt'),(vid,'manifest.json'),
                    (approval['version_id'],'original'),
                    (approval['version_id'],'text.txt'),(approval['version_id'],'manifest.json')}
    expected_dependencies={approval['version_id']}
    if archive_id:
        expected_files.update({(archive_id,'original'),(archive_id,'manifest.json')})
        expected_dependencies.add(archive_id)
    if (set(receipt['dependencies'])!=expected_dependencies
            or {(f['version_id'],f['name']) for f in receipt['files']}!=expected_files
            or len(receipt['files'])!=len(expected_files)
            or type(receipt['unit_count']) is not int or receipt['unit_count']<0
            or not re.fullmatch('[a-f0-9]{64}',receipt['inventory_sha256'])
            or receipt['coverage'].get('syntax_verified') is not True
            or approval['binding_sha256']!=digest(canonical(approval['binding']))):
        raise PreservationError('reference_profile_receipt_invalid')
    if not ({vid}|set(receipt['dependencies']))<=allowed.keys():
        raise PreservationError('reference_profile_dependency_withheld')
    if vid not in receipt['approval']['binding']['version_ids'] or receipt['approval']['binding']!=_binding(receipt['approval']['binding']['version_ids']):
        raise PreservationError('reference_profile_approval_mismatch')
    for anchor in receipt['files']:
        if _file_anchor(store,anchor['version_id'],anchor['name'],anchor['sha256'])!=anchor:
            raise PreservationError('reference_profile_source_changed')
    if verify_index:
        if _index_proof(store,vid,deadline=deadline,snapshot=snapshot)!=receipt['index']:
            raise PreservationError('reference_profile_index_changed')
    else:
        with store.connect() as db:
            document=db.execute('SELECT * FROM local_text_documents WHERE version_id=?',(vid,)).fetchone()
        if document is None or digest(canonical(dict(document)))!=receipt['index']['document_sha256']:
            raise PreservationError('reference_profile_index_changed')


def processing_profile_summary(store, version_ids=None):
    """Small selected cohort only; source bytes are not reread during scheduling."""
    with _index_snapshot(store) as snapshot:
        return _summary(store,version_ids,snapshot)


def _summary(store, version_ids, snapshot):
    selected=None if version_ids is None else set(version_ids)
    with store.connect() as db:
        rows=list(db.execute("SELECT key,value FROM settings WHERE substr(key,1,19)=? LIMIT ?",(PREFIX,MAX_SELECTED+1)))
    if len(rows)>MAX_SELECTED:
        raise PreservationError('reference_profile_count_limit')
    allowed=eligible_versions(store) if rows else {}
    views=[];checked={};audited_bytes=0;deadline=time.monotonic()+MAX_AUDIT_SECONDS
    for key,raw in rows:
        vid=key[len(PREFIX):]
        if selected is not None and vid not in selected:continue
        view={'version_id':vid,'profile':PROFILE,'selected':True,'state':'profile_invalid',
              'native_requirement':'requires_profile_revalidation','native_readiness_changed':False}
        try:
            envelope=json.loads(raw);receipt=envelope['receipt']
            if envelope['sha256']!=digest(canonical(receipt)) or receipt['version_id']!=vid:
                raise PreservationError('reference_profile_receipt_invalid')
            size=receipt['index']['text_bytes']
            if type(size) is not int or size<0 or audited_bytes+size>MAX_AUDIT_BYTES or time.monotonic()>=deadline:
                raise PreservationError('reference_profile_audit_budget')
            audited_bytes+=size
            _validate(store,receipt,allowed,deadline=deadline,snapshot=snapshot)
            _native_untouched(store,vid)
            checked[vid]=(raw,receipt)
            view.update(state='profile_complete',native_requirement='not_required_by_selected_profile',
                unit_count=receipt['unit_count'],kind=receipt['kind'],inventory_sha256=receipt['inventory_sha256'],
                approval_version_id=receipt['approval']['version_id'],completed_at=receipt['completed_at'])
        except (PreservationError,OSError,KeyError,ValueError,TypeError,AttributeError,sqlite3.Error) as exc:
            view['reason']=str(exc) if isinstance(exc,PreservationError) else 'reference_profile_receipt_invalid'
        views.append(view)
    if checked:
        with store.exclusive():
            try:
                snapshot.fence()
            except PreservationError as exc:
                for view in views:
                    if view['state']=='profile_complete':
                        view.update(state='profile_invalid',native_requirement='requires_profile_revalidation',
                                    reason=str(exc))
                return {'profile':PROFILE,'selected':len(views),'counts':dict(Counter(v['state'] for v in views)),
                        'versions':views,'cohort_membership_changed':False,'native_complete_inferred':False}
            final=eligible_versions(store)
            for view in views:
                if view['version_id'] not in checked:continue
                raw,receipt=checked[view['version_id']]
                try:
                    snapshot.check(view['version_id'])
                    if store.setting(PREFIX+view['version_id'])!=raw:
                        raise PreservationError('reference_profile_receipt_changed')
                    _validate(store,receipt,final,verify_index=False)
                    _native_untouched(store,view['version_id'])
                except (PreservationError,OSError,KeyError,ValueError,TypeError) as exc:
                    view.update(state='profile_invalid',native_requirement='requires_profile_revalidation',
                        reason=str(exc) if isinstance(exc,PreservationError) else 'reference_profile_receipt_invalid')
    return {'profile':PROFILE,'selected':len(views),'counts':dict(Counter(v['state'] for v in views)),
            'versions':views,'cohort_membership_changed':False,'native_complete_inferred':False}


def processing_profile(store, version_id):
    rows=processing_profile_summary(store,[version_id])['versions']
    return rows[0] if rows else {'version_id':version_id,'state':'not_selected','selected':False}


def native_not_required_ids(store):
    return {v['version_id'] for v in processing_profile_summary(store)['versions'] if v['state']=='profile_complete'}
