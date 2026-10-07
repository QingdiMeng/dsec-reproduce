"""Check atomic verifier evidence export and reward-sentinel consistency."""

import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from dsec_adapters.tb2_microvm_env import (TB2MicroVMEnv, _canonical_uv_network_setup,
                                          SHARED_MANIFEST)


class CanonicalNetworkTest(unittest.TestCase):
    def test_default_inherits_guest_policy_and_explicit_override_reaches_uv(self):
        for override, inherited, expected in ((None, None, "unset"),
                                             (None, "1", "1"),
                                             ("1", "1", "0"),
                                             ("0", None, "1")):
            with self.subTest(override=override, inherited=inherited):
                with patch.dict(os.environ, {}, clear=True):
                    if override is not None:
                        os.environ["DSEC_TB2_VERIFIER_ONLINE"] = override
                    setup = _canonical_uv_network_setup()
                guest = {"PATH": os.defpath}
                if inherited is not None:
                    guest["UV_OFFLINE"] = inherited
                result = subprocess.run(
                    ["sh", "-c", setup + "sh -c 'printf %s \"${UV_OFFLINE-unset}\"'"],
                    env=guest, check=True, capture_output=True, text=True)
                self.assertEqual(result.stdout, expected)
        with patch.dict(os.environ, {"DSEC_TB2_VERIFIER_ONLINE": "maybe"}):
            with self.assertRaises(ValueError):
                _canonical_uv_network_setup()


class EvidenceExportTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_verifier_script_selects_verifier_shell(self):
        calls = []

        class Sandbox:
            async def run_shell(self, command, **kwargs):
                calls.append(("agent", command, kwargs))
                return {"exit_code": 0}

            async def run_verifier_shell(self, command, **kwargs):
                calls.append(("verifier", command, kwargs))
                return {"exit_code": 0}

        env = object.__new__(TB2MicroVMEnv)
        env.sandbox = Sandbox()
        env.backend_name = "dsec-microvm"
        await env._shell("true")
        await env._shell("bash /tests/test.sh", timeout_ms=3600000, verifier=True)
        self.assertEqual([c[0] for c in calls], ["agent", "verifier"])
        self.assertEqual(calls[1][2]["timeout_ms"], 3600000)

    async def test_export_survives_guest_loss_and_rejects_reward_mismatch(self):
        report = {"results": {"summary": {"tests": 1}, "tests": [{
            "name": "test_answer", "status": "failed", "message": "missing answer"}]}}
        guest = {
            "/logs/verifier/ctrf.json": json.dumps(report).encode(),
            "/logs/verifier/dsec-test.log": b"FAILED test_answer: missing answer\n",
            "/logs/verifier/testsh.log": b"full pytest output\n",
            "/logs/verifier/reward.txt": b"0\n",
        }
        with tempfile.TemporaryDirectory() as root:
            async def read(source):
                return guest[source]

            env = object.__new__(TB2MicroVMEnv)
            env.task_id = "test-answer"
            env.evidence_dir = Path(root) / "episode"
            env._read_guest_evidence = read
            evidence = await env._save_verifier_evidence(0.0)
            guest.clear()  # The VM can now be stopped or deleted.
            self.assertEqual(json.loads((env.evidence_dir / "ctrf.json").read_text()), report)
            self.assertEqual((env.evidence_dir / "verifier.log").read_bytes(),
                             b"FAILED test_answer: missing answer\n")
            self.assertEqual(evidence["artifacts"]["ctrf.json"]["sha256"],
                             hashlib.sha256((env.evidence_dir / "ctrf.json").read_bytes()).hexdigest())
            self.assertEqual((env.evidence_dir / "manifest.json").stat().st_mode & 0o777,
                             0o600)

            guest["/logs/verifier/ctrf.json"] = json.dumps(report).encode()
            guest["/logs/verifier/dsec-test.log"] = b"failed\n"
            guest["/logs/verifier/testsh.log"] = b"failed\n"
            guest["/logs/verifier/reward.txt"] = b"0\n"
            env.evidence_dir = Path(root) / "mismatch"
            with self.assertRaisesRegex(RuntimeError, "disagrees"):
                await env._save_verifier_evidence(1.0)
            self.assertFalse(env.evidence_dir.exists())

    async def test_failed_evaluation_exports_logs_without_manufacturing_zero(self):
        for failure_kind in ("missing_ctrf", "cached_missing_ctrf", "timeout", "export_unavailable"):
            with self.subTest(failure_kind=failure_kind), tempfile.TemporaryDirectory() as root:
                task = Path(root) / "task-answer"
                (task / "tests").mkdir(parents=True)
                (task / "environment").mkdir()
                (task / "environment/Dockerfile").write_text('FROM python:3.13\nWORKDIR /app\n')
                (task / "task.toml").write_text('[verifier]\ntimeout_sec=30\n')
                (task / "instruction.md").write_text('Write an answer')
                (task / "tests/test.sh").write_text('echo 0 > /logs/verifier/reward.txt')
                async def status():
                    if failure_kind == "cached_missing_ctrf":
                        return {"verifier_storage": "local", "verifier_artifact_sha256":
                                json.loads(SHARED_MANIFEST.read_text())["verifier_artifact_sha256"]}
                    return {}
                env = TB2MicroVMEnv(SimpleNamespace(status=status), task_id=task.name, task_dir=task,
                                    evidence_dir=Path(root) / "evidence")
                async def stage():
                    pass
                calls = []
                async def shell(command, **kwargs):
                    calls.append(command)
                    if kwargs.get("verifier"):
                        if failure_kind == "cached_missing_ctrf":
                            self.assertIn("UV_CACHE_DIR=/.cache/uv", command)
                            self.assertNotIn("UV_OFFLINE=", command)
                        return {"exit_code": 0, "timed_out": failure_kind == "timeout",
                                "truncated": False, "output": "bootstrap error\n__TB2_REWARD__:0"}
                    if "mount -t overlay" in command:
                        return {"exit_code": 0, "timed_out": False, "truncated": False,
                                "output": ""}
                    return {"exit_code": 1, "timed_out": False,
                            "truncated": False, "output": "No such file: ctrf.json"}
                full_log = b"dependency output\n" * 3000  # Exceeds the displayed output tail.
                guest = {"/logs/verifier/dsec-test.log": b"bootstrap error\n",
                         "/logs/verifier/testsh.log": full_log,
                         "/logs/verifier/reward.txt": b"0\n"}
                async def read(source):
                    if failure_kind == "export_unavailable":
                        raise RuntimeError("guest unavailable")
                    if source not in guest:
                        raise FileNotFoundError(source)
                    return guest[source]
                env._stage_tests = stage
                env._shell = shell
                env._read_guest_evidence = read
                await env.reset(task_id=task.name)
                with patch.dict(os.environ, {}, clear=True):
                    result = await env.step(SimpleNamespace(action_type="evaluate"))
                self.assertIsNone(result.reward)
                self.assertTrue(result.observation.error)
                self.assertEqual(sum("__TB2_REWARD__" in c for c in calls), 1)
                evidence = result.observation.info["evidence"]
                self.assertEqual(evidence["directory"], str(env.evidence_dir))
                failure = json.loads((env.evidence_dir / "failure.json").read_text())
                self.assertIsNone(failure["reward"])
                self.assertIn("ctrf.json", failure["export_errors"])
                guest.clear()  # The receipt and available logs survive sandbox cleanup.
                if failure_kind != "export_unavailable":
                    self.assertEqual((env.evidence_dir / "testsh.log").read_bytes(), full_log)
                else:
                    self.assertIn("verifier.log", failure["export_errors"])


if __name__ == "__main__":
    unittest.main()
