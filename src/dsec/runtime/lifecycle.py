"""Sandbox state and Edge composition; transitions are a separate component."""
import errno
from collections import deque
import hashlib
import json
from dsec.contracts.resources import NodeAdmissionBusy
from dsec.contracts.errors import SandboxError, ServiceBusy, CommandOutcomeUnknown
from dsec.runtime.transitions import LifecycleController
import math
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid
from dsec.storage.digest import sha
from dsec.runtime.backends.firecracker import MicroVM
from dsec.runtime.isolation.proxy import guest_proxy_command, validate_proxy_url, validate_proxy_bypass_hosts





def _copy_sparse(source, destination):
    """Keep TB2's 10-GiB logical disk sparse across create/snapshot/restore."""
    subprocess.run(["cp", "--sparse=always", "--reflink=auto", "--",
                    str(source), str(destination)], check=True)


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sparse_sha256(path):
    """Hash allocated extents and their offsets without scanning sparse holes."""
    digest = hashlib.sha256(b"dsec-sparse-sha256-v1\0")
    fd = os.open(path, os.O_RDONLY)
    try:
        size = os.fstat(fd).st_size
        digest.update(size.to_bytes(8, "big"))
        cursor = 0
        while cursor < size:
            try:
                start = os.lseek(fd, cursor, os.SEEK_DATA)
            except OSError as exc:
                if exc.errno == errno.ENXIO:
                    break  # The remaining bytes are a hole.
                raise
            end = os.lseek(fd, start, os.SEEK_HOLE)
            if not cursor <= start < end <= size:
                raise OSError("Invalid sparse extent boundaries")
            digest.update(start.to_bytes(8, "big"))
            digest.update(end.to_bytes(8, "big"))
            offset = start
            while offset < end:
                block = os.pread(fd, min(1024*1024, end-offset), offset)
                if not block:
                    raise EOFError("Sparse extent changed while hashing")
                digest.update(block)
                offset += len(block)
            cursor = end
        return digest.hexdigest()
    finally:
        os.close(fd)


def _sparse_block_sha256(path):
    """Content hash over fixed logical blocks, skipping all-hole blocks."""
    block_size = 1024 * 1024
    digest = hashlib.sha256(b"dsec-sparse-block-sha256-v2\0")
    fd = os.open(path, os.O_RDONLY)
    try:
        size = os.fstat(fd).st_size
        digest.update(size.to_bytes(8, "big"))
        extents = []
        cursor = 0
        while cursor < size:
            try:
                start = os.lseek(fd, cursor, os.SEEK_DATA)
            except OSError as exc:
                if exc.errno == errno.ENXIO:
                    break
                if exc.errno in (errno.EINVAL, errno.ENOTSUP):
                    extents = [(0, size)]  # Correct dense fallback.
                    break
                raise
            try:
                end = os.lseek(fd, start, os.SEEK_HOLE)
            except OSError as exc:
                if exc.errno in (errno.EINVAL, errno.ENOTSUP):
                    extents = [(0, size)]
                    break
                raise
            if not cursor <= start < end <= size:
                raise OSError("Invalid sparse extent boundaries")
            extents.append((start, end))
            cursor = end
        zero_digest = hashlib.sha256(bytes(block_size)).digest()
        extent_index = 0
        for offset in range(0, size, block_size):
            end = min(size, offset + block_size)
            while extent_index < len(extents) and extents[extent_index][1] <= offset:
                extent_index += 1
            has_data = (extent_index < len(extents) and extents[extent_index][0] < end)
            if not has_data:
                digest.update(zero_digest if end-offset == block_size
                              else hashlib.sha256(bytes(end-offset)).digest())
                continue
            block = bytearray()
            while len(block) < end-offset:
                part = os.pread(fd, end-offset-len(block), offset+len(block))
                if not part:
                    raise EOFError("Snapshot file changed while hashing")
                block.extend(part)
            digest.update(hashlib.sha256(block).digest())
        return digest.hexdigest()
    finally:
        os.close(fd)


