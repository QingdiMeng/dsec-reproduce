"""Admin-published fs-verity receipts for lazy, authenticated artifact reads.

The Linux fs-verity UAPI measures a Merkle-tree file digest without scanning
the file. A protected publication receipt binds that digest to the existing
whole-file SHA-256 identity. Kernel verification remains active on reads.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys
import tempfile

HEX256 = re.compile(r"[0-9a-f]{64}\Z")
# Linux asm-generic ioctl ABI used by x86_64/aarch64.
FS_IOC_ENABLE_VERITY = 0x40806685
FS_IOC_MEASURE_VERITY = 0xC0046686


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_uid, info.st_gid)


def _require_admin_path(path, *, directory=False):
    """Protect both the publication inode and every ancestor from replacement."""
    path = Path(path).resolve(strict=True)
    for current in (path, *path.parents):
        info = current.lstat()
        is_directory = directory or current != path
        correct_kind = stat.S_ISDIR(info.st_mode) if is_directory else stat.S_ISREG(info.st_mode)
        if not correct_kind or info.st_uid != 0 or info.st_mode & 0o022:
            raise PermissionError("fs-verity publication requires an admin-controlled path: " + str(current))
    return path


def _require_kernel_filesystem(fd):
    if sys.platform != "linux":
        raise RuntimeError("fs-verity requires Linux")
    # A FUSE server can answer ioctls in userspace. Accept only filesystem
    # types where the Linux kernel itself implements verity read enforcement.
    buffer = ctypes.create_string_buffer(256)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.fstatfs.argtypes = [ctypes.c_int, ctypes.c_void_p]
    libc.fstatfs.restype = ctypes.c_int
    if libc.fstatfs(fd, ctypes.byref(buffer)) != 0:
        raise OSError(ctypes.get_errno(), "Cannot inspect artifact filesystem")
    magic = ctypes.c_long.from_buffer(buffer).value & 0xFFFFFFFF
    if magic not in {0xEF53, 0xF2F52010, 0x9123683E}:
        raise ValueError("fs-verity requires kernel ext4, f2fs or btrfs enforcement")


def measure_fd(fd):
    _require_kernel_filesystem(fd)
    buffer = bytearray(struct.pack("=HH", 0, 32) + bytes(32))
    fcntl.ioctl(fd, FS_IOC_MEASURE_VERITY, buffer, True)
    algorithm, size = struct.unpack_from("=HH", buffer)
    if algorithm != 1 or size != 32:
        raise ValueError("Expected a SHA-256 fs-verity digest")
    return bytes(buffer[4:36]).hex()


def measure(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        return measure_fd(fd)
    finally:
        os.close(fd)


def _read_receipt(path):
    path = _require_admin_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if before.st_size > 16384 or _identity(before) != _identity(path.stat()):
            raise ValueError("Invalid or changed fs-verity receipt")
        payload = json.loads(os.read(fd, 16385))
        if _identity(before) != _identity(os.fstat(fd)) or _identity(before) != _identity(path.stat()):
            raise ValueError("fs-verity receipt changed during read")
    finally:
        os.close(fd)
    if (not isinstance(payload, dict)
            or set(payload) != {"schema", "algorithm", "file_sha256", "verity_digest", "size_bytes"}
            or type(payload["schema"]) is not int or payload["schema"] != 1
            or payload["algorithm"] != "sha256"
            or not isinstance(payload["file_sha256"], str) or not HEX256.fullmatch(payload["file_sha256"])
            or not isinstance(payload["verity_digest"], str) or not HEX256.fullmatch(payload["verity_digest"])
            or type(payload["size_bytes"]) is not int or payload["size_bytes"] < 0):
        raise ValueError("Unsupported fs-verity publication receipt")
    return payload


def verify(path, expected_sha256, receipt_path):
    """Authenticate a published file without reading its content pages.

    Admin ownership prevents replacement before the subsequent VMM open.
    The protected receipt, rather than a writable manifest, authenticates
    the binding between the existing SHA-256 and the kernel's file digest.
    """
    receipt = _read_receipt(receipt_path)
    if receipt["file_sha256"] != expected_sha256:
        raise ValueError("fs-verity receipt differs from pinned artifact SHA-256")
    path = _require_admin_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if _identity(before) != _identity(path.stat()) or before.st_size != receipt["size_bytes"]:
            raise ValueError("fs-verity artifact identity or size changed")
        if measure_fd(fd) != receipt["verity_digest"]:
            raise ValueError("fs-verity artifact digest mismatch")
        if _identity(before) != _identity(os.fstat(fd)) or _identity(before) != _identity(path.stat()):
            raise ValueError("fs-verity artifact changed during measurement")
    finally:
        os.close(fd)
    return path


def seal(path, expected_sha256, receipt_path):
    """Publish once, as an administrator, on a verity-capable filesystem."""
    if os.geteuid() != 0:
        raise PermissionError("Publishing an fs-verity receipt requires administrator execution")
    if not isinstance(expected_sha256, str) or not HEX256.fullmatch(expected_sha256):
        raise ValueError("Invalid expected artifact SHA-256")
    path = _require_admin_path(path)
    receipt_path = Path(receipt_path).absolute()
    parent = _require_admin_path(receipt_path.parent, directory=True)
    receipt_path = parent / receipt_path.name
    if receipt_path.exists() or receipt_path.is_symlink():
        raise FileExistsError("Refusing to overwrite an existing publication receipt")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        # Enable before hashing: verity freezes contents and rejects writers.
        _require_kernel_filesystem(fd)
        argument = struct.pack("=IIIIQIIQ11Q", 1, 1, 4096, 0, 0, 0, 0, 0, *([0] * 11))
        try:
            fcntl.ioctl(fd, FS_IOC_ENABLE_VERITY, argument)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
        digest = hashlib.sha256()
        size = 0
        while block := os.read(fd, 4 * 1024 * 1024):
            digest.update(block)
            size += len(block)
        if digest.hexdigest() != expected_sha256 or size != os.fstat(fd).st_size:
            raise ValueError("Publication bytes differ from pinned SHA-256; no receipt published")
        payload = {"schema": 1, "algorithm": "sha256", "file_sha256": expected_sha256,
                   "verity_digest": measure_fd(fd), "size_bytes": size}
    finally:
        os.close(fd)
    descriptor, temporary = tempfile.mkstemp(prefix=".verity-receipt-", dir=receipt_path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o444)
        os.link(temporary, receipt_path)
        directory = os.open(receipt_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)
    verify(path, expected_sha256, receipt_path)
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    for operation in ("seal", "verify"):
        item = sub.add_parser(operation)
        item.add_argument("--file", type=Path, required=True)
        item.add_argument("--sha256", required=True)
        item.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if args.operation == "seal":
        result = seal(args.file, args.sha256, args.receipt)
    else:
        result = {"file": str(verify(args.file, args.sha256, args.receipt)), "content_scan": False}
    print(json.dumps({"status": "passed", **result}))


if __name__ == "__main__":
    main()
