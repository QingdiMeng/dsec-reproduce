"""E1 layered container backend; Docker owns namespaces, EROFS/OverlayFS own files."""

from __future__ import annotations

import hashlib
import fcntl
import json
from pathlib import Path
import re
import subprocess
import time
import uuid

from dsec.runtime.admission_guard import check_container_create
from dsec.runtime.backends.docker_broker import run_docker


class ContainerBackendError(RuntimeError):
    pass


def _run(argv, timeout=30, check=True):
    result = (run_docker(argv, timeout=timeout, check=False) if argv[0] == "docker"
              else subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, timeout=timeout))
    if check and result.returncode:
        raise ContainerBackendError(f"{argv[0]} failed ({result.returncode}): {result.stderr[-1000:]}")
    return result


def _qos_cpu(qos):
    if qos == "default":
        return None
    match = re.fullmatch(r"(?:ls_core_cookie|be_sched_idle):([2-9])", qos)
    if not match:
        raise ValueError("Unsupported E4 CPU QoS profile")
    return int(match.group(1))


def _qos_probe(name, qos):
    if qos == "default":
        return {"requested": qos, "effective": True}
    result = _run(["docker", "exec", name, "python3", "-B", "/dsec-agent.py",
                   "qos-probe"], check=False)
    try:
        proof = json.loads(result.stdout)
        mode, cpu_text = qos.split(":")
        cpu = int(cpu_text)
        expected_policy = 5 if mode == "be_sched_idle" else 0
        effective = (result.returncode == 0 and proof["profile"] == qos
                     and proof["affinity"] == [cpu]
                     and proof["policy"] == expected_policy
                     and ((proof["core_cookie"] != 0) == (mode == "ls_core_cookie")))
    except (ValueError, KeyError, TypeError):
        proof = {"error": result.stderr[-500:]}
        effective = False
    return {"requested": qos, "effective": effective, "probe": proof}


