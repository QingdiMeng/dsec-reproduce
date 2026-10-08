"""Dispatch shell commands through the existing Sandbox state and lock owner."""
from dsec.contracts.errors import SandboxError, CommandOutcomeUnknown
from dsec.contracts.execution import ShellResult


class ShellDispatcher:
    def __init__(self, *, proxy_command):
        self.proxy_command = proxy_command

    def execute(self, sandbox, command, timeout_ms=5000, output_limit=65536,
                execution_scope="agent") -> ShellResult:
        # Validate before any lifecycle side effect, including automatic resume.
        if not isinstance(command, str) or "\0" in command or len(command.encode()) > 65536:
            raise ValueError("Invalid command")
        execution_command = self.proxy_command(
            command, getattr(sandbox.manager, "egress_proxy_url", None),
            getattr(sandbox.manager, "egress_proxy_bypass_hosts", ()))
        if len(execution_command.encode()) > 65536:
            raise ValueError("Command exceeds guest limit after proxy environment")
        if execution_scope not in ("agent", "verifier"):
            raise ValueError("Unknown command execution scope")
        max_timeout_ms = (sandbox.manager.verifier_timeout_ms(sandbox.environment_id)
                          if execution_scope == "verifier" else
                          sandbox.manager.command_timeout_ms(sandbox.environment_id))
        if (not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or
                not 1 <= timeout_ms <= max_timeout_ms):
            raise ValueError(f"{execution_scope} timeout_ms={timeout_ms!r} exceeds "
                             f"allowed range 1..{max_timeout_ms}")
        if (not isinstance(output_limit, int) or isinstance(output_limit, bool) or
                not 1 <= output_limit <= 1048576):
            raise ValueError("output_limit must be an integer in 1..1048576")
        with sandbox.lock:
            if sandbox.reserved:
                raise SandboxError("Sandbox is reserved for warm checkout")
            sandbox._check()
            if sandbox.state == "PAUSED":
                sandbox._restore()
            if sandbox.state != "RUNNING":
                raise SandboxError("Cannot execute in "+sandbox.state)
            try:
                return sandbox.vm.execute(execution_command, timeout_ms, output_limit)
            except (OSError, EOFError, ValueError) as exc:
                sandbox._fail("command_transport_failed")
                raise CommandOutcomeUnknown("Command not replayed; inspect snapshot/side effects") from exc
            finally:
                sandbox._touch()
