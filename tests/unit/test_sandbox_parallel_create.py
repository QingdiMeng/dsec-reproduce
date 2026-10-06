"""Creation may overlap without reusing capacity or a network slot."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from microvm import MicroVM
from sandbox_sdk import SandboxError, SandboxManager


class ParallelCreateTests(unittest.TestCase):
    def test_refill_respects_disk_headroom_floor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "template.ext4"
            template.write_bytes(b"template")
            boots = []
            with patch.object(MicroVM, "boot", lambda *_args, **_kwargs: boots.append(1)):
                manager = SandboxManager(root / "sessions", root / "fc", root / "kernel",
                                         template, capacity=1,
                                         tb2_templates={"tb2-task": template},
                                         warm_pool_specs={("tb2-task", None): 1},
                                         warm_min_disk_gib=10**9)
                try:
                    time.sleep(.1)
                    self.assertEqual(boots, [])
                    self.assertEqual(manager.warm_pool_status()["pools"][0]["deficit"], 1)
                finally:
                    manager.close()

    def test_idle_refill_waits_for_foreground_and_quiet_period(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "template.ext4"
            template.write_bytes(b"template")
            boots = []

            class LiveProcess:
                pid = 123
                def poll(self):
                    return None

            def boot(vm, *_args, **_kwargs):
                boots.append(time.monotonic())
                vm.process = LiveProcess()

            def stop(vm):
                vm.process = None

            with (patch.object(MicroVM, "boot", boot),
                  patch.object(MicroVM, "stop", stop),
                  patch("sandbox_sdk._copy_sparse", side_effect=shutil.copy2)):
                manager = SandboxManager(root / "sessions", root / "fc", root / "kernel",
                                         template, capacity=2, poll_seconds=.01,
                                         start_monitor=False,
                                         tb2_templates={"tb2-task": template},
                                         warm_pool_specs={("tb2-task", None): 1},
                                         warm_idle_quiet_seconds=.1)
                try:
                    manager.foreground_enter()
                    manager.thread.start()
                    manager.warm_threads[0].start()
                    time.sleep(.15)
                    self.assertEqual(boots, [])
                    ended = time.monotonic()
                    manager.foreground_exit()
                    deadline = time.monotonic()+3
                    while manager.warm_pool_status()["pools"][0]["ready"] != 1:
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.01)
                    self.assertGreaterEqual(boots[0]-ended, .1)
                finally:
                    manager.close()

    def test_waiting_request_claims_refill_before_cold_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "template.ext4"
            template.write_bytes(b"template")
            refill_started = threading.Event()
            release_refill = threading.Event()
            boots = 0

            class LiveProcess:
                pid = 123
                def poll(self):
                    return None

            def boot(vm, *_args, **_kwargs):
                nonlocal boots
                boots += 1
                if boots == 2:
                    refill_started.set()
                    if not release_refill.wait(3):
                        raise TimeoutError("refill not released")
                vm.process = LiveProcess()

            def stop(vm):
                vm.process = None

            with (patch.object(MicroVM, "boot", boot),
                  patch.object(MicroVM, "stop", stop),
                  patch("sandbox_sdk._copy_sparse", side_effect=shutil.copy2)):
                manager = SandboxManager(root / "sessions", root / "fc", root / "kernel",
                                         template, capacity=3, poll_seconds=.01,
                                         tb2_templates={"tb2-task": template},
                                         warm_pool_specs={("tb2-task", None): 1},
                                         warm_wait_ms=1000)
                try:
                    deadline = time.monotonic()+3
                    while manager.warm_pool_status()["pools"][0]["ready"] != 1:
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.01)
                    first = manager.create(environment_id="tb2-task")
                    self.assertTrue(first.warm_pool_hit)
                    self.assertTrue(refill_started.wait(3))
                    with ThreadPoolExecutor(1) as executor:
                        waiting = executor.submit(manager.create, environment_id="tb2-task")
                        while manager.warm_pool_status()["pools"][0]["waiting_requests"] != 1:
                            self.assertLess(time.monotonic(), deadline)
                            time.sleep(.01)
                        release_refill.set()
                        second = waiting.result(3)
                    self.assertTrue(second.warm_pool_hit)
                    self.assertNotEqual(first.id, second.id)
                    self.assertEqual(manager.warm_pool_status()["misses"], 0)
                finally:
                    release_refill.set()
                    manager.close()

    def test_auto_refill_prepares_multiple_vms_in_parallel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "template.ext4"
            template.write_bytes(b"template")
            active = 0
            peak = 0
            count_lock = threading.Lock()

            class LiveProcess:
                pid = 123
                def poll(self):
                    return None

            def boot(vm, *_args, **_kwargs):
                nonlocal active, peak
                with count_lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(.05)
                vm.process = LiveProcess()
                with count_lock:
                    active -= 1

            def stop(vm):
                vm.process = None

            with (patch.object(MicroVM, "boot", boot),
                  patch.object(MicroVM, "stop", stop),
                  patch("sandbox_sdk._copy_sparse", side_effect=shutil.copy2)):
                manager = SandboxManager(root / "sessions", root / "fc", root / "kernel",
                                         template, capacity=4, poll_seconds=.01,
                                         tb2_templates={"tb2-task": template},
                                         warm_pool_specs={("tb2-task", None): 4})
                try:
                    deadline = time.monotonic()+3
                    while manager.warm_pool_status()["pools"][0]["ready"] != 4:
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.01)
                    self.assertGreaterEqual(peak, 2)
                    self.assertEqual(manager.warm_pool_status()["refill_errors"], 0)
                    self.assertEqual(sum(sb.state != "STOPPED" for sb in manager.sandboxes.values()), 4)
                finally:
                    manager.close()

    def test_auto_pool_refills_after_claim_and_respects_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "template.ext4"
            template.write_bytes(b"template")
            manager = None

            class LiveProcess:
                pid = 123
                def poll(self):
                    return None

            def boot(vm, *_args, **_kwargs):
                vm.process = LiveProcess()

            def stop(vm):
                vm.process = None

            with (patch.object(MicroVM, "boot", boot),
                  patch.object(MicroVM, "stop", stop),
                  patch("sandbox_sdk._copy_sparse", side_effect=shutil.copy2)):
                try:
                    manager = SandboxManager(root / "sessions", root / "fc", root / "kernel",
                                             template, capacity=2, poll_seconds=.01,
                                             tb2_templates={"tb2-task": template},
                                             warm_pool_specs={("tb2-task", None): 1})
                    deadline = time.monotonic() + 3
                    while manager.warm_pool_status()["pools"][0]["ready"] != 1:
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.01)
                    claimed = manager.create(environment_id="tb2-task")
                    self.assertTrue(claimed.warm_pool_hit)
                    while manager.warm_pool_status()["pools"][0]["ready"] != 1:
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.01)
                    self.assertEqual(manager.warm_pool_status()["hits"], 1)
                    second = manager.create(environment_id="tb2-task")
                    self.assertTrue(second.warm_pool_hit)
                    with self.assertRaisesRegex(SandboxError, "capacity"):
                        manager.create(environment_id="tb2-task")
                finally:
                    if manager is not None:
                        manager.close()

    def test_prewarmed_vm_is_private_until_claimed_and_claim_is_one_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "template.ext4"
            template.write_bytes(b"template")
            manager = SandboxManager(root / "sessions", root / "fc", root / "kernel",
                                     template, capacity=2, start_monitor=False,
                                     tb2_templates={"tb2-task":template})

            class LiveProcess:
                pid = 123
                def poll(self):
                    return None

            def boot(vm, *_args, **_kwargs):
                vm.process = LiveProcess()

            def stop(vm):
                vm.process = None

            with (patch.object(MicroVM, "boot", boot),
                  patch.object(MicroVM, "stop", stop),
                  patch("sandbox_sdk._copy_sparse", side_effect=shutil.copy2)):
                try:
                    ready = manager.prewarm(environment_id="tb2-task")[0]
                    self.assertEqual(ready["state"], "READY")
                    reserved = manager.sandboxes[ready["id"]]
                    with self.assertRaisesRegex(SandboxError, "reserved"):
                        reserved.execute("true")
                    claimed = manager.create(environment_id="tb2-task")
                    self.assertEqual(claimed.id, ready["id"])
                    self.assertTrue(claimed.status()["warm_pool_hit"])
                    self.assertIn("warm_checkout", claimed.last_create_phases)
                    cold = manager.create(environment_id="tb2-task")
                    self.assertNotEqual(cold.id, claimed.id)
                    self.assertFalse(cold.status()["warm_pool_hit"])
                finally:
                    manager.close()

    def test_preparation_overlaps_but_admission_remains_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "template.ext4"
            template.write_bytes(b"template")
            slots = {"tap-a":{"tap":"tap-a"}, "tap-b":{"tap":"tap-b"}}
            manager = SandboxManager(root / "sessions", root / "fc", root / "kernel",
                                     template, capacity=2, start_monitor=False,
                                     tb2_templates={"tb2-task":template},
                                     tb2_network_slots=slots)
            entered = threading.Event()
            release = threading.Event()
            count = 0
            lock = threading.Lock()

            def boot(*_args, **_kwargs):
                nonlocal count
                with lock:
                    count += 1
                    if count == 2:
                        entered.set()
                if not release.wait(5):
                    raise TimeoutError("Creates did not overlap")

            try:
                with (patch.object(MicroVM, "boot", boot),
                      patch("sandbox_sdk._copy_sparse", side_effect=shutil.copy2),
                      ThreadPoolExecutor(2) as pool):
                    first = pool.submit(manager.create, environment_id="tb2-task")
                    second = pool.submit(manager.create, environment_id="tb2-task")
                    self.assertTrue(entered.wait(5), "Boot preparations were serialized")
                    with self.assertRaisesRegex(SandboxError, "capacity"):
                        manager.create(environment_id="tb2-task")
                    release.set()
                    sandboxes = (first.result(5), second.result(5))
                self.assertEqual({sb.network_slot for sb in sandboxes}, {"tap-a", "tap-b"})
                for sb in sandboxes:
                    sb.stop()
            finally:
                release.set()
                manager.close()

    def test_close_waits_for_inflight_create_and_stops_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "template.ext4"
            template.write_bytes(b"template")
            manager = SandboxManager(root / "sessions", root / "fc", root / "kernel",
                                     template, capacity=1, start_monitor=False)
            entered = threading.Event()
            release = threading.Event()

            def boot(*_args, **_kwargs):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("Create was not released")

            with patch.object(MicroVM, "boot", boot), ThreadPoolExecutor(2) as pool:
                creating = pool.submit(manager.create)
                self.assertTrue(entered.wait(5))
                closing = pool.submit(manager.close)
                release.set()
                sb = creating.result(5)
                closing.result(5)
            self.assertEqual(sb.state, "STOPPED")
            self.assertFalse(sb.disk.exists())


if __name__ == "__main__":
    unittest.main()
