"""Versioned cluster envelopes; no host operations or control dependencies."""
from dataclasses import asdict, dataclass
import hashlib
import hmac
import math
import re


PROTOCOL = 'dsec-cluster-v1'
PERMISSIONS = frozenset(('sandbox.create', 'sandbox.read', 'sandbox.execute',
    'sandbox.stop', 'sandbox.snapshot', 'iam.delegate', 'iam.policy', 'iam.credentials'))


class AuthorizationDenied(PermissionError):
    pass


class CoordinationUnavailable(RuntimeError):
    """No quorum result; protected mutations must fail closed."""


class QuotaExceeded(RuntimeError):
    pass


class NoPlacement(RuntimeError):
    pass


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', value):
        raise ValueError('Invalid cluster identifier')
    return value


def request_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ValueError('Invalid cluster request ID')
    return value


def node_credential(route_key, node):
    """Internal gateway credential; mTLS remains independently mandatory."""
    identifier(node)
    if not isinstance(route_key, bytes) or len(route_key) < 32:
        raise ValueError('Strong cluster key required')
    return hmac.new(route_key, ('dsec-node-auth-v1:'+node).encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Quota:
    concurrency: int
    cpu: float
    memory_mb: int
    disk_mb: int
    network_mbps: float

    def __post_init__(self):
        for name, value in asdict(self).items():
            if (isinstance(value, bool) or not isinstance(value, (float, int))
                    or not math.isfinite(value) or value < 0):
                raise ValueError('Invalid quota: ' + name)
            if name in ('concurrency', 'memory_mb', 'disk_mb') and type(value) is not int:
                raise ValueError('Integer quota required: ' + name)

    def fits(self, limit):
        return all(value <= getattr(limit, name) for name, value in asdict(self).items())

    def plus(self, other):
        return Quota(**{name:value + getattr(other, name) for name,value in asdict(self).items()})

    def minus(self, other):
        return Quota(**{name:value - getattr(other, name) for name,value in asdict(self).items()})


ZERO_QUOTA = Quota(0, 0, 0, 0, 0)


@dataclass(frozen=True)
class SandboxRoute:
    project: str
    node: str
    backend: str
    local_id: str
    admission_id: str

    def __post_init__(self):
        identifier(self.project); identifier(self.node); request_id(self.admission_id)
        if self.backend not in ('microvm', 'container') or not re.fullmatch(
                r'[0-9a-f]{12}' if self.backend == 'microvm' else r'[0-9a-f]{32}', self.local_id):
            raise ValueError('Invalid sandbox route')

    def encode(self, key):
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError('A strong route integrity key is required')
        body = '.'.join(('ds1', self.project, self.node, self.backend, self.local_id, self.admission_id))
        return body + '.' + hmac.new(key, body.encode(), hashlib.sha256).hexdigest()

    @classmethod
    def decode(cls, value, key):
        if not isinstance(value, str) or len(value) > 320:
            raise AuthorizationDenied('Invalid sandbox route')
        parts = value.split('.')
        if len(parts) != 7 or parts[0] != 'ds1':
            raise AuthorizationDenied('Invalid sandbox route')
        body = '.'.join(parts[:-1])
        signature = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, parts[-1]):
            raise AuthorizationDenied('Sandbox route integrity rejected')
        try:
            return cls(*parts[1:-1])
        except ValueError as exc:
            raise AuthorizationDenied('Invalid sandbox route') from exc


@dataclass(frozen=True)
class NodeView:
    node_id: str
    boot_id: str
    backends: tuple[str, ...]
    environments: tuple[str, ...]
    capabilities: tuple[str, ...]
    load: float
    available: Quota
    active: int

    def __post_init__(self):
        identifier(self.node_id)
        if not isinstance(self.boot_id, str) or not self.boot_id:
            raise ValueError('Missing node boot identity')
        if type(self.active) is not int or self.active < 0:
            raise ValueError('Invalid node active count')
        if (isinstance(self.load, bool) or not isinstance(self.load, (int, float))
                or not math.isfinite(self.load) or self.load < 0):
            raise ValueError('Invalid node load')
        for values in (self.backends, self.environments, self.capabilities):
            if not isinstance(values, tuple) or any(not isinstance(v, str) or not v for v in values):
                raise ValueError('Invalid node capability list')
