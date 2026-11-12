from __future__ import annotations

import csv
import io
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

from puzzle_core.errors import InputValidationError  # noqa: E402
from services.consumption_calibration_service import (  # noqa: E402
    DUPLICATE_OBJECTIVE_WEIGHTS,
    _align_source_labels_if_geometry_compatible,
    _association_estimate_from_pool,
    _candidate_space,
    _cached_association_estimate,
    _consumption_prediction,
    _duplicate_objective_weight,
    _empirical_static_structure_features,
    _gradient_policy_evidence,
    _index_association_history,
    _load_structure_correction_model,
    _observed_seeds,
    _prefilter_ab_candidates,
    _prefilter_seed_candidates,
    _recommendation_issue_counts,
    _resolve_target_history,
    _resolve_calibration_template_id,
    _serialize_candidate,
    _select_one_baseline_per_output,
    _seed_search_values,
    _target_record,
    _transition_penalty,
    calibrate_consumption_parameters,
    parse_consumption_history,
)
from services.parameter_recommendation_service import DupParam  # noqa: E402
from services.profile_service import Profile  # noqa: E402
from scripts.tests.test_difficulty import _level  # noqa: E402


HEADER = [
    "level_template_id",
    "level_dup",
    "level_dup_para_id",
    "strategy_group_id",
    "strategy_effective_list",
    "进入人数",
    "人均消耗",
]


def _history(rows: list[list[object]], *, header: list[str] | None = None) -> bytes:
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(header or HEADER)
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


