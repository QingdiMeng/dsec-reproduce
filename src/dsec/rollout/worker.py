"""Single-host prototype of a rollout worker that outlives trainer clients.

The worker owns a sandbox handle and the interaction transcript. With a state
directory it journals transitions before and after side effects; an unfinished
operation after a crash is UNKNOWN and must not be replayed automatically.
"""

import argparse
import asyncio
from dataclasses import asdict, fields, replace
import json
import os
from pathlib import Path
import re
import signal
import uuid

from dsec.sdk.client import DSecClient, DSecMicroVMRunArgs, DSecContainerRunArgs
from dsec.contracts.profiles import FrameworkProfile, MECHANISM_ROADMAP
from dsec.sdk.sandbox_transport import RequestOutcomeUnknown, ServiceError
from dsec.rollout.store import RolloutStore
from dsec.sdk.client import DSecSandbox
from dsec.contracts.requests import request_digest
from dsec.contracts.evaluation import EvaluationContext, EvaluationFailure, EvaluationOutcome
from dsec.compat.counter_evaluator import CounterEvaluator
from dsec.compat.task_plugins import (LEGACY_EVALUATION_OPERATIONS,
                                      configured_evaluators, execution_command)
from dsec.runtime.scheduler import ProcHostSampler, ResourceBudget, ResourceDemand, WorkScheduler
from dsec.observability.elastic import ElasticResourceMonitor
from dsec.observability.shared import SharedServiceMonitor
from dsec.rollout.environment import SHELL_FEEDBACK_VERSION, format_shell_observation


def container_environment_id(profile):
    if profile.environment in ("erofs_split", "erofs_layers"):
        return profile.environment_id
    return "e1-real" if profile.environment == "erofs_overlay" else "e2-full"


class Rollout:
    def __init__(self, rollout_id, task_id, sandbox, profile, ttl_running_stop,
                 *, sandbox_id=None, store=None, resource_demand=None, lease_held=False):
        self.id = rollout_id
        self.task_id = task_id
        self.sandbox = sandbox
        self.sandbox_id = sandbox.id if sandbox is not None else sandbox_id
        self.store = store
        self.profile = profile
        self.ttl_running_stop = ttl_running_stop
        self.state = "ACTIVE"
        self.next_step = 0
        self.history = []
        self.dialogue_seed = None
        self.dialogue_feedback_version = SHELL_FEEDBACK_VERSION
        self.pending = None
        self.uncertain = []
        self.reward = None
        self.verifier_failure = None
        self.evaluation_identity = None
        self.baseline_sealed = False
        self.baseline_rollout_id = None
        self.baseline_sandbox_id = None
        self.resource_demand = resource_demand
        self.lease_held = lease_held
        self.resource_summary = None
        self.meter_error = None
        self.lock = asyncio.Lock()

    def view(self):
        result = {"rollout_id": self.id, "task_id": self.task_id,
                "sandbox_id": self.sandbox_id, "profile": self.profile.as_dict(),
                "ttl_running_stop": self.ttl_running_stop, "state": self.state,
                "next_step": self.next_step, "history": list(self.history),
                "dialogue_seed": self.dialogue_seed,
                "dialogue_feedback_version": self.dialogue_feedback_version,
                "pending": self.pending, "uncertain": list(self.uncertain),
                "reward": self.reward, "baseline_sealed": self.baseline_sealed,
                "baseline_rollout_id": self.baseline_rollout_id,
                "baseline_sandbox_id": self.baseline_sandbox_id}
        if self.resource_demand is not None:
            result["resource_demand"] = asdict(self.resource_demand)
            result["lease_held"] = self.lease_held
            result["resource_summary"] = self.resource_summary
            result["meter_error"] = self.meter_error
        if self.evaluation_identity is not None:
            result["evaluation"] = dict(self.evaluation_identity)
        if self.verifier_failure is not None:
            result["verifier_failure"] = self.verifier_failure
        return result

    def record(self):
        return {"version": 1, **self.view()}

    def persist(self):
        if self.store is not None:
            self.store.save(self.record())


