"""Pure role configuration derived from one validated deployment plan."""
from copy import deepcopy
from pathlib import PurePosixPath


def node_directory(plan, node):
    return 'node' if plan.legacy_node else 'nodes/'+node['spec']['id']


def node_token_file(plan, node):
    return 'node-token' if plan.legacy_node else 'node-'+node['spec']['id']+'-token'


def role_configurations(plan, *, uid, gid):
    topology = plan.topology
    endpoints = ([f"https://{m['address']}:{m['client_port']}" for m in topology['etcd']]
                 if topology else [f'https://etcd{n}:2379' for n in range(1, 4)])
    prefix = '/dsec/compose/'+plan.project+'/'
    coordination = dict(endpoints=endpoints, prefix=prefix, ca_file='/secrets/ca.crt',
        cert_file='/secrets/etcd-client.crt', key_file='/secrets/etcd-client.key',
        token_file='/secrets/etcd-token', timeout=1,
        authentication=dict(username='dsec', password_file='/secrets/dsec-password', renew_before_seconds=300))
    routes, nodes = {}, {}
    for node in plan.nodes:
        identity, remote = node['spec']['id'], PurePosixPath(node['directory'])
        routes[identity] = dict(endpoints=[f"https://{node['address']}:{node['port']}"],
            token_file='/secrets/'+node_token_file(plan, node),
            tls=dict(ca_file='/secrets/ca.crt', cert_file='/secrets/controller.crt', key_file='/secrets/controller.key'))
        node_coordination = dict(coordination, endpoints=(endpoints if topology else
            [f'https://{plan.host_address}:{plan.port_base+n}' for n in range(3)]))
        for field in ('ca_file', 'cert_file', 'key_file', 'token_file'):
            node_coordination[field] = str(remote/'secrets'/PurePosixPath(coordination[field]).name)
        node_coordination['authentication'] = dict(coordination['authentication'],
            password_file=str(remote/'secrets/dsec-password'))
        if topology or not plan.legacy_node:
            node_coordination.pop('token_file')
        nodes[identity] = dict(coordination=node_coordination,
            node_lifecycle=True,
            route_key_file=str(remote/'secrets/route-key'),
            node=dict(node['spec'], gateway_root=str(remote/'gateway')),
            server=dict(host=node['address'], port=node['port'], max_requests=32,
                cert_file=str(remote/'secrets/node.crt'), key_file=str(remote/'secrets/node.key'),
                client_ca_file=str(remote/'secrets/ca.crt'), node_token_file=str(remote/'secrets/node-token')))
    controller = dict(coordination=coordination, route_key_file='/secrets/route-key',
        node_lifecycle=True,
        dynamic_node_tls=dict(ca_file='/secrets/ca.crt',cert_file='/secrets/controller.crt',key_file='/secrets/controller.key'),
        bootstrap_token_file='/secrets/bootstrap-token', root_quota=plan.root_quota, nodes=routes,
        watcher_poll_seconds=1, watcher_max_age=5, placement_choices=2,
        server=dict(host='0.0.0.0', port=8443, max_requests=32, console=True,
                    cert_file='/secrets/api.crt', key_file='/secrets/api.key'))
    init_coordination = dict(coordination)
    init_coordination.pop('token_file')
    initialize = dict(endpoints=endpoints, prefix=prefix,
                      coordination=init_coordination, root_quota=plan.root_quota)
    if not plan.legacy_node:
        initialize['export_node_token'] = False
    api_endpoints = ([f"https://{m['address']}:{m['port']}" for m in topology['controllers']]
                     if topology else [f'https://{plan.host_address}:{plan.port_base+n}' for n in (10, 11)])
    client = dict(endpoints=api_endpoints, project='root',
                  token_file='secrets/controllers/bootstrap-token', ca_file='secrets/controllers/ca.crt')
    env = dict(COMPOSE_PROJECT_NAME=plan.project, ETCD_IMAGE=plan.etcd_image, PYTHON_IMAGE=plan.python_image,
        HOST_ADDRESS=plan.host_address, DSEC_UID=uid, DSEC_GID=gid,
        **{f'ETCD_PORT_{n+1}': plan.port_base+n for n in range(3)},
        API_PORT_1=plan.port_base+10, API_PORT_2=plan.port_base+11)
    return dict(controller=deepcopy(controller), initialize=deepcopy(initialize),
                nodes={identity:deepcopy(config) for identity,config in nodes.items()},
                client=client, env=env)
