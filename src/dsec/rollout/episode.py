"""One episode's in-memory state and unchanged version-1 durable representation.

The coordinating service owns mutation and locking. This object has no driver,
transport, evaluator registry or independent recovery loop.
"""
import asyncio
from dataclasses import asdict
from dsec.rollout.environment import SHELL_FEEDBACK_VERSION


class Episode:
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
