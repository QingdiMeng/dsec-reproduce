"""Exercise the compiled guest service with real persistent shell processes."""
import base64
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import signal
import socket
import select
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import tempfile
import time
import unittest
import uuid

from dsec.contracts.errors import ServiceBusy
from dsec.runtime.sessions.native import NativeChannel, NativeSessionReset

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("cc"), "C compiler required for real guest test")
class NativeGuestFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="dsec-native-build-", dir="/tmp")
        cls.binary = Path(cls.build.name) / "agent"
        subprocess.run(["cc", "-DDSEC_NATIVE_STANDALONE", "-O2", "-Wall", "-Wextra",
                        "-Werror", "-o", str(cls.binary), str(ROOT / "guest_native.c")], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dsec-native-test-", dir="/tmp")
        self.root = Path(self.temp.name)
        self.socket = self.root / "agent.sock"
        self.process = subprocess.Popen([str(self.binary), "--unix", str(self.socket)],
                                        start_new_session=True)
        self.addCleanup(self.ensure_stopped)
        deadline = time.monotonic() + 5
        while not self.socket.exists() and self.process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        if not self.socket.exists():
            raise RuntimeError(f"Native agent failed to bind: exit={self.process.poll()}")
        self.channel = NativeChannel(self.socket)
        self.sessions = []
        self.channel.capabilities()

    def tearDown(self):
        for sid in self.sessions:
            try:
                self.channel.call("close", session_id=sid)
            except (NativeSessionReset, OSError):
                pass
        try:
            if self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=5)
        finally:
            self.ensure_stopped()

    def ensure_stopped(self):
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
        self.temp.cleanup()

    def session(self):
        sid = uuid.uuid4().hex
        self.channel.call("open", session_id=sid)
        self.sessions.append(sid)
        return sid


