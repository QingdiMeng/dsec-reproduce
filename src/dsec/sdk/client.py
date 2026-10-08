"""Async libdsec-style client; all host lifecycle work belongs to Edge."""
from __future__ import annotations
import asyncio
from dataclasses import asdict
import os
from pathlib import Path
from dsec.contracts.sandbox import (UnsupportedCapability, DSecMicroVMRunArgs,
                                   DSecContainerRunArgs, DSecTB2RunArgs)
from dsec.sdk.sandbox_transport import SandboxClient, ServiceError
from dsec.sdk.scheduled import ScheduledDSecClient, ScheduledOutcomeUnknown


class DSecSandbox:
    def __init__(self, transport: SandboxClient, sandbox_id: str):
        self._transport = transport
        self.id = sandbox_id
        self.backend = "microvm"

    async def seal_baseline(self, *, allow_prepared_state=False, request_id=None):
        return await asyncio.to_thread(self._transport.call, "seal_baseline", self.id,
                                       allow_prepared_state=allow_prepared_state, request_id=request_id)

    async def status(self):
        return await self._call_when_ready("status")

    async def _call_when_ready(self, operation: str, *, request_id: str | None = None,
                               **args):
        # ServiceBusy proves non-admission. Never replace a caller's durable
        # request ID behind its journal; unidentified calls may wait and retry.
        for attempt in range(35):
            try:
                return await asyncio.to_thread(
                    self._transport.call, operation, self.id,
                    request_id=request_id, **args)
            except ServiceError as exc:
                if exc.kind != "ServiceBusy" or request_id is not None or attempt == 34:
                    raise
                await asyncio.sleep(min(0.05 * (2 ** attempt), 1.0))

    async def run_shell(self, command: str, *, timeout_ms: int = 5000, output_limit: int = 65536,
                        request_id: str | None = None):
        return await self._call_when_ready(
            "execute", request_id=request_id, command=command,
            timeout_ms=timeout_ms, output_limit=output_limit)

    async def run_verifier_shell(self, command: str, *, timeout_ms: int,
                                 output_limit: int = 65536,
                                 request_id: str | None = None):
        """Host evaluator only; ordinary agent actions keep their command limit."""
        return await self._call_when_ready(
            "execute", request_id=request_id, command=command,
            timeout_ms=timeout_ms, output_limit=output_limit,
            execution_scope="verifier")

    async def pause(self, *, request_id: str | None = None):
        return await asyncio.to_thread(self._transport.call, "pause", self.id,
                                       request_id=request_id)

    async def resume(self):
        return await asyncio.to_thread(self._transport.call, "resume", self.id)

    async def stop(self, *, request_id: str | None = None):
        return await self._call_when_ready("stop", request_id=request_id)


