"""Bind rotation safety models to canonical tests and required guard removals."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'compat/python'),str(ROOT)]
from tools.check_cluster_refinement import run
from dsec.control import credential_rotation
from dsec import _tls, _security_files
from dsec.host import security_publication
from tests.unit.test_credential_rotation import CredentialRotationTests
from tests.unit.test_security_publication import SecurityPublicationTests
from tests.unit.test_tls_rotation import TLSRotationTests


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,required=True);args=parser.parse_args()
    root=args.root.resolve();root.mkdir(parents=True,exist_ok=False)
    report=dict(status='failed',canonical=[],controls=[],scope='Targeted code guard correspondence, not whole-program refinement or HA proof')
    try:
        for cls in (CredentialRotationTests,SecurityPublicationTests,TLSRotationTests):
            result=run(root/cls.__name__,test_class=cls)
            if not result.wasSuccessful() or result.skipped:raise AssertionError('Canonical security tests must pass without skips')
            report['canonical'].append(dict(suite=cls.__name__,tests=result.testsRun))
        controls=(
            ('unproved_completion',credential_rotation,None,'rotate',
             [("if caller!=old[expected]:","if False:")],CredentialRotationTests,
             'test_overlap_and_completion_prove_the_new_token_without_storing_secrets','CompletionHasNewProof'),
            ('stale_cancel',credential_rotation,None,'rotate',
             [("if caller!=old[expected]:","if False:"),("if old['state']!='OVERLAP':","if False:")],CredentialRotationTests,
             'test_completed_rotation_cannot_be_cancelled_by_delayed_credentials','NoCredentialLockout'),
            ('stale_peer',_tls,'TLSFiles','fence',
             [("if current.trust!=view.trust:","if False:"),
              ("if current.peers and (not peer or hashlib.sha256(peer).hexdigest() not in current.peers):","if False:")],TLSRotationTests,
             'test_connection_authenticated_before_retirement_cannot_submit_a_new_effect_after_it','TrustedAtAcceptance'),
            ('stale_generation',_security_files,'VersionedFile','read',
             [("if generation<self.generation or (generation==self.generation and digest!=self.digest):","if False:")],SecurityPublicationTests,
             'test_node_overlap_cutover_and_retirement_do_not_change_existing_route_integrity','PublicationNeverRegresses'),
            ('publication_cas',security_publication,None,'publish',
             [("if (old['generation'] if old else 0)!=expected:","if False:")],SecurityPublicationTests,
             'test_competing_publishers_cannot_overwrite_the_winning_generation','PublicationNeverRegresses'),
        )
        for name,module,cls,method,replacements,test_class,test,invariant in controls:
            source=Path(module.__file__).read_text()
            for before,after in replacements:
                if source.count(before)!=1:raise AssertionError('Security mapping changed: '+name)
                source=source.replace(before,after,1)
            case=root/name;case.mkdir();path=case/'mutated.py';path.write_text(source)
            namespace=dict(module.__dict__);exec(compile(source,str(path),'exec'),namespace)
            target=getattr(module,cls) if cls else module
            replacement=getattr(namespace[cls],method) if cls else namespace[method]
            with patch.object(target,method,replacement):result=run(case/'test',test,test_class=test_class)
            passed=len(result.failures)==1 and not result.errors and not result.skipped
            report['controls'].append(dict(name=name,invariant=invariant,regression=test,passed=passed))
            if not passed:raise AssertionError('Security guard removal did not cause its required behavioral failure: '+name)
        report['status']='passed'
    finally:
        sources=['src/dsec/control/credential_rotation.py','src/dsec/control/iam.py','src/dsec/control/cluster_api.py',
                 'src/dsec/_credentials.py','src/dsec/_tls.py','src/dsec/_security_files.py','src/dsec/host/security_publication.py',
                 'src/dsec/host/cluster_http.py','src/dsec/control/etcd_auth.py','src/dsec/control/coordination.py',
                 'tests/unit/test_credential_rotation.py','tests/unit/test_security_publication.py','tests/unit/test_tls_rotation.py',
                 'verification/CredentialRotation.tla','verification/CredentialRotation.cfg',
                 'verification/TransportRotation.tla','verification/TransportRotation.cfg','tools/check_security_refinement.py']
        report['source_sha256']={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources}
        (root/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(dict(status=report['status'],controls=len(report['controls']))))


if __name__=='__main__':main()