class LayeredContainerBackend:
    container_prefix = "dsec-e1-"
    docker_label = "e1-erofs-overlay"

    def __init__(self, *, artifacts=None, root, image, agent, layers=None,
                 environment_id="erofs_overlay", catalog_sha256=None, storage="local"):
        self.artifacts = Path(artifacts).resolve(strict=True) if artifacts else None
        self.root = Path(root).resolve()
        self.agent = Path(agent).resolve(strict=True)
        self.image = image
        self.environment_id = environment_id
        self.catalog_sha256 = catalog_sha256
        self.storage = storage
        self.remote_sizes = {}
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise ValueError("Container image must be pinned by digest")
        self.hashes = {}
        if layers is None:
            if self.artifacts is None:
                raise ValueError("EROFS layers or legacy artifacts are required")
            manifest = json.loads((self.artifacts / "manifest.json").read_text())
            self.layers = [(name, self.artifacts / (name + ".erofs"))
                           for name in ("base", "workspace", "toolkit")]
            expected = {name: manifest["builds"][name]["sha256"]
                        for name, _ in self.layers}
            self.layer_sources = {name: "local" for name, _ in self.layers}
        else:
            self.layers = [(layer["name"], Path(layer["file"]).resolve(strict=True))
                           for layer in layers]
            expected = {layer["name"]: layer["sha256"] for layer in layers}
            self.layer_sources = {layer["name"]: layer.get("source", "local")
                                  for layer in layers}
            self.remote_sizes = {layer["name"]: layer["bytes"] for layer in layers
                                 if layer.get("source") == "threefs_lazy"}
            if (not 1 <= len(self.layers) <= 17 or len(expected) != len(self.layers) or
                    any(not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name)
                        for name, _ in self.layers) or
                    any(source not in ("local", "threefs_lazy")
                        for source in self.layer_sources.values())):
                raise ValueError("Invalid EROFS layer catalog")
        for name, path in self.layers:
            if self.layer_sources[name] == "local":
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if digest != expected[name]:
                    raise ValueError(f"{name} artifact digest mismatch")
            self.hashes[name] = expected[name]
        if storage == "local" and any(source != "local" for source in self.layer_sources.values()):
            raise ValueError("Local EROFS profile cannot use remote layers")
        if storage == "threefs_lazy" and not any(
                source == "threefs_lazy" for source in self.layer_sources.values()):
            raise ValueError("3FS EROFS profile requires at least one remote layer")
        _run(["docker", "image", "inspect", image])
        self.root.mkdir(parents=True, exist_ok=True)

    def purge_private(self, sid):
        if not re.fullmatch(r"[a-f0-9]{32}", sid):
            raise ValueError("Invalid sandbox ID")
        private = self.root / sid
        if not private.exists():
            return
        # The overlay upper contains root-owned files. Bind only this sandbox's
        # private directory into a short-lived helper after its container exits.
        script = ("import pathlib,shutil; p=pathlib.Path('/dsec-private'); "
                  "[shutil.rmtree(x) if x.is_dir() and not x.is_symlink() "
                  "else x.unlink() for x in p.iterdir()]")
        _run(["docker", "run", "--rm", "--network", "none",
              "--mount", f"type=bind,src={private},dst=/dsec-private",
              self.image, "python3", "-c", script], timeout=30)
        private.rmdir()

    def create(self, *, memory_mb=512, cpus=1.0, qos="default", sandbox_id=None):
        if not isinstance(memory_mb, int) or isinstance(memory_mb, bool) or memory_mb < 128:
            raise ValueError("memory_mb must be at least 128")
        if not isinstance(cpus, (int, float)) or isinstance(cpus, bool) or cpus <= 0:
            raise ValueError("cpus must be positive")
        _qos_cpu(qos)
        if sandbox_id is not None and not re.fullmatch(r"[a-f0-9]{32}", sandbox_id):
            raise ValueError("Invalid sandbox ID")
        environment_id = ({"erofs_overlay": "e1-real",
                           "e2_full_erofs": "e2-full"}.get(self.environment_id,
                                                              self.environment_id))
        if not getattr(self, 'edge_node_admission', False):
            check_container_create(self.root, sandbox_id, {
                "environment_id": environment_id, "storage": self.storage,
                "memory_mb": memory_mb, "cpus": cpus, "qos": qos})
        # losetup -f and the following attach are not atomic. Serialize the
        # layer setup across clients using this backend on one host.
        with (self.root / ".create.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._create_locked(memory_mb, cpus, qos, sandbox_id)

    def _create_locked(self, memory_mb, cpus, qos, sandbox_id=None):
        for layer_name, path in self.layers:
            if self.layer_sources[layer_name] == "threefs_lazy":
                actual_size = _run(["stat", "-c", "%s", str(path)], timeout=5)
                if int(actual_size.stdout.strip()) != self.remote_sizes[layer_name]:
                    raise ContainerBackendError("3FS EROFS layer size changed: " + layer_name)
                fs = _run(["findmnt", "-T", str(path), "-n", "-o", "FSTYPE"])
                if fs.stdout.strip() != "fuse.hf3fs":
                    raise ContainerBackendError("3FS EROFS layer mount is unavailable: " + layer_name)
        sid = sandbox_id or uuid.uuid4().hex
        name = self.container_prefix + sid
        private = self.root / sid
        private.mkdir(mode=0o700)
        (private / "config.json").write_text(json.dumps({"cpu_qos": qos,
            "artifact_sha256": self.hashes, "environment_id": self.environment_id,
            "artifact_source": self.layer_sources, "storage": self.storage,
            "runtime_image": self.image, "catalog_sha256": self.catalog_sha256}) + "\n")
        qos_args = ["--env", "DSEC_QOS=" + qos,
                    "--env", "DSEC_SANDBOX_ID=" + sid]
        cpu = _qos_cpu(qos)
        if cpu is not None:
            qos_args += ["--cpuset-cpus", str(cpu)]
        source_mounts = (["--mount", f"type=bind,src={self.artifacts},dst=/dsec-source,readonly,bind-propagation=rprivate"]
                         if self.artifacts else
                         [field for layer_name, path in self.layers for field in
                          ("--mount", f"type=bind,src={path},dst=/dsec-source/{layer_name}.erofs,readonly")])
        try:
            _run(["docker", "run", "-d", "--name", name,
                  "--label", "dsec.backend=" + self.docker_label,
                  "--label", "dsec.sandbox_id=" + sid, "--network", "none",
                  "--memory", f"{memory_mb}m", "--memory-swap", f"{memory_mb}m",
                  "--cpus", str(cpus), "--pids-limit", "256",
                  "--cap-add", "SYS_ADMIN", "--security-opt", "apparmor=unconfined",
                  "--device", "/dev/loop-control", "--device-cgroup-rule", "b 7:* rwm",
                  *source_mounts,
                  "--mount", f"type=bind,src={private},dst=/dsec-private,bind-propagation=rprivate",
                  "--mount", f"type=bind,src={self.agent},dst=/dsec-agent.py,readonly",
                  "--env", "DSEC_LAYER_NAMES=" + ",".join(name for name, _ in self.layers),
                  *qos_args,
                  self.image, "python3", "-B", "/dsec-agent.py", "serve"])
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if (private / "ready.json").exists():
                    container = LayeredContainer(self, sid)
                    if container.status()["state"] != "RUNNING":
                        raise ContainerBackendError("E1 runtime capability probe failed")
                    return container
                state = _run(["docker", "inspect", "--format", "{{.State.Running}}", name], check=False)
                if state.returncode or state.stdout.strip() != "true":
                    logs = _run(["docker", "logs", name], check=False)
                    raise ContainerBackendError("Container exited during setup: " + logs.stderr[-1000:])
                time.sleep(0.1)
            raise ContainerBackendError("Timed out waiting for EROFS/OverlayFS setup")
        except BaseException:
            _run(["docker", "stop", "--timeout", "5", name], timeout=12, check=False)
            _run(["docker", "rm", "-f", name], check=False)
            self.purge_private(sid)
            raise

    def attach(self, sid):
        if not re.fullmatch(r"[a-f0-9]{32}", sid):
            raise ValueError("Invalid sandbox ID")
        container = LayeredContainer(self, sid)
        if container.status()["state"] != "RUNNING":
            raise ContainerBackendError("Container is not running")
        return container

    def prove_running(self, sid):
        """Read-only attestation before repairing an uncertain create result."""
        if not re.fullmatch(r"[a-f0-9]{32}", sid):
            return False
        name = self.container_prefix + sid
        try:
            result = _run(["docker", "inspect", name], check=False)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode:
            return False
        try:
            value = json.loads(result.stdout)[0]
            labels = value["Config"]["Labels"]
            env = value["Config"]["Env"]
            identified = (value["Name"] == "/" + name and value["State"]["Running"]
                          and labels["dsec.backend"] == self.docker_label
                          and labels["dsec.sandbox_id"] == sid
                          and "DSEC_SANDBOX_ID=" + sid in env)
            return identified and self.attach(sid).status()["state"] == "RUNNING"
        except (IndexError, KeyError, TypeError, ValueError, ContainerBackendError, OSError):
            return False

    def prove_container_absent(self, sid):
        """A daemon error is not proof that a container disappeared."""
        if not re.fullmatch(r"[a-f0-9]{32}", sid):
            return False
        try:
            result = _run(["docker", "container", "ls", "-a", "--format", "{{.Names}}"],
                          check=False)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode:
            return False
        return self.container_prefix + sid not in result.stdout.splitlines()

    def prove_stopped(self, sid):
        return self.prove_container_absent(sid) and not (self.root / sid).exists()


class LayeredContainer:
    backend = "container"

    def __init__(self, manager, sid):
        self.manager = manager
        self.id = sid
        self.name = manager.container_prefix + sid
        self.qos = json.loads((self.manager.root / sid / "config.json").read_text())["cpu_qos"]

    def status(self):
        result = _run(["docker", "inspect", "--format", "{{.State.Running}} {{.Image}}", self.name], check=False)
        parts = result.stdout.split()
        running = result.returncode == 0 and len(parts) == 2 and parts[0] == "true"
        image_ok = running and parts[1] == self.manager.image
        ready = (self.manager.root / self.id / "ready.json").exists()
        config_path = self.manager.root / self.id / "config.json"
        pinned = json.loads(config_path.read_text()) if config_path.exists() else {}
        artifact_ok = (bool(pinned) and pinned.get("artifact_sha256", self.manager.hashes) == self.manager.hashes
                       and pinned.get("environment_id", self.manager.environment_id) == self.manager.environment_id
                       and pinned.get("runtime_image", self.manager.image) == self.manager.image
                       and pinned.get("artifact_source", self.manager.layer_sources) ==
                       self.manager.layer_sources
                       and pinned.get("storage", self.manager.storage) == self.manager.storage
                       and pinned.get("catalog_sha256", self.manager.catalog_sha256) ==
                       self.manager.catalog_sha256)
        mounted = False
        erofs = False
        private_upper = False
        network_none = False
        remote_source_ok = not any(source == "threefs_lazy"
                                   for source in self.manager.layer_sources.values())
        if running and ready:
            probe = _run(["docker", "exec", self.name, "findmnt", "-n", "-o", "FSTYPE",
                          "/dsec-private/rootfs"], check=False)
            mounted = probe.returncode == 0 and probe.stdout.strip() == "overlay"
            layer_types = []
            for layer, _ in self.manager.layers:
                lower = _run(["docker", "exec", self.name, "findmnt", "-n", "-o", "FSTYPE",
                              "/dsec-private/lower-" + layer], check=False)
                layer_types.append(lower.returncode == 0 and lower.stdout.strip() == "erofs")
            erofs = all(layer_types)
            remote_checks = []
            for layer_name, source in self.manager.layer_sources.items():
                if source == "threefs_lazy":
                    backing = _run(["docker", "exec", self.name, "findmnt", "-T",
                                    "/dsec-source/" + layer_name + ".erofs", "-n",
                                    "-o", "FSTYPE"], check=False)
                    remote_checks.append(backing.returncode == 0 and
                                         backing.stdout.strip() == "fuse.hf3fs")
            if remote_checks:
                remote_source_ok = all(remote_checks)
            options = _run(["docker", "exec", self.name, "findmnt", "-n", "-o", "OPTIONS",
                            "/dsec-private/rootfs"], check=False)
            private_upper = options.returncode == 0 and f"upperdir=/dsec-private/upper" in options.stdout
            network = _run(["docker", "inspect", "--format", "{{.HostConfig.NetworkMode}}", self.name], check=False)
            network_none = network.returncode == 0 and network.stdout.strip() == "none"
        qos = _qos_probe(self.name, self.qos) if running and ready else {"requested": self.qos, "effective": False}
        active = (running and image_ok and ready and mounted and erofs and private_upper and
                  network_none and artifact_ok and remote_source_ok and qos["effective"])
        return {"id": self.id, "backend": "container", "state": "RUNNING" if active else "STOPPED",
                "environment": self.manager.environment_id, "storage": self.manager.storage,
                "layer_sources": self.manager.layer_sources,
                "cpu_qos": qos,
                "artifact_sha256": self.manager.hashes,
                "actual_mechanisms": {"erofs": erofs, "overlayfs": mounted,
                                      "private_upper": private_upper, "network_none": network_none,
                                      "remote_source": remote_source_ok,
                                      "artifact_pin": artifact_ok, "runtime_image_pin": image_ok}}

    def run_shell(self, command, *, timeout_ms=5000, output_limit=65536, request_id=None):
        if not isinstance(command, str) or not command:
            raise ValueError("command must be a nonempty string")
        if (not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool)
                or not 1 <= timeout_ms <= 30000):
            raise ValueError("timeout_ms must be between 1 and 30000")
        if (not isinstance(output_limit, int) or isinstance(output_limit, bool)
                or not 1 <= output_limit <= 1048576):
            raise ValueError("output_limit must be between 1 and 1048576")
        if self.status()["state"] != "RUNNING":
            raise ContainerBackendError("Container is not running")
        if request_id is not None:
            if not re.fullmatch(r"[0-9a-f]{32}", request_id):
                raise ValueError("Invalid request ID")
            try:
                response = _run(["docker", "exec", self.name, "python3", "-B", "/dsec-agent.py",
                                 "request-shell", request_id, str(timeout_ms),
                                 str(output_limit), "--", command],
                                timeout=timeout_ms / 1000 + 8, check=False)
            except subprocess.TimeoutExpired as exc:
                from dsec.sdk.sandbox_transport import RequestOutcomeUnknown
                raise RequestOutcomeUnknown("Container request result unknown after Docker timeout",
                                            request_id) from exc
            try:
                proof = json.loads(response.stdout)
                if response.returncode or proof["request_id"] != request_id:
                    raise ValueError("Invalid container request response")
            except (ValueError, KeyError) as exc:
                from dsec.sdk.sandbox_transport import RequestOutcomeUnknown
                raise RequestOutcomeUnknown("Container request response unavailable: "
                                            + response.stderr[-500:], request_id) from exc
            if proof["state"] == "DONE" and proof["response"]["ok"]:
                return proof["response"]["result"]
            if proof["state"] == "CONFLICT":
                raise ValueError("Container request ID used with different arguments")
            from dsec.sdk.sandbox_transport import RequestOutcomeUnknown
            raise RequestOutcomeUnknown("Container request state: " + proof["state"], request_id)
        try:
            result = _run(["docker", "exec", self.name, "python3", "-B", "/dsec-agent.py",
                           "shell", command], timeout=timeout_ms / 1000, check=False)
        except subprocess.TimeoutExpired as exc:
            # docker exec can have executed a non-idempotent action before timeout.
            from dsec.sdk.sandbox_transport import RequestOutcomeUnknown
            raise RequestOutcomeUnknown("Container command outcome unknown after timeout") from exc
        if result.stderr:
            from dsec.sdk.sandbox_transport import RequestOutcomeUnknown
            raise RequestOutcomeUnknown("Container execution failed with uncertain command outcome: "
                                        + result.stderr[-500:])
        output = result.stdout + result.stderr
        return {"exit_code": result.returncode, "output": output[:output_limit],
                "truncated": len(output) > output_limit}

    def query_request(self, request_id):
        if not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise ValueError("Invalid request ID")
        if self.status()["state"] != "RUNNING":
            raise ContainerBackendError("Container is not running")
        result = _run(["docker", "exec", self.name, "python3", "-B", "/dsec-agent.py",
                       "request-query", request_id], check=False)
        if result.returncode:
            raise ContainerBackendError("Container request query failed: " + result.stderr[-500:])
        proof = json.loads(result.stdout)
        if proof["request_id"] != request_id:
            raise ContainerBackendError("Container request ID mismatch")
        return proof

    def stop(self):
        _run(["docker", "stop", "--timeout", "5", self.name], timeout=12, check=False)
        _run(["docker", "rm", "-f", self.name], check=False)
        if not self.manager.prove_container_absent(self.id):
            raise ContainerBackendError("Container absence has not been proven after stop")
        self.manager.purge_private(self.id)
        return self.status()


