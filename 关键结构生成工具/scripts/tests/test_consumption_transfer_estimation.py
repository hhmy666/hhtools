from __future__ import annotations

import csv
import io
import json
import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from services import consumption_calibration_service as service
from services.parameter_recommendation_service import DupParam
from services.profile_service import Profile
from scripts.tests.test_difficulty import _level


def _manifest():
    return {
        "signature": "s5", "source_tile_counts": {"A": 60, "B": 42},
        "replacements": [{"tile_index": index, "source_tile_id": -10,
                          "structure_type": chr(ord("C") + index // 3)}
                         for index in range(9)],
        "validation": {"replacement_count": 9},
    }


def _model(feature="delta_b"):
    return {
        "model_id": "transfer-test-v1",
        "scope": {"level_index_min": 200, "level_index_max": 300,
                  "signatures": ["s5"], "structure_count_min": 9,
                  "structure_count_max": 18},
        "feature_order": [feature], "means": {feature: 0},
        "scales": {feature: 1}, "coefficients": {feature: 0.2},
        "intercept": 0, "log_ratio_clip": [-1, 1],
        "prediction_interval_log_offsets": [-0.1, 0.1],
    }


class ConsumptionTransferEstimationTests(unittest.TestCase):
    def _predict(self, *, level=334, **changes):
        values = dict(target=800.0, manifest=_manifest(), original=DupParam(9, "1", "100", 9),
                      candidate=DupParam(9, "1", "100", 8), duplicate_index=1,
                      level_index=level, model=_model(), strategy_group_id="1003")
        values.update(changes)
        return service._consumption_prediction(**values)

    def test_transfer_334_1000_1200_uses_existing_formula_and_scope_labels(self):
        for level in (334, 1000, 1200):
            with self.subTest(level=level):
                result = self._predict(level=level)
                self.assertAlmostEqual(result["value"], 654.9846, places=4)
                self.assertTrue(result["structure_correction"]["applied"])
                self.assertFalse(result["structure_correction"]["in_scope"])
                self.assertEqual(result.get("estimate_mode"), "transfer_estimate")
                self.assertEqual(result["training_scope"]["level_index_max"], 300)
                self.assertIn("outside_level_range", result["transfer_reasons"])
                self.assertEqual(result["interval_kind"], "training_residual_reference")

    def test_training_band_numerical_result_does_not_change(self):
        result = self._predict(level=250)
        self.assertAlmostEqual(result["value"], 654.9846, places=4)
        self.assertTrue(result["structure_correction"]["in_scope"])
        self.assertEqual(result.get("estimate_mode"), "in_scope_model")

    def test_level_feature_is_not_clamped_to_training_boundary(self):
        result = self._predict(level=1000, model=_model("level_index"))
        self.assertAlmostEqual(result["value"], 800 * math.exp(1), places=4)
        self.assertTrue(result["structure_correction"]["bounded"])

    def test_invalid_targets_and_types_remain_unavailable(self):
        for target in (None, True, float("nan"), float("inf"), -1):
            with self.subTest(target=target):
                result = self._predict(target=target)
                self.assertIsNone(result["value"])
                self.assertFalse(result["structure_correction"]["applied"])
        for signature in ("s6", "", True):
            result = self._predict(manifest={**_manifest(), "signature": signature})
            self.assertIsNone(result["value"])
        invalid = _manifest()
        invalid["replacements"][0]["structure_type"] = "Z"
        self.assertIsNone(self._predict(manifest=invalid)["value"])

    def test_missing_or_nonfinite_feature_context_cannot_become_target_fallback(self):
        checked_in = service._load_structure_correction_model()
        self.assertIsNone(self._predict(model=checked_in)["value"])
        model = _model()
        model["coefficients"]["delta_b"] = float("nan")
        result = self._predict(model=model)
        self.assertIsNone(result["value"])
        self.assertFalse(result["structure_correction"]["applied"])

    def test_nonfinite_log_and_overflowing_point_remain_unavailable(self):
        for intercept in (float("inf"), float("nan")):
            model = _model()
            model["intercept"] = intercept
            self.assertIsNone(self._predict(model=model)["value"])
        model = _model()
        model["coefficients"]["delta_b"] = -10
        self.assertIsNone(self._predict(model=model, target=1e308)["value"])

    def test_unavailable_residual_range_does_not_block_valid_point(self):
        model = _model()
        model["prediction_interval_log_offsets"] = [-1000, 1000]
        result = self._predict(model=model)
        self.assertAlmostEqual(result["value"], 654.9846, places=4)
        self.assertEqual(result["range"], [None, None])

    def test_bad_scope_and_structure_count_return_unavailable_without_raising(self):
        cases = []
        model = _model()
        model["scope"]["level_index_max"] = "bad"
        cases.append({"model": model})
        model = _model()
        model["scope"]["structure_count_max"] = "bad"
        cases.append({"model": model})
        manifest = _manifest()
        manifest["validation"]["replacement_count"] = "bad"
        cases.append({"manifest": manifest})
        for changes in cases:
            with self.subTest(changes=changes):
                try:
                    result = self._predict(**changes)
                except (ValueError, TypeError, OverflowError) as exc:
                    self.fail(f"invalid schema escaped as an exception: {exc}")
                self.assertIsNone(result["value"])

    def test_invalid_residual_offsets_do_not_leak_nonfinite_json(self):
        model = _model()
        model["prediction_interval_log_offsets"] = [float("nan"), float("inf")]
        result = self._predict(model=model)
        try:
            json.dumps(result, allow_nan=False)
        except ValueError as exc:
            self.fail(f"nonfinite residual metadata escaped: {exc}")
        self.assertEqual(result["training_residual_log_offsets"], [None, None])

    def test_unavailable_level_or_scope_provenance_remains_json_safe(self):
        model = _model()
        model["scope"]["level_index_max"] = float("nan")
        for changes in ({"level": float("nan")}, {"model": model}):
            result = self._predict(**changes)
            self.assertIsNone(result["value"])
            try:
                json.dumps(result, allow_nan=False)
            except ValueError as exc:
                self.fail(f"invalid provenance escaped as nonfinite JSON: {exc}")

    def test_seed_changes_do_not_change_multiplier_estimate(self):
        first = self._predict()
        changed = self._predict(candidate=DupParam(9, "31", "100", 8))
        self.assertEqual(first["value"], changed["value"])

    def test_candidate_enumeration_starts_at_processed_source_and_original_seed(self):
        candidates = service._candidate_space(DupParam(9, "1", "100", 9),
                                              DupParam(9, "1", "100", 5),
                                              seeds=["0", "1", "10"])
        self.assertEqual(candidates[0].render(), "9|1|100|9")
        self.assertEqual([item.b for item in candidates if item.seed == "1"],
                         [9, 8, 7, 6, 5])

    def test_consumption_gap_precedes_simulation_and_gradient_in_dp(self):
        def candidate(b, estimate, distance):
            return {"parameter": DupParam(9, "1", "100", b),
                    "original": DupParam(12, "1", "100", 6),
                    "ranking_reference": DupParam(9, "1", "100", 9),
                    "baseline": DupParam(9, "1", "100", 5), "objective_weight": 4,
                    "node_score": 4 * abs(estimate - 800) / 800 + distance,
                    "consumption_prediction": {"value": estimate, "target": 800,
                                               "structure_correction": {"applied": True}},
                    "difficulty": {"distance": distance, "stable_tiebreak": distance}}
        pools = [[candidate(9, 900, 0), candidate(5, 800, 1)],
                 [candidate(5, 800, 1)]]
        indices = service._select_monotonic_candidate_path(pools)
        self.assertEqual(indices, [1, 0])

    def test_transfer_status_does_not_claim_training_scope_validation(self):
        prediction = self._predict()
        candidate = {"parameter": DupParam(9, "1", "100", 8),
                     "original": DupParam(9, "1", "100", 9),
                     "association": {}, "node_score": 0.1,
                     "consumption_prediction": prediction}
        serialized = service._serialize_candidate(candidate, {"value": 800})
        self.assertEqual(serialized["recommendation"].get("estimate_mode"), "transfer_estimate")
        self.assertEqual(serialized["recommendation"]["status"], "transfer_estimate_close")
        self.assertFalse(serialized["recommendation"]["empirically_validated_for_scope"])

    def test_processed_parameters_are_prediction_and_board_anchor_not_observed(self):
        level = _level()
        manifest = {**_manifest(), "source_template_id": "lessThr0010",
                    "output_template_id": "aipuzzlelessThr0010s5", "source_sha256": level.sha256}
        data = io.StringIO()
        writer = csv.writer(data)
        writer.writerow(["level_template_id", "level_dup", "level_dup_para_id",
                         "strategy_group_id", "进入人数", "人均消耗", "level_index"])
        writer.writerows(["hr0010", "12|1|100|6", i, 1003, 100, 800, 334]
                         for i in range(1, 11))
        def baseline(_profile, manifests, **_kwargs):
            source = manifests[0]["source_template_id"]
            processed = source == "lessThr0010"
            return {"rows": [{"status": "ready", "source_template_id": source,
                              "config_source_template_id": source,
                              "output_template_id": manifest["output_template_id"],
                              "level_ids": [], "online_positions": [334],
                              "before_dup": ["9|1|100|9" if processed else "12|1|100|6"] * 10,
                              "recommended_dup": ["9|1|100|5" if processed else "12|1|100|4"] * 10,
                              "strategy_group_id": "1003", "random_seed_group_id": 0}],
                    "warnings": [], "source_tables": {}}
        calls = []
        candidate_calls = []
        def pair(*_args, **kwargs):
            calls.append(kwargs["source_duplicate"])
            candidate_calls.append(kwargs["candidate_duplicate"])
            return {"source": {}, "difficulty_distance": {"primary_distance": 1,
                    "stable_tiebreak": 0.5}, "legacy_score_difference": 0, "evidence": {}}
        with patch.object(service, "build_parameter_recommendations", side_effect=baseline), \
             patch.object(service, "load_template_id", return_value=SimpleNamespace(level=level)), \
             patch.object(service, "_load_structure_correction_model", return_value=_model()), \
             patch.object(service, "compare_duplicate_difficulty_pair", side_effect=pair):
            result = service.calibrate_consumption_parameters(
                Profile("p", "test", None, None, "/tmp/out", "/tmp/config"), [manifest],
                config_set_id="csv", history_data=data.getvalue().encode(), history_filename="history.csv")
            automatic = service.calibrate_consumption_parameters(
                Profile("p", "test", None, None, "/tmp/out", "/tmp/config"), [manifest],
                config_set_id="csv", history_data=data.getvalue().encode(), history_filename="history.csv",
                auto_first_duplicate_max=True)
        first = result["rows"][0]["duplicates"][0]
        self.assertEqual(first["original_dup"], "12|1|100|6")
        self.assertEqual(first["recommended_dup"], "9|1|100|9")
        self.assertEqual(first["consumption_anchor"]["kind"], "processed_source_assumed_equal_online")
        self.assertFalse(first["consumption_anchor"]["observed"])
        self.assertEqual(first["consumption_anchor"]["source_dup"], "9|1|100|9")
        self.assertTrue(all((p.a, p.seed, p.b) == (9, "1", 9) for p in calls))
        self.assertEqual((candidate_calls[0].a, candidate_calls[0].seed, candidate_calls[0].b),
                         (9, "1", 9))
        self.assertEqual(first["search_evidence"]["enumeration_policy"],
                         "source_parameters_then_decrements")
        automatic_first = automatic["rows"][0]["duplicates"][0]
        self.assertEqual(automatic["rows"][0]["first_duplicate_adjustment"]["automatic_bounds"]["a_max"], 6)
        self.assertLessEqual(int(automatic_first["recommended_dup"].split('|')[0]), 6)
        self.assertEqual(automatic_first["search_evidence"]["parameter_bounds"]["a"], [4, 6])
        self.assertFalse(result["auto_first_duplicate_max"])
        self.assertTrue(automatic["auto_first_duplicate_max"])


if __name__ == "__main__":
    unittest.main()
