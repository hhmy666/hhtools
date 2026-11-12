"""Increase must honor the established inset without repairing source geometry."""
import unittest
from hashlib import sha256

from scripts.tests.test_increase_engine import source
from scripts.tower_reducer.increase import (
    IncreasePlan, TowerAddition, build_increase, normalise_increase_options,
    rewrite_increase,
)
from scripts.tower_reducer.increase_geometry import candidates, tower_fits
from scripts.tower_reducer.increase_verification import verify_increase
from scripts.tower_reducer.template_reader import decode_template


class IncreaseSafeBoundsTests(unittest.TestCase):
    def plan(self, raw, towers=(), translation=(0, 0), **options):
        return IncreasePlan(sha256(raw).hexdigest(), options, tuple(towers),
                            translation, len(decode_template(raw).tiles))

    def test_tower_fit_rejects_outer_anchors_and_includes_inset_edges(self):
        # Removing the inset guard would admit these complete, board-fitting cards.
        for x, y in ((0, 8), (1, 8), (15, 8), (8, 1), (8, 17)):
            with self.subTest(anchor=(x, y)):
                self.assertFalse(tower_fits(x, y, 1, 0, (), 17, 19))
        for x, y in ((2, 2), (14, 2), (2, 16), (14, 16)):
            with self.subTest(anchor=(x, y)):
                self.assertTrue(tower_fits(x, y, 1, 0, (), 17, 19))
        # Anchor legality must not replace the stricter unfolded-body checks.
        self.assertFalse(tower_fits(8, 2, 17, 0, (), 17, 19))

    def test_candidate_orbits_do_not_use_outer_grid_columns(self):
        document = decode_template(source())
        groups = candidates(document, normalise_increase_options({}), (0, 0))
        self.assertTrue(groups)
        self.assertTrue(all(2 <= x <= 14 and 2 <= y <= 16
                            for group in groups for x, y in group))

    def test_exact_new_positions_cannot_bypass_inset(self):
        raw = source()
        result = build_increase(raw, {
            'target_mode': 'manual', 'operation': 'new', 'tower_counts': [3, 3],
            'stack_count': 2, 'auto_translate': False,
            'tower_positions': [{'index': 0, 'x': 1, 'y': 12},
                                {'index': 1, 'x': 15, 'y': 12}],
        }, 'moreToriginal')
        self.assertFalse(result['ok'])
        self.assertIsNone(result['output'])

    def test_independent_verifier_rejects_new_outer_towers(self):
        raw = source()
        towers = (TowerAddition(0, 1, 12, 0, 0, 3, (-10,) * 3),
                  TowerAddition(1, 15, 12, 0, 0, 3, (-10,) * 3))
        plan = self.plan(raw, towers, target_mode='count', added_tiles=6)
        report = verify_increase(raw, rewrite_increase(raw, plan, 'moreToriginal'),
                                 plan, 'moreToriginal')
        self.assertFalse(report.ok)
        self.assertIn('safe_bounds', {issue.code for issue in report.errors})

    def test_appending_to_historical_outer_towers_is_rejected(self):
        raw = source(towers=((1, 12, 3, 0), (15, 12, 3, 0)))
        result = build_increase(raw, {'target_mode': 'manual', 'operation': 'existing',
                                     'tower_counts': [6, 6], 'auto_translate': False},
                                'moreToriginal')
        self.assertFalse(result['ok'])
        towers = (TowerAddition(0, 1, 12, 0, 3, 6, (-10,) * 3),
                  TowerAddition(1, 15, 12, 0, 3, 6, (-10,) * 3))
        plan = self.plan(raw, towers, target_mode='manual', operation='existing',
                         tower_counts=[6, 6], auto_translate=False)
        report = verify_increase(raw, rewrite_increase(raw, plan, 'moreToriginal'),
                                 plan, 'moreToriginal')
        self.assertFalse(report.ok)
        self.assertIn('safe_bounds', {issue.code for issue in report.errors})

    def test_translated_ordinary_tiles_must_stay_inside_inset(self):
        raw = source(points=[(2, 8), (8, 8), (14, 8)])
        options = {'target_mode': 'count', 'added_tiles': 0, 'translate_x': -1,
                   'auto_translate': False}
        result = build_increase(raw, options, 'moreToriginal')
        self.assertFalse(result['ok'])
        plan = self.plan(raw, translation=(-1, 0), **options)
        report = verify_increase(raw, rewrite_increase(raw, plan, 'moreToriginal'),
                                 plan, 'moreToriginal')
        self.assertFalse(report.ok)
        self.assertIn('safe_bounds', {issue.code for issue in report.errors})

    def test_direction_edit_cannot_reactivate_historical_outer_tower(self):
        raw = source(towers=((1, 12, 3, 0), (15, 12, 3, 0)))
        options = {'target_mode': 'count', 'added_tiles': 0, 'operation': 'existing',
                   'auto_translate': False,
                   'tower_directions': [{'index': 0, 'direction': 2},
                                        {'index': 1, 'direction': 2}]}
        result = build_increase(raw, options, 'moreToriginal')
        self.assertFalse(result['ok'])
        towers = (TowerAddition(0, 1, 12, 2, 3, 3, ()),
                  TowerAddition(1, 15, 12, 2, 3, 3, ()))
        plan = self.plan(raw, towers, **options)
        report = verify_increase(raw, rewrite_increase(raw, plan, 'moreToriginal'),
                                 plan, 'moreToriginal')
        self.assertFalse(report.ok)
        self.assertIn('safe_bounds', {issue.code for issue in report.errors})

    def test_tower_override_uses_final_anchor_after_ordinary_translation(self):
        raw = source(towers=((1, 12, 3, 0), (15, 12, 3, 0)))
        result = build_increase(raw, {
            'target_mode': 'count', 'added_tiles': 0, 'operation': 'existing',
            'translate_x': 1, 'auto_translate': False,
            'tower_positions': [{'index': 0, 'x': 4, 'y': 12},
                                {'index': 1, 'x': 14, 'y': 12}],
        }, 'moreToriginal')
        self.assertTrue(result['ok'], result.get('error'))
        document = decode_template(result['output'])
        self.assertEqual([(s.x, s.y) for s in document.stacks], [(4, 12), (14, 12)])

    def test_impossible_translated_old_tower_does_not_consume_new_layout_budget(self):
        # Ordinary cards still fit after +3Y, but the old tower anchor is Y17.
        # New card positions cannot repair that unchanged three-card minimum.
        raw = source(towers=((8, 14, 3, 2),), points=[(6, 4), (8, 4), (10, 4)])
        result = build_increase(raw, {
            'target_mode': 'count', 'added_tiles': 6, 'operation': 'both',
            'translate_y': 3, 'auto_translate': False,
        }, 'moreToriginal')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'existing_tower_layout_unavailable')
        self.assertIn('原塔', result['error']['message'])
        self.assertIn('安全区', result['error']['message'])

    def test_legal_old_layout_without_new_positions_keeps_distinct_diagnosis(self):
        # Some automatic translations invalidate the old tower, but (0,0) is
        # legal. The selected center is already occupied in every translation.
        raw = source(towers=((8, 14, 3, 2),))
        result = build_increase(raw, {
            'target_mode': 'count', 'added_tiles': 6, 'operation': 'both',
            'regions': ['center'], 'auto_translate': True,
        }, 'moreToriginal')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'new_tower_positions_unavailable')

    def test_unchanged_historical_outer_towers_remain_preserved(self):
        raw = source(towers=((1, 12, 3, 0), (15, 12, 3, 0)))
        result = build_increase(raw, {
            'target_mode': 'manual', 'operation': 'new', 'tower_counts': [3, 3, 6],
            'stack_count': 1, 'auto_translate': False,
            'tower_positions': [{'index': 2, 'x': 8, 'y': 14}],
        }, 'moreToriginal')
        self.assertTrue(result['ok'], result.get('error'))
        before = decode_template(raw)
        after = decode_template(result['output'])
        self.assertEqual([t.fields for t in after.tiles[:60]],
                         [t.fields for t in before.tiles])
        report = verify_increase(raw, result['output'], result['plan'], 'moreToriginal')
        self.assertTrue(report.ok, report.errors)
        self.assertIn('existing_safe_bounds', {issue.code for issue in report.warnings})


if __name__ == '__main__':
    unittest.main()
