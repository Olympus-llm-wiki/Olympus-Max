"""Durable source/dependency eligibility with explicit archival exceptions."""
from __future__ import annotations

import json

from .materials import material_profile
from .preservation import PreservationError


def eligible_versions(store, *, scope=None, delivered_only=False, diagnostics=False):
    with store.connect() as db:
        recovery = db.execute("SELECT value FROM settings WHERE key='recovery_state'").fetchone()
        if recovery and recovery[0] != 'ready':
            raise PreservationError('recovery_verification_required')
        changes = [dict(r) for r in db.execute('SELECT id,kind,source_id,applied FROM changes')]
        pending = any(not row['applied'] for row in changes)
        rows = db.execute('''SELECT v.id,v.source_id,v.active,s.forgotten_at,s.scope,d.state
            FROM versions v JOIN sources s ON s.id=v.source_id JOIN delivery d ON d.version_id=v.id''').fetchall()
        has_index = db.execute("SELECT 1 FROM sqlite_master WHERE name='local_text_documents'").fetchone()
        metadata, corrupt = {}, set()
        if has_index:
            for vid, raw in db.execute('SELECT version_id,metadata_json FROM local_text_documents'):
                try:
                    parsed = json.loads(raw)
                    material_profile(parsed)
                    metadata[vid] = parsed
                except (ValueError, TypeError, AttributeError, PreservationError):
                    corrupt.add(vid)
        has_dependencies = db.execute("SELECT 1 FROM sqlite_master WHERE name='local_text_dependencies'").fetchone()
        research_dependencies = list(db.execute('''SELECT d.report_version,d.source_version,d.package_version
            FROM local_text_dependencies d JOIN settings s ON s.value=d.package_version
            WHERE substr(s.key,1,17)='research_package:' ''')) if has_dependencies else []
        pending_sources = {r[0] for r in db.execute('SELECT DISTINCT source_id FROM changes WHERE applied=0')}
        deleted = set()
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='payload_deletions'").fetchone():
            deleted = {r[0] for r in db.execute('SELECT version_id FROM payload_deletions')}
        attempts = {r[0]:r[1] for r in db.execute('SELECT version_id,attempts FROM delivery')}
        archive_receipts = {}
        for key, raw in db.execute("SELECT key,value FROM settings WHERE substr(key,1,19)='correction_receipt:'"):
            try:
                archive_receipts[int(key.split(':')[1])] = json.loads(raw)
            except (ValueError, TypeError):
                continue
    active = {r['id'] for r in rows if r['active'] and r['forgotten_at'] is None}
    all_ids = {r['id'] for r in rows}
    sources = {}
    for row in rows:
        sources.setdefault(row['source_id'], set()).add(row['id'])
    semantic_history = False
    for change in changes:
        proof = archive_receipts.get(change['id'], {})
        versions = sources.get(change['source_id'], set())
        archive_only = (change['kind'] == 'forget' and bool(versions) and versions <= deleted
            and all(attempts.get(vid) == 0 for vid in versions) and isinstance(proof, dict)
            and proof.get('mode') == 'never-submitted-archive-deletion'
            and isinstance(proof.get('change_ids'), list) and isinstance(proof.get('document_ids'), list)
            and all(isinstance(vid, str) for vid in proof['document_ids'])
            and change['id'] in proof.get('change_ids', [])
            and set(proof.get('document_ids', [])) == versions
            and proof.get('native_document_absent') is True and proof.get('native_operation_absent') is True)
        if not archive_only:
            semantic_history = True
    held, dependencies, reasons = set(corrupt), {}, {v:{'text_index_metadata_invalid'} for v in corrupt}
    for row in rows:
        if row['source_id'] in pending_sources:
            held.add(row['id'])
            reasons.setdefault(row['id'], set()).add('source_correction_pending')
    research_reports = {row[0] for row in research_dependencies}
    for report, source, package in research_dependencies:
        dependencies.setdefault(report, set()).add(source)
    for vid in active:
        m = metadata.get(vid)
        if m is None:
            # Migration has not checked this representation; don't guess its role.
            if pending or semantic_history:
                held.add(vid)
                reasons.setdefault(vid, set()).add('unverified_material_profile')
            continue
        meta = m.get('metadata', {})
        role = material_profile(m)['material_role']
        try:
            parents = json.loads(meta.get('parent_sources', '[]'))
            if not isinstance(parents, list):
                raise ValueError
        except (ValueError, TypeError):
            parents = [None]
        deps, unknown = set(), False
        for parent in parents:
            if isinstance(parent, dict):
                parent = parent.get('version_id', parent.get('source_version', parent.get('source_id')))
            if isinstance(parent, str) and parent in all_ids:
                deps.add(parent)
            elif isinstance(parent, str) and parent in sources:
                # Source-only provenance cannot disambiguate superseded content.
                deps.update(sources[parent])
            else:
                unknown = True
        if deps:
            dependencies.setdefault(vid, set()).update(deps)
        # An extracted text with its own canonical original and no declared
        # external parents is a direct source representation. Synthesis and
        # assessment without lineage remain held after semantic corrections,
        # including changes restored from an older snapshot.
        if semantic_history and (unknown or (role in {'synthesis', 'assessment'} and not dependencies.get(vid))):
            held.add(vid)
            reasons.setdefault(vid, set()).add('unknown_derivative_dependencies')
    while True:
        available = active - held
        extra = {vid for vid, deps in dependencies.items() if not deps <= available}
        if extra <= held:
            break
        for vid in extra - held:
            reasons.setdefault(vid, set()).add('dependency_unavailable')
        held |= extra
    available = active - held
    unavailable = active & held
    eligible = {r['id']: (r['scope'], r['state']) for r in rows
            if r['id'] in available and (scope is None or r['scope'] == scope)
            and (not delivered_only or r['state'] == 'searchable')}
    if not diagnostics:
        return eligible
    withheld = [{'version_id':r['id'], 'scope':r['scope'], 'delivery_state':r['state'],
                 'reasons':sorted(reasons.get(r['id'], {'dependency_unavailable'})),
                 'research_report':r['id'] in research_reports}
                for r in rows if r['id'] in unavailable and (scope is None or r['scope'] == scope)
                and (not delivered_only or r['state'] == 'searchable')]
    return {'eligible':eligible, 'withheld':withheld}
