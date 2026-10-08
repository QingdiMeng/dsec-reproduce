"""Existing bounded guest-vsock and Docker command transports, without retries."""
import json
import re
import socket
import subprocess

from dsec.contracts.errors import RequestOutcomeUnknown
from dsec.contracts.execution import ShellRequest, ShellResult


class VsockCommandChannel:
    def __init__(self, endpoint, *, max_timeout_ms=30000, socket_factory=None):
        self.endpoint = endpoint
        self.max_timeout_ms = max_timeout_ms
        self.socket_factory = socket_factory or socket.socket

    def execute(self, request: ShellRequest) -> ShellResult:
        if request.request_id is not None:
            raise ValueError("Guest command protocol has no request journal; use Edge request identity")
        timeout_ms, output_limit = request.timeout_ms, request.output_limit
        data = request.command.encode()
        if not (1 <= timeout_ms <= self.max_timeout_ms and 1 <= output_limit <= 1048576
                and len(data) <= 65536):
            raise ValueError("Request outside protocol limits")
        with self.socket_factory(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout_ms/1000+3)
            sock.connect(str(self.endpoint))
            with sock.makefile("rb") as reader:
                sock.sendall(b"CONNECT 5000\n")
                if not reader.readline(128).startswith(b"OK "):
                    raise EOFError("Vsock connection rejected")
                sock.sendall(f"{timeout_ms} {output_limit} {len(data)}\n".encode()+data)
                header = reader.readline(128)
                if not header:
                    raise EOFError("Command outcome unknown; do not automatically retry")
                code, timedout, truncated, length = map(int, header.split())
                if not 0 <= length <= output_limit:
                    raise ValueError("Invalid response size")
                output = reader.read(length)
                if len(output) != length:
                    raise EOFError("Incomplete command result")
                return {"exit_code":code, "timed_out":bool(timedout), "truncated":bool(truncated),
                        "output":output.decode(errors="replace")}


class DockerCommandChannel:
    def __init__(self, container_name, *, run, error=RuntimeError):
        self.container_name = container_name
        self.run = run
        self.error = error

    def execute(self, request: ShellRequest) -> ShellResult:
        command, timeout_ms, output_limit, request_id = (request.command,
            request.timeout_ms, request.output_limit, request.request_id)
        if request_id is not None:
            if not re.fullmatch(r"[0-9a-f]{32}", request_id):
                raise ValueError("Invalid request ID")
            try:
                response = self.run(["docker", "exec", self.container_name, "python3", "-B", "/dsec-agent.py",
                                 "request-shell", request_id, str(timeout_ms),
                                 str(output_limit), "--", command],
                                timeout=timeout_ms / 1000 + 8, check=False)
            except subprocess.TimeoutExpired as exc:
                raise RequestOutcomeUnknown("Container request result unknown after Docker timeout",
                                            request_id) from exc
            try:
                proof = json.loads(response.stdout)
                if response.returncode or proof["request_id"] != request_id:
                    raise ValueError("Invalid container request response")
            except (ValueError, KeyError) as exc:
                raise RequestOutcomeUnknown("Container request response unavailable: "
                                            + response.stderr[-500:], request_id) from exc
            if proof["state"] == "DONE" and proof["response"]["ok"]:
                return proof["response"]["result"]
            if proof["state"] == "CONFLICT":
                raise ValueError("Container request ID used with different arguments")
            raise RequestOutcomeUnknown("Container request state: " + proof["state"], request_id)
        try:
            result = self.run(["docker", "exec", self.container_name, "python3", "-B", "/dsec-agent.py",
                           "shell", command], timeout=timeout_ms / 1000, check=False)
        except subprocess.TimeoutExpired as exc:
            # docker exec can have executed a non-idempotent action before timeout.
            raise RequestOutcomeUnknown("Container command outcome unknown after timeout") from exc
        if result.stderr:
            raise RequestOutcomeUnknown("Container execution failed with uncertain command outcome: "
                                        + result.stderr[-500:])
        output = result.stdout + result.stderr
        return {"exit_code": result.returncode, "output": output[:output_limit],
                "truncated": len(output) > output_limit}

    def query_request(self, request_id):
        result = self.run(["docker", "exec", self.container_name, "python3", "-B", "/dsec-agent.py",
                       "request-query", request_id], check=False)
        if result.returncode:
            raise self.error("Container request query failed: " + result.stderr[-500:])
        proof = json.loads(result.stdout)
        if proof["request_id"] != request_id:
            raise self.error("Container request ID mismatch")
        return proof
