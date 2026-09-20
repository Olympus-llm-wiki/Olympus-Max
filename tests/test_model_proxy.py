import asyncio
import gzip
import json
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from olympus.model_proxy import ModelProxy, ExecutorConfig


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, proxy, *, path='/v1/default/banks/olympus-v1/reflect', method='POST', body=b'{}', headers=()):
        messages, first = [], True
        async def receive():
            nonlocal first
            if first:
                first = False
                return {'type': 'http.request', 'body': body}
            await asyncio.Future()
        async def send(message): messages.append(message)
        await proxy({'type': 'http', 'path': path, 'method': method, 'headers': headers}, receive, send)
        return messages

    async def test_worker_authenticates_original_header_and_config_uses_executor(self):
        calls = []
        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def stream(self, method, url, **kwargs):
                calls.append((method, url, kwargs)); return Response()
        class Response:
            status_code = 401
            headers = SimpleNamespace(raw=[(b'content-type', b'application/json')])
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def aiter_raw(self): yield b'{"detail":"unauthorized"}'
        async def app(*args): self.fail('model route reached read engine')
        proxy = ModelProxy(app, client_factory=Client)
        messages = await self.request(proxy, headers=[(b'authorization', b'Bearer user-supplied'), (b'host', b'evil.example')])
        self.assertEqual(messages[0]['status'], 401)
        self.assertEqual(calls[0][1], 'http://worker:8889/model/v1/default/banks/olympus-v1/reflect')
        self.assertEqual(calls[0][2]['headers'], [('authorization', 'Bearer user-supplied')])
        await self.request(proxy, path='/v1/default/banks/olympus-v1/config', method='GET')
        self.assertEqual(calls[-1][0], 'GET')
        self.assertEqual(calls[-1][2]['headers'], [])

    async def test_recall_stays_available_without_model_service(self):
        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'read-api'})
        proxy = ModelProxy(app, client_factory=lambda: self.fail('unexpected upstream'))
        self.assertEqual((await self.request(proxy, path='/v1/default/banks/olympus-v1/memories/recall'))[0]['status'], 200)

    async def test_missing_worker_and_oversize_are_explicit_without_exception_text(self):
        class Client:
            async def __aenter__(self): raise ConnectionError('private-upstream-detail')
            async def __aexit__(self, *args): pass
        proxy = ModelProxy(None, client_factory=Client)
        result = await self.request(proxy)
        self.assertEqual(result[0]['status'], 503)
        self.assertNotIn('private-upstream-detail', str(result))
        with patch('olympus.model_proxy.MAX_BODY_BYTES', 1):
            self.assertEqual((await self.request(proxy))[0]['status'], 413)

    async def test_static_executor_fields_are_added_only_after_native_auth(self):
        status = 200
        async def app(scope, receive, send):
            await send({'type':'http.response.start', 'status':status, 'headers':[(b'content-length',b'13')]})
            await send({'type':'http.response.body','body':b'{"config":{}}'})
        profile = {'schema':1,'role':'worker','retain_llm_model':'fixture-model'}
        middleware = ExecutorConfig(app, profile=profile)
        result = await self.request(middleware,path='/v1/default/banks/test/config',method='GET')
        self.assertIn(b'fixture-model', result[-1]['body'])
        self.assertEqual(result[0]['headers'], [])
        status = 401
        denied = await self.request(middleware,path='/v1/default/banks/test/config',method='GET')
        self.assertNotIn('fixture-model', str(denied))

    async def test_disconnect_closes_upstream_and_does_not_finish_partial_response(self):
        sent, streaming, cancelled, closed = [], asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Response:
            status_code=200
            headers=SimpleNamespace(raw=[])
            async def __aenter__(self):return self
            async def __aexit__(self,*args):closed.set()
            async def aiter_raw(self):
                try:
                    yield b'first-part'
                    streaming.set()
                    await asyncio.Future()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
        class Client:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            def stream(self,*args,**kwargs):return Response()
        first=True
        async def receive():
            nonlocal first
            if first:
                first=False
                return {'type':'http.request','body':b'{}'}
            await streaming.wait()
            return {'type':'http.disconnect'}
        async def send(message):sent.append(message)
        await asyncio.wait_for(ModelProxy(None,client_factory=Client)({
            'type':'http','path':'/v1/default/banks/test/reflect','method':'POST','headers':[]},receive,send),1)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(closed.is_set())
        self.assertTrue(any(message.get('body')==b'first-part' for message in sent))
        self.assertFalse(any(message['type']=='http.response.body' and not message.get('more_body',False) for message in sent))

    async def test_broken_upstream_stream_is_not_terminated_as_success(self):
        sent=[]
        class Response:
            status_code=200
            headers=SimpleNamespace(raw=[])
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def aiter_raw(self):
                yield b'partial'
                raise ConnectionError('private-upstream-header-value')
        class Client:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            def stream(self,*args,**kwargs):return Response()
        first=True
        async def receive():
            nonlocal first
            if first:first=False;return {'type':'http.request','body':b'{}'}
            await asyncio.Future()
        async def send(message):sent.append(message)
        with self.assertRaisesRegex(RuntimeError,'model_response_incomplete') as raised:
            await ModelProxy(None,client_factory=Client)({'type':'http','path':'/v1/default/banks/test/reflect','method':'POST','headers':[]},receive,send)
        self.assertFalse(any(message['type']=='http.response.body' and not message.get('more_body',False) for message in sent))
        self.assertNotIn('private-upstream-header-value',''.join(traceback.format_exception(raised.exception)))

    async def test_config_augmentation_handles_compression_negotiation_and_bad_payload(self):
        payload=b'{"config":{"mission":"'+b'x'*2000+b'"}}'
        async def native(scope,receive,send):
            body=gzip.compress(payload) if b'gzip' in dict(scope['headers']).get(b'accept-encoding',b'') else payload
            headers=[(b'content-encoding',b'gzip')] if body!=payload else []
            await send({'type':'http.response.start','status':200,'headers':headers})
            await send({'type':'http.response.body','body':body})
        app=ExecutorConfig(native,profile={'schema':1,'role':'worker'})
        messages=await self.request(app,path='/v1/default/banks/test/config',method='GET',headers=[(b'accept-encoding',b'gzip')])
        self.assertEqual(messages[0]['status'],200)
        self.assertEqual(json.loads(messages[-1]['body'])['olympus_executor']['role'],'worker')
        self.assertNotIn(b'content-encoding',dict(messages[0]['headers']))
        for payload in [b'not-json',b'[]',b'{"detail":"not-config"}']:
            messages=await self.request(app,path='/v1/default/banks/test/config',method='GET')
            self.assertEqual(messages[0]['status'],502)
            self.assertNotIn(b'worker',messages[-1]['body'])

    async def test_connection_nominated_headers_do_not_cross_proxy(self):
        calls=[]
        class Response:
            status_code=200
            headers=SimpleNamespace(raw=[(b'connection',b'x-upstream-only'),(b'x-upstream-only',b'private'),(b'content-type',b'application/json')])
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def aiter_raw(self):yield b'{}'
        class Client:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            def stream(self,*args,**kwargs):calls.append(kwargs);return Response()
        messages=await self.request(ModelProxy(None,client_factory=Client),headers=[
            (b'connection',b'authorization, x-request-only'),(b'authorization',b'Bearer supplied'),(b'x-request-only',b'private')])
        self.assertEqual(calls[0]['headers'],[])
        self.assertEqual(messages[0]['headers'],[(b'content-type',b'application/json')])

    async def test_declared_oversize_response_is_rejected_before_success_headers(self):
        consumed=False
        class Response:
            status_code=200
            headers=SimpleNamespace(raw=[(b'content-length',b'2')])
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def aiter_raw(self):
                nonlocal consumed
                consumed=True
                yield b'{}'
        class Client:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            def stream(self,*args,**kwargs):return Response()
        with patch('olympus.model_proxy.MAX_RESPONSE_BYTES',1):
            result=await self.request(ModelProxy(None,client_factory=Client))
        self.assertEqual(result[0]['status'],502)
        self.assertFalse(consumed)

    async def test_disconnect_before_body_does_not_start_upstream(self):
        async def receive():return {'type':'http.disconnect'}
        async def send(message):self.fail('response sent to disconnected client')
        proxy=ModelProxy(None,client_factory=lambda:self.fail('upstream started after disconnect'))
        await proxy({'type':'http','path':'/v1/default/banks/test/reflect','method':'POST'},receive,send)
