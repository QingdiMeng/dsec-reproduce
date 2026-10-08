"""Small fsync-backed rollout journal for the single-host worker prototype."""

import json
import os
from pathlib import Path
import re
import uuid
import fcntl


class RolloutStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.lock = (self.root / "worker.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("Another rollout worker owns this state directory")

    def _path(self, rollout_id):
        if not re.fullmatch(r"[0-9a-f]{32}", rollout_id):
            raise ValueError("Invalid rollout ID")
        return self.root / (rollout_id + ".json")

    def load(self):
        records = {}
        for path in sorted(self.root.glob("*.json")):
            rollout_id = path.stem
            if self._path(rollout_id) != path:
                raise ValueError("Invalid rollout journal path")
            value = json.loads(path.read_text())
            if value.get("version") != 1 or value.get("rollout_id") != rollout_id:
                raise ValueError("Invalid rollout journal identity/version")
            records[rollout_id] = value
        return records

    def reserve(self, value):
        path = self._path(value["rollout_id"])
        data = json.dumps(value, sort_keys=True).encode() + b"\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        self._sync_dir()

    def save(self, value):
        path = self._path(value["rollout_id"])
        if not path.exists():
            raise FileNotFoundError("Rollout journal was not reserved")
        temporary = self.root / ("." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x") as stream:
                os.chmod(temporary, 0o600)
                json.dump(value, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._sync_dir()
        finally:
            temporary.unlink(missing_ok=True)

    def _sync_dir(self):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
