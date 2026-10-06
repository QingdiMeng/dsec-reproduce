import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from dsec_adapters import tb2_verifier_command as verifier


class VerifierCommandTests(unittest.TestCase):
    def test_executes_test_sh_in_task_workdir_and_reads_verdict(self):
        with tempfile.TemporaryDirectory(prefix="verifier space ") as directory:
            root = Path(directory)
            tests, logs, work = (root / name for name in ("tests", "logs", "work"))
            for path in (tests, logs, work):
                path.mkdir()
            (tests / "test.sh").write_text(
                'test -f answer || exit 7\n'
                f'printf 1 > "{logs}/reward.txt"\n'
                'echo tests-ran\n')
            (work / "answer").write_text("submitted")
            with patch.multiple(verifier, _VERIFY_TESTS_DIR=str(tests),
                                _VERIFIER_LOG_DIR=str(logs)):
                result = subprocess.run(
                    ["bash", "-c", verifier.canonical_eval_cmd(str(work), 5)],
                    text=True, capture_output=True, check=True)
            self.assertIn("tests-ran", result.stdout)
            self.assertEqual(verifier.parse_canonical_reward(result.stdout), 1.0)
            self.assertIn("tests-ran", (logs / "testsh.log").read_text())

    def test_missing_or_invalid_verdict_is_not_zero(self):
        for output in ("", "tests failed", "__TB2_REWARD__:",
                       "__TB2_REWARD__:nan", "__TB2_REWARD__:inf",
                       "__TB2_REWARD__:0.5", "__TB2_REWARD__:bad",
                       "__TB2_REWARD__:1\n__TB2_REWARD__:"):
            with self.subTest(output=output):
                self.assertIsNone(verifier.parse_canonical_reward(output))
        self.assertEqual(verifier.parse_canonical_reward("__TB2_REWARD__:0"), 0.0)


if __name__ == "__main__":
    unittest.main()
