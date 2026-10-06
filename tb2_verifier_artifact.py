"""Pinned, selectable verifier tool disk for TB2 microVMs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from artifact_integrity import verify as verify_verity


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class VerifierArtifactStore:
    def __init__(self, manifest_path, local_path, threefs_path=None):
        manifest = json.loads(Path(manifest_path).read_text())
        if (manifest.get("schema") != 1 or manifest.get("format") != "ext4" or
                manifest.get("tool") != "uvx 0.9.5" or
                manifest.get("guest_mountpoint") != "/mnt/dsec-verifier" or
                not isinstance(manifest.get("sha256"), str) or
                not re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"]) or
                type(manifest.get("size_bytes")) is not int or manifest["size_bytes"] < 0):
            raise ValueError("Unsupported verifier artifact manifest")
        self.manifest = manifest
        self.integrity = manifest.get("integrity", {})
        if not isinstance(self.integrity, dict) or set(self.integrity) - {"local", "threefs_lazy"}:
            raise ValueError("Invalid verifier integrity configuration")
        for descriptor in self.integrity.values():
            if (not isinstance(descriptor, dict) or set(descriptor) != {"mode", "receipt"}
                    or descriptor["mode"] != "fs-verity"
                    or not isinstance(descriptor["receipt"], str)
                    or not Path(descriptor["receipt"]).is_absolute()):
                raise ValueError("Unsupported verifier integrity mode")
        self.paths = {"local": Path(local_path)}
        if threefs_path is not None:
            self.paths["threefs_lazy"] = Path(threefs_path)
        self.validated = {}
        self.resolve("local")

    def resolve(self, storage):
        if storage not in self.paths:
            raise ValueError(f"Verifier artifact storage unavailable: {storage}")
        path = self.paths[storage].resolve(strict=True)
        if not path.is_file():
            raise ValueError("Verifier artifact source is not a regular file")
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if stat.st_size != self.manifest["size_bytes"]:
            raise ValueError("Verifier artifact size mismatch")
        if storage in self.integrity:
            # Re-measure each time; stat is not the authenticity proof.
            return verify_verity(path, self.manifest["sha256"], self.integrity[storage]["receipt"])
        if self.validated.get(storage) != identity:
            if sha256(path) != self.manifest["sha256"]:
                raise ValueError("Verifier artifact hash mismatch")
            after = path.stat()
            if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("Verifier artifact changed during hash verification")
            self.validated[storage] = identity
        return path

    @property
    def sha(self):
        return self.manifest["sha256"]
