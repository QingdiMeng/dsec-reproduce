"""A process dying inside a lifecycle effect must not admit the same ID again."""

import json
import unittest
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

from container_lifecycle_journal import ContainerLifecycleJournal
from sandbox_client import RequestOutcomeUnknown


def main():
    with tempfile.TemporaryDirectory(prefix="dsec-container-journal-") as directory:
        root = Path(directory)
        request_id = uuid.uuid4().hex
        marker = root / "effect.txt"
        child = r'''
import os, sys
from pathlib import Path
from container_lifecycle_journal import ContainerLifecycleJournal
root, request_id = Path(sys.argv[1]), sys.argv[2]
def effect():
    (root / "effect.txt").write_text("ran once")
    os._exit(17)
ContainerLifecycleJournal(root).execute(request_id, "create", None,
    {"environment_id": "e1-real", "storage": "local"}, effect)
'''
        result = subprocess.run([sys.executable, "-c", child, str(root), request_id],
                                cwd=Path(__file__).resolve().parents[2], check=False)
        assert result.returncode == 17 and marker.read_text() == "ran once"
        journal = ContainerLifecycleJournal(root)
        assert journal.lookup(request_id)["state"] == "UNKNOWN"
        try:
            journal.execute(request_id, "create", None,
                            {"environment_id": "e1-real", "storage": "local"},
                            lambda: marker.write_text("ran twice"))
        except RequestOutcomeUnknown:
            pass
        else:
            raise AssertionError("Uncommitted request was replayed")
        assert marker.read_text() == "ran once"
        print(json.dumps({"status": "passed", "checks": [
            "dead_process_pending_becomes_unknown", "unknown_request_cannot_reexecute"]}))


class ContainerLifecycleJournalTests(unittest.TestCase):
    def test_side_effect_is_not_replayed_after_process_death(self):
        main()


if __name__ == "__main__":
    unittest.main()
