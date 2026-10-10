"""Mount one E1 environment inside its own Docker mount/PID/network namespace."""

import argparse
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time


def start_native():
    binary = os.environ.get("DSEC_NATIVE_AGENT")
    if not binary:
        return None
    apply_qos()
    socket_path = Path("/dsec-private/native.sock")
    if socket_path.exists():
        raise RuntimeError("Native agent socket already exists")
    process = subprocess.Popen([binary, "--unix", str(socket_path),
                                "--root", "/dsec-private/rootfs"])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Native agent exited during startup")
        if socket_path.exists():
            return process
        time.sleep(.01)
    process.terminate()
    process.wait(timeout=5)
    raise RuntimeError("Native agent did not become ready")


def stop_native(process):
    if process is not None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        Path("/dsec-private/native.sock").unlink(missing_ok=True)


def run(*argv):
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise RuntimeError(f"{argv}: {result.stderr[-1000:]}")


def apply_qos():
    spec = os.environ.get("DSEC_QOS", "default")
    if spec == "default":
        return {"profile": spec}
    mode, separator, cpu_text = spec.partition(":")
    if separator != ":" or mode not in ("ls_core_cookie", "be_sched_idle") or not cpu_text.isdigit():
        raise ValueError("Invalid DSEC_QOS")
    cpu = int(cpu_text)
    if cpu not in range(2, 10):
        raise ValueError("E4 CPU must be 2–9")
    os.sched_setaffinity(0, {cpu})
    libc = ctypes.CDLL(None, use_errno=True)
    if mode == "be_sched_idle":
        # Alpine's Python sched_getscheduler wrapper returns ENOSYS in this
        # fixed control image; use the Linux x86_64 syscall directly.
        priority = ctypes.c_int(0)
        if libc.syscall(144, 0, 5, ctypes.byref(priority)) != 0:
            raise OSError(ctypes.get_errno(), "sched_setscheduler SCHED_IDLE failed")
    else:
        # PR_SCHED_CORE_CREATE (1), scope THREAD_GROUP (1): all current
        # threads receive one cookie; descendants inherit it on fork.
        if libc.prctl(62, 1, 0, 1, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_SCHED_CORE_CREATE failed")
    value = ctypes.c_ulonglong(0)
    if libc.prctl(62, 0, 0, 0, ctypes.byref(value)) != 0:
        raise OSError(ctypes.get_errno(), "PR_SCHED_CORE_GET failed")
    policy = libc.syscall(145, 0)
    if policy < 0:
        raise OSError(ctypes.get_errno(), "sched_getscheduler failed")
    affinity = sorted(os.sched_getaffinity(0))
    expected_policy = os.SCHED_IDLE if mode == "be_sched_idle" else os.SCHED_OTHER
    if policy != expected_policy or affinity != [cpu] or ((value.value != 0) != (mode == "ls_core_cookie")):
        raise RuntimeError("E4 scheduler state does not match requested profile")
    return {"profile": spec, "affinity": affinity, "policy": policy, "core_cookie": value.value}


def serve():
    source = Path("/dsec-source")
    private = Path("/dsec-private")
    layers = tuple(os.environ.get("DSEC_LAYER_NAMES", "base,workspace,toolkit").split(","))
    if (not 1 <= len(layers) <= 17 or len(layers) != len(set(layers)) or
            any(not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name) for name in layers)):
        raise ValueError("Invalid EROFS layer list")
    mounts = []
    loops = []
    native = None
    try:
        for name in layers:
            target = private / ("lower-" + name)
            target.mkdir(parents=True, exist_ok=True)
            device = subprocess.check_output(["losetup", "-f"], text=True).split()[0]
            if not device.startswith("/dev/loop") or not device[9:].isdigit():
                raise RuntimeError("Invalid loop device")
            if not Path(device).exists():
                os.mknod(device, 0o600 | 0o60000, os.makedev(7, int(device[9:])))
            run("losetup", "--read-only", device, str(source / (name + ".erofs")))
            loops.append(device)
            run("mount", "-t", "erofs", "-o", "ro", device, str(target))
            mounts.append(target)
        root = private / "rootfs"
        for name in ("upper", "work", "rootfs"):
            (private / name).mkdir(parents=True, exist_ok=True)
        lower = ":".join(str(private / ("lower-" + n)) for n in reversed(layers))
        run("mount", "-t", "overlay", "overlay", "-o",
            f"lowerdir={lower},upperdir={private / 'upper'},workdir={private / 'work'}", str(root))
        mounts.append(root)
        (root / "proc").mkdir(exist_ok=True)
        (root / "dev").mkdir(exist_ok=True)
        (root / "dev/null").touch(exist_ok=True)
        run("mount", "--bind", "/dev/null", str(root / "dev/null"))
        mounts.append(root / "dev/null")
        run("mount", "-t", "proc", "-o", "ro,nosuid,nodev,noexec", "proc", str(root / "proc"))
        mounts.append(root / "proc")
        native = start_native()
        (private / "ready.json").write_text(json.dumps({"state": "RUNNING", "layers":
            list(layers), "lowerdir": lower}) + "\n")
        stop = False
        def requested(*_):
            nonlocal stop
            stop = True
        signal.signal(signal.SIGTERM, requested)
        signal.signal(signal.SIGINT, requested)
        while not stop:
            if native is not None and native.poll() is not None:
                raise RuntimeError("Native agent exited")
            time.sleep(0.2)
    finally:
        stop_native(native)
        (private / "ready.json").unlink(missing_ok=True)
        for target in reversed(mounts):
            try: run("umount", str(target))
            except RuntimeError: pass
        for device in reversed(loops):
            try: run("losetup", "-d", device)
            except RuntimeError: pass


