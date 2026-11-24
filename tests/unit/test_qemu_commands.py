"""Controlled command boundaries: long budgets, inspection and proven recovery."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from dsec.contracts.execution import command_timeout
from dsec.contracts.requests import RequestUncertain
from dsec.runtime.backends.docker_guest import Supervisor, bounded_shell
from dsec.runtime.backends.qemu_container import QemuContainer
from dsec.runtime.requests import RequestJournal


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.rid='a'*32
        self.result=dict(exit_code=0,output='once',timed_out=False,truncated=False)

    def handle(self,control):
        lock=threading.RLock()
        def status():
            with lock:return dict(state='RUNNING',process_alive=True)
        vm=SimpleNamespace(directory=self.root,lock=lock,status=status,record=None)
        backend=SimpleNamespace(item=dict(mode='container',command_timeout_ms=120000),_ready=lambda control:{})
        return QemuContainer(backend,'b'*32,None,vm,control)

    def proof(self,request):
        return dict(request_id=request['request_id'],digest=hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest(),
                    state='DONE',response=dict(ok=True,result=self.result))

    def test_lost_reply_reconciles_exact_guest_proof_without_reexecution(self):
        control=Mock();control.call.side_effect=OSError('reply lost');box=self.handle(control)
        from dsec.contracts.errors import RequestOutcomeUnknown
        with self.assertRaises(RequestOutcomeUnknown):box.run_shell('append once',timeout_ms=45000,request_id=self.rid)
        self.assertEqual(box.journal.lookup(self.rid)['state'],'UNKNOWN')
        with self.assertRaises(RequestUncertain):box.run_shell('append once',timeout_ms=45000,request_id=self.rid)
        request=dict(operation='execute',request_id=self.rid,command='append once',timeout_ms=45000,output_limit=65536)
        control.call.side_effect=None;control.call.return_value=self.proof(request)
        self.assertEqual(box.query_request(self.rid)['response']['result'],self.result)
        self.assertEqual(box.run_shell('append once',timeout_ms=45000,request_id=self.rid),self.result)
        self.assertEqual([c.args[0] for c in control.call.call_args_list],['execute','query'])

    def test_restart_retains_unknown_until_matching_terminal_proof(self):
        request=dict(operation='execute',sandbox_id='b'*32,request_id=self.rid,args=dict(command='once',timeout_ms=45000,output_limit=65536))
        journal=RequestJournal(self.root,retain_args=True);journal.begin(request)
        control=Mock();box=self.handle(control)
        self.assertEqual(box.journal.lookup(self.rid)['state'],'UNKNOWN')
        guest=dict(operation='execute',request_id=self.rid,**request['args']);valid=self.proof(guest)
        for changed in (dict(valid,digest='0'*64),dict(valid,request_id='c'*32),dict(valid,state='UNKNOWN'),dict(state='NOT_FOUND')):
            control.call.return_value=changed
            self.assertEqual(box.query_request(self.rid)['state'],'UNKNOWN')
        control.call.return_value=valid
        self.assertEqual(box.query_request(self.rid)['state'],'DONE')
        self.assertTrue(all(c.args[0]=='query' for c in control.call.call_args_list))

    def test_inspection_does_not_wait_for_host_command_lock(self):
        entered,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        control=Mock()
        def call(operation,**kwargs):
            entered.set();assert release.wait(3)
            return self.proof(dict(operation=operation,**kwargs))
        control.call.side_effect=call;box=self.handle(control)
        with ThreadPoolExecutor(max_workers=2) as pool:
            work=pool.submit(box.run_shell,'once',timeout_ms=45000,request_id=self.rid)
            self.assertTrue(entered.wait(2))
            read=pool.submit(box.status)
            try:
                try:status=read.result(1)
                except TimeoutError:self.fail('Inspection queued behind admitted command')
                self.assertEqual(status['state'],'RUNNING')
                self.assertEqual(box.query_request(self.rid)['state'],'PENDING')
            finally:release.set()
            self.assertEqual(work.result(2),self.result)

    def test_guest_inspection_does_not_wait_for_execute_or_fncall_cleanup(self):
        for mode in ('container','fncall'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as root:
                entered,release=threading.Event(),threading.Event();cid='d'*64
                def run(argv,**kwargs):
                    if argv[0]=='inspect':return json.dumps([dict(Id=cid,State=dict(Running=True))])
                    if argv[0]=='run':return cid
                    if argv[:2]==['exec','-d']:return ''
                    if argv[0]=='exec':
                        entered.set();assert release.wait(3)
                        return json.dumps(self.result)
                    if argv[0]=='restart':return cid
                    raise AssertionError(argv)
                guest=Supervisor(dict(mode=mode,image='sha256:'+'e'*64,memory_mb=512,cpus=1,command_timeout_ms=120000),root,run=run)
                guest.start();request=dict(operation='execute',request_id=self.rid,command='once',timeout_ms=45000,output_limit=65536)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    work=pool.submit(guest.dispatch,request);self.assertTrue(entered.wait(2))
                    try:
                        self.assertEqual(pool.submit(guest.dispatch,dict(operation='status')).result(1)['command_timeout_ms'],120000)
                        self.assertEqual(pool.submit(guest.dispatch,dict(operation='query',request_id=self.rid)).result(1)['state'],'UNKNOWN')
                    finally:release.set()
                    self.assertEqual(work.result(2)['state'],'DONE')

    def test_native_channel_inherits_same_pinned_ceiling(self):
        from dsec.runtime.backends.qemu_container import QemuContainerBackend
        backend=object.__new__(QemuContainerBackend)
        backend.item=dict(mode='container',command_timeout_ms=120000)
        backend.attach=Mock(return_value=SimpleNamespace(lease=SimpleNamespace(cid=20000),control=SimpleNamespace(auth_key=b'k'*32)))
        channel=backend.native_channel('b'*32)
        self.assertEqual(channel.max_timeout_ms,120000)
        channel.prepare('run',session_id='a'*32,command='sleep 41',timeout_ms=45000,output_limit=1024)
        with self.assertRaises(ValueError):channel.prepare('run',session_id='a'*32,command='sleep 200',timeout_ms=120001,output_limit=1024)
        backend.item.pop('command_timeout_ms')
        self.assertEqual(backend.native_channel('b'*32).max_timeout_ms,30000)

    def test_ceiling_rejected_before_effect_and_legacy_default_retained(self):
        for value in (0,True,300001,1.5):
            with self.assertRaises(ValueError):command_timeout(value)
        self.assertEqual(command_timeout(300000),300000)
        control=Mock();box=self.handle(control)
        with self.assertRaises(ValueError):box.run_shell('once',timeout_ms=120001,request_id=self.rid)
        control.call.assert_not_called();self.assertEqual(box.query_request(self.rid)['state'],'NOT_FOUND')
        run=Mock();guest=Supervisor(dict(mode='container',image='sha256:'+'e'*64,memory_mb=512,cpus=1),self.root/'guest',run=run)
        self.assertEqual(guest.command_timeout_ms,30000)

    def test_standalone_inner_helper_contract_is_mounted_and_importable(self):
        import shutil
        from dsec.runtime.backends import docker_guest
        from dsec.contracts import execution
        directory=self.root/'standalone';directory.mkdir()
        shutil.copy2(docker_guest.__file__,directory/'helper.py')
        shutil.copy2(execution.__file__,directory/'execution_contract.py')
        process=subprocess.run([sys.executable,'-B','-c',"import runpy; value=runpy.run_path('helper.py'); assert value['DEFAULT_COMMAND_TIMEOUT_MS']==30000"],
            cwd=directory,capture_output=True,text=True,env=dict(os.environ,PYTHONPATH=''))
        self.assertEqual(process.returncode,0,process.stderr)
        calls=[];cid='d'*64
        def run(argv,**kwargs):
            calls.append(argv);return cid if argv[0]=='run' else ''
        guest=Supervisor(dict(mode='container',image='sha256:'+'e'*64,memory_mb=512,cpus=1),self.root/'guest',run=run)
        guest.start()
        self.assertIn('type=bind,src=/opt/dsec/execution_contract.py,dst=/execution_contract.py,readonly',calls[0])
        self.assertIn('type=bind,src=/usr/local/bin/docker-init,dst=/dsec-init,readonly',calls[0])
        self.assertEqual(calls[0][calls[0].index('--entrypoint')+1],'/dsec-init')

    @unittest.skipUnless(sys.platform=='linux','Linux subreaper/process tree')
    def test_timeout_retires_setsid_but_success_preserves_container_background_behavior(self):
        real=subprocess.Popen
        def popen(argv,**kwargs):
            kwargs['cwd']=self.temp.name;return real(argv,**kwargs)
        for tail,expected in (('sleep 10',True),('true',False)):
            with self.subTest(tail=tail),patch('dsec.runtime.backends.docker_guest.subprocess.Popen',popen):
                result=bounded_shell('setsid sleep 12345 >/dev/null 2>&1 & echo $!; '+tail,150,4096)
            self.assertEqual(result['timed_out'],expected)
            pid=int(result['output'].strip())
            if expected:self.assertFalse(Path(f'/proc/{pid}').exists(),f'descendant {pid} survived timeout')
            else:
                try:self.assertTrue(Path(f'/proc/{pid}').exists(),'successful Container background process was killed')
                finally:
                    try:os.kill(pid,9)
                    except ProcessLookupError:pass
                    try:os.waitpid(pid,0)
                    except ChildProcessError:pass


if __name__=='__main__':unittest.main()
