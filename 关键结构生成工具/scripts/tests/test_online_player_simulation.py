import unittest

from puzzle_core.online_player_simulation import (
    FEATURE_COUNT,
    ChoiceObservation,
    PlayerBoard,
    PlayerPolicy,
    fit_policy,
    observe_replay,
    simulate_players,
)


class OnlinePlayerSimulationTest(unittest.TestCase):
    def test_serialized_clicks_must_follow_blocker_and_bag_evidence(self):
        # TileNum 1 is under TileNum 2. An authored structure type is a real
        # matchable label despite the negative number in the template.
        board = PlayerBoard((-12, -12, -12), (0b010, 0, 0), ((), (0,), ()))
        good = [(1, (2,)), (1, (1,)), (1, (3,))]
        self.assertEqual(observe_replay(board, good, [1, 2, 0]).status, "won")
        blocked = observe_replay(board, [(1, (1,))], [1])
        self.assertEqual((blocked.status, len(blocked.evidence)), ("click_not_reproducible", 0))
        mismatch = observe_replay(board, good, [1, 2, 3])
        self.assertEqual((mismatch.status, len(mismatch.evidence)), ("bag_mismatch", 2))
        assisted = observe_replay(board, [good[0], (508, ()), good[1]], [1, 2])
        self.assertEqual((assisted.status, len(assisted.evidence)), ("non_click_action", 1))
        self.assertEqual(simulate_players(board, [PlayerPolicy(1, (0,) * FEATURE_COUNT)]), 1000)

    def test_seven_unique_clicks_are_full_even_when_replay_continues(self):
        labels = tuple(range(1, 8)) * 3
        board = PlayerBoard(labels, (0,) * len(labels), ((),) * len(labels))
        result = observe_replay(board, [(1, (index,)) for index in range(1, 9)], range(1, 9))
        self.assertEqual((result.status, len(result.evidence)), ("full", 7))

    def test_surface_curve_is_checked_after_the_click_releases_covered_tiles(self):
        board = PlayerBoard((-12, -12, -12), (0b010, 0, 0), ((), (0,), ()))
        moves = [(1, (2,)), (1, (1,)), (1, (3,))]
        clean = observe_replay(board, moves, [1, 2, 0], observed_surface_sizes=[2, 1, 0])
        self.assertEqual(clean.status, "won")
        bad_surface = observe_replay(board, moves, [1, 2, 0], observed_surface_sizes=[1, 1, 0])
        self.assertEqual((bad_surface.status, len(bad_surface.evidence)), ("surface_mismatch", 0))

    def test_fitted_preferences_account_for_number_of_alternatives(self):
        choices = [ChoiceObservation(((0, 3), (1, 1)), selected) for selected in [1, 0] * 30]
        # Both feature codes were selected equally often, but code 1 had
        # one quarter of the opportunities.
        policy = fit_policy(choices, weight=1)
        self.assertGreater(policy.priorities()[1], policy.priorities()[0] * 2)
