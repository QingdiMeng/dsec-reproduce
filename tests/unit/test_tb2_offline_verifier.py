import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

import json
import tempfile
from dsec_adapters import tb2_offline_verifier as offline


class OfflineVerifierTests(unittest.TestCase):
    def test_only_pinned_bootstrap_changes(self):
        original = (b"#!/bin/bash\n" + offline.DEPENDENCY_BOOTSTRAP +
                    b"\nuvx pytest --ctrf /logs/verifier/ctrf.json\n"
                    b"echo 0 > /logs/verifier/reward.txt\n")
        digest = hashlib.sha256(original).hexdigest()
        with patch.dict(offline.PINNED_TEST_SH_SHA256, {"regex-log":digest}):
            transformed = offline.offline_test_sh("regex-log", original)
        self.assertIn(b"UV_OFFLINE=1", transformed)
        self.assertIn(b"uvx pytest --ctrf /logs/verifier/ctrf.json", transformed)
        self.assertIn(b"echo 0 > /logs/verifier/reward.txt", transformed)
        self.assertNotIn(b"curl -LsSf", transformed)
        with self.assertRaises(ValueError):
            offline.offline_test_sh("other-task", original)
        with self.assertRaises(ValueError):
            offline.offline_test_sh("regex-log", original + b"# changed\n")

    def test_shared_runtime_transform_requires_exact_pinned_task(self):
        suffix = b"\n# Check if scoring remains unchanged\nexit 0\n"
        original = b"#!/bin/bash\n" + offline.SHARED_BOOTSTRAP + b"\n" + offline.SHARED_UV_COMMAND + suffix
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "shared.json"
            manifest.write_text(json.dumps({
                "tasks": {"regex-log": hashlib.sha256(original).hexdigest()},
                "bootstrap_sha256": hashlib.sha256(offline.SHARED_BOOTSTRAP).hexdigest(),
                "uv_command_sha256": hashlib.sha256(offline.SHARED_UV_COMMAND).hexdigest()}))
            with patch.object(offline, "SHARED_MANIFEST", manifest):
                transformed = offline.shared_offline_test_sh("regex-log", original)
                self.assertNotIn(b"astral.sh", transformed)
                self.assertIn(b"UV_CACHE_DIR=/.cache/uv", transformed)
                self.assertTrue(transformed.endswith(offline.SHARED_UV_COMMAND + suffix))
                with self.assertRaises(ValueError):
                    offline.shared_offline_test_sh("regex-log", original + b"# changed\n")
                with self.assertRaises(ValueError):
                    offline.shared_offline_test_sh("unreviewed-task", original)

    def test_layered_uv_keeps_task_specific_dependency_setup(self):
        prefix = b"#!/bin/bash\napt-get install -y curl binutils\n"
        suffix = b"\n# Check if scoring remains unchanged\nexit 0\n"
        original = prefix + offline.CURL_INSTALL + b"\n\n" + offline.SOURCE_UV_ENV + suffix
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "layered.json"
            manifest.write_text(json.dumps({"tasks": {"compile-compcert": {
                "test_sh_sha256": hashlib.sha256(original).hexdigest()}}}))
            with patch.object(offline, "LAYERED_UV_MANIFEST", manifest):
                transformed = offline.layered_uv_test_sh("compile-compcert", original)
                self.assertTrue(transformed.startswith(prefix))
                self.assertTrue(transformed.endswith(suffix))
                self.assertIn(b"export UV_CACHE_DIR=/.cache/uv", transformed)
                self.assertNotIn(b"astral.sh", transformed)
                with self.assertRaises(ValueError):
                    offline.layered_uv_test_sh("compile-compcert", original + b"# changed\n")


if __name__ == "__main__":
    unittest.main()
