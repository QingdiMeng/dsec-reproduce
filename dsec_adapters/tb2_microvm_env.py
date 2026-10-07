"""Minimal OpenEnv-shaped TB2 command adapter backed by a shell sandbox.

This adapter deliberately refuses evaluate until the canonical verifier runs
inside the same VM. A missing verdict must never become a zero reward.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import time
import tomllib
from types import SimpleNamespace
import uuid

from .tb2_verifier_command import (canonical_eval_cmd as _canonical_eval_cmd,
                                 parse_canonical_reward as _parse_canonical_reward)
from .tb2_offline_verifier import (offline_test_sh, shared_offline_test_sh,
                                  layered_uv_test_sh, SHARED_MANIFEST,
                                  LAYERED_UV_MANIFEST, PINNED_TEST_SH_SHA256)
from .tb2_task_runtime import task_workdir

LEGACY_CANONICAL_LINKS = Path(__file__).with_name("tb2_canonical_cache_links_r1.json")
EVIDENCE_CHUNK_BYTES = 32768
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024


def _pinned_uv_installer_setup() -> str:
    """Serve only the suite's fixed uv installer URL from the pinned tool disk."""
    script = (
        "#!/bin/sh",
        'for arg in "$@"; do',
        '  if [ "$arg" = "https://astral.sh/uv/0.9.5/install.sh" ]; then',
        "    cat <<'DSEC_PINNED_UV_INSTALL'",
        "#!/bin/sh",
        "set -eu",
        'mkdir -p "$HOME/.local/bin"',
        'cp /mnt/dsec-verifier/bin/uv /mnt/dsec-verifier/bin/uvx "$HOME/.local/bin/"',
        'chmod 0755 "$HOME/.local/bin/uv" "$HOME/.local/bin/uvx"',
        'printf \'export PATH="$HOME/.local/bin:$PATH"\\n\' > "$HOME/.local/bin/env"',
        "DSEC_PINNED_UV_INSTALL",
        "    exit 0",
        "  fi",
        "done",
        'exec /usr/bin/curl "$@"',
    )
    lines = " ".join(shlex.quote(line) for line in script)
    return ("mkdir -p /root/.local/bin && "
            f"printf '%s\\n' {lines} > /root/.local/bin/curl && "
            "chmod 0755 /root/.local/bin/curl")


def _pinned_regex_log_apt_setup(task_id: str, test_sh: bytes) -> str:
    """Skip only the pinned test's curl-install preamble; keep test.sh intact.

    The curl shim from the same tool disk serves its sole fixed installer URL.
    A changed official script loses this optimization rather than silently
    skipping package operations it might need.
    """
    if task_id != "regex-log" or hashlib.sha256(test_sh).hexdigest() != (
            PINNED_TEST_SH_SHA256["regex-log"]):
        return "true"
    script = (
        "#!/bin/sh",
        'if [ "$#" -eq 1 ] && [ "$1" = update ]; then exit 0; fi',
        'if [ "$#" -eq 3 ] && [ "$1" = install ] && [ "$2" = -y ] && '
        '[ "$3" = curl ] && [ -x /root/.local/bin/curl ]; then exit 0; fi',
        'exec /usr/bin/apt-get "$@"',
    )
    lines = " ".join(shlex.quote(line) for line in script)
    return (f"printf '%s\\n' {lines} > /root/.local/bin/apt-get && "
            "chmod 0755 /root/.local/bin/apt-get")


def _legacy_uv_archive_link_setup(manifest: dict) -> str:
    """Map one pinned export path so its cached wheel links resolve in a VM.

    The r1 verifier disk copied uv's absolute wheel-cache links unchanged.
    The disk SHA is pinned before this runs; future artifacts should carry
    relative links and omit this compatibility field.
    """
    export_root = manifest.get("cache_archive_export_root")
    if not export_root:
        return "true"
    if not isinstance(export_root, str) or not export_root.startswith("/") or (
            "/cache/uv/archive-v0" not in export_root):
        raise ValueError("Invalid verifier cache archive export root")
    quoted_root = shlex.quote(export_root)
    quoted_parent = shlex.quote(str(Path(export_root).parent))
    return (f"mkdir -p {quoted_parent} && "
            f"ln -s /.cache/uv/archive-v0 {quoted_root} && "
            "test -d /.cache/uv/wheels-v5/index/46901b1a4cb2cba0/pytest/"
            "8.4.1-py3-none-any")


