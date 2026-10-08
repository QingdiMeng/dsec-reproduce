"""Edge owns container backends and the existing durable lifecycle journal.

This preserves the trusted-host Docker backend. It does not implement
production VM containment or container memory snapshots.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import re
import threading

from dsec.contracts.requests import request_digest
from dsec.contracts.sandbox import DSecContainerRunArgs, DSecTB2RunArgs, UnsupportedCapability
from dsec.runtime.backends.container import (CatalogErofsBackend, CatalogLayeredBackend,
                                            FullErofsBackend, LayeredContainerBackend)
from dsec.runtime.container_journal import ContainerLifecycleJournal
from dsec.runtime.lifecycle import ServiceBusy
from dsec.storage.catalog import EnvironmentCatalog


CONTAINER_OPERATIONS = frozenset((
    "container_create", "container_status", "container_execute", "container_stop",
    "container_query_request", "container_query_action"))
CONTAINER_MUTATING = frozenset(("container_create", "container_execute", "container_stop"))


@dataclass(frozen=True)
class ContainerEntry:
    backend: object
    sandbox: object


class ContainerRuntime:
    def __init__(self, configuration=None, *, admission_worker_socket=None):
        # A service instance keeps its deployment configuration; RPC callers
        # cannot supply host directories, Docker sockets or artifact paths.
        self.configuration = dict(os.environ if configuration is None else configuration)
        self.admission_worker_socket = admission_worker_socket
        self.backends = {}
        self.catalog_digests = {}
        self.handles = {}
        self.lock = threading.RLock()
        self.journal = None
        self.owner_lock = None
        self.operation_locks = {}
        self.closed = False

    def _journal(self):
        with self.lock:
            if self.closed:
                raise RuntimeError("Container Edge is closed")
            if self.journal is None:
                root = self.configuration.get("DSEC_CONTAINER_ROOT")
                if not root:
                    raise UnsupportedCapability("DSEC_CONTAINER_ROOT is required at Edge")
                root = Path(root).resolve()
                root.mkdir(mode=0o700, parents=True, exist_ok=True)
                stream = (root / "edge.lock").open("a")
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.journal = ContainerLifecycleJournal(
                        root, admission_worker_socket=self.admission_worker_socket)
                except BaseException:
                    stream.close()
                    raise
                self.owner_lock = stream
            return self.journal

    def close(self):
        # Closing the service's owner handle does not stop its containers.
        with self.lock:
            self.closed = True
            if self.owner_lock is not None:
                self.owner_lock.close()
                self.owner_lock = None

    @contextmanager
    def _operation(self, sandbox_id):
        with self.lock:
            entry = self.operation_locks.setdefault(sandbox_id, [threading.Lock(), 0])
            entry[1] += 1
        acquired = entry[0].acquire(blocking=False)
        try:
            if not acquired:
                raise ServiceBusy("Container has another active operation; request not accepted")
            yield
        finally:
            if acquired:
                entry[0].release()
            with self.lock:
                entry[1] -= 1
                if entry[1] == 0:
                    self.operation_locks.pop(sandbox_id)

    def _spec(self, args):
        kind = args.get("kind", "container")
        cls = {"container": DSecContainerRunArgs, "tb2": DSecTB2RunArgs}.get(kind)
        if cls is None or not isinstance(args.get("spec"), dict):
            raise ValueError("Invalid container specification")
        spec = cls(**args["spec"])
        spec.validate()
        return kind, spec

    def _backend(self, kind, spec):
        with self.lock:
            if kind == "tb2":
                from tb2_backend import TB2ContainerBackend
                key = (kind, spec.task_id, spec.image)
                if key not in self.backends:
                    self.backends[key] = TB2ContainerBackend(spec.task_id, spec.image)
                return self.backends[key]
            return self._container_backend(spec)

    def _key(self, kind, spec, sandbox_id):
        return ((kind, spec.task_id, spec.image, sandbox_id) if kind == "tb2" else
                (kind, spec.environment_id, spec.storage, sandbox_id))

    def _entry(self, kind, spec, sandbox_id):
        backend = self._backend(kind, spec)
        key = self._key(kind, spec, sandbox_id)
        with self.lock:
            entry = self.handles.get(key)
            if entry is None:
                # Reattach only an attested running object; absence is handled
                # separately by prove_stopped, never by creating a new one.
                entry = ContainerEntry(backend, backend.attach(sandbox_id))
                self.handles[key] = entry
            return entry

    def _remember(self, kind, spec, backend, sandbox):
        with self.lock:
            self.handles[self._key(kind, spec, sandbox.id)] = ContainerEntry(backend, sandbox)

    def dispatch(self, operation, sandbox_id, args, request_id):
        if operation not in CONTAINER_OPERATIONS or not isinstance(args, dict):
            raise ValueError("Unknown container operation")
        if (operation in CONTAINER_MUTATING and (not isinstance(request_id, str) or
                not re.fullmatch(r"[a-f0-9]{32}", request_id))):
            raise ValueError("request_id must be 32 lowercase hex characters")
        if operation == "container_query_request":
            if set(args) != {"lookup_id"}:
                raise ValueError("Invalid lifecycle lookup")
            return self.lookup_request(args["lookup_id"])
        allowed = {"spec", "kind"} | {
            "container_execute": {"command", "timeout_ms", "output_limit"},
            "container_query_action": {"lookup_id"}}.get(operation, set())
        if set(args) - allowed:
            raise ValueError("Unexpected container arguments")
        kind, spec = self._spec(args)
        journal = self._journal()
        if operation == "container_create":
            if sandbox_id is not None:
                raise ValueError("Create cannot target an existing sandbox")
            backend = self._backend(kind, spec)
            def create():
                options = {"memory_mb": spec.memory_limit_mb, "cpus": spec.cpu_cores_limit,
                           "sandbox_id": request_id}
                if kind == "container":
                    options["qos"] = spec.cpu_qos
                made = backend.create(**options)
                self._remember(kind, spec, backend, made)
                return {"id": made.id}
            with self._operation(request_id):
                return journal.execute(request_id, "create", None, spec.lifecycle_args(), create)
        if not isinstance(sandbox_id, str) or not re.fullmatch(r"[a-f0-9]{32}", sandbox_id):
            raise ValueError("Invalid sandbox ID")
        if operation == "container_stop":
            def stop():
                backend = self._backend(kind, spec)
                if backend.prove_stopped(sandbox_id):
                    result = {"id": sandbox_id, "backend": kind, "state": "STOPPED"}
                else:
                    result = self._entry(kind, spec, sandbox_id).sandbox.stop()
                with self.lock:
                    self.handles.pop(self._key(kind, spec, sandbox_id), None)
                return result
            with self._operation(sandbox_id):
                return journal.execute(request_id, "stop", sandbox_id, spec.stop_args(), stop)
        backend = self._backend(kind, spec)
        if operation == "container_status" and backend.prove_stopped(sandbox_id):
            return {"id": sandbox_id, "backend": kind, "state": "STOPPED"}
        sandbox = self._entry(kind, spec, sandbox_id).sandbox
        if operation == "container_status":
            return {**sandbox.status(), "container_name": sandbox.name}
        if operation == "container_query_action":
            return sandbox.query_request(args["lookup_id"])
        with self._operation(sandbox_id):
            return sandbox.run_shell(args["command"], timeout_ms=args.get("timeout_ms", 5000),
                                     output_limit=args.get("output_limit", 65536),
                                     request_id=request_id)

    def lookup_request(self, request_id):
        journal = self._journal()
        proof = journal.lookup(request_id)
        if proof["state"] != "UNKNOWN" or not isinstance(proof.get("args"), dict):
            return proof
        args, operation = proof["args"], proof.get("operation")
        if (operation == "create" and proof.get("sandbox_id") is None and
                set(args) == {"environment_id", "storage", "memory_mb", "cpus", "qos"}):
            spec = DSecContainerRunArgs(environment_id=args["environment_id"],
                storage=args["storage"], memory_limit_mb=args["memory_mb"],
                cpu_cores_limit=args["cpus"], cpu_qos=args["qos"])
            sandbox_id = request_id
        elif (operation == "stop" and set(args) == {"environment_id", "storage"} and
                isinstance(proof.get("sandbox_id"), str)):
            spec = DSecContainerRunArgs(environment_id=args["environment_id"], storage=args["storage"])
            sandbox_id = proof["sandbox_id"]
        else:
            return proof
        if proof.get("digest") != request_digest(operation, proof.get("sandbox_id"), args):
            return proof
        try:
            spec.validate()
            backend = self._backend("container", spec)
            def verify(saved):
                if saved.get("digest") != proof["digest"]:
                    return None
                if operation == "create" and backend.prove_running(sandbox_id):
                    return {"id": sandbox_id}
                if operation == "stop" and backend.prove_stopped(sandbox_id):
                    return {"id": sandbox_id, "backend": "container", "state": "STOPPED"}
                return None
            return journal.recover(request_id, verify)
        except Exception as exc:
            return {**proof, "recovery_error": str(exc)}

    def _container_backend(self, args):
        key = (args.environment_id, args.storage)
        if key not in (("e1-real", "local"), ("e2-full", "local"), ("e2-full", "threefs_lazy")):
            catalog_path = self.configuration.get("DSEC_ENVIRONMENT_CATALOG")
            if not catalog_path or args.environment_id not in EnvironmentCatalog(catalog_path).entries:
                raise UnsupportedCapability("Unsupported container environment/storage combination")
        if key in self.catalog_digests:
            current = EnvironmentCatalog(self.configuration["DSEC_ENVIRONMENT_CATALOG"]).digest
            if current != self.catalog_digests[key]:
                raise UnsupportedCapability("Environment catalog changed while this Edge is active")
        if key not in self.backends:
            common = ("DSEC_CONTAINER_ROOT", "DSEC_CONTAINER_AGENT")
            e1 = ("DSEC_CONTAINER_ARTIFACTS", "DSEC_CONTAINER_IMAGE")
            e2_common = ("DSEC_E2_META_DIR", "DSEC_E2_IMAGE", "DSEC_E2_MOUNT_HELPER")
            e2_source = ("DSEC_E2_LOCAL_BLOB" if args.storage == "local"
                         else "DSEC_E2_REMOTE_MOUNT")
            legacy = args.environment_id in ("e1-real", "e2-full")
            required = common + ((e1 if args.environment_id == "e1-real"
                                 else e2_common + (e2_source,)) if legacy else
                                 ("DSEC_ENVIRONMENT_CATALOG",))
            missing = [name for name in required if not self.configuration.get(name)]
            if missing:
                raise UnsupportedCapability("Container backend is not configured: " + ", ".join(missing))
            if args.environment_id == "e1-real":
                backend = LayeredContainerBackend(
                    artifacts=self.configuration[e1[0]], image=self.configuration[e1[1]],
                    root=self.configuration[common[0]], agent=self.configuration[common[1]])
            elif args.environment_id == "e2-full":
                backend = FullErofsBackend(
                    meta_dir=self.configuration[e2_common[0]],
                    local_blob=self.configuration.get("DSEC_E2_LOCAL_BLOB", ""),
                    remote_mount=self.configuration.get("DSEC_E2_REMOTE_MOUNT", ""),
                    image=self.configuration[e2_common[1]],
                    mount_helper=self.configuration[e2_common[2]], root=self.configuration[common[0]],
                    agent=self.configuration[common[1]], storage=args.storage)
            else:
                item = EnvironmentCatalog(
                    self.configuration["DSEC_ENVIRONMENT_CATALOG"]).resolve(
                    args.environment_id, args.storage)
                if item["rootfs"] == "erofs_layers":
                    backend = CatalogLayeredBackend(
                        layers=item["layers"], image=item["runtime_image"],
                        root=self.configuration[common[0]], agent=self.configuration[common[1]],
                        environment_id=args.environment_id,
                        catalog_sha256=item["catalog_sha256"], storage=args.storage)
                else:
                    backend = CatalogErofsBackend(
                        meta_dir=item["metadata"].parent,
                        metadata_file=item["metadata"],
                        metadata_sha256=item["metadata_sha256"],
                        data_file=item["data"], data_sha256=item["data_sha256"],
                        local_blob=item["data"] if args.storage == "local" else "",
                        remote_mount=item["remote_mount"] or "",
                        image=item["runtime_image"], mount_helper=item["mount_helper"],
                        root=self.configuration[common[0]], agent=self.configuration[common[1]],
                        storage=args.storage, environment_id=args.environment_id,
                        catalog_sha256=item["catalog_sha256"])
                self.catalog_digests[key] = item["catalog_sha256"]
            self.backends[key] = backend
        return self.backends[key]
