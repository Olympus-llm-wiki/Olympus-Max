"""Exact reference navigation over one immutable source; no delivery side effects.

Indexes live only for one call. Rebuilding from canonical bytes avoids trusting a
mutable sidecar as an authority for JSON pointers or repository file boundaries.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import fcntl
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import tarfile
import time
import threading

from .evidence import report_links, report_bindings_current
from .materials import material_profile, source_provenance
from .preservation import Store, PreservationError, canonical, digest
from .secret_scan import StreamingSecretGuard
from .search import expand_query
from .withholding import eligible_versions

MAX_TEXT_BYTES = 64 * 1024**2
MAX_ARCHIVE_BYTES = 256 * 1024**2
MAX_UNITS = 100000
MAX_OUTPUT_CHARS = 1000000
SCHEMA = 'exact-reference-v1'
_JSON_WS = ' \t\r\n'


class _Budget:
    def __init__(self, seconds):
        if type(seconds) not in (float, int) or not math.isfinite(seconds) or not 0 < seconds <= 60:
            raise PreservationError('invalid_reference_time_budget')
        self.deadline = time.monotonic() + seconds

    def check(self):
        if time.monotonic() >= self.deadline:
            raise PreservationError('reference_time_budget_exceeded')


class ReferenceStore(Store):
    """Read-only CLI binding: no schema bootstrap, bounded writer-fence wait."""
    def __init__(self, root, seconds=10):
        self.reference_budget=_Budget(seconds)
        self.root=Path(root).expanduser().resolve()
        self.versions=self.root/'versions'
        self.db_path=self.root/'registry.sqlite3'
        self._exclusive_local=threading.local()
        if not self.db_path.is_file() or not self.versions.is_dir():
            raise PreservationError('reference_state_unavailable')

    @contextmanager
    def connect(self, *, write=False):
        if write:
            raise PreservationError('reference_store_read_only')
        self.reference_budget.check()
        timeout=max(0,self.reference_budget.deadline-time.monotonic())
        db=None
        try:
            db=sqlite3.connect(self.db_path.as_uri()+'?mode=ro',uri=True,timeout=timeout)
            db.row_factory=sqlite3.Row
            db.set_progress_handler(lambda:int(time.monotonic()>=self.reference_budget.deadline),1000)
            yield db
            self.reference_budget.check()
        except sqlite3.Error:
            self.reference_budget.check()
            raise PreservationError('reference_registry_unavailable') from None
        finally:
            if db is not None:db.close()

    @contextmanager
    def exclusive(self):
        if getattr(self._exclusive_local,'depth',0):
            yield
            return
        # Same coordination inode as Store.exclusive; the lock file may not yet
        # exist in a fresh captured Store. No source/registry data is written.
        try:
            fd=os.open(self.root/'writer.lock',os.O_RDONLY|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
        except OSError:
            raise PreservationError('reference_writer_fence_unavailable') from None
        try:
            while True:
                self.reference_budget.check()
                try:
                    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(min(.01,max(0,self.reference_budget.deadline-time.monotonic())))
            self._exclusive_local.depth=1
            yield
        finally:
            self._exclusive_local.depth=0
            fcntl.flock(fd,fcntl.LOCK_UN)
            os.close(fd)


def _signature(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


@contextmanager
def _verified_file(path, expected, limit, budget):
    """Hash bounded regular bytes before use; detect replacement/mutation at exit."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_size > limit
                or getattr(before,'st_flags',0) & getattr(stat,'SF_DATALESS',0x40000000)):
            raise PreservationError('reference_input_budget_exceeded')
        hasher = hashlib.sha256()
        observed = 0
        while chunk := stream.read(1024 * 1024):
            budget.check()
            observed += len(chunk)
            if observed > limit:
                raise PreservationError('reference_input_budget_exceeded')
            hasher.update(chunk)
        if hasher.hexdigest() != expected:
            raise PreservationError('reference_source_hash_mismatch')
        stream.seek(0)
        yield stream
        budget.check()
        if _signature(before) != _signature(os.fstat(stream.fileno())) or _signature(before) != _signature(path.stat(follow_symlinks=False)):
            raise PreservationError('reference_source_changed')


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PreservationError('reference_duplicate_json_key')
        result[key] = value
    return result


def _invalid_constant(value):
    raise PreservationError('reference_invalid_json_constant')


