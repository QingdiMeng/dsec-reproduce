"""Small libdsec-style async facade over the local single-host backends.

This follows the call shape published in the DSec paper. It is not the official
libdsec package and does not claim support for full-VM backends.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import uuid

from dsec.sdk.sandbox_transport import SandboxClient, ServiceError
from dsec.runtime.backends.container import (CatalogErofsBackend, CatalogLayeredBackend,
                               FullErofsBackend, LayeredContainerBackend)
from dsec.storage.catalog import EnvironmentCatalog
from dsec.runtime.container_journal import ContainerLifecycleJournal
from dsec.contracts.requests import request_digest
from dsec.sdk.scheduled import ScheduledDSecClient, ScheduledOutcomeUnknown


class UnsupportedCapability(ValueError):
    """A requested sandbox feature is not implemented by this backend."""


@dataclass(frozen=True)
class DSecMicroVMRunArgs:
    ttl_running_stop: float = 300
    environment_id: str | None = None
    storage: str = "local"
    memory_profile: str = "baseline"
    memory_limit_mb: int | None = None
    cpu_cores_limit: int | None = None
    network_rules: dict | None = None
    init_user: str | None = None
    verifier_storage: str | None = None
    baseline_id: str | None = None

    def service_args(self):
        if self.baseline_id is not None and not re.fullmatch(r"[0-9a-f]{12}", self.baseline_id):
            raise ValueError("Invalid baseline_id")
        # The current daemon boots one configured image with fixed 1-vCPU/256-MiB
        # resources. It must not silently accept a policy it cannot enforce.
        unsupported = ("memory_limit_mb", "cpu_cores_limit", "network_rules", "init_user")
        for name in unsupported:
            if getattr(self, name) is not None:
                raise UnsupportedCapability(f"microVM backend does not support {name} yet")
        if not isinstance(self.ttl_running_stop, (int, float)) or isinstance(self.ttl_running_stop, bool):
            raise ValueError("ttl_running_stop must be a positive number")
        if not math.isfinite(self.ttl_running_stop) or self.ttl_running_stop <= 0:
            raise ValueError("ttl_running_stop must be finite and positive")
        if self.storage not in ("local", "threefs_lazy"):
            raise UnsupportedCapability("Unsupported microVM storage")
        if self.environment_id is None:
            if (self.memory_profile != "baseline" or self.verifier_storage is not None or
                    self.storage != "local"):
                raise UnsupportedCapability("Nonbaseline memory requires e3-mixed environment")
            return {"idle_ttl_seconds": self.ttl_running_stop,
                    **({"baseline_id": self.baseline_id} if self.baseline_id else {})}
        if self.environment_id != "e3-mixed" or self.memory_profile not in (
                "baseline", "dax", "damon_fpr", "dax_damon_fpr"):
            if (not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,127}", self.environment_id)
                    or self.memory_profile != "baseline"):
                raise UnsupportedCapability("Unsupported microVM environment/memory profile")
        if self.verifier_storage is not None:
            if (not self.environment_id.startswith("tb2-") or
                    self.verifier_storage not in ("local", "threefs_lazy")):
                raise UnsupportedCapability("Verifier storage requires a TB2 microVM and local/threefs_lazy")
        if self.storage != "local" and (self.environment_id == "e3-mixed" or
                                         self.environment_id.startswith("tb2-")):
            raise UnsupportedCapability("Task-specific microVM storage policy is separate")
        result = {"idle_ttl_seconds": self.ttl_running_stop,
                  "environment_id": self.environment_id,
                  "memory_profile": self.memory_profile}
        if self.verifier_storage is not None:
            result["verifier_storage"] = self.verifier_storage
        if self.environment_id != "e3-mixed" and not self.environment_id.startswith("tb2-"):
            result["storage"] = self.storage
        if self.baseline_id:
            result["baseline_id"] = self.baseline_id
        return result


@dataclass(frozen=True)
class DSecContainerRunArgs:
    environment_id: str = "e1-real"
    storage: str = "local"
    memory_limit_mb: int = 512
    cpu_cores_limit: float = 1.0
    cpu_qos: str = "default"
    ttl_running_stop: float | None = None
    network_rules: dict | None = None
    init_user: str | None = None

    def validate(self):
        legacy = (("e1-real", "local"), ("e2-full", "local"),
                  ("e2-full", "threefs_lazy"))
        if (self.environment_id, self.storage) not in legacy:
            catalog_path = os.environ.get("DSEC_ENVIRONMENT_CATALOG")
            if (not catalog_path or self.storage not in ("local", "threefs_lazy") or
                    self.environment_id not in EnvironmentCatalog(catalog_path).entries):
                raise UnsupportedCapability("Unsupported container environment/storage combination")
        if self.network_rules is not None or self.init_user is not None:
            raise UnsupportedCapability("Container network rules and init_user are not integrated")
        if self.ttl_running_stop is not None:
            raise UnsupportedCapability("Container idle TTL is not integrated")
        if self.cpu_qos != "default" and not re.fullmatch(
                r"(?:ls_core_cookie:[2468]|be_sched_idle:[2-9])", self.cpu_qos):
            raise UnsupportedCapability("Unsupported E4 container CPU QoS profile")
        return self

    def lifecycle_args(self):
        return {"environment_id": self.environment_id, "storage": self.storage,
                "memory_mb": self.memory_limit_mb, "cpus": self.cpu_cores_limit,
                "qos": self.cpu_qos}

    def stop_args(self):
        return {"environment_id": self.environment_id, "storage": self.storage}


@dataclass(frozen=True)
class DSecTB2RunArgs:
    task_id: str
    image: str
    memory_limit_mb: int = 2048
    cpu_cores_limit: float = 1.0

    def lifecycle_args(self):
        return {"environment_id": "tb2-openenv", "task_id": self.task_id,
                "image": self.image, "memory_mb": self.memory_limit_mb,
                "cpus": self.cpu_cores_limit}

    def stop_args(self):
        return {"environment_id": "tb2-openenv", "task_id": self.task_id,
                "image": self.image}


class DSecTB2Sandbox:
    backend = "tb2"

    def __init__(self, container, journal, run_args):
        self._container = container
        self._journal = journal
        self._run_args = run_args
        self.id = container.id

    async def status(self):
        return await asyncio.to_thread(self._container.status)

    async def base_url(self):
        status = await self.status()
        if status["state"] != "RUNNING":
            raise RuntimeError("TB2 sandbox is not running")
        return status["base_url"]

    async def stop(self, *, request_id: str | None = None):
        if request_id is None:
            return await asyncio.to_thread(self._container.stop)
        return await asyncio.to_thread(self._journal.execute, request_id, "stop",
                                       self.id, self._run_args.stop_args(),
                                       self._container.stop)


class DSecContainerSandbox:
    backend = "container"

    def __init__(self, container, lifecycle_journal=None, run_args=None):
        self._container = container
        self.id = container.id
        self._lifecycle_journal = lifecycle_journal
        self._run_args = run_args

    async def status(self):
        return await asyncio.to_thread(self._container.status)

    async def run_shell(self, command: str, *, timeout_ms: int = 5000, output_limit: int = 65536,
                        request_id: str | None = None):
        return await asyncio.to_thread(self._container.run_shell, command,
                                       timeout_ms=timeout_ms, output_limit=output_limit,
                                       request_id=request_id)

    async def query_request(self, request_id: str):
        return await asyncio.to_thread(self._container.query_request, request_id)

    async def pause(self, *, request_id: str | None = None):
        raise UnsupportedCapability("Container snapshot/pause is not integrated")

    async def resume(self):
        raise UnsupportedCapability("Container snapshot/resume is not integrated")

    async def stop(self, *, request_id: str | None = None):
        if request_id is not None:
            if self._lifecycle_journal is None or self._run_args is None:
                raise UnsupportedCapability("Container lifecycle journal is unavailable")
            return await asyncio.to_thread(self._lifecycle_journal.execute,
                request_id, "stop", self.id, self._run_args.stop_args(),
                self._container.stop)
        return await asyncio.to_thread(self._container.stop)


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
        # The daemon can briefly hold this sandbox's lock while a background
        # lifecycle operation finishes. ServiceBusy explicitly means the new
        # request was not admitted, so it is safe to submit a fresh request ID.
        for attempt in range(35):
            try:
                return await asyncio.to_thread(
                    self._transport.call, operation, self.id,
                    request_id=request_id, **args)
            except ServiceError as exc:
                if exc.kind != "ServiceBusy" or attempt == 34:
                    raise
                request_id = uuid.uuid4().hex
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
        self._container_backends = {}
        self._catalog_digests = {}
        self._container_journal = None

    async def open(self):
        await asyncio.to_thread(self._transport.call, "health")
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
        self._require_open()
        if args is None:
            args = DSecContainerRunArgs()
        if not isinstance(args, DSecContainerRunArgs):
            raise TypeError("run_container requires DSecContainerRunArgs")
        args.validate()
        backend = await self._container_backend(args)
        journal = self._lifecycle_journal()
        if request_id is None:
            container = await asyncio.to_thread(backend.create,
                                                memory_mb=args.memory_limit_mb,
                                                cpus=args.cpu_cores_limit,
                                                qos=args.cpu_qos)
        else:
            def create():
                made = backend.create(memory_mb=args.memory_limit_mb,
                                      cpus=args.cpu_cores_limit, qos=args.cpu_qos,
                                      sandbox_id=request_id)
                return {"id": made.id}
            result = await asyncio.to_thread(journal.execute, request_id, "create",
                                             None, args.lifecycle_args(), create)
            container = await asyncio.to_thread(backend.attach, result["id"])
        return DSecContainerSandbox(container, journal, args)

    async def run_tb2(self, args: DSecTB2RunArgs, *, request_id: str | None = None):
        """Create a per-episode official TB2 image through our DSec lifecycle facade."""
        from tb2_backend import TB2ContainerBackend
        self._require_open()
        if not isinstance(args, DSecTB2RunArgs):
            raise TypeError("run_tb2 requires DSecTB2RunArgs")
        backend = await asyncio.to_thread(TB2ContainerBackend, args.task_id, args.image)
        journal = self._lifecycle_journal()
        if request_id is None:
            container = await asyncio.to_thread(backend.create,
                memory_mb=args.memory_limit_mb, cpus=args.cpu_cores_limit)
        else:
            def create():
                made = backend.create(memory_mb=args.memory_limit_mb,
                                      cpus=args.cpu_cores_limit,
                                      sandbox_id=request_id)
                return {"id": made.id}
            result = await asyncio.to_thread(journal.execute, request_id, "create",
                                             None, args.lifecycle_args(), create)
            container = await asyncio.to_thread(backend.attach, result["id"])
        return DSecTB2Sandbox(container, journal, args)

    async def attach_container(self, sandbox_id: str, args: DSecContainerRunArgs):
        """Reattach a known E1/E2 container after a worker restart."""
        self._require_open()
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise ValueError("sandbox_id is required")
        if not isinstance(args, DSecContainerRunArgs):
            raise TypeError("attach_container requires DSecContainerRunArgs")
        args.validate()
        backend = await self._container_backend(args)
        return DSecContainerSandbox(await asyncio.to_thread(backend.attach, sandbox_id),
                                    self._lifecycle_journal(), args)

    def _lifecycle_journal(self):
        if self._container_journal is None:
            root = os.environ.get("DSEC_CONTAINER_ROOT")
            if not root:
                raise UnsupportedCapability("DSEC_CONTAINER_ROOT is required")
            self._container_journal = ContainerLifecycleJournal(root)
        return self._container_journal

    async def lookup_container_request(self, request_id: str):
        journal = self._lifecycle_journal()
        proof = await asyncio.to_thread(journal.lookup, request_id)
        if proof["state"] != "UNKNOWN" or not isinstance(proof.get("args"), dict):
            return proof
        args = proof["args"]
        operation = proof.get("operation")
        if (operation == "create" and proof.get("sandbox_id") is None
                and set(args) == {"environment_id", "storage", "memory_mb", "cpus", "qos"}):
            spec = DSecContainerRunArgs(environment_id=args["environment_id"],
                storage=args["storage"], memory_limit_mb=args["memory_mb"],
                cpu_cores_limit=args["cpus"], cpu_qos=args["qos"])
            sandbox_id = request_id
        elif (operation == "stop" and set(args) == {"environment_id", "storage"}
              and isinstance(proof.get("sandbox_id"), str)):
            spec = DSecContainerRunArgs(environment_id=args["environment_id"],
                                       storage=args["storage"])
            sandbox_id = proof["sandbox_id"]
        else:
            return proof
        if proof.get("digest") != request_digest(operation, proof.get("sandbox_id"), args):
            return proof
        try:
            spec.validate()
            backend = await self._container_backend(spec)
            def verify(saved):
                if saved.get("digest") != proof["digest"]:
                    return None
                if operation == "create" and backend.prove_running(sandbox_id):
                    return {"id": sandbox_id}
                if operation == "stop" and backend.prove_stopped(sandbox_id):
                    return {"id": sandbox_id, "backend": "container", "state": "STOPPED"}
                return None
            return await asyncio.to_thread(journal.recover, request_id, verify)
        except Exception as exc:
            # Lost dependencies (especially 3FS) prevent proof, never imply
            # success or permission to repeat the side effect.
            return {**proof, "recovery_error": str(exc)}

    async def _container_backend(self, args):
        key = (args.environment_id, args.storage)
        if key in self._catalog_digests:
            current = EnvironmentCatalog(os.environ["DSEC_ENVIRONMENT_CATALOG"]).digest
            if current != self._catalog_digests[key]:
                raise UnsupportedCapability("Environment catalog changed while this client is active")
        if key not in self._container_backends:
            common = ("DSEC_CONTAINER_ROOT", "DSEC_CONTAINER_AGENT")
            e1 = ("DSEC_CONTAINER_ARTIFACTS", "DSEC_CONTAINER_IMAGE")
            e2_common = ("DSEC_E2_META_DIR", "DSEC_E2_IMAGE", "DSEC_E2_MOUNT_HELPER")
            e2_source = ("DSEC_E2_LOCAL_BLOB" if args.storage == "local"
                         else "DSEC_E2_REMOTE_MOUNT")
            legacy = args.environment_id in ("e1-real", "e2-full")
            required = common + ((e1 if args.environment_id == "e1-real"
                                 else e2_common + (e2_source,)) if legacy else
                                 ("DSEC_ENVIRONMENT_CATALOG",))
            missing = [name for name in required if not os.environ.get(name)]
            if missing:
                raise UnsupportedCapability("Container backend is not configured: " + ", ".join(missing))
            if args.environment_id == "e1-real":
                backend = await asyncio.to_thread(LayeredContainerBackend,
                    artifacts=os.environ[e1[0]], image=os.environ[e1[1]],
                    root=os.environ[common[0]], agent=os.environ[common[1]])
            elif args.environment_id == "e2-full":
                backend = await asyncio.to_thread(FullErofsBackend,
                    meta_dir=os.environ[e2_common[0]],
                    local_blob=os.environ.get("DSEC_E2_LOCAL_BLOB", ""),
                    remote_mount=os.environ.get("DSEC_E2_REMOTE_MOUNT", ""),
                    image=os.environ[e2_common[1]],
                    mount_helper=os.environ[e2_common[2]], root=os.environ[common[0]],
                    agent=os.environ[common[1]], storage=args.storage)
            else:
                item = await asyncio.to_thread(EnvironmentCatalog(
                    os.environ["DSEC_ENVIRONMENT_CATALOG"]).resolve,
                    args.environment_id, args.storage)
                if item["rootfs"] == "erofs_layers":
                    backend = await asyncio.to_thread(CatalogLayeredBackend,
                        layers=item["layers"], image=item["runtime_image"],
                        root=os.environ[common[0]], agent=os.environ[common[1]],
                        environment_id=args.environment_id,
                        catalog_sha256=item["catalog_sha256"], storage=args.storage)
                else:
                    backend = await asyncio.to_thread(CatalogErofsBackend,
                        meta_dir=item["metadata"].parent,
                        metadata_file=item["metadata"],
                        metadata_sha256=item["metadata_sha256"],
                        data_file=item["data"], data_sha256=item["data_sha256"],
                        local_blob=item["data"] if args.storage == "local" else "",
                        remote_mount=item["remote_mount"] or "",
                        image=item["runtime_image"], mount_helper=item["mount_helper"],
                        root=os.environ[common[0]], agent=os.environ[common[1]],
                        storage=args.storage, environment_id=args.environment_id,
                        catalog_sha256=item["catalog_sha256"])
                self._catalog_digests[key] = item["catalog_sha256"]
            self._container_backends[key] = backend
        return self._container_backends[key]

    async def attach(self, sandbox_id: str):
        """Reconstruct a handle after trainer reconnection without creating a VM."""
        self._require_open()
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise ValueError("sandbox_id is required")
        status = await asyncio.to_thread(self._transport.call, "status", sandbox_id)
        if status["state"] not in ("RUNNING", "PAUSED"):
            raise RuntimeError(f"Cannot attach to sandbox in {status['state']}")
        return DSecSandbox(self._transport, sandbox_id)
