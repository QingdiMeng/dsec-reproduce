"""Portable, digest-addressed handoff for published local MicroVM artifacts."""

import hashlib
import json
import os
from pathlib import Path
import tempfile

from dsec.contracts.environments import artifact_revision, validate_artifact_version
from dsec.storage.catalog import MicroVMEnvironmentCatalog


FILES = (("kernel", "kernel_sha256", ".kernel"), ("boot_template", "boot_sha256", ".ext4"),
         ("dax_binary", "dax_binary_sha256", ".vmm"))
SETTINGS = {"backend", "rootfs", "root_block_backend", "cpus", "memory_mb",
            "command_timeout_ms", "pack_diff_root", "pack_diff_parent"}
ENTRY_FIELDS = SETTINGS | {"layers", "overlaybd_root", "execution_contract"} | {
    key for path, digest, _ in FILES for key in (path, digest)}


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def portable_manifest(entry):
    """Project a verified deployment to the supported v1 semantics, preserving layer order."""
    if entry.keys() - ENTRY_FIELDS:
        raise ValueError("Unsupported portable environment settings")
    body = {key:entry[key] for key in SETTINGS if key in entry}
    for path, digest, _ in FILES:
        if path in entry:
            body[digest] = entry[digest]
    body["layers"] = []
    for layer in entry["layers"]:
        if "bytes" not in layer or layer.keys() - {"name", "file", "sha256", "bytes", "dax"}:
            raise ValueError("Portable v1 requires local EROFS layers")
        body["layers"].append({k:v for k,v in layer.items() if k != "file"})
    if entry.get("overlaybd_root") is not None:
        root = entry["overlaybd_root"]
        if set(root) != {"image", "image_sha256", "lowers"} or any(
                set(pin) != {"file", "bytes", "sha256"} for pin in root["lowers"]):
            raise ValueError("Unsupported portable OverlayBD settings")
        image = {"lowers":[{"file":p["file"]} for p in root["lowers"]], "upper":{}}
        if hashlib.sha256(encoded(image)).hexdigest() != root["image_sha256"]:
            raise ValueError("Export requires the publisher's canonical OverlayBD image")
        body["lowers"] = [{"bytes":p["bytes"], "sha256":p["sha256"]} for p in root["lowers"]]
    if entry.get("execution_contract") is not None:
        body["capabilities"] = entry["execution_contract"]["capabilities"]
    return {"schema":1, "environment":body}


def artifact_versions(catalog):
    versions = {}
    for name, entry in catalog.entries.items():
        try:
            manifest = portable_manifest(entry)
        except ValueError:
            continue
        versions[name] = hashlib.sha256(encoded(manifest)).hexdigest()
    return versions


def export_manifest(source_catalog, environment_id, output):
    catalog = MicroVMEnvironmentCatalog(source_catalog)
    catalog.resolve(environment_id)
    data = encoded(portable_manifest(catalog.entries[environment_id]))
    with tempfile.NamedTemporaryFile(dir=Path(output).parent) as stage:
        stage.write(data)
        stage.flush()
        os.fsync(stage.fileno())
        os.link(stage.name, output)
    return hashlib.sha256(data).hexdigest()


def import_manifest(manifest, version, objects, environment_id, destination, local_store):
    """Resolve only pinned object names in a node-controlled directory."""
    from dsec.storage.publication import publish_microvm

    validate_artifact_version(version)
    raw = Path(manifest).read_bytes()
    if hashlib.sha256(raw).hexdigest() != version:
        raise ValueError("Portable manifest digest differs from the requested artifact version")
    value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {"schema", "environment"}
            or type(value["schema"]) is not int or value["schema"] != 1):
        raise ValueError("Unsupported portable manifest")
    body = value["environment"]
    if not isinstance(body, dict) or body.keys() - (SETTINGS | {f[1] for f in FILES}
                                                       | {"layers","lowers","capabilities"}):
        raise ValueError("Unsupported portable environment fields")
    objects = Path(objects).resolve(strict=True)

    def source(digest, suffix):
        validate_artifact_version(digest)
        path = objects / (digest + suffix)
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing regular portable object: {path.name}")
        return str(path)

    for path, digest, suffix in FILES:
        if digest in body:
            body[path] = source(body[digest], suffix)
    if not isinstance(body.get("layers"), list) or "kernel_sha256" not in body:
        raise ValueError("Missing portable MicroVM kernel or layers")
    for pin in body["layers"]:
        if (not isinstance(pin, dict) or not {"name", "sha256", "bytes"} <= pin.keys()
                or pin.keys() - {"name", "bytes", "sha256", "dax"}):
            raise ValueError("Invalid portable layer")
        pin["file"] = source(pin["sha256"], ".erofs")
        if "bytes" in pin and Path(pin["file"]).stat().st_size != pin["bytes"]:
            raise ValueError("Portable EROFS layer size differs")
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        if "lowers" in body:
            lowers = body.pop("lowers")
            if not isinstance(lowers, list) or not lowers:
                raise ValueError("Missing portable OverlayBD pins")
            for pin in lowers:
                if not isinstance(pin, dict) or set(pin) != {"bytes", "sha256"}:
                    raise ValueError("Invalid portable OverlayBD pin")
                pin["file"] = source(pin["sha256"], ".commit")
            image = directory / "root.json"
            image_bytes = encoded({"lowers":[{"file":p["file"]} for p in lowers], "upper":{}})
            image.write_bytes(image_bytes)
            body["overlaybd_root"] = dict(image=str(image), image_sha256=hashlib.sha256(image_bytes).hexdigest(), lowers=lowers)
        if "capabilities" in body:
            caps = body.pop("capabilities")
            body["execution_contract"] = dict(schema=1, artifact_revision=artifact_revision(body), capabilities=caps)
        if encoded(portable_manifest(body)) != raw:
            raise ValueError("Noncanonical portable manifest")
        source_catalog = directory / "catalog.json"
        source_catalog.write_bytes(encoded({"format":1,"environments":{environment_id:body}}))
        return publish_microvm(source_catalog, environment_id, destination, local_store)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="operation", required=True)
    export = modes.add_parser("export")
    export.add_argument("--catalog", required=True, type=Path)
    install = modes.add_parser("import")
    install.add_argument("--artifact-version", required=True)
    install.add_argument("--objects", required=True, type=Path)
    install.add_argument("--destination-catalog", required=True, type=Path)
    install.add_argument("--local-store", required=True, type=Path)
    for mode in (export, install):
        mode.add_argument("--environment-id", required=True)
        mode.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    if args.operation == "export":
        print(export_manifest(args.catalog, args.environment_id, args.manifest))
    else:
        print(json.dumps(import_manifest(args.manifest, args.artifact_version, args.objects,
                                         args.environment_id, args.destination_catalog, args.local_store)))
