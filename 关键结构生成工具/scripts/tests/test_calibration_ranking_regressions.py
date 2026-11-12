from __future__ import annotations

import csv
import io
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


def _candidate(a=9, b=5, seed="1", *, primary=1.0, secondary=0.4,
               reference=None, baseline=None, score=None, weight=1.0):
    reference = reference or DupParam(9, "1", "100", 9)
    return {
        "parameter": DupParam(a, seed, "100", b),
        "original": DupParam(12, "1", "100", 6),
        "ranking_reference": reference,
        "baseline": baseline or DupParam(9, "1", "100", 5),
        "node_score": primary * 0.25 if score is None else score,
        "difficulty": {"distance": primary, "stable_tiebreak": secondary},
        "objective_weight": weight,
    }


class CalibrationRankingRegressions(unittest.TestCase):
    def _select(self, choices):
        select = getattr(service, "_select_monotonic_candidate_path", None)
        self.assertTrue(callable(select), "DP must compare the complete ranking cost")
        indices = select(choices)
        self.assertIsNotNone(indices)
        return [pool[index]["parameter"].render()
                for pool, index in zip(choices, indices)]

    def test_secondary_simulation_precedes_mechanical_baseline_distance(self):
        # Equal primary evidence: B9 is closer in the existing secondary metric.
        self.assertEqual(self._select([[
            _candidate(b=5, secondary=0.4),
            _candidate(b=9, secondary=0.2),
        ]]), ["9|1|100|9"])

    def test_main_prediction_cost_still_precedes_secondary_simulation(self):
        self.assertEqual(self._select([[
            _candidate(b=5, secondary=0.8, score=0.1),
            _candidate(b=9, secondary=0.0, score=0.2),
        ]]), ["9|1|100|5"])

    def test_secondary_reuses_first_duplicate_priority_over_later_tradeoff(self):
        # Primary path costs tie. Existing first-duplicate weight 4 makes
        # 4*.1 + .9 preferable to 4*.3 + .4; unweighted sums choose wrongly.
        self.assertEqual(self._select([[
            _candidate(b=9, secondary=0.1, score=1.0, weight=4.0),
            _candidate(b=5, secondary=0.3, score=1.0, weight=4.0),
        ], [
            _candidate(b=9, secondary=0.9, score=0.25),
            _candidate(b=5, secondary=0.4, score=0.25),
        ]]), ["9|1|100|9", "9|1|100|9"])

    def test_exact_path_tie_preserves_processed_reference_seed(self):
        reference = DupParam(9, "3", "100", 9)
        self.assertEqual(self._select([[
            _candidate(seed="1", reference=reference),
            _candidate(seed="3", reference=reference),
        ], [
            _candidate(seed="1", reference=reference),
            _candidate(seed="3", reference=reference),
        ]]), ["9|3|100|5", "9|3|100|5"])

    def test_changed_seed_tie_uses_numeric_order(self):
        self.assertEqual(self._select([[
            _candidate(seed="10"), _candidate(seed="2"),
        ]]), ["9|2|100|5"])

    def test_missing_secondary_is_not_a_zero_distance_advantage(self):
        self.assertEqual(self._select([[
            _candidate(secondary=None), _candidate(seed="2", secondary=0.8),
        ]]), ["9|2|100|5"])

    def test_nonfinite_main_cost_cannot_win(self):
        self.assertEqual(self._select([[
            _candidate(score=float("nan"), secondary=0),
            _candidate(seed="2", score=0.25, secondary=0.8),
        ]]), ["9|2|100|5"])

    def test_invalid_primary_cannot_beat_valid_weighted_primary(self):
        for invalid in (None, float("nan"), float("inf"), True):
            with self.subTest(primary=invalid):
                self.assertEqual(self._select([[
                    _candidate(primary=invalid, secondary=0, score=0.5),
                    _candidate(seed="2", primary=1, secondary=0.8, score=1),
                ]]), ["9|2|100|5"])

    def test_gradient_uses_processed_parameters_without_rewriting_original(self):
        previous = _candidate(b=9, reference=DupParam(9, "1", "100", 9))
        current = _candidate(b=5, reference=DupParam(9, "1", "100", 5))
        self.assertEqual(service._transition_penalty(previous, current), 0.0)
        self.assertEqual(previous["original"].render(), "12|1|100|6")

    def test_missing_evidence_cannot_make_a_rebound_path_reachable(self):
        self.assertEqual(self._select([[
            _candidate(b=5),
            _candidate(b=9, primary=None, secondary=None, score=0.5),
        ], [_candidate(b=7)]]), ["9|1|100|9", "9|1|100|7"])

    def _calibrate_service(self, compare, *, prediction=None):
        level = _level()
        manifest = {
            "source_template_id": "lessThr0010",
            "output_template_id": "aipuzzlelessThr0010s5",
            "source_sha256": level.sha256,
            "source_tile_counts": {"A": 12, "B": 0},
            "signature": "s5", "replacements": [],
        }
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(["level_template_id", "level_dup", "level_dup_para_id",
                         "strategy_group_id", "进入人数", "人均消耗", "level_index"])
        writer.writerows(["hr0010", "12|1|100|6", index, 1003, 100, 500, 334]
                         for index in range(1, 11))

        def baseline(_profile, manifests, **_kwargs):
            source_id = manifests[0]["source_template_id"]
            processed = source_id == "lessThr0010"
            return {"rows": [{
                "status": "ready", "source_template_id": source_id,
                "config_source_template_id": source_id,
                "output_template_id": manifest["output_template_id"],
                "level_ids": [], "online_positions": [334],
                "before_dup": ["9|3|100|9" if processed else "12|1|100|6"] * 10,
                "recommended_dup": ["9|3|100|5" if processed else "12|1|100|4"] * 10,
                "strategy_group_id": "1003", "random_seed_group_id": 0,
            }], "warnings": [], "source_tables": {}}

        with patch.object(service, "build_parameter_recommendations", side_effect=baseline), \
             patch.object(service, "load_template_id", return_value=SimpleNamespace(level=level)), \
             patch.object(service, "_consumption_prediction",
                          side_effect=prediction or service._consumption_prediction), \
             patch.object(service, "compare_duplicate_difficulty_pair", side_effect=compare):
            result = service.calibrate_consumption_parameters(
                Profile("p", "test", None, None, "/tmp/out", "/tmp/config"),
                [manifest], config_set_id="csv", history_data=stream.getvalue().encode(),
                history_filename="history.csv", template_name_prefix="aipuzzlelessT",
            )
        return result["rows"][0]["duplicates"]

    def test_processed_bounds_and_secondary_are_used_by_the_complete_service(self):
        def compare(_level, _replacements, **kwargs):
            candidate = kwargs["candidate_duplicate"]
            return {
                "source": {}, "difficulty_distance": {
                    "primary_distance": 1.0,
                    "stable_tiebreak": (9 - candidate.b) / 10,
                }, "legacy_score_difference": 0, "evidence": {},
            }

        duplicates = self._calibrate_service(compare)
        self.assertEqual([item["recommended_dup"] for item in duplicates],
                         ["9|3|100|9"] * 10)
        self.assertEqual([item["original_dup"] for item in duplicates],
                         ["12|1|100|6"] * 10)
        self.assertEqual(duplicates[0]["target_consumption"]["value"], 500)
        self.assertFalse(any(item["recommendation"]["is_recommended"]
                             for item in duplicates))

    def test_in_scope_secondary_precedes_the_mechanical_baseline_prior(self):
        def compare(_level, _replacements, **kwargs):
            candidate = kwargs["candidate_duplicate"]
            return {
                "source": {}, "difficulty_distance": {
                    "primary_distance": 1.0,
                    "stable_tiebreak": (9 - candidate.b) / 10,
                }, "legacy_score_difference": 0, "evidence": {},
            }

        def prediction(**_kwargs):
            return {"value": 500, "target": 500, "range": [400, 600],
                    "structure_correction": {"applied": True, "in_scope": True}}

        duplicates = self._calibrate_service(compare, prediction=prediction)
        self.assertEqual([item["recommended_dup"] for item in duplicates],
                         ["9|3|100|9"] * 10)

    def test_service_does_not_reward_unavailable_first_two_simulations(self):
        for invalid in (None, float("nan"), float("inf"), True):
            with self.subTest(primary=invalid):
                def compare(_level, _replacements, **kwargs):
                    missing = (kwargs["duplicate_index"] <= 2
                               and kwargs["candidate_duplicate"].b == 5)
                    return {
                        "source": {}, "difficulty_distance": {
                            "primary_distance": invalid if missing else 1.0,
                            "stable_tiebreak": 0 if missing else 0.2,
                        }, "legacy_score_difference": 0, "evidence": {},
                    }
                duplicates = self._calibrate_service(compare)
                self.assertEqual([item["recommended_dup"] for item in duplicates[:2]],
                                 ["9|3|100|6"] * 2)


if __name__ == "__main__":
    unittest.main()
