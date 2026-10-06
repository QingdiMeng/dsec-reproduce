"""Single-host resource-aware work admission and API rate accounting.

The scheduler never replays an episode. It grants a lease for each job, samples
host pressure while leases run, and records the resource that delayed queued
work. This is a local dispatch layer; a later multi-node coordinator can use
the same demand/budget contract with worker-specific samplers.
"""
from __future__ import annotations

import asyncio
from collections import Counter, deque
import contextvars
from dataclasses import asdict, dataclass
import math
from pathlib import Path
import re
import shutil
import time


@dataclass(frozen=True)
class ResourceDemand:
    cpu: float
    memory_mb: int
    disk_mb: int
    network_mbps: float
    api_episode_slots: int = 1
    disk_io_mbps: float = 0

    def __post_init__(self):
        for name in ("cpu", "network_mbps", "disk_io_mbps"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("Invalid resource demand: " + name)
        for name in ("memory_mb", "disk_mb", "api_episode_slots"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("Invalid resource demand: " + name)
        if (self.cpu <= 0 or self.memory_mb <= 0 or self.disk_mb <= 0 or
                self.network_mbps < 0 or self.api_episode_slots < 0 or
                self.disk_io_mbps < 0):
            raise ValueError("Resource demand must be positive, except optional network/API")


@dataclass(frozen=True)
class ResourceBudget:
    cpu: float
    memory_mb: int
    disk_mb: int
    network_mbps: float
    api_episode_slots: int
    api_inflight: int
    api_rpm: int
    api_tpm: int
    min_memory_free_mb: int = 4096
    min_disk_free_mb: int = 20480
    max_cpu_utilization: float = 0.95
    api_token_reserve: int = 2000
    disk_io_mbps: float = 0
    max_disk_busy: float = 0.95

    def __post_init__(self):
        if (self.cpu <= 0 or self.memory_mb <= 0 or self.disk_mb <= 0 or
                self.network_mbps <= 0 or self.api_episode_slots <= 0 or
                self.api_inflight <= 0 or self.api_rpm <= 0 or self.api_tpm <= 0 or
                self.min_memory_free_mb < 0 or self.min_disk_free_mb < 0 or
                not 0 < self.max_cpu_utilization <= 1 or
                not 0 < self.api_token_reserve <= self.api_tpm or
                self.disk_io_mbps < 0 or not 0 < self.max_disk_busy <= 1):
            raise ValueError("Invalid resource budget")


@dataclass(frozen=True)
class HostSample:
    timestamp: float
    memory_available_mb: int
    disk_available_mb: int
    cpu_utilization: float
    network_mbps: float
    disk_io_mbps: float = 0
    disk_busy: float = 0
    cpu_iowait: float = 0


class ProcHostSampler:
    def __init__(self, disk_path, interface, disk_device=None):
        self.disk_path = Path(disk_path)
        self.interface = interface
        self.disk_device = disk_device
        self.previous = None

    def _cpu(self):
        fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        values = [int(value) for value in fields]
        idle = values[3] + values[4]
        return sum(values), idle, values[4]

    def _disk(self):
        if self.disk_device is None:
            return 0, 0
        fields = (Path("/sys/class/block") / self.disk_device / "stat").read_text().split()
        sectors = int(fields[2]) + int(fields[6])
        io_ms = int(fields[9])
        return sectors, io_ms

    def _network(self):
        for line in Path("/proc/net/dev").read_text().splitlines():
            name, sep, values = line.partition(":")
            if sep and name.strip() == self.interface:
                columns = values.split()
                return int(columns[0]) + int(columns[8])
        raise ValueError(f"Network interface not found: {self.interface}")

    def sample(self):
        now = time.monotonic()
        memory = next(int(line.split()[1]) // 1024 for line in
                      Path("/proc/meminfo").read_text().splitlines()
                      if line.startswith("MemAvailable:"))
        disk = shutil.disk_usage(self.disk_path).free // (1024 * 1024)
        cpu_total, cpu_idle, cpu_wait = self._cpu()
        bytes_total = self._network()
        disk_sectors, disk_ms = self._disk()
        cpu_use = network_mbps = disk_io_mbps = disk_busy = cpu_iowait = 0.0
        if self.previous is not None:
            prev_time, prev_total, prev_idle, prev_wait, prev_bytes, prev_sectors, prev_io_ms = self.previous
            elapsed = max(now - prev_time, 0.001)
            cpu_delta = cpu_total - prev_total
            if cpu_delta > 0:
                cpu_use = max(0.0, min(1.0, 1 - (cpu_idle - prev_idle) / cpu_delta))
                cpu_iowait = max(0.0, min(1.0, (cpu_wait - prev_wait) / cpu_delta))
            network_mbps = max(0.0, (bytes_total - prev_bytes) * 8 / elapsed / 1_000_000)
            disk_io_mbps = max(0.0, (disk_sectors - prev_sectors) * 512 / elapsed / 1_000_000)
            disk_busy = max(0.0, min(1.0, (disk_ms - prev_io_ms) / (elapsed * 1000)))
        self.previous = (now, cpu_total, cpu_idle, cpu_wait, bytes_total,
                         disk_sectors, disk_ms)
        return HostSample(now, memory, disk, cpu_use, network_mbps,
                          disk_io_mbps, disk_busy, cpu_iowait)


_current_job = contextvars.ContextVar("dsec_scheduler_job", default=None)


class WorkScheduler:
    def __init__(self, budget: ResourceBudget, sampler, *, sample_interval=1.0,
                 dependency_ready=None):
        self.budget = budget
        self.sampler = sampler
        self.sample_interval = sample_interval
        self.condition = asyncio.Condition()
        self.api_condition = asyncio.Condition()
        self.api_semaphore = asyncio.Semaphore(budget.api_inflight)
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
        self.reserved = Counter()
        self.blocked_seconds = Counter()
        self.pressure_seconds = Counter()
        self.api_window = deque()
        self.api_calls = 0
        self.api_tokens = 0
        self.api_inflight = 0
        self.api_errors = 0
        self.api_429 = 0
        self.api_pause_until = 0.0
        self.samples = deque(maxlen=3600)
        self.monitor_task = None

    def _demand_too_large(self, demand):
        return (demand.cpu > self.budget.cpu or
                demand.memory_mb > self.budget.memory_mb or
                demand.disk_mb > self.budget.disk_mb or
                demand.network_mbps > self.budget.network_mbps or
                demand.api_episode_slots > self.budget.api_episode_slots or
                (self.budget.disk_io_mbps > 0 and
                 demand.disk_io_mbps > self.budget.disk_io_mbps))

    def _blockers(self, demand, sample, requirements=()):
        b = self.budget
        blockers = []
        for name in ("cpu", "memory_mb", "disk_mb", "network_mbps", "api_episode_slots"):
            if self.reserved[name] + getattr(demand, name) > getattr(b, name):
                blockers.append(name + "_budget")
        if b.disk_io_mbps > 0 and self.reserved["disk_io_mbps"] + demand.disk_io_mbps > b.disk_io_mbps:
            blockers.append("disk_io_budget")
        if sample.memory_available_mb - demand.memory_mb < b.min_memory_free_mb:
            blockers.append("memory_pressure")
        if sample.disk_available_mb - demand.disk_mb < b.min_disk_free_mb:
            blockers.append("disk_pressure")
        if sample.cpu_utilization >= b.max_cpu_utilization:
            blockers.append("cpu_saturation")
        if sample.network_mbps + demand.network_mbps > b.network_mbps:
            blockers.append("network_saturation")
        if sample.disk_busy >= b.max_disk_busy:
            blockers.append("disk_busy")
        if b.disk_io_mbps > 0 and sample.disk_io_mbps + demand.disk_io_mbps > b.disk_io_mbps:
            blockers.append("disk_io_saturation")
        for component in requirements:
            if not self.dependency_ready(component):
                blockers.append("dependency_" + component + "_unavailable")
        return blockers

    def _reserve(self, job_id, demand, wait_started):
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
        for name, value in asdict(demand).items():
            self.reserved[name] += value

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
                    sample = self.sampler.sample()
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
                            self._reserve(job_id, demand, waited_at)
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
        """Conservatively account for a possibly live sandbox after worker restart."""
        if job_id in self.pending or job_id in self.active or job_id in self.completed:
            raise ValueError("Duplicate scheduler job ID")
        self.active[job_id] = {"demand": demand, "started": time.monotonic(),
                               "wait_seconds": None, "api_calls": 0, "api_tokens": 0,
                               "recovered": True}
        for name, value in asdict(demand).items():
            self.reserved[name] += value

    async def release(self, job_id):
        async with self.condition:
            detail = self.active.pop(job_id)
            detail["duration_seconds"] = time.monotonic() - detail["started"]
            detail["demand"] = asdict(detail["demand"])
            self.completed[job_id] = detail
            self.completed_total += 1
            if len(self.completed) > 4096:
                self.completed.pop(next(iter(self.completed)))
            for name, value in detail["demand"].items():
                self.reserved[name] -= value
            self.condition.notify_all()

    async def run(self, job_id, demand: ResourceDemand, work):
        await self.acquire(job_id, demand)
        token = _current_job.set(job_id)
        try:
            return await work()
        finally:
            _current_job.reset(token)
            await self.release(job_id)

    def _prune_api_window(self, now):
        while self.api_window and now - self.api_window[0]["time"] >= 60:
            self.api_window.popleft()

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

    async def policy_call(self, policy, model, messages, request_kwargs):
        job_id = _current_job.get()
        async with self.api_condition:
            while True:
                now = time.monotonic()
                self._prune_api_window(now)
                reserve = max(self.budget.api_token_reserve,
                              max((item["tokens"] for item in self.api_window), default=0))
                tokens = sum(item["tokens"] for item in self.api_window)
                if (now >= self.api_pause_until and
                        len(self.api_window) < self.budget.api_rpm and
                        tokens + reserve <= self.budget.api_tpm):
                    entry = {"time": now, "tokens": reserve}
                    self.api_window.append(entry)
                    break
                self.blocked_seconds["api_rate"] += self.sample_interval
                try:
                    await asyncio.wait_for(self.api_condition.wait(), self.sample_interval)
                except asyncio.TimeoutError:
                    pass
        inflight_wait_started = time.monotonic()
        async with self.api_semaphore:
            self.blocked_seconds["api_inflight"] += max(
                0.0, time.monotonic() - inflight_wait_started)
            self.api_inflight += 1
            try:
                completion = await policy.chat.completions.create(
                    model=model, messages=messages, extra_body=request_kwargs)
                usage = getattr(completion, "usage", None)
                actual = getattr(usage, "total_tokens", None)
                if isinstance(actual, int) and actual >= 0:
                    entry["tokens"] = actual
                self.api_calls += 1
                self.api_tokens += entry["tokens"]
                if job_id in self.active:
                    self.active[job_id]["api_calls"] += 1
                    self.active[job_id]["api_tokens"] += entry["tokens"]
                return completion
            except Exception as exc:
                self.api_errors += 1
                if getattr(exc, "status_code", None) == 429:
                    self.api_429 += 1
                    headers = getattr(getattr(exc, "response", None), "headers", {})
                    try:
                        retry_after = float(headers.get("retry-after", 30))
                    except (TypeError, ValueError):
                        retry_after = 30
                    self.api_pause_until = max(self.api_pause_until,
                                               time.monotonic() + max(1, min(120, retry_after)))
                raise
            finally:
                self.api_inflight -= 1
                async with self.api_condition:
                    self.api_condition.notify_all()

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
                "active": list(self.active),
                "pending": list(self.pending), "pending_reasons": dict(self.pending_reasons),
                "completed": dict(self.completed), "completed_total": self.completed_total,
                "admitted_total": self.admitted_total,
                "queue_wait_seconds_total": self.queue_wait_seconds_total,
                "queue_wait_seconds_max": self.queue_wait_seconds_max,
                "queue_wait_seconds_last": self.queue_wait_seconds_last,
                "blocked_seconds": blocked, "pressure_seconds": pressure,
                "primary_constraints": primary,
                "likely_bottleneck": (primary[0] if len(primary) == 1 else
                                      "multiple_constraints" if primary else None),
                "api_calls": self.api_calls, "api_tokens": self.api_tokens,
                "api_errors":self.api_errors,"api_429":self.api_429,
                "api_cooldown_remaining_seconds":max(0.0,self.api_pause_until-time.monotonic()),
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
        gauges = {
            "dsec_scheduler_active": len(self.active),
            "dsec_scheduler_pending": len(self.pending),
            "dsec_scheduler_reserved_cpu": self.reserved["cpu"],
            "dsec_scheduler_reserved_memory_mb": self.reserved["memory_mb"],
            "dsec_scheduler_reserved_disk_mb": self.reserved["disk_mb"],
            "dsec_scheduler_reserved_network_mbps": self.reserved["network_mbps"],
            "dsec_scheduler_reserved_api_episode_slots": self.reserved["api_episode_slots"],
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
            "dsec_scheduler_api_calls_total": self.api_calls,
            "dsec_scheduler_api_tokens_total": self.api_tokens,
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
        pending_counts = Counter(reason for reasons in self.pending_reasons.values()
                                 for reason in reasons)
        for reason, count in sorted(pending_counts.items()):
            lines.append(f'dsec_scheduler_pending_reason{{reason="{reason}"}} {count}')
        return "\n".join(lines) + "\n"
