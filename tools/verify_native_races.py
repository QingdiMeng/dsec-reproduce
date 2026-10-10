"""Linux acceptance for native cancellation, callback fencing and stop admission.

Uses a new exclusive root, real Firecracker/KVM, prepared immutable guest disks
and optional real Docker/EROFS containers. No external service is restarted.
Barriers delay transport or callback delivery, never emulate backend drivers.
"""
import argparse
import asyncio
from contextlib import ExitStack
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import traceback
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from dsec.control.server import BoundedServer, Handler
from dsec.control.environments import load_configuration
from dsec.contracts.errors import CommandOutcomeUnknown, ServiceBusy
from dsec.contracts.sandbox import DSecContainerRunArgs
from dsec.runtime.container_edge import ContainerRuntime
from dsec.runtime.edge import open_edge
from dsec.runtime.requests import RequestJournal
from dsec.runtime.sessions import service
from dsec.runtime.sessions.native import NativeChannel
from dsec.sdk.client import DSecClient


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class Acceptance:
    def __init__(self, args):
        self.args = args
        self.root = args.root.resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.report = dict(status='running', checks=[], traces={}, cleanup_errors=[],
            identities={name:dict(path=str(getattr(args, name)), sha256=digest(getattr(args, name)))
                        for name in ('binary', 'kernel', 'template', 'native_agent')},
            runtime_module=service.__file__, root=str(self.root),
            boundary='Real Linux drivers with controlled transport/callback barriers; not a performance or full linearizability proof')
        self.report['identities']['source_manifest'] = args.source_manifest_sha256
        self.managers, self.boxes, self.pids, self.container_ids = [], [], [], []
        self.manager = self.open_manager()
        self.runtime = ContainerRuntime(dict(DSEC_CONTAINER_ROOT=str(self.root/'containers'),
            DSEC_CONTAINER_AGENT=str(Path(service.__file__).parents[1]/'backends/container_agent.py'),
            DSEC_ENVIRONMENT_CATALOG=str(args.container_catalog),
            DSEC_NATIVE_AGENT=str(args.native_agent))) if args.container_catalog else None
        self.socket = self.root/'edge.sock'
        self.server = BoundedServer(str(self.socket), Handler, 16)
        self.server.manager, self.server.journal = self.manager, RequestJournal(self.root/'requests')
        self.server.admission_worker_socket, self.server.node_admission = None, None
        self.server.container_runtime = self.runtime
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs=dict(poll_interval=.01))
        self.thread.start()
        self.write()

    def open_manager(self):
        options = dict(capacity=2, snapshot_concurrency=2)
        if self.args.vm_catalog:
            cfg = load_configuration(SimpleNamespace(microvm_environment_catalog=str(self.args.vm_catalog),
                template=str(self.args.template)), argparse.ArgumentParser())
            options.update(tb2_templates=cfg.templates, tb2_layers=cfg.layers,
                tb2_layer_counts=cfg.layer_counts, tb2_layer_transports=cfg.layer_transports,
                tb2_layer_dax_indices=cfg.layer_dax_indices, generic_dax_binaries=cfg.dax_binaries,
                microvm_environment_catalog=cfg.catalog, tb2_resources=cfg.resources)
        manager = open_edge(self.root/'vms', self.args.binary, self.args.kernel, self.args.template, **options)
        self.managers.append(manager)
        return manager

    def write(self):
        (self.root/'report.json').write_text(json.dumps(self.report, indent=2)+'\n')

    def passed(self, name, **details):
        self.report['checks'].append(dict(name=name, **details))
        self.write()
        print(json.dumps(dict(passed=name, **details)), flush=True)

    def box(self):
        box = self.manager.create(environment_id=self.args.vm_environment)
        self.boxes.append(box)
        self.pids.append(box.vm.process.pid)
        return box

    async def queue(self, backend):
        async with DSecClient(self.socket) as client:
            box = (await client.run_microvm() if backend == 'microvm' else
                   await client.run_container(DSecContainerRunArgs(environment_id=self.args.container_environment)))
            if backend == 'microvm':
                self.boxes.append(self.manager.sandboxes[box.id])
                self.pids.append((await box.status())['pid'])
            else:
                self.container_ids.append(box.id)
            try:
                session = await box.open_session()
                jobs, op = self.server.stream_jobs(), uuid.uuid4().hex
                arrived, release = threading.Event(), threading.Event()
                observations, reply, sent, queued_cancel = [], 'NONE', 0, False
                def observe(job):
                    proof = self.server.journal.lookup(op)
                    result = (proof.get('response') or {}).get('result', {})
                    observations.append(dict(phase=job['phase'], intent=job['cancel_requested'].is_set(),
                        reply=reply, queuedCancel=queued_cancel, executed=bool(sent),
                        result=('CANCELLED' if result.get('cancelled') else 'SUCCESS')
                            if proof['state']=='DONE' else proof['state']))
                def checkpoint(operation, stage, job):
                    nonlocal queued_cancel
                    if operation != op:
                        return
                    if stage=='cancel_intent':
                        queued_cancel = job['phase']=='QUEUED'
                    observe(job)
                    if stage=='before_dispatch':
                        arrived.set()
                        if not release.wait(10):
                            raise RuntimeError('Queue barrier timed out')
                original_stream = NativeChannel.stream
                def stream(channel, **values):
                    nonlocal sent
                    if values.get('operation_id')==op:
                        sent += 1
                    yield from original_stream(channel, **values)
                with patch.object(jobs, '_checkpoint', side_effect=checkpoint), patch.object(NativeChannel, 'stream', stream):
                    await box._native('stream', _operation='native_stream', request_id=op,
                        session_id=session.id, command='printf x > /tmp/dsec-queued-effect', timeout_ms=5000, output_limit=65536)
                    try:
                        assert await asyncio.to_thread(arrived.wait, 10)
                        job = jobs.active[op]
                        cancel = await session.cancel(op)
                        reply = str(cancel['cancel_requested']).upper()
                        observe(job)
                        assert self.server.journal.lookup(op)['cancel_requested']
                    finally:
                        release.set()
                    events = [event async for event in session.events(op)]
                    await asyncio.to_thread(job['thread'].join, 10)
                    assert not job['thread'].is_alive()
                assert cancel['cancel_requested'] and events[-1]['result']['cancelled'], events
                assert sent == 0
                assert (await session.run_shell('test ! -e /tmp/dsec-queued-effect; printf alive'))['stdout']=='alive'
                assert not (await session.cancel(op))['cancel_requested']
                replay = [event async for event in session.stream('printf x > /tmp/dsec-queued-effect',
                    request_id=op, timeout_ms=5000)]
                assert replay[-1]['result']['cancelled'] and sent==0
                name=backend+'-queued-cancel'
                self.report['traces'][name]=dict(model='QueueCancellation', observed=observations)
                self.passed(name, request_id=op, sends=sent)
            finally:
                await box.stop()

    async def in_transit(self):
        async with DSecClient(self.socket) as client:
            box = await client.run_microvm()
            self.boxes.append(self.manager.sandboxes[box.id]); self.pids.append((await box.status())['pid'])
            try:
                session = await box.open_session()
                op, arrived, release, missed = uuid.uuid4().hex, threading.Event(), threading.Event(), threading.Event()
                calls=[]
                original_stream, original_call = NativeChannel.stream, NativeChannel.call
                def stream(channel, **values):
                    if values.get('operation_id')==op:
                        arrived.set()
                        if not release.wait(10):
                            raise RuntimeError('Dispatch barrier timed out')
                    yield from original_stream(channel, **values)
                def call(channel, action, **values):
                    if action=='cancel':
                        calls.append(values['operation_id'])
                    try:
                        return original_call(channel, action, **values)
                    except FileNotFoundError:
                        missed.set()
                        raise
                with patch.object(NativeChannel, 'stream', stream), patch.object(NativeChannel, 'call', call):
                    await box._native('stream', _operation='native_stream', request_id=op,
                        session_id=session.id, command='printf started; sleep 20', timeout_ms=25000)
                    try:
                        assert await asyncio.to_thread(arrived.wait, 10)
                        job = self.server.stream_jobs().active[op]
                        assert (await session.cancel(op))['cancel_requested']
                        assert await asyncio.to_thread(missed.wait, 10)
                    finally:
                        release.set()
                    events=[event async for event in session.events(op)]
                    await asyncio.to_thread(job['thread'].join, 10)
                assert not job['thread'].is_alive()
                assert events[-1]['result']['cancelled'], events
                assert len(calls)>=2 and set(calls)=={op}, calls
                self.passed('microvm-in-transit-exact-id-cancel', request_id=op, cancellation_attempts=len(calls))
            finally:
                await box.stop()

    def callback(self, replace):
        box = self.box()
        if replace:
            box.pause(); box.resume(); self.pids.append(box.vm.process.pid)
        base_epoch = box.native_incarnation
        op_epoch = base_epoch
        stopped, unknown, stale_retired = False, False, False
        observations=[]
        def observe():
            observations.append(dict(life=box.state, epoch=box.native_incarnation-base_epoch,
                opEpoch=op_epoch-base_epoch, active=bool(box.native_inflight), stopped=stopped,
                unknown=unknown, obsolete=unknown and box.native_incarnation!=op_epoch, staleRetired=stale_retired))
        observe()
        def checkpoint(stage, **details):
            nonlocal unknown, stale_retired
            if stage=='outcome_unknown':
                unknown=True
                stale_retired=(box.native_incarnation!=op_epoch and observations[-1]['life']=='RUNNING' and box.state=='FAILED')
            observe()
        box._native_checkpoint=checkpoint
        arrived, release = threading.Event(), threading.Event()
        errors, responses = [], []
        sid=uuid.uuid4().hex
        NativeChannel(box.vm.vsock,vsock=True).call('open',session_id=sid)
        request_id=uuid.uuid4().hex
        def execute():
            try:
                with service.native_operation(self.server,box.id,
                    dict(backend='microvm',action='run',session_id=sid,command='printf real-guest-response'),request_id) as (channel,values):
                    responses.append(channel.call('run',**values))
                    arrived.set()
                    if not release.wait(30):
                        raise RuntimeError('Late callback barrier timed out')
                    raise CommandOutcomeUnknown('Controlled reply loss AFTER a real guest command')
            except BaseException as exc:
                errors.append(exc)
        thread=threading.Thread(target=execute)
        thread.start()
        old_pid=box.vm.process.pid
        try:
            assert arrived.wait(15), errors
            assert responses[0]['stdout']=='real-guest-response',responses
            if replace:
                with box.lock:
                    box._fail('acceptance_retire_old_process')
                observe()
                box.recover(allow_rollback=True)
                self.pids.append(box.vm.process.pid)
                observe()
                replacement=box.vm.process
                replacement_deadline=box.deadline
            else:
                box.stop(); stopped=True; observe()
        finally:
            release.set(); thread.join(15)
        assert not thread.is_alive()
        assert len(errors)==1 and isinstance(errors[0],CommandOutcomeUnknown),errors
        assert not Path(f'/proc/{old_pid}').exists()
        assert box.state==('RUNNING' if replace else 'STOPPED'),box.state
        assert not box.native_inflight
        name='microvm-old-incarnation' if replace else 'microvm-stop-before-callback'
        self.report['traces'][name]=dict(model='NativeCallbackFence',observed=observations)
        persisted=json.loads((box.directory/'registry.json').read_text())
        assert not persisted['native_inflight']
        if replace:
            assert replacement.poll() is None
            def guest_probe(label):
                channel=NativeChannel(box.vm.vsock,vsock=True)
                sid=uuid.uuid4().hex
                channel.call('open',session_id=sid)
                try:
                    value=channel.call('run',session_id=sid,command='printf '+label)
                    assert value['exit_code']==0 and value['stdout']==label,value
                    return value
                finally:
                    channel.call('close',session_id=sid)
            guest_before=guest_probe('replacement-native-alive')
            assert persisted['native_incarnation']==base_epoch+1
            assert box.deadline==replacement_deadline, 'Old callback renewed replacement idle deadline'
            self.manager.detach()
            self.manager=self.open_manager(); self.server.manager=self.manager
            box=self.manager.sandboxes[box.id]
            assert box.state=='RUNNING' and not box.native_inflight
            assert box.native_incarnation==base_epoch+1 and box.vm.process.pid==replacement.pid
            guest_after=guest_probe('adopted-native-alive')
            box.stop()
            # This harness keeps both Edge owners in ONE OS process. The
            # adopted pidfd proves death; only the original parent's Popen
            # handle can reap its child. Production service restarts exit the
            # original parent. Reap this exact owned child, never other PIDs.
            replacement.wait(timeout=10)
            assert not Path(f'/proc/{replacement.pid}').exists()
            self.passed(name,old_pid=old_pid,replacement_pid=replacement.pid,incarnation=base_epoch+1,
                registry_readopted=True,guest_before_adoption=guest_before,guest_after_adoption=guest_after)
        else:
            assert box.resource_cleanup_complete
            with box.lock: box._fail('second_late_failure')
            assert box.state=='STOPPED'
            self.passed(name,old_pid=old_pid,resource_cleanup_complete=True)

    async def container_gate(self, stop_first):
        spec=DSecContainerRunArgs(environment_id=self.args.container_environment)
        args=dict(kind='container',spec=asdict(spec))
        sid=uuid.uuid4().hex
        self.runtime.dispatch('container_create',None,args,sid)
        self.container_ids.append(sid)
        observations=[]; stopping,rejected=False,False
        def observe():
            active=sorted(int(value[0]) for value in self.runtime.native_activity.get(sid,{}).values())
            observations.append(dict(active=active,stopping=stopping,
                stopped=self.runtime._backend('container',spec).prove_stopped(sid),rejected=rejected))
        observe()
        entry=self.runtime._entry('container',spec,sid)
        real_stop=entry.sandbox.stop
        entered,release=threading.Event(),threading.Event()
        errors=[]
        def stop():
            nonlocal stopping
            stopping=True;observe();entered.set()
            if stop_first and not release.wait(30):raise RuntimeError('Stop barrier timed out')
            value=real_stop();stopping=False;observe();return value
        def dispatch():
            try:self.runtime.dispatch('container_stop',sid,args,'3'*32)
            except BaseException as exc:errors.append(exc)
        def checkpoint(stage, sandbox_id, **details):
            nonlocal rejected
            if sandbox_id!=sid:return
            if stage=='stop_rejected':rejected=True
            observe()
        old_checkpoint=self.runtime._native_checkpoint
        self.runtime._native_checkpoint=checkpoint
        try:
            with patch.object(entry.sandbox,'stop',side_effect=stop):
                if stop_first:
                    thread=threading.Thread(target=dispatch);thread.start()
                    try:
                        assert await asyncio.to_thread(entered.wait,15)
                        try:
                            with service.native_operation(self.server,sid,
                                dict(backend='container',action='open',session_id='1'*32,**args),'1'*32):
                                raise AssertionError('Native operation admitted while stop held admission')
                        except ServiceBusy:rejected=True;observe()
                    finally:
                        release.set();await asyncio.to_thread(thread.join,20)
                    assert not thread.is_alive() and not errors,errors
                else:
                    with ExitStack() as stack:
                        values=dict(backend='container',action='open',**args)
                        first=stack.enter_context(service.native_operation(self.server,sid,dict(values,session_id='1'*32),'1'*32))
                        second=stack.enter_context(service.native_operation(self.server,sid,dict(values,session_id='2'*32),'2'*32))
                        # Each admission really reaches the Docker native agent.
                        for channel,native in (first,second):assert channel.call('open',**native)['session_id']
                        assert len(self.runtime.native_activity[sid])==2
                        try:
                            self.runtime.dispatch('container_stop',sid,args,'3'*32)
                            raise AssertionError('Stop admitted while native operations active')
                        except ServiceBusy:pass
                        assert self.runtime.lookup_request('3'*32)['state']=='NOT_FOUND'
                        try:
                            self.runtime.close();raise AssertionError('Active container owner released')
                        except ServiceBusy:pass
                    self.runtime.dispatch('container_stop',sid,args,'4'*32)
            assert not self.runtime.native_activity
            name='container-stop-first' if stop_first else 'container-native-first'
            self.report['traces'][name]=dict(model='ContainerNativeGate',observed=observations)
            self.passed(name,sandbox_id=sid)
        finally:
            release.set()
            self.runtime._native_checkpoint=old_checkpoint
            self.runtime.dispatch('container_stop',sid,args,uuid.uuid4().hex)

    async def run(self):
        try:
            await self.queue('microvm')
            await self.in_transit()
            await asyncio.to_thread(self.callback,False)
            await asyncio.to_thread(self.callback,True)
            if self.runtime:
                await self.queue('container')
                await self.container_gate(False)
                await self.container_gate(True)
            self.report['status']='passed'
        except BaseException as exc:
            self.report.update(status='failed',error=repr(exc),traceback=traceback.format_exc())
        finally:
            self.server.shutdown();self.thread.join(10);self.server.server_close()
            for manager in reversed(self.managers):
                try:manager.close()
                except Exception as exc:self.report['cleanup_errors'].append(repr(exc))
            if self.runtime:
                for sid in self.container_ids:
                    try:self.runtime.dispatch('container_stop',sid,dict(kind='container',
                        spec=asdict(DSecContainerRunArgs(environment_id=self.args.container_environment))),uuid.uuid4().hex)
                    except Exception as exc:self.report['cleanup_errors'].append(dict(container=sid,error=repr(exc)))
                try:self.runtime.close()
                except Exception as exc:self.report['cleanup_errors'].append(repr(exc))
            alive=[pid for pid in self.pids if Path(f'/proc/{pid}').exists()]
            self.report['cleanup']=dict(vmm_pids=self.pids,alive_vmm_pids=alive,
                private_containers=[sid for sid in self.container_ids if subprocess.run(
                    ['docker','inspect',self.runtime._backend('container',DSecContainerRunArgs(
                        environment_id=self.args.container_environment)).container_prefix+sid],capture_output=True).returncode==0],
                active_native_operations=sum(len(box.native_inflight) for m in self.managers for box in m.sandboxes.values()),
                private_directories=[str(self.root/'containers'/sid) for sid in self.container_ids if (self.root/'containers'/sid).exists()])
            if alive or any(self.report['cleanup'][key] for key in ('private_containers','active_native_operations','private_directories')) or self.report['cleanup_errors']:
                self.report['status']='failed'
            self.write()
        return self.report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('root','binary','kernel','template','native-agent'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--vm-catalog',type=Path)
    parser.add_argument('--vm-environment',default='default')
    parser.add_argument('--container-catalog',type=Path)
    parser.add_argument('--container-environment')
    parser.add_argument('--source-manifest-sha256',required=True)
    args=parser.parse_args()
    if sys.platform!='linux':parser.error('Real Linux acceptance required')
    if bool(args.container_catalog)!=bool(args.container_environment):parser.error('Container catalog and environment must be supplied together')
    if not os.access('/dev/kvm',os.R_OK|os.W_OK):parser.error('KVM read/write permission required')
    report=asyncio.run(Acceptance(args).run())
    print(json.dumps(dict(status=report['status'],checks=len(report['checks']),root=str(args.root))),flush=True)
    raise SystemExit(report['status']!='passed')


if __name__=='__main__':main()