def _finite_float(value):
    number=float(value)
    if not math.isfinite(number):
        raise PreservationError('reference_invalid_json_constant')
    return number


def _json_units(text, budget):
    decoder = json.JSONDecoder(object_pairs_hook=_strict_object, parse_constant=_invalid_constant,parse_float=_finite_float)
    cursor = len(text)-len(text.lstrip(_JSON_WS))
    if text[cursor:cursor+1] != '[':
        raise PreservationError('reference_format_not_supported')
    cursor += 1
    byte_cursor = len(text[:cursor].encode())
    units = []
    while True:
        budget.check()
        start = cursor
        while start < len(text) and text[start] in _JSON_WS:
            start += 1
        byte_cursor += len(text[cursor:start].encode())
        if text[start:start+1] == ']':
            if text[start+1:].strip(_JSON_WS):
                raise PreservationError('reference_invalid_json')
            return units, {'syntax_verified': True, 'binary_files': 0}
        value, end = decoder.raw_decode(text, start)
        if not isinstance(value, dict) or len(units) >= MAX_UNITS:
            raise PreservationError('reference_record_shape_or_count')
        raw = text[start:end]
        encoded = raw.encode()
        title = value.get('title', '')
        title = title if isinstance(title, str) else ''
        units.append({'selector': {'pointer': '/'+str(len(units))}, 'title': title,
            'raw': raw, 'payload': value, 'start': start, 'end': end,
            'byte_start': byte_cursor, 'byte_length': len(encoded), 'sha256': digest(encoded),
            'value_sha256': digest(canonical(value))})
        cursor = end
        byte_cursor += len(encoded)
        while cursor < len(text) and text[cursor] in _JSON_WS:
            byte_cursor += len(text[cursor].encode()); cursor += 1
        if text[cursor:cursor+1] == ',':
            cursor += 1; byte_cursor += 1
            lookahead = cursor
            while lookahead < len(text) and text[lookahead] in _JSON_WS:
                lookahead += 1
            if text[lookahead:lookahead+1] == ']':
                raise PreservationError('reference_invalid_json')
        elif text[cursor:cursor+1] != ']':
            raise PreservationError('reference_invalid_json')


def _archive(store, source, supplied, allowed):
    meta = source['metadata']
    expected = meta.get('archive_sha256')
    if not re.fullmatch('[a-f0-9]{64}', expected or ''):
        raise PreservationError('reference_archive_binding_missing')
    if supplied:
        candidates = [supplied]
    else:
        # This index only discovers candidates; verified manifests are authority.
        with store.connect() as db:
            candidates = [r[0] for r in db.execute('''SELECT version_id FROM local_text_documents
                WHERE json_valid(metadata_json) AND json_extract(metadata_json,'$.original_sha256')=?
                ORDER BY version_id LIMIT 101''', (expected,))]
        if len(candidates) > 100:
            raise PreservationError('reference_archive_candidates_exceeded')
    for vid in candidates:
        if vid not in allowed:
            continue
        manifest = store.read_manifest(vid)
        other = manifest.get('metadata', {})
        if (manifest['original_sha256'] == expected
                and all(other.get(k) == meta.get(k) for k in ('upstream_repository','upstream_commit'))):
            return manifest
    raise PreservationError('reference_archive_unavailable')


