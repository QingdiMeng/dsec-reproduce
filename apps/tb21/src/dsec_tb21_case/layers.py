"""Convert a locally pinned Docker image into reusable EROFS OCI layers.

This is an offline build step.  The resulting manifest preserves layer order
and uses the exact docker-save layer blob bytes as cache keys.  Docker save
may contain gzip-compressed OCI blobs; their decompressed checksum must match
the image's diff ID.  mkfs.erofs --aufs
translates OCI/AUFS whiteouts into OverlayFS metadata; never extract a layer
as an ordinary directory and silently lose deletion semantics.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, text=True, capture_output=True)


def sha256(path: Path, *, compressed: bool = False) -> str:
    digest = hashlib.sha256()
    with (gzip.open(path, "rb") if compressed else path.open("rb")) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def convert(image: str, output: Path, tools_image: str) -> Path:
    inspected = json.loads(run("docker", "image", "inspect", image).stdout)[0]
    image_id = inspected["Id"]
    if image != image_id or not image_id.startswith("sha256:"):
        raise ValueError("Pass the exact locally pinned sha256 image ID")
    diff_ids = inspected["RootFS"]["Layers"]
    output.mkdir(parents=True, exist_ok=True)
    layer_dir = output / "layers"
    layer_dir.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tb2-erofs-", dir=output) as temp:
        work = Path(temp)
        archive = work / "docker-save.tar"
        subprocess.run(["docker", "save", "-o", str(archive), image_id], check=True)
        with tarfile.open(archive) as saved:
            member = saved.extractfile("manifest.json")
            if member is None:
                raise ValueError("docker save lacks manifest.json")
            entries = json.load(member)
            if len(entries) != 1 or len(entries[0]["Layers"]) != len(diff_ids):
                raise ValueError("Docker layer count differs from inspected diff IDs")
            layer_names = entries[0]["Layers"]
            layers = []
            for index, (name, diff_id) in enumerate(zip(layer_names, diff_ids)):
                if not diff_id.startswith("sha256:") or len(diff_id) != 71:
                    raise ValueError(f"Invalid diff ID at layer {index}")
                layer_tar = work / "layer.tar"
                with saved.extractfile(name) as source, layer_tar.open("wb") as destination:
                    if source is None:
                        raise ValueError(f"Missing Docker layer {name}")
                    shutil.copyfileobj(source, destination, 1024 * 1024)
                exported_digest = sha256(layer_tar)
                with layer_tar.open("rb") as stream:
                    compressed = stream.read(2) == b"\x1f\x8b"
                if sha256(layer_tar, compressed=compressed) != diff_id[7:]:
                    raise ValueError(f"Docker diff ID mismatch at layer {index}")
                target = layer_dir / f"{exported_digest}.lz4.erofs"
                if not target.is_file():
                    partial = work / "layer.erofs"
                    command = [
                        "docker", "run", "--rm", "--network", "none",
                        "--user", f"{os.getuid()}:{os.getgid()}",
                        "-v", f"{work}:/work", "--entrypoint", "mkfs.erofs",
                        tools_image, "--tar=f", "--aufs", "-zlz4",
                    ]
                    if compressed:
                        command.append("--gzip")
                    command += ["/work/layer.erofs", "/work/layer.tar"]
                    subprocess.run(command, check=True)
                    os.replace(partial, target)
                target.chmod(0o444)
                layer_tar.unlink()
                layers.append({"diff_id": diff_id,
                               "export_tar_sha256": exported_digest,
                               "source_gzip": compressed,
                               "erofs": str(target.resolve()),
                               "erofs_sha256": sha256(target),
                               "erofs_bytes": target.stat().st_size})
    manifest = {"format": 1, "image_id": image_id,
                "layers": layers, "order": "base-to-top",
                "whiteouts": "mkfs.erofs --tar=f --aufs",
                "compression": "lz4"}
    path = output / f"{image_id[7:]}.json"
    partial_manifest = path.with_suffix(".json.tmp")
    partial_manifest.write_text(json.dumps(manifest, indent=2) + "\n")
    os.replace(partial_manifest, path)
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tools-image", default="dsec-e1-tools:20260929")
    options = parser.parse_args()
    print(convert(options.image, options.output, options.tools_image))


if __name__ == "__main__":
    main()
