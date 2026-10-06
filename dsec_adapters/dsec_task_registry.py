"""Explicit task-plugin selection shared by Miles and its reward guard."""

from __future__ import annotations

from .counter_dsec_environment import CounterDSecEnvironment
from .tb2_dsec_environment import TB2DSecEnvironment


EVALUATORS = {"tb2": "tests/test.sh", "counter": "counter-exact-value"}


def environment_kind(metadata: dict | None) -> str:
    kind = (metadata or {}).get("dsec_environment", "tb2")
    if kind not in EVALUATORS:
        raise ValueError("Unsupported DSec task environment")
    return kind


def task_adapter(kind: str):
    if kind == "tb2":
        return TB2DSecEnvironment.from_environment()
    if kind == "counter":
        return CounterDSecEnvironment()
    raise ValueError("Unsupported DSec task environment")
