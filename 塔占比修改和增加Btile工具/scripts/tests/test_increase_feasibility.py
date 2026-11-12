"""Quantity-independent placement conflicts must finish with actionable facts."""
import unittest

from scripts.tests.test_increase_engine import source
from scripts.tower_reducer.increase import build_increase, normalise_increase_options
from scripts.tower_reducer.verification import verify_output


def blocked_original_towers():
    # Same XY obstruction as the reported five-tower board, with a complete
    # single-layer symmetric ordinary formation rather than private source bytes.
    points = [
        (2, 5), (2, 9), (3, 4), (3, 10), (4, 3), (4, 5), (4, 7),
        (4, 9), (4, 11), (5, 2), (5, 4), (5, 6), (5, 8), (5, 10),
        (5, 12), (6, 1), (6, 5), (6, 7), (6, 9), (6, 13), (7, 4),
        (7, 6), (7, 8), (7, 10), (8, 3), (8, 5), (8, 7), (8, 9),
        (8, 11), (9, 4), (9, 6), (9, 8), (9, 10), (10, 1), (10, 5),
        (10, 7), (10, 9), (10, 13), (11, 2), (11, 4), (11, 6),
        (11, 8), (11, 10), (11, 12), (12, 3), (12, 5), (12, 7),
        (12, 9), (12, 11), (13, 4), (13, 10), (14, 5), (14, 9),
    ]
    return source(total=111, points=points, towers=(
        (2, 13, 15, 2), (4, 15, 9, 2), (8, 15, 10, 2),
        (12, 15, 9, 2), (14, 13, 15, 2)))


def four_way_formation():
    # Every axis position is occupied, so new anchors come in groups of four.
    points = [(x, 9) for x in range(1, 16)] + [(8, y) for y in range(2, 17)]
    return source(total=30, points=points)


class IncreaseFeasibilityTests(unittest.TestCase):
    def build(self, raw, **options):
        return build_increase(raw, {
            'target_mode': 'count', 'added_tiles': 30, 'operation': 'both',
            **options}, 'moreToriginal')

    def test_zero_automatic_count_replays_legacy_empty_count(self):
        raw = source()
        zero = self.build(raw, stack_count=0, added_tiles=6, regions=['bottom'])
        legacy = self.build(raw, stack_count=None, added_tiles=6, regions=['bottom'])
        self.assertTrue(zero['ok'], zero['error'])
        self.assertEqual(zero['output'], legacy['output'])
        self.assertIsNone(zero['plan'].options['stack_count'])

    def test_automatic_alias_does_not_accept_negative_fraction_or_boolean(self):
        for value in (-1, .5, 0.0, False, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalise_increase_options({'stack_count': value})

    def test_old_towers_block_all_new_symmetric_positions(self):
        result = self.build(blocked_original_towers())
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'new_tower_positions_unavailable')
        self.assertIn('位置', result['error']['message'])
        self.assertNotIn('搜索预算', result['error']['message'])
        self.assertIsNone(result['output'])

    def test_fixed_one_or_two_cannot_form_four_way_symmetric_group(self):
        for count in (1, 2):
            with self.subTest(count=count):
                result = self.build(four_way_formation(), stack_count=count)
                self.assertFalse(result['ok'])
                self.assertEqual(result['error']['code'], 'stack_count_symmetry_unavailable')
                self.assertIn('对称', result['error']['message'])
                self.assertIn('0', ' '.join(result['error']['recommendations']))

    def test_towerless_both_keeps_working_with_a_complete_symmetric_group(self):
        result = self.build(four_way_formation(), added_tiles=12, stack_count=4)
        self.assertTrue(result['ok'], result['error'])
        self.assertEqual(result['summary']['added_tiles'], 12)
        self.assertEqual(len(result['plan'].towers), 4)
        self.assertTrue(verify_output(four_way_formation(), result['output'],
                                     result['plan'], 'moreToriginal').ok)

    def test_manual_locked_explicit_positions_keep_authority(self):
        result = self.build(source(), target_mode='manual', tower_counts=[6],
            regions=['bottom'], tower_positions=[{'index': 0, 'x': 8, 'y': 14}],
            tower_directions=[{'index': 0, 'direction': 2}], locked_towers=[0])
        self.assertTrue(result['ok'], result['error'])
        tower = result['plan'].towers[0]
        self.assertEqual((tower.x, tower.y, tower.final_count, tower.direction), (8, 14, 6, 2))

    def test_historical_overlapping_old_towers_do_not_consume_distinct_new_slots(self):
        old = tuple((x, y, 1, 0) for x in range(9) for y in range(9))[:73]
        raw = source(total=108, points=[(14, 12)], towers=old)
        result = self.build(raw, operation='new', added_tiles=3, stack_count=1)
        self.assertTrue(result['ok'], result['error'])
        self.assertEqual(len(result['plan'].towers), 74)
        self.assertEqual(result['summary']['added_tiles'], 3)


if __name__ == '__main__':
    unittest.main()
