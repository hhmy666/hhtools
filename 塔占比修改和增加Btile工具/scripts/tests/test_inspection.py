"""Structure checks for editable template documents."""

import unittest
from pathlib import Path

from scripts.tests.project_fixtures import read_project_fixture
from scripts.tower_reducer.inspection import inspect_template
from scripts.tower_reducer.models import Stack, TemplateDocument, Tile
from scripts.tower_reducer.template_reader import decode_template


def document(points, anchors=(), width=17, height=19):
    tiles = [
        Tile(i, i, x, y, z, kind, None, (), (x, y, z, kind, None))
        for i, (x, y, z, kind) in enumerate(points)
    ]
    stacks = [Stack(i, i, x, y, 2, ()) for i, (x, y) in enumerate(anchors)]
    return TemplateDocument(b"", (), "sample", 0, tiles, stacks, [], width=width, height=height)


class InspectionTests(unittest.TestCase):
    def test_counts_towers_ab_and_non_tower_layers(self):
        data = document(
            [(4, 4, 0, -10), (6, 4, 0, -11), (8, 8, 0, -10),
             (8, 8, 1, -11), (8, 8, 2, -10)],
            [(8, 8)],
        )
        result = inspect_template(data)
        self.assertEqual((result.total_tiles, result.a_count, result.b_count), (5, 3, 2))
        self.assertEqual((result.tower_tile_count, result.non_tower_layers), (3, (0,)))
        self.assertEqual([(s.anchor, s.count, s.z_levels) for s in result.stacks],
                         [((8, 8), 3, (0, 1, 2))])
        self.assertEqual(result.tower_ratio, 0.6)

    def test_rejects_duplicate_anchor_z_gap_and_special_tower_tile(self):
        data = document([(8, 8, 0, -10), (8, 8, 2, 42)], [(8, 8), (8, 8)])
        result = inspect_template(data)
        self.assertTrue({"duplicate_stack_anchor", "tower_z_gap", "special_tower_tile"}
                        <= {issue.code for issue in result.issues if issue.severity == "error"})

    def test_empty_stack_and_non_multiple_ab_counts_are_precheck_errors(self):
        result = inspect_template(document([(4, 4, 0, -10), (6, 4, 0, -11)], [(8, 8)]))
        self.assertTrue({"empty_stack", "ab_count_not_multiple_of_three"}
                        <= {issue.code for issue in result.issues if issue.severity == "error"})

    def test_same_layer_collision_includes_tower_and_non_tower_tiles(self):
        result = inspect_template(document([(8, 8, 0, -10), (9, 8, 0, -10)], [(8, 8)]))
        self.assertIn("same_layer_overlap", {issue.code for issue in result.issues})

    def test_all_tower_has_no_support_surface_and_no_tower_needs_no_optimization(self):
        all_tower = inspect_template(document([(8, 8, 0, -10)], [(8, 8)]))
        self.assertIn("no_non_tower_surface", {issue.code for issue in all_tower.issues})
        no_tower = inspect_template(document([(4, 4, 0, -10)]))
        self.assertFalse(no_tower.needs_optimization)
        self.assertEqual(no_tower.tower_tile_count, 0)

    def test_symmetry_reports_horizontal_vertical_and_rotation_separately(self):
        # Reflect around the occupied non-tower bounding box, retaining Z.
        both = inspect_template(document([(4, 4, 0, -10), (8, 4, 0, -10),
                                          (4, 8, 0, -10), (8, 8, 0, -10)]))
        self.assertEqual(both.symmetry, ("horizontal", "vertical", "rotation"))
        horizontal = inspect_template(document([(4, 4, 0, -10), (8, 4, 0, -10),
                                                (5, 8, 0, -10), (7, 8, 0, -10)]))
        self.assertEqual(horizontal.symmetry, ("horizontal",))

    def test_exact_profile_distinguishes_offset_sets_and_one_on_four(self):
        # First upper tile spans two lower tiles at 1/2 each. Second spans four at 1/4 each.
        data = document([
            (3, 4, 0, -10), (5, 4, 0, -10),
            (9, 9, 0, -10), (11, 9, 0, -10), (9, 11, 0, -10), (11, 11, 0, -10),
            (4, 4, 1, -10), (10, 10, 1, -10),
        ])
        result = inspect_template(data)
        self.assertEqual(len(result.profiles), 2)
        self.assertIn(((1, 0), (-1, 0)), result.profiles)
        self.assertIn(((1, 1), (1, -1), (-1, 1), (-1, -1)), result.profiles)
        self.assertEqual(result.layer_profiles[1], result.profiles)

    def test_same_fraction_with_different_relative_offset_is_different_profile(self):
        data = document([(3, 4, 0, -10), (9, 4, 0, -10),
                         (4, 4, 1, -10), (10, 5, 1, -10)])
        result = inspect_template(data)
        self.assertIn(((-1, 0),), result.profiles)
        self.assertIn(((-1, -1),), result.profiles)
        self.assertEqual(len(result.profiles), 2)

    def test_mixed_upper_layer_reports_each_tile_without_lower_overlap(self):
        data = document([
            (8, 8, 0, -10), (8, 8, 1, -10), (8, 8, 2, -10),
            (4, 4, 0, -10), (6, 4, 0, -10), (10, 10, 0, -10), (12, 4, 0, -10),
            (5, 4, 1, -10), (14, 14, 1, -10),
        ], [(8, 8)])
        result = inspect_template(data)
        self.assertEqual(result.layer_profiles[1], (((1, 0), (-1, 0)),))
        self.assertIn((8,), [issue.evidence for issue in result.issues
                             if issue.code == "unsupported_non_tower_tile"])
        self.assertTrue(result.can_optimize)
        self.assertTrue(all(issue.severity == "warning" for issue in result.issues
                            if issue.code == "unsupported_non_tower_tile"))

    def test_whole_existing_upper_layer_without_immediate_lower_is_preserved(self):
        data = document([
            (8, 8, 0, -10), (8, 8, 1, -10), (8, 8, 2, -10),
            (4, 4, 0, -10), (6, 4, 0, -10), (12, 12, 0, -10),
            (5, 4, 2, -10), (10, 4, 2, -10), (14, 14, 2, -10),
        ], [(8, 8)])
        result = inspect_template(data)
        self.assertIn("missing_lower_non_tower_layer", {issue.code for issue in result.issues})
        self.assertEqual(len([issue for issue in result.issues
                              if issue.code == "unsupported_non_tower_tile"]), 3)
        self.assertTrue(result.can_optimize)

    def test_missing_dimensions_block_optimization(self):
        for width, height in ((None, 19), (17, None), (None, None)):
            with self.subTest(width=width, height=height):
                result = inspect_template(document([
                    (8, 8, 0, -10), (8, 8, 1, -10), (8, 8, 2, -10),
                    (4, 4, 0, -10), (6, 4, 0, -10), (12, 12, 0, -10),
                ], [(8, 8)], width=width, height=height))
                self.assertIn("missing_board_dimensions", {issue.code for issue in result.issues})
                self.assertFalse(result.can_optimize)

    def test_coordinate_bounds_report_original_outlier_and_invalid_footprint(self):
        data = document([(1, 4, 0, -10), (16, 18, 1, -10)])
        result = inspect_template(data)
        codes = {issue.code for issue in result.issues}
        self.assertIn("outside_safe_anchor", codes)
        self.assertIn("outside_board_footprint", codes)
        self.assertTrue(all(issue.severity == "warning" for issue in result.issues
                            if issue.code == "outside_safe_anchor"))

    def test_all_four_golden_sources_have_no_blockers_and_expected_boundary_warnings(self):
        for name, expected_warnings in (("mxd0057v1", 4), ("hr0029", 2),
                                        ("hr0044", 0), ("mxd5010", 8)):
            with self.subTest(name=name):
                result = inspect_template(decode_template(read_project_fixture(name + "_m.bytes")))
                self.assertTrue(result.can_optimize)
                self.assertTrue(result.profiles)
                self.assertEqual([issue.code for issue in result.issues].count("outside_safe_anchor"),
                                 expected_warnings)
                self.assertFalse([issue for issue in result.issues if issue.severity == "error"])


if __name__ == "__main__":
    unittest.main()
