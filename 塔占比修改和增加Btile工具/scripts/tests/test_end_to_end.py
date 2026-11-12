"""End-to-end workflow tests for the local tower-ratio reducer.

These scenarios deliberately cross the real HTTP boundary and use the checked
in golden ``*_m.bytes`` fixtures.  Every writable path is rooted in a fresh
temporary directory.  In particular, the synchronization test binds two
temporary directories which merely end with the two production suffixes; it
never supplies either developer's real template repository to the service.

The suite opens a loopback socket through ``app.create_server``.  Sandboxes
which prohibit even local sockets must run this module with loopback permission
rather than weakening or skipping the HTTP assertions.
"""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path
import time
import unittest
from zipfile import ZipFile

from scripts.tests.project_fixtures import read_project_fixture
from scripts.tests.test_api import ApiContractCase
from scripts.tower_reducer.settings import load_settings
from scripts.tower_reducer.storage import JobRepository
from scripts.tower_reducer.sync_service import SyncService
from scripts.tower_reducer.template_reader import decode_template

TERMINAL = {"succeeded", "partial_failed", "failed", "interrupted"}


class TowerReducerEndToEndTests(ApiContractCase):
    """Exercise import, generation, delivery, sync and restart as one product."""

    def start_real_server(self, *, repository=None):
        repository = repository or self.repository
        sync = SyncService(repository, settings_path=self.settings_path)
        return self.start_server(repository=repository, sync_service=sync)

    @staticmethod
    def fixture(name):
        return read_project_fixture(name)

    def post_json(self, path, value, expected=200):
        status, headers, payload, raw = self.request("POST", path, json_body=value)
        self.assertEqual(status, expected, raw[:1000])
        return payload

    def put_json(self, path, value, expected=200):
        status, headers, payload, raw = self.request("PUT", path, json_body=value)
        self.assertEqual(status, expected, raw[:1000])
        return payload

    def wait_terminal(self, job_id, timeout=15):
        deadline = time.monotonic() + timeout
        revisions = []
        while time.monotonic() < deadline:
            status, _, payload, raw = self.request("GET", "/api/jobs/" + job_id)
            self.assertEqual(status, 200, raw[:1000])
            revisions.append(payload["revision"])
            if payload["status"] in TERMINAL:
                self.assertEqual(revisions, sorted(revisions))
                return payload
            time.sleep(0.02)
        self.fail("background job did not reach a terminal state")

    @staticmethod
    def rows_by_source(job):
        return {row.get("source_template_id"): row for row in job["templates"].values()}

    def create_job_from(self, files):
        imported = self.import_files(files)
        job_id = self.start_job(imported["import_id"])
        return imported, job_id, self.wait_terminal(job_id)

    def assert_artifact_downloads_and_zip(self, job_id, job):
        expected = {}
        for artifact in job["artifacts"]:
            status, headers, payload, raw = self.request(
                "GET", "/api/jobs/{}/download/{}".format(job_id, artifact["id"])
            )
            self.assertEqual(status, 200)
            self.assertIsNone(payload)
            self.assertEqual(sha256(raw).hexdigest(), artifact["sha256"])
            self.assertEqual(len(raw), artifact["size"])
            self.assertEqual(decode_template(raw).template_id, artifact["new_id"])
            self.assertIn(artifact["filename"], headers["content-disposition"])
            expected[artifact["filename"]] = raw

        status, headers, payload, raw = self.request(
            "GET", "/api/jobs/{}/download/bundle".format(job_id)
        )
        self.assertEqual(status, 200)
        self.assertIsNone(payload)
        self.assertTrue(headers["content-type"].startswith("application/zip"))
        with ZipFile(BytesIO(raw), "r") as archive:
            self.assertEqual(set(archive.namelist()), set(expected))
            self.assertEqual({name: archive.read(name) for name in archive.namelist()}, expected)
        return expected

    def test_mixed_golden_batch_isolates_bad_file_and_skips_no_tower_output(self):
        self.start_real_server()
        imported, job_id, job = self.create_job_from([
            ("mxd0057v1_m.bytes", self.fixture("mxd0057v1_m.bytes")),
            ("ntxkd021_m.bytes", self.fixture("ntxkd021_m.bytes")),
            ("broken_m.bytes", b"definitely-not-a-template"),
        ])

        imported_by_name = {row["original_name"]: row for row in imported["files"]}
        self.assertEqual(imported_by_name["mxd0057v1_m.bytes"]["status"], "ready")
        self.assertGreater(imported_by_name["mxd0057v1_m.bytes"]["inspection"]["tower_count"], 0)
        self.assertEqual(imported_by_name["ntxkd021_m.bytes"]["status"], "no_tower")
        self.assertEqual(imported_by_name["ntxkd021_m.bytes"]["inspection"]["tower_count"], 0)
        self.assertEqual(imported_by_name["broken_m.bytes"]["status"], "failed")

        self.assertEqual(job["status"], "partial_failed")
        self.assertEqual(job["progress"], {"completed_templates": 3, "total_templates": 3})
        self.assertEqual(len(job["artifacts"]), 1)
        rows = self.rows_by_source(job)
        tower = rows["mxd0057v1"]
        no_tower = rows["ntxkd021"]
        failed = rows[None]
        for row in (tower,):
            self.assertEqual(row["status"], "verified")
            self.assertTrue(row["verification"]["ok"])
            self.assertEqual(row["verification"]["status"], "passed")
            self.assertEqual(row["plan_summary"]["status"], "verified")
            self.assertEqual(len(row["artifacts"]), 1)
        self.assertLess(tower["plan_summary"]["result_tower_ratio"], 0.25)
        self.assertGreater(tower["plan_summary"]["move_count"], 0)
        self.assertEqual(no_tower["plan_summary"]["move_count"], 0)
        self.assertEqual(no_tower['status'],'no_op')
        self.assertEqual(no_tower['artifacts'],[])
        self.assertEqual(no_tower["plan_summary"]["result_tower_ratio"], 0.0)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["stage"], "import")
        self.assertEqual(failed["error"]["code"], "invalid_template")
        self.assertEqual(failed["artifacts"], [])

        self.assert_artifact_downloads_and_zip(job_id, job)

    def test_complete_workflow_exports_conflicts_syncs_only_temp_roots_and_survives_restart(self):
        self.start_real_server()
        imported, job_id, job = self.create_job_from([
            ("mxd0057v1_m.bytes", self.fixture("mxd0057v1_m.bytes")),
            ("ntxkd021_m.bytes", self.fixture("ntxkd021_m.bytes")),
        ])
        self.assertEqual([row["status"] for row in imported["files"]], ["ready", "no_tower"])
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["progress"], {"completed_templates": 2, "total_templates": 2})
        expected = self.assert_artifact_downloads_and_zip(job_id, job)

        # Export creates only the changed template; the no-tower source is skipped.
        export_root = self.root / "batch-export"
        export_root.mkdir()
        preview = self.post_json(
            "/api/jobs/{}/export".format(job_id),
            {"action": "preview", "export_root": str(export_root)},
        )
        self.assertEqual({row["classification"] for row in preview["items"]}, {"create", "no_op"})
        committed = self.post_json(
            "/api/jobs/{}/export".format(job_id),
            {"action": "commit", "plan_id": preview["plan_id"],
             "confirmation_token": preview["confirmation_token"], "conflict_decisions": {}},
        )
        self.assertTrue(committed["ok"])
        self.assertEqual({row["status"] for row in committed["items"]}, {"exported", "skipped"})
        self.assertEqual({path.name for path in export_root.iterdir()}, set(expected))

        # Every repeat, including a different-content original, creates v2/v3.
        conflict_name = sorted(expected)[0]
        conflict_path = export_root / conflict_name
        conflict_path.chmod(0o600)
        conflict_bytes = b"external-conflict-kept-by-skip"
        conflict_path.write_bytes(conflict_bytes)
        skip_preview = self.post_json(
            "/api/jobs/{}/export".format(job_id),
            {"action": "preview", "export_root": str(export_root)},
        )
        conflict = next(row for row in skip_preview["items"] if row.get('renamed'))
        self.assertEqual(conflict["filename"], conflict_name.removesuffix('_m.bytes')+'v2_m.bytes')
        skipped = self.post_json(
            "/api/jobs/{}/export".format(job_id),
            {"action": "commit", "plan_id": skip_preview["plan_id"],
             "confirmation_token": skip_preview["confirmation_token"],
             "conflict_decisions": {}},
        )
        self.assertTrue(skipped["ok"])
        self.assertEqual(conflict_path.read_bytes(), conflict_bytes)
        self.assertEqual(next(row for row in skipped["items"]
                              if row["filename"] == conflict['filename'])["status"], "exported")

        replace_preview = self.post_json(
            "/api/jobs/{}/export".format(job_id),
            {"action": "preview", "export_root": str(export_root)},
        )
        conflict = next(row for row in replace_preview["items"] if row.get('renamed'))
        self.assertEqual(conflict['filename'],conflict_name.removesuffix('_m.bytes')+'v3_m.bytes')
        replaced = self.post_json(
            "/api/jobs/{}/export".format(job_id),
            {"action": "commit", "plan_id": replace_preview["plan_id"],
             "confirmation_token": replace_preview["confirmation_token"],
             "conflict_decisions": {}},
        )
        replaced_row = next(row for row in replaced["items"] if row["filename"] == conflict['filename'])
        self.assertEqual(replaced_row["status"], "exported")
        self.assertIsNone(replaced_row['backup'])
        self.assertEqual(conflict_path.read_bytes(), conflict_bytes)
        job=self.request('GET','/api/jobs/'+job_id)[2]
        expected=self.assert_artifact_downloads_and_zip(job_id,job)

        # Bind only temporary look-alike repositories with the exact required
        # suffixes.  A decoy directory proves that no unbound location is used.
        tile_root = self.root / "colleague-a" / "tile" / "Tile" / "Assets" / "Resources" / "TileMatchLevelData"
        template_root = self.root / "colleague-b" / "tileleveltemplate" / "Template"
        tile_root.mkdir(parents=True)
        template_root.mkdir(parents=True)
        decoy = self.root / "not-a-binding"
        decoy.mkdir()
        marker = decoy / "must-not-change.txt"
        marker.write_text("untouched", encoding="utf-8")
        settings = self.put_json("/api/settings", {
            "tile_root": str(tile_root), "template_root": str(template_root),
            "tile_confirmed": True, "template_confirmed": True,
        })["settings"]
        self.assertTrue(settings["sync_ready"])
        self.assertEqual(load_settings(self.settings_path).tile_root, str(tile_root))

        sync_preview = self.post_json("/api/sync/plan", {
            "job_id": job_id, "bindings": ["tile", "template"],
        })
        self.assertEqual(sync_preview["job_id"], job_id)
        self.assertEqual(set(sync_preview["selected_bindings"]), {"tile", "template"})
        configured = {str(tile_root), str(template_root)}
        for row in sync_preview["rows"]:
            self.assertEqual(row["status"], "planned")
            self.assertEqual({target["binding_path"] for target in row["targets"]}, configured)
            self.assertEqual({target["classification"] for target in row["targets"]}, {"create"})
            for target in row["targets"]:
                self.assertTrue(Path(target["binding_path"]).is_relative_to(self.root))

        sync_result = self.post_json("/api/sync/commit", {
            "plan_id": sync_preview["plan_id"],
            "confirmation_token": sync_preview["confirmation_token"],
            "conflict_confirmations": {},
        })
        self.assertEqual(sync_result["status"], "committed")
        self.assertTrue(sync_result["ok"])
        self.assertEqual({row["status"] for row in sync_result["rows"]}, {"synced"})
        for root in (tile_root, template_root):
            self.assertEqual({path.name for path in root.iterdir()}, set(expected))
            self.assertEqual({path.name: path.read_bytes() for path in root.iterdir()}, expected)
        self.assertEqual(marker.read_text(encoding="utf-8"), "untouched")
        self.assertEqual(list(decoy.iterdir()), [marker])

        # A fresh Application/HTTP server over the same durable repository can
        # still list and download the completed job after startup recovery.
        self._stop_server()
        restarted_repository = JobRepository(self.runtime_root / "任务")
        self.repository = restarted_repository
        self.start_real_server(repository=restarted_repository)
        status, _, bootstrap, raw = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200, raw[:1000])
        self.assertIn(job_id, {row["job_id"] for row in bootstrap["recent_jobs"]})
        self.assertFalse(bootstrap["recovery"]["sync_locked"])
        status, _, restored, raw = self.request("GET", "/api/jobs/" + job_id)
        self.assertEqual(status, 200, raw[:1000])
        self.assertEqual(restored["status"], "succeeded")
        self.assertEqual({row["sha256"] for row in restored["artifacts"]},
                         {row["sha256"] for row in job["artifacts"]})
        self.assert_artifact_downloads_and_zip(job_id, restored)


if __name__ == "__main__":
    unittest.main()
