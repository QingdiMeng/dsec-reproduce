"""Bounded accounting for explicitly configured shared Docker services.

These are whole-service counters. They must not be added to one sandbox or
interpreted as its marginal cost when other workloads use the same service.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time

from dsec.runtime.backends.docker_broker import DockerBrokerError, run_docker

from dsec.observability.meters import _cgroup_for_pid, _cgroup_sample


_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
_CONTAINER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")


def _docker_identity(name):
    result = run_docker(["docker", "inspect", name], timeout=5)
    value = json.loads(result.stdout)[0]
    pid = value["State"].get("Pid")
    cid = value.get("Id")
    if (value.get("Name") != "/" + name or not value["State"].get("Running") or
            not isinstance(pid, int) or pid <= 0 or
            not isinstance(cid, str) or not re.fullmatch(r"[0-9a-f]{64}", cid)):
        raise ValueError("Shared service container identity mismatch")
    return pid, cid


class SharedServiceMonitor:
    def __init__(self, services, interval_seconds=5.0):
        if not isinstance(services, list) or interval_seconds <= 0:
            raise ValueError("Invalid shared service monitor configuration")
        self.services = {}
        for item in services:
            if not isinstance(item, dict) or set(item) != {"name", "container"}:
                raise ValueError("Shared service requires name and container")
            name, container = item["name"], item["container"]
            if (not isinstance(name, str) or not _NAME.fullmatch(name) or
                    not isinstance(container, str) or not _CONTAINER.fullmatch(container) or
                    name in self.services):
                raise ValueError("Invalid or duplicate shared service identity")
            self.services[name] = {"container": container, "up": False,
                                   "latest": None, "container_id": None,
                                   "samples": 0, "error": None,
                                   "last_success_monotonic": None,
                                   "sampling_wall_seconds": 0.0}
        self.interval_seconds = interval_seconds
        self.task = None

    @staticmethod
    def _sample(container):
        pid, cid = _docker_identity(container)
        group = _cgroup_for_pid(pid, cid)
        sample = _cgroup_sample(group)
        io = sample.pop("io_by_device", {})
        sample["read_bytes"] = sum(row.get("rbytes", 0) for row in io.values())
        sample["write_bytes"] = sum(row.get("wbytes", 0) for row in io.values())
        return cid, sample

    async def sample_once(self):
        for row in self.services.values():
            started = time.perf_counter()
            try:
                cid, sample = await asyncio.to_thread(self._sample, row["container"])
                row.update(up=True, latest=sample, container_id=cid, error=None,
                           samples=row["samples"] + 1,
                           last_success_monotonic=time.monotonic())
            except (OSError, ValueError, KeyError, subprocess.SubprocessError,
                    DockerBrokerError,
                    json.JSONDecodeError) as exc:
                row.update(up=False, latest=None,
                           error=f"{type(exc).__name__}: {exc}"[:180])
            finally:
                row["sampling_wall_seconds"] += time.perf_counter() - started

    async def start(self):
        await self.sample_once()
        self.task = asyncio.create_task(self._loop())

    async def _loop(self):
        while True:
            await asyncio.sleep(self.interval_seconds)
            await self.sample_once()

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    def snapshot(self):
        return {name: {**row, "up": self.ready(name),
                       "sample_age_seconds": (round(time.monotonic() - row["last_success_monotonic"], 3)
                                              if row["last_success_monotonic"] is not None else None),
                       "latest": dict(row["latest"] or {})}
                for name, row in self.services.items()}

    def ready(self, name):
        row = self.services.get(name)
        return bool(row and row["up"] and row["latest"] is not None and
                    row["error"] is None and row["last_success_monotonic"] is not None and
                    time.monotonic() - row["last_success_monotonic"] <=
                    max(3 * self.interval_seconds, 15.0))

    def prometheus_text(self):
        lines = ["# TYPE dsec_shared_service_up gauge",
                 "# TYPE dsec_shared_service_memory_bytes gauge",
                 "# TYPE dsec_shared_service_cpu_seconds_total counter",
                 "# TYPE dsec_shared_service_read_bytes_total counter",
                 "# TYPE dsec_shared_service_write_bytes_total counter"]
        for name, row in self.services.items():
            label = f'component="{name}"'
            ready = self.ready(name)
            lines.append(f'dsec_shared_service_up{{{label}}} {int(ready)}')
            if ready:
                sample = row["latest"]
                for metric, key in (("memory_bytes", "memory_current_bytes"),
                                    ("cpu_seconds_total", "cpu_seconds"),
                                    ("read_bytes_total", "read_bytes"),
                                    ("write_bytes_total", "write_bytes")):
                    lines.append(f'dsec_shared_service_{metric}{{{label}}} {sample.get(key, 0)}')
        return "\n".join(lines) + "\n"
