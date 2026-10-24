"""Node-local preflight and scoped bundle preparation; no remote provisioning."""
import json
import grp
import os
from pathlib import Path
import pwd

from dsec.contracts.cluster import Quota, identifier
from dsec.host import node_probe
from dsec.host.diagnostics import doctor
from dsec.storage.catalog import EnvironmentCatalog


def load_node(path):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict) or not isinstance(value.get('node'), dict):
        raise ValueError('Expected a generated Node gateway configuration')
    return value


def check_binding(cfg, configuration):
    """Bind Node advertising to this Edge, never a different local service."""
    node = configuration['node']
    identifier(node['id'])
    if 'microvm' not in node.get('backends', ['microvm']):
        raise ValueError('This preflight/initializer applies to microVM Nodes')
    root = Path(cfg['state_root'])
    if Path(node['edge_socket']).resolve() != root/'sandboxes/service.sock':
        raise ValueError('Node Edge socket does not belong to this host instance')
    if node.get('worker_socket') and Path(node['worker_socket']).resolve() != root/'worker/worker.sock':
        raise ValueError('Node worker socket does not belong to this host instance')
    catalog = EnvironmentCatalog(cfg['sandbox']['microvm_environment_catalog'])
    advertised = node['environments']
    if (not advertised or set(advertised) != set(node['minimum_demands'])
            or not set(advertised) <= set(catalog.entries)):
        raise ValueError('Every advertised environment requires a prepared catalog entry and minimum demand')
    budget = cfg['scheduler']
    capacity = Quota(node['max_sandboxes'], budget['cpu'], budget['memory_mb'],
                     budget['disk_mb'], budget['network_mbps'])
    for minimum in node['minimum_demands'].values():
        if not Quota(**minimum).fits(capacity):
            raise ValueError('An advertised environment minimum exceeds the configured Node budget')


def memory_available_mb():
    memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines() if ':' in line)
    available = int(memory['MemAvailable'].split()[0])*1024
    limit = Path('/sys/fs/cgroup/memory.max')
    if limit.is_file() and limit.read_text().strip() != 'max':
        available = min(available, max(0, int(limit.read_text())-
            int(Path('/sys/fs/cgroup/memory.current').read_text())))
    return available//2**20


def preflight(cfg, configuration=None):
    platform = node_probe.observe(network_required=True)
    checks = [dict(item, status='ADMIN_CHECK_REQUIRED' if item['ok'] is None else
                   'PASS' if item['ok'] else 'BLOCKED') for item in platform['checks']]
    facts = dict(platform['facts'])

    def add(name, function, failure='BLOCKED'):
        try:
            detail = function()
            checks.append(dict(check=name, ok=True, status='PASS', detail=detail))
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            checks.append(dict(check=name, ok=False, status=failure, error=str(exc)[:300]))

    def require(condition, message):
        if not condition:
            raise ValueError(message)

    add('runtime_identity', lambda: require(os.geteuid() != 0,
        'Run preflight as the actual unprivileged Node service account'))
    if platform['ok']:
        def runtime_group():
            gid = grp.getgrnam('kvm').gr_gid
            require(gid in set(os.getgroups()) | {os.getgid()},
                    'Runtime session needs its pre-provisioned kvm group; refresh the login/session if necessary')
        add('runtime_group', runtime_group)
        catalog = cfg['sandbox'].get('microvm_environment_catalog')
        add('isolated_catalog', lambda: require(catalog is not None,
            'MicroVM Node initialization requires a prepared environment catalog'))
        if configuration is not None:
            add('node_binding', lambda: check_binding(cfg, configuration))
        for item in doctor(cfg)['checks']:
            result = dict(item, check='host:'+item['check'], status='PASS')
            if not item['ok']:
                result['status'] = 'RESOURCE_WAIT' if item['check'] == 'disk_floor' else 'BLOCKED'
                if item['check'] == 'network_helper' and not Path(cfg['network']['helper']).exists():
                    result['status'] = 'NEEDS_INITIALIZATION'
            checks.append(result)
        if not cfg.get('network'):
            checks.append(dict(check='network_configuration', ok=False, status='NEEDS_INITIALIZATION',
                               error='Prepare and apply the instance network helper bundle'))
        if cfg['sandbox'].get('overlaybd_ublk_socket'):
            add('storage_socket_access', lambda: require(os.access(cfg['sandbox']['overlaybd_ublk_socket'], os.W_OK),
                'Runtime account cannot access the configured ublk service socket'))
        facts['cpu_affinity_count'] = len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else os.cpu_count()
        facts['configured_budget'] = {name:cfg['scheduler'][name] for name in ('cpu','memory_mb','disk_mb','network_mbps')}

        def memory_floor():
            facts['memory_available_mb'] = memory_available_mb()
            require(facts['memory_available_mb'] > cfg['scheduler'].get('min_memory_free_mb', 4096),
                    'Available memory is below the configured admission floor')
        add('memory_floor', memory_floor, 'RESOURCE_WAIT')
    blocked = any(item['status'] in ('BLOCKED', 'RESOURCE_WAIT') for item in checks)
    pending = any(item['status'] == 'NEEDS_INITIALIZATION' for item in checks)
    deferred = any(item['status'] == 'ADMIN_CHECK_REQUIRED' for item in checks)
    status = ('BLOCKED' if blocked else 'NEEDS_INITIALIZATION' if pending else
              'ADMIN_CHECK_REQUIRED' if deferred else 'READY')
    return dict(ok=status == 'READY', status=status, eligible_for_initialization=not blocked,
                instance=cfg['instance'], node_id=configuration['node']['id'] if configuration else None,
                checks=checks, facts=facts, sandbox_boot_verified=False,
                execution_acceptance_required=True, scope='read-only prerequisites; no services or helper execution')


def prepare_node(cfg, configuration, out, user, slot_offset):
    account = pwd.getpwnam(user)
    if account.pw_uid != os.getuid() or os.geteuid() == 0:
        raise ValueError('Prepare the bundle as the named unprivileged service account')
    report = preflight(cfg, configuration)
    if not report['eligible_for_initialization']:
        raise ValueError('Node is blocked before initialization: '+', '.join(
            item['check'] for item in report['checks'] if item['status'] in ('BLOCKED', 'RESOURCE_WAIT')))
    expected = '/usr/local/libexec/dsec-'+cfg['instance']+'-netns-helper'
    if cfg.get('network', {}).get('helper', expected) != expected:
        raise ValueError('Scoped initialization requires this instance canonical helper path')
    from dsec.host.deployment import render_privileges
    result = render_privileges(cfg, out, user, slot_offset)
    return dict(result, preflight=report, services_started=False,
                next='Review and run the generated administrator installer; recheck preflight with host-with-network.json')
