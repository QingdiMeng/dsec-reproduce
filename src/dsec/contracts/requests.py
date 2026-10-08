"""Existing v0.1 request identity and conflict semantics; no host operations."""
import hashlib
import json


MUTATING = frozenset(("create", "seal_baseline", "prewarm", "execute", "pause", "resume", "recover", "stop"))


class RequestConflict(ValueError):
    pass


class RequestPending(RuntimeError):
    pass


class RequestUncertain(RuntimeError):
    pass


def request_digest(operation, sandbox_id, args):
    payload = {"operation": operation, "sandbox_id": sandbox_id, "args": args}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()
