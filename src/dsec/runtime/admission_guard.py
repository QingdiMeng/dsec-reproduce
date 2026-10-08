"""Fail-closed create admission at the sandbox daemon boundary."""

import json
from pathlib import Path
import socket

from dsec.contracts.requests import request_digest


class AdmissionDenied(RuntimeError):
    pass


def _check(worker_socket, backend, request_id, args):
    if not worker_socket:
        return
    request = {"operation": "admission_check", "args": {
        "backend": backend,
        "request_id": request_id,
        "digest": request_digest("create", None, args)}}
    data = json.dumps(request, separators=(",", ":")).encode() + b"\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(str(worker_socket))
            connection.sendall(data)
            with connection.makefile("rb") as stream:
                line = stream.readline(4097)
        if not line.endswith(b"\n") or len(line) > 4096:
            raise ValueError("Invalid admission response")
        response = json.loads(line)
        if response.get("ok") is not True or response.get("result") != {"admitted": True}:
            raise ValueError("No matching scheduler lease")
    except (OSError, ValueError, KeyError) as exc:
        raise AdmissionDenied(f"{backend} create requires an active matching scheduler lease") from exc


def check_create(worker_socket, request_id, args):
    _check(worker_socket, "microvm", request_id, args)


def container_worker_socket(root):
    config = Path(root) / ".admission-worker.json"
    if not config.exists() and not config.is_symlink():
        return None
    try:
        if config.is_symlink():
            raise ValueError("Container admission config must be a regular file")
        value = json.loads(config.read_text())
        if (set(value) != {"version", "worker_socket"} or value["version"] != 1 or
                not isinstance(value["worker_socket"], str) or
                not value["worker_socket"].startswith("/")):
            raise ValueError("Invalid container admission config")
        return value["worker_socket"]
    except (OSError, ValueError, TypeError) as exc:
        raise AdmissionDenied("Container admission configuration is invalid") from exc


def check_container_create(root, request_id, args, *, worker_socket=None):
    legacy_socket = container_worker_socket(root)
    if worker_socket and legacy_socket and worker_socket != legacy_socket:
        raise AdmissionDenied("Container admission configuration disagrees with Edge")
    worker_socket = worker_socket or legacy_socket
    if worker_socket:
        _check(worker_socket, "container", request_id, args)
