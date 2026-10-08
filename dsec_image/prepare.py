"""Build an explicitly selected local OCI image as an EROFS microVM recipe."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess

from dsec.storage.catalog import MicroVMEnvironmentCatalog
from .boot import build
from .layers import convert

IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}\Z")


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare(*, image, tools_image, output, environment_id, kernel, agent_source,
            busybox, cpus=1, memory_mb=512, timeout_ms=30000, network=False,
            boot_size_mb=256, reserve_gib=10, metadata=None):
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        raise ValueError("Image preparation requires Linux x86_64")
    if not IMAGE_ID.fullmatch(image) or not IMAGE_ID.fullmatch(tools_image):
        raise ValueError("Image and tools must be exact local sha256 image IDs")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", environment_id):
        raise ValueError("Invalid environment ID")
    if (not 1 <= cpus <= 32 or not 128 <= memory_mb <= 65536 or
            not 30000 <= timeout_ms <= 900000 or not 256 <= boot_size_mb <= 102400 or reserve_gib < 1):
        raise ValueError("Invalid guest resources, timeout or disk reserve")
    output = Path(output).resolve()
    kernel, agent_source, busybox = (Path(p).resolve(strict=True) for p in (kernel, agent_source, busybox))
    if output.exists():
        raise FileExistsError("Use a fresh image output directory")
    inspected = json.loads(subprocess.check_output(["docker", "image", "inspect", image], text=True))[0]
    tools_id = subprocess.check_output(["docker", "image", "inspect", tools_image, "--format", "{{.Id}}"], text=True).strip()
    if inspected["Id"] != image or tools_id != tools_image:
        raise ValueError("Local image identity differs from its pin")
    if inspected.get("Architecture") != "amd64" or inspected.get("Os") != "linux":
        raise ValueError("Guest image must be Linux amd64")
    if not 1 <= len(inspected["RootFS"]["Layers"]) <= 12:
        raise ValueError("Direct-drive builder requires 1..12 OCI layers; compact larger recipes separately")
    ancestor = output.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    required = reserve_gib * 1024**3 + 3 * inspected["Size"] + boot_size_mb * 1024**2
    if shutil.disk_usage(ancestor).free < required:
        raise RuntimeError("Image preparation would violate the disk reserve")
    output.mkdir(parents=True)
    result = dict(metadata or {}, status="running", environment_id=environment_id,
                  image_id=image, tools_image_id=tools_image,
                  root_block_backend="file-ext4", network=network,
                  agent_source_sha256=sha(agent_source), busybox_sha256=sha(busybox))
    try:
        manifest_path = convert(image, output / "erofs", tools_image)
        layers = json.loads(manifest_path.read_text())["layers"]
        boot = output / "boot.ext4"
        build(manifest_path, agent_source, busybox, boot, timeout_ms, network, boot_size_mb)
        entry = {"backend": "microvm", "rootfs": "erofs_layers",
                 "boot_template": str(boot), "boot_sha256": sha(boot),
                 "kernel": str(kernel), "kernel_sha256": sha(kernel),
                 "cpus": cpus, "memory_mb": memory_mb, "command_timeout_ms": timeout_ms,
                 "layers": [{"name": "layer" + str(i), "file": layer["erofs"],
                             "sha256": layer["erofs_sha256"], "bytes": layer["erofs_bytes"]}
                            for i, layer in enumerate(layers)]}
        catalog = output / "catalog.json"
        with catalog.open("x") as stream:
            json.dump({"format": 1, "environments": {environment_id: entry}}, stream, indent=2)
        MicroVMEnvironmentCatalog(catalog).resolve(environment_id)
        result.update(status="passed", catalog=str(catalog), layer_count=len(layers))
        return result
    except BaseException as exc:
        result.update(status="failed", error=repr(exc))
        raise
    finally:
        with (output / "prepare-result.json").open("x") as stream:
            json.dump(result, stream, indent=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("image", "tools-image", "environment-id"):
        parser.add_argument("--" + name, required=True)
    for name in ("output", "kernel", "agent-source", "busybox"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name, default in (("cpus", 1), ("memory-mb", 512), ("timeout-ms", 30000),
                          ("boot-size-mb", 256), ("reserve-gib", 10)):
        parser.add_argument("--" + name, type=int, default=default)
    parser.add_argument("--network", action="store_true")
    print(json.dumps(prepare(**vars(parser.parse_args()))))
