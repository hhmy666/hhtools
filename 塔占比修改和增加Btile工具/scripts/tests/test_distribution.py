"""Tower allocation, mixed suffix movement and final replenishment contracts."""

import unittest
import subprocess
import sys
from pathlib import Path

from scripts.tests.project_fixtures import read_project_fixture
from scripts.tower_reducer.distribution import (
    DistributionFailure, DistributionOptions, plan_tower_distribution,
    iter_tower_distributions, select_move_type, strict_tile_cap,
)
from scripts.tower_reducer.inspection import inspect_template
from scripts.tower_reducer.models import Stack, TemplateDocument, Tile
from scripts.tower_reducer.template_reader import decode_template

def source(stacks, *, extra_a=0, extra_b=0):
    """Each stack is a bottom-to-top string of A/B tiles."""
    tiles = []
    anchors = []
    for stack_number, kinds in enumerate(stacks):
        x, y = 2 + 2 * (stack_number % 7), 10 + 2 * (stack_number // 7)
        anchors.append(Stack(stack_number, stack_number, x, y, 2, ()))
        for z, kind in enumerate(kinds):
            tile_id = -10 if kind == "A" else -11
            i = len(tiles)
            tiles.append(Tile(i, i, x, y, z, tile_id, None, (), (x, y, z, tile_id, None)))
    counts = {kind: sum(tile.tile_id == (-10 if kind == "A" else -11) for tile in tiles)
              for kind in ("A", "B")}
    extra_a += (-counts["A"] - extra_a) % 3
    extra_b += (-counts["B"] - extra_b) % 3
    for kind, amount, y in (("A", extra_a, 2), ("B", extra_b, 4)):
        for n in range(amount):
            x = 2 + 2 * (n % 7)
            z = n // 7
            tile_id = -10 if kind == "A" else -11
            i = len(tiles)
            tiles.append(Tile(i, i, x, y, z, tile_id, None, (), (x, y, z, tile_id, None)))
    return TemplateDocument(b"", (), "sample", 0, tiles, anchors, [], width=17, height=19)


class CapTests(unittest.TestCase):
    def test_zero_and_exact_boundary_are_strict(self):
        self.assertEqual(strict_tile_cap(105, 0), 0)
        self.assertEqual(strict_tile_cap(100, 0.25), 24)
        self.assertEqual(strict_tile_cap(101, 0.25), 25)
        self.assertEqual(strict_tile_cap(105, 0.25), 26)
        self.assertEqual(strict_tile_cap(100, 0.27), 26)
        self.assertEqual(strict_tile_cap(100, 1), 99)

    def test_invalid_ratio_is_rejected(self):
        for value in (-0.01, 1.01, float("nan"), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                strict_tile_cap(100, value)


class GoldenDistributionTests(unittest.TestCase):
    def test_reduce_accepts_27_percent_below_35_point_4_percent_source(self):
        doc = source(['A' * 17], extra_a=31)
        inspected = inspect_template(doc)
        self.assertAlmostEqual(inspected.tower_ratio, 17 / 48)
        result = plan_tower_distribution(doc, inspected, DistributionOptions(ratio=.27))
        self.assertTrue(result.ok, result)
        self.assertEqual(result.cap, 12)
        self.assertLess(result.final_ratio, .27)

    def test_reduce_minimum_may_also_exceed_25_percent(self):
        doc = source(['A' * 17], extra_a=31)
        result = plan_tower_distribution(doc, inspect_template(doc),
                                         DistributionOptions(ratio=.30, min_ratio=.27))
        self.assertTrue(result.ok, result)
        self.assertGreaterEqual(result.final_ratio, .27)
        self.assertLess(result.final_ratio, .30)

    def test_reduce_rejects_upper_bound_above_current_source_ratio(self):
        doc = source(['A' * 17], extra_a=31)
        result = plan_tower_distribution(doc, inspect_template(doc), DistributionOptions(ratio=.36))
        self.assertFalse(result.ok, result)
        self.assertEqual(result.code, 'reduce_ratio_above_source')
        self.assertIn('35.4', result.message)
        self.assertIn('sample', result.message)

    def test_source_ratio_as_upper_bound_remains_strict(self):
        doc = source(['A' * 17], extra_a=31)
        inspected = inspect_template(doc)
        result = plan_tower_distribution(doc, inspected, DistributionOptions(ratio=inspected.tower_ratio))
        self.assertTrue(result.ok, result)
        self.assertLess(result.final_ratio, inspected.tower_ratio)

    def test_four_accepted_sources_match_tower_targets(self):
        expected = {
            "mxd0057v1": (3, 5, 9, 5, 3),
            "hr0029": (6, 6, 6, 6),
            "hr0044": (4, 5, 7, 5, 4),
            "mxd5010": (4, 4, 4, 4, 4, 4),
        }
        for name, target in expected.items():
            with self.subTest(name=name):
                doc = decode_template(read_project_fixture(name + "_m.bytes"))
                inspected = inspect_template(doc)
                plan = plan_tower_distribution(doc, inspected, DistributionOptions())
                self.assertTrue(plan.ok, plan)
                self.assertEqual(plan.retained_counts, target)
                self.assertLess(sum(plan.retained_counts) / inspected.total_tiles, .25)
                self.assertEqual(plan.move_count, inspected.tower_tile_count - sum(target))
                self.assertEqual(plan.move_type, "A")
                self.assertEqual(plan, plan_tower_distribution(doc, inspected, DistributionOptions()))

    def test_ratio_zero_removes_every_tower_member(self):
        doc = decode_template(read_project_fixture("hr0029_m.bytes"))
        plan = plan_tower_distribution(doc, inspect_template(doc), DistributionOptions(ratio=0))
        self.assertTrue(plan.ok, plan)
        self.assertEqual(plan.retained_counts, (0, 0, 0, 0))
        self.assertEqual(len(plan.moved_tile_indices), 36)


class AllocationTests(unittest.TestCase):
    def test_dp_keeps_population_residues_when_shape_cost_and_total_tie(self):
        doc = source(["AAAA", "AAAB", "BABA"], extra_a=18)
        doc.tiles[-1].y = 4
        inspected = inspect_template(doc)
        self.assertFalse(inspected.symmetry)
        result = plan_tower_distribution(doc, inspected, DistributionOptions())
        self.assertTrue(result.ok, result)
        # (2,2,3) has the same shape score but would yield A=23/B=7.
        # Keeping the residue branch (2,3,2) yields A=24/B=6 at the same cap.
        self.assertEqual(result.retained_counts, (2, 3, 2))
        self.assertEqual(result.final_counts, (24, 6))

    def test_iterator_crosses_invalid_residues_to_reach_every_valid_height(self):
        from scripts.tower_reducer.placement import SearchBudget
        doc = source(["AAABBB"], extra_a=12)
        candidates = list(iter_tower_distributions(doc, inspect_template(doc),
                                                   DistributionOptions(), SearchBudget(100)))
        self.assertEqual({p.retained_counts for p in candidates}, {(0,), (3,), (4,)})
        self.assertEqual(len(candidates), 3)
        self.assertEqual({p.final_counts for p in candidates}, {(12, 6), (15, 3)})

    def test_many_nonmirror_equal_stacks_have_bounded_planning_time(self):
        # Fourteen independent equal-height towers used to make the old DP
        # retain almost every height-choice history instead of merging states.
        script = (
            "from scripts.tests.test_distribution import source\n"
            "from scripts.tower_reducer.inspection import inspect_template\n"
            "from scripts.tower_reducer.distribution import DistributionOptions, plan_tower_distribution\n"
            "doc = source(['AAAA'] * 14, extra_a=64)\n"
            "for tile in doc.tiles:\n"
            "    if tile.y == 2 and tile.x == 2: tile.y = 4\n"
            "inspection = inspect_template(doc)\n"
            "assert inspection.can_optimize and not inspection.symmetry, inspection.issues\n"
            "plan = plan_tower_distribution(doc, inspection, DistributionOptions())\n"
            "assert plan.ok and plan.retained_counts == (2,) * 14, plan\n"
        )
        try:
            result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                    text=True, timeout=3, check=False)
        except subprocess.TimeoutExpired:
            self.fail("equal-height tower planning exceeded the 3-second state-growth bound")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_nonmirror_equal_source_heights_can_diverge_to_keep_more_towers(self):
        doc = source(["AAA", "BBB"], extra_a=9, extra_b=6)
        doc.tiles[-1].x = 13  # Break non-tower reflection; the two source towers are not mirror partners.
        inspected = inspect_template(doc)
        self.assertFalse(inspected.symmetry)
        self.assertTrue(inspected.can_optimize)
        automatic = plan_tower_distribution(doc, inspected, DistributionOptions())
        self.assertTrue(automatic.ok, automatic)
        self.assertEqual(automatic.retained_counts, (3, 2))
        manual = plan_tower_distribution(doc, inspected,
                                         DistributionOptions(retained_counts=(3, 2)))
        self.assertTrue(manual.ok, manual)

    def test_single_stack_retains_as_many_as_strict_cap_allows(self):
        doc = source(["A" * 9], extra_a=18)
        result = plan_tower_distribution(doc, inspect_template(doc), DistributionOptions())
        self.assertTrue(result.ok, result)
        self.assertEqual(result.retained_counts, (6,))

    def test_symmetric_equal_stacks_stay_equal_with_even_and_odd_caps(self):
        for extra_a in (12, 15):
            with self.subTest(extra_a=extra_a):
                doc = source(["A" * 9, "A" * 9], extra_a=extra_a)
                result = plan_tower_distribution(doc, inspect_template(doc), DistributionOptions())
                self.assertTrue(result.ok, result)
                self.assertEqual(result.retained_counts[0], result.retained_counts[1])
                self.assertGreater(min(result.retained_counts), 0)
                self.assertLessEqual(sum(result.retained_counts), result.cap)

    def test_manual_counts_validate_range_symmetry_ratio_and_move_feasibility(self):
        doc = source(["A" * 6, "A" * 6], extra_a=6)
        inspected = inspect_template(doc)
        cases = (((7, 0), "manual_out_of_range"),
                 ((3, 2), "manual_symmetry"),
                 ((6, 6), "tower_ratio_exceeded"))
        for counts, code in cases:
            with self.subTest(counts=counts):
                result = plan_tower_distribution(doc, inspected,
                                                 DistributionOptions(retained_counts=counts))
                self.assertIsInstance(result, DistributionFailure)
                self.assertEqual(result.code, code)
                self.assertTrue(result.evidence)
                self.assertTrue(result.recommendations)

    def test_manual_mixed_suffix_still_requires_valid_final_populations(self):
        doc = source(["BAAA", "ABBB"], extra_a=5, extra_b=5)
        result = plan_tower_distribution(doc, inspect_template(doc),
                                         DistributionOptions(ratio=.25, retained_counts=(2, 2)))
        self.assertIsInstance(result, DistributionFailure)
        self.assertEqual(result.code, "final_type_counts_not_multiple_of_three")
        self.assertEqual(dict(result.evidence)["final_counts"], (11, 7))
        self.assertTrue(result.evidence)
        self.assertTrue(result.recommendations)

    def test_manual_failure_leaves_candidate_geometry_verification_to_joint_solver(self):
        doc = source(["AAA", "BBB"], extra_a=9, extra_b=6)
        doc.tiles[-1].x = 13
        result = plan_tower_distribution(doc, inspect_template(doc),
                                         DistributionOptions(retained_counts=(2, 2)))
        self.assertIsInstance(result, DistributionFailure)
        self.assertEqual(result.code, "final_type_counts_not_multiple_of_three")
        self.assertNotIn("可尝试合法保留数量", " ".join(result.recommendations))
        valid = plan_tower_distribution(doc, inspect_template(doc),
                                        DistributionOptions(retained_counts=(3, 2)))
        self.assertTrue(valid.ok, valid)

    def test_less_than_max_ratio_recolors_suffix_without_raising_ceiling(self):
        doc = source(["BBBAAA"], extra_a=3, extra_b=6)
        result = plan_tower_distribution(doc, inspect_template(doc), DistributionOptions(ratio=.20))
        self.assertTrue(result.ok, result)
        self.assertEqual(result.retained_counts, (0,))
        self.assertEqual(result.cap, 2)
        self.assertEqual(result.final_counts, (9, 6))
        self.assertLess(result.final_ratio, .20)

    def test_max_ratio_allows_replenishing_minority_from_majority_suffix(self):
        doc = source(["BBBAAA"], extra_b=6)
        result = plan_tower_distribution(doc, inspect_template(doc), DistributionOptions())
        self.assertTrue(result.ok, result)
        self.assertEqual(result.retained_counts, (0,))
        self.assertEqual(result.removed_counts, (3, 3))
        self.assertEqual(result.final_counts, (6, 6))
        self.assertEqual(result.move_type, "A")

    def test_unrepairable_population_reports_counts_without_raising_ratio(self):
        doc = source(["BBBAAA"], extra_b=6)
        doc.tiles[-1].tile_id = -12  # Eleven A/B Tiles cannot form two multiples of three.
        result = plan_tower_distribution(doc, inspect_template(doc), DistributionOptions(ratio=.20))
        self.assertIsInstance(result, DistributionFailure)
        recommendation = " ".join(result.recommendations)
        self.assertEqual(result.code, "final_type_counts_not_multiple_of_three")
        self.assertEqual(dict(result.evidence)["final_counts"], (6, 5))
        self.assertIn("保持当前塔占比", recommendation)
        self.assertNotIn("提高塔占比", recommendation)


class MoveTypeTests(unittest.TestCase):
    def test_minority_replenishment_accepts_mixed_source_suffixes(self):
        doc = source(["AAABBB"], extra_a=6)
        result = select_move_type(doc, inspect_template(doc), (0,))
        self.assertTrue(result.ok, result)
        self.assertEqual(result.tile_type, "B")
        self.assertEqual(result.moved_tile_indices, (0, 1, 2, 3, 4, 5))

    def test_manual_final_type_divisibility_reports_concrete_counts(self):
        doc = source(["AAA", "BBB"], extra_a=9, extra_b=6)
        doc.tiles[-1].x = 13
        result = plan_tower_distribution(doc, inspect_template(doc),
                                         DistributionOptions(retained_counts=(2, 2)))
        self.assertFalse(result.ok, result)
        self.assertEqual(result.code, "final_type_counts_not_multiple_of_three")
        evidence = dict(result.evidence)
        self.assertEqual(evidence["source_counts"], (12, 9))
        self.assertEqual(evidence["removed_counts"], (1, 1))
        self.assertEqual(evidence["final_counts"], (11, 10))

    def test_automatic_counts_avoid_invalid_final_type_residues(self):
        doc = source(["BBBAAA"], extra_b=6)
        result = plan_tower_distribution(doc, inspect_template(doc), DistributionOptions())
        self.assertTrue(result.ok, result)
        self.assertEqual(result.retained_counts, (0,))
        self.assertEqual(result.move_type, "A")
        self.assertEqual(getattr(result, "removed_counts", None), (3, 3))
        self.assertEqual(getattr(result, "final_counts", None), (6, 6))

    def test_final_divisibility_can_repair_nondivisible_source_counts(self):
        doc = source(["BBB"], extra_a=3, extra_b=3)
        doc.tiles[0].tile_id = -10
        result = plan_tower_distribution(doc, inspect_template(doc),
                                         DistributionOptions(retained_counts=(1,)))
        self.assertTrue(result.ok, result)
        self.assertEqual(getattr(result, "removed_counts", None), (0, 2))
        self.assertEqual(getattr(result, "final_counts", None), (6, 3))

    def test_a_only_and_b_only(self):
        for kind in ("A", "B"):
            with self.subTest(kind=kind):
                doc = source([kind * 6], extra_a=3 if kind == "A" else 0,
                             extra_b=3 if kind == "B" else 0)
                result = select_move_type(doc, inspect_template(doc), (3,))
                self.assertTrue(result.ok, result)
                self.assertEqual(result.tile_type, kind)
                self.assertEqual(len(result.moved_tile_indices), 3)

    def test_ab_chooses_less_common_type_even_if_other_type_is_feasible(self):
        doc = source(["AAABBB"], extra_a=6)
        result = select_move_type(doc, inspect_template(doc), (3,))
        self.assertTrue(result.ok, result)
        self.assertEqual(result.tile_type, "B")

    def test_equal_counts_choose_feasible_top_type_then_a_if_both_feasible(self):
        only_b = source(["AAABBB"])
        self.assertEqual(select_move_type(only_b, inspect_template(only_b), (3,)).tile_type, "B")
        both = source(["AAA", "BBB"])
        self.assertEqual(select_move_type(both, inspect_template(both), (0, 3)).tile_type, "A")

    def test_less_common_type_replenishment_rejects_nondivisible_final_counts(self):
        doc = source(["BBBAAA"], extra_b=6)
        result = select_move_type(doc, inspect_template(doc), (2,))
        self.assertIsInstance(result, DistributionFailure)
        self.assertEqual(result.code, "final_type_counts_not_multiple_of_three")
        self.assertEqual(dict(result.evidence)["final_counts"], (4, 8))
        self.assertTrue(result.evidence)
        self.assertTrue(result.recommendations)

    def test_selection_preserves_tile_ids_and_exact_top_indices(self):
        doc = source(["AAABBB"])
        before = tuple(tile.tile_id for tile in doc.tiles)
        result = select_move_type(doc, inspect_template(doc), (3,))
        self.assertEqual(result.moved_tile_indices, (3, 4, 5))
        self.assertEqual(tuple(tile.tile_id for tile in doc.tiles), before)


if __name__ == "__main__":
    unittest.main()
