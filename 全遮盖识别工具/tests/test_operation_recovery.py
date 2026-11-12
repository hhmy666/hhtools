"""A refreshed page can recover the real repair/export job without binary leaks."""

import base64
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from covercheck import server
from tests.test_repair_api import CLEAN


class OperationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.httpd = server.create_server(("127.0.0.1", 0), template_root=Path(self.tmp.name))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.port = self.httpd.server_port
        self.origin = "http://127.0.0.1:{}".format(self.port)
        self.token = self.request("GET", "/api/context")[1]["csrf_token"]
        code, created = self.request("POST", "/api/tasks", {"mode": "files", "files": [
            {"name": "source_m.bytes", "base64": base64.b64encode(CLEAN).decode()}]})
        self.assertEqual(code, 202)
        self.path = "/api/tasks/" + created["task_id"]
        self.poll(self.path)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(2)

    def request(self, method, path, payload=None, raw=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Origin": self.origin, "X-CSRF-Token": getattr(self, "token", "")}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        status = response.status
        connection.close()
        return status, data if raw else json.loads(data)

    def poll(self, path):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            code, result = self.request("GET", path)
            self.assertEqual(code, 200)
            if result["status"] != "running":
                return result
            time.sleep(.01)
        self.fail("后台任务未完成")

    def export(self, pattern="fix{原模板ID}"):
        _, row = self.request('GET', self.path + '/rows/0')
        if not row['edited']:
            row['tiles'][0]['x'] += 1
            code, _ = self.request('POST', self.path + '/rows/0/edit',
                                   {'tiles': row['tiles'], 'revision': row['revision']})
            self.assertEqual(code, 200)
        code, result = self.request("POST", self.path + "/export", {"pattern": pattern})
        self.assertEqual(code, 202)
        self.poll(self.path + "/export/" + result["export_id"] + "/status")
        return result["export_id"]

    def test_running_repair_is_recoverable_from_snapshot_and_conflict(self):
        from covercheck.repair import auto_repair
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def delayed(raw):
            entered.set()
            release.wait(3)
            return auto_repair(raw)

        with patch("covercheck.repair.auto_repair", side_effect=delayed):
            code, _ = self.request("POST", self.path + "/repair", {})
            self.assertEqual(code, 202)
            self.assertTrue(entered.wait(2))
            _, snapshot = self.request("GET", self.path)
            self.assertIn("operation", snapshot)
            operation = snapshot["operation"]
            self.assertEqual((operation["kind"], operation["status"], operation["completed"],
                              operation["total"]), ("repair", "running", 0, 1))
            code, conflict = self.request("POST", self.path + "/export", {"pattern": "fix{原模板ID}"})
            self.assertEqual(code, 409)
            self.assertEqual(conflict["error"]["operation"], operation)
            code, conflict = self.request("POST", self.path + "/rows/0/undo", {"revision": 0})
            self.assertEqual((code, conflict["error"]["code"]), (409, "repair_running"))
            release.set()
            self.poll(self.path + "/repair")
        operation = self.request("GET", self.path)[1]["operation"]
        self.assertEqual((operation["kind"], operation["status"], operation["completed"]),
                         ("repair", "succeeded", 1))
        self.assertEqual(operation["rows"][0]["status"], "unchanged")

    def test_running_export_exposes_same_export_id_on_refresh_and_conflict(self):
        _, row = self.request('GET', self.path + '/rows/0')
        row['tiles'][0]['x'] += 1
        self.assertEqual(self.request('POST', self.path + '/rows/0/edit',
                         {'tiles': row['tiles'], 'revision': row['revision']})[0], 200)
        from covercheck.repair import validate_board
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def delayed(raw):
            entered.set()
            release.wait(3)
            return validate_board(raw)

        with patch("covercheck.repair.validate_board", side_effect=delayed):
            code, created = self.request("POST", self.path + "/export", {"pattern": "fix{原模板ID}"})
            self.assertEqual(code, 202)
            self.assertTrue(entered.wait(2))
            _, snapshot = self.request("GET", self.path)
            self.assertIn("operation", snapshot)
            operation = snapshot["operation"]
            self.assertEqual((operation["kind"], operation["status"], operation["export_id"]),
                             ("export", "running", created["export_id"]))
            code, conflict = self.request("POST", self.path + "/repair", {})
            self.assertEqual(code, 409)
            self.assertEqual(conflict["error"]["operation"], operation)
            code, conflict = self.request("POST", self.path + "/rows/0/undo", {"revision": 0})
            self.assertEqual((code, conflict["error"]["code"]), (409, "export_running"))
            release.set()
            self.poll(self.path + "/export/" + created["export_id"] + "/status")

    def test_latest_completed_export_can_restore_download_without_binary_snapshot(self):
        self.export("first{原模板ID}")
        latest = self.export("second{原模板ID}")
        _, snapshot = self.request("GET", self.path)
        self.assertIn("operation", snapshot)
        operation = snapshot["operation"]
        self.assertEqual((operation["kind"], operation["export_id"], operation["status"]),
                         ("export", latest, "succeeded"))
        self.assertEqual(operation["files"], ["secondsource_m.bytes"])
        self.assertEqual(operation["skipped"], [])
        self.assertNotIn("data", operation)
        self.assertNotIn("exports", snapshot)
        self.assertNotIn("states", snapshot)
        code, package = self.request("GET", self.path + "/export/" + operation["export_id"], raw=True)
        self.assertEqual(code, 200)
        self.assertTrue(package.startswith(b"PK"))

    def test_saved_edit_does_not_restore_zip_for_an_older_revision(self):
        self.export()
        _, row = self.request("GET", self.path + "/rows/0")
        row["tiles"][0]["x"] = 3
        code, saved = self.request("POST", self.path + "/rows/0/edit", {
            "tiles": row["tiles"], "revision": row["revision"]})
        self.assertEqual(code, 200)
        self.assertEqual(saved["revision"], 2)
        _, snapshot = self.request("GET", self.path)
        self.assertIn("operation", snapshot)
        self.assertIsNone(snapshot["operation"])

    def test_new_repair_replaces_completed_export_as_latest_operation(self):
        self.export()
        code, _ = self.request("POST", self.path + "/repair", {})
        self.assertEqual(code, 202)
        self.poll(self.path + "/repair")
        _, snapshot = self.request("GET", self.path)
        self.assertIn("operation", snapshot)
        self.assertEqual((snapshot["operation"]["kind"], snapshot["operation"]["status"]),
                         ("repair", "succeeded"))


if __name__ == "__main__":
    unittest.main()
