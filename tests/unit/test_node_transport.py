"""Transport adapters preserve ownership, failures and existing wire envelopes."""
from dataclasses import asdict
import unittest
from unittest.mock import Mock

from dsec.contracts.cluster import AuthorizationDenied, NodeView, Quota, SandboxRoute
from dsec.contracts.errors import RequestOutcomeUnknown
from dsec.host.node_transport import NodeRPC, RemoteNode


class NodeTransportTests(unittest.TestCase):
    def test_controller_node_adapters_roundtrip_without_a_second_admission_owner(self):
        key = b'k' * 32
        view = NodeView('n1', 'boot', ('microvm',), ('default',), (), 0,
                        Quota(1, 1, 512, 1024, 0), 0)
        gateway = Mock(route_key=key)
        gateway.health.return_value = view
        ingress = NodeRPC(gateway)
        captured = []

        class RPC:
            def send(self, token, envelope, **options):
                captured.append((token, envelope, options))
                return ingress.dispatch(token, envelope)

        remote = RemoteNode(RPC(), token='internal', route_key=key)
        receipt = dict(project='root', request_id='a' * 32)
        route = SandboxRoute('root', 'n1', 'microvm', 'b' * 12, 'a' * 32)
        remote.create(receipt)
        gateway.create.assert_called_once_with(receipt)
        remote.query_create(receipt)
        gateway.query_create.assert_called_once_with(receipt)
        remote.create_rollout(receipt)
        gateway.create_rollout.assert_called_once_with(receipt)
        remote.call_rollout(receipt, 'stop', {'rollout_id': 'a' * 32})
        gateway.call_rollout.assert_called_once_with(receipt, 'stop', {'rollout_id': 'a' * 32})
        remote.call(route, 'status', 'c' * 32, {})
        gateway.call.assert_called_once_with(route, 'status', 'c' * 32, {})
        self.assertEqual(remote.probe(), view)
        self.assertEqual(asdict(view), ingress.dispatch('internal', {'operation': 'node_probe'}))
        self.assertEqual([entry[2] for entry in captured], [
            {}, {'readonly': True}, {'timeout': 1810},
            {'readonly': False, 'timeout': 12110}, {'readonly': True},
            {'readonly': True, 'timeout': 5}])
        self.assertTrue(all(entry[0] == 'internal' for entry in captured))
        self.assertEqual(captured[0][1], dict(operation='node_create',
            request_id='a' * 32, receipt=receipt))

    def test_unknown_reply_propagates_without_retry_or_target_change(self):
        failure = RequestOutcomeUnknown('Reply lost')
        rpc = Mock()
        rpc.send.side_effect = failure
        remote = RemoteNode(rpc, token='internal', route_key=b'k' * 32)
        receipt = dict(project='root', request_id='a' * 32)
        with self.assertRaises(RequestOutcomeUnknown) as raised:
            remote.create(receipt)
        self.assertIs(raised.exception, failure)
        rpc.send.assert_called_once()
        gateway = Mock()
        gateway.create.side_effect = failure
        with self.assertRaises(RequestOutcomeUnknown) as raised:
            NodeRPC(gateway).dispatch('internal', dict(operation='node_create',
                request_id='a' * 32, receipt=receipt))
        self.assertIs(raised.exception, failure)
        gateway.create.assert_called_once_with(receipt)

    def test_invalid_routing_envelope_cannot_reach_gateway_decisions(self):
        gateway = Mock(route_key=b'k' * 32)
        ingress = NodeRPC(gateway)
        with self.assertRaises(ValueError):
            ingress.dispatch('internal', dict(operation='node_create',
                request_id='invalid', receipt={}))
        with self.assertRaises(AuthorizationDenied):
            ingress.dispatch('internal', dict(operation='node_call',
                request_id='a' * 32, sandbox_id='forged', edge_operation='stop'))
        with self.assertRaises(ValueError):
            ingress.dispatch('internal', {'operation': 'unknown'})
        self.assertEqual(gateway.mock_calls, [])


if __name__ == '__main__':
    unittest.main()
