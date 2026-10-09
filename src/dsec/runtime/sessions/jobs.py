"""Bounded stream collection using the existing Edge request journal.

Subscribers page through an append-only spool. They do not own or block the
command, and reconnecting never creates another execution. The journal remains
the sole authority for PENDING/DONE/UNKNOWN across Edge restarts.
"""
import base64
from contextlib import ExitStack
import json
import os
import threading
import time

from dsec.contracts.errors import CommandOutcomeUnknown, ServiceBusy
from dsec.contracts.requests import RequestConflict, RequestUncertain, request_digest
from dsec.runtime.sessions.native import NativeSessionReset
from dsec.runtime.sessions.service import native_operation


class NativeJobs:
    def __init__(self, server):
        self.server = server
        self.journal = server.journal
        self.lock = threading.RLock()
        self.slots = threading.BoundedSemaphore(server.max_requests)
        self.active = {}
        self.closed = False

    def _trace(self, operation_id):
        return self.journal._path(operation_id).with_suffix(".events")

    def start(self, request):
        operation_id = request["request_id"]
        if request["args"].get("action") != "stream":
            raise ValueError("native_stream requires action=stream")
        with self.lock:
            if self.closed:
                raise ServiceBusy("Native stream service is closing; not admitted")
            proof = self.journal.lookup(operation_id)
            if proof["state"] != "NOT_FOUND":
                digest = request_digest(request["operation"], request.get("sandbox_id"), request["args"])
                if proof["digest"] != digest:
                    raise RequestConflict("Stream ID reused with different arguments")
                if proof["state"] == "UNKNOWN":
                    raise RequestUncertain("Stream outcome unknown; do not replay")
                return {"operation_id": operation_id, "state": proof["state"]}
            if not self.slots.acquire(blocking=False):
                raise ServiceBusy("Native execution capacity reached; not admitted")
            stack = ExitStack()
            writer = None
            foreground = False
            admitted = False
            try:
                self.journal.begin(request)
                admitted = True
                channel, values = stack.enter_context(native_operation(self.server,
                    request["sandbox_id"], request["args"], operation_id))
                writer = self._trace(operation_id).open("xb")
                job = dict(lock=threading.Lock(), bytes=0, seq=0, stream_truncated=False)
                self.server.manager.foreground_enter()
                foreground = True
                thread = threading.Thread(target=self._collect,
                    args=(request, channel, values, writer, stack, job),
                    name=f"native-{operation_id[:8]}", daemon=False)
                job["thread"] = thread
                self.active[operation_id] = job
                thread.start()
            except BaseException as exc:
                self.active.pop(operation_id, None)
                try:
                    if writer is not None:
                        writer.close()
                finally:
                    try:
                        stack.close()
                    finally:
                        if foreground:
                            self.server.manager.foreground_exit()
                        self.slots.release()
                if admitted:
                    if isinstance(exc, ServiceBusy):
                        self.journal.reject_before_effect(operation_id, operation="native_stream")
                    else:
                        self.journal.finish(operation_id, {"request_id": operation_id, "ok": False,
                            "error": {"type": type(exc).__name__, "message": str(exc)}})
                raise
            return {"operation_id": operation_id, "state": "PENDING"}

    def _append(self, writer, job, event):
        event = dict(event, seq=job["seq"])
        if event["type"] in ("stdout", "stderr"):
            event["data"] = base64.b64encode(event["data"]).decode()
        line = json.dumps(event, separators=(",", ":")).encode() + b"\n"
        # Reserve space for the terminal event; keep draining the guest even
        # when small writes exceed the bounded event-spool budget.
        if event["type"] != "result" and job["bytes"] + len(line) > 8*1024**2 - 16384:
            job["stream_truncated"] = True
            return
        with job["lock"]:
            try:
                writer.write(line)
                writer.flush()
                if event["type"] == "result":
                    os.fsync(writer.fileno())
            except OSError as exc:
                raise CommandOutcomeUnknown("Stream evidence could not be committed") from exc
            job["seq"] += 1
            job["bytes"] += len(line)

    def _collect(self, request, channel, values, writer, stack, job):
        operation_id = request["request_id"]
        completed = False
        try:
            with stack:
                queue_start = time.monotonic()
                queue_wait_ms = 0
                # Only an explicit guest EBUSY proves non-admission. A lost
                # stream/unknown result is never retried here.
                def admitted_events():
                    nonlocal queue_wait_ms
                    while True:
                        try:
                            yield from channel.stream(**values)
                            return
                        except ServiceBusy:
                            if time.monotonic() - queue_start >= 35:
                                raise
                            time.sleep(.05)
                            queue_wait_ms = int((time.monotonic() - queue_start) * 1000)
                for event in admitted_events():
                    if event["type"] == "result":
                        event["result"]["queue_wait_ms"] = queue_wait_ms
                        event["result"]["stream_truncated"] = job["stream_truncated"]
                        self._append(writer, job, event)
                        try:
                            self.journal.finish(operation_id, {"request_id": operation_id,
                                "ok": True, "result": event["result"]})
                        except OSError as exc:
                            raise CommandOutcomeUnknown("Stream result commit failed") from exc
                        completed = True
                    else:
                        try:
                            self._append(writer, job, event)
                        except OSError as exc:
                            raise CommandOutcomeUnknown("Stream evidence storage failed") from exc
                if not completed:
                    raise CommandOutcomeUnknown("Stream ended without a result")
        except Exception as exc:
            try:
                if isinstance(exc, CommandOutcomeUnknown) or completed:
                    self.journal.unknown(operation_id)
                else:
                    self.journal.finish(operation_id, {"request_id": operation_id, "ok": False,
                        "error": {"type": type(exc).__name__, "message": str(exc)}})
            except Exception as journal_error:
                # Leave the original PENDING intent authoritative if storage
                # cannot even persist UNKNOWN; restart converts it to UNKNOWN.
                self.server.manager.errors.append({"component": "native_stream",
                    "request_id": operation_id, "error": str(journal_error)})
        finally:
            try:
                writer.close()
            except OSError as exc:
                self.server.manager.errors.append({"component": "native_stream_close",
                    "request_id": operation_id, "error": str(exc)})
            finally:
                self.server.manager.foreground_exit()
                self.slots.release()
                with self.lock:
                    self.active.pop(operation_id, None)

    def events(self, sandbox_id, operation_id, cursor=0, session_id=None):
        if type(cursor) is not int or cursor < 0:
            raise ValueError("Invalid stream cursor")
        proof = self.journal.lookup(operation_id)
        if proof["state"] == "NOT_FOUND" or proof.get("sandbox_id") != sandbox_id:
            raise ValueError("Unknown stream for this sandbox")
        if proof.get("operation") != "native_stream":
            raise ValueError("Request is not a stream")
        if session_id is not None and proof["args"]["session_id"] != session_id:
            raise ValueError("Stream belongs to another session")
        with self.lock:
            job = self.active.get(operation_id)
        lock = job["lock"] if job else threading.Lock()
        events = []
        with lock:
            path = self._trace(operation_id)
            if path.exists():
                with path.open("rb") as reader:
                    if cursor > path.stat().st_size:
                        raise ValueError("Stream cursor exceeds captured evidence")
                    reader.seek(cursor)
                    for _ in range(16):
                        before = reader.tell()
                        line = reader.readline(8193)
                        if not line:
                            break
                        if len(line) > 8192 or not line.endswith(b"\n"):
                            reader.seek(before)
                            break
                        event = json.loads(line)
                        if event["type"] == "result" and proof["state"] != "DONE":
                            reader.seek(before)
                            break
                        events.append(dict(event, cursor=reader.tell()))
                    cursor = reader.tell()
        proof = self.journal.lookup(operation_id)
        return dict(events=events, cursor=cursor, state=proof["state"], response=proof.get("response"),
                    drained=not path.exists() or cursor == path.stat().st_size)

    def cancel(self, sandbox_id, args, request_id):
        proof = self.journal.lookup(args["lookup_id"])
        if (proof.get("sandbox_id") != sandbox_id or proof.get("operation") not in ("native_stream", "native")
                or proof.get("args", {}).get("action") not in ("run", "stream")
                or proof["args"].get("session_id") != args["session_id"]):
            raise ValueError("Unknown command for this session")
        if proof["state"] == "UNKNOWN":
            raise RequestUncertain("Command outcome unknown; reconcile before cancellation")
        if proof["state"] != "PENDING":
            return {"cancel_requested": False, "state": proof["state"]}
        values = {**proof["args"], "action": "cancel", "operation_id": args["lookup_id"]}
        values.pop("command", None)
        values.pop("timeout_ms", None)
        values.pop("output_limit", None)
        try:
            with native_operation(self.server, sandbox_id, values, request_id) as (channel, native):
                channel.call("cancel", **native)
        except (FileNotFoundError, NativeSessionReset):
            return {"cancel_requested": False, "state": self.journal.lookup(args["lookup_id"])["state"]}
        return {"cancel_requested": True, "state": "PENDING"}

    def close(self):
        with self.lock:
            self.closed = True
            threads = [job["thread"] for job in self.active.values()]
        for thread in threads:
            thread.join()
