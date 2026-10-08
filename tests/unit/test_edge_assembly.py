"""Composed Edge recovery with real v1 files/locks and instrumented host processes."""
import copy
import importlib
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

from dsec._persistence import atomic_json
from dsec.contracts.errors import SandboxError
from dsec.runtime.backends.firecracker import MicroVM
from dsec.runtime.edge import open_edge
from dsec.runtime.lifecycle import Sandbox, SandboxManager, _fsync_directory
from dsec.runtime.registry_store import RegistryOperations, SandboxRegistry
from dsec.runtime.requests import RequestJournal


class Process:
    def __init__(self, pid):
        self.pid, self.alive = pid, True
    def poll(self):
        return None if self.alive else 0
    def terminate(self):
        self.alive = False
    kill = terminate
    def wait(self, timeout):
        return 0


class Attached:
    def __init__(self, process):
        self.process, self.pid, self.closed = process, process.pid, False
    def poll(self):
        return 0 if self.closed else self.process.poll()
    def terminate(self):
        self.process.terminate()
    def kill(self):
        self.process.kill()
    def wait(self, timeout):
        self.close()
        return self.process.wait(timeout)
    def close(self):
        self.closed = True


class EdgeAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.binary, self.kernel, self.template = [self.root/n for n in ('fc','kernel','guest')]
        for path in (self.binary, self.kernel, self.template):
            path.write_bytes(path.name.encode())
        self.instances = []
        self.processes, self.identities, self.handles = {}, {}, []
        self.commands, self.boots, self.restores = [], 0, 0
        self.operations = RegistryOperations(
            boot_id='test-boot', identity=lambda pid: copy.deepcopy(self.identities[pid]),
            attach_process=self.attach, process_entries=lambda: [],
            detach_process=lambda process: process.close() if isinstance(process, Attached) else None,
            new_sandbox=lambda: Sandbox.__new__(Sandbox), new_microvm=MicroVM,
            write_json=atomic_json, sync_directory=_fsync_directory)
        for method, implementation in [('boot', self.boot), ('restore', self.restore),
                                       ('api', self.api), ('execute', self.execute)]:
            # Instance attributes on the test case are already bound; explicitly
            # forward the VMM argument instead of installing a bound descriptor.
            mock = patch.object(MicroVM, method, lambda vm, *a, _fn=implementation, **kw: _fn(vm,*a,**kw))
            mock.start()
            self.addCleanup(mock.stop)
        self.addCleanup(self.cleanup_instances)

    def cleanup_instances(self):
        for manager in reversed(self.instances):
            if not manager.registry.owner_lock.closed:
                manager.close()

    def attach(self, saved, binary, api_path, *, attester=None):
        if (saved != self.identities[saved['pid']] or saved['exe'] != str(binary) or
                saved['argv'] != [str(binary),'--api-sock',str(api_path)]):
            raise RuntimeError('VMM process identity mismatch')
        result = Attached(self.processes[saved['pid']])
        self.handles.append(result)
        return result

    def start_process(self, vm):
        pid = 10000 + len(self.processes)
        vm.process = Process(pid)
        self.processes[pid] = vm.process
        self.identities[pid] = {'pid':pid, 'start_ticks':str(pid), 'boot_id':'test-boot',
            'exe':str(self.binary), 'argv':[str(self.binary),'--api-sock',str(vm.api_path)],
            'uid':os.getuid()}
        if vm.on_process_started:
            vm.on_process_started()

    def boot(self, vm, *args, **kwargs):
        self.boots += 1
        self.start_process(vm)

    def restore(self, vm, *args, **kwargs):
        self.restores += 1
        self.start_process(vm)

    def execute(self, vm, command, *args, **kwargs):
        self.commands.append(command)
        return {'exit_code':0,'output':'ok','timed_out':False,'truncated':False}

    def api(self, vm, method, path, body=None, **kwargs):
        if method == 'GET' and path == '/':
            return {'state':'Running'}
        if path == '/snapshot/create':
            Path(body['snapshot_path']).write_bytes(b'state')
            Path(body['mem_file_path']).write_bytes(b'memory')
        return {}

    def open(self, *, operations=None):
        registry = SandboxRegistry(self.root/'instances', operations or self.operations)
        manager = open_edge(registry.root, self.binary, self.kernel, self.template, registry=registry)
        self.instances.append(manager)
        return manager

    def test_detach_and_restart_adopt_same_vm_without_boot_or_command_replay(self):
        first = self.open()
        self.assertIs(type(first), SandboxManager)
        sandbox = first.create()
        sandbox.execute('echo first')
        process = sandbox.vm.process
        first.detach()
        self.assertTrue(process.alive)
        restored = self.open()
        self.assertIs(restored.sandboxes[sandbox.id].manager, restored)
        self.assertEqual(restored.sandboxes[sandbox.id].state, 'RUNNING')
        self.assertEqual((self.boots,self.commands), (1,['echo first']))
        restored.sandboxes[sandbox.id].execute('echo second')
        restored.close()
        self.assertFalse(process.alive)
        self.assertEqual(self.commands, ['echo first','echo second'])
        self.assertTrue(restored.registry.owner_lock.closed)

    def test_paused_snapshot_restores_original_identity_after_service_restart(self):
        first = self.open()
        sandbox = first.create()
        sandbox.execute('prepare')
        sandbox.pause()
        snapshot = sandbox.snapshot
        first.detach()
        restored = self.open()
        recovered = restored.sandboxes[sandbox.id]
        self.assertEqual((recovered.state,recovered.snapshot,recovered.generation), ('PAUSED',snapshot,1))
        before = list(self.commands)
        recovered.resume()
        self.assertEqual(recovered.state, 'RUNNING')
        self.assertEqual((self.boots,self.restores), (1,1))
        self.assertEqual(self.commands, before)
        recovered.stop()
        self.assertFalse(snapshot.exists())

    def test_directory_owner_conflict_does_not_modify_live_instance(self):
        first = self.open()
        sandbox = first.create()
        path = sandbox.directory/'registry.json'
        record = path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Another manager'):
            self.open()
        self.assertEqual(path.read_bytes(), record)
        self.assertTrue(sandbox.vm.process.alive)
        self.assertFalse(first.registry.owner_lock.closed)

    def test_interrupted_action_stays_unknown_and_prunes_only_uncommitted_files(self):
        first = self.open()
        sandbox = first.create()
        sandbox.execute('committed action')
        pending = sandbox.directory/'pending-12345678'
        pending.mkdir()
        (pending/'memory').write_bytes(b'partial')
        request = {'request_id':'a'*32,'operation':'execute','sandbox_id':sandbox.id,
                   'args':{'command':'unknown action'}}
        RequestJournal(first.root).begin(request)
        sandbox.inflight = {'request_id':request['request_id'], 'operation':'execute'}
        sandbox._persist()
        first.detach()
        restored = self.open()
        recovered = restored.sandboxes[sandbox.id]
        self.assertEqual(recovered.state, 'FAILED')
        self.assertIn('interrupted_execute_outcome_unknown', recovered.reason)
        self.assertFalse(pending.exists())
        self.assertEqual(self.commands, ['committed action'])
        self.assertEqual(RequestJournal(restored.root).lookup('a'*32)['state'], 'UNKNOWN')

    def test_identity_mismatch_does_not_attach_or_signal_reused_process(self):
        first = self.open()
        sandbox = first.create()
        process = sandbox.vm.process
        first.detach()
        self.identities[process.pid]['start_ticks'] = 'new-process'
        restored = self.open()
        self.assertEqual(restored.sandboxes[sandbox.id].state, 'FAILED')
        self.assertTrue(process.alive)
        self.assertTrue(any(item['event']=='not_attached' for item in restored.recovery_events))

    def test_failed_monitor_start_releases_handles_and_owner_without_stopping_vm(self):
        first = self.open()
        sandbox = first.create()
        process = sandbox.vm.process
        first.detach()
        with patch.object(SandboxManager, 'start_monitors', side_effect=RuntimeError('thread start failed')):
            with self.assertRaisesRegex(RuntimeError, 'thread start failed'):
                self.open()
        self.assertTrue(process.alive)
        self.assertTrue(self.handles[-1].closed)
        restored = self.open()
        self.assertEqual(restored.sandboxes[sandbox.id].state, 'RUNNING')
        self.assertEqual(self.boots, 1)

    def test_stop_cleanup_failure_retains_owner_until_retry(self):
        manager = self.open()
        sandbox = manager.create()
        with patch.object(sandbox.disk.__class__, 'unlink', side_effect=OSError('disk cleanup failed')):
            with self.assertRaisesRegex(OSError, 'disk cleanup failed'):
                manager.close()
        self.assertFalse(manager.registry.owner_lock.closed)
        manager.close()
        self.assertTrue(manager.registry.owner_lock.closed)
        record = json.loads((sandbox.directory/'registry.json').read_text())
        self.assertTrue(record['resource_cleanup_complete'])
        self.assertEqual(record['state'], 'STOPPED')

    def test_retired_aggregate_cannot_mutate_vm_after_another_edge_adopts_it(self):
        first = self.open()
        sandbox = first.create()
        process = sandbox.vm.process
        first.detach()
        restored = self.open()
        before = list(self.commands)
        for operation in (lambda: sandbox.execute('stale command'), sandbox.pause, sandbox.stop,
                          first.create, lambda: first.create(baseline_id=sandbox.id)):
            with self.assertRaisesRegex(SandboxError, 'ownership has been retired'):
                operation()
        self.assertEqual(self.commands, before)
        self.assertTrue(process.alive)
        self.assertEqual(restored.sandboxes[sandbox.id].state, 'RUNNING')
        first.close()
        self.assertTrue(process.alive)

    def test_closed_registry_cannot_be_reused_to_bypass_directory_ownership(self):
        registry = SandboxRegistry(self.root/'instances', self.operations)
        registry.close()
        with self.assertRaisesRegex(SandboxError, 'ownership has been retired'):
            open_edge(registry.root, self.binary, self.kernel, self.template, registry=registry)
        self.assertEqual(self.boots, 0)
        self.assertEqual(list(registry.root.glob('*/registry.json')), [])

    def test_legacy_constructor_uses_the_same_composed_registry_and_recovery(self):
        # Only fake the Linux boot-id read on Mac. Native pidfd/process probing
        # remains untested here; the constructor receives our attested host stub.
        saved = {name:sys.modules.get(name) for name in ('dsec.runtime.registry','durable_manager')}
        package = importlib.import_module('dsec.runtime')
        saved_attribute = getattr(package, 'registry', None)
        read_text = Path.read_text
        def read(path, *args, **kwargs):
            if sys.platform != 'linux' and str(path) == '/proc/sys/kernel/random/boot_id':
                return 'test-boot\n'
            return read_text(path, *args, **kwargs)
        try:
            with patch.object(Path, 'read_text', read):
                native = importlib.import_module('dsec.runtime.registry')
            with patch.object(native, 'create_registry',
                    side_effect=lambda root: SandboxRegistry(root, self.operations)):
                legacy = native.DurableManager(self.root/'instances', self.binary,
                                              self.kernel, self.template)
            self.instances.append(legacy)
            self.assertIs(importlib.import_module('durable_manager'), native)
            self.assertIs(legacy.registry_lock, legacy.registry.owner_lock)
            sandbox = legacy.create()
            legacy.detach()
            restored = self.open()
            self.assertEqual(restored.sandboxes[sandbox.id].state, 'RUNNING')
            self.assertEqual(self.boots, 1)
        finally:
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module
            if saved_attribute is None:
                if hasattr(package, 'registry'):
                    delattr(package, 'registry')
            else:
                package.registry = saved_attribute


if __name__ == '__main__':
    unittest.main()
