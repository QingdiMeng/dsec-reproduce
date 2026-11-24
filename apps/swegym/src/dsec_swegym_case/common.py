"""Private evidence writes and commands for this prepared SWE-Gym task image."""
import json
import os
import shlex


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def task_shell(command):
    return 'bash -lc ' + shlex.quote('source /opt/miniconda3/bin/activate && conda activate testbed && cd /testbed && ' + command)
