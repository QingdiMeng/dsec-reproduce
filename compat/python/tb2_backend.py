"""Per-episode Terminal-Bench-2 container owned by the local DSec facade.

The image is built separately from the official task image plus the pinned
OpenEnv server. This module only starts, attests and destroys that image; it
never grants the evaluator access to the Docker socket inside the sandbox.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.request
import uuid


_TASK = re.compile(r"[a-z0-9][a-z0-9.-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")
_ID = re.compile(r"[a-f0-9]{32}\Z")


def _docker(*args, timeout=30, check=True):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr[-1000:]}")
    return result


class TB2ContainerBackend:
    """One task-specific immutable image, one fresh writable container per episode."""

    docker_label = "tb2-openenv"
    container_prefix = "dsec-tb2-"

    def __init__(self, task_id: str, image: str):
        if not isinstance(task_id, str) or not _TASK.fullmatch(task_id):
            raise ValueError("Invalid TB2 task ID")
        if not isinstance(image, str) or not _DIGEST.fullmatch(image):
            raise ValueError("TB2 image must be pinned to a local sha256 image ID")
        self.task_id = task_id
        self.image = image
        _docker("image", "inspect", image)

    def create(self, *, memory_mb=2048, cpus=1.0, sandbox_id=None):
        if not isinstance(memory_mb, int) or isinstance(memory_mb, bool) or memory_mb < 512:
            raise ValueError("memory_mb must be at least 512")
        if not isinstance(cpus, (int, float)) or isinstance(cpus, bool) or cpus <= 0:
            raise ValueError("cpus must be positive")
        if sandbox_id is not None and not _ID.fullmatch(sandbox_id):
            raise ValueError("Invalid sandbox ID")
        sid = sandbox_id or uuid.uuid4().hex
        name = self.container_prefix + sid
        _docker("run", "-d", "--name", name,
                "--label", "dsec.backend=" + self.docker_label,
                "--label", "dsec.sandbox_id=" + sid,
                "--label", "dsec.task_id=" + self.task_id,
                "--memory", f"{memory_mb}m", "--memory-swap", f"{memory_mb}m",
                "--cpus", str(cpus), "--pids-limit", "512",
                "-p", "127.0.0.1::8000", self.image)
        sandbox = TB2Container(self, sid)
        try:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                status = sandbox.status()
                if status["state"] != "RUNNING":
                    raise RuntimeError("TB2 environment server exited during startup")
                try:
                    with urllib.request.urlopen(status["base_url"] + "/health", timeout=2) as reply:
                        if reply.status == 200:
                            return sandbox
                except (OSError, TimeoutError):
                    pass
                time.sleep(.05)
            raise TimeoutError("TB2 environment server did not become healthy")
        except BaseException:
            sandbox.stop()
            raise

    def attach(self, sid):
        sandbox = TB2Container(self, sid)
        if sandbox.status()["state"] != "RUNNING":
            raise RuntimeError("TB2 container is not running")
        return sandbox

    def prove_running(self, sid):
        try:
            return self.attach(sid).status()["state"] == "RUNNING"
        except (RuntimeError, ValueError, subprocess.TimeoutExpired):
            return False

    def prove_stopped(self, sid):
        if not isinstance(sid, str) or not _ID.fullmatch(sid):
            return False
        result = _docker("container", "ls", "-a", "--format", "{{.Names}}", check=False)
        return result.returncode == 0 and self.container_prefix + sid not in result.stdout.splitlines()


class TB2Container:
    backend = "tb2"

    def __init__(self, manager: TB2ContainerBackend, sid: str):
        if not isinstance(sid, str) or not _ID.fullmatch(sid):
            raise ValueError("Invalid sandbox ID")
        self.manager = manager
        self.id = sid
        self.name = manager.container_prefix + sid

    def status(self):
        result = _docker("inspect", self.name, check=False)
        if result.returncode:
            if not self.manager.prove_stopped(self.id):
                raise RuntimeError('Cannot prove TB2 container absence')
            return {"id": self.id, "state": "STOPPED", "backend": "tb2"}
        value = json.loads(result.stdout)[0]
        labels = value["Config"].get("Labels") or {}
        if (value["Name"] != "/" + self.name
                or labels.get("dsec.backend") != self.manager.docker_label
                or labels.get("dsec.sandbox_id") != self.id
                or labels.get("dsec.task_id") != self.manager.task_id
                or value["Image"] != self.manager.image):
            raise RuntimeError("TB2 container attestation failed")
        running = value["State"]["Running"]
        ports = value["NetworkSettings"]["Ports"].get("8000/tcp") if running else None
        if running and (not ports or ports[0]["HostIp"] != "127.0.0.1"):
            raise RuntimeError("TB2 environment endpoint is not loopback-bound")
        return {"id": self.id, "state": "RUNNING" if running else "STOPPED",
                "backend": "tb2", "task_id": self.manager.task_id,
                "image": self.manager.image,
                "base_url": "http://127.0.0.1:" + ports[0]["HostPort"] if running else None}

    def stop(self):
        _docker("stop", "--timeout", "5", self.name, timeout=12, check=False)
        _docker("rm", "-f", self.name, check=False)
        if not self.manager.prove_stopped(self.id):
            raise RuntimeError("TB2 container absence has not been proven after stop")
        return self.status()
