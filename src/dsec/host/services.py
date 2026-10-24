"""Explicit host effects: private directories, service execution and inspection.

Only execution applies a composition plan. Configuration and planning remain
independent of these operations and do not import this module.
"""
from collections import Counter
import os
from pathlib import Path
import sys
import time
from dsec._persistence import atomic_json
from dsec.host.composition import plan_service, ServicePlan


def check_private_directory(path, *, storage_access=False):
    """Check existing state without changing permissions or ownership."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Instance directory cannot be a symlink')
    info = path.stat()
    if not path.is_dir():
        raise ValueError('Instance state must be a directory: '+str(path))
    forbidden = 0o067 if storage_access else 0o077
    if info.st_uid != os.getuid() or info.st_mode & forbidden:
        raise PermissionError('Instance directory has unexpected ownership or shared access: '+str(path))
    if storage_access:
        import grp
        gid = grp.getgrnam('kvm').gr_gid
        if info.st_mode & 0o010 and info.st_gid != gid:
            raise PermissionError('Storage traversal group does not match kvm: '+str(path))


def private_directory(path, *, storage_access=False):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Instance directory cannot be a symlink')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    check_private_directory(path, storage_access=storage_access)
    if storage_access:
        import grp
        gid = grp.getgrnam('kvm').gr_gid
        # Storage daemon gets search only; worker registry and sockets remain private.
        os.chown(path, -1, gid)
        path.chmod(0o2710)


def wait_ready(cfg, timeout):
    from dsec.sdk.sandbox_transport import SandboxClient
    from dsec.sdk.rollout_transport import RolloutClient
    root = Path(cfg['state_root'])
    started = time.monotonic()
    while True:
        try:
            SandboxClient(root/'sandboxes/service.sock').call('health')
            health = RolloutClient(root/'worker/worker.sock').call('health')
            if not health.get('scheduler_enabled'):
                raise RuntimeError('Worker scheduler is not enabled')
            return {'ok':True, 'instance':cfg['instance'], 'ready_s':time.monotonic()-started}
        except Exception:
            if time.monotonic()-started >= timeout:
                raise TimeoutError('Services did not become ready; inspect their journal')
            time.sleep(.1)


def execute_service_plan(plan: ServicePlan):
    os.umask(0o077)
    if plan.budget_file is None:
        os.execv(plan.command[0], list(plan.command))
        return
    for path, storage_access in plan.directories:
        private_directory(path, storage_access=storage_access)
    for variable, value in plan.environment_updates.items():
        os.environ[variable] = value
    atomic_json(plan.budget_file, plan.budget_settings)
    for variable in plan.environment_removals:
        os.environ.pop(variable, None)
    if plan.readiness_socket is not None:
        from dsec.sdk.sandbox_transport import SandboxClient
        deadline = time.monotonic()+110
        while True:
            try:
                SandboxClient(plan.readiness_socket).call('health')
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Sandbox service did not become ready')
                time.sleep(.25)
        from dsec.compat.task_plugins import configure_worker_environment
        configure_worker_environment(plan.worker_configuration, plan.budget_file.parent, atomic_json)
    os.execv(plan.command[0], list(plan.command))


def run_service(cfg, role, validate_only=False):
    execute_service_plan(plan_service(cfg, role, validate_only))


def status(cfg):
    from dsec.sdk.sandbox_transport import SandboxClient
    from dsec.sdk.rollout_transport import RolloutClient
    root = Path(cfg['state_root'])
    worker = RolloutClient(root/'worker/worker.sock')
    queue = worker.call('scheduler_status')
    return {'sandbox_health':SandboxClient(root/'sandboxes/service.sock').call('health'),
            'worker_health':worker.call('health'), 'active':queue['active'], 'pending':queue['pending'],
            'sandbox_states':dict(Counter(x['state'] for x in
             SandboxClient(root/'sandboxes/service.sock').call('list')))}
