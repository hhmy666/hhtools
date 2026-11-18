import unittest
from random import Random
from types import SimpleNamespace

import sources  # Resolves the shared client template/geometry modules.
from simulation import Behavior, OldLuck, PlayerBoard, State, _finish_labels


class FixedDraw:
    def __init__(self, value):
        self.value = value

    def get_random(self, maximum):
        if maximum != 100:
            raise AssertionError(maximum)
        return self.value

    def random(self):
        return self.value

    def choices(self, options, *, weights):
        return (next(option for option, weight in zip(options, weights) if weight),)


def state_for(labels, blockers, dependents, towers=(), memberships=None):
    board = PlayerBoard(
        list(labels), blockers, dependents,
        tuple((cover,) * 4 for cover in blockers),
        tuple((i * 2, 0) for i in range(len(labels))),
    )
    return State(board, towers, memberships or (None,) * len(labels), 7)


class SimulationTest(unittest.TestCase):
    def test_structure_groups_share_a_pattern_and_avoid_dealt_patterns(self):
        slots = (1000, -12, -13, 1000, -12, -13, 1000, -12, -13)
        board = SimpleNamespace(template_id="pattern_test", tiles=slots)
        catalog = SimpleNamespace(normal_tile_ids=(1000, 1001, 1002))
        self.assertEqual(_finish_labels(board, slots, catalog), [1000, 1001, 1002] * 3)
        self.assertEqual(
            _finish_labels(board, slots, SimpleNamespace(normal_tile_ids=(1000,))),
            [1000] * 9,
        )

    def test_tower_help_uses_pre_click_bag_and_swaps_hidden_identities(self):
        state = state_for(
            (1000, 1001, 1002, 1003, 1003, 1003, 1002), (0, 1, 0, 4, 0, 0, 0),
            ((1,), (), (3,), (), (), (), ()), ((0, 1), (2, 3)),
            (0, 0, 1, 1, None, None, None),
        )
        luck = OldLuck(FixedDraw(29), (200, 100, 80, 50), (10,) * 7, 100, 10, True, 1)
        self.assertTrue(luck.helps(0, 0))
        self.assertFalse(luck.helps(50, 0))
        state.play(0, luck)
        self.assertEqual(state.board.labels[:4], [1000, 1003, 1002, 1001])
        self.assertEqual(state.slot_for_identity[:4], [0, 3, 2, 1])
        self.assertEqual(state.bag_order, [0])
        self.assertIn(1, state.available)

    def test_forced_removal_cannot_reintroduce_hidden_tile(self):
        state = state_for((1000, 1001, 1000), (0, 1, 0), ((1,), (), ()))
        state._remove_from_board(1)
        self.assertEqual(state.available, {0, 2})
        state.play(0, None)
        self.assertEqual(state.available, {2})
        self.assertEqual(state.exposed_cells, [0, 0, 4])

    def test_undo_prefers_last_click_still_in_bag(self):
        state = state_for((1000, 1001, 1000), (0, 0, 0), ((), (), ()))
        for position in range(3):
            state.play(position, None)
        self.assertEqual(state.bag_order, [0, 2, 1])
        self.assertTrue(state.apply_prop(502, Random(0)))
        self.assertEqual(state.bag_order, [0, 1])
        self.assertEqual(state.stage_tops(), [2])
        self.assertEqual(state.bag[1000], 1)

    def test_unmodeled_prop_activity_does_not_increase_supported_prop_rate(self):
        behavior = Behavior({"prop_trigger": [
            {"bag": 6, "surface": 0, "retry": 0, "uses": 80, "opportunities": 100,
             "asset_503": 20, "asset_504": 60},
        ]})
        state = SimpleNamespace(size=6, available={0}, stage_tops=lambda: ())
        self.assertIsNone(behavior.prop(state, 0, FixedDraw(.55)))
        self.assertEqual(behavior.prop(state, 0, FixedDraw(.1)), 503)


if __name__ == "__main__":
    unittest.main()
