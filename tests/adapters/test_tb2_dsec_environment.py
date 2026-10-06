"""TB2 task plugin keeps its setup and verdict rules outside the core API."""

from pathlib import Path
import json
import tempfile
import unittest

from agent_environment import EnvironmentAction
from dsec_adapters.tb2_dsec_environment import TB2DSecEnvironment


class FakeSandbox:
    def __init__(self, verdict):
        self.verdict = verdict
        self.command = None
        self.kwargs = None

    async def agent_step(self, command, **kwargs):
        self.command, self.kwargs = command, kwargs
        return {"entry": {"result": {"exit_code": 0, "output": "ok"}},
                "dialogue": {"state": "ACTIVE", "next_step": 1}}

    async def tb2_evaluate(self):
        return self.verdict


class TB2EnvironmentTest(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_step_and_canonical_verdict(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory) / "regex-log"
            (task / "tests").mkdir(parents=True)
            (task / "environment").mkdir()
            (task / "task.toml").write_text("")
            (task / "instruction.md").write_text("Find dates")
            (task / "tests" / "test.sh").write_text("#!/bin/sh\n")
            (task / "environment" / "Dockerfile").write_text("FROM scratch\nWORKDIR /app\n")
            adapter = TB2DSecEnvironment(Path(directory))
            spec = adapter.prepare("regex-log")
            self.assertEqual(spec.instruction, "Find dates")
            self.assertEqual(spec.options["workdir"], "/app")
            self.assertEqual(spec.profile.environment_id, "tb2-regex-log")
            catalog = Path(directory) / "catalog.json"
            catalog.write_text(json.dumps({"format": 1, "environments": {
                "tb2-regex-log": {"backend": "microvm", "rootfs": "erofs_layers",
                                  "cpus": 2, "memory_mb": 2048}}}))
            registered = TB2DSecEnvironment(Path(directory), environment_catalog=catalog)
            self.assertEqual(registered.prepare("regex-log").resources["memory_mb"], 2048)
            self.assertEqual(registered.prepare("regex-log").resources["cpu"], 2.0)
            sandbox = FakeSandbox({"task_id": "regex-log", "value": 1.0,
                                   "harness": "tests/test.sh"})
            policy_message = {"role": "assistant", "content": "```bash\npwd\n```"}
            observation = await adapter.step(
                sandbox, spec, EnvironmentAction.shell(
                    step_id=0, action_id="turn-0", command="pwd"), policy_message)
            self.assertEqual(sandbox.command, "cd /app && pwd")
            self.assertIs(sandbox.kwargs["assistant_message"], policy_message)
            self.assertEqual(observation.result["output"], "ok")
            verdict = await adapter.evaluate(sandbox, spec)
            self.assertEqual((verdict.score, verdict.evaluator), (1.0, "tests/test.sh"))
            sandbox.verdict = {"task_id": "regex-log", "value": None,
                               "harness": "tests/test.sh"}
            with self.assertRaisesRegex(RuntimeError, "official TB2 verdict"):
                await adapter.evaluate(sandbox, spec)

    def test_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "Invalid TB2 task ID"):
                TB2DSecEnvironment(Path(directory)).prepare("../outside")


if __name__ == "__main__":
    unittest.main()
