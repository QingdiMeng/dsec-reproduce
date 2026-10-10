"""Sandbox state and Edge composition; transitions are a separate component."""
from collections import deque
import json
from dsec.contracts.errors import SandboxError, ServiceBusy, CommandOutcomeUnknown
from dsec.runtime.transitions import LifecycleController
from dsec.runtime.pool import ReadyPool
from dsec.runtime.provisioning import SandboxProvisioner
from dsec.runtime.sessions.dispatcher import ShellDispatcher
import math
import os
from pathlib import Path
import threading
import time
import uuid
from dsec.storage.digest import sha
from dsec.runtime.backends.firecracker import MicroVM
from dsec.runtime.isolation.proxy import guest_proxy_command, validate_proxy_url, validate_proxy_bypass_hosts


# Legacy hook names remain in the runtime module for fault-injection compatibility.
from dsec.storage.snapshots import (_copy_sparse, _fsync_directory,
    _sparse_sha256, _sparse_block_sha256, _snapshot_hash as _storage_snapshot_hash)
from dsec.storage.service import RuntimeStorage
from dsec.contracts.storage import DiskPaths


def _snapshot_hash(path, algorithm):
    return _storage_snapshot_hash(path, algorithm, hash_file=sha,
        sparse_hash=_sparse_sha256, sparse_block_hash=_sparse_block_sha256)