def serve_full(storage):
    if storage not in ("local", "threefs_lazy"):
        raise ValueError("Invalid E2 storage")
    private = Path("/dsec-private")
    data = os.environ.get("DSEC_EROFS_DATA") or (
        "/dsec-local/full.blob" if storage == "local" else
        "/dsec-remote/e2-full-split/full.blob")
    metadata = os.environ.get("DSEC_EROFS_METADATA", "/dsec-meta/full.meta.erofs")
    if (not data.startswith("/dsec-local/" if storage == "local" else "/dsec-remote/") or
            not metadata.startswith("/dsec-meta/")):
        raise ValueError("EROFS source paths must stay inside the mounted artifact roots")
    if storage == "threefs_lazy":
        source = subprocess.check_output(
            ["findmnt", "-T", data, "-n", "-o", "FSTYPE"], text=True).strip()
        if source != "fuse.hf3fs":
            raise RuntimeError("3FS FUSE data source is not mounted")
    mounts = []
    native = None
    try:
        lower = private / "lower-full"
        lower.mkdir(parents=True, exist_ok=True)
        run("python3", "/dsec-mount.py", metadata, data, str(lower))
        mounts.append(lower)
        root = private / "rootfs"
        for name in ("upper", "work", "rootfs"):
            (private / name).mkdir(parents=True, exist_ok=True)
        run("mount", "-t", "overlay", "overlay", "-o",
            f"lowerdir={lower},upperdir={private / 'upper'},workdir={private / 'work'}", str(root))
        mounts.append(root)
        (root / "proc").mkdir(exist_ok=True)
        (root / "dev").mkdir(exist_ok=True)
        (root / "dev/null").touch(exist_ok=True)
        run("mount", "--bind", "/dev/null", str(root / "dev/null"))
        mounts.append(root / "dev/null")
        run("mount", "-t", "proc", "-o", "ro,nosuid,nodev,noexec", "proc", str(root / "proc"))
        mounts.append(root / "proc")
        native = start_native()
        (private / "ready.json").write_text(json.dumps({"state": "RUNNING",
            "layers": ["full"], "storage": storage, "data": data}) + "\n")
        stop = False
        def requested(*_):
            nonlocal stop
            stop = True
        signal.signal(signal.SIGTERM, requested)
        signal.signal(signal.SIGINT, requested)
        while not stop:
            if native is not None and native.poll() is not None:
                raise RuntimeError("Native agent exited")
            time.sleep(0.2)
    finally:
        stop_native(native)
        (private / "ready.json").unlink(missing_ok=True)
        for target in reversed(mounts):
            try: run("umount", str(target))
            except RuntimeError: pass


