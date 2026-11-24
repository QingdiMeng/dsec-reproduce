"""Publication must pin bytes and leave catalog entries atomic and immutable."""

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dsec.storage import publication
from artifact_publisher import publish_microvm
from environment_catalog import MicroVMEnvironmentCatalog
from dsec.contracts.environments import artifact_revision
from dsec.contracts.native import STREAM_FEATURE, execution_requirements


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

    def test_publication_rebinds_only_a_valid_existing_execution_claim(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            source, _ = self.fixture(root)
            value = json.loads(source.read_text())
            entry = value['environments']['tools']
            entry['execution_contract'] = dict(schema=1,artifact_revision=artifact_revision(entry),
                                              capabilities=[STREAM_FEATURE])
            source.write_text(json.dumps(value))
            old = entry['execution_contract']['artifact_revision']
            destination = root/'published.json'
            publish_microvm(source,'tools',destination,root/'store')
            published = MicroVMEnvironmentCatalog(destination)
            item = published.entries['tools']
            self.assertNotEqual(item['execution_contract']['artifact_revision'],old)
            self.assertEqual(item['execution_contract']['artifact_revision'],artifact_revision(item))
            self.assertEqual(published.execution_contracts()[0].capabilities,
                             execution_requirements([STREAM_FEATURE]))
            entry['execution_contract']['artifact_revision'] = 'b'*64
            source.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError,'pinned environment'):
                publish_microvm(source,'tools',root/'bad.json',root/'bad-store')
            self.assertFalse((root/'bad.json').exists())
            self.assertFalse((root/'bad-store').exists())

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

    def test_overlaybd_only_publication_keeps_ordered_pins_without_ext4_export(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work).resolve()
            source, files = self.fixture(root)
            config = json.loads(source.read_text())
            entry = config["environments"]["tools"]
            del entry["boot_template"], entry["boot_sha256"]
            files["boot.ext4"].unlink()
            child = root / "child.commit"
            child.write_bytes(b"delta")
            overlay = entry["overlaybd_root"]
            overlay["lowers"].append(dict(file=str(child), sha256=sha(b"delta"), bytes=5))
            image = Path(overlay["image"])
            image.write_text(json.dumps({"lowers": [{"file": pin["file"]} for pin in overlay["lowers"]],
                                         "upper": {}}))
            overlay["image_sha256"] = sha(image.read_bytes())
            source.write_text(json.dumps(config))
            output = root / "published.json"
            publish_microvm(source, "tools", output, root / "store")
            published = MicroVMEnvironmentCatalog(output)
            self.assertIsNone(published.resolve("tools")["boot_template"])
            self.assertNotIn("boot_sha256", published.entries["tools"])
            objects = root / "store" / "objects"
            self.assertFalse(list(objects.glob("*.ext4")))
            lowers = published.entries["tools"]["overlaybd_root"]["lowers"]
            self.assertEqual([pin["file"] for pin in lowers],
                             [str(objects / (sha(data) + ".commit")) for data in (b"lower", b"delta")])

    def test_each_publication_reads_shared_source_and_existing_object_once(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work).resolve()
            source, files = self.fixture(root)
            store = root / "store"
            publish_microvm(source, "tools", root / "first.json", store)
            target = store / "objects" / (sha(b"tools") + ".erofs")
            original_open = Path.open
            reads = []

            def record(path, mode="r", *args, **kwargs):
                if mode == "rb" and path in (files["tools.erofs"], target):
                    reads.append(path)
                return original_open(path, mode, *args, **kwargs)

            for name in ("second", "third"):
                reads.clear()
                with patch.object(Path, "open", record):
                    publish_microvm(source, "tools", root / (name + ".json"), store)
                self.assertEqual(reads, [files["tools.erofs"], target])

    def test_source_replaced_after_catalog_resolution_is_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            source, files = self.fixture(root)
            store = root / "store"
            publish_microvm(source, "tools", root / "first.json", store)
            copy_object = publication._copy_object

            def replace_before_copy(path, *args, **kwargs):
                if path == files["tools.erofs"]:
                    replacement = root / "replacement"
                    replacement.write_bytes(b"other")
                    replacement.replace(path)
                return copy_object(path, *args, **kwargs)

            destination = root / "second.json"
            with patch.object(publication, "_copy_object", replace_before_copy):
                with self.assertRaisesRegex(ValueError, "Artifact digest mismatch"):
                    publish_microvm(source, "tools", destination, store)
            self.assertFalse(destination.exists())

    def test_target_replaced_before_commit_preserves_previous_catalog(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            source, _ = self.fixture(root)
            store = root / "store"
            destination = root / "published.json"
            publish_microvm(source, "tools", destination, store)
            before = destination.read_bytes()
            catalog = json.loads(source.read_text())
            catalog["environments"]["next"] = catalog["environments"]["tools"]
            source.write_text(json.dumps(catalog))
            commit = publication._atomic_catalog

            def replace_before_commit(path, name, entry, verification):
                replacement = root / "replacement"
                replacement.write_bytes(b"other")
                replacement.replace(entry["layers"][0]["file"])
                return commit(path, name, entry, verification)

            with patch.object(publication, "_atomic_catalog", replace_before_commit):
                with self.assertRaisesRegex(ValueError, "EROFS layer changed"):
                    publish_microvm(source, "next", destination, store)
            self.assertEqual(destination.read_bytes(), before)

    def test_conflicting_link_winner_is_checked_before_publication(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            source, _ = self.fixture(root)
            link = publication.os.link

            def competing_link(temp, target):
                if target.suffix == ".erofs":
                    target.write_bytes(b"other")
                return link(temp, target)

            destination = root / "published.json"
            with patch.object(publication.os, "link", competing_link):
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
