"""Durable at-most-once admission and result lookup for sandboxd RPCs."""

import json
import os
from pathlib import Path
import re
import threading

from dsec._persistence import atomic_json
from dsec.contracts.requests import (MUTATING, RequestConflict, RequestPending,
                                     RequestUncertain, request_digest)


class RequestJournal:
    def __init__(self, root):
        self.root = Path(root).resolve() / "requests"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.RLock()
        # A previous daemon died before committing the response. The command
        # might have run; the request must never be admitted again.
        for path in self.root.glob("*.json"):
            record = json.loads(path.read_text())
            if record.get("version") != 1 or path.stem != record.get("request_id"):
                raise ValueError("Invalid request journal record")
            if record["state"] == "PENDING":
                record["state"] = "UNKNOWN"
                atomic_json(path, record)

    def _path(self, request_id):
        if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise ValueError("request_id must be 32 lowercase hex characters")
        return self.root / (request_id + ".json")

    def begin(self, request):
        request_id = request["request_id"]
        path = self._path(request_id)
        digest = request_digest(request["operation"], request.get("sandbox_id"), request.get("args", {}))
        with self.lock:
            if path.exists():
                saved = json.loads(path.read_text())
                if saved["digest"] != digest:
                    raise RequestConflict("request_id reused with different operation/arguments")
                if saved["state"] == "DONE":
                    return saved["response"]
                if saved["state"] == "PENDING":
                    raise RequestPending("Request is still executing; query its result")
                raise RequestUncertain("Request outcome was not committed; do not replay")
            atomic_json(path, {"version": 1, "request_id": request_id,
                               "operation": request["operation"],
                               "sandbox_id": request.get("sandbox_id"),
                               "digest": digest, "state": "PENDING"})
            return None

    def reject_before_effect(self, request_id, *, operation='create'):
        """Forget a pending intent after a guard proves no effects were admitted."""
        path = self._path(request_id)
        with self.lock:
            record = json.loads(path.read_text())
            if (operation not in MUTATING or record['state'] != 'PENDING' or
                    record['operation'] != operation):
                raise RuntimeError('Only matching pending non-admitted operations can be rejected')
            path.unlink()
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def finish(self, request_id, response):
        path = self._path(request_id)
        with self.lock:
            record = json.loads(path.read_text())
            if record["state"] != "PENDING":
                raise RuntimeError("Request journal is not pending")
            record["state"] = "DONE"
            record["response"] = response
            atomic_json(path, record)

    def lookup(self, request_id):
        path = self._path(request_id)
        with self.lock:
            if not path.exists():
                return {"state": "NOT_FOUND", "request_id": request_id}
            record = json.loads(path.read_text())
            return {"state": record["state"], "request_id": request_id,
                    "operation": record["operation"],
                    "sandbox_id": record["sandbox_id"], "digest": record["digest"],
                    "response": record.get("response")}
