import base64
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from covercheck.server import Application, ApiError, MAX_BODY_BYTES
from test_engine import message
from test_server import ADJACENT


class WorkflowScaleTests(unittest.TestCase):
    def test_large_id_batch_has_lightweight_progress_and_lazy_boards(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "found_m.bytes").write_bytes(ADJACENT)
            app = Application(template_root=root)
            task = app.submit({"mode": "ids", "ids": ["found"] + [
                "missing" + str(i) for i in range(49999)]})
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                progress = app.snapshot(task, progress_only=True)
                self.assertNotIn("rows", progress)
                if progress["status"] != "running":
                    break
                time.sleep(.02)
            self.assertEqual(progress["status"], "succeeded")
            self.assertEqual(progress["completed"], 50000)
            self.assertNotIn("tiles", app.tasks[task]["rows"][0])
            self.assertEqual(len(app.row(task, 0)["tiles"]), 2)
            self.assertEqual(app.snapshot(task)["summary"]["error"], 49999)

    def test_upload_accepts_fifty_thousand_files_over_multiple_requests(self):
        app = Application()
        count = 50_000
        upload_id = app.begin_upload({"total": count})["upload_id"]
        raw = ADJACENT + message(100, bytes(2048))
        self.assertGreater(count * len(raw), MAX_BODY_BYTES)
        encoded = base64.b64encode(raw).decode()
        for offset in range(0, count, 250):
            reply = app.append_upload(upload_id, {"offset": offset, "files": [
                {"name": f"{i}_m.bytes", "base64": encoded}
                for i in range(offset, offset + 250)]})
            self.assertEqual(reply["received"], offset + 250)
        with patch.object(app, "_start_task", return_value="task") as start:
            self.assertEqual(app.submit({"mode": "upload", "upload_id": upload_id}), "task")
            self.assertEqual(len(start.call_args.args[0]), count)
            self.assertEqual(start.call_args.args[0][-1], ("49999_m.bytes", raw))
            self.assertEqual(start.call_args.kwargs["uploaded"], count)
        self.assertIsNone(app.upload)

    def test_upload_checks_order_count_and_cumulative_budget_before_accepting_chunk(self):
        app = Application()
        upload_id = app.begin_upload({"total": 2})["upload_id"]
        item = {"name": "one_m.bytes", "base64": "YQ=="}
        with self.assertRaises(ApiError) as incomplete:
            app.submit({"mode": "upload", "upload_id": upload_id})
        self.assertEqual(incomplete.exception.code, "upload_incomplete")
        app.append_upload(upload_id, {"offset": 0, "files": [item]})
        with self.assertRaises(ApiError) as duplicate:
            app.append_upload(upload_id, {"offset": 0, "files": [item]})
        self.assertEqual(duplicate.exception.code, "invalid_upload_offset")
        with patch("covercheck.server.MAX_UPLOAD_BYTES", 1), self.assertRaises(ApiError) as overflow:
            app.append_upload(upload_id, {"offset": 1, "files": [item]})
        self.assertEqual(overflow.exception.code, "batch_too_large")
        self.assertEqual(len(app.upload["files"]), 1)
        self.assertEqual(app.append_upload(upload_id, {"cancel": True}), {"cancelled": True})
        self.assertIsNone(app.upload)

    def test_sync_confirmation_is_required_before_write(self):
        app = Application()
        task_id = "fixture"
        app.tasks[task_id] = {"status": "succeeded", "exports": {"preview": {
            "mode": "preview", "status": "succeeded", "_plan": {}, "_revisions": {},
            "targets": {"output": "/output", "template": "/repository"}, "files": ["new_m.bytes"]}}}
        with self.assertRaises(ApiError) as caught:
            app.write_delivery(task_id, "preview", {})
        self.assertEqual(caught.exception.code, "sync_not_confirmed")
