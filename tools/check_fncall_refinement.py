"""Require controlled FnCall regressions to reject reviewed code mutations.

FnCallSlot is checked by check_concurrency_model.py. This gate binds named code
boundaries to safety properties and fingerprints their sources. It is not an
automatic simulation or liveness proof.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'),str(ROOT)]
from dsec.runtime.backends import docker_guest
from tests.unit.test_qemu_container import GuestTests

CONTROLS = (
    ('early-ready','ReadyIsClean','test_cleanup_is_a_linearization_boundary_not_a_reusable_slot',
     "if self.config['mode'] == 'fncall':\n            self.save_state('CLEANING')",
     "if False:\n            self.save_state('CLEANING')",'execute'),
    ('unknown-replay','UnknownNeverReplayed','test_unknown_intent_is_not_executed_again',
     "if path.exists():\n                record = json.loads(path.read_text())",
     "if False:\n                record = json.loads(path.read_text())",'dispatch'),
    ('failed-cleanup-reuse','ReadyIsClean','test_failed_cleanup_never_reuses_slot_or_returns_success',
     " or self.failed:", ":",'status'),
)


def run(out):
    out.mkdir(mode=0o700,parents=True,exist_ok=False)
    source = Path(docker_guest.__file__).read_text()
    results = []
    for name,property_name,method,before,after,target in CONTROLS:
        case = out/name; case.mkdir()
        if source.count(before) != 1:
            raise ValueError('Code/model boundary changed; review '+name)
        def check():
            log = io.StringIO()
            result = unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.TestSuite([GuestTests(method)]))
            return result,log.getvalue()
        safe,safe_log = check(); (case/'safe.log').write_text(safe_log)
        namespace = dict(docker_guest.__dict__)
        mutant = source.replace(before,after,1)
        (case/'mutant.py').write_text(mutant)
        exec(compile(mutant,str(case/'mutant.py'),'exec'),namespace)
        with patch.object(docker_guest.Supervisor,target,getattr(namespace['Supervisor'],target)):
            unsafe,unsafe_log = check()
        (case/'unsafe.log').write_text(unsafe_log)
        passed = (safe.wasSuccessful() and not safe.skipped and len(unsafe.failures)==1
                  and not unsafe.errors and not unsafe.skipped)
        results.append(dict(case=name,property=property_name,regression=method,passed=passed))
        print(json.dumps(results[-1]),flush=True)
    sources = ('src/dsec/runtime/backends/docker_guest.py','src/dsec/runtime/backends/qemu_container.py',
        'tests/unit/test_qemu_container.py','verification/FnCallSlot.tla','verification/FnCallSlot.cfg')
    report = dict(checks=results,source_sha256={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
        for name in sources},boundary=__doc__)
    (out/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    return all(r['passed'] for r in results)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    args = parser.parse_args()
    raise SystemExit(0 if run(args.out.resolve()) else 1)
