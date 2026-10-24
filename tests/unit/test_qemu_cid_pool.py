"""Controlled durable CID allocation/retirement; no simulated death proof."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from dsec._persistence import atomic_json
from dsec.runtime.backends.qemu import QemuArtifact, QemuConfig, QemuVM
from dsec.runtime.isolation.vsock import CIDLeaseConflict, QemuCIDPool


class CIDPoolTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dsec-cid-'); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.pool = QemuCIDPool(self.root, first=20000, last=20001)
        self.paths = []
        for name in ('a', 'b', 'c'):
            path = self.root/name; path.mkdir(mode=0o700); self.paths.append(path)
        artifact = self.root/'artifact'; artifact.write_bytes(b'trusted fixture')
        self.artifact = QemuArtifact(str(artifact), hashlib.sha256(artifact.read_bytes()).hexdigest())

    def vm(self, lease):
        vm = QemuVM(QemuConfig(self.artifact, self.artifact, self.artifact, lease.cid), lease.directory)
        self.addCleanup(vm.detach)
        return vm

    def test_independent_pool_owners_allocate_at_controlled_concurrent_boundary(self):
        other = QemuCIDPool(self.root, first=20000, last=20001)
        barrier = threading.Barrier(2)
        def reserve(pool, directory):
            barrier.wait(timeout=3); return pool.reserve(directory)
        with ThreadPoolExecutor(max_workers=2) as workers:
            a = workers.submit(reserve, self.pool, self.paths[0])
            b = workers.submit(reserve, other, self.paths[1])
            leases = [a.result(timeout=3), b.result(timeout=3)]
        self.assertEqual({lease.cid for lease in leases}, {20000, 20001})
        self.assertEqual(len(self.pool.snapshot()), 2)
        for lease in leases:
            self.assertEqual(other.reserve(lease.directory), lease)
        with self.assertRaisesRegex(CIDLeaseConflict, 'exhausted'):
            self.pool.reserve(self.paths[2])

    def test_controller_restart_retains_unbound_reservations(self):
        lease = self.pool.reserve(self.paths[0])
        restarted = QemuCIDPool(self.root, first=20000, last=20001)
        self.assertEqual(restarted.reserve(self.paths[0]), lease)
        self.assertEqual(len(restarted.snapshot()), 1)
        # No durable boot intent means there could not have been a driver spawn.
        self.assertEqual(restarted.retire(lease, self.vm(lease))['state'], 'STOPPED')
        self.assertEqual(self.pool.snapshot(), {})

    def test_stale_retirement_token_cannot_stop_or_release_replacement(self):
        old = self.pool.reserve(self.paths[0]); vm = self.vm(old)
        self.pool.retire(old, vm)
        new = self.pool.reserve(self.paths[1])
        self.assertEqual(old.cid, new.cid); self.assertNotEqual(old.token, new.token)
        vm.stop = Mock(side_effect=AssertionError('Stale owner must have no effects'))
        with self.assertRaisesRegex(CIDLeaseConflict, 'fenced'): self.pool.retire(old, vm)
        vm.stop.assert_not_called()
        self.assertEqual(self.pool.snapshot()[str(new.cid)]['token'], new.token)

    def test_stop_failure_or_detached_owner_never_releases(self):
        lease = self.pool.reserve(self.paths[0]); vm = self.vm(lease)
        vm.stop = Mock(side_effect=TimeoutError('Cannot prove death'))
        with self.assertRaises(TimeoutError): self.pool.retire(lease, vm)
        self.assertEqual(self.pool.reserve(self.paths[0]), lease)
        vm.detach()
        with self.assertRaisesRegex(RuntimeError, 'retired'): self.pool.retire(lease, vm)
        self.assertIn(str(lease.cid), self.pool.snapshot())

    def test_pending_intent_requires_adoption_before_retirement(self):
        lease = self.pool.reserve(self.paths[0]); vm = self.vm(lease)
        (vm.directory/'qemu.json').write_text('{"state":"STARTING"}')
        with self.assertRaisesRegex(RuntimeError, 'Adopt'): self.pool.retire(lease, vm)
        self.assertIn(str(lease.cid), self.pool.snapshot())

    def test_unrelated_allocation_does_not_wait_for_vmm_stop(self):
        lease = self.pool.reserve(self.paths[0]); vm = self.vm(lease)
        arrived, release = threading.Event(), threading.Event()
        stop = vm.stop
        def blocked_stop():
            arrived.set(); self.assertTrue(release.wait(3)); return stop()
        vm.stop = blocked_stop
        with ThreadPoolExecutor(max_workers=2) as workers:
            retiring = workers.submit(self.pool.retire, lease, vm)
            self.assertTrue(arrived.wait(3))
            try:
                allocated = workers.submit(self.pool.reserve, self.paths[1]).result(timeout=2)
                self.assertNotEqual(lease.cid, allocated.cid)
                self.assertFalse(retiring.done())
            finally:
                release.set()
            self.assertEqual(retiring.result(timeout=3)['state'], 'STOPPED')

    def test_lost_release_commit_retains_lease_for_safe_retry(self):
        lease = self.pool.reserve(self.paths[0]); vm = self.vm(lease)
        with patch('dsec.runtime.isolation.vsock.atomic_json', side_effect=OSError('fsync failed')):
            with self.assertRaises(OSError): self.pool.retire(lease, vm)
        self.assertEqual(vm.state, 'STOPPED')
        self.assertEqual(self.pool.reserve(self.paths[0]), lease)
        self.pool.retire(lease, vm)
        self.assertEqual(self.pool.snapshot(), {})

    def test_lost_reply_after_release_commit_fences_old_owner(self):
        lease = self.pool.reserve(self.paths[0]); vm = self.vm(lease)
        def committed_then_lost(path, value):
            atomic_json(path, value)
            raise OSError('Reply lost after durable release')
        with patch('dsec.runtime.isolation.vsock.atomic_json', side_effect=committed_then_lost):
            with self.assertRaises(OSError): self.pool.retire(lease, vm)
        replacement = self.pool.reserve(self.paths[1])
        self.assertEqual(replacement.cid, lease.cid)
        vm.stop = Mock(side_effect=AssertionError('Old lease cannot act'))
        with self.assertRaises(CIDLeaseConflict): self.pool.retire(lease, vm)
        vm.stop.assert_not_called()
        self.assertEqual(self.pool.snapshot()[str(replacement.cid)]['token'], replacement.token)

    def test_lost_reservation_reply_reconnects_to_same_durable_token(self):
        def committed_then_lost(path, value):
            atomic_json(path, value)
            raise OSError('Reply lost after durable reserve')
        with patch('dsec.runtime.isolation.vsock.atomic_json', side_effect=committed_then_lost):
            with self.assertRaises(OSError): self.pool.reserve(self.paths[0])
        before = self.pool.snapshot()
        restarted = QemuCIDPool(self.root, first=20000, last=20001)
        lease = restarted.reserve(self.paths[0])
        self.assertEqual(before[str(lease.cid)]['token'], lease.token)
        self.assertEqual(restarted.snapshot(), before)

    def test_invalid_lease_configuration_and_missing_or_damaged_ledger_fail_closed(self):
        lease = self.pool.reserve(self.paths[0]); vm = self.vm(lease)
        for bad in (replace(lease, cid=20001), replace(lease, directory=str(self.paths[1]))):
            with self.assertRaises(CIDLeaseConflict): self.pool.retire(bad, vm)
        with self.assertRaises(CIDLeaseConflict): QemuCIDPool(self.root, first=20001, last=20002)
        (self.root/'cids.json').write_text('{}'); (self.root/'cids.json').chmod(0o600)
        with self.assertRaises(CIDLeaseConflict): self.pool.snapshot()
        (self.root/'cids.json').unlink()
        with self.assertRaisesRegex(CIDLeaseConflict, 'missing'): QemuCIDPool(self.root)

    def test_existing_vm_cannot_reallocate_after_losing_its_reservation(self):
        (self.paths[0]/'qemu.json').write_text('{}')
        with self.assertRaisesRegex(CIDLeaseConflict, 'no CID lease'): self.pool.reserve(self.paths[0])
        self.assertEqual(self.pool.snapshot(), {})

    def test_symlink_ledger_and_lock_rejected(self):
        target = self.root/'other'; target.write_text('{}')
        self.pool.path.unlink(); self.pool.path.symlink_to(target)
        with self.assertRaises(OSError): self.pool.snapshot()
        lock = self.root/'cids.lock'; lock.unlink(); lock.symlink_to(target)
        with self.assertRaises(OSError): self.pool.snapshot()


if __name__ == '__main__':
    unittest.main()
