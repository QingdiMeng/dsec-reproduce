"""Read-only deployment planning; no secrets, subprocesses or host mutations."""
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import math
from pathlib import Path, PurePosixPath
import re
import zipfile


IMAGE = re.compile(r'[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}')
NAME = re.compile(r'[a-z0-9][a-z0-9_-]{0,47}')
DEFAULT_QUOTA = dict(concurrency=4, cpu=8, memory_mb=8192, disk_mb=16384, network_mbps=1000)


def ipv4_address(value, *, allow_loopback=True):
    if not isinstance(value, str):
        raise ValueError('Host addresses must be explicit IPv4 strings')
    address = ipaddress.IPv4Address(value)
    if (address.is_unspecified or address.is_multicast or address.is_link_local
            or (address.is_loopback and not allow_loopback)):
        raise ValueError('Explicit reachable IPv4 addresses required')
    return str(address)


@dataclass(frozen=True)
class DeploymentPlan:
    out: Path
    wheel: Path
    wheel_sha256: str
    project: str
    python_image: str
    etcd_image: str
    host_address: str
    port_base: int
    nodes_json: str
    root_quota_json: str
    legacy_node: bool
    topology_json: str | None

    @property
    def nodes(self):
        return json.loads(self.nodes_json)

    @property
    def root_quota(self):
        return json.loads(self.root_quota_json)

    def _single_node(self):
        nodes = self.nodes
        if len(nodes) != 1:
            raise ValueError('Use plan.nodes for a multi-Node deployment')
        return nodes[0]

    @property
    def node_spec(self):
        return self._single_node()['spec']

    @property
    def node_address(self):
        return self._single_node()['address']

    @property
    def node_directory(self):
        return self._single_node()['directory']

    @property
    def node_port(self):
        return self._single_node()['port']

    @property
    def topology(self):
        return json.loads(self.topology_json) if self.topology_json is not None else None

    def describe(self):
        topology = self.topology
        summary = dict(status='planned', project=self.project, directory=str(self.out),
            wheel_sha256=self.wheel_sha256,
            images=dict(python=self.python_image, etcd=self.etcd_image),
            controllers=topology['controllers'] if topology else [
                dict(address=self.host_address, port=self.port_base+n) for n in (10, 11)],
            etcd=topology['etcd'] if topology else [
                dict(address=self.host_address, client_port=self.port_base+n) for n in range(3)],
            etcd_failure_domains=len({m['address'] for m in topology['etcd']}) if topology else 1,
            nodes=[dict(id=n['spec']['id'], address=n['address'], port=n['port'],
                        directory=n['directory'], backends=n['spec']['backends'],
                        environments=n['spec']['environments']) for n in self.nodes],
            root_quota=self.root_quota,
            node_initialization='operator-provisioned Edge, images, storage and permissions',
            model_gpu_management='external', credentials_generated=False,
            files_written=False, services_started=False)
        if self.legacy_node:
            summary['node'] = dict(id=self.node_spec['id'], address=self.node_address,
                                   port=self.node_port, directory=self.node_directory)
        return summary


def topology_file(path):
    if path is None:
        return None
    value=json.loads(path.read_text())
    if not isinstance(value,dict) or set(value)!={'etcd','controllers'} or len(value['etcd'])!=3 or len(value['controllers'])!=2:
        raise ValueError('Topology requires three etcd members and two controllers')
    listeners=set()
    identities={}
    for kind,fields in (('etcd',{'address','client_port','peer_port','uid','gid'}),
                        ('controllers',{'address','port','uid','gid'})):
        for member in value[kind]:
            if not isinstance(member,dict) or set(member)!=fields:
                raise ValueError('Invalid topology member fields')
            address=ipv4_address(member['address'], allow_loopback=False)
            for name in fields-{'address','uid','gid'}:
                port=member[name]
                if type(port) is not int or not 1024<=port<=65535 or (str(address),port) in listeners:
                    raise ValueError('Topology listener collision or invalid port')
                listeners.add((str(address),port))
            if (type(member['uid']) is not int or member['uid']<=0
                    or type(member['gid']) is not int or member['gid']<0):
                raise ValueError('Service requires an explicit non-root UID/GID')
            identity=(member['uid'],member['gid'])
            if identities.setdefault(str(address),identity)!=identity:
                raise ValueError('One exported host directory requires one service UID/GID')
    return value


def quota_fields(value):
    """Check JSON configuration fields, not policy, admission or quota arithmetic."""
    if not isinstance(value, dict) or set(value) != set(DEFAULT_QUOTA):
        raise ValueError('Explicit concurrency/CPU/memory/disk/network quota fields required')
    for name, amount in value.items():
        if (type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0
                or (name in ('concurrency', 'memory_mb', 'disk_mb') and type(amount) is not int)):
            raise ValueError('Invalid quota configuration field: '+name)


def linux_path(value):
    if (not isinstance(value, str) or not value.startswith('/') or '\x00' in value
            or '..' in PurePosixPath(value).parts or value.startswith('//')):
        raise ValueError('Use an absolute Linux path without traversal')
    return str(PurePosixPath(value))


