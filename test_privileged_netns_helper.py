"""No-root checks for the installed helper's slot and executable authority."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import privileged_netns_helper as helper


class PrivilegedHelperTests(unittest.TestCase):
    def test_private_mount_context_is_rejected_before_network_mutations(self):
        def namespace(path):
            return 'private' if path == '/proc/self/ns/mnt' else 'host'
        with patch.object(helper.os, 'readlink', side_effect=namespace):
            with self.assertRaisesRegex(RuntimeError, 'host mnt'):
                helper.checked_host_context()

    def test_host_context_is_accepted(self):
        with patch.object(helper.os, 'readlink', return_value='host'):
            helper.checked_host_context()

    def test_offset_slots_are_bounded_and_keep_logical_identity(self):
        with patch.object(helper, 'SLOT_OFFSET', 4096), patch.object(helper, 'MAX_SLOTS', 2):
            spec = helper.names('abcdef123456', 1)
            self.assertEqual(spec['slot'], 1)
            self.assertEqual(spec['host_cidr'], '10.231.32.2/31')
            with self.assertRaises(ValueError):
                helper.names('abcdef123456', 2)

    def test_ordinary_and_dax_builds_require_separate_pins(self):
        with tempfile.TemporaryDirectory() as directory:
            ordinary, dax = Path(directory)/'ordinary', Path(directory)/'dax'
            ordinary.write_bytes(b'ordinary')
            dax.write_bytes(b'dax')
            cfg = {'firecracker':str(ordinary), 'firecracker_sha256':hashlib.sha256(b'ordinary').hexdigest(),
                   'dax_firecracker':str(dax), 'dax_firecracker_sha256':hashlib.sha256(b'dax').hexdigest()}
            self.assertEqual(helper.checked_binary(cfg), ordinary.resolve())
            self.assertEqual(helper.checked_binary(cfg, dax=True), dax.resolve())
            ordinary.write_bytes(b'replaced')
            with self.assertRaisesRegex(RuntimeError, 'administrator pin'):
                helper.checked_binary(cfg)
            with self.assertRaises(ValueError):
                helper.checked_binary({'firecracker':str(ordinary)}, dax=True)


if __name__ == '__main__':
    unittest.main()
