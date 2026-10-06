"""Publication must pin bytes and leave catalog entries atomic and immutable."""

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from artifact_publisher import publish_microvm
from environment_catalog import MicroVMEnvironmentCatalog


def sha(data):
    return hashlib.sha256(data).hexdigest()


class ArtifactPublisherTest(unittest.TestCase):
    def fixture(self, root):
        files = {}
        for name, data in (("boot.ext4", b"boot"), ("kernel", b"kernel"),
                           ("tools.erofs", b"tools"), ("root.commit", b"lower")):
            files[name] = root / name
            files[name].write_bytes(data)
        image = root / "root-image.json"
        image.write_text(json.dumps({"lowers": [{"file": str(files["root.commit"])}], "upper": {}}))
        entry = {"backend": "microvm", "rootfs": "erofs_layers",
                 "boot_template": str(files["boot.ext4"]), "boot_sha256": sha(b"boot"),
                 "kernel": str(files["kernel"]), "kernel_sha256": sha(b"kernel"),
                 "layers": [{"name": "tools", "file": str(files["tools.erofs"]),
                             "sha256": sha(b"tools")}],
                 "root_block_backend": "overlaybd-ublk",
                 "overlaybd_root": {"image": str(image),
                                    "image_sha256": sha(image.read_bytes()),
                                    "lowers": [{"file": str(files["root.commit"]),
                                                "sha256": sha(b"lower"), "bytes": 5}]}}
        source = root / "source.json"
        source.write_text(json.dumps({"format": 1, "environments": {"tools": entry}}))
        return source, files

    def test_publish_overlaybd_and_deduplicate_without_changing_existing_environment(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            source, files = self.fixture(root)
            destination = root / "published/catalog.json"
            store = root / "published/store"
            result = publish_microvm(source, "tools", destination, store)
            self.assertEqual(result["environment_id"], "tools")
            catalog = MicroVMEnvironmentCatalog(destination)
            spec = catalog.resolve("tools")
            self.assertEqual(spec["root_block_backend"], "overlaybd-ublk")
            entry = catalog.entries["tools"]
            self.assertEqual(json.loads(Path(entry["overlaybd_root"]["image"]).read_text()),
                             {"lowers": [{"file": entry["overlaybd_root"]["lowers"][0]["file"]}],
                              "upper": {}})
            self.assertEqual(Path(entry["layers"][0]["file"]).read_bytes(), b"tools")
            first_identity = result["environment_sha256"]
            payload = json.loads(source.read_text())
            payload["environments"]["tools-two"] = payload["environments"]["tools"]
            source.write_text(json.dumps(payload))
            objects_before = {p.name for p in (store / "objects").iterdir()}
            publish_microvm(source, "tools-two", destination, store)
            self.assertEqual({p.name for p in (store / "objects").iterdir()}, objects_before)
            files["tools.erofs"].unlink()
            self.assertEqual(MicroVMEnvironmentCatalog(destination).resolve("tools")["environment_sha256"],
                             first_identity)
            with self.assertRaisesRegex(ValueError, "already published"):
                publish_microvm(destination, "tools", destination, store)
            self.assertEqual(MicroVMEnvironmentCatalog(destination).environment_digest("tools"),
                             first_identity)

    def test_reject_changed_source_and_poisoned_existing_object_without_catalog_write(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            source, files = self.fixture(root)
            destination = root / "catalog.json"
            files["tools.erofs"].write_bytes(b"other")
            with self.assertRaisesRegex(ValueError, "changed"):
                publish_microvm(source, "tools", destination, root / "store")
            self.assertFalse(destination.exists())
            files["tools.erofs"].write_bytes(b"tools")
            poisoned = root / "store/objects" / (sha(b"tools") + ".erofs")
            poisoned.parent.mkdir(parents=True)
            poisoned.write_bytes(b"other")
            with self.assertRaisesRegex(ValueError, "Published object changed"):
                publish_microvm(source, "tools", destination, root / "store")
            self.assertFalse(destination.exists())

    def test_remote_layer_is_verified_at_publication_and_lazy_at_runtime(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            source, _ = self.fixture(root)
            mount = root / "threefs"
            mount.mkdir()
            store = mount / "objects"
            original_run = subprocess.run

            def fake_run(args, **kwargs):
                if args[0] == "findmnt":
                    return subprocess.CompletedProcess(args, 0, "fuse.hf3fs\n")
                return original_run(args, **kwargs)

            with patch("artifact_publisher.subprocess.run", side_effect=fake_run), \
                    patch("environment_catalog.resolve_threefs_file",
                          side_effect=lambda mount, path, size: (Path(mount), Path(path))):
                publish_microvm(source, "tools", root / "published.json", root / "local",
                                threefs_mount=mount, threefs_store=store)
                catalog = MicroVMEnvironmentCatalog(root / "published.json")
                remote = catalog.resolve("tools", "threefs_lazy")
                self.assertEqual(remote["layers"][0]["source"], "threefs_lazy")
                self.assertEqual(remote["layers"][0]["file"].read_bytes(), b"tools")
                with patch("environment_catalog.file_sha256",
                           side_effect=AssertionError("remote full read at create")):
                    catalog.resolve("tools", "threefs_lazy")

    def test_published_dax_vmm_remains_executable(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            source, files = self.fixture(root)
            layer = files["tools.erofs"]
            layer.write_bytes(b"x" * (2 * 1024 * 1024))
            binary = root / "firecracker"
            binary.write_bytes(b"vmm")
            binary.chmod(0o755)
            payload = json.loads(source.read_text())
            entry = payload["environments"]["tools"]
            entry["layers"][0].update(sha256=sha(layer.read_bytes()),
                                       bytes=layer.stat().st_size, dax=True)
            entry.update(dax_binary=str(binary), dax_binary_sha256=sha(b"vmm"))
            source.write_text(json.dumps(payload))
            destination = root / "published.json"
            publish_microvm(source, "tools", destination, root / "store")
            published = MicroVMEnvironmentCatalog(destination).resolve("tools")
            self.assertEqual(published["erofs_dax_indices"], (0,))
            self.assertTrue(published["dax_binary"].stat().st_mode & 0o111)


if __name__ == "__main__":
    unittest.main()