class DSecClient:
    def __init__(self, socket_path: str | Path | None = None):
        if socket_path is None:
            socket_path = os.environ.get("DSEC_SOCKET")
        if not socket_path:
            raise ValueError("socket_path or DSEC_SOCKET is required")
        self._transport = SandboxClient(socket_path)
        self._opened = False
        self._features = set()

    async def open(self):
        health = await asyncio.to_thread(self._transport.call, "health")
        self._features = set(health.get("protocol_features", []))
        self._opened = True
        return self

    async def close(self):
        # Closing the trainer-side client must not stop a live rollout sandbox.
        self._opened = False

    async def __aenter__(self):
        return await self.open()

    async def __aexit__(self, *_):
        await self.close()

    def _require_open(self):
        if not self._opened:
            raise RuntimeError("DSecClient.open() must be called first")

    def _require_container_service(self):
        self._require_open()
        if "container-rpc-v1" not in self._features:
            raise UnsupportedCapability("Upgrade sandbox service for container-rpc-v1")

    async def run_microvm(self, args: DSecMicroVMRunArgs | None = None, *, timeout: float | None = None,
                          request_id: str | None = None):
        self._require_open()
        if args is None:
            args = DSecMicroVMRunArgs()
        if not isinstance(args, DSecMicroVMRunArgs):
            raise TypeError("run_microvm requires DSecMicroVMRunArgs")
        service_args = args.service_args()
        if timeout is not None:
            raise UnsupportedCapability("per-create timeout is not supported by the local service")
        result = await asyncio.to_thread(self._transport.call, "create",
                                         request_id=request_id, **service_args)
        return DSecSandbox(self._transport, result["id"])

    async def lookup_request(self, request_id: str):
        self._require_open()
        return await asyncio.to_thread(self._transport.query_request, request_id)

    async def run_container(self, args: DSecContainerRunArgs | None = None,
                            *, request_id: str | None = None):
        self._require_container_service()
        args = DSecContainerRunArgs() if args is None else args
        if not isinstance(args, DSecContainerRunArgs):
            raise TypeError("run_container requires DSecContainerRunArgs")
        args.validate()
        result = await asyncio.to_thread(self._transport.call, "container_create",
            request_id=request_id, spec=asdict(args))
        return DSecContainerSandbox(self._transport, result["id"], args)

    async def run_tb2(self, args: DSecTB2RunArgs, *, request_id: str | None = None):
        self._require_container_service()
        if not isinstance(args, DSecTB2RunArgs):
            raise TypeError("run_tb2 requires DSecTB2RunArgs")
        args.validate()
        result = await asyncio.to_thread(self._transport.call, "container_create",
            request_id=request_id, kind="tb2", spec=asdict(args))
        return DSecTB2Sandbox(self._transport, result["id"], args)

    async def attach_container(self, sandbox_id: str, args: DSecContainerRunArgs):
        self._require_container_service()
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise ValueError("sandbox_id is required")
        if not isinstance(args, DSecContainerRunArgs):
            raise TypeError("attach_container requires DSecContainerRunArgs")
        args.validate()
        sandbox = DSecContainerSandbox(self._transport, sandbox_id, args)
        if (await sandbox.status())["state"] != "RUNNING":
            raise RuntimeError("Container is not running")
        return sandbox

    async def lookup_container_request(self, request_id: str):
        self._require_container_service()
        return await asyncio.to_thread(self._transport.call, "container_query_request",
                                       lookup_id=request_id)

    async def attach(self, sandbox_id: str):
        """Reconstruct a handle after trainer reconnection without creating a VM."""
        self._require_open()
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise ValueError("sandbox_id is required")
        status = await asyncio.to_thread(self._transport.call, "status", sandbox_id)
        if status["state"] not in ("RUNNING", "PAUSED"):
            raise RuntimeError(f"Cannot attach to sandbox in {status['state']}")
        return DSecSandbox(self._transport, sandbox_id)


class DSecContainerSandbox:
    backend = "container"

    def __init__(self, transport: SandboxClient, sandbox_id: str, run_args: DSecContainerRunArgs):
        self._transport = transport
        self.id = sandbox_id
        self._run_args = run_args

    async def _call(self, operation, *, request_id=None, **args):
        return await asyncio.to_thread(self._transport.call, operation, self.id,
            request_id=request_id, spec=asdict(self._run_args), **args)

    async def status(self):
        return await self._call("container_status")

    async def run_shell(self, command: str, *, timeout_ms: int = 5000, output_limit: int = 65536,
                        request_id: str | None = None):
        return await self._call("container_execute", request_id=request_id,
                               command=command, timeout_ms=timeout_ms, output_limit=output_limit)

    async def query_request(self, request_id: str):
        return await self._call("container_query_action", lookup_id=request_id)

    async def pause(self, *, request_id: str | None = None):
        raise UnsupportedCapability("Container snapshot/pause is not integrated")

    async def resume(self):
        raise UnsupportedCapability("Container snapshot/resume is not integrated")

    async def stop(self, *, request_id: str | None = None):
        return await self._call("container_stop", request_id=request_id)


class DSecTB2Sandbox(DSecContainerSandbox):
    backend = "tb2"

    async def _call(self, operation, *, request_id=None, **args):
        return await super()._call(operation, request_id=request_id, kind="tb2", **args)

    async def base_url(self):
        status = await self.status()
        if status["state"] != "RUNNING":
            raise RuntimeError("TB2 sandbox is not running")
        return status["base_url"]
