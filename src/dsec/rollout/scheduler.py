"""Work admission composing node leases, episode slots and external API quotas.

The worker journal still restores leases on restart. Node ownership has been
isolated in ``runtime.resources``; moving that authority into the Edge service
is a separate migration. Legacy reports are derived from the separate owners.
"""
from __future__ import annotations

import asyncio
from collections import Counter, deque
import contextvars
from dataclasses import asdict
from dsec.contracts.resources import APILimits, NodeBudget, NodeDemand, ResourceBudget, ResourceDemand
from dsec.runtime.resources import HostSample, ProcHostSampler, NodeResourceLedger
from dsec.rollout.quotas import APIQuota, EpisodeQuota
import re
import time


_current_job = contextvars.ContextVar("dsec_scheduler_job", default=None)


class WorkScheduler:
    def __init__(self, budget: ResourceBudget, sampler, *, sample_interval=1.0,
                 dependency_ready=None, node_ledger=None, api_quota=None, node_status=None):
        self._budget = budget
        self.sampler = sampler
        self.sample_interval = sample_interval
        self.node_status_reader = node_status
        self.edge_node_snapshot = None
        self.node_pending = {}
        self.node_budget = NodeBudget.from_resource(budget)
        if node_status is not None:
            if node_ledger is not None:
                raise ValueError('Edge authority cannot coexist with a worker node ledger')
            self.node_ledger = None
            self.condition = asyncio.Condition()
        else:
            self.node_ledger = node_ledger or NodeResourceLedger(self.node_budget, sampler)
            if self.node_ledger.budget != self.node_budget or self.node_ledger.sampler is not sampler:
                raise ValueError("Shared node ledger budget/sampler mismatch")
            self.condition = self.node_ledger.condition
        self.episode_quota = EpisodeQuota(budget.api_episode_slots)
        self.api_quota = api_quota or APIQuota(APILimits.from_resource(budget))
        if self.api_quota.limits != APILimits.from_resource(budget):
            raise ValueError("Shared API quota limits mismatch")
        self.pending = []
        self._pending_demand = {}
        self._pending_requirements = {}
        self.dependency_ready = dependency_ready or (lambda _: True)
        self.pending_reasons = {}
        self.active = {}
        self.completed = {}
        self.completed_total = 0
        self.admitted_total = 0
        self.queue_wait_seconds_total = 0.0
        self.queue_wait_seconds_max = 0.0
        self.queue_wait_seconds_last = 0.0
        self.blocked_seconds = Counter()
        self.pressure_seconds = Counter()
        self.samples = deque(maxlen=3600)
        self.monitor_task = None

    @property
    def budget(self):
        # Replacing only the legacy composite would silently leave quota and
        # node limits unchanged. Configure all owners at construction instead.
        return self._budget

    @property
    def reserved(self):
        """Legacy report view, not another reservation ledger."""
        node = (self.node_ledger.reserved if self.node_ledger is not None else
                (self.edge_node_snapshot or {}).get('reserved', {}))
        return Counter(**node, api_episode_slots=self.episode_quota.reserved)

    @property
    def api_window(self):
        return self.api_quota.window

    @property
    def api_condition(self):
        return self.api_quota.condition

    def _demand_too_large(self, demand):
        node = NodeDemand.from_resource(demand)
        return (any(getattr(node, name) > getattr(self.node_budget, name)
                    for name in ('cpu','memory_mb','disk_mb','network_mbps')) or
                (self.node_budget.disk_io_mbps > 0 and node.disk_io_mbps > self.node_budget.disk_io_mbps) or
                demand.api_episode_slots > self.episode_quota.capacity)

    def _blockers(self, demand, sample, requirements=()):
        blockers = (self.node_ledger.blockers(NodeDemand.from_resource(demand), sample)
                    if self.node_ledger is not None else [])
        if not self.episode_quota.available(demand.api_episode_slots):
            # Preserve the established metric reason and old resource wire schema.
            blockers.append("api_episode_slots_budget")
        for component in requirements:
            if not self.dependency_ready(component):
                blockers.append("dependency_" + component + "_unavailable")
        return blockers

    def _reserve(self, job_id, demand, wait_started, sample):
        # No await between these mutations. Roll back if the second owner refuses.
        if self.node_ledger is not None:
            self.node_ledger.reserve(job_id, NodeDemand.from_resource(demand), sample)
        try:
            self.episode_quota.reserve(job_id, demand.api_episode_slots)
        except BaseException:
            if self.node_ledger is not None:
                self.node_ledger.release(job_id)
            raise
        self.pending.remove(job_id)
        self.pending_reasons.pop(job_id, None)
        wait_seconds = time.monotonic() - wait_started
        self.admitted_total += 1
        self.queue_wait_seconds_total += wait_seconds
        self.queue_wait_seconds_max = max(self.queue_wait_seconds_max, wait_seconds)
        self.queue_wait_seconds_last = wait_seconds
        self.active[job_id] = {"demand": demand, "started": time.monotonic(),
                               "wait_seconds": wait_seconds,
                               "api_calls": 0, "api_tokens": 0}

    async def start(self):
        if self.monitor_task is not None:
            raise RuntimeError("Scheduler already started")
        self.monitor_task = asyncio.create_task(self._monitor())

    async def close(self):
        if self.monitor_task is not None:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
            self.monitor_task = None

    async def _monitor(self):
        while True:
            if self.node_status_reader is not None:
                await self.refresh_node_status()
                sample = HostSample(**self.edge_node_snapshot['sample'])
            else:
                sample = self.sampler.sample()
            self.samples.append({**asdict(sample), "active": len(self.active),
                                 "pending": len(self.pending),
                                 "reserved": dict(self.reserved)})
            if sample.cpu_utilization >= self.budget.max_cpu_utilization:
                self.pressure_seconds["cpu_saturation"] += self.sample_interval
            if sample.network_mbps >= self.budget.network_mbps:
                self.pressure_seconds["network_saturation"] += self.sample_interval
            if sample.memory_available_mb <= self.budget.min_memory_free_mb:
                self.pressure_seconds["memory_pressure"] += self.sample_interval
            if sample.disk_available_mb <= self.budget.min_disk_free_mb:
                self.pressure_seconds["disk_pressure"] += self.sample_interval
            if sample.disk_busy >= self.budget.max_disk_busy:
                self.pressure_seconds["disk_busy"] += self.sample_interval
            if self.budget.disk_io_mbps > 0 and sample.disk_io_mbps >= self.budget.disk_io_mbps:
                self.pressure_seconds["disk_io_saturation"] += self.sample_interval
            await asyncio.sleep(self.sample_interval)

    async def acquire(self, job_id, demand: ResourceDemand, *, requirements=()):
        """Hold a resource lease until an explicit release, including across RPCs."""
        if self._demand_too_large(demand):
            raise ValueError(f"Job {job_id} exceeds configured resource budget")
        requirements = tuple(requirements)
        if any(not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name)
               for name in requirements):
            raise ValueError("Invalid service dependency")
        waited_at = last_check = time.monotonic()
        async with self.condition:
            if job_id in self.pending or job_id in self.active or job_id in self.completed:
                raise ValueError("Duplicate scheduler job ID")
            self._pending_demand[job_id] = demand
            self._pending_requirements[job_id] = requirements
            self.pending.append(job_id)
            self.condition.notify_all()
            try:
                while True:
                    if self.node_ledger is not None and job_id in self.node_ledger.leases:
                        raise ValueError("Duplicate node lease ID")
                    sample = self.sampler.sample() if self.node_ledger is not None else None
                    now = time.monotonic()
                    blockers = self._blockers(demand, sample, requirements)
                    if not blockers:
                        # First-fit backfill: a large blocked job does not idle
                        # resources needed by a smaller ready task.
                        first_fit = next((candidate for candidate in self.pending
                                          if not self._blockers(
                                              self._pending_demand[candidate], sample,
                                              self._pending_requirements[candidate])), None)
                        if first_fit == job_id:
                            self._reserve(job_id, demand, waited_at, sample)
                            self._pending_demand.pop(job_id)
                            self._pending_requirements.pop(job_id)
                            self.condition.notify_all()
                            break
                        blockers = ["queue_order"]
                    self.pending_reasons[job_id] = list(blockers)
                    for blocker in blockers:
                        self.blocked_seconds[blocker] += now - last_check
                    last_check = now
                    try:
                        await asyncio.wait_for(self.condition.wait(), self.sample_interval)
                    except asyncio.TimeoutError:
                        pass
            except BaseException:
                if job_id in self.pending:
                    self.pending.remove(job_id)
                self._pending_demand.pop(job_id, None)
                self._pending_requirements.pop(job_id, None)
                self.pending_reasons.pop(job_id, None)
                self.condition.notify_all()
                raise

    def restore(self, job_id, demand: ResourceDemand):
        """Conservatively account for a possibly live sandbox after worker restart.

        Called during initialization before serving concurrent requests.
        """
        if job_id in self.pending or job_id in self.active or job_id in self.completed:
            raise ValueError("Duplicate scheduler job ID")
        if self.node_ledger is not None:
            self.node_ledger.restore(job_id, NodeDemand.from_resource(demand))
        try:
            self.episode_quota.restore(job_id, demand.api_episode_slots)
        except BaseException:
            if self.node_ledger is not None:
                self.node_ledger.release(job_id)
            raise
        self.active[job_id] = {"demand": demand, "started": time.monotonic(),
                               "wait_seconds": None, "api_calls": 0, "api_tokens": 0,
                               "recovered": True}

    async def release(self, job_id):
        async with self.condition:
            detail = self.active[job_id]
            demand = detail["demand"]
            if ((self.node_ledger is not None and
                 self.node_ledger.leases.get(job_id) != NodeDemand.from_resource(demand)) or
                    self.episode_quota.leases.get(job_id) != demand.api_episode_slots):
                raise RuntimeError("Resource lease ownership mismatch")
            if self.node_ledger is not None:
                self.node_ledger.release(job_id)
            self.episode_quota.release(job_id)
            self.active.pop(job_id)
            detail["duration_seconds"] = time.monotonic() - detail["started"]
            detail["demand"] = asdict(detail["demand"])
            self.completed[job_id] = detail
            self.completed_total += 1
            if len(self.completed) > 4096:
                self.completed.pop(next(iter(self.completed)))
            self.condition.notify_all()

    async def run(self, job_id, demand: ResourceDemand, work):
        await self.acquire(job_id, demand)
        token = _current_job.set(job_id)
        try:
            return await work()
        finally:
            _current_job.reset(token)
            await self.release(job_id)

    def record_episode(self, job_id, row):
        """Attach phase timing to a finished work item without changing its lease."""
        if job_id not in self.completed:
            raise ValueError("Episode work has not completed")
        metrics = row.get("metrics") or {}
        timing = row.get("timing") or {}
        phases = {
            "policy_generation":float(metrics.get("total_gen_time") or 0),
            "verifier":float(metrics.get("eval_time") or 0),
            "tool_execution":sum(float(x) for x in (metrics.get("tool_times") or [])),
            "reset":float(metrics.get("reset_time") or 0),
            "sandbox_create":float(timing.get("create_seconds") or 0),
            "sandbox_stop":float(timing.get("stop_seconds") or 0),
        }
        self.completed[job_id]["phase_seconds"] = phases
        self.completed[job_id]["valid_verdict"] = row.get("valid_verdict")

    async def refresh_node_status(self):
        if self.node_status_reader is None:
            return
        snapshot = await self.node_status_reader()
        if snapshot.get('authority') != 'edge' or snapshot.get('budget') != asdict(self.node_budget):
            raise RuntimeError('Edge node budget/authority disagrees with worker configuration')
        self.edge_node_snapshot = snapshot

    def record_node_wait(self, job_id, reasons, seconds=0):
        if reasons:
            self.node_pending[job_id] = list(reasons)
        else:
            self.node_pending.pop(job_id, None)
        for reason in reasons:
            self.blocked_seconds[reason] += seconds
        if seconds and job_id in self.active:
            detail = self.active[job_id]
            detail['wait_seconds'] = (detail['wait_seconds'] or 0) + seconds
            detail['node_wait_seconds'] = detail.get('node_wait_seconds', 0) + seconds
            self.queue_wait_seconds_total += seconds
            self.queue_wait_seconds_last = detail['wait_seconds']
            self.queue_wait_seconds_max = max(self.queue_wait_seconds_max, detail['wait_seconds'])

    async def policy_call(self, policy, model, messages, request_kwargs):
        job_id = _current_job.get()
        completion, tokens = await self.api_quota.call(
            policy, model, messages, request_kwargs,
            blocked_seconds=self.blocked_seconds, sample_interval=self.sample_interval)
        if job_id in self.active:
            self.active[job_id]["api_calls"] += 1
            self.active[job_id]["api_tokens"] += tokens
        return completion

    def report(self):
        pressure = {key:value for key,value in self.pressure_seconds.items() if value > 0}
        blocked = {key:value for key,value in self.blocked_seconds.items() if value > .01}
        # Pressure with no queued work is a warning, not evidence that the
        # resource delayed throughput. Keep it visible, but only rank actual
        # admission blockers as constraints.
        ranked = Counter(blocked).most_common()
        primary = ([key for key,value in ranked if value >= ranked[0][1] * .95]
                   if ranked else [])
        phase_totals = Counter()
        for item in self.completed.values():
            phase_totals.update(item.get("phase_seconds", {}))
        return {"budget": asdict(self.budget), "reserved": dict(self.reserved),
                "resource_scopes": {
                    "node": ({'authority':'worker', "budget": asdict(self.node_budget),
                              "reserved": dict(self.node_ledger.reserved),
                              "lease_ids": list(self.node_ledger.leases)}
                             if self.node_ledger is not None else self.edge_node_snapshot),
                    "job": {"api_episode_slots": self.episode_quota.capacity,
                            "reserved_api_episode_slots": self.episode_quota.reserved},
                    "api": {"scope": "process", "limits": asdict(self.api_quota.limits),
                            "inflight": self.api_quota.inflight}},
                "active": list(self.active),
                "pending": list(self.pending) + list(self.node_pending),
                "pending_reasons": {**self.pending_reasons, **self.node_pending},
                "completed": dict(self.completed), "completed_total": self.completed_total,
                "admitted_total": self.admitted_total,
                "queue_wait_seconds_total": self.queue_wait_seconds_total,
                "queue_wait_seconds_max": self.queue_wait_seconds_max,
                "queue_wait_seconds_last": self.queue_wait_seconds_last,
                "blocked_seconds": blocked, "pressure_seconds": pressure,
                "primary_constraints": primary,
                "likely_bottleneck": (primary[0] if len(primary) == 1 else
                                      "multiple_constraints" if primary else None),
                **self.api_quota.report(),
                "phase_seconds_total":dict(phase_totals),
                "dominant_latency_phase":(phase_totals.most_common(1)[0][0]
                                          if phase_totals else None),
                "min_memory_available_mb":min((s["memory_available_mb"] for s in self.samples),default=None),
                "min_disk_available_mb":min((s["disk_available_mb"] for s in self.samples),default=None),
                "peak_cpu_utilization":max((s["cpu_utilization"] for s in self.samples),default=None),
                "peak_network_mbps":max((s["network_mbps"] for s in self.samples),default=None),
                "peak_disk_io_mbps":max((s["disk_io_mbps"] for s in self.samples),default=None),
                "peak_disk_busy":max((s["disk_busy"] for s in self.samples),default=None),
                "peak_cpu_iowait":max((s["cpu_iowait"] for s in self.samples),default=None),
                "samples": list(self.samples)}

    def prometheus_text(self):
        """Low-cardinality worker metrics; no task IDs or high-volume samples."""
        latest = self.samples[-1] if self.samples else None
        reserved = self.reserved
        gauges = {
            "dsec_scheduler_active": len(self.active),
            "dsec_scheduler_pending": len(self.pending) + len(self.node_pending),
            "dsec_scheduler_reserved_cpu": reserved["cpu"],
            "dsec_scheduler_reserved_memory_mb": reserved["memory_mb"],
            "dsec_scheduler_reserved_disk_mb": reserved["disk_mb"],
            "dsec_scheduler_reserved_network_mbps": reserved["network_mbps"],
            "dsec_scheduler_reserved_api_episode_slots": reserved["api_episode_slots"],
            "dsec_scheduler_host_memory_available_mb": (
                latest["memory_available_mb"] if latest else 0),
            "dsec_scheduler_host_disk_available_mb": (
                latest["disk_available_mb"] if latest else 0),
            "dsec_scheduler_host_cpu_utilization": (
                latest["cpu_utilization"] if latest else 0),
            "dsec_scheduler_host_disk_busy": (latest["disk_busy"] if latest else 0),
            "dsec_scheduler_host_network_mbps": (
                latest["network_mbps"] if latest else 0),
            "dsec_scheduler_host_disk_io_mbps": (
                latest["disk_io_mbps"] if latest else 0),
            "dsec_scheduler_host_cpu_iowait": (
                latest["cpu_iowait"] if latest else 0),
            "dsec_scheduler_api_calls_total": self.api_quota.calls,
            "dsec_scheduler_api_tokens_total": self.api_quota.tokens,
            "dsec_scheduler_completed_total": self.completed_total,
            "dsec_scheduler_admitted_total": self.admitted_total,
            "dsec_scheduler_queue_wait_seconds_total": self.queue_wait_seconds_total,
            "dsec_scheduler_queue_wait_seconds_max": self.queue_wait_seconds_max,
            "dsec_scheduler_queue_wait_seconds_last": self.queue_wait_seconds_last,
        }
        lines = []
        for name, value in gauges.items():
            kind = "counter" if name.endswith("_total") else "gauge"
            lines.extend((f"# TYPE {name} {kind}", f"{name} {value}"))
        lines.append("# TYPE dsec_scheduler_blocked_seconds_total counter")
        for reason, seconds in sorted(self.blocked_seconds.items()):
            lines.append(f'dsec_scheduler_blocked_seconds_total{{reason="{reason}"}} {seconds}')
        lines.append("# TYPE dsec_scheduler_pending_reason gauge")
        pending_counts = Counter(reason for reasons in
                                 {**self.pending_reasons, **self.node_pending}.values()
                                 for reason in reasons)
        for reason, count in sorted(pending_counts.items()):
            lines.append(f'dsec_scheduler_pending_reason{{reason="{reason}"}} {count}')
        return "\n".join(lines) + "\n"
