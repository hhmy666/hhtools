import unittest
from copy import deepcopy

from covercheck.engine import analyze_bytes
from covercheck.repair import rewrite_tiles
from test_engine import fixture, message
from test_repair import SAFE_TILES, WHOLE_LAYER_TILES

try:
    from covercheck.lowimpact import audit_layout, audit_repair
except ImportError:
    audit_layout = audit_repair = None


def hidden_clusters():
    return [(x + dx, y, z, -10) for x, y in [(4, 3), (10, 3), (4, 9), (10, 9)]
            for dx, z in [(0, 0), (-1, 1), (1, 1)]]


def tower_case(growth):
    towers = [(4, 12, 0), (8, 12, 0), (12, 12, 0)]
    tiles = [(x, y, z, -10) for x, y, _ in towers for z in range(5)]
    tiles += [(x, 2, z, -10) for x in [2, 7, 12] for z in range(growth + 1)]
    original = analyze_bytes(fixture(tiles, towers))
    kept = [dict(t) for t in original['tiles'] if t['tower_index'] is not None or t['z'] == 0]
    kept += [dict(index=None, x=x, y=y, z=z, tile_id=-10, tower_index=i)
             for i, (x, y, _) in enumerate(towers) for z in range(5, 5 + growth)]
    return original, kept


class LowImpactAuditTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(audit_layout, 'low-impact audit not implemented')

    def test_equal_bbox_is_not_equal_ordinary_outline(self):
        original = analyze_bytes(fixture([(2, 2, 0, -10), (10, 10, 0, -10), (6, 2, 0, -10)]))
        tiles = deepcopy(original['tiles']); tiles[2]['y'] = 4
        result = audit_layout(original, tiles)
        self.assertFalse(result['valid'])
        self.assertTrue(any('外轮廓' in e for e in result['errors']))

    def test_same_type_recreation_cannot_hide_four_nonproblem_changes(self):
        original = analyze_bytes(fixture(hidden_clusters()))
        for count in (3, 4):
            tiles = deepcopy(original['tiles'])
            for index in [0, 3, 6, 9][:count]:
                tiles[index]['index'] = None
            result = audit_layout(original, tiles)
            self.assertEqual(result['metrics']['hidden_changed_count'], count)
            self.assertEqual(result['valid'], count == 3, result['errors'])

    def test_delete_three_same_type_problem_tiles_can_keep_surface_and_grouping(self):
        raw = fixture(SAFE_TILES + [(2, 2, 2, -10), (2, 2, 3, -10), (14, 2, 0, -10)])
        original = analyze_bytes(raw)
        tiles = [t for t in original['tiles'] if not (t['x'] == t['y'] == 2 and t['z'] > 0)]
        result = audit_layout(original, tiles)
        self.assertTrue(result['valid'], result['errors'])
        self.assertEqual(result['metrics']['tile_delta'], -3)
        self.assertEqual(result['metrics']['type_deltas'], {'-10': -3})

    def test_deleting_six_is_rejected_even_when_face_and_outline_match(self):
        original = analyze_bytes(fixture([(2, 2, z, -10) for z in range(7)] + [(8, 2, 0, -10), (12, 2, 0, -10)]))
        result = audit_layout(original, [t for t in original['tiles'] if t['z'] == 0])
        self.assertFalse(result['valid'])
        self.assertTrue(any('数量' in e for e in result['errors']))

    def test_tower_own_outline_can_grow_but_six_member_limit_is_enforced(self):
        for growth in (1, 2, 3):
            original, tiles = tower_case(growth)
            result = audit_layout(original, tiles)
            self.assertEqual(result['metrics']['tower_changed_count'], growth * 3)
            self.assertEqual(result['valid'], growth <= 2, result['errors'])

    def test_exposed_nonproblem_tile_cannot_move_even_if_top_pattern_is_same(self):
        original = analyze_bytes(fixture([(4, 4, 0, -10), (8, 4, 0, -10), (12, 4, 0, -10)]))
        tiles = deepcopy(original['tiles']); tiles[0]['index'], tiles[1]['index'] = 1, 0
        result = audit_layout(original, tiles)
        self.assertFalse(result['valid'])
        self.assertTrue(any('露出' in e for e in result['errors']))

    def test_lower_type_cannot_replace_different_exposed_problem_type(self):
        original = analyze_bytes(fixture([(4, 4, 0, -10), (4, 4, 1, -11), (8, 4, 0, -10)]))
        tiles = [dict(t) for t in original['tiles'] if t['index'] != 1]
        result = audit_layout(original, tiles)
        self.assertFalse(result['valid'])
        self.assertTrue(any('表面' in e for e in result['errors']))

    def test_existing_tower_gaps_are_preserved_without_requiring_repair(self):
        original = analyze_bytes(fixture([(4, 8, z, -10) for z in (0, 2, 3)], [(4, 8, 0)]))
        result = audit_layout(original, original['tiles'])
        self.assertTrue(result['valid'], result['errors'])

    def test_original_unsupported_positions_and_arbitrary_coordinates_are_allowed(self):
        original = analyze_bytes(fixture([(-7, 28, 300, 99), (6, 2, 0, 99), (10, 2, 0, 99)]))
        self.assertTrue(audit_layout(original, original['tiles'])['valid'])

    def test_original_float_exemption_cannot_be_recreated_or_reassigned(self):
        original = analyze_bytes(fixture([(4, 4, 2, 99), (8, 4, 0, 99), (12, 4, 0, 99)]))
        for changes in ({'index': None}, {'index': 1}, {'z': 3}, {'tile_id': 100}):
            tiles = deepcopy(original['tiles'])
            tiles[0].update(changes)
            result = audit_layout(original, tiles)
            self.assertFalse(result['valid'])
            if changes != {'index': 1}:
                self.assertTrue(any('悬空' in error for error in result['errors']), result['errors'])

    def test_deleting_support_cannot_relabel_a_new_float_as_original(self):
        # Removing a hidden ground tile preserves XY outline and exposed faces,
        # so the independent support guard must catch the newly unsupported upper.
        original = analyze_bytes(fixture([(4, 4, 0, 99), (3, 4, 1, 99), (5, 4, 1, 99),
                                          (10, 10, 0, 99), (12, 10, 0, 99), (14, 10, 0, 99)]))
        result = audit_layout(original, [dict(t, index=None) if t['index'] == 0 else dict(t) for t in original['tiles']])
        self.assertTrue(result['valid'], result['errors'])
        # Keep grouping while moving the support out of its role.
        tiles = deepcopy(original['tiles']); tiles[0].update(x=10, y=14)
        result = audit_layout(original, tiles)
        self.assertFalse(result['valid'])
        self.assertTrue(any('悬空' in e for e in result['errors']))

    def test_machine_grouping_and_same_layer_conflicts_remain_hard_guards(self):
        for tiles, diagnostic in [([(4, 4, 0, 99)], '三张成组'),
                                  ([(4, 4, 0, -10), (8, 4, 0, -11), (12, 4, 0, -11)], 'A牌'),
                                  ([(4, 4, 0, 99), (5, 4, 0, 99), (12, 4, 0, 99)], '同层')]:
            original = analyze_bytes(fixture(tiles))
            result = audit_layout(original, original['tiles'])
            self.assertFalse(result['valid'])
            self.assertTrue(any(diagnostic in e for e in result['errors']), result['errors'])

    def test_machine_coordinates_still_require_int32(self):
        original = analyze_bytes(fixture([(4, 4, 0, 99), (8, 4, 0, 99), (12, 4, 0, 99)]))
        tiles = deepcopy(original['tiles']); tiles[0]['x'] = 1 << 32
        self.assertFalse(audit_layout(original, tiles)['valid'])

    def test_tower_shrink_cannot_reveal_unchanged_hidden_ordinary_face(self):
        tiles = [(4, 8, z, 99) for z in range(12)] + [(6, 8, 0, 99), (12, 2, 0, 99), (12, 6, 0, 99)]
        original = analyze_bytes(fixture(tiles, [(4, 8, 3)]))
        result = audit_layout(original, [t for t in original['tiles'] if t['index'] not in (9, 10, 11)])
        self.assertFalse(result['valid'])
        self.assertFalse(result['metrics']['ordinary_surface_unchanged'])

    def test_tower_growth_cannot_cover_original_ordinary_exposed_face(self):
        original, tiles = tower_case(1)
        # Right-facing tower growth covers an extra sliver at X=6, Y=12.
        for tower in original['towers']:
            tower['direction'] = 3
        for x, y in [(6, 12), (6, 3), (12, 3)]:
            tile = dict(index=len(original['tiles']), x=x, y=y, z=0, tile_id=-10, tower_index=None)
            original['tiles'].append(tile)
            tiles.append(dict(tile))
        result = audit_layout(original, tiles)
        self.assertFalse(result['valid'])
        self.assertFalse(result['metrics']['ordinary_surface_unchanged'])

    def test_wire_audit_rejects_unknown_metadata_changes_and_forged_origin(self):
        raw = fixture([(4, 4, 0, -10), (8, 4, 0, -10), (12, 4, 0, -10)]) + message(80, b'private payload')
        self.assertTrue(audit_repair(raw, raw, [0, 1, 2])['valid'])
        self.assertFalse(audit_repair(raw, raw + message(81, b'injected'), [0, 1, 2])['valid'])
        self.assertFalse(audit_repair(raw, raw, [0, 0, 2])['valid'])
        self.assertFalse(audit_repair(raw, raw, [0, 1])['valid'])

    def test_whole_problem_layer_compression_preserves_visible_face_and_counts_other_changes(self):
        original = analyze_bytes(fixture(WHOLE_LAYER_TILES))
        tiles = [dict(t, z=3) if t['index'] == 17 else dict(t) for t in original['tiles'] if t['index'] != 16]
        tiles.append(dict(original['tiles'][16], x=8, y=8, z=1))
        result = audit_layout(original, tiles)
        self.assertTrue(result['valid'], result['errors'])
        self.assertEqual(result['metrics']['compressed_problem_layers'], [4])
        self.assertEqual(result['metrics']['hidden_changed_count'], 1)


if __name__ == '__main__':
    unittest.main()
