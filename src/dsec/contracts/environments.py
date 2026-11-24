"""Published execution claims bound to one immutable environment description.

An operator/build claim is not a live guest probe or a security certification.
Only verified catalogs produce these values; applications cannot publish them
through a sandbox creation request.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import re

from dsec.contracts.native import execution_requirements


ENVIRONMENT_REQUIREMENTS_FEATURE = 'environment-requirements-v1'


def artifact_revision(descriptor):
    """Deployment identity, excluding its claim; paths/policy remain significant.

    This is deliberately not an OCI digest or cross-host content-dedup key.
    No descriptor or host path is returned to application clients.
    """
    if not isinstance(descriptor, dict):
        raise ValueError('Environment descriptor must be an object')
    data = {key:value for key,value in descriptor.items() if key != 'execution_contract'}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class EnvironmentView:
    environment_id: str
    backend: str
    execution_mode: str
    revision: str
    capabilities: tuple[str, ...] = ()

    def __post_init__(self):
        if (not isinstance(self.environment_id, str) or
                not re.fullmatch('[a-z0-9][a-z0-9.-]{0,127}', self.environment_id)):
            raise ValueError('Invalid environment identity')
        if ((self.backend, self.execution_mode) not in
                (('microvm', 'microvm'), ('container', 'container'), ('container', 'fncall')) or
                not isinstance(self.revision, str) or not re.fullmatch('[a-f0-9]{64}', self.revision)):
            raise ValueError('Invalid environment backend/mode/revision')
        if (not isinstance(self.capabilities, tuple) or
                execution_requirements(self.capabilities) != self.capabilities):
            raise ValueError('Canonical environment execution capabilities required')
        if self.execution_mode == 'fncall' and self.capabilities:
            raise ValueError('Function invocations do not expose native sessions/files')

    def as_dict(self):
        return dict(asdict(self), capabilities=list(self.capabilities))

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {
                'environment_id', 'backend', 'execution_mode', 'revision', 'capabilities'}:
            raise ValueError('Explicit environment contract fields required')
        if not isinstance(value['capabilities'], (list, tuple)):
            raise ValueError('Environment capabilities must be an array')
        return cls(**dict(value, capabilities=tuple(value['capabilities'])))


def published_environment(environment_id, backend, mode, descriptor):
    """Validate a trusted, artifact-bound claim; absence means unknown capability."""
    revision = artifact_revision(descriptor)
    claim = descriptor.get('execution_contract')
    capabilities = ()
    if claim is not None:
        if (not isinstance(claim, dict) or set(claim) != {'schema', 'artifact_revision', 'capabilities'} or
                type(claim['schema']) is not int or claim['schema'] != 1 or
                claim['artifact_revision'] != revision):
            raise ValueError('Execution claim does not match the pinned environment artifacts')
        capabilities = execution_requirements(claim['capabilities'])
    # Include the claim too: changing the supported protocol is an environment
    # revision change, even when the underlying artifact pins stay the same.
    identity = hashlib.sha256(json.dumps(descriptor, sort_keys=True, separators=(',', ':'),
                                        allow_nan=False).encode()).hexdigest()
    return EnvironmentView(environment_id, backend, mode, identity, capabilities)


def check_environment_binding(binding, candidates, backend, environment_id):
    """Check the fixed deployment identity, permitting only supported features."""
    expected = EnvironmentView.from_dict(binding)
    actual = next((item for item in candidates if
                   (item.backend, item.environment_id) == (backend, environment_id)), None)
    if (actual is None or (expected.backend, expected.environment_id) != (backend, environment_id) or
            expected.execution_mode != actual.execution_mode or expected.revision != actual.revision or
            not set(expected.capabilities) <= set(actual.capabilities)):
        raise ValueError('Selected environment revision/capabilities no longer match')
    return expected
