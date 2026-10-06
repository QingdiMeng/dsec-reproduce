"""Check the digest binding and fail-closed fs-verity path without root."""
import errno
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import artifact_integrity as integrity
from tb2_verifier_artifact import VerifierArtifactStore


class ArtifactIntegrityTests(unittest.TestCase):
    def receipt(self, size):
        return {"schema": 1, "algorithm": "sha256", "file_sha256": "a" * 64,
                "verity_digest": "b" * 64, "size_bytes": size}

    def test_ioctl_reads_digest_without_a_content_read(self):
        def ioctl(fd, operation, buffer, mutate):
            self.assertEqual(operation, 0xC0046686)
            self.assertEqual(struct.unpack_from("=HH", buffer), (0, 32))
            buffer[:] = struct.pack("=HH", 1, 32) + bytes.fromhex("b" * 64)
        with patch.object(integrity, "_require_kernel_filesystem"), patch.object(integrity.fcntl, "ioctl", ioctl), patch.object(integrity.os, "read", side_effect=AssertionError("content scan")):
            self.assertEqual(integrity.measure_fd(42), "b" * 64)

    def test_userspace_filesystem_cannot_fake_kernel_enforcement(self):
        class Function:
            def __call__(self, fd, buffer):
                integrity.ctypes.cast(buffer, integrity.ctypes.POINTER(integrity.ctypes.c_long))[0] = 0x65735546
                return 0
        class Libc:
            fstatfs = Function()
        with patch.object(integrity.sys, "platform", "linux"), patch.object(integrity.ctypes, "CDLL", return_value=Libc()), patch.object(integrity.fcntl, "ioctl") as ioctl:
            with self.assertRaisesRegex(ValueError, "kernel ext4"):
                integrity.measure_fd(42)
            ioctl.assert_not_called()

    def test_unprotected_receipt_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "receipt.json"
            p.write_text(json.dumps(self.receipt(0)))
            p.chmod(0o666)
            with self.assertRaises(PermissionError):
                integrity._read_receipt(p)

    def test_publication_digest_cannot_be_rebound_to_another_sha256(self):
        with patch.object(integrity, "_read_receipt", return_value=self.receipt(0)), patch.object(integrity, "measure_fd") as measurement:
            with self.assertRaisesRegex(ValueError, "pinned artifact SHA"):
                integrity.verify("/unused", "c" * 64, "/unused-receipt")
            measurement.assert_not_called()

    def test_measured_digest_and_file_size_must_both_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "artifact"
            p.write_bytes(b"bytes")
            for size, digest in [(6, "b" * 64), (5, "c" * 64)]:
                with self.subTest(size=size), patch.object(integrity, "_read_receipt", return_value=self.receipt(size)), patch.object(integrity, "_require_admin_path", return_value=p), patch.object(integrity, "measure_fd", return_value=digest):
                    with self.assertRaises(ValueError):
                        integrity.verify(p, "a" * 64, "/receipt")

    def test_unsupported_verity_does_not_fall_back_to_full_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = root / "artifact"
            p.write_bytes(b"bytes")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "schema": 1, "format": "ext4", "tool": "uvx 0.9.5",
                "guest_mountpoint": "/mnt/dsec-verifier", "sha256": "a" * 64,
                "size_bytes": 5, "integrity": {"local": {"mode": "fs-verity", "receipt": "/receipt"}},
            }))
            with patch("tb2_verifier_artifact.verify_verity", side_effect=OSError(errno.ENODATA, "not sealed")), patch("tb2_verifier_artifact.sha256", side_effect=AssertionError("silent full scan")):
                with self.assertRaises(OSError):
                    VerifierArtifactStore(manifest, p)

    def test_every_resolution_rechecks_verity_instead_of_trusting_stat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = root / "artifact"
            p.write_bytes(b"bytes")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "schema": 1, "format": "ext4", "tool": "uvx 0.9.5",
                "guest_mountpoint": "/mnt/dsec-verifier", "sha256": "a" * 64,
                "size_bytes": 5, "integrity": {"local": {"mode": "fs-verity", "receipt": "/receipt"}},
            }))
            with patch("tb2_verifier_artifact.verify_verity", return_value=p) as measurement, patch("tb2_verifier_artifact.sha256", side_effect=AssertionError("full scan")):
                store = VerifierArtifactStore(manifest, p)
                store.resolve("local")
                self.assertEqual(measurement.call_count, 2)

    def test_seal_enable_argument_is_the_kernel_128_byte_abi(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = root / "artifact"
            p.write_bytes(b"bytes")
            arguments = []
            def ioctl(fd, operation, data):
                arguments.append(data)
                raise OSError(errno.EOPNOTSUPP, "unsupported volume")
            with patch.object(os, "geteuid", return_value=0), patch.object(integrity, "_require_kernel_filesystem"), patch.object(integrity, "_require_admin_path", side_effect=lambda p, **kw: Path(p)), patch.object(integrity.fcntl, "ioctl", ioctl):
                with self.assertRaises(OSError):
                    integrity.seal(p, "a" * 64, root / "receipt")
            self.assertEqual(len(arguments[0]), 128)
            self.assertEqual(struct.unpack_from("=IIII", arguments[0]), (1, 1, 4096, 0))
            self.assertFalse((root / "receipt").exists())


if __name__ == "__main__":
    unittest.main()