E2_META_SHA256 = "6e1d91144eb986286e3b3308b8004b05ca25a8a9929370a14ed518c3182854be"
E2_BLOB_SHA256 = "e592bfa6dae75ec7314a4015fe0114382f0eeec880be60d04f09a04f2372363d"


class FullErofsBackend(LayeredContainerBackend):
    """Same E2 full image via local blob or real 3FS FUSE blob."""

    container_prefix = "dsec-e2-"
    docker_label = "e2-full-erofs"

    def __init__(self, *, meta_dir, local_blob, remote_mount, root, image, agent,
                 mount_helper, storage, metadata_file=None, data_file=None,
                 metadata_sha256=E2_META_SHA256, data_sha256=E2_BLOB_SHA256,
                 environment_id="e2_full_erofs", catalog_sha256=None):
        if storage not in ("local", "threefs_lazy"):
            raise ValueError("Invalid E2 storage")
        self.storage = storage
        self.meta_dir = Path(meta_dir).resolve(strict=True)
        self.metadata_file = (Path(metadata_file).resolve(strict=True) if metadata_file else
                              self.meta_dir / "full.meta.erofs")
        self.local_blob = Path(local_blob).resolve(strict=True) if storage == "local" else None
        self.remote_mount = Path(remote_mount).resolve(strict=True) if storage == "threefs_lazy" else None
        self.data_file = (Path(data_file).resolve(strict=True) if data_file else
                          self.local_blob if storage == "local" else
                          self.remote_mount / "e2-full-split/full.blob")
        self.environment_id = environment_id
        self.catalog_sha256 = catalog_sha256
        self.root = Path(root).resolve()
        self.image = image
        self.agent = Path(agent).resolve(strict=True)
        self.mount_helper = Path(mount_helper).resolve(strict=True)
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise ValueError("Container image must be pinned by digest")
        digest = hashlib.sha256(self.metadata_file.read_bytes()).hexdigest()
        if digest != metadata_sha256:
            raise ValueError("EROFS metadata digest mismatch")
        self.hashes = ({"full.meta.erofs": digest, "full.blob": data_sha256}
                       if environment_id == "e2_full_erofs" and catalog_sha256 is None else
                       {"metadata": digest, "data": data_sha256})
        if storage == "local":
            h = hashlib.sha256()
            with self.data_file.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    h.update(block)
            if h.hexdigest() != data_sha256:
                raise ValueError("Local EROFS blob digest mismatch")
        else:
            mount = _run(["findmnt", "-T", str(self.remote_mount), "-n", "-o", "FSTYPE"])
            if mount.stdout.strip() != "fuse.hf3fs":
                raise ContainerBackendError("3FS FUSE client is not mounted")
        _run(["docker", "image", "inspect", image])
        self.root.mkdir(parents=True, exist_ok=True)
        if storage == "threefs_lazy":
            try:
                self.data_file.relative_to(self.remote_mount)
            except ValueError as exc:
                raise ValueError("3FS data file is outside the 3FS mount") from exc

    def _create_locked(self, memory_mb, cpus, qos, sandbox_id=None):
        sid = sandbox_id or uuid.uuid4().hex
        name = self.container_prefix + sid
        private = self.root / sid
        private.mkdir(mode=0o700)
        (private / "config.json").write_text(json.dumps({"cpu_qos": qos,
            "artifact_sha256": self.hashes, "environment_id": self.environment_id,
            "runtime_image": self.image, "catalog_sha256": self.catalog_sha256}) + "\n")
        qos_args = ["--env", "DSEC_QOS=" + qos,
                    "--env", "DSEC_SANDBOX_ID=" + sid]
        cpu = _qos_cpu(qos)
        if cpu is not None:
            qos_args += ["--cpuset-cpus", str(cpu)]
        mounts = ["--mount", f"type=bind,src={self.meta_dir},dst=/dsec-meta,readonly",
                  "--mount", f"type=bind,src={private},dst=/dsec-private",
                  "--mount", f"type=bind,src={self.agent},dst=/dsec-agent.py,readonly",
                  "--mount", f"type=bind,src={self.mount_helper},dst=/dsec-mount.py,readonly"]
        if self.storage == "local":
            mounts += ["--mount", f"type=bind,src={self.local_blob.parent},dst=/dsec-local,readonly"]
        else:
            mounts += ["--mount", f"type=bind,src={self.remote_mount},dst=/dsec-remote,readonly,bind-propagation=rslave"]
        try:
            metadata_inside = "/dsec-meta/" + str(self.metadata_file.relative_to(self.meta_dir))
            data_inside = ("/dsec-local/" + self.data_file.name if self.storage == "local" else
                           "/dsec-remote/" + str(self.data_file.relative_to(self.remote_mount)))
            _run(["docker", "run", "-d", "--name", name,
                  "--label", "dsec.backend=" + self.docker_label,
                  "--label", "dsec.sandbox_id=" + sid, "--network", "none",
                  "--memory", f"{memory_mb}m", "--memory-swap", f"{memory_mb}m",
                  "--cpus", str(cpus), "--pids-limit", "256",
                  "--cap-add", "SYS_ADMIN", "--security-opt", "apparmor=unconfined",
                  "--env", "DSEC_STORAGE=" + self.storage,
                  "--env", "DSEC_EROFS_METADATA=" + metadata_inside,
                  "--env", "DSEC_EROFS_DATA=" + data_inside,
                  *mounts, *qos_args,
                  self.image, "python3", "-B", "/dsec-agent.py", "full-serve"])
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if (private / "ready.json").exists():
                    container = FullErofsContainer(self, sid)
                    if container.status()["state"] != "RUNNING":
                        raise ContainerBackendError("E2 runtime capability probe failed")
                    return container
                state = _run(["docker", "inspect", "--format", "{{.State.Running}}", name], check=False)
                if state.returncode or state.stdout.strip() != "true":
                    logs = _run(["docker", "logs", name], check=False)
                    raise ContainerBackendError("Container exited during setup: " + logs.stderr[-1000:])
                time.sleep(0.1)
            raise ContainerBackendError("Timed out waiting for full EROFS/OverlayFS setup")
        except BaseException:
            _run(["docker", "stop", "--timeout", "5", name], timeout=12, check=False)
            _run(["docker", "rm", "-f", name], check=False)
            self.purge_private(sid)
            raise

    def attach(self, sid):
        if not re.fullmatch(r"[a-f0-9]{32}", sid):
            raise ValueError("Invalid sandbox ID")
        container = FullErofsContainer(self, sid)
        if container.status()["state"] != "RUNNING":
            raise ContainerBackendError("Container is not running")
        return container


