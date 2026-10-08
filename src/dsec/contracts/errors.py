"""Runtime error identities shared by Edge components and legacy imports."""


class SandboxError(RuntimeError):
    pass


class ServiceBusy(SandboxError):
    """The sandbox has an active operation; this request was not admitted."""
    pass


class CommandOutcomeUnknown(SandboxError):
    """Transport failed; the command may have had side effects. Never auto-replay."""


class RequestOutcomeUnknown(RuntimeError):
    def __init__(self, message, request_id=None):
        super().__init__(message)
        self.request_id=request_id
