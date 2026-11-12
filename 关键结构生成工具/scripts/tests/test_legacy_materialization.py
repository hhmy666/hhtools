from __future__ import annotations

import sys
import unittest
from collections import Counter
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.legacy_materialization import (  # noqa: E402
    LEGACY_MATERIALIZATION_VERSION,
    LegacyRandom,
    legacy_game_seed,
    materialize_legacy_ab,
)


class LegacyMaterializationTest(unittest.TestCase):
    def test_custom_random_known_vector_from_replay_reference(self) -> None:
        random = LegacyRandom(legacy_game_seed(7))
        self.assertEqual(
            [random.get_random(19) for _ in range(10)],
            [8, 2, 0, 4, 4, 8, 3, 14, 3, 12],
        )
        self.assertEqual(legacy_game_seed(0), 1_411_242_022)
        self.assertEqual(legacy_game_seed(1 << 32), legacy_game_seed(0))

    def test_a_b_assignment_matches_replay_reference_and_keeps_order(self) -> None:
        positions_a = tuple(range(100, 112))
        positions_b = tuple(range(200, 209))
        result = materialize_legacy_ab(
            pattern_pool=[11, 22, 33, 44, 55],
            a_positions=positions_a,
            b_positions=positions_b,
            dup_a_types=4,
            dup_b_types=3,
            dup_seed=7,
        )
        self.assertEqual(result.version, LEGACY_MATERIALIZATION_VERSION)
        self.assertEqual(result.random_seed_group_id, 0)
        self.assertEqual(result.effective_a_types, 4)
        self.assertEqual(result.effective_b_types, 3)
        self.assertEqual(
            result.a_assignments,
            tuple(zip(positions_a, (33, 44, 22, 11, 44, 11, 33, 22, 44, 33, 11, 22))),
        )
        self.assertEqual(
            result.b_assignments,
            tuple(zip(positions_b, (33, 22, 22, 11, 11, 33, 22, 33, 11))),
        )
        self.assertEqual(
            result,
            materialize_legacy_ab(
                pattern_pool=[11, 22, 33, 44, 55],
                a_positions=positions_a,
                b_positions=positions_b,
                dup_a_types=4,
                dup_b_types=3,
                dup_seed=7,
            ),
        )

    def test_each_placeholder_restarts_rng_and_pool_prefix_matters(self) -> None:
        kwargs = dict(
            a_positions=[9, 3, 7, 5, 1, 0],
            b_positions=[20, 10, 30, 40, 50, 60],
            dup_a_types=2,
            dup_b_types=2,
            dup_seed=3,
        )
        result = materialize_legacy_ab(pattern_pool=[1, 2, 3], **kwargs)
        self.assertEqual(
            tuple(tile_id for _, tile_id in result.a_assignments),
            tuple(tile_id for _, tile_id in result.b_assignments),
        )
        changed_pool = materialize_legacy_ab(pattern_pool=[3, 2, 1], **kwargs)
        self.assertNotEqual(
            tuple(tile_id for _, tile_id in result.a_assignments),
            tuple(tile_id for _, tile_id in changed_pool.a_assignments),
        )
        self.assertEqual(tuple(position for position, _ in result.a_assignments), tuple(kwargs["a_positions"]))

    def test_zero_and_single_group_boundaries(self) -> None:
        empty = materialize_legacy_ab(
            pattern_pool=[], a_positions=[], b_positions=[],
            dup_a_types=0, dup_b_types=0, dup_seed=-1,
        )
        self.assertEqual(empty.a_assignments, ())
        self.assertEqual(empty.b_assignments, ())
        self.assertEqual(empty.effective_a_types, 0)
        one_group = materialize_legacy_ab(
            pattern_pool=[4, 9], a_positions=[0, 1, 2], b_positions=[],
            dup_a_types=0, dup_b_types=0, dup_seed=0,
        )
        self.assertEqual(one_group.effective_a_types, 1)
        self.assertEqual(tuple(tile for _, tile in one_group.a_assignments), (4, 4, 4))

    def test_all_assigned_types_stay_in_complete_triples_and_seed_wraps(self) -> None:
        kwargs = dict(
            pattern_pool=[4, 9, 12, 17, 23],
            a_positions=range(30), b_positions=range(100, 124),
            dup_a_types=4, dup_b_types=5,
        )
        first = materialize_legacy_ab(**kwargs, dup_seed=5)
        wrapped = materialize_legacy_ab(**kwargs, dup_seed=5 + (1 << 32))
        self.assertEqual(first.a_assignments, wrapped.a_assignments)
        self.assertEqual(first.b_assignments, wrapped.b_assignments)
        for assignments in (first.a_assignments, first.b_assignments):
            self.assertTrue(all(count % 3 == 0 for count in Counter(tile for _, tile in assignments).values()))

    def test_rejects_unsupported_or_ambiguous_inputs(self) -> None:
        base = dict(
            pattern_pool=[1, 2, 3], a_positions=[0, 1, 2], b_positions=[3, 4, 5],
            dup_a_types=2, dup_b_types=2, dup_seed=0,
        )
        bad_cases = (
            ({"random_seed_group_id": 1003}, ValueError),
            ({"pattern_pool": [1]}, ValueError),
            ({"pattern_pool": [1, 1, 3]}, ValueError),
            ({"pattern_pool": [0, 2, 3]}, ValueError),
            ({"a_positions": [0, 1]}, ValueError),
            ({"b_positions": [2, 4, 5]}, ValueError),
            ({"dup_a_types": -1}, ValueError),
            ({"dup_seed": True}, TypeError),
            ({"dup_b_types": 1.5}, TypeError),
        )
        for override, error_type in bad_cases:
            with self.subTest(override=override), self.assertRaises(error_type):
                materialize_legacy_ab(**(base | override))
        with self.assertRaises(ValueError):
            LegacyRandom(0).get_random(0)


if __name__ == "__main__":
    unittest.main()
