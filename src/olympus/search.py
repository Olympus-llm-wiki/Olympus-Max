"""Broad, provenance-preserving discovery over registered sources only."""
from __future__ import annotations

from collections import Counter
import json
import re

from .delivery import recall_active
from .evidence import report_links
from .hindsight import HindsightError
from .materials import material_profile, package_versions, source_provenance
from .preservation import PreservationError, guard_no_secrets

# Explicit, inspectable bilingual associations, not inferred facts about a source.
CONCEPTS = (
    ('visual', ('генерац', 'изображ', 'купаль', 'косплей', 'lifestyle', 'livestyle',
                'swimwear', 'bikini', 'cosplay', 'ugc', 'инфлюенс', 'outfit'),
     ('image', 'lifestyle', 'ugc', 'инфлюенс', 'lora', 'comfyui', 'identity', 'consistency', 'одежд')),
    ('identity', ('lora', 'лора', 'лоры', 'внешност', 'персонаж', 'консистент', 'identity', 'consistency'),
     ('lora', 'identity', 'character', 'dataset', 'датасет', 'обучен', 'training', 'caption')),
    ('wardrobe', ('купаль', 'косплей', 'одежд', 'swimwear', 'bikini', 'cosplay', 'outfit'),
     ('swimwear', 'bikini', 'cosplay', 'outfit', 'clothing', 'купаль', 'косплей', 'одежд')),
    ('memory', ('памят', 'поиск', 'memory', 'recall', 'retrieval'),
     ('memory', 'recall', 'retrieval', 'памят', 'поиск')),
    ('speech', ('транскри', 'расшифр', 'transcript', 'audio', 'аудио'),
     ('transcript', 'audio', 'транскри', 'расшифр', 'аудио')),
)
STOP = set('вот у нас был была было получается инфой оттуда подскажи они как как-то что что-то про это есть или то какие какие-то далее корпус корпуса инфа инфой информация информации рассказывали рассказывать условно нюансов слушай the and this that with from have'.split())
MAX_VERSION_BYTES = 64 * 1024 * 1024
MAX_SCAN_BYTES = 256 * 1024 * 1024


def expand_query(query):
    original = {t for t in re.findall(r'[^\W_]{3,}', query.casefold()) if t not in STOP}
    groups = [name for name, triggers, _ in CONCEPTS if any(t in query.casefold() for t in triggers)]
    expanded = {term for name, _, terms in CONCEPTS if name in groups for term in terms} - original
    return original, expanded, groups


def _score(text, original, expanded):
    text = text.casefold()
    direct = sorted(t for t in original if t in text)
    related = sorted(t for t in expanded if t in text)
    return len(direct) * 3 + len(related), direct + related


def _live_ids(store, scopes):
    # active_documents applies the global recovery and correction barriers even
    # though local retrieval intentionally also admits undelivered versions.
    store.active_documents('')
    with store.connect() as db:
        rows = db.execute('''SELECT v.id, s.scope, d.state FROM versions v
            JOIN sources s ON s.id=v.source_id JOIN delivery d ON d.version_id=v.id
            WHERE v.active=1 AND s.forgotten_at IS NULL ORDER BY v.id''').fetchall()
    return {r[0]: (r[1], r[2]) for r in rows if scopes is None or r[1] in scopes}


def _catalog(store, live, original, expanded, package_id):
    state = json.loads(store.setting('legacy_catalog', '{}'))
    vid = state.get('version_id')
    if not vid or vid not in live:
        return [], 'unavailable'
    catalog = json.loads(store.read_version(vid)['original'])
    cards = []
    for row in catalog['rows']:
        if package_id and row['id'] != package_id:
            continue
        score, terms = _score(' '.join(str(row.get(k, '')) for k in ('title', 'category', 'description')), original, expanded)
        if not score and not package_id:
            continue
        registered = store.setting('legacy_package:' + row['id']) is not None
        ids = package_versions(store, row['id']) if registered else set()
        active = ids & live.keys()
        cards.append({**{k: row.get(k) for k in ('id', 'title', 'category', 'description', 'locations')},
                      'score': score, 'matched_terms': terms, 'catalog_version_id': vid,
                      'state': 'registered' if active else 'catalog_only',
                      'active_versions': len(active), 'searchable_versions': sum(live[v][1] == 'searchable' for v in active),
                      'interpretation': 'Navigation only; source content has not been established by this card.'})
    return sorted(cards, key=lambda c: (-c['score'], c['id'])), 'checked'


