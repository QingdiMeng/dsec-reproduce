"""Environment publication and transport contracts; no live guest attestation."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dsec.contracts.environments import EnvironmentView, artifact_revision, published_environment
from dsec.contracts.native import NATIVE_FEATURE, STREAM_FEATURE, execution_requirements
from dsec.contracts.cluster import NodeView, Quota
from dsec.host.node_transport import RemoteNode
from dsec.storage.catalog import MicroVMEnvironmentCatalog
from dsec.runtime.backends.qemu_container import ExecutionCatalog


def claim(item, capabilities=(STREAM_FEATURE,)):
    return dict(item,execution_contract=dict(schema=1,artifact_revision=artifact_revision(item),
                                             capabilities=list(capabilities)))


class EnvironmentRequirementTests(unittest.TestCase):
    def test_unknown_is_not_inferred_and_claim_is_bound_to_artifacts(self):
        item = dict(template={'path':'/private/guest','sha256':'a'*64},mode='container')
        unknown = published_environment('coding','container','container',item)
        self.assertEqual(unknown.capabilities,())
        known = published_environment('coding','container','container',claim(item))
        self.assertEqual(known.capabilities,execution_requirements([STREAM_FEATURE]))
        self.assertNotEqual(unknown.revision,known.revision)
        changed = claim(item)
        changed['template'] = dict(item['template'],sha256='b'*64)
        with self.assertRaisesRegex(ValueError,'pinned environment'):
            published_environment('coding','container','container',changed)
        self.assertNotIn('/private',json.dumps(known.as_dict()))

    def test_invalid_claims_and_fncall_state_promises_fail_closed(self):
        item = dict(template={'sha256':'a'*64})
        for update in ({'schema':True},{'schema':2},{'artifact_revision':'b'*64},
                       {'capabilities':['model-kv-cache']},{'capabilities':None},{'extra':'ignored'}):
            bad = claim(item)
            bad['execution_contract'].update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                published_environment('coding','microvm','microvm',bad)
        with self.assertRaisesRegex(ValueError,'Function invocations'):
            published_environment('function','container','fncall',claim(item))

    def test_node_wire_roundtrip_preserves_environment_and_legacy_has_no_claims(self):
        contract = published_environment('coding','microvm','microvm',claim({'boot_sha256':'a'*64}))
        view = NodeView('n1','boot',('microvm',),('coding',),(),0,Quota(1,1,512,1024,0),0,(contract,))
        class RPC:
            def send(self,*args,**kwargs):
                return json.loads(json.dumps(asdict(view)))
        remote = RemoteNode(RPC(),token='internal',route_key=b'k'*32)
        self.assertEqual(remote.probe(),view)
        class OldRPC:
            def send(self,*args,**kwargs):
                value = asdict(view); value.pop('environment_contracts')
                return value
        self.assertEqual(RemoteNode(OldRPC(),token='internal',route_key=b'k'*32).probe(),
                         replace(view,environment_contracts=()))
        with self.assertRaisesRegex(ValueError,'Ambiguous'):
            replace(view,environment_contracts=(contract,contract))
        with self.assertRaisesRegex(ValueError,'advertised'):
            replace(view,environment_contracts=(replace(contract,environment_id='other'),))

    def test_qemu_catalog_validates_claim_and_rejects_stale_descriptor(self):
        with tempfile.TemporaryDirectory() as work, patch('dsec.runtime.backends.qemu_container.QemuConfig.validate'):
            root = Path(work).resolve()
            pin = dict(path=str(root/'artifact'),sha256='a'*64)
            item = dict(mode='container',binary=pin,kernel=pin,template=pin,image='sha256:'+'a'*64,
                        memory_mb=1024,cpu_count=1,inner_memory_mb=512,inner_cpus=1)
            value = dict(schema=2,cid_pool=dict(root=str(root/'cids'),first=20000,last=20001),
                         environments={'coding':claim(item)})
            path = root/'catalog.json'; path.write_text(json.dumps(value))
            catalog = ExecutionCatalog(path)
            self.assertEqual(catalog.execution_contracts()[0].capabilities,execution_requirements([STREAM_FEATURE]))
            value['environments']['coding']['template'] = dict(pin,sha256='b'*64)
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError,'pinned environment'):
                ExecutionCatalog(path)

    def test_microvm_claim_is_checked_after_actual_artifact_integrity(self):
        import hashlib
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            files = {}
            for name in ('boot','kernel','layer'):
                target = root/name; target.write_bytes(name.encode()); files[name] = target
            sha = lambda path:hashlib.sha256(path.read_bytes()).hexdigest()
            item = dict(backend='microvm',rootfs='erofs_layers',boot_template=str(files['boot']),
                        boot_sha256=sha(files['boot']),kernel=str(files['kernel']),kernel_sha256=sha(files['kernel']),
                        layers=[dict(name='tools',file=str(files['layer']),sha256=sha(files['layer']))])
            item = claim(item)
            path = root/'catalog.json'
            path.write_text(json.dumps(dict(format=1,environments={'coding':item})))
            catalog = MicroVMEnvironmentCatalog(path)
            catalog.resolve('coding')
            identity = catalog.execution_contracts()[0]
            self.assertEqual(identity.capabilities,execution_requirements([STREAM_FEATURE]))
            files['boot'].write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'boot template changed'):
                catalog.resolve('coding')
            path.write_text('{}')
            with self.assertRaisesRegex(ValueError,'catalog changed'):
                catalog.execution_contracts()


if __name__ == '__main__':
    unittest.main()
