"""Build deterministic service plans from normalized host configuration.

Plans describe argv, environment overlays and owned output paths. Creating a
plan does not touch the state directory, publish budgets or contact a service.
"""
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import sys
from dsec.host.configuration import BOOL_OPTIONS, LIST_OPTIONS


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


@dataclass(frozen=True)
class ServicePlan:
    role: str
    command: tuple[str, ...]
    directories: tuple[tuple[Path, bool], ...]
    environment_updates: dict[str, str]
    environment_removals: tuple[str, ...]
    budget_file: Path | None
    budget_settings: dict | None
    readiness_socket: Path | None
    worker_configuration: dict | None


def plan_service(cfg, role, validate_only=False):
    """Plan one existing foreground service; execution remains explicit."""
    if role not in ('sandbox', 'worker'):
        raise ValueError('Unknown host service role')
    root = Path(cfg['state_root'])
    prefix = [sys.executable, '-I', '-B', '-m']
    if validate_only:
        if role != 'sandbox':
            raise ValueError('Artifact validation applies to the sandbox service')
        return ServicePlan(role, tuple(prefix + ['sandboxd', *sandbox_arguments(cfg), '--validate-only']),
                           (), {}, (), None, None, None, None)
    storage_access = 'overlaybd_ublk_socket' in cfg['sandbox']
    directories = ((root, storage_access), (root/'sandboxes', storage_access),
                   (root/'worker', False))
    environment = {'docker_broker_socket':'DSEC_DOCKER_BROKER_SOCKET'}
    if role == 'sandbox':
        environment.update(container_catalog='DSEC_ENVIRONMENT_CATALOG',
                           container_root='DSEC_CONTAINER_ROOT',
                           container_agent='DSEC_CONTAINER_AGENT')
    updates = {variable:cfg['worker'][key] for key,variable in environment.items()
               if key in cfg['worker']}
    budget_file = root/'worker/budget.json'
    settings = deepcopy({**cfg['scheduler'], 'disk_path':str(root)})
    if role == 'sandbox':
        command = prefix + ['sandboxd', *sandbox_arguments(cfg)]
        removals = ('DSEC_ADMISSION_WORKER_SOCKET',)
        readiness_socket = worker_configuration = None
    else:
        command = prefix + ['rollout_workerd', *worker_arguments(cfg, budget_file)]
        removals = ()
        readiness_socket = root/'sandboxes/service.sock'
        worker_configuration = deepcopy(cfg['sandbox'])
    return ServicePlan(role, tuple(command), directories, updates, removals,
                       budget_file, settings, readiness_socket, worker_configuration)