def search_corpus(store, query, *, client=None, scopes=None, package_id=None,
                  include_discussions=False, limit=20, max_semantic_scopes=4):
    if not query.strip() or len(query) > 10000 or not 1 <= limit <= 100:
        raise PreservationError('invalid_search_query_or_limit')
    guard_no_secrets(query.encode())
    original, expanded, groups = expand_query(query)
    scope_set = set(scopes) if scopes else None
    permitted = package_versions(store, package_id) if package_id else None
    coverage = {'skipped_versions': [], 'semantic': [], 'absence_proven': False}
    versions, matches, discussions = {}, [], []
    used = 0
    excluded = Counter()
    with store.exclusive():
        all_live = _live_ids(store, None)
        cards, catalog_state = _catalog(store, all_live, original, expanded, package_id)
        live = {v: info for v, info in all_live.items() if (scope_set is None or info[0] in scope_set)
                and (permitted is None or v in permitted)}
        coverage['catalog'] = catalog_state
        coverage['scopes'] = dict(Counter(info[0] for info in live.values()))
        coverage['delivery_states'] = dict(Counter(info[1] for info in live.values()))
        # Verify small manifests before touching bulky binary originals or discussions.
        candidates = []
        for vid, (scope, state) in live.items():
            try:
                manifest = store.read_manifest(vid)
                role = material_profile(manifest)['material_role']
                if role in {'catalog', 'artifact', 'assessment'} or (role == 'decision' and not material_profile(manifest)['current_owner_decision']):
                    excluded[role] += 1
                    continue
                if role == 'discussion' and not include_discussions:
                    excluded[role] += 1
                    continue
                folder = store.versions / vid
                size = sum((folder / n).stat().st_size for n in ('original', 'text.txt', 'manifest.json'))
                candidates.append((size, vid, scope, state))
            except (PreservationError, OSError):
                coverage['skipped_versions'].append({'version_id': vid, 'reason': 'manifest_unreadable_or_integrity_failed'})
        for _, vid, scope, state in sorted(candidates):
            folder = store.versions / vid
            try:
                size = sum((folder / n).stat().st_size for n in ('original', 'text.txt', 'manifest.json'))
                if size > MAX_VERSION_BYTES or used + size > MAX_SCAN_BYTES:
                    coverage['skipped_versions'].append({'version_id': vid, 'reason': 'byte_limit'})
                    continue
                used += size
                version = store.read_version(vid)
                profile = material_profile(version)
                role = profile['material_role']
                if role in {'catalog', 'artifact', 'assessment'} or (role == 'decision' and not profile['current_owner_decision']):
                    continue
                if role == 'discussion' and not include_discussions:
                    continue
                versions[vid] = version
                score, terms = _score(version['title'] + '\n' + version['text'], original, expanded)
                if not score or not version['text'].strip():
                    continue
                # Prefer the strongest original match, not the earliest expanded word.
                body = version['text']; folded = body.casefold()
                positions = [folded.find(t) for t in terms if t in folded]
                start = max(0, (positions[0] if positions else 0) - 150)
                item = {'version_id': vid, 'scope': scope, 'score': score, 'matched_terms': terms,
                        'match_method': 'local_lexical_expanded', 'delivery_state': state,
                        'excerpt': body[start:start + 1400], 'source': source_provenance(version)}
                (discussions if role == 'discussion' else matches).append(item)
            except (PreservationError, OSError):
                coverage['skipped_versions'].append({'version_id': vid, 'reason': 'source_unreadable_or_integrity_failed'})
    native = []
    priorities = Counter()
    for m in matches:
        priorities[m['scope']] += m['score']
    searchable_scopes = sorted({s for s, state in live.values() if state == 'searchable'}, key=lambda s: (-priorities[s], s))
    for i, scope in enumerate(searchable_scopes):
        if client is None or i >= max_semantic_scopes:
            coverage['semantic'].append({'scope': scope, 'state': 'not_requested' if client is None else 'scope_limit'})
            continue
        try:
            result = recall_active(store, client, query + '\nRelated terms: ' + ' '.join(sorted(expanded)), scope,
                                   package_id=package_id)
            native.append(result)
            coverage['semantic'].append({'scope': scope, 'state': 'checked'})
        except HindsightError as exc:
            coverage['semantic'].append({'scope': scope, 'state': 'unavailable', 'reason': exc.code})
    # Revalidate every exposed result after network calls. No lock held over HTTP.
    with store.exclusive():
        final = _live_ids(store, scope_set)
        if permitted is not None:
            final = {v: s for v, s in final.items() if v in package_versions(store, package_id)}
        held, bindings = set(), {}
        for scope in {s for s, _ in final.values()}:
            links = report_links(store, {v for v, (s, _) in final.items() if s == scope}, scope)
            bindings.update(links)
            held.update(v for v, checks in links.items() if any(c['state'] != 'ready_for_review' for c in checks))
        def safe(items):
            answer = []
            for item in items:
                vid = item['version_id']
                if vid not in final or vid in held:
                    continue
                store.read_version(vid)
                if vid in bindings:
                    item['source']['research_packages'] = bindings[vid]
                answer.append(item)
            return sorted(answer, key=lambda m: (-m['score'], m['version_id']))
        matches, discussions = safe(matches), safe(discussions)
        for result in native:
            result['results'] = [r for r in result.get('results', []) if r.get('document_id') in final and r.get('document_id') not in held]
            for row in result['results']:
                store.read_version(row['document_id'])
            allowed_chunks = {r.get('chunk_id') for r in result['results']}
            chunks = result.get('chunks', {})
            result['chunks'] = ({k: v for k, v in chunks.items() if k in allowed_chunks} if isinstance(chunks, dict)
                                else [v for v in chunks if v.get('document_id') in {r['document_id'] for r in result['results']}])
        cards, _ = _catalog(store, _live_ids(store, None), original, expanded, package_id)
        if scope_set is not None:
            cards = [c for c in cards if 'legacy' in scope_set or
                     (store.setting('legacy_package:' + c['id']) is not None and
                      bool(package_versions(store, c['id']) & final.keys()))]
        coverage['excluded_roles'] = dict(excluded)
        coverage['skipped_version_count'] = len(coverage['skipped_versions'])
        coverage['skipped_reasons'] = dict(Counter(v['reason'] for v in coverage['skipped_versions']))
        coverage['skipped_versions_truncated'] = len(coverage['skipped_versions']) > 20
        coverage['skipped_versions'] = coverage['skipped_versions'][:20]
        coverage.update({'bytes_checked': used, 'local_versions_checked': len(versions),
                         'local_matches': len(matches), 'results_truncated': len(matches) > limit,
                         'catalog_matches': len(cards), 'catalog_truncated': len(cards) > limit,
                         'discussions_truncated': len(discussions) > limit,
                         'withheld_reports': sorted(held)})
        return {'query': query, 'expansion': {'groups': groups, 'terms': sorted(expanded)},
                'filters': {'scopes': sorted(scope_set) if scope_set else None, 'package': package_id},
                'catalog': cards[:limit], 'results': matches[:limit], 'semantic_results': native,
                'discussions': discussions[:limit], 'coverage': coverage,
                'interpretation': 'Source claims, not verified facts. Catalog cards are navigation. Empty results do not prove absence.'}
