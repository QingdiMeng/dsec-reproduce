"""Bind reviewed command-recovery guards to controlled regressions and TLA+.

Each deliberate guard removal must produce a test assertion failure. Source
fingerprints require review when implementation/model/tests change. This is a
bounded correspondence check, not an automatic refinement or liveness proof.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from dsec.runtime.backends import qemu_container
from tests.unit.test_qemu_commands import CommandTests


def run(out):
    out.mkdir(mode=0o700,parents=True,exist_ok=False)
    source=Path(qemu_container.__file__).read_text()
    controls=[
        ('matching-digest','DoneRequiresExactProof',"proof.get('digest')==digest and",'True and','query_request','test_restart_retains_unknown_until_matching_terminal_proof'),
        ('matching-request','DoneRequiresExactProof',"proof.get('request_id')==request_id and",'True and','query_request','test_restart_retains_unknown_until_matching_terminal_proof'),
        ('terminal-proof','DoneRequiresExactProof',"proof.get('state')=='DONE' and",'True and','query_request','test_restart_retains_unknown_until_matching_terminal_proof'),
    ]
    start=source.index("        try:\n            proof = self.control.call('execute'",source.index('class QemuContainer:'))
    end=source.index('\n    def query_request',start)
    boundary=source[start:end]
    blocking='        with self.vm.lock:\n'+'\n'.join('    '+line if line else line for line in boundary.split('\n'))
    controls.append(('inspection-lock','InspectionDoesNotQueue',boundary,blocking,'run_shell',
                     'test_inspection_does_not_wait_for_host_command_lock'))
    checks=[]
    def test(method):
        log=io.StringIO();result=unittest.TextTestRunner(stream=log).run(unittest.TestSuite([CommandTests(method)]))
        return result,log.getvalue()
    for name,property_name,before,after,target,method in controls:
        if source.count(before)!=1:raise ValueError('Review changed command/model guard: '+name)
        case=out/name;case.mkdir();safe,safe_log=test(method)
        namespace=dict(qemu_container.__dict__);mutant=source.replace(before,after,1)
        exec(compile(mutant,str(case/'mutant.py'),'exec'),namespace)
        (case/'mutant.py').write_text(mutant)
        with patch.object(qemu_container.QemuContainer,target,getattr(namespace['QemuContainer'],target)):
            unsafe,unsafe_log=test(method)
        (case/'safe.log').write_text(safe_log);(case/'unsafe.log').write_text(unsafe_log)
        checks.append(dict(guard=name,property=property_name,test=method,passed=safe.wasSuccessful() and not safe.skipped and len(unsafe.failures)==1 and not unsafe.errors))
    sources=('src/dsec/runtime/backends/qemu_container.py','src/dsec/runtime/backends/docker_guest.py',
        'src/dsec/runtime/requests.py','tests/unit/test_qemu_commands.py',
        'verification/QemuCommandRecovery.tla','verification/QemuCommandRecovery.cfg')
    report=dict(checks=checks,source_sha256={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources},boundary=__doc__)
    (out/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(checks));return all(item['passed'] for item in checks)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--out',type=Path,required=True)
    raise SystemExit(0 if run(parser.parse_args().out.resolve()) else 1)
