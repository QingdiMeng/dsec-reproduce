"""API-only rollout client, sharing the existing episode/action SDK semantics."""
import json
import os
from pathlib import Path
import stat
import uuid
import time
import asyncio
import math

from dsec.contracts.rollout import ROLLOUT_FEATURE, ROLLOUT_READS
from dsec.contracts.cluster import PROTOCOL, request_id
from dsec.sdk.client import DSecClient
from dsec.sdk.rollout_transport import RolloutOutcomeUnknown, RolloutServiceError
from dsec.sdk.sandbox_transport import RequestOutcomeUnknown, ServiceError
from dsec.sdk.scheduled import ScheduledDSecClient, ScheduledOutcomeUnknown, ScheduledSandbox
from dsec._credentials import FileCredential


def api_configuration(path):
    """Read references, never embed a credential into trainer launch arguments."""
    path = Path(path).resolve(strict=True)
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or set(value) - {
            'endpoints','project','token_file','ca_file','cert_file','key_file','security_file'}
            or not {'endpoints','project','token_file'} <= set(value)):
        raise ValueError('API config requires endpoints, project and token_file')
    for name in ('token_file','ca_file','cert_file','key_file','security_file'):
        if value.get(name) is not None:
            target = Path(value[name])
            value[name] = str(target if target.is_absolute() else path.parent/target)
    value['token'] = FileCredential(value.pop('token_file'))
    return value


class RolloutAPITransport:
    def __init__(self, transport):
        self.api = transport

    def call(self, operation, *, timeout_s=None, **args):
        rid = args.get('rollout_id')
        operation_id = request_id(rid) if operation == 'create' else uuid.uuid4().hex
        payload = dict(project=self.api.project, request_id=operation_id,
            operation='health' if operation == 'health' else 'rollout.' + operation,
            sandbox_id=None, args=args)
        try:
            result = self.api.rpc.send(self.api.token, payload,
                readonly=operation == 'health' or operation in ROLLOUT_READS,
                timeout=60 if timeout_s is None else timeout_s)
        except RequestOutcomeUnknown as exc:
            raise RolloutOutcomeUnknown('API rollout outcome unknown; attach by episode ID') from exc
        except ServiceError as exc:
            raise RolloutServiceError(exc.kind, str(exc), exc.details) from exc
        if operation == 'health':
            if result.get('protocol') != PROTOCOL or ROLLOUT_FEATURE not in result.get('protocol_features', ()):
                raise RuntimeError('API Server does not support durable rollout routing')
        return result


class RolloutAPIClient(ScheduledDSecClient):
    """Application entry through API Server; no worker socket or local fallback."""
    def __init__(self, endpoints, *, token, project, ca_file=None, cert_file=None,
                 key_file=None, security_file=None, create_timeout_s=1810):
        if (isinstance(create_timeout_s, bool) or not isinstance(create_timeout_s,(int,float))
                or not math.isfinite(create_timeout_s) or not 0 < create_timeout_s <= 1810):
            raise ValueError('create_timeout_s must be in (0, 1810]')
        client = DSecClient(endpoints, token=token, project=project, ca_file=ca_file,
                            cert_file=cert_file, key_file=key_file,security_file=security_file)
        self._transport = RolloutAPITransport(client._transport)
        self._create_timeout_s = create_timeout_s
        self._opened = False

    @classmethod
    def from_config(cls, path):
        return cls(**api_configuration(path))

    async def open(self):
        await asyncio.to_thread(self._transport.call, 'health')
        self._opened = True
        return self

    async def recover_create(self, rollout_id):
        """Explicit recovery by original ID; never supply replacement arguments.

        The owning Node permits a first dispatch only while the durable receipt
        is ADMITTED. Once dispatched, it queries the same worker without resend.
        This method is a protected mutation and is never automatically retried.
        """
        self._require_open()
        request_id(rollout_id)
        try:
            view = await asyncio.to_thread(self._transport.call, 'recover_create',
                rollout_id=rollout_id, timeout_s=self._create_timeout_s)
        except RolloutOutcomeUnknown as exc:
            raise ScheduledOutcomeUnknown(rollout_id, 'recover_create') from exc
        return ScheduledSandbox(self._transport, view)

    async def create(self, **args):
        # A fresh watcher can still report the just-released slot as occupied.
        # NoPlacement is a definitive non-admission, so retry the same episode
        # identity. Transport loss/UNKNOWN and all other errors are never retried.
        maximum=args.pop('timeout_s',self._create_timeout_s)
        if isinstance(maximum,bool) or not isinstance(maximum,(int,float)) or not math.isfinite(maximum) or maximum<=0:
            raise ValueError('Create timeout must be finite and positive')
        deadline = time.monotonic() + min(self._create_timeout_s,maximum)
        delay = .05
        while True:
            remaining=deadline-time.monotonic()
            if remaining<=0:
                raise TimeoutError('Rollout placement remained unavailable before admission')
            try:
                return await super().create(timeout_s=remaining,**args)
            except RolloutServiceError as exc:
                if exc.kind != 'NoPlacement':
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Rollout placement remained unavailable before admission') from exc
                await asyncio.sleep(min(delay, remaining))
                delay = min(delay * 2, 1)

    @classmethod
    def from_environment(cls):
        path = os.environ.get('DSEC_API_CONFIG')
        if not path:
            raise ValueError('DSEC_API_CONFIG required; worker sockets are development-only')
        return cls.from_config(path)
