"""Explicit HA controller/node composition; no automatic host provisioning.

Run with python -m dsec.host.cluster {controller,node,bootstrap} --config PATH.
All artifacts, identities, certificates and etcd endpoints are provisioned by
the operator; this entrypoint never changes Docker, firewall or KVM permissions.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import threading

from dsec.contracts.cluster import Quota
from dsec.control.cluster_api import ClusterAPI
from dsec.control.coordination import EtcdStore
from dsec.control.iam import IAM
from dsec.control.placement import PlacementEngine
from dsec.control.watcher import Watcher
from dsec.host.cluster_http import ClusterHTTPServer
from dsec.host.cluster_node import NodeGateway, RemoteNode
from dsec.sdk.cluster import JSONRPC
from dsec.sdk.sandbox_transport import SandboxClient


def secret_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        details = os.fstat(fd)
        if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid()
                or details.st_mode & 0o077 or details.st_size > 4096):
            raise ValueError('Secret must be a private regular file owned by this service user')
        with os.fdopen(fd, closefd=False) as stream:
            value = stream.read().strip()
        if len(value) < 32 or '\n' in value or '\r' in value:
            raise ValueError('Invalid private credential')
        return value
    finally:
        os.close(fd)


def components(config, role):
    options = dict(config['coordination'])
    if options.get('token_file'):
        options['token'] = secret_file(options.pop('token_file'))
    iam = IAM(EtcdStore(**options))
    if role == 'bootstrap':
        iam.bootstrap(secret_file(config['bootstrap_token_file']), Quota(**config['root_quota']))
        return iam
    key = bytes.fromhex(secret_file(config['route_key_file']))
    if len(key) < 32:
        raise ValueError('Route key must contain at least 256 bits')
    if role == 'node':
        spec = config['node']
        boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return NodeGateway(spec['id'], SandboxClient(spec['edge_socket']), iam,
            route_key=key, boot_id=boot_id, environments=spec['environments'],
            backends=spec.get('backends', ['microvm']), capabilities=spec.get('capabilities', []),
            max_sandboxes=spec['max_sandboxes'], minimum_demands=spec['minimum_demands'],
            inspection=SandboxClient(spec['edge_socket'], readonly_timeout=3))
    nodes = {}
    for node, spec in config['nodes'].items():
        nodes[node] = RemoteNode(JSONRPC(spec['endpoints'], **spec['tls']),
            token=secret_file(spec['token_file']), route_key=key)
    watcher = Watcher({node:remote.probe for node, remote in nodes.items()},
                      max_age=config.get('watcher_max_age', 10))
    placement = PlacementEngine(watcher, choices=config.get('placement_choices', 2),
                               overlay_seconds=config.get('watcher_max_age', 10))
    return ClusterAPI(iam, placement, nodes, route_key=key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role', choices=('controller', 'node', 'bootstrap'))
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    os.umask(0o077)
    owner = None
    if args.role == 'node':
        root = Path(config['node']['gateway_root']).resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        # The lock is tied to the Edge socket, not a configurable gateway root:
        # two gateways cannot bypass the node gate using different log roots.
        edge_parent = Path(config['node']['edge_socket']).resolve().parent
        owner = (edge_parent/'cluster-gateway.owner.lock').open('a')
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
    value = components(config, args.role)
    if args.role == 'bootstrap':
        print(json.dumps({'status':'bootstrapped'}))
        return
    options = dict(config['server'])
    address = (options.pop('host'), options.pop('port'))
    if args.role == 'node':
        options['node_token'] = secret_file(options.pop('node_token_file'))
    server = ClusterHTTPServer(address, value.dispatch if args.role == 'node' else value.call, **options)
    stop = threading.Event()
    def signal_stop(*_):
        stop.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, signal_stop)
    threads = []
    if args.role == 'node':
        def reconcile():
            while not stop.is_set():
                try:
                    value.reconcile()
                except Exception as exc:
                    # No raw receipt, command, or credentials in diagnostics.
                    print(json.dumps({'event':'reconcile-deferred', 'error':type(exc).__name__}), flush=True)
                stop.wait(2)
        thread = threading.Thread(target=reconcile)
        thread.start(); threads.append(thread)
    if args.role == 'controller':
        interval = config.get('watcher_poll_seconds', 2)
        if not 0 < interval < value.placement.watcher.max_age:
            raise ValueError('Watcher probe interval must be shorter than freshness window')
        def probe(node):
            while not stop.is_set():
                value.placement.watcher.probe(node)
                stop.wait(interval)
        # Separate probe workers keep one slow node from blocking the fleet.
        for node in value.placement.watcher.probes:
            thread = threading.Thread(target=probe, args=(node,))
            thread.start(); threads.append(thread)
    service = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.1})
    service.start()
    print(json.dumps({'status':'serving', 'role':args.role, 'address':server.server_address}), flush=True)
    try:
        while not stop.wait(.5):
            if not service.is_alive():
                raise RuntimeError('Cluster listener terminated')
    finally:
        stop.set()
        server.shutdown(); service.join(); server.server_close()
        for thread in threads:
            thread.join()
        if owner:
            owner.close()


if __name__ == '__main__':
    main()
