"""Internal crash-durable file primitive; not a client or protocol module."""
import json
import os
import tempfile


def atomic_json(path, value):
    # Services may publish the same configuration concurrently. Each writer
    # owns its temporary inode; a fixed name lets one writer rename another's
    # still-open file, publishing partial JSON and failing the second rename.
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
