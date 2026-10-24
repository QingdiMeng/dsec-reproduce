"""Validate action identity and submit one prepared command.

No journal, lease, lifecycle mutation, retries or background recovery live here.
EpisodeService persists before submission and applies the result under its lock.
"""
from dataclasses import dataclass
import uuid


@dataclass(frozen=True)
class ActionReplay:
    entry: dict


@dataclass(frozen=True)
class PendingAction:
    pending: dict

    def result_entry(self, result):
        entry = {key: value for key, value in self.pending.items() if key != "operation"}
        entry["result"] = result
        return entry


class ActionExecutor:
    def prepare(self, rollout, args, command_transform):
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
            return ActionReplay(entry)
        if step_id != rollout.next_step:
            raise ValueError("Step is out of order")
        if rollout.state not in ("ACTIVE", "PAUSED"):
            raise RuntimeError("Cannot execute step in " + rollout.state)
        submitted_command = command_transform(rollout, command)
        if not isinstance(submitted_command, str):
            raise ValueError("Command transform must return a string")
        request_id = uuid.uuid4().hex
        pending = {"operation": "step", "step_id": step_id,
                   "action_id": action_id, "command": command,
                   "execution_command": submitted_command,
                   "timeout_ms": timeout_ms, "output_limit": output_limit,
                   "request_id": request_id}
        if assistant_message is not None:
            pending["assistant_message"] = assistant_message
        return PendingAction(pending)

    async def execute(self, sandbox, action):
        pending = action.pending
        return await sandbox.run_shell(
            pending["execution_command"], timeout_ms=pending["timeout_ms"],
            output_limit=pending["output_limit"], request_id=pending["request_id"])
