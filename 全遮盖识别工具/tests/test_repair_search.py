"""Behavioral regressions for bounded, identity-preserving repair search."""

import unittest
from collections import Counter
from unittest.mock import patch

from covercheck.engine import analyze_bytes
from covercheck.repair import validate_board
from test_engine import fixture, message
from test_repair import SAFE_TILES, WHOLE_LAYER_TILES, tower_board

try:
    from covercheck.repair_search import search_repair
except ImportError:
    search_repair = None


class RepairSearchTests(unittest.TestCase):
    def search(self, tiles, towers=()):
        self.assertIsNotNone(search_repair, "bounded low-impact search is missing")
        raw = fixture(tiles, towers)
        return raw, search_repair(raw, analyze_bytes(raw))

    def assert_repaired(self, source, result):
        self.assertEqual(result["status"], "repaired", result["reason"])
        checked = validate_board(result["raw"])
        self.assertTrue(checked["valid"], checked["errors"])
        self.assertFalse(checked["analysis"]["issues"])
        from covercheck.lowimpact import audit_repair
        audit = audit_repair(source, result["raw"], result["origin_indices"])
        self.assertTrue(audit["valid"], audit["errors"])

    def test_no_issues_keeps_exact_source_bytes(self):
        self.assertIsNotNone(search_repair, "bounded low-impact search is missing")
        raw = fixture([(2, 2, 0, 99), (6, 2, 0, 99), (10, 2, 0, 99)]) + message(99, b"opaque")
        result = search_repair(raw, analyze_bytes(raw))
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["raw"], raw)

    def test_clean_unsupported_source_keeps_exact_bytes(self):
        raw, result = self.search([(2, 2, 1, 99), (6, 6, 0, 99), (10, 10, 0, 99)])
        self.assertEqual(result["status"], "unchanged", result['reason'])
        self.assertEqual(result["raw"], raw)

    def test_fullcovers_are_repaired_without_moving_unrelated_original_floats(self):
        floating = [(x, 25, 7, -10) for x in (2, 6, 10)]
        source, result = self.search(SAFE_TILES + floating)
        self.assert_repaired(source, result)
        output = dict(zip(result['origin_indices'], analyze_bytes(result['raw'])['tiles']))
        for index, expected in enumerate(floating, len(SAFE_TILES)):
            self.assertEqual(tuple(output[index][key] for key in ('x', 'y', 'z', 'tile_id')), expected)

    def test_floating_fullcover_pair_is_still_repaired(self):
        tiles = SAFE_TILES + [(x, 16, z, -10) for x in (2, 6, 10) for z in (4, 6)]
        source, result = self.search(tiles)
        self.assert_repaired(source, result)
        self.assertEqual(analyze_bytes(result['raw'])['floating_count'], 0)

    def test_shared_zero_budget_retains_source_and_reports_incomplete(self):
        with patch("covercheck.repair.SEARCH_NODE_LIMIT", 0):
            raw, result = self.search(SAFE_TILES)
        self.assertEqual(result["status"], "unrepairable")
        self.assertFalse(result["search_complete"])
        self.assertEqual(result["raw"], raw)

    def test_same_count_hidden_move_preserves_source_identity_and_nonproblem_positions(self):
        source, result = self.search(SAFE_TILES)
        self.assert_repaired(source, result)
        self.assertEqual(sorted(result["origin_indices"]), list(range(15)))
        output = analyze_bytes(result["raw"])["tiles"]
        by_origin = dict(zip(result["origin_indices"], output))
        for index in [0] + list(range(2, 15)):
            self.assertEqual(tuple(by_origin[index][k] for k in ("x", "y", "z", "tile_id")), SAFE_TILES[index])
        self.assertEqual(len(output), 15)

    def test_middle_problem_move_precedes_larger_upper_removal_set(self):
        tiles = [(x, y, z, 99) for x, y, z, _ in SAFE_TILES]
        tiles += [(2, 2, 2, 99), (4, 2, 2, 99), (14, 16, 0, 99)]
        source, result = self.search(tiles)
        self.assert_repaired(source, result)
        self.assertEqual(result["metrics"]["problem_changed_count"], 1)
        self.assertEqual(result["metrics"]["displacement_total"], 1)

    def test_first_accepted_candidate_does_not_claim_search_exhaustion(self):
        source, result = self.search(SAFE_TILES)
        self.assert_repaired(source, result)
        self.assertFalse(result["search_complete"])

    def test_smaller_plan_probe_exhaustion_keeps_a_feasible_larger_fallback(self):
        tiles = [(x, y, z, 99) for x, y, z, _ in SAFE_TILES]
        tiles += [(2, 2, 2, 99), (4, 2, 2, 99), (14, 16, 0, 99)]
        with patch("covercheck.repair_search.PREFERRED_PLACEMENT_NODES", 0, create=True):
            source, result = self.search(tiles)
        self.assert_repaired(source, result)
        self.assertEqual(result["metrics"]["problem_changed_count"], 2)
        self.assertFalse(result["search_complete"])

    def test_compresses_only_a_wholly_removed_problem_layer_for_upper_support(self):
        raw, result = self.search(WHOLE_LAYER_TILES)
        self.assert_repaired(raw, result)
        output = dict(zip(result["origin_indices"], analyze_bytes(result["raw"])["tiles"]))
        self.assertEqual((output[17]["x"], output[17]["y"], output[17]["z"]), (3, 15, 3))
        self.assertEqual(len(output), 18)

    def test_three_same_type_problem_uppers_can_be_deleted_as_final_group(self):
        tiles = [(x, 4, z, -10) for x in (2, 6, 10) for z in (0, 1)]
        source, result = self.search(tiles)
        self.assert_repaired(source, result)
        self.assertEqual(result["origin_indices"], [0, 2, 4])
        self.assertEqual(len(analyze_bytes(result["raw"])["tiles"]), 3)

    def test_different_visible_upper_type_uses_lower_alternative_without_retyping(self):
        tiles = [(x, y, z, 100 if index == 1 else 99)
                 for index, (x, y, z, _) in enumerate(SAFE_TILES)]
        source, result = self.search(tiles)
        self.assert_repaired(source, result)
        output = analyze_bytes(result["raw"])["tiles"]
        by_origin = dict(zip(result["origin_indices"], output))
        self.assertEqual((by_origin[1]["x"], by_origin[1]["y"], by_origin[1]["tile_id"]), (2, 2, 100))
        self.assertEqual(Counter(tile["tile_id"] for tile in output), Counter({99: 14, 100: 1}))

    def test_original_missing_support_is_preserved_while_fullcover_is_repaired(self):
        tiles = [(x, y, z, 99) for x, y, z, _ in SAFE_TILES]
        tiles[6] = (8, 5, 0, 99)
        tiles += [(8, 8, 1, 99), (2, 15, 0, 99), (14, 15, 0, 99)]
        source, result = self.search(tiles)
        self.assertTrue(validate_board(source)["valid"])
        self.assertTrue(validate_board(source)['analysis']['support_issues'])
        self.assert_repaired(source, result)
        self.assertEqual(len(analyze_bytes(result["raw"])["tiles"]), 18)
        output = dict(zip(result["origin_indices"], analyze_bytes(result["raw"])["tiles"]))
        self.assertEqual(tuple(output[15][key] for key in ('x', 'y', 'z')), (8, 8, 1))

    def test_original_hidden_float_and_exposed_floats_are_not_unnecessary_helpers(self):
        tiles = [(x, y, z, 99) for x, y, z, _ in SAFE_TILES]
        tiles[6] = (8, 5, 0, 99)
        tiles += [(8, 8, 1, 99), (6, 8, 4, 99), (8, 8, 4, 99),
                  (10, 8, 4, 99), (2, 15, 0, 99), (14, 15, 0, 99)]
        source, result = self.search(tiles)
        self.assert_repaired(source, result)
        self.assertEqual(len(analyze_bytes(result["raw"])["tiles"]), 21)
        output = dict(zip(result["origin_indices"], analyze_bytes(result["raw"])["tiles"]))
        for index, expected in [(15, (8, 8, 1)), (16, (6, 8, 4)), (17, (8, 8, 4)), (18, (10, 8, 4))]:
            self.assertEqual(tuple(output[index][key] for key in ('x', 'y', 'z')), expected)
        self.assertEqual(result["strategy"].split(":")[0], "ordinary")

    def test_tower_growth_is_last_fallback_and_keeps_equal_groups_and_strict_rank(self):
        towers = [(4, 12, 0), (8, 12, 0), (12, 12, 0)]
        tiles = [(x, y, z, 99) for (x, y, _), height in zip(towers, (2, 4, 2))
                 for z in range(height)]
        tiles += [(x, 2, z, 99) for x in (2, 5, 8, 11, 14) for z in (0, 1)]
        source, result = self.search(tiles, towers)
        self.assert_repaired(source, result)
        self.assertEqual(result["strategy"].split(":")[0], "tower_fallback")
        output = analyze_bytes(result["raw"])
        self.assertEqual(Counter(tile["tower_index"] for tile in output["tiles"]
                                 if tile["tower_index"] is not None), Counter({0: 3, 1: 4, 2: 3}))
        self.assertEqual(len(output["tiles"]), 15)

    def test_mixed_visible_types_do_not_starve_feasible_lower_tower_alternative(self):
        tiles, towers = tower_board((2, 4, 2), (-11, -11, -11, -10, -10))
        source, result = self.search(tiles, towers)
        self.assert_repaired(source, result)
        output = analyze_bytes(result["raw"])
        self.assertEqual(Counter(tile["tower_index"] for tile in output["tiles"]
                                 if tile["tower_index"] is not None), Counter({0: 3, 1: 4, 2: 3}))
        self.assertEqual(Counter(tile["tile_id"] for tile in output["tiles"]), Counter({-10: 12, -11: 3}))


if __name__ == "__main__":
    unittest.main()
