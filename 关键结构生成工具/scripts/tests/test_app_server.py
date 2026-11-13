from __future__ import annotations

import json
import base64
import copy
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from app import (  # noqa: E402
    AppServer,
    AppState,
    ExportRunRecord,
    JobRecord,
    _duplicate_rejecting_json_object,
    _error_payload,
)
from puzzle_core.config_io import loads_batch_request, resolve_batch_config  # noqa: E402
from puzzle_core.errors import InputValidationError  # noqa: E402
from puzzle_core.search import search_candidates as core_search_candidates  # noqa: E402
from services.profile_service import ProfileService  # noqa: E402
from services.parameter_recommendation_service import (  # noqa: E402
    apply_generation_parameter_changes,
    build_generation_probe_request,
)
from services.job_service import (  # noqa: E402
    GenerationBatchResult,
    GenerationItemResult,
)
from services.task_control import CooperativeTaskControl  # noqa: E402
from tests.test_ui_service import _chain_source, _generated  # noqa: E402


class AppStateTests(unittest.TestCase):
    def test_derived_job_budget_resume_uses_its_own_candidates_not_parent_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = ProfileService(Path(temporary) / "settings.json")
            service.initialize(Path(temporary) / "tool")
            state = AppState(service)
            parent = self._job_record(state)
            work = parent.config.work_items[0]
            source = _chain_source()
            generated = _generated(source)
            row = GenerationItemResult(
                template_key=work.template_key, plan_id=work.plan.plan_id,
                source_plan_id=work.plan.source_plan_id, plan_signature=work.plan.signature,
                parameter_source="batch", generation_seed=7, source=source, inspection=None,
                candidates=(), attempts_completed=1, rejection_counts={},
                target_candidate_count=4, status="cancelled")
            parent.result = GenerationBatchResult(
                format_version=1, batch_seed=parent.config.batch.batch_seed,
                resolved_batch_hash=parent.config.batch.config_hash,
                source_config_hash=parent.config.source_config_hash, algorithm_version="test",
                items=(row,), run_status="cancelled")
            earned = replace(parent.result, run_status="partial", items=(replace(
                row, candidates=(generated,), status="partial", attempts_completed=30,
                shared_attempts_completed=30,
                error={"failure_kind": "budget_exhausted"}),))
            child = replace(parent, job_id="child", parent_job_id=parent.job_id,
                status="partial", result=earned, started_at="2026-10-29T20:00:00+08:00",
                events=[], _event_dedupe={})
            state.jobs[child.job_id] = child
            received = []
            def search(*args, **kwargs):
                received.append(kwargs["previous_result"])
                return kwargs["previous_result"]
            with patch("app.threading.Thread"):
                state.control_job(child.job_id, "resume_unfinished")
            with patch("app.generate_batch", side_effect=search), patch.object(
                    state, "_prepare_generation_recommendations"):
                state._run_job(child.job_id)
            self.assertIsNone(child.error)
            self.assertEqual(len(received[0].items[0].candidates), 1)
            self.assertEqual(received[0].items[0].shared_attempts_completed, 30)
            self.assertEqual(child.summary()["search"]["candidate_found_count"], 1)
            self.assertFalse(any(event["type"] == "parent_result_inherited" for event in child.events))

    def test_budget_resume_keeps_processed_progress_baseline_during_search(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = ProfileService(Path(temporary) / "settings.json")
            service.initialize(Path(temporary) / "tool")
            state = AppState(service)
            job = self._job_record(state)
            request = {**job.config_payload, "template_keys": [f"ui{i:03}" for i in range(15)]}
            job.config = resolve_batch_config(loads_batch_request(json.dumps(request)))
            rows = tuple(GenerationItemResult(
                template_key=work.template_key, plan_id=work.plan.plan_id,
                source_plan_id=work.plan.source_plan_id, plan_signature=work.plan.signature,
                parameter_source="batch", generation_seed=7, source=None, inspection=None,
                candidates=(), attempts_completed=10, rejection_counts={},
                target_candidate_count=1, status="failed" if index < 13 else "pending",
                error={"failure_kind": "budget_exhausted"} if index < 13 else None,
            ) for index, work in enumerate(job.config.work_items))
            previous = GenerationBatchResult(
                format_version=1, batch_seed=job.config.batch.batch_seed,
                resolved_batch_hash=job.config.batch.config_hash,
                source_config_hash=job.config.source_config_hash, algorithm_version="test",
                items=rows, run_status="cancelled")
            job.result = previous
            job.status = "cancelled"
            job.progress_completed = 13
            observed = []
            def search(*args, **kwargs):
                observed.append(job.summary()["progress"])
                kwargs["progress_callback"](replace(previous, run_status="running",
                    items=(replace(rows[0], status="running", error=None), *rows[1:])))
                observed.append(job.summary()["progress"])
                return previous
            with patch("app.threading.Thread"):
                state.control_job(job.job_id, "resume_unfinished")
            with patch("app.generate_batch", side_effect=search), patch.object(
                    state, "_prepare_generation_recommendations"):
                state._run_job(job.job_id)
            self.assertIsNone(job.error)
            self.assertEqual([value["completed_work_items"] for value in observed], [13, 13])
            self.assertEqual([value["percent"] for value in observed], [87, 87])

    def test_job_source_preview_uses_locked_source_even_without_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ProfileService(Path(temporary) / "settings.json")
            service.initialize(Path(temporary) / "tool")
            state = AppState(service)
            job = self._job_record(state)
            job.sources["ui001"] = _chain_source()
            job.status = "cancelled"
            preview = state.job_source_preview(job.job_id, "ui001")
            self.assertEqual(preview["template_key"], "ui001")
            self.assertTrue(preview["board"]["tiles"])
            self.assertIn("non_tower_tile_count", preview["inspection"])
            self.assertIsNone(job.result)
            with self.assertRaises(InputValidationError):
                state.job_source_preview(job.job_id, "not-in-this-job")

    def test_csv_name_preview_uses_selected_manifests_without_simulation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ProfileService(Path(temporary) / "settings.json")
            service.initialize(Path(temporary) / "tool")
            state = AppState(service)
            state.parameter_selections["names"] = {"manifests": [{"source_template_id":"lessThr0008", "output_template_id":"aipuzzlelessThr0008s5", "signature":"s5"}]}
            preview = getattr(state, "match_consumption_templates", None)
            self.assertTrue(callable(preview), "Match uploaded CSV before starting a long job")
            history = b"level_template_id,level_dup,level_dup_para_id,\xe8\xbf\x9b\xe5\x85\xa5\xe4\xba\xba\xe6\x95\xb0,\xe4\xba\xba\xe5\x9d\x87\xe6\xb6\x88\xe8\x80\x97\nhr0008,7|0|100|4,1,100,500\n"
            result = preview({"selection_id":"names", "history_file":{"name":"history.csv", "data_base64":base64.b64encode(history).decode("ascii")}})
            self.assertEqual(result["items"][0]["matched_template_id"], "hr0008")
            with self.assertRaisesRegex(InputValidationError, "失效"):
                preview({"selection_id":"missing", "history_file":{"name":"history.csv", "data_base64":"eA=="}})

    def test_calibration_draft_cannot_be_written_to_another_profile_or_config_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ProfileService(Path(temporary) / "settings.json")
            profile = service.initialize(Path(temporary) / "tool")
            state = AppState(service)
            state.parameter_selections["params_context"] = {"manifests": []}
            payload = {
                "selection_id": "params_context",
                "history_file": {
                    "name": "history.csv",
                    "data_base64": base64.b64encode(b"history").decode("ascii"),
                },
            }
            with patch("app.build_consumption_calibration", return_value={
                "config_set_id": "csv", "rows": [{"output_template_id": "output"}],
            }):
                result = state.calibrate_consumption_parameters(payload)
            self.assertIn("calibration_context", result)
            with patch("app.write_calibration_config", return_value={"updated_count": 1}) as writer:
                self.assertEqual(state.write_consumption_calibration_config({
                    "config_set_id": "csv", "rows": result["rows"],
                    "calibration_context": result["calibration_context"],
                })["updated_count"], 1)
                writer.reset_mock()
                with patch.object(state, "profile", return_value=replace(
                    profile, profile_id="profile_changed",
                    ulinkconfig_root=str(Path(temporary) / "other-config"),
                )):
                    with self.assertRaisesRegex(InputValidationError, "环境已变化"):
                        state.write_consumption_calibration_config({
                            "config_set_id": "csv", "rows": result["rows"],
                            "calibration_context": result["calibration_context"],
                        })
                with self.assertRaisesRegex(InputValidationError, "配置集已变化"):
                    state.write_consumption_calibration_config({
                        "config_set_id": "other", "rows": result["rows"],
                    })
                with self.assertRaisesRegex(InputValidationError, "来源已失效"):
                    state.write_consumption_calibration_config({
                        "config_set_id": "csv", "rows": result["rows"],
                        "calibration_context": {"context_id": "unknown-context"},
                    })
                writer.assert_not_called()

    def test_legacy_calibration_rows_without_context_keep_existing_write_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ProfileService(Path(temporary) / "settings.json")
            service.initialize(Path(temporary) / "tool")
            state = AppState(service)
            with patch("app.write_calibration_config", return_value={"updated_count": 1}):
                written = state.write_consumption_calibration_config({
                    "config_set_id": "csv", "rows": [{"output_template_id": "legacy"}],
                })
            self.assertEqual(written["updated_count"], 1)

    def test_consumption_calibration_runs_in_background_and_reports_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile = profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            state.parameter_selections["params_async"] = {
                "manifests": [{"output_template_id": "output"}],
                "source_export_id": "export_async",
            }
            body = {
                "selection_id": "params_async",
                "history_file": {
                    "name": "history.csv",
                    "data_base64": base64.b64encode(b"history").decode("ascii"),
                },
                "optimize_all_duplicate_seeds": True,
                "auto_first_duplicate_max": True,
                "template_name_prefix": "aipuzzlelessT",
                "template_name_suffix": "s5",
            }
            entered = threading.Event()
            release = threading.Event()
            returned = threading.Event()
            result = {"mode": "target_consumption_calibration", "rows": []}

            def slow_calibration(profile_snapshot, manifests, **kwargs):
                self.assertEqual(profile_snapshot, profile)
                self.assertEqual(manifests, [{"output_template_id": "output"}])
                self.assertTrue(kwargs["optimize_all_duplicate_seeds"])
                self.assertTrue(kwargs["auto_first_duplicate_max"])
                self.assertEqual(kwargs["template_name_prefix"], "aipuzzlelessT")
                self.assertEqual(kwargs["template_name_suffix"], "s5")
                kwargs["progress_callback"]({
                    "phase": "full_simulation",
                    "message": "正在仿真",
                    "template_total": 1,
                    "template_completed": 0,
                    "current_template": "output",
                    "duplicate_index": 3,
                    "duplicate_completed": 2,
                    "duplicate_total_all": 10,
                    "candidate_completed": 4,
                    "candidate_total": 12,
                })
                entered.set()
                try:
                    if not release.wait(5):
                        raise RuntimeError("test did not release calibration")
                    return result
                finally:
                    returned.set()

            with patch("app.build_consumption_calibration", side_effect=slow_calibration):
                try:
                    started = state.create_consumption_calibration_job(body)
                    self.assertTrue(entered.wait(2))
                    running = state.consumption_calibration_job(started["job_id"])
                    self.assertEqual(running["status"], "running")
                    self.assertIsNone(running["result"])
                    self.assertEqual(running["progress"]["duplicate_completed"], 2)
                    self.assertEqual(running["progress"]["candidate_completed"], 4)
                    self.assertEqual(
                        state.create_consumption_calibration_job(body)["job_id"],
                        started["job_id"],
                    )
                    running["progress"]["duplicate_completed"] = 99
                    self.assertEqual(
                        state.consumption_calibration_job(started["job_id"])
                        ["progress"]["duplicate_completed"], 2,
                    )
                finally:
                    release.set()
                    if entered.is_set():
                        self.assertTrue(returned.wait(2))
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    completed = state.consumption_calibration_job(started["job_id"])
                    if completed["status"] == "completed":
                        break
                    time.sleep(0.005)
                self.assertEqual(completed["status"], "completed")
                self.assertEqual(completed["result"]["mode"], result["mode"])
                self.assertEqual(completed["result"]["selection_id"], "params_async")
                self.assertEqual(completed["result"]["source_export_id"], "export_async")
                self.assertEqual(completed["progress"]["duplicate_completed"], 10)
                self.assertGreaterEqual(completed["elapsed_s"], 0)
                self.assertIsNone(completed["error"])

    def test_consumption_calibration_background_failure_is_pollable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            state.parameter_selections["params_failure"] = {"manifests": []}
            body = {
                "selection_id": "params_failure",
                "history_file": {
                    "name": "history.csv",
                    "data_base64": base64.b64encode(b"history").decode("ascii"),
                },
            }
            with patch(
                "app.build_consumption_calibration",
                side_effect=InputValidationError("历史副本数据缺少必填列"),
            ):
                started = state.create_consumption_calibration_job(body)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    failed = state.consumption_calibration_job(started["job_id"])
                    if failed["status"] == "failed":
                        break
                    time.sleep(0.005)
            self.assertEqual(failed["status"], "failed")
            self.assertIsNone(failed["result"])
            self.assertEqual(failed["error"]["message"], "历史副本数据缺少必填列")
            self.assertIsNotNone(failed["completed_at"])

    def test_export_run_keeps_its_profile_snapshot_when_settings_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            original_profile = profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            job = self._job_record(state)
            job.status = "completed"
            job.result = object()
            record = ExportRunRecord(
                export_id="export_profile_snapshot",
                job_id=job.job_id,
                created_at="2026-10-13T12:00:00+08:00",
                idempotency_key="export-profile-snapshot-key",
                payload={},
                output_directory=original_profile.output_root,
                profile_snapshot=original_profile,
            )
            state.exports[record.export_id] = record
            changed_output = Path(temporary) / "changed-output"
            profile_service.configure_output(
                original_profile.profile_id,
                changed_output,
            )
            regenerated = SimpleNamespace(
                run_status="completed",
                items=(),
                as_dict=lambda: {"summary": {}},
            )
            exported = SimpleNamespace(
                as_dict=lambda: {
                    "state": "completed",
                    "batch_id": "batch_profile_snapshot",
                    "outputs": [],
                }
            )

            with (
                patch("app.generate_batch", return_value=regenerated) as generate,
                patch("app.resolve_export_selections", return_value=()),
                patch("app.export_selected", return_value=exported) as export,
                patch.object(
                    state,
                    "profile",
                    side_effect=AssertionError(
                        "running export must not reload mutable profile settings"
                    ),
                ),
            ):
                state._run_export(record.export_id)

        self.assertEqual(record.status, "completed", record.error)
        self.assertEqual(generate.call_args.args[1], original_profile)
        self.assertEqual(export.call_args.args[0].profile, original_profile)
        self.assertNotEqual(original_profile.output_root, str(changed_output))

    def test_completed_export_opens_its_recorded_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            output_directory = Path(temporary) / "exported"
            output_directory.mkdir()
            record = ExportRunRecord(
                export_id="export_open",
                job_id="job_open",
                created_at="2026-10-13T12:00:00+08:00",
                idempotency_key="export-open-key",
                payload={},
                output_directory=str(output_directory),
                status="completed",
                result={"state": "completed", "outputs": []},
            )
            state.exports[record.export_id] = record

            with patch("app.open_directory", return_value=output_directory) as opener:
                result = state.open_export_output_directory(record.export_id)

        opener.assert_called_once_with(output_directory)
        self.assertEqual(result["export_id"], record.export_id)
        self.assertEqual(result["path"], str(output_directory))

    def test_unfinished_export_cannot_open_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            record = ExportRunRecord(
                export_id="export_running",
                job_id="job_running",
                created_at="2026-10-13T12:00:00+08:00",
                idempotency_key="export-running-key",
                payload={},
                output_directory=str(Path(temporary)),
                status="running",
            )
            state.exports[record.export_id] = record

            with patch("app.open_directory") as opener:
                with self.assertRaises(InputValidationError):
                    state.open_export_output_directory(record.export_id)

        opener.assert_not_called()

    def test_consumption_calibration_rejects_non_fidelity_parameter_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)

            with self.assertRaises(InputValidationError) as caught:
                state.calibrate_consumption_parameters(
                    {"parameter_strategy": "rebuild_gradient"}
                )

        self.assertIn("仅支持保真副本参数", str(caught.exception))

    def test_os_error_payload_does_not_expose_absolute_path(self) -> None:
        private_path = "/Users/example/private/config_level.csv"

        payload = _error_payload(OSError(13, "Permission denied", private_path))
        missing = _error_payload(FileNotFoundError(2, "No such file", private_path))

        self.assertEqual(payload["code"], "os_error")
        self.assertEqual(payload["reason"], "os_error_13")
        self.assertNotIn(private_path, json.dumps(payload, ensure_ascii=False))
        self.assertEqual(missing["code"], "not_found")
        self.assertNotIn(private_path, json.dumps(missing, ensure_ascii=False))

        try:
            raise OSError(13, "Permission denied", private_path)
        except OSError as cause:
            try:
                raise InputValidationError(
                    "配置表不可读", evidence={"path": private_path}
                ) from cause
            except InputValidationError as wrapped:
                wrapped_payload = _error_payload(wrapped)
        self.assertEqual(wrapped_payload["code"], "INPUT_VALIDATION_ERROR")
        self.assertEqual(wrapped_payload["reason"], "os_error_13")
        self.assertNotIn(
            private_path, json.dumps(wrapped_payload, ensure_ascii=False)
        )

    @staticmethod
    def _job_record(state: AppState) -> JobRecord:
        request = {
            "schema_version": 1,
            "template_keys": ["ui001"],
            "batch_plans": [
                {
                    "plan_id": "s4",
                    "tower_mode": "disallow",
                    "units": [{"unit_id": "U1", "step": 4}],
                }
            ],
        }
        config = resolve_batch_config(
            loads_batch_request(json.dumps(request, ensure_ascii=False))
        )
        job = JobRecord(
            job_id="job_test",
            created_at="2026-09-15T12:00:00+08:00",
            import_id="import_test",
            config_payload=request,
            config=config,
            sources={},
        )
        state.jobs[job.job_id] = job
        return job

    def test_duplicate_json_fields_are_rejected_at_every_object_depth(self) -> None:
        with self.assertRaises(InputValidationError) as caught:
            json.loads(
                '{"outer":{"selection_id":"a","selection_id":"b"}}',
                object_pairs_hook=_duplicate_rejecting_json_object,
            )
        self.assertEqual(caught.exception.detail.evidence["field"], "selection_id")

    def test_browser_presentation_keeps_candidate_parameter_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            job = self._job_record(state)
            work = job.config.work_items[0]
            source = _chain_source()
            generated = replace(
                _generated(source),
                resolved_plan=work.plan,
                parameter_source="template:replace",
            )
            row = GenerationItemResult(
                template_key=work.template_key,
                plan_id=work.plan.plan_id,
                source_plan_id=work.plan.source_plan_id,
                plan_signature=work.plan.signature,
                parameter_source="template:replace",
                generation_seed=7,
                source=source,
                inspection=None,
                candidates=(generated,),
                attempts_completed=1,
                rejection_counts={},
                target_candidate_count=1,
                status="success",
            )
            result = GenerationBatchResult(
                format_version=1,
                batch_seed=job.config.batch.batch_seed,
                resolved_batch_hash=job.config.batch.config_hash,
                source_config_hash=job.config.source_config_hash,
                algorithm_version="test",
                items=(row,),
                run_status="completed",
            )

            presented = state._present_result(job, result)

        provenance = presented["items"][0]["candidates"][0]["parameter_provenance"]
        self.assertTrue(provenance["non_uniform"])
        self.assertEqual(provenance["reason"], "template_specific")

    def test_only_passed_verified_parameter_card_can_be_applied(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            job = self._job_record(state)
            card = {
                "recommendation_id": "adjust_test",
                "template_key": "ui001",
                "source_plan_id": "s4",
                "field": "max_layer_percent",
                "changes": {"max_layer_percent": 60},
                "verified": False,
                "trial_result": {"status": "pending"},
            }
            job.parameter_recommendations = [card]

            for status in ("pending", "not_found", "error"):
                card["trial_result"] = {"status": status}
                card["verified"] = False
                with self.subTest(status=status):
                    with self.assertRaises(InputValidationError) as caught:
                        state.apply_generation_recommendation(
                            job.job_id, {"recommendation_id": "adjust_test"}
                        )
                    self.assertEqual(
                        caught.exception.detail.evidence["trial_status"], status
                    )

            card["verified"] = True
            card["trial_result"] = {"status": "passed", "candidate_count": 1}
            applied = state.apply_generation_recommendation(
                job.job_id, {"recommendation_id": "adjust_test"}
            )
            self.assertEqual(applied["import_id"], "import_test")
            self.assertEqual(applied["differences"][0]["after"], 60)
            self.assertTrue(applied["verified"])

    def test_search_limit_disables_further_identical_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            job = self._job_record(state)
            job.status = "failed"
            job.attempt_budget_multiplier = 16
            item = SimpleNamespace(
                status="failed",
                candidates=(),
                error={"failure_kind": "budget_exhausted"},
            )
            job.result = SimpleNamespace(
                items=(item,),
                as_dict=lambda: {"summary": {"run_status": "failed"}},
            )

            summary = job.summary()
            self.assertFalse(summary["actions"]["can_continue_search"])
            self.assertTrue(summary["search"]["limit_reached"])
            with self.assertRaises(InputValidationError) as caught:
                state.control_job(job.job_id, "continue_search")
            self.assertIn("搜索上限", str(caught.exception))

    def test_capped_budget_item_does_not_block_other_unfinished_items(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            request = {
                "schema_version": 1,
                "template_keys": ["ui001"],
                "generation_defaults": {
                    "replacement_sources": ["A", "B"],
                    "candidate_count": 1,
                    "attempt_budget": 10,
                },
                "batch_plans": [
                    {
                        "plan_id": "s4",
                        "tower_mode": "disallow",
                        "units": [{"unit_id": "U1", "step": 4}],
                    },
                    {
                        "plan_id": "s5",
                        "tower_mode": "disallow",
                        "units": [{"unit_id": "U1", "step": 5}],
                    },
                ],
            }
            config = resolve_batch_config(
                loads_batch_request(json.dumps(request, ensure_ascii=False))
            )
            job = JobRecord(
                job_id="job_mixed_limit",
                created_at="2026-09-16T12:00:00+08:00",
                import_id="import_test",
                config_payload=request,
                config=config,
                sources={},
            )
            state.jobs[job.job_id] = job
            first, second = config.work_items
            capped = GenerationItemResult(
                template_key=first.template_key,
                plan_id=first.plan.plan_id,
                plan_signature=first.plan.signature,
                parameter_source=first.parameter_source,
                generation_seed=1,
                source=None,
                inspection=None,
                status="failed",
                candidates=(),
                attempts_completed=160,
                rejection_counts={},
                target_candidate_count=1,
                slot_attempts_completed={1: 160},
                error={
                    "failure_kind": "budget_exhausted",
                    "evidence": {"attempt_budget": 160},
                },
                effective_attempt_budget_per_slot=160,
            )
            pending = GenerationItemResult(
                template_key=second.template_key,
                plan_id=second.plan.plan_id,
                plan_signature=second.plan.signature,
                parameter_source=second.parameter_source,
                generation_seed=0,
                source=None,
                inspection=None,
                status="pending",
                candidates=(),
                attempts_completed=0,
                rejection_counts={},
                target_candidate_count=1,
                slot_attempts_completed={},
                error=None,
            )
            job.result = GenerationBatchResult(
                format_version=1,
                batch_seed=config.batch.batch_seed,
                resolved_batch_hash=config.batch.config_hash,
                source_config_hash=config.source_config_hash,
                algorithm_version="test",
                items=(capped, pending),
                run_status="partial",
            )
            job.status = "partial"
            job.attempt_budget_multiplier = 16
            job.cumulative_elapsed_seconds = 8.0

            summary = job.summary()
            self.assertTrue(summary["actions"]["can_resume_unfinished"])
            states = {
                item["plan_id"]: item
                for item in summary["search"]["budget"]["resume_items"]
            }
            self.assertFalse(states[first.plan.plan_id]["resumable"])
            self.assertEqual(
                states[first.plan.plan_id]["blocked_reason"],
                "search_budget_limit_reached",
            )
            self.assertTrue(states[second.plan.plan_id]["resumable"])
            self.assertIsNone(states[second.plan.plan_id]["blocked_reason"])
            recommended = summary["search"]["budget"][
                "recommended_next_attempt_budget_by_item"
            ]
            self.assertEqual(
                [item["plan_id"] for item in recommended],
                [second.plan.plan_id],
            )
            self.assertEqual(
                recommended[0]["unfinished_slots"][0]["additional_attempts"],
                160,
            )
            estimate = summary["search"]["estimated_additional_duration"]
            self.assertEqual(estimate["status"], "available")
            self.assertEqual(
                estimate["basis"]["planned_additional_attempts"], 160
            )
            presented = state._present_result(job, job.result)
            self.assertEqual(
                presented["items"][0]["resume"]["blocked_reason"],
                "search_budget_limit_reached",
            )
            self.assertTrue(presented["items"][1]["resume"]["resumable"])

            with patch("app.threading.Thread") as thread_factory:
                state.control_job(job.job_id, "resume_unfinished")
                thread_factory.return_value.start.assert_called_once_with()
            self.assertEqual(job.attempt_budget_multiplier, 16)
            self.assertEqual(
                job.excluded_retry_failure_kinds,
                ("parameter_conflict", "budget_exhausted"),
            )

    def test_budget_summary_and_adjustable_resume_are_evidence_labeled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            job = self._job_record(state)
            request = dict(job.config_payload)
            request["generation_defaults"] = {
                "replacement_sources": ["A", "B"],
                "candidate_count": 4,
                "attempt_budget": 10,
            }
            job.config_payload = request
            job.config = resolve_batch_config(
                loads_batch_request(json.dumps(request, ensure_ascii=False))
            )
            work = job.config.work_items[0]
            target = work.generation_options.candidate_count
            base_budget = work.generation_options.attempt_budget
            candidate = SimpleNamespace(
                candidate=SimpleNamespace(candidate_index=1)
            )
            item = SimpleNamespace(
                template_key=work.template_key,
                plan_id=work.plan.plan_id,
                status="partial",
                candidates=(candidate,),
                attempts_completed=base_budget,
                shared_attempts_completed=base_budget,
                slot_attempts_completed={
                    index: base_budget for index in range(1, target + 1)
                },
                error={
                    "failure_kind": "budget_exhausted",
                    "evidence": {"attempt_budget": base_budget},
                },
            )
            job.result = SimpleNamespace(
                items=(item,),
                as_dict=lambda: {"summary": {"run_status": "partial"}},
            )
            job.status = "partial"
            job.cumulative_elapsed_seconds = 12.0

            summary = job.summary()
            budget = summary["search"]["budget"]
            self.assertEqual(budget["scope"], "per_work_item")
            self.assertEqual(budget['recommended_next_attempt_budget_by_item'][0]['additional_attempts'], 10)
            self.assertEqual(
                budget["current_attempt_budget_by_item"][0][
                    "current_attempt_budget"
                ],
                base_budget,
            )
            self.assertEqual(budget["recommended_next_multiplier"], 2)
            self.assertEqual(
                summary["search"]["estimated_additional_duration"]["status"],
                "available",
            )
            self.assertEqual(
                summary["search"]["success_probability"]["status"],
                "unavailable",
            )
            self.assertTrue(summary["actions"]["can_resume_unfinished"])

            with patch("app.threading.Thread") as thread_factory:
                state.control_job(
                    job.job_id,
                    "resume_unfinished",
                    {"attempt_budget_multiplier": 3},
                )
                thread_factory.return_value.start.assert_called_once_with()
            self.assertEqual(job.attempt_budget_multiplier, 3)
            self.assertEqual(job.summary()["search"]["round"], {
                "retained_candidate_count": 1, "new_candidate_count": 0,
            })
            item.candidates = (candidate, candidate)
            self.assertEqual(job.summary()["search"]["round"]["new_candidate_count"], 1)
            event = job.summary()["events"][-1]
            self.assertEqual(event["type"], "resume_unfinished")
            self.assertEqual(
                event["details"]["canonical_action"], "resume_unfinished"
            )

    def test_unverified_span_opportunity_can_be_applied_without_false_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            job = self._job_record(state)
            job.parameter_recommendations = [
                {
                    "recommendation_id": "adjust_span_opportunity",
                    "template_key": "ui001",
                    "source_plan_id": "s4",
                    "field": "max_structure_layer_span",
                    "changes": {"max_structure_layer_span": 6},
                    "verified": False,
                    "trial_result": {"status": "not_found", "candidate_count": 0},
                }
            ]

            applied = state.apply_generation_recommendation(
                job.job_id,
                {"recommendation_id": "adjust_span_opportunity"},
            )

            self.assertFalse(applied["verified"])
            self.assertFalse(applied["trial_verified"])
            self.assertTrue(applied["opportunity_only"])
            self.assertEqual(
                applied["application_semantics"], "unverified_opportunity"
            )
            self.assertEqual(applied["continue_request"]["path"], "/api/v1/jobs")
            self.assertEqual(
                applied["continue_request"]["body"]["import_id"], "import_test"
            )
            self.assertEqual(
                applied["continue_request"]["body"]["parent_job_id"],
                job.job_id,
            )
            self.assertEqual(
                applied["continue_request"]["body"]["retry_scope"],
                {"template_key": "ui001", "plan_id": "s4"},
            )
            self.assertEqual(
                applied["differences"][0]["field"], "max_structure_layer_span"
            )

    def test_scoped_derived_job_retains_only_unaffected_parent_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            raw = {
                "schema_version": 1,
                "template_keys": ["first", "second"],
                "batch_seed": 42,
                "batch_plans": [
                    {
                        "plan_id": "s5",
                        "tower_mode": "disallow",
                        "units": [{"unit_id": "U1", "step": 5}],
                    }
                ],
            }
            config = resolve_batch_config(
                loads_batch_request(json.dumps(raw, ensure_ascii=False))
            )
            rows = tuple(
                GenerationItemResult(
                    template_key=work.template_key,
                    plan_id=work.plan.plan_id,
                    plan_signature=work.plan.signature,
                    parameter_source=work.parameter_source,
                    generation_seed=index,
                    source=None,
                    inspection=None,
                    candidates=(),
                    attempts_completed=10,
                    rejection_counts={},
                    target_candidate_count=1,
                    status="failed",
                    error={"failure_kind": "budget_exhausted"},
                )
                for index, work in enumerate(config.work_items, start=1)
            )
            parent_result = GenerationBatchResult(
                format_version=1,
                batch_seed=config.batch.batch_seed,
                resolved_batch_hash=config.batch.config_hash,
                source_config_hash=config.source_config_hash,
                algorithm_version="test",
                items=rows,
                run_status="partial",
            )
            child = JobRecord(
                job_id="job_child",
                created_at="2026-09-16T12:00:00+08:00",
                import_id="import_test",
                config_payload=raw,
                config=config,
                sources={},
                parent_job_id="job_parent",
                retry_scope={"template_key": "first", "plan_id": "s5"},
                retry_statuses=(),
            )

            carried = state._compatible_parent_result(child, parent_result)

            self.assertEqual(
                [(item.template_key, item.plan_id) for item in carried.items],
                [("first", "s5"), ("second", "s5")],
            )
            first = carried.items[0]
            self.assertEqual(first.status, "pending")
            self.assertEqual(first.candidates, ())
            self.assertEqual(carried.resolved_batch_hash, config.batch.config_hash)
            self.assertEqual(carried.run_status, "running")
            self.assertEqual(carried.total_work_item_count, 2)

    def test_scoped_plan_change_retains_parent_candidates_under_source_plan_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            parent_raw = {
                "schema_version": 1,
                "template_keys": ["first"],
                "batch_seed": 42,
                "generation_defaults": {"candidate_count": 2},
                "batch_plans": [
                    {
                        "plan_id": "main",
                        "tower_mode": "disallow",
                        "units": [{"unit_id": "U1", "step": 5}],
                    }
                ],
            }
            child_raw = copy.deepcopy(parent_raw)
            child_raw["batch_plans"][0]["units"][0]["step"] = 6
            parent_config = resolve_batch_config(
                loads_batch_request(json.dumps(parent_raw, ensure_ascii=False))
            )
            child_config = resolve_batch_config(
                loads_batch_request(json.dumps(child_raw, ensure_ascii=False))
            )
            parent_work = parent_config.work_items[0]
            parent_row = GenerationItemResult(
                template_key="first",
                plan_id=parent_work.plan.plan_id,
                source_plan_id=parent_work.plan.source_plan_id,
                plan_signature=parent_work.plan.signature,
                parameter_source=parent_work.parameter_source,
                generation_seed=1,
                source=None,
                inspection=None,
                candidates=("retained-candidate",),
                attempts_completed=10,
                rejection_counts={},
                target_candidate_count=2,
                status="partial",
            )
            parent_result = GenerationBatchResult(
                format_version=1,
                batch_seed=parent_config.batch.batch_seed,
                resolved_batch_hash=parent_config.batch.config_hash,
                source_config_hash=parent_config.source_config_hash,
                algorithm_version="test",
                items=(parent_row,),
                run_status="partial",
            )
            parent = JobRecord(
                job_id="job_parent_changed_plan",
                created_at="2026-09-16T12:00:00+08:00",
                import_id="import_test",
                config_payload=parent_raw,
                config=parent_config,
                sources={},
                result=parent_result,
                status="partial",
            )
            state.jobs[parent.job_id] = parent
            child = JobRecord(
                job_id="job_child_changed_plan",
                created_at="2026-09-16T12:01:00+08:00",
                import_id="import_test",
                config_payload=child_raw,
                config=child_config,
                sources={},
                parent_job_id=parent.job_id,
                retry_scope={"template_key": "first", "plan_id": "main"},
                retry_statuses=(),
            )

            carried = state._compatible_parent_result(child, parent_result)

            self.assertEqual(len(carried.items), 1)
            self.assertEqual(carried.items[0].candidates, ("retained-candidate",))
            self.assertEqual(carried.items[0].source_plan_id, "main")
            self.assertEqual(
                carried.items[0].plan_signature,
                child_config.work_items[0].plan.signature,
            )
            self.assertEqual(carried.items[0].status, "pending")

    def test_scoped_derived_job_searches_only_adjusted_template(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            first_source = _chain_source()
            second_data = first_source.level.data.replace(b"ui001", b"ui002", 1)
            imported = state.import_templates(
                {
                    "mode": "files",
                    "files": [
                        {
                            "name": "ui001_m.bytes",
                            "data_base64": base64.b64encode(
                                first_source.level.data
                            ).decode("ascii"),
                        },
                        {
                            "name": "ui002_m.bytes",
                            "data_base64": base64.b64encode(second_data).decode(
                                "ascii"
                            ),
                        },
                    ],
                }
            )
            request = {
                "schema_version": 1,
                "template_keys": ["ui001", "ui002"],
                "batch_seed": 404,
                "generation_defaults": {
                    "replacement_sources": ["A", "B"],
                    "candidate_count": 1,
                    "attempt_budget": 30,
                    "assignment_attempts": 500,
                    "unit_defaults": {"type_count": 3},
                },
                "batch_plans": [
                    {
                        "plan_id": "s4",
                        "tower_mode": "disallow",
                        "units": [{"unit_id": "U1", "step": 4}],
                    }
                ],
            }
            parent = state.create_job(
                {
                    "import_id": imported["import_id"],
                    "batch_request": request,
                }
            )
            # Whole-board paired DupPara1–10 evidence intentionally exercises
            # five fixed strategies per candidate.  Leave enough wall-clock
            # headroom for slower CI hosts without weakening product limits.
            deadline = time.monotonic() + 30
            while parent.status in {"queued", "running"} and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(parent.status, "completed", parent.error)
            parent_second = next(
                item for item in parent.result.items if item.template_key == "ui002"
            )
            parent_candidate_id = parent_second.candidates[0].candidate.candidate_id
            searched_templates: list[str] = []

            def record_search(*args, **kwargs):
                searched_templates.append(args[0].template_id)
                return core_search_candidates(*args, **kwargs)

            with patch(
                "services.job_service.search_candidates",
                side_effect=record_search,
            ), patch.object(state, "_prepare_generation_recommendations"):
                child = state.create_job(
                    {
                        "import_id": imported["import_id"],
                        "batch_request": request,
                        "parent_job_id": parent.job_id,
                        "retry_scope": {
                            "template_key": "ui001",
                            "plan_id": "s4",
                        },
                    }
                )
                deadline = time.monotonic() + 8
                while (
                    child.status in {"queued", "running"}
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.02)

            self.assertEqual(child.status, "completed", child.error)
            # A scoped row that already has its complete 1/1 candidate target
            # is inherited as-is; local continuation only fills missing slots.
            self.assertEqual(searched_templates, [])
            parent_first = next(
                item for item in parent.result.items if item.template_key == "ui001"
            )
            child_first = next(
                item for item in child.result.items if item.template_key == "ui001"
            )
            self.assertEqual(
                child_first.candidates[0].candidate.candidate_id,
                parent_first.candidates[0].candidate.candidate_id,
            )
            child_second = next(
                item for item in child.result.items if item.template_key == "ui002"
            )
            self.assertIs(child_second, parent_second)
            self.assertEqual(
                child_second.candidates[0].candidate.candidate_id,
                parent_candidate_id,
            )

    def test_multi_scope_overrides_search_only_selected_templates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            first_source = _chain_source()
            uploaded = []
            for template_key in ("ui001", "ui002", "ui003"):
                data = first_source.level.data.replace(
                    b"ui001", template_key.encode("ascii"), 1
                )
                uploaded.append(
                    {
                        "name": f"{template_key}_m.bytes",
                        "data_base64": base64.b64encode(data).decode("ascii"),
                    }
                )
            imported = state.import_templates(
                {"mode": "files", "files": uploaded}
            )
            request = {
                "schema_version": 1,
                "template_keys": ["ui001", "ui002", "ui003"],
                "batch_seed": 505,
                "generation_defaults": {
                    "replacement_sources": ["A", "B"],
                    "candidate_count": 1,
                    "attempt_budget": 30,
                    "assignment_attempts": 500,
                    "unit_defaults": {"type_count": 3},
                },
                "batch_plans": [
                    {
                        "plan_id": "s4",
                        "tower_mode": "disallow",
                        "units": [{"unit_id": "U1", "step": 4}],
                    }
                ],
            }
            parent = state.create_job(
                {
                    "import_id": imported["import_id"],
                    "batch_request": request,
                }
            )
            # This three-template fixture runs the complete paired evidence
            # pipeline; the timeout is test-runner headroom, not a product
            # search-budget change.
            deadline = time.monotonic() + 30
            while parent.status in {"queued", "running"} and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(parent.status, "completed", parent.error)
            parent_third = next(
                item for item in parent.result.items if item.template_key == "ui003"
            )
            third_candidate_id = parent_third.candidates[0].candidate.candidate_id

            # Simulate two unfinished templates while retaining one completed
            # parent candidate.  A derived multi-scope run must keep that 1/3
            # progress and search only the two requested scopes.
            unfinished_rows = []
            for item in parent.result.items:
                if item.template_key in {"ui001", "ui002"}:
                    unfinished_rows.append(
                        replace(
                            item,
                            candidates=(),
                            status="failed",
                            error={
                                "code": "NO_VALID_CANDIDATE",
                                "failure_kind": "budget_exhausted",
                                "evidence": {"attempt_budget": 30},
                            },
                        )
                    )
                else:
                    unfinished_rows.append(item)
            parent.result = replace(
                parent.result,
                items=tuple(unfinished_rows),
                run_status="partial",
            )
            parent.status = "partial"
            parent.presentation = state._present_result(parent, parent.result)

            retry_scopes = [
                {"template_key": "ui001", "plan_id": "s4"},
                {"template_key": "ui002", "plan_id": "s4"},
            ]
            overrides = [
                {
                    "template_key": "ui001",
                    "plan_id": "s4",
                    "changes": {"max_structure_layer_span": 5},
                },
                {
                    "template_key": "ui002",
                    "plan_id": "s4",
                    "changes": {"max_structure_layer_span": 6},
                },
            ]
            first_search_started = threading.Event()
            allow_search = threading.Event()
            searched_templates: list[str] = []

            def record_search(*args, **kwargs):
                searched_templates.append(args[0].template_id)
                if len(searched_templates) == 1:
                    first_search_started.set()
                    if not allow_search.wait(timeout=5):
                        raise RuntimeError("test did not release scoped search")
                return core_search_candidates(*args, **kwargs)

            with patch(
                "services.job_service.search_candidates",
                side_effect=record_search,
            ), patch.object(state, "_prepare_generation_recommendations"):
                child = state.create_job(
                    {
                        "import_id": imported["import_id"],
                        "parent_job_id": parent.job_id,
                        "retry_scopes": retry_scopes,
                        "parameter_overrides": overrides,
                    }
                )
                self.assertTrue(first_search_started.wait(timeout=5))
                initial = child.summary()
                self.assertIsNone(initial["retry_scope"])
                self.assertEqual(initial["retry_scopes"], retry_scopes)
                self.assertEqual(initial["parameter_overrides"], overrides)
                self.assertEqual(
                    initial["search"]["candidate_found_count"], 1
                )
                self.assertEqual(
                    initial["search"]["candidate_target_count"], 3
                )
                self.assertEqual(
                    initial["progress"]["completed_work_items"], 1
                )
                self.assertEqual(
                    [item.status for item in child.result.items],
                    ["pending", "pending", "success"],
                )
                allow_search.set()
                deadline = time.monotonic() + 60
                while (
                    child.status in {"queued", "running"}
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.02)

            self.assertIn(child.status, {"completed", "partial"}, child.error)
            self.assertEqual(set(searched_templates), {"ui001", "ui002"})
            child_third = next(
                item for item in child.result.items if item.template_key == "ui003"
            )
            self.assertIs(child_third, parent_third)
            self.assertEqual(
                child_third.candidates[0].candidate.candidate_id,
                third_candidate_id,
            )
            self.assertEqual(
                [item["source_plan_id"] for item in child.presentation["items"]],
                ["s4", "s4", "s4"],
            )

    def test_single_scope_candidate_target_increase_keeps_parent_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            first_source = _chain_source()
            second_data = first_source.level.data.replace(b"ui001", b"ui002", 1)
            imported = state.import_templates(
                {
                    "mode": "files",
                    "files": [
                        {
                            "name": "ui001_m.bytes",
                            "data_base64": base64.b64encode(
                                first_source.level.data
                            ).decode("ascii"),
                        },
                        {
                            "name": "ui002_m.bytes",
                            "data_base64": base64.b64encode(second_data).decode(
                                "ascii"
                            ),
                        },
                    ],
                }
            )
            request = {
                "schema_version": 1,
                "template_keys": ["ui001", "ui002"],
                "batch_seed": 606,
                "generation_defaults": {
                    "replacement_sources": ["A", "B"],
                    "candidate_count": 1,
                    "attempt_budget": 30,
                    "assignment_attempts": 500,
                    "unit_defaults": {"type_count": 3},
                },
                "batch_plans": [
                    {
                        "plan_id": "s4",
                        "tower_mode": "disallow",
                        "units": [{"unit_id": "U1", "step": 4}],
                    }
                ],
            }
            parent = state.create_job(
                {
                    "import_id": imported["import_id"],
                    "batch_request": request,
                }
            )
            # The full generator completes after the search counter reaches
            # 100%; leave room for final validation and result assembly on a
            # loaded CI/developer machine.
            deadline = time.monotonic() + 15
            while parent.status in {"queued", "running"} and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(parent.status, "completed", parent.error)
            parent_first = parent.result.items[0]
            parent_second = parent.result.items[1]
            first_candidate_id = parent_first.candidates[0].candidate.candidate_id

            child_request = json.loads(json.dumps(request))
            scoped_plan = json.loads(json.dumps(request["batch_plans"][0]))
            scoped_plan["generation"] = {
                "candidate_count": 2,
                "max_structure_layer_span": 5,
            }
            child_request["template_rules"] = [
                {
                    "template_key": "ui001",
                    "mode": "replace",
                    "plans": [scoped_plan],
                }
            ]
            first_search_started = threading.Event()
            allow_search = threading.Event()
            searched_templates: list[str] = []

            def record_search(*args, **kwargs):
                searched_templates.append(args[0].template_id)
                first_search_started.set()
                if not allow_search.wait(timeout=5):
                    raise RuntimeError("test did not release missing candidate slot")
                return core_search_candidates(*args, **kwargs)

            with patch(
                "services.job_service.search_candidates",
                side_effect=record_search,
            ), patch.object(state, "_prepare_generation_recommendations"):
                child = state.create_job(
                    {
                        "import_id": imported["import_id"],
                        "batch_request": child_request,
                        "parent_job_id": parent.job_id,
                        "retry_scopes": [
                            {"template_key": "ui001", "plan_id": "s4"}
                        ],
                    }
                )
                self.assertTrue(first_search_started.wait(timeout=5))
                initial = child.summary()
                self.assertEqual(
                    initial["search"]["candidate_found_count"], 2
                )
                self.assertEqual(
                    initial["search"]["candidate_target_count"], 3
                )
                carried_first = next(
                    item
                    for item in child.result.items
                    if item.template_key == "ui001"
                )
                self.assertEqual(carried_first.status, "pending")
                self.assertEqual(
                    carried_first.candidates[0].candidate.candidate_id,
                    first_candidate_id,
                )
                allow_search.set()
                deadline = time.monotonic() + 15
                while (
                    child.status in {"queued", "running"}
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.02)

            self.assertIn(child.status, {"completed", "partial"}, child.error)
            self.assertEqual(set(searched_templates), {"ui001"})
            child_first = next(
                item for item in child.result.items if item.template_key == "ui001"
            )
            self.assertGreaterEqual(len(child_first.candidates), 1)
            self.assertIn(
                first_candidate_id,
                {
                    generated.candidate.candidate_id
                    for generated in child_first.candidates
                },
            )
            child_second = next(
                item for item in child.result.items if item.template_key == "ui002"
            )
            self.assertIs(child_second, parent_second)

    def test_scoped_candidate_target_cannot_drop_below_retained_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            source = _chain_source()
            imported = state.import_templates(
                {
                    "mode": "files",
                    "files": [
                        {
                            "name": "ui001_m.bytes",
                            "data_base64": base64.b64encode(
                                source.level.data
                            ).decode("ascii"),
                        }
                    ],
                }
            )
            request = {
                "schema_version": 1,
                "template_keys": ["ui001"],
                "batch_seed": 808,
                "generation_defaults": {
                    "replacement_sources": ["A", "B"],
                    "candidate_count": 2,
                    "attempt_budget": 30,
                    "assignment_attempts": 500,
                    "unit_defaults": {"type_count": 3},
                },
                "batch_plans": [
                    {
                        "plan_id": "s4",
                        "tower_mode": "disallow",
                        "units": [{"unit_id": "U1", "step": 4}],
                    }
                ],
            }
            parent = state.create_job(
                {
                    "import_id": imported["import_id"],
                    "batch_request": request,
                }
            )
            deadline = time.monotonic() + 15
            while parent.status in {"queued", "running"} and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(parent.status, "completed", parent.error)
            self.assertEqual(len(parent.result.items[0].candidates), 2)

            child_request = json.loads(json.dumps(request))
            plan = json.loads(json.dumps(request["batch_plans"][0]))
            plan["generation"] = {"candidate_count": 1}
            child_request["template_rules"] = [
                {
                    "template_key": "ui001",
                    "mode": "replace",
                    "plans": [plan],
                }
            ]
            with self.assertRaises(InputValidationError) as caught:
                state.create_job(
                    {
                        "import_id": imported["import_id"],
                        "parent_job_id": parent.job_id,
                        "batch_request": child_request,
                        "retry_scopes": [
                            {"template_key": "ui001", "plan_id": "s4"}
                        ],
                    }
                )
            self.assertEqual(
                caught.exception.detail.evidence["retained_candidate_count"], 2
            )
            self.assertEqual(
                caught.exception.detail.evidence["requested_candidate_count"], 1
            )

    def test_budget_exhaustion_event_is_emitted_once_per_work_item_and_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            job = self._job_record(state)
            work = job.config.work_items[0]

            def snapshot(
                status: str,
                attempts: int,
                budget: int,
                candidate_index: int = 1,
            ):
                error = {
                    "code": "NO_CANDIDATE",
                    "message": "budget exhausted",
                    "failure_kind": "budget_exhausted",
                    "evidence": {
                        "candidate_index": candidate_index,
                        "attempt_budget": budget,
                        "slot_budget_exhausted": True,
                    },
                }
                row = GenerationItemResult(
                    template_key=work.template_key,
                    plan_id=work.plan.plan_id,
                    plan_signature=work.plan.signature,
                    parameter_source=work.parameter_source,
                    generation_seed=1,
                    source=None,
                    inspection=None,
                    candidates=(),
                    attempts_completed=attempts,
                    rejection_counts={},
                    target_candidate_count=1,
                    status=status,
                    error=error,
                    effective_attempt_budget_per_slot=budget,
                    slot_attempts_completed={1: budget},
                )
                return GenerationBatchResult(
                    format_version=1,
                    batch_seed=job.config.batch.batch_seed,
                    resolved_batch_hash=job.config.batch.config_hash,
                    source_config_hash=job.config.source_config_hash,
                    algorithm_version="test",
                    items=(row,),
                    run_status="running" if status == "running" else "failed",
                    total_work_item_count=1,
                )

            state._record_job_progress(job, snapshot("running", 30, 30))
            self.assertFalse(
                any(
                    (event["details"].get("error") or {}).get("failure_kind")
                    == "budget_exhausted"
                    for event in job.events
                )
            )
            state._record_job_progress(job, snapshot("failed", 30, 30))
            state._record_job_progress(
                job,
                snapshot("failed", 30, 30, candidate_index=2),
            )
            matching = [
                event
                for event in job.events
                if event["details"].get("attempt_budget") == 30
            ]
            self.assertEqual(len(matching), 1)

            state._record_job_progress(
                job,
                snapshot("failed", 60, 60, candidate_index=2),
            )
            matching = [
                event
                for event in job.events
                if event["details"].get("attempt_budget") in {30, 60}
            ]
            self.assertEqual(len(matching), 2)
            self.assertEqual(
                [event["details"]["attempt_budget"] for event in matching],
                [30, 60],
            )

    def test_search_observations_use_distinct_events_and_do_not_finish_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / 'settings.json')
            profile_service.initialize(Path(temporary) / 'tool')
            state = AppState(profile_service)
            job = self._job_record(state)
            work = job.config.work_items[0]
            row = GenerationItemResult(
                template_key=work.template_key, plan_id=work.plan.plan_id,
                plan_signature=work.plan.signature, parameter_source=work.parameter_source,
                generation_seed=1, source=None, inspection=None, candidates=(),
                attempts_completed=0, rejection_counts={}, target_candidate_count=1,
                status='running', search_progress={'candidate_index': 1,
                'attempts_searched': 20, 'attempt_budget': 2000})
            value = GenerationBatchResult(
                format_version=1, batch_seed=job.config.batch.batch_seed,
                resolved_batch_hash=job.config.batch.config_hash,
                source_config_hash=job.config.source_config_hash,
                algorithm_version='test', items=(row,), run_status='running')
            state._record_job_progress(job, value)
            state._record_job_progress(job, value)
            events = [event for event in job.events if event['type'] == 'candidate_search_progress']
            self.assertEqual(len(events), 1)
            self.assertIn('20 / 2000', events[0]['message'])
            for attempts in (30, 100, 999, 1000, 1010, 1999):
                progress_row = replace(row, search_progress={**row.search_progress,
                    'attempts_searched': attempts,
                    'rejection_counts': {'assignment_five_singleton_types': 1}})
                state._record_job_progress(job, replace(value, items=(progress_row,)))
            events = [event for event in job.events if event['type'] == 'candidate_search_progress']
            self.assertEqual(len(events), 2)
            self.assertIn('1000 / 2000', events[-1]['message'])
            self.assertNotIn('五种单张', events[-1]['message'])
            self.assertEqual(state._completed_work_item_count(value), 0)
            exhausted = replace(row, search_progress={**row.search_progress, 'exhausted': True})
            state._record_job_progress(job, replace(value, items=(exhausted,)))
            self.assertIn('已保留 0 个，还缺 1 个', job.events[-1]['message'])
            self.assertEqual(job.events[-1]['type'], 'candidate_search_progress')

    def test_seedless_job_keeps_exact_seed_through_probe_apply_and_regeneration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            source = _chain_source()
            imported = state.import_templates(
                {
                    "mode": "files",
                    "files": [
                        {
                            "name": "ui001_m.bytes",
                            "data_base64": base64.b64encode(source.level.data).decode(
                                "ascii"
                            ),
                        }
                    ],
                }
            )
            request = {
                "schema_version": 1,
                "template_keys": ["ui001"],
                "generation_defaults": {
                    "replacement_sources": ["A", "B"],
                    "candidate_count": 1,
                    "attempt_budget": 30,
                    "assignment_attempts": 500,
                    "unit_defaults": {"type_count": 3},
                },
                "batch_plans": [
                    {
                        "plan_id": "s4",
                        "tower_mode": "disallow",
                        "units": [{"unit_id": "U1", "step": 4}],
                    }
                ],
            }
            job = state.create_job(
                {"import_id": imported["import_id"], "batch_request": request}
            )
            deadline = time.monotonic() + 8
            while job.status in {"queued", "running"} and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(job.status, "completed", job.error)
            locked_seed = job.config.batch.batch_seed
            self.assertEqual(job.config_payload["batch_seed"], locked_seed)

            probe = build_generation_probe_request(
                job.config_payload,
                template_key="ui001",
                source_plan_id="s4",
                attempt_budget=24,
            )
            self.assertEqual(probe["batch_seed"], locked_seed)
            self.assertEqual(
                resolve_batch_config(
                    loads_batch_request(json.dumps(probe, ensure_ascii=False))
                ).batch.batch_seed,
                locked_seed,
            )

            job.parameter_recommendations = [
                {
                    "recommendation_id": "adjust_seed_test",
                    "template_key": "ui001",
                    "source_plan_id": "s4",
                    "field": "max_structure_layer_span",
                    "changes": {"max_structure_layer_span": 99},
                    "verified": True,
                    "trial_result": {"status": "passed", "candidate_count": 1},
                }
            ]
            applied = state.apply_generation_recommendation(
                job.job_id, {"recommendation_id": "adjust_seed_test"}
            )
            self.assertEqual(applied["batch_request"]["batch_seed"], str(locked_seed))

            regenerated = state.create_job(applied["continue_request"]["body"])
            self.assertEqual(regenerated.config.batch.batch_seed, locked_seed)
            self.assertEqual(regenerated.parent_job_id, job.job_id)
            self.assertEqual(
                regenerated.retry_scope,
                {"template_key": "ui001", "plan_id": "s4"},
            )
            deadline = time.monotonic() + 8
            while (
                regenerated.status in {"queued", "running"}
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            self.assertEqual(regenerated.status, "completed", regenerated.error)

    def test_recycle_config_conflicts_return_verified_preflight_drafts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            source = _chain_source()
            imported = state.import_templates(
                {
                    "mode": "files",
                    "files": [
                        {
                            "name": "ui001_m.bytes",
                            "data_base64": base64.b64encode(
                                source.level.data
                            ).decode("ascii"),
                        }
                    ],
                }
            )
            cases = (
                (
                    "insufficient_tiles_for_recycles",
                    {
                        "type_count": 3,
                        "required_pre_gate_recycles": 2,
                    },
                    "required_pre_gate_recycles",
                    1,
                ),
                (
                    "first_recycle_lead_impossible",
                    {
                        "type_count": 3,
                        "required_pre_gate_recycles": 1,
                        "first_recycle_lead_tiles": 5,
                    },
                    "first_recycle_lead_tiles",
                    4,
                ),
            )
            for error_code, unit_defaults, field, after in cases:
                with self.subTest(error_code=error_code):
                    request = {
                        "schema_version": 1,
                        "template_keys": ["ui001"],
                        "batch_seed": 99,
                        "generation_defaults": {
                            "replacement_sources": ["A", "B"],
                            "candidate_count": 1,
                            "attempt_budget": 30,
                            "assignment_attempts": 500,
                            "unit_defaults": unit_defaults,
                        },
                        "batch_plans": [
                            {
                                "plan_id": "s5",
                                "tower_mode": "disallow",
                                "units": [{"unit_id": "U1", "step": 5}],
                            }
                        ],
                    }
                    preflight = state.resolve_plan(
                        {
                            "import_id": imported["import_id"],
                            "batch_request": request,
                        }
                    )
                    self.assertFalse(preflight["valid"])
                    self.assertEqual(preflight["error"]["code"], error_code)
                    self.assertEqual(preflight["import_id"], imported["import_id"])
                    self.assertFalse(preflight["recommendations_pending"])
                    self.assertEqual(
                        preflight["recommendation_summary"]["verified_count"], 1
                    )
                    (card,) = preflight["parameter_recommendations"]
                    self.assertEqual(card["field"], field)
                    self.assertEqual(card["after"], after)
                    self.assertTrue(card["verified"])
                    self.assertEqual(card["trial_result"]["status"], "passed")

                    applied = state.apply_preflight_generation_recommendation(
                        preflight["preflight_id"],
                        {"recommendation_id": card["recommendation_id"]},
                    )
                    self.assertEqual(applied["import_id"], imported["import_id"])
                    self.assertEqual(applied["differences"][0]["after"], after)
                    self.assertEqual(applied["batch_request"]["batch_seed"], "99")
                    self.assertTrue(
                        state.resolve_plan(
                            {"batch_request": applied["batch_request"]}
                        )["valid"]
                    )
                    undone = state.undo_preflight_generation_recommendation(
                        preflight["preflight_id"],
                        {"undo_token": applied["undo_token"]},
                    )
                    self.assertTrue(undone["undone"])
                    self.assertEqual(undone["batch_request"]["batch_seed"], "99")

    def test_preflight_aggregates_multiunit_edits_and_keeps_templates_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            first_source = _chain_source(30)
            second_data = first_source.level.data.replace(b"ui001", b"ui002", 1)
            imported = state.import_templates(
                {
                    "mode": "files",
                    "files": [
                        {
                            "name": "ui001_m.bytes",
                            "data_base64": base64.b64encode(
                                first_source.level.data
                            ).decode("ascii"),
                        },
                        {
                            "name": "ui002_m.bytes",
                            "data_base64": base64.b64encode(second_data).decode(
                                "ascii"
                            ),
                        },
                    ],
                }
            )
            request = {
                "schema_version": 1,
                "template_keys": ["ui001", "ui002"],
                "batch_seed": 99,
                "generation_defaults": {
                    "replacement_sources": ["A", "B"],
                    "candidate_count": 1,
                    "attempt_budget": 30,
                    "assignment_attempts": 500,
                    "unit_defaults": {
                        "type_count": 3,
                        "required_pre_gate_recycles": 2,
                    },
                },
                "batch_plans": [
                    {
                        "plan_id": "dual-s5",
                        "layout": "x",
                        "tower_mode": "disallow",
                        "units": [
                            {"unit_id": "U1", "step": 5},
                            {"unit_id": "U2", "step": 5},
                        ],
                    }
                ],
            }
            successful_trial = SimpleNamespace(
                items=(SimpleNamespace(candidates=(object(),)),)
            )
            with patch("app.generate_batch", return_value=successful_trial):
                first = state.resolve_plan(
                    {
                        "import_id": imported["import_id"],
                        "batch_request": request,
                    }
                )
                (first_card,) = first["parameter_recommendations"]
                self.assertEqual(first_card["template_key"], "ui001")
                self.assertEqual(first_card["affected_units"], [1, 2])
                self.assertEqual(len(first_card["unit_adjustments"]), 2)
                self.assertTrue(first_card["verified"])
                first_applied = state.apply_preflight_generation_recommendation(
                    first["preflight_id"],
                    {"recommendation_id": first_card["recommendation_id"]},
                )
                self.assertEqual(len(first_applied["differences"]), 2)
                self.assertEqual(
                    [
                        rule["template_key"]
                        for rule in first_applied["batch_request"]["template_rules"]
                    ],
                    ["ui001"],
                )

                second = state.resolve_plan(
                    {
                        "import_id": imported["import_id"],
                        "batch_request": first_applied["batch_request"],
                    }
                )
                self.assertFalse(second["valid"])
                (second_card,) = second["parameter_recommendations"]
                self.assertEqual(second_card["template_key"], "ui002")
                second_applied = state.apply_preflight_generation_recommendation(
                    second["preflight_id"],
                    {"recommendation_id": second_card["recommendation_id"]},
                )
                self.assertEqual(
                    [
                        rule["template_key"]
                        for rule in second_applied["batch_request"]["template_rules"]
                    ],
                    ["ui001", "ui002"],
                )
                final = state.resolve_plan(
                    {
                        "import_id": imported["import_id"],
                        "batch_request": second_applied["batch_request"],
                    }
                )
                self.assertTrue(final["valid"])

    def test_finish_partial_is_accepted_while_paused_and_retains_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            job = self._job_record(state)
            item = SimpleNamespace(
                status="running",
                candidates=(object(),),
                error=None,
            )
            job.result = SimpleNamespace(
                items=(item,),
                as_dict=lambda: {"summary": {"run_status": "running"}},
            )
            job.status = "pause_requested"
            self.assertTrue(job.control.request_pause())
            self.assertTrue(job.summary()["actions"]["can_finish_partial"])

            state.control_job(job.job_id, "finish_partial")

            self.assertEqual(job.status, "cancel_requested")
            self.assertEqual(job.control.state, "cancel_requested")
            self.assertTrue(
                job.summary()["events"][-1]["details"]["retain_partial_results"]
            )

    def test_recent_export_resolution_is_pinned_and_marker_persists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            job = self._job_record(state)
            manifest_a = {
                "source_template_id": "ui001",
                "output_template_id": "aipuzzleui001s4",
                "signature": "signature-a",
                "replacements": [{"source_tile_id": -10}],
            }
            manifest_b = {
                "source_template_id": "ui001",
                "output_template_id": "aipuzzleui001s4v2",
                "signature": "signature-b",
                "replacements": [{"source_tile_id": -11}],
            }
            export_a = ExportRunRecord(
                export_id="export_a",
                job_id=job.job_id,
                created_at="2026-09-17T10:00:00+08:00",
                idempotency_key="export-key-a",
                payload={},
                status="completed",
                result={"state": "completed"},
                exported_manifests=[manifest_a],
            )
            export_b = ExportRunRecord(
                export_id="export_b",
                job_id=job.job_id,
                created_at="2026-09-17T10:01:00+08:00",
                idempotency_key="export-key-b",
                payload={},
                status="completed",
                result={"state": "completed"},
                exported_manifests=[manifest_b],
            )
            state.exports = {
                export_a.export_id: export_a,
                export_b.export_id: export_b,
            }
            # The legacy job cache deliberately points at B.  Resolving A must
            # still use A's own manifests rather than the job's latest export.
            job.exported_manifests = [manifest_b]

            with self.assertRaises(InputValidationError):
                state.resolve_parameter_templates(
                    {"source": "recent_export", "job_id": job.job_id}
                )
            with self.assertRaises(InputValidationError):
                state.resolve_parameter_templates(
                    {
                        "source": "recent_export",
                        "job_id": "job_other",
                        "export_id": export_a.export_id,
                    }
                )

            selected = state.resolve_parameter_templates(
                {
                    "source": "recent_export",
                    "job_id": job.job_id,
                    "export_id": export_a.export_id,
                }
            )
            self.assertEqual(selected["source_export_id"], export_a.export_id)
            self.assertEqual(selected["source_job_id"], job.job_id)
            self.assertFalse(selected["parameter_configured"])
            self.assertIsNone(selected["parameter_configured_at"])
            self.assertEqual(
                [item["output_template_id"] for item in selected["items"]],
                [manifest_a["output_template_id"]],
            )

            recommendation = {
                "schema_version": 1,
                "rows": [],
                "warnings": [],
            }
            with patch(
                "app.build_parameter_recommendations",
                return_value=recommendation,
            ) as build:
                calculated = state.calculate_parameters(
                    {
                        "selection_id": selected["selection_id"],
                        "config_set_id": "release",
                        "level_start": 1,
                        "level_end": 10,
                        "strategy": "balanced",
                    }
                )
            self.assertEqual(
                build.call_args.args[1][0]["output_template_id"],
                manifest_a["output_template_id"],
            )
            marker = calculated["parameter_configured_at"]
            self.assertTrue(marker)
            self.assertTrue(calculated["parameter_configured"])
            self.assertEqual(calculated["source_export_id"], export_a.export_id)
            self.assertEqual(export_a.parameter_configured_at, marker)
            self.assertIsNone(export_b.parameter_configured_at)
            self.assertEqual(export_a.summary()["parameter_configured_at"], marker)
            self.assertEqual(export_a.summary()["source_export_id"], "export_a")
            self.assertIn(
                "parameter_configured",
                {event["type"] for event in export_a.summary()["events"]},
            )
            resolved_again = state.resolve_parameter_templates(
                {
                    "source": "recent_export",
                    "export_id": export_a.export_id,
                }
            )
            self.assertEqual(resolved_again["parameter_configured_at"], marker)
            self.assertTrue(resolved_again["parameter_configured"])

    def test_recent_export_job_only_contract_is_compatible_when_unambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            job = self._job_record(state)
            manifest = {
                "source_template_id": "ui001",
                "output_template_id": "aipuzzleui001s4",
                "replacements": [{"source_tile_id": -10}],
            }
            export = ExportRunRecord(
                export_id="export_only",
                job_id=job.job_id,
                created_at="2026-09-17T10:00:00+08:00",
                idempotency_key="export-only-key",
                payload={},
                status="completed",
                result={"state": "completed"},
                exported_manifests=[manifest],
            )
            state.exports[export.export_id] = export

            selected = state.resolve_parameter_templates(
                {"source": "recent_export", "job_id": job.job_id}
            )

            self.assertEqual(selected["source_export_id"], export.export_id)
            self.assertEqual(selected["source_job_id"], job.job_id)
            self.assertEqual(
                selected["items"][0]["output_template_id"],
                manifest["output_template_id"],
            )

    def test_legacy_job_manifest_remains_readable_without_export_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            job = self._job_record(state)
            job.exported_manifests = [
                {
                    "source_template_id": "ui001",
                    "output_template_id": "aipuzzleui001s4",
                    "replacements": [{"source_tile_id": -10}],
                }
            ]

            selected = state.resolve_parameter_templates(
                {"source": "recent_export", "job_id": job.job_id}
            )

            self.assertIsNone(selected["source_export_id"])
            self.assertEqual(selected["source_job_id"], job.job_id)
            self.assertFalse(selected["parameter_configured"])

    def test_parameter_level_range_requires_exact_decimal_integers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            state.parameter_selections["params_exact_int"] = {
                "manifests": [],
                "source_export_id": None,
                "source_job_id": None,
            }
            with patch(
                "app.build_parameter_recommendations",
                return_value={"schema_version": 1, "rows": [], "warnings": []},
            ) as build:
                state.calculate_parameters(
                    {
                        "selection_id": "params_exact_int",
                        "config_set_id": "release",
                        "level_start": "200",
                        "level_end": 300,
                        "strategy": "fidelity",
                    }
                )
            self.assertEqual(build.call_args.kwargs["level_start"], 200)
            self.assertEqual(build.call_args.kwargs["level_end"], 300)

            for invalid in (True, 200.0, "200.5", "", None):
                with self.subTest(value=invalid):
                    with self.assertRaises(InputValidationError):
                        state.calculate_parameters(
                            {
                                "selection_id": "params_exact_int",
                                "config_set_id": "release",
                                "level_start": invalid,
                                "level_end": 300,
                                "strategy": "fidelity",
                            }
                        )

    def test_unlaunched_lookup_uses_selection_source_ids_and_marks_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            job = self._job_record(state)
            manifest = {
                "source_template_id": "ui001",
                "output_template_id": "aipuzzleui001s4",
                "signature": "signature-a",
                "replacements": [{"source_tile_id": -10}],
            }
            export = ExportRunRecord(
                export_id="export_unlaunched",
                job_id=job.job_id,
                created_at="2026-09-17T10:00:00+08:00",
                idempotency_key="export-key-unlaunched",
                payload={},
                status="completed",
                result={"state": "completed"},
                exported_manifests=[manifest],
            )
            state.exports[export.export_id] = export
            selected = state.resolve_parameter_templates(
                {"source": "recent_export", "export_id": export.export_id}
            )
            lookup_result = {
                "schema_version": 1,
                "rows": [],
                "warnings": [],
                "requested_template_ids": ["ui001"],
            }

            with patch(
                "app.lookup_unlaunched_template_parameters",
                return_value=lookup_result,
            ) as lookup:
                result = state.lookup_unlaunched_parameters(
                    {"selection_id": selected["selection_id"]}
                )

            self.assertEqual(lookup.call_args.args[1], ["ui001"])
            self.assertIsNone(lookup.call_args.kwargs["config_set_id"])
            self.assertEqual(result["source_export_id"], export.export_id)
            self.assertTrue(result["parameter_configured"])
            self.assertTrue(export.parameter_configured_at)

    def test_queued_job_cancel_becomes_terminal_without_worker_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            job = self._job_record(state)
            self.assertEqual(job.status, "queued")

            state.control_job(job.job_id, "cancel")

            self.assertEqual(job.status, "cancelled")
            self.assertEqual(job.control.state, "cancelled")
            self.assertIsNotNone(job.completed_at)
            self.assertIsNotNone(job.result)
            self.assertEqual(job.result.run_status, "cancelled")
            self.assertEqual(
                [item.status for item in job.result.items], ["cancelled"]
            )
            result_summary = job.summary()["result_summary"]
            self.assertEqual(result_summary["work_item_count"], 1)
            self.assertEqual(result_summary["cancelled_count"], 1)
            self.assertEqual(result_summary["classified_work_item_count"], 1)
            self.assertEqual(job.summary()["events"][-1]["type"], "cancelled")

            # A daemon thread that was scheduled just after cancellation must
            # observe the terminal state and never revive the job.
            state._run_job(job.job_id)
            self.assertEqual(job.status, "cancelled")

    def test_pause_finish_partial_reaches_exportable_terminal_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            source = _chain_source()
            imported = state.import_templates(
                {
                    "mode": "files",
                    "files": [
                        {
                            "name": "ui001_m.bytes",
                            "data_base64": base64.b64encode(source.level.data).decode(
                                "ascii"
                            ),
                        }
                    ],
                }
            )
            second_slot_ready = threading.Event()
            allow_pause_checkpoint = threading.Event()

            def pause_before_second_slot(*args, **kwargs):
                if kwargs.get("candidate_index") == 2:
                    second_slot_ready.set()
                    if not allow_pause_checkpoint.wait(timeout=5):
                        raise RuntimeError("test did not release second candidate slot")
                    kwargs["cooperative_checkpoint"]("test_finish_partial")
                return core_search_candidates(*args, **kwargs)

            with patch(
                "services.job_service.search_candidates",
                side_effect=pause_before_second_slot,
            ):
                job = state.create_job(
                    {
                        "import_id": imported["import_id"],
                        "batch_request": {
                            "schema_version": 1,
                            "template_keys": ["ui001"],
                            "generation_defaults": {
                                "replacement_sources": ["A", "B"],
                                "candidate_count": 2,
                                "attempt_budget": 30,
                                "assignment_attempts": 500,
                                "unit_defaults": {"type_count": 3},
                            },
                            "batch_plans": [
                                {
                                    "plan_id": "s4",
                                    "tower_mode": "disallow",
                                    "units": [{"unit_id": "U1", "step": 4}],
                                }
                            ],
                        },
                    }
                )
                self.assertTrue(second_slot_ready.wait(timeout=5))
                state.control_job(job.job_id, "pause")
                allow_pause_checkpoint.set()
                deadline = time.monotonic() + 5
                while job.control.state != "paused" and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(job.control.state, "paused")
                state.control_job(job.job_id, "finish_partial")
                deadline = time.monotonic() + 8
                while (
                    job.status
                    in {"queued", "running", "pause_requested", "cancel_requested"}
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.02)

            self.assertEqual(job.status, "partial", job.error)
            self.assertEqual(job.result.run_status, "partial")
            item = job.result.items[0]
            self.assertEqual(item.status, "partial")
            self.assertEqual(len(item.candidates), 1)
            candidate_id = item.candidates[0].candidate.candidate_id
            with patch("app.threading.Thread") as thread_factory:
                export_run = state.create_export_run(
                    job.job_id,
                    {
                        "selections": [
                            {
                                "template_key": item.template_key,
                                "resolved_plan_id": item.plan_id,
                                "candidate_id": candidate_id,
                            }
                        ]
                    },
                )
            thread_factory.return_value.start.assert_called_once_with()
            self.assertEqual(export_run.status, "queued")

    def test_web_seed_round_trips_as_exact_uint64_decimal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile_service.initialize(Path(temporary) / "tool")
            state = AppState(profile_service)
            resolved = state.resolve_plan(
                {
                    "schema_version": 1,
                    "template_keys": ["seedtest"],
                    "batch_seed": "18446744073709551615",
                    "batch_plans": [
                        {"plan_id": "s4", "units": [{"step": 4}]}
                    ],
                }
            )
            self.assertEqual(resolved["batch_seed"], "18446744073709551615")

    def test_uploaded_template_generates_comparable_playable_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile_service = ProfileService(Path(temporary) / "settings.json")
            profile = profile_service.initialize(Path(temporary) / "tool")
            ulinkconfig_root = Path(temporary) / "ulinkconfig"
            ulinkconfig_root.mkdir()
            duplicate_values = [f"3|{index}|100|3" for index in range(1, 11)]
            (ulinkconfig_root / "config_level.csv").write_text(
                "\n".join(
                    (
                        ",".join(
                            [
                                "LevelId",
                                "TemplateId",
                                *(f"DupPara{index}" for index in range(1, 11)),
                            ]
                        ),
                        ",".join(["string"] * 12),
                        ",".join(["说明"] * 12),
                        ",".join(["100", "ui001", *duplicate_values]),
                    )
                )
                + "\n",
                encoding="utf-8-sig",
            )
            profile_service.configure_ulinkconfig(
                profile.profile_id, ulinkconfig_root
            )
            state = AppState(profile_service)
            source = _chain_source()
            imported = state.import_templates(
                {
                    "mode": "files",
                    "files": [
                        {
                            "name": "ui001_m.bytes",
                            "data_base64": base64.b64encode(source.level.data).decode("ascii"),
                        }
                    ],
                }
            )
            job = state.create_job(
                {
                    "import_id": imported["import_id"],
                    "batch_request": {
                        "schema_version": 1,
                        "template_keys": ["ui001"],
                        "batch_seed": 99,
                        "generation_defaults": {
                            "replacement_sources": ["A", "B"],
                            "candidate_count": 2,
                            "attempt_budget": 30,
                            "assignment_attempts": 500,
                            "unit_defaults": {"type_count": 3},
                        },
                        "batch_plans": [
                            {
                                "plan_id": "s4",
                                "tower_mode": "disallow",
                                "units": [{"unit_id": "U1", "step": 4}],
                            }
                        ],
                    },
                }
            )
            deadline = time.monotonic() + 15
            while job.status in {"queued", "running"} and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(job.status, "completed", job.error)
            job_summary = job.summary()
            self.assertIsNotNone(job_summary["last_progress_at"])
            self.assertEqual(
                job_summary["last_progress_at"], job_summary["completed_at"]
            )
            self.assertEqual(
                job_summary["progress"],
                {
                    "completed_work_items": 1,
                    "total_work_items": 1,
                    "percent": 100,
                    "current_work_item": None,
                },
            )
            # start + at least one real generation callback + completion
            self.assertGreaterEqual(job.revision, 3)
            job_events = job_summary["events"]
            self.assertTrue(
                {
                    "queued",
                    "started",
                    "work_item_started",
                    "work_item_progress",
                    "completed",
                }
                <= {event["type"] for event in job_events}
            )
            current_events = [
                event
                for event in job_events
                if event["type"] == "work_item_started"
            ]
            self.assertEqual(len(current_events), 1)
            self.assertEqual(
                current_events[0]["details"],
                {
                    "index": 1,
                    "total": 1,
                    "template_key": "ui001",
                    "plan_id": "s4",
                    "plan_signature": "s4",
                },
            )
            self.assertEqual(
                [event["sequence"] for event in job_events],
                sorted(event["sequence"] for event in job_events),
            )
            self.assertEqual(
                [event["at"] for event in job_events],
                sorted(event["at"] for event in job_events),
            )
            item = job.presentation["items"][0]
            self.assertEqual(len(item["candidates"]), 2)
            self.assertEqual(item["comparison"]["candidate_count"], 2)
            board = item["candidates"][0]["board"]
            self.assertTrue(board["solver_traces"]["available"])
            self.assertTrue(board["solver_traces"]["units"][0]["threshold_trace"]["steps"])

            candidate_ids = [
                candidate["candidate"]["candidate_id"]
                for candidate in item["candidates"]
            ]
            export_run = state.create_export_run(
                job.job_id,
                {
                    "selections": [
                        {
                            "template_key": item["template_key"],
                            "resolved_plan_id": item["plan_id"],
                            "candidate_id": candidate_id,
                            # The deterministic fallback simulator is deliberately
                            # labelled synthetic.  The UI asks for this explicit
                            # acknowledgement before an otherwise valid candidate
                            # is exported; exercise the same contract here.
                            "manual_evidence_override": True,
                        }
                        for candidate_id in candidate_ids
                    ]
                },
            )
            deadline = time.monotonic() + 8
            while (
                export_run.status in {"queued", "running"}
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            self.assertEqual(export_run.status, "completed", export_run.error)
            export_summary = export_run.summary()
            self.assertEqual(
                [
                    selection["candidate_id"]
                    for selection in export_summary["locked_selections"]
                ],
                candidate_ids,
            )
            self.assertEqual(
                export_summary["selection_summary"]["candidate_count"], 2
            )
            self.assertEqual(
                export_summary["selection_summary"]["work_item_count"], 1
            )
            self.assertIsNotNone(export_summary["last_progress_at"])
            self.assertEqual(
                export_summary["last_progress_at"], export_summary["completed_at"]
            )
            # start + rebuild/export progress callbacks + completion
            self.assertGreaterEqual(export_run.revision, 4)
            export_events = export_summary["events"]
            self.assertTrue(
                {
                    "queued",
                    "started",
                    "rebuild_progress",
                    "rebuild_completed",
                    "candidate_export_progress",
                    "commit",
                    "completed",
                }
                <= {event["type"] for event in export_events}
            )
            self.assertEqual(
                [event["sequence"] for event in export_events],
                sorted(event["sequence"] for event in export_events),
            )
            exported = export_run.result
            self.assertIsNotNone(exported)
            self.assertEqual(exported["state"], "completed")
            self.assertEqual(
                [output["candidate_id"] for output in exported["outputs"]],
                candidate_ids,
            )
            self.assertEqual(
                [output["output_template_id"] for output in exported["outputs"]],
                ["aipuzzleui001s4", "aipuzzleui001s4v2"],
            )
            parameter_selection = state.resolve_parameter_templates(
                {
                    "source": "recent_export",
                    "job_id": job.job_id,
                    "export_id": export_run.export_id,
                }
            )
            self.assertEqual(
                parameter_selection["source_export_id"], export_run.export_id
            )
            self.assertEqual(export_summary["manifest_count"], 2)
            self.assertEqual(len(parameter_selection["items"]), 2)
            self.assertEqual(
                [
                    selected["output_template_id"]
                    for selected in parameter_selection["items"]
                ],
                [output["output_template_id"] for output in exported["outputs"]],
            )

            # Control events represent accepted backend state transitions. Mock
            # only retry thread construction so this assertion remains local
            # and deterministic.
            job.control = CooperativeTaskControl()
            job.status = "running"
            job.worker = None
            state.control_job(job.job_id, "pause")
            state.control_job(job.job_id, "resume")
            state.control_job(job.job_id, "cancel")
            job.status = "failed"
            job.result = None
            with patch("app.threading.Thread") as thread_factory:
                state.control_job(job.job_id, "retry")
                thread_factory.return_value.start.assert_called_once_with()
            self.assertTrue(
                {"pause", "resume", "cancel", "retry"}
                <= {event["type"] for event in job.summary()["events"]}
            )

            export_run.control = CooperativeTaskControl()
            export_run.status = "running"
            export_run.worker = None
            state.control_export(export_run.export_id, "pause")
            state.control_export(export_run.export_id, "resume")
            state.control_export(export_run.export_id, "cancel")
            export_run.status = "failed"
            with patch("app.threading.Thread") as thread_factory:
                state.control_export(export_run.export_id, "retry")
                thread_factory.return_value.start.assert_called_once_with()
            self.assertTrue(
                {"pause", "resume", "cancel", "retry"}
                <= {event["type"] for event in export_run.summary()["events"]}
            )

            before = len(job.summary()["events"])
            added = job.add_event(
                "work_item_progress",
                status="success",
                message="duplicate-test",
                dedupe_scope="duplicate-test",
                dedupe_value=(1, "success"),
            )
            duplicate = job.add_event(
                "work_item_progress",
                status="success",
                message="duplicate-test",
                dedupe_scope="duplicate-test",
                dedupe_value=(1, "success"),
            )
            self.assertTrue(added)
            self.assertFalse(duplicate)
            self.assertEqual(len(job.summary()["events"]), before + 1)
            for index in range(120):
                job.add_event(
                    "test_progress",
                    status="running",
                    message=str(index),
                )
            bounded_events = job.summary()["events"]
            self.assertEqual(len(bounded_events), 100)
            sequences = [event["sequence"] for event in bounded_events]
            self.assertTrue(all(left < right for left, right in zip(sequences, sequences[1:])))


class AppServerTests(unittest.TestCase):
    def test_cancelled_job_source_route_is_read_only_and_scoped(self) -> None:
        job = AppStateTests._job_record(self.state)
        job.sources["ui001"] = _chain_source()
        job.status = "cancelled"
        with urllib.request.urlopen(f"{self.base}/api/v1/jobs/{job.job_id}/source/ui001") as response:
            preview = json.load(response)
        self.assertTrue(preview["board"]["tiles"])
        self.assertEqual(job.status, "cancelled")
        self.assertIsNone(job.worker)
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(f"{self.base}/api/v1/jobs/{job.job_id}/source/other")

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        settings = Path(self.temporary.name) / "settings.json"
        profile_service = ProfileService(settings)
        profile_service.initialize(Path(self.temporary.name) / "tool")
        self.state = AppState(profile_service)
        try:
            self.server = AppServer(("127.0.0.1", 0), self.state)
        except PermissionError:
            self.temporary.cleanup()
            self.skipTest("当前沙箱禁止监听本机端口")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def test_bootstrap_and_static_entry_are_same_origin_hardened(self) -> None:
        with urllib.request.urlopen(f"{self.base}/") as response:
            html = response.read().decode("utf-8")
            self.assertIn("关键结构生成", html)
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])

        with urllib.request.urlopen(f"{self.base}/api/v1/bootstrap") as response:
            payload = json.load(response)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["app_version"], "0.2.1")
        self.assertTrue(payload["csrf_token"])
        self.assertIn("profile_status", payload)

    def test_frontend_assets_are_served_from_the_isolated_frontend_directory(self) -> None:
        expected = {
            "/assets/styles.css": "text/css",
            "/assets/app.js": "text/javascript",
            "/assets/fonts/InterVariable.woff2": "font/woff2",
            "/assets/fonts/SourceHanSansSC-VF.woff2": "font/woff2",
        }
        for path, content_type in expected.items():
            with self.subTest(path=path):
                with urllib.request.urlopen(f"{self.base}{path}") as response:
                    self.assertGreater(len(response.read()), 0)
                    self.assertEqual(
                        response.headers.get_content_type(), content_type
                    )
                    self.assertEqual(
                        response.headers["X-Content-Type-Options"], "nosniff"
                    )

    def test_frontend_guards_imports_and_resume_fallbacks(self) -> None:
        with urllib.request.urlopen(f"{self.base}/assets/app.js") as response:
            javascript = response.read().decode("utf-8")

        self.assertIn("function isValidTemplateId(value)", javascript)
        self.assertIn("/^[A-Za-z0-9_-]+$/", javascript)
        self.assertIn("normalized.length <= 180", javascript)
        self.assertIn("Number(file?.size) > 0", javascript)
        self.assertIn("slot?.resumable !== false", javascript)
        self.assertNotIn("Number(error?.status) === 404", javascript)
        self.assertIn("updateImportActionState();", javascript)
        self.assertNotIn("const fallbackOrder =", javascript)
        self.assertIn("const recommended = explicitRank > 0;", javascript)

    def test_frontend_asset_route_does_not_expose_non_font_files(self) -> None:
        for path in (
            "/assets/fonts/INTER-LICENSE.txt",
            "/assets/fonts/../app.js",
            "/assets/unknown.js",
        ):
            with self.subTest(path=path):
                with self.assertRaises(urllib.error.HTTPError) as captured:
                    urllib.request.urlopen(f"{self.base}{path}")
                self.assertEqual(captured.exception.code, 404)

    def test_mutation_rejects_missing_csrf(self) -> None:
        request = urllib.request.Request(
            f"{self.base}/api/v1/templates/inspect",
            method="POST",
            data=json.dumps({"mode": "template_ids", "template_ids": ["x"]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(request)
        self.assertEqual(captured.exception.code, 400)
        payload = json.loads(captured.exception.read().decode("utf-8"))
        self.assertFalse(payload["ok"])
        self.assertIn("会话", payload["error"]["message"])

    def test_mutation_rejects_duplicate_json_fields(self) -> None:
        request = urllib.request.Request(
            f"{self.base}/api/v1/profile/output",
            method="POST",
            data=b'{"output_root":"first","output_root":"second"}',
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": self.state.csrf_token,
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(request)
        self.assertEqual(captured.exception.code, 400)
        payload = json.loads(captured.exception.read().decode("utf-8"))
        self.assertFalse(payload["ok"])
        self.assertIn("重复字段", payload["error"]["message"])
        self.assertEqual(payload["error"]["evidence"]["field"], "output_root")

    def test_http_completed_export_can_open_its_recorded_directory(self) -> None:
        output_directory = Path(self.temporary.name) / "exported"
        output_directory.mkdir()
        record = ExportRunRecord(
            export_id="export_http_open",
            job_id="job_http_open",
            created_at="2026-10-13T12:00:00+08:00",
            idempotency_key="export-http-open-key",
            payload={},
            output_directory=str(output_directory),
            status="completed",
            result={"state": "completed", "outputs": []},
        )
        self.state.exports[record.export_id] = record
        request = urllib.request.Request(
            f"{self.base}/api/v1/exports/{record.export_id}/open-directory",
            method="POST",
            data=json.dumps({"path": "/tmp/untrusted-client-path"}).encode(
                "utf-8"
            ),
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": self.state.csrf_token,
            },
        )

        with patch("app.open_directory", return_value=output_directory) as opener:
            with urllib.request.urlopen(request) as response:
                payload = json.load(response)

        opener.assert_called_once_with(output_directory)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["export_id"], record.export_id)
        self.assertEqual(payload["path"], str(output_directory))

    def test_http_preflight_recommendation_can_be_applied_and_undone(self) -> None:
        source = _chain_source()
        imported = self.state.import_templates(
            {
                "mode": "files",
                "files": [
                    {
                        "name": "ui001_m.bytes",
                        "data_base64": base64.b64encode(source.level.data).decode(
                            "ascii"
                        ),
                    }
                ],
            }
        )

        def post(path: str, body: dict) -> dict:
            request = urllib.request.Request(
                f"{self.base}/api/v1{path}",
                method="POST",
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-CSRF-Token": self.state.csrf_token,
                },
            )
            with urllib.request.urlopen(request) as response:
                return json.load(response)

        resolved = post(
            "/plans/resolve",
            {
                "import_id": imported["import_id"],
                "batch_request": {
                    "schema_version": 1,
                    "template_keys": ["ui001"],
                    "batch_seed": "99",
                    "generation_defaults": {
                        "replacement_sources": ["A", "B"],
                        "candidate_count": 1,
                        "attempt_budget": 30,
                        "assignment_attempts": 500,
                        "unit_defaults": {
                            "type_count": 3,
                            "required_pre_gate_recycles": 2,
                        },
                    },
                    "batch_plans": [
                        {
                            "plan_id": "s5",
                            "tower_mode": "disallow",
                            "units": [{"unit_id": "U1", "step": 5}],
                        }
                    ],
                },
            },
        )
        self.assertTrue(resolved["ok"])
        self.assertFalse(resolved["valid"])
        (card,) = resolved["parameter_recommendations"]
        self.assertTrue(card["verified"])
        preflight_id = resolved["preflight_id"]

        with urllib.request.urlopen(
            f"{self.base}/api/v1/preflights/{preflight_id}"
        ) as response:
            snapshot = json.load(response)
        self.assertEqual(snapshot["preflight_id"], preflight_id)
        self.assertEqual(snapshot["recommendation_summary"]["verified_count"], 1)

        applied = post(
            f"/preflights/{preflight_id}/recommendations/apply",
            {"recommendation_id": card["recommendation_id"]},
        )
        self.assertEqual(applied["differences"][0]["after"], 1)
        undone = post(
            f"/preflights/{preflight_id}/recommendations/undo",
            {"undo_token": applied["undo_token"]},
        )
        self.assertTrue(undone["undone"])

    def test_http_create_job_accepts_two_retry_scopes_without_legacy_scope(self) -> None:
        source = _chain_source()
        second_data = source.level.data.replace(b"ui001", b"ui002", 1)
        imported = self.state.import_templates(
            {
                "mode": "files",
                "files": [
                    {
                        "name": "ui001_m.bytes",
                        "data_base64": base64.b64encode(source.level.data).decode(
                            "ascii"
                        ),
                    },
                    {
                        "name": "ui002_m.bytes",
                        "data_base64": base64.b64encode(second_data).decode(
                            "ascii"
                        ),
                    },
                ],
            }
        )
        batch_request = {
            "schema_version": 1,
            "template_keys": ["ui001", "ui002"],
            "batch_seed": 707,
            "generation_defaults": {
                "replacement_sources": ["A", "B"],
                "candidate_count": 1,
                "attempt_budget": 30,
                "assignment_attempts": 500,
                "unit_defaults": {"type_count": 3},
            },
            "batch_plans": [
                {
                    "plan_id": "s4",
                    "tower_mode": "disallow",
                    "units": [{"unit_id": "U1", "step": 4}],
                }
            ],
        }
        parent = self.state.create_job(
            {
                "import_id": imported["import_id"],
                "batch_request": batch_request,
            }
        )
        deadline = time.monotonic() + 8
        while parent.status in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(parent.status, "completed", parent.error)
        retry_scopes = [
            {"template_key": "ui001", "plan_id": "s4"},
            {"template_key": "ui002", "plan_id": "s4"},
        ]
        request = urllib.request.Request(
            f"{self.base}/api/v1/jobs",
            method="POST",
            data=json.dumps(
                {
                    "import_id": imported["import_id"],
                    "parent_job_id": parent.job_id,
                    "batch_request": batch_request,
                    "retry_scopes": retry_scopes,
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": self.state.csrf_token,
            },
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 202)
            payload = json.load(response)

        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["retry_scope"])
        self.assertEqual(payload["retry_scopes"], retry_scopes)
        child = self.state.get_job(payload["job_id"])
        self.assertEqual(child.retry_scopes, tuple(retry_scopes))
        self.assertIsNone(child.retry_scope)

    def test_http_name_matching_previews_csv_identity_and_explicit_mismatch(self) -> None:
        self.state.parameter_selections["preview"] = {"manifests":[{"source_template_id":"lessThr0008", "output_template_id":"aipuzzlelessThr0008s5", "signature":"s5"}]}
        history = "level_template_id,level_dup,level_dup_para_id,进入人数,人均消耗\nhr0008,7|0|100|4,1,100,500\n".encode("utf-8")
        body = {"selection_id":"preview", "history_file":{"name":"history.csv", "data_base64":base64.b64encode(history).decode("ascii")}}
        for prefix, status in [("", "matched"), ("wrong", "mismatch")]:
            request = urllib.request.Request(f"{self.base}/api/v1/parameters/match-consumption-templates", method="POST", data=json.dumps({**body,"template_name_prefix":prefix}).encode("utf-8"), headers={"Content-Type":"application/json","X-CSRF-Token":self.state.csrf_token})
            with urllib.request.urlopen(request) as response:
                result = json.load(response)
            self.assertEqual(result["items"][0]["status"], status)
            self.assertEqual(result["matched_count"], 1 if not prefix else 0)

    def test_http_consumption_calibration_route_is_wired(self) -> None:
        body = {
            "selection_id": "params_test",
            "config_set_id": "csv",
            "level_start": 200,
            "level_end": 300,
            "history_file": {
                "name": "history.csv",
                "data_base64": base64.b64encode(b"x").decode("ascii"),
            },
        }
        with patch.object(
            AppState,
            "calibrate_consumption_parameters",
            return_value={
                "mode": "target_consumption_calibration",
                "claim_level": "directional_beta",
            },
        ) as mocked:
            request = urllib.request.Request(
                f"{self.base}/api/v1/parameters/calibrate-consumption",
                method="POST",
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-CSRF-Token": self.state.csrf_token,
                },
            )
            with urllib.request.urlopen(request) as response:
                payload = json.load(response)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["claim_level"], "directional_beta")
        mocked.assert_called_once_with(body)

    def test_http_consumption_calibration_job_routes_return_pollable_failure(self) -> None:
        self.state.parameter_selections["params_http_async"] = {"manifests": []}
        body = {
            "selection_id": "params_http_async",
            "history_file": {
                "name": "history.csv",
                "data_base64": base64.b64encode(b"bad csv").decode("ascii"),
            },
        }
        with patch(
            "app.build_consumption_calibration",
            side_effect=InputValidationError("历史副本数据缺少必填列"),
        ):
            request = urllib.request.Request(
                f"{self.base}/api/v1/parameters/consumption-calibration-jobs",
                method="POST",
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-CSRF-Token": self.state.csrf_token,
                },
            )
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status, 202)
                started = json.load(response)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with urllib.request.urlopen(
                    f"{self.base}/api/v1/parameters/consumption-calibration-jobs/"
                    f"{started['job_id']}"
                ) as response:
                    self.assertEqual(response.status, 200)
                    failed = json.load(response)
                if failed["status"] == "failed":
                    break
                time.sleep(0.005)
        self.assertTrue(failed["ok"])
        self.assertEqual(failed["status"], "failed")
        self.assertIsNone(failed["result"])
        self.assertEqual(failed["error"]["message"], "历史副本数据缺少必填列")


if __name__ == "__main__":
    unittest.main()
