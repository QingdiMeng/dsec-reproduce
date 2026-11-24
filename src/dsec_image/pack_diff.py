"""Capture approved file additions from a builder and publish a derived MicroVM.

The publisher supplies the policy independently of the capture. Only a trusted
parent with /opt on the native ext4 root can opt in to this format.
"""
from contextlib import contextmanager
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile

from dsec.storage.catalog import DIGEST, IDENTIFIER, MicroVMEnvironmentCatalog, file_sha256
from dsec.storage.publication import publish_microvm


PROFILE = "direct-ext4-opt-v1"
OVERLAY_PROFILE = "overlaybd-ext4-opt-v1"
LIMIT = 16 * 1024 * 1024


def _approved(policy):
    if (not isinstance(policy, dict) or set(policy) != {"parent", "revision", "files"}
            or not isinstance(policy["parent"], str) or not IDENTIFIER.fullmatch(policy["parent"])
            or not isinstance(policy["revision"], str) or not DIGEST.fullmatch(policy["revision"])):
        raise ValueError("Expected a pinned parent environment and file policy")
    files = policy["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= 64:
        raise ValueError("Expected 1..64 approved file additions")
    paths = set()
    total = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "mode", "size", "sha256"}:
            raise ValueError("Invalid file policy")
        name = item["path"]
        if (not isinstance(name, str) or len(name) > 512
                or not re.fullmatch(r"/opt(?:/[A-Za-z0-9_][A-Za-z0-9._-]{0,127})+", name)
                or name in paths or type(item["mode"]) is not int or item["mode"] not in (0o644, 0o755)
                or type(item["size"]) is not int or item["size"] < 0
                or not isinstance(item["sha256"], str) or not DIGEST.fullmatch(item["sha256"])):
            raise ValueError("Unsafe or unpinned file addition")
        paths.add(name)
        total += item["size"]
    if total > LIMIT or any(str(p) in paths for n in paths for p in PurePosixPath(n).parents):
        raise ValueError("Overlapping files or capture exceeds size budget")
    return files


def _check_bytes(item, data):
    if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise ValueError(f"File differs from publisher policy: {item['path']}")


async def capture(builder, policy, output):
    """Capture an owned SDK sandbox into a new transferable file-level delta."""
    files = _approved(policy)

    async def identity():
        status = await builder.status()
        if (status["id"] != builder.id or status["state"] != "RUNNING"
                or status["environment_id"] != policy["parent"] or status["storage"] != "local"
                or status["environment_manifest_sha256"] != policy["revision"]):
            raise ValueError("Builder differs from pinned local parent")
        return status["generation"]

    generation = await identity()
    payload = {}
    for item in files:
        data = await builder.read_file(item["path"], max_bytes=item["size"])
        _check_bytes(item, data)
        payload[item["path"]] = base64.b64encode(data).decode("ascii")
    if await identity() != generation:
        raise ValueError("Builder generation changed during capture")
    output = Path(output)
    with tempfile.NamedTemporaryFile(mode="w", dir=output.parent) as stream:
        json.dump(dict(schema=1, policy=policy, files=payload), stream)
        stream.flush()
        os.fsync(stream.fileno())
        os.link(stream.name, output)


def _debugfs(work, command, *, write=False, missing=None):
    args = ["debugfs", *(["-w"] if write else []), "-R", command, "boot.ext4"]
    result = subprocess.run(args, cwd=work, env={**os.environ, "LC_ALL": "C"},
                            text=True, capture_output=True, check=True, timeout=60)
    # debugfs can return success even when its individual command failed.
    errors = [s.strip() for s in result.stderr.splitlines() if not s.startswith("debugfs ")]
    if missing and errors == [missing + ": File not found by ext2_lookup"]:
        return None
    if errors:
        raise ValueError("debugfs: " + " ".join(errors))
    return result.stdout


def _install(work, item, data):
    name = item["path"]
    for parent in reversed(PurePosixPath(name).parents):
        if parent == PurePosixPath("/"):
            continue
        path = str(parent)
        info = _debugfs(work, "stat " + path, missing=path)
        if info is None:
            _debugfs(work, "mkdir " + path, write=True)
        elif not re.search(r"Type:\s+directory\b", info.splitlines()[0]):
            raise ValueError(f"Parent must be a directory: {path}")
    if _debugfs(work, "stat " + name, missing=name) is not None:
        raise ValueError(f"Destination already exists: {name}")
    (work / "payload").write_bytes(data)
    _debugfs(work, "write payload " + name, write=True)
    _debugfs(work, f"set_inode_field {name} mode 0{0o100000 | item['mode']:o}", write=True)
    info = _debugfs(work, "stat " + name)
    if not re.search(rf"Type:\s+regular\s+Mode:\s+0{item['mode']:o}\b", info.splitlines()[0]):
        raise ValueError(f"Published file has unexpected type or permissions: {name}")
    _debugfs(work, "dump " + name + " readback", write=False)
    _check_bytes(item, (work / "readback").read_bytes())
    (work / "readback").unlink()


def _install_files(work, files):
    for item, data in files:
        _install(work, item, data)
    subprocess.run(["e2fsck", "-fn", str(work / "boot.ext4")], capture_output=True,
                   check=True, timeout=120)


