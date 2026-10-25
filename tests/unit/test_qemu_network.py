"""Controlled ownership, setup-order and retirement regressions for QEMU TAPs."""
import array
import json
import os
from pathlib import Path
import socket
import stat
import struct
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from dsec.host import qemu_network_helper as broker
from dsec.runtime.isolation.qemu_network import QemuNetwork,configuration
from dsec.runtime.isolation.qemu_host import HostIsolation
from tests.unit import test_qemu_backend as qemu_tests


def settings():
    return dict(helper='/usr/local/libexec/dsec-unit-qemu-network',uplink='eth0',slot_offset=400,max_slots=2,
                policy=dict(dns='1.1.1.1',egress_proxy=None))


class NetworkBrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.enterContext(patch.object(broker,'STATE',self.root))
        self.cfg=dict(uid=1000,prefix='dsec-unit',runtime_root=str(self.root/'containers'),
            python='/usr/bin/python3',launcher='/opt/qemu_launch.py',object='/etc/egress.o',
            **{k:v for k,v in settings().items() if k!='helper'})
        self.sid='a'*32;self.spec=broker.names(self.cfg,self.sid,0)
        self.record=dict(state='CLAIMED',spec=self.spec,process={'pid':123,'start_ticks':'5'})
        self.run=self.enterContext(patch.object(broker,'run',return_value=''))
        self.exists=self.enterContext(patch.object(broker,'exists',return_value=False))
        self.enterContext(patch.object(broker,'identity',return_value=dict(pid=123,uid=1000,start_ticks='5',boot_id='test',exe=self.cfg['python'],argv=[self.cfg['python'],'-I','-B',self.cfg['launcher'],str(Path(self.cfg['runtime_root'])/self.sid/'launch.json'),'8'])))
        self.enterContext(patch.object(broker,'inspect',return_value={}))
        self.dead=self.enterContext(patch.object(broker,'prove_dead'))

    def test_live_process_never_loses_network_or_rules(self):
        self.dead.side_effect=RuntimeError('still live')
        with self.assertRaisesRegex(RuntimeError,'live'):broker.retire(self.cfg,self.sid,self.record)
        self.run.assert_not_called();self.exists.assert_not_called()
        self.assertEqual(self.record['state'],'CLAIMED')

    def test_retirement_fences_claim_before_teardown_and_failure_keeps_fence(self):
        self.record['identity']={}
        def failure(*args):
            saved=json.loads((self.root/(self.sid+'.json')).read_text())
            self.assertEqual(saved['state'],'RETIRING')
            with self.assertRaisesRegex(RuntimeError,'fenced'):
                broker.claim(self.cfg,self.sid,saved,['123','5'])
            raise RuntimeError('simulated teardown failure')
        self.exists.side_effect=failure
        with self.assertRaisesRegex(RuntimeError,'teardown'):broker.retire(self.cfg,self.sid,self.record)
        self.assertEqual(json.loads((self.root/(self.sid+'.json')).read_text())['state'],'RETIRING')
        self.exists.side_effect=None;self.exists.return_value=False
        self.assertEqual(broker.retire(self.cfg,self.sid,self.record)['state'],'RETIRED')
        self.assertEqual(broker.retire(self.cfg,self.sid,self.record)['state'],'RETIRED')

    def test_descriptor_delivery_is_one_shot_and_never_replays_after_retirement(self):
        broker.inspect.side_effect=AssertionError('Descriptor reissue reached kernel operation')
        for phase in ('LENT','CLAIMED','RETIRING','RETIRED'):
            self.record['state']=phase
            with self.assertRaisesRegex(RuntimeError,'no replay'):broker.lend(self.cfg,self.sid,self.record)
        self.run.assert_not_called()

    def test_setup_attaches_policy_and_drops_defaults_before_link_activation(self):
        calls=[];self.run.side_effect=lambda *args:(calls.append(args) or ('1\n' if args[0]=='sysctl' else '[]' if args[:5]==('ip','-j','-4','route','show') else '1.1.1.1 via 1.2.3.4 dev eth0 src 1.2.3.5 '))
        real_exists=Path.exists
        real_stat=Path.stat
        def info(path,*a,**kw):
            if str(path).startswith('/run/netns/'):return Mock(st_ino=42)
            return real_stat(path,*a,**kw)
        with patch.object(Path,'exists',lambda p:False if str(p).startswith('/run/netns/') else real_exists(p)), \
                patch.object(Path,'stat',info),patch.object(broker,'program',return_value={'id':1,'tag':'abc'}), \
                patch.object(broker,'link',return_value=5),patch.object(broker,'firewall_digest',return_value='fixed'), \
                patch.object(broker,'inspect',side_effect=lambda cfg,record:record):
            broker.prepare(self.cfg,self.sid)
        policy=next(i for i,c in enumerate(calls) if 'filter' in c and 'add' in c)
        up=next(i for i,c in enumerate(calls) if c[-1]=='up')
        forward=next(i for i,c in enumerate(calls) if '-P' in c and 'FORWARD' in c)
        label=next(i for i,c in enumerate(calls) if c[:4]==('ip','link','set',self.spec['host_veth']) and 'alias' in c)
        route=next(i for i,c in enumerate(calls) if c[:5]==('ip','-n',self.spec['namespace'],'route','add'))
        peer_up=calls.index(('ip','-n',self.spec['namespace'],'link','set','vpeer','up'))
        host_up=calls.index(('ip','link','set',self.spec['host_veth'],'up'))
        self.assertLess(label,policy)
        self.assertLess(policy,up);self.assertLess(forward,up)
        self.assertLess(peer_up,route);self.assertLess(host_up,route)
        self.assertTrue(all('-F' not in c and c[:3]!=('ip','link','del') for c in calls))
        self.assertEqual(json.loads((self.root/(self.sid+'.json')).read_text())['state'],'READY')

    def test_existing_or_retired_intent_never_recreates_links(self):
        for phase in ('ALLOCATING','LENT','CLAIMED','RETIRING','RETIRED'):
            (self.root/(self.sid+'.json')).write_text(json.dumps(dict(state=phase,spec=self.spec)))
            with self.assertRaisesRegex(RuntimeError,'cannot be replayed'):broker.prepare(self.cfg,self.sid)
        self.run.assert_not_called()

    def test_ipv4_firewall_counter_and_timestamp_changes_are_not_policy_drift(self):
        with patch.object(broker,'ns',side_effect=['# Generated now\n:FORWARD DROP [1:2]\n-A FORWARD -j ACCEPT\n',
                                                  '# Generated later\n:FORWARD DROP [99:88]\n-A FORWARD -j ACCEPT\n']):
            self.assertEqual(broker.firewall_digest(self.spec),broker.firewall_digest(self.spec))


