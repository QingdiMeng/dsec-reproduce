import asyncio
import unittest

from work_scheduler import HostSample, ResourceBudget, ResourceDemand, WorkScheduler


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

    async def test_oversized_job_is_rejected_without_waiting(self):
        scheduler = WorkScheduler(budget(), FakeSampler())
        with self.assertRaises(ValueError):
            await scheduler.run("huge", ResourceDemand(3, 1000, 1000, 1),
                                lambda: asyncio.sleep(0))


if __name__ == "__main__":
    unittest.main()
