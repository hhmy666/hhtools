from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from services.parameter_recommendation_service import DupParam
from services.player_consumption_estimate import PlayerConsumptionEstimator


class PlayerConsumptionEstimateTests(TestCase):
    def test_uses_same_player_sample_and_raw_online_anchor(self):
        rollout = Mock(side_effect=[
            {"player_styles": 1000, "mean_prop_uses": 1.0},
            {"player_styles": 1000, "mean_prop_uses": 0.8},
        ])
        estimator = PlayerConsumptionEstimator(
            rollout, lambda metrics, model: metrics["mean_prop_uses"] * 1000,
            {"level_range": [300, 600]}, SimpleNamespace,
            lambda entry, config: (entry, None), Path("."),
        )
        original = DupParam(12, "1", "100", 4)
        candidate = DupParam(8, "5", "100", 4)
        board = SimpleNamespace(template_id="nthr0062")
        prediction = estimator.predict(
            board, board, ("T:1000",) * 3, ("S:C",) * 3, original, candidate,
            level_index=579, level_id="list579r1v3nthr0062", duplicate=1,
            observed_cost=3000,
        )
        self.assertEqual(prediction["value"], 2400)
        self.assertEqual(prediction["player_rollout"]["player_styles"], 1000)
        source, output = rollout.call_args_list
        self.assertEqual(source.args[3:5], output.args[3:5])
        self.assertEqual(source.kwargs["materialized_labels"], (1000,) * 3)
        self.assertEqual(output.kwargs["materialized_labels"], (-12,) * 3)
        self.assertEqual(output.args[2].seed, 5)

    def test_rejects_insufficient_distinct_player_styles(self):
        estimator = PlayerConsumptionEstimator(
            Mock(return_value={"player_styles": 999}), Mock(),
            {"level_range": [300, 600]}, SimpleNamespace,
            lambda entry, config: (entry, None), Path("."),
        )
        board = SimpleNamespace(template_id="nthr0062")
        parameter = DupParam(12, "1", "100", 4)
        self.assertIsNone(estimator.predict(
            board, board, ("T:1000",) * 3, ("S:C",) * 3, parameter, parameter,
            level_index=579, level_id=None, duplicate=1, observed_cost=3000,
        ))
