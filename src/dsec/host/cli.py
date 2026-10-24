"""Configure, inspect and run an installed single-host DSec runtime."""
import argparse
import asyncio
# Retain the historical module's patch/import surface for installed callers.
import importlib.metadata
import json
import math
from pathlib import Path
import sys
from dsec.host.configuration import (
    load_config, normalize_config, initial_configuration,
    PATH_OPTIONS, SCALAR_OPTIONS, LIST_OPTIONS, BOOL_OPTIONS, WORKER_PATHS, NETWORK_KEYS)
from dsec.host.composition import sandbox_arguments, worker_arguments
from dsec.host.services import (
    run_service, wait_ready, status, check_private_directory, private_directory)
from dsec.host.deployment import (
    initialize_configuration, render_units, render_privileges, systemd_quote)
from dsec.host.diagnostics import doctor
from dsec.host.acceptance import smoke
from dsec.host.node_setup import preflight, load_node, prepare_node


def build_parser():
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
    node = commands.add_parser('preflight', help='read-only microVM Node capability and configuration checks')
    node.add_argument('--node-config', type=Path)
    setup = commands.add_parser('prepare-node', help='preflight and render scoped administrator initialization; never apply/start')
    setup.add_argument('--node-config', type=Path, required=True)
    setup.add_argument('--out', type=Path, required=True)
    setup.add_argument('--user', required=True)
    setup.add_argument('--slot-offset', type=int, required=True)
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
    return parser


def main():
    args = build_parser().parse_args()
    if args.operation == 'init':
        result = initialize_configuration(args.config, binary=args.binary, kernel=args.kernel,
            template=args.template, state_root=args.state_root,
            network_interface=args.network_interface, instance=args.instance, catalog=args.catalog)
        print(json.dumps(result))
        return
    cfg = load_config(args.config)
    if args.operation == 'validate':
        result = {'ok':True,'instance':cfg['instance'],'configuration':cfg['_file']}
    elif args.operation == 'doctor':
        result = doctor(cfg, args.live)
    elif args.operation == 'preflight':
        result = preflight(cfg, load_node(args.node_config) if args.node_config else None)
    elif args.operation == 'prepare-node':
        result = prepare_node(cfg, load_node(args.node_config), args.out, args.user, args.slot_offset)
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
        result = status(cfg)
    print(json.dumps(result, indent=2))
    if result.get('ok') is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
