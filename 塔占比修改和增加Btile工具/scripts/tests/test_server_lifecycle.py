"""Service lifecycle, restart recovery and rerun contracts for Task 9."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import threading
import time
import unittest
from unittest.mock import patch

from scripts.tests.project_fixtures import read_project_fixture
from scripts.tests.test_api import ApiContractCase, TERMINAL_JOB_STATES
from scripts.tests.test_rewrite import synthetic
from scripts.tower_reducer.path_binding import TEMPLATE_SUFFIX, TILE_SUFFIX
from scripts.tower_reducer.rewrite import rewrite_template
from scripts.tower_reducer.settings import LocalSettings, save_settings
from scripts.tower_reducer.storage import InputSnapshot, JobRepository, atomic_write_json
from scripts.tower_reducer.sync_service import SyncService


class RecoveringSyncService:
    def __init__(self):
        self.recover_calls = 0
        self.recovered = [{"plan_id": "plan-" + "c" * 32, "status": "rolled_back"}]

    def recover_pending(self):
        self.recover_calls += 1
        return list(self.recovered)

    def create_sync_plan(self, job, selected_bindings):
        return {"plan_id": "plan-" + "d" * 32, "status": "ready", "rows": []}

    def commit_sync(self, plan_id, conflict_confirmations):
        return {"plan_id": plan_id, "status": "committed", "rows": []}


class FaultingRecoverySyncService(RecoveringSyncService):
    def __init__(self, *, failure=None, recovered=(), diagnostics=()):
        super().__init__()
        self.failure = failure
        self.recovered = list(recovered)
        self.diagnostics = list(diagnostics)
        self.plan_calls = 0
        self.commit_calls = 0

    def recover_pending(self):
        self.recover_calls += 1
        if self.failure is not None:
            raise self.failure
        return list(self.recovered)

    def create_sync_plan(self, job, selected_bindings):
        self.plan_calls += 1
        return super().create_sync_plan(job, selected_bindings)

    def commit_sync(self, plan_id, conflict_confirmations):
        self.commit_calls += 1
        return super().commit_sync(plan_id, conflict_confirmations)


class ServerLifecycleTests(ApiContractCase):
    def test_dual_sync_and_same_content_issue_exact_confirmed_repository_directories(self):
        self._confirmed_bindings()
        application = self.start_server(sync_service=SyncService(self.repository,self.settings_path))
        opened=[]
        application.directory_actions.opener=opened.append
        job_id,_=self._completed_job()
        for _ in range(2):
            plan=self.request('POST','/api/sync/plan',json_body={'job_id':job_id,'bindings':['template','tile']})[2]
            self.assertNotIn('directories',plan)
            result=self.request('POST','/api/sync/commit',json_body={'plan_id':plan['plan_id'],
                'confirmation_token':plan['confirmation_token'],'conflict_confirmations':{}})[2]
            self.assertEqual(result['status'],'committed')
            actions=result['directories']
            self.assertEqual({a['label'] for a in actions},{'模板仓','Tile 仓'})
            self.assertEqual(len(actions),2)
            self.assertEqual(opened,[])
            self.assertTrue(any(t['status'] in ('synced','skipped_same_sha') for t in result['rows']))
        for action in actions:
            self.assertEqual(self.request('POST','/api/open-directory',json_body={'key':action['key']})[0],200)
        self.assertEqual(set(opened),{self.discovery_root/'work'/TEMPLATE_SUFFIX,self.discovery_root/'work'/TILE_SUFFIX})

    def _confirmed_bindings(self):
        tile = self.discovery_root / "work" / TILE_SUFFIX
        template = self.discovery_root / "work" / TEMPLATE_SUFFIX
        tile.mkdir(parents=True, exist_ok=True)
        template.mkdir(parents=True, exist_ok=True)
        save_settings(LocalSettings(
            tile_root=str(tile), template_root=str(template),
            tile_confirmed=True, template_confirmed=True,
            export_root=str(self.export_root),
        ), self.settings_path)

    def _completed_job(self):
        raw, _ = synthetic()
        imported = self.import_files([("tower_m.bytes", raw)])
        job_id = self.start_job(imported["import_id"])
        return job_id, self.wait_job(job_id)

    def test_public_server_factory_is_loopback_only_and_health_is_versioned(self):
        app, api = self._modules()
        application = api.Application(
            tool_root=self.tool_root,
            repository=self.repository,
            settings_path=self.settings_path,
            discovery_root=self.discovery_root,
            sync_service=self.sync_service,
            csrf_token=self.csrf,
        )
        with self.assertRaises(ValueError):
            app.create_server(("0.0.0.0", 0), application)
        with self.assertRaises(ValueError):
            app.create_server(("::", 0), application)
        self.start_server()
        status, headers, payload, _ = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["service_id"], app.SERVICE_ID)
        self.assertEqual(payload["protocol"], 1)
        self.assertEqual(payload["version"], app.APP_VERSION)
        self.assertEqual(payload["instance_id"], app.TOOL_INSTANCE_ID)
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["local_only"], True)
        self.assertEqual(headers["x-frame-options"], "DENY")

    def test_startup_marks_job_and_template_active_states_interrupted_without_auto_resume(self):
        raw, _ = synthetic()
        job = self.repository.create([InputSnapshot(raw, "tower_m.bytes")])
        key = next(iter(job.snapshot()["templates"]))
        job.update_template(key, status="generating", stage="generating", progress={"completed": 1, "total": 3})
        job.set_status("running")
        self.start_server()
        status, _, payload, _ = self.request("GET", "/api/jobs/" + job.job_id)
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "interrupted")
        row = self.rows(payload)[0]
        self.assertEqual(row["status"], "interrupted")
        self.assertEqual(row["interrupted_stage"], "generating")
        revision = payload["revision"]
        time.sleep(0.15)
        again = self.request("GET", "/api/jobs/" + job.job_id)[2]
        self.assertEqual(again["revision"], revision)
        self.assertEqual(again["status"], "interrupted")

    def test_restart_preserves_verified_current_artifact_and_explicit_history(self):
        raw, plan = synthetic()
        job = self.repository.create([InputSnapshot(raw, "tower_m.bytes")])
        key = next(iter(job.snapshot()["templates"]))
        first = job.register_output(key, rewrite_template(raw, plan, "first"), plan, "first")
        job.start_attempt(key, parameters={"ratio": 0})
        second = job.register_output(key, rewrite_template(raw, plan, "second"), plan, "second")
        job.set_status("succeeded")
        self.start_server()
        payload = self.request("GET", "/api/jobs/" + job.job_id)[2]
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual([item["id"] for item in payload["artifacts"]], [second["id"]])
        row = self.rows(payload)[0]
        self.assertEqual(row["attempts"][0]["artifact_ids"], [first["id"]])
        self.assertEqual(row["attempts"][0]["attempt_id"], 0)
        self.assertEqual(row["current_attempt_id"], 1)
        self.assert_error(self.request("GET", "/api/jobs/{}/download/{}".format(job.job_id, first["id"])),
                          404, "artifact_not_found")
        self.assertEqual(self.request("GET", "/api/jobs/{}/download/{}".format(job.job_id, second["id"]))[0], 200)

    def test_corrupt_job_state_is_diagnosed_but_healthy_recent_job_still_loads(self):
        raw, _ = synthetic()
        broken = self.repository.create([InputSnapshot(raw, "broken_m.bytes")])
        healthy = self.repository.create([InputSnapshot(raw + b"\x78\x01", "healthy_m.bytes")])
        (broken.directory / "state.json").write_text("[]", encoding="utf-8")
        self.start_server()
        bootstrap = self.request("GET", "/api/bootstrap")[2]
        recent = {row["job_id"]: row for row in bootstrap["recent_jobs"]}
        self.assertIn(healthy.job_id, recent)
        self.assertNotIn(broken.job_id, recent)
        self.assertEqual(recent[healthy.job_id]["template_count"], 1)
        self.assertRegex(recent[healthy.job_id]["updated_at"], r"^\d{4}-\d\d-\d\dT")
        self.assertGreaterEqual(bootstrap["recovery"]["invalid_jobs"], 1)
        self.assert_error(self.request("GET", "/api/jobs/" + broken.job_id), 409, "job_state_invalid")

    def test_startup_recovers_sync_journals_before_reporting_ready(self):
        sync = RecoveringSyncService()
        application = self.start_server(sync_service=sync)
        self.assertEqual(sync.recover_calls, 1)
        bootstrap = self.request("GET", "/api/bootstrap")[2]
        self.assertEqual(bootstrap["recovery"]["sync"], sync.recovered)
        self.assertTrue(application.ready)
        self.assertTrue(bootstrap["status"]["sync_mutation_ready"])

    def test_sync_recovery_exception_keeps_reads_ready_but_locks_all_new_sync_mutations(self):
        self._confirmed_bindings()
        sync = FaultingRecoverySyncService(
            failure=OSError(str(self.root / "secret-journal")),
        )
        self.start_server(sync_service=sync)
        job_id, _ = self._completed_job()
        bootstrap = self.request("GET", "/api/bootstrap")[2]
        self.assertTrue(bootstrap["status"]["ready"])
        self.assertFalse(bootstrap["status"]["sync_mutation_ready"])
        self.assertNotIn(str(self.root), json.dumps(bootstrap, ensure_ascii=False))
        self.assert_error(self.request("POST", "/api/sync/plan", json_body={
            "job_id": job_id, "bindings": ["tile"],
        }), 409, "sync_recovery_required")
        self.assert_error(self.request("POST", "/api/sync/commit", json_body={
            "plan_id": "plan-" + "d" * 32,
            "confirmation_token": "unused",
            "conflict_confirmations": {},
        }), 409, "sync_recovery_required")
        self.assertEqual((sync.plan_calls, sync.commit_calls), (0, 0))

    def test_recovery_required_or_unverifiable_diagnostic_locks_sync_but_terminal_recovery_does_not(self):
        cases = (
            FaultingRecoverySyncService(recovered=[{"plan_id": "plan-" + "a" * 32,
                                                    "status": "recovery_required"}]),
            FaultingRecoverySyncService(recovered=[{"status": "rolled_back"}],
                                        diagnostics=[{"code": "invalid_sync_journal",
                                                      "message": str(self.root / "secret")}]),
        )
        for sync in cases:
            with self.subTest(sync=sync):
                self._stop_server()
                self.start_server(sync_service=sync)
                bootstrap = self.request("GET", "/api/bootstrap")[2]
                self.assertFalse(bootstrap["status"]["sync_mutation_ready"])
                self.assertNotIn(str(self.root), json.dumps(bootstrap, ensure_ascii=False))

        self._stop_server()
        healthy = FaultingRecoverySyncService(recovered=[
            {"status": "rolled_back"}, {"status": "committed"},
        ])
        self.start_server(sync_service=healthy)
        self.assertTrue(self.request("GET", "/api/bootstrap")[2]["status"]["sync_mutation_ready"])

    def test_real_sync_service_create_same_sha_conflict_backup_and_bad_recovery_lock(self):
        self._confirmed_bindings()
        service = SyncService(self.repository, self.settings_path)
        self.start_server(sync_service=service)
        job_id, completed = self._completed_job()
        artifact = completed["artifacts"][0]

        def preview():
            response = self.request("POST", "/api/sync/plan", json_body={
                "job_id": job_id, "bindings": ["template"],
            })
            self.assertEqual(response[0], 200, response[3][:1000])
            return response[2]

        def commit(plan, decisions=None):
            response = self.request("POST", "/api/sync/commit", json_body={
                "plan_id": plan["plan_id"],
                "confirmation_token": plan["confirmation_token"],
                "conflict_confirmations": decisions or {},
            })
            self.assertEqual(response[0], 200, response[3][:1000])
            return response[2]

        created_plan = preview()
        self.assertEqual(created_plan["rows"][0]["targets"][0]["classification"], "create")
        self.assertEqual(commit(created_plan)["status"], "committed")

        same_plan = preview()
        self.assertEqual(same_plan["rows"][0]["targets"][0]["classification"], "same_sha")
        self.assertEqual(commit(same_plan)["status"], "committed")

        settings = self.request("GET", "/api/settings")[2]["settings"]
        target = Path(settings["template_root"]) / artifact["filename"]
        target.write_bytes(b"third-party-conflict")
        conflict_plan = preview()
        conflict_ids = {
            target_row["conflict_id"]: "backup_and_replace"
            for row in conflict_plan["rows"] for target_row in row["targets"]
            if target_row.get("conflict_id")
        }
        self.assertTrue(conflict_ids)
        conflict_result = commit(conflict_plan, conflict_ids)
        self.assertEqual(conflict_result["status"], "committed")
        self.assertEqual(target.read_bytes(),
                         self.repository.get(job_id).read_artifact(artifact["id"]))

        # A malformed persisted plan is an unverifiable recovery diagnostic.
        # A fresh real service must therefore keep reads available while the
        # API refuses to issue any new repository-write capability.
        pending = service.create_sync_plan(self.repository.get(job_id), ("template",))
        plan_path = (self.repository.get(job_id).directory / "sync" /
                     pending.plan_id / "plan.json")
        plan_path.write_text("{broken", encoding="utf-8")
        self._stop_server()
        self.start_server(sync_service=SyncService(self.repository, self.settings_path))
        bootstrap = self.request("GET", "/api/bootstrap")[2]
        self.assertFalse(bootstrap["status"]["sync_mutation_ready"])
        self.assert_error(self.request("POST", "/api/sync/plan", json_body={
            "job_id": job_id, "bindings": ["template"],
        }), 409, "sync_recovery_required")

    def test_current_history_and_startup_recovery_errors_are_projected_without_paths(self):
        secret_current = str(self.root / "customer-current.bytes")
        secret_history = str(self.root / "customer-history.bytes")
        secret_startup = str(self.root / "customer-startup.bytes")
        raw, _ = synthetic()
        job = self.repository.create([InputSnapshot(raw, "tower_m.bytes")])
        key = next(iter(job.snapshot()["templates"]))
        job.update_template(key, status="failed", stage="verification", error={
            "code": "output_verification_failed", "stage": "verification",
            "message": secret_history, "recommendations": [secret_history],
        })
        job.start_attempt(key, parameters={"ratio": 0.25})
        job.update_template(key, status="failed", stage="recovery", error={
            "code": "recovery_verification_failed", "stage": "recovery",
            "message": secret_current, "recommendations": [secret_current],
        })
        job.set_status("failed")

        # Startup discovery converts active states to interrupted. The raw
        # pre-crash error must not become browser-visible after recovery.
        startup_job = self.repository.create([InputSnapshot(raw, "startup_m.bytes")])
        startup_key = next(iter(startup_job.snapshot()["templates"]))
        startup_job.update_template(startup_key, status="running", stage="placement", error={
            "code": "processing_failed", "stage": "placement",
            "message": secret_startup, "recommendations": [secret_startup],
        })
        startup_job.set_status("running")
        self.start_server()
        response = self.request("GET", "/api/jobs/" + job.job_id)
        self.assertEqual(response[0], 200)
        body = response[3].decode("utf-8")
        self.assertNotIn(secret_current, body)
        self.assertNotIn(secret_history, body)
        row = self.rows(response[2])[0]
        self.assertEqual(row["error"]["code"], "recovery_verification_failed")
        self.assertEqual(row["attempts"][0]["error"]["code"], "output_verification_failed")
        startup_response = self.request("GET", "/api/jobs/" + startup_job.job_id)
        self.assertEqual(startup_response[0], 200)
        self.assertNotIn(secret_startup, startup_response[3].decode("utf-8"))

    def test_concurrent_reruns_create_exactly_one_attempt_and_execute_winning_parameters(self):
        application = self.start_server()
        job_id, completed = self._completed_job()
        row = self.rows(completed)[0]
        key = row["key"]
        initial_attempt = row["current_attempt_id"]
        release = threading.Event()
        worker_started = threading.Event()
        observed = []

        def slow_run(job, keys):
            observed.append(dict(job.snapshot()["templates"][key]["parameters"]))
            worker_started.set()
            release.wait(3)
            job.update_template(key, status="failed", stage="processing",
                                error={"code": "processing_failed", "message": "test"})
            job.set_status("failed")

        application._run_job = slow_run
        barrier = threading.Barrier(3)
        results = {}

        def submit(name, seed, new_id):
            barrier.wait()
            results[name] = self.request(
                "POST", "/api/jobs/{}/rerun/{}".format(job_id, key),
                json_body={"defaults": dict(row["parameters"], seed=seed, new_id=new_id)},
            )

        threads = [
            threading.Thread(target=submit, args=("a", 101, "winnerA")),
            threading.Thread(target=submit, args=("b", 202, "winnerB")),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=4)
        self.assertTrue(worker_started.wait(2))
        statuses = sorted(response[0] for response in results.values())
        self.assertEqual(statuses, [202, 409])
        self.assertIn(self.request("GET", "/api/jobs/" + job_id)[2]["status"],
                      {"queued", "running"})
        winning_name = next(name for name, response in results.items() if response[0] == 202)
        winning = (101, "winnerA") if winning_name == "a" else (202, "winnerB")
        state = self.repository.get(job_id).snapshot()["templates"][key]
        self.assertEqual(state["attempt_id"], initial_attempt + 1)
        self.assertEqual((state["parameters"]["seed"], state["parameters"]["new_id"]), winning)
        self.assertEqual((observed[0]["seed"], observed[0]["new_id"]), winning)
        release.set()

    def test_rerun_preparation_failure_withdraws_attempt_and_worker_capacity(self):
        application = self.start_server()
        job_id, completed = self._completed_job()
        row = self.rows(completed)[0]
        key = row["key"]
        before = self.repository.get(job_id).snapshot()
        job = self.repository.get(job_id)
        with patch.object(job, "set_status", side_effect=OSError("manifest unavailable")):
            response = self.request(
                "POST", "/api/jobs/{}/rerun/{}".format(job_id, key),
                json_body={"defaults": dict(row["parameters"], seed=913)},
            )
        self.assert_error(response, 409, "job_state_invalid")
        after = job.snapshot()
        self.assertEqual(after["templates"][key]["attempt_id"],
                         before["templates"][key]["attempt_id"])
        self.assertEqual(after["templates"][key]["output_artifacts"],
                         before["templates"][key]["output_artifacts"])
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(application.bootstrap()["resources"]["active_jobs"], 0)

    def test_active_job_budget_is_acquired_before_job_directory_and_released_on_success_or_failure(self):
        _, api = self._modules()
        limits = api.ApiLimits(active_jobs=1)
        application = self.start_server(limits=limits)
        raw, _ = synthetic()
        imported = self.import_files([("tower_m.bytes", raw)])
        started = threading.Event()
        release = threading.Event()

        def slow_run(job, keys):
            started.set()
            release.wait(3)
            job.set_status("succeeded")

        application._run_job = slow_run
        first = self.request("POST", "/api/jobs", json_body={
            "import_id": imported["import_id"], "defaults": {}, "overrides": {},
        })
        self.assertEqual(first[0], 202)
        self.assertTrue(started.wait(2))
        before = {path.name for path in self.repository.root.iterdir()}
        rejected = self.request("POST", "/api/jobs", json_body={
            "import_id": imported["import_id"], "defaults": {}, "overrides": {},
        })
        self.assert_error(rejected, 429, "job_busy")
        self.assertEqual({path.name for path in self.repository.root.iterdir()}, before)
        self.assertEqual(self.request("GET", "/api/bootstrap")[2]["resources"]["active_jobs"], 1)
        release.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.request("GET", "/api/bootstrap")[2]["resources"]["active_jobs"] == 0:
                break
            time.sleep(0.02)
        self.assertEqual(self.request("GET", "/api/bootstrap")[2]["resources"]["active_jobs"], 0)

        def failing_run(job, keys):
            raise RuntimeError("worker failed at " + str(self.root / "secret"))

        application._run_job = failing_run
        failed = self.request("POST", "/api/jobs", json_body={
            "import_id": imported["import_id"], "defaults": {}, "overrides": {},
        })
        self.assertEqual(failed[0], 202)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            payload = self.request("GET", "/api/jobs/" + failed[2]["job_id"])[2]
            if payload["status"] == "failed":
                break
            time.sleep(0.02)
        self.assertEqual(payload["status"], "failed")
        self.assertNotIn(str(self.root), json.dumps(payload, ensure_ascii=False))
        self.assertEqual(self.request("GET", "/api/bootstrap")[2]["resources"]["active_jobs"], 0)

    def test_worker_start_failure_marks_created_job_failed_and_releases_capacity(self):
        _, api = self._modules()
        limits = api.ApiLimits(active_jobs=1)
        application = self.start_server(limits=limits)
        raw, _ = synthetic()
        imported = self.import_files([("tower_m.bytes", raw)])
        with patch("scripts.tower_reducer.api.threading.Thread.start",
                   side_effect=RuntimeError("cannot start")):
            with self.assertRaises(api.ApiError) as raised:
                application.create_job({
                    "import_id": imported["import_id"], "defaults": {}, "overrides": {},
                })
        self.assertEqual((raised.exception.status, raised.exception.code),
                         (409, "job_start_failed"))
        jobs = list(self.repository.discover())
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].snapshot()["status"], "failed")
        self.assertEqual(application.bootstrap()["resources"]["active_jobs"], 0)

    def test_successful_rerun_replaces_current_artifact_and_keeps_previous_attempt_audit(self):
        self.start_server()
        raw, _ = synthetic()
        imported = self.import_files([("tower_m.bytes", raw)])
        job_id = self.start_job(imported["import_id"])
        first_result = self.wait_job(job_id)
        first_row = self.rows(first_result)[0]
        first_id = first_result["artifacts"][0]["id"]
        response = self.request("POST", "/api/jobs/{}/rerun/{}".format(job_id, first_row["key"]), json_body={
            "defaults": {"ratio": 0.25, "retained_counts": None,
                         "strategy": "top_first", "max_tiles_per_layer": 24,
                         "max_added_layers": 3, "seed": 0,
                         "naming_rule": "lessT{原模板ID}"}
        })
        self.assertEqual(response[0], 202)
        second_result = self.wait_job(job_id)
        second_row = self.rows(second_result)[0]
        self.assertIn(second_row["status"], {"verified", "succeeded"})
        second_id = second_result["artifacts"][0]["id"]
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(second_row["attempts"][0]["artifact_ids"], [first_id])
        self.assert_error(self.request("GET", "/api/jobs/{}/download/{}".format(job_id, first_id)),
                          404, "artifact_not_found")
        self.assertEqual(self.request("GET", "/api/jobs/{}/download/{}".format(job_id, second_id))[0], 200)

    def test_no_tower_template_is_skipped_even_if_rerun_requests_a_new_id(self):
        self.start_server()
        raw = read_project_fixture("ntxkd021_m.bytes")
        imported = self.import_files([("no-tower_m.bytes", raw)])
        job_id = self.start_job(imported["import_id"])
        initial = self.wait_job(job_id)
        row = self.rows(initial)[0]
        self.assertEqual(row["status"], "no_op")
        self.assertEqual(initial["artifacts"], [])

        response = self.request(
            "POST", "/api/jobs/{}/rerun/{}".format(job_id, row["key"]),
            json_body={
                "defaults": {
                    "ratio": 0.25,
                    "retained_counts": None,
                    "strategy": "top_first",
                    "max_tiles_per_layer": 20,
                    "max_added_layers": 2,
                    "seed": 0,
                    "naming_rule": "renamed_{原模板ID}",
                    "new_id": "chosenNoTowerId",
                },
            },
        )
        self.assertEqual(response[0], 202)
        completed = self.wait_job(job_id)
        updated = self.rows(completed)[0]
        self.assertEqual(updated["status"], "no_op")
        self.assertEqual(completed["artifacts"], [])
        self.assertEqual(updated["naming"], {})
        self.assertEqual(updated["plan_summary"]["move_count"], 0)
        self.assertEqual(updated["plan_summary"]["result_tower_ratio"], 0.0)

    def test_job_creation_is_background_and_persists_monotonic_progress_and_bounded_logs(self):
        self.start_server()
        raw, _ = synthetic()
        imported = self.import_files([("one_m.bytes", raw), ("two_m.bytes", raw + b"\x78\x01")])
        before = time.monotonic()
        job_id = self.start_job(imported["import_id"])
        self.assertLess(time.monotonic() - before, 1.0)
        observed = []
        deadline = time.monotonic() + 12
        payload = None
        while time.monotonic() < deadline:
            payload = self.request("GET", "/api/jobs/" + job_id)[2]
            progress = payload["progress"]
            observed.append((progress["completed_templates"], payload["revision"]))
            if payload["status"] in TERMINAL_JOB_STATES:
                break
            time.sleep(0.02)
        self.assertIsNotNone(payload)
        self.assertIn(payload["status"], TERMINAL_JOB_STATES)
        self.assertEqual(observed, sorted(observed))
        self.assertLessEqual(len(payload["logs"]), 200)
        self.assertEqual(payload["progress"]["completed_templates"], 2)
        self.assertEqual(payload["progress"]["total_templates"], 2)

    def test_api_methods_and_truncated_bodies_return_json_and_do_not_poison_server(self):
        self.start_server()
        unsupported = self.request("DELETE", "/api/jobs", json_body={})
        self.assert_error(unsupported, 405, "method_not_allowed")
        stream = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        stream.sendall((
            "POST /api/jobs HTTP/1.1\r\n"
            "Host: 127.0.0.1:{port}\r\n"
            "Origin: {origin}\r\n"
            "X-CSRF-Token: {csrf}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 100\r\n\r\n{{}}"
        ).format(port=self.port, origin=self.origin, csrf=self.csrf).encode("ascii"))
        stream.shutdown(socket.SHUT_WR)
        response = stream.recv(4096)
        stream.close()
        self.assertIn(b"400", response.split(b"\r\n", 1)[0])
        self.assertIn(b"application/json", response.lower())
        # The truncated request is isolated to its handler thread.
        self.assertEqual(self.request("GET", "/api/health")[0], 200)

    def test_host_is_checked_on_reads_not_only_writes(self):
        self.start_server()
        response = self.request("GET", "/api/bootstrap", host="attacker.example:{}".format(self.port))
        self.assert_error(response, 403, "host_rejected")

    def test_expired_export_token_is_rejected_without_writing(self):
        clock = [500.0]
        self.start_server(clock=lambda: clock[0])
        raw, _ = synthetic()
        imported = self.import_files([("tower_m.bytes", raw)])
        job_id = self.start_job(imported["import_id"])
        self.wait_job(job_id)
        preview = self.request("POST", "/api/jobs/{}/export".format(job_id),
                               json_body={"action": "preview"})[2]
        clock[0] = preview["expires_at"] + 1
        response = self.request("POST", "/api/jobs/{}/export".format(job_id), json_body={
            "action": "commit", "plan_id": preview["plan_id"],
            "confirmation_token": preview["confirmation_token"], "conflict_decisions": {},
        })
        self.assert_error(response, 409, "plan_expired")
        self.assertEqual(list(self.export_root.iterdir()), [])

    def test_repository_root_and_settings_are_not_exposed_by_generic_errors(self):
        self.start_server()
        # An unknown job containing traversal-looking data must not echo request
        # payload or server paths in its stable public error.
        response = self.request("POST", "/api/jobs", json_body={
            "import_id": "../../" + str(self.root), "defaults": {}, "overrides": {}
        })
        self.assert_error(response, 400, "invalid_request")
        text = response[3].decode("utf-8")
        self.assertNotIn(str(self.repository.root), text)
        self.assertNotIn(str(self.settings_path), text)


if __name__ == "__main__":
    unittest.main()