class TB2MicroVMEnv:
    def __init__(self, sandbox, *, task_id: str, task_dir: Path, after_exec=None,
                 verifier_mode: str = "canonical", backend_name="dsec-microvm",
                 verifier_phase=None, evidence_dir: Path | None = None):
        self.sandbox = sandbox
        self.task_id = task_id
        self.task_dir = Path(task_dir)
        identified = (self.task_dir.name == task_id or
                      (self.task_dir.name == "task" and self.task_dir.parent.name == task_id))
        if not identified or not (self.task_dir / "task.toml").is_file():
            raise ValueError("Pinned task directory does not match task ID")
        if not (self.task_dir / "tests" / "test.sh").is_file():
            raise ValueError("Official task verifier is missing")
        self.workdir = task_workdir(self.task_dir)
        if verifier_mode not in ("canonical", "offline", "offline-shared", "layered-uv"):
            raise ValueError("Unsupported verifier mode")
        if verifier_mode == "offline":
            offline_test_sh(task_id, (self.task_dir / "tests" / "test.sh").read_bytes())
        if verifier_mode == "offline-shared":
            shared_offline_test_sh(task_id, (self.task_dir / "tests" / "test.sh").read_bytes())
        if verifier_mode == "layered-uv":
            layered_uv_test_sh(task_id, (self.task_dir / "tests" / "test.sh").read_bytes())
        task_config = tomllib.loads((self.task_dir / "task.toml").read_text())
        self.verifier_timeout_s = int(task_config.get("verifier", {}).get("timeout_sec", 900))
        if not 1 <= self.verifier_timeout_s <= 12000:
            raise ValueError("Invalid TB2 verifier timeout")
        self.verifier_mode = verifier_mode
        self.backend_name = backend_name
        self.started = False
        self.evaluated = False
        self.exec_count = 0
        self.agent_exec_seconds = 0.0
        self.verifier_seconds = 0.0
        self.after_exec = after_exec
        self.verifier_phase = verifier_phase
        self.verifier_cache = False
        self.evidence_dir = Path(evidence_dir) if evidence_dir is not None else None

    async def _read_guest_evidence(self, source: str) -> bytes:
        quoted = shlex.quote(source)
        probe = await self._shell(f"stat -c %s {quoted} && sha256sum {quoted}")
        if probe["exit_code"] or probe["timed_out"] or probe["truncated"]:
            raise RuntimeError(f"Could not inspect verifier evidence: {source}")
        lines = probe["output"].splitlines()
        if len(lines) != 2 or not lines[0].isdigit():
            raise RuntimeError(f"Invalid verifier evidence metadata: {source}")
        size = int(lines[0])
        if size > MAX_EVIDENCE_BYTES:
            raise RuntimeError(f"Verifier evidence exceeds {MAX_EVIDENCE_BYTES} bytes: {source}")
        expected_sha = lines[1].split()[0]
        if len(expected_sha) != 64:
            raise RuntimeError(f"Invalid verifier evidence hash: {source}")
        chunks = []
        for index in range((size + EVIDENCE_CHUNK_BYTES - 1) // EVIDENCE_CHUNK_BYTES):
            chunk = await self._shell(
                f"dd if={quoted} bs={EVIDENCE_CHUNK_BYTES} skip={index} count=1 "
                "2>/dev/null | base64 -w0")
            if chunk["exit_code"] or chunk["timed_out"] or chunk["truncated"]:
                raise RuntimeError(f"Could not export verifier evidence: {source}")
            try:
                chunks.append(base64.b64decode(chunk["output"], validate=True))
            except ValueError as exc:
                raise RuntimeError(f"Invalid encoded verifier evidence: {source}") from exc
        data = b"".join(chunks)
        if len(data) != size or hashlib.sha256(data).hexdigest() != expected_sha:
            raise RuntimeError(f"Verifier evidence changed during export: {source}")
        return data

    async def _save_verifier_evidence(self, reward: float) -> dict:
        """Export complete verifier artifacts before the episode VM can stop."""
        destination = self.evidence_dir
        if destination is None:
            return {}
        if destination.exists():
            raise RuntimeError("Verifier evidence destination already exists")
        data = {}
        for name, source in (("ctrf.json", "/logs/verifier/ctrf.json"),
                             ("verifier.log", "/logs/verifier/dsec-test.log"),
                             ("reward.txt", "/logs/verifier/reward.txt")):
            data[name] = await self._read_guest_evidence(source)
        report = json.loads(data["ctrf.json"])
        if report["results"]["summary"]["tests"] <= 0:
            raise RuntimeError("Verifier evidence has no executed tests")
        if float(data["reward.txt"].decode().strip()) != reward:
            raise RuntimeError("Verifier reward sentinel disagrees with parsed verdict")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = destination.parent / ("." + uuid.uuid4().hex + ".tmp")
        temporary.mkdir(mode=0o700)
        try:
            artifacts = {}
            for name, content in data.items():
                path = temporary / name
                with path.open("xb") as stream:
                    os.chmod(path, 0o600)
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                artifacts[name] = {"bytes": len(content),
                                   "sha256": hashlib.sha256(content).hexdigest()}
            with (temporary / "manifest.json").open("x") as stream:
                os.chmod(temporary / "manifest.json", 0o600)
                json.dump({"task_id": self.task_id, "artifacts": artifacts}, stream,
                          sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary_fd = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(temporary_fd)
            finally:
                os.close(temporary_fd)
            os.rename(temporary, destination)
            directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return {"directory": str(destination), "artifacts": artifacts}

    async def _shell(self, command: str, *, timeout_ms: int = 120000,
                     verifier: bool = False):
        if self.backend_name == "dsec-microvm":
            # Docker's PATH prioritizes /usr/local/bin. The bootstrap guest
            # shell otherwise defaults to /bin, and appending image paths lets
            # apt-installed /usr/bin/pip shadow the image's Python pip.
            command = ("export HOME=/root "
                       "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; "
                       + command)
        shell = (self.sandbox.run_verifier_shell if verifier and
                 hasattr(self.sandbox, "run_verifier_shell") else self.sandbox.run_shell)
        return await shell(command, timeout_ms=timeout_ms,
                           output_limit=65536, request_id=uuid.uuid4().hex)

    async def _stage_tests(self):
        if hasattr(self.sandbox, "prepare_verifier"):
            await self.sandbox.prepare_verifier()
        tests = self.task_dir / "tests"
        files = sorted(tests.rglob("*"))
        if any(path.is_symlink() for path in files):
            raise ValueError("Verifier assets may not contain symlinks")
        assets = [path for path in files if path.is_file()]
        if not assets or sum(path.stat().st_size for path in assets) > 16 * 1024 * 1024:
            raise ValueError("Verifier assets missing or exceed staging limit")
        base64_probe = await self._shell("command -v base64 >/dev/null 2>&1")
        use_base64 = base64_probe["exit_code"] == 0 and not base64_probe["timed_out"]
        init = await self._shell("rm -rf /tests /logs/verifier && mkdir -p /tests /logs/verifier")
        if init["exit_code"] or init["timed_out"]:
            raise RuntimeError("Could not prepare verifier directory")
        for asset in assets:
            relative = asset.relative_to(tests)
            target = Path("/tests") / relative
            quoted = shlex.quote(str(target))
            prepare = await self._shell(f"mkdir -p {shlex.quote(str(target.parent))}; : > {quoted}")
            if prepare["exit_code"] or prepare["timed_out"]:
                raise RuntimeError("Could not create verifier asset")
            data = asset.read_bytes()
            if relative == Path("test.sh") and self.verifier_mode == "offline":
                data = offline_test_sh(self.task_id, data)
            if relative == Path("test.sh") and self.verifier_mode == "offline-shared":
                data = shared_offline_test_sh(self.task_id, data)
            if relative == Path("test.sh") and self.verifier_mode == "layered-uv":
                data = layered_uv_test_sh(self.task_id, data)
            # Both encodings stay below the guest agent's 64 KiB command cap.
            # Minimal task images may omit the external base64 utility.
            chunk_size = 45000 if use_base64 else 12000
            for offset in range(0, len(data), chunk_size):
                block = data[offset:offset + chunk_size]
                if use_base64:
                    chunk = base64.b64encode(block).decode()
                    command = f"printf %s '{chunk}' | base64 -d >> {quoted}"
                else:
                    chunk = "".join(f"\\{byte:03o}" for byte in block)
                    command = f"printf '%b' '{chunk}' >> {quoted}"
                written = await self._shell(command)
                if written["exit_code"] or written["timed_out"]:
                    raise RuntimeError("Could not transfer verifier asset")
            if relative == Path("test.sh"):
                executable = await self._shell(f"chmod 0755 {quoted}")
                if executable["exit_code"] or executable["timed_out"]:
                    raise RuntimeError("Could not make canonical verifier executable")

    async def reset(self, *, task_id: str):
        if task_id != self.task_id or self.started:
            raise ValueError("This per-episode VM only supports one reset for its pinned task")
        self.started = True
        observation = SimpleNamespace(
            instruction=(self.task_dir / "instruction.md").read_text(),
            output="", error="", info={"backend": self.backend_name})
        return SimpleNamespace(observation=observation, reward=None)

    async def step(self, action):
        if not self.started:
            raise RuntimeError("reset must precede step")
        if action.action_type == "evaluate":
            if self.evaluated:
                raise RuntimeError("This episode was already evaluated")
            self.evaluated = True
            verifier_started = time.monotonic()
            try:
                if self.verifier_mode in ("offline", "offline-shared", "layered-uv"):
                    status = await self.sandbox.status()
                    if (status.get("verifier_storage") not in ("local", "threefs_lazy") or
                            not status.get("verifier_artifact_sha256")):
                        raise RuntimeError("Offline verifier requires a pinned mounted tool disk")
                    if self.verifier_mode in ("offline-shared", "layered-uv"):
                        manifest = SHARED_MANIFEST if self.verifier_mode == "offline-shared" else LAYERED_UV_MANIFEST
                        pinned = json.loads(manifest.read_text()).get(
                            "verifier_artifact_sha256" if self.verifier_mode == "offline-shared"
                            else "shared_runtime_sha256")
                        if status["verifier_artifact_sha256"] != pinned:
                            raise RuntimeError("Pinned verifier runtime hash mismatch")
                await self._stage_tests()
                if self.verifier_phase is not None:
                    await self.verifier_phase("tests_staged")
                eval_command = _canonical_eval_cmd(
                    self.workdir, timeout_s=self.verifier_timeout_s)
                if self.verifier_mode == "canonical":
                    status = await self.sandbox.status()
                    if status.get("verifier_storage") in ("local", "threefs_lazy"):
                        canonical_manifest = os.getenv("DSEC_TB2_CANONICAL_VERIFIER_MANIFEST")
                        if status.get("verifier_dax"):
                            index_path = os.getenv("DSEC_TB2_TASK_VERIFIER_MANIFESTS_FILE")
                            if not index_path:
                                raise RuntimeError("DAX verifier requires an independent task manifest pin")
                            task_pins = json.loads(Path(index_path).read_text())
                            canonical_manifest = task_pins.get(self.task_id)
                            if not canonical_manifest:
                                raise RuntimeError("Task DAX verifier manifest pin is missing")
                        shared_manifest = json.loads(SHARED_MANIFEST.read_text())
                        pinned = (json.loads(Path(canonical_manifest).read_text())["sha256"]
                                  if canonical_manifest else
                                  shared_manifest["verifier_artifact_sha256"])
                        if status.get("verifier_artifact_sha256") != pinned:
                            raise RuntimeError("Canonical cache tool disk hash mismatch")
                        if self.backend_name == "dsec-microvm":
                            cache_setup = (
                                "test -d /mnt/dsec-verifier/cache/uv/archive-v0 && "
                                "test -d /mnt/dsec-verifier/runtime/python && "
                                "mkdir -p /mnt/dsec-root-base /.cache/uv && "
                                "mount -t ext4 /dev/vda /mnt/dsec-root-base && "
                                "mkdir -p /mnt/dsec-root-base/uv-cache-upper "
                                "/mnt/dsec-root-base/uv-cache-work && "
                                "mount -t overlay overlay -o "
                                "lowerdir=/mnt/dsec-verifier/cache/uv,"
                                "upperdir=/mnt/dsec-root-base/uv-cache-upper,"
                                "workdir=/mnt/dsec-root-base/uv-cache-work "
                                "/.cache/uv")
                        else:
                            cache_setup = (
                                "test -d /mnt/dsec-verifier/cache/uv/archive-v0 && "
                                "test -d /mnt/dsec-verifier/runtime/python && "
                                "test -d /.cache/uv/archive-v0")
                        if not status.get("verifier_dax") and LEGACY_CANONICAL_LINKS.is_file():
                            cache_links = json.loads(LEGACY_CANONICAL_LINKS.read_text())
                            if pinned == cache_links["artifact_sha256"]:
                                cache_setup += " && " + _legacy_uv_archive_link_setup(
                                    cache_links)
                        prepared = await self._shell(cache_setup + " && " +
                                                     _pinned_uv_installer_setup() + " && " +
                                                     _pinned_regex_log_apt_setup(
                                                         self.task_id,
                                                         (self.task_dir / "tests" /
                                                          "test.sh").read_bytes()))
                        if prepared["exit_code"] or prepared["timed_out"]:
                            raise RuntimeError(
                                "Could not prepare pinned canonical verifier cache: "
                                f"exit={prepared['exit_code']} "
                                f"output={prepared['output'][-1000:]!r}")
                        eval_command = (
                            "export PATH=/root/.local/bin:$PATH "
                            "UV_PYTHON_INSTALL_DIR=/mnt/dsec-verifier/runtime/python "
                            "UV_PYTHON_DOWNLOADS=never UV_CACHE_DIR=/.cache/uv "
                            + ("UV_LINK_MODE=symlink " if status.get("verifier_dax") else "") +
                            "UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple " +
                            ("UV_OFFLINE=0; " if os.getenv("DSEC_TB2_VERIFIER_ONLINE") == "1"
                             else "UV_OFFLINE=1; ")
                            + ("export GIT_CONFIG_COUNT=1 "
                               "GIT_CONFIG_KEY_0=http.version "
                               "GIT_CONFIG_VALUE_0=HTTP/1.1; "
                               if os.getenv("DSEC_TB2_GIT_HTTP11") == "1" else "")
                            + eval_command)
                        self.verifier_cache = True
                if self.verifier_phase is not None:
                    await self.verifier_phase("verifier_ready")
                if self.evidence_dir is not None or os.getenv("DSEC_CAPTURE_VERIFIER_LOG") == "1":
                    eval_command = (
                        "(" + eval_command + ") > /logs/verifier/dsec-test.log 2>&1; "
                        "status=$?; tail -c 8192 /logs/verifier/dsec-test.log; "
                        "exit $status")
                result = await self._shell(eval_command,
                    timeout_ms=self.verifier_timeout_s * 1000, verifier=True)
                if self.verifier_phase is not None:
                    await self.verifier_phase("test_sh_finished")
                reward = _parse_canonical_reward(result["output"])
                if result["timed_out"] or reward not in (0.0, 1.0):
                    raise RuntimeError(
                        "Canonical verifier did not produce a binary verdict: "
                        f"exit={result['exit_code']} timed_out={result['timed_out']} "
                        f"output_tail={result['output'][-1200:]!r}")
                ctrf = await self._shell(
                    "for py in /mnt/dsec-verifier/runtime/python/*/bin/python3.13 "
                    "/root/.local/share/uv/python/*/bin/python3.13 "
                    "/usr/bin/python3 /bin/python3; do "
                    "test -x \"$py\" || continue; "
                    "\"$py\" -c 'import json,sys; "
                    "r=json.load(open(sys.argv[1])); "
                    "n=r[\"results\"][\"summary\"][\"tests\"]; "
                    "assert isinstance(n,int) and n>0; print(n)' "
                    "/logs/verifier/ctrf.json; exit $?; done; exit 127")
                if ctrf["exit_code"] or ctrf["timed_out"] or ctrf["truncated"]:
                    raise RuntimeError("Verifier wrote a reward without a complete CTRF report; "
                                       f"ctrf_exit={ctrf['exit_code']} "
                                       f"ctrf_output={ctrf['output'][-400:]!r}; "
                                       f"verifier output tail: {result['output'][-1600:]!r}")
                if not ctrf["output"].strip().isdigit():
                    raise RuntimeError("Verifier CTRF report has no executed tests")
                evidence = await self._save_verifier_evidence(reward)
                observation = SimpleNamespace(
                    instruction="", output=result["output"], error="",
                    info={"harness": ("tests/test.sh" if self.verifier_mode == "canonical"
                                      else "tests/test.sh+" + self.verifier_mode + "-bootstrap"),
                          "backend": self.backend_name, "verifier_mode":self.verifier_mode,
                          "verifier_cache":self.verifier_cache,
                          "evidence":evidence})
                return SimpleNamespace(observation=observation, reward=reward)
            except Exception as exc:
                observation = SimpleNamespace(
                    instruction="", output="", error=f"{type(exc).__name__}: {exc}",
                    info={"backend": self.backend_name, "verifier_mode":self.verifier_mode})
                return SimpleNamespace(observation=observation, reward=None)
            finally:
                self.verifier_seconds += time.monotonic() - verifier_started
        if action.action_type != "exec":
            raise ValueError("Only exec is supported by the DSec microVM adapter")
        if self.evaluated:
            raise RuntimeError("No agent actions are allowed after verifier staging")
        if not isinstance(action.command, str) or not action.command.strip():
            raise ValueError("exec requires a command")
        exec_started = time.monotonic()
        try:
            result = await self._shell("cd " + shlex.quote(self.workdir) +
                                       " && " + action.command)
        finally:
            self.agent_exec_seconds += time.monotonic() - exec_started
        self.exec_count += 1
        if self.after_exec is not None:
            await self.after_exec(self)
        observation = SimpleNamespace(
            instruction="", output=result["output"], error="",
            info={"exit_code": result["exit_code"], "timed_out": result["timed_out"],
                  "truncated": result["truncated"], "backend": self.backend_name})
        return SimpleNamespace(observation=observation, reward=None)
