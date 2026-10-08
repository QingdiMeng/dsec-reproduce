"""Registry records and directory ownership composed into a runtime Edge.

Process attestation and native process handles are injected by the Linux host
assembly. Records reconstruct the existing Sandbox aggregate; no side effects
are replayed from an execution request journal.
"""
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Callable
from dsec.contracts.errors import SandboxError


@dataclass(frozen=True)
class RegistryOperations:
    boot_id: str
    identity: Callable
    attach_process: Callable
    process_entries: Callable
    detach_process: Callable
    new_sandbox: Callable
    new_microvm: Callable
    write_json: Callable
    sync_directory: Callable


class RegistryRecords:
    def __init__(self, operations):
        self.operations = operations

    def recover(self, manager):
        manager.recovery_events = []
        self.reconcile_network_orphans(manager)
        self.check_network_registry(manager)
        self.load(manager)
        self.find_orphans(manager)
        if manager.overlaybd_root_store:
            manager.overlaybd_root_store.shared_layers.reconcile(
                # Preserve referenced artifacts for invalid records too.
                {p.parent.name for p in manager.root.glob("*/registry.json")
                 if re.fullmatch(r"[0-9a-f]{12}", p.parent.name) and
                 (p.parent.name not in manager.sandboxes or
                  manager.sandboxes[p.parent.name].state != "STOPPED")})
        if manager.node_admission is not None:
            manager.node_admission.reconcile_microvms(manager)

    def persist(self, manager, sb):
        process = None
        if sb.vm.process is not None and sb.vm.process.poll() is None:
            try:
                process = (manager.tb2_network_manager.attest(sb.id,sb.network_slot,
                           sb.vm.process.pid) if sb.network_mode == "netns" else
                           self.operations.identity(sb.vm.process.pid))
            except (OSError,ValueError):
                pass
        self.operations.write_json(sb.directory/"registry.json", {
            "version":1,"id":sb.id,"state":sb.state,"reason":sb.reason,"ttl":sb.ttl,
            "environment_id":sb.environment_id,"memory_profile":sb.memory_profile,
            "storage":sb.storage,
            "environment_catalog_sha256":sb.environment_catalog_sha256,
            "environment_manifest_sha256":sb.environment_manifest_sha256,
            "guest_kernel":str(sb.kernel),
            "layer_disks":[str(path) for path in sb.layer_disks],
            "free_page_reporting":sb.free_page_reporting,
            "dirty_tracking_enabled":sb.dirty_tracking_enabled,
            "snapshot_mode":sb.snapshot_mode,
            "baseline_sealed":sb.baseline_sealed,"fork_origin":sb.fork_origin,
            "baseline_identity":sb.baseline_identity,
            "reserved":sb.reserved,"warm_pool_hit":sb.warm_pool_hit,
            "verifier_storage":sb.verifier_storage,
            "verifier_dax":sb.verifier_dax,
            "erofs_dax_layers":list(sb.erofs_dax_layers),
            "verifier_artifact_sha256":sb.verifier_artifact_sha,
            "network_slot":sb.network_slot,
            "network_mode":sb.network_mode,"network_ready":sb.network_ready,
            "snapshot_cache_evicted":sb.snapshot_cache_evicted,
            "rootfs_block_backend":"overlaybd-ublk" if sb.overlaybd_store else "file-ext4",
            "overlaybd_device_id":sb.overlaybd_device_id,
            "overlaybd_runtime":str(sb.overlaybd_runtime) if sb.overlaybd_runtime else None,
            "overlaybd_daemon_socket_identity":sb.overlaybd_daemon_socket_identity,
            "overlaybd_image":str(sb.overlaybd_image) if sb.overlaybd_image else None,
            "deadline_monotonic":sb.deadline,"deadline_wall":time.time()+sb.deadline-time.monotonic(),
            "boot_id":self.operations.boot_id,"snapshot":sb.snapshot.name if sb.snapshot else None,
            "generation":sb.generation,"process":process,"inflight":sb.inflight,
            "node_lease_id":sb.node_lease_id,
            "resource_cleanup_complete":sb.resource_cleanup_complete})

    def check_network_registry(self, manager):
        """Fail closed before adopting VMMs if a live slot could be reused."""
        occupied = set()
        for path in manager.root.glob("*/registry.json"):
            value = json.loads(path.read_text())
            if value.get("state") == "STOPPED":
                continue
            slot = value.get("network_slot")
            if slot is None:
                if value.get("environment_id") in manager.tb2_templates and (
                        manager.tb2_network_slots or manager.tb2_network_manager):
                    raise RuntimeError(f"Active TB2 sandbox has no network slot: {path}")
                continue
            if value.get("network_mode") == "netns":
                if manager.tb2_network_manager is None or (
                        manager.tb2_network_manager.slot_number(slot) >=
                        manager.tb2_network_manager.max_slots):
                    raise RuntimeError(f"Active sandbox uses an unavailable netns slot: {slot}")
                if value.get("network_ready"):
                    if value.get("state") == "RUNNING":
                        manager.tb2_network_manager.inspect(value["id"], slot)
                    else:
                        manager.tb2_network_manager.ensure(value["id"], slot)
                elif value.get("state") == "RUNNING":
                    # A crash can occur between helper launch and registry write.
                    manager.tb2_network_manager.inspect(value["id"], slot)
                else:
                    manager.tb2_network_manager.ensure(value["id"], slot)
            elif slot not in manager.tb2_network_slots:
                raise RuntimeError(f"Active sandbox uses an unavailable network slot: {slot}")
            if slot in occupied:
                raise RuntimeError(f"Duplicate active network slot: {slot}")
            occupied.add(slot)

    def reconcile_network_orphans(self, manager):
        if manager.tb2_network_manager is None:
            return
        active = {}
        for path in manager.root.glob("*/registry.json"):
            value = json.loads(path.read_text())
            if value.get("network_mode") == "netns" and value.get("state") != "STOPPED":
                active[value["id"]] = manager.tb2_network_manager.slot_number(value["network_slot"])
        for item in manager.tb2_network_manager.list():
            sid, number = item["id"], item["slot"]
            if sid in active:
                if active[sid] != number:
                    raise RuntimeError("Persisted netns slot disagrees with helper state")
                continue
            manager.tb2_network_manager.release(sid, f"ns-{number}")
            manager.recovery_events.append({"id":sid,"event":"released_orphan_network"})

    def load(self, manager):
        for path in sorted(manager.root.glob("*/registry.json")):
            try:
                value = json.loads(path.read_text())
                sid = path.parent.name
                if not re.fullmatch("[0-9a-f]{12}",sid) or value["id"] != sid or value["version"] != 1:
                    raise ValueError("Invalid registry identity/version")
                environment_id = value.get("environment_id", "default")
                if (value.get("state") == "STOPPED" and
                        isinstance(environment_id, str) and
                        environment_id.startswith("tb2-")):
                    store = manager.verifier_artifacts_for(environment_id)
                    storage = value.get("verifier_storage")
                    if (environment_id not in manager.tb2_templates or
                            (storage is not None and
                             (store is None or storage not in store.paths or
                              value.get("verifier_artifact_sha256") != store.sha))):
                        # An unstaged task or superseded verifier has no live
                        # VM to recover. Preserve its historical record.
                        continue
                snapshot = value["snapshot"]
                if snapshot is not None and not re.fullmatch(r"snapshot-[0-9]+",snapshot):
                    raise ValueError("Invalid snapshot path")
                sb = self.operations.new_sandbox()
                sb.manager=manager; sb.id=sid; sb.directory=path.parent; sb.disk=path.parent/"rootfs.ext4"
                sb.environment_id=value.get("environment_id", "default")
                sb.storage=value.get("storage", "local")
                sb.environment_catalog_sha256=value.get("environment_catalog_sha256")
                sb.environment_manifest_sha256=value.get("environment_manifest_sha256")
                generic=(manager.microvm_environment_catalog is not None and
                         sb.environment_id in manager.microvm_environment_catalog.entries)
                if generic:
                    current_identity=manager.microvm_environment_catalog.environment_digest(
                        sb.environment_id)
                    identity_matches=(
                        sb.environment_manifest_sha256 == current_identity
                        if sb.environment_manifest_sha256 is not None else
                        sb.environment_catalog_sha256 == manager.microvm_environment_catalog.digest)
                    if not identity_matches:
                        if value["state"] == "STOPPED":
                            continue
                        raise ValueError("Persisted microVM environment changed")
                    sb.environment_manifest_sha256=current_identity
                    sb.environment_catalog_sha256=manager.microvm_environment_catalog.digest
                    resolved=manager.disk_storage.prepare(manager.microvm_environment_catalog,
                                                          sb.environment_id, sb.storage)
                    sb.layer_disks=tuple(layer["file"] for layer in resolved["layers"])
                    sb.kernel=resolved["kernel"]
                    if value.get("layer_disks") != [str(item) for item in sb.layer_disks]:
                        raise ValueError("Persisted microVM layer sources changed")
                else:
                    if (sb.environment_catalog_sha256 is not None or
                            sb.environment_manifest_sha256 is not None or sb.storage != "local"):
                        if value["state"] == "STOPPED":
                            continue
                        raise ValueError("Persisted microVM environment is unavailable")
                    sb.layer_disks=manager.tb2_layers.get(sb.environment_id, ())
                    sb.kernel=manager.kernel
                if value.get("guest_kernel", str(manager.kernel)) != str(sb.kernel):
                    raise ValueError("Persisted guest kernel changed")
                sb.overlaybd_store=(manager.overlaybd_root_store if
                                    manager.overlaybd_root_store is not None and
                                    sb.environment_id in manager.overlaybd_root_store.source_images
                                    else None)
                if value.get("rootfs_block_backend", "file-ext4") != (
                        "overlaybd-ublk" if sb.overlaybd_store else "file-ext4"):
                    if value["state"] == "STOPPED":
                        continue
                    raise ValueError("Persisted block backend changed")
                sb.overlaybd_device_id=value.get("overlaybd_device_id")
                sb.overlaybd_runtime=(Path(value["overlaybd_runtime"])
                                      if value.get("overlaybd_runtime") else None)
                sb.overlaybd_daemon_socket_identity=value.get("overlaybd_daemon_socket_identity")
                overlaybd_service_changed=False
                if sb.overlaybd_store:
                    image=value.get("overlaybd_image")
                    if value["state"] == "STOPPED":
                        image=str(sb.overlaybd_store.source_for(sb.environment_id))
                    if not image:
                        raise ValueError("Persisted OverlayBD source is missing")
                    sb.overlaybd_image=Path(image)
                    if not sb.overlaybd_image.is_file():
                        raise ValueError("Persisted OverlayBD source is unavailable")
                    if sb.overlaybd_runtime is not None and (
                            sb.overlaybd_runtime.parent != sb.directory or
                            not re.fullmatch(r"ublk-runtime-[0-9a-f]{12}",
                                             sb.overlaybd_runtime.name)):
                        raise ValueError("Persisted OverlayBD runtime path is invalid")
                    if value["state"] == "RUNNING" and (
                            sb.overlaybd_device_id is None or
                            sb.overlaybd_runtime is None or
                            not sb.disk.is_symlink() or
                            os.readlink(sb.disk) != f"/dev/ublkb{sb.overlaybd_device_id}"):
                        raise ValueError("Running OverlayBD device identity mismatch")
                    if value["state"] == "RUNNING":
                        overlaybd_service_changed=not sb._overlaybd_service_matches()
                else:
                    sb.overlaybd_image=None
                    sb.overlaybd_device_id=None
                    sb.overlaybd_runtime=None
                    sb.overlaybd_daemon_socket_identity=None
                sb.memory_profile=value.get("memory_profile", "baseline")
                sb.free_page_reporting=value.get("free_page_reporting",
                    sb.memory_profile in ("damon_fpr","dax_damon_fpr"))
                if (value["state"] != "STOPPED" and
                        sb.free_page_reporting != (
                            (sb.environment_id in manager.tb2_templates and
                             manager.tb2_free_page_reporting) or
                            sb.memory_profile in ("damon_fpr","dax_damon_fpr"))):
                    raise ValueError("Persisted free-page reporting policy changed")
                sb.dirty_tracking_enabled=value.get("dirty_tracking_enabled",False)
                sb.snapshot_mode=value.get("snapshot_mode", "boot-diff" if sb.dirty_tracking_enabled else "full")
                sb.baseline_identity=value.get("baseline_identity")
                sb.baseline_verified=False
                sb.fork_readers=0
                sb.baseline_closing=False
                sb.baseline_sealed=value.get("baseline_sealed",False)
                sb.fork_origin=value.get("fork_origin")
                sb.node_lease_id=value.get('node_lease_id')
                sb.resource_cleanup_complete=value.get('resource_cleanup_complete', False)
                if not isinstance(sb.resource_cleanup_complete, bool):
                    raise ValueError('Invalid persisted cleanup completion flag')
                sb.reserved=value.get("reserved",False)
                sb.warm_pool_hit=value.get("warm_pool_hit",False)
                if sb.snapshot_mode not in ("full", "boot-diff", "incremental") or (
                        sb.snapshot_mode == "incremental" and manager.snapshot_editor is None):
                    raise ValueError("Persisted snapshot strategy is unavailable")
                sb.verifier_storage=value.get("verifier_storage")
                sb.verifier_dax=value.get("verifier_dax",False)
                sb.erofs_dax_layers=manager.tb2_layer_dax_indices.get(sb.environment_id, ())
                if (value["state"] != "STOPPED" and
                        value.get("erofs_dax_layers", []) != list(sb.erofs_dax_layers)):
                    raise ValueError("Persisted EROFS DAX policy changed")
                sb.verifier_artifact_sha=value.get("verifier_artifact_sha256")
                if (value["state"] != "STOPPED" and
                        sb.verifier_dax != (sb.verifier_storage is not None and
                                            sb.environment_id in manager.tb2_verifier_dax_tasks)):
                    raise ValueError("Persisted verifier DAX policy changed")
                if sb.verifier_storage is not None:
                    store = manager.verifier_artifacts_for(sb.environment_id)
                    if (sb.environment_id not in manager.tb2_templates or
                            store is None or sb.verifier_storage not in store.paths or
                            sb.verifier_artifact_sha != store.sha):
                        raise ValueError("Persisted TB2 verifier artifact unavailable or changed")
                sb.network_slot=value.get("network_slot")
                sb.network_mode=value.get("network_mode")
                sb.network_ready=(value.get("network_ready",False) or
                                  (sb.network_mode == "netns" and value["state"] != "STOPPED"))
                known_e3 = (sb.environment_id == "e3-mixed" and manager.e3 is not None and
                            sb.memory_profile in ("baseline", "dax", "damon_fpr", "dax_damon_fpr"))
                known_tb2 = (sb.environment_id in manager.tb2_templates and
                             sb.memory_profile == "baseline")
                if (sb.environment_id, sb.memory_profile) != ("default", "baseline") and not (
                        known_e3 or known_tb2):
                    raise ValueError("Unsupported persisted environment/memory profile")
                sb.work_disk=sb.directory/"work.ext4" if sb.environment_id=="e3-mixed" else None
                binary=(manager.e3["binary"] if sb.work_disk else
                        manager.generic_dax_binaries[sb.environment_id]
                        if sb.environment_id in manager.generic_dax_binaries else
                        manager.tb2_verifier_dax_binary if (sb.verifier_dax or sb.erofs_dax_layers)
                        else manager.binary)
                sb.vm=self.operations.new_microvm(binary,sb.directory,
                              max_timeout_ms=manager.command_timeout_ms(sb.environment_id))
                if sb.network_mode == "netns":
                    sb.vm.start_launcher=manager.tb2_network_manager.launcher(sid,sb.network_slot)
                sb.vm.on_process_started=sb._persist
                sb.memory_evidence=None
                sb.lock=threading.RLock(); sb.ttl=value["ttl"]
                sb.fork_condition=threading.Condition(sb.lock)
                sb.deadline=(value["deadline_monotonic"] if value["boot_id"] == self.operations.boot_id else
                             time.monotonic()+max(0,value["deadline_wall"]-time.time()))
                sb.state=value["state"]; sb.reason=value["reason"]
                sb.snapshot=sb.directory/snapshot if snapshot else None
                sb.snapshot_cache_evicted=value.get("snapshot_cache_evicted")
                sb.last_pause_phases=None
                sb.last_create_phases=None
                sb.generation=value["generation"]; sb.inflight=value["inflight"]
                manager.sandboxes[sid]=sb
                saved=value["process"]
                if saved:
                    try:
                        attester=(lambda pid, sid=sid, slot=sb.network_slot:
                                  manager.tb2_network_manager.attest(sid,slot,pid)) if sb.network_mode == "netns" else None
                        sb.vm.process=self.operations.attach_process(saved,binary,sb.vm.api_path,
                                                      attester=attester)
                    except (OSError,RuntimeError,ValueError) as exc:
                        manager.recovery_events.append({"id":sid,"event":"not_attached","reason":str(exc)})
                if sb.inflight:
                    # The in-flight request has an unknown outcome. Stop the
                    # possibly paused VMM before deleting unpublished files.
                    sb.vm.stop()
                    self.prune_uncommitted(manager, sb)
                    sb._fail("interrupted_"+sb.inflight["operation"]+"_outcome_unknown")
                    sb.inflight=None
                elif overlaybd_service_changed:
                    sb._fail("ublk_service_identity_changed_during_daemon_restart")
                elif sb.state == "RUNNING" and sb.vm.process is not None:
                    try:
                        if sb.vm.api("GET","/")["state"] != "Running":
                            raise RuntimeError("Unexpected VM state")
                        sb.vm.state="RUNNING"
                        manager.recovery_events.append({"id":sid,"event":"adopted_running_vmm"})
                    except Exception as exc:
                        sb._fail("adoption_failed: "+str(exc))
                elif sb.state == "PAUSED" and sb.snapshot and sb.snapshot.is_dir():
                    sb.vm.stop()
                    if sb.baseline_sealed and sb.overlaybd_device_id is not None:
                        sb._release_overlaybd_device()
                        sb._persist()
                    manager.recovery_events.append({"id":sid,"event":"loaded_paused_snapshot"})
                elif sb.state not in ("STOPPED","FAILED"):
                    sb._fail("vmm_missing_after_restart")
                else:
                    sb.vm.stop()
                self.prune_uncommitted(manager, sb)
                if sb.overlaybd_store and sb.state == "PAUSED" and sb.snapshot:
                    try:
                        removed = sb.overlaybd_store.prune_unreferenced_layers(
                            sb.snapshot/"disk-image.json", sb.directory,
                            sb.overlaybd_store.source_for(sb.environment_id))
                        if removed:
                            manager.recovery_events.append({"id": sid,
                                                         "event": "pruned_orphan_disk_layers",
                                                         "paths": removed})
                    except (OSError, ValueError) as exc:
                        manager.recovery_events.append({"id": sid,
                                                     "event": "disk_layer_prune_failed",
                                                     "reason": str(exc)})
                sb._check()
                if sb.state == "FAILED" and time.monotonic() >= sb.deadline:
                    sb._stop("failed_ttl_expired")
                sb._persist()
            except Exception as exc:
                # Retain invalid records for inspection; never trust their arbitrary paths/PIDs.
                partial=manager.sandboxes.pop(path.parent.name,None)
                if partial is not None:
                    partial.vm.stop()
                manager.recovery_events.append({"record":str(path),"event":"registry_error","reason":str(exc)})

    def prune_uncommitted(self, manager, sb):
        """Reclaim only private snapshot directories absent from the registry."""
        keep = sb.snapshot.name if sb.snapshot else None
        removed = []
        for path in sb.directory.iterdir():
            if path.name == keep:
                continue
            if not (re.fullmatch(r"pending-[0-9a-f]{8}", path.name) or
                    re.fullmatch(r"snapshot-[0-9]+", path.name)):
                continue
            if path.is_symlink() or not path.is_dir():
                raise ValueError(f"Unexpected snapshot path: {path}")
            shutil.rmtree(path)
            removed.append(path.name)
        if removed:
            self.operations.sync_directory(sb.directory)
            manager.recovery_events.append({"id":sb.id,"event":"pruned_uncommitted_snapshots",
                                         "paths":removed})

    def find_orphans(self, manager):
        adopted={sb.vm.process.pid for sb in manager.sandboxes.values() if sb.vm.process is not None}
        binaries={str(manager.binary.resolve())}
        if manager.e3:
            binaries.add(str(manager.e3["binary"].resolve()))
        for proc in self.operations.process_entries():
            if not proc.name.isdigit() or int(proc.name) in adopted:
                continue
            try:
                info=self.operations.identity(int(proc.name)); args=info["argv"]
                if info["uid"] != os.getuid() or info["exe"] not in binaries or len(args)!=3 or args[1]!="--api-sock":
                    continue
                api=Path(args[2])
                if api.name!="api.sock" or api.parent.parent!=manager.root or not re.fullmatch("[0-9a-f]{12}",api.parent.name):
                    continue
                process=self.operations.attach_process(info,Path(args[0]),api)
                process.terminate()
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(5)
                manager.recovery_events.append({"pid":info["pid"],"event":"stopped_unregistered_vmm","api":str(api)})
            except (OSError,RuntimeError,ValueError):
                continue


