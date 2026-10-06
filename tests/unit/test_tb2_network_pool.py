"""Network slot admission, persistence preflight, and per-VM boot arguments."""
import json
import unittest
from pathlib import Path
import tempfile
import sys
from unittest.mock import patch

if sys.platform == "linux":
    from durable_manager import DurableManager
from microvm import MicroVM
from sandbox_sdk import SandboxError, SandboxManager


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    slots = {
        "tap-a": {"tap": "tap-a", "guest_cidr": "172.30.112.2/30",
                  "gateway": "172.30.112.1", "dns": "192.168.0.1", "mac": "06:00:ac:1e:70:01"},
        "tap-b": {"tap": "tap-b", "guest_cidr": "172.30.112.6/30",
                  "gateway": "172.30.112.5", "dns": "192.168.0.1", "mac": "06:00:ac:1e:70:02"},
    }
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        disk = root / "template.ext4"
        disk.write_bytes(b"test")
        manager = SandboxManager(root / "sessions", root / "fc", root / "kernel", disk,
                                 capacity=3, start_monitor=False,
                                 tb2_templates={"tb2-task": disk}, tb2_network_slots=slots)
        with patch.object(MicroVM, "boot", lambda *args, **kwargs: None):
            first = manager.create(environment_id="tb2-task")
            second = manager.create(environment_id="tb2-task")
            check((first.network_slot, second.network_slot) == ("tap-a", "tap-b"),
                  "concurrent sandboxes must occupy distinct slots")
            try:
                manager.create(environment_id="tb2-task")
            except SandboxError as exc:
                check("network slot" in str(exc), "unexpected admission error")
            else:
                raise AssertionError("third sandbox reused an active slot")
            first.stop()
            replacement = manager.create(environment_id="tb2-task")
            check(replacement.network_slot == "tap-a", "stopped slot not reusable")
            second.stop()
            replacement.stop()

        records = root / "records"
        records.mkdir()
        durable = DurableManager.__new__(DurableManager)
        durable.root, durable.tb2_network_slots = records, slots
        durable.tb2_templates = {"tb2-task": disk}
        for name, slot in (("one", "tap-a"), ("two", "tap-a")):
            folder = records / name
            folder.mkdir()
            (folder / "registry.json").write_text(json.dumps({
                "state": "PAUSED", "environment_id": "tb2-task", "network_slot": slot}))
        try:
            durable._check_network_registry()
        except RuntimeError as exc:
            check("Duplicate" in str(exc), "wrong duplicate-slot failure")
        else:
            raise AssertionError("duplicate recovered slots accepted")

        calls = []
        vm = MicroVM(root / "fc", root)
        vm.start_process = lambda name: None
        vm.api = lambda method, path, body=None: calls.append((path, body))
        vm.wait_ready = lambda: None
        vm.boot(root / "kernel", disk, memory_mib=2048, cpu_count=2,
                network=slots["tap-b"])
        boot = dict(calls)["/boot-source"]["boot_args"]
        check("dsec_guest_ip=172.30.112.6/30" in boot and
              "dsec_gateway=172.30.112.5" in boot,
              "boot arguments did not use selected slot")
        check(dict(calls)["/network-interfaces/eth0"]["host_dev_name"] == "tap-b",
              "wrong TAP passed to Firecracker")
        check(dict(calls)["/machine-config"]["vcpu_count"] == 2,
              "TB2 CPU requirement not passed to Firecracker")
    print("TB2 network pool checks passed")


@unittest.skipUnless(sys.platform == "linux", "Registry recovery requires Linux /proc")
class NetworkSlotTests(unittest.TestCase):
    def test_slot_allocation_recovery_and_boot_arguments(self):
        main()


if __name__ == "__main__":
    unittest.main()
