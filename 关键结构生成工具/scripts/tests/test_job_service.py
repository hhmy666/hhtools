from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.config_io import loads_batch_request, resolve_batch_config  # noqa: E402
from puzzle_core.difficulty import DifficultyProfile, DuplicateParameter  # noqa: E402
from puzzle_core.errors import NoCandidateError  # noqa: E402
from puzzle_core.protobuf_wire import encode_varint  # noqa: E402
from puzzle_core.search import (  # noqa: E402
    SearchResult,
    search_candidates as core_search_candidates,
)
from puzzle_core.template_reader import parse_level_bytes  # noqa: E402
from services.job_service import (  # noqa: E402
    GenerationBatchResult,
    GenerationItemResult,
    _recommend_candidates,
    generate_batch,
    render_generation_result,
)
from services.profile_service import Profile  # noqa: E402
from services.task_control import CooperativeTaskControl  # noqa: E402


def _varint_field(number: int, value: int) -> bytes:
    return encode_varint(number << 3) + encode_varint(value)


def _bytes_field(number: int, value: bytes) -> bytes:
    return encode_varint((number << 3) | 2) + encode_varint(len(value)) + value


def _chain_bytes(template_id: str, tile_count: int = 9) -> bytes:
    payload = [
        _bytes_field(2, template_id.encode()),
        _varint_field(4, tile_count + 3),
        _varint_field(5, 4),
    ]
    for index in range(tile_count):
        tile = b"".join(
            (
                _varint_field(1, index),
                _varint_field(2, 1),
                _varint_field(3, index),
                _varint_field(4, -10 if index % 2 == 0 else -11),
            )
        )
        payload.append(_bytes_field(6, tile))
    return b"".join(payload)


def _config(
    keys: list[str], *, candidate_count: int = 1, precision_mode: bool = False
):
    return resolve_batch_config(
        loads_batch_request(
            json.dumps(
                {
                    "schema_version": 1,
                    "template_keys": keys,
                    "batch_seed": 99,
                    "generation_defaults": {
                        "replacement_sources": ["A", "B"],
                        "candidate_count": candidate_count,
                        **({"precision_mode": True} if precision_mode else {}),
                        "attempt_budget": 30,
                        "assignment_attempts": 500,
                        "unit_defaults": {"type_count": 3},
                    },
                    "batch_plans": [
                        {
                            "plan_id": "normal-s4",
                            "units": [{"unit_id": "U1", "step": 4}],
                        }
                    ],
                }
            )
        )
    )


