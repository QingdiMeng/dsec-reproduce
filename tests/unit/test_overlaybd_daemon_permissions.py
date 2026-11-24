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

    def test_lost_create_reply_preserves_daemon_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "image.json"
            image.write_text("{}")
            store = OverlayBDRootStore.__new__(OverlayBDRootStore)
            store.socket_identity = Mock(return_value=[1, 2, 123, "tick", "boot"])
            store.require_daemon_group_access = Mock()
            store.client = Mock()

            def lost_reply(source, config, runtime):
                runtime.mkdir()
                (runtime / "upper").write_bytes(b"live")
                raise EOFError("lost reply")

            store.global_config = root / "global.json"
            store.client.create_runtime_device.side_effect = lost_reply
            with self.assertRaises(EOFError):
                store.create(image, root, root / "disk")
            self.assertEqual([p.read_bytes() for p in root.glob("ublk-runtime-*/upper")], [b"live"])
            store.client.delete.assert_not_called()

    def test_missing_umask_is_not_treated_as_permissive(self):
        with patch.object(Path,'read_text',return_value='Name:\tublk\n'):
            with self.assertRaisesRegex(RuntimeError,'creation refused'):
                OverlayBDRootStore.require_daemon_group_access(os.getpid())

    def test_service_umask_preserves_storage_group_access(self):
        with patch.object(Path,'read_text',return_value='Umask:\t0007\n'):
            OverlayBDRootStore.require_daemon_group_access(os.getpid())


if __name__ == '__main__':
    unittest.main()
