"""Bind QEMU host-gate safety to controlled regressions and required mutants."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import types
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'compat/python'),str(ROOT)]
from tools.check_cluster_refinement import run
from dsec.host import qemu_profile_helper, qemu_network_helper
from dsec.runtime.isolation import qemu_host
from tests.unit.test_qemu_host_isolation import BrokerTests,CgroupTests,HostBootTests
from tests.unit.test_qemu_network import NetworkBrokerTests,NetworkGateTests,CommandFenceTests
from dsec.runtime.backends import qemu


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,required=True)
    root=parser.parse_args().root.resolve();root.mkdir(parents=True,exist_ok=False)
    report=dict(status='failed',canonical=[],controls=[],boundary='Bounded guard correspondence, not whole-program refinement')
    try:
        for cls in (BrokerTests,CgroupTests,HostBootTests,NetworkBrokerTests,NetworkGateTests,CommandFenceTests):
            result=run(root/cls.__name__,test_class=cls)
            if not result.wasSuccessful() or result.skipped:raise AssertionError('Canonical host gates failed')
            report['canonical'].append(dict(suite=cls.__name__,tests=result.testsRun))
        controls=(
            ('network-command-lock',qemu_network_helper,None,'run',
             "return subprocess.check_output(args,text=True,stderr=subprocess.PIPE,timeout=15,pass_fds=(() if LOCK_FD is None else (LOCK_FD,)))",
             "return subprocess.check_output(args,text=True,stderr=subprocess.PIPE,timeout=15)",
             CommandFenceTests,'test_broker_death_does_not_unlock_an_inflight_mutation','RetiredNeverReopens'),
            ('network-live-teardown',qemu_network_helper,None,'retire',
             "if record.get('process'):prove_dead(record['process'])","if False:prove_dead(record['process'])",
             NetworkBrokerTests,'test_live_process_never_loses_network_or_rules','LiveNeedsNetwork'),
            ('network-late-claim',qemu_network_helper,None,'claim',
             "record['state'] not in ('LENT','CLAIMED')","record['state'] not in ('LENT','CLAIMED','RETIRING')",
             NetworkBrokerTests,'test_retirement_fences_claim_before_teardown_and_failure_keeps_fence','RetiredNeverReopens'),
            ('network-repeat-lend',qemu_network_helper,None,'lend',
             "if record['state']!='READY':raise RuntimeError('TAP descriptor already lent or retired; no replay')",
             "if False:raise RuntimeError('TAP descriptor already lent or retired; no replay')",
             NetworkBrokerTests,'test_descriptor_delivery_is_one_shot_and_never_replays_after_retirement','OneShotDescriptor'),
            ('network-unattested-cont',qemu,'QemuVM','boot',
             "if self.network is not None:self.network.attest()\n                self.qmp.execute('cont')",
             "if False:self.network.attest()\n                self.qmp.execute('cont')",
             NetworkGateTests,'test_network_attestation_must_pass_before_guest_continuation','ActiveHasPolicy'),
            ('live-unload',qemu_profile_helper,None,'operate',
             "if old and old['state']=='CLAIMED':prove_dead(old['process'])","if False:prove_dead(old['process'])",
             BrokerTests,'test_claimed_process_must_die_before_profile_unload','RunningIsConfined'),
            ('late-claim',qemu_profile_helper,None,'operate',
             "old['state'] not in ('PREPARED','CLAIMED')","old['state'] not in ('PREPARED','CLAIMED','RETIRED')",
             BrokerTests,'test_retirement_fences_a_delayed_claim_before_unload_finishes','RunningIsConfined'),
            ('outside-group',qemu_host,'HostIsolation','attest',
             "if (proc/'cgroup').read_text().strip()!='0::/'+str(self.group.relative_to('/sys/fs/cgroup')):","if False:",
             CgroupTests,'test_attestation_rejects_a_vmm_outside_its_group','RunningIsConfined'),
        )
        for name,module,cls,method,before,after,test_class,test,invariant in controls:
            source=Path(module.__file__).read_text()
            if source.count(before)!=1:raise AssertionError('Host model/code mapping changed: '+name)
            case=root/name;case.mkdir();path=case/'mutated.py';source=source.replace(before,after)
            path.write_text(source);namespace=dict(module.__dict__);exec(compile(source,str(path),'exec'),namespace)
            target=getattr(module,cls) if cls else module
            replacement=getattr(namespace[cls],method) if cls else namespace[method]
            replacement=types.FunctionType(replacement.__code__,module.__dict__,replacement.__name__,
                                           replacement.__defaults__,replacement.__closure__)
            with patch.object(target,method,replacement):result=run(case/'test',test,test_class=test_class)
            passed=bool(result.failures) and not result.errors and not result.skipped
            report['controls'].append(dict(name=name,invariant=invariant,regression=test,passed=passed))
            if not passed:raise AssertionError('Host guard mutant was not caught: '+name)
        report['status']='passed'
    finally:
        sources=['src/dsec/runtime/isolation/qemu_network.py','src/dsec/host/qemu_network_helper.py',
            'src/dsec/runtime/isolation/network_policy.py','verification/QemuNetworkLease.tla',
            'verification/QemuNetworkLease.cfg','tests/unit/test_qemu_network.py',
            'src/dsec/runtime/isolation/qemu_host.py','src/dsec/runtime/isolation/qemu_launch.py',
            'src/dsec/host/qemu_profile_helper.py','src/dsec/host/qemu_privileges.py',
            'src/dsec/runtime/backends/qemu.py','tests/unit/test_qemu_host_isolation.py',
            'verification/QemuHostGate.tla','verification/QemuHostGate.cfg','tools/check_host_isolation_refinement.py']
        report['source_sha256']={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources}
        (root/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(dict(status=report['status'],controls=len(report['controls']))))


if __name__=='__main__':main()
