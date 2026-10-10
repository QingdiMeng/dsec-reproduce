"""Exercise the compiled guest service with real persistent shell processes."""
import base64
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import signal
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