class ConsumptionCalibrationTests(unittest.TestCase):
    def test_explicit_prefix_overrides_manifest_exact_and_other_suffix_matches(self) -> None:
        manifest = {"source_template_id": "lessThr0008", "output_template_id": "aipuzzlelessThr0008s5v2_m.bytes", "signature": "s5"}
        self.assertEqual(_resolve_calibration_template_id(
            manifest, {"lessThr0008", "Thr0008", "hr0008"},
            prefix="aipuzzlelessT", suffix="s5",
        )[0], "hr0008")
        with self.assertRaisesRegex(InputValidationError, "hr0008"):
            _resolve_calibration_template_id(manifest, {"lessThr0008"}, prefix="aipuzzlelessT", suffix="s5")
        with self.assertRaisesRegex(InputValidationError, "前缀"):
            _resolve_calibration_template_id(manifest, {"lessThr0008"}, prefix="wrong", suffix="s5")

    def test_auto_matching_rejects_multiple_complete_csv_ids(self) -> None:
        manifest = {"source_template_id": "lessThr0008", "output_template_id": "aipuzzlelessThr0008s5", "signature": "s5"}
        with self.assertRaisesRegex(InputValidationError, "Thr0008.*hr0008"):
            _resolve_calibration_template_id(manifest, {"Thr0008", "hr0008"})
        self.assertEqual(_resolve_calibration_template_id(manifest, {"hr0008"})[0], "hr0008")

    def test_target_history_uses_explicit_canonical_match_before_processed_source(self) -> None:
        rows, _ = parse_consumption_history(_history([
            ["lessThr0008", "7|0|100|4", 1, 1003, "", 100, 900],
            ["hr0008", "7|0|100|4", 1, 1003, "", 100, 500],
        ]))
        selected, template_id, _ = _resolve_target_history(rows,
            source_template_id="lessThr0008", config_source_template_id="lessThr0008",
            canonical_template_id="hr0008", selected_level_ids=set())
        self.assertEqual(template_id, "hr0008")
        self.assertEqual(len(selected), 1)

    def test_name_preview_reports_matches_and_conflicts_without_running_simulation(self) -> None:
        from services import consumption_calibration_service as service
        preview = getattr(service, "match_calibration_templates", None)
        self.assertTrue(callable(preview), "CSV matching must be available before starting simulation")
        manifests = [{"source_template_id": "lessThr0008", "output_template_id": "aipuzzlelessThr0008s5", "signature": "s5"}]
        data = _history([[key, "7|0|100|4", 1, 1003, "", 100, 500] for key in ("hr0008", "Thr0008")])
        result = preview(manifests, history_data=data)
        self.assertEqual(result["items"][0]["status"], "ambiguous")
        self.assertEqual(result["items"][0]["matches"], ["Thr0008", "hr0008"])
        result = preview(manifests, history_data=data, template_name_prefix="aipuzzlelessT", template_name_suffix="s5")
        self.assertEqual(result["matched_count"], 1)
        self.assertEqual(result["items"][0]["matched_template_id"], "hr0008")
        self.assertEqual(manifests[0]["source_template_id"], "lessThr0008")

    def test_calibration_template_names_resolve_complete_unique_history_ids(self) -> None:
        manifest = {
            "source_template_id": "lessThr0010",
            "output_template_id": "aipuzzlelessThr0010s5v2_m.bytes",
            "signature": "s5",
        }
        self.assertEqual(
            _resolve_calibration_template_id(manifest, {"hr0010", "hr001"})[0],
            "hr0010",
        )
        self.assertEqual(
            _resolve_calibration_template_id(
                manifest, {"hr0010", "lessThr0010"}
            )[0], "lessThr0010",
        )
        custom = {
            "source_template_id": "processed-source",
            "output_template_id": "aipuzzlelessTmxd0057v1s5v4_m.bytes",
            "signature": "s5",
        }
        self.assertEqual(
            _resolve_calibration_template_id(
                custom, {"mxd0057v1", "mxd0057"},
                prefix="aipuzzlelessT", suffix="s5",
            )[0], "mxd0057v1",
        )
        self.assertIsNone(
            _resolve_calibration_template_id(manifest, {"hr001"})[0]
        )
        with self.assertRaisesRegex(InputValidationError, "无法唯一匹配"):
            _resolve_calibration_template_id(manifest, {"hr0010", "r0010"})

    def test_canonical_history_id_supplies_parameters_targets_and_position_without_rewriting_source(self) -> None:
        level = _level()
        manifest = {
            "source_template_id": "lessThr0010",
            "output_template_id": "aipuzzlelessThr0010s5v2",
            "source_sha256": level.sha256,
            "source_tile_counts": {"A": 12, "B": 0},
            "signature": "s5",
            "replacements": [],
        }
        history = _history([
            ["hr0010", "7|0|100|4", index, 1003, "", 100, 500, 330]
            for index in range(1, 11)
        ], header=[*HEADER, "level_index"])
        passed_manifests: list[dict[str, object]] = []

        def baseline(_profile, manifests, **_kwargs):
            passed_manifests.extend(manifests)
            return {
                "rows": [{
                    "status": "ready", "source_template_id": "hr0010",
                    "config_source_template_id": "hr0010",
                    "output_template_id": "aipuzzlelessThr0010s5v2",
                    "level_ids": ["online0010"], "online_positions": [1200],
                    "before_dup": ["7|0|100|4"] * 10,
                    "recommended_dup": ["7|0|100|4"] * 10,
                    "strategy_group_id": "1003", "random_seed_group_id": 0,
                }], "warnings": [], "source_tables": {},
            }

        with patch(
            "services.consumption_calibration_service.build_parameter_recommendations",
            side_effect=baseline,
        ), patch(
            "services.consumption_calibration_service.load_template_id",
            return_value=SimpleNamespace(level=level),
        ) as loader:
            result = calibrate_consumption_parameters(
                Profile("p", "test", None, None, "/tmp/out", "/tmp/config"),
                [manifest], config_set_id="csv", history_data=history,
                history_filename="history.csv", template_name_prefix="aipuzzlelessT",
                template_name_suffix="s5",
            )
        (row,) = result["rows"]
        self.assertEqual(passed_manifests[0]["source_template_id"], "hr0010")
        self.assertEqual(manifest["source_template_id"], "lessThr0010")
        self.assertEqual(row["source_template_id"], "lessThr0010")
        self.assertEqual(row["config_source_template_id"], "hr0010")
        self.assertEqual(row["canonical_template_id"], "hr0010")
        self.assertEqual(row["seed_simulation_level_index"], 330)
        self.assertEqual(row["duplicates"][0]["target_consumption"]["value"], 500.0)
        self.assertEqual(row["source_fingerprints"]["source_template_sha256"], level.sha256)
        self.assertEqual(loader.call_args_list[0].args[1], "lessThr0010")

    def test_processed_config_caps_search_but_preserves_online_consumption_target(self) -> None:
        level = _level()
        manifest = {"source_template_id":"lessTmxd0003", "output_template_id":"aipuzzlelessTmxd0003s5", "source_sha256":level.sha256, "source_tile_counts":{"A":12,"B":0}, "signature":"s5", "replacements":[]}
        history = _history([["mxd0003","8|0|100|4",i,1003,"",100,500,330] for i in range(1,11)], header=[*HEADER,"level_index"])
        def baseline(_profile, manifests, **_kwargs):
            processed = manifests[0]["source_template_id"] == "lessTmxd0003"
            return {"rows":[{"status":"ready", "source_template_id":manifests[0]["source_template_id"], "config_source_template_id":manifests[0]["source_template_id"], "output_template_id":manifest["output_template_id"], "level_ids":[], "online_positions":[330], "before_dup":["5|0|100|4" if processed else "8|0|100|4"]*10, "recommended_dup":["4|0|100|4" if processed else "7|0|100|4"]*10, "strategy_group_id":"1003", "random_seed_group_id":0}], "warnings":[], "source_tables":{}}
        with patch("services.consumption_calibration_service.build_parameter_recommendations",side_effect=baseline), patch("services.consumption_calibration_service.load_template_id", return_value=SimpleNamespace(level=level)):
            result = calibrate_consumption_parameters(Profile("p","test",None,None,"/tmp/out","/tmp/config"), [manifest], config_set_id="csv", history_data=history, history_filename="history.csv")
            with self.assertRaisesRegex(InputValidationError, "4–5"):
                calibrate_consumption_parameters(Profile("p","test",None,None,"/tmp/out","/tmp/config"), [manifest], config_set_id="csv", history_data=history, history_filename="history.csv", focus_output_template_id=manifest["output_template_id"], first_duplicate_max=6)
        row = result["rows"][0]
        self.assertLessEqual(max(parse for parse in (int(row["duplicates"][0]["recommended_dup"].split("|")[i]) for i in (0,3))), 5)
        self.assertEqual(row["first_duplicate_adjustment"]["upper_bound"], 5)
        self.assertEqual(row["source_parameter_comparison"]["status"], "different")
        self.assertEqual(row["source_parameter_comparison"]["different_duplicates"], list(range(1,11)))
        self.assertEqual(row["duplicates"][0]["target_consumption"]["value"], 500.0)
        self.assertEqual(row["duplicates"][0]["original_dup"], "8|0|100|4")

    def test_online_source_labels_are_aligned_only_for_identical_geometry(self) -> None:
        clean = _level()
        source = replace(
            clean,
            data=b"online-source-with-structures",
            tiles=tuple(
                replace(tile, tile_id=-12 if tile.index < 3 else tile.tile_id)
                for tile in clean.tiles
            ),
        )
        labels = tuple(f"source:{tile.index}" for tile in source.tiles)

        aligned, evidence = _align_source_labels_if_geometry_compatible(
            source, clean, labels
        )

        self.assertEqual(aligned, labels)
        self.assertEqual(evidence["status"], "compatible")
        self.assertTrue(evidence["source_contains_structures"])

        incompatible = replace(
            source,
            tiles=(replace(source.tiles[0], x=999), *source.tiles[1:]),
        )
        aligned, evidence = _align_source_labels_if_geometry_compatible(
            incompatible, clean, labels
        )
        self.assertIsNone(aligned)
        self.assertEqual(evidence["status"], "incompatible")

    def test_duplicate_objective_weights_prioritize_first_four(self) -> None:
        self.assertEqual(
            DUPLICATE_OBJECTIVE_WEIGHTS,
            {1: 4.0, 2: 4.0, 3: 2.0, 4: 2.0, **{index: 1.0 for index in range(5, 11)}},
        )
        self.assertEqual(_duplicate_objective_weight(1), 4.0)
        self.assertEqual(_duplicate_objective_weight(4), 2.0)
        self.assertEqual(_duplicate_objective_weight(10), 1.0)

    def test_target_history_resolves_whichever_source_alias_the_csv_contains(self) -> None:
        clean_rows, _warnings = parse_consumption_history(
            _history(
                [
                    ["source-clean", "7|0|100|4", index, 1003, "", 100, 500]
                    for index in range(1, 11)
                ]
            )
        )
        rows, template_id, scope = _resolve_target_history(
            clean_rows,
            source_template_id="source-clean",
            config_source_template_id="source",
            selected_level_ids=set(),
        )
        self.assertEqual(template_id, "source-clean")
        self.assertEqual(len(rows), 10)
        self.assertEqual(scope, "resolved_source_alias_fallback")

        config_rows, _warnings = parse_consumption_history(
            _history(
                [
                    ["source", "7|0|100|4", index, 1003, "", 100, 500]
                    for index in range(1, 11)
                ]
            )
        )
        rows, template_id, scope = _resolve_target_history(
            config_rows,
            source_template_id="source-clean",
            config_source_template_id="source",
            selected_level_ids=set(),
        )
        self.assertEqual(template_id, "source")
        self.assertEqual(len(rows), 10)
        self.assertEqual(scope, "resolved_source_alias_fallback")

    def test_gradient_policy_evidence_does_not_claim_hard_monotonicity_for_fallback(self) -> None:
        normal = _gradient_policy_evidence(False)
        fallback = _gradient_policy_evidence(True)

        self.assertTrue(normal["no_new_parameter_rebound"])
        self.assertIn("hard no-new-rebound", normal["policy"])
        self.assertFalse(fallback["no_new_parameter_rebound"])
        self.assertNotIn("hard no-new-rebound", fallback["policy"])
        self.assertTrue(fallback["infeasible_bounds_fallback"])

    def test_unavailable_model_preserves_target_without_inventing_prediction(self) -> None:
        prediction = _consumption_prediction(
            target=500.0,
            manifest={"signature": "s6", "validation": {}},
            original=DupParam(8, "0", "100", 5),
            candidate=DupParam(7, "31", "100", 5),
            duplicate_index=1,
            level_index=250,
            model=None,
        )

        self.assertIsNone(prediction["value"])
        self.assertEqual(prediction["target"], 500.0)
        self.assertEqual(prediction["source"], "consumption_estimate_unavailable")
        self.assertFalse(prediction["structure_correction"]["applied"])
        self.assertIn("model_unavailable", prediction["structure_correction"]["reasons"])

    def test_supported_prediction_is_bounded_and_seed_is_not_a_consumption_feature(self) -> None:
        model = {
            "model_id": "test-model",
            "scope": {
                "level_index_min": 200,
                "level_index_max": 300,
                "signatures": ["s5"],
                "structure_count_min": 9,
                "structure_count_max": 18,
            },
            "feature_order": ["delta_a"],
            "means": {"delta_a": 0.0},
            "scales": {"delta_a": 1.0},
            "coefficients": {"delta_a": 10.0},
            "intercept": 0.0,
            "log_ratio_clip": [-0.2, 0.2],
            "prediction_interval_log_offsets": [-0.1, 0.1],
        }
        manifest = {
            "signature": "s5",
            "source_tile_counts": {"A": 60, "B": 40},
            "replacements": [
                {"source_tile_id": -10, "structure_type": "C"}
                for _ in range(9)
            ],
            "validation": {"replacement_count": 9, "structure_counts": [["C", 9]]},
        }
        first = _consumption_prediction(
            target=500.0,
            manifest=manifest,
            original=DupParam(8, "0", "100", 5),
            candidate=DupParam(7, "0", "100", 5),
            duplicate_index=1,
            level_index=250,
            model=model,
        )
        changed_seed = _consumption_prediction(
            target=500.0,
            manifest=manifest,
            original=DupParam(8, "0", "100", 5),
            candidate=DupParam(7, "31", "100", 5),
            duplicate_index=1,
            level_index=250,
            model=model,
        )

        self.assertEqual(first["value"], changed_seed["value"])
        self.assertAlmostEqual(first["value"], 500.0 * __import__("math").exp(-0.2), places=4)
        self.assertEqual(first["source"], "source_target_empirical_structure_correction")
        self.assertTrue(first["structure_correction"]["applied"])
        self.assertTrue(first["structure_correction"]["bounded"])

        out_of_scope = _consumption_prediction(
            target=500.0,
            manifest={
                **manifest,
                "replacements": manifest["replacements"][:3],
                "validation": {"replacement_count": 3, "structure_counts": [["C", 3]]},
            },
            original=DupParam(8, "0", "100", 5),
            candidate=DupParam(7, "0", "100", 5),
            duplicate_index=1,
            level_index=250,
            model=model,
        )
        self.assertFalse(out_of_scope["structure_correction"]["applied"])
        self.assertIn(
            "outside_structure_count",
            out_of_scope["structure_correction"]["reasons"],
        )

    def test_checked_in_model_applies_to_supported_manifest(self) -> None:
        model = _load_structure_correction_model()
        self.assertIsNotNone(model)
        manifest = {
            "signature": "s5",
            "source_tile_counts": {"A": 60, "B": 42},
            "replacements": [
                {
                    "tile_index": index,
                    "source_tile_id": -10,
                    "structure_type": chr(ord("C") + index // 3),
                }
                for index in range(9)
            ],
            "validation": {
                "replacement_count": 9,
                "replaced_a_count": 9,
                "replaced_b_count": 0,
                "structure_counts": [["C", 3], ["D", 3], ["E", 3]],
            },
            "puzzle_units": [],
        }

        level = _level()
        static_features = _empirical_static_structure_features(
            level,
            [SimpleNamespace(**item) for item in manifest["replacements"]],
        )
        self.assertEqual(static_features["replacement_count"], 9.0)
        self.assertEqual(static_features["source_total"], 12.0)
        from services.consumption_calibration_service import _paired_board_features
        static_features.update(_paired_board_features(level, level))

        prediction = _consumption_prediction(
            target=800.0,
            manifest=manifest,
            original=DupParam(9, "0", "100", 6),
            candidate=DupParam(7, "31", "100", 6),
            duplicate_index=1,
            level_index=250,
            model=model,
            static_structure_features=static_features,
            strategy_group_id="1003",
        )

        self.assertEqual(
            prediction["source"], "source_target_empirical_structure_correction"
        )
        self.assertTrue(prediction["structure_correction"]["applied"])
        self.assertEqual(
            prediction["structure_correction"]["model_id"],
            "consumption-structure-correction-20261027-v2",
        )

        without_exact_static_context = _consumption_prediction(
            target=800.0,
            manifest=manifest,
            original=DupParam(9, "0", "100", 6),
            candidate=DupParam(7, "31", "100", 6),
            duplicate_index=1,
            level_index=250,
            model=model,
            strategy_group_id="1003",
        )
        self.assertFalse(
            without_exact_static_context["structure_correction"]["applied"]
        )
        self.assertIn(
            "static_feature_context_unavailable",
            without_exact_static_context["structure_correction"]["reasons"],
        )

        restricted_model = {**model, "scope": {**model["scope"], "strategy_groups_observed": [1003]}}
        unsupported_group = _consumption_prediction(
            target=800.0,
            manifest=manifest,
            original=DupParam(9, "0", "100", 6),
            candidate=DupParam(7, "31", "100", 6),
            duplicate_index=1,
            level_index=250,
            model=restricted_model,
            static_structure_features=static_features,
            strategy_group_id="9999",
        )
        self.assertFalse(unsupported_group["structure_correction"]["applied"])
        self.assertIn(
            "outside_strategy_group",
            unsupported_group["structure_correction"]["reasons"],
        )

        mismatched_count = _consumption_prediction(
            target=800.0,
            manifest=manifest,
            original=DupParam(9, "0", "100", 6),
            candidate=DupParam(7, "31", "100", 6),
            duplicate_index=1,
            level_index=250,
            model=model,
            static_structure_features={
                **static_features,
                "replacement_count": 3.0,
            },
            strategy_group_id="1003",
        )
        self.assertFalse(mismatched_count["structure_correction"]["applied"])
        self.assertIn(
            "static_manifest_structure_count_mismatch",
            mismatched_count["structure_correction"]["reasons"],
        )

    def test_transition_rejects_a_new_parameter_rebound(self) -> None:
        previous = {
            "original": DupParam(8, "0", "100", 6),
            "parameter": DupParam(7, "0", "100", 5),
        }
        current = {
            "original": DupParam(7, "0", "100", 5),
            "parameter": DupParam(8, "0", "100", 5),
        }
        self.assertEqual(_transition_penalty(previous, current), float("inf"))
        source_also_rebounds = {
            "original": DupParam(9, "0", "100", 6),
            "parameter": DupParam(8, "0", "100", 5),
        }
        self.assertEqual(
            _transition_penalty(previous, source_also_rebounds),
            float("inf"),
        )

    def test_two_stage_prefilter_bounds_full_simulation_and_keeps_anchors(self) -> None:
        original = DupParam(8, "0", "100", 6)
        baseline = DupParam(5, "0", "100", 4)
        raw = []
        for a in range(5, 9):
            for b in range(4, 7):
                for seed in range(32):
                    raw.append(
                        {
                            "parameter": DupParam(a, str(seed), "100", b),
                            "node_score": abs(a - 7) + abs(b - 5),
                        }
                    )
        ab_candidates, evidence = _prefilter_ab_candidates(
            raw, original=original, baseline=baseline
        )
        self.assertEqual(evidence["enumerated_seed_count"], 32)
        self.assertLessEqual(evidence["selected_ab_count"], 8)
        selected_ab = {(item["parameter"].a, item["parameter"].b) for item in ab_candidates}
        self.assertIn((baseline.a, baseline.b), selected_ab)
        self.assertIn((original.a, original.b), selected_ab)

        for item in ab_candidates:
            item["light_seed_distance"] = abs(int(item["parameter"].seed) - 17)
        full, seed_evidence = _prefilter_seed_candidates(
            ab_candidates, original=original
        )
        self.assertTrue(any(item["parameter"].seed == "17" for item in full))
        self.assertLessEqual(
            len(full),
            evidence["selected_ab_count"] * 3,
        )
        self.assertEqual(seed_evidence["seed_range"], [0, 31])

    def test_candidate_space_rejects_anchor_below_duplicate_fidelity_baseline(self) -> None:
        original = DupParam(8, "0", "100", 6)
        baseline = DupParam(7, "0", "100", 5)

        candidates = _candidate_space(
            original,
            baseline,
            seeds=["0", "31"],
            additional_ab_pairs=[(4, 3), (7, 5), (8, 6)],
        )

        self.assertTrue(candidates)
        self.assertTrue(all(item.a >= baseline.a for item in candidates))
        self.assertTrue(all(item.b >= baseline.b for item in candidates))
        self.assertNotIn((4, 3), {(item.a, item.b) for item in candidates})
        self.assertEqual(
            {(item.a, item.b) for item in candidates},
            {(7, 5), (7, 6), (8, 5), (8, 6)},
        )

    def test_selected_candidate_is_labeled_best_fit_even_outside_tolerance(self) -> None:
        candidate = {
            "parameter": DupParam(7, "31", "100", 4),
            "original": DupParam(8, "0", "100", 4),
            "association": {"status": "unavailable", "point": None, "range": [None, None]},
            "consumption_prediction": {
                "value": 900.0,
                "range": [800.0, 1000.0],
                "target": 500.0,
                "source": "source_target_empirical_structure_correction",
                "structure_correction": {"applied": True, "in_scope": True},
            },
            "node_score": 1.0,
        }
        serialized = _serialize_candidate(candidate, {"value": 500.0})
        self.assertEqual(serialized["recommendation"]["status"], "best_fit_outside_tolerance")
        self.assertEqual(serialized["recommendation"]["label"], "偏差较大")
        self.assertTrue(serialized["recommendation"]["is_best_fit"])
        self.assertFalse(serialized["recommendation"]["is_recommended"])

    def test_target_anchor_fallback_is_directional_not_a_false_recommendation(self) -> None:
        candidate = {
            "parameter": DupParam(7, "0", "100", 4),
            "original": DupParam(8, "0", "100", 4),
            "association": {"status": "association_only", "point": 510.0, "range": [450.0, 600.0]},
            "consumption_prediction": {
                "value": 500.0,
                "range": [None, None],
                "target": 500.0,
                "source": "source_target_conservative_baseline",
                "structure_correction": {
                    "applied": False,
                    "in_scope": False,
                    "reasons": ["outside_structure_count"],
                },
            },
            "node_score": 0.0,
        }
        serialized = _serialize_candidate(candidate, {"value": 500.0})
        self.assertEqual(
            serialized["recommendation"]["status"],
            "best_fit_directional",
        )
        self.assertEqual(
            serialized["recommendation"]["label"],
            "仅供参考",
        )
        self.assertFalse(serialized["recommendation"]["is_recommended"])
        self.assertFalse(serialized["recommendation"]["export_blocked"])

    def test_incomplete_board_simulation_cannot_be_labeled_recommended(self) -> None:
        candidate = {
            "parameter": DupParam(7, "0", "100", 4),
            "original": DupParam(8, "0", "100", 4),
            "association": {"status": "unavailable", "point": None},
            "consumption_prediction": {
                "value": 505.0,
                "target": 500.0,
                "source": "source_target_empirical_structure_correction",
                "structure_correction": {"applied": True, "in_scope": True},
            },
            "simulation_evidence_ready": False,
            "simulation_evidence_reasons": [
                "structure_strategy_config_unavailable"
            ],
            "node_score": 0.01,
        }

        serialized = _serialize_candidate(candidate, {"value": 500.0})

        self.assertEqual(
            serialized["recommendation"]["status"], "best_fit_directional"
        )
        self.assertEqual(
            serialized["recommendation"]["label"],
            "模拟未完成 · 需确认",
        )
        self.assertFalse(serialized["recommendation"]["is_recommended"])
        self.assertFalse(serialized["recommendation"]["export_blocked"])

    def test_recommendation_issue_counts_separate_gap_from_evidence(self) -> None:
        rows = [
            {
                "recommendation": {
                    "status": "recommended",
                    "is_recommended": True,
                    "relative_gap": 0.10,
                }
            },
            {
                "recommendation": {
                    "status": "best_fit_directional",
                    "is_recommended": False,
                    "relative_gap": 0.05,
                }
            },
            {
                "recommendation": {
                    "status": "best_fit_directional",
                    "is_recommended": False,
                    "relative_gap": 0.40,
                }
            },
            {
                "recommendation": {
                    "status": "best_fit_review",
                    "is_recommended": False,
                    "relative_gap": 0.30,
                }
            },
        ]

        self.assertEqual(_recommendation_issue_counts(rows), (3, 2, 2))

    def test_association_history_is_indexed_and_unique_queries_are_cached(self) -> None:
        data = _history(
            [
                ["source", "7|0|100|4", 1, 1003, "", 100, 500],
                ["other", "6|0|100|4", 1, 1003, "", 200, 480],
                ["other", "6|0|100|4", 2, 1003, "", 300, 460],
                ["other", "6|0|100|4", 1, 2000, "", 400, 440],
            ]
        )
        rows, _warnings = parse_consumption_history(data)
        index = _index_association_history(rows)
        self.assertEqual(len(index[(1, "1003")]), 2)
        self.assertEqual(len(index[(2, "1003")]), 1)
        self.assertEqual(len(index[(1, "2000")]), 1)

        cache: dict[tuple[int, str, int, int, str], dict[str, object]] = {}
        candidate = DupParam(6, "0", "100", 4)
        with patch(
            "services.consumption_calibration_service._association_estimate_from_pool",
            wraps=_association_estimate_from_pool,
        ) as estimate:
            first = _cached_association_estimate(
                index,
                cache,
                duplicate_index=1,
                strategy_group_id="1003",
                candidate=candidate,
            )
            second = _cached_association_estimate(
                index,
                cache,
                duplicate_index=1,
                strategy_group_id="1003",
                candidate=candidate,
            )

        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertEqual(estimate.call_count, 1)
        scoped_pool = estimate.call_args.args[0]
        self.assertEqual(len(scoped_pool), 2)

    def test_duplicate_csv_headers_are_rejected(self) -> None:
        data = _history(
            [["source", "7|0|100|4", 1, 1003, "", 100, 500]],
            header=[*HEADER, "人均消耗"],
        )
        with self.assertRaises(InputValidationError) as caught:
            parse_consumption_history(data)
        self.assertIn("重复表头", str(caught.exception))
        self.assertEqual(
            caught.exception.detail.evidence["duplicate_columns"],
            ["人均消耗"],
        )

    def test_current_tile_duplicate_export_parses_without_strategy_columns(self) -> None:
        header = [
            "user_info",
            "level_index",
            "level_id",
            "level_template_id",
            "level_dup",
            "level_dup_para_id",
            "进入人数",
            "胜率",
            "首局胜率",
            "裸胜率",
            "人均关卡时长",
            "流失",
            "失败流失",
            "胜利流失",
            "人均消耗",
            "消耗人数占比",
            "消耗关卡占比",
            "人均道具",
            "通关高压次数",
            "失败无解进度",
            "胜利FUUU",
            "失败FUUU",
            "人均失败",
            "人均复活",
            "人均高压",
            "人均背包满",
        ]
        row = [
            "tile01g", "185", "list150v2ntmx0016v1", "ntmx0016",
            "12|2|100|4", "4", "31226", "86.36%", "89.72%",
            "35.06%", "3.77", "1.84%", "0.69%", "1.15%", "994.88",
            "69.98%", "69.98%", "0.62", "5.24", "57.94", "65.00%",
            "59.56%", "0.08", "0.25", "4.35", "0.35",
        ]

        rows, warnings = parse_consumption_history(
            _history([row], header=header)
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].template_id, "ntmx0016")
        self.assertEqual(rows[0].duplicate_index, 4)
        self.assertEqual(rows[0].parameter.render(), "12|2|100|4")
        self.assertEqual(rows[0].strategy_group_id, "")
        self.assertEqual(rows[0].effective_strategies, ())
        self.assertEqual(rows[0].entrants, 31226)
        self.assertEqual(rows[0].consumption, 994.88)
        self.assertFalse(any("StrategyGroupId" in item for item in warnings))

        target = _target_record(
            rows,
            source_template_id="ntmx0016",
            duplicate_index=4,
            strategy_group_id="1003",
            override=None,
        )
        self.assertEqual(target["value"], 994.88)
        self.assertEqual(target["sample_rows"], 1)

        index = _index_association_history(rows)
        association = _cached_association_estimate(
            index,
            {},
            duplicate_index=4,
            strategy_group_id="1003",
            candidate=DupParam(12, "2", "100", 4),
        )
        self.assertEqual(association["status"], "association_only")
        self.assertEqual(association["neighbour_count"], 1)

    def test_observed_seed_search_excludes_non_integer_seed(self) -> None:
        data = _history(
            [
                ["source", "7|0|100|4", 1, 1003, "", 100, 500],
                ["other", "7|2|100|4", 1, 1003, "", 200, 480],
                ["other", "7|abc|100|4", 1, 1003, "", 5000, 1],
                ["other", "7|2147483648|100|4", 1, 1003, "", 5000, 1],
            ]
        )
        rows, _warnings = parse_consumption_history(data)
        self.assertEqual(
            _observed_seeds(
                rows,
                duplicate_index=1,
                strategy_group_id="1003",
                original_seed="0",
            ),
            ["2", "0"],
        )

    def test_seed_search_modes_are_not_ignored(self) -> None:
        rows, _warnings = parse_consumption_history(
            _history(
                [
                    ["source", "7|0|100|4", 1, 1003, "", 100, 500],
                    ["other", "7|2|100|4", 1, 1003, "", 200, 480],
                ]
            )
        )
        original = DupParam(7, "0", "100", 4)
        self.assertEqual(
            _seed_search_values(
                rows,
                duplicate_index=1,
                strategy_group_id="1003",
                original=original,
                mode="preserve",
            ),
            ["0"],
        )
        self.assertEqual(
            _seed_search_values(
                rows,
                duplicate_index=1,
                strategy_group_id="1003",
                original=original,
                mode="observed",
            ),
            ["2", "0"],
        )
        self.assertEqual(
            _seed_search_values(
                rows,
                duplicate_index=1,
                strategy_group_id="1003",
                original=original,
                mode="joint",
            ),
            [str(value) for value in range(32)],
        )

    def test_observed_seed_mode_enumerates_every_observed_seed(self) -> None:
        rows, _warnings = parse_consumption_history(
            _history(
                [
                    ["source", f"7|{seed}|100|4", 1, 1003, "", 100, 500]
                    for seed in range(20)
                ]
            )
        )

        seeds = _seed_search_values(
            rows,
            duplicate_index=1,
            strategy_group_id="1003",
            original=DupParam(7, "31", "100", 4),
            mode="observed",
        )

        self.assertEqual(set(seeds), {*(str(value) for value in range(20)), "31"})
        self.assertEqual(len(seeds), 21)

    def test_duplicate_config_rows_use_complete_history_vector_then_safe_group(self) -> None:
        rows, _warnings = parse_consumption_history(
            _history(
                [
                    ["source", f"{8 - min(index, 3)}|0|100|4", index, 1003, "", 100, 500]
                    for index in range(1, 11)
                ]
            )
        )
        matching_vector = [f"{8 - min(index, 3)}|0|100|4" for index in range(1, 11)]
        candidates = [
            {
                "source_template_id": "source-clean",
                "config_source_template_id": "source",
                "output_template_id": "output",
                "before_dup": ["9|0|100|5"] * 10,
                "level_ids": ["random-40"],
                "random_seed_group_id": 40,
            },
            {
                "source_template_id": "source-clean",
                "config_source_template_id": "source",
                "output_template_id": "output",
                "before_dup": matching_vector,
                "level_ids": ["legacy"],
                "random_seed_group_id": 0,
            },
            {
                "source_template_id": "source-clean",
                "config_source_template_id": "source",
                "output_template_id": "output",
                "before_dup": matching_vector,
                "level_ids": ["random-41"],
                "random_seed_group_id": 41,
            },
        ]

        selected, warnings = _select_one_baseline_per_output(candidates, rows)

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["level_ids"], ["legacy"])
        self.assertIn("history_complete_dup_match", warnings[0])

    def test_read_only_calibration_returns_ten_reviewable_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            strategy_path = root / "csv" / "config_level_strategy_group.csv"
            strategy_path.parent.mkdir(parents=True)
            for directory in (root / "csv", root / "other"):
                directory.mkdir(parents=True, exist_ok=True)
                with (directory / "config_level.csv").open(
                    "w", encoding="utf-8", newline=""
                ) as stream:
                    writer = csv.writer(stream)
                    writer.writerow(
                        ["LevelId", "TemplateId", *(f"DupPara{i}" for i in range(1, 11))]
                    )
                    writer.writerow(["string"] * 12)
                    writer.writerow(["说明"] * 12)
                with (directory / "config_level_list.csv").open(
                    "w", encoding="utf-8", newline=""
                ) as stream:
                    writer = csv.writer(stream)
                    writer.writerow(["LevelIndex", "LevelId"])
                    writer.writerow(["int", "string"])
                    writer.writerow(["说明", "说明"])
            with strategy_path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(
                    ["StrategyGroupId", *(f"InstanceId{i}" for i in range(1, 11))]
                )
                writer.writerow(["int", *(["array"] * 10)])
                writer.writerow(["说明", *(["说明"] * 10)])
                writer.writerow(
                    [
                        "1003",
                        "0|0|0",
                        "0|0|0",
                        "1|1|1;4",
                        "1|1|2;3;1;4",
                        "1|2|3;1;2;4",
                        "1|2|3;2;1;4",
                        "1|2|5;1;2;4",
                        "1|2|5;2;1;4",
                        "1|1|8;7;6;5;3;2;4;1",
                        "1|1|7;6;5;3;2;4;1",
                    ]
                )
            with (root / "other" / "config_level_strategy_group.csv").open(
                "w", encoding="utf-8", newline=""
            ) as stream:
                writer = csv.writer(stream)
                writer.writerow(
                    ["StrategyGroupId", *(f"InstanceId{i}" for i in range(1, 11))]
                )
                writer.writerow(["int", *(["array"] * 10)])
                writer.writerow(["说明", *(["说明"] * 10)])
                writer.writerow(["1003", *(["conflict"] * 10)])
            (root / "csv" / "config_client_level_tile.csv").write_text(
                "Id,StartLevel,TileList1,TileList2\n"
                "int,int,array,array\n"
                "id,起点,列表1,列表2\n"
                "1,0,,1000|1001|1002|1003|1004|1005|1006\n",
                encoding="utf-8",
            )
            (root / "csv" / "config_client_tile.csv").write_text(
                "TileId,Des,IconType,Board,Icon\n"
                "int,string,int,string,string\n"
                "Tile id,备注,类型,底板,图案\n"
                "1000,a,0,bg,a\n1001,b,0,bg,b\n1002,c,0,bg,c\n"
                "1003,d,0,bg,d\n1004,e,0,bg,e\n1005,f,0,bg,f\n"
                "1006,g,0,bg,g\n",
                encoding="utf-8",
            )
            history_rows: list[list[object]] = []
            for duplicate in range(1, 11):
                history_rows.extend(
                    [
                        ["online-source", "7|0|100|4", duplicate, 1003, "s1", 1200, 500 - duplicate, 200, "newv1source"],
                        ["online-source", "9|0|100|5", duplicate, 1003, "s1", 900, 900 - duplicate, 205, "random-40"],
                        ["neighbour", "5|0|100|4", duplicate, 1003, "s1", 900, 470 - duplicate, 230, "neighbour-level"],
                        ["neighbour2", "6|0|100|4", duplicate, 1003, "s1", 800, 485 - duplicate, 240, "neighbour2-level"],
                        ["seed-test", "5|2|100|4", duplicate, 1003, "s1", 700, 468 - duplicate, 250, "seed-test-level"],
                    ]
                )
            data = _history(
                history_rows, header=[*HEADER, "level_index", "level_id"]
            )
            originals = ["7|0|100|4"] * 10
            baselines = ["5|0|100|4"] * 10
            baseline_result = {
                "rows": [
                    {
                        "status": "ready",
                        "source_template_id": "source",
                        "config_source_template_id": "online-source",
                        "config_source_match": "trailing_clean_alias",
                        "output_template_id": "output",
                        "level_ids": ["newv1source"],
                        "online_positions": [16_161],
                        "before_dup": originals,
                        "recommended_dup": baselines,
                        "random_seed_group_id": 0,
                    },
                    {
                        "status": "ready",
                        "source_template_id": "source",
                        "config_source_template_id": "online-source",
                        "config_source_match": "trailing_clean_alias",
                        "output_template_id": "output",
                        "level_ids": ["random-40"],
                        "online_positions": [16_200],
                        "before_dup": ["9|0|100|5"] * 10,
                        "recommended_dup": ["7|0|100|4"] * 10,
                        "random_seed_group_id": 40,
                    },
                    {
                        "status": "ready",
                        "source_template_id": "source",
                        "config_source_template_id": "online-source",
                        "config_source_match": "trailing_clean_alias",
                        "output_template_id": "output",
                        "level_ids": ["random-41"],
                        "online_positions": [16_300],
                        "before_dup": ["10|0|100|6"] * 10,
                        "recommended_dup": ["8|0|100|5"] * 10,
                        "random_seed_group_id": 41,
                    },
                ],
                "warnings": [],
                "source_tables": {"access": "read_only"},
            }
            level = _level()
            online_source_level = replace(
                level,
                data=b"online-source-with-c-n",
                tiles=tuple(
                    replace(
                        tile,
                        tile_id=-12 if tile.index in {0, 2, 4} else tile.tile_id,
                    )
                    for tile in level.tiles
                ),
            )
            pair_calls: list[dict[str, object]] = []

            def pair(*_args, **kwargs):
                pair_calls.append(kwargs)
                duplicate = kwargs["candidate_duplicate"]
                source = kwargs.get("source_report") or {
                    "duplicate_index": kwargs["duplicate_index"],
                    "parameters": {
                        "a": kwargs["source_duplicate"].a,
                        "seed": kwargs["source_duplicate"].seed,
                        "luck": kwargs["source_duplicate"].luck,
                        "b": kwargs["source_duplicate"].b,
                    },
                }
                return {
                    "source": source,
                    "difficulty_distance": {
                        "primary_distance": abs(7 - duplicate.a) / 10
                    },
                    "legacy_score_difference": abs(7 - duplicate.a),
                    "evidence": {"materialization_fidelity": "synthetic"},
                }

            profile = Profile(
                "p", "test", str(root), str(root), str(root / "out"), str(root)
            )
            manifest = {
                "source_template_id": "source",
                "output_template_id": "output",
                "signature": "s5",
                "source_sha256": level.sha256,
                "output_sha256": "output-sha",
                "replacements": [],
            }
            with patch(
                "services.consumption_calibration_service.build_parameter_recommendations",
                return_value=baseline_result,
            ), patch(
                "services.consumption_calibration_service.load_template_id",
                side_effect=lambda _profile, template_id: SimpleNamespace(
                    level=(
                        online_source_level
                        if template_id == "online-source"
                        else level
                    )
                ),
            ), patch(
                "services.consumption_calibration_service.compare_duplicate_difficulty_pair",
                side_effect=pair,
            ):
                result = calibrate_consumption_parameters(
                    profile,
                    [manifest],
                    config_set_id="csv",
                    level_start=200,
                    level_end=300,
                    history_data=data,
                    history_filename="history.csv",
                    search_observed_seeds=True,
                )
                focused = calibrate_consumption_parameters(
                    profile,
                    [manifest],
                    config_set_id="csv",
                    level_start=200,
                    level_end=300,
                    history_data=data,
                    history_filename="history.csv",
                    focus_output_template_id="output",
                    first_duplicate_max=6,
                    simulate_first_duplicate_seeds=True,
                )
                progress: list[dict[str, object]] = []
                joint = calibrate_consumption_parameters(
                    profile,
                    [manifest],
                    config_set_id="csv",
                    level_start=200,
                    level_end=300,
                    history_data=data,
                    history_filename="history.csv",
                    optimize_all_duplicate_seeds=True,
                    progress_callback=progress.append,
                )
                joint_without_progress = calibrate_consumption_parameters(
                    profile,
                    [manifest],
                    config_set_id="csv",
                    level_start=200,
                    level_end=300,
                    history_data=data,
                    history_filename="history.csv",
                    optimize_all_duplicate_seeds=True,
                )
                with patch(
                    "services.consumption_calibration_service._load_structure_correction_model",
                    return_value={
                        "model_id": "forced-outside-tolerance",
                        "scope": {
                            "level_index_min": 200,
                            "level_index_max": 300,
                            "signatures": ["s5"],
                        },
                        "feature_order": [],
                        "means": {},
                        "scales": {},
                        "coefficients": {},
                        "intercept": 0.7,
                        "log_ratio_clip": [-0.75, 0.75],
                    },
                ):
                    missed = calibrate_consumption_parameters(
                        profile,
                        [manifest],
                        config_set_id="csv",
                        level_start=200,
                        level_end=300,
                        history_data=data,
                        history_filename="history.csv",
                    )

            self.assertEqual(
                result["claim_level"], "target_anchored_directional_beta"
            )
            self.assertEqual(
                result["calibration_objective"]["duplicate_weights"],
                {"1": 4.0, "2": 4.0, "3": 2.0, "4": 2.0, **{str(index): 1.0 for index in range(5, 11)}},
            )
            self.assertTrue(
                result["calibration_objective"][
                    "complete_path_returned_when_tolerance_missed"
                ]
            )
            self.assertTrue(
                result["calibration_objective"]["no_new_parameter_rebound"]
            )
            self.assertFalse(result["seed_causal_identification"])
            self.assertNotIn("client_strategy_1003_replay", result)
            self.assertEqual(
                result["structure_strategy_replay"]["status"],
                "local_sampled_replay",
            )
            self.assertTrue(
                result["structure_strategy_replay"][
                    "applied_before_ab_materialization"
                ]
            )
            self.assertIn(
                "backfill",
                result["structure_strategy_replay"]["execution_order"],
            )
            self.assertFalse(result["config_mutation"])
            self.assertEqual(result["writes_performed"], [])
            self.assertEqual(result["coverage"]["covered_duplicate_count"], 10)
            self.assertEqual(result["coverage"]["ignored_row_count"], 0)
            self.assertEqual(result["strategy_group_config"]["status"], "available")
            self.assertEqual(
                result["strategy_group_config"]["source_paths"],
                ["csv/config_level_strategy_group.csv"],
            )
            (row,) = result["rows"]
            self.assertTrue(
                any("history_level_id" in warning for warning in result["warnings"])
            )
            self.assertEqual(
                row["baseline_resolution"]["status"],
                "ambiguous_deterministic",
            )
            self.assertEqual(
                row["baseline_resolution"]["target_history_scope"],
                "selected_baseline_level_ids",
            )
            self.assertEqual(row["config_source_template_id"], "online-source")
            self.assertEqual(row["config_source_match"], "trailing_clean_alias")
            self.assertEqual(
                row["duplicates"][0]["target_consumption"]["value"], 499.0
            )
            self.assertEqual(len(row["duplicates"]), 10)
            self.assertEqual(
                row["source_fingerprints"]["manifest_output_sha256"],
                "output-sha",
            )
            self.assertEqual(
                row["source_fingerprints"]["online_source_template_id"],
                "online-source",
            )
            self.assertEqual(
                row["source_simulation_reference"]["status"],
                "online_source_paired",
            )
            self.assertEqual(row["seed_simulation_level_index"], 200)
            self.assertEqual(
                row["seed_simulation_level_index_source"],
                "history_resolved_source_alias",
            )
            self.assertTrue(row["gradient"]["no_new_parameter_rebound"])
            self.assertTrue(
                any(
                    call["duplicate_index"] == 1
                    and call["materialization_evidence"] is not None
                    and call["materialization_evidence"]["source"]
                    ["structure_label_count"] == 3
                    and call["materialization_evidence"]["source_structure_strategy"]
                    ["status"] == "not_applicable"
                    for call in pair_calls
                ),
                msg=str(row["seed_simulation_fallback_reasons"]),
            )
            self.assertTrue(
                any(
                    call["duplicate_index"] == 3
                    and call["materialization_evidence"] is not None
                    and call["materialization_evidence"]["source_structure_strategy"]
                    ["status"] == "replayed"
                    and call["materialization_evidence"]["source_structure_strategy"]
                    ["candidate_order"] == [1, 4]
                    for call in pair_calls
                )
            )
            for duplicate in row["duplicates"]:
                recommended = DupParam(*(
                    lambda parts: (int(parts[0]), parts[1], parts[2], int(parts[3]))
                )(duplicate["recommended_dup"].split("|")))
                fidelity_baseline = DupParam(*(
                    lambda parts: (int(parts[0]), parts[1], parts[2], int(parts[3]))
                )(duplicate["fidelity_baseline_dup"].split("|")))
                self.assertGreaterEqual(recommended.a, fidelity_baseline.a)
                self.assertGreaterEqual(recommended.b, fidelity_baseline.b)
                self.assertEqual(duplicate["recommended_dup"].split("|")[1], "0")
                self.assertTrue(duplicate["luck_preserved"])
                self.assertGreaterEqual(len(duplicate["alternatives"]), 2)
                self.assertIn("historical_association", duplicate)
                self.assertNotIn("association_estimate", duplicate)
                self.assertEqual(
                    duplicate["historical_association"]["ranking_role"],
                    "context_only",
                )
                self.assertTrue(duplicate["recommendation"]["is_best_fit"])
                self.assertFalse(duplicate["recommendation"]["is_recommended"])
                self.assertIn(
                    "baseline_config_resolution_ambiguous",
                    duplicate["recommendation"]["simulation_evidence_reasons"],
                )
                self.assertFalse(duplicate["recommendation"]["export_blocked"])
                self.assertEqual(
                    duplicate["search_evidence"]["enumerated_seed_count"], 2
                )
                self.assertEqual(
                    duplicate["search_evidence"]["seed_search_mode"], "observed"
                )
                self.assertLessEqual(
                    duplicate["search_evidence"][
                        "full_simulation_candidate_count"
                    ],
                    24,
                )
                self.assertEqual(
                    duplicate["seed_simulation"]["mode"],
                    "current_local_config_simulation",
                )
                self.assertIsNone(duplicate["seed_simulation"]["fallback_reason"])
                self.assertTrue(
                    all("seed_simulation" in item for item in duplicate["alternatives"])
                )
                self.assertTrue(
                    any(item["seed_experimental"] for item in duplicate["alternatives"])
                )
            self.assertTrue(
                all(
                    item["recommendation"]["relative_gap"] is None
                    and item["expected_consumption"]["value"] is None
                    and not item["recommendation"]["is_recommended"]
                    for item in row["duplicates"]
                )
            )
            self.assertFalse(
                any(
                    "偏差超过 25%" in warning
                    for warning in row["gradient"]["warnings"]
                )
            )
            self.assertEqual(
                row["duplicates"][2]["configured_strategy_rule"]["raw"],
                "1|1|1;4",
            )
            self.assertEqual(
                row["duplicates"][0]["observed_effective_strategies"],
                ["s1"],
            )
            selected_parameters = [
                DupParam(
                    int(parts[0]), parts[1], parts[2], int(parts[3])
                )
                for parts in (
                    duplicate["recommended_dup"].split("|")
                    for duplicate in row["duplicates"]
                )
            ]
            self.assertTrue(
                all(
                    previous.a >= current.a and previous.b >= current.b
                    for previous, current in zip(
                        selected_parameters, selected_parameters[1:]
                    )
                )
            )
            (focused_row,) = focused["rows"]
            first = DupParam(*(
                lambda parts: (int(parts[0]), parts[1], parts[2], int(parts[3]))
            )(focused_row["duplicates"][0]["recommended_dup"].split("|")))
            second = DupParam(*(
                lambda parts: (int(parts[0]), parts[1], parts[2], int(parts[3]))
            )(focused_row["duplicates"][1]["recommended_dup"].split("|")))
            self.assertEqual(max(first.a, first.b), 6)
            self.assertIsInstance(max(second.a, second.b), int)
            self.assertEqual(
                focused_row["first_duplicate_adjustment"]["seed_search"],
                "0–31",
            )
            self.assertIn(
                first.seed,
                {str(value) for value in range(32)},
            )
            self.assertIn(
                "expected_consumption",
                focused_row["duplicates"][0],
            )
            self.assertEqual(joint["seed_search"]["mode"], "joint_ab_seed_0_31")
            self.assertEqual(joint, joint_without_progress)
            self.assertEqual(progress[-1]["phase"], "completed")
            self.assertEqual(progress[-1]["template_total"], 1)
            self.assertEqual(progress[-1]["template_completed"], 1)
            self.assertEqual(progress[-1]["duplicate_completed"], 10)
            self.assertEqual(progress[-1]["duplicate_total_all"], 10)
            full_simulation_progress = [
                item for item in progress if item["phase"] == "full_simulation"
            ]
            self.assertTrue(full_simulation_progress)
            self.assertEqual(
                {item["duplicate_index"] for item in full_simulation_progress},
                set(range(1, 11)),
            )
            self.assertTrue(
                all(
                    0 <= item["candidate_completed"] <= item["candidate_total"]
                    and item["current_template"] == "output"
                    for item in full_simulation_progress
                )
            )
            self.assertTrue(joint["optimize_all_duplicate_seeds"])
            (joint_row,) = joint["rows"]
            self.assertTrue(joint_row["optimize_all_duplicate_seeds"])
            self.assertTrue(
                all(
                    duplicate["seed_simulation"]["structure_strategy"]["status"]
                    in {"not_applicable", "replayed"}
                    for duplicate in joint_row["duplicates"]
                )
            )
            (missed_row,) = missed["rows"]
            self.assertEqual(len(missed_row["duplicates"]), 10)
            self.assertEqual(missed["seed_search"]["mode"], "preserve_original_seed")
            self.assertFalse(missed["optimize_all_duplicate_seeds"])
            self.assertTrue(
                all(
                    item["search_evidence"]["enumerated_seed_count"] == 1
                    and item["search_evidence"]["seed_search_mode"] == "preserve"
                    for item in missed_row["duplicates"]
                )
            )
            self.assertEqual(missed_row["recommendation_summary"]["review_count"], 10)
            self.assertTrue(
                all(
                    item["recommendation"]["is_best_fit"]
                    and not item["recommendation"]["export_blocked"]
                    for item in missed_row["duplicates"]
                )
            )

    def test_structured_random_seed_group_is_forwarded_then_falls_back_unsupported(self) -> None:
        data = _history(
            [
                ["source", "7|0|100|4", duplicate, 1003, "", 1000, 500]
                for duplicate in range(1, 11)
            ]
        )
        baseline_result = {
            "rows": [
                {
                    "status": "ready",
                    "source_template_id": "source",
                    "output_template_id": "output",
                    "level_ids": ["level"],
                    "online_positions": [200],
                    "before_dup": ["7|0|100|4"] * 10,
                    "recommended_dup": ["7|0|100|4"] * 10,
                    "random_seed_group_id": 37,
                }
            ],
            "warnings": [],
            "source_tables": {"access": "read_only"},
        }
        configured_strategy = {
            "status": "available",
            "rules": [None] * 10,
            "source_paths": [],
            "warnings": [],
        }
        seed_context = {
            "level_tile_rows": (),
            "tile_catalog": object(),
            "source_paths": [
                "csv/config_client_level_tile.csv",
                "csv/config_client_tile.csv",
            ],
        }
        level = _level()

        def pair(*_args, **kwargs):
            return {
                "source": kwargs.get("source_report") or {},
                "difficulty_distance": {"primary_distance": 0.0},
                "legacy_score_difference": 0.0,
                "evidence": {"materialization_fidelity": "synthetic"},
            }

        profile = Profile("p", "test", None, None, "/tmp/out", "/tmp/config")
        manifest = {
            "source_template_id": "source",
            "output_template_id": "output",
            "source_sha256": level.sha256,
            "output_sha256": "output-sha",
            "replacements": [],
        }
        with patch(
            "services.consumption_calibration_service.build_parameter_recommendations",
            return_value=baseline_result,
        ), patch(
            "services.consumption_calibration_service._strategy_group_config",
            return_value=configured_strategy,
        ), patch(
            "services.consumption_calibration_service._legacy_seed_config_context",
            return_value=(seed_context, None),
        ), patch(
            "services.consumption_calibration_service.load_template_id",
            return_value=SimpleNamespace(level=level),
        ), patch(
            "services.consumption_calibration_service._whole_board_labels",
            side_effect=ValueError(
                "structured randomSeed_groupID > 0 is not supported"
            ),
        ) as legacy_materialization, patch(
            "services.consumption_calibration_service.compare_duplicate_difficulty_pair",
            side_effect=pair,
        ):
            result = calibrate_consumption_parameters(
                profile,
                [manifest],
                config_set_id="csv",
                level_start=200,
                level_end=300,
                history_data=data,
                history_filename="history.csv",
            )

        self.assertEqual(legacy_materialization.call_count, 10)
        self.assertTrue(
            all(
                call.kwargs["random_seed_group_id"] == 37
                for call in legacy_materialization.call_args_list
            )
        )
        self.assertEqual(result["seed_simulation_config"]["mode"], "synthetic")
        self.assertEqual(result["seed_simulation_config"]["random_seed_group_id"], 37)
        (row,) = result["rows"]
        self.assertEqual(row["random_seed_group_id"], 37)
        for duplicate in row["duplicates"]:
            simulation = duplicate["seed_simulation"]
            self.assertEqual(simulation["mode"], "synthetic")
            self.assertEqual(simulation["status"], "unsupported")
            self.assertEqual(
                simulation["fallback_code"],
                "structured_random_seed_group_unsupported",
            )
            self.assertEqual(simulation["random_seed_group_id"], 37)


if __name__ == "__main__":
    unittest.main()
