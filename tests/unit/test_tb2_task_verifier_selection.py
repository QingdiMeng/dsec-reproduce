"""Task-scoped verifier artifacts must not change older TB2 environments."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from sandbox_sdk import Sandbox, SandboxManager
from tb2_verifier_artifact import VerifierArtifactStore


def artifact(root: Path, name: str, payload: bytes, **extra):
    disk = root / f"{name}.ext4"
    disk.write_bytes(payload)
    manifest = root / f"{name}.json"
    manifest.write_text(json.dumps({
        "schema": 1, "format": "ext4", "tool": "uvx 0.9.5",
        "guest_mountpoint": "/mnt/dsec-verifier",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload), **extra}))
    return VerifierArtifactStore(manifest, disk)


class TaskVerifierSelectionTests(unittest.TestCase):
    def test_dax_requires_compact_task_pinned_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "guest"
            dax_binary = root / "firecracker-dax"
            dax_binary.write_bytes(b"pinned test binary")
            task = "tb2-install-windows-3.11"
            compact = artifact(root, "compact", bytes(2 * 1024 * 1024),
                               task_id="install-windows-3.11", dax_compact=True)
            manager = SandboxManager(
                root / "runs", root / "firecracker", root / "kernel", template,
                start_monitor=False, tb2_templates={task: template},
                tb2_verifier_artifacts={task: compact},
                tb2_verifier_dax_tasks={task},
                tb2_verifier_dax_binary=dax_binary)
            try:
                sandbox = Sandbox(manager, 30, task, verifier_storage="local")
                self.assertTrue(sandbox.verifier_dax)
                self.assertTrue(sandbox.status()["verifier_dax"])
                self.assertEqual(Path(sandbox.vm.binary).resolve(), dax_binary.resolve())
            finally:
                manager.close()
            with self.assertRaisesRegex(ValueError, "compact, task-pinned"):
                SandboxManager(root / "invalid", root / "firecracker", root / "kernel",
                               template, start_monitor=False, tb2_templates={task: template},
                               tb2_verifier_artifacts={task: artifact(
                                   root, "generic", bytes(2 * 1024 * 1024))},
                               tb2_verifier_dax_tasks={task},
                               tb2_verifier_dax_binary=dax_binary)

    def test_old_task_keeps_default_artifact_and_new_task_gets_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = artifact(root, "old", b"original verifier")
            new = artifact(root, "new", b"incremental verifier")
            manager = SandboxManager(
                root / "runs", root / "firecracker", root / "kernel", root / "guest",
                start_monitor=False,
                tb2_templates={"tb2-regex-log": root / "guest",
                               "tb2-log-summary-date-ranges": root / "guest"},
                tb2_resources={"tb2-log-summary-date-ranges":
                               {"verifier_timeout_ms": 3600000}},
                tb2_verifier_artifacts={"default": old,
                                        "tb2-log-summary-date-ranges": new})
            try:
                self.assertIs(manager.verifier_artifacts_for("tb2-regex-log"), old)
                self.assertIs(manager.verifier_artifacts_for("tb2-log-summary-date-ranges"), new)
                old_sandbox = Sandbox(manager, 30, "tb2-regex-log", verifier_storage="local")
                new_sandbox = Sandbox(manager, 30, "tb2-log-summary-date-ranges",
                                      verifier_storage="local")
                self.assertEqual(old_sandbox.verifier_artifact_sha, old.sha)
                self.assertEqual(new_sandbox.verifier_artifact_sha, new.sha)
                self.assertNotEqual(old_sandbox.verifier_artifact_sha,
                                    new_sandbox.verifier_artifact_sha)
                self.assertEqual(old_sandbox.vm.max_timeout_ms, 900000)
                self.assertEqual(new_sandbox.vm.max_timeout_ms, 3600000)
            finally:
                manager.close()


if __name__ == "__main__":
    unittest.main()
