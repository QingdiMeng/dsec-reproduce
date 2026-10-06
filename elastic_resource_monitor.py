"""Bounded live accounting for long-lived DSec rollout sandboxes.

Container metrics are from its dedicated cgroup v2. MicroVM metrics are from
the Firecracker process (plus optional ublk logical counters). Neither scope
silently includes shared 3FS/FUSE/OverlayBD services or host page cache.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

from sandbox_client import ServiceError
from docker_broker import run_docker
from sandbox_resource_meter import (
    _cgroup_for_pid, _cgroup_sample, _process_identity, _process_sample,
    _ublk_sample,
)


def _container_identity(name: str, sandbox_id: str) -> tuple[int, str]:
    if not re.fullmatch(r"dsec-[a-z0-9-]+-" + re.escape(sandbox_id), name):
        raise ValueError("Container name does not match sandbox ID")
    result = run_docker(["docker", "inspect", name], timeout=5)
    value = json.loads(result.stdout)[0]
    labels = value["Config"].get("Labels") or {}
    pid = value["State"].get("Pid")
    container_id = value.get("Id")
    if (value.get("Name") != "/" + name or not value["State"].get("Running") or
            labels.get("dsec.sandbox_id") != sandbox_id or
            not isinstance(pid, int) or pid <= 0 or
            not isinstance(container_id, str) or not re.fullmatch(r"[0-9a-f]{64}", container_id)):
        raise ValueError("Container identity attestation failed")
    return pid, container_id


class ElasticResourceMonitor:
    def __init__(self, *, backend: str, pid: int, container_id: str | None = None,
                 ublk_device_id: int | None = None, interval_seconds: float = 2.0,
                 sample_reader=None):
        if backend not in ("container", "microvm") or pid <= 0 or interval_seconds <= 0:
            raise ValueError("Invalid live resource monitor")
        self.backend = backend
        self.scope = "container_cgroup_v2" if backend == "container" else "firecracker_process"
        self.pid = pid
        self.container_id = container_id
        self.ublk_device_id = ublk_device_id
        self.sample_reader = sample_reader
        self.interval_seconds = interval_seconds
        self.start_ticks = None
        self.cgroup = None
        self.task = None
        self.latest = None
        self.peaks = {}
        self.samples = 0
        self.errors = []
        self.started = time.monotonic()
        self.sampling_wall_seconds = 0.0
        self.expect_exit = False

    def mark_stopping(self, expected: bool = True):
        """Treat source removal as normal only during a confirmed lifecycle action."""
        self.expect_exit = expected

    @classmethod
    async def for_sandbox(cls, sandbox, *, interval_seconds=2.0):
        if sandbox.backend == "container":
            name = sandbox._container.name
            pid, container_id = await asyncio.to_thread(_container_identity,
                                                        name, sandbox.id)
            monitor = cls(backend="container", pid=pid, container_id=container_id,
                          interval_seconds=interval_seconds)
        elif sandbox.backend == "microvm":
            status = await sandbox.status()
            if status.get("state") != "RUNNING" or not isinstance(status.get("pid"), int):
                raise ValueError("A running microVM PID is required for accounting")
            monitor = cls(backend="microvm", pid=status["pid"],
                          ublk_device_id=status.get("overlaybd_device_id"),
                          interval_seconds=interval_seconds,
                          sample_reader=lambda: sandbox._transport.call(
                              "resource_sample", sandbox.id))
        else:
            raise ValueError("Unsupported metering backend")
        await monitor.start()
        return monitor

    def _read(self):
        started = time.perf_counter()
        try:
            if self.cgroup is not None:
                sample = _cgroup_sample(self.cgroup)
                devices = sample.pop("io_by_device", {})
                sample["read_bytes"] = sum(item.get("rbytes", 0) for item in devices.values())
                sample["write_bytes"] = sum(item.get("wbytes", 0) for item in devices.values())
            else:
                if self.sample_reader is not None:
                    response = self.sample_reader()
                    if response.get("pid") != self.pid or not isinstance(response.get("sample"), dict):
                        raise ValueError("Sandbox resource identity changed")
                    sample = response["sample"]
                else:
                    sample = _process_sample(self.pid, self.start_ticks)
                if self.ublk_device_id is not None:
                    try:
                        sample.update(_ublk_sample(self.ublk_device_id))
                    except (OSError, ValueError) as exc:
                        if not any(error.startswith("ublk:") for error in self.errors):
                            self.errors.append("ublk: " + str(exc)[:160])
            self.latest = sample
            self.samples += 1
            for name in ("memory_current_bytes", "memory_peak_bytes",
                         "memory_anon_bytes", "memory_file_bytes",
                         "rss_bytes", "pss_bytes"):
                value = sample.get(name)
                if isinstance(value, int):
                    self.peaks[name] = max(self.peaks.get(name, 0), value)
        except (OSError, ProcessLookupError, ValueError, KeyError, ServiceError) as exc:
            expected_service_exit = (
                isinstance(exc, ServiceError) and
                (exc.kind in ("FileNotFoundError", "ProcessLookupError") or
                 (exc.kind == "SandboxError" and
                  str(exc) == "No running sandbox to meter")))
            if self.expect_exit and (isinstance(exc, (FileNotFoundError, ProcessLookupError))
                                     or expected_service_exit):
                return
            if len(self.errors) < 5:
                self.errors.append(f"sample: {type(exc).__name__}: {exc}"[:200])
        finally:
            self.sampling_wall_seconds += time.perf_counter() - started

    async def start(self):
        self.start_ticks = await asyncio.to_thread(lambda: _process_identity(self.pid)[1])
        if self.backend == "container":
            self.cgroup = await asyncio.to_thread(_cgroup_for_pid, self.pid,
                                                  self.container_id or "")
        await asyncio.to_thread(self._read)
        if self.latest is None:
            raise RuntimeError("Initial sandbox resource sample failed")
        self.task = asyncio.create_task(self._loop())

    async def _loop(self):
        while True:
            await asyncio.sleep(self.interval_seconds)
            await asyncio.to_thread(self._read)

    def snapshot(self):
        result = {"scope": self.scope, "pid": self.pid,
                  "sample_interval_seconds": self.interval_seconds,
                  "sample_count": self.samples,
                  "coverage_seconds": round(time.monotonic() - self.started, 3),
                  "sampling_wall_seconds": round(self.sampling_wall_seconds, 6),
                  "errors": list(self.errors),
                  "latest": dict(self.latest or {}), "peaks": dict(self.peaks),
                  "excluded_shared_components": (
                      ["docker_daemon", "3fs_fuse", "host_page_cache"]
                      if self.backend == "container" else
                      ["sandboxd", "overlaybd_ublk_service", "3fs_fuse", "host_page_cache"])}
        return result

    async def finish(self):
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        await asyncio.to_thread(self._read)
        return self.snapshot()
