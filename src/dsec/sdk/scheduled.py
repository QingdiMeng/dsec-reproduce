"""Task-independent, scheduler-backed DSec sandbox client for RL rollouts.

The caller owns stable rollout and action IDs.  Transport failures never retry
side effects; the caller can attach to the durable worker record and reconcile.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import re
import time
import uuid

from dsec.contracts.profiles import FrameworkProfile
from dsec.sdk.rollout_transport import RolloutClient, RolloutOutcomeUnknown
from dsec.contracts.resources import ResourceDemand


ROLLOUT_ID = re.compile(r"[0-9a-f]{32}\Z")


class ScheduledOutcomeUnknown(RolloutOutcomeUnknown):
    def __init__(self, rollout_id: str, operation: str):
        super().__init__(f"{operation} outcome is unknown for rollout {rollout_id}; attach and reconcile")
        self.rollout_id = rollout_id
        self.operation = operation


class ScheduledSandbox:
    def __init__(self, transport: RolloutClient, view: dict):
        self._transport = transport
        self._view = view
        self.id = view["rollout_id"]

    @property
    def sandbox_id(self):
        return self._view.get("sandbox_id")

    @property
    def state(self):
        return self._view["state"]

    @property
    def next_step(self):
        return self._view["next_step"]

    async def _call(self, operation: str, **args):
        try:
            return await asyncio.to_thread(self._transport.call, operation,
                                           rollout_id=self.id, **args)
        except RolloutOutcomeUnknown as exc:
            raise ScheduledOutcomeUnknown(self.id, operation) from exc

    async def refresh(self):
        self._view = await self._call("status")
        return self._view

    async def wait_ready(self, *, timeout: float = 120, interval: float = 0.25):
        deadline = time.monotonic() + timeout
        while True:
            view = await self.refresh()
            if view["state"] in ("ACTIVE", "PAUSED", "COMPLETED"):
                return view
            if view["state"] in ("FAILED", "UNKNOWN", "STOPPED"):
                raise RuntimeError(f"Rollout {self.id} entered {view['state']}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Rollout {self.id} is still {view['state']}")
            await asyncio.sleep(interval)

    async def run_shell(self, command: str, *, step_id: int, action_id: str,
                        timeout_ms: int = 5000, output_limit: int = 65536):
        """Submit one explicit, durable action; never invent a retry ID."""
        response = await self._call("step", timeout_s=max(180, timeout_ms / 1000 + 30),
                                    step_id=step_id, action_id=action_id,
                                    command=command, timeout_ms=timeout_ms,
                                    output_limit=output_limit)
        self._view = response["rollout"]
        return response["entry"]["result"]

    async def start_dialogue(self, messages: list[dict]):
        """Pin the policy prefix and task instruction before the first action."""
        return await self._call("dialogue_start", messages=messages)

    async def dialogue(self):
        """Read the worker-owned policy context after trainer reconnection."""
        return await self._call("dialogue_status")

    async def agent_step(self, command: str, *, step_id: int, action_id: str,
                         assistant_message: dict, timeout_ms: int = 120000,
                         output_limit: int = 65536):
        """Durably bind one model reply to one shell action and observation."""
        response = await self._call(
            "agent_step", timeout_s=max(180, timeout_ms / 1000 + 30),
            step_id=step_id, action_id=action_id, command=command,
            assistant_message=assistant_message, timeout_ms=timeout_ms,
            output_limit=output_limit)
        self._view = response["rollout"]
        return response

    async def tb2_evaluate(self):
        """Obtain the official TB2 verdict from the same worker-owned VM."""
        # Official task budgets reach 12,000 s. A generic 60 s RPC timeout
        # would turn a valid verdict into an unknown client outcome.
        transport = RolloutClient(self._transport.socket_path, timeout_s=12100)
        try:
            self._view = await asyncio.to_thread(transport.call, "tb2_evaluate",
                                                 rollout_id=self.id)
        except RolloutOutcomeUnknown as exc:
            raise ScheduledOutcomeUnknown(self.id, "tb2_evaluate") from exc
        return self._view["reward"]

    async def evaluate_counter(self, expected_counter: int):
        """Use the worker's built-in counter verifier for non-TB2 examples."""
        if type(expected_counter) is not int or expected_counter < 0:
            raise ValueError("expected_counter must be a nonnegative integer")
        self._view = await self._call("evaluate", expected_counter=expected_counter)
        return self._view["reward"]

    async def seal_baseline(self, *, allow_prepared_state=False):
        self._view = await self._call("seal_baseline", timeout_s=300,
                                     allow_prepared_state=allow_prepared_state)
        return self._view

    async def pause(self):
        self._view = await self._call("pause")
        return self._view

    async def stop(self):
        self._view = await self._call("stop")
        return self._view

    async def reconcile(self):
        proof = await self._call("reconcile")
        await self.refresh()
        return proof

    async def resource_status(self):
        return await self._call("resource_status")


