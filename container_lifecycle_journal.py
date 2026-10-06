"""Host-side request journal for E1/E2 container create and stop."""

import fcntl
import json
from pathlib import Path
import re

from request_journal import atomic_json, request_digest
from admission_guard import check_container_create
from sandbox_client import RequestOutcomeUnknown


class ContainerLifecycleJournal:
    def __init__(self, root):
        self.root = Path(root).resolve() / "lifecycle-requests"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _path(self, request_id):
        if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise ValueError("request_id must be 32 lowercase hex characters")
        return self.root / (request_id + ".json")

    def _lookup_locked(self, path, request_id):
        if not path.exists():
            return {"state": "NOT_FOUND", "request_id": request_id}
        record = json.loads(path.read_text())
        if record["state"] == "PENDING":
            # An unlocked PENDING record survived a process failure. Its side
            # effect may have happened, so never admit the same request again.
            record["state"] = "UNKNOWN"
            atomic_json(path, record)
        return record

    def lookup(self, request_id):
        path = self._path(request_id)
        with path.with_suffix(".lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"state": "PENDING", "request_id": request_id}
            return self._lookup_locked(path, request_id)

    def recover(self, request_id, verifier):
        """Commit a proven terminal result for a previously uncertain request."""
        path = self._path(request_id)
        with path.with_suffix(".lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"state": "PENDING", "request_id": request_id}
            record = self._lookup_locked(path, request_id)
            if record["state"] != "UNKNOWN" or not isinstance(record.get("args"), dict):
                return record
            result = verifier(record)
            if result is None:
                return record
            record["state"] = "DONE"
            record["response"] = {"request_id": request_id, "ok": True, "result": result}
            record["recovered_from_unknown"] = True
            atomic_json(path, record)
            return record

    def execute(self, request_id, operation, sandbox_id, args, effect):
        if operation not in ("create", "stop"):
            raise ValueError("Unsupported container lifecycle operation")
        path = self._path(request_id)
        digest = request_digest(operation, sandbox_id, args)
        with path.with_suffix(".lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RequestOutcomeUnknown("Container lifecycle request is pending", request_id) from exc
            saved = self._lookup_locked(path, request_id)
            if saved["state"] != "NOT_FOUND":
                if saved["digest"] != digest:
                    raise ValueError("Container request ID used with different operation/arguments")
                if saved["state"] == "DONE":
                    return saved["response"]["result"]
                raise RequestOutcomeUnknown("Container lifecycle result is unknown", request_id)
            if operation == "create":
                check_container_create(self.root.parent, request_id, args)
            record = {"version": 1, "state": "PENDING", "request_id": request_id,
                      "operation": operation, "sandbox_id": sandbox_id,
                      "args": args, "digest": digest}
            atomic_json(path, record)
            try:
                result = effect()
            except BaseException:
                # The effect may have completed before a Docker/transport error.
                record["state"] = "UNKNOWN"
                atomic_json(path, record)
                raise
            record["state"] = "DONE"
            record["response"] = {"request_id": request_id, "ok": True, "result": result}
            atomic_json(path, record)
            return result
