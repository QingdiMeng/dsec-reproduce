"""Offline split preserves task semantics and never executes task code on host."""
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from dsec_image.split import partition_rootfs, docker_archive, split
from dsec.storage.erofs import build_tar_layer


class SplitTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name).resolve()
        self.archive=self.root/'export.tar'
        with tarfile.open(self.archive,'w') as stream:
            for name,uid in (('.',0),('opt',0),('opt/conda',0),('testbed',1000),('usr',0),('usr/bin',0)):
                info=tarfile.TarInfo(name);info.type=tarfile.DIRTYPE;info.mode=0o755;info.uid=info.gid=uid;stream.addfile(info)
            for name,data,uid in (('opt/conda/python',b'python-bytes',0),('testbed/code.py',b'original task source',1000),('usr/bin/sh',b'base shell',0)):
                info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o755;info.uid=info.gid=uid;info.pax_headers={'SCHILY.xattr.user.fixture':'kept'};stream.addfile(info,io.BytesIO(data))
            link=tarfile.TarInfo('opt/conda/python-link');link.type=tarfile.LNKTYPE;link.linkname='opt/conda/python';stream.addfile(link)
            link=tarfile.TarInfo('testbed/env');link.type=tarfile.SYMTYPE;link.linkname='/opt/conda';stream.addfile(link)
        self.image=dict(Id='sha256:'+'a'*64,Architecture='amd64',Os='linux',Created='2026-10-26T00:00:00Z',Size=1024,
            Config=dict(Env=['HOME=/work','PIP_USER=1'],WorkingDir='/testbed',Entrypoint=['/bin/sh'],Cmd=['-lc','true'],User='1000:1000',Labels={'fixture':'exact'}))

    def partition(self):
        base=self.root/'base.tar';layers=[self.root/'conda.tar',self.root/'repo.tar']
        result=partition_rootfs(self.archive,['/opt/conda','/testbed'],base,layers)
        return base,layers,result

    def test_selected_contents_removed_but_mount_roots_and_permissions_remain(self):
        base,layers,result=self.partition()
        with tarfile.open(base) as stream:
            names=stream.getnames();self.assertIn('opt/conda',names);self.assertIn('testbed',names)
            self.assertNotIn('opt/conda/python',names);self.assertNotIn('testbed/code.py',names)
            self.assertEqual(stream.extractfile('usr/bin/sh').read(),b'base shell')
        with tarfile.open(layers[1]) as stream:
            self.assertEqual(stream.extractfile('testbed/code.py').read(),b'original task source')
            self.assertEqual(stream.getmember('testbed').uid,1000)
            self.assertEqual(stream.getmember('testbed/code.py').gid,1000)
            self.assertEqual(stream.getmember('testbed/code.py').pax_headers['SCHILY.xattr.user.fixture'],'kept')
            self.assertEqual(stream.getmember('testbed/env').linkname,'/opt/conda')
        with tarfile.open(layers[0]) as stream:
            self.assertTrue(stream.getmember('opt/conda/python-link').islnk())
            self.assertEqual(stream.extractfile('opt/conda/python-link').read(),b'python-bytes')
            self.assertNotIn('testbed/code.py',stream.getnames())
        self.assertEqual(result['retained_file_bytes'],len(b'base shell'))

    def test_cross_boundary_hardlink_rejected_instead_of_silently_changed(self):
        with tarfile.open(self.archive,'a') as stream:
            link=tarfile.TarInfo('usr/bin/alias');link.type=tarfile.LNKTYPE;link.linkname='opt/conda/python';stream.addfile(link)
        with self.assertRaisesRegex(ValueError,'Hardlink crosses'):self.partition()

    def test_missing_or_link_shared_root_and_overlaps_are_rejected(self):
        for targets in (['/missing'],['/opt/conda','/opt/conda/sub'],['/testbed/env']):
            with self.subTest(targets=targets),self.assertRaises(ValueError):
                partition_rootfs(self.archive,targets,self.root/'base.tar',[self.root/f'part-{i}.tar' for i in range(len(targets))])

    def test_archive_preserves_entire_runtime_config_and_exact_rootfs_digest(self):
        base,_,_=self.partition();output=self.root/'task.tar';original=copy.deepcopy(self.image)
        image=docker_archive(base,original,output)
        with tarfile.open(output) as stream:
            manifest=json.load(stream.extractfile('manifest.json'))[0]
            blob=stream.extractfile(manifest['Config']).read();config=json.loads(blob)
            self.assertEqual(image,'sha256:'+hashlib.sha256(blob).hexdigest())
            self.assertEqual(config['config'],self.image['Config']);self.assertEqual(original,self.image)
            self.assertEqual(config['rootfs']['diff_ids'],['sha256:'+hashlib.sha256(base.read_bytes()).hexdigest()])
            self.assertEqual(stream.extractfile(manifest['Layers'][0]).read(),base.read_bytes())

    def publisher(self,*,fail_export=False,image=None):
        calls=[];image=image or self.image
        def read(argv,**kwargs):
            calls.append(argv)
            return json.dumps([image]) if argv[:3]==['docker','image','inspect'] else 'b'*64
        def run(argv,**kwargs):
            calls.append(argv)
            if argv[:2]==['docker','export']:
                if fail_export:raise subprocess.CalledProcessError(1,argv)
                with self.archive.open('rb') as source:shutil.copyfileobj(source,kwargs['stdout'])
            elif argv[:2]!=['docker','rm']:raise AssertionError(argv)
        def publish(source,target,**kwargs):
            target.write_bytes(b'erofs-fixture');target.chmod(0o444)
            return dict(path=str(target),sha256=hashlib.sha256(target.read_bytes()).hexdigest(),bytes=128*2**20)
        self.enterContext(patch('dsec_image.split.subprocess.check_output',side_effect=read))
        self.enterContext(patch('dsec_image.split.subprocess.run',side_effect=run))
        self.enterContext(patch('dsec_image.split.build_tar_layer',side_effect=publish))
        pool=self.root/'pool';pool.mkdir()
        return pool,calls

    def test_full_publication_uses_stopped_export_only_and_reports_lean_identity(self):
        pool,calls=self.publisher();out=self.root/'published'
        result=split(self.image['Id'],out,pool,['/opt/conda','/testbed'],reserve_gib=1)
        self.assertEqual(result['status'],'passed');self.assertEqual(len(result['readonly_layers']),2)
        self.assertEqual(result['shared_mounts'],['/opt/conda','/testbed'])
        self.assertEqual(result['task_archive_sha256'],hashlib.sha256((out/'task-lean.tar').read_bytes()).hexdigest())
        self.assertTrue(any(argv[:2]==['docker','rm'] for argv in calls))
        self.assertFalse(any(argv[:2] in (['docker','run'],['docker','start'],['docker','exec'],['docker','load']) for argv in calls))
        self.assertEqual(json.loads((out/'split.json').read_bytes())['status'],'passed')

    def test_export_failure_removes_own_stopped_container_and_retains_failure(self):
        pool,calls=self.publisher(fail_export=True);out=self.root/'failed'
        with self.assertRaises(subprocess.CalledProcessError):split(self.image['Id'],out,pool,['/testbed'],reserve_gib=1)
        self.assertTrue(any(argv[:2]==['docker','rm'] for argv in calls))
        self.assertEqual(json.loads((out/'split.json').read_bytes())['status'],'failed')

    def test_guest_builder_rejects_wrong_archive_pin_before_host_effects(self):
        import importlib.util,sys
        tools=Path(__file__).resolve().parents[2]/'tools'
        with patch.object(sys,'path',[str(tools),*sys.path]):
            spec=importlib.util.spec_from_file_location('build_guest_fixture',tools/'build_docker_guest.py')
            builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
        with patch.object(builder,'export_image',side_effect=AssertionError('must not export')):
            with self.assertRaisesRegex(ValueError,'archive identity'):
                builder.build('unused','unused',None,self.root,self.root/'guest.ext4',mode='container',
                    task_archive=self.archive,task_archive_sha256='b'*64)
            with self.assertRaisesRegex(ValueError,'Select one'):
                builder.build('unused','unused','sha256:'+'a'*64,self.root,self.root/'guest.ext4',mode='container',
                    task_archive=self.archive,task_archive_sha256='b'*64)

    def test_declared_volumes_fail_before_creating_host_container(self):
        image=copy.deepcopy(self.image);image['Config']['Volumes']={'/testbed':{}}
        pool,calls=self.publisher(image=image)
        with self.assertRaisesRegex(ValueError,'volumes'):split(image['Id'],self.root/'bad',pool,['/testbed'],reserve_gib=1)
        self.assertFalse(any(argv[:2]==['docker','create'] for argv in calls))


if __name__=='__main__':unittest.main()
