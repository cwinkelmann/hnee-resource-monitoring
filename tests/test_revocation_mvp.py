"""Tests for the revocation watch-file MVP (spike).

The contract under test: a child process keeps running as long as the
watch file is empty (or absent); the parent stops it as soon as the
file gains content.
"""

import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from resourcemonitor.revocation_mvp import run_job


class RunJobTest(unittest.TestCase):
    def test_completes_when_watch_file_stays_empty(self):
        with TemporaryDirectory() as tmp:
            watch = Path(tmp) / "revoke"
            watch.touch()
            outcome = run_job(work_seconds=0.3, watch_path=watch, poll_seconds=0.05)
        self.assertEqual(outcome, "completed")

    def test_missing_watch_file_counts_as_empty(self):
        with TemporaryDirectory() as tmp:
            watch = Path(tmp) / "does-not-exist-yet"
            outcome = run_job(work_seconds=0.3, watch_path=watch, poll_seconds=0.05)
        self.assertEqual(outcome, "completed")

    def test_revoked_once_the_file_gains_content(self):
        with TemporaryDirectory() as tmp:
            watch = Path(tmp) / "revoke"
            watch.touch()

            def revoke_soon():
                time.sleep(0.3)
                watch.write_text("revoked: higher-priority booking needs the card\n")

            threading.Thread(target=revoke_soon, daemon=True).start()
            started = time.monotonic()
            outcome = run_job(work_seconds=30.0, watch_path=watch, poll_seconds=0.05)
            elapsed = time.monotonic() - started

        self.assertEqual(outcome, "revoked")
        # Far sooner than the 30 s of "work": the poll loop saw the notice.
        self.assertLess(elapsed, 5.0)


if __name__ == "__main__":
    unittest.main()
