"""Local durable registry. Uses pidfds to avoid signalling reused PIDs."""
import fcntl
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import subprocess
import threading
import time
from dsec.runtime.backends.firecracker import MicroVM
from dsec.runtime.lifecycle import Sandbox, SandboxManager, _fsync_directory

BOOT_ID = Path("/proc/sys/kernel/random/boot_id").read_text().strip()

def identity(pid):
    proc = Path(f"/proc/{pid}")
    fields = (proc/"stat").read_text().rsplit(")",1)[1].split()
    return {"pid":pid,"start_ticks":fields[19],"boot_id":BOOT_ID,
            "exe":str((proc/"exe").resolve(strict=True)),
            "argv":(proc/"cmdline").read_bytes().decode().strip("\0").split("\0"),
            "uid":proc.stat().st_uid}

class AttachedProcess:
    def __init__(self, saved, binary, api_path, *, attester=None):
        self.pid = saved["pid"]
        self.fd = os.pidfd_open(self.pid)
        try:
            current = attester(self.pid) if attester else identity(self.pid)
            if current != saved or current["uid"] != os.getuid() or current["exe"] != str(Path(binary).resolve()):
                raise RuntimeError("VMM process identity mismatch")
            if current["argv"] != [str(binary),"--api-sock",str(api_path)]:
                raise RuntimeError("Unexpected VMM command line")
        except Exception:
            os.close(self.fd); self.fd = None
            raise
    def poll(self):
        if self.fd is None:
            return 0
        return 0 if select.select([self.fd],[],[],0)[0] else None
    def terminate(self):
        if self.poll() is None:
            signal.pidfd_send_signal(self.fd, signal.SIGTERM)
    def kill(self):
        if self.poll() is None:
            signal.pidfd_send_signal(self.fd, signal.SIGKILL)
    def wait(self, timeout):
        if self.fd is not None and not select.select([self.fd],[],[],timeout)[0]:
            raise subprocess.TimeoutExpired("attached VMM",timeout)
        self.close()
        return 0
    def close(self):
        if self.fd is not None:
            os.close(self.fd); self.fd = None
    def __del__(self):
        if getattr(self,"fd",None) is not None:
            self.close()