class SandboxRegistry:
    """Own one instance directory before constructing or recovering its Edge."""
    def __init__(self, root, operations):
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.owner_lock = (self.root / "manager.lock").open("a")
        try:
            fcntl.flock(self.owner_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException as exc:
            self.owner_lock.close()
            if isinstance(exc, BlockingIOError):
                raise RuntimeError("Another manager owns this directory") from exc
            raise
        self.records = RegistryRecords(operations)

    def recover(self, manager):
        self.require_owner()
        if manager.root != self.root:
            raise ValueError("Registry root disagrees with Edge root")
        self.records.recover(manager)

    def persist(self, manager, sandbox):
        self.require_owner()
        self.records.persist(manager, sandbox)

    def require_owner(self):
        if self.owner_lock.closed:
            raise SandboxError('Edge directory ownership has been retired')

    def close(self):
        self.owner_lock.close()

    def release_handles(self, manager):
        for sandbox in manager.sandboxes.values():
            with sandbox.lock:
                if sandbox.vm.log:
                    sandbox.vm.log.close()
                    sandbox.vm.log = None
                self.records.operations.detach_process(sandbox.vm.process)

    def detach(self, manager):
        self.require_owner()
        # Retiring control threads preserves living/paused sandboxes. Explicit
        # sandbox stop or Edge.close() has different resource-release semantics.
        manager._retire_threads()
        for sandbox in manager.sandboxes.values():
            with sandbox.lock:
                sandbox._persist()
        self.release_handles(manager)
        self.close()
