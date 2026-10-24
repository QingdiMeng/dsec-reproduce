"""Shared RPC failure identities; no sockets, retries or SDK dependency."""


class ServiceError(RuntimeError):
    def __init__(self, kind, message, details=None):
        super().__init__(message); self.kind=kind; self.details=details or {}


class RolloutServiceError(RuntimeError):
    def __init__(self, kind, message, details=None):
        super().__init__(message)
        self.kind = kind
        self.details = details or {}


class RolloutOutcomeUnknown(RuntimeError):
    """Connection failed after submission; query by rollout/step before retrying."""