@contextmanager
def _overlaybd_child(entry, files, socket, config, workspace):
    from dsec.storage.overlaybd import OverlayBDRootStore

    store = OverlayBDRootStore(socket, config, {})
    work = Path(tempfile.mkdtemp(prefix="pack-diff-", dir=Path(workspace).resolve(strict=True)))
    # A lost daemon reply leaves device ownership uncertain. Retain the
    # runtime and evidence until the operator can reconcile the device ID.
    safe_to_remove = True
    try:
        store.share_directory(work)
        image = work / "parent-image.json"
        image.write_text(json.dumps({"lowers": [{"file": pin["file"]} for pin in
                                                entry["overlaybd_root"]["lowers"]], "upper": {}}))
        image.chmod(0o640)
        owner = store.socket_identity()
        safe_to_remove = False
        device, runtime = store.create(image, work, work / "boot.ext4")
        try:
            _install_files(work, files)
            if store.socket_identity() != owner:
                raise RuntimeError("ublk daemon changed before sealing the child layer")
            commit = store.snapshot(device, image, work, work / "checkpoint")
        finally:
            if store.socket_identity() != owner:
                raise RuntimeError("ublk daemon changed; original device ownership is uncertain")
            store.delete(device, runtime, work)
            safe_to_remove = True
        child = dict(entry)
        child.pop("boot_template", None)
        child.pop("boot_sha256", None)
        image_file = work / "disk-image.json"
        child["overlaybd_root"] = {
            "image": str(image_file), "image_sha256": file_sha256(image_file),
            "lowers": [*entry["overlaybd_root"]["lowers"],
                       {"file": str(commit), "bytes": commit.stat().st_size,
                        "sha256": file_sha256(commit)}]}
        yield child
    except Exception as exc:
        if not safe_to_remove:
            exc.add_note(f"Check ublk device ownership before removing {work}")
        raise
    finally:
        if safe_to_remove:
            shutil.rmtree(work)


def _publish_child(work, entry, revision, child, destination, store):
    entry.pop("execution_contract", None)
    entry["pack_diff_parent"] = revision
    candidate = work / "catalog.json"
    candidate.write_text(json.dumps({"format": 1, "environments": {child: entry}}))
    return publish_microvm(candidate, child, destination, Path(store))


def publish(policy, delta, parent_catalog, child, destination, store, *,
            ublk_socket=None, ublk_global_config=None, ublk_work_dir=None):
    files = _approved(policy)
    if not IDENTIFIER.fullmatch(child) or child == policy["parent"]:
        raise ValueError("A distinct child environment ID is required")
    catalog = MicroVMEnvironmentCatalog(parent_catalog)
    parent = policy["parent"]
    if catalog.environment_digest(parent) != policy["revision"]:
        raise ValueError("Incompatible parent revision")
    spec = catalog.resolve(parent, "local")
    entry = dict(catalog.entries[parent])
    layered = spec["root_block_backend"] == "overlaybd-ublk"
    if entry.get("pack_diff_root") != (OVERLAY_PROFILE if layered else PROFILE):
        raise ValueError("Parent requires an explicitly attested ext4 /opt profile for its root backend")
    ublk = (ublk_socket, ublk_global_config, ublk_work_dir)
    if layered and not all(ublk):
        raise ValueError("OverlayBD publication requires a ublk socket, global config and writable workspace")
    if not layered and any(ublk):
        raise ValueError("ublk options require an OverlayBD parent")
    with Path(delta).open("rb") as stream:
        raw = stream.read(2 * LIMIT + 1)
    if len(raw) > 2 * LIMIT:
        raise ValueError("Oversized delta")
    value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {"schema", "policy", "files"}
            or value["schema"] != 1 or value["policy"] != policy
            or not isinstance(value["files"], dict)
            or set(value["files"]) != {item["path"] for item in files}):
        raise ValueError("Delta differs from publisher policy")
    approved = []
    for item in files:
        data = base64.b64decode(value["files"][item["path"]], validate=True)
        _check_bytes(item, data)
        approved.append((item, data))
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".pack-diff-", dir=destination.parent) as name:
        work = Path(name)
        if layered:
            with _overlaybd_child(entry, approved, *ublk) as derived:
                return _publish_child(work, derived, policy["revision"], child, destination, store)
        disk = work / "boot.ext4"
        shutil.copyfile(entry["boot_template"], disk)
        if file_sha256(disk) != entry["boot_sha256"]:
            raise ValueError("Parent disk changed while preparing child")
        _install_files(work, approved)
        entry.update(boot_template=str(disk.resolve()), boot_sha256=file_sha256(disk))
        return _publish_child(work, entry, policy["revision"], child, destination, store)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("policy", "delta", "parent-catalog", "destination", "store"):
        parser.add_argument("--" + key, required=True, type=Path)
    for key in ("ublk-socket", "ublk-global-config", "ublk-work-dir"):
        parser.add_argument("--" + key, type=Path)
    parser.add_argument("--child", required=True)
    args = vars(parser.parse_args())
    args["policy"] = json.loads(args["policy"].read_text())
    print(json.dumps(publish(**args)))


if __name__ == "__main__":
    main()
