# Derived from radixark/miles; Copyright 2025 Zhipu AI, Apache License 2.0.
# See THIRD_PARTY_NOTICES.md and licenses/miles-APACHE-2.0.txt.
"""Framework policy session wiring, independent of any benchmark or env server."""
import asyncio
import logging
import os
from collections.abc import Callable
from typing import Any
from openai import AsyncOpenAI

try:
    from miles.rollout.agentic.session import openai_session_url
except ImportError:
    def openai_session_url(base_url: str) -> str:
        return base_url.rstrip("/") + "/v1"

logger = logging.getLogger(__name__)
_MAX_ROLLOUT_TIME_S = float(os.getenv("OPENENV_MAX_ROLLOUT_TIME_SECONDS", "3600"))


def _extract_messages(prompt: Any) -> list[dict[str, str]]:
    """Accept either a chat-message list or a raw string prompt."""
    if isinstance(prompt, list):
        return list(prompt)
    return [{"role": "user", "content": str(prompt)}]


async def run_for_training(
    base_url: str,
    prompt: Any,
    request_kwargs: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
    run_episode_fn: Callable[..., Any],
    *, manages_episode_budget: bool = False,
) -> dict[str, Any] | None:
    """miles-side wrapper around one episode: session-server policy wiring plus
    training failure semantics (timeout -> reward 0, no verdict -> drop sample).

    Shared by every agent-function module: each passes its own run_episode.
    """
    request_kwargs = request_kwargs or {}
    metadata = metadata or {}

    session_url = openai_session_url(base_url)
    model_name = os.getenv("AGENT_MODEL_NAME", os.getenv("SWE_AGENT_MODEL_NAME", "model"))

    policy = AsyncOpenAI(base_url=session_url, api_key="EMPTY")
    messages = _extract_messages(prompt)

    try:
        # Hard wall-clock cap: cancel the episode if it overruns and score it 0.
        # wait_for cancels the coroutine, so any in-flight policy call / env.step
        # is interrupted and the env session is closed by the env context manager
        # during cancellation cleanup.
        episode = run_episode_fn(policy, model_name, messages, request_kwargs, metadata)
        # DSec owns budget termination so the last policy response and its real
        # token/logprob record survive. Other providers retain the legacy cap.
        if manages_episode_budget:
            reward, agent_metrics = await episode
        else:
            reward, agent_metrics = await asyncio.wait_for(
                episode, timeout=_MAX_ROLLOUT_TIME_S)
    except asyncio.TimeoutError:
        if manages_episode_budget:
            logger.error("DSec transport/inference timed out before a settled budget verdict; dropping sample")
            return None
        logger.warning(f"Agent episode exceeded {_MAX_ROLLOUT_TIME_S:.0f}s; " "terminating with reward 0")
        # eval_report empty: the episode was cancelled before evaluate ever
        # ran, so there is no pytest report to surface.
        return {
            "reward": 0.0,
            "exit_status": "timeout",
            "eval_report": {},
            "agent_metrics": {"timed_out": 1},
        }
    except Exception as e:
        logger.error(f"Agent episode failed: {e}", exc_info=True)
        return None
    finally:
        await policy.close()

    # No canonical verdict (infra/harness failure or a non-canonical server,
    # not a legitimate task failure). Drop the
    # sample: returning it as reward 0.0 would inject a false negative into
    # training.
    if reward is None:
        logger.warning("Agent episode produced no canonical reward; dropping sample")
        return None

    # The trainer consumes the scalar reward and metrics; the environment
    # retains its own detailed scoring evidence.
    return {
        "reward": reward,
        "exit_status": "timeout" if agent_metrics.get("end_reason") == "episode_timeout" else "completed",
        "eval_report": {},
        "agent_metrics": agent_metrics,
    }



def __getattr__(name):
    # The old OpenEnv module exposed these names. Resolve its task-specific
    # API only when explicitly accessed, without importing tasks for Miles.
    if name not in {
            "load_tbench2", "_purge_trial_dirs", "_is_retryable_env_error",
            "_CAPACITY_BACKOFF_S", "multi_turn", "_obs_field", "run_episode",
            "run", "_FENCE_RE", "_CAPACITY_MAX_WAIT_S", "MESSAGE_TIMEOUT_S",
            "_strip_fence", "_with_env", "_obs_info", "_shared_run_body",
            "_DEFAULT_ENV_URL", "_OBS_CHAR_CAP", "TB2_AGENT_SYSTEM_PROMPT"}:
        raise AttributeError(name)
    from dsec.compat.applications import require_tb21
    return getattr(require_tb21("openenv_agent_function"), name)