class KernelJsonTests(unittest.TestCase):
    def test_preference_heading_is_not_a_second_program(self):
        rows=[dict(kind='bpf',pref=1,chain=0),dict(kind='bpf',pref=1,chain=0,
              options={'direct-action':True,'id':37,'tag':'1234'})]
        with patch.object(broker,'ns',return_value=json.dumps(rows)):
            self.assertEqual(broker.program({}),dict(id=37,tag='1234'))
        for extra in (dict(kind='flower',options={}),rows[1],dict(kind='bpf',options={'handle':'0x2'})):
            with patch.object(broker,'ns',return_value=json.dumps(rows+[extra])):
                with self.assertRaises(RuntimeError):broker.program({})

    def test_actual_linux_tc_nested_program_identity(self):
        # Captured from the failed Linux acceptance, not synthesized from the
        # parser's expected shape. Program IDs themselves are host-local.
        rows=[{'protocol':'all','pref':1,'kind':'bpf','chain':0},
              {'protocol':'all','pref':1,'kind':'bpf','chain':0,'options':{
                  'handle':'0x1','bpf_name':'dsec-qnet-qemu-network.o:[classifier]',
                  'direct-action':True,'not_in_hw':True,
                  'prog':{'id':210,'name':'dsec_egress','tag':'7ec11d5fa79d9c4f','jited':1}}}]
        with patch.object(broker,'ns',return_value=json.dumps(rows)):
            self.assertEqual(broker.program({}),dict(id=210,tag='7ec11d5fa79d9c4f'))
        for changed in ({'prog':None},{'prog':{'id':True,'tag':'tag'}},
                        {'prog':{'id':210}},{'id':211},{'tag':'other'},
                        {'direct-action':False}):
            value=[rows[0],dict(rows[1],options=dict(rows[1]['options'],**changed))]
            with self.subTest(changed=changed),patch.object(broker,'ns',return_value=json.dumps(value)):
                with self.assertRaises(RuntimeError):broker.program({})


