"""Configure, inspect and run an installed single-host DSec runtime."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import fields
import importlib.metadata
import json
import hashlib
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import traceback
from urllib.parse import urlsplit

from dsec.runtime.requests import atomic_json
from dsec.runtime.isolation.proxy import validate_proxy_bypass_hosts
from dsec.contracts.resources import ResourceBudget, NodeDemand


PATH_OPTIONS = {
    'binary', 'kernel', 'template', 'microvm_environment_catalog',
    'overlaybd_ublk_socket', 'overlaybd_global_config', 'snapshot_editor',
    'tb2_manifest', 'tb2_verifier_artifact_manifest', 'tb2_verifier_artifact_local',
    'tb2_verifier_artifact_threefs', 'tb2_verifier_dax_binary',
}
SCALAR_OPTIONS = {
    'capacity', 'max_requests', 'snapshot_concurrency', 'snapshot_strategy',
    'snapshot_cache_policy', 'warm_refill_workers', 'warm_wait_ms',
    'warm_idle_quiet_seconds', 'warm_min_memory_mib', 'warm_min_disk_gib',
    'egress_proxy_url',
}
LIST_OPTIONS = {'warm_pool', 'tb2_task_verifier_artifact', 'tb2_verifier_dax_task',
                'egress_proxy_bypass_host'}
BOOL_OPTIONS = {'tb2_free_page_reporting'}
WORKER_PATHS = {'tb2_tasks_dir', 'container_catalog', 'container_root', 'container_agent', 'docker_broker_socket'}
NETWORK_KEYS = {'helper', 'max_slots', 'dns', 'dax_binary'}


def _path(value, base):
    if not isinstance(value, str) or not value or any(x in value for x in ('\n', '\r', '\0')):
        raise ValueError('Configuration paths must be nonempty strings without control characters')
    path = Path(value).expanduser()
    return str((path if path.is_absolute() else base/path).resolve())


def _keys(value, allowed, context):
    if not isinstance(value, dict) or set(value)-allowed:
        raise ValueError('Unknown or invalid '+context+' fields')


def load_config(path):
    path = Path(path).resolve(strict=True)
    cfg = json.loads(path.read_text())
    _keys(cfg, {'schema', 'instance', 'state_root', 'sandbox', 'worker', 'scheduler', 'network',
                'service_group'}, 'host')
    if cfg.get('schema') != 1 or isinstance(cfg['schema'], bool):
        raise ValueError('Unsupported host configuration schema')
    if not isinstance(cfg.get('instance'), str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', cfg['instance']):
        raise ValueError('Invalid instance name')
    cfg['state_root'] = _path(cfg['state_root'], path.parent)
    if cfg.get('service_group') is not None and not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', cfg['service_group']):
        raise ValueError('Invalid service group')
    # A private instance owns its sockets, registry and leases independently.
    if len(os.fsencode(cfg['state_root']+'/worker/worker.sock')) >= 108:
        raise ValueError('State root is too long for Linux Unix sockets')
    sandbox = cfg['sandbox']
    _keys(sandbox, PATH_OPTIONS | SCALAR_OPTIONS | LIST_OPTIONS | BOOL_OPTIONS, 'sandbox')
    if not {'binary', 'kernel', 'template'}.issubset(sandbox):
        raise ValueError('Sandbox binary, kernel and template are required')
    for key in PATH_OPTIONS & sandbox.keys():
        sandbox[key] = _path(sandbox[key], path.parent)
    for key in BOOL_OPTIONS & sandbox.keys():
        if type(sandbox[key]) is not bool:
            raise ValueError(key+' must be boolean')
    for key in LIST_OPTIONS & sandbox.keys():
        if not isinstance(sandbox[key], list) or any(
                not isinstance(x, str) or '\n' in x or '\0' in x for x in sandbox[key]):
            raise ValueError(key+' must be a string list')
    if 'tb2_task_verifier_artifact' in sandbox:
        pinned = []
        for item in sandbox['tb2_task_verifier_artifact']:
            parts = item.split('=')
            if len(parts) != 3 or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,127}', parts[0]):
                raise ValueError('Task verifier artifact must be task-id=manifest=local-image')
            pinned.append(parts[0]+'='+_path(parts[1], path.parent)+'='+_path(parts[2], path.parent))
        sandbox['tb2_task_verifier_artifact'] = pinned
    for key in SCALAR_OPTIONS & sandbox.keys():
        value = sandbox[key]
        if isinstance(value, bool) or not isinstance(value, (str, int, float)) or (
                isinstance(value, (int, float)) and not math.isfinite(value)):
            raise ValueError('Invalid sandbox option: '+key)
    integer_options = {'capacity', 'max_requests', 'snapshot_concurrency', 'warm_refill_workers',
                       'warm_wait_ms', 'warm_min_memory_mib', 'warm_min_disk_gib'}
    for key in integer_options & sandbox.keys():
        if type(sandbox[key]) is not int or sandbox[key] < (
                0 if key in ('warm_wait_ms', 'warm_min_memory_mib', 'warm_min_disk_gib') else 1):
            raise ValueError('Invalid integer sandbox option: '+key)
    if 'warm_idle_quiet_seconds' in sandbox and (
            type(sandbox['warm_idle_quiet_seconds']) not in (int, float) or
            sandbox['warm_idle_quiet_seconds'] < 0):
        raise ValueError('Invalid idle refill interval')
    if sandbox.get('snapshot_concurrency', 1) > sandbox.get('capacity', 4) or (
            sandbox.get('warm_refill_workers', 1) > sandbox.get('capacity', 4)):
        raise ValueError('Snapshot/refill concurrency exceeds capacity')
    if sandbox.get('snapshot_strategy', 'full') not in ('full', 'boot-diff', 'incremental'):
        raise ValueError('Invalid snapshot strategy')
    if sandbox.get('snapshot_cache_policy', 'retain') not in ('retain', 'evict'):
        raise ValueError('Invalid snapshot cache policy')
    proxy = sandbox.get('egress_proxy_url')
    if 'egress_proxy_bypass_host' in sandbox:
        sandbox['egress_proxy_bypass_host'] = list(validate_proxy_bypass_hosts(
            sandbox['egress_proxy_bypass_host']))
    if proxy:
        url = urlsplit(proxy)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
            raise ValueError('Proxy must be HTTP(S) without embedded credentials')
    worker = cfg.get('worker', {})
    _keys(worker, WORKER_PATHS | {'metrics_port'}, 'worker')
    for key in WORKER_PATHS & worker.keys():
        worker[key] = _path(worker[key], path.parent)
    if 'metrics_port' in worker and (type(worker['metrics_port']) is not int or
                                    not 1 <= worker['metrics_port'] <= 65535):
        raise ValueError('Invalid metrics port')
    cfg['worker'] = worker
    scheduler = cfg['scheduler']
    budget_keys = {f.name for f in fields(ResourceBudget)}
    _keys(scheduler, budget_keys | {'network_interface', 'disk_device', 'shared_services', 'node_default_demand', 'node_ready_demand'}, 'scheduler')
    budget = {key: value for key, value in scheduler.items() if key in budget_keys}
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
           for value in budget.values()):
        raise ValueError('Scheduler budgets must be finite numbers')
    integers = {'memory_mb', 'disk_mb', 'api_episode_slots', 'api_inflight', 'api_rpm', 'api_tpm',
                'min_memory_free_mb', 'min_disk_free_mb', 'api_token_reserve'}
    if any(type(budget[key]) is not int for key in integers & budget.keys()):
        raise ValueError('Integer scheduler budgets cannot be fractional')
    ResourceBudget(**budget)
    for name in ('node_default_demand','node_ready_demand'):
        if name in scheduler:
            NodeDemand(**scheduler[name])
    if not isinstance(scheduler.get('network_interface'), str) or not re.fullmatch(
            r'[A-Za-z0-9_.:-]{1,15}', scheduler['network_interface']):
        raise ValueError('Invalid scheduler network interface')
    if 'disk_device' in scheduler and not re.fullmatch(r'[A-Za-z0-9_.-]+', scheduler['disk_device']):
        raise ValueError('Invalid disk device name')
    if 'shared_services' in scheduler and not isinstance(scheduler['shared_services'], dict):
        raise ValueError('Invalid shared services')
    network = cfg.get('network')
    if network is not None:
        _keys(network, NETWORK_KEYS, 'network')
        network.setdefault('helper', '/usr/local/libexec/dsec-'+cfg['instance']+'-netns-helper')
        network['helper'] = _path(network['helper'], path.parent)
        if 'dax_binary' in network:
            network['dax_binary'] = _path(network['dax_binary'], path.parent)
        if type(network.get('max_slots', 8)) is not int or not 1 <= network.get('max_slots', 8) <= 32768:
            raise ValueError('Invalid network slot count')
        import ipaddress
        ipaddress.IPv4Address(network.get('dns', '1.1.1.1'))
        if not sandbox.get('microvm_environment_catalog'):
            raise ValueError('Isolated network requires a microVM environment catalog')
    cfg['_file'] = str(path)
    return cfg


def sandbox_arguments(cfg):
    root = Path(cfg['state_root'])
    args = ['--root', str(root/'sandboxes'), '--node-budget', str(root/'worker/budget.json')]
    for key, value in cfg['sandbox'].items():
        flag = '--'+key.replace('_', '-')
        if key in BOOL_OPTIONS:
            if value:
                args.append(flag)
        elif key in LIST_OPTIONS:
            for item in value:
                args += [flag, item]
        else:
            args += [flag, str(value)]
    network = cfg.get('network')
    if network:
        args += ['--tb2-netns-helper', network['helper'], '--tb2-netns-max-slots',
                 str(network.get('max_slots', 8)), '--tb2-netns-dns', network.get('dns', '1.1.1.1')]
        if network.get('dax_binary'):
            args += ['--tb2-netns-dax-binary', network['dax_binary']]
    return args


def worker_arguments(cfg, budget_file):
    root = Path(cfg['state_root'])
    args = ['--socket', str(root/'worker/worker.sock'), '--sandbox-socket', str(root/'sandboxes/service.sock'),
            '--state-dir', str(root/'worker/rollouts'), '--scheduler-budget', str(budget_file)]
    for key in ('tb2_tasks_dir', 'metrics_port'):
        if key in cfg['worker']:
            args += ['--'+key.replace('_', '-'), str(cfg['worker'][key])]
    return args


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


def systemd_quote(value):
    # systemd expands specifiers even inside quotes; never interpolate a shell.
    return json.dumps(str(value).replace('%', '%%'))


def render_units(cfg, out):
    out = Path(out).resolve()
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    def command(arguments):
        group = cfg.get('service_group')
        if group:
            arguments = ['/usr/bin/sg', group, '-c', 'exec '+shlex.join(arguments)]
        return ':'+' '.join(systemd_quote(x) for x in arguments)
    base = [sys.executable, '-I', '-B', '-m', 'dsec_host', '--config', cfg['_file'], 'run']
    name = 'dsec-'+cfg['instance']
    units = {}
    for role in ('sandbox', 'worker'):
        unit = name+'-'+role+'.service'
        before = ''
        if role == 'sandbox':
            admin = command([sys.executable, '-I', '-B', '-m', 'service_admin', '--root',
                             str(Path(cfg['state_root'])/'sandboxes')])
            before = 'ExecStartPre='+admin+'\nExecStop='+admin+'\n'
        units[unit] = ('[Unit]\nDescription=DSec '+role+' ('+cfg['instance']+')\n'+
            'StartLimitIntervalSec=60\nStartLimitBurst=5\n'+
            ('After='+name+'-sandbox.service\nWants='+name+'-sandbox.service\n' if role == 'worker' else '')+
            '\n[Service]\nType=simple\n'+before+'ExecStart='+command(base+[role])+'\n'+
            'Restart=always\nRestartSec=2\nTimeoutStartSec=120\nTimeoutStopSec=60\n'+
            ('KillMode=process\n' if role == 'sandbox' else '')+
            'UMask=0077\n\n[Install]\nWantedBy=default.target\n')
    for name, text in units.items():
        destination = out/name
        if destination.exists() and destination.read_text() != text:
            raise FileExistsError('Refusing to overwrite a different unit: '+str(destination))
        destination.write_text(text)
    return {'units':[str(out/name) for name in units], 'scope':'trusted single-user user services'}


def render_privileges(cfg, out, user, slot_offset):
    import grp
    import ipaddress
    import pwd
    from dsec.host import privileged_helper as privileged_netns_helper
    from dsec.host.install_template import INSTALLER
    account = pwd.getpwnam(user)
    group = grp.getgrnam('kvm')
    if account.pw_uid <= 0 or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', user):
        raise ValueError('Expected an unprivileged runtime account')
    if account.pw_gid != group.gr_gid and user not in group.gr_mem:
        raise ValueError('Runtime user must already have kvm group membership')
    if not cfg['sandbox'].get('microvm_environment_catalog'):
        raise ValueError('Network privilege bundle requires an environment catalog')
    network = cfg.get('network', {})
    slots = network.get('max_slots', cfg['sandbox'].get('capacity', 4))
    if not 0 <= slot_offset <= 32768-slots:
        raise ValueError('Network slot range exceeds the reserved address pool')
    name = 'dsec-'+cfg['instance']+'-netns'
    helper_path = '/usr/local/libexec/'+name+'-helper'
    config_path = '/etc/dsec/'+name+'.json'
    state = '/var/lib/dsec/'+name
    privileged = {'uid':account.pw_uid, 'gid':group.gr_gid,
                  'runtime_root':str(Path(cfg['state_root'])/'sandboxes'),
                  'firecracker':cfg['sandbox']['binary'],
                  'uplink':cfg['scheduler']['network_interface'], 'dns':network.get('dns', '1.1.1.1')}
    ipaddress.IPv4Address(privileged['dns'])
    for key, path in [('firecracker', cfg['sandbox']['binary']),
                      ('dax_firecracker', network.get('dax_binary') or
                       cfg['sandbox'].get('tb2_verifier_dax_binary'))]:
        if path:
            privileged[key] = str(Path(path).resolve(strict=True))
            privileged[key+'_sha256'] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    proxy = cfg['sandbox'].get('egress_proxy_url')
    if proxy:
        url = urlsplit(proxy)
        privileged['egress_proxy'] = {'ip':str(ipaddress.IPv4Address(url.hostname)),
                                     'port':url.port or (443 if url.scheme == 'https' else 80)}
    source = Path(privileged_netns_helper.__file__).read_text()
    replacements = {'CONFIG = Path("/etc/dsec-netns-helper.json")':'CONFIG = Path('+repr(config_path)+')',
                    'STATE = Path("/var/lib/dsec-netns")':'STATE = Path('+repr(state)+')',
                    'SLOT_OFFSET = 0':'SLOT_OFFSET = '+str(slot_offset),
                    'MAX_SLOTS = 32768':'MAX_SLOTS = '+str(slots)}
    for old, new in replacements.items():
        if source.count(old) != 1:
            raise RuntimeError('Network helper template differs from this generator')
        source = source.replace(old, new)
    compile(source, helper_path, 'exec')
    contents = {'netns-helper.py':source.encode(),
                'helper-config.json':(json.dumps(privileged, indent=2)+'\n').encode(),
                'sudoers':(user+' ALL=(root) NOPASSWD: '+helper_path+' *\n').encode()}
    specs = [(filename, target, mode, hashlib.sha256(contents[filename]).hexdigest())
             for filename, target, mode in [('netns-helper.py', helper_path, 0o755),
             ('helper-config.json', config_path, 0o644),
             ('sudoers', '/etc/sudoers.d/'+name, 0o440)]]
    installer = INSTALLER.replace('__SPECS__', repr(specs)).replace('__STATE__', repr(state))
    compile(installer, 'install-privileges.py', 'exec')
    connected = {key:value for key,value in cfg.items() if key != '_file'}
    connected['network'] = {'helper':helper_path, 'max_slots':slots, 'dns':privileged['dns']}
    if privileged.get('dax_firecracker'):
        connected['network']['dax_binary'] = privileged['dax_firecracker']
    contents['install-privileges.py'] = installer.encode()
    contents['host-with-network.json'] = (json.dumps(connected, indent=2)+'\n').encode()
    destination = Path(out).resolve()
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for filename, blob in contents.items():
        path = destination/filename
        path.write_bytes(blob)
        path.chmod(0o600)
    return {'bundle':str(destination), 'install':str(destination/'install-privileges.py'),
            'configuration':str(destination/'host-with-network.json'),
            'slot_range':[slot_offset, slot_offset+slots-1],
            'storage_provisioning':'existing ublk service must authorize this sandbox root'}


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


def run_service(cfg, role, validate_only=False):
    os.umask(0o077)
    root = Path(cfg['state_root'])
    if validate_only:
        if role != 'sandbox':
            raise ValueError('Artifact validation applies to the sandbox service')
        os.execv(sys.executable, [sys.executable, '-I', '-B', '-m', 'sandboxd',
                                  *sandbox_arguments(cfg), '--validate-only'])
    storage_access = 'overlaybd_ublk_socket' in cfg['sandbox']
    for path in (root, root/'sandboxes', root/'worker'):
        private_directory(path, storage_access=storage_access and path != root/'worker')
    # Keep schema-1 configuration keys while applying backend configuration to
    # the sandbox service, which now owns container runtime state.
    environment = {'docker_broker_socket':'DSEC_DOCKER_BROKER_SOCKET'}
    if role == 'sandbox':
        environment.update(container_catalog='DSEC_ENVIRONMENT_CATALOG',
                           container_root='DSEC_CONTAINER_ROOT',
                           container_agent='DSEC_CONTAINER_AGENT')
    for key, variable in environment.items():
        if key in cfg['worker']:
            os.environ[variable] = cfg['worker'][key]
    settings = {**cfg['scheduler'], 'disk_path':str(root)}
    budget_file = root/'worker/budget.json'
    atomic_json(budget_file, settings)
    if role == 'sandbox':
        os.environ.pop('DSEC_ADMISSION_WORKER_SOCKET', None)
        command = [sys.executable, '-I', '-B', '-m', 'sandboxd', *sandbox_arguments(cfg)]
    else:
        from dsec.sdk.sandbox_transport import SandboxClient
        deadline = time.monotonic()+110
        while True:
            try:
                SandboxClient(root/'sandboxes/service.sock').call('health')
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Sandbox service did not become ready')
                time.sleep(.25)
        from dsec.compat.task_plugins import configure_worker_environment
        configure_worker_environment(cfg['sandbox'], root/'worker', atomic_json)
        command = [sys.executable, '-I', '-B', '-m', 'rollout_workerd', *worker_arguments(cfg, budget_file)]
    os.execv(sys.executable, command)


async def smoke(cfg, out, *, restart_services=False):
    from dsec.rollout.environment import DSecAgentEnvironment, EnvironmentAction
    from dsec_adapters.counter_dsec_environment import CounterDSecEnvironment
    from dsec.sdk.scheduled import ScheduledDSecClient
    report = {'status':'running', 'instance':cfg['instance'],
              'version':importlib.metadata.version('dsec-reproduce')}
    destination = Path(out).resolve()
    if destination.exists():
        raise FileExistsError('Refusing to overwrite acceptance evidence')
    destination.parent.mkdir(parents=True, exist_ok=True)
    environment = None
    async def restart(*, automatic=False):
        names = ['dsec-'+cfg['instance']+'-'+role+'.service' for role in ('sandbox', 'worker')]
        if automatic:
            from dsec.sdk.sandbox_transport import SandboxClient
            from dsec.sdk.rollout_transport import RolloutClient
            daemon = SandboxClient(Path(cfg['state_root'])/'sandboxes/service.sock')
            worker = RolloutClient(Path(cfg['state_root'])/'worker/worker.sock')
            old_daemon = await asyncio.to_thread(daemon.call, 'health')
            old_worker = await asyncio.to_thread(worker.call, 'health')
            # Kill only this instance's supervised launcher. ExecStartPre reaps
            # its orphaned daemon, while the original VMM and worker survive.
            await asyncio.to_thread(subprocess.run,
                ['systemctl', '--user', 'kill', '--kill-whom=main', '--signal=SIGKILL', names[0]],
                check=True, capture_output=True, text=True, timeout=10)
            deadline = time.monotonic()+120
            while time.monotonic() < deadline:
                try:
                    health = await asyncio.to_thread(daemon.call, 'health')
                    if health['pid'] != old_daemon['pid']:
                        break
                except Exception:
                    pass
                await asyncio.sleep(.1)
            else:
                raise TimeoutError('Supervisor did not restart the sandbox service')
            current_worker = await asyncio.to_thread(worker.call, 'health')
            if current_worker['pid'] != old_worker['pid']:
                raise RuntimeError('Sandbox supervisor failure interrupted the worker')
            report['automatic_supervision'] = {'new_daemon_pid':health['pid'],
                                               'same_worker_pid':current_worker['pid']}
        else:
            await asyncio.to_thread(subprocess.run, ['systemctl', '--user', 'restart', *names],
                                    check=True, capture_output=True, text=True, timeout=120)
        await asyncio.to_thread(wait_ready, cfg, 30)
    client = ScheduledDSecClient(Path(cfg['state_root'])/'worker/worker.sock')
    try:
        await client.open()
        started = time.monotonic()
        environment = DSecAgentEnvironment(client, CounterDSecEnvironment(3), 'counter-example',
                                           client.new_rollout_id())
        report['rollout_id'] = environment.rollout_id
        await environment.reset([{'role':'system','content':'Execute one shell action per turn.'}])
        report['create_s'] = time.monotonic()-started
        report['sandbox_id'] = environment.sandbox.sandbox_id
        command = 'test ! -e /rl-counter && printf 3 > /rl-counter'
        action = EnvironmentAction.shell(step_id=0, action_id='accept-write',
                                         command=command, timeout_ms=5000)
        message = {'role':'assistant','content':'```bash\n'+command+'\n```'}
        first = await environment.step(action, policy_message=message)
        if first.result['exit_code'] != 0:
            raise RuntimeError('Write action failed')
        duplicate = await environment.step(action, policy_message=message)
        if duplicate != first:
            raise RuntimeError('Repeated action did not return the committed observation')
        if restart_services:
            from dsec.sdk.sandbox_transport import SandboxClient
            daemon = SandboxClient(Path(cfg['state_root'])/'sandboxes/service.sock')
            before = await asyncio.to_thread(daemon.call, 'status', environment.sandbox.sandbox_id)
            await restart(automatic=True)
            after = await asyncio.to_thread(daemon.call, 'status', environment.sandbox.sandbox_id)
            if before['pid'] != after['pid'] or after['state'] != 'RUNNING':
                raise RuntimeError('Live VMM identity was not preserved across service restart')
            if await environment.step(action, policy_message=message) != first:
                raise RuntimeError('Committed action changed after service restart')
            report['live_restart'] = {'same_vmm_pid':after['pid'], 'action_deduplicated':True}
        await environment.sandbox.pause()
        if restart_services:
            await restart()
            view = await environment.sandbox.refresh()
            if view['state'] != 'PAUSED' or not view.get('lease_held'):
                raise RuntimeError('Paused rollout or lease was not restored')
            report['paused_restart'] = True
        observed = await environment.step(EnvironmentAction.shell(step_id=1,
                action_id='accept-read', command='cat /rl-counter', timeout_ms=5000),
                policy_message={'role':'assistant','content':'```bash\ncat /rl-counter\n```'})
        if observed.result['exit_code'] != 0 or observed.result['output'].strip() != '3':
            raise RuntimeError('Pause/resume lost the private file')
        verdict = await environment.evaluate()
        if verdict.score != 1:
            raise RuntimeError('Counter verifier did not return one')
        report.update(status='passed', reward=verdict.score, action_deduplicated=True,
                      pause_resume=True, dialogue=await environment.dialogue())
    except BaseException as exc:
        report.update(status='failed', error=repr(exc), traceback=traceback.format_exc())
        raise
    finally:
        if environment and environment.sandbox:
            try:
                await environment.stop()
                record = await environment.sandbox.refresh()
                report['cleanup'] = {k:record.get(k) for k in ('state','pending','lease_held')}
                if record['state'] != 'STOPPED' or record.get('pending') or record.get('lease_held'):
                    raise RuntimeError('Acceptance did not release the sandbox and lease')
            except Exception as exc:
                report.update(status='failed', cleanup_error=repr(exc))
        await client.close()
        atomic_json(destination, report)
    if report['status'] != 'passed':
        raise RuntimeError('Acceptance cleanup failed; see saved evidence')
    return {'status':report['status'], 'result':str(destination), 'reward':report['reward']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    commands = parser.add_subparsers(dest='operation', required=True)
    init = commands.add_parser('init', help='write an explicit host configuration without installing services')
    for key in ('binary', 'kernel', 'template', 'state-root', 'network-interface'):
        init.add_argument('--'+key, required=True)
    init.add_argument('--instance', default='local')
    init.add_argument('--catalog')
    commands.add_parser('validate', help='check configuration structure without reading image contents')
    check = commands.add_parser('doctor', help='inspect host prerequisites; never change permissions')
    check.add_argument('--live', action='store_true')
    render = commands.add_parser('render', help='render user systemd units; does not install/start them')
    render.add_argument('--out', type=Path, required=True)
    privileges = commands.add_parser('render-privileges', help='prepare a reviewable, scoped network installer')
    privileges.add_argument('--out', type=Path, required=True)
    privileges.add_argument('--user', required=True)
    privileges.add_argument('--slot-offset', type=int, required=True,
                            help='administrator-selected disjoint range within the 32768-slot pool')
    run = commands.add_parser('run', help='run a configured installed service in the foreground')
    run.add_argument('role', choices=('sandbox', 'worker'))
    run.add_argument('--validate-only', action='store_true')
    commands.add_parser('status')
    ready = commands.add_parser('wait', help='wait for sandbox and scheduled worker readiness')
    ready.add_argument('--timeout', type=float, default=30)
    acceptance = commands.add_parser('smoke', help='verify a non-TB episode on running services')
    acceptance.add_argument('--out', type=Path, required=True)
    acceptance.add_argument('--restart-services', action='store_true',
                            help='also restart this instance\'s user services; use an isolated acceptance instance')
    args = parser.parse_args()
    if args.operation == 'init':
        if args.config.exists():
            raise FileExistsError('Configuration already exists')
        cfg = {'schema':1, 'instance':args.instance, 'state_root':str(Path(args.state_root).resolve()),
               'service_group':'kvm',
               'sandbox':{k:str(Path(getattr(args,k)).resolve()) for k in ('binary','kernel','template')},
               'worker':{}, 'scheduler':{'cpu':2, 'memory_mb':1024, 'disk_mb':2048,
                'network_mbps':1000, 'api_episode_slots':2, 'api_inflight':2, 'api_rpm':60,
                'api_tpm':100000, 'min_memory_free_mb':1024, 'min_disk_free_mb':4096,
                'network_interface':args.network_interface}}
        cfg['sandbox']['capacity'] = 2
        if args.catalog:
            cfg['sandbox']['microvm_environment_catalog'] = str(Path(args.catalog).resolve())
        args.config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Reserve the final path without overwriting a concurrent initializer.
        with args.config.open('x') as stream:
            os.chmod(args.config, 0o600)
            json.dump(cfg, stream, indent=2)
        load_config(args.config)
        print(json.dumps({'configuration':str(args.config.resolve())}))
        return
    cfg = load_config(args.config)
    if args.operation == 'validate':
        result = {'ok':True,'instance':cfg['instance'],'configuration':cfg['_file']}
    elif args.operation == 'doctor':
        result = doctor(cfg, args.live)
    elif args.operation == 'render':
        result = render_units(cfg, args.out)
    elif args.operation == 'render-privileges':
        result = render_privileges(cfg, args.out, args.user, args.slot_offset)
    elif args.operation == 'wait':
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 120:
            raise ValueError('Readiness timeout must be between zero and 120 seconds')
        result = wait_ready(cfg, args.timeout)
    elif args.operation == 'run':
        run_service(cfg, args.role, args.validate_only)
        return
    elif args.operation == 'smoke':
        result = asyncio.run(smoke(cfg, args.out, restart_services=args.restart_services))
    else:
        from dsec.sdk.sandbox_transport import SandboxClient
        from dsec.sdk.rollout_transport import RolloutClient
        root = Path(cfg['state_root'])
        worker = RolloutClient(root/'worker/worker.sock')
        queue = worker.call('scheduler_status')
        result = {'sandbox_health':SandboxClient(root/'sandboxes/service.sock').call('health'),
                  'worker_health':worker.call('health'), 'active':queue['active'], 'pending':queue['pending'],
                  'sandbox_states':dict(Counter(x['state'] for x in
                   SandboxClient(root/'sandboxes/service.sock').call('list')))}
    print(json.dumps(result, indent=2))
    if result.get('ok') is False:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
