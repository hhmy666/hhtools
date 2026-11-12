"""Template ID import uses confirmed repositories and retains per-ID failures."""

import json
import unittest

from scripts.tests.project_fixtures import read_project_fixture
from scripts.tests.test_api import ApiContractCase
from scripts.tower_reducer.api import ApiLimits
from scripts.tower_reducer.path_binding import TEMPLATE_SUFFIX, TILE_SUFFIX
from scripts.tests.test_source_geometry_preservation import historical_source


class TemplateIdImportTests(ApiContractCase):
    def test_bulk_chunks_from_different_confirmed_bindings_cannot_be_mixed(self):
        self.start_server()
        root = self.bind_repository('tile')
        self.put_fixture(root)
        first = self.import_ids(['mxd0057v1'])[2]
        other = self.bind_repository('template')
        self.put_fixture(other)
        self.assert_error(self.request('POST', '/api/import-ids', json_body={
            'template_ids': ['mxd0057v1'], 'source_repository': 'template',
            'expected_binding': first['source_binding']}), 409, 'binding_changed')
        second = self.import_ids(['mxd0057v1'], 'template')[2]
        self.assert_error(self.request('POST', '/api/imports/merge', json_body={
            'import_ids': [first['import_id'], second['import_id']]}), 409, 'binding_changed')

    def test_existing_cross_layer_source_imports_but_cannot_be_delivered_with_floating_tiles(self):
        self.start_server()
        root = self.bind_repository()
        raw = historical_source("cross_layer")
        (root / "sample_m.bytes").write_bytes(raw)
        response = self.import_ids(["sample"])
        self.assertEqual(response[0], 200, response[3])
        row = response[2]["files"][0]
        self.assertEqual(row["status"], "ready")
        self.assertTrue(any(issue["code"] == "unsupported_non_tower_tile"
                            and issue["severity"] == "warning"
                            for issue in row["inspection"]["issues"]))
        job = self.wait_job(self.start_job(response[2]["import_id"]))
        result=self.rows(job)[0]
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result['verification']['issues'] or result['error'])
        self.assertFalse(result['artifacts'])
        self.assertEqual((root / "sample_m.bytes").read_bytes(), raw)

    def bind_repository(self, source="tile", *, confirmed=True):
        suffix = TILE_SUFFIX if source == "tile" else TEMPLATE_SUFFIX
        root = self.discovery_root / "work" / suffix
        root.mkdir(parents=True, exist_ok=True)
        response = self.request("PUT", "/api/settings", json_body={
            source + "_root": str(root), source + "_confirmed": confirmed,
        })
        self.assertEqual(response[0], 200, response[3])
        return root

    def import_ids(self, template_ids, source="tile"):
        return self.request("POST", "/api/import-ids", json_body={
            "template_ids": template_ids, "source_repository": source,
        })

    @staticmethod
    def put_fixture(root, name="mxd0057v1"):
        raw = read_project_fixture(name + "_m.bytes")
        (root / (name + "_m.bytes")).write_bytes(raw)
        return raw

    def test_confirmed_id_import_can_create_independently_verified_output(self):
        self.start_server()
        root = self.bind_repository()
        raw = self.put_fixture(root)
        response = self.import_ids(["mxd0057v1"])
        self.assertEqual(response[0], 200, response[3])
        row = response[2]["files"][0]
        self.assertEqual(row["original_name"], "mxd0057v1_m.bytes")
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["inspection"]["template_id"], "mxd0057v1")
        job = self.wait_job(self.start_job(response[2]["import_id"]))
        self.assertEqual(self.rows(job)[0]["status"], "verified")
        self.assertEqual((root / "mxd0057v1_m.bytes").read_bytes(), raw)

    def test_requested_repository_is_used_without_requiring_other_repository(self):
        self.start_server()
        root = self.bind_repository("template")
        self.put_fixture(root, "hr0029")
        response = self.import_ids(["hr0029"], "template")
        self.assertEqual(response[0], 200, response[3])
        self.assertEqual(response[2]["files"][0]["inspection"]["template_id"], "hr0029")

    def test_missing_and_bad_files_preserve_failure_rows_without_losing_valid_file(self):
        self.start_server()
        root = self.bind_repository()
        self.put_fixture(root)
        (root / "bad_m.bytes").write_bytes(b"broken")
        response = self.import_ids(["missing", "mxd0057v1", "bad", "missing2"])
        self.assertEqual(response[0], 200, response[3])
        rows = response[2]["files"]
        self.assertEqual([row["status"] for row in rows], ["failed", "ready", "failed", "failed"])
        self.assertEqual(rows[0]["error"]["code"], "template_not_found")
        self.assertEqual(rows[2]["error"]["code"], "invalid_template")
        job = self.wait_job(self.start_job(response[2]["import_id"]))
        self.assertEqual(len(self.rows(job)), 4)
        self.assertEqual(sum(row["status"] == "verified" for row in self.rows(job)), 1)

    def test_repeated_ids_are_imported_once_in_first_seen_order(self):
        self.start_server(limits=ApiLimits(file_count=2))
        root = self.bind_repository()
        self.put_fixture(root)
        self.put_fixture(root, "hr0029")
        response = self.import_ids(["hr0029", "mxd0057v1", "hr0029", "mxd0057v1"])
        self.assertEqual(response[0], 200, response[3])
        self.assertEqual([row["original_name"] for row in response[2]["files"]],
                         ["hr0029_m.bytes", "mxd0057v1_m.bytes"])

    def test_invalid_ids_fail_individually_without_accessing_outside_repository(self):
        self.start_server()
        root = self.bind_repository()
        self.put_fixture(root)
        for template_id in ("../mxd0057v1", "x/y", "x\\y", "mxd0057v1_m.bytes",
                            "", "中文", "x" * 181):
            with self.subTest(template_id=template_id):
                response = self.import_ids([template_id, "mxd0057v1"])
                self.assertEqual(response[0], 200, response[3])
                self.assertEqual(response[2]["files"][0]["error"]["code"], "invalid_template_id")
                self.assertEqual(response[2]["files"][1]["status"], "ready")
        for value in (None, 1):
            self.assert_error(self.import_ids([value]), 400, "invalid_request")

    def test_missing_or_unconfirmed_binding_requires_path_confirmation(self):
        self.start_server()
        self.assert_error(self.import_ids(["mxd0057v1"]), 409, "binding_not_confirmed")
        root = self.bind_repository(confirmed=False)
        self.put_fixture(root)
        self.assert_error(self.import_ids(["mxd0057v1"]), 409, "binding_not_confirmed")

    def test_invalid_unicode_id_is_a_failed_item_not_a_batch_server_error(self):
        self.start_server()
        root = self.bind_repository()
        self.put_fixture(root)
        body = json.dumps({"template_ids": ["\ud800", "mxd0057v1"],
                           "source_repository": "tile"}).encode("ascii")
        response = self.request("POST", "/api/import-ids", body=body,
                                headers={"Content-Type": "application/json"})
        self.assertEqual(response[0], 200, response[3])
        self.assertEqual(response[2]["files"][0]["error"]["code"], "invalid_template_id")
        self.assertEqual(response[2]["files"][1]["status"], "ready")

    def test_symlink_file_is_not_followed_and_other_ids_still_import(self):
        self.start_server()
        root = self.bind_repository()
        self.put_fixture(root)
        outside = self.root / "outside_m.bytes"
        outside.write_bytes(read_project_fixture("hr0029_m.bytes"))
        (root / "linked_m.bytes").symlink_to(outside)
        response = self.import_ids(["linked", "mxd0057v1"])
        self.assertEqual(response[0], 200, response[3])
        self.assertEqual(response[2]["files"][0]["error"]["code"], "template_read_rejected")
        self.assertEqual(response[2]["files"][1]["status"], "ready")

    def test_directory_or_special_file_is_rejected_before_reading(self):
        self.start_server()
        root = self.bind_repository()
        (root / "directory_m.bytes").mkdir()
        response = self.import_ids(["directory"])
        self.assertEqual(response[0], 200, response[3])
        self.assertEqual(response[2]["files"][0]["error"]["code"], "template_read_rejected")

    def test_id_count_single_file_and_total_read_budgets_are_enforced(self):
        self.start_server(limits=ApiLimits(file_count=2, file_bytes=2050, request_bytes=2051))
        root = self.bind_repository()
        self.put_fixture(root)
        self.put_fixture(root, "hr0029")
        self.assert_error(self.import_ids(["a", "b", "c"]), 413, "too_many_files")
        (root / "large_m.bytes").write_bytes(b"x" * 2051)
        response = self.import_ids(["large", "hr0029"])
        self.assertEqual(response[0], 200, response[3])
        self.assertEqual(response[2]["files"][0]["error"]["code"], "file_too_large")
        self.assertEqual(response[2]["files"][1]["status"], "ready")
        self.assert_error(self.import_ids(["mxd0057v1", "hr0029"]), 413, "import_too_large")

    def test_id_import_shares_upload_resource_lock_and_releases_it_on_failure(self):
        application = self.start_server(limits=ApiLimits(active_uploads=1))
        root = self.bind_repository()
        self.put_fixture(root)
        application.acquire_upload()
        try:
            self.assert_error(self.import_ids(["mxd0057v1"]), 429, "upload_busy")
        finally:
            application.release_upload()
        self.assertEqual(self.import_ids(["mxd0057v1"])[0], 200)
        self.assertEqual(application.bootstrap()["resources"]["active_uploads"], 0)

    def test_id_import_json_schema_does_not_accept_arbitrary_paths(self):
        self.start_server()
        self.bind_repository()
        cases = ({"template_ids": ["x"], "source_repository": "tile", "path": "/tmp"},
                 {"template_ids": ["x"], "source_repository": "unknown"},
                 {"template_ids": [], "source_repository": "tile"},
                 {"template_ids": "x", "source_repository": "tile"})
        for payload in cases:
            with self.subTest(payload=payload):
                self.assert_error(self.request("POST", "/api/import-ids", json_body=payload),
                                  400, "invalid_request")


if __name__ == "__main__":
    unittest.main()
