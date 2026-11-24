"""Read-only pmem identity, enforced DAX and writable-isolation regressions."""
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from dsec.host.qemu_profile_helper import render_profile
from dsec.runtime.backends.qemu import QemuArtifact, QemuConfig, QemuReadOnlyLayer, QemuVM
from dsec.runtime.backends.qemu_container import ExecutionCatalog, QemuContainerBackend
from dsec.runtime.backends.docker_guest import Supervisor
from dsec.runtime.isolation import guest_layers
from dsec.storage.erofs import build_layer, guest_manifest
from tests.unit.test_qemu_host_isolation import settings
from tests.unit.test_qemu_container import Docker


class LayerTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()

    def artifact(self):
        path=self.root/'shared.erofs'
        with path.open('wb') as stream:
            stream.seek(1024);stream.write(b'\xe2\xe1\xf5\xe0');stream.truncate(128*2**20)
        path.chmod(0o444)
        with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        return QemuReadOnlyLayer(str(path),digest,path.stat().st_size)

    def vm(self, layer):
        pins=[]
        for name in ('binary','kernel','template'):
            path=self.root/name;path.write_bytes(name.encode())
            pins.append(QemuArtifact(str(path),hashlib.sha256(name.encode()).hexdigest()))
        directory=self.root/'vm';directory.mkdir(mode=0o700)
        vm=QemuVM(QemuConfig(*pins,guest_cid=20000,readonly_layers=(layer,)),directory)
        self.addCleanup(vm.detach);return vm

    def test_qemu_maps_original_inode_as_shared_readonly_rom_not_private_copy(self):
        layer=self.artifact();vm=self.vm(layer)
        args=vm._argv('fixture',vm._artifacts())
        obj=json.loads(args[args.index('-object')+1])
        self.assertEqual(obj['mem-path'],layer.path)
        for flag in ('share','readonly'):self.assertIs(obj[flag],True)
        for flag in ('prealloc','discard-data'):self.assertIs(obj[flag],False)
        self.assertEqual(obj['rom'],'on')
        self.assertEqual(obj['size'],layer.bytes)
        self.assertIn('virtio-pmem-pci,id=dseclayer0,memdev=dsecpmem0',args)
        self.assertEqual(vm._artifacts()['readonly_layers'],[asdict(layer)])
        self.assertIn('maxmem=',args[args.index('-m')+1])
        self.assertFalse((vm.directory/'shared.erofs').exists())

    def test_layer_permissions_digest_capacity_and_path_fail_closed(self):
        layer=self.artifact();self.assertEqual(layer.verify(),Path(layer.path))
        Path(layer.path).chmod(0o644)
        with self.assertRaisesRegex(ValueError,'write bits'):layer.verify()
        Path(layer.path).chmod(0o444)
        with self.assertRaisesRegex(ValueError,'identity'):replace(layer,sha256='b'*64).verify()
        with self.assertRaises(ValueError):replace(layer,bytes=129).validate()
        for value in ('/tmp/bad,share=off','/tmp/a/../b','/tmp/a\n','relative'):
            with self.assertRaises(ValueError):replace(layer,path=value).validate()
        alias=self.root/'alias';alias.symlink_to(layer.path)
        with self.assertRaises(ValueError):replace(layer,path=str(alias)).verify()

    def test_layer_change_cannot_be_adopted_as_old_instance(self):
        layer=self.artifact();vm=self.vm(layer)
        record=vm._initial_record(vm._artifacts());record.update(state='STOPPED',process=None)
        record['artifacts']['readonly_layers'][0]['sha256']='b'*64
        vm.record_path.write_text(json.dumps(record))
        vm._attach=Mock(side_effect=AssertionError('must not attach'))
        with self.assertRaisesRegex(ValueError,'configuration identity'):vm.adopt()
        vm._attach.assert_not_called()

    def test_guest_metadata_and_pci_counts_are_bounded(self):
        layer=self.artifact();vm=self.vm(layer)
        with self.assertRaisesRegex(ValueError,'eight'):replace(vm.config,readonly_layers=(layer,)*9).validate()
        with self.assertRaisesRegex(ValueError,'Duplicate'):replace(vm.config,readonly_layers=(layer,)*2).validate()
        large=replace(layer,bytes=32*2**30)
        with self.assertRaisesRegex(ValueError,'metadata'):replace(vm.config,memory_mb=512,readonly_layers=(large,)).validate()

    def test_apparmor_grants_only_exact_read_mmap_and_never_shared_writes(self):
        layer=self.artifact();cfg=dict(settings(),layer_root=str(self.root))
        text=render_profile(cfg,'a'*32,[asdict(layer)])
        rule=f'"{layer.path}" mr,'
        self.assertIn(rule,text);self.assertNotIn(f'"{layer.path}" rw',text)
        self.assertNotIn('shared.erofs/**',text)
        with self.assertRaises(ValueError):render_profile(cfg,'a'*32,[asdict(layer)]*2)
        with self.assertRaises(ValueError):render_profile(cfg,'a'*32,[dict(asdict(layer),path=cfg['runtime_root']+'/other')])

    def test_publication_uses_uncompressed_noninline_data_and_preserves_uid_gid(self):
        source=self.root/'tree';source.mkdir();(source/'tool').write_bytes(b'a'*8192)
        (source/'link').symlink_to('tool');output=self.root/'published.erofs';seen=[]
        def mkfs(argv,**kwargs):
            seen.append(argv)
            with tarfile.open(argv[-1]) as stream:
                tool=stream.getmember('./tool');self.assertEqual((tool.uid,tool.gid),(os.getuid(),os.getgid()))
                self.assertTrue(stream.getmember('./link').issym())
                self.assertEqual(stream.extractfile(guest_layers.PROBE).read(),guest_layers.PROBE_DATA)
            path=Path(argv[-2])
            with path.open('wb') as stream:stream.seek(1024);stream.write(b'\xe2\xe1\xf5\xe0')
        with patch('dsec.storage.erofs.subprocess.run',side_effect=mkfs):result=build_layer(source,output)
        self.assertIn('noinline_data',seen[0]);self.assertIn('--tar=f',seen[0])
        self.assertFalse(any(arg.startswith('-z') for arg in seen[0]))
        self.assertEqual(result['bytes'],128*2**20);self.assertFalse((source/guest_layers.PROBE).exists())
        self.assertEqual(output.stat().st_mode & 0o777,0o444)
        with self.assertRaises(FileExistsError):build_layer(source,output)

    def test_catalog_ready_requires_dax_proof_and_mount_identity(self):
        layer=self.artifact();vm=self.vm(layer)
        artifact=asdict(vm.config.template)
        item=dict(mode='container',binary=artifact,kernel=artifact,template=artifact,image='sha256:'+'a'*64,
            memory_mb=1024,cpu_count=1,inner_memory_mb=512,inner_cpus=1,
            readonly_layers=[asdict(layer)],shared_mounts=['/opt/shared'])
        value=dict(schema=2,cid_pool=dict(root=str(self.root/'cids'),first=20000,last=20002),environments=dict(shared=item))
        path=self.root/'catalog';path.write_text(json.dumps(value));catalog=ExecutionCatalog(path)
        backend=QemuContainerBackend.__new__(QemuContainerBackend);backend.item=catalog.environments['shared'];backend.network=None
        from dsec.runtime.isolation.guest_auth import FEATURE
        result=dict(mode='container',image=item['image'],network='none',memory_mb=512,cpus=1,transport_auth=FEATURE,
                    readonly_layers=[dict(sha256=layer.sha256,bytes=layer.bytes,dax=True)],shared_mounts=['/opt/shared'])
        control=Mock();control.call.return_value=result
        self.assertEqual(backend._ready(control),result)
        for change in ({'readonly_layers':[]},{'readonly_layers':[dict(result['readonly_layers'][0],dax=False)]},
                       {'shared_mounts':['/wrong']}):
            control.call.return_value=dict(result,**change)
            with self.assertRaisesRegex(ValueError,'identity'):backend._ready(control)


