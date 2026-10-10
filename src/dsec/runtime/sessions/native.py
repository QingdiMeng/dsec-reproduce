"""Bounded native binary channel; no shell emulation or execution retries."""
import base64
import errno
import socket
import time

from dsec.contracts.errors import CommandOutcomeUnknown, ServiceBusy
from dsec.contracts.native import (CHUNK_BYTES, MAX_FILE_BYTES, NATIVE_FEATURE, STREAM_FEATURE,
                                   identifier, file_path)
from dsec.contracts.sandbox import UnsupportedCapability


class NativeSessionReset(RuntimeError):
    pass


class NativeChannel:
    def __init__(self, endpoint, *, vsock=False, max_timeout_ms=30000):
        self.endpoint = str(endpoint)
        self.vsock = vsock
        self.max_timeout_ms = max_timeout_ms

    @staticmethod
    def _read(stream, length):
        result = bytearray()
        while len(result) < length:
            chunk = stream.read(length - len(result))
            if not chunk:
                raise EOFError("Incomplete native response")
            result.extend(chunk)
        return bytes(result)

    def _exchange(self, header, body=b"", *, timeout_ms=5000):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout_ms / 1000 + 5)
            connection.connect(self.endpoint)
            with connection.makefile("rb") as stream:
                if self.vsock:
                    connection.sendall(b"CONNECT 5001\n")
                    if not stream.readline(128).startswith(b"OK "):
                        raise UnsupportedCapability("Guest has no native v1 channel")
                connection.sendall(header + body)
                fields = stream.readline(128).split()
                if len(fields) != 9 or fields[0] != b"DSEC1":
                    raise ValueError("Invalid native response header")
                error, code, timed_out, truncated, reset, cancelled, no, ne = map(int, fields[1:])
                if (error < 0 or any(x not in (0, 1) for x in (timed_out, truncated, reset, cancelled))
                        or no < 0 or ne < 0 or no + ne > 1048576):
                    raise ValueError("Invalid native response bounds")
                out, err = self._read(stream, no), self._read(stream, ne)
                if error:
                    if code == -2:
                        raise CommandOutcomeUnknown("Guest file rename completed but durability was not confirmed")
                    if error == errno.EBUSY:
                        raise ServiceBusy("Guest operation rejected before effects")
                    if reset:
                        raise NativeSessionReset("Native shell session is no longer available")
                    exc = OSError(error, "Native guest operation failed")
                    exc.native_result = True
                    raise exc
                return dict(exit_code=code if code >= 0 else None, timed_out=bool(timed_out),
                            truncated=bool(truncated), session_reset=bool(reset),
                            cancelled=bool(cancelled),
                            stdout_bytes=out, stderr_bytes=err)

    def capabilities(self):
        try:
            result = self._exchange(b"CAP\n")
        except (OSError, EOFError, ValueError) as exc:
            raise UnsupportedCapability("Upgrade guest for native sessions and files") from exc
        features = set(result["stdout_bytes"].decode().split())
        if NATIVE_FEATURE not in features:
            raise UnsupportedCapability("Native guest capability mismatch")
        return features

    def prepare(self, action, **args):
        if action in ("open", "close", "run", "stream", "cancel"):
            sid = identifier(args["session_id"])
            if action in ("run", "stream"):
                command = args["command"]
                if not isinstance(command, str) or "\x00" in command:
                    raise ValueError("Command must be text without NUL")
                payload = command.encode()
                timeout, limit = args.get("timeout_ms", 5000), args.get("output_limit", 65536)
                if (type(timeout) is not int or not 1 <= timeout <= self.max_timeout_ms
                        or type(limit) is not int or not 1 <= limit <= 1048576
                        or len(payload) > CHUNK_BYTES):
                    raise ValueError("Invalid native command limits")
                operation_id = identifier(args.get("operation_id", "0" * 32))
                header = f"{'STREAM' if action == 'stream' else 'RUN'} {sid} {timeout} {limit} {len(payload)} {operation_id}\n".encode()
            elif action == "cancel":
                payload, timeout = b"", 5000
                header = f"CANCEL {sid} {identifier(args['operation_id'])}\n".encode()
            else:
                payload, timeout = b"", 5000
                header = f"{action.upper()} {sid}\n".encode()
        else:
            op = {"read": "READ", "write_begin": "WBEGIN", "write_chunk": "WCHUNK",
                  "write_commit": "WCOMMIT", "write_abort": "WABORT"}.get(action)
            if op is None:
                raise ValueError("Unknown native action")
            transfer = identifier(args["transfer_id"])
            path = file_path(args["path"]).encode()
            offset, total = args.get("offset", 0), args.get("total", 0)
            mode = args.get("mode", 0o600)
            length = args.get("length", CHUNK_BYTES if action == "read" else 0)
            if action == "write_chunk":
                data = base64.b64decode(args["data"], validate=True)
                length = len(data)
            else:
                data = b""
            if (any(type(n) is not int for n in (offset, total, mode, length))
                    or not 0 <= offset <= MAX_FILE_BYTES or not 0 <= total <= MAX_FILE_BYTES
                    or not 0 <= mode <= 0o777 or not 0 <= length <= CHUNK_BYTES):
                raise ValueError("Invalid native file limits")
            header = f"{op} {transfer} {offset} {total} {length} {len(path)} {mode}\n".encode()
            payload, timeout = path + data, 5000
        return header, payload, timeout

    def call(self, action, **args):
        header, payload, timeout = self.prepare(action, **args)
        try:
            result = self._exchange(header, payload, timeout_ms=timeout)
        except OSError as exc:
            if getattr(exc, "native_result", False):
                raise
            raise CommandOutcomeUnknown("Native operation reply lost; query Edge request ID") from exc
        except (EOFError, ValueError) as exc:
            raise CommandOutcomeUnknown("Native operation reply lost; query Edge request ID") from exc
        if action == "read":
            data = result["stdout_bytes"]
            if len(data) < 40:
                raise CommandOutcomeUnknown("Missing native file version")
            return {"data": base64.b64encode(data[40:]).decode(), "version": data[:40].hex(),
                    "eof": len(data) - 40 < args.get("length", CHUNK_BYTES)}
        if action == "run":
            out, err = result.pop("stdout_bytes"), result.pop("stderr_bytes")
            return dict(result, stdout=out.decode(errors="replace"), stderr=err.decode(errors="replace"))
        return {"session_id": args["session_id"]} if action == "open" else {"ok": True}

    def stream(self, **args):
        header, body, timeout = self.prepare("stream", **args)
        deadline = time.monotonic() + timeout / 1000 + 5
        captured = 0
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(timeout / 1000 + 5)
                connection.connect(self.endpoint)
                with connection.makefile("rb") as reader:
                    if self.vsock:
                        connection.sendall(b"CONNECT 5001\n")
                        if not reader.readline(128).startswith(b"OK "):
                            raise UnsupportedCapability("Guest has no native stream channel")
                    connection.sendall(header + body)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("Native stream exceeded transport deadline")
                        connection.settimeout(remaining)
                        fields = reader.readline(128).split()
                        if len(fields) == 3 and fields[0] == b"DSECE":
                            kind, length = fields[1].decode(), int(fields[2])
                            if kind not in ("stdout", "stderr") or not 1 <= length <= 4096:
                                raise ValueError("Invalid stream event bounds")
                            captured += length
                            if captured > args.get("output_limit", 65536):
                                raise ValueError("Native stream exceeded output budget")
                            yield {"type": kind, "data": self._read(reader, length)}
                        elif len(fields) == 9 and fields[0] == b"DSEC1":
                            error, code, timedout, truncated, reset, cancelled, no, ne = map(int, fields[1:])
                            if error == errno.EBUSY:
                                if captured:
                                    raise CommandOutcomeUnknown("Guest reported busy after producing output")
                                raise ServiceBusy("Guest stream not admitted")
                            if error:
                                if reset:
                                    raise NativeSessionReset("Native session is no longer available")
                                exc = OSError(error, "Guest stream failed")
                                exc.native_result = True
                                raise exc
                            if (no or ne or any(n not in (0, 1) for n in (timedout, truncated, reset, cancelled))):
                                raise ValueError("Invalid terminal stream result")
                            yield {"type": "result", "result": dict(exit_code=code if code>=0 else None,
                                timed_out=bool(timedout), truncated=bool(truncated),
                                session_reset=bool(reset), cancelled=bool(cancelled))}
                            return
                        else:
                            raise EOFError("Native stream lost its terminal result")
        except OSError as exc:
            if getattr(exc, "native_result", False):
                raise
            raise CommandOutcomeUnknown("Native stream reply lost; do not replay") from exc
        except (EOFError, ValueError) as exc:
            raise CommandOutcomeUnknown("Native stream reply lost; do not replay") from exc
