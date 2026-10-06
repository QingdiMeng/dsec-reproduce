"""Immutable environment descriptions consumed by the DSec container provider.

The catalog names task-independent execution environments.  An environment
uses either an ordered set of read-only EROFS layers or a split EROFS image
with metadata local and data in a local file or 3FS FUSE mount.  A rollout
pins the environment ID and storage choice; split variants advertise the same
content hash.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import threading


IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9.-]{0,127}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")


def file_sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def resolve_threefs_file(mount_name: str, file_name: str, size: int) -> tuple[Path, Path]:
    """Check a live 3FS source without reading the whole lazy object."""
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ValueError("Invalid 3FS object size")
    # Every path operation stays in a child: a FUSE request can enter D state,
    # in which case subprocess.run(timeout=...) blocks while waiting to reap it.
    # Do not use Popen as a context manager or wait after kill on timeout.
    probe = r'''
import json, pathlib, re, sys
mount = pathlib.Path(sys.argv[1]).resolve(strict=True)
path = pathlib.Path(sys.argv[2]).resolve(strict=True)
if path == mount or mount not in path.parents:
    raise ValueError("3FS object is outside its pinned mount")
def unescape(value):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)
with open("/proc/self/mountinfo") as stream:
    mounts = [line.split(" - ", 1) for line in stream]
if not any(unescape(left.split()[4]) == str(mount) and right.split()[0] == "fuse.hf3fs"
           for left, right in mounts):
    raise ValueError("3FS object source is not a live FUSE mount")
if path.stat().st_size != int(sys.argv[3]):
    raise ValueError("3FS object size changed")
print(json.dumps([str(mount), str(path)]))
'''
    process = subprocess.Popen([sys.executable, "-c", probe, mount_name, file_name, str(size)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        output, error = process.communicate(timeout=5)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        raise ValueError("3FS object probe timed out") from exc
    if process.returncode != 0:
        raise ValueError("3FS object probe failed: " + error.strip()[-300:])
    mount, path = (Path(value) for value in json.loads(output))
    return mount, path


class EnvironmentCatalog:
    def __init__(self, filename: str | Path):
        self.path = Path(filename).resolve(strict=True)
        payload = json.loads(self.path.read_text())
        entries = payload.get("environments")
        if payload.get("format") != 1 or not isinstance(entries, dict) or not entries:
            raise ValueError("Invalid DSec environment catalog")
        if any(not IDENTIFIER.fullmatch(name) for name in entries):
            raise ValueError("Invalid DSec environment ID")
        self.entries = entries
        self.digest = file_sha256(self.path)
        self._verified_files = {}
        self._verification_lock = threading.Lock()

    def _verify_file(self, path: Path, expected: str, error: str) -> None:
        """Hash once per daemon lifetime, then reject changed file identities."""
        key = (str(path), expected)

        def identity():
            stat = path.stat()
            return (stat.st_dev, stat.st_ino, stat.st_mode, stat.st_size,
                    stat.st_mtime_ns, stat.st_ctime_ns)

        with self._verification_lock:
            before = identity()
            if self._verified_files.get(key) == before:
                return
            actual = file_sha256(path)
            after = identity()
            if before != after or actual != expected:
                raise ValueError(error)
            self._verified_files[key] = after

    def resolve(self, environment_id: str, storage: str) -> dict:
        if environment_id not in self.entries:
            raise ValueError(f"Unknown DSec environment: {environment_id}")
        item = self.entries[environment_id]
        if (item.get("backend") != "container" or
                not IMAGE.fullmatch(item.get("runtime_image", ""))):
            raise ValueError(f"Invalid EROFS environment: {environment_id}")
        if item.get("rootfs") == "erofs_layers":
            if storage not in ("local", "threefs_lazy"):
                raise ValueError("Unsupported EROFS layer storage")
            layers = item.get("layers")
            if not isinstance(layers, list) or not 1 <= len(layers) <= 17:
                raise ValueError("Invalid EROFS layer list")
            names = set()
            resolved = []
            for layer in layers:
                name = layer.get("name") if isinstance(layer, dict) else None
                if (not isinstance(name, str) or
                        not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name) or
                        name in names or not DIGEST.fullmatch(layer.get("sha256", ""))):
                    raise ValueError("Invalid EROFS layer identity")
                names.add(name)
                if storage == "threefs_lazy" and "threefs_file" in layer:
                    size = layer.get("bytes")
                    if (not isinstance(size, int) or isinstance(size, bool) or size <= 0 or
                            not isinstance(layer.get("threefs_mount"), str) or
                            not isinstance(layer.get("threefs_file"), str) or
                            Path(layer["threefs_file"]).name != layer["sha256"] + ".erofs"):
                        raise ValueError("Invalid content-addressed 3FS EROFS layer")
                    mount, path = resolve_threefs_file(
                        layer["threefs_mount"], layer["threefs_file"], size)
                    resolved.append({"name": name, "file": path, "sha256": layer["sha256"],
                                     "source": "threefs_lazy", "bytes": size,
                                     "threefs_mount": mount})
                else:
                    path = Path(layer["file"]).resolve(strict=True)
                    self._verify_file(path, layer["sha256"], f"EROFS layer changed: {name}")
                    resolved.append({"name": name, "file": path,
                                     "sha256": layer["sha256"], "source": "local"})
            if storage == "threefs_lazy" and not any(
                    layer["source"] == "threefs_lazy" for layer in resolved):
                raise ValueError("3FS EROFS profile requires at least one remote layer")
            return {"environment_id": environment_id,
                    "runtime_image": item["runtime_image"],
                    "rootfs": "erofs_layers", "layers": resolved,
                    "catalog_sha256": self.digest}
        if (item.get("rootfs") != "erofs_split" or
                not DIGEST.fullmatch(item.get("metadata_sha256", "")) or
                not DIGEST.fullmatch(item.get("data_sha256", "")) or
                not isinstance(item.get("data_bytes"), int) or
                isinstance(item["data_bytes"], bool) or item["data_bytes"] < 1):
            raise ValueError(f"Invalid EROFS environment: {environment_id}")
        metadata = Path(item["metadata"]).resolve(strict=True)
        helper = Path(item["mount_helper"]).resolve(strict=True)
        self._verify_file(metadata, item["metadata_sha256"], "EROFS metadata changed")
        if storage == "local":
            data = Path(item["local_blob"]).resolve(strict=True)
            if data.stat().st_size != item["data_bytes"]:
                raise ValueError("Local EROFS data changed")
            self._verify_file(data, item["data_sha256"], "Local EROFS data changed")
            remote_mount = None
        elif storage == "threefs_lazy":
            remote_mount, data = resolve_threefs_file(
                item["threefs_mount"], item["threefs_blob"], item["data_bytes"])
        else:
            raise ValueError(f"Unsupported storage backend: {storage}")
        return {"environment_id": environment_id, "runtime_image": item["runtime_image"],
                "rootfs": "erofs_split",
                "metadata": metadata, "metadata_sha256": item["metadata_sha256"],
                "data": data, "data_sha256": item["data_sha256"],
                "remote_mount": remote_mount, "mount_helper": helper,
                "catalog_sha256": self.digest}


class MicroVMEnvironmentCatalog(EnvironmentCatalog):
    """Pinned, task-independent Firecracker boot disks and EROFS layer disks."""

    def environment_digest(self, environment_id: str) -> str:
        if environment_id not in self.entries:
            raise ValueError(f"Unknown DSec environment: {environment_id}")
        encoded = json.dumps(self.entries[environment_id], sort_keys=True,
                             separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def resolve(self, environment_id: str, storage: str = "local") -> dict:
        if storage not in ("local", "threefs_lazy"):
            raise ValueError("Unsupported microVM layer storage")
        if environment_id not in self.entries:
            raise ValueError(f"Unknown DSec environment: {environment_id}")
        item = self.entries[environment_id]
        if item.get("backend") != "microvm" or item.get("rootfs") != "erofs_layers":
            raise ValueError(f"Invalid microVM environment: {environment_id}")
        # OverlayBD is itself the complete boot disk. A source ext4 export is
        # not consumed at runtime and need not be retained or rehashed.
        boot = None
        if (item.get("root_block_backend") != "overlaybd-ublk" or
                "boot_template" in item or "boot_sha256" in item):
            if not isinstance(item.get("boot_template"), str):
                raise ValueError("Invalid microVM boot template")
            boot = Path(item["boot_template"]).resolve(strict=True)
            if not DIGEST.fullmatch(item.get("boot_sha256", "")):
                raise ValueError("MicroVM boot template changed")
            self._verify_file(boot, item["boot_sha256"], "MicroVM boot template changed")
        if not isinstance(item.get("kernel"), str):
            raise ValueError("Invalid microVM guest kernel")
        kernel = Path(item["kernel"]).resolve(strict=True)
        if not DIGEST.fullmatch(item.get("kernel_sha256", "")):
            raise ValueError("MicroVM guest kernel changed")
        self._verify_file(kernel, item["kernel_sha256"], "MicroVM guest kernel changed")
        layers = item.get("layers")
        if not isinstance(layers, list) or not 1 <= len(layers) <= 17:
            raise ValueError("Invalid microVM EROFS layer list")
        resolved = []
        names = set()
        dax_indices = []
        for index, layer in enumerate(layers):
            if not isinstance(layer, dict):
                raise ValueError("Invalid microVM EROFS layer")
            name = layer.get("name")
            if (not isinstance(name, str) or
                    not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name) or name in names or
                    not DIGEST.fullmatch(layer.get("sha256", ""))):
                raise ValueError("Invalid microVM EROFS layer identity")
            if storage == "threefs_lazy" and "threefs_file" in layer:
                size = layer.get("bytes")
                if (not isinstance(size, int) or isinstance(size, bool) or size <= 0 or
                        not isinstance(layer.get("threefs_mount"), str) or
                        not isinstance(layer.get("threefs_file"), str) or
                        Path(layer["threefs_file"]).name != layer["sha256"] + ".erofs"):
                    raise ValueError("Invalid content-addressed 3FS microVM layer")
                mount, path = resolve_threefs_file(
                    layer["threefs_mount"], layer["threefs_file"], size)
                source = "threefs_lazy"
            else:
                if not isinstance(layer.get("file"), str):
                    raise ValueError("Invalid local microVM layer")
                path = Path(layer["file"]).resolve(strict=True)
                self._verify_file(path, layer["sha256"],
                                  f"MicroVM EROFS layer changed: {name}")
                source = "local"
            names.add(name)
            dax = layer.get("dax", False)
            if not isinstance(dax, bool):
                raise ValueError("Invalid microVM EROFS DAX selection")
            if dax:
                if (source != "local" or
                        layer.get("bytes") != path.stat().st_size or
                        path.stat().st_size % (2 * 1024 * 1024)):
                    raise ValueError("DAX EROFS layer must be local, pinned and 2 MiB aligned")
                dax_indices.append(index)
            resolved.append({"name": name, "file": path, "sha256": layer["sha256"],
                             "source": source, "dax": dax})
        if storage == "threefs_lazy" and not any(
                layer["source"] == "threefs_lazy" for layer in resolved):
            raise ValueError("3FS microVM profile requires at least one remote layer")
        dax_binary = None
        if dax_indices:
            if (not isinstance(item.get("dax_binary"), str) or
                    not DIGEST.fullmatch(item.get("dax_binary_sha256", ""))):
                raise ValueError("DAX EROFS requires a pinned Firecracker binary")
            dax_binary = Path(item["dax_binary"]).resolve(strict=True)
            self._verify_file(dax_binary, item["dax_binary_sha256"],
                              "DAX Firecracker binary changed")
        elif item.get("dax_binary") is not None or item.get("dax_binary_sha256") is not None:
            raise ValueError("DAX Firecracker binary has no DAX EROFS layer")
        cpus = item.get("cpus", 1)
        memory_mb = item.get("memory_mb", 256)
        command_timeout_ms = item.get("command_timeout_ms", 30000)
        if (not isinstance(cpus, int) or isinstance(cpus, bool) or not 1 <= cpus <= 32 or
                not isinstance(memory_mb, int) or isinstance(memory_mb, bool) or
                not 128 <= memory_mb <= 65536 or
                not isinstance(command_timeout_ms, int) or isinstance(command_timeout_ms, bool) or
                not 1 <= command_timeout_ms <= 900000):
            raise ValueError("Invalid microVM CPU, memory or command timeout limit")
        root_backend = item.get("root_block_backend", "file-ext4")
        overlaybd_image = None
        if root_backend == "overlaybd-ublk":
            root = item.get("overlaybd_root")
            if not isinstance(root, dict) or not isinstance(root.get("image"), str) or \
                    not DIGEST.fullmatch(root.get("image_sha256", "")):
                raise ValueError("Invalid OverlayBD root identity")
            overlaybd_image = Path(root["image"]).resolve(strict=True)
            self._verify_file(overlaybd_image, root["image_sha256"],
                              "OverlayBD root image config changed")
            image = json.loads(overlaybd_image.read_text())
            lowers = root.get("lowers")
            if (not isinstance(image, dict) or
                    not isinstance(lowers, list) or not lowers or
                    any(not isinstance(lower, dict) for lower in lowers) or
                    image.get("upper") != {} or
                    image.get("lowers") != [{"file": lower.get("file")}
                                             for lower in lowers]):
                raise ValueError("OverlayBD root lower list changed")
            for lower in lowers:
                if (not isinstance(lower, dict) or not isinstance(lower.get("file"), str) or
                        not DIGEST.fullmatch(lower.get("sha256", "")) or
                        not isinstance(lower.get("bytes"), int) or
                        isinstance(lower["bytes"], bool) or lower["bytes"] <= 0):
                    raise ValueError("Invalid OverlayBD lower identity")
                path = Path(lower["file"]).resolve(strict=True)
                if path.stat().st_size != lower["bytes"]:
                    raise ValueError("OverlayBD lower changed")
                self._verify_file(path, lower["sha256"], "OverlayBD lower changed")
        elif root_backend != "file-ext4" or item.get("overlaybd_root") is not None:
            raise ValueError("Unsupported microVM root block backend")
        return {"environment_id": environment_id, "boot_template": boot,
                "kernel": kernel, "kernel_sha256": item["kernel_sha256"],
                "layers": resolved, "cpus": cpus, "memory_mb": memory_mb,
                "command_timeout_ms": command_timeout_ms,
                "root_block_backend": root_backend,
                "overlaybd_root_image": overlaybd_image,
                "erofs_dax_indices": tuple(dax_indices), "dax_binary": dax_binary,
                "environment_sha256": self.environment_digest(environment_id),
                "catalog_sha256": self.digest}
