"""Explain availability separately from process heartbeat."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import time


def pipeline_health(store):
    from .jobs import PipelineJobs
    from .text_index import TextIndex
    now = time.time()
    stages = PipelineJobs(store).snapshot()
    issues = []
    for row in stages:
        if row['state'] == 'failed' or row['lease_expired']:
            issues.append({'stage': row['kind'], 'object_id': row['object_id'],
                           'reason': row['error_code'] or 'lease_expired',
                           'scope': row['failure_scope'] or 'stage'})
    services = {}
    for mode in ('collect', 'library', 'delivery', 'backup'):
        heartbeat = store.setting('heartbeat:' + mode)
        try:
            age = now-datetime.fromisoformat(heartbeat.replace('Z', '+00:00')).timestamp() if heartbeat else None
        except (ValueError, TypeError):
            age = None
        stale_after = 120 if mode != 'library' else 300
        try:
            reported = json.loads(store.setting('issues:' + mode, '[]'))
            if not isinstance(reported, list) or any(not isinstance(item, dict) for item in reported):
                raise ValueError
        except (ValueError, TypeError):
            reported = [{'code':'service_status_invalid'}]
        services[mode] = {'heartbeat': heartbeat, 'age_seconds': age,
                          'stale': age is None or age > stale_after,
                          'last_attempt': store.setting('last_attempt:' + mode),
                          'issues': reported}
        active_snapshot = next((r for r in stages if r['kind'] == 'backup.snapshot'
                                and r['state'] == 'running' and not r['lease_expired']), None) if mode == 'backup' else None
        if active_snapshot:
            services[mode]['stale'] = False
            services[mode]['state'] = 'busy'
            services[mode]['last_progress'] = active_snapshot['last_progress']
            if active_snapshot['last_progress'] and now-active_snapshot['last_progress'] > 300:
                issues.append({'stage': 'backup.snapshot', 'reason': 'snapshot_progress_stale', 'scope': 'stage'})
        if services[mode]['stale']:
            issues.append({'stage': mode, 'reason': 'service_stale', 'scope': 'stage'})
        for issue in reported:
            reason = issue.get('code') or issue.get('error') or ('capture_coverage_gap' if issue.get('coverage_gaps') else 'service_reported_issue')
            issues.append({'stage':mode,'reason':reason,'scope':'stage',
                           **{key:issue[key] for key in ('version_id','thread_id','errno','coverage_gaps') if key in issue}})
    try:
        capture = json.loads(store.setting('hook_signal_status', '{}'))
        if not isinstance(capture, dict):
            raise ValueError
    except (ValueError, TypeError):
        capture = {'state':'degraded', 'errors':[{'code':'capture_status_invalid'}]}
        issues.append({'stage':'capture','reason':'capture_status_invalid','scope':'stage'})
    if capture.get('state') not in {'healthy', 'ok', 'idle', 'ready'} or capture.get('pending_total') or capture.get('errors'):
        issues.append({'stage': 'capture', 'reason': 'capture_coverage_unproven', 'scope': 'stage'})
    index = TextIndex(store).coverage()
    pending_index = index['unindexed'] + index.get('dependency_repairs', 0)
    if pending_index:
        issues.append({'stage': 'indexing', 'reason': 'local_index_pending', 'count': pending_index, 'scope': 'stage'})
    from .reference_profiles import processing_profile_summary
    profiles = processing_profile_summary(store)
    for profile in profiles['versions']:
        if profile['state'] != 'profile_complete':
            issues.append({'stage': 'processing_profile', 'version_id': profile['version_id'],
                           'reason': profile.get('reason', 'profile_requires_revalidation'), 'scope': 'source'})
    quota = None
    raw_quota = store.setting('provider_quota_notice')
    if raw_quota:
        try:
            saved = json.loads(raw_quota)
            if (saved['reason'] != 'usage_limit_reached' or type(saved['reset_at']) is not int
                    or type(saved.get('native_account_matches_current_codex_account')) is not bool):
                raise ValueError
            quota = {'reason': 'usage_limit_reached', 'reset_at': saved['reset_at'],
                     'observed_at': saved.get('checked_at'),
                     'native_account_matches_current_codex_account': saved['native_account_matches_current_codex_account'],
                     'state': 'waiting_for_reset' if saved['reset_at'] > now else 'requires_fresh_verification'}
        except (ValueError, TypeError, KeyError):
            quota = {'state': 'unverified', 'reason': 'provider_quota_notice_invalid'}
        issues.append({'stage': 'provider', 'reason': quota['reason'], 'scope': 'provider'})
    return {'state': 'degraded' if issues else 'observed_stages_current', 'checked_at': datetime.now(timezone.utc).isoformat(),
            'stages': stages, 'services': services, 'capture': capture, 'local_index': index, 'issues': issues,
            'processing_profiles': profiles,
            'provider_quota': quota,
            'recovery_verified': False, 'release_ready': False,
            'interpretation': 'Stage observations do not prove complete corpus or remote recovery; release acceptance is separate.'}
