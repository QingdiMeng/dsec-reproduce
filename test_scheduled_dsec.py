"""The public scheduled SDK must preserve IDs across unknown RPC outcomes."""

import unittest

from framework_profile import FrameworkProfile
from rollout_client import RolloutOutcomeUnknown
from scheduled_dsec import ScheduledDSecClient, ScheduledOutcomeUnknown


ROLL_ID = "a" * 32
PROFILE = FrameworkProfile(backend="microvm", environment="erofs_layers",
                           environment_id="general-tools")


def view(state="ACTIVE", next_step=0):
    return {"rollout_id": ROLL_ID, "sandbox_id": "sandbox-1",
            "state": state, "next_step": next_step}


class FakeWorker:
    def __init__(self):
        self.calls = []
        self.fail_create_once = False
        self.statuses = [view()]

    def call(self, operation, **args):
        self.calls.append((operation, args))
        if operation == "health":
            return {"scheduler_enabled": True, "durable_journal": True}
        if operation == "create":
            if self.fail_create_once:
                self.fail_create_once = False
                raise RolloutOutcomeUnknown("lost reply")
            return view()
        if operation == "status":
            return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if operation == "step":
            return {"entry": {"result": {"exit_code": 0, "output": "ok\n"}},
                    "rollout": view(next_step=args["step_id"] + 1)}
        if operation == "pause":
            return view("PAUSED")
        if operation == "stop":
            return view("STOPPED")
        if operation == "resource_status":
            return {"resource": {"memory_current_bytes": 10}}
        if operation == "evaluate":
            return {**view("COMPLETED", next_step=1),
                    "reward": {"value": 1.0, "expected_counter": args["expected_counter"],
                               "observed": str(args["expected_counter"]),
                               "verifier_exit_code": 0}}
        raise AssertionError(operation)


class ScheduledDSecTest(unittest.IsolatedAsyncioTestCase):
    async def test_create_execute_pause_stop_go_through_worker(self):
        worker = FakeWorker()
        client = ScheduledDSecClient("unused.sock")
        client._transport = worker
        await client.open()
        sandbox = await client.create(task_id="task-1", rollout_id=ROLL_ID,
                                      profile=PROFILE,
                                      resources={"cpu": 1.0, "memory_mb": 512,
                                                 "disk_mb": 1024, "network_mbps": 1.0})
        self.assertEqual(sandbox.sandbox_id, "sandbox-1")
        self.assertEqual((await sandbox.run_shell("echo ok", step_id=0,
                                                  action_id="action-0",
                                                  timeout_ms=12000))["output"], "ok\n")
        self.assertEqual(sandbox.next_step, 1)
        self.assertEqual((await sandbox.pause())["state"], "PAUSED")
        self.assertEqual((await sandbox.resource_status())["resource"]["memory_current_bytes"], 10)
        self.assertEqual((await sandbox.stop())["state"], "STOPPED")
        self.assertEqual([name for name, _ in worker.calls],
                         ["health", "create", "step", "pause", "resource_status", "stop"])
        self.assertEqual(worker.calls[1][1]["rollout_id"], ROLL_ID)
        self.assertEqual(worker.calls[2][1]["action_id"], "action-0")
        self.assertEqual(worker.calls[2][1]["timeout_ms"], 12000)

    async def test_lost_create_reply_preserves_rollout_id_for_attach(self):
        worker = FakeWorker()
        worker.fail_create_once = True
        client = ScheduledDSecClient("unused.sock")
        client._transport = worker
        await client.open()
        with self.assertRaises(ScheduledOutcomeUnknown) as error:
            await client.create(task_id="task-1", rollout_id=ROLL_ID, profile=PROFILE)
        self.assertEqual(error.exception.rollout_id, ROLL_ID)
        sandbox = await client.attach(ROLL_ID)
        self.assertEqual(sandbox.sandbox_id, "sandbox-1")
        self.assertEqual([name for name, _ in worker.calls], ["health", "create", "status"])

    async def test_wait_ready_tracks_queued_rollout(self):
        worker = FakeWorker()
        worker.statuses = [view("QUEUED"), view("ACTIVE")]
        client = ScheduledDSecClient("unused.sock")
        client._transport = worker
        await client.open()
        sandbox = await client.attach(ROLL_ID)
        self.assertEqual(sandbox.state, "QUEUED")
        self.assertEqual((await sandbox.wait_ready(timeout=1, interval=0))["state"], "ACTIVE")

    async def test_counter_verifier_uses_worker_and_rejects_bool(self):
        worker = FakeWorker()
        client = ScheduledDSecClient("unused.sock")
        client._transport = worker
        await client.open()
        sandbox = await client.attach(ROLL_ID)
        with self.assertRaises(ValueError):
            await sandbox.evaluate_counter(True)
        verdict = await sandbox.evaluate_counter(3)
        self.assertEqual(verdict["value"], 1.0)
        self.assertEqual(worker.calls[-1][0], "evaluate")
        self.assertEqual(worker.calls[-1][1]["expected_counter"], 3)

    async def test_refuses_worker_without_scheduler(self):
        worker = FakeWorker()
        worker.call = lambda op, **kwargs: {"scheduler_enabled": False,
                                           "durable_journal": True}
        client = ScheduledDSecClient("unused.sock")
        client._transport = worker
        with self.assertRaisesRegex(RuntimeError, "durable worker with a scheduler"):
            await client.open()


if __name__ == "__main__":
    unittest.main()
