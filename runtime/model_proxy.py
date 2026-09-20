"""Route native model operations to their executing worker, keeping recall alive.

No credentials are injected: the native worker HTTP app authenticates the original
request. In particular its GET config reports the worker's effective profile;
provider=none changes the read process's resolved extraction defaults to chunks.
"""
from __future__ import annotations

import asyncio
import json
import re

MAX_BODY_BYTES = 64 * 1024**2
MAX_RESPONSE_BYTES = 64 * 1024**2
DEADLINE_SECONDS = 105
_PATH = re.compile(r'^/v1/default/banks/[A-Za-z0-9_.:-]+/(config|reflect|llm/health|memories(?:/files)?)$')
_HOP_HEADERS = {b'host', b'connection', b'keep-alive', b'proxy-authenticate',
                b'proxy-authorization', b'te', b'trailer', b'transfer-encoding', b'upgrade'}


def _forward_headers(headers, *, request=False):
    excluded = set(_HOP_HEADERS)
    for key, value in headers:
        if key.lower() == b'connection':
            excluded.update(token.strip().lower() for token in value.split(b',') if token.strip())
    if request:
        excluded.add(b'content-length')
    return [(key, value) for key, value in headers if key.lower() not in excluded]


class ExecutorConfig:
    """Attach a safe static executor identity to its authenticated native config."""
    def __init__(self, app, *, profile):
        self.app, self.profile = app, profile

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        prefix = scope.get('root_path', '')
        if prefix and path.startswith(prefix):
            path = path[len(prefix):]
        match = _PATH.fullmatch(path)
        if scope.get('type') != 'http' or scope.get('method') != 'GET' or not match or match[1] != 'config':
            return await self.app(scope, receive, send)
        messages, used = [], 0
        async def collect(message):
            nonlocal used
            used += len(message.get('body', b''))
            if used > 1024**2:
                raise ValueError('executor_config_too_large')
            messages.append(message)
        # Native create_app installs an inner GZipMiddleware. Ask that local
        # app for identity bytes before augmenting JSON; authentication headers
        # remain exactly as supplied by the caller.
        inner_scope = {**scope, 'headers':[(k,v) for k,v in scope.get('headers', []) if k.lower()!=b'accept-encoding']
                       + [(b'accept-encoding',b'identity')]}
        try:
            await self.app(inner_scope, receive, collect)
            starts = [m for m in messages if m['type'] == 'http.response.start']
            if len(starts)!=1 or not messages or messages[0] is not starts[0]:
                raise ValueError('invalid_config_response')
            start = starts[0]
            if messages[-1]['type'] != 'http.response.body' or messages[-1].get('more_body',False):
                raise ValueError('incomplete_config_response')
            if start['status'] == 200:
                payload = json.loads(b''.join(m.get('body', b'') for m in messages))
                if not isinstance(payload,dict) or not isinstance(payload.get('config'),dict):
                    raise ValueError('invalid_config_response')
                payload['olympus_executor'] = dict(self.profile)
                body = json.dumps(payload, ensure_ascii=False).encode()
            else:
                body = None
        except Exception:
            await send({'type':'http.response.start','status':502,'headers':[(b'content-type',b'application/json')]})
            return await send({'type':'http.response.body','body':b'{"detail":"executor_config_invalid"}'})
        if body is not None:
            await send({**start, 'headers': [(k,v) for k,v in start.get('headers', []) if k.lower() not in {b'content-length',b'content-encoding'}]})
            return await send({'type':'http.response.body','body':body})
        for message in messages:
            await send(message)


class ModelProxy:
    def __init__(self, app, *, client_factory=None):
        self.app, self.client_factory = app, client_factory

    async def __call__(self, scope, receive, send):
        match = _PATH.fullmatch(scope.get('path', '')) if scope.get('type') == 'http' else None
        method = scope.get('method')
        selected = match and ((method == 'GET' and match[1] == 'config') or
                              (method == 'POST' and match[1] != 'config'))
        if not selected:
            return await self.app(scope, receive, send)
        started = False

        async def error(status, code):
            nonlocal started
            if not started:
                body = json.dumps({'detail': code}).encode()
                await send({'type': 'http.response.start', 'status': status,
                            'headers': [(b'content-type', b'application/json')]})
                started = True
                await send({'type': 'http.response.body', 'body': body})
            else:
                # A clean final body would falsely mark an interrupted SSE or
                # chunked HTTP 200 as successful. Let the ASGI server abort the
                # connection, suppressing the upstream exception's private data.
                raise RuntimeError('model_response_incomplete') from None

        body = bytearray()
        try:
            async with asyncio.timeout(DEADLINE_SECONDS):
                while True:
                    message = await receive()
                    if message['type'] == 'http.disconnect':
                        return
                    if message['type'] != 'http.request':
                        continue
                    part = message.get('body', b'')
                    if len(body)+len(part) > MAX_BODY_BYTES:
                        return await error(413, 'model_request_too_large')
                    body.extend(part)
                    if not message.get('more_body', False):
                        break
                headers = [(k.decode('latin1'), v.decode('latin1'))
                           for k,v in _forward_headers(scope.get('headers', []), request=True)]
                # A fixed internal destination; path selection cannot become SSRF.
                url = 'http://worker:8889/model' + scope['path']
                if scope.get('query_string'):
                    url += '?' + scope['query_string'].decode('ascii')
                factory = self.client_factory
                if factory is None:
                    import httpx
                    factory = lambda: httpx.AsyncClient(timeout=100, follow_redirects=False, trust_env=False)

                async def forward():
                    nonlocal started
                    async with factory() as client:
                        async with client.stream(method, url, headers=headers, content=bytes(body)) as response:
                            response_headers = _forward_headers(response.headers.raw)
                            lengths = [int(v) for k,v in response_headers if k.lower()==b'content-length']
                            if any(length>MAX_RESPONSE_BYTES or length<0 for length in lengths):
                                return await error(502,'model_response_too_large')
                            await send({'type': 'http.response.start', 'status': response.status_code,
                                'headers': response_headers})
                            started = True
                            used = 0
                            async for chunk in response.aiter_raw():
                                used += len(chunk)
                                if used > MAX_RESPONSE_BYTES:
                                    raise ValueError('model_response_too_large')
                                await send({'type': 'http.response.body', 'body': chunk, 'more_body': True})
                            await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

                async def disconnected():
                    while (await receive())['type'] != 'http.disconnect':
                        pass

                request = asyncio.create_task(forward())
                watcher = asyncio.create_task(disconnected())
                try:
                    done, _ = await asyncio.wait({request, watcher}, return_when=asyncio.FIRST_COMPLETED)
                    if request in done:
                        await request
                finally:
                    for task in (request, watcher):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(request, watcher, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The provider exception can contain body/header data. Never log it.
            await error(503, 'model_service_unavailable')
