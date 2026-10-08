"""Pinned, task-specific verifier bootstrap replacement for offline diagnosis.

The assertions, CTRF generation, and reward logic stay byte-for-byte from the
official task. Only the dependency download preamble is replaced. This output
is deliberately not called the canonical TB2 verifier.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


PINNED_TEST_SH_SHA256 = {
    "regex-log": "4770437ea96c3cc84684b4f99d55fb148fcac09f9ea1e8ef49de487716e6c334",
    "log-summary-date-ranges": "4770437ea96c3cc84684b4f99d55fb148fcac09f9ea1e8ef49de487716e6c334",
    "cancel-async-tasks": "79600a6b863def31b4daa008297423010cd2d6243e689d67503e118e4dc53397",
    "chess-best-move": "4770437ea96c3cc84684b4f99d55fb148fcac09f9ea1e8ef49de487716e6c334",
}
DEPENDENCY_BOOTSTRAP = b"""apt-get update
apt-get install -y curl

# Install uv
curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh

source $HOME/.local/bin/env"""
OFFLINE_BOOTSTRAP = b"""# DSec offline diagnostic: pinned shared read-only verifier tool disk.
test -x /mnt/dsec-verifier/bin/uvx || exit 125
test \"$(/mnt/dsec-verifier/bin/uvx --version)\" = \"uvx 0.9.5\" || exit 125
export PATH=/mnt/dsec-verifier/bin:$PATH
export UV_OFFLINE=1"""
RUNTIME_BOOTSTRAP = b"""# DSec offline diagnostic: pinned shared read-only verifier tool disk.
test -x /mnt/dsec-verifier/bin/uvx || exit 125
test \"$(/mnt/dsec-verifier/bin/uvx --version)\" = \"uvx 0.9.5\" || exit 125
test -d /mnt/dsec-verifier/cache/uv/archive-v0 || exit 125
test -d /mnt/dsec-verifier/runtime/python || exit 125
mkdir -p /.cache
test -d /.cache/uv/archive-v0 || cp -a /mnt/dsec-verifier/cache/uv /.cache/
export PATH=/mnt/dsec-verifier/bin:$PATH
export UV_PYTHON_INSTALL_DIR=/mnt/dsec-verifier/runtime/python
export UV_PYTHON_DOWNLOADS=never
export UV_CACHE_DIR=/.cache/uv
export UV_OFFLINE=1"""

SHARED_BOOTSTRAP = DEPENDENCY_BOOTSTRAP
SHARED_UV_COMMAND = b"\\\n".join((
    b"uvx ", b"  -p 3.13 ", b"  -w pytest==8.4.1 ",
    b"  -w pytest-json-ctrf==0.3.5 ", b"  pytest"))
SHARED_MANIFEST = Path(__file__).with_name("tb2_offline_shared_manifest.json")
LAYERED_UV_MANIFEST = Path(__file__).with_name("tb2_layered_uv_manifest.json")
CURL_INSTALL = b"curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh"
SOURCE_UV_ENV = b"source $HOME/.local/bin/env"
LAYERED_UV_BOOTSTRAP = RUNTIME_BOOTSTRAP.replace(b"export UV_OFFLINE=1", b"")


def layered_uv_test_sh(task_id: str, original: bytes) -> bytes:
    """Replace the pinned GitHub uv installer; leave other task setup intact.

This mode is not fully offline: apt and task-specific uv wheels may still use
the network. It must never be reported as the canonical verifier.
"""
    manifest = json.loads(LAYERED_UV_MANIFEST.read_text())
    record = manifest["tasks"].get(task_id)
    if record is None or hashlib.sha256(original).hexdigest() != record["test_sh_sha256"]:
        raise ValueError("Task verifier is not pinned for layered uv bootstrap")
    if original.count(CURL_INSTALL) != 1 or original.count(SOURCE_UV_ENV) != 1:
        raise ValueError("Expected pinned uv installer is missing")
    begin = original.index(CURL_INSTALL)
    end = original.index(SOURCE_UV_ENV, begin) + len(SOURCE_UV_ENV)
    between = original[begin + len(CURL_INSTALL):end - len(SOURCE_UV_ENV)]
    if between not in (b"\n", b"\n\n"):
        raise ValueError("Unexpected code between uv installer and environment setup")
    return original[:begin] + LAYERED_UV_BOOTSTRAP + original[end:]


def shared_offline_test_sh(task_id: str, original: bytes) -> bytes:
    """Replace one reviewed curl-only bootstrap with the pinned runtime layer.

    The task's test assertions and reward logic remain unchanged. This is an
    offline diagnostic verifier, never a canonical TB2 score.
    """
    manifest = json.loads(SHARED_MANIFEST.read_text())
    expected = manifest["tasks"].get(task_id)
    if expected is None or hashlib.sha256(original).hexdigest() != expected:
        raise ValueError("Task verifier is not pinned for shared offline bootstrap")
    if (hashlib.sha256(SHARED_BOOTSTRAP).hexdigest() != manifest["bootstrap_sha256"] or
            hashlib.sha256(SHARED_UV_COMMAND).hexdigest() != manifest["uv_command_sha256"] or
            original.count(SHARED_BOOTSTRAP) != 1 or
            original.count(SHARED_UV_COMMAND) != 1):
        raise ValueError("Pinned dependency bootstrap changed")
    return original.replace(SHARED_BOOTSTRAP, RUNTIME_BOOTSTRAP, 1)


def offline_test_sh(task_id: str, original: bytes) -> bytes:
    expected = PINNED_TEST_SH_SHA256.get(task_id)
    if expected is None:
        raise ValueError("Offline verifier bootstrap is not reviewed for this TB2 task")
    if hashlib.sha256(original).hexdigest() != expected:
        raise ValueError("Official task verifier changed; offline transform refused")
    bootstrap = (DEPENDENCY_BOOTSTRAP.replace(b"| sh\n\nsource", b"| sh\nsource")
                 if task_id == "cancel-async-tasks" else DEPENDENCY_BOOTSTRAP)
    if original.count(bootstrap) != 1:
        raise ValueError("Expected task dependency bootstrap missing")
    replacement = OFFLINE_BOOTSTRAP if task_id == "regex-log" else RUNTIME_BOOTSTRAP
    return original.replace(bootstrap, replacement, 1)
