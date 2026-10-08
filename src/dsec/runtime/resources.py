"""Node pressure sampling and the single physical sandbox reservation ledger.

Mutations must run under ``condition`` on one event loop. This is an in-process
ledger, not yet the Edge's durable node lease authority. Recovery must restore
possibly live reservations before new work is admitted.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
import shutil
import time
from types import MappingProxyType

from dsec.contracts.resources import NodeBudget, NodeDemand


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
        # Linux includes guest/guest_nice in user/nice already.
        return sum(values[:8]), idle, values[4]

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


class NodeResourceLedger:
    def __init__(self, budget: NodeBudget, sampler):
        if not isinstance(budget, NodeBudget):
            raise TypeError("Node ledger requires a NodeBudget")
        self.budget = budget
        self.sampler = sampler
        self.condition = asyncio.Condition()
        self._leases = {}
        self._reserved_snapshot = None

    @property
    def leases(self):
        return MappingProxyType(self._leases)

    @property
    def reserved(self):
        # Cache a derived snapshot for first-fit scans of the same lease set.
        # Mutations invalidate it; callers receive a copy, never ledger state.
        if self._reserved_snapshot is None:
            total = Counter(dict.fromkeys(NodeDemand.__dataclass_fields__, 0))
            for demand in self._leases.values():
                total.update(asdict(demand))
            self._reserved_snapshot = total
        return self._reserved_snapshot.copy()

    def too_large(self, demand: NodeDemand):
        self._validate(demand)
        b = self.budget
        return (any(getattr(demand, name) > getattr(b, name)
                    for name in ("cpu", "memory_mb", "disk_mb", "network_mbps")) or
                (b.disk_io_mbps > 0 and demand.disk_io_mbps > b.disk_io_mbps))

    def blockers(self, demand: NodeDemand, sample: HostSample):
        self._validate(demand)
        b, reserved = self.budget, self.reserved
        blockers = []
        for name in ("cpu", "memory_mb", "disk_mb", "network_mbps"):
            if reserved[name] + getattr(demand, name) > getattr(b, name):
                blockers.append(name + "_budget")
        if b.disk_io_mbps > 0 and reserved["disk_io_mbps"] + demand.disk_io_mbps > b.disk_io_mbps:
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
        return blockers

    @staticmethod
    def _validate(demand):
        if not isinstance(demand, NodeDemand):
            raise TypeError("Node ledger requires a NodeDemand")

    def reserve(self, lease_id, demand: NodeDemand, sample: HostSample):
        self._validate(demand)
        if lease_id in self._leases:
            raise ValueError("Duplicate node lease ID")
        blockers = self.blockers(demand, sample)
        if blockers:
            raise ValueError("Node resources unavailable: " + ", ".join(blockers))
        self._leases[lease_id] = demand
        self._reserved_snapshot = None

    def restore(self, lease_id, demand: NodeDemand):
        self._validate(demand)
        if lease_id in self._leases:
            raise ValueError("Duplicate node lease ID")
        # UNKNOWN may already consume resources beyond the current budget.
        # Rejecting recovery would incorrectly make those resources available.
        self._leases[lease_id] = demand
        self._reserved_snapshot = None

    def release(self, lease_id):
        demand = self._leases.pop(lease_id)
        self._reserved_snapshot = None
        return demand
