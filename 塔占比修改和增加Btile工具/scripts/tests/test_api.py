"""Black-box HTTP contract for the local tower-reduction API.

These tests intentionally name only public HTTP resources and one small server
construction seam.  Production code may be organized freely behind this
contract.  The seam is::

    api.Application(...dependency injection...)
    app.create_server(("127.0.0.1", 0), application)

All filesystem roots used here are temporary.  Task 8 is represented by a
small adapter because its transaction mechanics have their own test module;
these tests verify that HTTP never turns a browser-supplied path into sync
authority.
"""

from __future__ import annotations

import hashlib
import http.client
import importlib
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote

from scripts.tests.project_fixtures import read_project_fixture
from scripts.tests.test_rewrite import synthetic
from scripts.tower_reducer.path_binding import TEMPLATE_SUFFIX, TILE_SUFFIX
from scripts.tower_reducer.settings import LocalSettings, save_settings
from scripts.tower_reducer.storage import JobRepository

TERMINAL_JOB_STATES = {"succeeded", "partial_failed", "failed", "interrupted"}


class FakeSyncService:
    """Task-8-shaped adapter; records logical binding keys, never paths."""

    def __init__(self) -> None:
        self.plans = []
        self.commits = []

    def create_sync_plan(self, job, selected_bindings):
        self.plans.append((job.job_id, tuple(selected_bindings)))
        return {
            "plan_id": "plan-" + "a" * 32,
            "status": "ready",
            "rows": [],
            "summary_sha256": "1" * 64,
        }

    def commit_sync(self, plan_id, conflict_confirmations):
        self.commits.append((plan_id, dict(conflict_confirmations)))
        return {"plan_id": plan_id, "status": "committed", "rows": []}

    def recover_pending(self):
        return []


class BlockingSyncService(FakeSyncService):
    def __init__(self):
        super().__init__()
        self.commit_entered = threading.Event()
        self.commit_release = threading.Event()

    def commit_sync(self, plan_id, conflict_confirmations):
        self.commits.append((plan_id, dict(conflict_confirmations)))
        self.commit_entered.set()
        self.commit_release.wait(3)
        return {"plan_id": plan_id, "status": "committed", "rows": []}


class ConfirmationSyncService(FakeSyncService):
    def commit_sync(self, plan_id, conflict_confirmations):
        self.commits.append((plan_id, dict(conflict_confirmations)))
        if not conflict_confirmations:
            return {"plan_id": plan_id, "status": "confirmation_required", "rows": []}
        return {"plan_id": plan_id, "status": "committed", "rows": []}