class JobServiceTests(unittest.TestCase):
    def test_resume_progress_retains_completed_items_after_active_item(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            keys = ['chain001', 'chain002', 'chain003', 'chain004', 'chain005']
            files = {key: root / (key + '_m.bytes') for key in keys}
            for key, path in files.items():
                path.write_bytes(_chain_bytes(key))
            config = _config(keys)
            profile = Profile('test', 'test', None, None, str(root / 'output'))
            original = generate_batch(config, profile, file_inputs=files)
            self.assertTrue(all(row.candidates for row in original.items))
            failed = replace(original.items[1], status='failed', candidates=(),
                             error=None, attempts_completed=0, slot_attempts_completed={})
            previous = replace(original, items=(original.items[0], failed, *original.items[2:]),
                               run_status='partial')
            observed = []
            with patch('services.job_service.search_candidates', side_effect=NoCandidateError('miss')):
                resumed = generate_batch(config, profile, file_inputs=files,
                                         previous_result=previous, progress_callback=observed.append)
            self.assertTrue(observed)
            for snapshot in [*observed, resumed]:
                self.assertEqual([row.template_key for row in snapshot.items], keys)
                self.assertGreaterEqual(sum(len(row.candidates) for row in snapshot.items), 4)
                for index in (0, 2, 3, 4):
                    self.assertIs(snapshot.items[index], previous.items[index])

    def test_search_progress_is_observation_not_a_completed_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'chain001_m.bytes'
            source.write_bytes(_chain_bytes('chain001'))
            output = root / 'output'
            output.mkdir()
            observations = []
            result = generate_batch(_config(['chain001']),
                                    Profile('p1', 'test', None, None, str(output)),
                                    file_inputs={'chain001': source},
                                    progress_callback=observations.append)
            running = [row for value in observations for row in value.items if row.search_progress]
            self.assertTrue(running)
            self.assertEqual(running[0].search_progress['attempts_searched'], 0)
            self.assertEqual(running[-1].attempts_completed, 0)
            self.assertFalse(running[-1].slot_attempts_completed)
            self.assertLess(result.items[0].attempts_completed, 30)

    def test_percent_minimum_reaches_generation_and_saved_candidate_options(self):
        raw = {
            "schema_version": 1, "template_keys": ["chain001"], "batch_seed": 99,
            "generation_defaults": {
                "replacement_sources": ["A", "B"], "candidate_count": 1,
                "attempt_budget": 30, "assignment_attempts": 500,
                "total_percent_minimum": 100,
                "unit_defaults": {"type_count": 3, "uniform_groups_per_type": 0},
            },
            "batch_plans": [{"plan_id": "s4", "units": [{"step": 4}]}],
        }
        config = resolve_batch_config(loads_batch_request(json.dumps(raw)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chain001_m.bytes"
            source.write_bytes(_chain_bytes("chain001"))
            output = root / "output"
            output.mkdir()
            profile = Profile("p1", "test", None, None, str(output))
            result = generate_batch(config, profile, file_inputs={"chain001": source})
            resumed = generate_batch(config, profile, file_inputs={"chain001": source}, previous_result=result)
        self.assertEqual(result.items[0].status, "success")
        candidate = result.items[0].candidates[0]
        self.assertEqual(candidate.generation_options.total_percent_minimum, 100)
        self.assertEqual(candidate.generation_options.unit_options[0].tile_count, 9)
        self.assertEqual(candidate.as_dict()["resolved_generation_options"]["total_percent_minimum"], 100)
        self.assertEqual(resumed.items[0].candidates[0].candidate.candidate_id, candidate.candidate.candidate_id)

    def test_precision_incomplete_profile_is_missing_evidence_without_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chain001_m.bytes"
            source.write_bytes(_chain_bytes("chain001"))
            output = root / "output"
            output.mkdir()
            profile = Profile("p1", "test", None, None, str(output))
            config = _config(["chain001"], candidate_count=3, precision_mode=True)

            result = generate_batch(config, profile, file_inputs={"chain001": source})

        item = result.items[0]
        self.assertEqual(result.run_status, "failed")
        self.assertEqual(item.status, "failed")
        self.assertEqual(item.error["failure_kind"], "missing_required_evidence")
        self.assertNotEqual(item.error["recommended_action"]["id"], "continue_search")
        self.assertEqual(item.error["recommended_action"]["id"], "complete_evidence")
        self.assertEqual(item.attempts_completed, 0)
        self.assertEqual(item.slot_attempts_completed, {})
        self.assertEqual(item.rejection_counts["precision_profile_coverage"], 1)
        self.assertEqual(item.error["evidence"]["profile_coverage"], "missing")

    def test_precision_missing_evidence_can_retry_after_profile_is_completed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chain001_m.bytes"
            source.write_bytes(_chain_bytes("chain001"))
            output = root / "output"
            output.mkdir()
            profile = Profile("p1", "test", None, None, str(output))
            config = _config(["chain001"], precision_mode=True)
            first = generate_batch(config, profile, file_inputs={"chain001": source})
            self.assertEqual(first.items[0].error["failure_kind"], "missing_required_evidence")
            complete = DifficultyProfile(
                duplicate_params=tuple(
                    DuplicateParameter.parse("7|0|100|4") for _ in range(10)
                ),
                coverage="complete",
            )
            searched: list[str] = []

            def resumed_search(*args, **kwargs):
                searched.append(args[0].template_id)
                raise NoCandidateError(
                    "预算内未命中",
                    evidence={
                        "attempt_budget": args[2].attempt_budget,
                        "attempt_start_index": kwargs["attempt_start_index"],
                        "attempt_count": kwargs["attempt_count"],
                        "rejection_counts": {},
                    },
                )

            with patch(
                "services.job_service.load_difficulty_profiles",
                return_value={"chain001": complete},
            ), patch(
                "services.job_service.search_candidates",
                side_effect=resumed_search,
            ):
                resumed = generate_batch(
                    config, profile,
                    file_inputs={"chain001": source},
                    previous_result=first,
                )

        self.assertEqual(searched, ["chain001"] * 30)
        self.assertEqual(resumed.items[0].error["failure_kind"], "budget_exhausted")

    def test_precision_job_reuses_independent_validation_for_accepted_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chain001_m.bytes"
            source.write_bytes(_chain_bytes("chain001"))
            output = root / "output"
            output.mkdir()
            profile = Profile("p1", "test", None, None, str(output))
            config = _config(["chain001"], precision_mode=True)
            normal_config = _config(["chain001"])
            loaded = core_search_candidates(
                parse_level_bytes(source.read_bytes(), source_name=source.name),
                normal_config.work_items[0].plan,
                normal_config.work_items[0].generation_options,
                candidate_seed=2026,
                candidate_index=1,
            ).candidates[0]
            first = replace(loaded, candidate_id="first-precision-probe")
            second = replace(
                loaded,
                candidate_id="second-precision-probe",
                structure_signature="second-precision-structure",
                experience_signature="second-precision-experience",
            )
            validated_ids: list[str] = []

            def independent_validate(*args, **kwargs):
                candidate = args[3]
                validated_ids.append(candidate.candidate_id)
                return SimpleNamespace(
                    passed=True,
                    auto_recommendation_eligible=(candidate.candidate_id == second.candidate_id),
                    as_dict=lambda: {"candidate_id": candidate.candidate_id},
                )

            def search_with_validation(*args, **kwargs):
                acceptance = kwargs["final_candidate_acceptance"]
                selected = next(
                    candidate
                    for candidate in (first, second)
                    if acceptance(candidate)
                )
                return SearchResult(
                    candidates=(selected,),
                    attempts_completed=2,
                    rejection_counts={"precision_independent_validation": 1},
                    candidate_seed=kwargs["candidate_seed"],
                    candidate_seed_sha256=kwargs["candidate_seed_sha256"],
                    candidate_seed_ordinal=kwargs["candidate_index"],
                )

            with patch(
                "services.job_service.search_candidates",
                side_effect=search_with_validation,
            ), patch(
                "services.job_service.validate_candidate",
                side_effect=independent_validate,
            ):
                result = generate_batch(
                    config, profile, file_inputs={"chain001": source}
                )

            self.assertEqual(result.items[0].status, "success")
            self.assertEqual(
                result.items[0].candidates[0].candidate.candidate_id,
                second.candidate_id,
            )
            self.assertEqual(validated_ids, [first.candidate_id, second.candidate_id])

    def test_running_snapshot_classifies_every_work_item_without_failure(self) -> None:
        config = _config(["chain001", "chain002"])
        first = config.work_items[0]
        running = GenerationItemResult(
            template_key=first.template_key,
            plan_id=first.plan.plan_id,
            plan_signature=first.plan.signature,
            parameter_source=first.parameter_source,
            generation_seed=1,
            source=None,
            inspection=None,
            candidates=(),
            attempts_completed=7,
            rejection_counts={},
            target_candidate_count=1,
            status="running",
            effective_attempt_budget_per_slot=30,
            slot_attempts_completed={1: 7},
        )
        result = GenerationBatchResult(
            format_version=1,
            batch_seed=config.batch.batch_seed,
            resolved_batch_hash=config.batch.config_hash,
            source_config_hash=config.source_config_hash,
            algorithm_version="test",
            items=(running,),
            run_status="running",
            total_work_item_count=len(config.work_items),
        )

        summary = result.as_dict()["summary"]

        self.assertEqual(summary["work_item_count"], 2)
        self.assertEqual(summary["running_count"], 1)
        self.assertEqual(summary["pending_count"], 1)
        self.assertEqual(summary["success_count"], 0)
        self.assertEqual(summary["partial_success_count"], 0)
        self.assertEqual(summary["failure_count"], 0)
        self.assertEqual(summary["cancelled_count"], 0)
        self.assertEqual(summary["classified_work_item_count"], 2)

    def test_capped_budget_result_can_be_excluded_while_pending_item_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_source = root / "chain001_m.bytes"
            second_source = root / "chain002_m.bytes"
            first_source.write_bytes(_chain_bytes("chain001"))
            second_source.write_bytes(_chain_bytes("chain002"))
            output = root / "output"
            output.mkdir()
            profile = Profile("p1", "test", None, None, str(output))
            config = _config(["chain001", "chain002"])
            first, second = config.work_items
            capped = GenerationItemResult(
                template_key=first.template_key,
                plan_id=first.plan.plan_id,
                plan_signature=first.plan.signature,
                parameter_source=first.parameter_source,
                generation_seed=1,
                source=None,
                inspection=None,
                candidates=(),
                attempts_completed=480,
                rejection_counts={"geometry_pool": 480},
                target_candidate_count=1,
                status="failed",
                error={
                    "failure_kind": "budget_exhausted",
                    "evidence": {"attempt_budget": 480},
                },
                effective_attempt_budget_per_slot=480,
                slot_attempts_completed={1: 480},
            )
            pending = GenerationItemResult(
                template_key=second.template_key,
                plan_id=second.plan.plan_id,
                plan_signature=second.plan.signature,
                parameter_source=second.parameter_source,
                generation_seed=0,
                source=None,
                inspection=None,
                candidates=(),
                attempts_completed=0,
                rejection_counts={},
                target_candidate_count=1,
                status="pending",
            )
            previous = GenerationBatchResult(
                format_version=1,
                batch_seed=config.batch.batch_seed,
                resolved_batch_hash=config.batch.config_hash,
                source_config_hash=config.source_config_hash,
                algorithm_version="test",
                items=(capped, pending),
                run_status="partial",
            )
            searched_templates: list[str] = []

            def miss_pending_only(*args, **kwargs):
                searched_templates.append(args[0].template_id)
                if args[0].template_id == "chain001":
                    self.fail("capped budget item must not be searched again")
                raise NoCandidateError(
                    "当前新增预算内未命中",
                    evidence={
                        "attempt_budget": args[2].attempt_budget,
                        "attempt_start_index": kwargs["attempt_start_index"],
                        "attempt_count": kwargs["attempt_count"],
                        "rejection_counts": {"geometry_pool": 1},
                    },
                )

            with patch(
                "services.job_service.search_candidates",
                side_effect=miss_pending_only,
            ):
                resumed = generate_batch(
                    config,
                    profile,
                    file_inputs={
                        "chain001": first_source,
                        "chain002": second_source,
                    },
                    previous_result=previous,
                    attempt_budget_multiplier=16,
                    excluded_retry_failure_kinds=(
                        "parameter_conflict",
                        "budget_exhausted",
                    ),
                )

            self.assertIs(resumed.items[0], capped)
            self.assertEqual(searched_templates, ["chain002"] * 480)

    def test_cancelled_item_resumes_after_its_completed_candidate_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chain001_m.bytes"
            source.write_bytes(_chain_bytes("chain001"))
            output = root / "output"
            output.mkdir()
            profile = Profile("p1", "test", None, None, str(output))
            config = _config(["chain001"], candidate_count=2)
            control = CooperativeTaskControl()
            original_checkpoint = control.checkpoint
            candidate_starts = 0

            def cancel_before_second_slot(phase="checkpoint"):
                nonlocal candidate_starts
                if phase == "generation_candidate":
                    candidate_starts += 1
                    if candidate_starts == 2:
                        control.request_cancel()
                original_checkpoint(phase)

            control.checkpoint = cancel_before_second_slot
            cancelled = generate_batch(
                config,
                profile,
                file_inputs={"chain001": source},
                control=control,
            )
            self.assertEqual(cancelled.run_status, "partial")
            self.assertEqual(cancelled.items[0].status, "partial")
            self.assertEqual(len(cancelled.items[0].candidates), 1)
            completed_slot = cancelled.items[0].candidates[0]

            retried = generate_batch(
                config,
                profile,
                file_inputs={"chain001": source},
                previous_result=cancelled,
                control=CooperativeTaskControl(),
            )
            self.assertEqual(retried.items[0].status, "success")
            self.assertEqual(len(retried.items[0].candidates), 2)
            resumed_slot = retried.items[0].candidates[0]
            self.assertEqual(
                resumed_slot.candidate.candidate_id,
                completed_slot.candidate.candidate_id,
            )
            self.assertEqual(
                resumed_slot.candidate.candidate_seed_sha256,
                completed_slot.candidate.candidate_seed_sha256,
            )
            self.assertEqual(
                resumed_slot.candidate.structure_signature,
                completed_slot.candidate.structure_signature,
            )

    def test_cancel_preserves_completed_items_and_retry_skips_successes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_path = root / "first_m.bytes"
            second_path = root / "second_m.bytes"
            first_path.write_bytes(_chain_bytes("first"))
            second_path.write_bytes(_chain_bytes("second"))
            output = root / "output"
            output.mkdir()
            profile = Profile("p1", "test", None, None, str(output))
            config = _config(["first", "second"])
            control = CooperativeTaskControl()

            def cancel_after_first(result):
                if result.succeeded == 1:
                    control.request_cancel()

            cancelled = generate_batch(
                config,
                profile,
                file_inputs={"first": first_path, "second": second_path},
                control=control,
                progress_callback=cancel_after_first,
            )
            self.assertEqual(cancelled.run_status, "partial")
            self.assertEqual(
                [item.status for item in cancelled.items],
                ["success", "cancelled"],
            )
            completed = cancelled.items[0]

            retried = generate_batch(
                config,
                profile,
                file_inputs={"first": first_path, "second": second_path},
                previous_result=cancelled,
                control=CooperativeTaskControl(),
            )
            self.assertEqual(retried.run_status, "completed")
            self.assertEqual([item.status for item in retried.items], ["success", "success"])
            self.assertIs(retried.items[0], completed)

    def test_candidate_slots_lock_ranges_and_full_seed_digests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chain001_m.bytes"
            source.write_bytes(_chain_bytes("chain001", 15))
            output = root / "output"
            output.mkdir()
            profile = Profile("p1", "test", None, None, str(output))
            config = resolve_batch_config(
                loads_batch_request(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "template_keys": ["chain001"],
                            "batch_seed": 1,
                            "generation_defaults": {
                                "replacement_sources": ["A", "B"],
                                "candidate_count": 4,
                                "attempt_budget": 20,
                                "assignment_attempts": 500,
                                "allowed_types": ["C", "D", "E", "F", "G"],
                                "unit_defaults": {
                                    "type_count": {
                                        "kind": "range",
                                        "minimum": 3,
                                        "maximum": 5,
                                    }
                                },
                            },
                            "batch_plans": [
                                {"plan_id": "normal-s4", "units": [{"step": 4}]}
                            ],
                        }
                    )
                )
            )

            first = generate_batch(
                config, profile, file_inputs={"chain001": source}
            )
            replay = generate_batch(
                config, profile, file_inputs={"chain001": source}
            )

            self.assertEqual(render_generation_result(first), render_generation_result(replay))
            item = first.items[0]
            self.assertEqual(item.status, "success")
            self.assertEqual(
                [value.candidate.candidate_index for value in item.candidates],
                [1, 2, 3, 4],
            )
            self.assertEqual(
                len(
                    {
                        value.candidate.structure_signature
                        for value in item.candidates
                    }
                ),
                4,
            )
            type_counts = [
                value.generation_options.unit_options[0].type_count
                for value in item.candidates
            ]
            self.assertTrue(set(type_counts).issubset({3, 4, 5}))
            self.assertGreater(len(set(type_counts)), 1)
            for value in item.candidates:
                digest = value.candidate.candidate_seed_sha256
                self.assertEqual(len(digest), 64)
                self.assertEqual(
                    value.candidate.candidate_seed_ordinal,
                    value.candidate.attempt_index + 1,
                )
            payload = first.as_dict()
            self.assertEqual(payload["batch_seed"], 1)
            self.assertEqual(payload["batch_seed_source"], "provided")
            self.assertEqual(
                payload["locked_config_snapshot"]["request"]["batch_seed"], 1
            )

    def test_one_missing_template_does_not_abort_other_work_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template_dir = root / "Tile" / "Assets" / "Resources" / "TileMatchLevelData"
            template_dir.mkdir(parents=True)
            (template_dir / "chain001_m.bytes").write_bytes(_chain_bytes("chain001"))
            output = root / "output"
            output.mkdir()
            profile = Profile("p1", "test", str(root), str(template_dir), str(output))

            result = generate_batch(_config(["chain001", "missing"]), profile)

            self.assertEqual(result.succeeded, 1)
            self.assertEqual(result.failed, 1)
            self.assertEqual(result.items[0].status, "success")
            self.assertEqual(result.items[1].status, "failed")
            self.assertTrue(result.items[0].candidates[0].validation.passed)
            decoded = json.loads(render_generation_result(result))
            self.assertEqual(decoded["summary"]["success_count"], 1)
            self.assertEqual(list(output.iterdir()), [])

    def test_budget_miss_uses_later_slots_and_retains_partial_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chain001_m.bytes"
            source.write_bytes(_chain_bytes("chain001"))
            output = root / "output"
            output.mkdir()
            profile = Profile("p1", "test", None, None, str(output))
            calls: list[int] = []

            def search_with_middle_miss(*args, **kwargs):
                candidate_index = kwargs["candidate_index"]
                calls.append(candidate_index)
                if candidate_index == 3:
                    raise NoCandidateError(
                        "固定搜索预算内没有找到满足全部硬规则的候选",
                        evidence={
                            "attempt_budget": kwargs["options"].attempt_budget
                            if "options" in kwargs
                            else args[2].attempt_budget,
                            "rejection_counts": {"unit_geometry": 30},
                        },
                    )
                return core_search_candidates(*args, **kwargs)

            with patch(
                "services.job_service.search_candidates",
                side_effect=search_with_middle_miss,
            ):
                result = generate_batch(
                    _config(["chain001"], candidate_count=3),
                    profile,
                    file_inputs={"chain001": source},
                )

            self.assertEqual(len(calls), 30)
            self.assertEqual(calls[0], 1)
            self.assertIn(2, calls)
            self.assertIn(3, calls)
            self.assertEqual(calls, sorted(calls))
            self.assertEqual(result.run_status, "partial")
            item = result.items[0]
            self.assertEqual(item.status, "partial")
            self.assertEqual(
                [candidate.candidate.candidate_index for candidate in item.candidates],
                [1, 2],
            )
            self.assertEqual(item.error["failure_kind"], "budget_exhausted")
            self.assertEqual(
                item.error["recommended_action"]["id"], "continue_search"
            )
            self.assertEqual(
                item.error["candidate_progress"], {"found": 2, "target": 3}
            )

    def test_static_count_conflict_is_not_reported_as_budget_exhaustion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chain001_m.bytes"
            source.write_bytes(_chain_bytes("chain001"))
            output = root / "output"
            output.mkdir()
            profile = Profile("p1", "test", None, None, str(output))
            config = resolve_batch_config(
                loads_batch_request(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "template_keys": ["chain001"],
                            "batch_seed": 99,
                            "generation_defaults": {
                                "replacement_sources": ["A", "B"],
                                "candidate_count": 2,
                                "attempt_budget": 30,
                                "total_count_limit": 6,
                                "unit_defaults": {"type_count": 3},
                            },
                            "batch_plans": [
                                {
                                    "plan_id": "normal-s4",
                                    "units": [{"unit_id": "U1", "step": 4}],
                                }
                            ],
                        }
                    )
                )
            )

            result = generate_batch(
                config,
                profile,
                file_inputs={"chain001": source},
            )

            item = result.items[0]
            self.assertEqual(result.run_status, "failed")
            self.assertEqual(item.status, "failed")
            self.assertEqual(item.error["failure_kind"], "parameter_conflict")
            self.assertEqual(
                item.error["recommended_action"]["id"], "adjust_parameters"
            )
            self.assertNotIn("candidate_progress", item.error)

    def test_cross_slot_renamed_duplicate_is_not_counted_as_another_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chain001_m.bytes"
            source.write_bytes(_chain_bytes("chain001"))
            output = root / "output"
            output.mkdir()
            profile = Profile("p1", "test", None, None, str(output))
            first_search = None

            def repeat_first_layout(*args, **kwargs):
                nonlocal first_search
                if first_search is None:
                    first_search = core_search_candidates(*args, **kwargs)
                    return first_search
                candidate = first_search.candidates[0]
                payload = json.loads(candidate.structure_signature)
                for group in payload:
                    for tile in group["tiles"]:
                        tile[3] = {"C":"D", "D":"E", "E":"F", "F":"G", "G":"H", "H":"I", "I":"J", "J":"K", "K":"L", "L":"M", "M":"N", "N":"C"}[tile[3]]
                return replace(first_search, candidates=(replace(candidate,
                    structure_signature=json.dumps(payload),
                    experience_signature="different-simulation-same-partition"),))

            with patch(
                "services.job_service.search_candidates",
                side_effect=repeat_first_layout,
            ):
                result = generate_batch(
                    _config(["chain001"], candidate_count=3),
                    profile,
                    file_inputs={"chain001": source},
                )

            item = result.items[0]
            self.assertEqual(item.status, "partial")
            self.assertEqual(len(item.candidates), 1)
            self.assertEqual(
                len({value.candidate.structure_signature for value in item.candidates}),
                1,
            )
            self.assertEqual(item.error["candidate_progress"], {"found": 1, "target": 3})
            self.assertEqual(item.rejection_counts["duplicate_candidate_across_slots"], 29)

    def test_cross_slot_experience_duplicate_is_defensively_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chain001_m.bytes"
            source.write_bytes(_chain_bytes("chain001"))
            output = root / "output"
            output.mkdir()
            profile = Profile("p1", "test", None, None, str(output))
            seen_exclusions: list[set[str]] = []

            def repeat_experience(*args, **kwargs):
                seen_exclusions.append(
                    set(kwargs["exclude_experience_signatures"])
                )
                searched = core_search_candidates(*args, **kwargs)
                candidate = replace(
                    searched.candidates[0],
                    experience_signature="same-experience",
                )
                return replace(searched, candidates=(candidate,))

            with patch(
                "services.job_service.search_candidates",
                side_effect=repeat_experience,
            ), patch(
                "services.job_service.validate_candidate",
                return_value=SimpleNamespace(
                    passed=True,
                    as_dict=lambda: {"passed": True},
                ),
            ):
                result = generate_batch(
                    _config(["chain001"], candidate_count=3),
                    profile,
                    file_inputs={"chain001": source},
                )

            item = result.items[0]
            self.assertEqual(item.status, "partial", item.error)
            self.assertEqual(len(item.candidates), 1)
            self.assertEqual(seen_exclusions[0], set())
            self.assertTrue(
                all(
                    exclusions == {"same-experience"}
                    for exclusions in seen_exclusions[1:]
                )
            )
            self.assertGreaterEqual(
                item.rejection_counts[
                    "duplicate_experience_signature_across_slots"
                ],
                1,
            )
            self.assertEqual(
                item.as_dict()["candidate_progress"][
                    "independent_experience_signature_count"
                ],
                1,
            )
            self.assertEqual(
                result.as_dict()["summary"][
                    "independent_experience_signature_count"
                ],
                1,
            )

    def test_resume_dedupes_recovered_experience_and_researches_its_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chain001_m.bytes"
            source.write_bytes(_chain_bytes("chain001"))
            output = root / "output"
            output.mkdir()
            profile = Profile("p1", "test", None, None, str(output))
            config = _config(["chain001"], candidate_count=2)
            baseline = generate_batch(
                config,
                profile,
                file_inputs={"chain001": source},
            )
            first, second = baseline.items[0].candidates
            duplicated_second = replace(
                second,
                candidate=replace(
                    second.candidate,
                    experience_signature=first.candidate.experience_signature,
                ),
            )
            previous_item = replace(
                baseline.items[0],
                candidates=(first, duplicated_second),
                status="partial",
                error={
                    "failure_kind": "budget_exhausted",
                    "evidence": {"attempt_budget": 30},
                },
                slot_attempts_completed={1: 30, 2: 30},
            )
            previous = replace(
                baseline,
                items=(previous_item,),
                run_status="partial",
            )
            calls: list[tuple[int, int, set[str]]] = []

            def record_search(*args, **kwargs):
                calls.append(
                    (
                        kwargs["candidate_index"],
                        kwargs["attempt_start_index"],
                        set(kwargs["exclude_experience_signatures"]),
                    )
                )
                return core_search_candidates(*args, **kwargs)

            with patch(
                "services.job_service.search_candidates",
                side_effect=record_search,
            ):
                resumed = generate_batch(
                    config,
                    profile,
                    file_inputs={"chain001": source},
                    previous_result=previous,
                )

            item = resumed.items[0]
            self.assertEqual(item.status, "success", (item.error, calls))
            self.assertTrue(calls)
            self.assertEqual(calls, [
                (2, baseline.items[0].attempts_completed + offset,
                 {first.candidate.experience_signature})
                for offset in range(len(calls))
            ])
            self.assertEqual(
                item.candidates[0].candidate.candidate_id,
                first.candidate.candidate_id,
            )
            self.assertEqual(
                len(
                    {
                        candidate.candidate.experience_signature
                        for candidate in item.candidates
                    }
                ),
                2,
            )
            self.assertEqual(
                item.rejection_counts[
                    "duplicate_experience_signature_recovered"
                ],
                1,
            )

    def test_larger_budget_continuation_excludes_the_retained_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chain001_m.bytes"
            source.write_bytes(_chain_bytes("chain001"))
            output = root / "output"
            output.mkdir()
            profile = Profile("p1", "test", None, None, str(output))
            config = _config(["chain001"], candidate_count=2)
            windows: list[tuple[int, int, int, int]] = []

            def fail_second_at_original_budget(*args, **kwargs):
                options = args[2]
                windows.append(
                    (
                        kwargs["candidate_index"],
                        options.attempt_budget,
                        kwargs["attempt_start_index"],
                        kwargs["attempt_count"],
                    )
                )
                if kwargs["candidate_index"] == 2 and options.attempt_budget == 30:
                    raise NoCandidateError(
                        "固定搜索预算内没有找到满足全部硬规则的候选",
                        evidence={
                            "attempt_budget": 30,
                            "rejection_counts": {"geometry_pool": 30},
                        },
                    )
                return core_search_candidates(*args, **kwargs)

            with patch(
                "services.job_service.search_candidates",
                side_effect=fail_second_at_original_budget,
            ):
                first = generate_batch(
                    config,
                    profile,
                    file_inputs={"chain001": source},
                )
                retained_signature = (
                    first.items[0].candidates[0].candidate.structure_signature
                )
                continued = generate_batch(
                    config,
                    profile,
                    file_inputs={"chain001": source},
                    previous_result=first,
                    attempt_budget_multiplier=2,
                )

            self.assertEqual(first.items[0].status, "partial")
            self.assertEqual(continued.items[0].status, "success")
            signatures = [
                value.candidate.structure_signature
                for value in continued.items[0].candidates
            ]
            self.assertEqual(len(set(signatures)), 2)
            self.assertEqual(signatures[0], retained_signature)
            # Single draws use distinct deterministic seeds; continuation
            # adds only one draw here and stops as soon as the target is met.
            self.assertIn((2, 30, 1, 1), windows)
            self.assertIn((2, 60, 30, 1), windows)
            self.assertEqual(continued.items[0].shared_attempts_completed, 31)
            self.assertEqual(
                continued.items[0].attempts_completed,
                first.items[0].attempts_completed + 1,
            )

    def test_forced_scoped_retry_keeps_candidate_and_searches_only_missing_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chain001_m.bytes"
            source.write_bytes(_chain_bytes("chain001"))
            output = root / "output"
            output.mkdir()
            profile = Profile("p1", "test", None, None, str(output))
            parent_config = _config(["chain001"], candidate_count=1)
            parent = generate_batch(
                parent_config,
                profile,
                file_inputs={"chain001": source},
            )
            child_config = _config(["chain001"], candidate_count=2)
            child_work = child_config.work_items[0]
            parent_candidate_id = (
                parent.items[0].candidates[0].candidate.candidate_id
            )
            inherited = replace(
                parent,
                resolved_batch_hash=child_config.batch.config_hash,
                source_config_hash=child_config.source_config_hash,
                items=(
                    replace(
                        parent.items[0],
                        target_candidate_count=2,
                        status="pending",
                        error=None,
                        slot_attempts_completed={},
                    ),
                ),
                run_status="running",
                total_work_item_count=1,
            )
            searched_slots: list[int] = []

            def record_search(*args, **kwargs):
                searched_slots.append(kwargs["candidate_index"])
                return core_search_candidates(*args, **kwargs)

            with patch(
                "services.job_service.search_candidates",
                side_effect=record_search,
            ):
                child = generate_batch(
                    child_config,
                    profile,
                    file_inputs={"chain001": source},
                    previous_result=inherited,
                    retry_statuses=(),
                    retry_work_item_keys={
                        (
                            child_work.template_key,
                            child_work.plan.plan_id,
                            child_work.plan.signature,
                        )
                    },
                )

            self.assertTrue(searched_slots)
            self.assertEqual(set(searched_slots), {2})
            self.assertIn(
                parent_candidate_id,
                {
                    generated.candidate.candidate_id
                    for generated in child.items[0].candidates
                },
            )
            self.assertGreaterEqual(len(child.items[0].candidates), 1)

    def test_recommendation_requires_sufficient_evidence_and_tracks_eligible_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chain001_m.bytes"
            source.write_bytes(_chain_bytes("chain001"))
            output = root / "output"
            output.mkdir()
            profile = Profile("p1", "test", None, None, str(output))

            single = generate_batch(
                _config(["chain001"], candidate_count=1),
                profile,
                file_inputs={"chain001": source},
            )
            multiple = generate_batch(
                _config(["chain001"], candidate_count=3),
                profile,
                file_inputs={"chain001": source},
            )

            # This synthetic source has no authoritative parameter profile and
            # is intentionally far from its generated C–N replacement.  It may
            # remain visible, but evidence-inconclusive candidates must not be
            # silently promoted to an automatic recommendation.
            self.assertEqual(
                sum(value.is_recommended for value in single.items[0].candidates), 0
            )
            self.assertEqual(
                sum(value.is_recommended for value in multiple.items[0].candidates),
                0,
            )

            eligible_inputs = []
            for index, generated in enumerate(multiple.items[0].candidates[:3]):
                eligible_inputs.append(
                    replace(
                        generated,
                        candidate=replace(
                            generated.candidate,
                            experience_analysis={
                                **generated.candidate.experience_analysis,
                                "hard_validation": {"status": "proved"},
                                "archetype": {
                                    "requested": "auto",
                                    "primary": "board_reveal",
                                    "match_score": 0.9 - index * 0.1,
                                },
                                "simulation_pressure": {"level": "low"},
                                "evidence_coverage": {"overall": "acceptable"},
                                "online_style_distance": {"total": 0.1 + index},
                            },
                            difficulty_simulation={
                                **generated.candidate.difficulty_simulation,
                                "qualified": True,
                                "trend": {"safety_qualified": True},
                                "difficulty_distance": {
                                    "total": 0.1 + index,
                                },
                            },
                        ),
                    )
                )
            recommended = _recommend_candidates(eligible_inputs)
            recommendations = [value for value in recommended if value.is_recommended]
            self.assertEqual(len(recommendations), 2)
            self.assertEqual(
                sorted(value.recommendation_rank for value in recommendations),
                [1, 2],
            )
            self.assertTrue(
                all(value.recommendation_reasons for value in recommendations)
            )

            ranked_inputs = list(eligible_inputs)
            ranked_inputs[0] = replace(
                ranked_inputs[0],
                candidate=replace(
                    ranked_inputs[0].candidate,
                    experience_analysis={
                        **ranked_inputs[0].candidate.experience_analysis,
                        "simulation_pressure": {"level": "high"},
                    },
                ),
            )
            risk_aware = _recommend_candidates(ranked_inputs)
            by_id = {item.candidate.candidate_id: item for item in risk_aware}
            self.assertFalse(by_id[ranked_inputs[0].candidate.candidate_id].is_recommended)
            self.assertTrue(by_id[ranked_inputs[1].candidate.candidate_id].is_recommended)
            self.assertTrue(
                all(
                    "机制压力未越线" in item.recommendation_reasons[0]
                    for item in risk_aware
                    if item.is_recommended
                )
            )

            # “强卡点”是用户显式选择的目标：高机制压力仍需硬校验
            # 与趋势安全，但不应被当成未选择高压模式时的越线。
            strong_candidate = replace(
                ranked_inputs[0],
                candidate=replace(
                    ranked_inputs[0].candidate,
                    difficulty_simulation={
                        **ranked_inputs[0].candidate.difficulty_simulation,
                        "target": "strong",
                    },
                ),
            )
            (strong_ranked,) = _recommend_candidates((strong_candidate,))
            self.assertTrue(strong_ranked.is_recommended)

    def test_direct_file_input_does_not_require_configured_repo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chain001_m.bytes"
            source.write_bytes(_chain_bytes("chain001"))
            output = root / "output"
            output.mkdir()
            profile = Profile("p1", "test", None, None, str(output))

            result = generate_batch(
                _config(["chain001"]),
                profile,
                file_inputs={"chain001": source},
            )

            self.assertEqual(result.succeeded, 1)
            self.assertEqual(result.items[0].source.input_mode, "file")


if __name__ == "__main__":
    unittest.main()
