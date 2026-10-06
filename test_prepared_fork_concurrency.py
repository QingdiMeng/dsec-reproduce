"""Fork restores overlap; source deletion waits for pins, not a long source lock."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import shutil
import unittest
from unittest.mock import patch

from sandbox_sdk import Sandbox, SandboxManager, SandboxError
from snapshot_fork import fork_baseline, _file_identities, _validate_snapshot


class PreparedForkConcurrencyTests(unittest.TestCase):
    def setup_source(self, root):
        binary, kernel = root/'fc', root/'kernel'
        binary.write_bytes(b'fc')
        kernel.write_bytes(b'kernel')
        manager = SandboxManager(root/'sessions', binary, kernel, root/'template',
                                 capacity=4, start_monitor=False)
        sb = Sandbox(manager, 300)
        sb.disk.write_bytes(b'disk')
        sb.snapshot = sb.directory/'snapshot-1'
        sb.snapshot.mkdir()
        for name in ('memory', 'state', 'disk.ext4'):
            (sb.snapshot/name).write_bytes(name.encode())
        manifest = {'id':sb.id, 'generation':1, 'snapshot_mode':'full',
                    'binary_sha256':hashlib.sha256(b'fc').hexdigest(),
                    'guest_kernel':str(kernel), 'environment_manifest_sha256':None,
                    'files':{n:hashlib.sha256((sb.snapshot/n).read_bytes()).hexdigest()
                             for n in ('memory', 'state', 'disk.ext4')}}
        (sb.snapshot/'manifest.json').write_text(json.dumps(manifest))
        sb.generation = 1
        sb.state = 'PAUSED'
        sb.baseline_sealed = True
        sb.baseline_verified = False
        sb._check = lambda: None
        manager.sandboxes[sb.id] = sb
        return manager, sb

    def test_restore_overlap_cached_validation_and_stop_pins(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, source = self.setup_source(Path(tmp))
            entered = threading.Barrier(3, timeout=5)
            release = threading.Event()
            source_stopped = threading.Event()
            source.vm.stop = source_stopped.set

            def create(*_a, **_kw):
                child = Sandbox(manager, 300)
                manager.sandboxes[child.id] = child
                def restore(*_a, **_kw):
                    entered.wait()
                    if not release.wait(5):
                        raise TimeoutError('restore not released')
                child.vm.restore_fork = restore
                child.vm.execute = lambda *_a, **_kw: {'exit_code':0}
                return child
            manager._create_cold = create
            try:
                with (patch('snapshot_fork._validate_snapshot', wraps=_validate_snapshot) as validate,
                      patch('sandbox_sdk._copy_sparse', side_effect=shutil.copyfile)):
                    with ThreadPoolExecutor(4) as pool:
                        args = (manager, source.id, 300, source.environment_id,
                                source.memory_profile, source.verifier_storage, source.storage)
                        first, second = [pool.submit(fork_baseline, *args) for _ in range(2)]
                        try:
                            entered.wait()
                        except threading.BrokenBarrierError:
                            release.set()
                            first.result(5)
                            second.result(5)
                            raise
                        with source.lock:
                            self.assertEqual(source.fork_readers, 2)
                        # _stop waits on a condition and lets forks finish.
                        stops = [pool.submit(source.stop) for _ in range(2)]
                        self.assertFalse(source_stopped.wait(.05))
                        release.set()
                        children = [first.result(5), second.result(5)]
                        for stopped in stops:
                            stopped.result(5)
                    self.assertEqual(validate.call_count, 1)
                self.assertEqual(source.fork_readers, 0)
                self.assertEqual(source.state, 'STOPPED')
                self.assertTrue(all(c.state == 'RUNNING' for c in children))
                self.assertNotEqual(children[0].disk, children[1].disk)
            finally:
                release.set()
                manager.close()

    def test_changed_cached_artifact_rejected_before_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, source = self.setup_source(Path(tmp))
            try:
                source.baseline_verified = True
                source.baseline_identity = _file_identities(source)
                (source.snapshot/'memory').write_bytes(b'changed')
                with patch.object(manager, '_create_cold') as create:
                    with self.assertRaisesRegex(SandboxError, 'artifact identity changed'):
                        fork_baseline(manager, source.id, 300, source.environment_id,
                                      source.memory_profile, source.verifier_storage, source.storage)
                    create.assert_not_called()
            finally:
                manager.close()
