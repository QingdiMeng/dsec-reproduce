"""Shared values for bounded shell execution, independent of runtime and trainer.

A request ID is an optional backend journal capability. The current microVM
wire protocol has no such field; Edge owns its request identity and journal.
Absent timeout evidence stays absent in legacy container results.
"""
from dataclasses import dataclass
from typing import NotRequired, Protocol, TypedDict


@dataclass(frozen=True)
class ShellRequest:
    """Value only; the Edge validates backend limits before dispatch."""
    command: str
    timeout_ms: int = 5000
    output_limit: int = 65536
    request_id: str | None = None


class ShellResult(TypedDict):
    exit_code: int
    output: str
    truncated: bool
    timed_out: NotRequired[bool]


class CommandChannel(Protocol):
    def execute(self, request: ShellRequest) -> ShellResult: ...