def atomic_json(path, value):
    temp = path.with_suffix(".tmp")
    with temp.open("w") as stream:
        json.dump(value,stream,indent=2); stream.flush(); os.fsync(stream.fileno())
    os.replace(temp,path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

class DurableManager(SandboxManager):
    def __init__(self, root, binary, kernel, template, **kwargs):
        root = Path(root).resolve(); root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.registry_lock = (root/"manager.lock").open("a")
        try:
            fcntl.flock(self.registry_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.registry_lock.close()
            raise RuntimeError("Another manager owns this directory")
        try:
            super().__init__(root,Path(binary).resolve(),Path(kernel).resolve(),Path(template).resolve(),start_monitor=False,**kwargs)
            self.recovery_events = []
            self._reconcile_network_orphans()
            self._check_network_registry()
            self._load()
            self._find_orphans()
            if self.overlaybd_root_store:
                self.overlaybd_root_store.shared_layers.reconcile(
                    # Retain holds for invalid records too: their checkpoints
                    # remain evidence and must not lose shared backing objects.
                    {p.parent.name for p in self.root.glob("*/registry.json")
                     if re.fullmatch(r"[0-9a-f]{12}", p.parent.name) and
                     (p.parent.name not in self.sandboxes or
                      self.sandboxes[p.parent.name].state != "STOPPED")})
            self.thread.start()
            for thread in self.warm_threads:
                thread.start()
        except Exception:
            self.registry_lock.close()
            raise

    def persist(self, sb):
        process = None
        if sb.vm.process is not None and sb.vm.process.poll() is None:
            try:
                process = (self.tb2_network_manager.attest(sb.id,sb.network_slot,
                           sb.vm.process.pid) if sb.network_mode == "netns" else
                           identity(sb.vm.process.pid))
            except (OSError,ValueError):
                pass
        atomic_json(sb.directory/"registry.json", {
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
            "boot_id":BOOT_ID,"snapshot":sb.snapshot.name if sb.snapshot else None,
            "generation":sb.generation,"process":process,"inflight":sb.inflight})

    def _check_network_registry(self):
        """Fail closed before adopting VMMs if a live slot could be reused."""
        occupied = set()
        for path in self.root.glob("*/registry.json"):
            value = json.loads(path.read_text())
            if value.get("state") == "STOPPED":
                continue
            slot = value.get("network_slot")
            if slot is None:
                if value.get("environment_id") in self.tb2_templates and (
                        self.tb2_network_slots or self.tb2_network_manager):
                    raise RuntimeError(f"Active TB2 sandbox has no network slot: {path}")
                continue
            if value.get("network_mode") == "netns":
                if self.tb2_network_manager is None or (
                        self.tb2_network_manager.slot_number(slot) >=
                        self.tb2_network_manager.max_slots):
                    raise RuntimeError(f"Active sandbox uses an unavailable netns slot: {slot}")
                if value.get("network_ready"):
                    if value.get("state") == "RUNNING":
                        self.tb2_network_manager.inspect(value["id"], slot)
                    else:
                        self.tb2_network_manager.ensure(value["id"], slot)
                elif value.get("state") == "RUNNING":
                    # A crash can occur between helper launch and registry write.
                    self.tb2_network_manager.inspect(value["id"], slot)
                else:
                    self.tb2_network_manager.ensure(value["id"], slot)
            elif slot not in self.tb2_network_slots:
                raise RuntimeError(f"Active sandbox uses an unavailable network slot: {slot}")
            if slot in occupied:
                raise RuntimeError(f"Duplicate active network slot: {slot}")
            occupied.add(slot)

    def _reconcile_network_orphans(self):
        if self.tb2_network_manager is None:
            return
        active = {}
        for path in self.root.glob("*/registry.json"):
            value = json.loads(path.read_text())
            if value.get("network_mode") == "netns" and value.get("state") != "STOPPED":
                active[value["id"]] = self.tb2_network_manager.slot_number(value["network_slot"])
        for item in self.tb2_network_manager.list():
            sid, number = item["id"], item["slot"]
            if sid in active:
                if active[sid] != number:
                    raise RuntimeError("Persisted netns slot disagrees with helper state")
                continue
            self.tb2_network_manager.release(sid, f"ns-{number}")
            self.recovery_events.append({"id":sid,"event":"released_orphan_network"})

    def _load(self):
        for path in sorted(self.root.glob("*/registry.json")):
            try:
                value = json.loads(path.read_text())
                sid = path.parent.name
                if not re.fullmatch("[0-9a-f]{12}",sid) or value["id"] != sid or value["version"] != 1:
                    raise ValueError("Invalid registry identity/version")
                environment_id = value.get("environment_id", "default")
                if (value.get("state") == "STOPPED" and
                        isinstance(environment_id, str) and
                        environment_id.startswith("tb2-")):
                    store = self.verifier_artifacts_for(environment_id)
                    storage = value.get("verifier_storage")
                    if (environment_id not in self.tb2_templates or
                            (storage is not None and
                             (store is None or storage not in store.paths or
                              value.get("verifier_artifact_sha256") != store.sha))):
                        # An unstaged task or superseded verifier has no live
                        # VM to recover. Preserve its historical record.
                        continue
                snapshot = value["snapshot"]
                if snapshot is not None and not re.fullmatch(r"snapshot-[0-9]+",snapshot):
                    raise ValueError("Invalid snapshot path")
                sb = Sandbox.__new__(Sandbox)
                sb.manager=self; sb.id=sid; sb.directory=path.parent; sb.disk=path.parent/"rootfs.ext4"
                sb.environment_id=value.get("environment_id", "default")
                sb.storage=value.get("storage", "local")
                sb.environment_catalog_sha256=value.get("environment_catalog_sha256")
                sb.environment_manifest_sha256=value.get("environment_manifest_sha256")
                generic=(self.microvm_environment_catalog is not None and
                         sb.environment_id in self.microvm_environment_catalog.entries)
                if generic:
                    current_identity=self.microvm_environment_catalog.environment_digest(
                        sb.environment_id)
                    identity_matches=(
                        sb.environment_manifest_sha256 == current_identity
                        if sb.environment_manifest_sha256 is not None else
                        sb.environment_catalog_sha256 == self.microvm_environment_catalog.digest)
                    if not identity_matches:
                        if value["state"] == "STOPPED":
                            continue
                        raise ValueError("Persisted microVM environment changed")
                    sb.environment_manifest_sha256=current_identity
                    sb.environment_catalog_sha256=self.microvm_environment_catalog.digest
                    resolved=self.microvm_environment_catalog.resolve(sb.environment_id,
                                                                       sb.storage)
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
                    sb.layer_disks=self.tb2_layers.get(sb.environment_id, ())
                    sb.kernel=self.kernel
                if value.get("guest_kernel", str(self.kernel)) != str(sb.kernel):
                    raise ValueError("Persisted guest kernel changed")
                sb.overlaybd_store=(self.overlaybd_root_store if
                                    self.overlaybd_root_store is not None and
                                    sb.environment_id in self.overlaybd_root_store.source_images
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
                            (sb.environment_id in self.tb2_templates and
                             self.tb2_free_page_reporting) or
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
                sb.reserved=value.get("reserved",False)
                sb.warm_pool_hit=value.get("warm_pool_hit",False)
                if sb.snapshot_mode not in ("full", "boot-diff", "incremental") or (
                        sb.snapshot_mode == "incremental" and self.snapshot_editor is None):
                    raise ValueError("Persisted snapshot strategy is unavailable")
                sb.verifier_storage=value.get("verifier_storage")
                sb.verifier_dax=value.get("verifier_dax",False)
                sb.erofs_dax_layers=self.tb2_layer_dax_indices.get(sb.environment_id, ())
                if (value["state"] != "STOPPED" and
                        value.get("erofs_dax_layers", []) != list(sb.erofs_dax_layers)):
                    raise ValueError("Persisted EROFS DAX policy changed")
                sb.verifier_artifact_sha=value.get("verifier_artifact_sha256")
                if (value["state"] != "STOPPED" and
                        sb.verifier_dax != (sb.verifier_storage is not None and
                                            sb.environment_id in self.tb2_verifier_dax_tasks)):
                    raise ValueError("Persisted verifier DAX policy changed")
                if sb.verifier_storage is not None:
                    store = self.verifier_artifacts_for(sb.environment_id)
                    if (sb.environment_id not in self.tb2_templates or
                            store is None or sb.verifier_storage not in store.paths or
                            sb.verifier_artifact_sha != store.sha):
                        raise ValueError("Persisted TB2 verifier artifact unavailable or changed")
                sb.network_slot=value.get("network_slot")
                sb.network_mode=value.get("network_mode")
                sb.network_ready=(value.get("network_ready",False) or
                                  (sb.network_mode == "netns" and value["state"] != "STOPPED"))
                known_e3 = (sb.environment_id == "e3-mixed" and self.e3 is not None and
                            sb.memory_profile in ("baseline", "dax", "damon_fpr", "dax_damon_fpr"))
                known_tb2 = (sb.environment_id in self.tb2_templates and
                             sb.memory_profile == "baseline")
                if (sb.environment_id, sb.memory_profile) != ("default", "baseline") and not (
                        known_e3 or known_tb2):
                    raise ValueError("Unsupported persisted environment/memory profile")
                sb.work_disk=sb.directory/"work.ext4" if sb.environment_id=="e3-mixed" else None
                binary=(self.e3["binary"] if sb.work_disk else
                        self.generic_dax_binaries[sb.environment_id]
                        if sb.environment_id in self.generic_dax_binaries else
                        self.tb2_verifier_dax_binary if (sb.verifier_dax or sb.erofs_dax_layers)
                        else self.binary)
                sb.vm=MicroVM(binary,sb.directory,
                              max_timeout_ms=self.command_timeout_ms(sb.environment_id))
                if sb.network_mode == "netns":
                    sb.vm.start_launcher=self.tb2_network_manager.launcher(sid,sb.network_slot)
                sb.vm.on_process_started=sb._persist
                sb.memory_evidence=None
                sb.lock=threading.RLock(); sb.ttl=value["ttl"]
                sb.fork_condition=threading.Condition(sb.lock)
                sb.deadline=(value["deadline_monotonic"] if value["boot_id"] == BOOT_ID else
                             time.monotonic()+max(0,value["deadline_wall"]-time.time()))
                sb.state=value["state"]; sb.reason=value["reason"]
                sb.snapshot=sb.directory/snapshot if snapshot else None
                sb.snapshot_cache_evicted=value.get("snapshot_cache_evicted")
                sb.last_pause_phases=None
                sb.last_create_phases=None
                sb.generation=value["generation"]; sb.inflight=value["inflight"]
                self.sandboxes[sid]=sb
                saved=value["process"]
                if saved:
                    try:
                        attester=(lambda pid, sid=sid, slot=sb.network_slot:
                                  self.tb2_network_manager.attest(sid,slot,pid)) if sb.network_mode == "netns" else None
                        sb.vm.process=AttachedProcess(saved,binary,sb.vm.api_path,
                                                      attester=attester)
                    except (OSError,RuntimeError,ValueError) as exc:
                        self.recovery_events.append({"id":sid,"event":"not_attached","reason":str(exc)})
                if sb.inflight:
                    # The in-flight request has an unknown outcome. Stop the
                    # possibly paused VMM before deleting unpublished files.
                    sb.vm.stop()
                    self._prune_uncommitted(sb)
                    sb._fail("interrupted_"+sb.inflight["operation"]+"_outcome_unknown")
                    sb.inflight=None
                elif overlaybd_service_changed:
                    sb._fail("ublk_service_identity_changed_during_daemon_restart")
                elif sb.state == "RUNNING" and sb.vm.process is not None:
                    try:
                        if sb.vm.api("GET","/")["state"] != "Running":
                            raise RuntimeError("Unexpected VM state")
                        sb.vm.state="RUNNING"
                        self.recovery_events.append({"id":sid,"event":"adopted_running_vmm"})
                    except Exception as exc:
                        sb._fail("adoption_failed: "+str(exc))
                elif sb.state == "PAUSED" and sb.snapshot and sb.snapshot.is_dir():
                    sb.vm.stop()
                    if sb.baseline_sealed and sb.overlaybd_device_id is not None:
                        sb._release_overlaybd_device()
                        sb._persist()
                    self.recovery_events.append({"id":sid,"event":"loaded_paused_snapshot"})
                elif sb.state not in ("STOPPED","FAILED"):
                    sb._fail("vmm_missing_after_restart")
                else:
                    sb.vm.stop()
                self._prune_uncommitted(sb)
                if sb.overlaybd_store and sb.state == "PAUSED" and sb.snapshot:
                    try:
                        removed = sb.overlaybd_store.prune_unreferenced_layers(
                            sb.snapshot/"disk-image.json", sb.directory,
                            sb.overlaybd_store.source_for(sb.environment_id))
                        if removed:
                            self.recovery_events.append({"id": sid,
                                                         "event": "pruned_orphan_disk_layers",
                                                         "paths": removed})
                    except (OSError, ValueError) as exc:
                        self.recovery_events.append({"id": sid,
                                                     "event": "disk_layer_prune_failed",
                                                     "reason": str(exc)})
                sb._check()
                if sb.state == "FAILED" and time.monotonic() >= sb.deadline:
                    sb._stop("failed_ttl_expired")
                sb._persist()
            except Exception as exc:
                # Retain invalid records for inspection; never trust their arbitrary paths/PIDs.
                partial=self.sandboxes.pop(path.parent.name,None)
                if partial is not None:
                    partial.vm.stop()
                self.recovery_events.append({"record":str(path),"event":"registry_error","reason":str(exc)})

    def _prune_uncommitted(self, sb):
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
            _fsync_directory(sb.directory)
            self.recovery_events.append({"id":sb.id,"event":"pruned_uncommitted_snapshots",
                                         "paths":removed})

    def _find_orphans(self):
        adopted={sb.vm.process.pid for sb in self.sandboxes.values() if sb.vm.process is not None}
        binaries={str(self.binary.resolve())}
        if self.e3:
            binaries.add(str(self.e3["binary"].resolve()))
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit() or int(proc.name) in adopted:
                continue
            try:
                info=identity(int(proc.name)); args=info["argv"]
                if info["uid"] != os.getuid() or info["exe"] not in binaries or len(args)!=3 or args[1]!="--api-sock":
                    continue
                api=Path(args[2])
                if api.name!="api.sock" or api.parent.parent!=self.root or not re.fullmatch("[0-9a-f]{12}",api.parent.name):
                    continue
                process=AttachedProcess(info,Path(args[0]),api)
                process.terminate()
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(5)
                self.recovery_events.append({"pid":info["pid"],"event":"stopped_unregistered_vmm","api":str(api)})
            except (OSError,RuntimeError,ValueError):
                continue

    def detach(self):
        """Graceful service stop preserves registration and living/paused sandboxes."""
        self.closed=True; self.shutdown.set(); self.warm_wakeup.set()
        with self.warm_condition:
            self.warm_condition.notify_all()
        for thread in self.warm_threads:
            if thread.is_alive():
                thread.join()
        self.thread.join(timeout=5)
        for sb in self.sandboxes.values():
            with sb.lock:
                sb._persist()
                if sb.vm.log:
                    sb.vm.log.close(); sb.vm.log=None
                if isinstance(sb.vm.process,AttachedProcess):
                    sb.vm.process.close()
        self.registry_lock.close()
