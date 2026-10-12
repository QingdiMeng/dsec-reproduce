"""Private, bounded QMP control. No caller-supplied monitor commands or retries."""
import json
import socket
import time
import uuid


class QMPError(RuntimeError):
    pass


class QMP:
    COMMANDS = frozenset(('query-status', 'query-kvm', 'stop', 'cont', 'quit'))
    MAX_FRAME = 1024 * 1024
    MAX_EVENTS = 128

    def __init__(self, endpoint, *, timeout=5):
        self.endpoint, self.timeout = str(endpoint), timeout

    def execute(self, command):
        if command not in self.COMMANDS:
            raise ValueError('Unsupported private QMP command')
        deadline = time.monotonic() + self.timeout
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            def remaining():
                value = deadline - time.monotonic()
                if value <= 0:
                    raise TimeoutError('QMP deadline exceeded; do not replay control mutation')
                connection.settimeout(value)
            remaining()
            connection.connect(self.endpoint)
            # A file.readline timeout is per recv, so a byte-at-a-time sender
            # could extend the deadline indefinitely. Check it on every recv.
            pending = bytearray()
            def read():
                while b'\n' not in pending:
                    if len(pending) >= self.MAX_FRAME:
                        raise QMPError('Oversized QMP frame; outcome may be unknown')
                    remaining()
                    chunk = connection.recv(min(65536, self.MAX_FRAME - len(pending)))
                    if not chunk:
                        raise QMPError('Incomplete QMP frame; outcome may be unknown')
                    pending.extend(chunk)
                end = pending.index(b'\n') + 1
                raw = bytes(pending[:end]); del pending[:end]
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise QMPError('Invalid QMP object')
                return value
            greeting = read()
            if not isinstance(greeting.get('QMP'), dict):
                raise QMPError('QMP greeting required')
            def exchange(name):
                request_id = uuid.uuid4().hex
                remaining()
                connection.sendall(json.dumps({'execute':name, 'id':request_id}).encode() + b'\n')
                for _ in range(self.MAX_EVENTS + 1):
                    value = read()
                    if 'event' in value and 'id' not in value:
                        continue
                    if value.get('id') != request_id:
                        raise QMPError('QMP response ID mismatch')
                    if 'error' in value:
                        raise QMPError(f'QMP {name} rejected: {value["error"]!r}')
                    if 'return' not in value:
                        raise QMPError('Missing QMP return')
                    return value['return']
                raise QMPError('QMP event budget exceeded')
            exchange('qmp_capabilities')
            return exchange(command)
