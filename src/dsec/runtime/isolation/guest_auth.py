"""Per-VM private bootstrap and bounded, port-bound vsock authentication.

Also copied standalone into the trusted QEMU guest. This protects against other
host users, not root or a compromised Edge account. No retries or guest effects
occur in this transport handshake. Keys never enter task containers or argv.
"""
import hmac
import os
from pathlib import Path
import secrets
import stat
import time

FEATURE = 'dsec-vsock-auth-v1'
GREETING = b'DSECA1\n'
KEY_BYTES = 32
BOOT_ACK = b'DSEC-BOOT-OK\n'


def remaining(sock, deadline):
    duration = deadline-time.monotonic()
    if duration <= 0:
        raise TimeoutError('Guest channel authentication deadline exceeded')
    sock.settimeout(duration)


def read_exact(sock, size, *, deadline=None):
    data = bytearray()
    while len(data) < size:
        if deadline is not None:
            remaining(sock, deadline)
        part = sock.recv(size-len(data))
        if not part:
            raise EOFError('Incomplete authenticated guest channel')
        data.extend(part)
    return bytes(data)


def validate_key(key):
    if not isinstance(key, bytes) or len(key) != KEY_BYTES:
        raise ValueError('A private 256-bit guest channel key is required')
    return key


def credential(directory, *, create=False):
    path = Path(directory)/'channel.key'
    if create:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb', closefd=False) as stream:
                stream.write(secrets.token_bytes(KEY_BYTES)); stream.flush(); os.fsync(fd)
        finally:
            os.close(fd)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise PermissionError('Guest channel key ownership/type/mode mismatch')
        return validate_key(os.read(fd, KEY_BYTES+1))
    finally:
        os.close(fd)


def proof(key, port, nonce, role):
    validate_key(key)
    if port not in (5001, 5002) or len(nonce) != KEY_BYTES or role not in (b'client', b'server'):
        raise ValueError('Invalid guest authentication context')
    return hmac.digest(key, GREETING+str(port).encode()+b'\0'+role+b'\0'+nonce, 'sha256')


def authenticate_client(sock, key, port):
    validate_key(key)
    old_timeout = sock.gettimeout()
    deadline = time.monotonic()+2
    try:
        greeting = read_exact(sock, len(GREETING)+KEY_BYTES, deadline=deadline)
        if greeting[:len(GREETING)] != GREETING:
            raise PermissionError('Guest channel authentication required')
        nonce = greeting[len(GREETING):]
        remaining(sock, deadline)
        sock.sendall(proof(key, port, nonce, b'client'))
        if not hmac.compare_digest(read_exact(sock, KEY_BYTES, deadline=deadline), proof(key, port, nonce, b'server')):
            raise PermissionError('Guest channel authentication rejected')
    finally:
        sock.settimeout(old_timeout)


def authenticate_server(sock, key, port):
    validate_key(key)
    old_timeout = sock.gettimeout()
    deadline = time.monotonic()+2
    try:
        nonce = secrets.token_bytes(KEY_BYTES)
        remaining(sock, deadline)
        sock.sendall(GREETING+nonce)
        if not hmac.compare_digest(read_exact(sock, KEY_BYTES, deadline=deadline), proof(key, port, nonce, b'client')):
            raise PermissionError('Guest channel authentication rejected')
        remaining(sock, deadline)
        sock.sendall(proof(key, port, nonce, b'server'))
    finally:
        sock.settimeout(old_timeout)


def bootstrap_key(*, sysfs='/sys/class/virtio-ports', deadline_seconds=10):
    """Read once from a dedicated virtio-serial device before starting Docker.

    No file is written in the guest. Inner containers receive neither this
    device nor the host's private Unix socket. The serial port is never reused
    for execution, recovery, or key rotation.
    """
    deadline = time.monotonic()+deadline_seconds
    while time.monotonic() < deadline:
        for name in Path(sysfs).glob('vport*/name'):
            if name.read_text().strip() == FEATURE:
                fd = os.open('/dev/'+name.parent.name, os.O_RDWR | os.O_NONBLOCK)
                try:
                    key = bytearray()
                    while len(key) < KEY_BYTES and time.monotonic() < deadline:
                        try:
                            part = os.read(fd, KEY_BYTES-len(key))
                        except BlockingIOError:
                            part = b''
                        if part:
                            key.extend(part)
                        else:
                            time.sleep(.01)
                    key = validate_key(bytes(key))
                    if os.write(fd, BOOT_ACK) != len(BOOT_ACK):
                        raise OSError('Incomplete guest channel bootstrap acknowledgement')
                    return key
                finally:
                    os.close(fd)
        time.sleep(.01)
    raise TimeoutError('Private guest channel bootstrap not available')
