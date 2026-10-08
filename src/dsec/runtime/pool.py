"""Ready-pool queue, checkout and idle refill; state remains owned by Edge."""
import shutil
import time

from dsec.contracts.errors import SandboxError
from dsec.contracts.resources import NodeAdmissionBusy


class ReadyPool:
    def status(self, manager):
        with manager.lock:
            pools = []
            for (environment_id, storage), target in manager.warm_pool_specs.items():
                reserved = [sb for sb in manager.sandboxes.values()
                            if sb.reserved and sb.environment_id == environment_id and
                            sb.verifier_storage == storage and sb.state != "STOPPED"]
                ready = sum(sb.state == "RUNNING" for sb in reserved)
                pools.append({"environment_id": environment_id, "verifier_storage": storage,
                              "target": target, "ready": ready,
                              "preparing": sum(sb.state == "CREATING" for sb in reserved),
                              "refill_workers_active": manager.warm_inflight[(environment_id, storage)],
                              "waiting_requests": len(manager.warm_waiters[(environment_id, storage)]),
                              "deficit": max(0, target-ready),
                              "node_wait_reasons":manager.warm_node_waits.get((environment_id, storage), [])})
            return {"pools": pools, "hits": manager.warm_hits, "misses": manager.warm_misses,
                    "foreground_active": manager.foreground_active,
                    "refill_errors": manager.warm_refill_errors}

    def foreground_enter(self, manager):
        with manager.lock:
            manager.foreground_active += 1
            manager.last_foreground = time.monotonic()

    def foreground_exit(self, manager):
        with manager.lock:
            manager.foreground_active -= 1
            manager.last_foreground = time.monotonic()
        manager.warm_wakeup.set()

    def resource_headroom(self, manager):
        if manager.warm_min_memory_mib:
            with open("/proc/meminfo") as stream:
                memory = next(int(line.split()[1]) // 1024 for line in stream
                              if line.startswith("MemAvailable:"))
            if memory < manager.warm_min_memory_mib:
                return False
        if manager.warm_min_disk_gib:
            disk = shutil.disk_usage(manager.root).free // (1024**3)
            if disk < manager.warm_min_disk_gib:
                return False
        return True

    def refill(self, manager):
        while not manager.shutdown.is_set():
            choice = None
            with manager.lock:
                active = sum(sb.state != "STOPPED" for sb in manager.sandboxes.values())
                idle = (manager.foreground_active == 0 and
                        time.monotonic()-manager.last_foreground >= manager.warm_idle_quiet_seconds)
                if not manager.closed and active < manager.capacity and idle:
                    for key, target in manager.warm_pool_specs.items():
                        existing = sum(sb.reserved and sb.environment_id == key[0] and
                                       sb.verifier_storage == key[1] and sb.state != "STOPPED"
                                       for sb in manager.sandboxes.values())
                        if existing + manager.warm_inflight[key] < target:
                            choice = key
                            break
                if choice is not None and manager._warm_resource_headroom():
                    manager.warm_inflight[choice] += 1
                else:
                    choice = None
            if choice is None:
                manager.warm_wakeup.wait(1)
                manager.warm_wakeup.clear()
                continue
            try:
                manager._create_cold(3600, choice[0], "baseline", choice[1], reserved=True)
                with manager.lock:
                    manager.warm_node_waits.pop(choice, None)
            except NodeAdmissionBusy as exc:
                with manager.lock:
                    manager.warm_node_waits[choice] = exc.reasons
                manager.shutdown.wait(1)
            except SandboxError as exc:
                if "capacity reached" not in str(exc):
                    with manager.lock:
                        manager.warm_refill_errors += 1
                        manager.errors.append({"component": "warm_pool", "error": str(exc)})
                manager.shutdown.wait(1)
            except Exception as exc:
                with manager.lock:
                    manager.warm_refill_errors += 1
                    manager.errors.append({"component": "warm_pool", "error": str(exc)})
                manager.shutdown.wait(1)
            finally:
                with manager.lock:
                    manager.warm_inflight[choice] -= 1
                manager.warm_wakeup.set()

    def checkout(self, manager, idle_ttl_seconds, environment_id,
                 memory_profile, verifier_storage):
        started = time.monotonic()
        key = (environment_id, verifier_storage)
        pooled = key in manager.warm_pool_specs and memory_profile == "baseline"
        ticket = object() if pooled else None
        deadline = started + manager.warm_wait_ms/1000
        selected = None
        manager_wait = 0.0
        queue_wait = 0.0
        lock_started = time.monotonic()
        with manager.warm_condition:
            manager_wait += time.monotonic()-lock_started
            if manager.closed:
                raise SandboxError("Manager closed")
            if pooled:
                manager.warm_waiters[key].append(ticket)
            try:
                while True:
                    at_front = not pooled or manager.warm_waiters[key][0] is ticket
                    if at_front:
                        for sb in manager.sandboxes.values():
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
                            if getattr(manager, 'node_admission', None) is not None:
                                manager.node_admission.checkout(selected)
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
                            manager.warm_waiters[key].popleft()
                            manager.warm_condition.notify_all()
                        break
                    pending = False
                    if pooled:
                        reserved = [sb for sb in manager.sandboxes.values()
                                    if sb.reserved and sb.state != "STOPPED" and
                                    sb.environment_id == environment_id and
                                    sb.verifier_storage == verifier_storage]
                        active = sum(sb.state != "STOPPED" for sb in manager.sandboxes.values())
                        pending = bool(manager.warm_inflight[key] or any(
                            sb.state == "CREATING" for sb in reserved) or
                            (len(reserved) < manager.warm_pool_specs[key] and active < manager.capacity))
                    remaining = deadline-time.monotonic()
                    if not pending or remaining <= 0:
                        if pooled:
                            manager.warm_misses += 1
                        break
                    manager.warm_condition.wait(remaining)
                    if manager.closed:
                        raise SandboxError("Manager closed")
            finally:
                if pooled and ticket in manager.warm_waiters[key]:
                    manager.warm_waiters[key].remove(ticket)
                    manager.warm_condition.notify_all()
        if selected is not None:
            persist_started = time.monotonic()
            try:
                selected._persist()
            except Exception:
                (selected.reserved, selected.warm_pool_hit, selected.ttl,
                 selected.deadline, selected.last_create_phases) = original
                selected.lock.release()
                manager.warm_wakeup.set()
                with manager.warm_condition:
                    manager.warm_condition.notify_all()
                raise
            selected.last_create_phases = {
                "warm_checkout": round(time.monotonic()-started, 6),
                "warm_manager_wait": round(manager_wait, 6),
                "warm_queue_wait": round(queue_wait, 6),
                "warm_persist": round(time.monotonic()-persist_started, 6)}
            selected.lock.release()
            if pooled:
                with manager.lock:
                    manager.warm_hits += 1
            manager.warm_wakeup.set()
            return selected
        return None
