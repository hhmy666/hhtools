"""Hand-checked board fixtures guard editing and conservative repair."""

import unittest
from collections import Counter
from unittest.mock import patch

from covercheck.engine import analyze_bytes
from covercheck.wire import parse_fields, scalar
from test_engine import fixture, integer, message

try:
    from covercheck import repair
except ImportError:
    repair = None

auto_repair = getattr(repair, "auto_repair", None)
rewrite_tiles = getattr(repair, "rewrite_tiles", None)
validate_board = getattr(repair, "validate_board", None)


# A sole upper problem tile can move into (7/8/9,8,Z=1), hidden by the
# four Z=2 rectangles. The staggered lower positions avoid a new one-on-one.
SAFE_TILES = [(2, 2, 0, -10), (2, 2, 1, -10),
              (5, 5, 0, -10), (11, 5, 0, -10), (5, 11, 0, -10),
              (11, 11, 0, -10), (8, 7, 0, -10),
              (6, 6, 1, -10), (10, 6, 1, -10), (6, 10, 1, -10), (10, 10, 1, -10),
              (7, 7, 2, -10), (9, 7, 2, -10), (7, 9, 2, -10), (9, 9, 2, -10)]

# Z=3 contains only the removable upper. The retained Z=4 tile needs that
# support, so repair must remove exactly layer 3 and lower Z=4 to Z=3.
WHOLE_LAYER_TILES = SAFE_TILES[2:] + [
    (2, 12, 0, -10), (3, 13, 1, -10), (2, 14, 2, -10),
    (2, 14, 3, -10), (3, 15, 4, -10)]


def tower_board(heights, upper_types):
    towers = [(4, 12, 0), (8, 12, 0), (12, 12, 0)]
    tiles = [(x, y, z, -10) for (x, y, _), height in zip(towers, heights) for z in range(height)]
    for index, tile_id in enumerate(upper_types):
        x = 2 + 3 * index
        tiles.extend([(x, 2, 0, -10), (x, 2, 1, tile_id)])
    return tiles, towers


def surface(tiles):
    """Independent quarter-cell ownership for integer non-tower fixtures."""
    result = {}
    for tile in sorted(tiles, key=lambda point: point[2]):
        x, y, z, tile_id = tile
        for sx in range(x * 4 - 4, x * 4 + 4):
            for sy in range(y * 4 - 4, y * 4 + 4):
                result[sx, sy] = tile
    return result


class RewriteTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(rewrite_tiles, "board rewriting is not implemented")

    def test_edit_retains_unknown_top_level_nested_and_tower_metadata(self):
        first = b"".join(integer(n, value) for n, value in enumerate((2, 2, 0, -10), 1))
        first += message(99, b"tile payload")
        second = b"".join(integer(n, value) for n, value in enumerate((6, 2, 0, -11), 1))
        tower = integer(1, 12) + integer(2, 12) + integer(3, 1) + message(22, b"tower payload")
        raw = message(2, b"old") + message(100, b"top payload") + message(6, first)
        raw += integer(103, 271) + message(7, tower) + message(6, second)
        result = rewrite_tiles(raw, [
            {"index": 0, "x": 3, "y": 2, "z": 0, "tile_id": -10},
            {"index": None, "x": 10, "y": 2, "z": 0, "tile_id": -11},
        ], new_id="new")
        fields = parse_fields(result)
        self.assertEqual(scalar(fields, 2, 2), b"new")
        self.assertEqual(scalar(fields, 100, 2), b"top payload")
        self.assertEqual(scalar(fields, 103, 0), 271)
        self.assertEqual(scalar(fields, 7, 2), tower)
        tile_fields = [parse_fields(f.value) for f in fields if f.number == 6]
        self.assertEqual(scalar(tile_fields[0], 99, 2), b"tile payload")
        self.assertEqual([t["x"] for t in analyze_bytes(result)["tiles"]], [3, 10])
        self.assertEqual(scalar(parse_fields(raw), 2, 2), b"old")

    def test_noop_is_byte_exact_including_noncanonical_unknown_varints(self):
        raw = fixture([(2, 2, 0, -10)]) + b"\xa0\x06\x81\x00"
        self.assertEqual(rewrite_tiles(raw, analyze_bytes(raw)["tiles"]), raw)

    def test_noop_does_not_move_metadata_interleaved_between_tile_messages(self):
        raw = fixture([(2, 2, 0, -10)]) + b"\xa0\x06\x81\x00"
        tile = b"".join(integer(n, value) for n, value in enumerate((6, 2, 0, -10), 1))
        raw += message(6, tile)
        self.assertEqual(rewrite_tiles(raw, analyze_bytes(raw)["tiles"], new_id="fixture"), raw)

    def test_invalid_edit_indices_and_noninteger_coordinates_are_rejected(self):
        raw = fixture([(2, 2, 0, -10)])
        source = {"index": 0, "x": 2, "y": 2, "z": 0, "tile_id": -10}
        for tiles in [[dict(source, index=20)], [source, source], [dict(source, x=2.5)],
                      [dict(source, z=True)], [dict(source, tile_id=1 << 32)]]:
            with self.subTest(tiles=tiles), self.assertRaises(ValueError):
                rewrite_tiles(raw, tiles)

    def test_tower_edit_rewrites_matching_metadata_and_keeps_unknown_fields(self):
        raw = fixture([(12, 12, 0, -10)], [(12, 12, 1)])
        tower_fields = scalar(parse_fields(raw), 7, 2) + message(22, b"tower payload")
        raw = b"".join(message(7, tower_fields) if field.number == 7 else field.raw
                       for field in parse_fields(raw))
        result = rewrite_tiles(raw, [{"index": 0, "x": 10, "y": 14, "z": 0, "tile_id": -10}],
                               towers=[{"index": 0, "x": 10, "y": 14, "direction": 2}])
        value = analyze_bytes(result)
        self.assertEqual(value["towers"], [{"index": 0, "x": 10, "y": 14, "direction": 2}])
        self.assertEqual(value["tiles"][0]["tower_index"], 0)
        self.assertEqual(scalar(parse_fields(scalar(parse_fields(result), 7, 2)), 22, 2), b"tower payload")

    def test_tower_edit_cannot_create_delete_duplicate_or_misidentify_descriptors(self):
        raw = fixture([(12, 12, 0, -10)], [(12, 12, 1)])
        tower = {"index": 0, "x": 12, "y": 12, "direction": 1}
        for towers in ([], [tower, tower], [dict(tower, index=1)], [dict(tower, direction=4)],
                       [dict(tower, x=True)], [dict(tower, index=None)]):
            with self.subTest(towers=towers), self.assertRaises(ValueError):
                rewrite_tiles(raw, analyze_bytes(raw)["tiles"], towers=towers)
        empty = fixture([(2, 2, 0, -10)])
        self.assertEqual(rewrite_tiles(empty, analyze_bytes(empty)["tiles"], towers=[]), empty)
        self.assertEqual(rewrite_tiles(raw, analyze_bytes(raw)["tiles"], towers=[tower]), raw)

    def test_tower_array_order_does_not_replace_original_descriptor_identity(self):
        raw = fixture([(4, 12, 0, -10), (12, 12, 0, -10)], [(4, 12, 0), (12, 12, 1)])
        fields, seen = [], 0
        for field in parse_fields(raw):
            if field.number == 7:
                fields.append(message(7, field.value + message(22, b"left" if seen == 0 else b"right")))
                seen += 1
            else:
                fields.append(field.raw)
        raw = b"".join(fields)
        result = rewrite_tiles(raw, analyze_bytes(raw)["tiles"], towers=[
            {"index": 1, "x": 12, "y": 12, "direction": 2},
            {"index": 0, "x": 4, "y": 12, "direction": 3}])
        descriptors = [parse_fields(field.value) for field in parse_fields(result) if field.number == 7]
        self.assertEqual([scalar(fields, 3, 0) for fields in descriptors], [3, 2])
        self.assertEqual([scalar(fields, 22, 2) for fields in descriptors], [b"left", b"right"])

    def test_unchanged_supplied_tower_retains_its_exact_wire_bytes_during_tile_edit(self):
        raw = fixture([(2, 2, 0, -10)], [(12, 12, 0)])
        tower = scalar(parse_fields(raw), 7, 2)
        original_wire = b"\x3a" + bytes([len(tower) | 128, 0]) + tower
        raw = b"".join(original_wire if field.number == 7 else field.raw for field in parse_fields(raw))
        result = rewrite_tiles(raw, [{"index": 0, "x": 4, "y": 2, "z": 0, "tile_id": -10}],
                               towers=[{"index": 0, "x": 12, "y": 12, "direction": 0}])
        self.assertEqual(next(field.raw for field in parse_fields(result) if field.number == 7), original_wire)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(validate_board, "board validation is not implemented")

    def test_valid_ground_board_and_full_cover_warning_can_be_saved(self):
        raw = fixture([(2, 2, 0, -10), (2, 2, 1, -10), (8, 2, 0, -10)])
        value = validate_board(raw)
        self.assertTrue(value["valid"])
        self.assertEqual(value["errors"], [])
        self.assertEqual(value["analysis"]["adjacent_count"], 1)
        self.assertTrue(value["warnings"])

    def test_arbitrary_int32_coordinates_have_no_safe_range_diagnostic(self):
        self.assertTrue(validate_board(fixture([(2, 2, 0, -10), (14, 2, 0, -10),
                                                (14, 16, 0, -10)]))["valid"])
        for xyz in [(1, 2, 0), (15, 2, 0), (2, 1, 0), (2, 17, 0), (2, 2, -1), (2, 2, 256)]:
            with self.subTest(xyz=xyz):
                value = validate_board(fixture([(*xyz, 99), (100, 100, 0, 99), (104, 100, 0, 99)]))
                self.assertTrue(value["valid"])
                self.assertEqual(value["errors"], [])
                self.assertNotIn("安全范围", " ".join(value["warnings"]))

    def test_same_layer_positive_overlap_warns_but_touching_edges_allowed(self):
        raw = fixture([(2, 2, 0, -10), (3, 3, 0, -10), (10, 2, 0, -10)])
        self.assertTrue(validate_board(raw)["valid"])
        self.assertIn("同层", " ".join(validate_board(raw)["warnings"]))
        self.assertTrue(validate_board(fixture([(2, 2, 0, -10), (4, 2, 0, -10),
                                                (6, 2, 0, -10)]))["valid"])

    def test_ordinary_tiles_need_immediately_lower_positive_support(self):
        for lower, valid in [((3, 3, 0), True), ((4, 2, 0), False), ((3, 3, 1), False)]:
            with self.subTest(lower=lower):
                value = validate_board(fixture([(*lower, -10), (2, 2, 1, -10), (10, 10, 0, -10)]))
                if valid:
                    self.assertTrue(value["valid"])
                else:
                    self.assertTrue(value["valid"])
                    self.assertIn("悬空", " ".join(value["warnings"]))
                    self.assertIn({"index": 1, "x": 2, "y": 2, "z": 1, "layer": 2,
                                   "kind": "unsupported"}, value["analysis"]["support_issues"])

    def test_projected_tower_geometry_provides_support(self):
        # Direction left projects the tower's layer-2 tile from X=4 to 3.81;
        # its positive overlap supports (2,2,Z=2). Edge-only unprojected X=4 cannot.
        for direction, valid in [(1, True), (3, False)]:
            with self.subTest(direction=direction):
                raw = fixture([(4, 2, 1, -10), (2, 2, 2, -10), (10, 10, 0, -10)], [(4, 2, direction)])
                value = validate_board(raw)
                self.assertTrue(value["valid"])
                self.assertEqual(any(issue["index"] == 1 for issue in value["analysis"]["support_issues"]), not valid)

    def test_total_and_each_A_B_count_include_towers_and_other_types_have_no_group_rule(self):
        self.assertTrue(validate_board(fixture([(2, 2, 0, 99), (6, 2, 0, 100),
                                                (10, 2, 0, 101)]))["valid"])
        for tiles, diagnostic in [([(2, 2, 0, 99)], '全部牌共1张'),
                                  ([(2, 2, 0, -10), (6, 2, 0, 99), (10, 2, 0, 99)], 'A牌共1张'),
                                  ([(2, 2, 0, -11), (6, 2, 0, 99), (10, 2, 0, 99)], 'B牌共1张')]:
            with self.subTest(diagnostic=diagnostic):
                value = validate_board(fixture(tiles))
                self.assertFalse(value['valid'])
                self.assertIn(diagnostic, ' '.join(value['errors']))
                self.assertNotIn('三张成组', ' '.join(value['warnings']))
        raw = fixture([(2, 2, 0, -10), (2, 2, 1, -10), (6, 2, 0, -10)], [(2, 2, 0)])
        self.assertTrue(validate_board(raw)["valid"])

    def test_malformed_wire_returns_invalid_instead_of_raising(self):
        for raw in [b"bad", b"", message(2, b"x") + message(6, integer(1, 0)),
                    message(2, b"x") + message(6, integer(1, 1 << 32) + integer(2, 0) + integer(4, -10))]:
            with self.subTest(raw=raw):
                value = validate_board(raw)
                self.assertFalse(value["valid"])
                self.assertTrue(value["errors"])

    def test_empty_board_and_duplicate_coordinates_remain_saveable(self):
        for raw in (fixture([]), fixture([(2, 2, 0, 99), (2, 2, 0, 100), (8, 2, 0, 101)])):
            value = validate_board(raw)
            self.assertTrue(value["valid"], value["errors"])
            self.assertEqual(value["errors"], [])

    def test_support_diagnostics_include_projected_tower_members(self):
        raw = fixture([(4, 8, 0, 99), (4, 8, 2, 99), (4, 8, 3, 99)], [(4, 8, 0)])
        self.assertEqual(validate_board(raw)["analysis"]["support_issues"], [
            {"index": 1, "x": 4, "y": 8, "z": 2, "layer": 3, "kind": "unsupported"}])


class AutoRepairTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(auto_repair, "conservative repair is not implemented")

    def assert_lowimpact(self, raw, result):
        from covercheck.lowimpact import audit_repair
        audit = audit_repair(raw, result['raw'], result.get('origin_indices'))
        self.assertTrue(audit['valid'], audit['errors'])
        return audit['metrics']

    def test_moved_replacements_keep_each_source_tile_unknown_payload_in_hidden_and_tower_slots(self):
        tower_tiles, towers = tower_board((5, 5, 5), (-11, -11, -11))
        for tiles, descriptors in ((SAFE_TILES, []), (tower_tiles, towers)):
            with self.subTest(towers=descriptors):
                source, tile_index = [], 0
                for field in parse_fields(fixture(tiles, descriptors)):
                    if field.number == 6:
                        payload = "source-{}".format(tile_index).encode()
                        source.append(message(6, field.value + message(99, payload)))
                        tile_index += 1
                    else:
                        source.append(field.raw)
                raw = b"".join(source)
                value = auto_repair(raw)
                self.assertEqual(value["status"], "repaired", value["reason"])
                payloads = [scalar(parse_fields(field.value), 99, 2)
                            for field in parse_fields(value["raw"]) if field.number == 6]
                self.assertEqual(payloads, ["source-{}".format(index).encode() if index is not None else None
                                            for index in value['origin_indices']])
                self.assert_lowimpact(raw, value)

    def test_repair_returns_unique_input_origins_in_output_order_for_each_strategy(self):
        tower_tiles, towers = tower_board((5, 5, 5), (-11, -11, -11))
        cases = [(SAFE_TILES, [], [0] + list(range(2, 15)) + [1]),
                 (tower_tiles, towers, list(range(15)) + [16, 18, 20]),
                 (WHOLE_LAYER_TILES, [], list(range(16)) + [17, 16])]
        for tiles, descriptors, expected_origins in cases:
            with self.subTest(towers=descriptors, count=len(tiles)):
                value = auto_repair(fixture(tiles, descriptors))
                self.assertEqual(value["status"], "repaired", value["reason"])
                self.assertEqual(value.get("origin_indices"), expected_origins)
                known = [i for i in value['origin_indices'] if i is not None]
                self.assertEqual(len(known), len(set(known)))
                self.assert_lowimpact(fixture(tiles, descriptors), value)
                output = analyze_bytes(value["raw"])["tiles"]
                self.assertEqual([tile["tile_id"] for tile in output],
                                 [tiles[index][3] for index in value["origin_indices"]])

    def test_safe_repair_preserves_counts_types_and_nonproblem_source_positions(self):
        raw = fixture(SAFE_TILES) + message(80, b"metadata")
        source = bytes(raw)
        value = auto_repair(raw)
        self.assertEqual(value["status"], "repaired", value["reason"])
        self.assertEqual((value["removed_count"], value["added_count"]), (1, 1))
        checked = validate_board(value["raw"])
        self.assertTrue(checked["valid"], checked["errors"])
        self.assertEqual(checked["analysis"]["issues"], [])
        points = [(t["x"], t["y"], t["z"], t["tile_id"]) for t in checked["analysis"]["tiles"]]
        expected_kept = set(SAFE_TILES) - {(2, 2, 1, -10)}
        self.assertTrue(expected_kept <= set(points))
        self.assertIn(next(iter(set(points) - expected_kept)),
                      {(7, 8, 1, -10), (8, 8, 1, -10), (9, 8, 1, -10)})
        self.assertEqual(Counter(t[3] for t in points), {-10: 15})
        self.assertEqual(scalar(parse_fields(value["raw"]), 80, 2), b"metadata")
        self.assertEqual(raw, source)

    def test_normal_tower_bytes_are_unchanged_and_do_not_become_problems(self):
        tiles = SAFE_TILES + [(14, 14, z, -11) for z in (0, 1, 2)]
        raw = fixture(tiles, [(14, 14, 2)])
        value = auto_repair(raw)
        self.assertEqual(value["status"], "repaired", value["reason"])
        self.assertEqual(scalar(parse_fields(value["raw"]), 7, 2), scalar(parse_fields(raw), 7, 2))
        towers = [t for t in analyze_bytes(value["raw"])["tiles"] if t["tower_index"] is not None]
        self.assertEqual([(t["x"], t["y"], t["z"], t["tile_id"]) for t in towers],
                         [(14, 14, z, -11) for z in (0, 1, 2)])

    def test_alternative_lower_relocation_can_repair_without_breaking_upper_support(self):
        raw = fixture(SAFE_TILES + [(3, 3, 2, -10), (12, 2, 0, -10), (14, 4, 0, -10)])
        self.assertTrue(validate_board(raw)["valid"])
        value = auto_repair(raw)
        self.assertEqual(value["status"], "repaired", value['reason'])
        self.assert_lowimpact(raw, value)
        by_origin = dict(zip(value['origin_indices'], analyze_bytes(value['raw'])['tiles']))
        self.assertEqual((by_origin[15]['x'], by_origin[15]['y'], by_origin[15]['z']), (3, 3, 2))
        self.assertEqual((by_origin[0]['x'], by_origin[0]['y'], by_origin[0]['z']), (3, 3, 0))

    def test_preexisting_unsupported_tile_is_unchanged_while_fullcover_is_repaired(self):
        raw = fixture(SAFE_TILES + [(14, 2, 4, -10), (12, 2, 0, -10), (14, 4, 0, -10)])
        value = auto_repair(raw)
        self.assertEqual(value["status"], "repaired", value['reason'])
        output = dict(zip(value['origin_indices'], analyze_bytes(value['raw'])['tiles']))
        self.assertEqual(tuple(output[15][key] for key in ('x', 'y', 'z', 'tile_id')), (14, 2, 4, -10))
        self.assert_lowimpact(raw, value)

    def test_floating_problem_upper_can_be_removed_before_support_validation(self):
        # The identified upper tile is intentionally unsupported. Rejecting the
        # source before removing it would make every floating-cover repair fail.
        for upper_z in (2, 3, 18):
            with self.subTest(upper_z=upper_z):
                tiles = [(x, y, upper_z if (x, y, z) == (2, 2, 1) else z, tile_id)
                         for x, y, z, tile_id in SAFE_TILES]
                raw = fixture(tiles)
                self.assertTrue(validate_board(raw)["valid"])
                self.assertTrue(validate_board(raw)['analysis']['support_issues'])
                self.assertEqual(analyze_bytes(raw)["floating_count"], 1)
                value = auto_repair(raw)
                self.assertEqual(value["status"], "repaired", value["reason"])
                self.assertEqual((value["removed_count"], value["added_count"]), (1, 1))
                checked = validate_board(value["raw"])
                self.assertTrue(checked["valid"], checked["errors"])
                self.assertEqual(checked["analysis"]["issues"], [])
                points = [(t["x"], t["y"], t["z"], t["tile_id"])
                          for t in checked["analysis"]["tiles"]]
                self.assertEqual(Counter(t[3] for t in points), Counter(t[3] for t in tiles))
                self.assertEqual(surface(points).keys(), surface(tiles).keys())

    def test_original_float_and_arbitrary_bounds_do_not_hide_machine_conflict_guard(self):
        for extra in ((14, 2, 4, -10), (1, 2, 0, -10), (6, 5, 0, -10)):
            with self.subTest(extra=extra):
                tiles = [(x, y, 2 if (x, y, z) == (2, 2, 1) else z, tile_id)
                         for x, y, z, tile_id in SAFE_TILES]
                raw = fixture(tiles + [extra, (12, 2, 0, -10), (14, 4, 0, -10)])
                value = auto_repair(raw)
                if extra == (6, 5, 0, -10):
                    self.assertEqual(value["status"], "unrepairable")
                    self.assertEqual(value["raw"], raw)
                else:
                    self.assertEqual(value['status'], 'repaired', value['reason'])
                    self.assert_lowimpact(raw, value)

    def test_no_lower_middle_hidden_slot_reports_exhausted_scope_and_retains_source(self):
        raw = fixture([(2, 2, 0, -10), (2, 2, 1, -10), (8, 2, 0, -10)])
        value = auto_repair(raw)
        self.assertEqual(value["status"], "unrepairable")
        self.assertFalse(value["search_complete"])
        self.assertEqual(value["raw"], raw)

    def test_adding_a_new_adjacent_full_cover_is_not_a_legal_slot(self):
        tiles = [((8, 8, 0, -10) if point == (8, 7, 0, -10) else point) for point in SAFE_TILES]
        raw = fixture(tiles)
        value = auto_repair(raw)
        self.assertEqual(value["status"], "repaired", value["reason"])
        result = analyze_bytes(value["raw"])
        self.assertFalse(result["issues"])
        self.assertNotIn((8, 8, 1), [(t["x"], t["y"], t["z"]) for t in result["tiles"]])

    def test_budget_exhaustion_is_not_reported_as_proven_absence(self):
        raw = fixture(SAFE_TILES)
        with patch.object(repair, "SEARCH_NODE_LIMIT", 0):
            value = auto_repair(raw)
        self.assertEqual(value["status"], "unrepairable")
        self.assertFalse(value["search_complete"])
        self.assertIn("预算", value["reason"])
        self.assertEqual(value["raw"], raw)

    def test_clean_valid_board_is_unchanged_byte_for_byte(self):
        raw = fixture([(2, 2, 0, -10), (6, 2, 0, -10), (10, 2, 0, -10)])
        value = auto_repair(raw)
        self.assertEqual(value["status"], "unchanged")
        self.assertEqual(value["raw"], raw)
        self.assertEqual((value["removed_count"], value["added_count"]), (0, 0))

    def test_multiple_candidate_search_backtracks_and_replaces_each_removed_type(self):
        # First safe slot (8,7,1) conflicts with all other slots. A greedy
        # placement fails; the successful pair is (7,8,1) and (9,8,1).
        tiles = [((8, 6, 0, -10) if point == (8, 7, 0, -10) else point) for point in SAFE_TILES]
        tiles += [(8, 9, 0, -10), (14, 2, 0, -10), (14, 4, 0, -10),
                  (2, 14, 0, -10), (2, 14, 1, -10), (2, 8, 0, -10)]
        b_points = {(5, 5, 0), (11, 5, 0), (2, 14, 1)}
        tiles = [(x, y, z, -11 if (x, y, z) in b_points else tile_id) for x, y, z, tile_id in tiles]
        raw = fixture(tiles)
        value = auto_repair(raw)
        self.assertEqual(value["status"], "repaired", value["reason"])
        result = analyze_bytes(value["raw"])
        points = [(t["x"], t["y"], t["z"], t["tile_id"]) for t in result["tiles"]]
        additions = set(points) - set(tiles)
        self.assertEqual({point[:3] for point in additions}, {(7, 8, 1), (9, 8, 1), (2, 14, 0)})
        self.assertEqual(Counter(point[3] for point in points), {-10: 18, -11: 3})
        self.assertEqual(Counter(point[3] for point in additions), {-10: 2, -11: 1})
        self.assertEqual((value["removed_count"], value["added_count"]), (4, 4))
        self.assert_lowimpact(raw, value)

    def test_silhouette_and_other_original_exposed_faces_are_unchanged(self):
        raw = fixture(SAFE_TILES)
        value = auto_repair(raw)
        self.assertEqual(value["status"], "repaired", value["reason"])
        result = analyze_bytes(value["raw"])
        points = [(t["x"], t["y"], t["z"], t["tile_id"]) for t in result["tiles"]]
        before, after = surface(SAFE_TILES), surface(points)
        self.assertEqual(before.keys(), after.keys())
        for position, owner in before.items():
            with self.subTest(position=position):
                if owner == (2, 2, 1, -10):
                    self.assertEqual(after[position], (2, 2, 0, -10))
                else:
                    self.assertEqual(after[position], owner)

    def test_stacked_problem_chain_removes_each_upper_once(self):
        raw = fixture(SAFE_TILES + [(2, 2, 2, -10), (14, 2, 0, -10), (14, 4, 0, -10)])
        value = auto_repair(raw)
        self.assertEqual(value["status"], "repaired", value["reason"])
        self.assertEqual((value["removed_count"], value["added_count"]), (2, 2))
        self.assertEqual(analyze_bytes(value["raw"])["tile_count"], 18)

    def test_original_middle_layer_remains_available_after_its_only_problem_tile_is_removed(self):
        # Four sparse normal towers provide high cover without relying on the
        # removed ordinary layer. Their Z=2 projections shift Y by -32/85.
        towers = [(7, 7, 0), (9, 7, 0), (7, 9, 0), (9, 9, 0)]
        tiles = [(2, 2, 0, -10), (2, 2, 1, -10), (8, 7, 0, -10),
                 (12, 2, 0, -10), (14, 4, 0, -10)]
        tiles += [(x, y, 2, -10) for x, y, direction in towers]
        raw = fixture(tiles, towers)
        value = auto_repair(raw)
        self.assertEqual(value["status"], "repaired", value["reason"])
        # These supports share the existing tower anchors: all four count as
        # tower-member changes, even though the new members are hidden below.
        self.assertEqual((value["removed_count"], value["added_count"]), (1, 4))
        self.assertEqual(self.assert_lowimpact(raw, value)['tower_changed_count'], 4)
        self.assertFalse(analyze_bytes(value["raw"])["issues"])

    def test_wholly_problem_layer_can_be_removed_and_only_that_layer_compressed(self):
        raw = fixture(WHOLE_LAYER_TILES)
        self.assertTrue(validate_board(raw)["valid"])
        value = auto_repair(raw)
        self.assertEqual(value["status"], "repaired", value["reason"])
        checked = validate_board(value["raw"])
        self.assertTrue(checked["valid"], checked["errors"])
        self.assertEqual(checked["analysis"]["issues"], [])
        points = {(t["x"], t["y"], t["z"], t["tile_id"]) for t in checked["analysis"]["tiles"]}
        self.assertIn((3, 15, 3, -10), points)
        self.assertNotIn((3, 15, 4, -10), points)
        self.assertNotIn((2, 14, 3, -10), points)
        self.assertEqual(len(points), len(WHOLE_LAYER_TILES))
        self.assertEqual((value["removed_count"], value["added_count"]), (2, 2))
        self.assertEqual(self.assert_lowimpact(raw, value)['compressed_problem_layers'], [4])

    def test_layer_compression_preserves_other_original_empty_layer_gaps(self):
        raw = fixture(WHOLE_LAYER_TILES + [(14, 14, z, -10) for z in (7, 8, 9)], [(14, 14, 3)])
        value = auto_repair(raw)
        # A separate pre-existing empty gap must not be silently collapsed to
        # make a floating tower appear valid under the new all-tile check.
        self.assertEqual(value["status"], "unrepairable", value["reason"])
        self.assertEqual(value['raw'], raw)
        analysis = analyze_bytes(raw)
        variants = list(repair._removal_variants(analysis, {i['upper_index'] for i in analysis['issues']}, repair._Budget()))
        compressed = next(tiles for tiles, layers in variants if layers == (3,))
        self.assertEqual([t['z'] for t in compressed if t['tower_index'] == 0], [6, 7, 8])

    def test_layer_compression_that_moves_tower_projection_over_other_faces_fails_exactly(self):
        raw = fixture(WHOLE_LAYER_TILES + [(8, 8, z, -10) for z in (6, 7, 8)], [(8, 8, 0)])
        self.assertTrue(validate_board(raw)["valid"])
        value = auto_repair(raw)
        self.assertEqual(value["status"], "unrepairable", value["reason"])
        self.assertEqual(value["raw"], raw)

    def test_non_tower_solution_is_preferred_to_equal_tower_growth(self):
        tiles, towers = tower_board((5, 5, 5), (-11, -11, -11))
        raw = fixture(tiles, towers)
        value = auto_repair(raw)
        self.assertEqual(value["status"], "repaired", value["reason"])
        checked = validate_board(value["raw"])
        self.assertTrue(checked["valid"], checked["errors"])
        result = checked["analysis"]
        self.assertEqual(result["issues"], [])
        self.assertEqual([sum(t["tower_index"] == index for t in result["tiles"]) for index in range(3)], [5, 5, 5])
        self.assertEqual(Counter(t["tile_id"] for t in result["tiles"]), {-10: 15, -11: 3})
        self.assertEqual(self.assert_lowimpact(raw, value)['tower_changed_count'], 0)
        self.assertEqual([f.raw for f in parse_fields(value["raw"]) if f.number == 7],
                         [f.raw for f in parse_fields(raw) if f.number == 7])

    def test_unequal_towers_keep_rank_and_equal_groups_without_preserving_difference_ratios(self):
        tiles, towers = tower_board((2, 4, 2), (-11, -11, -11, -10, -10))
        raw = fixture(tiles, towers)
        value = auto_repair(raw)
        self.assertEqual(value["status"], "repaired", value["reason"])
        checked = validate_board(value["raw"])
        self.assertTrue(checked["valid"], checked["errors"])
        result = checked["analysis"]
        self.assertEqual(result["issues"], [])
        self.assertEqual([sum(t["tower_index"] == index for t in result["tiles"]) for index in range(3)], [3, 4, 3])
        self.assertEqual(Counter(t["tile_id"] for t in result["tiles"]), {-10: 12, -11: 3})
        self.assertLessEqual(self.assert_lowimpact(raw, value)['tower_changed_count'], 6)

    def test_uneven_tower_growth_cannot_break_original_equal_height_group(self):
        tiles, towers = tower_board((2, 2, 2), (-10,))
        # Eight source tiles need a ninth ground tile to satisfy the group rule.
        raw = fixture(tiles + [(14, 16, 0, -10)], towers)
        value = auto_repair(raw)
        self.assertEqual(value["status"], "unrepairable")
        self.assertFalse(value["search_complete"])
        self.assertEqual(value["raw"], raw)

    def test_tower_growth_cannot_cover_an_existing_ordinary_exposed_face(self):
        tiles, towers = tower_board((5, 5, 5), (-11, -11, -11))
        towers = [(x, y, 1) for x, y, _ in towers]
        raw = fixture(tiles + [(2, 12, 0, -10), (6, 16, 0, -10), (10, 16, 0, -10)], towers)
        self.assertTrue(validate_board(raw)["valid"])
        value = auto_repair(raw)
        self.assertEqual(value["status"], "repaired", value["reason"])
        self.assertEqual(self.assert_lowimpact(raw, value)['tower_changed_count'], 0)

    def test_tower_group_and_hidden_slot_can_share_the_exact_replacement_balance(self):
        tiles = SAFE_TILES + [(6, 2, 0, -10), (6, 2, 1, -10), (10, 2, 0, -10),
                              (10, 2, 1, -10), (14, 2, 0, -10)]
        tiles += [(x, 14, z, -10) for x in (2, 14) for z in (0, 1)]
        raw = fixture(tiles, [(2, 14, 0), (14, 14, 0)])
        value = auto_repair(raw)
        self.assertEqual(value["status"], "repaired", value["reason"])
        result = validate_board(value["raw"])
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["analysis"]["issues"], [])
        result_tiles = result["analysis"]["tiles"]
        self.assertEqual([sum(t["tower_index"] == index for t in result_tiles) for index in range(2)], [2, 2])
        self.assertEqual(Counter(t["tile_id"] for t in result_tiles), {-10: 24})
        self.assertEqual((value["removed_count"], value["added_count"]), (3, 3))
        self.assert_lowimpact(raw, value)

    def test_empty_tower_descriptor_cannot_be_used_to_start_a_new_tower(self):
        raw = fixture([(2, 2, 0, -10), (2, 2, 1, -10), (8, 2, 0, -10)], [(12, 12, 0)])
        value = auto_repair(raw)
        self.assertEqual(value["status"], "unrepairable")
        self.assertEqual(value["raw"], raw)

    def test_existing_high_tower_gaps_do_not_prevent_ordinary_fullcover_repair(self):
        raw = fixture([(2, 2, 0, -10), (2, 2, 1, -10), (6, 2, 0, -10),
                       (10, 2, 0, -10), (12, 12, 1, -10), (12, 12, 255, -10)], [(12, 12, 0)])
        self.assertTrue(validate_board(raw)["valid"])
        value = auto_repair(raw)
        self.assertEqual(value['status'], 'repaired', value['reason'])
        self.assert_lowimpact(raw, value)


if __name__ == "__main__":
    unittest.main()
