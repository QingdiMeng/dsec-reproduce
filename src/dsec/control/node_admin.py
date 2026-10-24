"""Root-authorized fleet API composition; domain owns transitions, Node owns proof."""
from dsec.contracts.cluster import AuthorizationDenied, identifier
from dsec.control.node_lifecycle import NodeLifecycle


OPERATIONS = frozenset('node.'+name for name in ('join','get','security','activate','drain','maintain','resume','retire','withdraw','upgrade','commit','rollback'))


def dispatch(api, token, project, operation_id, operation, args):
    if project != 'root':
        raise AuthorizationDenied('Fleet administration requires the root project')
    api.iam.authorize(token, 'root', 'iam.policy')
    if not api.iam.managed_nodes:
        raise ValueError('Enable managed Node lifecycle on all controllers and Nodes before enrollment')
    lifecycle = NodeLifecycle(api.iam)
    if operation == 'node.join':
        if set(args) != {'spec'}:
            raise ValueError('Node join requires a public Node specification only')
        return lifecycle.join(token, operation_id, args['spec'])
    expected = {'node'} if operation in ('node.get','node.security') else {'node','version'}
    if operation == 'node.upgrade':expected.add('release')
    if set(args) != expected:
        raise ValueError('Node lifecycle requires node and an explicit expected version')
    node = identifier(args['node'])
    current = lifecycle.read(node)
    if operation == 'node.get':
        return current
    if operation == 'node.security':return api.nodes[node].security()
    action, version = operation.removeprefix('node.'), args['version']
    # A lost successful reply can be reconciled without probing a now-retired
    # Node. The domain still revalidates authorization and request digest.
    repeated = current and current['last'] and current['last']['request_id'] == operation_id
    proof = None
    if action not in ('drain','withdraw') and not repeated and current and current['version'] == version:
        proof = api.nodes[node].lifecycle_proof(version)
    return lifecycle.transition(token, operation_id, node, version, action, proof=proof, release=args.get('release'))