class RolloutWorker:
    def __init__(self, sandbox_client, state_dir=None, scheduler=None, tb2_tasks_dir=None,
                 *, evaluators=None, command_transform=None):
        self.evaluators = dict(evaluators or {})
        for key, evaluator in configured_evaluators(tb2_tasks_dir).items():
            if key in self.evaluators:
                raise ValueError("Conflicting evaluator configuration: " + key)
            self.evaluators[key] = evaluator
        for key, evaluator in self.evaluators.items():
            if (not isinstance(key, str) or
                    not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", key) or
                    getattr(evaluator, "id", None) != key or
                    any(not callable(getattr(evaluator, method, None))
                        for method in ("validate", "accepts_reward", "evaluate"))):
                raise ValueError("Invalid evaluator registration")
        self.command_transform = command_transform or execution_command
        if not callable(self.command_transform):
            raise TypeError("command_transform must be callable")
        self.sandbox_client = sandbox_client
        self.rollouts = {}
        self.create_lock = asyncio.Lock()
        self.store = RolloutStore(state_dir) if state_dir is not None else None
        if scheduler is not None and self.store is None:
            raise ValueError("Scheduled rollouts require a durable state directory")
        self.scheduler = scheduler
        self.tb2_tasks_dir = Path(tb2_tasks_dir).resolve() if tb2_tasks_dir else None
        self._scheduled_inflight = set()
        self.meters = {}
        self.shared_services = None

    async def _start_meter(self, rollout):
        if (self.scheduler is None or rollout.sandbox is None or
                rollout.id in self.meters or rollout.state != "ACTIVE"):
            return
        try:
            self.meters[rollout.id] = await ElasticResourceMonitor.for_sandbox(
                rollout.sandbox)
            rollout.meter_error = None
        except Exception as exc:
            rollout.meter_error = f"{type(exc).__name__}: {exc}"[:200]
        try:
            rollout.persist()
        except Exception:
            # Observability must not change the outcome of a durable create.
            pass

    async def _finish_meter(self, rollout):
        monitor = self.meters.pop(rollout.id, None)
        if monitor is None:
            return
        try:
            rollout.resource_summary = await monitor.finish()
        except Exception as exc:
            rollout.meter_error = f"meter finish: {type(exc).__name__}: {exc}"[:200]

    async def close_meters(self):
        for monitor in list(self.meters.values()):
            try:
                await monitor.finish()
            except Exception:
                pass
        self.meters.clear()

    def resource_metrics_text(self):
        totals = {"container_cgroup_v2": {"cpu": 0.0}}
        memory_totals = {("container_cgroup_v2", "cgroup"): 0}
        for monitor in self.meters.values():
            sample = monitor.latest or {}
            totals.setdefault(monitor.scope, {"cpu": 0.0})["cpu"] += sample.get("cpu_seconds", 0.0)
            if monitor.backend == "container":
                memory_totals[(monitor.scope, "cgroup")] += sample.get("memory_current_bytes", 0)
            else:
                basis = "pss" if "pss_bytes" in sample else "rss"
                memory_totals[(monitor.scope, basis)] = (
                    memory_totals.get((monitor.scope, basis), 0) +
                    sample.get("pss_bytes" if basis == "pss" else "rss_bytes", 0))
        lines = ["# TYPE dsec_sandbox_meter_active gauge",
                 f"dsec_sandbox_meter_active {len(self.meters)}",
                 "# TYPE dsec_sandbox_actual_memory_bytes gauge",
                 "# TYPE dsec_sandbox_active_cpu_seconds gauge"]
        for (scope, basis), value in memory_totals.items():
            lines.append(f'dsec_sandbox_actual_memory_bytes{{scope="{scope}",basis="{basis}"}} {value}')
        for scope, values in totals.items():
            lines.append(f'dsec_sandbox_active_cpu_seconds{{scope="{scope}"}} {values["cpu"]}')
        return "\n".join(lines) + "\n"

    async def initialize(self):
        """Reattach confirmed microVM rollouts; never replay unfinished actions."""
        if self.store is None:
            return
        for rollout_id, saved in self.store.load().items():
            profile = FrameworkProfile.from_dict(saved["profile"])
            profile.validate_runtime()
            sandbox_id = saved.get("sandbox_id")
            state = saved["state"]
            pending = saved.get("pending")
            sandbox = None
            if sandbox_id and profile.backend == "microvm":
                sandbox = DSecSandbox(self.sandbox_client._transport, sandbox_id)
            elif sandbox_id and profile.backend == "container" and state != "STOPPED":
                try:
                    spec = DSecContainerRunArgs(
                        environment_id=container_environment_id(profile),
                        storage=profile.storage, cpu_qos=profile.cpu_qos,
                        ttl_running_stop=saved["ttl_running_stop"])
                    sandbox = await self.sandbox_client.attach_container(sandbox_id, spec)
                except Exception:
                    sandbox = None
            rollout = Rollout(rollout_id, saved["task_id"], sandbox, profile,
                              saved["ttl_running_stop"], sandbox_id=sandbox_id,
                              store=self.store,
                              resource_demand=(ResourceDemand(**saved["resource_demand"])
                                               if saved.get("resource_demand") else None),
                              lease_held=saved.get("lease_held", False))
            if self.scheduler is not None and rollout.lease_held:
                if rollout.resource_demand is None:
                    raise ValueError("Scheduled rollout has no resource demand")
                self.scheduler.restore(rollout_id, rollout.resource_demand)
            rollout.history = saved["history"]
            rollout.dialogue_seed = saved.get("dialogue_seed")
            feedback_version = saved.get("dialogue_feedback_version", 1)
            if type(feedback_version) is not int or feedback_version not in (1, SHELL_FEEDBACK_VERSION):
                raise ValueError("Unsupported dialogue feedback version")
            rollout.dialogue_feedback_version = feedback_version
            rollout.next_step = saved["next_step"]
            rollout.reward = saved.get("reward")
            rollout.verifier_failure = saved.get("verifier_failure")
            rollout.evaluation_identity = saved.get("evaluation")
            rollout.baseline_sealed = saved.get("baseline_sealed", False)
            rollout.baseline_rollout_id = saved.get("baseline_rollout_id")
            rollout.baseline_sandbox_id = saved.get("baseline_sandbox_id")
            rollout.resource_summary = saved.get("resource_summary")
            rollout.meter_error = saved.get("meter_error")
            rollout.pending = pending
            rollout.uncertain = saved.get("uncertain", [])
            rollout.state = state
            if pending or state in ("CREATING", "EXECUTING", "PAUSING", "STOPPING"):
                rollout.state = "UNKNOWN"
            elif state in ("ACTIVE", "PAUSED", "COMPLETED"):
                if sandbox is None:
                    rollout.state = "UNKNOWN"
                else:
                    try:
                        status = await sandbox.status()
                        if status["state"] not in ("RUNNING", "PAUSED"):
                            rollout.state = "UNKNOWN"
                        elif rollout.baseline_sealed and (
                                status["state"] != "PAUSED" or not status.get("baseline_sealed")):
                            rollout.state = "UNKNOWN"
                    except Exception:
                        rollout.state = "UNKNOWN"
            rollout.persist()
            self.rollouts[rollout_id] = rollout
            if rollout.state == "UNKNOWN" and rollout.pending and rollout.pending.get("request_id"):
                await self._reconcile(rollout)
            if rollout.state == "ACTIVE":
                await self._start_meter(rollout)

    async def dispatch(self, request):
        op = request.get("operation")
        args = request.get("args", {})
        if not isinstance(args, dict):
            raise ValueError("args must be an object")
        if op == "health":
            return {"pid": os.getpid(), "rollouts": len(self.rollouts),
                    "durable_journal": self.store is not None,
                    "scheduler_enabled": self.scheduler is not None,
                    "active_profile": FrameworkProfile().as_dict(),
                    "mechanism_roadmap": MECHANISM_ROADMAP}
        if op == "admission_check":
            if self.scheduler is None:
                return {"admitted": False}
            request_id, digest, backend = (args.get("request_id"),
                                           args.get("digest"), args.get("backend"))
            admitted = any(
                rollout.state == "CREATING" and rollout.lease_held and
                rollout.id in self.scheduler.active and
                rollout.profile.backend == backend and
                isinstance(rollout.pending, dict) and
                rollout.pending.get("operation") == "create" and
                rollout.pending.get("request_id") == request_id and
                rollout.pending.get("request_digest") == digest
                for rollout in self.rollouts.values())
            return {"admitted": admitted}
        if op == "scheduler_status":
            if self.scheduler is None:
                raise ValueError("Scheduler is not configured")
            report = self.scheduler.report()
            report.pop("samples", None)
            return report
        if op == "shared_service_status":
            if self.shared_services is None:
                raise ValueError("Shared service monitoring is not configured")
            return self.shared_services.snapshot()
        if op == "resource_status":
            rollout_id = args.get("rollout_id")
            if not isinstance(rollout_id, str) or rollout_id not in self.rollouts:
                raise ValueError("Unknown rollout_id")
            rollout = self.rollouts[rollout_id]
            monitor = self.meters.get(rollout_id)
            return {"rollout_id": rollout_id, "sandbox_id": rollout.sandbox_id,
                    "state": rollout.state, "resource": (
                        monitor.snapshot() if monitor else rollout.resource_summary),
                    "meter_error": rollout.meter_error}
        if op == "create":
            task_id = args.get("task_id")
            requested_id = args.get("rollout_id")
            profile = FrameworkProfile.from_dict(args.get("profile"))
            profile.validate_runtime()
            ttl = args.get("ttl_running_stop", 300 if profile.backend == "microvm" else None)
            if not isinstance(task_id, str) or not task_id or len(task_id) > 128:
                raise ValueError("task_id must be a nonempty string of at most 128 characters")
            if requested_id is not None and (not isinstance(requested_id, str)
                    or len(requested_id) != 32 or any(c not in "0123456789abcdef" for c in requested_id)):
                raise ValueError("rollout_id must be 32 lowercase hex characters")
            baseline_rollout_id = args.get("baseline_rollout_id")
            baseline_id = None
            if baseline_rollout_id is not None:
                source = self.rollouts.get(baseline_rollout_id)
                known_child = self.rollouts.get(requested_id)
                completed_create = (known_child is not None and known_child.sandbox_id is not None
                                    and known_child.baseline_rollout_id == baseline_rollout_id)
                if (self.scheduler is None or profile.backend != "microvm" or source is None or
                        not source.baseline_sealed or (source.state != "PAUSED" and not completed_create) or
                        source.task_id != task_id or source.profile != profile):
                    raise ValueError("Baseline must be a sealed scheduled microVM of the same task/profile")
                baseline_id = source.sandbox_id
            environment_id = container_environment_id(profile)
            spec = (DSecContainerRunArgs(environment_id=environment_id, storage=profile.storage,
                                         cpu_qos=profile.cpu_qos,
                                         ttl_running_stop=ttl) if profile.backend == "container"
                    else DSecMicroVMRunArgs(ttl_running_stop=ttl,
                                           environment_id=("e3-mixed" if profile.environment == "e3_mixed"
                                                           else profile.environment_id),
                                           storage=profile.storage,
                                           memory_profile=profile.memory,
                                           verifier_storage=profile.verifier_storage,
                                           baseline_id=baseline_id))
            if profile.backend == "microvm":
                spec.service_args()
            else:
                spec.validate()
            if self.scheduler is not None:
                if requested_id is None:
                    raise ValueError("Scheduled create requires rollout_id for timeout recovery")
                return await self._scheduled_create(task_id, requested_id, profile, ttl,
                                                    spec, args.get("resources"), baseline_rollout_id)
            async with self.create_lock:
                if requested_id in self.rollouts:
                    existing = self.rollouts[requested_id]
                    if (existing.task_id != task_id or existing.profile != profile
                            or existing.ttl_running_stop != ttl):
                        raise ValueError("rollout_id already belongs to another task/profile")
                    return existing.view()
                rollout_id = requested_id or uuid.uuid4().hex
                create_request_id = uuid.uuid4().hex
                if self.store is not None:
                    reservation = Rollout(rollout_id, task_id, None, profile, ttl, store=self.store)
                    reservation.state = "CREATING"
                    reservation.pending = {"operation": "create", "request_id": create_request_id}
                    self.store.reserve(reservation.record())
                    self.rollouts[rollout_id] = reservation
                try:
                    sandbox = (await self.sandbox_client.run_container(spec, request_id=create_request_id)
                               if profile.backend == "container"
                               else await self.sandbox_client.run_microvm(spec,request_id=create_request_id))
                except Exception:
                    if self.store is not None:
                        reservation.state = "UNKNOWN"
                        reservation.persist()
                    raise
                rollout = Rollout(rollout_id, task_id, sandbox, profile, ttl, store=self.store)
                try:
                    rollout.persist()
                except Exception:
                    # The reservation survives; never create another sandbox for
                    # this ID when its sandbox identity was not durably committed.
                    try:
                        await sandbox.stop()
                    finally:
                        if self.store is not None:
                            reservation.state = "UNKNOWN"
                    raise
                self.rollouts[rollout_id] = rollout
                return rollout.view()
        rollout_id = args.get("rollout_id")
        if not isinstance(rollout_id, str) or rollout_id not in self.rollouts:
            raise ValueError("Unknown rollout_id")
        rollout = self.rollouts[rollout_id]
        if op == "status":
            # Long official verifiers hold the mutation lock. Let a trainer
            # that lost its response inspect durable progress while they run.
            return rollout.view()
        if op == "dialogue_status":
            return self._dialogue(rollout)
        async with rollout.lock:
            if op == "dialogue_start":
                messages = args.get("messages")
                if (not isinstance(messages, list) or not messages or len(messages) > 64 or
                        any(not isinstance(item, dict) or
                            item.get("role") not in ("system", "developer", "user") or
                            not isinstance(item.get("content"), str)
                            for item in messages) or
                        len(json.dumps(messages).encode()) > 65536):
                    raise ValueError("Invalid initial dialogue messages")
                if rollout.dialogue_seed is not None:
                    if rollout.dialogue_seed != messages:
                        raise ValueError("Dialogue is already initialized with different messages")
                elif rollout.next_step or rollout.state not in ("ACTIVE", "PAUSED"):
                    raise RuntimeError("Dialogue must start before the first action")
                else:
                    rollout.dialogue_seed = messages
                    rollout.dialogue_feedback_version = SHELL_FEEDBACK_VERSION
                    rollout.persist()
                return self._dialogue(rollout)
            if op == "agent_step":
                if rollout.dialogue_seed is None:
                    raise RuntimeError("Dialogue must be initialized")
                message = args.get("assistant_message")
                if (not isinstance(message, dict) or message.get("role") != "assistant" or
                        not isinstance(message.get("content"), str) or
                        len(json.dumps(message).encode()) > 32768):
                    raise ValueError("Invalid assistant message")
                result = await self._step(rollout, {**args, "assistant_message": message})
                return {**result, "dialogue": self._dialogue(rollout)}
            if op == "reconcile":
                return await self._reconcile(rollout)
            if op == "sandbox_status":
                if rollout.sandbox is None:
                    raise RuntimeError("Sandbox identity is unknown after interrupted creation")
                return await rollout.sandbox.status()
            if op == "seal_baseline":
                if (rollout.profile.backend != "microvm" or rollout.dialogue_seed is not None or
                        rollout.reward is not None or args.get("allow_prepared_state") is not True):
                    raise ValueError("Baseline requires trusted preparation before policy dialogue/verifier")
                if rollout.baseline_sealed:
                    return rollout.view()
                if rollout.state != "ACTIVE":
                    raise RuntimeError("Seal requires ACTIVE rollout")
                request_id = uuid.uuid4().hex
                rollout.pending = {"operation": "seal_baseline", "request_id": request_id}
                rollout.state = "PAUSING"
                rollout.persist()
                try:
                    await rollout.sandbox.seal_baseline(allow_prepared_state=True, request_id=request_id)
                except Exception:
                    rollout.state = "UNKNOWN"
                    rollout.persist()
                    raise
                rollout.baseline_sealed = True
                rollout.state = "PAUSED"
                rollout.pending = None
                await self._finish_meter(rollout)
                rollout.persist()
                return rollout.view()
            if rollout.baseline_sealed and op not in ("stop", "sandbox_status", "reconcile"):
                raise RuntimeError("Sealed baseline is immutable; create a new episode from it")
            if op == "pause":
                if rollout.state == "PAUSED":
                    return rollout.view()
                if rollout.state != "ACTIVE":
                    raise RuntimeError("Cannot pause rollout in " + rollout.state)
                request_id = uuid.uuid4().hex if rollout.profile.backend == "microvm" else None
                rollout.pending = {"operation": "pause", "request_id": request_id}
                rollout.state = "PAUSING"
                rollout.persist()
                monitor = self.meters.get(rollout.id)
                if monitor is not None:
                    monitor.mark_stopping()
                try:
                    await rollout.sandbox.pause(**({"request_id": request_id} if request_id else {}))
                except RequestOutcomeUnknown:
                    if monitor is not None:
                        monitor.mark_stopping(False)
                    rollout.state = "UNKNOWN"
                    rollout.persist()
                    proof = await self._reconcile(rollout)
                    if proof["reconciled"]:
                        return rollout.view()
                    raise
                except Exception:
                    if monitor is not None:
                        monitor.mark_stopping(False)
                    rollout.state = "UNKNOWN"
                    rollout.persist()
                    raise
                rollout.state = "PAUSED"
                rollout.pending = None
                await self._finish_meter(rollout)
                try:
                    rollout.persist()
                except Exception:
                    rollout.state = "UNKNOWN"
                    raise
                return rollout.view()
            if op == "step":
                if rollout.dialogue_seed is not None:
                    raise RuntimeError("Use agent_step for a dialogue rollout")
                return await self._step(rollout, args)
            if op == "evaluate":
                return await self._evaluate(rollout, args)
            if op in LEGACY_EVALUATION_OPERATIONS:
                return await self._plugin_evaluate(
                    rollout, LEGACY_EVALUATION_OPERATIONS[op], {}, operation=op)
            if op == "task_evaluate":
                return await self._plugin_evaluate(
                    rollout, args.get("evaluator"), args.get("parameters", {}))
            if op == "stop":
                if rollout.state != "STOPPED":
                    if rollout.sandbox is None:
                        raise RuntimeError("Sandbox identity is unknown after interrupted creation")
                    unresolved = rollout.pending if rollout.state == "UNKNOWN" else None
                    request_id = uuid.uuid4().hex
                    rollout.pending = {"operation": "stop", "request_id": request_id,
                                       "prior_unknown": unresolved}
                    rollout.state = "STOPPING"
                    rollout.persist()
                    monitor = self.meters.get(rollout.id)
                    if monitor is not None:
                        monitor.mark_stopping()
                    try:
                        await rollout.sandbox.stop(**({"request_id": request_id} if request_id else {}))
                    except RequestOutcomeUnknown:
                        if monitor is not None:
                            monitor.mark_stopping(False)
                        rollout.state = "UNKNOWN"
                        rollout.persist()
                        proof = await self._reconcile(rollout)
                        if proof["reconciled"]:
                            return rollout.view()
                        raise
                    except Exception:
                        if monitor is not None:
                            monitor.mark_stopping(False)
                        rollout.state = "UNKNOWN"
                        rollout.persist()
                        raise
                    await self._finish_meter(rollout)
                    rollout.state = "STOPPED"
                    rollout.pending = None
                    if unresolved is not None:
                        rollout.uncertain.append(unresolved)
                    release_lease = self.scheduler is not None and rollout.lease_held
                    if release_lease:
                        rollout.lease_held = False
                    try:
                        rollout.persist()
                    except Exception:
                        rollout.state = "UNKNOWN"
                        rollout.lease_held = release_lease
                        rollout.pending = {"operation": "stop", "request_id": request_id,
                                           "prior_unknown": unresolved}
                        raise
                    if release_lease:
                        await self.scheduler.release(rollout.id)
                return rollout.view()
            raise ValueError("Unknown operation")

    @staticmethod
    def _dialogue(rollout):
        if rollout.dialogue_seed is None:
            raise RuntimeError("Dialogue is not initialized")
        messages = list(rollout.dialogue_seed)
        for entry in rollout.history:
            message = entry.get("assistant_message")
            if message is None:
                raise RuntimeError("Dialogue history contains a step without an assistant message")
            messages.append(message)
            if rollout.dialogue_feedback_version == 1:
                output = entry["result"].get("output", "")
                content = output[:4000] or "(no output)"
            else:
                content = format_shell_observation(entry)
            messages.append({"role": "user", "content": content})
        return {"rollout_id": rollout.id, "sandbox_id": rollout.sandbox_id,
                "state": rollout.state, "next_step": rollout.next_step,
                "messages": messages, "pending": rollout.pending,
                "dialogue_feedback_version": rollout.dialogue_feedback_version}

    async def _scheduled_create(self, task_id, requested_id, profile, ttl, spec, resources,
                                baseline_rollout_id=None):
        defaults = {"cpu": 1.0, "memory_mb": 512, "disk_mb": 1024,
                    "network_mbps": 1.0, "api_episode_slots": 1}
        if resources is not None:
            if not isinstance(resources, dict) or set(resources) - set(ResourceDemand.__dataclass_fields__):
                raise ValueError("Invalid resource demand fields")
            defaults.update(resources)
        demand = ResourceDemand(**defaults)
        if profile.backend == "container":
            if demand.memory_mb < 128:
                raise ValueError("Container memory demand must be at least 128 MiB")
            spec = replace(spec, memory_limit_mb=demand.memory_mb,
                           cpu_cores_limit=demand.cpu)
        if self.scheduler._demand_too_large(demand):
            raise ValueError("Rollout exceeds configured resource budget")
        async with self.create_lock:
            if requested_id in self.rollouts:
                rollout = self.rollouts[requested_id]
                if (rollout.task_id != task_id or rollout.profile != profile or
                        rollout.ttl_running_stop != ttl or rollout.resource_demand != demand or
                        rollout.baseline_rollout_id != baseline_rollout_id):
                    raise ValueError("rollout_id already belongs to another task/profile/resources")
                if rollout.state != "QUEUED" or requested_id in self._scheduled_inflight:
                    return rollout.view()
                rollout_id = requested_id
            else:
                rollout_id = requested_id or uuid.uuid4().hex
                rollout = Rollout(rollout_id, task_id, None, profile, ttl,
                                  store=self.store, resource_demand=demand)
                rollout.state = "QUEUED"
                rollout.baseline_rollout_id = baseline_rollout_id
                rollout.baseline_sandbox_id = spec.baseline_id if profile.backend == "microvm" else None
                self.store.reserve(rollout.record())
                self.rollouts[rollout_id] = rollout
            create_request_id = uuid.uuid4().hex
            self._scheduled_inflight.add(rollout_id)
        try:
            requirements = (("threefs_server", "threefs_client")
                            if profile.storage == "threefs_lazy" and
                            self.shared_services is not None else ())
            await self.scheduler.acquire(rollout_id, demand,
                                         requirements=requirements)
            rollout.lease_held = True
            rollout.state = "CREATING"
            rollout.pending = {"operation": "create", "request_id": create_request_id,
                               "request_digest": request_digest(
                                   "create", None,
                                   spec.service_args() if profile.backend == "microvm"
                                   else spec.lifecycle_args())}
            try:
                rollout.persist()
            except BaseException:
                rollout.lease_held = False
                rollout.state = "FAILED"
                rollout.pending = None
                await self.scheduler.release(rollout_id)
                raise
            try:
                sandbox = (await self.sandbox_client.run_container(spec, request_id=create_request_id)
                           if profile.backend == "container"
                           else await self.sandbox_client.run_microvm(spec, request_id=create_request_id))
            except BaseException:
                rollout.state = "UNKNOWN"
                rollout.persist()
                try:
                    await self._reconcile(rollout)
                except Exception:
                    # An unproven create keeps its lease; never infer cleanup.
                    pass
                raise
            rollout.sandbox = sandbox
            rollout.sandbox_id = sandbox.id
            rollout.pending = None
            rollout.state = "ACTIVE"
            try:
                rollout.persist()
            except Exception:
                rollout.state = "UNKNOWN"
                rollout.pending = {"operation": "create", "request_id": create_request_id}
                rollout.persist()
                raise
            await self._start_meter(rollout)
            return rollout.view()
        finally:
            self._scheduled_inflight.discard(rollout_id)

    async def _step(self, rollout, args):
        step_id = args.get("step_id")
        action_id = args.get("action_id")
        command = args.get("command")
        timeout_ms = args.get("timeout_ms", 5000)
        output_limit = args.get("output_limit", 65536)
        assistant_message = args.get("assistant_message")
        if not isinstance(step_id, int) or isinstance(step_id, bool) or step_id < 0:
            raise ValueError("step_id must be a nonnegative integer")
        if not isinstance(action_id, str) or not action_id or len(action_id) > 128:
            raise ValueError("action_id must be a nonempty string of at most 128 characters")
        if not isinstance(command, str):
            raise ValueError("command must be a string")
        if (not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or
                not 1 <= timeout_ms <= 900000 or
                not isinstance(output_limit, int) or isinstance(output_limit, bool) or
                not 1 <= output_limit <= 1024 * 1024):
            raise ValueError("Invalid step timeout/output limit")
        if step_id < rollout.next_step:
            entry = rollout.history[step_id]
            if (entry["action_id"] != action_id or entry["command"] != command or
                    entry.get("timeout_ms", 5000) != timeout_ms or
                    entry.get("output_limit", 65536) != output_limit or
                    entry.get("assistant_message") != assistant_message):
                raise ValueError("Completed step conflicts with submitted action")
            return {"entry": entry, "replayed_result": True, "rollout": rollout.view()}
        if step_id != rollout.next_step:
            raise ValueError("Step is out of order")
        if rollout.state not in ("ACTIVE", "PAUSED"):
            raise RuntimeError("Cannot execute step in " + rollout.state)
        submitted_command = self._execution_command(rollout, command)
        if not isinstance(submitted_command, str):
            raise ValueError("Command transform must return a string")
        request_id = uuid.uuid4().hex
        rollout.pending = {"operation": "step", "step_id": step_id,
                           "action_id": action_id, "command": command,
                           "execution_command": submitted_command,
                           "timeout_ms": timeout_ms, "output_limit": output_limit,
                           "request_id": request_id}
        if assistant_message is not None:
            rollout.pending["assistant_message"] = assistant_message
        rollout.state = "EXECUTING"
        rollout.persist()
        try:
            result = await rollout.sandbox.run_shell(
                submitted_command,
                timeout_ms=timeout_ms, output_limit=output_limit,
                **({"request_id": request_id} if request_id else {}))
        except RequestOutcomeUnknown:
            rollout.state = "UNKNOWN"
            rollout.persist()
            proof = await self._reconcile(rollout)
            if proof["reconciled"]:
                return {"entry": rollout.history[-1], "replayed_result": False,
                        "recovered_result": True, "rollout": rollout.view()}
            raise
        except ServiceError as exc:
            if exc.kind in ("CommandOutcomeUnknown", "SandboxError"):
                rollout.state = "UNKNOWN"
            else:
                rollout.state = "ACTIVE"
                rollout.pending = None
            rollout.persist()
            raise
        except Exception:
            # Unexpected transport/runtime errors have no proven execution result.
            rollout.state = "UNKNOWN"
            rollout.persist()
            raise
        entry = {"step_id": step_id, "action_id": action_id,
                 "command": command, "execution_command": submitted_command,
                 "timeout_ms": timeout_ms,
                 "output_limit": output_limit, "request_id": request_id,
                 "result": result}
        if assistant_message is not None:
            entry["assistant_message"] = assistant_message
        rollout.history.append(entry)
        rollout.next_step += 1
        rollout.pending = None
        rollout.state = "ACTIVE"
        try:
            rollout.persist()
        except Exception:
            rollout.state = "UNKNOWN"
            raise
        return {"entry": entry, "replayed_result": False, "rollout": rollout.view()}

    async def _reconcile(self, rollout):
        pending = rollout.pending
        if rollout.state != "UNKNOWN" or not pending or pending.get("operation") not in (
                "step", "create", "pause", "seal_baseline", "evaluate", "stop"):
            return {"reconciled": False, "reason": "no_reconcilable_unknown_operation",
                    "rollout": rollout.view()}
        request_id = pending.get("request_id")
        if not request_id or (rollout.profile.backend == "container" and
                              pending["operation"] not in ("step", "evaluate", "create", "stop")):
            return {"reconciled": False, "reason": "no_durable_sandbox_request",
                    "rollout": rollout.view()}
        if rollout.profile.backend == "container":
            if pending["operation"] in ("create", "stop"):
                proof = await self.sandbox_client.lookup_container_request(request_id)
            else:
                if rollout.sandbox is None:
                    return {"reconciled": False, "reason": "sandbox_unavailable",
                            "rollout": rollout.view()}
                proof = await rollout.sandbox.query_request(request_id)
        else:
            proof = await self.sandbox_client.lookup_request(request_id)
        if proof["state"] != "DONE":
            return {"reconciled": False, "request_state": proof["state"],
                    "rollout": rollout.view()}
        if pending["operation"] == "create":
            if rollout.profile.backend == "container":
                spec = DSecContainerRunArgs(
                    environment_id=container_environment_id(rollout.profile),
                    storage=rollout.profile.storage, cpu_qos=rollout.profile.cpu_qos,
                    ttl_running_stop=rollout.ttl_running_stop,
                    memory_limit_mb=(rollout.resource_demand.memory_mb
                                     if rollout.resource_demand else 512),
                    cpu_cores_limit=(rollout.resource_demand.cpu
                                     if rollout.resource_demand else 1.0))
                expected = request_digest("create", None, spec.lifecycle_args())
            else:
                spec = DSecMicroVMRunArgs(ttl_running_stop=rollout.ttl_running_stop,
                    environment_id=("e3-mixed" if rollout.profile.environment == "e3_mixed"
                                    else rollout.profile.environment_id),
                    storage=rollout.profile.storage,
                    memory_profile=rollout.profile.memory,
                    verifier_storage=rollout.profile.verifier_storage,
                    baseline_id=rollout.baseline_sandbox_id)
                expected = request_digest("create", None, spec.service_args())
            if (proof["operation"] != "create" or proof["sandbox_id"] is not None
                    or proof["digest"] != expected):
                raise RuntimeError("Sandbox create proof does not match rollout reservation")
            if not proof["response"]["ok"]:
                if self.scheduler is not None and rollout.lease_held:
                    rollout.state = "FAILED"
                    rollout.pending = None
                    rollout.lease_held = False
                    rollout.persist()
                    await self.scheduler.release(rollout.id)
                return {"reconciled": False, "request_state": "DONE_ERROR",
                        "error": proof["response"]["error"], "rollout": rollout.view()}
            sandbox_id = proof["response"]["result"]["id"]
            try:
                sandbox = (await self.sandbox_client.attach_container(sandbox_id, spec)
                           if rollout.profile.backend == "container"
                           else await self.sandbox_client.attach(sandbox_id))
            except Exception:
                return {"reconciled": False, "request_state": "DONE_BUT_SANDBOX_UNAVAILABLE",
                        "sandbox_id": sandbox_id, "rollout": rollout.view()}
            rollout.sandbox = sandbox
            rollout.sandbox_id = sandbox_id
            rollout.pending = None
            rollout.state = "ACTIVE"
            rollout.persist()
            await self._start_meter(rollout)
            return {"reconciled": True, "request_state": "DONE", "rollout": rollout.view()}
        operation = pending["operation"]
        rpc_operation = operation if operation in ("pause", "seal_baseline", "stop") else "execute"
        rpc_args = ({"allow_prepared_state": True} if operation == "seal_baseline" else
                    {} if operation in ("pause", "stop") else
                    {"command": ((pending["execution_command"] if "execution_command" in pending
                                  else self._execution_command(rollout, pending["command"]))
                                 if operation == "step" else CounterEvaluator.command),
                     "timeout_ms": pending.get("timeout_ms", 5000) if operation == "step" else 5000,
                     "output_limit": pending.get("output_limit", 65536) if operation == "step" else 65536})
        if operation == "stop" and rollout.profile.backend == "container":
            rpc_args = DSecContainerRunArgs(
                environment_id=container_environment_id(rollout.profile),
                storage=rollout.profile.storage).stop_args()
        expected = request_digest(rpc_operation, rollout.sandbox_id, rpc_args)
        if (proof["operation"] != rpc_operation or proof["sandbox_id"] != rollout.sandbox_id
                or proof["digest"] != expected):
            raise RuntimeError("Sandbox request proof does not match pending rollout operation")
        response = proof["response"]
        if not response["ok"]:
            return {"reconciled": False, "request_state": "DONE_ERROR",
                    "error": response["error"], "rollout": rollout.view()}
        if operation in ("pause", "seal_baseline"):
            if operation == "seal_baseline":
                if not response["result"].get("baseline_sealed"):
                    raise RuntimeError("Confirmed seal did not return sealed baseline")
                rollout.baseline_sealed = True
            if response["result"].get("state") != "PAUSED":
                raise RuntimeError("Confirmed pause did not return PAUSED")
            monitor = self.meters.get(rollout.id)
            if monitor is not None:
                monitor.mark_stopping()
            rollout.state = "PAUSED"
            rollout.pending = None
            await self._finish_meter(rollout)
            rollout.persist()
            return {"reconciled": True, "request_state": "DONE", "rollout": rollout.view()}
        if operation == "stop":
            if response["result"].get("state") != "STOPPED":
                raise RuntimeError("Confirmed stop did not return STOPPED")
            monitor = self.meters.get(rollout.id)
            if monitor is not None:
                monitor.mark_stopping()
            if pending.get("prior_unknown") is not None:
                rollout.uncertain.append(pending["prior_unknown"])
            rollout.state = "STOPPED"
            rollout.pending = None
            await self._finish_meter(rollout)
            release_lease = self.scheduler is not None and rollout.lease_held
            if release_lease:
                rollout.lease_held = False
            rollout.persist()
            if release_lease:
                await self.scheduler.release(rollout.id)
            return {"reconciled": True, "request_state": "DONE", "rollout": rollout.view()}
        if operation == "evaluate":
            rollout.reward = CounterEvaluator.reward_from_result(
                response["result"], pending["expected_counter"])
            rollout.state = "COMPLETED"
            rollout.pending = None
            rollout.persist()
            return {"reconciled": True, "request_state": "DONE", "rollout": rollout.view()}
        entry = {"step_id": pending["step_id"], "action_id": pending["action_id"],
                 "command": pending["command"],
                 "timeout_ms": pending.get("timeout_ms", 5000),
                 "output_limit": pending.get("output_limit", 65536),
                 "request_id": request_id,
                 "result": response["result"]}
        if "execution_command" in pending:
            entry["execution_command"] = pending["execution_command"]
        if pending.get("assistant_message") is not None:
            entry["assistant_message"] = pending["assistant_message"]
        if entry["step_id"] != rollout.next_step:
            raise RuntimeError("Pending step no longer matches rollout position")
        rollout.history.append(entry)
        rollout.next_step += 1
        rollout.pending = None
        rollout.state = "ACTIVE"
        try:
            rollout.persist()
        except Exception:
            rollout.state = "UNKNOWN"
            raise
        return {"reconciled": True, "request_state": "DONE", "rollout": rollout.view()}

    async def _evaluate(self, rollout, args):
        expected = CounterEvaluator.expected(args)
        if rollout.state not in ("ACTIVE", "PAUSED", "COMPLETED"):
            raise RuntimeError("Cannot evaluate rollout in " + rollout.state)
        if rollout.state == "COMPLETED":
            if rollout.reward["expected_counter"] != expected:
                raise ValueError("Evaluation conflicts with completed reward")
            return rollout.view()
        previous_state = rollout.state
        request_id = uuid.uuid4().hex
        rollout.pending = {"operation": "evaluate", "expected_counter": expected,
                           "previous_state": previous_state, "request_id": request_id}
        rollout.state = "EXECUTING"
        rollout.persist()
        try:
            result = await rollout.sandbox.run_shell(CounterEvaluator.command, **(
                {"request_id": request_id} if request_id else {}))
        except RequestOutcomeUnknown:
            rollout.state = "UNKNOWN"
            rollout.persist()
            proof = await self._reconcile(rollout)
            if proof["reconciled"]:
                return rollout.view()
            raise
        except ServiceError as exc:
            if exc.kind in ("CommandOutcomeUnknown", "SandboxError"):
                rollout.state = "UNKNOWN"
            else:
                rollout.state = previous_state
                rollout.pending = None
            rollout.persist()
            raise
        except Exception:
            rollout.state = "UNKNOWN"
            rollout.persist()
            raise
        rollout.reward = CounterEvaluator.reward_from_result(result, expected)
        rollout.state = "COMPLETED"
        rollout.pending = None
        try:
            rollout.persist()
        except Exception:
            rollout.state = "UNKNOWN"
            raise
        return rollout.view()

    def _evaluation_context(self, rollout):
        evidence = (str(self.store.root / "evidence" / rollout.id)
                    if self.store is not None else None)
        return EvaluationContext(rollout.id, rollout.task_id, rollout.profile.backend,
                                 rollout.profile.environment_id, evidence)

    def _execution_command(self, rollout, command):
        return self.command_transform(self._evaluation_context(rollout), command)

    async def _tb2_evaluate(self, rollout):
        """Compatibility entry point; task implementation belongs to the application."""
        return await self._plugin_evaluate(
            rollout, LEGACY_EVALUATION_OPERATIONS["tb2_evaluate"], {}, operation="tb2_evaluate")

    async def _plugin_evaluate(self, rollout, evaluator_id, parameters, *, operation="task_evaluate"):
        if not isinstance(evaluator_id, str) or evaluator_id not in self.evaluators:
            raise RuntimeError("Evaluator is not configured: " + str(evaluator_id))
        if not isinstance(parameters, dict):
            raise ValueError("Evaluation parameters must be an object")
        encoded_parameters = json.dumps(parameters, allow_nan=False, sort_keys=True)
        parameters = json.loads(encoded_parameters)
        evaluator = self.evaluators[evaluator_id]
        context = self._evaluation_context(rollout)
        evaluator.validate(context, json.loads(encoded_parameters))
        identity = {"evaluator": evaluator_id, "parameters": parameters,
                    "digest": request_digest("task_evaluate", rollout.sandbox_id,
                                             {"evaluator": evaluator_id, "parameters": parameters})}
        if rollout.state == "COMPLETED":
            if (rollout.evaluation_identity is not None and
                    rollout.evaluation_identity != identity):
                raise ValueError("Evaluation conflicts with completed evaluator/parameters")
            if evaluator.accepts_reward(rollout.reward, json.loads(encoded_parameters)):
                return rollout.view()
            raise RuntimeError("Rollout was completed by a different evaluator")
        if rollout.state not in ("ACTIVE", "PAUSED"):
            raise RuntimeError("Cannot evaluate rollout in " + rollout.state)
        rollout.evaluation_identity = identity
        rollout.pending = {"operation": operation, "task_id": rollout.task_id,
                           "evaluator": evaluator_id, "parameters": parameters}
        rollout.state = "EXECUTING"
        rollout.persist()
        try:
            outcome = await evaluator.evaluate(context, rollout.sandbox,
                                               json.loads(encoded_parameters))
            if not isinstance(outcome, EvaluationOutcome):
                raise ValueError("Evaluator returned no validated outcome")
            # Freeze plugin-owned dictionaries before committing them into worker state.
            outcome = EvaluationOutcome(json.loads(json.dumps(outcome.reward, allow_nan=False)))
        except EvaluationFailure as exc:
            rollout.state = "UNKNOWN"
            try:
                rollout.verifier_failure = json.loads(json.dumps(exc.details, allow_nan=False))
            finally:
                rollout.persist()
            raise RuntimeError(str(exc)) from exc
        except BaseException:
            # Includes cancellation: a multi-command verifier may already have run.
            rollout.state = "UNKNOWN"
            rollout.persist()
            raise
        rollout.reward = outcome.reward
        rollout.state = "COMPLETED"
        rollout.pending = None
        try:
            rollout.persist()
        except Exception:
            rollout.state = "UNKNOWN"
            raise
        return rollout.view()


