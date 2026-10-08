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
