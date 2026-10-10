"""End-to-end SDK -> Edge journal -> compiled native guest over local sockets.

This exercises transport/fault semantics; VMM snapshots need Linux acceptance.
"""
import asyncio
import base64
from contextlib import contextmanager
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from dsec.contracts.errors import RequestOutcomeUnknown
from dsec.contracts.native import NATIVE_FEATURE
from dsec.contracts.sandbox import UnsupportedCapability
from dsec.control.server import BoundedServer, Handler
from dsec.runtime.requests import RequestJournal
from dsec.runtime.sessions.native import NativeChannel
from dsec.sdk.client import DSecClient, DSecSandbox
from dsec.sdk.sandbox_transport import SandboxClient, ServiceError
from tests.unit.test_native_guest import NativeGuestFixture


class NativeSDKTests(NativeGuestFixture):
    def setUp(self):
        super().setUp()
        self.sb = SimpleNamespace(id="abcdef012345", lock=threading.RLock(), state="RUNNING",
            native_inflight={}, reserved=False, baseline_sealed=False, environment_id="default",
            vm=SimpleNamespace(vsock=self.socket), inflight=None,
            _check=Mock(), _persist=Mock(), _touch=Mock(), _fail=Mock(),
            status=lambda: {"id": "abcdef012345", "state": "RUNNING"})
        self.manager = SimpleNamespace(sandboxes={self.sb.id: self.sb},
            command_timeout_ms=lambda _: 30000, egress_proxy_url=None,
            egress_proxy_bypass_hosts=(), foreground_enter=Mock(), foreground_exit=Mock(),
            recovery_events=[], errors=[], warm_pool_status=lambda: {})
        self.channel_patch = patch("dsec.runtime.sessions.service.NativeChannel",
            side_effect=lambda endpoint, **_: NativeChannel(endpoint))
        self.channel_patch.start()
        self.start_edge()

    def start_edge(self):
        self.service_socket = self.root / "edge.sock"
        self.service_socket.unlink(missing_ok=True)
        self.server = BoundedServer(str(self.service_socket), Handler, 8)
        self.server.manager = self.manager
        self.server.journal = RequestJournal(self.root / "journal")
        self.server.admission_worker_socket = None
        self.server.node_admission = None
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        self.thread.start()

    def stop_edge(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def tearDown(self):
        self.stop_edge()
        self.channel_patch.stop()
        super().tearDown()

    async def client(self):
        client = await DSecClient(self.service_socket).open()
        return client, await client.attach(self.sb.id)

    def test_public_sdk_keeps_state_serializes_same_session_and_transfers_binary(self):
        async def check():
            client, sandbox = await self.client()
            session = await sandbox.open_session()
            self.sessions.append(session.id)
            await session.run_shell("export DSEC_SDK=works")
            result = await session.run_shell('printf "%s" "$DSEC_SDK"')
            self.assertEqual(result["stdout"], "works")
            path = str(self.root / "sdk-file")
            data = b"\x00\xff" * 70000
            op = uuid.uuid4().hex
            await sandbox.write_file(path, data, request_id=op)
            await sandbox.write_file(path, data, request_id=op)
            self.assertEqual(await sandbox.read_file(path), data)
            with self.assertRaises(ServiceError) as rejected:
                await sandbox.write_file(path, b"b" * len(data), request_id=op)
            self.assertEqual(rejected.exception.kind, "RequestConflict")
            self.assertEqual(Path(path).read_bytes(), data)
            results = await asyncio.gather(session.run_shell("sleep .1; printf one"),
                                           session.run_shell("printf two"))
            self.assertEqual([r["stdout"] for r in results], ["one", "two"])
            await session.close()
            await client.close()
        asyncio.run(check())

    def test_command_deduplication_and_commit_crash_never_reexecute(self):
        async def check():
            client, sandbox = await self.client()
            session = await sandbox.open_session()
            self.sessions.append(session.id)
            path = self.root / "effects"
            command = f"printf x >> '{path}'"
            op = uuid.uuid4().hex
            await session.run_shell(command, request_id=op)
            await session.run_shell(command, request_id=op)
            self.assertEqual(path.read_bytes(), b"x")
            crash_id = uuid.uuid4().hex
            finish = self.server.journal.finish
            def crash_before_commit(request_id, response):
                if request_id == crash_id:
                    raise OSError("injected result commit failure")
                finish(request_id, response)
            with patch.object(self.server.journal, "finish", side_effect=crash_before_commit):
                with self.assertRaises(RequestOutcomeUnknown):
                    await session.run_shell(command, request_id=crash_id)
            self.assertEqual(path.read_bytes(), b"xx")
            self.stop_edge()
            self.start_edge()
            proof = await client.lookup_request(crash_id)
            self.assertEqual(proof["state"], "UNKNOWN")
            with self.assertRaises(ServiceError) as uncertain:
                await session.run_shell(command, request_id=crash_id)
            self.assertEqual(uncertain.exception.kind, "RequestUncertain")
            self.assertEqual(path.read_bytes(), b"xx")
            attached = sandbox.attach_session(session.id)
            self.assertEqual((await attached.run_shell("printf alive"))["stdout"], "alive")
        asyncio.run(check())

    def test_pause_is_nonadmitted_while_native_command_runs(self):
        async def check():
            client, sandbox = await self.client()
            session = await sandbox.open_session()
            self.sessions.append(session.id)
            running = asyncio.create_task(session.run_shell("sleep 10", timeout_ms=20000))
            for _ in range(100):
                if self.sb.native_inflight:
                    break
                await asyncio.sleep(.01)
            pause_id = uuid.uuid4().hex
            with self.assertRaises(ServiceError) as busy:
                await sandbox.pause(request_id=pause_id)
            self.assertEqual(busy.exception.kind, "ServiceBusy")
            self.assertEqual((await client.lookup_request(pause_id))["state"], "NOT_FOUND")
            await session.close()
            result = await running
            self.assertTrue(result["cancelled"])
            self.assertFalse(self.sb.native_inflight)
        asyncio.run(check())

    def test_old_edge_is_explicitly_unsupported_without_emulated_shell(self):
        sandbox = DSecSandbox(SandboxClient(self.service_socket), self.sb.id)
        with self.assertRaises(UnsupportedCapability):
            asyncio.run(sandbox.open_session())
        self.assertEqual(list(self.server.journal.root.glob("*.json")), [])

    def test_stream_first_output_detach_reattach_and_duplicate_do_not_reexecute(self):
        async def check():
            _, sandbox = await self.client()
            session = await sandbox.open_session()
            self.sessions.append(session.id)
            op = uuid.uuid4().hex
            path = self.root / "stream-effects"
            command = f"printf x >> '{path}'; printf early; sleep .5; printf late >&2"
            stream = session.stream(command, request_id=op)
            start = time.monotonic()
            first = await anext(stream)
            self.assertEqual(first["type"], "stdout")
            self.assertEqual(first["data"], b"early")
            self.assertLess(time.monotonic() - start, .4)
            await stream.aclose()
            # Starting the same ID while it runs attaches to that intent.
            replay = [event async for event in session.stream(command, request_id=op)]
            self.assertEqual(path.read_bytes(), b"x")
            self.assertEqual(b"".join(e["data"] for e in replay if e["type"] == "stderr"), b"late")
            self.assertEqual(replay[-1]["type"], "result")
            proof = await session.query(op)
            self.assertEqual(proof["state"], "DONE")
            resumed = [event async for event in session.events(op, cursor=first["cursor"])]
            self.assertFalse(any(e["type"] == "stdout" for e in resumed))
            self.assertEqual(resumed[-1]["type"], "result")
        asyncio.run(check())

    def test_stream_cancellation_targets_the_operation_and_cannot_cancel_later_work(self):
        async def check():
            _, sandbox = await self.client()
            session = await sandbox.open_session()
            self.sessions.append(session.id)
            op = uuid.uuid4().hex
            stream = session.stream("printf started; sleep 10", timeout_ms=20000, request_id=op)
            self.assertEqual((await anext(stream))["data"], b"started")
            result = await session.cancel(op)
            self.assertTrue(result["cancel_requested"])
            events = [e async for e in stream]
            self.assertTrue(events[-1]["result"]["cancelled"])
            self.assertTrue(events[-1]["result"]["session_reset"])
            self.assertFalse((await session.cancel(op))["cancel_requested"])
            other = await sandbox.open_session()
            self.sessions.append(other.id)
            with self.assertRaises(ServiceError):
                await other.cancel(op)
            self.assertEqual((await other.run_shell("printf alive"))["stdout"], "alive")
        asyncio.run(check())

    def test_slow_subscriber_does_not_delay_timeout_or_expand_output(self):
        async def check():
            _, sandbox = await self.client()
            session = await sandbox.open_session()
            self.sessions.append(session.id)
            op = uuid.uuid4().hex
            stream = session.stream("while :; do printf 'abcdefgh'; done", timeout_ms=100,
                                    output_limit=64, request_id=op)
            first = await anext(stream)
            await asyncio.sleep(.3)
            self.assertEqual((await session.query(op))["state"], "DONE")
            rest = [e async for e in stream]
            data = sum(len(e["data"]) for e in [first, *rest] if e["type"] in ("stdout", "stderr"))
            self.assertLessEqual(data, 64)
            self.assertTrue(rest[-1]["result"]["timed_out"])
            self.assertTrue(rest[-1]["result"]["truncated"])
        asyncio.run(check())

    def test_uncommitted_stream_result_is_hidden_and_never_replayed(self):
        async def check():
            _, sandbox = await self.client()
            session = await sandbox.open_session()
            self.sessions.append(session.id)
            op = uuid.uuid4().hex
            path = self.root / "uncommitted-stream"
            command = f"printf x >> '{path}'; printf evidence"
            finish = self.server.journal.finish
            def fail(request_id, response):
                if request_id == op:
                    raise OSError("injected commit failure")
                finish(request_id, response)
            collected = []
            with patch.object(self.server.journal, "finish", side_effect=fail):
                with self.assertRaises(RequestOutcomeUnknown):
                    async for event in session.stream(command, request_id=op):
                        collected.append(event)
            self.assertFalse(any(e["type"] == "result" for e in collected))
            self.assertEqual((await session.query(op))["state"], "UNKNOWN")
            self.assertEqual(path.read_bytes(), b"x")
            with self.assertRaises(ServiceError) as error:
                async for _ in session.stream(command, request_id=op):
                    pass
            self.assertEqual(error.exception.kind, "RequestUncertain")
            self.assertEqual(path.read_bytes(), b"x")
            self.sb._fail.assert_called_with("native_operation_outcome_unknown")
        asyncio.run(check())

    def test_file_version_change_is_rejected_instead_of_returning_mixed_chunks(self):
        async def check():
            _, sandbox = await self.client()
            path = self.root / "changing-file"
            path.write_bytes(b"a"*100000)
            call = NativeChannel.call
            changed = False
            def mutate(channel, action, **args):
                nonlocal changed
                result = call(channel, action, **args)
                if action == "read" and not changed:
                    path.write_bytes(b"b"*100000)
                    changed = True
                return result
            with patch.object(NativeChannel, "call", new=mutate):
                with self.assertRaisesRegex(RuntimeError, "changed during"):
                    await sandbox.read_file(str(path))
        asyncio.run(check())

    def test_two_handles_to_the_same_session_serialize_streams_without_replay(self):
        async def check():
            _, sandbox = await self.client()
            session = await sandbox.open_session()
            self.sessions.append(session.id)
            other = sandbox.attach_session(session.id)
            first = session.stream("printf first; sleep .3")
            await anext(first)
            second_task = asyncio.create_task(self.collect(other.stream("printf second")))
            rest = [event async for event in first]
            second = await second_task
            self.assertEqual(rest[-1]["result"]["exit_code"], 0)
            self.assertEqual(b"".join(e["data"] for e in second if e["type"] == "stdout"), b"second")
            self.assertGreater(second[-1]["result"]["queue_wait_ms"], 0)
        asyncio.run(check())

    async def collect(self, stream):
        return [event async for event in stream]
