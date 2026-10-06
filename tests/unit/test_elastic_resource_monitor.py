import asyncio
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

import elastic_resource_monitor as resource
from rollout_workerd import RolloutWorker
import sandbox_resource_rpc
from sandbox_client import ServiceError


class Container:
    backend = "container"
    id = "a" * 32
    _container = type("Inner", (), {"name": "dsec-e1-" + "a" * 32})()


class ResourceMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_manager_meter_attests_registered_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = {"pid": 123, "start_ticks": "9", "uid": 1000}
            (root / "registry.json").write_text(json.dumps({"process": expected}))
            sb = type("Sandbox", (), {"state": "RUNNING",
                "directory": root, "vm": type("VM", (), {
                    "process": type("Process", (), {"pid": 123})()})()})()
            manager = type("Manager", (), {"lock": __import__("threading").Lock(),
                "sandboxes": {"abc": sb}})()
            sample = {"pss_bytes": 100, "rss_bytes": 200, "cpu_seconds": 1.0}
            self.assertEqual(sandbox_resource_rpc.sandbox_resource_sample(
                manager, "abc", lambda _: expected, lambda *_: sample),
                                 {"pid": 123, "sample": {"pss_bytes": 100,
                                  "rss_bytes": 200, "cpu_seconds": 1.0}})
            with self.assertRaisesRegex(sandbox_resource_rpc.SandboxError, "identity mismatch"):
                sandbox_resource_rpc.sandbox_resource_sample(
                    manager, "abc", lambda _: {**expected, "start_ticks": "10"})

    async def test_microvm_monitor_accepts_attested_service_sample(self):
        calls = iter([{"pid": 123, "sample": {
            "pss_bytes": 100, "rss_bytes": 200, "cpu_seconds": 1.0}},
            ServiceError("SandboxError", "No running sandbox to meter")])
        def read():
            value = next(calls)
            if isinstance(value, Exception):
                raise value
            return value
        monitor = resource.ElasticResourceMonitor(
            backend="microvm", pid=123, sample_reader=read)
        with patch.object(resource, "_process_identity", return_value=(123, 9)):
            await monitor.start()
            self.assertEqual(monitor.snapshot()["latest"]["pss_bytes"], 100)
            monitor.mark_stopping()
            self.assertEqual((await monitor.finish())["errors"], [])

    async def test_prometheus_uses_rss_when_microvm_pss_unavailable(self):
        worker = RolloutWorker(None)
        worker.meters["vm"] = type("Meter", (), {
            "scope": "firecracker_process", "backend": "microvm",
            "latest": {"rss_bytes": 1234, "cpu_seconds": 0.2}})()
        metrics = worker.resource_metrics_text()
        self.assertIn('dsec_sandbox_actual_memory_bytes{scope="firecracker_process",basis="rss"} 1234', metrics)
        self.assertNotIn('basis="pss"', metrics)

    async def test_expected_source_removal_preserves_last_sample(self):
        with (patch.object(resource, "_container_identity", return_value=(123, "b" * 64)),
              patch.object(resource, "_process_identity", return_value=(123, 99)),
              patch.object(resource, "_cgroup_for_pid", return_value=Path("/fake/cgroup")),
              patch.object(resource, "_cgroup_sample", side_effect=[
                  {"cpu_seconds": 0.2, "memory_current_bytes": 100,
                   "io_by_device": {}}, FileNotFoundError("removed")])):
            monitor = await resource.ElasticResourceMonitor.for_sandbox(
                Container(), interval_seconds=100)
            monitor.mark_stopping()
            summary = await monitor.finish()
            self.assertEqual(summary["sample_count"], 1)
            self.assertEqual(summary["latest"]["memory_current_bytes"], 100)
            self.assertEqual(summary["errors"], [])

    async def test_bounded_cgroup_memory_cpu_and_io_snapshot(self):
        values = [
            {"cpu_seconds": 1.0, "memory_current_bytes": 100,
             "memory_peak_bytes": 120, "memory_anon_bytes": 60,
             "memory_file_bytes": 40,
             "io_by_device": {"259:0": {"rbytes": 10, "wbytes": 20}}},
            {"cpu_seconds": 1.5, "memory_current_bytes": 80,
             "memory_peak_bytes": 130, "memory_anon_bytes": 50,
             "memory_file_bytes": 30,
             "io_by_device": {"259:0": {"rbytes": 30, "wbytes": 50}}},
        ]
        with (patch.object(resource, "_container_identity", return_value=(123, "b" * 64)),
              patch.object(resource, "_process_identity", return_value=(123, 99)),
              patch.object(resource, "_cgroup_for_pid", return_value=Path("/fake/cgroup")),
              patch.object(resource, "_cgroup_sample", side_effect=lambda _: values.pop(0) if values else {
                  "cpu_seconds": 1.5, "memory_current_bytes": 80,
                  "memory_peak_bytes": 130, "io_by_device": {}})):
            monitor = await resource.ElasticResourceMonitor.for_sandbox(
                Container(), interval_seconds=100)
            await asyncio.to_thread(monitor._read)
            snapshot = monitor.snapshot()
            self.assertEqual(snapshot["scope"], "container_cgroup_v2")
            self.assertEqual(snapshot["sample_count"], 2)
            self.assertEqual(snapshot["latest"]["read_bytes"], 30)
            self.assertEqual(snapshot["latest"]["write_bytes"], 50)
            self.assertEqual(snapshot["peaks"]["memory_current_bytes"], 100)
            self.assertEqual(snapshot["peaks"]["memory_peak_bytes"], 130)
            self.assertEqual((await monitor.finish())["sample_count"], 3)


if __name__ == "__main__":
    unittest.main()
