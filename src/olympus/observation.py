"""Bounded unattended observations; elapsed time alone never passes acceptance."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from .health import pipeline_health
from .preservation import PreservationError, canonical, digest, guard_no_secrets, _sync_dir, _write_file


@contextmanager
def _observation_lock(store):
    """Only serialize monitor writers, leaving source capture/search unblocked."""
    fd = os.open(store.root/'observation.lock', os.O_RDWR | os.O_CREAT | getattr(os,'O_NOFOLLOW',0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PreservationError('observation_lock_not_regular')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def start_observation(store, cohort_ids, *, thread_id, output_dir):
    ids = sorted(set(cohort_ids))
    if not ids or len(ids) > 100000 or any(not re.fullmatch(r'olv-[a-f0-9]{64}', v) for v in ids):
        raise PreservationError('invalid_observation_cohort')
    output = Path(output_dir).expanduser().resolve()
    if store.root not in output.parents:
        raise PreservationError('observations_require_state_directory')
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = {'schema': 1, 'started_at': time.time(), 'thread_id': thread_id,
              'cohort_ids': ids, 'cohort_sha256': digest(canonical(ids)),
              'output_dir': str(output), 'controlled_seconds': 86400,
              'observation_seconds': 259200, 'notification_channel': 'current_codex_task_when_available'}
    with store.exclusive():
        previous = store.setting('pipeline_observation')
        if previous:
            saved = json.loads(previous)
            if saved['cohort_sha256'] != config['cohort_sha256']:
                raise PreservationError('observation_cohort_change_requires_new_run')
            return saved
        store.set_setting('pipeline_observation', canonical(config).decode())
    return config


def record_observation(store, *, force=False):
    with _observation_lock(store):
        return _record_observation(store, force=force)


def _pause_record(raw):
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or not value:
            raise ValueError
        paused_at = datetime.fromisoformat(value['paused_at'].replace('Z', '+00:00'))
        if paused_at.tzinfo is None:
            raise ValueError
        return value, paused_at.timestamp()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise PreservationError('invalid_observation_pause') from None


def restart_observation(store, *, reason):
    """Begin an explicitly resumed epoch, retaining the paused run and cohort."""
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise PreservationError('observation_restart_reason_required')
    guard_no_secrets(reason.encode())
    with _observation_lock(store), store.exclusive():
        raw = store.setting('pipeline_observation')
        pause = store.setting('pipeline_observation_pause')
        if not raw or not pause:
            raise PreservationError('observation_restart_requires_paused_run')
        pause_value, _ = _pause_record(pause)
        if store.setting('delivery_mode') != 'continuous':
            raise PreservationError('observation_restart_requires_continuous')
        config = json.loads(raw)
        if digest(canonical(sorted(set(config['cohort_ids'])))) != config['cohort_sha256']:
            raise PreservationError('observation_cohort_anchor_mismatch')
        identifier = uuid.uuid4().hex
        archived = {'config': config, 'pause': pause_value,
                    'latest': store.setting('pipeline_observation_latest'), 'restart_reason': reason}
        output_base = Path(config.get('output_base', config['output_dir'])).resolve()
        if store.root not in output_base.parents:
            raise PreservationError('observations_require_state_directory')
        output = output_base / ('run-' + identifier)
        output.mkdir(parents=True, mode=0o700)
        updated = {**config, 'run_id': identifier, 'started_at': time.time(),
                   'output_base': str(output_base), 'output_dir': str(output),
                   'previous_run_sha256': digest(canonical(archived)), 'restart_reason': reason}
        with store.connect(write=True) as db:
            current = dict(db.execute("SELECT key,value FROM settings WHERE key IN ('pipeline_observation','pipeline_observation_pause','delivery_mode')"))
            if (current.get('pipeline_observation') != raw or current.get('pipeline_observation_pause') != pause
                    or current.get('delivery_mode') != 'continuous'):
                raise PreservationError('observation_restart_state_changed')
            db.execute('INSERT INTO settings VALUES(?,?)',
                       ('pipeline_observation_history:' + identifier, canonical(archived).decode()))
            db.execute('UPDATE settings SET value=? WHERE key=?',
                       (canonical(updated).decode(), 'pipeline_observation'))
            db.execute("DELETE FROM settings WHERE key IN ('pipeline_observation_pause','pipeline_observation_last','pipeline_observation_latest')")
        return updated


def _record_observation(store, *, force=False):
    raw = store.setting('pipeline_observation')
    if raw is None:
        return {'state': 'not_started'}
    config = json.loads(raw)
    now = time.time()
    if not force and now-float(store.setting('pipeline_observation_last', '0')) < 60:
        return {'state': 'not_due'}
    ids = set(config['cohort_ids'])
    if digest(canonical(sorted(ids))) != config['cohort_sha256']:
        raise PreservationError('observation_cohort_anchor_mismatch')
    with store.connect() as db:
        rows = {r[0]: r[1] for r in db.execute('SELECT version_id,state FROM delivery') if r[0] in ids}
        registration = db.execute('SELECT last_gap FROM registrations WHERE thread_id=?', (config['thread_id'],)).fetchone()
    counts = dict(Counter(rows.values()))
    health = pipeline_health(store)
    profiles = [item for item in health['processing_profiles']['versions'] if item['version_id'] in ids]
    local_complete = {item['version_id'] for item in profiles if item['state'] == 'profile_complete'}
    processing_counts = dict(Counter('local_reference_complete' if vid in local_complete else state
                                     for vid, state in rows.items()))
    pause_raw = store.setting('pipeline_observation_pause')
    pause = None
    end = now
    if pause_raw is not None:
        pause, paused_at = _pause_record(pause_raw)
        end = min(now, paused_at)
    elapsed = max(0, end-config['started_at'])
    report = {'schema': 1, 'observed_at': datetime.now(timezone.utc).isoformat(),
              'elapsed_seconds': elapsed, 'cohort_sha256': config['cohort_sha256'],
              'cohort_size': len(ids), 'cohort_states': counts, 'cohort_missing': sorted(ids-rows.keys()),
              'cohort_processing_states': processing_counts, 'cohort_processing_profiles': profiles,
              'cohort_interpretation': 'Original delivery states and cohort membership are preserved; local reference completion is not native completion.',
              'current_task_registered': registration is not None,
              'current_task_gap': registration[0] if registration else 'not_registered',
              'run_id': config.get('run_id'), 'pause': pause,
              'resume_blocked': json.loads(store.setting('pipeline_observation_resume_blocked', 'null')),
              'phase': 'paused' if pause else 'controlled_24h' if elapsed < 86400 else 'observation_72h' if elapsed < 345600 else 'awaiting_acceptance_review',
              'duration_completed': not pause and elapsed >= 345600, 'acceptance_passed': False,
              'health': health}
    output = Path(config['output_dir'])
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ') + '-' + uuid.uuid4().hex + '.json'
    temporary = output/('.'+name+'.tmp')
    try:
        _write_file(temporary,canonical(report))
        temporary.replace(output/name)
        _sync_dir(output)
    finally:
        temporary.unlink(missing_ok=True)
    store.set_setting('pipeline_observation_latest', str(output/name))
    store.set_setting('pipeline_observation_last', str(now))
    return {'state': 'recorded', 'path': str(output/name), 'phase': report['phase'],
            'cohort_size': len(ids), 'cohort_states': counts, 'acceptance_passed': False}
