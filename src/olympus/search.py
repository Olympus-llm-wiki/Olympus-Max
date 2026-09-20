"""Broad, provenance-preserving discovery over registered sources only."""
from __future__ import annotations

from collections import Counter
import copy
import json
import math
import queue
import re
import threading
import time

from .delivery import recall_active
from .evidence import report_links, report_bindings_current
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
STOP.update('how what why when where who which does did are was were should would could can many during about please tell these those'.split())
MAX_VERSION_BYTES = 64 * 1024 * 1024
MAX_SCAN_BYTES = 256 * 1024 * 1024
NATIVE_DEADLINE_SECONDS = 5.0
_NATIVE_READ_SLOTS = threading.BoundedSemaphore(4)


def _deadline_recall(store, client, query, scope, package_id, remaining):
    """Bound native transport only; no detached task may continue using Store."""
    deadline = time.monotonic() + remaining
    class BoundedTransport:
        def recall(self, *args, **kwargs):
            budget = deadline-time.monotonic()
            if budget <= 0:
                raise HindsightError('deadline_exceeded')
            transport = copy.copy(client)
            if not _NATIVE_READ_SLOTS.acquire(timeout=budget):
                raise HindsightError('deadline_exceeded')
            budget = deadline-time.monotonic()
            if budget <= 0:
                _NATIVE_READ_SLOTS.release()
                raise HindsightError('deadline_exceeded')
            if hasattr(transport, 'timeout'):
                transport.timeout = min(transport.timeout, budget)
            output = queue.Queue(maxsize=1)
            def read():
                try:
                    output.put((True, transport.recall(*args, **kwargs)))
                except Exception as exc:
                    output.put((False, exc))
                finally:
                    _NATIVE_READ_SLOTS.release()
            # The worker only owns transport/result bytes. Late output is
            # discarded; all provenance/Store work stays on the caller thread.
            threading.Thread(target=read, daemon=True, name='olympus-native-transport').start()
            try:
                success, result = output.get(timeout=budget)
            except queue.Empty:
                raise HindsightError('deadline_exceeded') from None
            if not success:
                raise result
            return result
    return recall_active(store, BoundedTransport(), query, scope, package_id=package_id)


def expand_query(query):
    # Attribution boilerplate isn't the subject being looked up. Keep explicit
    # uppercase abbreviations (e.g. WHO) even when they resemble a stop word.
    lexical = re.sub(r'\b(?:in|from|according to) (?:the|this) (?:source|document|text)\b', ' ', query, flags=re.I)
    original = {t.casefold() for t in re.findall(r'[^\W_]{3,}', lexical)
                if t.casefold() not in STOP or (t.isascii() and t.isupper())}
    groups = [name for name, triggers, _ in CONCEPTS if any(t in query.casefold() for t in triggers)]
    expanded = {term for name, _, terms in CONCEPTS if name in groups for term in terms} - original
    return original, expanded, groups


def _score(text, original, expanded):
    text = text.casefold()
    direct = sorted(t for t in original if t in text)
    related = sorted(t for t in expanded if t in text)
    return len(direct) * 3 + len(related), direct + related


def _excerpt_start(body, terms, original, *, length=1400):
    """Select a dense answer window instead of an alphabetically first term."""
    fillers = {'how','what','does','do','did','should','would','could','many'}
    meaningful = [term for term in terms if term not in fillers] or terms
    weights = {term:3 if term in original else 1 for term in meaningful}
    events = sorted((match.start(),term) for term in meaningful
                    for match in re.finditer(re.escape(term),body,re.IGNORECASE))
    starts = sorted({0} | {max(0,position-150) for position,_ in events})
    counts, left, right, score = Counter(), 0, 0, 0
    best_start, best_score = 0, -1
    for start in starts:
        while right < len(events) and events[right][0] < start+length:
            term=events[right][1]
            if not counts[term]:score += weights[term]
            counts[term] += 1;right += 1
        while left < right and events[left][0] < start:
            term=events[left][1];counts[term] -= 1
            if not counts[term]:score -= weights[term]
            left += 1
        if score > best_score:
            best_start,best_score=start,score
    return best_start


