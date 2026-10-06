"""Small trainer-side client for the rollout worker prototype."""

import json
import socket


class RolloutServiceError(RuntimeError):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


class RolloutOutcomeUnknown(RuntimeError):
    """Connection failed after submission; query by rollout/step before retrying."""


class RolloutClient:
    def __init__(self, socket_path, timeout_s=60):
        self.socket_path = str(socket_path)
        self.timeout_s = timeout_s

    def call(self, operation, *, timeout_s=None, **args):
        request = json.dumps({"operation": operation, "args": args}).encode() + b"\n"
        if len(request) > 131072:
            raise ValueError("Request too large")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(self.timeout_s if timeout_s is None else timeout_s)
                conn.connect(self.socket_path)
                conn.sendall(request)
                with conn.makefile("rb") as stream:
                    line = stream.readline(8 * 1024 * 1024 + 1)
        except (OSError, EOFError) as exc:
            raise RolloutOutcomeUnknown("Worker request outcome unknown; query status") from exc
        if not line.endswith(b"\n") or len(line) > 8 * 1024 * 1024:
            raise RolloutOutcomeUnknown("Worker response missing or too large; query status")
        response = json.loads(line)
        if not response["ok"]:
            error = response["error"]
            raise RolloutServiceError(error["type"], error["message"])
        return response["result"]
