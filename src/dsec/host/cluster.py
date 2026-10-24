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
import threading

from dsec._credentials import secret_file, NodeCredentials
from dsec.contracts.cluster import Quota, node_credential
from dsec.control.cluster_api import ClusterAPI
from dsec.control.coordination import EtcdStore
from dsec.control.iam import IAM
from dsec.control.placement import PlacementEngine
from dsec.control.watcher import Watcher
from dsec.host.cluster_http import ClusterHTTPServer
from dsec.control.node_gateway import NodeGateway
from dsec.host.node_transport import NodeRPC, RemoteNode
from dsec.sdk.cluster import JSONRPC
from dsec.sdk.sandbox_transport import SandboxClient
from dsec.sdk.rollout_transport import RolloutClient


def components(config, role):
    options = dict(config['coordination'])
    if options.get('token_file'):
        options['token'] = secret_file(options.pop('token_file'))
    iam = IAM(EtcdStore(**options), managed_nodes=config.get('node_lifecycle',False))
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
            inspection=SandboxClient(spec['edge_socket'], readonly_timeout=3),
            worker=RolloutClient(spec['worker_socket']) if spec.get('worker_socket') else None,
            worker_inspection=RolloutClient(spec['worker_socket'], timeout_s=3) if spec.get('worker_socket') else None)
    nodes = {}
    for node, spec in config['nodes'].items():
        legacy=secret_file(spec['token_file'])
        nodes[node] = RemoteNode(JSONRPC(spec['endpoints'], **spec['tls']),
            token=NodeCredentials(config['node_auth_keys_file'],node,legacy_token=legacy) if config.get('node_auth_keys_file') else legacy, route_key=key)
    membership = None
    if iam.managed_nodes:
        from dsec.control.node_lifecycle import NodeLifecycle
        from dsec.host.node_directory import NodeDirectory, NodeProbes
        membership = NodeLifecycle(iam)
        nodes = NodeDirectory(nodes, membership, config['dynamic_node_tls'], key,auth_keys_file=config.get('node_auth_keys_file'))
        probes = NodeProbes(nodes)
    else:
        probes = {node:remote.probe for node, remote in nodes.items()}
    watcher = Watcher(probes,
                      max_age=config.get('watcher_max_age', 10))
    placement = PlacementEngine(watcher, choices=config.get('placement_choices', 2),
                               overlay_seconds=config.get('watcher_max_age', 10), membership=membership)
    return ClusterAPI(iam, placement, nodes, route_key=key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role', choices=('controller', 'node', 'bootstrap'))
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if args.role == 'node' and 'microvm' in config['node'].get('backends', ['microvm']):
        # Fail before umask, gateway directories, owner locks or client creation.
        from dsec.host.node_probe import observe, require_eligible
        require_eligible(observe())
        if os.geteuid() == 0:
            raise ValueError('MicroVM Node gateway requires an unprivileged service account')
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
        mode = options.pop('node_token_mode',None)
        if mode is None:
            options['node_token'] = secret_file(options.pop('node_token_file'))
        elif mode == 'derived-v1' and 'node_token_file' not in options:
            options['node_token'] = node_credential(bytes.fromhex(secret_file(config['route_key_file'])),config['node']['id'])
        else:
            raise ValueError('Invalid Node gateway credential configuration')
        if config.get('node_auth_keys_file'):
            options['node_token']=NodeCredentials(config['node_auth_keys_file'],config['node']['id'],legacy_token=options['node_token'])
    server = ClusterHTTPServer(address, NodeRPC(value).dispatch if args.role == 'node' else value.call, **options)
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
        from dsec.host.node_polling import poll_nodes
        thread = threading.Thread(target=poll_nodes,args=(value.placement.watcher,stop,interval),
            kwargs={'workers':config.get('watcher_probe_workers',16)})
        thread.start();threads.append(thread)
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
