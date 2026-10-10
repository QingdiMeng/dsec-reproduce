"""Versioned native guest operations, independent of rollout frameworks."""
import re

NATIVE_FEATURE = "native-sessions-files-v1"
STREAM_FEATURE = "native-stream-v1"
CHUNK_BYTES = 65536
MAX_FILE_BYTES = 64 * 1024 * 1024
NATIVE_OPERATIONS = frozenset(("native",))
NATIVE_MUTATING = NATIVE_OPERATIONS | {"native_stream", "native_cancel"}
SESSION_ACTIONS = frozenset(("open", "run", "close", "stream", "cancel"))
FILE_ACTIONS = frozenset(("read", "write_begin", "write_chunk", "write_commit", "write_abort"))


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("Expected a 32-character hexadecimal ID")
    return value


def file_path(value):
    if (not isinstance(value, str) or not value.startswith("/") or "\x00" in value
            or len(value.encode()) > 4096):
        raise ValueError("Expected an absolute guest path of at most 4096 bytes")
    return value
