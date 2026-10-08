"""Durable work dispatch state; interrupted jobs are never silently replayed."""
import fcntl
import json
import os
from pathlib import Path
import time
import uuid


class WorkJournal:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = self.path.with_suffix(self.path.suffix + ".lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("Another evaluator owns this work journal")
        self.records = json.loads(self.path.read_text()) if self.path.exists() else {}
        if not isinstance(self.records, dict):
            raise ValueError("Invalid work journal")

    def _save(self):
        temporary = self.path.parent / ("." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x") as stream:
                os.chmod(temporary, 0o600)
                json.dump(self.records, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            fd = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)

    def reconcile(self, job_ids, completed):
        unknown = []
        for job_id in job_ids:
            record = self.records.get(job_id)
            if job_id in completed:
                self.records[job_id] = {"state":"COMPLETED",
                                        "sandbox_id":(record or {}).get("sandbox_id")}
            elif record and record.get("state") in ("RUNNING", "UNKNOWN"):
                record["state"] = "UNKNOWN"
                unknown.append({"job_id":job_id,"sandbox_id":record.get("sandbox_id")})
            elif record and record.get("state") == "COMPLETED":
                # A missing result line cannot be reconstructed from the journal.
                record["state"] = "UNKNOWN"
                unknown.append({"job_id":job_id,"sandbox_id":record.get("sandbox_id")})
            else:
                self.records[job_id] = {"state":"QUEUED","sandbox_id":None}
        self._save()
        return unknown

    def started(self, job_id):
        record = self.records[job_id]
        if record["state"] != "QUEUED":
            raise RuntimeError("Work is not queued")
        record.update(state="RUNNING", started_at=time.time())
        self._save()

    def sandbox_created(self, job_id, sandbox_id):
        record = self.records[job_id]
        if record["state"] != "RUNNING" or not isinstance(sandbox_id, str):
            raise RuntimeError("Cannot attach sandbox to unstarted work")
        record["sandbox_id"] = sandbox_id
        self._save()

    def completed(self, job_id):
        record = self.records[job_id]
        if record["state"] != "RUNNING":
            raise RuntimeError("Work is not running")
        record.update(state="COMPLETED", completed_at=time.time())
        self._save()

    def close(self):
        self.lock.close()
