import json
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from dsec.contracts.errors import RequestOutcomeUnknown
from dsec.contracts.rpc import ServiceError
from dsec_openhands.transport import APISandbox


class TransportTests(unittest.TestCase):
    def test_interrupt_selects_its_own_native_session_while_action_is_in_flight(self):
        from dsec.sdk.native import DSecSession
        backend = APISandbox(dict(command_timeout_ms=45000, transport_timeout_ms=120000))
        backend.url = 'http://127.0.0.1:58731'
        backend.epoch = uuid.uuid4().hex
        entered, interrupted = threading.Event(), threading.Event()
        fake = type('Sandbox', (), {})()
        fake._features = {'native-http-v1'}
        async def native(action, **args):
            if args['url'].endswith('/interrupt'):
                interrupted.set()
            else:
                entered.set()
                if not await asyncio.to_thread(interrupted.wait, 3):
                    raise AssertionError('Interrupt was serialized behind its own command')
            import base64
            return dict(status_code=200, timed_out=False, truncated=False,
                body=base64.b64encode(json.dumps(dict(ok=True, cwd='/testbed', result={})).encode()).decode())
        fake._native = native
        backend.session = DSecSession(fake, uuid.uuid4().hex)
        backend.interrupt_session = DSecSession(fake, uuid.uuid4().hex)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                running = pool.submit(backend.action, 'terminal', action=dict(command='long-command'))
                self.assertTrue(entered.wait(2))
                stopping = pool.submit(backend.action, 'interrupt')
                try:
                    stopping.result(timeout=2)
                    self.assertTrue(interrupted.is_set())
                finally:
                    interrupted.set()
                running.result(timeout=3)
        finally:
            interrupted.set()
            backend.close()

    def backend(self, **settings):
        backend = APISandbox(dict(command_timeout_ms=45000, transport_timeout_ms=120000, **settings))
        self.addCleanup(backend.close)
        backend.url = 'http://127.0.0.1:58731'
        backend.epoch = uuid.uuid4().hex
        return backend

    def test_http_identity_is_preserved_and_response_is_not_reinterpreted_as_shell(self):
        backend = self.backend()
        body = dict(ok=True, cwd='/workspace', result=dict(content='a\x00b', exit_code=7))
        backend.session = type('Session', (), {})()
        backend.session.request = AsyncMock(return_value=dict(status_code=200, timed_out=False,
                                                              truncated=False, body=json.dumps(body).encode()))
        identity = uuid.uuid4().hex
        result = backend.action('bash', request_id=identity, command='printf hi')
        self.assertEqual(result, body['result'])
        call = backend.session.request.call_args
        self.assertEqual(call.kwargs['request_id'], identity)
        self.assertEqual(call.kwargs['timeout_ms'], 120000)
        self.assertEqual(json.loads(call.kwargs['body'])['id'], identity)

    def test_incomplete_http_result_is_unknown_and_never_resubmitted(self):
        for field in ('timed_out', 'truncated'):
            backend = self.backend()
            backend.session = type('Session', (), {})()
            value = dict(status_code=200, timed_out=False, truncated=False, body=b'partial')
            value[field] = True
            backend.session.request = AsyncMock(return_value=value)
            with self.assertRaises(RequestOutcomeUnknown):
                backend.action('bash', command='one-side-effect')
            self.assertEqual(backend.session.request.await_count, 1)

    def test_invalid_budgets_are_rejected_before_tool_admission(self):
        backend = self.backend()
        for value in (0, -1, True, 46):
            with self.assertRaises(ValueError):
                backend.action('bash', command='must-not-run', timeout_seconds=value)
        self.assertEqual(backend.operations, [])
        with self.assertRaises(ValueError):
            APISandbox(dict(command_timeout_ms=45000, transport_timeout_ms=45000))

    def test_close_fences_new_work_and_joins_admitted_http_before_stop(self):
        backend = self.backend()
        entered, release, draining = threading.Event(), threading.Event(), threading.Event()
        async def request(*args, **kwargs):
            entered.set()
            if not await asyncio.to_thread(release.wait, 5):
                raise AssertionError('test did not release admitted work')
            return dict(status_code=200, timed_out=False, truncated=False,
                body=json.dumps(dict(ok=True, cwd='/workspace', result=dict(content='done', exit_code=0))).encode())
        backend.session = type('Session', (), {})()
        backend.session.request = AsyncMock(side_effect=request)
        backend.box = type('Box', (), dict(id='sandbox'))()
        backend.box.stop = AsyncMock(return_value=dict(state='STOPPED', resource_cleanup_complete=True))
        original_drain = backend.portal.drain
        def drain(timeout):
            draining.set()
            original_drain(timeout)
        with patch.object(backend.portal, 'drain', side_effect=drain), ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(backend.action, 'bash', command='admitted-once')
            self.assertTrue(entered.wait(2))
            closing = pool.submit(backend.close)
            self.assertTrue(draining.wait(2))
            try:
                self.assertEqual(backend.box.stop.await_count, 0)
                with self.assertRaisesRegex(RuntimeError, 'no new work admitted'):
                    backend.action('bash', command='must-not-run')
            finally:
                release.set()
            self.assertEqual(running.result(timeout=5)['content'], 'done')
            closing.result(timeout=5)
        self.assertEqual(backend.session.request.await_count, 1)
        self.assertEqual(backend.box.stop.await_count, 1)
        self.assertEqual(backend.cleanup, 'stopped')

    def test_only_explicit_stop_nonadmission_can_retry_and_unknown_stays_sticky(self):
        for failure, expected in ((ServiceError('ServiceBusy', 'not admitted'), 2),
                                  (RequestOutcomeUnknown('stop reply lost'), 1)):
            backend = self.backend()
            backend.box = type('Box', (), dict(id='sandbox'))()
            backend.box.stop = AsyncMock(side_effect=[failure, dict(state='STOPPED', resource_cleanup_complete=True)])
            if expected == 2:
                backend.close()
                self.assertEqual(backend.cleanup, 'stopped')
                calls = backend.box.stop.call_args_list
                self.assertEqual(calls[0].kwargs['request_id'], calls[1].kwargs['request_id'])
            else:
                with self.assertRaises(RequestOutcomeUnknown):
                    backend.close()
                self.assertEqual(backend.cleanup, 'unknown')
                backend.close()
            self.assertEqual(backend.box.stop.await_count, expected)

    def test_startup_failures_do_not_reenter_health_or_recreate_sandbox(self):
        with tempfile.TemporaryDirectory() as root:
            token = Path(root) / 'token'
            token.write_text('a' * 64)
            token.chmod(0o600)
            config = Path(root) / 'api.json'
            config.write_text(json.dumps(dict(client=dict(endpoints=['https://localhost:8443'], project='test'),
                                              token_file=str(token))))
            backend = self.backend(api_config=str(config), spec=dict(environment_id='coding'), resource_demand={})
            session = type('Session', (), {})()
            session.request = AsyncMock(side_effect=[
                dict(status_code=200, timed_out=False, truncated=False,
                     body=json.dumps(dict(epoch=backend.epoch, cwd='/workspace', capabilities=[])).encode()),
                dict(status_code=409, timed_out=False, truncated=False, body=b'{"ok":false,"error":"kernel missing"}')])
            box = type('Box', (), dict(id='sandbox'))()
            box.open_session = AsyncMock(return_value=session)
            box.run_shell = AsyncMock(return_value=dict(exit_code=0, timed_out=False, truncated=False, output=''))
            box.write_file = AsyncMock()
            box.stop = AsyncMock(return_value=dict(state='STOPPED', resource_cleanup_complete=True))
            client = type('Client', (), {})()
            client.open = AsyncMock()
            client.close = AsyncMock()
            client.run_container = AsyncMock(return_value=box)
            with patch('dsec_openhands.transport.DSecClient', return_value=client):
                with self.assertRaisesRegex(RuntimeError, 'kernel missing'):
                    backend.connect()
            self.assertEqual(client.run_container.await_count, 1)
            self.assertEqual(session.request.await_count, 2)
            backend.close()
            self.assertEqual(backend.cleanup, 'stopped')
            # A protocol failure is not a service that is merely warming up.
            # It must not become 60 seconds of retries and a generic timeout.
            protocol = self.backend(api_config=str(config), spec=dict(environment_id='coding'), resource_demand={})
            session.request.reset_mock()
            session.request.side_effect = ServiceError('AttributeError', 'remote capability interface missing')
            client.run_container.reset_mock()
            with patch('dsec_openhands.transport.DSecClient', return_value=client):
                with self.assertRaisesRegex(ServiceError, 'capability interface missing'):
                    protocol.connect()
            self.assertEqual(client.run_container.await_count, 1)
            self.assertEqual(session.request.await_count, 1)
            self.assertIn('capability interface missing', protocol.startup_evidence['last_health_error']['message'])
            protocol.close()
            self.assertEqual(protocol.cleanup, 'stopped')
