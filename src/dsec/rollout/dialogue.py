"""Build model-facing messages from the authoritative committed action history."""
from dsec.rollout.environment import format_shell_observation


def transcript(rollout):
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
