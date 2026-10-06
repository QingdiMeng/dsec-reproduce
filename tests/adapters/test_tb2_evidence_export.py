"""Check atomic verifier evidence export and reward-sentinel consistency."""

import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from dsec_adapters.tb2_microvm_env import TB2MicroVMEnv


class EvidenceExportTest(unittest.IsolatedAsyncioTestCase):
    async def test_export_survives_guest_loss_and_rejects_reward_mismatch(self):
        report = {"results": {"summary": {"tests": 1}, "tests": [{
            "name": "test_answer", "status": "failed", "message": "missing answer"}]}}
        guest = {
            "/logs/verifier/ctrf.json": json.dumps(report).encode(),
            "/logs/verifier/dsec-test.log": b"FAILED test_answer: missing answer\n",
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
            guest["/logs/verifier/reward.txt"] = b"0\n"
            env.evidence_dir = Path(root) / "mismatch"
            with self.assertRaisesRegex(RuntimeError, "disagrees"):
                await env._save_verifier_evidence(1.0)
            self.assertFalse(env.evidence_dir.exists())


if __name__ == "__main__":
    unittest.main()