class NetworkGateTests(unittest.TestCase):
    setUp=qemu_tests.QemuTests.setUp
    instrument=qemu_tests.QemuTests.instrument

    def test_network_attestation_must_pass_before_guest_continuation(self):
        process=self.instrument();process.pid=1234;self.vm.host=Mock();self.vm.host.policy={};self.vm.host.created=True
        self.vm.host.prepare.return_value={}
        self.vm.network=Mock();self.vm.network.policy=settings();self.vm.network.record={'fd':99}
        self.vm.network.prepare.return_value=self.vm.network.record
        self.vm.network.attest.side_effect=RuntimeError('network guard detached')
        with self.assertRaisesRegex(RuntimeError,'guard detached'):self.vm.boot()
        self.assertNotIn('cont',[c.args[0] for c in self.vm.qmp.execute.call_args_list])
        self.vm.network.retire.assert_called_once()

    def test_network_cleanup_failure_preserves_nonterminal_state_and_death(self):
        self.instrument();self.vm.boot()
        self.vm.network=Mock();self.vm.network.retire.side_effect=RuntimeError('namespace still in use')
        with self.assertRaisesRegex(RuntimeError,'still in use'):self.vm.stop()
        saved=json.loads(self.vm.record_path.read_text())
        self.assertEqual(saved['state'],'STOPPING');self.assertIsNone(saved['process'])
        self.vm.network.retire.side_effect=None
        self.assertEqual(self.vm.stop()['state'],'STOPPED')
        self.assertTrue(json.loads(self.vm.record_path.read_text())['network_retired'])

    def test_network_claim_failure_precedes_apparmor_claim_and_exec_permission(self):
        host=HostIsolation.__new__(HostIsolation);host.directory=self.root;host.record={};host.broker=Mock()
        commands=[];popen=__import__('subprocess').Popen
        def launch(argv,**kw):commands.append(argv);return popen(argv,**kw)
        saved=lambda pid:dict(pid=pid,uid=os.getuid(),exe=commands[-1][0],argv=commands[-1],start_ticks='1')
        order=[]
        def commit(*args):order.append('commit')
        def claim(value):order.append('claim');raise RuntimeError('network lease retired')
        with (self.root/'gate-log').open('wb') as log, \
                patch('dsec.runtime.isolation.qemu_host.subprocess.Popen',launch), \
                patch.dict(sys.modules,{'dsec.runtime.registry':SimpleNamespace(identity=saved)}):
            with self.assertRaisesRegex(RuntimeError,'retired'):host.launch(['/bin/false'],log,commit,claim=claim)
        self.assertEqual(order,['commit','claim']);host.broker.assert_not_called()


class ClientTests(unittest.TestCase):
    def test_resource_bounds_and_policy_are_explicit(self):
        self.assertEqual(configuration(settings()),settings())
        for key,value in (('helper','/bin/sh'),('slot_offset',-1),('max_slots',True),('uplink','eth0;bad')):
            with self.assertRaises(ValueError):configuration(dict(settings(),**{key:value}))

    def test_cross_instance_descriptor_is_closed_and_not_adopted(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);network=QemuNetwork(settings(),root);network.broker=Mock(return_value=dict(state='READY',policy=settings()['policy'],identity={},spec={},enforced=True))
            reader,writer=os.pipe();self.addCleanup(os.close,writer)
            channel=Mock();channel.__enter__=Mock(return_value=channel);channel.__exit__=Mock(return_value=False)
            channel.getsockopt.return_value=struct.pack('3i',1,0,0)
            payload=json.dumps(dict(sandbox_id='other',state='LENT',policy=settings()['policy'],identity={},spec={},enforced=True)).encode()
            channel.recvmsg.return_value=(len(payload).to_bytes(4,'big')+payload,[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[reader]).tobytes())],0,None)
            listener=Mock();listener.__enter__=Mock(return_value=listener);listener.__exit__=Mock(return_value=False);listener.accept.return_value=(channel,None)
            child=Mock();child.poll.return_value=0
            with patch.object(Path,'lstat',return_value=Mock(st_mode=stat.S_IFREG|0o755,st_uid=0)), \
                    patch('dsec.runtime.isolation.qemu_network.socket.socket',return_value=listener), \
                    patch('dsec.runtime.isolation.qemu_network.subprocess.Popen',return_value=child), \
                    patch.object(socket,'SO_PEERCRED',17,create=True):
                with self.assertRaisesRegex(RuntimeError,'identity mismatch'):network.prepare()
            with self.assertRaises(OSError):os.fstat(reader)
            self.assertIsNone(network.fd)


class CommandFenceTests(unittest.TestCase):
    def test_broker_death_does_not_unlock_an_inflight_mutation(self):
        import fcntl
        import subprocess
        import time
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);lock=root/'lock';ready=root/'ready'
            script=("import fcntl,runpy,sys\n"
                    "ns=runpy.run_path("+repr(broker.run.__code__.co_filename)+")\n"
                    "with open("+repr(str(lock))+",'w') as guard:\n"
                    " fcntl.flock(guard,fcntl.LOCK_EX)\n"
                    " ns['run'].__globals__['LOCK_FD']=guard.fileno()\n"
                    " ns['run'](sys.executable,'-c',"+repr("from pathlib import Path;import os;Path("+repr(str(ready))+").write_text('held');os.read(0,1)")+")\n")
            child=subprocess.Popen([sys.executable,'-I','-c',script],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            try:
                deadline=time.monotonic()+5
                while not ready.exists():
                    if child.poll() is not None:raise AssertionError(child.stderr.read().decode())
                    if time.monotonic()>deadline:raise AssertionError('Mutation barrier was not reached')
                    time.sleep(.01)
                child.kill();child.wait(timeout=3)
                with lock.open('a') as candidate:
                    with self.assertRaises(BlockingIOError):fcntl.flock(candidate,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    child.stdin.write(b'G');child.stdin.flush()
                    deadline=time.monotonic()+3
                    while True:
                        try:fcntl.flock(candidate,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                        except BlockingIOError:
                            if time.monotonic()>deadline:raise AssertionError('Finished mutation retained lock')
                            time.sleep(.01)
            finally:
                child.stdin.close()
                if child.poll() is None:child.kill();child.wait(timeout=3)
                child.stderr.close()


if __name__=='__main__':unittest.main()
