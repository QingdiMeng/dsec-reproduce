"""TB2 task plugin for the framework-independent DSec agent environment."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex

from dsec.rollout.environment import (EnvironmentAction, EnvironmentObservation,
                               EnvironmentSpec, EnvironmentVerdict)
from dsec.contracts.profiles import FrameworkProfile
from .tb2_task_runtime import task_workdir


TASK_ID = re.compile(r"[a-z0-9][a-z0-9.-]{0,127}\Z")


class TB2DSecEnvironment:
    def __init__(self, tasks_dir: Path, *, resources: dict | None = None,
                 environment_catalog: Path | None = None):
        self.tasks_dir = Path(tasks_dir).resolve()
        self.environment_catalog = environment_catalog
        self.resources_explicit = resources is not None
        self.resources = resources or {"cpu": 1.0, "memory_mb": 512,
                                       "disk_mb": 1024, "network_mbps": 1.0,
                                       "api_episode_slots": 1}

    @classmethod
    def from_environment(cls) -> "TB2DSecEnvironment":
        supplied = os.getenv("DSEC_TB2_RESOURCE_DEMAND")
        resources = json.loads(supplied) if supplied else None
        if resources is not None and not isinstance(resources, dict):
            raise ValueError("DSEC_TB2_RESOURCE_DEMAND must be a JSON object")
        catalog = os.getenv("DSEC_TB2_ENVIRONMENT_CATALOG")
        tasks = os.getenv("DSEC_TB2_TASKS_DIR") or os.getenv("OPENENV_TB2_TASKS_DIR")
        if not tasks:
            raise ValueError("DSEC_TB2_TASKS_DIR is required for the TB2 task adapter")
        return cls(Path(tasks), resources=resources,
                   environment_catalog=Path(catalog) if catalog else None)

    def prepare(self, task_id: str) -> EnvironmentSpec:
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            raise ValueError("Invalid TB2 task ID")
        task_dir = (self.tasks_dir / task_id).resolve()
        if task_dir.parent != self.tasks_dir or not (
                task_dir / "task.toml").is_file() or not (
                task_dir / "tests" / "test.sh").is_file() or not (
                task_dir / "instruction.md").is_file():
            raise ValueError("Pinned TB2 task directory is unavailable")
        profile = FrameworkProfile(
            backend="microvm", environment="erofs_layers",
            environment_id="tb2-" + task_id, storage="local",
            memory="baseline", lifecycle="full_snapshot_stop",
            verifier_storage="local")
        resources = dict(self.resources)
        if self.environment_catalog is not None:
            catalog = json.loads(self.environment_catalog.read_text())
            entry = catalog["environments"][profile.environment_id]
            if entry.get("backend") != "microvm" or entry.get("rootfs") != "erofs_layers":
                raise ValueError("TB2 environment is not registered as an EROFS microVM")
            if not self.resources_explicit:
                resources.update(cpu=float(entry["cpus"]), memory_mb=entry["memory_mb"])
        return EnvironmentSpec(
            task_id=task_id, instruction=(task_dir / "instruction.md").read_text(),
            profile=profile, resources=resources,
            options={"workdir": task_workdir(task_dir)})

    async def step(self, sandbox, spec: EnvironmentSpec,
                   action: EnvironmentAction,
                   policy_message: dict) -> EnvironmentObservation:
        if action.kind != "shell" or not isinstance(action.payload.get("command"), str):
            raise ValueError("TB2 expects a shell action")
        command = ("cd " + shlex.quote(spec.options["workdir"]) + " && " +
                   action.payload["command"])
        response = await sandbox.agent_step(
            command, step_id=action.step_id, action_id=action.action_id,
            assistant_message=policy_message, timeout_ms=action.timeout_ms,
            output_limit=action.output_limit)
        return EnvironmentObservation(
            step_id=action.step_id, action_id=action.action_id,
            result=response["entry"]["result"], dialogue=response["dialogue"])

    async def evaluate(self, sandbox, spec: EnvironmentSpec) -> EnvironmentVerdict:
        record = await sandbox.tb2_evaluate()
        if (record.get("harness") != "tests/test.sh" or
                record.get("task_id") != spec.task_id or
                record.get("value") not in (0.0, 1.0)):
            raise RuntimeError("DSec worker returned no official TB2 verdict")
        return EnvironmentVerdict(
            score=float(record["value"]), evaluator="tests/test.sh",
            details=dict(record))
