"""Durable resource leases for generic rollout sandboxes."""

import asyncio
from pathlib import Path
import tempfile
import unittest
import uuid

from rollout_workerd import RolloutWorker
from work_scheduler import HostSample, ResourceBudget, WorkScheduler


class Sampler:
    def sample(self):
        return HostSample(0, 10000, 10000, 0, 0)


class Transport:
    def __init__(self):
        self.states = {}

    def call(self, operation, sandbox_id=None, **_):
        if operation == "status":
            return {"state": self.states[sandbox_id]}
        if operation == "stop":
            self.states[sandbox_id] = "STOPPED"
            return {"state": "STOPPED"}
        raise AssertionError(operation)


class Sandbox:
    def __init__(self, transport):
        self.id = uuid.uuid4().hex
        self.transport = transport
        transport.states[self.id] = "RUNNING"

    async def stop(self, **_):
        return self.transport.call("stop", self.id)


class Client:
    def __init__(self):
        self._transport = Transport()

    async def run_microvm(self, *_args, **_kwargs):
        return Sandbox(self._transport)


def scheduler():
    budget = ResourceBudget(cpu=1, memory_mb=512, disk_mb=1024,
                            network_mbps=2, api_episode_slots=1,
                            api_inflight=1, api_rpm=10, api_tpm=10000,
                            min_memory_free_mb=0, min_disk_free_mb=0)
    return WorkScheduler(budget, Sampler(), sample_interval=.01)


class RolloutSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduled_create_requires_recoverable_id(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = RolloutWorker(Client(), state_dir=Path(directory), scheduler=scheduler())
            with self.assertRaisesRegex(ValueError, "requires rollout_id"):
                await worker.dispatch({"operation": "create", "args": {"task_id": "no-id"}})
            worker.store.lock.close()

    async def test_queued_rollout_can_resume_after_worker_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            client = Client()
            first_scheduler = scheduler()
            worker = RolloutWorker(client, state_dir=Path(directory),
                                   scheduler=first_scheduler)
            first_id, queued_id = uuid.uuid4().hex, uuid.uuid4().hex
            await worker.dispatch({"operation": "create", "args": {
                "task_id": "first", "rollout_id": first_id}})
            queued = asyncio.create_task(worker.dispatch({"operation": "create", "args": {
                "task_id": "queued", "rollout_id": queued_id}}))
            await asyncio.sleep(.03)
            queued.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await queued
            self.assertEqual(worker.rollouts[queued_id].state, "QUEUED")
            worker.store.lock.close()

            recovered_scheduler = scheduler()
            recovered = RolloutWorker(client, state_dir=Path(directory),
                                      scheduler=recovered_scheduler)
            await recovered.initialize()
            retried = asyncio.create_task(recovered.dispatch({"operation": "create", "args": {
                "task_id": "queued", "rollout_id": queued_id}}))
            await asyncio.sleep(.03)
            self.assertFalse(retried.done())
            await recovered.dispatch({"operation": "stop", "args": {"rollout_id": first_id}})
            self.assertEqual((await asyncio.wait_for(retried, .5))["state"], "ACTIVE")
            await recovered.dispatch({"operation": "stop", "args": {"rollout_id": queued_id}})
            recovered.store.lock.close()

    async def test_wait_release_and_restart_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            client = Client()
            first_scheduler = scheduler()
            worker = RolloutWorker(client, state_dir=Path(directory),
                                   scheduler=first_scheduler)
            resources = {"cpu": 1, "memory_mb": 512, "disk_mb": 1024,
                         "network_mbps": 1, "api_episode_slots": 1}
            first_id, second_id = uuid.uuid4().hex, uuid.uuid4().hex
            first = await worker.dispatch({"operation": "create", "args": {
                "task_id": "first", "rollout_id": first_id, "resources": resources}})
            self.assertEqual(first["state"], "ACTIVE")
            queued = asyncio.create_task(worker.dispatch({"operation": "create", "args": {
                "task_id": "second", "rollout_id": second_id, "resources": resources}}))
            await asyncio.sleep(.04)
            self.assertFalse(queued.done())
            self.assertIn("memory_mb_budget",
                          first_scheduler.report()["pending_reasons"][second_id])
            await worker.dispatch({"operation": "stop", "args": {"rollout_id": first_id}})
            second = await asyncio.wait_for(queued, .5)
            self.assertEqual(second["state"], "ACTIVE")
            self.assertEqual(first_scheduler.reserved["memory_mb"], 512)
            worker.store.lock.close()

            recovered_scheduler = scheduler()
            recovered = RolloutWorker(client, state_dir=Path(directory),
                                      scheduler=recovered_scheduler)
            await recovered.initialize()
            self.assertEqual(recovered_scheduler.reserved["memory_mb"], 512)
            self.assertTrue(recovered_scheduler.active[second_id]["recovered"])
            await recovered.dispatch({"operation": "stop", "args": {"rollout_id": second_id}})
            self.assertEqual(recovered_scheduler.reserved["memory_mb"], 0)
            recovered.store.lock.close()


if __name__ == "__main__":
    unittest.main()
