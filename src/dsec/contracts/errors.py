"""Runtime error identities shared by Edge components and legacy imports."""


class SandboxError(RuntimeError):
    pass


class ServiceBusy(SandboxError):
    """The sandbox has an active operation; this request was not admitted."""
    pass


class CommandOutcomeUnknown(SandboxError):
    """Transport failed; the command may have had side effects. Never auto-replay."""
