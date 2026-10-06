"""Miles custom agent function backed by the durable DSec rollout worker.

Configure Miles with ``--custom-agent-function-path miles_dsec_agent_function.run``.
The policy still uses Miles' session server, which records tokens/logprobs;
the worker owns the sandbox, dialogue, actions, and official TB2 verdict.

For process-restart recovery, resubmit the same ``metadata.dsec_rollout_id``.
The sandbox episode can finish, but its result is deliberately dropped from
training: a restarted Miles session cannot reconstruct tokens/logprobs emitted
by the previous session.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import uuid
from typing import Any

from agent_environment import DSecAgentEnvironment, EnvironmentAction
from .dsec_task_registry import EVALUATORS, environment_kind, task_adapter
from scheduled_dsec import ScheduledDSecClient, ScheduledOutcomeUnknown


_FENCE = re.compile(r"```bash[ \t]*\n(.*?)\n?```", re.DOTALL | re.IGNORECASE)


def _private_json(directory: Path, filename: str, value: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    destination = directory / filename
    fd, temporary = tempfile.mkstemp(prefix=".record-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        dirfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _persist_model_reply(rollout_id: str, step_id: int, reply: str,
                         finish_reason: str | None,
                         final_reply: str | None = None,
                         thinking_boundary_normalized: bool = False) -> None:
    root = os.getenv("DSEC_MODEL_OUTPUT_DIR")
    if not root:
        return
    _private_json(Path(root) / rollout_id, f"{step_id:04d}.json",
                  {"rollout_id": rollout_id, "step_id": step_id,
                   "finish_reason": finish_reason, "reply": reply,
                   "final_reply": final_reply,
                   "thinking_end_tag_count": reply.count("</think>"),
                   "thinking_boundary_normalized": thinking_boundary_normalized})


async def _capture_tito(policy, rollout_id: str, turns: int, *, fetch=None) -> str | None:
    root = os.getenv("DSEC_TITO_AUDIT_DIR")
    if not root or not turns:
        return None
    base_url = str(policy.base_url).rstrip("/")
    if not base_url.endswith("/v1") or "/sessions/" not in base_url:
        raise ValueError("Miles policy URL is not a session-scoped endpoint")
    session_url = base_url[:-3]
    if fetch is None:
        import httpx
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(session_url)
            response.raise_for_status()
            payload = response.json()
    else:
        payload = await fetch(session_url)
    records = payload.get("records")
    if not isinstance(records, list) or len(records) != turns:
        raise ValueError("Miles TITO session record count does not match model turns")
    audit_records = []
    for index, record in enumerate(records):
        request = record.get("request") or {}
        choice = (record.get("response") or {}).get("choices", [{}])[0]
        meta = choice.get("meta_info") or {}
        input_ids = request.get("input_ids")
        token_logprobs = meta.get("output_token_logprobs")
        if not isinstance(input_ids, list) or not isinstance(token_logprobs, list):
            raise ValueError("Miles TITO record lacks input or output token IDs")
        if meta.get("completion_tokens") != len(token_logprobs):
            raise ValueError("Miles TITO output-token count mismatch")
        audit_records.append({
            "step_id": index,
            "request": {"messages": request.get("messages"),
                        "input_ids": input_ids,
                        "sampling": {key: value for key, value in request.items()
                                     if key not in ("messages", "input_ids")}},
            "response": {"message": choice.get("message"),
                         "finish_reason": choice.get("finish_reason"),
                         "output_token_logprobs": token_logprobs,
                         "usage": (record.get("response") or {}).get("usage")},
        })
    directory = Path(root) / rollout_id
    _private_json(directory, "tito.json", {
        "rollout_id": rollout_id, "session_id": payload.get("session_id"),
        "metadata": payload.get("metadata", {}), "records": audit_records,
    })
    return str(directory / "tito.json")


def _strip_fence(reply: str) -> str | None:
    # Match Miles' OpenEnv loop: one model turn schedules the first fenced
    # action, then the environment result is sent back before the next turn.
    # Only inspect the final channel, never a Qwen thinking segment.
    match = _FENCE.search(reply)
    return match.group(1).strip() if match else None


def _final_reply(reply: str, reasoning_content: str | None = None) -> str | None:
    """Read the final channel when Miles preserves Qwen's thinking in content.

    Qwen3.5's template prefills ``<think>`` in the input tokens, so the model
    output may contain only ``</think>`` before its final answer. Keep the raw
    reply in TITO and model_outputs; only the final answer can become an action.
    """
    if reasoning_content:
        return reply
    if os.getenv("DSEC_QWEN35_THINKING") != "1":
        return reply
    if "</think>" not in reply:
        return reply
    parts = reply.split("</think>")
    # Qwen's official template takes the answer after the LAST closing tag.
    # Accept repeated prose boundaries, but never discard a candidate action
    # between tags or split a closing tag embedded inside a fenced command.
    if any("```" in part or "<tool_call>" in part or
           part.strip() == "TASK_COMPLETE" for part in parts[1:-1]):
        return None
    final = parts[-1].strip()
    return final if "<think>" not in final else None


async def run_episode(policy, model_name: str, messages: list[dict],
                      request_kwargs: dict, metadata: dict,
                      *, client_factory=ScheduledDSecClient, policy_call=None,
                      tito_fetch=None):
    task_id = metadata.get("task_id") or metadata.get("task_name")
    if not isinstance(task_id, str):
        raise ValueError("Miles metadata must include a task_id")
    kind = environment_kind(metadata)
    rollout_id = metadata.get("dsec_rollout_id") or uuid.uuid4().hex
    if not isinstance(rollout_id, str) or not re.fullmatch(r"[0-9a-f]{32}", rollout_id):
        raise ValueError("dsec_rollout_id must be 32 lowercase hex characters")
    socket_path = os.environ["DSEC_ROLLOUT_WORKER_SOCKET"]
    adapter = task_adapter(kind)
    max_turns = int(os.getenv("OPENENV_MAX_TURNS", "30"))
    if max_turns < 1:
        raise ValueError("OPENENV_MAX_TURNS must be positive")
    command_timeout_ms = int(os.getenv(
        "DSEC_AGENT_STEP_TIMEOUT_MS", "30000" if kind == "counter" else "120000"))
    if command_timeout_ms < 1000:
        raise ValueError("DSEC_AGENT_STEP_TIMEOUT_MS must be at least 1000")
    if kind == "counter" and command_timeout_ms > 30000:
        raise ValueError("Counter task command timeout cannot exceed 30000 ms")
    budget_seconds = float(os.getenv("OPENENV_MAX_ROLLOUT_TIME_SECONDS", "3600"))
    if not math.isfinite(budget_seconds) or budget_seconds <= 0:
        raise ValueError("Episode budget must be finite and positive")
    registry = os.getenv("DSEC_EPISODE_REGISTRY_DIR")
    if registry:
        # Record ownership before creating a sandbox, including runs interrupted
        # before their first model response. Cleanup must never scan all work.
        _private_json(Path(registry), rollout_id + ".json", {
            "rollout_id": rollout_id, "task_id": task_id,
            "dsec_environment": kind, "worker_socket": socket_path})
    gen_times, tool_times = [], []
    normalized_thinking_steps = []
    reset_started = time.monotonic()
    async with client_factory(socket_path) as client:
        episode = DSecAgentEnvironment(client, adapter, task_id, rollout_id)
        context = await episode.reset(messages)
        resumed = bool(context["next_step"] or context.get("pending") or
                       context["state"] == "COMPLETED")
        reset_time = time.monotonic() - reset_started
        # Waiting for a scheduled sandbox does not spend the task's agent budget.
        agent_started = time.monotonic()
        deadline = agent_started + budget_seconds
        end_reason = "max_turns"
        keep_alive = False
        try:
            while context["next_step"] < max_turns and context["state"] != "COMPLETED":
                if time.monotonic() >= deadline:
                    end_reason = "episode_timeout"
                    break
                step_id = context["next_step"]
                t0 = time.monotonic()
                if policy_call is None:
                    completion = await policy.chat.completions.create(
                        model=model_name, messages=context["messages"],
                        extra_body=request_kwargs)
                else:
                    completion = await policy_call(
                        policy, model_name, context["messages"], request_kwargs)
                gen_times.append(time.monotonic() - t0)
                choice = completion.choices[0]
                reply = choice.message.content or ""
                reasoning_content = getattr(choice.message, "reasoning_content", None)
                final_reply = _final_reply(reply, reasoning_content)
                normalized = (os.getenv("DSEC_QWEN35_THINKING") == "1" and
                              not reasoning_content and reply.count("</think>") > 1
                              and final_reply is not None)
                if normalized:
                    normalized_thinking_steps.append(step_id)
                _persist_model_reply(rollout_id, step_id, reply,
                                     choice.finish_reason, final_reply, normalized)
                if time.monotonic() >= deadline:
                    # Keep the completed response in TITO but execute no action
                    # after budget expiration. Do not cancel away token evidence.
                    end_reason = "episode_timeout"
                    break
                if choice.finish_reason == "length":
                    end_reason = "length"
                    break
                if final_reply is None:
                    end_reason = "invalid_thinking_format"
                    break
                if final_reply.strip() == "TASK_COMPLETE":
                    end_reason = "task_complete"
                    break
                command = _strip_fence(final_reply)
                if not command:
                    end_reason = "invalid_format"
                    break
                assistant_message = choice.message.model_dump(exclude_none=True)
                # Echo the original response into the next model request, as
                # Miles does. Rewriting content to the extracted command breaks
                # session prefix matching and changes the multi-turn context.
                t0 = time.monotonic()
                remaining_ms = int((deadline - t0) * 1000)
                if remaining_ms < 1000:
                    # Wait only for the final sub-second budget; the shell API
                    # cannot express a shorter timeout. No command is submitted.
                    await asyncio.sleep(max(0, deadline - time.monotonic()))
                    end_reason = "episode_timeout"
                    break
                try:
                    observation = await episode.step(
                        EnvironmentAction.shell(
                            step_id=step_id, action_id=f"turn-{step_id}",
                            command=command, timeout_ms=min(command_timeout_ms, remaining_ms)),
                        policy_message=assistant_message)
                    context = observation.dialogue
                except ScheduledOutcomeUnknown:
                    # A long command may complete after its RPC response is
                    # lost. Read the durable step result; never submit the
                    # same command a second time from this policy session.
                    context = await episode.dialogue()
                    if context["next_step"] != step_id + 1:
                        raise
                tool_times.append(time.monotonic() - t0)
            agent_elapsed = time.monotonic() - agent_started
            if agent_elapsed >= budget_seconds:
                end_reason = "episode_timeout"
            eval_started = time.monotonic()
            verdict = None if end_reason == "episode_timeout" else await episode.evaluate()
            eval_time = time.monotonic() - eval_started
            reward = 0.0 if verdict is None else verdict.score
            tito_path = await _capture_tito(
                policy, rollout_id, len(gen_times), fetch=tito_fetch)
        except (ScheduledOutcomeUnknown, asyncio.CancelledError):
            keep_alive = True
            raise
        finally:
            if not keep_alive:
                await episode.stop()
    metrics = {
        "dsec_environment": kind,
        "rollout_id": rollout_id, "sandbox_id": episode.sandbox.sandbox_id,
        "resumed_after_trainer_exit": resumed,
        "turns": len(gen_times), "end_reason": end_reason,
        "budget_seconds": budget_seconds, "agent_elapsed_seconds": agent_elapsed,
        "budget_overrun_seconds": max(0.0, agent_elapsed - budget_seconds),
        "trajectory_complete": bool(tito_path and gen_times and not resumed),
        "reward_source": "episode_budget" if end_reason == "episode_timeout" else "task_verifier",
        "format_valid": end_reason not in ("invalid_format", "invalid_thinking_format"),
        "tool_calls": len(tool_times), "gen_times": gen_times,
        "normalized_thinking_steps": normalized_thinking_steps,
        "tool_times": tool_times, "reset_time": reset_time,
        "eval_time": eval_time,
        "verifier_diagnostic": {"raw_reward": verdict.score if verdict else None,
                                "harness": verdict.evaluator if verdict else None,
                                "skipped": end_reason == "episode_timeout", "error": None},
        "total_gen_time": sum(gen_times),
        "total_tool_time": sum(tool_times) + reset_time + eval_time,
        "tito_path": tito_path,
    }
    # The previous Miles session's token/logprob trail is unavailable after a
    # process restart. Preserve sandbox correctness, but never train on the
    # incomplete policy trajectory.
    # A response cut off by generation length cannot be treated as a completed
    # agent action, even if the official verifier observes an unchanged guest.
    # An unrecognized but complete final response is a policy-format error,
    # not a harness failure. Keep the real verifier score (including zero)
    # and the format diagnostic; otherwise sampling silently discards correct
    # solutions and ordinary failed actions. Never execute unparsed text.
    training_reward = (None if resumed or end_reason in ("length", "invalid_thinking_format")
                       else reward)
    if registry:
        _private_json(Path(registry), rollout_id + ".json", {
            "rollout_id": rollout_id, "task_id": task_id,
            "dsec_environment": kind, "worker_socket": socket_path,
            "training_result": {
                "reward": training_reward,
                "exit_status": ("incomplete" if training_reward is None else
                                "timeout" if end_reason == "episode_timeout" else "completed"),
                "agent_metrics": metrics}})
    return training_reward, metrics


async def run(base_url: str, prompt: Any, request_kwargs: dict | None = None,
              metadata: dict | None = None, **kwargs):
    """Miles ``--custom-agent-function-path`` entry point."""
    import importlib
    oaf = importlib.import_module(".openenv_agent_function", __package__)
    result = await oaf.run_for_training(
        base_url, prompt, request_kwargs, metadata, run_episode,
        manages_episode_budget=True)
    return canonical_training_result(result, metadata)


def canonical_training_result(result, metadata):
    """Apply the same complete-verdict gate to ordinary and instrumented runners."""
    kind = environment_kind(metadata)
    from .miles_dsec_generate import valid_budget_timeout
    if valid_budget_timeout(result, kind):
        result["dsec_budget_verdict"] = True
        return result
    # Current Miles treats None as "no metadata", not "abort sample". A
    # companion generate wrapper checks this explicit marker and aborts every
    # sample lacking a validated, complete DSec episode.
    if (not isinstance(result, dict) or result.get("exit_status") != "completed" or
            result.get("reward") not in (0.0, 1.0) or
            result.get("agent_metrics", {}).get("resumed_after_trainer_exit") or
            result.get("agent_metrics", {}).get("dsec_environment", "tb2") != kind or
            result.get("agent_metrics", {}).get("verifier_diagnostic", {}).get("harness")
            != EVALUATORS[kind]):
        return None
    result["dsec_canonical_verdict"] = True
    return result
