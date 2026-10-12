"""Private service credential files; no networking or lifecycle dependencies."""
import os
import stat


def secret_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        details = os.fstat(fd)
        if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid()
                or details.st_mode & 0o077 or details.st_size > 4096):
            raise ValueError('Secret must be a private regular file owned by this service user')
        with os.fdopen(fd, closefd=False) as stream:
            value = stream.read().strip()
        if len(value) < 32 or '\n' in value or '\r' in value:
            raise ValueError('Invalid private credential')
        return value
    finally:
        os.close(fd)