def shell(command):
    if not (Path("/dsec-private") / "ready.json").exists():
        raise RuntimeError("Sandbox is not ready")
    apply_qos()
    result = subprocess.run(["chroot", "/dsec-private/rootfs", "/bin/sh", "-lc", command],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    os.write(1, result.stdout)
    raise SystemExit(result.returncode)


def _request_path(request_id):
    if not re.fullmatch(r"[0-9a-f]{32}", request_id):
        raise ValueError("Invalid request ID")
    root = Path("/dsec-private/requests")
    root.mkdir(mode=0o700, exist_ok=True)
    return root / (request_id + ".json")


def _save_request(path, record):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(record, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _request_digest(sandbox_id, command, timeout_ms, output_limit):
    payload = {"operation": "execute", "sandbox_id": sandbox_id,
               "args": {"command": command, "timeout_ms": timeout_ms,
                        "output_limit": output_limit}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def request_query(request_id):
    path = _request_path(request_id)
    lock = path.with_suffix(".lock")
    with lock.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "PENDING", "request_id": request_id}
        if not path.exists():
            return {"state": "NOT_FOUND", "request_id": request_id}
        record = json.loads(path.read_text())
        if record["state"] == "PENDING":
            record["state"] = "UNKNOWN"
            _save_request(path, record)
        return record


def request_shell(request_id, command, timeout_ms, output_limit):
    if not (Path("/dsec-private") / "ready.json").exists():
        raise RuntimeError("Sandbox is not ready")
    if not 1 <= timeout_ms <= 30000 or not 1 <= output_limit <= 1048576:
        raise ValueError("Invalid timeout/output limit")
    path = _request_path(request_id)
    sandbox_id = os.environ["DSEC_SANDBOX_ID"]
    digest = _request_digest(sandbox_id, command, timeout_ms, output_limit)
    lock = path.with_suffix(".lock")
    with lock.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "PENDING", "request_id": request_id}
        if path.exists():
            record = json.loads(path.read_text())
            if record["digest"] != digest:
                return {"state": "CONFLICT", "request_id": request_id}
            if record["state"] == "PENDING":
                record["state"] = "UNKNOWN"
                _save_request(path, record)
            return record
        record = {"version": 1, "state": "PENDING", "request_id": request_id,
                  "operation": "execute", "sandbox_id": sandbox_id, "digest": digest}
        _save_request(path, record)
        try:
            apply_qos()
            result = subprocess.run(["chroot", "/dsec-private/rootfs", "/bin/sh", "-lc", command],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    timeout=timeout_ms / 1000)
            output = result.stdout.decode(errors="replace")
            record["state"] = "DONE"
            record["response"] = {"request_id": request_id, "ok": True,
                                  "result": {"exit_code": result.returncode,
                                             "output": output[:output_limit],
                                             "truncated": len(output) > output_limit}}
        except Exception as exc:
            # A timeout or agent failure may leave child side effects.
            record["state"] = "UNKNOWN"
            record["reason"] = str(exc)
        _save_request(path, record)
        return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["serve", "full-serve", "shell", "qos-probe",
                                         "request-shell", "request-query"])
    parser.add_argument("command", nargs="?")
    parser.add_argument("extra", nargs="*")
    args = parser.parse_args()
    if args.mode == "serve":
        serve()
    elif args.mode == "full-serve":
        serve_full(os.environ["DSEC_STORAGE"])
    elif args.mode == "request-shell":
        if args.command is None or len(args.extra) != 3:
            parser.error("request-shell requires ID, timeout, output limit, command")
        request_id = args.command
        timeout_ms, output_limit = map(int, args.extra[:2])
        print(json.dumps(request_shell(request_id, args.extra[2], timeout_ms, output_limit)))
    elif args.mode == "request-query":
        if args.command is None or args.extra:
            parser.error("request-query requires ID")
        print(json.dumps(request_query(args.command)))
    elif args.command is not None:
        shell(args.command)
    elif args.mode == "qos-probe":
        print(json.dumps(apply_qos()))
    else:
        parser.error("shell requires command")
