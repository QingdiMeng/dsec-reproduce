import tempfile
from pathlib import Path
import unittest

from work_journal import WorkJournal


class WorkJournalTests(unittest.TestCase):
    def test_interrupted_work_is_unknown_and_keeps_sandbox_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "work.json"
            first = WorkJournal(path)
            self.assertEqual(first.reconcile(["job"], set()), [])
            first.started("job")
            first.sandbox_created("job", "sandbox-123")
            first.close()

            second = WorkJournal(path)
            self.assertEqual(second.reconcile(["job"], set()),
                             [{"job_id":"job","sandbox_id":"sandbox-123"}])
            self.assertEqual(second.records["job"]["state"], "UNKNOWN")
            second.close()

            third = WorkJournal(path)
            self.assertEqual(third.reconcile(["job"], {"job"}), [])
            self.assertEqual(third.records["job"]["state"], "COMPLETED")
            third.close()

    def test_completed_journal_without_result_is_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "work.json"
            first = WorkJournal(path)
            first.reconcile(["job"], set())
            first.started("job")
            first.completed("job")
            first.close()
            second = WorkJournal(path)
            self.assertEqual(second.reconcile(["job"], set())[0]["job_id"], "job")
            second.close()


if __name__ == "__main__":
    unittest.main()
