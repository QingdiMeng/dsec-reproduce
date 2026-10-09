"""Framework-independent sessions and bounded binary file transfers."""
import base64
import hashlib
import uuid
import asyncio
from dsec.sdk.sandbox_transport import ServiceError

from dsec.contracts.native import (CHUNK_BYTES, MAX_FILE_BYTES, NATIVE_FEATURE, STREAM_FEATURE,
                                   identifier, file_path)
from dsec.contracts.sandbox import UnsupportedCapability


def _part_id(operation_id, part):
    return hashlib.sha256(f"native-v1:{operation_id}:{part}".encode()).hexdigest()[:32]


class NativeSandboxMixin:
    async def _native(self, action, *, request_id=None, _operation="native", **args):
        if NATIVE_FEATURE not in getattr(self, "_features", ()):
            raise UnsupportedCapability("Upgrade Edge for native sessions and files")
        backend = "container" if self.backend in ("container", "tb2") else "microvm"
        if backend == "container":
            from dataclasses import asdict
            args.update(spec=asdict(self._run_args), kind="tb2" if self.backend == "tb2" else "container")
        # The transport sends one stable intent. Unknown outcomes are queried,
        # never replayed automatically by a transfer/session wrapper.
        operation_id = identifier(request_id or uuid.uuid4().hex)
        for attempt in range(35):
            try:
                return await asyncio.to_thread(self._transport.call, _operation, self.id,
                    request_id=operation_id, backend=backend, action=action, **args)
            except ServiceError as exc:
                if exc.kind != "ServiceBusy" or attempt == 34:
                    raise
                await asyncio.sleep(min(.05 * 2 ** attempt, 1.0))

    async def open_session(self, *, request_id=None):
        operation_id = identifier(request_id or uuid.uuid4().hex)
        result = await self._native("open", session_id=operation_id, request_id=operation_id)
        return DSecSession(self, result["session_id"])

    def attach_session(self, session_id):
        """Reconstruct a handle; availability is checked by the next operation."""
        return DSecSession(self, identifier(session_id))

    async def read_file(self, path, *, max_bytes=MAX_FILE_BYTES, request_id=None):
        path = file_path(path)
        if type(max_bytes) is not int or not 0 <= max_bytes <= MAX_FILE_BYTES:
            raise ValueError("Invalid file read budget")
        operation_id = identifier(request_id or uuid.uuid4().hex)
        chunks, offset, version = [], 0, None
        while True:
            result = await self._native("read", path=path, transfer_id=operation_id,
                offset=offset, length=min(CHUNK_BYTES, max_bytes - offset + 1),
                request_id=_part_id(operation_id, f"read:{offset}"))
            chunk = base64.b64decode(result["data"], validate=True)
            if version is not None and result["version"] != version:
                raise RuntimeError("Guest file changed during chunked read")
            version = result["version"]
            if offset + len(chunk) > max_bytes:
                raise ValueError("File exceeds read budget")
            chunks.append(chunk)
            offset += len(chunk)
            if result["eof"]:
                return b"".join(chunks)

    async def write_file(self, path, data, *, mode=0o600, request_id=None):
        path = file_path(path)
        if not isinstance(data, bytes) or len(data) > MAX_FILE_BYTES:
            raise ValueError("File data must be bytes within the 64 MiB transfer budget")
        if type(mode) is not int or not 0 <= mode <= 0o777:
            raise ValueError("Invalid file mode")
        operation_id = identifier(request_id or uuid.uuid4().hex)
        args = dict(path=path, total=len(data), mode=mode, transfer_id=operation_id)
        await self._native("write_begin", **args, sha256=hashlib.sha256(data).hexdigest(),
                           request_id=_part_id(operation_id, "begin"))
        for offset in range(0, len(data), CHUNK_BYTES):
            await self._native("write_chunk", **args, offset=offset,
                data=base64.b64encode(data[offset:offset + CHUNK_BYTES]).decode(),
                request_id=_part_id(operation_id, f"chunk:{offset}"))
        return await self._native("write_commit", **args, request_id=operation_id)

    async def abort_file_write(self, path, transfer_id, *, request_id=None):
        """Remove an incomplete transfer explicitly, without replaying its writes."""
        return await self._native("write_abort", path=file_path(path),
            transfer_id=identifier(transfer_id), request_id=request_id)


class DSecSession:
    def __init__(self, sandbox, session_id):
        self.sandbox = sandbox
        self.id = session_id
        self._run_lock = asyncio.Lock()

    async def run_shell(self, command, *, timeout_ms=5000, output_limit=65536, request_id=None):
        async with self._run_lock:
            return await self.sandbox._native("run", session_id=self.id, command=command,
                timeout_ms=timeout_ms, output_limit=output_limit, request_id=request_id)

    async def close(self, *, request_id=None):
        try:
            return await self.sandbox._native("close", session_id=self.id, request_id=request_id)
        except ServiceError as exc:
            if exc.kind != "NativeSessionReset":
                raise
            return {"ok": True, "already_closed": True}

    async def cancel(self, operation_id, *, request_id=None):
        """Cancel this exact command; a completed ID cannot kill later work."""
        return await self.sandbox._native("cancel", _operation="native_cancel",
            lookup_id=identifier(operation_id), session_id=self.id, request_id=request_id)

    async def query(self, operation_id):
        proof = await asyncio.to_thread(self.sandbox._transport.query_request, identifier(operation_id))
        if (proof["state"] != "NOT_FOUND" and (proof.get("sandbox_id") != self.sandbox.id
                or proof.get("args", {}).get("session_id") != self.id)):
            raise ValueError("Request belongs to another session")
        return proof

    async def stream(self, command, *, timeout_ms=5000, output_limit=65536, request_id=None):
        if STREAM_FEATURE not in getattr(self.sandbox, "_features", ()):
            raise UnsupportedCapability("Upgrade Edge for native streaming")
        operation_id = identifier(request_id or uuid.uuid4().hex)
        async with self._run_lock:
            await self.sandbox._native("stream", _operation="native_stream", session_id=self.id,
                command=command, timeout_ms=timeout_ms, output_limit=output_limit,
                request_id=operation_id)
            async for event in self.events(operation_id):
                yield event

    async def events(self, operation_id, *, cursor=0):
        """Reattach to captured events without submitting a command again.

        stdout/stderr data is bytes. Save each event's cursor for reconnection.
        Closing this iterator detaches the subscriber; use cancel() to cancel.
        """
        from dsec.contracts.errors import RequestOutcomeUnknown
        operation_id = identifier(operation_id)
        saw_result = False
        while True:
            page = await self.sandbox._native("events", _operation="native_events",
                lookup_id=operation_id, cursor=cursor, session_id=self.id)
            for event in page["events"]:
                if event["type"] in ("stdout", "stderr"):
                    event["data"] = base64.b64decode(event["data"], validate=True)
                saw_result |= event["type"] == "result"
                yield dict(event, operation_id=operation_id)
            cursor = page["cursor"]
            if page["state"] == "UNKNOWN":
                raise RequestOutcomeUnknown("Native stream outcome unknown; do not replay", operation_id)
            if page["state"] == "DONE" and page["drained"]:
                response = page["response"]
                if not response["ok"]:
                    raise ServiceError(response["error"]["type"], response["error"]["message"])
                if not saw_result:
                    yield dict(type="result", result=response["result"], operation_id=operation_id)
                return
            await asyncio.sleep(.05 if page["events"] else .1)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()
