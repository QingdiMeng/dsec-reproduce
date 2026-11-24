"""Shared values for bounded shell execution, independent of runtime and trainer.

A request ID is an optional backend journal capability. The current microVM
wire protocol has no such field; Edge owns its request identity and journal.
Absent timeout evidence stays absent in legacy container results.
"""
from dataclasses import dataclass
from typing import Protocol, TypedDict


DEFAULT_COMMAND_TIMEOUT_MS = 30000
MAX_COMMAND_TIMEOUT_MS = 300000


def command_timeout(value):
    if type(value) is not int or not 1 <= value <= MAX_COMMAND_TIMEOUT_MS:
        raise ValueError('Command timeout ceiling must be 1..300000 milliseconds')
    return value


@dataclass(frozen=True)
class ShellRequest:
    """Value only; the Edge validates backend limits before dispatch."""
    command: str
    timeout_ms: int = 5000
    output_limit: int = 65536
    request_id: str | None = None


class _RequiredShellResult(TypedDict):
    exit_code: int
    output: str
    truncated: bool


class ShellResult(_RequiredShellResult, total=False):
    timed_out: bool


class CommandChannel(Protocol):
    def execute(self, request: ShellRequest) -> ShellResult: ...
