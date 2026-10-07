"""Task-scoped verifier artifacts must not change older TB2 environments."""

import hashlib
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sandbox_sdk import Sandbox, SandboxManager
from tb2_verifier_artifact import VerifierArtifactStore
from libdsec_compat import DSecSandbox


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
    def test_long_verifier_does_not_expand_agent_command_deadline(self):
        for verifier_ms in (3600000, 12000000):
            with self.subTest(verifier_ms=verifier_ms), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                task = "tb2-long-verifier"
                manager = SandboxManager(
                    root / "runs", root / "firecracker", root / "kernel", root / "guest",
                    start_monitor=False, tb2_templates={task: root / "guest"},
                    tb2_resources={task: {"command_timeout_ms": 900000,
                                          "verifier_timeout_ms": verifier_ms}})
                try:
                    sandbox = Sandbox(manager, 30, task)
                    sandbox.state = "RUNNING"
                    self.assertEqual(sandbox.vm.max_timeout_ms, verifier_ms)
                    reply = {"exit_code": 0, "timed_out": False, "output": "ok"}
                    with patch.object(sandbox, "_check"), patch.object(
                            sandbox.vm, "execute", return_value=reply) as execute:
                        with self.assertRaisesRegex(ValueError, "agent timeout_ms"):
                            sandbox.execute("true", timeout_ms=verifier_ms)
                        execute.assert_not_called()
                        self.assertEqual(sandbox.execute(
                            "true", timeout_ms=verifier_ms, execution_scope="verifier"), reply)
                        execute.assert_called_once_with("true", verifier_ms, 65536)
                        with self.assertRaisesRegex(ValueError, "verifier timeout_ms"):
                            sandbox.execute("true", timeout_ms=verifier_ms+1,
                                            execution_scope="verifier")
                        with self.assertRaisesRegex(ValueError, "execution scope"):
                            sandbox.execute("true", execution_scope="unknown")
                        self.assertEqual(execute.call_count, 1)
                finally:
                    manager.close()

    def test_verifier_client_explicitly_selects_evaluator_scope(self):
        class Transport:
            def __init__(self):
                self.calls = []

            def call(self, operation, sandbox_id=None, **args):
                self.calls.append((operation, sandbox_id, args))
                return {"exit_code": 0}

        transport = Transport()
        sandbox = DSecSandbox(transport, "sandbox")
        asyncio.run(sandbox.run_shell("true", timeout_ms=120000))
        asyncio.run(sandbox.run_verifier_shell("true", timeout_ms=3600000))
        self.assertNotIn("execution_scope", transport.calls[0][2])
        self.assertEqual(transport.calls[1][2]["execution_scope"], "verifier")
        self.assertEqual(transport.calls[1][2]["timeout_ms"], 3600000)

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
