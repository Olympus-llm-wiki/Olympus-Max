"""Read metadata for one signalled Codex task; never discover or resume history."""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import subprocess
import time

from .codex_capture import SUPPORTED_CODEX_VERSIONS
from .preservation import PreservationError

CODEX_BINARY = Path('/Applications/ChatGPT.app/Contents/Resources/codex')


def read_task_metadata(thread_id: str, project_root: Path, *, timeout: float = 2,
                       binary: Path = CODEX_BINARY) -> dict:
    """Short-lived read-only App Server connection, gated by native hook trust.

    The server does receive its normal local configuration. No thread/start,
    resume, turns/list, model or trust mutation is issued. Only metadata for the
    exact signalled ID is returned; preview, model and instructions are discarded.
    """
    if not binary.is_file():
        raise PreservationError('codex_metadata_runtime_unavailable')
    deadline = time.monotonic() + timeout
    process = subprocess.Popen([str(binary), 'app-server', '--stdio'], cwd=project_root,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    buffer = bytearray()

    def send(value):
        process.stdin.write((json.dumps(value)+'\n').encode())
        process.stdin.flush()

    def receive(identity):
        while time.monotonic() < deadline:
            while b'\n' in buffer:
                line, _, tail = buffer.partition(b'\n')
                buffer[:] = tail
                try:
                    response = json.loads(line)
                except ValueError:
                    raise PreservationError('codex_metadata_invalid_response') from None
                if response.get('id') == identity:
                    if 'error' in response:
                        error = response['error']
                        if isinstance(error, dict):
                            message = error.get('message')
                            if identity == 3 and error.get('code') == -32600 and message == 'thread not loaded: ' + thread_id:
                                raise PreservationError('codex_metadata_thread_not_loaded')
                            if error.get('code') == -32601:
                                raise PreservationError('codex_metadata_method_unavailable')
                        raise PreservationError('codex_metadata_read_failed')
                    return response.get('result', {})
            if not selector.select(max(0, deadline-time.monotonic())):
                break
            part = os.read(process.stdout.fileno(), 65536)
            if not part:
                raise PreservationError('codex_metadata_process_exited')
            buffer.extend(part)
            if len(buffer) > 2*1024*1024:
                raise PreservationError('codex_metadata_response_limit')
        raise PreservationError('codex_metadata_timeout')

    try:
        send({'id':1,'method':'initialize','params':{'clientInfo':{'name':'olympus_capture_metadata','version':'1'},
              'capabilities':{'experimentalApi':True}}})
        receive(1)
        send({'method':'initialized','params':{}})
        send({'id':2,'method':'hooks/list','params':{'cwds':[str(project_root)]}})
        hooks = receive(2)
        expected = str(project_root / '.codex/hooks.json')
        trusted = {h.get('eventName') for entry in hooks.get('data', []) for h in entry.get('hooks', [])
                   if h.get('sourcePath') == expected and h.get('source') == 'project'
                   and h.get('enabled') and h.get('trustStatus') == 'trusted'}
        if not {'sessionStart','stop'} <= trusted:
            raise PreservationError('codex_capture_hook_trust_required')
        send({'id':3,'method':'thread/read','params':{'threadId':thread_id,'includeTurns':False}})
        thread = receive(3).get('thread', {})
        if thread.get('parentThreadId'):
            raise PreservationError('codex_metadata_subagent')
        if thread.get('id') != thread_id or thread.get('sessionId') != thread_id:
            raise PreservationError('codex_metadata_identity_mismatch')
        if thread.get('cwd') != str(project_root):
            raise PreservationError('codex_metadata_project_mismatch')
        if thread.get('cliVersion') not in SUPPORTED_CODEX_VERSIONS:
            raise PreservationError('unsupported_codex_version')
        path = thread.get('path')
        if path is not None and (not isinstance(path, str) or len(path) > 4096):
            raise PreservationError('codex_metadata_invalid_path')
        return {'thread_id':thread_id,'transcript_path':path,'codex_version':thread['cliVersion']}
    except (OSError, ValueError, TypeError, AttributeError):
        raise PreservationError('codex_metadata_unavailable') from None
    finally:
        selector.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=.5)
        process.stdin.close()
        process.stdout.close()
