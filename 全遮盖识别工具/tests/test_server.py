"""HTTP contracts: isolated errors, read-only inputs, safe bounds and real exports."""

import base64
import csv
import http.client
import importlib
import io
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


try:
    service = importlib.import_module("covercheck.server")
except ModuleNotFoundError:
    service = None


# Hand-checked protobuf: template "sample", 2 ordinary tiles at (2,2,Z=0/1).
ADJACENT = bytes.fromhex(
    "0801120673616d706c65200a280a"
    "321108021002180020f6ffffffffffffffff01"
    "321108021002180120f6ffffffffffffffff01"
)
CLEAN = bytes.fromhex(
    "08011205636c65616e200a280a"
    "321108021002180020f6ffffffffffffffff01"
)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(service, "The local HTTP service is not implemented yet")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.templates = self.root / "templates"
        self.templates.mkdir()
        (self.templates / "good_m.bytes").write_bytes(ADJACENT)
        (self.templates / "clean_m.bytes").write_bytes(CLEAN)
        self.web = self.root / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("<!doctype html><title>Tool</title>")
        self.httpd = service.create_server(
            ("127.0.0.1", 0), template_root=self.templates, frontend_root=self.web)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.port = self.httpd.server_port
        self.origin = "http://127.0.0.1:{}".format(self.port)
        _, self.context = self.request("GET", "/api/context")

    def _stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, payload=None, headers=None, raw=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        request_headers = {"Origin": self.origin}
        if hasattr(self, "context"):
            request_headers["X-CSRF-Token"] = self.context["csrf_token"]
        if headers:
            request_headers.update(headers)
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        status = response.status
        response_headers = dict(response.getheaders())
        connection.close()
        return (status, data, response_headers) if raw else (status, json.loads(data))

    def finish(self, task_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            code, task = self.request("GET", "/api/tasks/" + task_id)
            self.assertEqual(code, 200)
            if task["status"] != "running":
                return task
            time.sleep(0.01)
        self.fail("Task did not finish")

    def test_id_batch_preserves_missing_as_error_and_never_writes_template(self):
        before = {path.name: path.read_bytes() for path in self.templates.iterdir()}
        code, created = self.request("POST", "/api/tasks", {
            "mode": "ids", "ids": ["good", "missing", "clean"]})
        self.assertEqual(code, 202)
        task = self.finish(created["task_id"])
        self.assertEqual((task["status"], task["completed"], task["total"]),
                         ("succeeded", 3, 3))
        self.assertEqual([row["status"] for row in task["rows"]],
                         ["issue", "error", "clean"])
        self.assertEqual(task["summary"], {
            "total": 3, "issue": 1, "clean": 1, "error": 1,
            "adjacent": 1, "floating": 0})
        self.assertNotIn("tiles", task["rows"][0])
        _, detail = self.request("GET", "/api/tasks/{}/rows/0".format(created["task_id"]))
        self.assertEqual(len(detail["tiles"]), 2)
        self.assertEqual(detail["issues"][0]["upper_layer"], 2)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.templates.iterdir()})

    def test_uploaded_corrupt_file_does_not_hide_other_results(self):
        code, created = self.request("POST", "/api/tasks", {
            "mode": "files", "files": [
                {"name": "good.bytes", "base64": base64.b64encode(ADJACENT).decode()},
                {"name": "broken.bytes", "base64": base64.b64encode(b"broken").decode()}]})
        self.assertEqual(code, 202)
        task = self.finish(created["task_id"])
        self.assertEqual([row["status"] for row in task["rows"]], ["issue", "error"])
        self.assertTrue(task["rows"][1]["error"])
        self.assertIn("无法解析", task["rows"][1]["error"])
        self.assertTrue(task["rows"][1]["error_detail"])
        self.assertEqual(task["summary"]["clean"], 0)

    def test_duplicate_submission_is_rejected_until_real_processing_finishes(self):
        from covercheck.engine import analyze_bytes
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def delayed_analysis(raw, name):
            entered.set()
            release.wait(timeout=4)
            return analyze_bytes(raw, name)

        with patch("covercheck.engine.analyze_bytes", side_effect=delayed_analysis):
            code, first = self.request("POST", "/api/tasks", {"mode": "ids", "ids": ["good", "clean"]})
            self.assertEqual(code, 202)
            self.assertTrue(entered.wait(timeout=2))
            _, running = self.request("GET", "/api/tasks/" + first["task_id"])
            self.assertEqual((running["status"], running["completed"], running["total"]), ("running", 0, 2))
            code, interrupted = self.request("POST", "/api/tasks", {"mode": "ids", "ids": ["good"]})
            self.assertEqual(code, 409)
            self.assertEqual(interrupted["error"].get("task_id"), first["task_id"])
            code, recovered = self.request("GET", "/api/tasks/" + interrupted["error"]["task_id"])
            self.assertEqual((code, recovered["total"], recovered["completed"]), (200, 2, 0))
            code, _ = self.request("GET", "/api/tasks/{}/download".format(first["task_id"]))
            self.assertEqual(code, 409)
            release.set()
            done = self.finish(first["task_id"])
        self.assertEqual((done["completed"], done["summary"]["issue"]), (2, 1))
        code, next_task = self.request("POST", "/api/tasks", {"mode": "ids", "ids": ["clean"]})
        self.assertEqual(code, 202)
        self.assertEqual(self.finish(next_task["task_id"])["status"], "succeeded")

    def test_downloads_keep_real_coordinates_and_error_rows(self):
        _, created = self.request("POST", "/api/tasks", {
            "mode": "ids", "ids": ["good", "missing"]})
        self.finish(created["task_id"])
        for detail in ("coordinates", "templates"):
            code, data, headers = self.request(
                "GET", "/api/tasks/{}/download?detail={}".format(created["task_id"], detail), raw=True)
            self.assertEqual(code, 200)
            self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
            self.assertIn("attachment", headers["Content-Disposition"])
            rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
            self.assertEqual(len(rows), 2)
            self.assertIn("评估状态", rows[1])
            self.assertEqual(rows[1]["评估状态"], "读取失败")
            self.assertTrue(rows[1]["失败原因"])
            self.assertEqual(rows[0]["评估状态"], "存在全遮盖")
            self.assertEqual(rows[0]["文件名"], "good")
            if detail == "coordinates":
                self.assertEqual((rows[0]["X"], rows[0]["Y"], rows[0]["下方层"],
                                  rows[0]["上方层"]), ("2", "2", "1", "2"))
                self.assertEqual(rows[0]["全遮盖类型"], "邻层一压一")

        code, errors = self.request("GET", "/api/tasks/{}/errors.json".format(created["task_id"]))
        self.assertEqual(code, 200)
        self.assertEqual(len(errors["errors"]), 1)
        self.assertEqual((errors["errors"][0]["row_index"], errors["errors"][0]["name"]), (1, "missing"))

    def test_csv_reads_in_chinese_and_keeps_floating_clean_and_safe_names(self):
        rows = [
            {"name": "=x.bytes", "template_id": "x", "status": "issue", "issues": [
                {"kind": "floating", "x": 8, "y": 10, "lower_layer": 16, "upper_layer": 19}]},
            {"name": "clean.bytes", "template_id": "clean", "status": "clean", "issues": []},
        ]
        exported = list(csv.DictReader(io.StringIO(service.export_csv(rows, "coordinates").decode("utf-8-sig"))))
        self.assertIn("全遮盖类型", exported[0])
        self.assertEqual(exported[0]["全遮盖类型"], "悬空全遮盖")
        self.assertEqual((exported[0]["下方层"], exported[0]["上方层"]), ("16", "19"))
        self.assertEqual(exported[0]["文件名"], "'=x.bytes")
        self.assertEqual(exported[1]["评估状态"], "未发现全遮盖")
        self.assertEqual(exported[1]["全遮盖类型"], "")

    def test_traversal_and_outside_symlink_are_never_read(self):
        for identifier in ("../good", "good/../good", "/tmp/good", "..", "a\\b"):
            with self.subTest(identifier=identifier):
                code, _ = self.request("POST", "/api/tasks", {"mode": "ids", "ids": [identifier]})
                self.assertEqual(code, 400)
        outside = self.root / "outside.bytes"
        outside.write_bytes(ADJACENT)
        (self.templates / "linked_m.bytes").symlink_to(outside)
        _, created = self.request("POST", "/api/tasks", {"mode": "ids", "ids": ["linked"]})
        task = self.finish(created["task_id"])
        self.assertEqual(task["rows"][0]["status"], "error")

    def test_existing_custom_root_uses_exact_filename_without_recursive_guessing(self):
        alternative = self.root / "custom"
        alternative.mkdir()
        (alternative / "good_m.bytes").write_bytes(CLEAN)
        _, created = self.request("POST", "/api/tasks", {
            "mode": "ids", "ids": ["good"], "template_root": str(alternative)})
        task = self.finish(created["task_id"])
        self.assertEqual(task["rows"][0]["status"], "clean")
        self.assertEqual(task["rows"][0]["template_id"], "clean")

    def test_rejects_malformed_upload_count_and_declared_large_body(self):
        payloads = [
            {"mode": "files", "files": [{"name": "x.bytes", "base64": "%%%"}]},
            {"mode": "files", "files": []},
            {"mode": "ids", "ids": ["good"] * 501},
            {"mode": "ids", "ids": ["good"], "template_root": str(self.root / "missing")},
        ]
        for payload in payloads:
            with self.subTest(payload=str(payload)[:80]):
                code, _ = self.request("POST", "/api/tasks", payload)
                self.assertEqual(code, 400)
        code, _ = self.request("POST", "/api/tasks", headers={
            "Content-Length": str(64 * 1024 * 1024 + 1), "Content-Type": "application/json"})
        self.assertEqual(code, 413)

    def test_rejects_cross_origin_missing_token_and_foreign_host(self):
        payload = {"mode": "ids", "ids": ["good"]}
        for headers in ({"Origin": "http://evil.example"}, {"X-CSRF-Token": ""},
                        {"Host": "evil.example:{}".format(self.port)}):
            code, _ = self.request("POST", "/api/tasks", payload, headers)
            self.assertEqual(code, 403)
        code, _ = self.request("GET", "/api/context", headers={"Host": "evil.example"})
        self.assertEqual(code, 403)

    def test_loopback_only_static_traversal_and_unknown_task(self):
        with self.assertRaises(ValueError):
            service.create_server(("0.0.0.0", 0), template_root=self.templates, frontend_root=self.web)
        code, _, _ = self.request("GET", "/", raw=True)
        self.assertEqual(code, 200)
        code, _, _ = self.request("GET", "/%2e%2e/server.py", raw=True)
        self.assertEqual(code, 404)
        code, _ = self.request("GET", "/api/tasks/missing")
        self.assertEqual(code, 404)

    def test_launcher_reuses_only_this_tool_service(self):
        process_env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(
            [sys.executable, "-m", "covercheck.app",
             "--port", str(self.port)], capture_output=True, text=True,
            timeout=5, env=process_env, cwd=Path(service.__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("http://127.0.0.1:{}/".format(self.port), result.stdout)
        self.assertIn("已在运行", result.stdout)

    def test_launcher_chooses_free_port_without_stopping_unrelated_service(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class OtherTool(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        other = ThreadingHTTPServer(("127.0.0.1", 0), OtherTool)
        other_thread = threading.Thread(target=other.serve_forever, daemon=True)
        other_thread.start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        process = subprocess.Popen(
            [sys.executable, "-m", "covercheck.app",
             "--port", str(other.server_port)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            cwd=Path(service.__file__).resolve().parents[1])
        output = b""
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + 5
        found = None
        try:
            while time.monotonic() < deadline and found is None:
                if selector.select(timeout=0.1):
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    output += chunk
                    found = re.search(rb"http://127\.0\.0\.1:(\d+)/", output)
            self.assertIsNotNone(found, output.decode("utf-8"))
            new_port = int(found.group(1))
            self.assertNotEqual(new_port, other.server_port)
            connection = http.client.HTTPConnection("127.0.0.1", new_port, timeout=2)
            connection.request("GET", "/api/health")
            response = connection.getresponse()
            self.assertEqual(json.loads(response.read())["service_id"], service.SERVICE_ID)
            connection.close()
            existing = http.client.HTTPConnection("127.0.0.1", other.server_port, timeout=2)
            existing.request("GET", "/")
            self.assertEqual(existing.getresponse().read(), b"{}")
            existing.close()
        finally:
            selector.close()
            process.send_signal(signal.SIGINT)
            process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)


if __name__ == "__main__":
    unittest.main()
