"""A file delta is useful only for its approved bytes and exact parent."""
import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from dsec.storage.catalog import MicroVMEnvironmentCatalog, file_sha256
from dsec_image.pack_diff import PROFILE, capture, publish


def policy_for(revision):
    return dict(parent="base", revision=revision, files=[
        dict(path="/opt/demo", size=3, mode=0o755, sha256=hashlib.sha256(b"abc").hexdigest())])


class CaptureTest(unittest.IsolatedAsyncioTestCase):
    async def test_generation_content_parent_and_exclusive_output(self):
        policy = policy_for("a" * 64)
        status = dict(id="one", state="RUNNING", environment_id="base", storage="local",
                      generation=0, environment_manifest_sha256=policy["revision"])
        builder = Mock(id="one", status=AsyncMock(return_value=status),
                       read_file=AsyncMock(return_value=b"abc"))
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "delta"
            builder.status.side_effect = [status, dict(status, generation=1)]
            with self.assertRaisesRegex(ValueError, "generation"):
                await capture(builder, policy, output)
            builder.status.side_effect = None
            builder.status.return_value = dict(status, environment_id="other")
            with self.assertRaisesRegex(ValueError, "parent"):
                await capture(builder, policy, output)
            builder.status.return_value = status
            builder.read_file.return_value = b"xyz"
            with self.assertRaisesRegex(ValueError, "publisher policy"):
                await capture(builder, policy, output)
            self.assertFalse(output.exists())
            builder.read_file.return_value = b"abc"
            await capture(builder, policy, output)
            with self.assertRaises(FileExistsError):
                await capture(builder, policy, output)
            self.assertEqual(json.loads(output.read_text())["files"], {"/opt/demo": "YWJj"})

    async def test_policy_rejects_unsafe_paths_and_modes_before_guest_access(self):
        builder = Mock()
        for changes in (dict(path="/tmp/answer"), dict(path="/opt/../etc/a"),
                        dict(mode=0o4755), dict(size=32 * 1024**2)):
            policy = policy_for("a" * 64)
            policy["files"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                await capture(builder, policy, Path("unused"))
        builder.status.assert_not_called()


@unittest.skipUnless(all(shutil.which(x) for x in ("mkfs.ext4", "debugfs", "e2fsck")),
                     "Linux e2fsprogs required")
class PublicationTest(unittest.TestCase):
    def test_parent_and_file_pins_atomicity_and_symlink_parents(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            stage = root / "stage"
            (stage / "opt").mkdir(parents=True)
            (stage / "opt/link").symlink_to("/tmp")
            (stage / "opt/existing").write_text("original")
            boot = root / "parent.ext4"
            with boot.open("wb") as stream:
                stream.truncate(16 * 1024**2)
            subprocess.run(["mkfs.ext4", "-qF", "-d", str(stage), str(boot)], check=True)
            layer = root / "layer"
            layer.write_bytes(b"layer")
            parent = root / "parent.json"
            entry = dict(backend="microvm", rootfs="erofs_layers", pack_diff_root=PROFILE,
                         boot_template=str(boot), boot_sha256=file_sha256(boot),
                         kernel=str(layer), kernel_sha256=file_sha256(layer),
                         layers=[dict(name="base", file=str(layer), sha256=file_sha256(layer))])
            parent.write_text(json.dumps(dict(format=1, environments=dict(base=entry))))
            rev = MicroVMEnvironmentCatalog(parent).environment_digest("base")
            delta, dst, store = root / "delta", root / "published", root / "store"
            def attempt(policy, content, extra=None):
                payload = {policy["files"][0]["path"]: base64.b64encode(content).decode()}
                delta.write_text(json.dumps(dict(schema=1, policy=policy, files={**payload, **(extra or {})})))
                return publish(policy, delta, parent, "child", dst, store)

            for revision, path, data, extra in (
                    ("b" * 64, "/opt/demo", b"abc", None),
                    (rev, "/opt/demo", b"bad", None),
                    (rev, "/opt/demo", b"abc", {"/tmp/answer": "YWJj"}),
                    (rev, "/opt/link/data", b"abc", None),
                    (rev, "/opt/existing", b"abc", None)):
                policy = policy_for(revision)
                policy["files"][0]["path"] = path
                with self.subTest(path=path, revision=revision), self.assertRaises(ValueError):
                    attempt(policy, data, extra)
                self.assertFalse(dst.exists())
                self.assertFalse(store.exists())
            attempt(policy_for(rev), b"abc")
            self.assertEqual(file_sha256(boot), entry["boot_sha256"])
            child = MicroVMEnvironmentCatalog(dst).resolve("child")
            self.assertNotEqual(file_sha256(child["boot_template"]), entry["boot_sha256"])
