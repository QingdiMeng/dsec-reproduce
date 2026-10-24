"""Read-only host prerequisite checks and explicitly requested live probes."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
from dsec.host.configuration import PATH_OPTIONS
from dsec.host.services import check_private_directory


def doctor(cfg, live=False):
    checks = []
    def check(name, fn):
        try:
            fn()
            checks.append({'check':name, 'ok':True})
        except Exception as exc:
            checks.append({'check':name, 'ok':False, 'error':str(exc)[:300]})
    def require(condition, message):
        if not condition:
            raise RuntimeError(message)
    check('linux', lambda: require(sys.platform == 'linux', 'Linux is required to run microVMs'))
    check('firecracker', lambda: require(os.access(cfg['sandbox']['binary'], os.X_OK), 'Firecracker is not executable'))
    for key in PATH_OPTIONS & cfg['sandbox'].keys() - {'binary', 'overlaybd_ublk_socket'}:
        check(key, lambda key=key: require(Path(cfg['sandbox'][key]).is_file() and
              os.access(cfg['sandbox'][key], os.R_OK), 'Required artifact/config file is not readable'))
    def kvm():
        fd = os.open('/dev/kvm', os.O_RDWR | os.O_CLOEXEC)
        try:
            import fcntl
            require(fcntl.ioctl(fd, 0xAE00, 0) == 12, 'Unexpected KVM API version')
        finally:
            os.close(fd)
    check('kvm', kvm)
    check('cgroup_v2', lambda: require(Path('/sys/fs/cgroup/cgroup.controllers').is_file(), 'cgroup v2 is unavailable'))
    check('network_interface', lambda: require(
        (Path('/sys/class/net')/cfg['scheduler']['network_interface']).is_dir(), 'Configured network interface is unavailable'))
    root = Path(cfg['state_root'])
    existing = root
    while not existing.exists():
        existing = existing.parent
    check('state_storage', lambda: require(os.access(existing, os.W_OK | os.X_OK), 'State parent is not writable'))
    for path in (root, root/'sandboxes', root/'worker'):
        if path.exists() or path.is_symlink():
            access='overlaybd_ublk_socket' in cfg['sandbox'] and path != root/'worker'
            check('state_permissions:'+path.name, lambda path=path, access=access:
                  check_private_directory(path, storage_access=access))
    check('disk_floor', lambda: require(shutil.disk_usage(existing).free // 2**20 >
          cfg['scheduler'].get('min_disk_free_mb', 20480), 'Free disk is below the configured admission floor'))
    if 'overlaybd_ublk_socket' in cfg['sandbox']:
        check('ublk_socket', lambda: require(Path(cfg['sandbox']['overlaybd_ublk_socket']).is_socket(), 'ublk daemon socket is unavailable'))
    if cfg.get('network'):
        def helper():
            p = Path(cfg['network']['helper'])
            st = p.lstat()
            require(p.is_file() and not p.is_symlink() and st.st_uid == 0 and not st.st_mode & 0o022,
                    'Network helper must be a root-owned non-writable regular file')
        check('network_helper', helper)
        if live:
            check('network_helper_access', lambda: subprocess.run(
                ['sudo', '-n', cfg['network']['helper'], 'list'], check=True,
                capture_output=True, text=True, timeout=10))
    if live:
        from dsec.sdk.sandbox_transport import SandboxClient
        from dsec.sdk.rollout_transport import RolloutClient
        check('sandbox_service', lambda: SandboxClient(root/'sandboxes/service.sock').call('health'))
        check('worker_service', lambda: RolloutClient(root/'worker/worker.sock').call('health'))
    return {'ok':all(x['ok'] for x in checks), 'instance':cfg['instance'], 'checks':checks}
