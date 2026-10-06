"""Reject bad storage service permissions before allocating a root-owned device."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from overlaybd_root_store import OverlayBDRootStore


class OverlayBDDaemonPermissionsTest(unittest.TestCase):
    def test_bad_umask_refuses_create_before_rpc_and_runtime_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root/'source.json';source.write_text('{}')
            store = OverlayBDRootStore.__new__(OverlayBDRootStore)
            store.client = Mock()
            store.socket_identity = Mock(return_value=[1,2,os.getpid(),'tick','boot'])
            with patch.object(Path,'read_text',return_value='Name:\tublk\nUmask:\t0027\n'):
                with self.assertRaisesRegex(PermissionError,'UMask=0007'):
                    store.create(source,root,root/'disk')
            store.client.create_runtime_device.assert_not_called()
            self.assertEqual(list(root.glob('ublk-runtime-*')),[])

    def test_missing_umask_is_not_treated_as_permissive(self):
        with patch.object(Path,'read_text',return_value='Name:\tublk\n'):
            with self.assertRaisesRegex(RuntimeError,'creation refused'):
                OverlayBDRootStore.require_daemon_group_access(os.getpid())

    def test_service_umask_preserves_storage_group_access(self):
        with patch.object(Path,'read_text',return_value='Umask:\t0007\n'):
            OverlayBDRootStore.require_daemon_group_access(os.getpid())


if __name__ == '__main__':
    unittest.main()
