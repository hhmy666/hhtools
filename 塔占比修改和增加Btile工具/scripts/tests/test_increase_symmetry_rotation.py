"""Tower additions follow source geometry, including diagonal symmetry."""
import unittest
from hashlib import sha256

from scripts.tests.test_increase_engine import source
from scripts.tower_reducer.increase import (
    IncreasePlan, TowerAddition, build_increase, normalise_increase_options,
    rewrite_increase,
)
from scripts.tower_reducer.increase_geometry import candidates, orbit, symmetry
from scripts.tower_reducer.increase_verification import verify_increase
from scripts.tower_reducer.template_reader import decode_template


ROTATION_ONLY = [(6, 6), (10, 10), (6, 8), (10, 8)]


class IncreaseSymmetryRotationTests(unittest.TestCase):
    def audited_pair(self, points, positions):
        raw = source(points=points)
        towers = tuple(TowerAddition(i, x, y, 0, 0, 6, (-10,) * 6)
                       for i, (x, y) in enumerate(positions))
        plan = IncreasePlan(sha256(raw).hexdigest(), {
            'target_mode': 'count', 'added_tiles': 12, 'stack_count': 2,
            'auto_translate': False,
        }, towers, (0, 0), 60)
        output = rewrite_increase(raw, plan, 'moreToriginal')
        return verify_increase(raw, output, plan, 'moreToriginal')

    def test_rotation_only_source_generates_diagonal_pairs(self):
        document = decode_template(source(points=ROTATION_ONLY))
        axes, names = symmetry(document)
        self.assertEqual(axes, (16, 16))
        self.assertEqual(orbit((3, 12), axes, names), ((3, 12), (13, 4)))
        groups = candidates(document, normalise_increase_options({}), (0, 0))
        self.assertIn(((3, 12), (13, 4)), groups)
        self.assertNotIn(((3, 12), (13, 12)), groups)

    def test_only_actual_horizontal_or_vertical_reflection_is_required(self):
        cases = (
            ([(6, 6), (10, 6), (6, 8), (10, 8), (8, 10)],
             ((3, 12), (13, 12))),
            ([(6, 6), (6, 10), (8, 6), (8, 10), (10, 8)],
             ((3, 4), (3, 12))),
        )
        for points, expected in cases:
            with self.subTest(points=points):
                axes, names = symmetry(decode_template(source(points=points)))
                self.assertEqual(orbit((3, 12), axes, names), expected)

    def test_asymmetric_source_defaults_to_center_rotation(self):
        # Source bbox center is (7, 9), so (2, 14) pairs with (12, 4).
        document = decode_template(source(points=[(4, 8), (8, 8), (10, 10)]))
        axes, names = symmetry(document)
        self.assertEqual(orbit((2, 14), axes, names), ((2, 14), (12, 4)))
        self.assertEqual(orbit((3, 16), *symmetry(document, 1, 2)),
                         ((3, 16), (13, 6)))

    def test_planner_accepts_explicit_diagonal_rotation_pair(self):
        raw = source(points=ROTATION_ONLY)
        result = build_increase(raw, {
            'target_mode': 'count', 'added_tiles': 12, 'stack_count': 2,
            'auto_translate': False,
            'tower_positions': [{'index': 0, 'x': 3, 'y': 12},
                                {'index': 1, 'x': 13, 'y': 4}],
        }, 'moreToriginal')
        self.assertTrue(result['ok'], result.get('error'))
        self.assertEqual([(s.x, s.y) for s in decode_template(result['output']).stacks],
                         [(3, 12), (13, 4)])

    def test_existing_centerline_pair_moves_using_rotation_of_both_axes(self):
        raw = source(total=72, points=ROTATION_ONLY,
                     towers=((8, 4, 6, 0), (8, 12, 6, 2)))
        result = build_increase(raw, {
            'target_mode': 'count', 'added_tiles': 0, 'operation': 'existing',
            'auto_translate': False,
            'tower_positions': [{'index': 0, 'x': 6, 'y': 3}],
        }, 'moreToriginal')
        self.assertTrue(result['ok'], result.get('error'))
        self.assertEqual([(s.x, s.y) for s in decode_template(result['output']).stacks],
                         [(6, 3), (10, 13)])

    def test_readback_accepts_true_rotation_pair_without_x_mirror(self):
        report = self.audited_pair(ROTATION_ONLY, [(3, 12), (13, 4)])
        self.assertTrue(report.ok, report.errors)

    def test_readback_rejects_x_pair_that_breaks_source_rotation(self):
        report = self.audited_pair(ROTATION_ONLY, [(3, 12), (13, 12)])
        self.assertFalse(report.ok)
        self.assertIn('position_symmetry', {issue.code for issue in report.errors})

    def test_readback_accepts_center_rotation_default_for_asymmetric_source(self):
        report = self.audited_pair([(4, 8), (8, 8), (10, 10)],
                                   [(2, 14), (12, 4)])
        self.assertTrue(report.ok, report.errors)


if __name__ == '__main__':
    unittest.main()
