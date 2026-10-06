import json
import os
from pathlib import Path
import tempfile
import unittest

from overlaybd_root_store import OverlayBDRootStore


class FakeDaemon:
    def restack_snapshot(self, dev_id, path):
        assert dev_id == 12
        Path(path).write_bytes(b"sealed")
        return {"status": "restack_snapshot_created"}


class OverlayBDSnapshotConfigTest(unittest.TestCase):
    def test_three_generations_reference_persistent_layers_without_copying(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_layer = root / "base.commit"
            base_layer.write_bytes(b"base")
            source = root / "base-image.json"
            source.write_text(json.dumps({"lowers": [{"file": str(base_layer)}],
                                          "upper": {}}))
            sandbox = root / "sandbox"
            sandbox.mkdir()
            store = OverlayBDRootStore.__new__(OverlayBDRootStore)
            store.client = FakeDaemon()
            store.kvm_gid = os.getgid()
            current = source
            first_layer = None
            for generation in range(1, 4):
                staging = sandbox / f"pending-{generation}"
                staging.mkdir()
                target = sandbox / f"snapshot-{generation}"
                new_layer = store.snapshot(12, current, staging, target)
                self.assertEqual(new_layer.parent, sandbox / "disk-layers")
                self.assertEqual(new_layer.read_bytes(), b"sealed")
                layers = store.disk_layers(staging / "disk-image.json", sandbox, source)
                self.assertEqual(len(layers), generation)
                self.assertEqual(layers[-1], new_layer)
                self.assertEqual(len(list((sandbox / "disk-layers").iterdir())), generation)
                if first_layer is None:
                    first_layer = new_layer
                    first_inode = first_layer.stat().st_ino
                else:
                    self.assertEqual(first_layer.stat().st_ino, first_inode)
                staging.rename(target)
                if generation > 1:
                    previous = sandbox / f"snapshot-{generation - 1}"
                    for path in previous.iterdir():
                        path.unlink()
                    previous.rmdir()
                current = target / "disk-image.json"
            self.assertEqual(first_layer.read_bytes(), b"sealed")
            orphan = sandbox / "disk-layers" / ("layer-" + "f" * 32 + ".commit")
            orphan.write_bytes(b"orphan")
            removed = store.prune_unreferenced_layers(current, sandbox, source)
            self.assertEqual(removed, [orphan.name])
            self.assertFalse(orphan.exists())
            self.assertEqual(first_layer.read_bytes(), b"sealed")


if __name__ == "__main__":
    unittest.main()
