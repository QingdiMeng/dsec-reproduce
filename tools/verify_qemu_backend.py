"""Manual Linux/KVM acceptance; at most two VMs with --cid-gates, no network.

Requires an administrator-prepared artifact manifest and reserved host-wide CID.
Does not install software, edit host policies, call Docker, or claim production.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import threading
import uuid

from dsec.contracts.sandbox import UnsupportedCapability
from dsec.runtime.backends.qemu import QemuArtifact, QemuConfig, QemuVM
from dsec.runtime.sessions.native import NativeSessionReset
from dsec.runtime.isolation.vsock import CIDLeaseConflict, QemuCIDPool


def configuration(manifest, guest_cid):
    artifacts = json.loads(manifest.read_text())
    return QemuConfig(*(QemuArtifact(**artifacts[name]) for name in ('binary', 'kernel', 'template')),
                      guest_cid=guest_cid)


def crash_before_record(root, manifest, guest_cid, cid_pool=None):
    root.mkdir(mode=0o700, exist_ok=False)
    if cid_pool is not None:
        lease = QemuCIDPool(cid_pool, first=guest_cid, last=guest_cid+1).reserve(root)
        guest_cid = lease.cid
    vm = QemuVM(configuration(manifest, guest_cid), root)
    def crash(stage):
        assert stage == 'spawned_before_pid_record'
        print(json.dumps({'boundary':stage, 'vmm_pid':vm.child.pid}), flush=True)
        os.kill(os.getpid(), signal.SIGKILL)
    vm._checkpoint = crash
    vm.boot()
    raise AssertionError('Controller crash boundary was not reached')


def verify_cid_gates(root, manifest, guest_cid, passed):
    pool_root = root/'cid-pool'; pool_root.mkdir(mode=0o700)
    pool = QemuCIDPool(pool_root, first=guest_cid, last=guest_cid+1)
    entries, detached = [], []
    try:
        directories = [root/name for name in ('cid-a', 'cid-b', 'cid-c')]
        for directory in directories:
            directory.mkdir(mode=0o700)
        barrier = threading.Barrier(2)
        def reserve(directory):
            peer = QemuCIDPool(pool_root, first=guest_cid, last=guest_cid+1)
            barrier.wait(timeout=5)
            return peer.reserve(directory)
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(reserve, directory) for directory in directories[:2]]
            leases = [future.result(timeout=10) for future in futures]
        assert len({lease.cid for lease in leases}) == 2
        for lease in leases:
            entries.append((lease, QemuVM(configuration(manifest, lease.cid), lease.directory)))
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(vm.boot) for _, vm in entries]
            for future in futures:
                assert future.result(timeout=30)['state'] == 'RUNNING'
        for name, (_, vm) in zip(('a', 'b'), entries):
            assert vm.execute(f'printf {name} > /tmp/cid-owner; cat /tmp/cid-owner')['output'] == name
        passed('concurrent_cid_reservations_route_to_distinct_live_guests', cids=[l.cid for l in leases])
        pool = QemuCIDPool(pool_root, first=guest_cid, last=guest_cid+1)
        assert [pool.reserve(directory) for directory in directories[:2]] == leases
        try:
            pool.reserve(directories[2]); raise AssertionError('Live CID reused')
        except CIDLeaseConflict:
            pass
        passed('pool_restart_retains_live_leases_and_exhaustion')
        lease, original = entries[0]; original.detach(); detached.append(original)
        adopted = QemuVM(configuration(manifest, lease.cid), lease.directory)
        entries[0] = (lease, adopted)
        assert adopted.adopt()['state'] == 'RUNNING'
        pool.retire(lease, adopted)
        replacement = pool.reserve(directories[2])
        assert replacement.cid == lease.cid and replacement.token != lease.token
        new_vm = QemuVM(configuration(manifest, replacement.cid), replacement.directory)
        entries.append((replacement, new_vm)); new_vm.boot()
        try:
            pool.retire(lease, adopted); raise AssertionError('Stale CID retirement accepted')
        except CIDLeaseConflict:
            pass
        assert new_vm.execute('printf c > /tmp/cid-owner; cat /tmp/cid-owner')['output'] == 'c'
        assert entries[1][1].execute('cat /tmp/cid-owner')['output'] == 'b'
        passed('attested_retirement_and_token_fence_preserve_peer')
        adopted.detach(); original.child.wait(timeout=5)
        entries.pop(0)
        for live_lease, vm in entries:
            pool.retire(live_lease, vm); vm.detach()
        entries.clear()
        crash_root = root/'cid-crash'
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()),
            '--root', str(crash_root), '--manifest', str(manifest.resolve()),
            '--guest-cid', str(guest_cid), '--cid-pool', str(pool_root), '--crash-before-record'],
            env=dict(os.environ), capture_output=True, text=True, timeout=30)
        (root/'cid-controller-crash.stdout').write_text(child.stdout)
        (root/'cid-controller-crash.stderr').write_text(child.stderr)
        assert child.returncode == -signal.SIGKILL, child
        observation = json.loads(child.stdout)
        restored = QemuCIDPool(pool_root, first=guest_cid, last=guest_cid+1)
        lease = restored.reserve(crash_root)
        vm = QemuVM(configuration(manifest, lease.cid), crash_root); entries.append((lease, vm))
        from dsec.runtime.registry import identity, AttachedProcess
        record = json.loads(vm.record_path.read_text())
        orphan = AttachedProcess(identity(observation['vmm_pid']), vm.config.binary.path,
                                 vm.qmp.endpoint, expected_argv=record['argv'])
        try:
            assert vm.adopt()['state'] == 'FAILED'
            assert select.select([orphan.fd], [], [], 5)[0], 'Unrecorded VMM survived'
            assert str(lease.cid) in restored.snapshot(), 'Crash released CID without retirement'
            restored.retire(lease, vm); vm.detach(); entries.clear()
        finally:
            if orphan.poll() is None:
                orphan.terminate()
                try:
                    orphan.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    orphan.kill(); orphan.wait(timeout=5)
            orphan.close()
        assert restored.snapshot() == {}
        passed('real_controller_crash_retains_cid_until_attested_recovery', observation=observation)
        passed('all_pooled_vm_deaths_proved_and_cids_returned', leases=restored.snapshot())
    finally:
        errors = []
        for lease, vm in entries:
            try:
                # A completed retirement may already have removed the lease.
                if str(lease.cid) in pool.snapshot():
                    pool.retire(lease, vm)
                else:
                    vm.stop()
            except BaseException as exc:
                errors.append(repr(exc))
            finally:
                vm.detach()
        for vm in detached:
            if vm.child is not None:
                try:
                    vm.child.wait(timeout=5)
                except BaseException as exc:
                    errors.append(repr(exc))
        if errors:
            raise RuntimeError('CID acceptance cleanup failed: ' + '; '.join(errors))


def verify(root, manifest, guest_cid, *, fault_gates=False, cid_gates=False):
    root = root.resolve()
    root.mkdir(mode=0o700, exist_ok=False)
    report = dict(status='running', scope='private-qemu-primitive', production=False,
                  checks=[], cleanup_errors=[])
    output = root/'report.json'
    vm = retired = None
    def save():
        output.write_text(json.dumps(report, indent=2)+'\n')
    def passed(name, **details):
        report['checks'].append(dict(name=name, **details)); save()
    try:
        config = configuration(manifest, guest_cid)
        report['configuration'] = asdict(config)
        source_manifest = Path(__file__).resolve().parents[1]/'SOURCE_MANIFEST.json'
        if source_manifest.exists():
            report['source_manifest_sha256'] = hashlib.sha256(source_manifest.read_bytes()).hexdigest()
        vm = QemuVM(config, root)
        status = vm.boot()
        assert status['state'] == 'RUNNING' and status['process_alive'], status
        passed('kvm_boot_and_native_readiness', status=status)
        result = vm.execute('printf legacy-qemu')
        assert result['exit_code'] == 0 and result['output'] == 'legacy-qemu', result
        passed('legacy_guest_wire')
        sid = uuid.uuid4().hex
        vm.native.call('open', session_id=sid)
        result = vm.native.call('run', session_id=sid, command='cd /tmp; export QEMU_STATE=kept')
        assert result['exit_code'] == 0, result
        result = vm.native.call('run', session_id=sid, command='printf "%s:%s" "$PWD" "$QEMU_STATE"')
        assert result['stdout'] == '/tmp:kept', result
        passed('persistent_chronus_shell')
        roles = vm.native.call('run', session_id=sid,
            command='for p in /proc/[0-9]*/comm; do cat "$p" 2>/dev/null || :; done')
        assert 'dsec-aether' in roles['stdout'] and 'dsec-chronus' in roles['stdout'], roles
        passed('aether_chronus_process_roles')
        data = b'\x00\xffQEMU-file'
        transfer = uuid.uuid4().hex
        values = dict(session_id=sid, path='/tmp/qemu-scoped', transfer_id=transfer,
                      total=len(data), mode=0o600)
        vm.native.call('write_begin', **values)
        vm.native.call('write_chunk', **values, offset=0, data=base64.b64encode(data).decode())
        vm.native.call('write_commit', **values)
        read = vm.native.call('read', session_id=sid, path=values['path'], length=1024,
                              transfer_id=uuid.uuid4().hex)
        assert base64.b64decode(read['data']) == data, read
        passed('chronus_binary_files')
        events = list(vm.native.stream(session_id=sid, command='printf out; printf err >&2',
                                      operation_id=uuid.uuid4().hex))
        assert b''.join(e['data'] for e in events if e['type']=='stdout') == b'out', events
        assert b''.join(e['data'] for e in events if e['type']=='stderr') == b'err', events
        assert events[-1]['type']=='result' and events[-1]['result']['exit_code']==0, events
        passed('native_stdout_stderr_stream')
        assert vm.pause()['state']=='PAUSED'
        assert vm.qmp.execute('query-status')['running'] is False
        assert vm.resume()['state']=='RUNNING'
        result = vm.native.call('run', session_id=sid, command='printf "$QEMU_STATE"')
        assert result['stdout']=='kept', result
        passed('qmp_pause_resume_keeps_chronus_state')
        vm.native.call('close', session_id=sid)
        try:
            vm.native.call('read', session_id=sid, path='/tmp/qemu-scoped', transfer_id=uuid.uuid4().hex)
            raise AssertionError('Closed session performed read')
        except NativeSessionReset:
            pass
        passed('closed_session_files_rejected')
        try:
            vm.checkpoint()
            raise AssertionError('Firecracker checkpoint falsely accepted')
        except UnsupportedCapability:
            pass
        passed('unsupported_checkpoint_rejected')
        retired = vm
        retired.detach()
        vm = QemuVM(config, root)
        status = vm.adopt()
        assert status['state']=='RUNNING' and status['process_alive'], status
        assert vm.execute('printf adopted')['output']=='adopted'
        passed('pidfd_attested_process_adoption')
        status = vm.stop()
        assert status['state']=='STOPPED' and not status['process_alive'], status
        if fault_gates:
            vm.detach(); retired.child.wait(timeout=5)
            fault_root = root/'abrupt-vmm'; fault_root.mkdir(mode=0o700)
            vm = QemuVM(config, fault_root)
            vm.boot(); vm.process.kill(); vm.process.wait(timeout=5)
            assert vm.status()['state']=='FAILED'
            try:
                vm.execute('printf must-not-run')
                raise AssertionError('Dead VMM accepted command')
            except RuntimeError:
                pass
            vm.stop(); vm.detach()
            passed('abrupt_vmm_death_fences_execution')
            fault_root = root/'lost-control'; fault_root.mkdir(mode=0o700)
            vm = QemuVM(config, fault_root); vm.boot()
            execute = vm.qmp.execute
            calls = []
            def lose(command):
                calls.append(command); execute(command)
                raise EOFError('Controlled loss after QMP stop effect')
            vm.qmp.execute = lose
            try:
                vm.pause(); raise AssertionError('Lost pause reply accepted')
            except EOFError:
                pass
            assert json.loads(vm.record_path.read_text())['state']=='PAUSING'
            retired = vm; retired.detach()
            vm = QemuVM(config, fault_root)
            status = vm.adopt()
            assert status['state']=='FAILED' and not status['process_alive'], status
            assert calls == ['stop'], calls
            vm.stop(); vm.detach(); retired.child.wait(timeout=5)
            passed('lost_control_reply_retires_without_replay')
            fault_root = root/'fork-gap'
            child = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                '--root', str(fault_root), '--manifest', str(manifest.resolve()),
                '--guest-cid', str(guest_cid), '--crash-before-record'],
                env=dict(os.environ), capture_output=True, text=True, timeout=30)
            (root/'controller-crash.stdout').write_text(child.stdout)
            (root/'controller-crash.stderr').write_text(child.stderr)
            assert child.returncode == -signal.SIGKILL, child
            observation = json.loads(child.stdout)
            assert observation['boundary']=='spawned_before_pid_record', observation
            record = json.loads((fault_root/'qemu.json').read_text())
            assert record['state']=='STARTING' and record['process'] is None, record
            vm = QemuVM(config, fault_root)
            # Open/attest this exact orphan before recovery. PID replacement
            # must not turn an unrelated exit into cleanup evidence.
            from dsec.runtime.registry import identity, AttachedProcess
            saved = identity(observation['vmm_pid'])
            orphan = AttachedProcess(saved, config.binary.path, vm.qmp.endpoint,
                                     expected_argv=record['argv'])
            try:
                status = vm.adopt()
                assert status['state']=='FAILED' and not status['process_alive'], status
                assert select.select([orphan.fd], [], [], 5)[0], 'Unregistered VMM survived'
                try:
                    vm.boot(); raise AssertionError('Interrupted boot replayed')
                except RuntimeError:
                    pass
                passed('real_controller_crash_recovers_unrecorded_vmm', observation=observation)
            finally:
                if orphan.poll() is None:
                    orphan.terminate()
                    try:
                        orphan.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        orphan.kill(); orphan.wait(timeout=5)
                orphan.close()
        if cid_gates:
            if not vm.owner.closed:
                vm.stop(); vm.detach()
            if retired is not None and retired.child is not None:
                retired.child.wait(timeout=5)
            verify_cid_gates(root, manifest, guest_cid, passed)
        report['status'] = 'passed'
    except BaseException as exc:
        report.update(status='failed', error=dict(type=type(exc).__name__, message=str(exc)))
        raise
    finally:
        if vm is not None and not vm.owner.closed:
            try:
                stopped = vm.stop()
                assert stopped['state']=='STOPPED' and not stopped['process_alive'], stopped
                passed('owned_vmm_stopped', status=stopped)
            except BaseException as exc:
                report['cleanup_errors'].append(repr(exc)); report['status']='failed'
            finally:
                vm.detach()
        if retired is not None and retired.child is not None:
            try:
                retired.child.wait(timeout=5)
            except BaseException as exc:
                report['cleanup_errors'].append(repr(exc)); report['status']='failed'
        save()
    if report['status'] != 'passed':
        raise RuntimeError('QEMU acceptance cleanup failed; inspect report.json')
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', required=True, type=Path)
    p.add_argument('--manifest', required=True, type=Path)
    p.add_argument('--guest-cid', required=True, type=int)
    p.add_argument('--fault-gates', action='store_true')
    p.add_argument('--cid-gates', action='store_true', help='Reserve two adjacent administrator CIDs')
    p.add_argument('--cid-pool', type=Path, help=argparse.SUPPRESS)
    p.add_argument('--crash-before-record', action='store_true', help=argparse.SUPPRESS)
    a = p.parse_args()
    if a.crash_before_record:
        crash_before_record(a.root, a.manifest, a.guest_cid, a.cid_pool)
        return
    report = verify(a.root, a.manifest, a.guest_cid, fault_gates=a.fault_gates, cid_gates=a.cid_gates)
    print(json.dumps({'status':report['status'], 'checks':len(report['checks']), 'root':str(a.root)}))


if __name__ == '__main__':
    main()
