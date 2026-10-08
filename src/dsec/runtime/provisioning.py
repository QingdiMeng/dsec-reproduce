"""Cold creation, prepared-child allocation and prewarm through Edge ownership."""
import math
import shutil
import time

from dsec.contracts.errors import SandboxError
from dsec.contracts.resources import NodeAdmissionBusy


class SandboxProvisioner:
    def __init__(self, *, sandbox_factory, copy_sparse):
        self.sandbox_factory = sandbox_factory
        self.copy_sparse = copy_sparse

    def create(self, manager, idle_ttl_seconds=300, environment_id="default", memory_profile="baseline",
               verifier_storage=None, storage="local", baseline_id=None):
        if baseline_id is not None:
            # Admit before fork anchor publication can modify the source.
            if getattr(manager, 'node_admission', None) is not None:
                manager.node_admission.allocate('microvm')
                manager.node_admission.local.source_effects = True
            from dsec.runtime.fork import fork_baseline
            return fork_baseline(manager, baseline_id, idle_ttl_seconds, environment_id,
                                 memory_profile, verifier_storage, storage)
        if not math.isfinite(idle_ttl_seconds) or idle_ttl_seconds <= 0:
            raise ValueError("TTL must be finite and positive")
        if storage != "local" and (manager.microvm_environment_catalog is None or
                                    environment_id not in manager.microvm_environment_catalog.entries):
            raise ValueError("Nonlocal storage requires a generic microVM environment")
        if manager.microvm_environment_catalog is not None and \
                environment_id in manager.microvm_environment_catalog.entries:
            # Validate before allocating a warm slot or creating a VM.
            manager.microvm_environment_catalog.resolve(environment_id, storage)
        if verifier_storage is not None:
            store = manager.verifier_artifacts_for(environment_id)
            if store is None or environment_id not in manager.tb2_templates:
                raise ValueError("TB2 verifier artifact is not configured")
            store.resolve(verifier_storage)
        selected = manager.ready_pool.checkout(manager, idle_ttl_seconds,
            environment_id, memory_profile, verifier_storage)
        if selected is not None:
            return selected
        return manager._create_cold(idle_ttl_seconds, environment_id, memory_profile,
                                    verifier_storage, storage=storage)

    def prewarm(self, manager, *, environment_id, verifier_storage=None, count=1,
                memory_profile="baseline", idle_ttl_seconds=3600):
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= manager.capacity:
            raise ValueError("Prewarm count must be in 1..capacity")
        if environment_id not in manager.tb2_templates or memory_profile != "baseline":
            raise ValueError("Prewarm currently supports configured TB2 tasks only")
        prepared = []
        for _ in range(count):
            sb = manager._create_cold(idle_ttl_seconds, environment_id, memory_profile,
                                   verifier_storage, reserved=True)
            prepared.append(sb.status())
        return prepared

    def create_cold(self, manager, idle_ttl_seconds=300, environment_id="default", memory_profile="baseline",
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
        environment_spec = (manager.microvm_environment_catalog.resolve(environment_id, storage)
                            if manager.microvm_environment_catalog is not None and
                            environment_id in manager.microvm_environment_catalog.entries else None)
        if storage != "local" and environment_spec is None:
            raise ValueError("Nonlocal storage requires a generic microVM environment")
        if (environment_id, memory_profile) != ("default", "baseline"):
            if environment_id == "e3-mixed" and memory_profile in (
                    "baseline", "dax", "damon_fpr", "dax_damon_fpr"):
                if manager.e3 is None:
                    raise SandboxError("E3 artifacts are not configured")
            elif environment_id not in manager.tb2_templates or memory_profile != "baseline":
                raise ValueError("Unsupported environment/memory profile")
        if verifier_storage is not None:
            store = manager.verifier_artifacts_for(environment_id)
            if environment_id not in manager.tb2_templates or store is None:
                raise ValueError("TB2 verifier artifact is not configured")
            verifier_disk = store.resolve(verifier_storage)
        else:
            verifier_disk = None
        with manager.lock:
            if manager.closed:
                raise SandboxError("Manager closed")
            if sum(s.state != "STOPPED" for s in manager.sandboxes.values()) >= manager.capacity:
                if (getattr(manager, 'node_admission', None) is not None and
                        not getattr(manager.node_admission.local, 'source_effects', False)):
                    raise NodeAdmissionBusy(['sandbox_capacity'])
                raise SandboxError("Sandbox capacity reached")
            network_slot = None
            if environment_id in manager.tb2_templates and manager.tb2_network_manager:
                occupied = {s.network_slot for s in manager.sandboxes.values()
                            if s.state != "STOPPED"}
                network_slot = manager.tb2_network_manager.allocate(occupied)
                if network_slot is None:
                    if (getattr(manager, 'node_admission', None) is not None and
                            not getattr(manager.node_admission.local, 'source_effects', False)):
                        raise NodeAdmissionBusy(['network_slots'])
                    raise SandboxError("TB2 network namespace capacity reached")
            elif environment_id in manager.tb2_templates and manager.tb2_network_slots:
                occupied = {s.network_slot for s in manager.sandboxes.values()
                            if s.state != "STOPPED"}
                network_slot = next((name for name in manager.tb2_network_slots
                                     if name not in occupied), None)
                if network_slot is None:
                    if (getattr(manager, 'node_admission', None) is not None and
                            not getattr(manager.node_admission.local, 'source_effects', False)):
                        raise NodeAdmissionBusy(['network_slots'])
                    raise SandboxError("TB2 network slot capacity reached")
            lease_id = None
            if getattr(manager, 'node_admission', None) is not None:
                lease_id = manager.node_admission.allocate('microvm', configured_limits={
                    'environment_id':environment_id,
                    'cpu_count':manager.tb2_resources.get(environment_id, {}).get('cpus', 1),
                    'memory_mb':(manager.tb2_resources.get(environment_id, {}).get('memory_mb', 2048)
                                 if environment_id in manager.tb2_templates else
                                 512 if environment_id == 'e3-mixed' else 256)})
            try:
                sb = self.sandbox_factory(manager, idle_ttl_seconds, environment_id, memory_profile,
                             verifier_storage, reserved=reserved, storage=storage,
                             environment_spec=environment_spec)
            except BaseException:
                # Internal pool creation has no RPC request context to release
                # an unbound intent. No host handles have been allocated yet.
                if lease_id is not None:
                    manager.node_admission.release_unbound(lease_id)
                raise
            sb.node_lease_id = lease_id
            sb.network_slot = network_slot
            sb.network_mode = ("netns" if manager.tb2_network_manager and network_slot else
                               "tap_pool" if network_slot else
                               "legacy_tap" if manager.tb2_network_tap and
                               environment_id in manager.tb2_templates else None)
            manager.sandboxes[sb.id] = sb
            # Only admission and slot reservation need the manager lock.
            # Hold this sandbox's lock across preparation so monitor/close
            # cannot observe or stop a half-built VM.
            sb.lock.acquire()
        try:
            if getattr(manager, 'node_admission', None) is not None:
                manager.node_admission.bind(lease_id, sb.id)
            sb._persist()
            advance("network_setup")
            network=None
            if sb.network_mode == "netns":
                network=manager.tb2_network_manager.ensure(sb.id, sb.network_slot)
                sb.network_ready=True
                sb.vm.start_launcher=manager.tb2_network_manager.launcher(sb.id,
                                                                       sb.network_slot)
                sb._persist()
            elif sb.network_mode == "tap_pool":
                network=manager.tb2_network_slots[network_slot]
            if prepare_only:
                return sb
            advance("rootfs_copy")
            source = (manager.e3["guest"] if sb.work_disk else
                      manager.tb2_templates.get(environment_id, manager.template))
            if sb.overlaybd_store:
                sb.overlaybd_device_id, sb.overlaybd_runtime = sb.overlaybd_store.create(
                    sb.overlaybd_image, sb.directory, sb.disk)
                sb.overlaybd_daemon_socket_identity = sb.overlaybd_store.socket_identity()
                sb._persist()
            elif environment_id in manager.tb2_templates:
                self.copy_sparse(source, sb.disk)
            else:
                shutil.copy2(source, sb.disk)
            if not sb.overlaybd_store:
                sb.disk.chmod(0o600)
            if sb.work_disk:
                shutil.copy2(manager.e3["work_template"], sb.work_disk)
                sb.work_disk.chmod(0o600)
                advance("vm_boot")
                sb.vm.boot(sb.kernel, sb.disk, memory_profile=sb.memory_profile,
                           data=manager.e3["data"], work=sb.work_disk)
                sb.memory_evidence = sb.vm.memory_probe(sb.memory_profile)
            else:
                tb2_spec = manager.tb2_resources.get(environment_id, {})
                advance("vm_boot")
                sb.vm.boot(sb.kernel, sb.disk,
                           memory_mib=tb2_spec.get("memory_mb", 2048) if environment_id in manager.tb2_templates else None,
                           cpu_count=tb2_spec.get("cpus", 1),
                           tap_name=manager.tb2_network_tap if environment_id in manager.tb2_templates else None,
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
            if getattr(manager, 'node_admission', None) is not None:
                manager.node_admission.created('microvm', sb.id, ready=sb.reserved)
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
                with manager.warm_condition:
                    manager.warm_condition.notify_all()
