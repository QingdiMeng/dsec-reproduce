"""Catalog identity and immutability checks without Docker or 3FS services."""

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from environment_catalog import EnvironmentCatalog, MicroVMEnvironmentCatalog, resolve_threefs_file
from framework_profile import FrameworkProfile, UnsupportedProfile


def digest(data):
    return hashlib.sha256(data).hexdigest()


class CatalogTest(unittest.TestCase):
    def test_threefs_probe_timeout_does_not_wait_for_stuck_child(self):
        with patch("environment_catalog.subprocess.Popen") as factory:
            child = factory.return_value
            child.communicate.side_effect = subprocess.TimeoutExpired("stat", 5)
            with self.assertRaisesRegex(ValueError, "timed out"):
                resolve_threefs_file("/mnt/threefs", "/mnt/threefs/object", 4)
            child.kill.assert_called_once()
            child.wait.assert_not_called()

    def test_threefs_probe_rejects_local_mount(self):
        if not Path("/proc/self/mountinfo").exists():
            self.skipTest("Linux mountinfo required")
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            data = root / "object"
            data.write_bytes(b"data")
            with self.assertRaisesRegex(ValueError, "not a live FUSE"):
                resolve_threefs_file(str(root), str(data), 4)

    def test_generic_microvm_environment_pins_boot_layers_and_resources(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            boot, layer, kernel = root / "boot.ext4", root / "tools.erofs", root / "kernel"
            boot.write_bytes(b"boot")
            layer.write_bytes(b"layer")
            kernel.write_bytes(b"kernel")
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps({"format": 1, "environments": {
                "general-tools": {"backend": "microvm", "rootfs": "erofs_layers",
                                  "boot_template": str(boot), "boot_sha256": digest(boot.read_bytes()),
                                  "kernel": str(kernel), "kernel_sha256": digest(kernel.read_bytes()),
                                  "layers": [{"name": "tools", "file": str(layer),
                                              "sha256": digest(layer.read_bytes())}],
                                  "cpus": 2, "memory_mb": 512}}}))
            resolved = MicroVMEnvironmentCatalog(catalog_path).resolve("general-tools")
            original_identity = resolved["environment_sha256"]
            self.assertEqual((resolved["cpus"], resolved["memory_mb"]), (2, 512))
            self.assertEqual(resolved["command_timeout_ms"], 30000)
            self.assertEqual(resolved["layers"][0]["file"], layer.resolve())
            profile = FrameworkProfile(backend="microvm", environment="erofs_layers",
                                       environment_id="general-tools")
            self.assertEqual(profile.validate_runtime(), profile)
            payload = json.loads(catalog_path.read_text())
            payload["environments"]["unrelated-env"] = dict(payload["environments"]["general-tools"])
            catalog_path.write_text(json.dumps(payload))
            changed_catalog = MicroVMEnvironmentCatalog(catalog_path)
            self.assertEqual(changed_catalog.resolve("general-tools")["environment_sha256"],
                             original_identity)
            self.assertNotEqual(changed_catalog.digest, resolved["catalog_sha256"])
            payload["environments"]["general-tools"]["command_timeout_ms"] = 0
            catalog_path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "command timeout"):
                MicroVMEnvironmentCatalog(catalog_path).resolve("general-tools")
            del payload["environments"]["general-tools"]["command_timeout_ms"]
            catalog_path.write_text(json.dumps(payload))
            layer.write_bytes(b"mutated")
            with self.assertRaisesRegex(ValueError, "changed"):
                MicroVMEnvironmentCatalog(catalog_path).resolve("general-tools")
            with self.assertRaisesRegex(ValueError, "changed"):
                MicroVMEnvironmentCatalog(catalog_path).resolve("general-tools", "threefs_lazy")

    def test_microvm_storage_variant_keeps_layer_identity(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            boot = root / "boot.ext4"
            kernel = root / "kernel"
            local = root / "layer.erofs"
            boot.write_bytes(b"boot")
            kernel.write_bytes(b"kernel")
            local.write_bytes(b"shared layer")
            layer_hash = digest(local.read_bytes())
            remote = root / (layer_hash + ".erofs")
            remote.write_bytes(local.read_bytes())
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps({"format": 1, "environments": {
                "general-tools": {"backend": "microvm", "rootfs": "erofs_layers",
                                  "boot_template": str(boot), "boot_sha256": digest(boot.read_bytes()),
                                  "kernel": str(kernel), "kernel_sha256": digest(kernel.read_bytes()),
                                  "layers": [{"name": "tools", "file": str(local),
                                              "sha256": layer_hash, "bytes": remote.stat().st_size,
                                              "threefs_mount": str(root),
                                              "threefs_file": str(remote)}]}}}))
            catalog = MicroVMEnvironmentCatalog(catalog_path)
            local_layer = catalog.resolve("general-tools", "local")["layers"][0]
            with patch("environment_catalog.resolve_threefs_file",
                       return_value=(root.resolve(), remote.resolve())):
                remote_layer = catalog.resolve("general-tools", "threefs_lazy")["layers"][0]
            self.assertEqual(local_layer["sha256"], remote_layer["sha256"])
            self.assertEqual((local_layer["source"], remote_layer["source"]),
                             ("local", "threefs_lazy"))

    def test_microvm_overlaybd_root_pins_image_and_lower(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            boot, kernel, layer, lower = [root / name for name in
                                          ("boot.ext4", "kernel", "tools.erofs", "root.commit")]
            for path, value in ((boot, b"boot"), (kernel, b"kernel"),
                                (layer, b"layer"), (lower, b"root lower")):
                path.write_bytes(value)
            image = root / "root-image.json"
            image.write_text(json.dumps({"lowers": [{"file": str(lower)}], "upper": {}}))
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps({"format": 1, "environments": {
                "generic-block": {"backend": "microvm", "rootfs": "erofs_layers",
                                  "boot_template": str(boot), "boot_sha256": digest(boot.read_bytes()),
                                  "kernel": str(kernel), "kernel_sha256": digest(kernel.read_bytes()),
                                  "layers": [{"name": "tools", "file": str(layer),
                                              "sha256": digest(layer.read_bytes())}],
                                  "root_block_backend": "overlaybd-ublk",
                                  "overlaybd_root": {"image": str(image),
                                                     "image_sha256": digest(image.read_bytes()),
                                                     "lowers": [{"file": str(lower),
                                                                 "sha256": digest(lower.read_bytes()),
                                                                 "bytes": lower.stat().st_size}]}}}}))
            resolved = MicroVMEnvironmentCatalog(catalog_path).resolve("generic-block")
            self.assertEqual(resolved["root_block_backend"], "overlaybd-ublk")
            self.assertEqual(resolved["overlaybd_root_image"], image.resolve())
            payload = json.loads(catalog_path.read_text())
            del payload["environments"]["generic-block"]["boot_template"]
            del payload["environments"]["generic-block"]["boot_sha256"]
            catalog_path.write_text(json.dumps(payload))
            boot.unlink()
            resolved = MicroVMEnvironmentCatalog(catalog_path).resolve("generic-block")
            self.assertIsNone(resolved["boot_template"])
            self.assertEqual(resolved["overlaybd_root_image"], image.resolve())
            lower.write_bytes(b"mutated")
            with self.assertRaisesRegex(ValueError, "OverlayBD lower changed"):
                MicroVMEnvironmentCatalog(catalog_path).resolve("generic-block")

    def test_generic_dax_layer_requires_local_aligned_artifact_and_pinned_vmm(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            boot, kernel, layer, binary = [root / name for name in
                                           ("boot.ext4", "kernel", "tools.erofs", "firecracker")]
            boot.write_bytes(b"boot")
            kernel.write_bytes(b"kernel")
            layer.write_bytes(b"x" * (2 * 1024 * 1024))
            binary.write_bytes(b"vmm")
            entry = {"backend": "microvm", "rootfs": "erofs_layers",
                     "boot_template": str(boot), "boot_sha256": digest(boot.read_bytes()),
                     "kernel": str(kernel), "kernel_sha256": digest(kernel.read_bytes()),
                     "layers": [{"name": "tools", "file": str(layer),
                                 "sha256": digest(layer.read_bytes()),
                                 "bytes": layer.stat().st_size, "dax": True}],
                     "dax_binary": str(binary),
                     "dax_binary_sha256": digest(binary.read_bytes())}
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps({"format": 1, "environments": {"dax-env": entry}}))
            resolved = MicroVMEnvironmentCatalog(catalog_path).resolve("dax-env")
            self.assertEqual(resolved["erofs_dax_indices"], (0,))
            self.assertEqual(resolved["dax_binary"], binary.resolve())
            binary.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "DAX Firecracker binary changed"):
                MicroVMEnvironmentCatalog(catalog_path).resolve("dax-env")
            binary.write_bytes(b"vmm")
            layer.write_bytes(b"short")
            entry["layers"][0].update(sha256=digest(layer.read_bytes()), bytes=layer.stat().st_size)
            catalog_path.write_text(json.dumps({"format": 1, "environments": {"dax-env": entry}}))
            with self.assertRaisesRegex(ValueError, "2 MiB aligned"):
                MicroVMEnvironmentCatalog(catalog_path).resolve("dax-env")

    def test_verified_local_layer_is_not_read_again_until_identity_changes(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            boot, kernel, layer = [root / name for name in
                                   ("boot.ext4", "kernel", "data.erofs")]
            for path, value in ((boot, b"boot"), (kernel, b"kernel"),
                                (layer, b"first")):
                path.write_bytes(value)
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps({"format": 1, "environments": {
                "cached": {"backend": "microvm", "rootfs": "erofs_layers",
                           "boot_template": str(boot), "boot_sha256": digest(boot.read_bytes()),
                           "kernel": str(kernel), "kernel_sha256": digest(kernel.read_bytes()),
                           "layers": [{"name": "data", "file": str(layer),
                                       "sha256": digest(layer.read_bytes())}]}}}))
            catalog = MicroVMEnvironmentCatalog(catalog_path)
            catalog.resolve("cached")
            with patch("environment_catalog.file_sha256",
                       side_effect=AssertionError("Unexpected full rehash")):
                catalog.resolve("cached")
            layer.write_bytes(b"other")  # Same size, different content and ctime.
            with self.assertRaisesRegex(ValueError, "MicroVM EROFS layer changed"):
                catalog.resolve("cached")

    def test_arbitrary_environment_and_content_pin(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            meta, blob, helper = [root / name for name in ("meta.erofs", "data.blob", "mount.py")]
            meta.write_bytes(b"pinned metadata")
            blob.write_bytes(b"pinned data")
            helper.write_text("# mounted by the runtime\n")
            entry = {"backend": "container", "rootfs": "erofs_split",
                     "runtime_image": "sha256:" + "a" * 64,
                     "metadata": str(meta), "metadata_sha256": digest(meta.read_bytes()),
                     "data_sha256": digest(blob.read_bytes()), "data_bytes": blob.stat().st_size,
                     "local_blob": str(blob), "mount_helper": str(helper),
                     "threefs_mount": str(root), "threefs_blob": str(blob)}
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps({"format": 1, "environments": {
                "my-general-env": entry}}))
            catalog = EnvironmentCatalog(catalog_path)
            resolved = catalog.resolve("my-general-env", "local")
            self.assertEqual(resolved["data"], blob.resolve())
            self.assertEqual(resolved["environment_id"], "my-general-env")
            with patch("environment_catalog.resolve_threefs_file",
                       side_effect=ValueError("3FS object source is not a live FUSE mount")):
                with self.assertRaisesRegex(ValueError, "not a live FUSE"):
                    catalog.resolve("my-general-env", "threefs_lazy")
            profile = FrameworkProfile(backend="container", environment="erofs_split",
                                       environment_id="my-general-env", lifecycle="stop")
            self.assertEqual(FrameworkProfile.from_dict(profile.as_dict()).validate_runtime(), profile)
            blob.write_bytes(b"changed data")
            with self.assertRaisesRegex(ValueError, "changed"):
                catalog.resolve("my-general-env", "local")

    def test_environment_id_required_for_generic_profile(self):
        with self.assertRaises(UnsupportedProfile):
            FrameworkProfile(backend="container", environment="erofs_split",
                             lifecycle="stop").validate_runtime()
        self.assertNotIn("environment_id", FrameworkProfile().as_dict())

    def test_variable_layer_count_and_order(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            files = [root / "first.erofs", root / "second.erofs"]
            for index, path in enumerate(files):
                path.write_bytes(f"layer-{index}".encode())
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps({"format": 1, "environments": {
                "two-layer-env": {"backend": "container", "rootfs": "erofs_layers",
                                  "runtime_image": "sha256:" + "a" * 64,
                                  "layers": [{"name": name, "file": str(path),
                                              "sha256": digest(path.read_bytes())}
                                             for name, path in zip(("base", "tools"), files)]}}}))
            catalog = EnvironmentCatalog(catalog_path)
            resolved = catalog.resolve("two-layer-env", "local")
            self.assertEqual([layer["name"] for layer in resolved["layers"]], ["base", "tools"])
            profile = FrameworkProfile(backend="container", environment="erofs_layers",
                                       environment_id="two-layer-env", lifecycle="stop")
            self.assertEqual(profile.validate_runtime(), profile)
            self.assertEqual(FrameworkProfile(backend="container", environment="erofs_layers",
                                              environment_id="two-layer-env", storage="threefs_lazy",
                                              lifecycle="stop").validate_runtime().storage,
                             "threefs_lazy")
            with self.assertRaisesRegex(ValueError, "at least one remote layer"):
                catalog.resolve("two-layer-env", "threefs_lazy")

    def test_mixed_local_and_content_addressed_threefs_layer(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            base = root / "base.erofs"
            base.write_bytes(b"shared base")
            remote_data = b"task layer"
            remote = root / (digest(remote_data) + ".erofs")
            remote.write_bytes(remote_data)
            entry = {"backend": "container", "rootfs": "erofs_layers",
                     "runtime_image": "sha256:" + "a" * 64,
                     "layers": [
                         {"name": "base", "file": str(base),
                          "sha256": digest(base.read_bytes())},
                         {"name": "task", "file": str(remote),
                          "sha256": digest(remote_data), "bytes": len(remote_data),
                          "threefs_mount": str(root), "threefs_file": str(remote)}]}
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps({"format": 1, "environments": {
                "mixed-env": entry}}))
            catalog = EnvironmentCatalog(catalog_path)
            with patch("environment_catalog.resolve_threefs_file",
                       return_value=(root.resolve(), remote.resolve())):
                layers = catalog.resolve("mixed-env", "threefs_lazy")["layers"]
            self.assertEqual([item["source"] for item in layers], ["local", "threefs_lazy"])
            self.assertEqual(layers[1]["file"], remote.resolve())
            with patch("environment_catalog.resolve_threefs_file",
                       side_effect=ValueError("3FS object source is not a live FUSE mount")):
                with self.assertRaisesRegex(ValueError, "live FUSE"):
                    catalog.resolve("mixed-env", "threefs_lazy")


if __name__ == "__main__":
    unittest.main()
