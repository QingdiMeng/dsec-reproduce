"""Versioned native guest operations, independent of rollout frameworks."""
import re

NATIVE_FEATURE = "native-sessions-files-v1"
STREAM_FEATURE = "native-stream-v1"
SESSION_FILE_FEATURE = "native-aether-chronus-v1"
HTTP_FEATURE = "native-http-v1"
REQUIREMENTS_FEATURE = "sandbox-requirements-v1"
EXECUTION_FEATURES = frozenset((NATIVE_FEATURE, STREAM_FEATURE, SESSION_FILE_FEATURE, HTTP_FEATURE))
CHUNK_BYTES = 65536
MAX_FILE_BYTES = 64 * 1024 * 1024
NATIVE_OPERATIONS = frozenset(("native",))
NATIVE_MUTATING = NATIVE_OPERATIONS | {"native_stream", "native_cancel"}
SESSION_ACTIONS = frozenset(("open", "run", "close", "stream", "cancel", "http"))
FILE_ACTIONS = frozenset(("read", "write_begin", "write_chunk", "write_commit", "write_abort"))


def execution_requirements(value):
    """Canonical supported execution requirements; no backend or harness labels.

    API protocol support alone does not establish Node support. Extensions need
    the base native protocol as well. Empty requirements preserve old digests.
    Storage, isolation and snapshot promises need their own attested contracts;
    they cannot be asserted with arbitrary capability strings here.
    """
    if not isinstance(value, (list, tuple)) or len(value) > len(EXECUTION_FEATURES):
        raise ValueError("Execution requirements must be a bounded list or tuple")
    if any(not isinstance(item, str) or item not in EXECUTION_FEATURES for item in value):
        raise ValueError("Unsupported execution capability requirement")
    result = set(value)
    if result:
        result.add(NATIVE_FEATURE)
    return tuple(sorted(result))


def required_extension(action, args):
    """Never allow old endpoints to silently drop new operation semantics."""
    if action == "http":
        return HTTP_FEATURE
    if action in FILE_ACTIONS and args.get("session_id") is not None:
        return SESSION_FILE_FEATURE
    return None


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("Expected a 32-character hexadecimal ID")
    return value


def file_path(value):
    if (not isinstance(value, str) or not value.startswith("/") or "\x00" in value
            or len(value.encode()) > 4096):
        raise ValueError("Expected an absolute guest path of at most 4096 bytes")
    return value
