"""Shared prepared layers survive owner deletion and restart reconciliation."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from overlaybd_root_store import OverlayBDRootStore
from shared_snapshot_layers import SharedSnapshotLayers


class SharedLayersTests(unittest.TestCase):
    def test_two_owners_one_object_and_last_owner_gc(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root/'layer'
            source.write_bytes(b'prepared data')
            store = SharedSnapshotLayers(root, lambda *_a, **_kw: None)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            obj = store.publish_and_hold('a'*12, source, digest)
            inode = obj.stat().st_ino
            self.assertEqual(obj, store.publish_and_hold('b'*12, source, digest))
            self.assertEqual(obj.stat().st_ino, inode)
            store.release('a'*12)
            self.assertEqual(obj.read_bytes(), b'prepared data')
            # Simulate startup with one live/failed child and one abandoned hold.
            store.hold('c'*12, [obj])
            restored = SharedSnapshotLayers(root, lambda *_a, **_kw: None)
            restored.reconcile({'b'*12})
            self.assertTrue(obj.exists())
            self.assertFalse((restored.references/('c'*12+'.json')).exists())
            restored.release('b'*12)
            self.assertFalse(obj.exists())

    def test_bad_digest_and_symlink_rejected_without_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root/'layer'
            source.write_bytes(b'data')
            store = SharedSnapshotLayers(root, lambda *_a, **_kw: None)
            with self.assertRaisesRegex(ValueError, 'changed during'):
                store.publish_and_hold('a'*12, source, '0'*64)
            self.assertEqual(list(store.objects.iterdir()), [])
            link = store.objects/('0'*64+'.commit')
            link.symlink_to(source)
            with self.assertRaisesRegex(ValueError, 'Invalid shared'):
                store.hold('b'*12, [link])
            with self.assertRaises(ValueError):
                store.hold('../escape', [])

    def test_private_pruning_never_removes_shared_backing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root/'layer'
            source.write_bytes(b'data')
            shared = SharedSnapshotLayers(root, lambda *_a, **_kw: None)
            obj = shared.publish_and_hold('a'*12, source,
                                          hashlib.sha256(b'data').hexdigest())
            sb = root/'sandbox'
            (sb/'disk-layers').mkdir(parents=True)
            unused = sb/'disk-layers'/('layer-'+'1'*32+'.commit')
            unused.write_bytes(b'unused')
            base = root/'base.json'
            base.write_text(json.dumps({'lowers':[{'file':str(source)}]}))
            image = sb/'image.json'
            image.write_text(json.dumps({'lowers':[{'file':str(source)},
                            {'file':str(obj)}, {'file':str(obj)}]}))
            store = OverlayBDRootStore.__new__(OverlayBDRootStore)
            store.shared_layers = shared
            self.assertEqual(store.disk_layers(image, sb, base), [obj])
            self.assertEqual(store.prune_unreferenced_layers(image, sb, base), [unused.name])
            self.assertTrue(obj.exists())
