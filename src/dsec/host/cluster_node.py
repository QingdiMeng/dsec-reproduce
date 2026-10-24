"""Compatibility constructor; production composition uses the control gateway."""
from dsec.control.node_gateway import NodeGateway as _NodeGateway, edge_request_id, public_proof
from dsec.host.node_transport import NodeRPC, RemoteNode


class NodeGateway(_NodeGateway):
    # Preserve the historical .dispatch entry point without making domain
    # decisions depend on RPC framing or the Host composition layer.
    def dispatch(self, credential, envelope):
        return NodeRPC(self).dispatch(credential, envelope)
