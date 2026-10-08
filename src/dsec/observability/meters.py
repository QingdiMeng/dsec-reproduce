"""Read-only, per-sandbox Linux resource samples for TB2 episodes.

The Docker scope is the task container's cgroup.  The microVM scope is its
Firecracker process plus per-device ublk logical I/O; the shared ublk daemon
and host page cache are intentionally *not* attributed to a single VM.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import time


_CGROUP_ROOT = Path("/sys/fs/cgroup")
_PROC_ROOT = Path("/proc")
_BLOCK_ROOT = Path("/sys/class/block")


def _kv(path: Path) -> dict[str, int]:
    return {parts[0].rstrip(":"): int(parts[1])
            for line in path.read_text().splitlines()
            if len(parts := line.split()) >= 2 and parts[1].isdigit()}


def _process_identity(pid: int) -> tuple[int, int]:
    fields = (_PROC_ROOT / str(pid) / "stat").read_text().rsplit(") ", 1)[1].split()
    return pid, int(fields[19])  # /proc/stat field 22: starttime


def _process_sample(pid: int, start_ticks: int) -> dict:
    root = _PROC_ROOT / str(pid)
    fields = (root / "stat").read_text().rsplit(") ", 1)[1].split()
    if int(fields[19]) != start_ticks:
        raise ProcessLookupError("Sandbox PID changed or was reused")
    values = {"cpu_seconds": (int(fields[11]) + int(fields[12])) /
              os.sysconf("SC_CLK_TCK")}
    rollup = root / "smaps_rollup"
    try:
        memory = _kv(rollup)
    except (FileNotFoundError, PermissionError):
        memory = None
    if memory is None:
        values["rss_bytes"] = _kv(root / "status").get("VmRSS", 0) * 1024
    else:
        values.update(rss_bytes=memory.get("Rss", 0) * 1024,
                      pss_bytes=memory.get("Pss", 0) * 1024)
    try:
        io = _kv(root / "io")
    except (FileNotFoundError, PermissionError):
        io = None
    if io is not None:
        values.update(read_bytes=io.get("read_bytes", 0),
                      write_bytes=io.get("write_bytes", 0))
    return values


def _cgroup_for_pid(pid: int, container_id: str) -> Path:
    lines = (_PROC_ROOT / str(pid) / "cgroup").read_text().splitlines()
    entry = next((line.split("::", 1)[1] for line in lines
                  if line.startswith("0::")), None)
    if not entry or entry == "/" or ".." in Path(entry).parts:
        raise ValueError("A dedicated cgroup v2 is required for container metrics")
    if len(container_id) != 64 or container_id not in entry:
        raise ValueError("Container PID does not belong to its own Docker cgroup")
    root = _CGROUP_ROOT / entry.lstrip("/")
    if not (root / "memory.current").is_file():
        raise FileNotFoundError(f"Container cgroup is unavailable: {root}")
    return root


def _cgroup_sample(root: Path) -> dict:
    cpu = _kv(root / "cpu.stat")
    memory = _kv(root / "memory.stat")
    values = {"cpu_seconds": cpu["usage_usec"] / 1_000_000,
              "memory_current_bytes": int((root / "memory.current").read_text()),
              "memory_anon_bytes": memory.get("anon"),
              "memory_file_bytes": memory.get("file"),
              "memory_peak_bytes": int((root / "memory.peak").read_text())
              if (root / "memory.peak").exists() else None}
    io = {}
    if (root / "io.stat").exists():
        for line in (root / "io.stat").read_text().splitlines():
            device, *tokens = line.split()
            io[device] = {key: int(value) for token in tokens
                          if "=" in token for key, value in [token.split("=", 1)]
                          if value.isdigit()}
    values["io_by_device"] = io
    return values


def _ublk_sample(device_id: int) -> dict:
    values = (_BLOCK_ROOT / f"ublkb{device_id}" / "stat").read_text().split()
    return {"logical_read_bytes": int(values[2]) * 512,
            "logical_write_bytes": int(values[6]) * 512}


def _network_sample(pid: int, interface: str) -> dict:
    for line in (_PROC_ROOT / str(pid) / "net" / "dev").read_text().splitlines():
        name, separator, data = line.partition(":")
        if separator and name.strip() == interface:
            counters = data.split()
            return {"network_rx_bytes": int(counters[0]),
                    "network_tx_bytes": int(counters[8])}
    raise FileNotFoundError(f"Network interface {interface} is unavailable")


class SandboxResourceMeter:
    def __init__(self, backend: str, pid: int, *, ublk_device_id: int | None = None,
                 container_id: str | None = None, interval_seconds: float = 1.0):
        if backend not in ("docker-direct", "microvm") or pid <= 0:
            raise ValueError("A live Docker or microVM PID is required")
        self.backend = backend
        self.pid = pid
        self.ublk_device_id = ublk_device_id
        self.container_id = container_id
        self.network_interface = "eth0" if backend == "docker-direct" else "tap0"
        self.interval_seconds = interval_seconds
        self.scope = "container_cgroup_v2" if backend == "docker-direct" else "firecracker_process"
        self.start_ticks = None
        self.cgroup = None
        self.samples = []
        self.errors = []
        self.sample_errors = 0
        self.sampling_wall_seconds = 0.0
        self.task = None

    def _sample(self) -> None:
        started = time.perf_counter()
        try:
            values = (_cgroup_sample(self.cgroup) if self.cgroup is not None else
                      _process_sample(self.pid, self.start_ticks))
            if self.ublk_device_id is not None:
                try:
                    values.update(_ublk_sample(self.ublk_device_id))
                except (FileNotFoundError, PermissionError, ValueError) as exc:
                    if not any(error.startswith("ublk counter") for error in self.errors):
                        self.errors.append(f"ublk counter unavailable: {exc}")
            try:
                values.update(_network_sample(self.pid, self.network_interface))
            except (FileNotFoundError, PermissionError, ValueError) as exc:
                if not any(error.startswith("network counter") for error in self.errors):
                    self.errors.append(f"network counter unavailable: {exc}")
            values["monotonic_seconds"] = time.monotonic()
            self.samples.append(values)
        except (OSError, ProcessLookupError, ValueError, KeyError) as exc:
            self.sample_errors += 1
            if len(self.errors) < 5:
                self.errors.append(f"resource sample unavailable: {exc}")
        finally:
            self.sampling_wall_seconds += time.perf_counter() - started

    async def start(self) -> None:
        self.start_ticks = _process_identity(self.pid)[1]
        if self.backend == "docker-direct":
            self.cgroup = _cgroup_for_pid(self.pid, self.container_id or "")
        self._sample()
        self.task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            self._sample()

    async def stop(self) -> dict:
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        self._sample()
        first, last = (self.samples[0], self.samples[-1]) if self.samples else ({}, {})
        result = {"scope": self.scope, "sample_interval_seconds": self.interval_seconds,
                  "sample_count": len(self.samples), "errors": self.errors[:5],
                  "sample_errors": self.sample_errors,
                  "sampling_wall_seconds": round(self.sampling_wall_seconds, 6),
                  "coverage_seconds": round(last.get("monotonic_seconds", 0) -
                                            first.get("monotonic_seconds", 0), 3)
                  if self.samples else 0}
        if not self.samples:
            return result
        result["cpu_seconds"] = max(0, last["cpu_seconds"] - first["cpu_seconds"])
        for name in ("network_rx_bytes", "network_tx_bytes"):
            if name in first and name in last:
                result[name + "_delta"] = max(0, last[name] - first[name])
        if self.cgroup is not None:
            result["peak_sampled_memory_bytes"] = max(
                sample["memory_current_bytes"] for sample in self.samples)
            result["memory_peak_bytes"] = last.get("memory_peak_bytes")
            result["peak_sampled_anon_bytes"] = max(
                sample.get("memory_anon_bytes") or 0 for sample in self.samples)
            result["peak_sampled_file_bytes"] = max(
                sample.get("memory_file_bytes") or 0 for sample in self.samples)
            result["io_by_device_delta"] = {
                device: {name: max(0, count - first.get("io_by_device", {}).get(
                    device, {}).get(name, 0)) for name, count in counters.items()}
                for device, counters in last.get("io_by_device", {}).items()}
            result["excluded_shared_components"] = ["docker_daemon",
                                                     "verifier_fuse_process"]
        else:
            for name in ("rss_bytes", "pss_bytes"):
                values = [sample[name] for sample in self.samples if name in sample]
                if values:
                    result["peak_sampled_" + name] = max(values)
            for name in ("read_bytes", "write_bytes", "logical_read_bytes",
                         "logical_write_bytes"):
                if name in first and name in last:
                    result[name + "_delta"] = max(0, last[name] - first[name])
            result["excluded_shared_components"] = ["ublk_service_cpu_memory",
                                                     "host_page_cache"]
        return result