class ScheduledDSecClient:
    """Application-facing entry that always goes through the rollout worker."""

    def __init__(self, worker_socket, *, create_timeout_s=1800):
        if create_timeout_s <= 0:
            raise ValueError("create_timeout_s must be positive")
        self._transport = RolloutClient(worker_socket)
        self._create_timeout_s = create_timeout_s
        self._opened = False

    async def open(self):
        health = await asyncio.to_thread(self._transport.call, "health")
        if not health.get("scheduler_enabled") or not health.get("durable_journal"):
            raise RuntimeError("Scheduled DSec requires a durable worker with a scheduler")
        self._opened = True
        return self

    async def close(self):
        self._opened = False

    async def __aenter__(self):
        return await self.open()

    async def __aexit__(self, *_):
        await self.close()

    @staticmethod
    def new_rollout_id():
        return uuid.uuid4().hex

    def _require_open(self):
        if not self._opened:
            raise RuntimeError("ScheduledDSecClient.open() must be called first")

    async def create(self, *, task_id: str, rollout_id: str,
                     profile: FrameworkProfile | dict,
                     resources: ResourceDemand | dict | None = None,
                     ttl_running_stop: int | None = None,
                     baseline_rollout_id: str | None = None) -> ScheduledSandbox:
        self._require_open()
        if not isinstance(rollout_id, str) or not ROLLOUT_ID.fullmatch(rollout_id):
            raise ValueError("rollout_id must be 32 lowercase hex characters")
        if isinstance(profile, dict):
            profile = FrameworkProfile.from_dict(profile)
        if not isinstance(profile, FrameworkProfile):
            raise TypeError("profile must be FrameworkProfile or dict")
        profile.validate_runtime()
        if isinstance(resources, ResourceDemand):
            resources = asdict(resources)
        if resources is not None and not isinstance(resources, dict):
            raise TypeError("resources must be ResourceDemand or dict")
        args = {"task_id": task_id, "rollout_id": rollout_id,
                "profile": profile.as_dict()}
        if baseline_rollout_id is not None:
            if not isinstance(baseline_rollout_id, str) or not ROLLOUT_ID.fullmatch(baseline_rollout_id):
                raise ValueError("Invalid baseline_rollout_id")
            args["baseline_rollout_id"] = baseline_rollout_id
        if resources is not None:
            args["resources"] = resources
        if ttl_running_stop is not None:
            args["ttl_running_stop"] = ttl_running_stop
        try:
            view = await asyncio.to_thread(self._transport.call, "create",
                                           timeout_s=self._create_timeout_s, **args)
        except RolloutOutcomeUnknown as exc:
            raise ScheduledOutcomeUnknown(rollout_id, "create") from exc
        return ScheduledSandbox(self._transport, view)

    async def attach(self, rollout_id: str) -> ScheduledSandbox:
        self._require_open()
        if not isinstance(rollout_id, str) or not ROLLOUT_ID.fullmatch(rollout_id):
            raise ValueError("rollout_id must be 32 lowercase hex characters")
        try:
            view = await asyncio.to_thread(self._transport.call, "status",
                                           rollout_id=rollout_id)
        except RolloutOutcomeUnknown as exc:
            raise ScheduledOutcomeUnknown(rollout_id, "status") from exc
        return ScheduledSandbox(self._transport, view)
