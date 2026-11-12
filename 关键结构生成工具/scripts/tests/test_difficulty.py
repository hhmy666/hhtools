from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.config_io import (  # noqa: E402
    loads_batch_request,
    resolve_batch_config,
)
from puzzle_core.difficulty import (  # noqa: E402
    COMMON_SEED_VERSION,
    COMPLETION_WITNESS_VERSION,
    DifficultyProfile,
    DuplicateParameter,
    _difficulty_distance,
    _completion_witness_coverage,
    _reveal_summary,
    _simulate_path,
    _strategy_qualification_report,
    _trend_report,
    _wilson_interval,
    _materialized_types,
    compare_duplicate_difficulty_pair,
    difficulty_profile_from_simulation,
    simulate_candidate_difficulty,
)
from puzzle_core.protobuf_wire import encode_varint  # noqa: E402
from puzzle_core.template_reader import parse_level_bytes  # noqa: E402
from services.difficulty_profile_service import load_difficulty_profile  # noqa: E402
from services.job_service import GeneratedCandidate, _recommend_candidates  # noqa: E402
from services.profile_service import Profile  # noqa: E402


def _varint_field(number: int, value: int) -> bytes:
    return encode_varint(number << 3) + encode_varint(value)


def _bytes_field(number: int, value: bytes) -> bytes:
    return encode_varint((number << 3) | 2) + encode_varint(len(value)) + value


def _level():
    payload = [
        _bytes_field(2, b"difficulty001"),
        _varint_field(4, 24),
        _varint_field(5, 4),
    ]
    for index in range(12):
        payload.append(
            _bytes_field(
                6,
                b"".join(
                    (
                        _varint_field(1, index * 3),
                        _varint_field(2, 0),
                        _varint_field(3, index % 2),
                        _varint_field(4, -10 if index % 2 == 0 else -11),
                    )
                ),
            )
        )
    return parse_level_bytes(b"".join(payload), source_name="difficulty001_m.bytes")


def _write_config(path: Path, rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            ["LevelId", "TemplateId", *(f"DupPara{i}" for i in range(1, 11))]
        )
        writer.writerow(["string"] * 12)
        writer.writerow(["说明"] * 12)
        writer.writerows(rows)


_STRATEGIES = (
    "random",
    "greedy",
    "local_optimal",
    "unlock",
    "adversarial",
)


def _strategy_row(
    failures: dict[str, float] | None = None,
    *,
    random_failure_upper: float | None = None,
    difficulty_score: float = 0.0,
    reveal_pressure: float = 0.0,
) -> dict[str, object]:
    values = {strategy: 0.0 for strategy in _STRATEGIES}
    values.update(failures or {})
    if random_failure_upper is None:
        random_failure_upper = values["random"]
    strategies = {}
    for strategy in _STRATEGIES:
        pass_probability = 1.0 - values[strategy]
        strategies[strategy] = {
            "trials": 12 if strategy == "random" else 1,
            "pass_probability": pass_probability,
            "pass_probability_wilson95": {
                "lower": 1.0 - random_failure_upper,
                "upper": min(1.0, pass_probability + 0.05),
                "half_width": 0.05,
                "confidence": 0.95,
            },
            "first_recycle_click": None,
            "first_full_bag_click": None,
            "settled_peak": 0,
            "reveal_curve": {
                "dangling_one_area": {"median": reveal_pressure},
                "dangling_two_area": {"median": 0.0},
            },
        }
    return {
        "difficulty_score": difficulty_score,
        "strategy_failure_vector": values,
        "strategies": strategies,
        "random_trial_results": [values["random"] == 0.0] * 12,
    }


