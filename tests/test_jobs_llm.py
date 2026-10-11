import tempfile
import threading
import time
import unittest
from pathlib import Path

from erp_agent.adapters.jobs import ThreadJobRunner
from erp_agent.adapters.persistence import SQLiteRepository


class JobTests(unittest.TestCase):
    def test_cancel_and_needs_input(self):
        with tempfile.TemporaryDirectory(prefix="erp-jobs-") as directory:
            repository = SQLiteRepository(Path(directory) / "db.sqlite3")
            runner = ThreadJobRunner(repository)
            entered, release = threading.Event(), threading.Event()

            def operation(control):
                entered.set()
                release.wait(2)
                control.checkpoint("checking")
                return {"ok": True}

            def wait(job_id):
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    job = repository.get("job", "a" * 32, job_id)
                    if job["status"] not in {"queued", "running"}:
                        return job
                    time.sleep(0.01)
                self.fail("任务未结束")

            try:
                job = runner.submit("a" * 32, "test", operation)
                self.assertTrue(entered.wait(2))
                runner.cancel("a" * 32, job["id"])
                release.set()
                self.assertEqual(wait(job["id"])["status"], "cancelled")
                another = runner.submit("a" * 32, "test", lambda control: {"needs_input": True})
                self.assertEqual(wait(another["id"])["status"], "needs_input")
            finally:
                release.set()
                runner.close()
