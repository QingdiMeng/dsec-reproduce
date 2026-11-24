"""Controlled launch/retirement fences; Linux kernel acceptance is separate."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from dsec.host import qemu_profile_helper as broker
from dsec.runtime.isolation.qemu_host import HostIsolation,configuration
from dsec.runtime.backends.qemu_container import ExecutionCatalog,QemuContainerBackend
from tests.unit import test_qemu_backend as qemu_tests


def settings():
    return dict(uid=1000,prefix='dsec-unit',runtime_root='/srv/dsec/containers',
        binary='/usr/bin/qemu-system-x86_64',kernel='/srv/dsec/kernel',python='/usr/bin/python3',
        launcher='/srv/dsec/qemu_launch.py',pins={k:'a'*64 for k in ('binary','kernel','python','launcher')})


class BrokerTests(unittest.TestCase):
    sid='a'*32

    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve();self.enterContext(patch.object(broker,'STATE',self.root))
        self.cfg=settings();self.loaded=False;self.dead=False
        self.enterContext(patch.object(broker.Path,'read_text',lambda path,*a,**kw:self.read_text(path,*a,**kw)))
        self.run=self.enterContext(patch.object(broker.subprocess,'run',side_effect=self.parser))
        self.current=dict(pid=1234,uid=1000,start_ticks='123',boot_id='test',exe=self.cfg['python'],
            argv=[self.cfg['python'],'-I','-B',self.cfg['launcher'],
                  '/srv/dsec/containers/'+self.sid+'/launch.json','8'])
        self.enterContext(patch.object(broker,'identity',return_value=self.current))
        self.enterContext(patch.object(broker,'prove_dead',side_effect=self.prove_dead))

    original_read_text=staticmethod(Path.read_text)

    def read_text(self,path,*args,**kwargs):
        if str(path)=='/sys/kernel/security/apparmor/profiles':
            return broker.profile_name(self.cfg,self.sid)+' (enforce)\n' if self.loaded else ''
        return self.original_read_text(path,*args,**kwargs)

    def parser(self,args,**kwargs):
        if '-a' in args:self.loaded=True
        elif '-R' in args:self.loaded=False
        else:raise AssertionError(args)

    def prove_dead(self,saved):
        self.assertEqual(saved,self.current)
        if not self.dead:raise RuntimeError('still live')

    def prepare(self):return broker.operate(self.cfg,'prepare',self.sid,[])
    def claim(self):return broker.operate(self.cfg,'claim',self.sid,['1234','123'])
    def retire(self):return broker.operate(self.cfg,'retire',self.sid,[])

    def test_claimed_process_must_die_before_profile_unload(self):
        self.assertEqual(self.prepare()['state'],'PREPARED')
        self.assertEqual(self.claim()['state'],'CLAIMED')
        with self.assertRaisesRegex(RuntimeError,'live'):self.retire()
        self.assertTrue(self.loaded)
        self.assertEqual(json.loads((self.root/(self.sid+'.json')).read_text())['state'],'CLAIMED')
        self.dead=True
        self.assertEqual(self.retire()['state'],'RETIRED');self.assertFalse(self.loaded)

    def test_retirement_fences_a_delayed_claim_before_unload_finishes(self):
        self.prepare();entered=threading.Event();release=threading.Event()
        def parser(args,**kwargs):
            entered.set();self.assertTrue(release.wait(3));self.parser(args,**kwargs)
        self.run.side_effect=parser
        with ThreadPoolExecutor(max_workers=1) as pool:
            retirement=pool.submit(self.retire)
            self.assertTrue(entered.wait(2));self.assertTrue(self.loaded)
            try:
                with self.assertRaisesRegex(RuntimeError,'fenced'):self.claim()
            finally:release.set()
            self.assertEqual(retirement.result(3)['state'],'RETIRED')
        with self.assertRaisesRegex(RuntimeError,'retired'):self.prepare()

    def test_retired_profile_is_not_recreated_after_parser_failure(self):
        self.prepare();self.run.side_effect=RuntimeError('parser unavailable')
        with self.assertRaisesRegex(RuntimeError,'parser'):self.retire()
        self.assertTrue(self.loaded)
        with self.assertRaisesRegex(RuntimeError,'fenced'):self.claim()
        self.run.side_effect=self.parser;self.retire();self.assertFalse(self.loaded)

    def test_foreign_launcher_never_claims_policy(self):
        self.prepare();self.current['argv'][-2]='/srv/foreign/launch.json'
        with self.assertRaisesRegex(ValueError,'identity'):self.claim()
        self.assertEqual(json.loads((self.root/(self.sid+'.json')).read_text())['state'],'PREPARED')

    def test_missing_corrupt_or_changed_ledger_never_unloads(self):
        self.prepare();path=self.root/(self.sid+'.json')
        old=path.read_text();path.unlink()
        with self.assertRaisesRegex(RuntimeError,'missing'):self.retire()
        path.write_text(old);record=json.loads(old);record['state']='unexpected';path.write_text(json.dumps(record))
        with self.assertRaisesRegex(RuntimeError,'Corrupt'):self.retire()
        self.assertTrue(self.loaded)

    def test_shared_layer_grant_is_fixed_at_prepare_and_retirement_needs_no_live_artifact(self):
        pool=self.root/'layers';pool.mkdir();path=pool/'base.erofs'
        with path.open('wb') as stream:stream.truncate(128*2**20)
        path.chmod(0o444);self.cfg['layer_root']=str(pool)
        layer=dict(path=str(path),sha256='a'*64,bytes=128*2**20)
        prepared=broker.operate(self.cfg,'prepare',self.sid,[json.dumps([layer])])
        self.assertEqual(prepared['readonly_layers'],[layer])
        with self.assertRaisesRegex(RuntimeError,'layer identity changed'):
            broker.operate(self.cfg,'prepare',self.sid,[json.dumps([dict(layer,sha256='b'*64)])])
        self.assertEqual(self.claim()['readonly_layers'],[layer])
        path.unlink();self.dead=True
        self.assertEqual(self.retire()['state'],'RETIRED')
        with self.assertRaisesRegex(RuntimeError,'fenced'):self.claim()

    def test_no_layer_policy_remains_identical_and_outside_pool_cannot_be_granted(self):
        old=broker.render_profile(self.cfg,self.sid)
        self.cfg['layer_root']=str(self.root/'layers')
        self.assertEqual(broker.render_profile(self.cfg,self.sid),old)
        layer=dict(path='/etc/private.erofs',sha256='a'*64,bytes=128*2**20)
        with self.assertRaisesRegex(ValueError,'scoped'):
            broker.operate(self.cfg,'prepare',self.sid,[json.dumps([layer])])
        self.run.assert_not_called()
        self.assertFalse((self.root/(self.sid+'.json')).exists())

    def test_uefi_policy_uses_only_approved_boot_files_and_own_writable_state(self):
        pins = {}
        for name in ('code','template','base'):
            path = self.root/name
            path.write_bytes(name.encode())
            path.chmod(0o444)
            pins[name] = dict(path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        self.cfg['uefi_boots'] = [pins]
        changed = dict(pins, base=dict(pins['base'],path='/srv/foreign.qcow2'))
        with self.assertRaisesRegex(ValueError,'not approved'):
            broker.operate(self.cfg,'prepare',self.sid,['[]',json.dumps(changed)])
        prepared = broker.operate(self.cfg,'prepare',self.sid,['[]',json.dumps(pins)])
        self.assertEqual(prepared['uefi'],pins)
        text = broker.render_profile(self.cfg,self.sid,uefi=pins)
        for pin in pins.values():
            self.assertIn(f'"{pin["path"]}" rk,',text)
            self.assertNotIn(f'"{pin["path"]}" rw',text)
        self.assertIn(f'/{self.sid}/root.qcow2" rwk,',text)
        self.assertIn(f'/{self.sid}/uefi-vars.fd" rwk,',text)
        self.assertIn(f'/{self.sid}/display.png" rw,',text)
        self.assertNotIn('/root.ext4',text)
        self.assertEqual(self.claim()['uefi'],pins)
        Path(pins['base']['path']).unlink()
        self.dead=True
        self.assertEqual(self.retire()['state'],'RETIRED')

    def test_uefi_only_broker_can_fence_retirement_before_prepare(self):
        self.cfg.pop('kernel')
        self.cfg['pins'].pop('kernel')
        self.cfg['uefi_boots'] = [dict(code=dict(path='/srv/boot.fd',sha256='a'*64),
                                       template=dict(path='/srv/root.qcow2',sha256='b'*64))]
        self.assertEqual(self.retire()['state'],'RETIRED')
        self.assertEqual(self.retire()['state'],'RETIRED')
        with self.assertRaisesRegex(RuntimeError,'retired'):
            self.claim()

    def test_profile_is_per_instance_and_does_not_grant_exec_or_host_secrets(self):
        text=broker.render_profile(self.cfg,self.sid)
        self.assertIn('/'+self.sid+'/root.ext4',text)
        self.assertNotIn('channel.key',text);self.assertNotIn('launch.json',text)
        self.assertNotIn('docker.sock',text);self.assertNotIn('/sys/fs/cgroup',text)
        self.assertNotIn('capability ',text);self.assertNotIn(' ix,',text)
        self.assertIn('unix (create) type=stream,',text)
        self.assertIn('peer=(label=dsec-unit-'+self.sid+', addr=none)',text)
        self.assertNotIn('network unix',text)
        self.assertNotIn('deny unix',text)
        self.assertNotIn('attach_disconnected',text)
        for sid in ('../bad','a'*31,'a'*32+'\n'):
            with self.assertRaises(ValueError):broker.render_profile(self.cfg,sid)
        bad=dict(self.cfg,kernel='/srv/kernel" r,\n/** rw,')
        with self.assertRaises(ValueError):broker.render_profile(bad,self.sid)


class CgroupTests(unittest.TestCase):
    def fixture(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        host=HostIsolation.__new__(HostIsolation);host.group=Path(temp.name).resolve()/'group';host.group.mkdir()
        host.directory=Path(temp.name).resolve()
        host.record=None;host.created=True;host.broker=Mock(return_value=dict(state='RETIRED'))
        host.limits={'memory.max':'123','memory.swap.max':'0','memory.oom.group':'1','pids.max':'32','cpu.max':'100000 100000'}
        for key,value in host.limits.items():(host.group/key).write_text(value)
        (host.group/'cgroup.events').write_text('populated 0\nfrozen 0\n')
        return host

    def test_policy_rejects_broad_paths_and_unbounded_values(self):
        valid=dict(cgroup_root='/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/dsec.service',helper='/usr/local/libexec/dsec-test-qemu-profile',memory_overhead_mb=256,pids_max=128)
        self.assertEqual(configuration(valid),valid)
        for key,value in (('cgroup_root','/sys/fs/cgroup'),('cgroup_root','/tmp/any.service'),
                ('helper','/bin/sh'),('pids_max',0),('memory_overhead_mb',True)):
            with self.assertRaises(ValueError):configuration(dict(valid,**{key:value}))

    def test_limit_mismatch_fails_closed(self):
        host=self.fixture();host.check_limits()
        (host.group/'cpu.max').write_text('max 100000')
        with self.assertRaisesRegex(RuntimeError,'cpu.max'):host.check_limits()

    def test_live_or_replaced_cgroup_cannot_release_policy(self):
        host=self.fixture();(host.group/'cgroup.events').write_text('populated 1\n')
        with self.assertRaisesRegex(RuntimeError,'populated'):host.retire()
        host.broker.assert_not_called()
        host.record=dict(device=0,inode=0)
        with self.assertRaisesRegex(RuntimeError,'inode'):host.retire()
        host.broker.assert_not_called()

    def test_retirement_saves_oom_and_peak_before_kernel_counters_disappear(self):
        host=self.fixture();(host.group/'memory.peak').write_text('1342177280\n')
        (host.group/'memory.events').write_text('max 9\noom 1\noom_kill 1\n')
        def remove(group):
            evidence=json.loads((host.directory/'host-resources-retired.json').read_text())
            self.assertEqual(evidence['counters']['memory.peak'],'1342177280\n')
            self.assertIn('oom_kill 1',evidence['counters']['memory.events'])
            self.assertNotIn('io.stat',evidence['counters'])
        with patch.object(Path,'rmdir',remove):host.retire()

    def test_attestation_rejects_a_vmm_outside_its_group(self):
        host=self.fixture();host.group=Path('/sys/fs/cgroup/dsec.service/vm-test')
        host.record=dict(profile='dsec-test');host.check_limits=Mock()
        original=Path.read_text
        def read(path,*a,**kw):
            if str(path)=='/proc/123/cgroup':return '0::/other-group\n'
            if str(path)=='/proc/123/attr/current':return 'dsec-test (enforce)\n'
            if str(path)=='/proc/123/status':return 'NoNewPrivs: 1\nSeccomp: 2\nCapEff: 0\nCapPrm: 0\n'
            return original(path,*a,**kw)
        with patch.object(Path,'read_text',read):
            with self.assertRaisesRegex(RuntimeError,'outside'):host.attest(123)

    def test_uncommitted_launcher_never_receives_permission_to_exec(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        host=HostIsolation.__new__(HostIsolation);host.directory=Path(temp.name).resolve()
        host.record={};host.broker=Mock()
        marker=host.directory/'must-not-exist'
        real_popen=subprocess.Popen;commands=[]
        def popen(argv,**kw):commands.append(argv);return real_popen(argv,**kw)
        def identity(pid):return dict(pid=pid,uid=os.getuid(),exe=commands[-1][0],
                                      argv=commands[-1],start_ticks='1')
        def refuse(saved,argv):raise RuntimeError('durable commit failed')
        with (host.directory/'log').open('wb') as log, \
                patch('dsec.runtime.isolation.qemu_host.subprocess.Popen',popen), \
                patch.dict(sys.modules,{'dsec.runtime.registry':SimpleNamespace(identity=identity)}):
            with self.assertRaisesRegex(RuntimeError,'commit'):
                host.launch([sys.executable,'-c','open('+repr(str(marker))+',"w").write("bad")'],log,refuse)
        host.broker.assert_not_called();self.assertFalse(marker.exists())


class DeathProofTests(unittest.TestCase):
    def test_missing_proc_details_are_not_death_when_pidfd_is_live(self):
        saved=dict(pid=123,start_ticks='123',boot_id='test',uid=1000)
        def read(path,*args,**kwargs):
            if str(path)=='/proc/sys/kernel/random/boot_id':return 'test'
            raise FileNotFoundError('proc details disappeared')
        with patch.object(broker.os,'pidfd_open',return_value=42,create=True), \
                patch.object(broker.os,'close'),patch.object(broker.select,'select',return_value=([],[],[])), \
                patch.object(Path,'read_text',read):
            with self.assertRaisesRegex(RuntimeError,'pidfd remains live'):broker.prove_dead(saved)


class DeploymentTests(unittest.TestCase):
    def bundle(self,networked=False):
        from dsec.host import qemu_privileges
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        root=Path(temp.name).resolve();artifact=root/'artifact';artifact.write_bytes(b'trusted fixture')
        pin=dict(path=str(artifact),sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())
        policy=dict(cgroup_root='/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/dsec-test-sandbox.service',
                    helper='/usr/local/libexec/dsec-test-qemu-profile',memory_overhead_mb=256,pids_max=128)
        pool=root/'pool';pool.mkdir(mode=0o700);containers=root/'containers';containers.mkdir(mode=0o700)
        entry=dict(mode='container',binary=pin,kernel=pin,template=pin,image='sha256:'+'b'*64,
                   memory_mb=1024,cpu_count=1,inner_memory_mb=512,inner_cpus=1)
        value=dict(schema=2,cid_pool=dict(root=str(pool),first=20000,last=20001),environments={'test':entry},host_isolation=policy)
        if networked:
            entry['network']='isolated'
            value['network']=dict(helper='/usr/local/libexec/dsec-test-qemu-network',uplink='eth0',slot_offset=400,max_slots=2,
                                  policy=dict(dns='1.1.1.1',egress_proxy=None))
        path=root/'execution.json';path.write_text(json.dumps(value))
        catalog=ExecutionCatalog(path);backend=QemuContainerBackend(catalog,'test',containers)
        self.assertEqual(backend.minimum_demand.memory_mb,1280)
        cfg=dict(instance='test',sandbox=dict(qemu_execution_catalog=str(path)),state_root=str(root/'state'),
                 worker=dict(container_root=str(containers)))
        account=SimpleNamespace(pw_uid=os.getuid(),pw_gid=42)
        def compiler(argv,**kwargs):
            output=Path(argv[-1]);blob=bytearray(64);blob[:4]=b'\x7fELF';blob[18:20]=(247).to_bytes(2,'little');output.write_bytes(blob)
        with patch.object(qemu_privileges.subprocess,'run',side_effect=compiler), \
                patch.object(qemu_privileges.pwd,'getpwnam',return_value=account), \
                patch.object(qemu_privileges.grp,'getgrnam',return_value=SimpleNamespace(gr_gid=42)), \
                patch.object(qemu_privileges.os,'getgrouplist',return_value=[42]):
            result=qemu_privileges.render(cfg,root/'bundle','runner')
        return root,result,policy

    def test_installed_bundle_is_fixed_and_outer_overhead_is_charged(self):
        root,result,policy=self.bundle()
        self.assertEqual(len(result['targets']),3)
        self.assertEqual(result['targets'][0],policy['helper'])
        source=Path(result['install']).read_text();compile(source,'installer','exec')
        self.assertIn("['observe'](network_required=False, administrator=True)",source)
        self.assertIn('report = observe(network_required=False)',source)
        self.assertNotIn('__SPECS__',source)
        settings=json.loads((root/'bundle/helper-config.json').read_text())
        self.assertEqual(settings['uid'],os.getuid());self.assertEqual(settings['runtime_root'],str(root/'containers'))

    def test_network_bundle_pins_object_and_only_grants_own_inherited_device(self):
        root,result,policy=self.bundle(networked=True)
        self.assertEqual(len(result['targets']),6)
        config=json.loads((root/'bundle/network-config.json').read_text())
        self.assertEqual(config['pins']['object'],hashlib.sha256((root/'bundle/egress.o').read_bytes()).hexdigest())
        sample=(root/'bundle/sample.profile').read_text()
        self.assertIn('/var/lib/dsec/dsec-test-qemu-network/taps/'+'a'*32+'/tun',sample)
        self.assertNotIn('/dev/net/tun',sample)
        installer=(root/'bundle/install-qemu-privileges.py').read_text()
        self.assertIn("compile(blobs['network-helper.py']",installer)
        self.assertIn("directory('/var/lib/dsec/dsec-test-qemu-network', mode=0o700)",installer)
        self.assertEqual((root/'bundle/sudoers').read_text().count('NOPASSWD'),2)


class HostBootTests(unittest.TestCase):
    setUp=qemu_tests.QemuTests.setUp
    instrument=qemu_tests.QemuTests.instrument

    def test_boot_verifies_confinement_before_running(self):
        process=self.instrument();process.pid=1234
        host=Mock();host.policy={'mode':'test'};host.prepare.return_value={'prepared':True}
        host.attest.side_effect=PermissionError('profile not enforcing');host.created=True
        self.vm.host=host
        with self.assertRaisesRegex(PermissionError,'enforcing'):self.vm.boot()
        self.assertEqual(self.vm.state,'FAILED');host.retire.assert_called_once()
        self.assertNotIn('cont',[call.args[0] for call in self.vm.qmp.execute.call_args_list])

    def test_failed_broker_retirement_keeps_nonterminal_state_and_durable_death(self):
        process=self.instrument();self.vm.boot()
        host=Mock();host.created=True;host.retire.side_effect=RuntimeError('broker unavailable')
        self.vm.host=host;self.vm.record['host_isolation']={'prepared':True}
        with self.assertRaisesRegex(RuntimeError,'broker'):self.vm.stop()
        saved=json.loads(self.vm.record_path.read_text())
        self.assertEqual(saved['state'],'STOPPING');self.assertIsNone(saved['process'])
        host.retire.side_effect=None
        self.assertEqual(self.vm.stop()['state'],'STOPPED')
        self.assertTrue(json.loads(self.vm.record_path.read_text())['host_retired'])

    def test_boot_failure_is_saved_before_cleanup_failure(self):
        self.instrument()
        self.vm._prove_kvm=Mock(side_effect=RuntimeError('original boot failure'))
        self.vm._terminate=Mock(side_effect=RuntimeError('cleanup failure'))
        with self.assertRaisesRegex(RuntimeError,'cleanup failure'):self.vm.boot()
        saved=json.loads(self.vm.record_path.read_text())
        self.assertEqual(saved['boot_error']['message'],'original boot failure')
        self.assertEqual(saved['cleanup_error']['message'],'cleanup failure')
        self.assertEqual(saved['state'],'STOPPING')
        self.vm._terminate.side_effect=None


if __name__=='__main__':unittest.main()
