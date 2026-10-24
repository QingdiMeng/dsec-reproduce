"""Dynamic, authenticated route materialization; no membership decisions here."""
from collections.abc import Mapping
import threading

from dsec.contracts.cluster import node_credential
from dsec.control.node_lifecycle import specification
from dsec.host.node_transport import RemoteNode
from dsec.sdk.cluster import JSONRPC
from dsec._credentials import NodeCredentials


class NodeDirectory(Mapping):
    def __init__(self, static, lifecycle, tls, route_key, *, rpc_factory=JSONRPC, auth_keys_file=None):
        self.static, self.lifecycle = dict(static), lifecycle
        self.tls, self.route_key, self.rpc_factory = dict(tls), route_key, rpc_factory
        if set(self.tls) not in ({'ca_file','cert_file','key_file'},{'security_file'}):
            raise ValueError('Dynamic Nodes require fleet CA and controller mTLS identity')
        self.auth_keys_file=auth_keys_file
        self.lock, self.cache = threading.Lock(), {}

    def _materialize(self, member):
        if member['spec'] is None:
            return
        spec = specification(member['spec'])
        node = spec['id']
        if node in self.static:
            if tuple(spec['endpoints']) != self.static[node].rpc.endpoints:
                raise ValueError('Registered endpoint conflicts with an existing static Node route')
            return
        with self.lock:
            if node not in self.cache:
                legacy=node_credential(self.route_key,node)
                token=NodeCredentials(self.auth_keys_file,node,legacy_token=legacy) if self.auth_keys_file else legacy
                self.cache[node] = RemoteNode(self.rpc_factory(spec['endpoints'], **self.tls),token=token, route_key=self.route_key)

    def refresh(self):
        for member in self.lifecycle.records():
            self._materialize(member)

    def __getitem__(self, node):
        member = None
        if node in self.static:
            member = self.lifecycle.read(node)
            if member is not None:
                self._materialize(member)
            return self.static[node]
        with self.lock:
            result = self.cache.get(node)
        if result is not None:
            return result
        member = self.lifecycle.read(node)
        if member is None or member['spec'] is None:
            raise KeyError(node)
        self._materialize(member)
        with self.lock:
            return self.cache[node]

    def __iter__(self):
        self.refresh()
        with self.lock:
            return iter(sorted(set(self.static) | set(self.cache)))

    def __len__(self):
        return sum(1 for _ in self)


class NodeProbes(Mapping):
    def __init__(self, directory):
        self.directory = directory

    def __getitem__(self, node):return self.directory[node].probe
    def __iter__(self):return iter(self.directory)
    def __len__(self):return len(self.directory)