async def serve(socket_path, sandbox_socket, state_dir=None,
                scheduler_budget=None, metrics_port=None, tb2_tasks_dir=None):
    client = DSecClient(sandbox_socket)
    await client.open()
    scheduler = None
    shared_services = None
    if scheduler_budget is not None:
        settings = json.loads(Path(scheduler_budget).read_text())
        budget_keys = {field.name for field in fields(ResourceBudget)}
        budget = ResourceBudget(**{key: value for key, value in settings.items()
                                   if key in budget_keys})
        sampler = ProcHostSampler(settings.get("disk_path", state_dir),
                                  settings["network_interface"],
                                  settings.get("disk_device"))
        if settings.get("shared_services") is not None:
            shared_services = SharedServiceMonitor(settings["shared_services"])
        scheduler = WorkScheduler(
            budget, sampler,
            dependency_ready=(shared_services.ready if shared_services is not None else None))
    elif metrics_port is not None:
        raise ValueError("Metrics endpoint requires a scheduler budget")
    worker = RolloutWorker(client, state_dir=state_dir, scheduler=scheduler,
                           tb2_tasks_dir=tb2_tasks_dir)
    await worker.initialize()
    worker.shared_services = shared_services
    if scheduler is not None:
        await scheduler.start()
    if shared_services is not None:
        await shared_services.start()
    socket_path = Path(socket_path)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        if worker.store is None:
            raise FileExistsError(f"Refusing to replace existing socket: {socket_path}")
        try:
            _, probe_writer = await asyncio.open_unix_connection(str(socket_path))
        except (OSError, ConnectionError):
            socket_path.unlink()  # State-directory lock excludes another worker.
        else:
            probe_writer.close()
            await probe_writer.wait_closed()
            raise FileExistsError(f"Another worker is serving: {socket_path}")

    handler_tasks = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        handler_tasks.add(task)
        try:
            try:
                line = await reader.readline()
                if len(line) > 131072 or not line.endswith(b"\n"):
                    raise ValueError("Invalid request frame")
                request = json.loads(line)
                response = {"ok": True, "result": await worker.dispatch(request)}
            except Exception as exc:
                response = {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
            handler_tasks.discard(task)

    old_umask = os.umask(0o077)
    try:
        server = await asyncio.start_unix_server(handle, path=str(socket_path), limit=131073)
    finally:
        os.umask(old_umask)
    os.chmod(socket_path, 0o600)
    metrics_server = None
    if metrics_port is not None:
        async def handle_metrics(reader, writer):
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=2)
                if line.startswith(b"GET /metrics "):
                    body = (scheduler.prometheus_text()
                            + worker.resource_metrics_text()
                            + (shared_services.prometheus_text()
                               if shared_services is not None else "")).encode()
                    status = b"200 OK"
                else:
                    body = b"Not Found\n"
                    status = b"404 Not Found"
                writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: text/plain; version=0.0.4\r\n"
                             + b"Content-Length: " + str(len(body)).encode()
                             + b"\r\nConnection: close\r\n\r\n" + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        metrics_server = await asyncio.start_server(handle_metrics, "127.0.0.1", metrics_port)
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)
    print("READY " + str(socket_path), flush=True)
    try:
        await shutdown.wait()
    finally:
        server.close()
        for task in tuple(handler_tasks):
            task.cancel()
        if handler_tasks:
            await asyncio.gather(*tuple(handler_tasks), return_exceptions=True)
        await server.wait_closed()
        if metrics_server is not None:
            metrics_server.close()
            await metrics_server.wait_closed()
        socket_path.unlink(missing_ok=True)
        await worker.close_meters()
        if shared_services is not None:
            await shared_services.close()
        if scheduler is not None:
            await scheduler.close()
        await client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--sandbox-socket", required=True)
    parser.add_argument("--state-dir")
    parser.add_argument("--scheduler-budget")
    parser.add_argument("--metrics-port", type=int)
    parser.add_argument("--tb2-tasks-dir")
    options = parser.parse_args()
    asyncio.run(serve(options.socket, options.sandbox_socket,
                      options.state_dir or str(Path(options.socket).parent / "rollouts"),
                      options.scheduler_budget, options.metrics_port,
                      options.tb2_tasks_dir))


if __name__ == "__main__":
    main()
