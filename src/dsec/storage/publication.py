"""Publish a pinned microVM environment into a content-addressed artifact store.

Publication is deliberately separate from daemon startup.  A published catalog
still uses the runtime's normal hash checks; this tool does not treat a digest
in a filename or a writable filesystem as a substitute for verification.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

from dsec.storage.catalog import DIGEST, IDENTIFIER, MicroVMEnvironmentCatalog


def _digest_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_object(source: Path, store: Path, suffix: str, expected: str | None = None,
                 *, executable: bool = False) -> tuple[Path, str, int]:
    source = source.resolve(strict=True)
    before = source.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"Artifact is not a regular file: {source}")
    if expected is not None and not DIGEST.fullmatch(expected):
        raise ValueError("Invalid expected artifact SHA-256")
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = store.resolve(strict=True)
    if expected is not None:
        destination = store / (expected + suffix)
        if destination.exists() or destination.is_symlink():
            if not stat.S_ISREG(destination.lstat().st_mode):
                raise ValueError(f"Published object is not a regular file: {destination}")
            if executable and not destination.stat().st_mode & 0o111:
                raise ValueError(f"Published executable lost execute permission: {destination}")
            source_digest, source_size = _digest_file(source)
            after = source.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                    before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size,
                                         after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError(f"Artifact changed during publication: {source}")
            if source_digest != expected:
                raise ValueError(f"Artifact digest mismatch: {source}")
            actual, existing_size = _digest_file(destination)
            if (actual, existing_size) != (expected, source_size):
                raise ValueError(f"Published object changed: {destination}")
            return destination, expected, source_size
    fd, temporary = tempfile.mkstemp(prefix=".publish-", dir=store)
    temp = Path(temporary)
    try:
        digest = hashlib.sha256()
        total = 0
        with source.open("rb") as reader, os.fdopen(fd, "wb") as writer:
            for chunk in iter(lambda: reader.read(4 * 1024 * 1024), b""):
                writer.write(chunk)
                digest.update(chunk)
                total += len(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        after = source.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size,
                                     after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError(f"Artifact changed during publication: {source}")
        value = digest.hexdigest()
        if expected is not None and expected != value:
            raise ValueError(f"Artifact digest mismatch: {source}")
        destination = store / (value + suffix)
        os.chmod(temp, 0o555 if executable else 0o444)
        try:
            os.link(temp, destination)
            _sync_directory(store)
        except FileExistsError:
            if not stat.S_ISREG(destination.lstat().st_mode):
                raise ValueError(f"Published object is not a regular file: {destination}")
            if executable and not destination.stat().st_mode & 0o111:
                raise ValueError(f"Published executable lost execute permission: {destination}")
            actual, existing_size = _digest_file(destination)
            if (actual, existing_size) != (value, total):
                raise ValueError(f"Published object changed: {destination}")
        return destination, value, total
    finally:
        temp.unlink(missing_ok=True)


def _publish_json(payload: dict, store: Path) -> tuple[Path, str]:
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".root-image-", dir=store)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        path, digest, _ = _copy_object(temp, store, ".json")
        return path, digest
    finally:
        temp.unlink(missing_ok=True)


def _validate_remote_store(mount: Path, store: Path) -> tuple[Path, Path]:
    mount = mount.resolve(strict=True)
    if not store.resolve().is_relative_to(mount) or store.resolve() == mount:
        raise ValueError("Remote artifact store must be inside the 3FS mount")
    filesystem = subprocess.run(
        ["findmnt", "-T", str(mount), "-no", "FSTYPE"],
        capture_output=True, text=True, check=True, timeout=5).stdout.strip()
    if filesystem != "fuse.hf3fs":
        raise ValueError("Remote artifact store requires a live 3FS FUSE mount")
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = store.resolve(strict=True)
    if not store.is_relative_to(mount) or store == mount:
        raise ValueError("Remote artifact store must be inside the 3FS mount")
    return mount, store


def _atomic_catalog(path: Path, environment_id: str, entry: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    with lock.open("a+") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        payload = json.loads(path.read_text()) if path.exists() else {"format": 1, "environments": {}}
        if payload.get("format") != 1 or not isinstance(payload.get("environments"), dict):
            raise ValueError("Invalid destination environment catalog")
        if environment_id in payload["environments"]:
            raise ValueError(f"Environment already published: {environment_id}")
        payload["environments"][environment_id] = entry
        fd, name = tempfile.mkstemp(prefix=".catalog-", dir=path.parent)
        temp = Path(name)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp, 0o600)
            MicroVMEnvironmentCatalog(temp).resolve(environment_id, "local")
            if any("threefs_file" in layer for layer in entry["layers"]):
                MicroVMEnvironmentCatalog(temp).resolve(environment_id, "threefs_lazy")
            os.replace(temp, path)
            _sync_directory(path.parent)
        finally:
            temp.unlink(missing_ok=True)


def publish_microvm(source_catalog: Path, environment_id: str, destination_catalog: Path,
                    local_store: Path, *, threefs_mount: Path | None = None,
                    threefs_store: Path | None = None) -> dict:
    """Copy immutable artifacts, then atomically add one environment to a catalog."""
    if not IDENTIFIER.fullmatch(environment_id):
        raise ValueError("Invalid environment ID")
    if (threefs_mount is None) != (threefs_store is None):
        raise ValueError("3FS mount and store must be provided together")
    remote = (_validate_remote_store(threefs_mount, threefs_store)
              if threefs_mount is not None else None)
    catalog = MicroVMEnvironmentCatalog(source_catalog)
    resolved = catalog.resolve(environment_id, "local")
    source = copy.deepcopy(catalog.entries[environment_id])
    if remote is None and any("threefs_file" in layer for layer in source["layers"]):
        # A pre-existing remote reference is preserved only after publication-time
        # content verification, never inferred from its content-addressed name.
        catalog.resolve(environment_id, "threefs_lazy")
        for layer in source["layers"]:
            if "threefs_file" in layer:
                remote_path = Path(layer["threefs_file"])
                if _digest_file(remote_path) != (layer["sha256"], layer["bytes"]):
                    raise ValueError("Existing 3FS layer content changed")
    store = local_store.resolve() / "objects"
    for path_key, sha_key, suffix in (("boot_template", "boot_sha256", ".ext4"),
                                      ("kernel", "kernel_sha256", ".kernel")):
        path, digest, _ = _copy_object(Path(source[path_key]), store, suffix, source[sha_key])
        source[path_key], source[sha_key] = str(path), digest
    if resolved["dax_binary"] is not None:
        path, digest, _ = _copy_object(Path(source["dax_binary"]), store, ".vmm",
                                       source["dax_binary_sha256"], executable=True)
        source["dax_binary"], source["dax_binary_sha256"] = str(path), digest
    for layer in source["layers"]:
        path, digest, size = _copy_object(Path(layer["file"]), store, ".erofs", layer["sha256"])
        layer.update(file=str(path), sha256=digest, bytes=size)
        if remote is not None and not layer.get("dax", False):
            mount, remote_store = remote
            target, _, _ = _copy_object(path, remote_store, ".erofs", digest)
            layer.update(threefs_mount=str(mount), threefs_file=str(target))
    if resolved["root_block_backend"] == "overlaybd-ublk":
        root = source["overlaybd_root"]
        for lower in root["lowers"]:
            path, digest, size = _copy_object(Path(lower["file"]), store, ".commit",
                                             lower["sha256"])
            lower.update(file=str(path), sha256=digest, bytes=size)
        image = {"lowers": [{"file": lower["file"]} for lower in root["lowers"]], "upper": {}}
        path, digest = _publish_json(image, store)
        root.update(image=str(path), image_sha256=digest)
    _atomic_catalog(destination_catalog.resolve(), environment_id, source)
    return {"environment_id": environment_id,
            "environment_sha256": MicroVMEnvironmentCatalog(destination_catalog).environment_digest(environment_id),
            "catalog": str(destination_catalog.resolve()),
            "local_store": str(store),
            "threefs_lazy": remote is not None or any("threefs_file" in layer for layer in source["layers"])}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-catalog", type=Path, required=True)
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--destination-catalog", type=Path, required=True)
    parser.add_argument("--local-store", type=Path, required=True)
    parser.add_argument("--threefs-mount", type=Path)
    parser.add_argument("--threefs-store", type=Path)
    args = parser.parse_args()
    print(json.dumps(publish_microvm(args.source_catalog, args.environment_id,
                                     args.destination_catalog, args.local_store,
                                     threefs_mount=args.threefs_mount,
                                     threefs_store=args.threefs_store)))


if __name__ == "__main__":
    main()
