"""Explicit same-host development SDK; never selected by the product client."""
import os
from pathlib import Path

from dsec.sdk.client import (_SandboxClient, DSecSandbox, DSecContainerSandbox,
    DSecTB2Sandbox, DSecMicroVMRunArgs, DSecContainerRunArgs, DSecTB2RunArgs,
    UnsupportedCapability)
from dsec.sdk.sandbox_transport import SandboxClient, ServiceError
from dsec.sdk.scheduled import ScheduledDSecClient, ScheduledSandbox, ScheduledOutcomeUnknown


class DevEdgeClient(_SandboxClient):
    """Private Edge UDS for developer tests and node-local worker composition."""
    def __init__(self, socket_path: str | Path | None = None):
        socket_path = socket_path or os.environ.get('DSEC_SOCKET')
        if not socket_path:
            raise ValueError('Development socket_path or DSEC_SOCKET is required')
        self._transport = SandboxClient(socket_path)
        self._opened = False
        self._features = set()


# Preserve old explicitly imported compatibility modules, not the public SDK
# default. Existing node-local workers and historical tests keep their identity.
DSecClient = DevEdgeClient
