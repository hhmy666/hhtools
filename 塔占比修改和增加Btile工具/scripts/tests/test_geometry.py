import unittest

from scripts.tower_reducer.geometry import (
    footprint, overlap_area, overlap_profile, symmetry_orbit, within_bounds,
)


class GeometryTests(unittest.TestCase):
    def test_footprint_and_partial_or_full_overlap(self):
        self.assertEqual(footprint((2, 3)), frozenset({(2, 3), (3, 3), (2, 4), (3, 4)}))
        for other, area in [((3, 4), 1), ((2, 4), 2), ((2, 3), 4), ((4, 3), 0)]:
            self.assertEqual(overlap_area((2, 3), other), area)

    def test_profiles_retain_individual_lower_tile_areas(self):
        self.assertEqual(overlap_profile((3, 3), [(2, 2), (4, 2), (2, 4), (4, 4)]), (1, 1, 1, 1))
        self.assertEqual(overlap_profile((3, 3), [(2, 3), (4, 4)]), (2, 1))
        self.assertEqual(overlap_profile((3, 3), [(3, 3)]), (4,))

    def test_safe_anchor_and_complete_board_footprint(self):
        for point in [(1, 3), (15, 3), (3, 1), (3, 17)]:
            self.assertFalse(within_bounds(point, (17, 19)))
        self.assertTrue(within_bounds((14, 16), (17, 19)))
        self.assertFalse(within_bounds((14, 16), (15, 17)))

    def test_actual_symmetries_and_axis_deduplication(self):
        self.assertEqual(symmetry_orbit((3, 4), (16, 18), ()), ((3, 4),))
        self.assertEqual(symmetry_orbit((3, 4), (16, 18), ('horizontal',)), ((3, 4), (13, 4)))
        self.assertEqual(symmetry_orbit((3, 4), (16, 18), ('vertical',)), ((3, 4), (3, 14)))
        self.assertEqual(symmetry_orbit((3, 4), (16, 18), ('rotation',)), ((3, 4), (13, 14)))
        self.assertEqual(symmetry_orbit((8, 9), (16, 18), ('horizontal', 'vertical')), ((8, 9),))
