"""Existing v0.1 resource schemas, independent of host sampling and dispatch."""
from __future__ import annotations

from dataclasses import dataclass
import math


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
class NodeDemand:
    """Physical sandbox reservation; contains no episode or model/API quota."""
    cpu: float
    memory_mb: int
    disk_mb: int
    network_mbps: float
    disk_io_mbps: float = 0

    def __post_init__(self):
        ResourceDemand(self.cpu, self.memory_mb, self.disk_mb,
                       self.network_mbps, 0, self.disk_io_mbps)

    @classmethod
    def from_resource(cls, demand: ResourceDemand):
        return cls(demand.cpu, demand.memory_mb, demand.disk_mb,
                   demand.network_mbps, demand.disk_io_mbps)


@dataclass(frozen=True)
class NodeBudget:
    """Node capacity and observed-pressure floors, independent of job quotas."""
    cpu: float
    memory_mb: int
    disk_mb: int
    network_mbps: float
    min_memory_free_mb: int = 4096
    min_disk_free_mb: int = 20480
    max_cpu_utilization: float = 0.95
    disk_io_mbps: float = 0
    max_disk_busy: float = 0.95

    def __post_init__(self):
        NodeDemand(self.cpu, self.memory_mb, self.disk_mb,
                   self.network_mbps, self.disk_io_mbps)
        if (self.network_mbps <= 0 or self.min_memory_free_mb < 0 or
                self.min_disk_free_mb < 0 or not 0 < self.max_cpu_utilization <= 1 or
                not 0 < self.max_disk_busy <= 1):
            raise ValueError("Invalid node budget")

    @classmethod
    def from_resource(cls, budget: ResourceBudget):
        return cls(**{name: getattr(budget, name) for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class APILimits:
    """Limits for external policy calls; DSec does not own model memory."""
    inflight: int
    rpm: int
    tpm: int
    token_reserve: int

    def __post_init__(self):
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
               for value in (self.inflight, self.rpm, self.tpm, self.token_reserve)):
            raise ValueError("Invalid API limits")
        if self.token_reserve > self.tpm:
            raise ValueError("API token reserve exceeds TPM")

    @classmethod
    def from_resource(cls, budget: ResourceBudget):
        return cls(budget.api_inflight, budget.api_rpm, budget.api_tpm,
                   budget.api_token_reserve)