def _snapshot_hash(path, algorithm):
    if algorithm == "sha256":
        return sha(path)
    if algorithm == "sparse-sha256-v1":
        return _sparse_sha256(path)
    if algorithm == "sparse-block-sha256-v2":
        return _sparse_block_sha256(path)
    raise ValueError("Unsupported snapshot hash algorithm")


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
        self.vm.on_process_started = self._persist

    def _persist(self):
        hook = getattr(self.manager, "persist", None)
        if hook:
            hook(self)

    def _snapshot_checkpoint(self, stage):
        """No-op boundary used by isolated crash-injection tests."""

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

    def _release_overlaybd_device(self):
        return self.manager.lifecycle.release_overlaybd_device(self)

    def _fail(self, reason):
        return self.manager.lifecycle.fail(self, reason)

    def _check(self):
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

    def _restore(self):
        return self.manager.lifecycle.restore(self)

    def execute(self, command, timeout_ms=5000, output_limit=65536,
                execution_scope="agent"):
        # Validate before creating side effects (including automatic resume).
        if not isinstance(command, str) or "\0" in command or len(command.encode()) > 65536:
            raise ValueError("Invalid command")
        execution_command = guest_proxy_command(
            command, getattr(self.manager, "egress_proxy_url", None),
            getattr(self.manager, "egress_proxy_bypass_hosts", ()))
        if len(execution_command.encode()) > 65536:
            raise ValueError("Command exceeds guest limit after proxy environment")
        if execution_scope not in ("agent", "verifier"):
            raise ValueError("Unknown command execution scope")
        max_timeout_ms = (self.manager.verifier_timeout_ms(self.environment_id)
                          if execution_scope == "verifier" else
                          self.manager.command_timeout_ms(self.environment_id))
        if (not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or
                not 1 <= timeout_ms <= max_timeout_ms):
            raise ValueError(f"{execution_scope} timeout_ms={timeout_ms!r} exceeds "
                             f"allowed range 1..{max_timeout_ms}")
        if (not isinstance(output_limit, int) or isinstance(output_limit, bool) or
                not 1 <= output_limit <= 1048576):
            raise ValueError("output_limit must be an integer in 1..1048576")
        with self.lock:
            if self.reserved:
                raise SandboxError("Sandbox is reserved for warm checkout")
            self._check()
            if self.state == "PAUSED":
                self._restore()
            if self.state != "RUNNING":
                raise SandboxError("Cannot execute in "+self.state)
            try:
                return self.vm.execute(execution_command, timeout_ms, output_limit)
            except (OSError, EOFError, ValueError) as exc:
                self._fail("command_transport_failed")
                raise CommandOutcomeUnknown("Command not replayed; inspect snapshot/side effects") from exc
            finally:
                self._touch()

    def pause(self):
        return self.manager.lifecycle.pause(self)

    def resume(self):
        return self.manager.lifecycle.resume(self)

    def recover(self, *, allow_rollback=False):
        return self.manager.lifecycle.recover(self, allow_rollback=allow_rollback)

    def _stop(self, reason):
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
                 egress_proxy_url=None, egress_proxy_bypass_hosts=(), node_admission=None):
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
        # Late binding keeps legacy module-level fault hooks working while
        # the transition component has no dependency on this compatibility host.
        self.lifecycle = LifecycleController(
            copy_sparse=lambda *args: _copy_sparse(*args),
            snapshot_hash=lambda *args: _snapshot_hash(*args),
            hash_file=lambda path: sha(path),
            fsync_directory=lambda path: _fsync_directory(path))
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
            manifest = store.manifest if store is not None else {}
            if (manifest.get("task_id") != task.removeprefix("tb2-") or
                    manifest.get("dax_compact") is not True or
                    manifest.get("size_bytes", 0) > 4 * 1024**3 or
                    manifest.get("size_bytes", 0) % (2 * 1024**2)):
                raise ValueError("DAX verifier requires a compact, task-pinned, 2 MiB aligned artifact")
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
            self.thread.start()
            for thread in self.warm_threads:
                thread.start()

    def warm_pool_status(self):
        with self.lock:
            pools = []
            for (environment_id, storage), target in self.warm_pool_specs.items():
                reserved = [sb for sb in self.sandboxes.values()
                            if sb.reserved and sb.environment_id == environment_id and
                            sb.verifier_storage == storage and sb.state != "STOPPED"]
                ready = sum(sb.state == "RUNNING" for sb in reserved)
                pools.append({"environment_id": environment_id, "verifier_storage": storage,
                              "target": target, "ready": ready,
                              "preparing": sum(sb.state == "CREATING" for sb in reserved),
                              "refill_workers_active": self.warm_inflight[(environment_id, storage)],
                              "waiting_requests": len(self.warm_waiters[(environment_id, storage)]),
                              "deficit": max(0, target-ready),
                              "node_wait_reasons":self.warm_node_waits.get((environment_id, storage), [])})
            return {"pools": pools, "hits": self.warm_hits, "misses": self.warm_misses,
                    "foreground_active": self.foreground_active,
                    "refill_errors": self.warm_refill_errors}

    def foreground_enter(self):
        with self.lock:
            self.foreground_active += 1
            self.last_foreground = time.monotonic()

    def foreground_exit(self):
        with self.lock:
            self.foreground_active -= 1
            self.last_foreground = time.monotonic()
        self.warm_wakeup.set()

    def _warm_resource_headroom(self):
        if self.warm_min_memory_mib:
            with open("/proc/meminfo") as stream:
                memory = next(int(line.split()[1]) // 1024 for line in stream
                              if line.startswith("MemAvailable:"))
            if memory < self.warm_min_memory_mib:
                return False
        if self.warm_min_disk_gib:
            disk = shutil.disk_usage(self.root).free // (1024**3)
            if disk < self.warm_min_disk_gib:
                return False
        return True

    def _refill_warm_pool(self):
        while not self.shutdown.is_set():
            choice = None
            with self.lock:
                active = sum(sb.state != "STOPPED" for sb in self.sandboxes.values())
                idle = (self.foreground_active == 0 and
                        time.monotonic()-self.last_foreground >= self.warm_idle_quiet_seconds)
                if not self.closed and active < self.capacity and idle:
                    for key, target in self.warm_pool_specs.items():
                        existing = sum(sb.reserved and sb.environment_id == key[0] and
                                       sb.verifier_storage == key[1] and sb.state != "STOPPED"
                                       for sb in self.sandboxes.values())
                        if existing + self.warm_inflight[key] < target:
                            choice = key
                            break
                if choice is not None and self._warm_resource_headroom():
                    self.warm_inflight[choice] += 1
                else:
                    choice = None
            if choice is None:
                self.warm_wakeup.wait(1)
                self.warm_wakeup.clear()
                continue
            try:
                self._create_cold(3600, choice[0], "baseline", choice[1], reserved=True)
                with self.lock:
                    self.warm_node_waits.pop(choice, None)
            except NodeAdmissionBusy as exc:
                with self.lock:
                    self.warm_node_waits[choice] = exc.reasons
                self.shutdown.wait(1)
            except SandboxError as exc:
                if "capacity reached" not in str(exc):
                    with self.lock:
                        self.warm_refill_errors += 1
                        self.errors.append({"component": "warm_pool", "error": str(exc)})
                self.shutdown.wait(1)
            except Exception as exc:
                with self.lock:
                    self.warm_refill_errors += 1
                    self.errors.append({"component": "warm_pool", "error": str(exc)})
                self.shutdown.wait(1)
            finally:
                with self.lock:
                    self.warm_inflight[choice] -= 1
                self.warm_wakeup.set()

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
        if baseline_id is not None:
            # Admit before fork anchor publication can modify the source.
            if getattr(self, 'node_admission', None) is not None:
                self.node_admission.allocate('microvm')
                self.node_admission.local.source_effects = True
            from dsec.runtime.fork import fork_baseline
            return fork_baseline(self, baseline_id, idle_ttl_seconds, environment_id,
                                 memory_profile, verifier_storage, storage)
        if not math.isfinite(idle_ttl_seconds) or idle_ttl_seconds <= 0:
            raise ValueError("TTL must be finite and positive")
        if storage != "local" and (self.microvm_environment_catalog is None or
                                    environment_id not in self.microvm_environment_catalog.entries):
            raise ValueError("Nonlocal storage requires a generic microVM environment")
        if self.microvm_environment_catalog is not None and \
                environment_id in self.microvm_environment_catalog.entries:
            # Validate before allocating a warm slot or creating a VM.
            self.microvm_environment_catalog.resolve(environment_id, storage)
        if verifier_storage is not None:
            store = self.verifier_artifacts_for(environment_id)
            if store is None or environment_id not in self.tb2_templates:
                raise ValueError("TB2 verifier artifact is not configured")
            store.resolve(verifier_storage)
        started = time.monotonic()
        key = (environment_id, verifier_storage)
        pooled = key in self.warm_pool_specs and memory_profile == "baseline"
        ticket = object() if pooled else None
        deadline = started + self.warm_wait_ms/1000
        selected = None
        manager_wait = 0.0
        queue_wait = 0.0
        lock_started = time.monotonic()
        with self.warm_condition:
            manager_wait += time.monotonic()-lock_started
            if self.closed:
                raise SandboxError("Manager closed")
            if pooled:
                self.warm_waiters[key].append(ticket)
            try:
                while True:
                    at_front = not pooled or self.warm_waiters[key][0] is ticket
                    if at_front:
                        for sb in self.sandboxes.values():
                            if not (sb.reserved and sb.state == "RUNNING" and
                                    sb.environment_id == environment_id and
                                    sb.memory_profile == memory_profile and
                                    sb.verifier_storage == verifier_storage):
                                continue
                            if not sb.lock.acquire(blocking=False):
                                continue
                            try:
                                sb._check()
                                if sb.reserved and sb.state == "RUNNING":
                                    selected = sb
                                    break
                            finally:
                                if selected is not sb:
                                    sb.lock.release()
                    if selected is not None:
                        try:
                            if getattr(self, 'node_admission', None) is not None:
                                self.node_admission.checkout(selected)
                        except BaseException:
                            selected.lock.release()
                            raise
                        original = (selected.reserved, selected.warm_pool_hit,
                                    selected.ttl, selected.deadline,
                                    selected.last_create_phases)
                        selected.reserved = False
                        selected.warm_pool_hit = True
                        selected.ttl = idle_ttl_seconds
                        selected.deadline = time.monotonic() + idle_ttl_seconds
                        queue_wait = time.monotonic()-started
                        if pooled:
                            self.warm_waiters[key].popleft()
                            self.warm_condition.notify_all()
                        break
                    pending = False
                    if pooled:
                        reserved = [sb for sb in self.sandboxes.values()
                                    if sb.reserved and sb.state != "STOPPED" and
                                    sb.environment_id == environment_id and
                                    sb.verifier_storage == verifier_storage]
                        active = sum(sb.state != "STOPPED" for sb in self.sandboxes.values())
                        pending = bool(self.warm_inflight[key] or any(
                            sb.state == "CREATING" for sb in reserved) or
                            (len(reserved) < self.warm_pool_specs[key] and active < self.capacity))
                    remaining = deadline-time.monotonic()
                    if not pending or remaining <= 0:
                        if pooled:
                            self.warm_misses += 1
                        break
                    self.warm_condition.wait(remaining)
                    if self.closed:
                        raise SandboxError("Manager closed")
            finally:
                if pooled and ticket in self.warm_waiters[key]:
                    self.warm_waiters[key].remove(ticket)
                    self.warm_condition.notify_all()
        if selected is not None:
            persist_started = time.monotonic()
            try:
                selected._persist()
            except Exception:
                (selected.reserved, selected.warm_pool_hit, selected.ttl,
                 selected.deadline, selected.last_create_phases) = original
                selected.lock.release()
                self.warm_wakeup.set()
                with self.warm_condition:
                    self.warm_condition.notify_all()
                raise
            selected.last_create_phases = {
                "warm_checkout": round(time.monotonic()-started, 6),
                "warm_manager_wait": round(manager_wait, 6),
                "warm_queue_wait": round(queue_wait, 6),
                "warm_persist": round(time.monotonic()-persist_started, 6)}
            selected.lock.release()
            if pooled:
                with self.lock:
                    self.warm_hits += 1
            self.warm_wakeup.set()
            return selected
        return self._create_cold(idle_ttl_seconds, environment_id, memory_profile,
                                 verifier_storage, storage=storage)

    def prewarm(self, *, environment_id, verifier_storage=None, count=1,
                memory_profile="baseline", idle_ttl_seconds=3600):
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= self.capacity:
            raise ValueError("Prewarm count must be in 1..capacity")
        if environment_id not in self.tb2_templates or memory_profile != "baseline":
            raise ValueError("Prewarm currently supports configured TB2 tasks only")
        prepared = []
        for _ in range(count):
            sb = self._create_cold(idle_ttl_seconds, environment_id, memory_profile,
                                   verifier_storage, reserved=True)
            prepared.append(sb.status())
        return prepared

    def _create_cold(self, idle_ttl_seconds=300, environment_id="default", memory_profile="baseline",
                     verifier_storage=None, reserved=False, storage="local", prepare_only=False):
        stage = "admission"
        stage_started = time.monotonic()
        phases = {}

        def advance(next_stage):
            nonlocal stage, stage_started
            now = time.monotonic()
            phases[stage] = round(now-stage_started, 6)
            stage, stage_started = next_stage, now

        if not math.isfinite(idle_ttl_seconds) or idle_ttl_seconds <= 0:
            raise ValueError("TTL must be finite and positive")
        environment_spec = (self.microvm_environment_catalog.resolve(environment_id, storage)
                            if self.microvm_environment_catalog is not None and
                            environment_id in self.microvm_environment_catalog.entries else None)
        if storage != "local" and environment_spec is None:
            raise ValueError("Nonlocal storage requires a generic microVM environment")
        if (environment_id, memory_profile) != ("default", "baseline"):
            if environment_id == "e3-mixed" and memory_profile in (
                    "baseline", "dax", "damon_fpr", "dax_damon_fpr"):
                if self.e3 is None:
                    raise SandboxError("E3 artifacts are not configured")
            elif environment_id not in self.tb2_templates or memory_profile != "baseline":
                raise ValueError("Unsupported environment/memory profile")
        if verifier_storage is not None:
            store = self.verifier_artifacts_for(environment_id)
            if environment_id not in self.tb2_templates or store is None:
                raise ValueError("TB2 verifier artifact is not configured")
            verifier_disk = store.resolve(verifier_storage)
        else:
            verifier_disk = None
        with self.lock:
            if self.closed:
                raise SandboxError("Manager closed")
            if sum(s.state != "STOPPED" for s in self.sandboxes.values()) >= self.capacity:
                if (getattr(self, 'node_admission', None) is not None and
                        not getattr(self.node_admission.local, 'source_effects', False)):
                    raise NodeAdmissionBusy(['sandbox_capacity'])
                raise SandboxError("Sandbox capacity reached")
            network_slot = None
            if environment_id in self.tb2_templates and self.tb2_network_manager:
                occupied = {s.network_slot for s in self.sandboxes.values()
                            if s.state != "STOPPED"}
                network_slot = self.tb2_network_manager.allocate(occupied)
                if network_slot is None:
                    if (getattr(self, 'node_admission', None) is not None and
                            not getattr(self.node_admission.local, 'source_effects', False)):
                        raise NodeAdmissionBusy(['network_slots'])
                    raise SandboxError("TB2 network namespace capacity reached")
            elif environment_id in self.tb2_templates and self.tb2_network_slots:
                occupied = {s.network_slot for s in self.sandboxes.values()
                            if s.state != "STOPPED"}
                network_slot = next((name for name in self.tb2_network_slots
                                     if name not in occupied), None)
                if network_slot is None:
                    if (getattr(self, 'node_admission', None) is not None and
                            not getattr(self.node_admission.local, 'source_effects', False)):
                        raise NodeAdmissionBusy(['network_slots'])
                    raise SandboxError("TB2 network slot capacity reached")
            lease_id = None
            if getattr(self, 'node_admission', None) is not None:
                lease_id = self.node_admission.allocate('microvm', configured_limits={
                    'environment_id':environment_id,
                    'cpu_count':self.tb2_resources.get(environment_id, {}).get('cpus', 1),
                    'memory_mb':(self.tb2_resources.get(environment_id, {}).get('memory_mb', 2048)
                                 if environment_id in self.tb2_templates else
                                 512 if environment_id == 'e3-mixed' else 256)})
            try:
                sb = Sandbox(self, idle_ttl_seconds, environment_id, memory_profile,
                             verifier_storage, reserved=reserved, storage=storage,
                             environment_spec=environment_spec)
            except BaseException:
                # Internal pool creation has no RPC request context to release
                # an unbound intent. No host handles have been allocated yet.
                if lease_id is not None:
                    self.node_admission.release_unbound(lease_id)
                raise
            sb.node_lease_id = lease_id
            sb.network_slot = network_slot
            sb.network_mode = ("netns" if self.tb2_network_manager and network_slot else
                               "tap_pool" if network_slot else
                               "legacy_tap" if self.tb2_network_tap and
                               environment_id in self.tb2_templates else None)
            self.sandboxes[sb.id] = sb
            # Only admission and slot reservation need the manager lock.
            # Hold this sandbox's lock across preparation so monitor/close
            # cannot observe or stop a half-built VM.
            sb.lock.acquire()
        try:
            if getattr(self, 'node_admission', None) is not None:
                self.node_admission.bind(lease_id, sb.id)
            sb._persist()
            advance("network_setup")
            network=None
            if sb.network_mode == "netns":
                network=self.tb2_network_manager.ensure(sb.id, sb.network_slot)
                sb.network_ready=True
                sb.vm.start_launcher=self.tb2_network_manager.launcher(sb.id,
                                                                       sb.network_slot)
                sb._persist()
            elif sb.network_mode == "tap_pool":
                network=self.tb2_network_slots[network_slot]
            if prepare_only:
                return sb
            advance("rootfs_copy")
            source = (self.e3["guest"] if sb.work_disk else
                      self.tb2_templates.get(environment_id, self.template))
            if sb.overlaybd_store:
                sb.overlaybd_device_id, sb.overlaybd_runtime = sb.overlaybd_store.create(
                    sb.overlaybd_image, sb.directory, sb.disk)
                sb.overlaybd_daemon_socket_identity = sb.overlaybd_store.socket_identity()
                sb._persist()
            elif environment_id in self.tb2_templates:
                _copy_sparse(source, sb.disk)
            else:
                shutil.copy2(source, sb.disk)
            if not sb.overlaybd_store:
                sb.disk.chmod(0o600)
            if sb.work_disk:
                shutil.copy2(self.e3["work_template"], sb.work_disk)
                sb.work_disk.chmod(0o600)
                advance("vm_boot")
                sb.vm.boot(sb.kernel, sb.disk, memory_profile=sb.memory_profile,
                           data=self.e3["data"], work=sb.work_disk)
                sb.memory_evidence = sb.vm.memory_probe(sb.memory_profile)
            else:
                tb2_spec = self.tb2_resources.get(environment_id, {})
                advance("vm_boot")
                sb.vm.boot(sb.kernel, sb.disk,
                           memory_mib=tb2_spec.get("memory_mb", 2048) if environment_id in self.tb2_templates else None,
                           cpu_count=tb2_spec.get("cpus", 1),
                           tap_name=self.tb2_network_tap if environment_id in self.tb2_templates else None,
                           network=network, verifier_disk=verifier_disk,
                           verifier_dax=sb.verifier_dax,
                           layer_disks=sb.layer_disks,
                           layer_dax_indices=sb.erofs_dax_layers,
                           track_dirty_pages=sb.dirty_tracking_enabled,
                           free_page_reporting=sb.free_page_reporting)
            advance("publish")
            sb._event("RUNNING"); sb._touch()
            advance("done")
            sb.last_create_phases = phases
            if getattr(self, 'node_admission', None) is not None:
                self.node_admission.created('microvm', sb.id, ready=sb.reserved)
            return sb
        except Exception as exc:
            advance("failed")
            sb.last_create_phases = phases
            sb._fail("create_failed: "+str(exc))
            try:
                sb._stop("create_failed_cleanup")
            except Exception as cleanup_exc:
                raise SandboxError(f"create failed: {exc}; cleanup failed: {cleanup_exc}") from exc
            raise
        finally:
            sb.lock.release()
            if reserved and sb.state == "RUNNING":
                with self.warm_condition:
                    self.warm_condition.notify_all()

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

    def close(self):
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
        for sb in list(self.sandboxes.values()):
            sb.stop()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