class DifficultyTests(unittest.TestCase):
    def test_config_defaults_match_tool_and_accepts_three_targets(self) -> None:
        for target in ("fidelity", "standard", "strong"):
            resolved = resolve_batch_config(
                loads_batch_request(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "template_keys": ["difficulty001"],
                            "batch_seed": 7,
                            "generation_defaults": {
                                "quality": {"difficulty_target": target}
                            },
                            "batch_plans": [
                                {"plan_id": "s5", "units": [{"step": 5}]}
                            ],
                        }
                    )
                )
            )
            options = resolved.work_items[0].generation_options
            self.assertEqual(options.candidate_count, 6)
            self.assertEqual(options.attempt_budget, 2000)
            self.assertEqual(options.quality.difficulty_target, target)

    def test_simulation_exposes_ten_duplicates_and_all_strategy_fields(self) -> None:
        level = _level()
        profile = DifficultyProfile(
            duplicate_params=tuple(
                DuplicateParameter(
                    a=max(1, 5 - index // 3),
                    seed=str(100 + index),
                    luck="100",
                    b=max(1, 3 - index // 4),
                )
                for index in range(10)
            ),
            coverage="complete",
            source={"config_level_path": "test/config_level.csv"},
        )
        replacements = tuple(
            SimpleNamespace(
                tile_index=index,
                structure_type="C",
                source_tile_id=-10 if index in (0, 2) else -11,
            )
            for index in (0, 2, 4)
        )

        report = simulate_candidate_difficulty(
            level,
            replacements,
            target="standard",
            profile=profile,
        )

        self.assertEqual(report["coverage"], "complete")
        self.assertEqual(len(report["duplicates"]), 10)
        self.assertIn("becomes_easier", report["trend"])
        self.assertIsInstance(report["source_difficulty_distance"], float)
        self.assertEqual(report["duplicates"][0]["parameters"]["a"], 4)
        self.assertEqual(report["duplicates"][0]["parameters"]["b"], 2)
        self.assertEqual(report["source_duplicates"][0]["parameters"]["a"], 5)
        self.assertEqual(report["source_duplicates"][0]["parameters"]["b"], 3)
        self.assertEqual(
            report["parameter_adjustment"]["replaced_a_structure_type_count"],
            1,
        )
        for duplicate in report["duplicates"]:
            self.assertIn("naked_pass_probability", duplicate)
            self.assertIn("capacity7_failure_path_ratio", duplicate)
            self.assertIn("first_recycle_click", duplicate)
            self.assertIn("first_full_bag_click", duplicate)
            self.assertEqual(
                set(duplicate["strategy_failure_vector"]), set(_STRATEGIES)
            )
            self.assertEqual(
                duplicate["compatibility_aggregate"]["policy"],
                "fixed_equal_weight_across_five_strategies",
            )
            self.assertFalse(
                duplicate["compatibility_aggregate"]["used_for_qualification"]
            )
            self.assertEqual(
                set(duplicate["strategies"]),
                {
                    "random",
                    "greedy",
                    "local_optimal",
                    "unlock",
                    "adversarial",
                },
            )
            self.assertIn("whole_board_reveal", duplicate)
            self.assertEqual(
                duplicate["common_seed_version"], COMMON_SEED_VERSION
            )
            self.assertEqual(duplicate["completion_witness_status"], "proved")
            self.assertEqual(
                duplicate["completion_witness"]["version"],
                COMPLETION_WITNESS_VERSION,
            )
            self.assertEqual(
                len(
                    duplicate["completion_witness"][
                        "clicked_source_indices"
                    ]
                ),
                duplicate["completion_witness"]["click_count"],
            )
        coverage = report["evidence_coverage"]
        self.assertEqual(coverage["config_coverage"], "exact_unique")
        self.assertEqual(coverage["materialization_fidelity"], "synthetic")
        self.assertEqual(coverage["overall_status"], "inconclusive")
        self.assertEqual(coverage["path_coverage"]["mode"], "sampled")
        self.assertTrue(coverage["paired_comparison"])
        self.assertIn(coverage["solver_status"], {"disproved", "limit_exceeded"})
        self.assertEqual(
            coverage["solver_evidence_kind"], "sampled_path_counterexample"
        )
        self.assertEqual(
            coverage["claim_scope"], "sampled_paths_not_exhaustive_proof"
        )
        self.assertEqual(coverage["completion_witness_status"], "proved")
        self.assertEqual(
            coverage["completion_witness_scope"],
            "existential_path_per_synthetic_duplicate",
        )
        self.assertEqual(
            coverage["missing_completion_witness_duplicate_indices"], []
        )

    def test_completion_witness_coverage_is_fail_closed_but_not_disproved(self) -> None:
        status, missing = _completion_witness_coverage(
            (
                {
                    "duplicate_index": 1,
                    "completion_witness_status": "proved",
                    "completion_witness": {"clicked_source_indices": [1, 2, 3]},
                },
                {
                    "duplicate_index": 2,
                    "completion_witness_status": "inconclusive",
                    "completion_witness": None,
                },
            )
        )

        self.assertEqual(status, "inconclusive")
        self.assertEqual(missing, [2])

    def test_source_compared_with_itself_has_zero_multidimensional_distance(self) -> None:
        level = _level()
        profile = DifficultyProfile(
            duplicate_params=tuple(
                DuplicateParameter(5, str(index), "100", 3)
                for index in range(10)
            ),
            coverage="complete",
        )

        first = simulate_candidate_difficulty(
            level, (), target="fidelity", profile=profile
        )
        replay = simulate_candidate_difficulty(
            level,
            (),
            target="fidelity",
            profile=profile,
            source_duplicates=first["source_duplicates"],
        )

        self.assertEqual(first["source_difficulty_distance"], 0.0)
        self.assertEqual(first["difficulty_distance"]["primary_distance"], 0.0)
        self.assertEqual(first["difficulty_distance"]["stable_tiebreak"], 0.0)
        self.assertEqual(
            first["difficulty_distance"]["curve_area_difference"]["absolute"],
            0.0,
        )
        self.assertEqual(replay["difficulty_distance"]["primary_distance"], 0.0)
        for duplicate in replay["difficulty_distance"]["duplicates"]:
            self.assertTrue(
                all(value == 0.0 for value in duplicate["components"].values())
            )
            self.assertEqual(
                duplicate["paired_pass_difference"]["interval95"], [0.0, 0.0]
            )

    def test_whole_board_path_records_reveal_and_structure_release(self) -> None:
        # Three initially clickable C tiles jointly cover one A tile.  The
        # third C click recycles the structure group and releases the A tile.
        blocker_masks = (0, 0, 0, (1 << 0) | (1 << 1) | (1 << 2))
        outcome = _simulate_path(
            blocker_masks,
            ("S:C", "S:C", "S:C", "A:0"),
            strategy="greedy",
            source_indices=(10, 11, 12, 13),
            template_sha="fixture-sha",
            duplicate_index=1,
            trial_id=1001,
        )

        self.assertEqual(outcome.cumulative_structure_exposed, 3)
        self.assertEqual(len(outcome.structure_match_events), 1)
        event = outcome.structure_match_events[0]
        self.assertEqual(event["click"], 3)
        self.assertEqual(len(outcome.structure_click_release_events), 3)
        release = outcome.structure_click_release_events[-1]
        self.assertEqual(release["released_ab_count"], 1)
        self.assertEqual(release["released_ab_source_indices"], [13])
        self.assertTrue(release["completed_structure_match"])
        self.assertEqual(outcome.clicked_source_indices, (10, 11, 12, 13))
        self.assertEqual(
            outcome.reveal_trace[0]["current_clickable_structure_count"], 3
        )
        self.assertEqual(
            outcome.reveal_trace[0]["complete_structure_group_type_count"], 1
        )

    def test_structure_click_releases_before_match_are_not_lost(self) -> None:
        # Every C click releases one A/B tile.  The first two releases happen
        # before the third C completes the group and must remain attributable
        # to those individual structure clicks.
        blocker_masks = (
            0,
            0,
            0,
            1 << 0,
            1 << 1,
            1 << 2,
        )
        outcome = _simulate_path(
            blocker_masks,
            ("S:C", "S:C", "S:C", "A:0", "B:0", "A:1"),
            strategy="unlock",
            source_indices=(10, 11, 12, 13, 14, 15),
            template_sha="release-fixture",
            duplicate_index=1,
            trial_id=1003,
        )

        releases = outcome.structure_click_release_events
        self.assertEqual(len(releases), 3)
        self.assertEqual([event["released_ab_count"] for event in releases], [1, 1, 1])
        self.assertFalse(releases[0]["completed_structure_match"])
        self.assertFalse(releases[1]["completed_structure_match"])
        self.assertTrue(releases[2]["completed_structure_match"])
        self.assertNotIn("released_ab_count", outcome.structure_match_events[0])
        summary = _reveal_summary((outcome,))
        self.assertEqual(
            summary["released_ab_per_structure_click"]["median"], 1.0
        )
        self.assertEqual(summary["released_ab_total_per_path"]["median"], 3.0)
        self.assertEqual(
            summary["released_ab_on_match_completion_click"]["median"], 1.0
        )
        self.assertNotIn(
            "released_ab_count", summary["first_three_structure_matches"]["1"]
        )

    def test_common_action_priorities_replay_same_source_index_order(self) -> None:
        blocker_masks = (0, 0, 0, 0, 0, 0)
        common = {
            "strategy": "random",
            "source_indices": (20, 21, 22, 23, 24, 25),
            "template_sha": "paired-fixture",
            "duplicate_index": 4,
            "trial_id": 7,
        }
        source = _simulate_path(
            blocker_masks,
            ("A:0", "A:0", "A:0", "B:0", "B:0", "B:0"),
            **common,
        )
        candidate = _simulate_path(
            blocker_masks,
            ("S:C", "S:C", "S:C", "B:0", "B:0", "B:0"),
            **common,
        )

        source_order = [
            value["clicked_source_index"] for value in source.reveal_trace[1:]
        ]
        candidate_order = [
            value["clicked_source_index"] for value in candidate.reveal_trace[1:]
        ]
        self.assertEqual(source_order, candidate_order)
        replay = _simulate_path(
            blocker_masks,
            ("S:C", "S:C", "S:C", "B:0", "B:0", "B:0"),
            **common,
        )
        self.assertEqual(candidate, replay)

    def test_fidelity_trend_uses_source_residual_not_absolute_monotonicity(self) -> None:
        def row(value: float) -> dict[str, object]:
            return _strategy_row(
                {strategy: value for strategy in _STRATEGIES}
            )

        source = (row(0.10), row(0.20), row(0.15))
        same_reverse_gradient = (row(0.12), row(0.22), row(0.17))
        fidelity = _trend_report(
            same_reverse_gradient,
            source,
            mode="preserve_original_gradient",
        )
        rebuilt = _trend_report(
            same_reverse_gradient,
            source,
            mode="rebuild_gradient",
        )

        self.assertTrue(fidelity["becomes_easier"])
        self.assertFalse(fidelity["candidate_absolute_becomes_easier"])
        self.assertFalse(rebuilt["becomes_easier"])
        amplified = _trend_report(
            (row(0.12), row(0.35), row(0.17)),
            source,
            mode="preserve_original_gradient",
        )
        self.assertFalse(amplified["becomes_easier"])
        self.assertEqual(
            amplified["violations"][0]["kind"], "amplified_reverse_gradient"
        )

    def test_trend_keeps_adversarial_as_pressure_only_for_standard(self) -> None:
        source = (_strategy_row(), _strategy_row())
        candidate = (
            _strategy_row(),
            _strategy_row({"adversarial": 1.0}),
        )

        report = _trend_report(
            candidate,
            source,
            mode="preserve_original_gradient",
            qualification_strategies=(
                "random",
                "greedy",
                "local_optimal",
                "unlock",
            ),
        )

        self.assertTrue(report["becomes_easier"])
        self.assertEqual(report["violations"], [])
        self.assertTrue(report["pressure_only_violations"])
        self.assertTrue(
            all(
                item["strategy"] == "adversarial"
                for item in report["pressure_only_violations"]
            )
        )

    def test_target_gates_use_versioned_per_strategy_evidence(self) -> None:
        adversarial_only = tuple(
            _strategy_row(
                {"adversarial": 1.0}, random_failure_upper=0.30
            )
            for _ in range(10)
        )
        standard = _strategy_qualification_report(
            adversarial_only, target="standard", source_distance=0.10
        )
        fidelity = _strategy_qualification_report(
            adversarial_only, target="fidelity", source_distance=0.10
        )

        self.assertTrue(standard["passed"])
        self.assertEqual(
            standard["policy"]["pressure_only_strategies"],
            ("adversarial",),
        )
        self.assertFalse(fidelity["passed"])
        self.assertTrue(fidelity["observed_failures"])

        # Until an outcome-calibrated equivalence band exists, source
        # distance orders otherwise safe fidelity candidates but is not an
        # invented hard rejection threshold.
        safe_but_distant = _strategy_qualification_report(
            tuple(_strategy_row() for _ in range(10)),
            target="fidelity",
            source_distance=1.0,
        )
        self.assertTrue(safe_but_distant["passed"])
        self.assertIsNone(
            safe_but_distant["policy"]["maximum_source_distance"]
        )
        self.assertEqual(
            safe_but_distant["policy"]["source_distance_policy"],
            "ranking_only_until_calibrated",
        )
        self.assertEqual(
            safe_but_distant["source_distance_calibration"],
            "unavailable",
        )

        one_constructive_path = tuple(
            _strategy_row(
                {
                    "random": 1.0,
                    "greedy": 0.0,
                    "local_optimal": 1.0,
                    "unlock": 1.0,
                    "adversarial": 1.0,
                },
                random_failure_upper=1.0,
            )
            for _ in range(10)
        )
        no_constructive_path = tuple(
            _strategy_row(
                {
                    "random": 1.0,
                    "greedy": 1.0,
                    "local_optimal": 1.0,
                    "unlock": 1.0,
                    "adversarial": 1.0,
                },
                random_failure_upper=1.0,
            )
            for _ in range(10)
        )
        self.assertTrue(
            _strategy_qualification_report(
                one_constructive_path,
                target="strong",
                source_distance=1.0,
            )["passed"]
        )
        self.assertFalse(
            _strategy_qualification_report(
                no_constructive_path,
                target="strong",
                source_distance=1.0,
            )["passed"]
        )

    def test_strategy_distance_uses_worst_named_strategy_not_trial_pool(self) -> None:
        source = _strategy_row({"random": 0.25})
        candidate = _strategy_row({"random": 0.25, "adversarial": 1.0})
        candidate["strategies"]["random"]["trials"] = 240
        source["strategies"]["random"]["trials"] = 12

        report = _difficulty_distance(
            (candidate,),
            (source,),
            board_size=10,
            source_has_cn=True,
        )

        duplicate = report["duplicates"][0]
        self.assertEqual(duplicate["components"]["strategy_failure"], 1.0)
        self.assertEqual(
            duplicate["per_strategy_components"]["strategy_failure"],
            {
                "random": 0.0,
                "greedy": 0.0,
                "local_optimal": 0.0,
                "unlock": 0.0,
                "adversarial": 1.0,
            },
        )

    def test_no_cn_source_uses_frozen_dynamic_reference_cohort(self) -> None:
        def complete_reveal_row(duplicate_index: int) -> dict[str, object]:
            row = _strategy_row(reveal_pressure=7.0)
            row["duplicate_index"] = duplicate_index
            for strategy in _STRATEGIES:
                reveal = row["strategies"][strategy]["reveal_curve"]
                reveal["curve"] = [
                    {
                        metric: {"median": float(bin_index)}
                        for metric in (
                            "current_clickable_structure_count",
                            "newly_exposed_structure_count",
                            "cumulative_exposed_structure_count",
                            "complete_structure_group_type_count",
                            "dangling_one_type_count",
                            "dangling_two_type_count",
                        )
                    }
                    for bin_index in range(5)
                ]
                reveal["first_three_structure_matches"] = {
                    str(index): {"progress": {"median": index / 4.0}}
                    for index in (1, 2, 3)
                }
                for metric in (
                    "dangling_one_area",
                    "dangling_two_area",
                    "dangling_one_max_duration",
                    "dangling_two_max_duration",
                    "released_ab_per_structure_click",
                    "released_ab_on_match_completion_click",
                    "released_ab_total_per_path",
                ):
                    reveal[metric] = {"median": 7.0}
            return row

        source_rows = tuple(
            complete_reveal_row(index) for index in range(1, 11)
        )
        candidate_rows = tuple(
            complete_reveal_row(index) for index in range(1, 11)
        )

        cohort = _difficulty_distance(
            candidate_rows,
            source_rows,
            board_size=10,
            source_has_cn=False,
            structure_count=9,
            requested_archetype="auto",
        )
        direct = _difficulty_distance(
            candidate_rows,
            source_rows,
            board_size=10,
            source_has_cn=True,
        )

        self.assertGreater(cohort["primary_distance"], 0.0)
        self.assertEqual(
            cohort["whole_board_reveal_baseline"]["mode"],
            "reference_cohort",
        )
        self.assertEqual(
            cohort["reveal_reference_distance"]["status"], "compared"
        )
        self.assertIn(
            "whole_board_reveal_pressure",
            cohort["duplicates"][0]["components"],
        )
        self.assertEqual(direct["primary_distance"], 0.0)
        self.assertEqual(
            direct["whole_board_reveal_baseline"]["mode"],
            "source_template",
        )

    def test_missing_dynamic_reference_is_inconclusive_not_zero_distance(self) -> None:
        source = _strategy_row(reveal_pressure=0.0)
        candidate = _strategy_row(reveal_pressure=7.0)
        report = _difficulty_distance(
            (candidate,),
            (source,),
            board_size=10,
            source_has_cn=False,
            structure_count=6,
            requested_archetype="auto",
        )
        reference = report["reveal_reference_distance"]
        self.assertEqual(reference["status"], "inconclusive")
        self.assertIsNone(reference["primary_distance"])
        self.assertNotIn(
            "whole_board_reveal_pressure",
            report["duplicates"][0]["components"],
        )

    def test_wilson_precision_can_remain_inconclusive_at_resource_limit(self) -> None:
        interval = _wilson_interval(12, 24)
        self.assertGreater(interval["half_width"], 0.18)

    def test_parameter_underflow_is_explicitly_downgraded(self) -> None:
        level = _level()
        profile = DifficultyProfile(
            duplicate_params=tuple(
                DuplicateParameter(a=0, seed=str(index), luck="100", b=1)
                for index in range(10)
            ),
            coverage="complete",
        )
        report = simulate_candidate_difficulty(
            level,
            (
                SimpleNamespace(
                    tile_index=0, structure_type="C", source_tile_id=-10
                ),
            ),
            target="strong",
            profile=profile,
        )

        self.assertFalse(report["qualified"])
        self.assertEqual(len(report["parameter_adjustment"]["underflow"]), 10)
        self.assertTrue(
            any("已按 0 模拟并降级" in value for value in report["downgrade_reasons"])
        )

    def test_a_and_b_source_positions_never_borrow_each_others_pool(self) -> None:
        level = _level()
        labels = _materialized_types(
            level,
            (),
            DuplicateParameter(a=2, seed="9", luck="100", b=2),
            1,
        )

        for tile in level.tiles:
            expected_prefix = "A:" if tile.tile_id == -10 else "B:"
            self.assertTrue(labels[tile.index].startswith(expected_prefix))

    def test_luck_does_not_change_initial_ab_materialization_proxy(self) -> None:
        level = _level()
        first = _materialized_types(
            level,
            (),
            DuplicateParameter(a=2, seed="9", luck="100", b=2),
            1,
        )
        changed_luck = _materialized_types(
            level,
            (),
            DuplicateParameter(a=2, seed="9", luck="777", b=2),
            1,
        )

        self.assertEqual(first, changed_luck)

    def test_embedded_profile_snapshot_rehydrates_for_later_validation(self) -> None:
        level = _level()
        profile = DifficultyProfile(
            duplicate_params=tuple(
                DuplicateParameter(5, str(index), "100", 3)
                for index in range(10)
            ),
            coverage="complete",
            source={"config_level_path": "stable/config_level.csv"},
        )
        replacements = (
            SimpleNamespace(tile_index=0, structure_type="C", source_tile_id=-10),
        )
        first = simulate_candidate_difficulty(
            level, replacements, target="fidelity", profile=profile
        )
        restored = difficulty_profile_from_simulation(first)
        self.assertIsNotNone(restored)
        replay = simulate_candidate_difficulty(
            level, replacements, target="fidelity", profile=restored
        )
        self.assertEqual(first, replay)

    def test_profile_lookup_never_silently_selects_conflicting_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = ["6|1|100|4"] * 10
            _write_config(
                root / "first" / "config_level.csv",
                [["l1", "difficulty001", *values]],
            )
            profile = Profile("p", "test", None, None, str(root), str(root))
            complete = load_difficulty_profile(profile, "difficulty001")
            self.assertEqual(complete.coverage, "complete")
            self.assertEqual(len(complete.duplicate_params), 10)

            different = ["9|2|100|7"] * 10
            _write_config(
                root / "second" / "config_level.csv",
                [["l2", "difficulty001", *different]],
            )
            ambiguous = load_difficulty_profile(profile, "difficulty001")
            self.assertEqual(ambiguous.coverage, "ambiguous")
            self.assertEqual(ambiguous.duplicate_params, ())
            self.assertEqual(ambiguous.source["match_count"], 2)

    def test_recommendation_prefers_source_distance_before_layout_score(self) -> None:
        def generated(candidate_id: str, distance: float, quality: float):
            return GeneratedCandidate(
                candidate=SimpleNamespace(
                    candidate_id=candidate_id,
                    candidate_index=1,
                    quality_score=quality,
                    quality_metrics={"experience_risk_penalty": 1.0},
                    quality_summary=(),
                    difficulty_simulation={
                        "qualified": True,
                        "source_difficulty_distance": distance,
                        "trend": {"becomes_easier": True, "violations": []},
                    },
                ),
                validation=None,
            )

        far_pretty = generated("far", 20.0, 100.0)
        close_plain = generated("close", 2.0, 1.0)
        ranked = _recommend_candidates((far_pretty, close_plain))
        by_id = {item.candidate.candidate_id: item for item in ranked}
        self.assertEqual(by_id["close"].recommendation_rank, 1)
        self.assertEqual(by_id["far"].recommendation_rank, 2)

    def test_trend_violation_is_not_recommended(self) -> None:
        candidate = SimpleNamespace(
            candidate_id="reverse-gradient",
            candidate_index=1,
            quality_score=999.0,
            quality_metrics={"experience_risk_penalty": 0.0},
            quality_summary=(),
            difficulty_simulation={
                "qualified": False,
                "source_difficulty_distance": 0.0,
                "trend": {"becomes_easier": False, "violations": [{}]},
            },
        )
        (ranked,) = _recommend_candidates(
            (GeneratedCandidate(candidate=candidate, validation=None),)
        )
        self.assertFalse(ranked.is_recommended)
        self.assertIsNone(ranked.recommendation_rank)

    def test_explicit_pair_source_cache_cannot_cross_templates(self) -> None:
        source = _level()
        other = parse_level_bytes(
            source.data.replace(b"difficulty001", b"difficulty002", 1),
            source_name="difficulty002_m.bytes",
        )
        duplicate = DuplicateParameter(a=6, seed="0", luck="100", b=4)
        first = compare_duplicate_difficulty_pair(
            source,
            (),
            source_duplicate=duplicate,
            candidate_duplicate=duplicate,
            duplicate_index=1,
        )
        self.assertEqual(
            first["source"]["pair_context"]["template_sha256"],
            source.sha256,
        )
        with self.assertRaisesRegex(ValueError, "template, geometry"):
            compare_duplicate_difficulty_pair(
                other,
                (),
                source_duplicate=duplicate,
                candidate_duplicate=duplicate,
                duplicate_index=1,
                source_report=first["source"],
            )


if __name__ == "__main__":
    unittest.main()
