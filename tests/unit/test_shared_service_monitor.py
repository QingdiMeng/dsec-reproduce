import unittest
from unittest.mock import patch

import shared_service_monitor as shared


class SharedServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cgroup_scope_and_loss_of_service(self):
        monitor = shared.SharedServiceMonitor(
            [{"name": "threefs_client", "container": "dsec-e2-3fs-client"}],
            interval_seconds=100)
        values = [
            {"cpu_seconds": 1.5, "memory_current_bytes": 100,
             "io_by_device": {"259:0": {"rbytes": 30, "wbytes": 40}}},
            FileNotFoundError("cgroup removed")]
        def sample(_):
            value = values.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        with (patch.object(shared, "_docker_identity", return_value=(123, "a" * 64)),
              patch.object(shared, "_cgroup_for_pid", return_value="/fake/cgroup"),
              patch.object(shared, "_cgroup_sample", side_effect=sample)):
            await monitor.start()
            self.assertTrue(monitor.ready("threefs_client"))
            self.assertIn('dsec_shared_service_memory_bytes{component="threefs_client"} 100',
                          monitor.prometheus_text())
            self.assertIn('dsec_shared_service_read_bytes_total{component="threefs_client"} 30',
                          monitor.prometheus_text())
            monitor.services["threefs_client"]["last_success_monotonic"] -= 1000
            self.assertFalse(monitor.ready("threefs_client"))
            self.assertIn('dsec_shared_service_up{component="threefs_client"} 0',
                          monitor.prometheus_text())
            await monitor.sample_once()
            self.assertFalse(monitor.ready("threefs_client"))
            self.assertFalse(monitor.snapshot()["threefs_client"]["up"])
            self.assertNotIn('dsec_shared_service_memory_bytes{component="threefs_client"}',
                             monitor.prometheus_text())
            await monitor.close()

    async def test_rejects_untrusted_identity_fields(self):
        with self.assertRaises(ValueError):
            shared.SharedServiceMonitor([{"name": "bad-name", "container": "x"}])
        with self.assertRaises(ValueError):
            shared.SharedServiceMonitor([{"name": "threefs", "container": "x;echo bad"}])
        with self.assertRaises(ValueError):
            shared.SharedServiceMonitor([
                {"name": "threefs", "container": "x"},
                {"name": "threefs", "container": "y"}])


if __name__ == "__main__":
    unittest.main()
