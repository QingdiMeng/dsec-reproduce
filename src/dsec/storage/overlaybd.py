"""DSec writable ext4 root-disk lifecycle using upstream Rust OverlayBD+ublk."""

import grp
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import stat
import struct
import time
import uuid

from dsec.storage.ublk import UblkDaemonClient


class OverlayBDRootStore:
    def __init__(self, socket_path, global_config, source_images, *, permission_container=None):
        self.client = UblkDaemonClient(socket_path)
        self.global_config = Path(global_config).resolve(strict=True)
        self.source_images = {name: Path(path).resolve(strict=True)
                              for name, path in source_images.items()}
        if (permission_container is not None and
                (not permission_container or not permission_container.replace("-", "").replace("_", "").isalnum())):
            raise ValueError("OverlayBD permission container name is invalid")
        self.permission_container = permission_container
        self.kvm_gid = grp.getgrnam("kvm").gr_gid

    def configure_shared_layers(self, root):
        from dsec.storage.layers import SharedSnapshotLayers
        self.shared_layers = SharedSnapshotLayers(root, self.share_directory)

    def source_for(self, environment_id):
        return self.source_images[environment_id]

    def socket_identity(self):
        path = self.client.socket_path
        info = path.stat()
        if not stat.S_ISSOCK(info.st_mode):
            raise RuntimeError("ublk daemon control path is not a socket")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.settimeout(2)
            stream.connect(str(path))
            pid, uid, _ = struct.unpack("3i", stream.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        if uid != 0 or pid < 1:
            raise RuntimeError("ublk daemon socket peer is not root")
        current = path.stat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise RuntimeError("ublk daemon socket changed during identity check")
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        start_ticks = fields[19]
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return [info.st_dev, info.st_ino, pid, start_ticks, boot_id]

    def share_directory(self, path, *, mode=0o2770):
        """Grant the ublk service group only the requested directory access."""
        path = Path(path)
        os.chown(path, -1, self.kvm_gid)
        path.chmod(mode)

    @staticmethod
    def require_daemon_group_access(pid):
        """Reject a root service configuration that prevents user cleanup.

        Upstream atomically creates (and owns) a previously absent runtime
        directory. Its umask must preserve the setgid parent's group access.
        Check before sending a create request, while no device exists yet.
        """
        fields = Path(f"/proc/{pid}/status").read_text().splitlines()
        masks = [line.split()[1] for line in fields if line.startswith("Umask:")]
        if len(masks) != 1:
            raise RuntimeError("Cannot verify root ublk daemon umask; creation refused")
        mask = int(masks[0], 8)
        if mask & 0o070:
            raise PermissionError(
                f"Root ublk daemon umask {mask:04o} removes storage group permissions; "
                "configure its service UMask=0007 before creating sandboxes"
            )

    def create(self, source_image, directory, stable_path):
        source_image = Path(source_image).resolve(strict=True)
        identity = self.socket_identity()
        self.require_daemon_group_access(identity[2])
        runtime = Path(directory) / ("ublk-runtime-" + uuid.uuid4().hex[:12])
        try:
            response = self.client.create_runtime_device(source_image,
                                                         self.global_config, runtime)
        except Exception:
            if runtime.exists():
                shutil.rmtree(runtime)
            raise
        dev_id, device = response["dev_id"], Path(response["device_path"])
        try:
            if not re.fullmatch(r"/dev/ublkb[0-9]+", str(device)):
                raise ValueError("ublk daemon returned an unexpected block-device path")
            if self.permission_container:
                subprocess.run(["docker", "exec", self.permission_container,
                                "chown", f"0:{self.kvm_gid}", str(device)], check=True)
                subprocess.run(["docker", "exec", self.permission_container,
                                "chmod", "660", str(device)], check=True)
            else:
                deadline = time.monotonic() + 5
                while True:
                    mode = device.stat()
                    if (mode.st_gid == self.kvm_gid and
                            stat.S_ISBLK(mode.st_mode) and
                            mode.st_mode & stat.S_IRGRP and mode.st_mode & stat.S_IWGRP):
                        break
                    if time.monotonic() >= deadline:
                        raise PermissionError(f"ublk device has no kvm group access: {device}")
                    time.sleep(.05)
            stable_path = Path(stable_path)
            new_link = stable_path.with_name(stable_path.name + ".new")
            new_link.unlink(missing_ok=True)
            new_link.symlink_to(device)
            os.replace(new_link, stable_path)
            return dev_id, runtime
        except Exception:
            self.client.delete(dev_id)
            if runtime.exists():
                shutil.rmtree(runtime)
            raise

    def snapshot(self, dev_id, current_image, staging, target):
        """Seal the live upper and prepare the *published* next source config.

        The daemon may mutate live state before replying.  Callers must mark
        transport errors terminal and must never resume that VMM blindly.
        """
        sandbox_dir = Path(target).parent
        layers_dir = sandbox_dir / "disk-layers"
        layers_dir.mkdir(mode=0o700, exist_ok=True)
        self.share_directory(layers_dir)
        diff = layers_dir / ("layer-" + uuid.uuid4().hex + ".commit")
        result = self.client.restack_snapshot(dev_id, diff)
        if not diff.is_file():
            raise RuntimeError("OverlayBD restack returned without a layer")
        with diff.open("rb") as stream:
            os.fsync(stream.fileno())
        directory_fd = os.open(layers_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        current = json.loads(Path(current_image).read_text())
        lowers = current.get("lowers")
        if not isinstance(lowers, list) or not lowers:
            raise ValueError("OverlayBD source image has no immutable lowers")
        next_image = {"lowers": lowers + [{"file": str(diff)}],
                      "upper": {}}
        image_file = Path(staging) / "disk-image.json"
        image_file.write_text(json.dumps(next_image, indent=2) + "\n")
        image_file.chmod(0o660)
        return diff

    def disk_layers(self, image_file, sandbox_dir, source_image=None):
        """Return private and held shared immutable checkpoint layers."""
        config = json.loads(Path(image_file).read_text())
        lowers = config.get("lowers")
        if not isinstance(lowers, list) or not lowers:
            raise ValueError("OverlayBD image has no lower layers")
        if source_image is not None:
            base = json.loads(Path(source_image).read_text()).get("lowers")
            if not isinstance(base, list) or not base or lowers[:len(base)] != base:
                raise ValueError("OverlayBD image base layers changed")
        layers_dir = Path(sandbox_dir) / "disk-layers"
        result = []
        for layer in lowers:
            filename = layer.get("file") if isinstance(layer, dict) else None
            if not isinstance(filename, str):
                raise ValueError("OverlayBD lower layer has no file path")
            path = Path(filename)
            if path.parent == layers_dir:
                if not re.fullmatch(r"layer-[0-9a-f]{32}\.commit", path.name):
                    raise ValueError("Invalid persistent OverlayBD layer name")
                if path.is_symlink() or not path.is_file():
                    raise ValueError("Persistent OverlayBD layer is unavailable")
                result.append(path)
            elif getattr(self, "shared_layers", None) and self.shared_layers.owns(path):
                self.shared_layers.validate(path)
                # Content addressing can coalesce byte-identical generations.
                # Keep lower order in the image, but hash each object only once.
                if path not in result:
                    result.append(path)
            elif path.is_relative_to(Path(sandbox_dir)):
                raise ValueError("OverlayBD image references an unmanaged sandbox layer")
        return result

    def prune_unreferenced_layers(self, image_file, sandbox_dir, source_image):
        """Reclaim unpublished commits after a fully published paused snapshot."""
        layers_dir = Path(sandbox_dir) / "disk-layers"
        if not layers_dir.exists():
            return []
        keep = {path.name for path in
                self.disk_layers(image_file, sandbox_dir, source_image)}
        removed = []
        for path in layers_dir.iterdir():
            if path.name in keep:
                continue
            if (not re.fullmatch(r"layer-[0-9a-f]{32}\.commit", path.name) or
                    path.is_symlink() or not path.is_file()):
                raise ValueError("Unexpected OverlayBD persistent layer entry")
            path.unlink()
            removed.append(path.name)
        if removed:
            directory_fd = os.open(layers_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return removed

    def delete(self, dev_id, runtime_dir=None, sandbox_dir=None):
        if dev_id is not None:
            self.client.delete(dev_id)
        if runtime_dir is not None:
            runtime = Path(runtime_dir)
            if (sandbox_dir is None or runtime.parent != Path(sandbox_dir) or
                    not re.fullmatch(r"ublk-runtime-[0-9a-f]{12}", runtime.name)):
                raise ValueError("Refusing to remove unexpected OverlayBD runtime directory")
            if runtime.exists():
                shutil.rmtree(runtime)
