"""Rolling overlays pin only API images and preserve the original deployment."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy.cluster.upgrade import context, overlays
from dsec.contracts.release import COMPATIBILITY
from tests.unit import test_release_artifacts as fixtures


class ClusterUpgradeTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.deployment=self.root/'deployment';(self.deployment/'image').mkdir(parents=True)
        (self.deployment/'config').mkdir();(self.deployment/'config/controller.json').write_text('{"node_lifecycle": true}')
        (self.deployment/'compose.yaml').write_text('services: {}')
        for name in ('Dockerfile','initialize.py'):(self.deployment/'image'/name).write_text('# fixture\n')
        (self.deployment/'deployment.json').write_text(json.dumps(dict(python_image='python@sha256:'+'a'*64,
            source_sha256={name:hashlib.sha256((self.deployment/'image'/name).read_bytes()).hexdigest() for name in ('Dockerfile','initialize.py')})))
        self.source=dict(build='a'*64,version='0.1.dev0',compatibility=COMPATIBILITY)
        self.target=dict(build='b'*64,version='0.1.dev1',compatibility=COMPATIBILITY)

    def test_offline_context_has_no_credentials_and_never_changes_master(self):
        wheel,sha=fixtures.ReleaseArtifactTests().wheel(self.root)
        before={str(p):p.read_bytes() for p in self.deployment.rglob('*') if p.is_file()}
        with patch('subprocess.run',side_effect=AssertionError('no Docker or package processes')):
            result=context(self.deployment,wheel,sha,self.root/'image')
        self.assertFalse(result['services_changed'])
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.deployment.rglob('*') if p.is_file()})
        self.assertEqual(set(p.name for p in (self.root/'image').iterdir()),{'Dockerfile','initialize.py','release.json','build.json','wheels'})

    def test_one_replica_overlays_pin_forward_and_rollback_without_etcd_or_volume_changes(self):
        result=overlays(self.deployment,self.source,self.target,'sha256:'+'a'*64,'sha256:'+'b'*64,self.root/'plan')
        self.assertFalse(result['services_changed'])
        for role in ('api1','api2'):
            value=json.loads((self.root/'plan'/('upgrade-'+role+'.json')).read_text())
            self.assertEqual(value,dict(services={role:dict(image='sha256:'+'b'*64,pull_policy='never')}))
            value=json.loads((self.root/'plan'/('rollback-'+role+'.json')).read_text())
            self.assertEqual(value['services'][role]['image'],'sha256:'+'a'*64)
        bad=deepcopy(self.target);bad['compatibility']['coordination']=2
        with self.assertRaises(ValueError):overlays(self.deployment,self.source,bad,'sha256:'+'a'*64,'sha256:'+'b'*64,self.root/'bad')
        self.assertFalse((self.root/'bad').exists())


if __name__=='__main__':unittest.main()