class Sandbox:
    def __init__(self, manager, ttl, environment_id="default", memory_profile="baseline",
                 verifier_storage=None, reserved=False, storage="local",
                 environment_spec=None):
        self.manager = manager
        self.node_lease_id = None
        self.resource_cleanup_complete = False
        self.id = uuid.uuid4().hex[:12]
        self.directory = manager.root/self.id
        self.directory.mkdir(mode=0o700)
        # A storage-enabled parent has setgid. File-backed guests must still
        # present the exact private mode expected by the scoped VMM launcher.
        self.directory.chmod(0o700)
        self.disk = self.directory/"rootfs.ext4"
        self.environment_id = environment_id
        self.storage = storage
        self.environment_catalog_sha256 = (manager.microvm_environment_catalog.digest
                                           if environment_spec is not None else None)
        self.environment_manifest_sha256 = (environment_spec["environment_sha256"]
                                            if environment_spec is not None else None)
        self.layer_disks = (tuple(layer["file"] for layer in environment_spec["layers"])
                            if environment_spec is not None else
                            manager.tb2_layers.get(environment_id, ()))
        self.kernel = environment_spec["kernel"] if environment_spec is not None else manager.kernel
        self.overlaybd_store = (manager.overlaybd_root_store if
                                manager.overlaybd_root_store is not None and
                                environment_id in manager.overlaybd_root_store.source_images
                                else None)
        if self.overlaybd_store:
            self.overlaybd_store.share_directory(self.directory)
        self.overlaybd_image = (self.overlaybd_store.source_for(environment_id)
                                if self.overlaybd_store else None)
        self.overlaybd_device_id = None
        self.overlaybd_runtime = None
        self.overlaybd_daemon_socket_identity = None
        self.memory_profile = memory_profile
        self.free_page_reporting = ((environment_id in manager.tb2_templates and
                                     manager.tb2_free_page_reporting) or
                                    memory_profile in ("damon_fpr", "dax_damon_fpr"))
        self.snapshot_mode = (manager.snapshot_strategy if environment_id in manager.tb2_templates
                              else "full")
        self.dirty_tracking_enabled = self.snapshot_mode != "full"
        self.verifier_storage = verifier_storage
        self.verifier_dax = (verifier_storage is not None and
                             environment_id in manager.tb2_verifier_dax_tasks)
        self.erofs_dax_layers = manager.tb2_layer_dax_indices.get(environment_id, ())
        self.reserved = reserved
        self.warm_pool_hit = False
        self.verifier_artifact_sha = (manager.verifier_artifacts_for(environment_id).sha
                                      if verifier_storage is not None else None)
        self.work_disk = self.directory/"work.ext4" if environment_id == "e3-mixed" else None
        binary = (manager.e3["binary"] if environment_id == "e3-mixed" else
                  manager.generic_dax_binaries[environment_id]
                  if environment_id in manager.generic_dax_binaries else
                  manager.tb2_verifier_dax_binary if (self.verifier_dax or self.erofs_dax_layers)
                  else manager.binary)
        self.vm = MicroVM(binary, self.directory,
                          max_timeout_ms=max(manager.command_timeout_ms(environment_id),
                                             manager.verifier_timeout_ms(environment_id)))
        self.memory_evidence = None
        self.lock = threading.RLock()
        self.ttl = ttl
        self.deadline = time.monotonic()+ttl
        self.state = "CREATING"
        self.reason = None
        self.snapshot = None
        self.baseline_sealed = False
        self.baseline_identity = None
        self.baseline_verified = False
        self.fork_readers = 0
        self.baseline_closing = False
        self.fork_condition = threading.Condition(self.lock)
        self.fork_origin = None
        self.snapshot_cache_evicted = None
        self.last_pause_phases = None
        self.last_create_phases = None
        self.network_slot = None
        self.network_mode = None
        self.network_ready = False
        self.generation = 0
        self.inflight = None
        self.native_inflight = {}
        self.native_incarnation = 0
        self.vm.on_process_started = self._persist

    def _persist(self):
        hook = getattr(self.manager, "persist", None)
        if hook:
            hook(self)

    def _snapshot_checkpoint(self, stage):
        """No-op boundary used by isolated crash-injection tests."""

    def _native_checkpoint(self, stage, **details):
        """No-op boundary for controlled native/lifecycle correspondence checks."""

    def _touch(self):
        self.deadline = time.monotonic()+self.ttl
        self._persist()

    def _event(self, state, reason=None):
        self.state, self.reason = state, reason
        with (self.directory/"events.jsonl").open("a") as out:
            out.write(json.dumps({"time":time.time(), "state":state, "reason":reason,
                                  "generation":self.generation})+"\n")
        self._persist()

    def _overlaybd_service_matches(self):
        return self.manager.lifecycle.overlaybd_service_matches(self)

    def _require_owner(self):
        registry = getattr(self.manager, 'registry', None)
        if registry is not None:
            registry.require_owner()

    def _release_overlaybd_device(self):
        self._require_owner()
        return self.manager.lifecycle.release_overlaybd_device(self)

    def _fail(self, reason):
        self._require_owner()
        return self.manager.lifecycle.fail(self, reason)

    def _check(self):
        self._require_owner()
        return self.manager.lifecycle.check(self)

    def status(self):
        with self.lock:
            self._check()
            if self.state == "RUNNING" and self.environment_id == "e3-mixed":
                try:
                    self.memory_evidence = self.vm.memory_probe(self.memory_profile)
                except Exception as exc:
                    self._fail("e3_memory_probe_failed: " + str(exc))
            return {"id":self.id,
                    "state":"READY" if self.reserved and self.state == "RUNNING" else self.state,
                    "reason":self.reason,
                    "generation":self.generation, "has_snapshot":self.snapshot is not None,
                    "node_lease_id":getattr(self, 'node_lease_id', None),
                    "resource_cleanup_complete":getattr(self, 'resource_cleanup_complete', False),
                    "baseline_sealed":self.baseline_sealed, "fork_origin":self.fork_origin,
                    "fork_readers":self.fork_readers,
                    "active_native_operations":len(getattr(self, "native_inflight", {})),
                    "snapshot_cache_policy":self.manager.snapshot_cache_policy,
                    "snapshot_cache_evicted":self.snapshot_cache_evicted,
                    "last_pause_phases":getattr(self, "last_pause_phases", None),
                    "last_create_phases":getattr(self, "last_create_phases", None),
                    "last_boot_phases":getattr(self.vm, "last_boot_phases", None),
                    "pid":self.vm.process.pid if self.vm.process else None,
                    "overlaybd_device_id":self.overlaybd_device_id,
                    "environment_id":self.environment_id,
                    "storage":self.storage,
                    "environment_catalog_sha256":self.environment_catalog_sha256,
                    "environment_manifest_sha256":self.environment_manifest_sha256,
                    "guest_kernel":str(self.kernel),
                    "rootfs_format":"erofs-overlay" if self.environment_id in self.manager.tb2_layers else "ext4",
                    "rootfs_layer_count":self.manager.tb2_layer_counts.get(
                        self.environment_id,
                        len(self.manager.tb2_layers.get(self.environment_id, ()))),
                    "rootfs_layer_devices":len(self.layer_disks),
                    "rootfs_transport":self.manager.tb2_layer_transports.get(self.environment_id),
                    "rootfs_block_backend":"overlaybd-ublk" if self.overlaybd_store else "file-ext4",
                    "network_slot":self.network_slot,
                    "network_mode":self.network_mode,
                    "memory_profile":self.memory_profile,
                    "free_page_reporting":self.free_page_reporting,
                    "dirty_tracking_enabled":self.dirty_tracking_enabled,
                    "snapshot_mode":self.snapshot_mode,
                    "warm_pool_hit":self.warm_pool_hit,
                    "verifier_storage":self.verifier_storage,
                    "verifier_dax":self.verifier_dax,
                    "erofs_dax_layers":list(self.erofs_dax_layers),
                    "verifier_artifact_sha256":self.verifier_artifact_sha,
                    "memory_evidence":self.memory_evidence if self.state == "RUNNING" else None}

    def seal_baseline(self, *, allow_prepared_state=False):
        from dsec.runtime.fork import seal_baseline
        return seal_baseline(self, allow_prepared_state=allow_prepared_state)

    def disk_paths(self):
        return DiskPaths(self.directory, self.disk, self.work_disk)

    def _restore(self):
        self._require_owner()
        return self.manager.lifecycle.restore(self)

    def execute(self, command, timeout_ms=5000, output_limit=65536,
                execution_scope="agent"):
        return self.manager.commands.execute(self, command, timeout_ms, output_limit,
                                             execution_scope)

    def pause(self):
        return self.manager.lifecycle.pause(self)

    def resume(self):
        return self.manager.lifecycle.resume(self)

    def recover(self, *, allow_rollback=False):
        return self.manager.lifecycle.recover(self, allow_rollback=allow_rollback)

    def _stop(self, reason):
        self._require_owner()
        return self.manager.lifecycle.stop(self, reason)

    def stop(self):
        with self.lock:
            self._stop("explicit_stop")