class FullErofsContainer(LayeredContainer):
    def __init__(self, manager, sid):
        super().__init__(manager, sid)
        self.name = manager.container_prefix + sid

    def status(self):
        result = _run(["docker", "inspect", "--format",
                       "{{.State.Running}} {{.Image}}", self.name], check=False)
        parts = result.stdout.split()
        running = result.returncode == 0 and len(parts) == 2 and parts[0] == "true"
        image_ok = running and parts[1] == self.manager.image
        ready = (self.manager.root / self.id / "ready.json").exists()
        erofs = overlay = private_upper = network_none = source_ok = False
        config_path = self.manager.root / self.id / "config.json"
        pinned = json.loads(config_path.read_text()) if config_path.exists() else {}
        artifact_ok = (bool(pinned) and
                       pinned.get("artifact_sha256", self.manager.hashes) == self.manager.hashes and
                       pinned.get("environment_id", self.manager.environment_id) ==
                       self.manager.environment_id and
                       pinned.get("runtime_image", self.manager.image) == self.manager.image and
                       pinned.get("catalog_sha256", self.manager.catalog_sha256) ==
                       self.manager.catalog_sha256)
        if running and ready:
            lower = _run(["docker", "exec", self.name, "findmnt", "-n", "-o", "FSTYPE",
                          "/dsec-private/lower-full"], check=False)
            erofs = lower.returncode == 0 and lower.stdout.strip() == "erofs"
            upper = _run(["docker", "exec", self.name, "findmnt", "-n", "-o", "FSTYPE,OPTIONS",
                          "/dsec-private/rootfs"], check=False)
            overlay = upper.returncode == 0 and upper.stdout.startswith("overlay ")
            private_upper = "upperdir=/dsec-private/upper" in upper.stdout
            network = _run(["docker", "inspect", "--format", "{{.HostConfig.NetworkMode}}", self.name], check=False)
            network_none = network.returncode == 0 and network.stdout.strip() == "none"
            if self.manager.storage == "threefs_lazy":
                path = "/dsec-remote/" + str(self.manager.data_file.relative_to(self.manager.remote_mount))
                source = _run(["docker", "exec", self.name, "findmnt", "-T",
                               path, "-n", "-o", "FSTYPE"], check=False)
                source_ok = source.returncode == 0 and source.stdout.strip() == "fuse.hf3fs"
            else:
                source = _run(["docker", "exec", self.name, "test", "-r",
                               "/dsec-local/" + self.manager.data_file.name], check=False)
                source_ok = source.returncode == 0
        qos = _qos_probe(self.name, self.qos) if running and ready else {"requested": self.qos, "effective": False}
        active = (running and image_ok and ready and erofs and overlay and private_upper and
                  network_none and source_ok and artifact_ok and qos["effective"])
        return {"id": self.id, "backend": "container", "state": "RUNNING" if active else "STOPPED",
                "environment": self.manager.environment_id, "storage": self.manager.storage,
                "cpu_qos": qos,
                "artifact_sha256": self.manager.hashes,
                "actual_mechanisms": {"erofs": erofs, "overlayfs": overlay,
                    "private_upper": private_upper, "network_none": network_none,
                    "storage_source": source_ok, "artifact_pin": artifact_ok,
                    "runtime_image_pin": image_ok,
                    "threefs_lazy": source_ok and self.manager.storage == "threefs_lazy"}}


class CatalogErofsBackend(FullErofsBackend):
    """An arbitrary pinned environment using the E2-proven split EROFS path."""

    container_prefix = "dsec-env-"
    docker_label = "erofs-split"


class CatalogLayeredBackend(LayeredContainerBackend):
    """An arbitrary pinned list of shared EROFS layers and private upperdir."""

    container_prefix = "dsec-env-"
    docker_label = "erofs-layers"
