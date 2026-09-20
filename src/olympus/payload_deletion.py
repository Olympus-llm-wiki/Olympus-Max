"""Explicit owner deletion of selected archived payloads; identity remains auditable."""
from __future__ import annotations

import json
import os

from .preservation import PreservationError, canonical, digest, guard_no_secrets, timestamp, _sync_dir


def ensure_schema(store):
    with store.connect(write=True) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS payload_deletions (
            version_id TEXT PRIMARY KEY REFERENCES versions(id), schema_version INTEGER NOT NULL,
            owner_wording TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
            deleted_at TEXT NOT NULL, receipt_json TEXT NOT NULL)''')


def delete_archived_payloads(store, version_ids, *, owner_wording):
    """No inferred selection, no external path traversal, no native/library deletion."""
    if not owner_wording.strip() or not version_ids:
        raise PreservationError('explicit_payload_deletion_scope_required')
    guard_no_secrets(owner_wording.encode())
    ensure_schema(store)
    selected = set(version_ids)
    plans = []
    with store.exclusive():
        # Complete the preflight before the first deletion.
        for vid in sorted(selected):
            manifest = store.read_manifest(vid)
            if manifest.get('metadata', {}).get('material_role') != 'artifact':
                raise PreservationError('payload_deletion_requires_archived_artifact')
            folder = store.versions / vid
            if set(p.name for p in folder.iterdir()) - {'original', 'text.txt', 'manifest.json', 'manifest.sha256'}:
                raise PreservationError('payload_deletion_unknown_extra_files')
            with store.connect() as db:
                row = db.execute('SELECT state,attempts FROM delivery WHERE version_id=?', (vid,)).fetchone()
                siblings = {r[0] for r in db.execute('SELECT id FROM versions WHERE source_id=?', (manifest['source_id'],))}
                existing = db.execute('SELECT * FROM payload_deletions WHERE version_id=?', (vid,)).fetchone()
            if not siblings <= selected or row['state'] not in {'archived', 'forgotten'} or row['attempts']:
                raise PreservationError('payload_deletion_requires_complete_never_submitted_source')
            if existing and existing['manifest_sha256'] != digest((folder/'manifest.json').read_bytes()):
                raise PreservationError('payload_deletion_anchor_mismatch')
            sizes = {}
            for name, hash_key in [('original', 'original_sha256'), ('text.txt', 'text_sha256')]:
                path = folder/name
                if path.exists():
                    import hashlib
                    h = hashlib.sha256()
                    with path.open('rb') as stream:
                        for block in iter(lambda: stream.read(1024*1024), b''):
                            h.update(block)
                    if h.hexdigest() != manifest[hash_key]:
                        raise PreservationError('source_hash_mismatch')
                    sizes[name] = path.stat().st_size
                elif not existing:
                    raise PreservationError('payload_deletion_missing_unrecorded_bytes')
            plans.append((manifest, sizes))
        for manifest, sizes in plans:
            vid = manifest['version_id']; folder = store.versions/vid
            store.forget(manifest['source_id'], owner_wording)
            receipt = {'state': 'planned', 'version_id': vid, 'bytes': sizes,
                       'native': 'never_submitted', 'external_copies': 'separate_verification_required'}
            with store.connect(write=True) as db:
                db.execute('INSERT OR IGNORE INTO payload_deletions VALUES(?,?,?,?,?,?)',
                    (vid, 1, owner_wording, digest((folder/'manifest.json').read_bytes()), timestamp(), canonical(receipt).decode()))
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='local_text_documents'").fetchone():
                    db.execute('DELETE FROM local_text_fts WHERE version_id=?', (vid,))
                    db.execute('DELETE FROM local_text_documents WHERE version_id=?', (vid,))
            for name in ('original', 'text.txt'):
                (folder/name).unlink(missing_ok=True)
            _sync_dir(folder)
            receipt['state'] = 'removed'
            with store.connect(write=True) as db:
                db.execute('UPDATE payload_deletions SET receipt_json=? WHERE version_id=?', (canonical(receipt).decode(), vid))
        return {'removed': len(plans), 'payload_bytes': sum(sum(s.values()) for _, s in plans),
                'version_ids': sorted(selected), 'external_copies': 'separate_verification_required'}