def node_spec(spec):
    required = {'id','edge_socket','max_sandboxes','backends','environments','minimum_demands'}
    if not isinstance(spec, dict) or not required <= set(spec) or set(spec)-required-{'worker_socket'}:
        raise ValueError('Node spec requires identity, sockets, backends, environments and minimum demands')
    if not isinstance(spec['id'], str) or not NAME.fullmatch(spec['id']):
        raise ValueError('Invalid Node identity')
    for key in ('edge_socket','worker_socket'):
        if key == 'edge_socket' or key in spec:
            linux_path(spec[key])
    for key in ('backends', 'environments'):
        values = spec[key]
        if (not isinstance(values, list) or not values
                or any(not isinstance(v, str) or not v for v in values)
                or len(set(values)) != len(values)):
            raise ValueError('Node backends/environments must be nonempty unique string lists')
    if not isinstance(spec['minimum_demands'], dict):
        raise ValueError('Node minimum demands must be an environment mapping')
    if (not spec['environments'] or set(spec['environments']) != set(spec['minimum_demands'])
            or type(spec['max_sandboxes']) is not int or spec['max_sandboxes'] < 1):
        raise ValueError('Register every advertised environment with a minimum demand')
    for minimum in spec['minimum_demands'].values():
        quota_fields(minimum)
    return spec


def read_nodes(args):
    inventory = getattr(args, 'nodes', None)
    legacy = not bool(inventory)
    if inventory:
        if any(getattr(args, name, None) is not None for name in (
                'node_address', 'node_directory', 'node_spec', 'node_port')):
            raise ValueError('--nodes cannot be combined with single-Node options')
        value = json.loads(Path(inventory).read_text())
        if (not isinstance(value, dict) or type(value.get('schema')) is not int
                or value['schema'] != 1 or set(value) - {'schema', 'nodes', 'root_quota'}):
            raise ValueError('Node inventory requires schema 1, nodes and optional root_quota')
        nodes = value.get('nodes')
        quota = value.get('root_quota', DEFAULT_QUOTA)
    else:
        if any(getattr(args, name, None) is None for name in ('node_address', 'node_directory', 'node_spec')):
            raise ValueError('Provide --nodes or all single-Node address/directory/spec options')
        port = getattr(args, 'node_port', None)
        nodes = [dict(address=args.node_address, directory=args.node_directory,
                      port=18444 if port is None else port,
                      spec=json.loads(Path(args.node_spec).read_text()))]
        quota = DEFAULT_QUOTA
    if not isinstance(nodes, list) or not nodes:
        raise ValueError('A nonempty Node inventory is required')
    quota_fields(quota)
    ids = set()
    for node in nodes:
        if not isinstance(node, dict) or set(node) != {'address', 'directory', 'port', 'spec'}:
            raise ValueError('Each Node needs address, port, directory and spec')
        node['address'] = ipv4_address(node['address'])
        if type(node['port']) is not int or not 1024 <= node['port'] <= 65535:
            raise ValueError('Use valid non-privileged Node ports')
        node['directory'] = linux_path(node['directory'])
        if node['directory'] == '/':
            raise ValueError('Node destination needs a private subdirectory')
        node_spec(node['spec'])
        identity = node['spec']['id']
        if identity in ids:
            raise ValueError('Duplicate Node identity')
        ids.add(identity)
    return nodes, quota, legacy


def validate_bindings(nodes, topology, host_address, port_base):
    listeners = set()
    if topology:
        for member in topology['etcd']:
            for field in ('client_port', 'peer_port'):
                listeners.add((member['address'], member[field]))
        listeners.update((m['address'], m['port']) for m in topology['controllers'])
    else:
        listeners.update((host_address, port_base+n) for n in (0, 1, 2, 10, 11))
    roots = {}
    for node in nodes:
        endpoint = (node['address'], node['port'])
        if endpoint in listeners:
            raise ValueError('Node listener collision with another deployment role')
        listeners.add(endpoint)
        # The gateway's exclusive owner lock is tied to the Edge socket parent.
        paths = [PurePosixPath(node['directory']), PurePosixPath(node['spec']['edge_socket']).parent]
        if node['spec'].get('worker_socket'):
            paths.append(PurePosixPath(node['spec']['worker_socket']).parent)
        for previous in roots.setdefault(node['address'], []):
            if any(a == b or a in b.parents or b in a.parents for a in paths for b in previous):
                raise ValueError('Nodes on the same host require disjoint state/socket roots')
        roots[node['address']].append(paths)


def read_plan(args):
    topology=topology_file(getattr(args,'topology',None))
    if not NAME.fullmatch(args.project):
        raise ValueError('Use a short lowercase dedicated Compose project name')
    for value in (args.python_image, args.etcd_image):
        if not IMAGE.fullmatch(value):
            raise ValueError('Container images must be repository@sha256 identities')
    ipv4_address(args.host_address)
    if type(args.port_base) is not int or not 1024 <= args.port_base <= 65524:
        raise ValueError('Use valid non-privileged ports')
    nodes, quota, legacy = read_nodes(args)
    validate_bindings(nodes, topology, args.host_address, args.port_base)
    wheel = args.wheel.resolve(strict=True)
    if not wheel.name.startswith('dsec_reproduce-') or not wheel.name.endswith('-py3-none-any.whl'):
        raise ValueError('An explicitly built universal dsec-reproduce wheel is required')
    with zipfile.ZipFile(wheel) as package:
        for module in ('dsec/host/cluster.py','dsec/sdk/rollout_api.py',
                       'dsec/control/node_lifecycle.py','dsec/host/node_directory.py'):
            if module not in package.namelist():
                raise ValueError('Wheel lacks the current cluster/rollout API entry')
    return DeploymentPlan(out=args.out.resolve(), wheel=wheel,
        wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
        project=args.project, python_image=args.python_image, etcd_image=args.etcd_image,
        host_address=args.host_address, port_base=args.port_base, legacy_node=legacy,
        nodes_json=json.dumps(nodes), root_quota_json=json.dumps(quota),
        topology_json=json.dumps(topology) if topology is not None else None)
