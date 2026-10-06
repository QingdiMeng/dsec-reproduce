"""A snapshot permit limits concurrent Firecracker writes without limiting live VMs."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from sandbox_sdk import Sandbox, SandboxManager, _sparse_block_sha256, _sparse_sha256


class SnapshotConcurrencyTests(unittest.TestCase):
    def test_sparse_hash_covers_data_offsets_and_logical_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "disk.ext4"
            with path.open("wb") as stream:
                stream.write(b"start")
                stream.seek(16 * 1024 * 1024)
                stream.write(b"end")
            original = _sparse_sha256(path)
            self.assertEqual(original, _sparse_sha256(path))
            with path.open("r+b") as stream:
                stream.seek(16 * 1024 * 1024)
                stream.write(b"END")
            self.assertNotEqual(original, _sparse_sha256(path))
            with path.open("r+b") as stream:
                stream.seek(16 * 1024 * 1024)
                stream.write(b"end")
                stream.truncate(16 * 1024 * 1024 + 5)
            self.assertNotEqual(original, _sparse_sha256(path))

    def test_sparse_block_hash_depends_on_content_not_hole_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sparse = root / "sparse"
            dense = root / "dense"
            with sparse.open("wb") as stream:
                stream.write(b"head")
                stream.seek(4 * 1024 * 1024 + 17)
                stream.write(b"tail")
            dense.write_bytes(sparse.read_bytes())
            expected = _sparse_block_sha256(sparse)
            self.assertEqual(expected, _sparse_block_sha256(dense))
            with dense.open("r+b") as stream:
                stream.seek(2 * 1024 * 1024)
                stream.write(b"changed")
            self.assertNotEqual(expected, _sparse_block_sha256(dense))

    def test_full_snapshot_api_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "fc"
            binary.write_bytes(b"fc")
            manager = SandboxManager(root / "sessions", binary, root / "kernel",
                                     root / "template", capacity=2,
                                     snapshot_concurrency=1, start_monitor=False)
            release = threading.Event()
            both_paused = threading.Event()
            first_snapshot = threading.Event()
            guard = threading.Lock()
            active = peak = paused_count = 0

            def make_sandbox():
                sb = Sandbox(manager, 300)
                sb.disk.write_bytes(b"rootfs")
                sb.state = "RUNNING"
                sb._check = lambda: None
                sb.vm.execute = lambda *_args, **_kwargs: {"exit_code": 0}

                def pause_vm():
                    nonlocal paused_count
                    with guard:
                        paused_count += 1
                        if paused_count == 2:
                            both_paused.set()

                def snapshot(_method, _path, body, **_kwargs):
                    nonlocal active, peak
                    with guard:
                        active += 1
                        peak = max(peak, active)
                        first_snapshot.set()
                    if not release.wait(5):
                        raise TimeoutError("test snapshot not released")
                    Path(body["snapshot_path"]).write_bytes(b"state")
                    Path(body["mem_file_path"]).write_bytes(b"memory")
                    with guard:
                        active -= 1

                sb.vm.pause = pause_vm
                sb.vm.api = snapshot
                sb.vm.stop = lambda: None
                return sb

            first, second = make_sandbox(), make_sandbox()
            try:
                with patch("sandbox_sdk.sha", return_value="test-sha"), ThreadPoolExecutor(2) as pool:
                    a = pool.submit(first.pause)
                    b = pool.submit(second.pause)
                    self.assertTrue(both_paused.wait(5))
                    self.assertTrue(first_snapshot.wait(5))
                    with guard:
                        self.assertEqual(active, 1)
                    release.set()
                    a.result(5); b.result(5)
                self.assertEqual(peak, 1)
                self.assertEqual((first.state, second.state), ("PAUSED", "PAUSED"))
            finally:
                release.set()
                manager.close()

    def test_invalid_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for limit in (0, 3, True):
                with self.assertRaises(ValueError):
                    SandboxManager(root / "sessions", root / "fc", root / "kernel",
                                   root / "template", capacity=2,
                                   snapshot_concurrency=limit, start_monitor=False)

    def test_incremental_requires_editor_and_is_scoped_to_tb2(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                SandboxManager(root / "sessions", root / "fc", root / "kernel",
                               root / "template", snapshot_strategy="incremental",
                               start_monitor=False)
            editor = root / "editor"
            editor.write_text("#!/bin/sh\nexit 0\n")
            editor.chmod(0o700)
            manager = SandboxManager(root / "sessions", root / "fc", root / "kernel",
                                     root / "template", snapshot_strategy="incremental",
                                     snapshot_editor=editor,
                                     tb2_templates={"tb2-example": root / "guest"},
                                     start_monitor=False)
            try:
                tb2 = Sandbox(manager, 300, environment_id="tb2-example")
                default = Sandbox(manager, 300)
                self.assertEqual((tb2.snapshot_mode, tb2.dirty_tracking_enabled),
                                 ("incremental", True))
                self.assertEqual((default.snapshot_mode, default.dirty_tracking_enabled),
                                 ("full", False))
            finally:
                manager.close()

    def test_publish_error_preserves_previous_snapshot_and_removes_new_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "fc"
            binary.write_bytes(b"fc")
            manager = SandboxManager(root / "sessions", binary, root / "kernel",
                                     root / "template", start_monitor=False)
            try:
                sb = Sandbox(manager, 300)
                sb.disk.write_bytes(b"rootfs")
                previous = sb.directory / "snapshot-1"
                previous.mkdir()
                (previous / "memory").write_bytes(b"old")
                sb.snapshot = previous
                sb.generation = 1
                sb.state = "RUNNING"
                sb._check = lambda: None
                sb.vm.execute = lambda *_args, **_kwargs: {"exit_code": 0}
                sb.vm.pause = lambda: None
                sb.vm.stop = lambda: None

                def snapshot(_method, _path, body, **_kwargs):
                    Path(body["snapshot_path"]).write_bytes(b"state")
                    Path(body["mem_file_path"]).write_bytes(b"memory")

                def fail_after_publish(stage):
                    if stage == "after_publish":
                        raise OSError("injected publish failure")

                sb.vm.api = snapshot
                sb._snapshot_checkpoint = fail_after_publish
                with patch("sandbox_sdk.sha", return_value="test"), self.assertRaises(OSError):
                    sb.pause()
                self.assertEqual((sb.state, sb.generation, sb.snapshot),
                                 ("FAILED", 1, previous))
                self.assertEqual((previous / "memory").read_bytes(), b"old")
                self.assertFalse((sb.directory / "snapshot-2").exists())
                self.assertFalse(list(sb.directory.glob("pending-*")))
            finally:
                manager.close()


if __name__ == "__main__":
    unittest.main()