def _live_ids(store, scopes):
    from .withholding import eligible_versions
    rows = eligible_versions(store)
    return {v: info for v, info in rows.items() if scopes is None or info[0] in scopes}


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
                  include_discussions=False, limit=20, max_semantic_scopes=4,
                  native_deadline_seconds=NATIVE_DEADLINE_SECONDS):
    if not query.strip() or len(query) > 10000 or not 1 <= limit <= 100:
        raise PreservationError('invalid_search_query_or_limit')
    if (not isinstance(native_deadline_seconds, (int, float)) or not math.isfinite(native_deadline_seconds)
            or native_deadline_seconds <= 0):
        raise PreservationError('invalid_native_deadline')
    guard_no_secrets(query.encode())
    original, expanded, groups = expand_query(query)
    scope_set = set(scopes) if scopes else None
    permitted = package_versions(store, package_id) if package_id else None
    coverage = {'skipped_versions': [], 'semantic': [], 'absence_proven': False}
    from .text_index import TextIndex, MAX_HITS
    index = TextIndex(store)
    versions, matches, discussions = {}, [], []
    used = 0
    excluded = Counter()
    from .withholding import eligible_versions
    eligibility = eligible_versions(store, diagnostics=True)
    all_live = eligibility['eligible']
    cards, catalog_state = _catalog(store, all_live, original, expanded, package_id)
    live = {v: info for v, info in all_live.items() if (scope_set is None or info[0] in scope_set)
            and (permitted is None or v in permitted)}
    coverage['catalog'] = catalog_state
    coverage['scopes'] = dict(Counter(info[0] for info in live.values()))
    coverage['delivery_states'] = dict(Counter(info[1] for info in live.values()))
    coverage['local_index'] = index.coverage()
    coverage['withheld_version_count'] = len(eligibility['withheld'])
    coverage['withheld_versions'] = eligibility['withheld'][:20]
    hits = index.query(original | expanded, version_ids=set(live))
    coverage['index_hits_truncated'] = len(hits) > MAX_HITS
    best = {}
    for hit in hits[:MAX_HITS]:
        vid = hit['version_id']
        if vid not in live:
            continue
        manifest = json.loads(hit['metadata_json'])
        profile = material_profile(manifest)
        role = profile['material_role']
        if role in {'catalog', 'artifact', 'assessment'} or (role == 'decision' and not profile['current_owner_decision']):
            excluded[role] += 1
            continue
        if role == 'discussion' and not include_discussions:
            excluded[role] += 1
            continue
        lexical_score, terms = _score(hit['title'] + '\n' + hit['body'], original, expanded)
        if not lexical_score or not hit['body'].strip():
            continue
        # Preserve corpus frequency and length normalization. Counting distinct
        # matches here promoted long generic documents over concise answers.
        score = -float(hit['bm25_score'])
        if vid not in best or score > best[vid][0]:
            best[vid] = (score, terms, hit)
    candidates = sorted(best.items(), key=lambda item: (-item[1][0], item[0]))
    verification_limit = max(limit * 3, limit + 20)
    coverage['candidate_verification_truncated'] = len(candidates) > verification_limit
    for vid, (score, terms, hit) in candidates[:verification_limit]:
        try:
            # Candidate text verification happens outside the writer lock.
            text_size = hit.get('byte_length')
            if type(text_size) is not int:
                raise PreservationError('text_index_span_unavailable')
            if text_size > MAX_VERSION_BYTES or used + text_size > MAX_SCAN_BYTES:
                coverage['skipped_versions'].append({'version_id': vid, 'reason': 'byte_limit'})
                continue
            used += text_size
            version, body = index.verify_hit(hit)
            profile = material_profile(version)
            role = profile['material_role']
            if role in {'catalog','artifact','assessment'} or (role == 'decision' and not profile['current_owner_decision']):
                continue
            if role == 'discussion' and not include_discussions:
                continue
            offset = int(hit['start'])
            local_start = _excerpt_start(body,terms,original)
            start = offset + local_start
            excerpt = body[local_start:local_start+1400]
            versions[vid] = version
            item = {'version_id': vid, 'scope': live[vid][0], 'score': score, 'matched_terms': terms,
                    'match_method': 'local_fts5_trigram', 'ranking_method':'fts5_bm25', 'delivery_state': live[vid][1],
                    'excerpt': excerpt, 'source': source_provenance(version),
                    'anchor': {'text_sha256': version['text_sha256'], 'start': start,
                               'end': start+len(excerpt), 'unit': 'unicode_codepoints',
                               'span_sha256':hit['span_sha256'], 'integrity':'verified_span_and_manifest'}}
            (discussions if material_profile(version)['material_role'] == 'discussion' else matches).append(item)
        except (PreservationError, OSError):
            coverage['skipped_versions'].append({'version_id': vid, 'reason': 'source_unreadable_or_integrity_failed'})
    native = []
    priorities = Counter()
    for m in matches:
        priorities[m['scope']] += m['score']
    searchable_scopes = sorted({s for s, state in live.values() if state == 'searchable'}, key=lambda s: (-priorities[s], s))
    native_deadline = time.monotonic() + native_deadline_seconds
    coverage['native_deadline_seconds'] = native_deadline_seconds
    for i, scope in enumerate(searchable_scopes):
        if client is None or i >= max_semantic_scopes:
            coverage['semantic'].append({'scope': scope, 'state': 'not_requested' if client is None else 'scope_limit'})
            continue
        try:
            remaining = native_deadline - time.monotonic()
            if remaining <= 0:
                coverage['semantic'].append({'scope': scope, 'state': 'deadline_exceeded'})
                continue
            result = _deadline_recall(store, client, query + '\nRelated terms: ' + ' '.join(sorted(expanded)),
                                      scope, package_id, remaining)
            native.append(result)
            coverage['semantic'].append({'scope': scope, 'state': 'checked'})
        except HindsightError as exc:
            coverage['semantic'].append({'scope': scope, 'state': 'unavailable', 'reason': exc.code})
        except PreservationError as exc:
            coverage['semantic'].append({'scope': scope, 'state': 'unavailable', 'reason': str(exc)})
    # Citation/source checks run outside writer locks. Their SQL-only guards
    # are compared again under the final withdrawal snapshot.
    held, bindings, binding_snapshots = set(), {}, []
    candidate_ids = {m['version_id'] for m in matches + discussions}
    candidate_ids.update(row.get('document_id') for result in native for row in result.get('results', []))
    for scope in {s for v,(s,_) in live.items() if v in candidate_ids}:
        ids = {v for v in candidate_ids if v in live and live[v][0] == scope}
        links = report_links(store, ids, scope)
        binding_snapshots.append((ids, links))
        bindings.update(links)
        held.update(v for v, checks in links.items() if any(c['state'] != 'ready_for_review' for c in checks))
    # Revalidate every exposed result after network calls. No lock held over HTTP.
    with store.exclusive():
        eligibility = eligible_versions(store, diagnostics=True)
        all_final = eligibility['eligible']
        coverage['withheld_version_count'] = len(eligibility['withheld'])
        coverage['withheld_versions'] = eligibility['withheld'][:20]
        final = {v:info for v,info in all_final.items() if scope_set is None or info[0] in scope_set}
        if permitted is not None:
            final = {v: s for v, s in final.items() if v in package_versions(store, package_id)}
        for ids, snapshot in binding_snapshots:
            if not report_bindings_current(store, snapshot):
                held.update(ids)
        def safe(items):
            answer = []
            for item in items:
                vid = item['version_id']
                if vid not in final or vid in held:
                    continue
                if vid in bindings:
                    item['source']['research_packages'] = bindings[vid]
                answer.append(item)
            return sorted(answer, key=lambda m: (-m['score'], m['version_id']))
        matches, discussions = safe(matches), safe(discussions)
        for result in native:
            result['results'] = [r for r in result.get('results', []) if r.get('document_id') in final and r.get('document_id') not in held]
            allowed_chunks = {r.get('chunk_id') for r in result['results']}
            chunks = result.get('chunks', {})
            result['chunks'] = ({k: v for k, v in chunks.items() if k in allowed_chunks} if isinstance(chunks, dict)
                                else [v for v in chunks if v.get('document_id') in {r['document_id'] for r in result['results']}])
        catalog_now = json.loads(store.setting('legacy_catalog', '{}')).get('version_id')
        cards = [card for card in cards if card['catalog_version_id'] == catalog_now and catalog_now in all_final]
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