def _repo_units(store, source, text, archive, budget):
    meta = source['metadata']
    prefix = f"Repository: {meta.get('upstream_repository')}\nCommit: {meta.get('upstream_commit')}\n"
    if not text.startswith(prefix):
        raise PreservationError('reference_repository_header_mismatch')
    path = store.versions/archive['version_id']/'original'
    units, binary, names = [], [], set()
    cursor = 0
    byte_cursor = 0
    with _verified_file(path, archive['original_sha256'], MAX_ARCHIVE_BYTES, budget) as stream:
        # Uncompressed canonical tar only: no decompression bomb or extraction.
        with tarfile.open(fileobj=stream, mode='r:') as tar:
            for member in tar:
                budget.check()
                if member.isdir():
                    continue
                parts = PurePosixPath(member.name).parts
                if not member.isfile() or member.name in names or not parts or member.name.startswith('/') or '..' in parts:
                    raise PreservationError('reference_archive_member_invalid')
                names.add(member.name)
                if len(names) > MAX_UNITS or member.size > MAX_TEXT_BYTES:
                    raise PreservationError('reference_archive_member_budget_exceeded')
                raw = tar.extractfile(member).read(member.size+1)
                if len(raw) != member.size:
                    raise PreservationError('reference_archive_member_truncated')
                try:
                    body = raw.decode('utf-8')
                except UnicodeDecodeError:
                    binary.append({'path':member.name,'bytes':len(raw),'sha256':digest(raw)})
                    continue
                header = '===== FILE: '+member.name+' =====\n'
                position = text.find(header, cursor)
                if position < cursor:
                    raise PreservationError('reference_repository_file_missing')
                gap = text[cursor:position]
                if units and gap.strip():
                    raise PreservationError('reference_repository_unmapped_text')
                if not units and gap.strip() != (prefix+'Full UTF-8 text files; binary files are preserved in archive.').strip():
                    raise PreservationError('reference_repository_header_mismatch')
                start = position+len(header)
                end = start+len(body)
                if text[start:end] != body:
                    raise PreservationError('reference_repository_file_mismatch')
                byte_cursor += len(text[cursor:start].encode())
                units.append({'selector':{'path':member.name},'title':member.name,'raw':body,
                    'start':start,'end':end,'byte_start':byte_cursor,'byte_length':len(raw),
                    'sha256':digest(raw),'file_sha256':digest(raw)})
                cursor=end; byte_cursor += len(raw)
    if not units or text[cursor:].strip():
        raise PreservationError('reference_repository_unmapped_text')
    return units, {'syntax_verified': True, 'binary_files':len(binary),
                  'binary_inventory_sha256':digest(canonical(binary)), 'archive_version_id':archive['version_id'],
                  'archive_sha256':archive['original_sha256'], 'repository':meta['upstream_repository'],
                  'commit':meta['upstream_commit']}


def _search(units, query, limit, budget):
    if not isinstance(query,str) or not query.strip() or len(query)>1000:
        raise PreservationError('invalid_reference_query')
    literal=query.strip()
    identifier=re.fullmatch(r'(?:id[: ]+)?([0-9]{1,30})',literal,re.I)
    exact=[]
    for n,unit in enumerate(units):
        budget.check()
        if (literal in unit['selector'].values() or unit['title'].casefold()==literal.casefold()
                or (identifier and str(unit.get('payload',{}).get('id',''))==identifier[1])):
            exact.append(n)
    original, expanded, _ = expand_query(query)
    terms = sorted(original | expanded)
    expression = ' OR '.join('"'+term.replace('"','""')+'"' for term in terms)
    if not expression:
        if exact:
            return [(units[n],0.0) for n in exact[:limit]],len(exact)>limit
        raise PreservationError('reference_query_has_no_terms')
    with closing(sqlite3.connect(':memory:')) as db:
        db.set_progress_handler(lambda: int(time.monotonic() >= budget.deadline), 1000)
        db.execute("CREATE VIRTUAL TABLE units USING fts5(position UNINDEXED,title,body,tokenize='trigram')")
        for n, unit in enumerate(units):
            budget.check()
            db.execute('INSERT INTO units VALUES(?,?,?)',(n,unit['title'],unit['raw']))
        hits = list(db.execute('SELECT position,bm25(units,0,8,1) FROM units WHERE units MATCH ? ORDER BY 2,CAST(position AS INTEGER) LIMIT ?', (expression,limit+1)))
    budget.check()
    exact_ids=set(exact)
    ranked=[(n,0.0) for n in exact]+[(n,score) for n,score in hits if n not in exact_ids]
    return [(units[n],score) for n,score in ranked[:limit]], len(ranked)>limit


