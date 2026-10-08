"""MicroVM state transitions composed into Edge without a second state owner.

Sandbox is the authoritative state/journal aggregate. This controller holds only
file operations; it uses the same locks, Driver and Storage handles as before.
It does not replay commands, launch tasks or manage models.
"""
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from dsec.contracts.errors import SandboxError
from dsec.contracts.storage import DiskStorage


class LifecycleController:
    def __init__(self, *, storage: DiskStorage, hash_file, fsync_directory):
        self.storage = storage
        self.hash_file = hash_file
        self.fsync_directory = fsync_directory

    def overlaybd_service_matches(self, sandbox):
        return sandbox.manager.disk_storage.service_matches(sandbox.overlaybd_store,
            sandbox.overlaybd_device_id, sandbox.overlaybd_daemon_socket_identity)

    def release_overlaybd_device(self, sandbox):
        if not sandbox.overlaybd_store or sandbox.overlaybd_device_id is None:
            return
        sandbox.manager.disk_storage.release_device(sandbox.overlaybd_store,
            sandbox.overlaybd_device_id, sandbox.overlaybd_runtime, sandbox.directory,
            sandbox.overlaybd_daemon_socket_identity)
        sandbox.overlaybd_device_id = None
        sandbox.overlaybd_runtime = None
        sandbox.overlaybd_daemon_socket_identity = None

    def fail(self, sandbox, reason):
        sandbox.vm.stop()
        if sandbox.overlaybd_store and sandbox.overlaybd_device_id is not None:
            try:
                sandbox._release_overlaybd_device()
            except Exception as exc:
                reason += "; ublk_cleanup_failed: " + str(exc)
        sandbox._event("FAILED", reason)

    def check(self, sandbox):
        if sandbox.state in ("RUNNING", "PAUSED") and time.monotonic() >= sandbox.deadline:
            sandbox._stop("idle_ttl_expired")
        if sandbox.state == "RUNNING" and not sandbox._overlaybd_service_matches():
            sandbox._fail("ublk_service_identity_changed")
        if sandbox.state == "RUNNING" and (
                sandbox.vm.process is None or sandbox.vm.process.poll() is not None):
            sandbox._fail("vmm_exited")

    def restore(self, sandbox):
        if sandbox.baseline_sealed:
            raise SandboxError("Sealed baseline cannot execute or resume")
        if sandbox.snapshot is None:
            raise SandboxError("No snapshot available")
        try:
            manifest = json.loads((sandbox.snapshot/"manifest.json").read_text())
            if (manifest.get("environment_id", "default") != sandbox.environment_id
                    or manifest.get("storage", "local") != sandbox.storage
                    or (manifest.get("environment_manifest_sha256") !=
                        sandbox.environment_manifest_sha256
                        if manifest.get("environment_manifest_sha256") is not None else
                        manifest.get("environment_catalog_sha256") !=
                        sandbox.environment_catalog_sha256)
                    or manifest.get("guest_kernel", str(sandbox.kernel)) != str(sandbox.kernel)
                    or manifest.get("layer_disks", [str(path) for path in sandbox.layer_disks]) !=
                    [str(path) for path in sandbox.layer_disks]
                    or manifest.get("memory_profile", "baseline") != sandbox.memory_profile
                    or manifest.get("free_page_reporting", sandbox.memory_profile in (
                        "damon_fpr", "dax_damon_fpr")) != sandbox.free_page_reporting
                    or manifest.get("snapshot_mode", sandbox.snapshot_mode) != sandbox.snapshot_mode
                    or manifest.get("network_slot") != sandbox.network_slot
                    or manifest.get("network_mode") != sandbox.network_mode
                    or manifest.get("verifier_storage") != sandbox.verifier_storage
                    or manifest.get("verifier_dax", False) != sandbox.verifier_dax
                    or manifest.get("erofs_dax_layers", []) != list(sandbox.erofs_dax_layers)
                    or manifest.get("verifier_artifact_sha256") != sandbox.verifier_artifact_sha):
                raise SandboxError("Snapshot environment/memory profile mismatch")
            if sandbox.environment_catalog_sha256 is not None:
                spec = sandbox.manager.disk_storage.prepare(sandbox.manager.microvm_environment_catalog,
                    sandbox.environment_id, sandbox.storage)
                if (sandbox.manager.microvm_environment_catalog.digest !=
                        sandbox.environment_catalog_sha256 or
                        spec["environment_sha256"] != sandbox.environment_manifest_sha256 or
                        tuple(layer["file"] for layer in spec["layers"]) != sandbox.layer_disks):
                    raise SandboxError("Snapshot EROFS source identity changed")
            if sandbox.verifier_storage is not None:
                sandbox.manager.verifier_artifacts_for(sandbox.environment_id).resolve(sandbox.verifier_storage)
            if manifest["binary_sha256"] != self.hash_file(Path(sandbox.vm.binary)):
                raise SandboxError("VMM binary changed")
            overlaybd = sandbox.overlaybd_store is not None
            sandbox.manager.disk_storage.verify_checkpoint(sandbox.disk_paths(),
                sandbox.snapshot, manifest, store=sandbox.overlaybd_store,
                source_image=(sandbox.overlaybd_store.source_for(sandbox.environment_id)
                              if overlaybd else None))
            if sandbox.work_disk and (manifest.get("shared_data_sha256") != sandbox.manager.e3["data_sha256"]
                                   or self.hash_file(sandbox.manager.e3["data"]) != manifest["shared_data_sha256"]):
                raise SandboxError("E3 shared read-only data changed")
            if overlaybd:
                sandbox.overlaybd_image = sandbox.snapshot/"disk-image.json"
                sandbox.overlaybd_device_id, sandbox.overlaybd_runtime = sandbox.manager.disk_storage.restore_disk(
                    sandbox.disk_paths(), sandbox.snapshot, store=sandbox.overlaybd_store)
                sandbox.overlaybd_daemon_socket_identity = sandbox.manager.disk_storage.device_identity(sandbox.overlaybd_store)
                sandbox._persist()
            else:
                sandbox.manager.disk_storage.restore_disk(sandbox.disk_paths(), sandbox.snapshot,
                    sparse=sandbox.environment_id in sandbox.manager.tb2_templates)
            if sandbox.work_disk:
                sandbox.manager.disk_storage.copy_file(sandbox.snapshot/"work.ext4", sandbox.work_disk, mode=0o600)
            sandbox.vm.restore(sandbox.snapshot/"state", sandbox.snapshot/"memory",
                            track_dirty_pages=sandbox.snapshot_mode == "incremental")
            if sandbox.work_disk:
                sandbox.memory_evidence = sandbox.vm.memory_probe(sandbox.memory_profile)
            sandbox._event("RUNNING"); sandbox._touch()
        except Exception as exc:
            sandbox._fail("restore_failed: "+str(exc))
            raise

    def pause(self, sandbox):
        """Snapshot + stop the VMM, releasing its runtime memory."""
        with sandbox.lock:
            if sandbox.reserved:
                raise SandboxError("Sandbox is reserved for warm checkout")
            sandbox._check()
            if sandbox.state == "PAUSED":
                return
            if sandbox.state != "RUNNING":
                raise SandboxError("Cannot pause in "+sandbox.state)
            staging = sandbox.directory/("pending-"+uuid.uuid4().hex[:8])
            staging.mkdir(mode=0o700)
            if sandbox.overlaybd_store:
                sandbox.overlaybd_store.share_directory(staging)
            previous = sandbox.snapshot
            target = sandbox.directory/("snapshot-"+str(sandbox.generation+1))
            stage = "guest_sync"
            stage_started = time.monotonic()
            phases = {}
            incremental_rebase = sandbox.snapshot_mode == "incremental" and sandbox.generation > 0
            snapshot_type = ("Diff" if sandbox.dirty_tracking_enabled and
                             (sandbox.generation == 0 or incremental_rebase) else "Full")

            def advance(next_stage):
                nonlocal stage, stage_started
                now = time.monotonic()
                phases[stage] = round(now-stage_started, 6)
                stage, stage_started = next_stage, now

            try:
                if sandbox.vm.execute("sync")["exit_code"] != 0:
                    raise SandboxError("Guest sync failed")
                advance("vm_pause")
                sandbox.vm.pause()
                advance("snapshot_gate_wait")
                with sandbox.manager.snapshot_slots:
                    advance("snapshot_create")
                    sandbox.vm.api("PUT", "/snapshot/create", {"snapshot_type":snapshot_type,
                                "snapshot_path":str(staging/"state"),
                                "mem_file_path":str(staging/("diff" if incremental_rebase else "memory"))},
                                timeout=120)
                sandbox._snapshot_checkpoint("after_diff")
                if incremental_rebase:
                    advance("memory_rebase")
                    if previous is None:
                        raise SandboxError("Incremental snapshot has no base memory")
                    sandbox.manager.disk_storage.copy_file(previous/"memory", staging/"memory", sparse=True)
                    subprocess.run([str(sandbox.manager.snapshot_editor), "edit-memory", "rebase",
                                    "--memory-path", str(staging/"memory"),
                                    "--diff-path", str(staging/"diff")], check=True)
                    (staging/"diff").unlink()
                    sandbox._snapshot_checkpoint("after_rebase")
                advance("disk_copy")
                latest_disk_layer = sandbox.manager.disk_storage.checkpoint_disk(
                    sandbox.disk_paths(), staging, target,
                    sparse=sandbox.environment_id in sandbox.manager.tb2_templates,
                    store=sandbox.overlaybd_store, image=sandbox.overlaybd_image,
                    device_id=sandbox.overlaybd_device_id)
                advance("snapshot_manifest")
                names = (("state", "memory", "disk-image.json")
                         if sandbox.overlaybd_store else
                         (("state","memory","disk.ext4","work.ext4") if sandbox.work_disk
                          else ("state","memory","disk.ext4")))
                if sandbox.overlaybd_store:
                    disk_layers = sandbox.overlaybd_store.disk_layers(
                        staging/"disk-image.json", sandbox.directory,
                        sandbox.overlaybd_store.source_for(sandbox.environment_id))
                hash_algorithms = ({"memory":"sparse-block-sha256-v2",
                                    **({} if sandbox.overlaybd_store else
                                       {"disk.ext4":"sparse-block-sha256-v2"})}
                                   if sandbox.environment_id in sandbox.manager.tb2_templates else {})
                manifest = {"id":sandbox.id, "generation":sandbox.generation+1,
                            "snapshot_type":snapshot_type,
                            "snapshot_mode":sandbox.snapshot_mode,
                            "rootfs_block_backend":"overlaybd-ublk" if sandbox.overlaybd_store else "file-ext4",
                            "disk_path":str(sandbox.disk), "binary_sha256":self.hash_file(Path(sandbox.vm.binary)),
                            "environment_id":sandbox.environment_id,
                            "storage":sandbox.storage,
                            "environment_catalog_sha256":sandbox.environment_catalog_sha256,
                            "environment_manifest_sha256":sandbox.environment_manifest_sha256,
                            "guest_kernel":str(sandbox.kernel),
                            "layer_disks":[str(path) for path in sandbox.layer_disks],
                            "network_slot":sandbox.network_slot,
                            "network_mode":sandbox.network_mode,
                            "memory_profile":sandbox.memory_profile,
                            "free_page_reporting":sandbox.free_page_reporting,
                            "verifier_storage":sandbox.verifier_storage,
                            "verifier_dax":sandbox.verifier_dax,
                            "erofs_dax_layers":list(sandbox.erofs_dax_layers),
                            "verifier_artifact_sha256":sandbox.verifier_artifact_sha,
                            "shared_data_sha256":sandbox.manager.e3["data_sha256"] if sandbox.work_disk else None,
                            "disk_layers":({path.name:self.hash_file(path) for path in disk_layers}
                                           if sandbox.overlaybd_store else None),
                            "latest_disk_layer":(latest_disk_layer.name if sandbox.overlaybd_store
                                                 else None),
                            "hash_algorithms":hash_algorithms,
                            "files":{name:self.storage.snapshot_hash(staging/name,
                                     hash_algorithms.get(name, "sha256")) for name in names}}
                for name in manifest["files"]:
                    with (staging/name).open("rb") as stream:
                        os.fsync(stream.fileno())
                with (staging/"manifest.json").open("w") as stream:
                    json.dump(manifest, stream, indent=2); stream.flush(); os.fsync(stream.fileno())
                self.fsync_directory(staging)
                advance("vm_stop_commit")
                sandbox.vm.stop()
                if sandbox.overlaybd_store:
                    sandbox._release_overlaybd_device()
                sandbox._snapshot_checkpoint("before_publish")
                staging.rename(target)
                self.fsync_directory(sandbox.directory)
                sandbox._snapshot_checkpoint("after_publish")
                sandbox.snapshot = target; sandbox.generation += 1
                if sandbox.overlaybd_store:
                    sandbox.overlaybd_image = target/"disk-image.json"
                sandbox._event("PAUSED"); sandbox._touch()
                sandbox._snapshot_checkpoint("after_registry")
                advance("done")
                sandbox.last_pause_phases = phases
                # Previous memory backing is no longer in use after vm.stop().
                if previous is not None:
                    sandbox.manager.disk_storage.remove_checkpoint(previous)
            except Exception as exc:
                advance("failed")
                sandbox.last_pause_phases = phases
                # A real ENOSPC can also prevent _event/_persist from writing
                # FAILED. Remove the unpublished snapshot first to reclaim
                # space for the durable failure record.
                cleanup_error = None
                if staging.exists():
                    try:
                        sandbox.manager.disk_storage.remove_checkpoint(staging)
                    except Exception as error:
                        cleanup_error = error
                if sandbox.snapshot != target and target.exists():
                    try:
                        sandbox.manager.disk_storage.remove_checkpoint(target)
                        self.fsync_directory(sandbox.directory)
                    except Exception as error:
                        cleanup_error = error
                sandbox._fail("snapshot_failed at "+stage+": "+str(exc))
                if cleanup_error is not None:
                    raise SandboxError("Failed to remove incomplete snapshot") from cleanup_error
                raise
            if sandbox.overlaybd_store:
                try:
                    sandbox.overlaybd_store.prune_unreferenced_layers(
                        sandbox.overlaybd_image, sandbox.directory,
                        sandbox.overlaybd_store.source_for(sandbox.environment_id))
                except (OSError, ValueError) as exc:
                    sandbox.manager.errors.append({"id": sandbox.id,
                                                "component": "overlaybd_layer_gc",
                                                "error": str(exc)})
            # Cache advice is an optional local-storage optimization, never a
            # reason to turn an already committed PAUSED snapshot into FAILED.
            sandbox.snapshot_cache_evicted = None
            if sandbox.manager.snapshot_cache_policy == "evict":
                sandbox.snapshot_cache_evicted = True
                for path in (target/"memory", target/"state") + (
                        (() if sandbox.overlaybd_store else (target/"disk.ext4",))) + (
                        (target/"work.ext4",) if sandbox.work_disk else ()):
                    try:
                        with path.open("rb") as stream:
                            os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
                    except OSError:
                        sandbox.snapshot_cache_evicted = False
                try:
                    sandbox._persist()
                except OSError:
                    # PAUSED was committed before the advisory status update.
                    pass

    def resume(self, sandbox):
        with sandbox.lock:
            if sandbox.reserved:
                raise SandboxError("Sandbox is reserved for warm checkout")
            sandbox._check()
            if sandbox.state == "RUNNING":
                return
            if sandbox.state != "PAUSED":
                raise SandboxError("Cannot resume in "+sandbox.state)
            sandbox._restore()

    def recover(self, sandbox, *, allow_rollback=False):
        """Explicitly discard post-snapshot changes; do not replay commands."""
        with sandbox.lock:
            if sandbox.reserved:
                raise SandboxError("Sandbox is reserved for warm checkout")
            sandbox._check()
            if sandbox.state != "FAILED" or not allow_rollback:
                raise SandboxError("Recovery requires FAILED and explicit allow_rollback=True")
            sandbox._restore()

    def stop(self, sandbox, reason):
        if sandbox.state == "STOPPED" and getattr(sandbox, 'resource_cleanup_complete', False):
            if getattr(sandbox.manager, 'node_admission', None) is not None:
                sandbox.manager.node_admission.stopped('microvm', sandbox.id)
            return
        sandbox.baseline_closing = True
        while sandbox.fork_readers:
            sandbox.fork_condition.wait()
        if sandbox.state == "STOPPED" and getattr(sandbox, 'resource_cleanup_complete', False):
            if getattr(sandbox.manager, 'node_admission', None) is not None:
                sandbox.manager.node_admission.stopped('microvm', sandbox.id)
            return
        sandbox.vm.stop()
        if sandbox.overlaybd_store and sandbox.overlaybd_device_id is not None:
            sandbox._release_overlaybd_device()
        if sandbox.network_mode == "netns" and sandbox.network_ready:
            try:
                sandbox.manager.tb2_network_manager.release(sandbox.id, sandbox.network_slot)
                sandbox.network_ready = False
            except Exception as exc:
                sandbox._event("FAILED", "network_cleanup_failed: " + str(exc))
                raise
        sandbox.manager.disk_storage.release_writable(sandbox.disk_paths())
        if sandbox.snapshot is not None:
            if sandbox.snapshot.exists():
                sandbox.manager.disk_storage.remove_checkpoint(sandbox.snapshot)
            sandbox.snapshot = None
        if sandbox.overlaybd_store:
            sandbox.manager.disk_storage.release_layers(sandbox.directory)
            sandbox.overlaybd_image = sandbox.overlaybd_store.source_for(sandbox.environment_id)
        sandbox.vm.api_path.unlink(missing_ok=True); sandbox.vm.vsock.unlink(missing_ok=True)
        sandbox._event("STOPPED", reason)
        if sandbox.overlaybd_store and getattr(sandbox.overlaybd_store, "shared_layers", None):
            sandbox.overlaybd_store.shared_layers.release(sandbox.id)
        sandbox.resource_cleanup_complete = True
        sandbox._persist()
        if getattr(sandbox.manager, 'node_admission', None) is not None:
            sandbox.manager.node_admission.stopped('microvm', sandbox.id)
        sandbox.manager.warm_wakeup.set()
