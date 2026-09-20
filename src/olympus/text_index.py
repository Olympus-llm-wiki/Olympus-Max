"""Rebuildable lexical index anchored to immutable text, never to binary size."""
from __future__ import annotations

import json
import os
import stat
import time

from .preservation import PreservationError, canonical, digest, guard_no_secrets

CHUNK_CHARS = 16000
OVERLAP = 256
MAX_HITS = 2000


class TextIndex:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS local_text_documents (
                    version_id TEXT PRIMARY KEY REFERENCES versions(id),
                    schema_version INTEGER NOT NULL, text_sha256 TEXT NOT NULL,
                    manifest_sha256 TEXT NOT NULL, metadata_json TEXT NOT NULL,
                    indexed_at REAL NOT NULL, text_bytes INTEGER NOT NULL);
                CREATE VIRTUAL TABLE IF NOT EXISTS local_text_fts USING fts5(
                    version_id UNINDEXED, start UNINDEXED, title, body, tokenize='trigram');
                CREATE TABLE IF NOT EXISTS local_text_dependencies (
                    report_version TEXT NOT NULL, source_version TEXT NOT NULL,
                    package_version TEXT NOT NULL,
                    PRIMARY KEY(report_version,source_version,package_version));
                CREATE INDEX IF NOT EXISTS local_text_dependency_package ON local_text_dependencies(package_version);
                CREATE TABLE IF NOT EXISTS local_text_spans (
                    version_id TEXT NOT NULL, start INTEGER NOT NULL, byte_start INTEGER NOT NULL,
                    byte_length INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    PRIMARY KEY(version_id,start));
            """)

    def put(self, version):
        vid, body = version['version_id'], version['text']
        raw = body.encode()
        if digest(raw) != version['text_sha256']:
            raise PreservationError('source_hash_mismatch')
        guard_no_secrets(raw)
        manifest = {k: v for k, v in version.items() if k not in ('text', 'original')}
        dependency_rows = []
        if manifest.get('metadata', {}).get('artifact_type') == 'research_package':
            from .evidence import validate_manifest, record_key
            try:
                package = json.loads(body)
                validate_manifest(package)
                if (record_key('research_package', package['scope'], package['id']) != manifest['source_key']
                        or package['scope'] != manifest['scope']):
                    raise PreservationError('evidence_record_identity_mismatch')
            except (ValueError, TypeError, KeyError):
                raise PreservationError('invalid_research_registry') from None
            dependencies = {vid} | {entry['source_version'] for claim in package['claims'] for entry in claim['evidence']}
            dependency_rows = [(package['report_version'], source, vid) for source in dependencies]
        # Anchor to disk bytes, including original capture formatting.
        anchor = digest((self.store.versions / vid / 'manifest.json').read_bytes())
        with self.store.connect(write=True) as db:
            row = db.execute('''SELECT v.active,s.forgotten_at FROM versions v
                JOIN sources s ON s.id=v.source_id WHERE v.id=?''', (vid,)).fetchone()
            if not row or not row['active'] or row['forgotten_at'] is not None:
                return False
            db.execute('DELETE FROM local_text_dependencies WHERE package_version=?', (vid,))
            db.executemany('INSERT INTO local_text_dependencies VALUES(?,?,?)', dependency_rows)
            old = db.execute('SELECT text_sha256,manifest_sha256,metadata_json FROM local_text_documents WHERE version_id=?', (vid,)).fetchone()
            spans_present = db.execute('SELECT 1 FROM local_text_spans WHERE version_id=? LIMIT 1', (vid,)).fetchone()
            if old and spans_present and tuple(old) == (version['text_sha256'], anchor, canonical(manifest).decode()):
                return False
            db.execute('DELETE FROM local_text_fts WHERE version_id=?', (vid,))
            db.execute('DELETE FROM local_text_spans WHERE version_id=?', (vid,))
            byte_start = 0
            step = CHUNK_CHARS - OVERLAP
            for start in range(0, max(1, len(body)), step):
                chunk = body[start:start+CHUNK_CHARS]
                encoded = chunk.encode()
                db.execute('INSERT INTO local_text_fts(version_id,start,title,body) VALUES(?,?,?,?)',
                           (vid, start, version['title'], chunk))
                db.execute('INSERT INTO local_text_spans VALUES(?,?,?,?,?)',
                           (vid, start, byte_start, len(encoded), digest(encoded)))
                byte_start += len(body[start:start+step].encode())
            db.execute('INSERT OR REPLACE INTO local_text_documents VALUES(?,?,?,?,?,?,?)',
                (vid, 1, version['text_sha256'], anchor, canonical(manifest).decode(), time.time(), len(raw)))
        return True

    def rebuild(self, *, limit=100, max_seconds=5, reset=False):
        if not 1 <= limit <= 10000 or not 0 < max_seconds <= 3600:
            raise PreservationError('invalid_text_index_rebuild_limit')
        if reset:
            with self.store.connect(write=True) as db:
                db.execute('DELETE FROM local_text_fts')
                db.execute('DELETE FROM local_text_documents')
                db.execute('DELETE FROM local_text_dependencies')
                db.execute('DELETE FROM local_text_spans')
        with self.store.connect() as db:
            ids = [r[0] for r in db.execute('''SELECT v.id FROM versions v
                JOIN sources s ON s.id=v.source_id LEFT JOIN local_text_documents i ON i.version_id=v.id
                WHERE (i.version_id IS NULL OR NOT json_valid(i.metadata_json)
                    OR NOT EXISTS(SELECT 1 FROM local_text_spans p WHERE p.version_id=v.id) OR
                    ((CASE WHEN json_valid(i.metadata_json) THEN json_extract(i.metadata_json,'$.metadata.artifact_type') END)='research_package'
                     AND NOT EXISTS(SELECT 1 FROM local_text_dependencies d WHERE d.package_version=v.id)))
                AND v.active=1 AND s.forgotten_at IS NULL
                ORDER BY v.observed_at,v.id LIMIT ?''', (limit,))]
        done, errors, started = 0, [], time.monotonic()
        for vid in ids:
            if time.monotonic()-started >= max_seconds:
                break
            try:
                self.put(self.store.read_text_version(vid))
                done += 1
            except (PreservationError, OSError) as exc:
                errors.append({'version_id': vid, 'error': str(exc) if isinstance(exc, PreservationError) else 'text_io_error'})
        coverage = self.coverage()
        return {'indexed': done, 'errors': errors, 'remaining': coverage['unindexed']+coverage['dependency_repairs'],
                'elapsed': time.monotonic()-started}

    def coverage(self):
        with self.store.connect() as db:
            row = db.execute('''SELECT count(*),count(i.version_id),coalesce(sum(i.text_bytes),0)
                FROM versions v JOIN sources s ON s.id=v.source_id
                LEFT JOIN local_text_documents i ON i.version_id=v.id
                WHERE v.active=1 AND s.forgotten_at IS NULL''').fetchone()
            repairs = db.execute('''SELECT count(*) FROM local_text_documents i
                JOIN versions v ON v.id=i.version_id JOIN sources s ON s.id=v.source_id
                WHERE v.active=1 AND s.forgotten_at IS NULL AND
                (NOT json_valid(i.metadata_json) OR NOT EXISTS(SELECT 1 FROM local_text_spans p WHERE p.version_id=v.id) OR
                 ((CASE WHEN json_valid(i.metadata_json) THEN json_extract(i.metadata_json,'$.metadata.artifact_type') END)='research_package'
                  AND NOT EXISTS(SELECT 1 FROM local_text_dependencies d WHERE d.package_version=v.id)))''').fetchone()[0]
        return {'active': row[0], 'indexed': row[1], 'unindexed': row[0]-row[1], 'text_bytes': row[2],
                'dependency_repairs': repairs, 'profile': 'sqlite-fts5-trigram-v1'}

    def query(self, terms, *, version_ids=None):
        terms = sorted({t for t in terms if len(t) >= 3})
        if not terms:
            return []
        expression = ' OR '.join('"' + t.replace('"', '""') + '"' for t in terms)
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('''SELECT f.version_id,f.start,f.body,f.title,f.rank AS bm25_score,
                i.metadata_json,i.text_sha256,i.manifest_sha256,i.text_bytes,
                p.byte_start,p.byte_length,p.sha256 AS span_sha256
                FROM local_text_fts f JOIN local_text_documents i ON i.version_id=f.version_id
                LEFT JOIN local_text_spans p ON p.version_id=f.version_id AND p.start=CAST(f.start AS INTEGER)
                JOIN versions v ON v.id=f.version_id JOIN sources s ON s.id=v.source_id
                WHERE local_text_fts MATCH ? AND v.active=1 AND s.forgotten_at IS NULL
                AND (? IS NULL OR f.version_id IN (SELECT value FROM json_each(?)))
                ORDER BY rank LIMIT ?''', (expression, None if version_ids is None else 1,
                                           json.dumps(sorted(version_ids or [])), MAX_HITS+1))]

    def verify_hit(self, hit):
        """Check the exact returned chunk, anchored when the full text was indexed."""
        vid = hit['version_id']
        manifest = self.store.read_manifest(vid)
        if (manifest != json.loads(hit['metadata_json']) or manifest['text_sha256'] != hit['text_sha256']
                or digest((self.store.versions/vid/'manifest.json').read_bytes()) != hit['manifest_sha256']):
            raise PreservationError('text_index_manifest_mismatch')
        start, length = hit.get('byte_start'), hit.get('byte_length')
        if type(start) is not int or type(length) is not int or start < 0 or not 0 <= length <= CHUNK_CHARS*4:
            raise PreservationError('text_index_span_unavailable')
        path = self.store.versions/vid/'text.txt'
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != hit['text_bytes']:
                raise PreservationError('text_index_source_changed')
            stream.seek(start)
            raw = stream.read(length)
            after = os.fstat(stream.fileno())
        if (before.st_size,before.st_mtime_ns) != (after.st_size,after.st_mtime_ns) or digest(raw) != hit['span_sha256']:
            raise PreservationError('text_index_span_mismatch')
        body = raw.decode('utf-8')
        if body != hit['body']:
            raise PreservationError('text_index_span_mismatch')
        guard_no_secrets(raw)
        return manifest, body