class SandboxManager:
    def __init__(self, root, binary, kernel, template, *, capacity=4, poll_seconds=.1,
                 start_monitor=True, e3=None, tb2_templates=None, tb2_network_tap=None,
                 tb2_resources=None, tb2_layers=None, tb2_layer_counts=None,
                 tb2_layer_transports=None, tb2_layer_dax_indices=None,
                 generic_dax_binaries=None,
                 tb2_network_slots=None, tb2_network_manager=None,
                 tb2_free_page_reporting=False,
                 overlaybd_root_store=None,
                 snapshot_cache_policy="retain", tb2_verifier_artifacts=None,
                 tb2_verifier_dax_tasks=None, tb2_verifier_dax_binary=None,
                 snapshot_concurrency=None, snapshot_strategy="full", snapshot_editor=None,
                 warm_pool_specs=None, warm_refill_workers=None, warm_wait_ms=0,
                 warm_idle_quiet_seconds=0, warm_min_memory_mib=0,
                 warm_min_disk_gib=0, microvm_environment_catalog=None,
                 egress_proxy_url=None, egress_proxy_bypass_hosts=(), node_admission=None, registry=None):
        if not isinstance(capacity, int) or capacity < 1 or not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise ValueError("Invalid manager limits")
        if snapshot_cache_policy not in ("retain", "evict"):
            raise ValueError("Unsupported snapshot cache policy")
        if snapshot_strategy not in ("full", "boot-diff", "incremental"):
            raise ValueError("Unsupported snapshot strategy")
        if snapshot_strategy == "incremental" and (
                snapshot_editor is None or not Path(snapshot_editor).is_file() or
                not os.access(snapshot_editor, os.X_OK)):
            raise ValueError("Incremental snapshots require an executable snapshot editor")
        if snapshot_concurrency is None:
            snapshot_concurrency = capacity
        if (not isinstance(snapshot_concurrency, int) or isinstance(snapshot_concurrency, bool)
                or not 1 <= snapshot_concurrency <= capacity):
            raise ValueError("Snapshot concurrency must be in 1..capacity")
        self.root = Path(root).resolve(); self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.egress_proxy_url = validate_proxy_url(egress_proxy_url)
        self.egress_proxy_bypass_hosts = validate_proxy_bypass_hosts(egress_proxy_bypass_hosts)
        self.binary, self.kernel, self.template = map(Path, (binary,kernel,template))
        self.node_admission = node_admission
        self.registry = registry
        if registry is not None:
            self.registry_lock = registry.owner_lock
        # Late binding keeps legacy module-level fault hooks working while
        # the transition component has no dependency on this compatibility host.
        self.disk_storage = RuntimeStorage(
            copy_sparse=lambda *args: _copy_sparse(*args),
            snapshot_hash=lambda *args: _snapshot_hash(*args),
            hash_file=lambda path: sha(path))
        self.lifecycle = LifecycleController(
            storage=self.disk_storage,
            hash_file=lambda path: sha(path),
            fsync_directory=lambda path: _fsync_directory(path))
        self.commands = ShellDispatcher(
            proxy_command=lambda *args: guest_proxy_command(*args))
        self.ready_pool = ReadyPool()
        self.provisioner = SandboxProvisioner(
            sandbox_factory=lambda *args, **kwargs: Sandbox(*args, **kwargs))
        self.warm_node_waits = {}
        self.e3 = e3
        self.microvm_environment_catalog = microvm_environment_catalog
        self.tb2_templates = tb2_templates or {}
        self.tb2_layers = {key: tuple(value) for key, value in (tb2_layers or {}).items()}
        self.tb2_layer_counts = dict(tb2_layer_counts or {})
        self.tb2_layer_transports = dict(tb2_layer_transports or {})
        self.tb2_layer_dax_indices = {key: tuple(value) for key, value in
                                      (tb2_layer_dax_indices or {}).items()}
        self.generic_dax_binaries = {key: Path(value).resolve(strict=True) for key, value in
                                     (generic_dax_binaries or {}).items()}
        if any(key not in self.tb2_layer_dax_indices or
               self.microvm_environment_catalog is None or
               key not in self.microvm_environment_catalog.entries
               for key in self.generic_dax_binaries):
            raise ValueError("Generic DAX binary requires a catalogued DAX EROFS layer")
        if not isinstance(tb2_free_page_reporting, bool):
            raise ValueError("tb2_free_page_reporting must be boolean")
        self.tb2_free_page_reporting = tb2_free_page_reporting
        self.overlaybd_root_store = overlaybd_root_store
        if self.overlaybd_root_store is not None:
            # The daemon needs to traverse the manager root; only per-sandbox
            # runtime directories need group read/write access.
            self.overlaybd_root_store.share_directory(self.root, mode=0o2710)
            self.overlaybd_root_store.configure_shared_layers(self.root)
        if (self.overlaybd_root_store is not None and
                any(key not in self.tb2_templates
                    for key in self.overlaybd_root_store.source_images)):
            raise ValueError("OverlayBD root image requires a TB2 boot template")
        if any(key not in self.tb2_templates or not 1 <= len(value) <= 17
               for key, value in self.tb2_layers.items()):
            raise ValueError("Layered TB2 task needs a boot template and 1..17 EROFS layers")
        if self.tb2_free_page_reporting and any(
                len(value) > 12 for value in self.tb2_layers.values()):
            raise ValueError("TB2 free-page reporting leaves at most 12 EROFS layer devices")
        if any(key not in self.tb2_layers or not isinstance(value, int) or value < 1
               for key, value in self.tb2_layer_counts.items()):
            raise ValueError("Invalid logical EROFS layer count")
        self.tb2_network_tap = tb2_network_tap
        self.tb2_resources = tb2_resources or {}
        self.tb2_network_slots = tb2_network_slots or {}
        self.tb2_network_manager = tb2_network_manager
        self.tb2_verifier_artifacts = tb2_verifier_artifacts
        self.tb2_verifier_dax_tasks = frozenset(tb2_verifier_dax_tasks or ())
        legacy_dax_layers = set(self.tb2_layer_dax_indices) - set(self.generic_dax_binaries)
        if bool(self.tb2_verifier_dax_tasks or legacy_dax_layers) != bool(tb2_verifier_dax_binary):
            raise ValueError("DAX tasks and DAX-capable binary must be configured together")
        self.tb2_verifier_dax_binary = (Path(tb2_verifier_dax_binary).resolve(strict=True)
                                        if tb2_verifier_dax_binary else None)
        if legacy_dax_layers and self.tb2_verifier_dax_binary is None:
            raise ValueError("DAX EROFS layers need the DAX-capable Firecracker binary")
        if any(key not in self.tb2_layers or not value or
               any(not isinstance(i, int) or i < 0 or i >= len(self.tb2_layers[key])
                   for i in value)
               for key, value in self.tb2_layer_dax_indices.items()):
            raise ValueError("Invalid task EROFS DAX layer selection")
        if not self.tb2_verifier_dax_tasks <= set(self.tb2_templates):
            raise ValueError("DAX verifier task is not configured")
        for task in self.tb2_verifier_dax_tasks:
            store = self.verifier_artifacts_for(task)
            from dsec.compat.task_plugins import validate_verifier_dax
            validate_verifier_dax(store, task)
        if self.tb2_network_manager and (self.tb2_network_slots or self.tb2_network_tap):
            raise ValueError("Choose one TB2 network backend")
        self.snapshot_cache_policy = snapshot_cache_policy
        self.snapshot_strategy = snapshot_strategy
        self.snapshot_editor = Path(snapshot_editor) if snapshot_editor else None
        self.snapshot_concurrency = snapshot_concurrency
        self.snapshot_slots = threading.BoundedSemaphore(snapshot_concurrency)
        self.capacity, self.poll_seconds = capacity, poll_seconds
        self.warm_pool_specs = dict(warm_pool_specs or {})
        if warm_refill_workers is None:
            warm_refill_workers = min(4, capacity)
        if not isinstance(warm_refill_workers, int) or isinstance(warm_refill_workers, bool) or not 1 <= warm_refill_workers <= capacity:
            raise ValueError("Warm refill workers must be in 1..capacity")
        if not isinstance(warm_wait_ms, int) or isinstance(warm_wait_ms, bool) or not 0 <= warm_wait_ms <= 10000:
            raise ValueError("Warm wait must be in 0..10000 ms")
        if (not math.isfinite(warm_idle_quiet_seconds) or
                not 0 <= warm_idle_quiet_seconds <= 60):
            raise ValueError("Warm idle quiet period must be in 0..60 seconds")
        if (not isinstance(warm_min_memory_mib, int) or warm_min_memory_mib < 0 or
                not isinstance(warm_min_disk_gib, int) or warm_min_disk_gib < 0):
            raise ValueError("Invalid warm refill resource floor")
        self.warm_wait_ms = warm_wait_ms
        self.warm_idle_quiet_seconds = warm_idle_quiet_seconds
        self.warm_min_memory_mib = warm_min_memory_mib
        self.warm_min_disk_gib = warm_min_disk_gib
        if sum(self.warm_pool_specs.values()) > capacity:
            raise ValueError("Warm pool targets exceed sandbox capacity")
        for (environment_id, storage), target in self.warm_pool_specs.items():
            if environment_id not in self.tb2_templates or not isinstance(target, int) or target < 1:
                raise ValueError("Invalid warm pool specification")
            if storage is not None:
                store = self.verifier_artifacts_for(environment_id)
                if store is None:
                    raise ValueError("Warm pool verifier artifact is not configured")
                store.resolve(storage)
        self.lock = threading.RLock(); self.sandboxes = {}; self.closed = False
        self.warm_condition = threading.Condition(self.lock)
        self.warm_waiters = {key: deque() for key in self.warm_pool_specs}
        self.foreground_active = 0
        self.last_foreground = 0.0
        self.errors = []; self.shutdown = threading.Event()
        self.thread = threading.Thread(target=self._monitor, daemon=True)
        self.warm_wakeup = threading.Event()
        self.warm_inflight = {key: 0 for key in self.warm_pool_specs}
        self.warm_threads = [threading.Thread(target=self._refill_warm_pool, daemon=True)
                             for _ in range(min(warm_refill_workers, sum(self.warm_pool_specs.values())))]
        self.warm_hits = 0; self.warm_misses = 0; self.warm_refill_errors = 0
        if start_monitor:
            self.start_monitors()

    def start_monitors(self):
        self._require_owner()
        self.thread.start()
        for thread in self.warm_threads:
            thread.start()

    def persist(self, sandbox):
        if self.registry is not None:
            return self.registry.persist(self, sandbox)

    def _require_owner(self):
        registry = getattr(self, 'registry', None)
        if registry is not None:
            registry.require_owner()

    def detach(self):
        """Retire durable service ownership while preserving registered sandboxes."""
        if self.registry is None:
            raise SandboxError("Edge has no durable registry")
        return self.registry.detach(self)

    def warm_pool_status(self):
        return self.ready_pool.status(self)

    def foreground_enter(self):
        return self.ready_pool.foreground_enter(self)

    def foreground_exit(self):
        return self.ready_pool.foreground_exit(self)

    def _warm_resource_headroom(self):
        return self.ready_pool.resource_headroom(self)

    def _refill_warm_pool(self):
        return self.ready_pool.refill(self)

    def verifier_artifacts_for(self, environment_id):
        stores = self.tb2_verifier_artifacts
        return (stores.get(environment_id, stores.get("default"))
                if isinstance(stores, dict) else stores)

    def command_timeout_ms(self, environment_id):
        if environment_id not in self.tb2_templates:
            return 30000
        limits = self.tb2_resources.get(environment_id, {})
        return limits.get("command_timeout_ms", 900000)

    def verifier_timeout_ms(self, environment_id):
        """Trusted evaluator deadline, pinned independently of agent commands."""
        limits = self.tb2_resources.get(environment_id, {})
        return limits.get("verifier_timeout_ms", self.command_timeout_ms(environment_id))

    def create(self, idle_ttl_seconds=300, environment_id="default", memory_profile="baseline",
               verifier_storage=None, storage="local", baseline_id=None):
        self._require_owner()
        return self.provisioner.create(self, idle_ttl_seconds, environment_id, memory_profile,
                                       verifier_storage, storage, baseline_id)

    def prewarm(self, *, environment_id, verifier_storage=None, count=1,
                memory_profile="baseline", idle_ttl_seconds=3600):
        self._require_owner()
        return self.provisioner.prewarm(self, environment_id=environment_id,
            verifier_storage=verifier_storage, count=count, memory_profile=memory_profile,
            idle_ttl_seconds=idle_ttl_seconds)

    def _create_cold(self, idle_ttl_seconds=300, environment_id="default", memory_profile="baseline",
                     verifier_storage=None, reserved=False, storage="local", prepare_only=False):
        self._require_owner()
        return self.provisioner.create_cold(self, idle_ttl_seconds, environment_id, memory_profile,
            verifier_storage, reserved, storage, prepare_only)

    def _monitor(self):
        while not self.shutdown.wait(self.poll_seconds):
            with self.lock:
                items = list(self.sandboxes.values())
            for sb in items:
                if not sb.lock.acquire(blocking=False):
                    continue  # Active bounded commands are not counted as idle.
                try:
                    sb._check()
                    if sb.state == "FAILED" and time.monotonic() >= sb.deadline:
                        sb._stop("failed_ttl_expired")
                except Exception as exc:
                    self.errors.append({"id":sb.id, "error":str(exc)})
                finally:
                    sb.lock.release()

    def _retire_threads(self):
        with self.lock:
            self.closed = True
        self.shutdown.set()
        with self.warm_condition:
            self.warm_condition.notify_all()
        self.warm_wakeup.set()
        for thread in self.warm_threads:
            if thread.is_alive():
                thread.join()
        if self.thread.is_alive():
            self.thread.join(timeout=5)

    def close(self):
        self._retire_threads()
        if self.registry is not None and self.registry.owner_lock.closed:
            return  # A retired Edge cannot terminate a later owner's sandboxes.
        for sb in list(self.sandboxes.values()):
            sb.stop()
        if self.registry is not None:
            self.registry.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