class ApiContractCase(unittest.TestCase):
    """Reusable real-socket harness with explicit local-origin headers."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.tool_root = self.root / "tool"
        (self.tool_root / "frontend" / "assets").mkdir(parents=True)
        (self.tool_root / "frontend" / "index.html").write_text(
            "<!doctype html><title>塔占比修改工具</title>", encoding="utf-8"
        )
        (self.tool_root / "frontend" / "assets" / "app.js").write_text(
            "window.localTool = true;\n", encoding="utf-8"
        )
        self.runtime_root = self.tool_root / "运行数据"
        self.repository = JobRepository(self.runtime_root / "任务")
        self.settings_path = self.runtime_root / "settings.json"
        self.export_root = self.root / "exports"
        self.export_root.mkdir()
        save_settings(LocalSettings(export_root=str(self.export_root)), self.settings_path)
        self.discovery_root = self.root / "home"
        self.discovery_root.mkdir()
        self.sync_service = FakeSyncService()
        self.csrf = "test-csrf-token-0123456789"
        self.server = None
        self.worker = None

    def _modules(self):
        app = importlib.import_module("scripts.app")
        try:
            api = importlib.import_module("scripts.tower_reducer.api")
        except ModuleNotFoundError as error:
            self.fail("Task 9 production module scripts.tower_reducer.api is not implemented: {}".format(error))
        return app, api

    def start_server(self, *, limits=None, repository=None, sync_service=None, clock=None):
        app, api = self._modules()
        kwargs = {
            "tool_root": self.tool_root,
            "repository": repository or self.repository,
            "settings_path": self.settings_path,
            "discovery_root": self.discovery_root,
            "sync_service": self.sync_service if sync_service is None else sync_service,
            "csrf_token": self.csrf,
        }
        if limits is not None:
            kwargs["limits"] = limits
        if clock is not None:
            kwargs["clock"] = clock
        application = api.Application(**kwargs)
        self.server = app.create_server((app.HOST, 0), application)
        self.addCleanup(self._stop_server)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.port = self.server.server_port
        self.origin = "http://127.0.0.1:{}".format(self.port)
        return application

    def _stop_server(self):
        if self.server is None:
            return
        self.server.shutdown()
        self.server.server_close()
        if self.worker is not None:
            self.worker.join(timeout=2)
        self.server = None

    def request(self, method, path, *, body=None, json_body=None, headers=None,
                same_origin=True, csrf=True, host=None):
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = dict(headers or {})
        headers.setdefault("Host", host or "127.0.0.1:{}".format(self.port))
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            if json_body is not None:
                headers.setdefault("Content-Type", "application/json")
            if same_origin:
                headers.setdefault("Origin", self.origin)
            if csrf:
                headers.setdefault("X-CSRF-Token", self.csrf)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        result_headers = {name.lower(): value for name, value in response.getheaders()}
        connection.close()
        content_type = result_headers.get("content-type", "")
        payload = json.loads(raw.decode("utf-8")) if content_type.startswith("application/json") and raw else None
        return response.status, result_headers, payload, raw

    def assert_error(self, response, status, code):
        actual_status, headers, payload, raw = response
        self.assertEqual(actual_status, status, raw[:500])
        self.assertEqual(set(payload), {"error"})
        error = payload["error"]
        self.assertEqual(error["protocol"], 1)
        self.assertEqual(error["code"], code)
        self.assertIsInstance(error["message"], str)
        self.assertTrue(error["message"])
        self.assertRegex(error["request_id"], r"^req-[A-Za-z0-9_-]{12,}$")
        self.assertTrue(headers["content-type"].startswith("application/json"))
        self.assertEqual(headers["cache-control"], "no-store")
        decoded = raw.decode("utf-8", errors="replace")
        self.assertNotIn("Traceback", decoded)
        self.assertNotIn(str(self.root), decoded)

    @staticmethod
    def multipart(files, *, boundary="TowerBoundary", fields=()):
        chunks = []
        marker = boundary.encode("ascii")
        for name, value in fields:
            chunks.extend((b"--" + marker + b"\r\n",
                           ('Content-Disposition: form-data; name="{}"\r\n\r\n'.format(name)).encode("ascii"),
                           str(value).encode("utf-8"), b"\r\n"))
        for filename, raw in files:
            chunks.extend((b"--" + marker + b"\r\n",
                           b'Content-Disposition: form-data; name="files"; filename="' + filename.encode("utf-8") + b'"\r\n',
                           b"Content-Type: application/octet-stream\r\n\r\n", raw, b"\r\n"))
        chunks.append(b"--" + marker + b"--\r\n")
        return b"".join(chunks), "multipart/form-data; boundary=" + boundary

    def import_files(self, files, *, extra_fields=()):
        body, content_type = self.multipart(files, fields=extra_fields)
        status, headers, payload, raw = self.request(
            "POST", "/api/import", body=body, headers={"Content-Type": content_type}
        )
        self.assertEqual(status, 200, raw[:1000])
        self.assertRegex(payload["import_id"], r"^import-[0-9a-f]{32}$")
        return payload

    def start_job(self, import_id, *, defaults=None, overrides=None):
        payload = {
            "import_id": import_id,
            "defaults": defaults or {
                "ratio": 0.25,
                "retained_counts": None,
                "strategy": "top_first",
                "max_tiles_per_layer": 20,
                "max_added_layers": 2,
                "seed": 0,
                "naming_rule": "lessT{原模板ID}",
            },
            "overrides": overrides or {},
        }
        status, _, result, raw = self.request("POST", "/api/jobs", json_body=payload)
        self.assertEqual(status, 202, raw[:1000])
        self.assertRegex(result["job_id"], r"^job-[0-9a-f]{32}$")
        self.assertEqual(result["status"], "queued")
        return result["job_id"]

    def wait_job(self, job_id, timeout=12):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            status, _, last, _ = self.request("GET", "/api/jobs/" + job_id)
            self.assertEqual(status, 200)
            if last["status"] in TERMINAL_JOB_STATES:
                return last
            time.sleep(0.03)
        self.fail("job did not finish; last state={!r}".format(last))

    @staticmethod
    def rows(payload):
        rows = payload.get("templates", [])
        return list(rows.values()) if isinstance(rows, dict) else list(rows)


class ApiProtocolTests(ApiContractCase):
    def test_bootstrap_versions_contract_and_publishes_exact_resource_limits(self):
        self.start_server()
        status, headers, payload, _ = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(payload["protocol"], 1)
        self.assertEqual(payload["status"]["local_only"], True)
        self.assertEqual(payload["csrf_token"], self.csrf)
        self.assertEqual(payload["limits"]["file_bytes"], 32 * 1024 * 1024)
        self.assertEqual(payload["limits"]["file_count"], 20)
        self.assertEqual(payload["limits"]["request_bytes"], 128 * 1024 * 1024)
        self.assertEqual(payload["limits"]["active_uploads"], 2)
        self.assertEqual(payload["limits"]["active_jobs"], 2)
        self.assertIn("confirmation_plans", payload["limits"])
        self.assertIn("zip_uncompressed_bytes", payload["limits"])
        self.assertEqual(payload["limits"]["active_zip_downloads"], 1)
        self.assertEqual(payload["resources"]["active_jobs"], 0)
        self.assertEqual(payload["resources"]["confirmation_plans"], 0)
        for capability in ("multi_file_import", "background_jobs", "rerun_template",
                           "two_phase_export", "safe_sync", "zip_download"):
            self.assertIn(capability, payload["capabilities"])
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
        self.assertNotIn("access-control-allow-origin", headers)

    def test_all_api_failures_use_one_versioned_json_envelope(self):
        self.start_server()
        cases = [
            self.request("GET", "/api/does-not-exist"),
            self.request("POST", "/api/jobs", body=b"{", headers={"Content-Type": "application/json"}),
            self.request("POST", "/api/jobs", body=b"{}", headers={"Content-Type": "text/plain"}),
            self.request("GET", "/api/jobs/job-" + "f" * 32),
        ]
        expected = [(404, "not_found"), (400, "invalid_json"),
                    (415, "unsupported_media_type"), (404, "job_not_found")]
        for response, (status, code) in zip(cases, expected):
            with self.subTest(code=code):
                self.assert_error(response, status, code)

    def test_host_origin_referer_fetch_site_and_csrf_are_fail_closed(self):
        self.start_server()
        valid = {"import_id": "import-" + "a" * 32, "defaults": {}, "overrides": {}}
        rejected = [
            (dict(headers={"Host": ""}), "host_rejected"),
            (dict(host="evil.example:{}".format(self.port)), "host_rejected"),
            (dict(host="127.0.0.1:{}".format(self.port + 1)), "host_rejected"),
            (dict(headers={"Origin": "null"}, same_origin=False), "origin_rejected"),
            (dict(headers={"Origin": "http://evil.example"}, same_origin=False), "origin_rejected"),
            (dict(headers={"Origin": "http://127.0.0.1:{}".format(self.port + 1)}, same_origin=False), "origin_rejected"),
            (dict(same_origin=False), "origin_rejected"),
            (dict(headers={"Sec-Fetch-Site": "cross-site"}), "origin_rejected"),
            (dict(csrf=False), "csrf_failed"),
            (dict(headers={"X-CSRF-Token": "wrong"}), "csrf_failed"),
        ]
        for kwargs, code in rejected:
            with self.subTest(code=code, kwargs=kwargs):
                self.assert_error(self.request("POST", "/api/jobs", json_body=valid, **kwargs), 403, code)
        # Non-browser local clients may omit Origin only when a same-origin Referer exists.
        response = self.request("POST", "/api/jobs", json_body=valid, same_origin=False,
                                headers={"Referer": self.origin + "/"})
        self.assertNotEqual(response[0], 403)

    def test_json_schemas_reject_unknown_fields_wrong_roots_and_bool_as_integer(self):
        self.start_server()
        wrong_root = json.dumps([1, 2]).encode("utf-8")
        self.assert_error(self.request("POST", "/api/jobs", body=wrong_root,
                                       headers={"Content-Type": "application/json"}),
                          400, "invalid_json_root")
        response = self.request("POST", "/api/jobs", json_body={
            "import_id": "import-" + "a" * 32,
            "defaults": {"ratio": True},
            "overrides": {},
            "target_path": str(self.root / "outside"),
        })
        self.assert_error(response, 400, "invalid_request")

    def test_static_files_are_contained_and_every_response_gets_security_headers(self):
        self.start_server()
        for path in ("/", "/assets/app.js"):
            status, headers, _, _ = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertEqual(headers["x-content-type-options"], "nosniff")
            self.assertEqual(headers["x-frame-options"], "DENY")
        outside = self.root / "secret.txt"
        outside.write_text("do not serve", encoding="utf-8")
        response = self.request("GET", "/%2e%2e/%2e%2e/secret.txt")
        self.assert_error(response, 404, "not_found")
        self.assertNotIn(b"do not serve", response[3])


class MultipartImportTests(ApiContractCase):
    def test_multiple_files_are_isolated_deduplicated_and_server_names_snapshots(self):
        self.start_server()
        valid, _ = synthetic()
        payload = self.import_files([
            ("first_m.bytes", valid),
            ("duplicate_m.bytes", valid),
            ("broken_m.bytes", b"not-a-template"),
        ])
        self.assertEqual(len(payload["files"]), 3)
        first, duplicate, broken = payload["files"]
        self.assertEqual(first["sha256"], hashlib.sha256(valid).hexdigest())
        self.assertEqual(duplicate["duplicate_of"], first["file_id"])
        self.assertEqual(first["status"], "ready")
        inspection = first["inspection"]
        self.assertIsInstance(inspection["template_id"], str)
        self.assertTrue(inspection["template_id"])
        for field in ("total_tiles", "tile_count", "a_count", "b_count", "tower_count",
                      "tower_tile_count", "tower_ratio", "stacks", "tower_stacks", "non_tower_layers",
                      "symmetry", "profiles", "issues"):
            self.assertIn(field, inspection)
        self.assertEqual(broken["status"], "failed")
        self.assertEqual(broken["error"]["code"], "invalid_template")
        # Browser filename is only display metadata, never a persisted path.
        self.assertFalse(any(path.name == "first_m.bytes" for path in self.runtime_root.rglob("*") if path.is_file()))

    def test_malicious_filename_wrong_extension_empty_file_and_extra_fields_are_rejected_per_file(self):
        self.start_server()
        valid, _ = synthetic()
        payload = self.import_files([
            ("../../escape_m.bytes", valid),
            ("wrong.bytes", valid),
            ("empty_m.bytes", b""),
            ("good_m.bytes", valid),
        ])
        statuses = {row["original_name"]: row for row in payload["files"]}
        self.assertEqual(statuses["../../escape_m.bytes"]["error"]["code"], "invalid_filename")
        self.assertEqual(statuses["wrong.bytes"]["error"]["code"], "invalid_filename")
        self.assertEqual(statuses["empty_m.bytes"]["error"]["code"], "invalid_template")
        self.assertEqual(statuses["good_m.bytes"]["status"], "ready")
        self.assertFalse((self.root / "escape_m.bytes").exists())
        body, content_type = self.multipart([("good_m.bytes", valid)], fields=(("target_path", "/tmp"),))
        self.assert_error(self.request("POST", "/api/import", body=body,
                                       headers={"Content-Type": content_type}),
                          400, "invalid_request")

    def test_multipart_media_boundary_length_and_encoding_failures_are_protocol_errors(self):
        self.start_server()
        bad = [
            self.request("POST", "/api/import", body=b"x", headers={"Content-Type": "application/json"}),
            self.request("POST", "/api/import", body=b"x", headers={"Content-Type": "multipart/form-data"}),
            self.request("POST", "/api/import", body=b"not multipart", headers={"Content-Type": "multipart/form-data; boundary=x"}),
            self.request("POST", "/api/import", body=b"x", headers={"Content-Type": "multipart/form-data; boundary=x",
                                                                      "Content-Encoding": "gzip"}),
        ]
        for response, expected in zip(bad, ((415, "unsupported_media_type"),
                                             (400, "invalid_multipart"),
                                             (400, "invalid_multipart"),
                                             (415, "unsupported_media_type"))):
            with self.subTest(expected=expected):
                self.assert_error(response, *expected)

    def test_file_count_file_size_and_request_size_boundaries_use_configurable_small_limits(self):
        _, api = self._modules()
        valid, _ = synthetic()
        exact_body, _ = self.multipart([("exact_m.bytes", valid)])
        limits = api.ApiLimits(file_bytes=len(valid), file_count=2,
                               request_bytes=len(exact_body) + 4096, active_uploads=2,
                               download_bytes=1024 * 1024, zip_uncompressed_bytes=1024 * 1024,
                               active_zip_downloads=1)
        self.start_server(limits=limits)
        payload = self.import_files([("exact_m.bytes", valid)])
        self.assertEqual(payload["files"][0]["status"], "ready")
        too_large_file = self.import_files([("large_m.bytes", valid + b"x")])
        self.assertEqual(too_large_file["files"][0]["error"]["code"], "file_too_large")
        count_body, count_type = self.multipart([("a_m.bytes", valid), ("b_m.bytes", valid),
                                                  ("c_m.bytes", valid)])
        # Increase only the request ceiling so the file-count branch is observable.
        self._stop_server()
        limits = api.ApiLimits(file_bytes=len(valid), file_count=2,
                               request_bytes=len(count_body) + 1, active_uploads=2,
                               download_bytes=1024 * 1024, zip_uncompressed_bytes=1024 * 1024,
                               active_zip_downloads=1)
        self.start_server(limits=limits)
        self.assert_error(self.request("POST", "/api/import", body=count_body,
                                       headers={"Content-Type": count_type}), 413, "too_many_files")
        self._stop_server()
        limits = api.ApiLimits(file_bytes=len(valid) + 1, file_count=3,
                               request_bytes=len(exact_body) - 1, active_uploads=2,
                               download_bytes=1024 * 1024, zip_uncompressed_bytes=1024 * 1024,
                               active_zip_downloads=1)
        self.start_server(limits=limits)
        body, content_type = self.multipart([("exact_m.bytes", valid)])
        self.assert_error(self.request("POST", "/api/import", body=body,
                                       headers={"Content-Type": content_type}), 413, "body_too_large")

    def test_a_third_incomplete_upload_is_rejected_while_two_upload_slots_are_held(self):
        self.start_server()
        boundary = "HeldUpload"
        sockets = []
        try:
            for _ in range(2):
                stream = socket.create_connection(("127.0.0.1", self.port), timeout=2)
                request = (
                    "POST /api/import HTTP/1.1\r\n"
                    "Host: 127.0.0.1:{port}\r\n"
                    "Origin: {origin}\r\n"
                    "X-CSRF-Token: {csrf}\r\n"
                    "Content-Type: multipart/form-data; boundary={boundary}\r\n"
                    "Content-Length: 4096\r\n\r\n"
                    "--{boundary}\r\n"
                ).format(port=self.port, origin=self.origin, csrf=self.csrf, boundary=boundary).encode("ascii")
                stream.sendall(request)
                sockets.append(stream)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                status, _, bootstrap, _ = self.request("GET", "/api/bootstrap")
                self.assertEqual(status, 200)
                if bootstrap["resources"]["active_uploads"] == 2:
                    break
                time.sleep(0.02)
            valid, _ = synthetic()
            body, content_type = self.multipart([("third_m.bytes", valid)])
            self.assert_error(self.request("POST", "/api/import", body=body,
                                           headers={"Content-Type": content_type}),
                              429, "upload_busy")
        finally:
            for stream in sockets:
                stream.close()


class JobsAndArtifactsTests(ApiContractCase):
    def test_bad_template_does_not_block_tower_output_and_no_tower_is_skipped(self):
        self.start_server()
        tower, _ = synthetic()
        no_tower = read_project_fixture("ntxkd021_m.bytes")
        imported = self.import_files([
            ("tower_m.bytes", tower),
            ("no-tower_m.bytes", no_tower),
            ("bad_m.bytes", b"broken"),
        ])
        job_id = self.start_job(imported["import_id"])
        result = self.wait_job(job_id)
        rows = self.rows(result)
        by_name = {row["original_names"][0]: row for row in rows}
        self.assertIn(by_name["tower_m.bytes"]["status"], {"verified", "succeeded"})
        self.assertEqual(by_name["no-tower_m.bytes"]["status"], "no_op")
        self.assertEqual(by_name["no-tower_m.bytes"]["artifacts"], [])
        self.assertFalse(by_name["no-tower_m.bytes"].get("naming", {}).get("new_id"))
        no_tower_key = by_name["no-tower_m.bytes"]["key"]
        self.assertFalse((self.repository.get(job_id).directory / 'templates' / no_tower_key / 'outputs').exists())
        self.assertEqual(by_name["bad_m.bytes"]["status"], "failed")
        self.assertEqual(result["status"], "partial_failed")
        self.assertEqual(result["progress"]["completed_templates"], 3)
        self.assertEqual(result["progress"]["total_templates"], 3)
        self.assertGreaterEqual(result["revision"], 3)
        tower_row = by_name["tower_m.bytes"]
        summary = tower_row["plan_summary"]
        for field in ("new_id", "source_sha256", "output_sha256", "source_tower_counts",
                      "retained_counts", "source_tower_ratio",
                      "result_tower_ratio", "move_type", "move_count", "added_layers",
                      "strategy", "seed", "destinations", "audit", "recommendations"):
            self.assertIn(field, summary)
        verification = tower_row["verification"]
        self.assertEqual(verification["status"], "passed")
        self.assertIs(verification["ok"], True)
        self.assertIn("issues", verification)

    def test_job_schema_rejects_unknown_options_and_override_for_unknown_file(self):
        self.start_server()
        valid, _ = synthetic()
        imported = self.import_files([("valid_m.bytes", valid)])
        file_id = imported["files"][0]["file_id"]
        unknown = self.request("POST", "/api/jobs", json_body={
            "import_id": imported["import_id"], "defaults": {"ratio": 0.25, "mystery": 1}, "overrides": {}
        })
        self.assert_error(unknown, 400, "invalid_request")
        wrong_type = self.request("POST", "/api/jobs", json_body={
            "import_id": imported["import_id"], "defaults": {"ratio": True}, "overrides": {}
        })
        self.assert_error(wrong_type, 400, "invalid_request")
        missing_file = self.request("POST", "/api/jobs", json_body={
            "import_id": imported["import_id"], "defaults": {},
            "overrides": {file_id + "x": {"ratio": 0}}
        })
        self.assert_error(missing_file, 400, "invalid_request")

    def _completed_tower_job(self):
        tower, _ = synthetic()
        imported = self.import_files([("tower_m.bytes", tower)])
        job_id = self.start_job(imported["import_id"])
        return job_id, self.wait_job(job_id)

    def test_download_uses_opaque_current_artifact_whitelist_and_rechecks_bytes(self):
        self.start_server()
        job_id, result = self._completed_tower_job()
        artifact = result["artifacts"][0]
        status, headers, _, raw = self.request(
            "GET", "/api/jobs/{}/download/{}".format(job_id, artifact["id"])
        )
        self.assertEqual(status, 200)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), artifact["sha256"])
        self.assertIn("attachment", headers["content-disposition"])
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        for malicious in ("../source.bytes", "%2e%2e%2fsource.bytes", quote("/tmp/out"), "unknown"):
            with self.subTest(malicious=malicious):
                self.assert_error(self.request("GET", "/api/jobs/{}/download/{}".format(job_id, malicious)),
                                  404, "artifact_not_found")
        # Replacing a registered artifact with a symlink must not stream outside bytes.
        state = self.repository.get(job_id).snapshot()
        record = state["artifacts"][artifact["id"]]
        path = self.repository.get(job_id).directory / record["relative_path"]
        outside = self.root / "outside.bytes"
        outside.write_bytes(b"outside-secret")
        path.unlink()
        path.symlink_to(outside)
        response = self.request("GET", "/api/jobs/{}/download/{}".format(job_id, artifact["id"]))
        self.assertIn(response[0], {404, 409})
        self.assertNotIn(b"outside-secret", response[3])

    def test_download_and_zip_apply_size_budgets_before_streaming(self):
        _, api = self._modules()
        limits = api.ApiLimits(file_bytes=32 * 1024 * 1024, file_count=20,
                               request_bytes=128 * 1024 * 1024, active_uploads=2,
                               download_bytes=1, zip_uncompressed_bytes=1,
                               active_zip_downloads=1)
        self.start_server(limits=limits)
        job_id, result = self._completed_tower_job()
        artifact_id = result["artifacts"][0]["id"]
        self.assert_error(self.request("GET", "/api/jobs/{}/download/{}".format(job_id, artifact_id)),
                          413, "download_too_large")
        self.assert_error(self.request("GET", "/api/jobs/{}/download/bundle".format(job_id)),
                          413, "zip_too_large")

    def test_rerun_archives_previous_attempt_and_failed_rerun_has_no_current_download(self):
        self.start_server()
        job_id, completed = self._completed_tower_job()
        row = self.rows(completed)[0]
        first_artifact = completed["artifacts"][0]["id"]
        template_key = row["key"]
        response = self.request("POST", "/api/jobs/{}/rerun/{}".format(job_id, template_key), json_body={
            "defaults": {"ratio": 0, "strategy": "top_first", "max_tiles_per_layer": 1,
                         "max_added_layers": 0, "seed": 0, "naming_rule": "lessT{原模板ID}"}
        })
        self.assertEqual(response[0], 202)
        rerun = self.wait_job(job_id)
        current = self.rows(rerun)[0]
        self.assertEqual(current["status"], "failed")
        self.assertEqual(current.get("artifacts", []), [])
        self.assertTrue(current["attempts"])
        self.assertEqual(current["attempts"][0]["artifact_ids"], [first_artifact])
        self.assert_error(self.request("GET", "/api/jobs/{}/download/{}".format(job_id, first_artifact)),
                          404, "artifact_not_found")

    def test_rerun_can_change_naming_rule_or_set_one_template_id_override(self):
        self.start_server()
        job_id, completed = self._completed_tower_job()
        row = self.rows(completed)[0]
        response = self.request("POST", "/api/jobs/{}/rerun/{}".format(job_id, row["key"]), json_body={
            "defaults": {"ratio": 0.25, "strategy": "top_first", "max_tiles_per_layer": 24,
                         "max_added_layers": 3, "seed": 0,
                         "naming_rule": "batch_{\u539f\u6a21\u677fID}", "new_id": "one_template_override"}
        })
        self.assertEqual(response[0], 202)
        rerun = self.wait_job(job_id)
        self.assertEqual(rerun["artifacts"][0]["new_id"], "one_template_override")
        self.assertEqual(rerun["artifacts"][0]["filename"], "one_template_override_m.bytes")


class NamingValidationTests(ApiContractCase):
    BAD_RULES = ("新模板{原模板ID}", "less T{原模板ID}", "x{原模板ID}{原模板ID}")

    def test_invalid_settings_naming_is_rejected_without_replacing_saved_settings(self):
        self.start_server()
        before = self.settings_path.read_bytes()
        for rule in self.BAD_RULES:
            with self.subTest(rule=rule):
                response = self.request("PUT", "/api/settings", json_body={"naming_rule": rule})
                self.assert_error(response, 400, "invalid_naming_rule")
                self.assertIn("命名", response[2]["error"]["message"])
                self.assertEqual(self.settings_path.read_bytes(), before)

    def test_default_sync_choices_round_trip_but_never_start_sync(self):
        self.start_server()
        for targets in ([], ['template'], ['tile'], ['template', 'tile']):
            response = self.request('PUT', '/api/settings', json_body={'default_sync_targets': targets})
            self.assertEqual(response[0], 200)
            fetched = self.request('GET', '/api/settings')[2]['settings']
            self.assertEqual(fetched['default_sync_targets'], targets)
            self.assertFalse(fetched['sync_ready'])
            self.assertEqual(self.sync_service.plans, [])
            self.assertEqual(self.sync_service.commits, [])
        before = self.settings_path.read_bytes()
        for invalid in ('tile', ['other'], ['tile', 'tile'], [True]):
            self.assertEqual(self.request('PUT', '/api/settings', json_body={'default_sync_targets': invalid})[0], 400)
            self.assertEqual(self.settings_path.read_bytes(), before)

    def test_legal_settings_naming_rule_is_saved(self):
        self.start_server()
        response = self.request("PUT", "/api/settings", json_body={"naming_rule": "lessT_{原模板ID}_v2"})
        self.assertEqual(response[0], 200)
        self.assertEqual(response[2]["settings"]["naming_rule"], "lessT_{原模板ID}_v2")

    def test_invalid_generation_naming_rejects_before_creating_jobs(self):
        application = self.start_server()
        raw = read_project_fixture("mxd0057v1_m.bytes")
        imported = self.import_files([("mxd0057v1_m.bytes", raw)])
        before = tuple(self.repository.root.iterdir())
        for rule in self.BAD_RULES:
            with self.subTest(rule=rule):
                response = self.request("POST", "/api/jobs", json_body={
                    "import_id": imported["import_id"], "defaults": {"naming_rule": rule}, "overrides": {}
                })
                self.assert_error(response, 400, "invalid_naming_rule")
                self.assertEqual(tuple(self.repository.root.iterdir()), before)
                self.assertEqual(application.bootstrap()["resources"]["active_jobs"], 0)

    def test_source_specific_invalid_naming_rejects_before_generation(self):
        self.start_server()
        raw = read_project_fixture("mxd0057v1_m.bytes")
        imported = self.import_files([("mxd0057v1_m.bytes", raw)])
        response = self.request("POST", "/api/jobs", json_body={
            "import_id": imported["import_id"], "defaults": {"new_id": "mxd0057v1"}, "overrides": {}
        })
        self.assert_error(response, 400, "invalid_template_name")
        self.assertIn("原模板名称", response[2]["error"]["message"])
        self.assertEqual(list(self.repository.root.iterdir()), [])

    def test_invalid_rerun_name_preserves_current_verified_artifact(self):
        self.start_server()
        raw = read_project_fixture("mxd0057v1_m.bytes")
        imported = self.import_files([("mxd0057v1_m.bytes", raw)])
        job_id = self.start_job(imported["import_id"])
        completed = self.wait_job(job_id)
        row = self.rows(completed)[0]
        before = self.repository.get(job_id).snapshot()
        response = self.request("POST", "/api/jobs/{}/rerun/{}".format(job_id, row["key"]),
                                json_body={"defaults": {"new_id": "mxd0057v1"}})
        self.assert_error(response, 400, "invalid_template_name")
        self.assertEqual(self.repository.get(job_id).snapshot(), before)
        self.assertEqual(self.request("GET", "/api/jobs/{}/download/{}".format(
            job_id, completed["artifacts"][0]["id"]))[0], 200)

    def test_worker_retains_naming_reason_and_isolates_invalid_template(self):
        from scripts.tower_reducer.storage import InputSnapshot
        from unittest.mock import patch
        application = self.start_server()
        job = self.repository.create([
            InputSnapshot(read_project_fixture("mxd0057v1_m.bytes"), "mxd0057v1_m.bytes"),
            InputSnapshot(read_project_fixture("hr0029_m.bytes"), "hr0029_m.bytes"),
        ])
        keys = tuple(job.snapshot()["templates"])
        job.update_template(keys[0], parameters={"naming_rule": "新模板{原模板ID}"})
        from scripts.tower_reducer import api as api_module
        original = api_module.plan_tower_distribution
        with patch.object(api_module, "plan_tower_distribution", wraps=original) as planning:
            application._run_job(job, keys)
        rows = job.snapshot()["templates"]
        self.assertEqual(rows[keys[0]]["status"], "failed")
        self.assertEqual(rows[keys[0]]["stage"], "naming")
        self.assertEqual(rows[keys[0]]["error"]["code"], "invalid_template_name")
        self.assertIn("英文字母", rows[keys[0]]["error"]["message"])
        self.assertTrue(rows[keys[0]]["error"]["recommendations"])
        self.assertEqual(rows[keys[1]]["status"], "verified")
        self.assertEqual(job.snapshot()["status"], "partial_failed")
        self.assertEqual(planning.call_count, 1)


class SettingsExportAndSyncTests(ApiContractCase):
    def test_completed_export_issues_a_directory_button_not_an_arbitrary_path_launcher(self):
        application = self.start_server()
        opened = []
        application.directory_actions.opener = opened.append
        job_id, _ = self._complete_job()
        plan = self.request('POST', '/api/jobs/{}/export'.format(job_id), json_body={'action':'preview'})[2]
        self.assertNotIn('directories', plan)
        self.assertEqual(opened, [])
        result = self.request('POST', '/api/jobs/{}/export'.format(job_id), json_body={
            'action':'commit','plan_id':plan['plan_id'], 'confirmation_token':plan['confirmation_token'], 'conflict_decisions':{}})[2]
        self.assertTrue(result['ok'])
        action = result['directories'][0]
        self.assertEqual(action['path'], str(self.export_root))
        self.assertEqual(opened, [])
        for bad in ({'path':str(self.export_root)}, {'key':'invented'}, {'key':action['key'],'path':'/'}):
            self.assertGreaterEqual(self.request('POST','/api/open-directory',json_body=bad)[0],400)
        self.assertEqual(self.request('POST','/api/open-directory',json_body={'key':action['key']},csrf=False)[0],403)
        self.assertEqual(self.request('POST','/api/open-directory',json_body={'key':action['key']})[2],{'opened':True})
        self.assertEqual(opened,[self.export_root])

    def _make_bindings(self, prefix="work"):
        base = self.discovery_root / prefix
        tile = base / TILE_SUFFIX
        template = base / TEMPLATE_SUFFIX
        tile.mkdir(parents=True)
        template.mkdir(parents=True)
        return tile, template

    def test_settings_discovers_fixed_suffixes_requires_confirmation_and_path_change_revokes_it(self):
        tile, template = self._make_bindings()
        self.start_server()
        status, _, initial, _ = self.request("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertIn(str(tile), initial["candidates"]["tile"])
        self.assertIn(str(template), initial["candidates"]["template"])
        self.assertFalse(initial["settings"]["tile_confirmed"])
        self.assertFalse(initial["settings"]["template_confirmed"])
        confirmed = self.request("PUT", "/api/settings", json_body={
            "tile_root": str(tile), "template_root": str(template),
            "tile_confirmed": True, "template_confirmed": True,
        })
        self.assertEqual(confirmed[0], 200)
        self.assertTrue(confirmed[2]["settings"]["sync_ready"])
        tile2, _ = self._make_bindings("other")
        changed = self.request("PUT", "/api/settings", json_body={"tile_root": str(tile2)})
        self.assertEqual(changed[0], 200)
        self.assertFalse(changed[2]["settings"]["tile_confirmed"])
        self.assertTrue(changed[2]["settings"]["template_confirmed"])
        before = self.settings_path.read_bytes()
        wrong = self.root / "not-a-template-root"
        wrong.mkdir()
        rejected = self.request("PUT", "/api/settings", json_body={
            "tile_root": str(wrong), "tile_confirmed": True,
        })
        self.assert_error(rejected, 400, "binding_invalid")
        self.assertEqual(self.settings_path.read_bytes(), before)

    def test_bad_settings_fail_closed_and_cannot_be_overwritten_without_explicit_recovery(self):
        self.settings_path.write_text("{broken-json", encoding="utf-8")
        self.start_server()
        current = self.request("GET", "/api/settings")
        self.assertEqual(current[0], 200)
        self.assertTrue(current[2]["load_error"])
        response = self.request("PUT", "/api/settings", json_body={"naming_rule": "x{原模板ID}"})
        self.assert_error(response, 409, "settings_recovery_required")
        self.assertEqual(self.settings_path.read_text(encoding="utf-8"), "{broken-json")
        recovered = self.request("PUT", "/api/settings", json_body={
            "recover": True, "naming_rule": "x{原模板ID}", "export_root": str(self.export_root)
        })
        self.assertEqual(recovered[0], 200)
        self.assertFalse(recovered[2]["load_error"])

    def _complete_job(self):
        tower, _ = synthetic()
        imported = self.import_files([("tower_m.bytes", tower)])
        job_id = self.start_job(imported["import_id"])
        return job_id, self.wait_job(job_id)

    def test_export_is_two_phase_token_bound_single_use_and_detects_target_change(self):
        clock = [1000.0]
        self.start_server(clock=lambda: clock[0])
        job_id, completed = self._complete_job()
        preview = self.request("POST", "/api/jobs/{}/export".format(job_id), json_body={"action": "preview"})
        self.assertEqual(preview[0], 200)
        plan = preview[2]
        self.assertRegex(plan["plan_id"], r"^export-[0-9a-f]{32}$")
        self.assertGreater(plan["expires_at"], clock[0])
        self.assertTrue(plan["confirmation_token"])
        self.assertEqual(list(self.export_root.iterdir()), [])
        forged = self.request("POST", "/api/jobs/{}/export".format(job_id), json_body={
            "action": "commit", "plan_id": plan["plan_id"],
            "confirmation_token": "forged-token", "conflict_decisions": {}
        })
        self.assert_error(forged, 409, "plan_expired")
        self.assertEqual(list(self.export_root.iterdir()), [])
        target = self.export_root / plan["items"][0]["filename"]
        target.write_bytes(b"changed-after-preview")
        changed = self.request("POST", "/api/jobs/{}/export".format(job_id), json_body={
            "action": "commit", "plan_id": plan["plan_id"],
            "confirmation_token": plan["confirmation_token"], "conflict_decisions": {}
        })
        self.assert_error(changed, 409, "target_changed")
        target.unlink()
        fresh = self.request("POST", "/api/jobs/{}/export".format(job_id), json_body={"action": "preview"})[2]
        committed = self.request("POST", "/api/jobs/{}/export".format(job_id), json_body={
            "action": "commit", "plan_id": fresh["plan_id"],
            "confirmation_token": fresh["confirmation_token"], "conflict_decisions": {}
        })
        self.assertEqual(committed[0], 200)
        replay = self.request("POST", "/api/jobs/{}/export".format(job_id), json_body={
            "action": "commit", "plan_id": fresh["plan_id"],
            "confirmation_token": fresh["confirmation_token"], "conflict_decisions": {}
        })
        self.assert_error(replay, 409, "plan_expired")

    def test_export_preview_may_pin_a_batch_export_root_without_changing_settings(self):
        self.start_server()
        job_id, _ = self._complete_job()
        temporary_root = self.root / "one-batch-output"
        before = self.settings_path.read_bytes()
        preview = self.request("POST", "/api/jobs/{}/export".format(job_id), json_body={
            "action": "preview", "export_root": str(temporary_root)
        })
        self.assertEqual(preview[0], 200)
        self.assertEqual(preview[2]["export_root"], str(temporary_root))
        self.assertEqual(self.settings_path.read_bytes(), before)
        committed = self.request("POST", "/api/jobs/{}/export".format(job_id), json_body={
            "action": "commit", "plan_id": preview[2]["plan_id"],
            "confirmation_token": preview[2]["confirmation_token"], "conflict_decisions": {}
        })
        self.assertEqual(committed[0], 200)
        self.assertTrue(any(temporary_root.iterdir()))
        self.assert_error(self.request("POST", "/api/jobs/{}/export".format(job_id), json_body={
            "action": "preview", "export_root": "relative/output"
        }), 400, "invalid_export_root")

    def test_export_conflict_automatically_versions_file_and_internal_id(self):
        self.start_server()
        job_id, completed = self._complete_job()
        filename = completed["artifacts"][0]["filename"]
        (self.export_root / filename).write_bytes(b"old")
        plan = self.request("POST", "/api/jobs/{}/export".format(job_id),
                            json_body={"action": "preview"})[2]
        versioned = plan["items"][0]
        self.assertEqual(versioned["classification"], "create")
        self.assertEqual(versioned["filename"], filename.removesuffix('_m.bytes')+'v2_m.bytes')
        self.assertTrue(versioned['renamed'])
        committed = self.request("POST", "/api/jobs/{}/export".format(job_id), json_body={
            "action": "commit", "plan_id": plan["plan_id"],
            "confirmation_token": plan["confirmation_token"],
            "conflict_decisions": {},
        })
        self.assertEqual(committed[0], 200)
        self.assertEqual(committed[2]["items"][0]["status"], "exported")
        self.assertFalse(committed[2]["items"][0]["backup"])
        self.assertEqual((self.export_root/filename).read_bytes(),b'old')
        from scripts.tower_reducer.template_reader import decode_template
        new_id=versioned['filename'].removesuffix('_m.bytes')
        self.assertEqual(decode_template((self.export_root/versioned['filename']).read_bytes()).template_id,new_id)
        current=self.request('GET','/api/jobs/'+job_id)[2]
        self.assertEqual(current['artifacts'][0]['new_id'],new_id)

    def test_export_versions_multiple_conflicts_independently(self):
        self.start_server()
        imported = self.import_files([
            ("mxd0057v1_m.bytes", read_project_fixture("mxd0057v1_m.bytes")),
            ("hr0044_m.bytes", read_project_fixture("hr0044_m.bytes")),
        ])
        job_id = self.start_job(imported["import_id"])
        completed = self.wait_job(job_id)
        self.assertEqual(len(completed["artifacts"]), 2)
        for artifact in completed["artifacts"]:
            (self.export_root / artifact["filename"]).write_bytes(b"keep-existing-" + artifact["id"].encode())
        plan = self.request("POST", "/api/jobs/{}/export".format(job_id),
                            json_body={"action": "preview"})[2]
        conflicts = [item for item in plan["items"] if item.get("renamed")]
        self.assertEqual(len(conflicts), 2)
        kept = {artifact['filename']:(self.export_root/artifact['filename']).read_bytes() for artifact in completed['artifacts']}
        committed = self.request("POST", "/api/jobs/{}/export".format(job_id), json_body={
            "action": "commit", "plan_id": plan["plan_id"],
            "confirmation_token": plan["confirmation_token"], "conflict_decisions": {},
        })
        self.assertEqual(committed[0], 200)
        items = {row["filename"]: row for row in committed[2]["items"]}
        self.assertEqual(items[conflicts[0]["filename"]]["status"], "exported")
        self.assertEqual(items[conflicts[1]["filename"]]["status"], "exported")
        self.assertEqual({name:(self.export_root/name).read_bytes() for name in kept},kept)
        persisted = self.repository.get(job_id).snapshot()["exports"][-1]
        persisted_statuses = {row["relative_name"]: row["status"] for row in persisted}
        self.assertEqual(persisted_statuses, {
            conflicts[0]["filename"]: "exported",
            conflicts[1]["filename"]: "exported",
        })

    def test_same_export_confirmation_token_cannot_enter_mutation_twice_concurrently(self):
        self.start_server()
        job_id, _ = self._complete_job()
        plan = self.request("POST", "/api/jobs/{}/export".format(job_id),
                            json_body={"action": "preview"})[2]
        from scripts.tower_reducer import api as api_module
        original = api_module.export_outputs
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def blocked(*args, **kwargs):
            calls.append(1)
            entered.set()
            release.wait(3)
            return original(*args, **kwargs)

        payload = {
            "action": "commit", "plan_id": plan["plan_id"],
            "confirmation_token": plan["confirmation_token"], "conflict_decisions": {},
        }
        results = []
        with patch.object(api_module, "export_outputs", side_effect=blocked):
            first = threading.Thread(target=lambda: results.append(
                self.request("POST", "/api/jobs/{}/export".format(job_id), json_body=payload)))
            first.start()
            self.assertTrue(entered.wait(2))
            second = self.request("POST", "/api/jobs/{}/export".format(job_id), json_body=payload)
            release.set()
            first.join(timeout=4)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted([results[0][0], second[0]]), [200, 409])
        self.assertEqual((results[0] if results[0][0] == 409 else second)[2]["error"]["code"],
                         "plan_expired")

    def test_expired_and_consumed_confirmation_plans_are_cleaned_and_bounded(self):
        _, api = self._modules()
        clock = [1000.0]
        limits = api.ApiLimits(confirmation_plans=2)
        self._make_bindings()
        self.start_server(limits=limits, clock=lambda: clock[0])
        job_id, _ = self._complete_job()
        tile = self.discovery_root / "work" / TILE_SUFFIX
        template = self.discovery_root / "work" / TEMPLATE_SUFFIX
        self.assertEqual(self.request("PUT", "/api/settings", json_body={
            "tile_root": str(tile), "template_root": str(template),
            "tile_confirmed": True, "template_confirmed": True,
        })[0], 200)
        export_plan = self.request("POST", "/api/jobs/{}/export".format(job_id),
                                   json_body={"action": "preview"})[2]
        sync_plan = self.request("POST", "/api/sync/plan", json_body={
            "job_id": job_id, "bindings": ["tile"],
        })[2]
        self.assertEqual(self.request("GET", "/api/bootstrap")[2]["resources"]["confirmation_plans"], 2)
        blocked = self.request("POST", "/api/jobs/{}/export".format(job_id),
                               json_body={"action": "preview"})
        self.assert_error(blocked, 429, "plan_busy")
        clock[0] = max(export_plan["expires_at"], sync_plan["expires_at"]) + 1
        fresh = self.request("POST", "/api/jobs/{}/export".format(job_id),
                             json_body={"action": "preview"})
        self.assertEqual(fresh[0], 200)
        self.assertEqual(self.request("GET", "/api/bootstrap")[2]["resources"]["confirmation_plans"], 1)

    def test_export_plan_is_bound_to_the_job_revision(self):
        self.start_server()
        job_id, completed = self._complete_job()
        row = self.rows(completed)[0]
        plan = self.request("POST", "/api/jobs/{}/export".format(job_id),
                            json_body={"action": "preview"})[2]
        rerun = self.request("POST", "/api/jobs/{}/rerun/{}".format(job_id, row["key"]),
                             json_body={"defaults": row["parameters"]})
        self.assertEqual(rerun[0], 202)
        self.wait_job(job_id)
        stale = self.request("POST", "/api/jobs/{}/export".format(job_id), json_body={
            "action": "commit", "plan_id": plan["plan_id"],
            "confirmation_token": plan["confirmation_token"], "conflict_decisions": {},
        })
        self.assert_error(stale, 409, "plan_stale")

    def test_sync_api_accepts_only_logical_bindings_and_tokens_are_plan_bound(self):
        self._make_bindings()
        self.start_server()
        job_id, _ = self._complete_job()
        for injected in ({"job_id": job_id, "bindings": ["tile"], "target_path": "/tmp/x"},
                         {"job_id": job_id, "bindings": {"template": "../../outside"}},
                         {"job_id": job_id, "bindings": ["unknown"]}):
            with self.subTest(injected=injected):
                self.assert_error(self.request("POST", "/api/sync/plan", json_body=injected),
                                  400, "invalid_request")
        # Confirm paths through settings, not through sync.
        tile = self.discovery_root / "work" / TILE_SUFFIX
        template = self.discovery_root / "work" / TEMPLATE_SUFFIX
        self.assertEqual(self.request("PUT", "/api/settings", json_body={
            "tile_root": str(tile), "template_root": str(template),
            "tile_confirmed": True, "template_confirmed": True,
        })[0], 200)
        preview = self.request("POST", "/api/sync/plan", json_body={
            "job_id": job_id, "bindings": ["template", "tile"]
        })
        self.assertEqual(preview[0], 200)
        self.assertEqual(self.sync_service.plans, [(job_id, ("template", "tile"))])
        token = preview[2]["confirmation_token"]
        self.assert_error(self.request("POST", "/api/sync/commit", json_body={
            "plan_id": preview[2]["plan_id"], "confirmation_token": "forged",
            "conflict_confirmations": {},
        }), 409, "plan_expired")
        committed = self.request("POST", "/api/sync/commit", json_body={
            "plan_id": preview[2]["plan_id"], "confirmation_token": token,
            "conflict_confirmations": {},
        })
        self.assertEqual(committed[0], 200)
        self.assertEqual(self.sync_service.commits[0][0], preview[2]["plan_id"])
        self.assert_error(self.request("POST", "/api/sync/commit", json_body={
            "plan_id": preview[2]["plan_id"], "confirmation_token": token,
            "conflict_confirmations": {},
        }), 409, "plan_expired")

    def test_same_sync_confirmation_token_is_reserved_atomically_and_correctable_preflight_releases_it(self):
        self._make_bindings()
        sync = BlockingSyncService()
        self.start_server(sync_service=sync)
        job_id, _ = self._complete_job()
        tile = self.discovery_root / "work" / TILE_SUFFIX
        template = self.discovery_root / "work" / TEMPLATE_SUFFIX
        self.assertEqual(self.request("PUT", "/api/settings", json_body={
            "tile_root": str(tile), "template_root": str(template),
            "tile_confirmed": True, "template_confirmed": True,
        })[0], 200)
        preview = self.request("POST", "/api/sync/plan", json_body={
            "job_id": job_id, "bindings": ["tile"],
        })[2]
        payload = {"plan_id": preview["plan_id"],
                   "confirmation_token": preview["confirmation_token"],
                   "conflict_confirmations": {}}
        results = []
        first = threading.Thread(target=lambda: results.append(
            self.request("POST", "/api/sync/commit", json_body=payload)))
        first.start()
        self.assertTrue(sync.commit_entered.wait(2))
        second = self.request("POST", "/api/sync/commit", json_body=payload)
        sync.commit_release.set()
        first.join(timeout=4)
        self.assertEqual(len(sync.commits), 1)
        self.assertEqual(sorted([results[0][0], second[0]]), [200, 409])

        self._stop_server()
        confirmation = ConfirmationSyncService()
        self.start_server(sync_service=confirmation)
        preview = self.request("POST", "/api/sync/plan", json_body={
            "job_id": job_id, "bindings": ["tile"],
        })[2]
        first_try = self.request("POST", "/api/sync/commit", json_body={
            "plan_id": preview["plan_id"], "confirmation_token": preview["confirmation_token"],
            "conflict_confirmations": {},
        })
        self.assertEqual(first_try[0], 200)
        self.assertEqual(first_try[2]["status"], "confirmation_required")
        retry = self.request("POST", "/api/sync/commit", json_body={
            "plan_id": preview["plan_id"], "confirmation_token": preview["confirmation_token"],
            "conflict_confirmations": {"conflict-" + "a" * 32: "backup_and_replace"},
        })
        self.assertEqual(retry[0], 200)
        self.assertEqual(retry[2]["status"], "committed")


if __name__ == "__main__":
    unittest.main()
