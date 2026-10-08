"""No-root checks for namespace allocation and packet-policy construction."""
import importlib.util
import sys
from types import SimpleNamespace
from pathlib import Path
import unittest
from unittest.mock import patch

from network_namespace import NetnsNetworkManager

import privileged_netns_helper as helper


class NetnsPlanTests(unittest.TestCase):
    def test_dax_alias_uses_explicit_binary_location(self):
        manager = NetnsNetworkManager(dax_binary='/opt/dsec/firecracker-dax')
        launcher = manager.launcher('aaaaaaaaaaaa', 'ns-0')
        response = {'pid':42, 'namespace_attested':True, 'identity':{}}
        attached = SimpleNamespace(AttachedProcess=lambda *args, **kwargs:'attached')
        with patch.object(manager, 'ensure'), patch.object(manager, '_call', return_value=response) as call, \
                patch.dict(sys.modules, {'dsec.runtime.registry':attached}):
            self.assertEqual(launcher.start('/opt/dsec/firecracker-dax', '/tmp/api.sock', None, 'vmm.log'),
                             'attached')
            call.assert_called_once_with('launch', 'aaaaaaaaaaaa', 'vmm.log@dax39')
            call.reset_mock()
            launcher.start('/opt/dsec/firecracker', '/tmp/api.sock', None, 'vmm.log')
            call.assert_called_once_with('launch', 'aaaaaaaaaaaa', 'vmm.log')

    def test_fixed_guest_link_unique_host_links(self):
        first = helper.names("aaaaaaaaaaaa", 0)
        second = helper.names("bbbbbbbbbbbb", 1)
        self.assertEqual(first["guest_cidr"], second["guest_cidr"])
        self.assertNotEqual(first["host_cidr"], second["host_cidr"])
        self.assertNotEqual(first["namespace"], second["namespace"])
        self.assertEqual(first["host_cidr"], "10.231.0.0/31")
        self.assertEqual(second["host_cidr"], "10.231.0.2/31")
        self.assertEqual(helper.names("cccccccccccc", 32767)["peer_cidr"],
                         "10.231.255.255/31")
        with self.assertRaises(ValueError):
            helper.names("cccccccccccc", 32768)

    def test_slots_do_not_reuse_live_identity(self):
        manager = NetnsNetworkManager(max_slots=2)
        self.assertEqual(manager.allocate(set()), "ns-0")
        self.assertEqual(manager.allocate({"ns-0"}), "ns-1")
        self.assertIsNone(manager.allocate({"ns-0", "ns-1"}))
        with self.assertRaises(ValueError):
            manager.slot_number("ns-00")

    def test_namespace_policy_blocks_spoofing_and_private_destinations(self):
        commands = []
        with patch.object(helper, "run", side_effect=lambda *args: commands.append(args)):
            helper.namespace_rules("dsec-aaaaaaaaaaaa", {"dns":"192.168.0.1"})
        self.assertTrue(any(("!", "-s", "169.254.110.2/32", "-j", "DROP") ==
                            command[-5:] for command in commands))
        self.assertTrue(any(("-d", "192.168.0.0/16", "-j", "DROP") ==
                            command[-4:] for command in commands))
        self.assertTrue(any(("-d", "192.168.0.1", "-p", "udp", "--dport", "53", "-j", "ACCEPT") ==
                            command[-8:] for command in commands))

    def test_namespace_policy_allows_only_configured_proxy_port(self):
        commands = []
        config = {"dns":"192.168.0.1",
                  "egress_proxy":{"ip":"192.168.0.106","port":18888}}
        with patch.object(helper, "run", side_effect=lambda *args: commands.append(args)):
            helper.namespace_rules("dsec-aaaaaaaaaaaa", config)
        allow = ("-d", "192.168.0.106/32", "-p", "tcp",
                 "--dport", "18888", "-j", "ACCEPT")
        self.assertTrue(any(command[-8:] == allow for command in commands))
        proxy_index = next(i for i, command in enumerate(commands)
                           if command[-8:] == allow)
        private_index = next(i for i, command in enumerate(commands)
                             if command[-4:] == ("-d", "192.168.0.0/16", "-j", "DROP"))
        self.assertLess(proxy_index, private_index)


if __name__ == "__main__":
    unittest.main()
