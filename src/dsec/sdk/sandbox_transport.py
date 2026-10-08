"""Unix-socket client. No automatic retries, including after a lost response."""
import json
import socket
import uuid
import re

class ServiceError(RuntimeError):
    def __init__(self, kind, message):
        super().__init__(message); self.kind=kind

class RequestOutcomeUnknown(RuntimeError):
    def __init__(self, message, request_id=None):
        super().__init__(message)
        self.request_id=request_id

class SandboxClient:
    def __init__(self, socket_path):
        self.socket_path=str(socket_path)
    def call(self, operation, sandbox_id=None, *, request_id=None, **args):
        request_id=request_id or uuid.uuid4().hex
        if not isinstance(request_id,str) or not re.fullmatch(r"[0-9a-f]{32}",request_id):
            raise ValueError("request_id must be 32 lowercase hex characters")
        request={"request_id":request_id,"operation":operation,"sandbox_id":sandbox_id,"args":args}
        data=json.dumps(request).encode()+b"\n"
        if len(data)>131072:
            raise ValueError("Request too large")
        try:
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
                timeout_ms = args.get("timeout_ms", 5000) if operation in ("execute", "container_execute") else 5000
                wait_s = max(60, timeout_ms / 1000 + 30) if isinstance(timeout_ms, (int, float)) else 60
                if operation in ("create", "container_create", "seal_baseline", "pause"):
                    # A cold microVM create includes serialized host netns setup
                    # and root-disk preparation. A lost reply is not retryable.
                    wait_s = max(wait_s, 300)
                sock.settimeout(wait_s); sock.connect(self.socket_path); sock.sendall(data)
                with sock.makefile("rb") as reader:
                    line=reader.readline(8*1024**2+1)
                    if not line.endswith(b"\n") or len(line)>8*1024**2:
                        raise EOFError("Missing or oversized response")
                    response=json.loads(line)
        except (OSError,EOFError,ValueError) as exc:
            raise RequestOutcomeUnknown(f"Request {request_id}: transport failed; not retried",request_id) from exc
        if response.get("request_id") is None and not response.get("ok") and response.get("error",{}).get("type")=="ServiceBusy":
            raise ServiceError("ServiceBusy",response["error"]["message"])
        if response.get("request_id")!=request_id:
            raise RequestOutcomeUnknown("Response ID mismatch",request_id)
        if not response["ok"]:
            if response["error"]["type"] == "RequestOutcomeUnknown":
                raise RequestOutcomeUnknown(response["error"]["message"], request_id)
            raise ServiceError(response["error"]["type"],response["error"]["message"])
        return response["result"]

    def query_request(self, request_id):
        if not isinstance(request_id,str) or not re.fullmatch(r"[0-9a-f]{32}",request_id):
            raise ValueError("request_id must be 32 lowercase hex characters")
        return self.call("query_request",lookup_id=request_id)
