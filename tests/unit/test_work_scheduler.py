import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from work_scheduler import HostSample, ProcHostSampler, ResourceBudget, ResourceDemand, WorkScheduler
from dsec.contracts.resources import APILimits, NodeBudget, NodeDemand
from dsec.runtime.resources import NodeResourceLedger
from dsec.rollout.quotas import APIQuota


class HostSamplerTests(unittest.TestCase):
    def test_cpu_total_counts_vm_time_once(self):
        with patch.object(Path, "read_text", return_value="cpu 100 10 50 500 20 5 5 0 40 2\n"):
            self.assertEqual(ProcHostSampler("/tmp", "unused")._cpu(), (690, 520, 20))


def budget(**changes):
    values = dict(cpu=2, memory_mb=4096, disk_mb=6000, network_mbps=10,
                  api_episode_slots=2, api_inflight=2, api_rpm=5, api_tpm=5000,
                  min_memory_free_mb=100, min_disk_free_mb=100,
                  api_token_reserve=1000)
    values.update(changes)
    return ResourceBudget(**values)


class FakeSampler:
    def __init__(self):
        self.memory_mb = 10000
        self.disk_mb = 20000
        self.cpu = 0
        self.network = 0

    def sample(self):
        return HostSample(0, self.memory_mb, self.disk_mb, self.cpu, self.network)


class FakeCompletion:
    usage = type("Usage", (), {"total_tokens": 123})()


class FakePolicy:
    class Chat:
        class Completions:
            async def create(self, **_):
                return FakeCompletion()
        completions = Completions()
    chat = Chat()


class Fake429Policy:
    class Chat:
        class Completions:
            async def create(self, **_):
                error = RuntimeError("rate limited")
                error.status_code = 429
                error.response = type("Response", (), {"headers":{"retry-after":"2"}})()
                raise error
        completions = Completions()
    chat = Chat()


class WorkSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_dependency_queues_only_affected_work(self):
        ready = {"threefs_client": False}
        scheduler = WorkScheduler(
            budget(), FakeSampler(), sample_interval=.01,
            dependency_ready=lambda name: ready.get(name, False))
        demand = ResourceDemand(1, 512, 1000, 1)
        remote = asyncio.create_task(scheduler.acquire(
            "remote", demand, requirements=("threefs_client",)))
        await asyncio.sleep(.03)
        self.assertFalse(remote.done())
        self.assertIn("dependency_threefs_client_unavailable",
                      scheduler.report()["pending_reasons"]["remote"])
        await scheduler.acquire("local", demand)
        self.assertFalse(remote.done())
        await scheduler.release("local")
        ready["threefs_client"] = True
        await asyncio.wait_for(remote, .2)
        await scheduler.release("remote")

    async def test_lease_survives_create_call_until_explicit_stop(self):
        scheduler = WorkScheduler(budget(memory_mb=1024), FakeSampler(), sample_interval=.01)
        demand = ResourceDemand(1, 1024, 1000, 1)
        await scheduler.acquire("live-sandbox", demand)
        queued = asyncio.create_task(scheduler.acquire("next-sandbox", demand))
        await asyncio.sleep(.03)
        self.assertFalse(queued.done())
        self.assertEqual(scheduler.report()["pending_reasons"]["next-sandbox"],
                         ["memory_mb_budget"])
        self.assertIn('dsec_scheduler_pending_reason{reason="memory_mb_budget"} 1',
                      scheduler.prometheus_text())
        await scheduler.release("live-sandbox")
        await asyncio.wait_for(queued, .2)
        self.assertGreater(scheduler.report()["queue_wait_seconds_last"], .02)
        self.assertIn("dsec_scheduler_queue_wait_seconds_total",
                      scheduler.prometheus_text())
        await scheduler.release("next-sandbox")
        self.assertEqual(scheduler.reserved["memory_mb"], 0)

    async def test_restart_restores_unknown_sandbox_reservation(self):
        scheduler = WorkScheduler(budget(memory_mb=1024), FakeSampler(), sample_interval=.01)
        demand = ResourceDemand(1, 1024, 1000, 1)
        scheduler.restore("uncertain-sandbox", demand)
        queued = asyncio.create_task(scheduler.acquire("next-sandbox", demand))
        await asyncio.sleep(.03)
        self.assertFalse(queued.done())
        await scheduler.release("uncertain-sandbox")
        await asyncio.wait_for(queued, .2)
        await scheduler.release("next-sandbox")

    async def test_dispatch_waits_for_reserved_memory_then_releases(self):
        scheduler = WorkScheduler(budget(memory_mb=2048), FakeSampler(), sample_interval=.01)
        await scheduler.start()
        entered = asyncio.Event()
        release = asyncio.Event()
        order = []
        demand = ResourceDemand(1, 2048, 1000, 1)

        async def first():
            order.append("first")
            entered.set()
            await release.wait()

        async def second():
            order.append("second")

        one = asyncio.create_task(scheduler.run("one", demand, first))
        await entered.wait()
        two = asyncio.create_task(scheduler.run("two", demand, second))
        await asyncio.sleep(.03)
        self.assertEqual(order, ["first"])
        self.assertIn("memory_mb_budget", scheduler.blocked_seconds)
        release.set()
        await asyncio.gather(one, two)
        self.assertEqual(order, ["first", "second"])
        self.assertEqual(scheduler.reserved["memory_mb"], 0)
        await scheduler.close()

    async def test_live_pressure_blocks_even_with_free_budget(self):
        sampler = FakeSampler()
        sampler.memory_mb = 200
        scheduler = WorkScheduler(budget(), sampler, sample_interval=.01)
        demand = ResourceDemand(1, 1000, 1000, 1)
        ran = asyncio.Event()
        async def work():
            ran.set()
        task = asyncio.create_task(scheduler.run("pressure", demand, work))
        await asyncio.sleep(.03)
        self.assertFalse(ran.is_set())
        self.assertIn("memory_pressure", scheduler.blocked_seconds)
        sampler.memory_mb = 10000
        await asyncio.wait_for(task, .2)

    async def test_backfill_runs_small_ready_job_behind_blocked_job(self):
        scheduler = WorkScheduler(budget(), FakeSampler(), sample_interval=.01)
        release = asyncio.Event()
        started = asyncio.Event()
        order = []

        async def first():
            order.append("first")
            started.set()
            await release.wait()

        async def second():
            order.append("large")

        async def third():
            order.append("small")

        one = asyncio.create_task(scheduler.run(
            "one", ResourceDemand(1, 1024, 1000, 1), first))
        await started.wait()
        two = asyncio.create_task(scheduler.run(
            "two", ResourceDemand(2, 1024, 1000, 1), second))
        await asyncio.sleep(.01)
        three = asyncio.create_task(scheduler.run(
            "three", ResourceDemand(1, 1024, 1000, 1), third))
        await asyncio.wait_for(three, .2)
        self.assertEqual(order, ["first", "small"])
        release.set()
        await asyncio.gather(one, two)
        self.assertEqual(order, ["first", "small", "large"])

    async def test_api_usage_is_accounted_per_job(self):
        scheduler = WorkScheduler(budget(), FakeSampler(), sample_interval=.01)

        async def work():
            await scheduler.policy_call(FakePolicy(), "model", [], {})

        await scheduler.run("api", ResourceDemand(1, 1000, 1000, 1), work)
        report = scheduler.report()
        self.assertEqual(report["api_calls"], 1)
        self.assertEqual(report["api_tokens"], 123)
        self.assertEqual(report["completed"]["api"]["api_tokens"], 123)

    async def test_episode_phase_diagnosis_is_separate_from_admission(self):
        scheduler = WorkScheduler(budget(), FakeSampler())
        await scheduler.run("episode", ResourceDemand(1, 1000, 1000, 1),
                            lambda: asyncio.sleep(0))
        scheduler.record_episode("episode", {
            "valid_verdict": True,
            "metrics": {"total_gen_time":45, "eval_time":28,
                        "tool_times":[.1,.2], "reset_time":.5},
            "timing": {"create_seconds":1.4,"stop_seconds":.2}})
        report = scheduler.report()
        self.assertEqual(report["dominant_latency_phase"], "policy_generation")
        self.assertIsNone(report["likely_bottleneck"])

    async def test_pressure_without_wait_is_not_reported_as_bottleneck(self):
        scheduler = WorkScheduler(budget(), FakeSampler())
        scheduler.pressure_seconds["disk_io_saturation"] = 30
        report = scheduler.report()
        self.assertEqual(report["pressure_seconds"]["disk_io_saturation"], 30)
        self.assertEqual(report["primary_constraints"], [])
        self.assertIsNone(report["likely_bottleneck"])

    async def test_api_rpm_waits_until_window_has_capacity(self):
        scheduler = WorkScheduler(budget(api_rpm=1), FakeSampler(), sample_interval=.01)
        await scheduler.policy_call(FakePolicy(), "model", [], {})
        second = asyncio.create_task(scheduler.policy_call(FakePolicy(), "model", [], {}))
        await asyncio.sleep(.03)
        self.assertFalse(second.done())
        self.assertIn("api_rate", scheduler.blocked_seconds)
        scheduler.api_window[0]["time"] -= 61
        async with scheduler.api_condition:
            scheduler.api_condition.notify_all()
        await asyncio.wait_for(second, .2)
        self.assertEqual(scheduler.report()["api_calls"], 2)

    async def test_provider_429_enters_bounded_cooldown(self):
        scheduler = WorkScheduler(budget(), FakeSampler())
        with self.assertRaises(RuntimeError):
            await scheduler.policy_call(Fake429Policy(), "model", [], {})
        report = scheduler.report()
        self.assertEqual(report["api_429"], 1)
        self.assertGreater(report["api_cooldown_remaining_seconds"], 1)

    async def test_episode_quota_blocks_without_charging_node_twice(self):
        scheduler = WorkScheduler(budget(api_episode_slots=1), FakeSampler(), sample_interval=.01)
        demand = ResourceDemand(.5, 512, 1000, 1)
        await scheduler.acquire("one", demand)
        queued = asyncio.create_task(scheduler.acquire("two", demand))
        await asyncio.sleep(.03)
        self.assertFalse(queued.done())
        self.assertEqual(scheduler.pending_reasons["two"], ["api_episode_slots_budget"])
        self.assertEqual(scheduler.node_ledger.reserved["memory_mb"], 512)
        self.assertNotIn("api_episode_slots", scheduler.node_ledger.reserved)
        self.assertEqual(scheduler.episode_quota.reserved, 1)
        # Legacy reporting is a view: editing a snapshot cannot alter admission.
        scheduler.reserved["memory_mb"] = 0
        self.assertEqual(scheduler.report()["reserved"]["memory_mb"], 512)
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        self.assertEqual(scheduler.pending, [])
        self.assertEqual(set(scheduler.node_ledger.leases), {"one"})
        await scheduler.release("one")
        self.assertEqual(scheduler.episode_quota.reserved, 0)
        self.assertEqual(scheduler.node_ledger.reserved["memory_mb"], 0)

    async def test_shared_node_ledger_enforces_one_physical_budget(self):
        b, sampler = budget(memory_mb=1024), FakeSampler()
        ledger = NodeResourceLedger(NodeBudget.from_resource(b), sampler)
        first = WorkScheduler(b, sampler, node_ledger=ledger, sample_interval=.01)
        second = WorkScheduler(b, sampler, node_ledger=ledger, sample_interval=.01)
        demand = ResourceDemand(.5, 1024, 1000, 1)
        await first.acquire("first", demand)
        queued = asyncio.create_task(second.acquire("second", demand))
        await asyncio.sleep(.03)
        self.assertFalse(queued.done())
        self.assertEqual(second.pending_reasons["second"], ["memory_mb_budget"])
        self.assertEqual(second.episode_quota.reserved, 0)
        self.assertEqual(first.report()["resource_scopes"]["node"],
                         second.report()["resource_scopes"]["node"])
        await first.release("first")
        await asyncio.wait_for(queued, .2)
        self.assertEqual(ledger.reserved["memory_mb"], 1024)
        await second.release("second")
        with self.assertRaises(ValueError):
            WorkScheduler(budget(memory_mb=2048), sampler, node_ledger=ledger)
        with self.assertRaises(AttributeError):
            first.budget = budget(memory_mb=2048)
        self.assertEqual(first.report()["budget"]["memory_mb"], 1024)

    async def test_node_identity_conflict_does_not_release_another_owners_lease(self):
        b, sampler = budget(), FakeSampler()
        ledger = NodeResourceLedger(NodeBudget.from_resource(b), sampler)
        first = WorkScheduler(b, sampler, node_ledger=ledger)
        second = WorkScheduler(b, sampler, node_ledger=ledger)
        demand = ResourceDemand(.5, 512, 1000, 1)
        await first.acquire("same", demand)
        with self.assertRaisesRegex(ValueError, "Duplicate node lease"):
            await second.acquire("same", demand)
        self.assertEqual(second.pending, [])
        self.assertEqual(second.episode_quota.reserved, 0)
        self.assertEqual(ledger.leases["same"], NodeDemand.from_resource(demand))
        await first.release("same")

    async def test_partial_admission_failure_rolls_back_only_new_node_lease(self):
        scheduler = WorkScheduler(budget(), FakeSampler())
        demand = ResourceDemand(.5, 512, 1000, 1)
        await scheduler.acquire("live", demand)
        with patch.object(scheduler.episode_quota, "reserve", side_effect=RuntimeError("quota fault")):
            with self.assertRaisesRegex(RuntimeError, "quota fault"):
                await scheduler.acquire("failed", demand)
        self.assertEqual(scheduler.pending, [])
        self.assertEqual(list(scheduler.node_ledger.leases), ["live"])
        self.assertEqual(list(scheduler.episode_quota.leases), ["live"])
        self.assertEqual(list(scheduler.active), ["live"])
        await scheduler.release("live")

    async def test_over_budget_unknown_recovery_keeps_both_owners_reserved(self):
        scheduler = WorkScheduler(budget(memory_mb=512, api_episode_slots=1),
                                  FakeSampler(), sample_interval=.01)
        scheduler.restore("unknown", ResourceDemand(1, 1024, 1000, 1, 2))
        self.assertEqual(scheduler.node_ledger.reserved["memory_mb"], 1024)
        self.assertEqual(scheduler.episode_quota.reserved, 2)
        queued = asyncio.create_task(scheduler.acquire("new", ResourceDemand(.5, 512, 1000, 1)))
        await asyncio.sleep(.03)
        self.assertEqual(scheduler.pending_reasons["new"],
                         ["memory_mb_budget", "api_episode_slots_budget"])
        await scheduler.release("unknown")
        await asyncio.wait_for(queued, .2)
        await scheduler.release("new")
        self.assertEqual(scheduler.report()["reserved"]["api_episode_slots"], 0)

    async def test_cancelled_api_waiter_uses_neither_concurrency_nor_rate_quota(self):
        scheduler = WorkScheduler(budget(api_inflight=1, api_rpm=2),
                                  FakeSampler(), sample_interval=.01)
        entered, finish = asyncio.Event(), asyncio.Event()
        async def create(**_):
            entered.set()
            await finish.wait()
            return FakeCompletion()
        policy = type("Policy", (), {"chat": type("Chat", (), {
            "completions": type("Completions", (), {"create": staticmethod(create)})()})()})()
        first = asyncio.create_task(scheduler.policy_call(policy, "model", [], {}))
        await entered.wait()
        waiting = asyncio.create_task(scheduler.policy_call(FakePolicy(), "model", [], {}))
        await asyncio.sleep(.03)
        self.assertFalse(waiting.done())
        self.assertEqual(len(scheduler.api_window), 1)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertEqual(scheduler.api_quota.inflight, 1)
        finish.set()
        await first
        await asyncio.wait_for(scheduler.policy_call(FakePolicy(), "model", [], {}), .2)
        self.assertEqual(scheduler.api_quota.inflight, 0)
        self.assertEqual(scheduler.report()["api_calls"], 2)
        self.assertEqual(len(scheduler.api_window), 2)
        self.assertEqual(scheduler.node_ledger.reserved["memory_mb"], 0)

    async def test_cancelled_attempt_keeps_rate_reservation_and_live_sandbox_lease(self):
        scheduler = WorkScheduler(budget(), FakeSampler())
        entered = asyncio.Event()
        async def create(**_):
            entered.set()
            await asyncio.Event().wait()
        policy = type("Policy", (), {"chat": type("Chat", (), {
            "completions": type("Completions", (), {"create": staticmethod(create)})()})()})()
        await scheduler.acquire("live", ResourceDemand(.5, 512, 1000, 1))
        call = asyncio.create_task(scheduler.policy_call(policy, "model", [], {}))
        await entered.wait()
        call.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await call
        self.assertEqual(scheduler.api_quota.inflight, 0)
        self.assertEqual(len(scheduler.api_window), 1)
        self.assertEqual(scheduler.node_ledger.reserved["memory_mb"], 512)
        await scheduler.release("live")

    async def test_explicit_shared_api_quota_limits_calls_across_schedulers(self):
        b = budget(api_inflight=1)
        quota = APIQuota(APILimits.from_resource(b))
        first = WorkScheduler(b, FakeSampler(), api_quota=quota, sample_interval=.01)
        second = WorkScheduler(b, FakeSampler(), api_quota=quota, sample_interval=.01)
        entered, finish = asyncio.Event(), asyncio.Event()
        async def create(**_):
            entered.set()
            await finish.wait()
            return FakeCompletion()
        policy = type("Policy", (), {"chat": type("Chat", (), {
            "completions": type("Completions", (), {"create": staticmethod(create)})()})()})()
        call = asyncio.create_task(first.policy_call(policy, "model", [], {}))
        await entered.wait()
        waiting = asyncio.create_task(second.policy_call(FakePolicy(), "model", [], {}))
        await asyncio.sleep(.03)
        self.assertFalse(waiting.done())
        self.assertEqual(len(quota.window), 1)
        self.assertGreater(second.blocked_seconds["api_inflight"], 0)
        finish.set()
        await asyncio.gather(call, waiting)
        self.assertEqual(first.report()["api_calls"], 2)
        self.assertEqual(second.report()["api_calls"], 2)
        with self.assertRaises(ValueError):
            WorkScheduler(budget(api_inflight=2), FakeSampler(), api_quota=quota)

    async def test_oversized_job_is_rejected_without_waiting(self):
        scheduler = WorkScheduler(budget(), FakeSampler())
        with self.assertRaises(ValueError):
            await scheduler.run("huge", ResourceDemand(3, 1000, 1000, 1),
                                lambda: asyncio.sleep(0))


if __name__ == "__main__":
    unittest.main()
