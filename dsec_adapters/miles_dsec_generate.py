"""Miles generate/reward guards for DSec TB2 episodes.

Use both ``--custom-generate-function-path miles_dsec_generate.generate`` and
``--custom-rm-path miles_dsec_generate.reward_func``. Miles' generic agentic
generator treats a None agent result as absent metadata, not an aborted sample;
without this guard its example reward function defaults missing rewards to 0.
"""

from __future__ import annotations

from dataclasses import replace
import os

from .dsec_task_registry import EVALUATORS


QWEN35_NONTHINKING_SAMPLING = {
    "temperature": 0.7,
    "top_p": 0.8,
    "top_k": 20,
    "min_p": 0.0,
    # Miles sampling-support replay cannot reproduce presence penalties.
    "presence_penalty": 0.0,
    "repetition_penalty": 1.0,
}

QWEN35_THINKING_CODING_SAMPLING = {
    "temperature": 0.6,
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 0.0,
    "repetition_penalty": 1.0,
}


def _verified(metadata: dict) -> bool:
    if not isinstance(metadata, dict):
        return False
    reward = metadata.get("reward")
    metrics = metadata.get("agent_metrics") or {}
    if not isinstance(metrics, dict):
        return False
    diagnostic = metrics.get("verifier_diagnostic") or {}
    if not isinstance(diagnostic, dict):
        return False
    kind = metrics.get("dsec_environment", "tb2")
    return (kind in EVALUATORS and
            metadata.get("dsec_canonical_verdict") is True and
            metadata.get("exit_status") == "completed" and
            type(reward) in (int, float) and reward in (0.0, 1.0) and
            diagnostic.get("harness") == EVALUATORS[kind] and
            not metrics.get("resumed_after_trainer_exit"))


def _iter_samples(samples):
    if isinstance(samples, list):
        for sample in samples:
            yield from _iter_samples(sample)
    else:
        yield samples


async def generate(input):
    from miles.rollout.generate_hub.agentic_tool_call import generate as base_generate
    from miles.utils.types import Sample

    if os.getenv("DSEC_QWEN35_THINKING_CODING_SAMPLING") == "1":
        if input.evaluation:
            raise ValueError("Qwen3.5 thinking coding profile is validated for rollout-only runs")
        input = replace(input, sampling_params={
            **input.sampling_params, **QWEN35_THINKING_CODING_SAMPLING})
    elif os.getenv("DSEC_QWEN35_NONTHINKING_SAMPLING") == "1":
        if input.evaluation:
            raise ValueError("Qwen3.5 sampling profile is validated for rollout-only runs")
        input = replace(input, sampling_params={
            **input.sampling_params, **QWEN35_NONTHINKING_SAMPLING})
    result = await base_generate(input)
    for sample in _iter_samples(result.samples):
        mismatches = (sample.metadata or {}).get("tito_session_mismatch") or []
        hard_mismatch = any(
            not isinstance(item, dict) or item.get("type") != "assistant_text"
            for item in mismatches)
        if not _verified(sample.metadata) or hard_mismatch:
            sample.status = Sample.Status.ABORTED
    return result


def _add_arguments(parser):
    from miles.rollout.generate_hub.agentic_tool_call import generate as base_generate
    return base_generate.add_arguments(parser)


generate.add_arguments = _add_arguments


async def reward_func(args, samples, **kwargs):
    from miles.utils.types import Sample

    def value(sample):
        if sample.status == Sample.Status.ABORTED:
            return 0.0  # Filtered by Miles before training, never a task verdict.
        if not _verified(sample.metadata):
            raise RuntimeError("Non-aborted DSec sample lacks a canonical task verdict")
        return float(sample.metadata["reward"])

    if isinstance(samples, list):
        return [value(sample) for sample in samples]
    return value(samples)