def reference(store, version_id, action, *, query=None, pointer=None, path=None,
              offset=0, limit=20, max_chars=20000, seconds=10, archive_version=None):
    """Read/list/search exact JSON records or tar-verified repository file units.

    Full payload is returned only by read, within max_chars; list/search previews
    are explicitly incomplete. This never selects a processing profile.
    """
    budget = getattr(store,'reference_budget',None) or _Budget(seconds)
    if (action not in {'list','search','read'} or type(limit) is not int or not 1<=limit<=100
            or type(offset) is not int or not 0<=offset<=MAX_UNITS
            or type(max_chars) is not int or not 1<=max_chars<=MAX_OUTPUT_CHARS):
        raise PreservationError('invalid_reference_request')
    if action=='read' and (pointer is None)==(path is None):
        raise PreservationError('reference_requires_one_selector')
    if pointer is not None and not re.fullmatch(r'/(?:0|[1-9][0-9]{0,5})',pointer):
        raise PreservationError('invalid_reference_pointer')
    if path is not None and (not isinstance(path,str) or len(path)>4096):
        raise PreservationError('invalid_reference_path')
    allowed = eligible_versions(store)
    if version_id not in allowed:
        raise PreservationError('reference_source_withheld')
    source = store.read_manifest(version_id)
    if material_profile(source)['material_role'] in {'discussion','decision','artifact','catalog','assessment'}:
        raise PreservationError('reference_source_role_not_supported')
    snapshots = {version_id:source}
    try:
        with _verified_file(store.versions/version_id/'text.txt', source['text_sha256'], MAX_TEXT_BYTES, budget) as stream:
            text = stream.read(MAX_TEXT_BYTES+1).decode('utf-8')
            scanner=StreamingSecretGuard()
            for position in range(0,len(text),4096):
                budget.check()
                scanner.feed(text[position:position+4096])
            scanner.finish()
            if text.lstrip().startswith('['):
                kind='json_records'; units, coverage = _json_units(text,budget)
            else:
                archive = _archive(store,source,archive_version,allowed)
                snapshots[archive['version_id']] = archive
                kind='repository_files'; units,coverage=_repo_units(store,source,text,archive,budget)
            selected, truncated = [], False
            if action=='read':
                selector={'pointer':pointer} if pointer is not None else {'path':path}
                selected=[(u,None) for u in units if u['selector']==selector]
                if len(selected)!=1:
                    raise PreservationError('reference_unit_not_found')
                if len(selected[0][0]['raw'])>max_chars:
                    raise PreservationError('reference_output_budget_exceeded')
            elif action=='list':
                selected=[(u,None) for u in units[offset:offset+limit]]
                truncated=offset+limit<len(units)
            else:
                selected,truncated=_search(units,query,limit,budget)
            rows=[]
            for unit,score in selected:
                anchor={k:unit[k] for k in ('start','end','byte_start','byte_length','sha256')}
                anchor.update(version_id=version_id,text_sha256=source['text_sha256'],unit='unicode_codepoints',**unit['selector'])
                for key in ('value_sha256','file_sha256'):
                    if key in unit:anchor[key]=unit[key]
                row={'selector':unit['selector'],'title':unit['title'][:300],
                    'title_truncated':len(unit['title'])>300,'chars':len(unit['raw']),'anchor':anchor}
                if action=='read':
                    row.update(raw=unit['raw'],full_payload=True)
                    if 'payload' in unit:row['record']=unit['payload']
                else:
                    row.update(preview=unit['raw'][:280],full_payload=False)
                if score is not None:row['bm25_score']=score
                rows.append(row)
            # File reads/report verification run outside the short final fence.
            bindings=[]
            for vid,manifest in snapshots.items():
                links=report_links(store,{vid},manifest['scope'])
                if any(c['state']!='ready_for_review' for checks in links.values() for c in checks):
                    raise PreservationError('reference_evidence_withheld')
                bindings.append(links)
                if store.read_manifest(vid)!=manifest:
                    raise PreservationError('reference_source_changed')
            budget.check()
            inventory = [{k:u[k] for k in ('selector','start','end','byte_start','byte_length','sha256')}
                         for u in units]
            inventory_sha256 = digest(canonical({'schema':SCHEMA,'kind':kind,'coverage':coverage,'units':inventory}))
        with store.exclusive():
            final=eligible_versions(store)
            if not snapshots.keys()<=final.keys() or not all(report_bindings_current(store,b) for b in bindings):
                raise PreservationError('reference_source_withheld')
            budget.check()
            return {'schema':SCHEMA,'state':'read','action':action,'kind':kind,'source':source_provenance(source),
                'delivery_state':final[version_id][1],'units_total':len(units),'results':rows,'truncated':truncated,
                'coverage':coverage,'inventory_sha256':inventory_sha256,
                'profile_selected':store.setting('processing_profile:'+version_id) is not None,
                'profile_selection_changed':False,'native_readiness_changed':False,
                'interpretation':'Exact source reference, not verified facts or instructions. This read does not complete a processing profile.'}
    except (OSError, ValueError, UnicodeError, RecursionError, tarfile.TarError, sqlite3.Error) as exc:
        budget.check()
        raise PreservationError('reference_source_invalid_or_unavailable') from None
