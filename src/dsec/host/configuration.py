"""Read and normalize host configuration; no service or installation effects."""
from copy import deepcopy
from dataclasses import fields
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from dsec.contracts.resources import ResourceBudget, NodeDemand
from dsec.contracts.network import validate_proxy_bypass_hosts


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


def normalize_config(data, source):
    """Copy and validate schema-1 data without writing files or changing process state."""
    path = Path(source).resolve()
    cfg = deepcopy(data)
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


def load_config(path):
    path = Path(path).resolve(strict=True)
    return normalize_config(json.loads(path.read_text()), path)


def initial_configuration(*, binary, kernel, template, state_root, network_interface,
                          instance='local', catalog=None):
    """Build the existing init defaults in memory; the caller chooses when to write."""
    cfg = {'schema':1, 'instance':instance, 'state_root':str(Path(state_root).resolve()),
           'service_group':'kvm',
           'sandbox':{key:str(Path(value).resolve()) for key,value in
                      (('binary',binary),('kernel',kernel),('template',template))},
           'worker':{}, 'scheduler':{'cpu':2, 'memory_mb':1024, 'disk_mb':2048,
            'network_mbps':1000, 'api_episode_slots':2, 'api_inflight':2, 'api_rpm':60,
            'api_tpm':100000, 'min_memory_free_mb':1024, 'min_disk_free_mb':4096,
            'network_interface':network_interface}}
    cfg['sandbox']['capacity'] = 2
    if catalog:
        cfg['sandbox']['microvm_environment_catalog'] = str(Path(catalog).resolve())
    return cfg
