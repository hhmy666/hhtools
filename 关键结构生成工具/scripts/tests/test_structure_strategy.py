from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.structure_strategy import (  # noqa: E402
    parse_strategy_rule,
    replay_structure_strategy,
)


class StructureStrategyReplayTests(unittest.TestCase):
    def test_parses_disabled_and_random_rules(self) -> None:
        self.assertEqual(parse_strategy_rule("0|0|0").strategy_ids, ())
        parsed = parse_strategy_rule("2|2|3;2;1;4")
        self.assertEqual(parsed.mode, 2)
        self.assertEqual(parsed.requested_count, 2)
        self.assertEqual(parsed.strategy_ids, (3, 2, 1, 4))

    def test_strategy_one_replaces_type_from_second_structure_layer(self) -> None:
        output, evidence = replay_structure_strategy(
            ("S:C", "S:C", "S:C", "S:D", "S:D", "S:D", "A"),
            (3, 3, 3, 2, 2, 2, 1),
            "1|1|1;4",
            strategy_group_id="1003",
            duplicate_index=3,
            duplicate_seed="0",
            board_role="candidate",
        )
        self.assertEqual(output.count("A"), 4)
        self.assertNotIn("S:D", output)
        self.assertEqual(evidence["effective_strategies"], [1])

    def test_failed_strategy_uses_later_configured_strategy_as_fill(self) -> None:
        output, evidence = replay_structure_strategy(
            ("S:C", "S:C", "S:C", "A"),
            (1, 1, 1, 0),
            "1|1|1;7",
            strategy_group_id="test",
            duplicate_index=4,
            duplicate_seed="0",
            board_role="candidate",
        )
        self.assertEqual(output, ("B", "B", "B", "A"))
        self.assertEqual(evidence["failed_strategies"], [1])
        self.assertEqual(evidence["effective_strategies"], [7])

    def test_strategy_four_converts_bottom_group_to_existing_structure(self) -> None:
        output, evidence = replay_structure_strategy(
            ("S:C", "A", "A", "A", "B"),
            (4, 1, 1, 1, 0),
            "1|1|4",
            strategy_group_id="test",
            duplicate_index=4,
            duplicate_seed="2",
            board_role="candidate",
        )
        self.assertEqual(output.count("S:C"), 4)
        self.assertEqual(evidence["effective_strategies"], [4])

    def test_strategy_four_never_combines_tiles_across_bottom_layers(self) -> None:
        original = ("S:C", "A", "A", "B", "A", "A")
        output, evidence = replay_structure_strategy(
            original,
            (4, 1, 1, 1, 0, 0),
            "1|1|4",
            strategy_group_id="test",
            duplicate_index=4,
            duplicate_seed="2",
            board_role="candidate",
        )
        self.assertEqual(output, original)
        self.assertEqual(evidence["effective_strategies"], [])
        self.assertEqual(evidence["failed_strategies"], [4])

    def test_strategy_four_falls_from_second_bottom_to_bottom_layer(self) -> None:
        output, evidence = replay_structure_strategy(
            ("S:C", "A", "A", "B", "A", "A", "A"),
            (4, 1, 1, 1, 0, 0, 0),
            "1|1|4",
            strategy_group_id="test",
            duplicate_index=4,
            duplicate_seed="2",
            board_role="candidate",
        )
        self.assertEqual(output[:4], ("S:C", "A", "A", "B"))
        self.assertEqual(output[4:], ("S:C", "S:C", "S:C"))
        self.assertEqual(evidence["effective_strategies"], [4])
        self.assertEqual(evidence["attempts"][0]["selected_layer"], 0)

    def test_random_mode_is_reproducible_and_labeled_as_local_sample(self) -> None:
        args = (
            ("S:C", "S:C", "S:C", "S:D", "S:D", "S:D", "A", "A", "A"),
            (3, 3, 3, 2, 2, 2, 1, 1, 1),
            "2|2|2;3;4",
        )
        first = replay_structure_strategy(
            *args,
            strategy_group_id="1001",
            duplicate_index=6,
            duplicate_seed="9",
            board_role="candidate",
        )
        second = replay_structure_strategy(
            *args,
            strategy_group_id="1001",
            duplicate_index=6,
            duplicate_seed="9",
            board_role="candidate",
        )
        self.assertEqual(first, second)
        self.assertEqual(
            first[1]["random_semantics"],
            "stable_local_sample_not_exact_client_random_stream",
        )


if __name__ == "__main__":
    unittest.main()
