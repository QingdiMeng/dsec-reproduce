"""Mixed root backends retain the launcher's explicit directory authority."""
from pathlib import Path
import stat
import sys
import tempfile
import unittest

from sandbox_sdk import Sandbox, SandboxManager


@unittest.skipUnless(sys.platform == 'linux', 'Linux setgid directory semantics')
class SandboxDirectoryTests(unittest.TestCase):
    def test_private_guest_under_setgid_storage_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager = SandboxManager(root/'sessions', root/'fc', root/'kernel',
                                     root/'template', start_monitor=False)
            manager.root.chmod(0o2710)
            sandbox = Sandbox(manager, 30)
            self.assertEqual(stat.S_IMODE(sandbox.directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(manager.root.stat().st_mode), 0o2710)

    def test_overlaybd_guest_retains_explicit_group_access(self):
        class Store:
            source_images = {'shared':None}

            def share_directory(self, path):
                path.chmod(0o2770)

            def source_for(self, environment_id):
                return Path('/prepared/root-image.json')

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager = SandboxManager(root/'sessions', root/'fc', root/'kernel',
                                     root/'template', start_monitor=False)
            manager.root.chmod(0o2710)
            manager.overlaybd_root_store = Store()
            sandbox = Sandbox(manager, 30, environment_id='shared')
            self.assertEqual(stat.S_IMODE(sandbox.directory.stat().st_mode), 0o2770)
            self.assertIs(sandbox.overlaybd_store, manager.overlaybd_root_store)


if __name__ == '__main__':
    unittest.main()
