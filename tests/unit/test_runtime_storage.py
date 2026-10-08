"""Private disks, pinned sources and block-handle failures without KVM/ublk."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from dsec.contracts.errors import SandboxError
from dsec.contracts.storage import DiskPaths
from dsec.runtime.lifecycle import SandboxManager
from dsec.storage.catalog import MicroVMEnvironmentCatalog
from dsec.storage.service import RuntimeStorage
from dsec.storage.snapshots import _snapshot_hash


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RuntimeStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.copies = []
        def copy_sparse(source, destination):
            self.copies.append((source,destination))
            shutil.copyfile(source,destination)
        self.storage = RuntimeStorage(copy_sparse=copy_sparse,
                                      snapshot_hash=_snapshot_hash,hash_file=sha)
        self.private = self.root/'private'; self.private.mkdir()
        self.paths = DiskPaths(self.private,self.private/'rootfs.ext4',self.private/'work.ext4')
        self.source = self.root/'source.ext4'; self.source.write_bytes(b'base content')

    def snapshot(self, *, overlay=False):
        path=self.private/'snapshot-1'; path.mkdir()
        names=('memory','state','disk-image.json') if overlay else ('memory','state','disk.ext4','work.ext4')
        for name in names: (path/name).write_bytes(name.encode())
        manifest={'rootfs_block_backend':'overlaybd-ublk' if overlay else 'file-ext4',
                  'files':{name:sha(path/name) for name in names}}
        return path,manifest

    def catalog(self, *, dax=False, remote=False):
        kernel=self.root/'kernel'; kernel.write_bytes(b'kernel')
        layer=self.root/'tools.erofs'; layer.write_bytes(b'x'*(2*1024*1024) if dax else b'tools')
        entry={'backend':'microvm','rootfs':'erofs_layers','boot_template':str(self.source),
               'boot_sha256':sha(self.source),'kernel':str(kernel),'kernel_sha256':sha(kernel),
               'layers':[{'name':'tools','file':str(layer),'sha256':sha(layer)}]}
        if dax:
            vmm=self.root/'dax-vmm'; vmm.write_bytes(b'pinned vmm')
            entry.update(dax_binary=str(vmm),dax_binary_sha256=sha(vmm))
            entry['layers'][0].update(dax=True,bytes=layer.stat().st_size)
        if remote:
            mount=self.root/'threefs'; mount.mkdir()
            remote_path=mount/(sha(layer)+'.erofs')
            entry['layers'][0].update(threefs_mount=str(mount),threefs_file=str(remote_path),
                                      bytes=layer.stat().st_size)
        path=self.root/'catalog.json'
        path.write_text(json.dumps({'format':1,'environments':{'general-tools':entry}}))
        return MicroVMEnvironmentCatalog(path),entry

    def test_prepare_keeps_pinned_local_dax_metadata_and_rejects_changed_source(self):
        catalog,entry=self.catalog(dax=True)
        resolved=self.storage.prepare(catalog,'general-tools','local')
        self.assertEqual(resolved['erofs_dax_indices'],(0,))
        self.assertEqual(resolved['dax_binary'],Path(entry['dax_binary']))
        self.assertEqual(resolved['layers'][0]['file'],Path(entry['layers'][0]['file']))
        self.assertEqual(self.copies,[])
        Path(entry['dax_binary']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'DAX Firecracker binary changed'):
            self.storage.prepare(catalog,'general-tools','local')

    def test_prepare_threefs_keeps_remote_layer_without_reading_or_copying_payload(self):
        catalog,entry=self.catalog(remote=True)
        layer=entry['layers'][0]; remote=Path(layer['threefs_file'])
        # The catalog's Linux mount/stat probe has a substitute; the remote
        # payload is deliberately absent and must not be opened or copied.
        with patch('dsec.storage.catalog.resolve_threefs_file',return_value=(remote.parent,remote)) as probe:
            resolved=self.storage.prepare(catalog,'general-tools','threefs_lazy')
        self.assertEqual(resolved['layers'][0]['source'],'threefs_lazy')
        self.assertEqual(resolved['layers'][0]['file'],remote)
        probe.assert_called_once_with(layer['threefs_mount'],layer['threefs_file'],layer['bytes'])
        self.assertFalse(remote.exists()); self.assertEqual(self.copies,[])

    def test_two_writable_disks_keep_source_and_sibling_independent(self):
        other=self.root/'other'; other.mkdir()
        sibling=DiskPaths(other,other/'rootfs.ext4')
        for paths in (self.paths,sibling): self.storage.create_writable(paths,self.source,sparse=True)
        self.paths.root.write_bytes(b'private mutation')
        self.assertEqual(sibling.root.read_bytes(),b'base content')
        self.assertEqual(self.source.read_bytes(),b'base content')
        self.assertEqual(self.paths.root.stat().st_mode & 0o777,0o600)

    def test_file_checkpoint_and_restore_keep_private_work_and_integrity(self):
        self.storage.create_writable(self.paths,self.source,sparse=True)
        self.paths.work.write_bytes(b'private work')
        staging=self.private/'pending-test'; staging.mkdir()
        latest=self.storage.checkpoint_disk(self.paths,staging,self.private/'snapshot-1',sparse=True)
        self.assertIsNone(latest)
        for name in ('memory','state'): (staging/name).write_bytes(name.encode())
        manifest={'files':{name:sha(staging/name) for name in ('memory','state','disk.ext4','work.ext4')}}
        self.storage.verify_checkpoint(self.paths,staging,manifest)
        self.paths.root.write_bytes(b'changed after checkpoint')
        self.storage.restore_disk(self.paths,staging,sparse=True)
        self.storage.copy_file(staging/'work.ext4',self.paths.work,mode=0o600)
        self.assertEqual(self.paths.root.read_bytes(),b'base content')
        self.assertEqual(self.paths.work.read_bytes(),b'private work')
        (staging/'work.ext4').write_bytes(b'tampered')
        with self.assertRaisesRegex(SandboxError,'work.ext4'):
            self.storage.verify_checkpoint(self.paths,staging,manifest)

    def test_checkpoint_backend_mismatch_rejected_before_payload_hashing(self):
        snapshot,manifest=self.snapshot()
        manifest['rootfs_block_backend']='overlaybd-ublk'
        with patch.object(self.storage,'snapshot_hash') as hash_file:
            with self.assertRaisesRegex(SandboxError,'block backend mismatch'):
                self.storage.verify_checkpoint(self.paths,snapshot,manifest)
            hash_file.assert_not_called()

    def test_overlaybd_checkpoint_restack_runs_once_and_returns_actual_layer(self):
        staging=self.private/'pending-test'; staging.mkdir()
        layer=self.private/'layer.commit'; layer.write_bytes(b'immutable layer')
        store=SimpleNamespace(snapshot=Mock(return_value=layer))
        latest=self.storage.checkpoint_disk(DiskPaths(self.private,self.paths.root),staging,
            self.private/'snapshot-1',store=store,image=self.source,device_id=7)
        self.assertEqual(latest,layer)
        store.snapshot.assert_called_once_with(7,self.source,staging,self.private/'snapshot-1')
        store.snapshot.side_effect=OSError('reply lost after restack')
        store.snapshot.reset_mock()
        with self.assertRaises(OSError):
            self.storage.checkpoint_disk(DiskPaths(self.private,self.paths.root),staging,
                self.private/'snapshot-1',store=store,image=self.source,device_id=7)
        store.snapshot.assert_called_once()

    def test_overlaybd_checkpoint_layer_set_and_content_are_both_pinned(self):
        snapshot,manifest=self.snapshot(overlay=True)
        layer=self.private/'layer.commit'; layer.write_bytes(b'immutable')
        manifest['disk_layers']={layer.name:sha(layer)}
        store=SimpleNamespace(disk_layers=Mock(return_value=[layer]))
        paths=DiskPaths(self.private,self.paths.root)
        self.storage.verify_checkpoint(paths,snapshot,manifest,store=store,source_image=self.source)
        layer.write_bytes(b'changed')
        with self.assertRaisesRegex(SandboxError,'layer integrity mismatch'):
            self.storage.verify_checkpoint(paths,snapshot,manifest,store=store,source_image=self.source)
        store.disk_layers.return_value=[layer,layer]
        with self.assertRaisesRegex(SandboxError,'layer list mismatch'):
            self.storage.verify_checkpoint(paths,snapshot,manifest,store=store,source_image=self.source)

    def test_device_acquisition_returns_handle_before_fallible_identity_query(self):
        store=SimpleNamespace(create=Mock(return_value=(7,self.private/'ublk-runtime')),
                              socket_identity=Mock(side_effect=OSError('identity unavailable')))
        handle=self.storage.create_writable(self.paths,None,store=store,image=self.source)
        self.assertEqual(handle,(7,self.private/'ublk-runtime'))
        store.socket_identity.assert_not_called()
        with self.assertRaises(OSError): self.storage.device_identity(store)
        self.assertEqual(handle[0],7)

    def test_release_does_not_delete_reused_device_after_daemon_change(self):
        store=SimpleNamespace(socket_identity=Mock(return_value=['new daemon']),delete=Mock())
        original_exists=Path.exists
        def exists(path):
            return True if str(path) in ('/sys/block/ublkb7','/dev/ublkb7') else original_exists(path)
        with patch.object(Path,'exists',exists):
            with self.assertRaisesRegex(SandboxError,'after daemon change'):
                self.storage.release_device(store,7,self.private/'runtime',self.private,['old daemon'])
        store.delete.assert_not_called()

    def test_absent_device_cleans_private_runtime_without_deleting_numeric_id(self):
        store=SimpleNamespace(socket_identity=Mock(),delete=Mock())
        original_exists=Path.exists
        def exists(path):
            return False if str(path) in ('/sys/block/ublkb7','/dev/ublkb7') else original_exists(path)
        with patch.object(Path,'exists',exists):
            self.storage.release_device(store,7,self.private/'runtime',self.private,['old daemon'])
        store.delete.assert_called_once_with(None,self.private/'runtime',self.private)
        store.socket_identity.assert_not_called()

    def test_identity_failure_after_create_is_cleaned_through_real_edge_state(self):
        store=SimpleNamespace(source_images={'general-tools':self.source},
            share_directory=Mock(),configure_shared_layers=Mock(),source_for=lambda _:self.source,
            socket_identity=Mock(side_effect=OSError('identity unavailable')),delete=Mock(),
            shared_layers=SimpleNamespace(release=Mock()))
        def create(image,directory,stable):
            runtime=directory/'ublk-runtime-123456789abc'; runtime.mkdir()
            stable.symlink_to('/dev/ublkb7654321')
            return 7654321,runtime
        def delete(dev_id,runtime,directory):
            shutil.rmtree(runtime)
        store.create=Mock(side_effect=create); store.delete.side_effect=delete
        manager=SandboxManager(self.root/'edge',self.root/'vmm',self.root/'kernel',self.source,
                               tb2_templates={'general-tools':self.source},overlaybd_root_store=store,
                               start_monitor=False)
        with patch('dsec.runtime.backends.firecracker.MicroVM.boot') as boot:
            with self.assertRaisesRegex(OSError,'identity unavailable'):
                manager.create(environment_id='general-tools')
            boot.assert_not_called()
        sandbox=next(iter(manager.sandboxes.values()))
        self.assertEqual(sandbox.state,'STOPPED')
        self.assertTrue(sandbox.resource_cleanup_complete)
        self.assertIsNone(sandbox.overlaybd_device_id)
        self.assertFalse(sandbox.disk.is_symlink())
        self.assertEqual(list(sandbox.directory.glob('ublk-runtime-*')),[])
        store.delete.assert_called_once()
        self.assertIsNone(store.delete.call_args.args[0])
        manager.close()

    def test_private_release_keeps_source_and_sibling_files(self):
        self.paths.root.write_bytes(b'private'); self.paths.work.write_bytes(b'work')
        sibling=self.root/'sibling'; sibling.write_bytes(b'live sibling')
        self.storage.release_writable(self.paths)
        self.storage.release_writable(self.paths)
        self.assertFalse(self.paths.root.exists() or self.paths.work.exists())
        self.assertEqual(self.source.read_bytes(),b'base content')
        self.assertEqual(sibling.read_bytes(),b'live sibling')

    def test_capabilities_do_not_claim_pack_diff_or_incremental_prepared_fork(self):
        self.assertEqual(self.storage.capabilities().root_backend,'file-ext4')
        self.assertEqual(self.storage.capabilities(SimpleNamespace()).root_backend,'overlaybd-ublk')
        self.assertFalse(self.storage.capabilities().publish_diff)
        self.assertFalse(self.storage.capabilities(snapshot_mode='incremental').prepared_fork)


if __name__ == '__main__':
    unittest.main()
