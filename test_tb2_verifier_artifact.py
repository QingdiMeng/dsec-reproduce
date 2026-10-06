import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tb2_verifier_artifact import VerifierArtifactStore


class VerifierArtifactStoreTests(unittest.TestCase):
    def test_local_and_threefs_must_have_identical_pinned_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = root / "local.ext4"
            remote = root / "threefs.ext4"
            local.write_bytes(b"same artifact")
            remote.write_bytes(local.read_bytes())
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "schema":1, "format":"ext4", "tool":"uvx 0.9.5",
                "guest_mountpoint":"/mnt/dsec-verifier",
                "sha256":hashlib.sha256(local.read_bytes()).hexdigest(),
                "size_bytes":local.stat().st_size}))
            store = VerifierArtifactStore(manifest, local, remote)
            self.assertEqual(store.resolve("local"), local.resolve())
            self.assertEqual(store.resolve("threefs_lazy"), remote.resolve())
            remote.write_bytes(b"bad artifact!")
            with self.assertRaises(ValueError):
                store.resolve("threefs_lazy")


if __name__ == "__main__":
    unittest.main()