class GuestLayerTests(unittest.TestCase):
    def test_manifest_rejects_mount_overlap_internals_and_injection(self):
        layer=dict(sha256='a'*64,bytes=128*2**20)
        for mounts in ([],['/'],['/proc'],['/var/lib'],['/work'],['/run'],['/opt/../etc'],['/opt/a,b'],
                       ['/opt/a','/opt/a/b'],['/opt/a','/opt/a'],[None]):
            with self.subTest(mounts=mounts),self.assertRaises(ValueError):
                guest_layers.manifest(dict(schema=1,layers=[layer],mounts=mounts))

    def test_dax_mount_option_alone_is_insufficient(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);root=Path(temp.name)
        layer=dict(sha256='a'*64,bytes=128*2**20)
        value=dict(schema=1,layers=[layer],mounts=['/opt/tool'])
        (root/'0').mkdir();(root/'0'/guest_layers.PROBE).write_bytes(guest_layers.PROBE_DATA)
        private=root/'private'
        def entry(target):
            if target==root/'0':return ['/dev/pmem0',str(target),'erofs','ro,dax=always']
            if target==private:return ['/dev/vda',str(target),'ext4','rw']
            return ['overlay',str(target),'overlay',f'rw,lowerdir={root}/0,upperdir={private}/upper,workdir={private}/work']
        with patch.object(guest_layers,'ROOT',root),patch.object(guest_layers,'PRIVATE',private),patch.object(guest_layers,'mount_entry',side_effect=entry):
            with self.assertRaisesRegex(RuntimeError,'active DAX'):guest_layers.attest(value,check_dax=lambda path:False)
            proof=guest_layers.attest(value,check_dax=lambda path:True)
            self.assertTrue(proof['readonly_layers'][0]['dax'])

    def test_shared_mount_is_private_writable_for_container_and_readonly_for_fncall(self):
        for mode in ('container','fncall'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as root:
                docker=Docker();layers=dict(schema=1,layers=[dict(sha256='a'*64,bytes=128*2**20)],mounts=['/opt/tool'])
                proof=dict(readonly_layers=[dict(layers['layers'][0],dax=True)],shared_mounts=layers['mounts'])
                with patch.object(guest_layers,'attest',return_value=proof):
                    supervisor=Supervisor(dict(mode=mode,image='sha256:'+'a'*64,memory_mb=512,cpus=1,layers=layers),root,run=docker)
                supervisor.start();args=docker.calls[0]
                binding='type=bind,src=/var/lib/dsec-layers/merged/opt/tool,dst=/opt/tool'
                self.assertIn(binding+(',readonly' if mode=='fncall' else ''),args)
                self.assertEqual(supervisor.status()['readonly_layers'],proof['readonly_layers'])
                self.assertIn('--cap-drop',args);self.assertIn('ALL',args)
                self.assertFalse(any('pmem' in arg or '/run/dsec-layers' in arg for arg in args))


if __name__=='__main__':unittest.main()
