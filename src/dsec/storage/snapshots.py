"""Sparse checkpoint file primitives; format and hash algorithms remain v0.1."""
import errno
import hashlib
import os
from pathlib import Path
import subprocess
from dsec.storage.digest import sha


def _copy_sparse(source, destination):
    """Keep TB2's 10-GiB logical disk sparse across create/snapshot/restore."""
    subprocess.run(["cp", "--sparse=always", "--reflink=auto", "--",
                    str(source), str(destination)], check=True)


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sparse_sha256(path):
    """Hash allocated extents and their offsets without scanning sparse holes."""
    digest = hashlib.sha256(b"dsec-sparse-sha256-v1\0")
    fd = os.open(path, os.O_RDONLY)
    try:
        size = os.fstat(fd).st_size
        digest.update(size.to_bytes(8, "big"))
        cursor = 0
        while cursor < size:
            try:
                start = os.lseek(fd, cursor, os.SEEK_DATA)
            except OSError as exc:
                if exc.errno == errno.ENXIO:
                    break  # The remaining bytes are a hole.
                raise
            end = os.lseek(fd, start, os.SEEK_HOLE)
            if not cursor <= start < end <= size:
                raise OSError("Invalid sparse extent boundaries")
            digest.update(start.to_bytes(8, "big"))
            digest.update(end.to_bytes(8, "big"))
            offset = start
            while offset < end:
                block = os.pread(fd, min(1024*1024, end-offset), offset)
                if not block:
                    raise EOFError("Sparse extent changed while hashing")
                digest.update(block)
                offset += len(block)
            cursor = end
        return digest.hexdigest()
    finally:
        os.close(fd)


def _sparse_block_sha256(path):
    """Content hash over fixed logical blocks, skipping all-hole blocks."""
    block_size = 1024 * 1024
    digest = hashlib.sha256(b"dsec-sparse-block-sha256-v2\0")
    fd = os.open(path, os.O_RDONLY)
    try:
        size = os.fstat(fd).st_size
        digest.update(size.to_bytes(8, "big"))
        extents = []
        cursor = 0
        while cursor < size:
            try:
                start = os.lseek(fd, cursor, os.SEEK_DATA)
            except OSError as exc:
                if exc.errno == errno.ENXIO:
                    break
                if exc.errno in (errno.EINVAL, errno.ENOTSUP):
                    extents = [(0, size)]  # Correct dense fallback.
                    break
                raise
            try:
                end = os.lseek(fd, start, os.SEEK_HOLE)
            except OSError as exc:
                if exc.errno in (errno.EINVAL, errno.ENOTSUP):
                    extents = [(0, size)]
                    break
                raise
            if not cursor <= start < end <= size:
                raise OSError("Invalid sparse extent boundaries")
            extents.append((start, end))
            cursor = end
        zero_digest = hashlib.sha256(bytes(block_size)).digest()
        extent_index = 0
        for offset in range(0, size, block_size):
            end = min(size, offset + block_size)
            while extent_index < len(extents) and extents[extent_index][1] <= offset:
                extent_index += 1
            has_data = (extent_index < len(extents) and extents[extent_index][0] < end)
            if not has_data:
                digest.update(zero_digest if end-offset == block_size
                              else hashlib.sha256(bytes(end-offset)).digest())
                continue
            block = bytearray()
            while len(block) < end-offset:
                part = os.pread(fd, end-offset-len(block), offset+len(block))
                if not part:
                    raise EOFError("Snapshot file changed while hashing")
                block.extend(part)
            digest.update(hashlib.sha256(block).digest())
        return digest.hexdigest()
    finally:
        os.close(fd)


def _snapshot_hash(path, algorithm, *, hash_file=sha,
                   sparse_hash=_sparse_sha256, sparse_block_hash=_sparse_block_sha256):
    if algorithm == "sha256":
        return hash_file(path)
    if algorithm == "sparse-sha256-v1":
        return sparse_hash(path)
    if algorithm == "sparse-block-sha256-v2":
        return sparse_block_hash(path)
    raise ValueError("Unsupported snapshot hash algorithm")


