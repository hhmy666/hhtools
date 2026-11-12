from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.errors import OperationCancelledError  # noqa: E402
from services.task_control import CooperativeTaskControl  # noqa: E402


class CooperativeTaskControlTests(unittest.TestCase):
    def test_pause_resume_and_cancel_are_observed_at_checkpoints(self) -> None:
        control = CooperativeTaskControl()
        self.assertTrue(control.request_pause())
        completed = threading.Event()

        def worker() -> None:
            control.checkpoint("unit-test")
            completed.set()

        thread = threading.Thread(target=worker)
        thread.start()
        for _ in range(100_000):
            if control.state == "paused":
                break
        self.assertEqual(control.state, "paused")
        self.assertFalse(completed.is_set())
        self.assertTrue(control.resume())
        thread.join(timeout=1)
        self.assertTrue(completed.is_set())

        cancelled = CooperativeTaskControl()
        self.assertTrue(cancelled.request_cancel())
        with self.assertRaises(OperationCancelledError):
            cancelled.checkpoint("unit-test-cancel")
        self.assertEqual(cancelled.state, "cancelled")

    def test_commit_boundary_rejects_new_pause_and_cancel(self) -> None:
        control = CooperativeTaskControl()
        control.begin_uninterruptible("export_commit")
        self.assertEqual(control.state, "committing")
        self.assertFalse(control.request_pause())
        self.assertFalse(control.request_cancel())
        control.mark_completed()
        self.assertEqual(control.state, "completed")


if __name__ == "__main__":
    unittest.main()
