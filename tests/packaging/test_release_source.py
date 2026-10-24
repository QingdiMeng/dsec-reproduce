"""Validate the actual archive, including its reproducibility and boundary."""
import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from tools.package_release_source import package, source_files, SECRET
from tools.check_docs import check


class SourceReleaseTests(unittest.TestCase):
    def test_reproducible_archive_has_only_pinned_sources_and_license(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp)/'a.tar.gz', Path(tmp)/'b.tar.gz'
            package(root, a)
            package(root, b)
            self.assertEqual(a.read_bytes(), b.read_bytes())
            with tarfile.open(a) as archive:
                manifest = json.load(archive.extractfile('SOURCE_MANIFEST.json'))
                self.assertEqual(set(archive.getnames()), set(manifest['files'])|{'SOURCE_MANIFEST.json'})
                for name, digest in manifest['files'].items():
                    self.assertEqual(hashlib.sha256(archive.extractfile(name).read()).hexdigest(), digest)
                    self.assertFalse(name.startswith(('experiments/', 'results/', '.runtime/')))
                for name in ('src/dsec/host/console/index.html', 'src/dsec/host/console/console.js',
                             'src/dsec/host/console/console.css', 'src/dsec/control/cluster_inventory.py',
                             'docs/guides/CLUSTER_CONSOLE.md', 'tests/unit/test_cluster_console.py',
                             'LICENSE', 'licenses/miles-APACHE-2.0.txt', 'guest_agent.c', 'guest_native.c',
                             'tools/build_native_agent.py', 'tools/verify_native_sdk.py', 'tools/verify_native_races.py', 'src/dsec/sdk/native.py',
                             'tools/check_concurrency_model.py', 'tools/verify_rollout_restart.py',
                             'tools/check_shutdown_refinement.py',
                             'tools/check_native_race_refinement.py', 'tests/unit/test_native_races.py',
                             'verification/QueueCancellation.tla', 'verification/QueueCancellation.cfg',
                             'verification/NativeCallbackFence.tla', 'verification/NativeCallbackFence.cfg',
                             'verification/ContainerNativeGate.tla', 'verification/ContainerNativeGate.cfg',
                             'verification/RaceReplay.tla',
                             'verification/NativeLifecycle.tla', 'verification/NativeLifecycle.cfg',
                             'verification/ShutdownSignal.tla', 'verification/ShutdownSignal.cfg',
                             'verification/ShutdownReplay.tla', 'verification/native_shutdown_probe.h',
                             'src/dsec/runtime/sessions/jobs.py',
                             'docs/guides/QUICKSTART.md', 'apps/tb21/pyproject.toml',
                             'apps/mbpp/pyproject.toml', 'apps/mbpp/src/dsec_mbpp_case/reward.py'):
                    self.assertIn(name, manifest['files'])
                self.assertFalse(any(' ' in Path(name).name for name in manifest['files']))
                for name in ('deploy/cluster/planning.py', 'deploy/cluster/configuration.py',
                             'deploy/cluster/enroll_node.py', 'deploy/cluster/pki.py',
                             'deploy/cluster/upgrade.py', 'src/dsec/host/releases.py',
                             'src/dsec/contracts/release.py', 'verification/NodeUpgrade.tla',
                             'src/dsec/control/node_lifecycle.py', 'src/dsec/host/node_directory.py',
                             'src/dsec/host/fleet.py', 'verification/NodeLifecycle.tla',
                             'deploy/cluster/nodes.example.json',
                             'src/dsec/host/node_probe.py', 'src/dsec/host/node_setup.py',
                             'tests/unit/test_node_preflight.py',
                             'src/dsec/contracts/resources.py', 'src/dsec/storage/overlaybd.py',
                             'src/dsec/runtime/backends/qemu.py', 'src/dsec/runtime/backends/qmp.py',
                             'tests/unit/test_qemu_backend.py', 'verification/QemuLifecycle.tla',
                             'src/dsec/runtime/isolation/vsock.py', 'tests/unit/test_qemu_cid_pool.py',
                             'verification/QemuCID.tla', 'verification/QemuCID.cfg',
                             'src/dsec/sdk/sandbox_transport.py',
                             'src/dsec/contracts/evaluation.py',
                             'src/dsec/contracts/sandbox.py', 'src/dsec/sdk/client.py',
                             'src/dsec/sdk/development.py', 'tests/unit/test_api_sdk.py',
                             'src/dsec/sdk/rollout_api.py','src/dsec/control/rollout_api.py',
                             'src/dsec/host/rollout_gateway.py','src/dsec/contracts/rollout.py',
                             'tests/unit/test_rollout_api.py',
                             'src/dsec/runtime/container_edge.py', 'src/dsec/control/server.py',
                             'src/dsec/runtime/resources.py', 'src/dsec/rollout/scheduler.py',
                             'src/dsec/rollout/quotas.py', 'src/dsec/runtime/node_admission.py',
                             'tests/unit/test_node_admission.py',
                             'src/dsec/runtime/transitions.py', 'src/dsec/contracts/errors.py',
                             'src/dsec/runtime/edge.py', 'src/dsec/runtime/registry_store.py',
                             'src/dsec/runtime/pool.py', 'src/dsec/runtime/provisioning.py',
                             'tests/unit/test_edge_assembly.py',
                             'src/dsec/contracts/execution.py',
                             'src/dsec/runtime/sessions/channel.py',
                             'src/dsec/runtime/sessions/dispatcher.py',
                             'tests/unit/test_command_sessions.py',
                             'src/dsec/contracts/storage.py',
                             'src/dsec/storage/service.py', 'src/dsec/storage/snapshots.py',
                             'tests/unit/test_runtime_storage.py',
                             'tests/unit/test_task_application_boundary.py',
                             'src/dsec/control/environments.py',
                             'src/dsec/compat/applications.py',
                             'apps/tb21/src/dsec_tb21_case/host_configuration.py',
                             'apps/tb21/src/dsec_tb21_case/tb2_microvm_env.py',
                             'apps/tb21/src/dsec_tb21_case/tb2_layered_uv_manifest.json',
                             'apps/tb21/src/dsec_tb21_case/worker_evaluator.py',
                             'tests/unit/test_worker_evaluators.py',
                             'tests/unit/test_container_edge.py',
                             'tests/contracts/v01_compatibility.json'):
                    self.assertIn(name, manifest['files'])
                self.assertFalse(any('miles_lora_nvme' in n or n.endswith('.ext4')
                                     for n in manifest['files']))
                # Validate the exported tree, which has fewer files than the
                # development workspace. A link must work for a new checkout.
                exported = Path(tmp)/'exported'
                for name in manifest['files']:
                    target = exported/name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.extractfile(name).read())
                self.assertEqual(check(exported), [])
            with self.assertRaises(FileExistsError):
                package(root, a)

    def test_new_portable_tests_are_exported_without_a_filename_registration(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp:
            exported = Path(tmp)
            _, names = source_files(root)
            for name in names:
                target = exported / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((root / name).read_bytes())
            new_test = exported / 'tests/unit/test_new_component.py'
            new_test.write_text('import unittest\n')
            (new_test.parent / 'test_new_component 2.py').write_text('private copy\n')
            private = exported / 'tests/integration/test_live_machine.py'
            private.parent.mkdir(parents=True)
            private.write_text('not a portable regression\n')
            output = exported / 'source.tar.gz'
            package(exported, output)
            with tarfile.open(output) as archive:
                self.assertIn('tests/unit/test_new_component.py', archive.getnames())
                self.assertNotIn('tests/unit/test_new_component 2.py', archive.getnames())
                self.assertNotIn('tests/integration/test_live_machine.py', archive.getnames())

    def test_secret_guard_recognizes_tokens_and_private_keys(self):
        for value in (b'-----BEGIN OPENSSH '+b'PRIVATE KEY-----',
                      b'sk-'+b'a'*30, b'hf_'+b'b'*30):
            self.assertIsNotNone(SECRET.search(value))
        self.assertIsNone(SECRET.search(b'export API_KEY="$API_KEY"'))


if __name__ == '__main__':
    unittest.main()