class NativeGuestTests(NativeGuestFixture):
    def test_slow_header_cannot_block_other_sessions_or_shutdown(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as slow:
            slow.connect(str(self.socket))
            slow.sendall(b"R")
            start = time.monotonic()
            self.channel.capabilities()
            sid = self.session()
            self.assertEqual(self.channel.call("run", session_id=sid, command="printf alive")["stdout"], "alive")
            self.assertLess(time.monotonic() - start, 1)
            self.process.terminate()
            self.assertEqual(self.process.wait(timeout=1), 0)
            self.sessions.clear()

    def test_session_file_operations_reject_closed_session(self):
        sid = self.session()
        path = str(self.root / "scoped")
        Path(path).write_bytes(b"scoped-content")
        args = dict(session_id=sid, path=path, transfer_id=uuid.uuid4().hex)
        result = self.channel.call("read", **args)
        self.assertEqual(base64.b64decode(result["data"]), b"scoped-content")
        self.channel.call("close", session_id=sid)
        with self.assertRaises(NativeSessionReset):
            self.channel.call("read", **args)
        # A closed identity is a tombstone, not a name for a fresh shell.
        with self.assertRaises(NativeSessionReset):
            self.channel.call("open", session_id=sid)

    def test_closed_session_releases_its_incomplete_file_transfer(self):
        a, b = self.session(), self.session()
        path = str(self.root / 'unfinished')
        transfer = uuid.uuid4().hex
        self.channel.call('write_begin', session_id=a, path=path, transfer_id=transfer, total=64*1024**2)
        with self.assertRaises(PermissionError):
            self.channel.call('write_abort', session_id=b, path=path, transfer_id=transfer)
        self.channel.call('close', session_id=a)
        # Synchronize with chronus cleanup by awaiting a response from its
        # proxy reaper; retry only proven pre-effect ENOSPC, not an unknown.
        for _ in range(100):
            try:
                other=uuid.uuid4().hex
                self.channel.call('write_begin', session_id=b, path=path, transfer_id=other, total=1)
                self.channel.call('write_abort', session_id=b, path=path, transfer_id=other)
                break
            except OSError as exc:
                if exc.errno != 28: raise
                time.sleep(.01)
        else:
            self.fail('Closed chronus retained upload quota')
        self.assertFalse(Path(path+'.dsec-upload-'+transfer).exists())

    def test_native_http_binary_post_headers_redirect_and_limits(self):
        class Endpoint(BaseHTTPRequestHandler):
            def do_POST(self):
                data = self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(201)
                self.end_headers()
                self.wfile.write(self.headers["X-Test"].encode() + data)
            def do_GET(self):
                self.send_response(302 if self.path == "/redirect" else 200)
                self.send_header("Location", "/unexpected")
                self.end_headers()
                self.wfile.write(b"content")
            def log_message(self, *_): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        try:
            sid = self.session()
            url = f"http://127.0.0.1:{server.server_port}"
            result = self.channel.call("http", session_id=sid, method="POST", url=url,
                headers={"X-Test":"header:"}, body=base64.b64encode(b"\x00\xff").decode())
            self.assertEqual(result["status_code"], 201)
            self.assertEqual(base64.b64decode(result["body"]), b"header:\x00\xff")
            result = self.channel.call("http", session_id=sid, method="GET", url=url+"/redirect")
            self.assertEqual(result["status_code"], 302)
            result = self.channel.call("http", session_id=sid, method="GET", url=url, response_limit=3)
            self.assertTrue(result["truncated"])
            self.assertEqual(base64.b64decode(result["body"]), b"con")
            for fields in ({"url":"file:///etc/passwd"}, {"headers":{"x":"a\r\nb"}}, {"timeout_ms":0}):
                with self.assertRaises(ValueError):
                    self.channel.call("http", **{"session_id":sid, "method":"GET", "url":url, **fields})
        finally:
            server.shutdown();thread.join(timeout=2);server.server_close()

    def test_http_timeout_and_close_have_distinct_session_semantics(self):
        admitted, release = threading.Event(), threading.Event()
        class Endpoint(BaseHTTPRequestHandler):
            def do_GET(self):
                admitted.set();release.wait(5)
                try:
                    self.send_response(200);self.end_headers();self.wfile.write(b'late')
                except BrokenPipeError: pass
            def log_message(self, *_): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Endpoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        try:
            sid = self.session();url = f'http://127.0.0.1:{server.server_port}'
            value = self.channel.call('http', session_id=sid, method='GET', url=url, timeout_ms=100)
            self.assertTrue(value['timed_out']);self.assertFalse(value['session_reset'])
            self.assertEqual(self.channel.call('run', session_id=sid, command='printf still-alive')['stdout'], 'still-alive')
            admitted.clear()
            with ThreadPoolExecutor(1) as pool:
                active = pool.submit(self.channel.call, 'http', session_id=sid, method='GET', url=url, timeout_ms=10000)
                self.assertTrue(admitted.wait(2), 'HTTP operation did not reach server')
                self.channel.call('close', session_id=sid)
                value = active.result(timeout=2)
            self.assertTrue(value['cancelled']);self.assertTrue(value['session_reset'])
            self.assertIsNone(value['status_code'])
        finally:
            release.set();server.shutdown();thread.join(timeout=2);server.server_close()

    @unittest.skipUnless(Path('/proc/self/task').exists(), 'Linux process ownership gate')
    def test_chronus_death_does_not_recreate_session_or_kill_other_session(self):
        a, b = self.session(), self.session()
        children = Path(f'/proc/{self.process.pid}/task/{self.process.pid}/children')
        pids = [int(pid) for pid in children.read_text().split()
                if Path(f'/proc/{pid}/comm').read_text().strip() == 'dsec-chronus']
        self.assertEqual(len(pids), 2)
        death = os.pidfd_open(pids[0])
        try:
            os.kill(pids[0], signal.SIGKILL)
            self.assertTrue(select.select([death], [], [], 2)[0], 'chronus did not exit')
        finally:
            os.close(death)
        gone, alive = [], []
        for sid in (a,b):
            try:
                self.channel.call("run", session_id=sid, command="printf survived")
                alive.append(sid)
            except NativeSessionReset:
                gone.append(sid)
        self.assertEqual(len(gone), 1);self.assertEqual(len(alive), 1)
        with self.assertRaises(NativeSessionReset):
            self.channel.call("run", session_id=gone[0], command="printf cannot-replay")
        self.assertEqual(self.channel.call("run", session_id=alive[0], command="printf alive")["stdout"], "alive")

    def test_idle_sigterm_does_not_race_with_capability_child_exit(self):
        for index in range(12):
            endpoint = self.root / f'shutdown-{index}.sock'
            process = subprocess.Popen([str(self.binary), '--unix', str(endpoint)])
            try:
                for _ in range(100):
                    if endpoint.exists():
                        break
                    time.sleep(.01)
                NativeChannel(endpoint).capabilities()
                process.terminate()
                self.assertEqual(process.wait(timeout=2), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

    def test_persistent_cwd_environment_and_separate_output(self):
        sid = self.session()
        result = self.channel.call("run", session_id=sid,
            command="cd /tmp; export DSEC_TEST='中文'; printf 'stdout'; printf 'stderr' >&2")
        self.assertEqual(result["stdout"], "stdout")
        self.assertEqual(result["stderr"], "stderr")
        result = self.channel.call("run", session_id=sid,
            command='pwd; printf "%s" "$DSEC_TEST"')
        self.assertEqual(result["stdout"], "/tmp\n中文")
        self.assertEqual(result["exit_code"], 0)

    def test_distinct_sessions_overlap_and_environment_is_private(self):
        a, b = self.session(), self.session()
        self.channel.call("run", session_id=a, command="export DSEC_TEST=secret")
        self.assertEqual(self.channel.call("run", session_id=b,
            command='printf "%s" "$DSEC_TEST"')["stdout"], "")
        with ThreadPoolExecutor(2) as pool:
            start = time.monotonic()
            futures = [pool.submit(self.channel.call, "run", session_id=s,
                                   command="sleep .3; printf done") for s in (a, b)]
            self.assertEqual([f.result()["stdout"] for f in futures], ["done", "done"])
            self.assertLess(time.monotonic() - start, .55)

    def test_timeout_and_exit_reset_the_shell(self):
        sid = self.session()
        result = self.channel.call("run", session_id=sid, command="sleep 10", timeout_ms=100)
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["session_reset"])
        with self.assertRaises(NativeSessionReset):
            self.channel.call("run", session_id=sid, command="echo cannot-replay")
        sid = self.session()
        result = self.channel.call("run", session_id=sid, command="exit 7")
        self.assertTrue(result["session_reset"])

    def test_infinite_output_is_bounded_and_deadline_still_fires(self):
        result = self.channel.call("run", session_id=self.session(),
            command="while :; do printf 'abcdefgh'; done", timeout_ms=100, output_limit=64)
        self.assertLessEqual(len(result["stdout"]) + len(result["stderr"]), 64)
        self.assertTrue(result["truncated"])
        self.assertTrue(result["timed_out"])

    def test_close_cancels_an_active_command(self):
        sid = self.session()
        with ThreadPoolExecutor(1) as pool:
            running = pool.submit(self.channel.call, "run", session_id=sid,
                                  command="sleep 10", timeout_ms=20000)
            time.sleep(.1)
            start = time.monotonic()
            self.channel.call("close", session_id=sid)
            result = running.result()
            self.assertLess(time.monotonic() - start, 1)
            self.assertTrue(result["session_reset"])
            self.assertTrue(result["cancelled"])

    def test_binary_files_are_atomic_chunked_and_report_missing_paths(self):
        path = str(self.root / "binary")
        Path(path).write_bytes(b"old")
        transfer = uuid.uuid4().hex
        data = b"\x00\xff" * 40000 + "中文".encode()
        args = dict(path=path, transfer_id=transfer, total=len(data), mode=0o640)
        self.channel.call("write_begin", **args)
        for offset in range(0, len(data), 65536):
            self.channel.call("write_chunk", **args, offset=offset,
                data=base64.b64encode(data[offset:offset+65536]).decode())
            self.assertEqual(Path(path).read_bytes(), b"old")
        self.channel.call("write_commit", **args)
        self.assertEqual(Path(path).read_bytes(), data)
        self.assertEqual(Path(path).stat().st_mode & 0o777, 0o640)
        result = self.channel.call("read", path=path, transfer_id=uuid.uuid4().hex,
                                   offset=65536, length=65536)
        self.assertEqual(base64.b64decode(result["data"]), data[65536:])
        with self.assertRaises(FileNotFoundError):
            self.channel.call("read", path=str(self.root / "missing"), transfer_id=uuid.uuid4().hex)
        for content in (b"", b"\x00"):
            transfer = uuid.uuid4().hex
            args = dict(path=path, transfer_id=transfer, total=len(content), mode=0o600)
            self.channel.call("write_begin", **args)
            if content:
                self.channel.call("write_chunk", **args, offset=0,
                                  data=base64.b64encode(content).decode())
            self.channel.call("write_commit", **args)
            self.assertEqual(Path(path).read_bytes(), content)

    def test_incomplete_commit_and_quota_do_not_replace_the_target(self):
        path = str(self.root / "target")
        Path(path).write_bytes(b"original")
        transfer = uuid.uuid4().hex
        args = dict(path=path, transfer_id=transfer, total=64*1024**2, mode=0o600)
        self.channel.call("write_begin", **args)
        with self.assertRaises(OSError):
            self.channel.call("write_commit", **args)
        with self.assertRaises(OSError):
            self.channel.call("write_begin", path=str(self.root/"other"),
                transfer_id=uuid.uuid4().hex, total=1, mode=0o600)
        self.assertEqual(Path(path).read_bytes(), b"original")
        self.channel.call("write_abort", path=path, transfer_id=transfer)
        self.assertFalse(Path(path+".dsec-upload-"+transfer).exists())
