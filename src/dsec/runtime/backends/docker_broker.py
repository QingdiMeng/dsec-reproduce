"""Bounded Unix-socket bridge for Docker CLI calls from the container backend.

The bridge does not retry commands. A lost response after a mutating Docker
call remains unknown to the caller and is handled by the lifecycle journal.
"""

import argparse
import json
import os
from pathlib import Path
import socket
import socketserver
import struct
import subprocess


MAX_FRAME = 131072
MAX_REPLY = 8 * 1024 * 1024
ALLOWED = {"image", "run", "exec", "inspect", "logs", "stop", "rm", "container"}


class DockerBrokerError(RuntimeError):
    pass


def docker_call(socket_path, argv, timeout):
    if not argv or argv[0] != "docker" or len(argv) < 2:
        raise ValueError("Docker broker requires a docker command")
    request = json.dumps({"argv": argv[1:], "timeout": timeout}).encode() + b"\n"
    if len(request) > MAX_FRAME:
        raise ValueError("Docker broker request is too large")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout + 10)
            connection.connect(str(socket_path))
            connection.sendall(request)
            with connection.makefile("rb") as stream:
                line = stream.readline(MAX_REPLY + 1)
        if len(line) > MAX_REPLY or not line.endswith(b"\n"):
            raise ValueError("Docker broker response is missing or oversized")
        result = json.loads(line)
        if not result.get("ok"):
            if result.get("kind") == "TimeoutExpired":
                raise subprocess.TimeoutExpired(argv, timeout)
            raise DockerBrokerError(result.get("error", "Docker broker failed"))
        return subprocess.CompletedProcess(argv, result["returncode"],
                                           result["stdout"], result["stderr"])
    except (OSError, ValueError, KeyError) as exc:
        raise DockerBrokerError("Docker broker result is unknown; do not retry the command") from exc


def run_docker(argv, *, timeout=30, check=True):
    if not argv or argv[0] != "docker":
        raise ValueError("run_docker requires a Docker command")
    broker = os.environ.get("DSEC_DOCKER_BROKER_SOCKET")
    result = (docker_call(broker, argv, timeout) if broker else
              subprocess.run(argv, capture_output=True, text=True, timeout=timeout))
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, argv,
                                            output=result.stdout, stderr=result.stderr)
    return result


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(180)
        try:
            _, uid, _ = struct.unpack("3i", self.request.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            if uid != self.server.client_uid:
                raise PermissionError("Docker broker peer UID is not authorized")
            line = self.rfile.readline(MAX_FRAME + 1)
            if len(line) > MAX_FRAME or not line.endswith(b"\n"):
                raise ValueError("Invalid Docker broker request")
            request = json.loads(line)
            argv = request["argv"]
            timeout = request["timeout"]
            if (not isinstance(argv, list) or not argv or argv[0] not in ALLOWED or
                    not all(isinstance(value, str) and value and "\x00" not in value
                            for value in argv) or
                    not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or
                    not 0 < timeout <= 180):
                raise ValueError("Unsupported Docker broker command")
            result = subprocess.run(["docker", *argv], capture_output=True,
                                    text=True, timeout=timeout)
            print(f"docker {argv[0]} rc={result.returncode}", flush=True)
            response = {"ok": True, "returncode": result.returncode,
                        "stdout": result.stdout, "stderr": result.stderr}
            encoded = json.dumps(response).encode() + b"\n"
            if len(encoded) > MAX_REPLY:
                raise ValueError("Docker response exceeds broker limit")
        except subprocess.TimeoutExpired as exc:
            encoded = json.dumps({"ok": False, "kind": "TimeoutExpired",
                                  "error": str(exc)}).encode() + b"\n"
        except Exception as exc:
            encoded = json.dumps({"ok": False, "kind": type(exc).__name__,
                                  "error": str(exc)[:1000]}).encode() + b"\n"
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = False
    block_on_close = True
    request_queue_size = 32


def serve(path, client_uid):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        if not path.is_socket():
            raise FileExistsError(path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            try:
                probe.connect(str(path))
            except (OSError, TimeoutError):
                path.unlink()  # A previous broker died without removing its socket.
            else:
                raise FileExistsError(path)
    old_umask = os.umask(0o077)
    try:
        server = Server(str(path), Handler)
    finally:
        os.umask(old_umask)
    os.chmod(path, 0o600)
    server.client_uid = client_uid
    try:
        print("READY " + str(path), flush=True)
        server.serve_forever()
    finally:
        server.server_close()
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--client-uid", type=int, default=os.getuid())
    options = parser.parse_args()
    serve(options.socket, options.client_uid)
