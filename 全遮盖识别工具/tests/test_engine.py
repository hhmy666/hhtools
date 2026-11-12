"""Hand-built wire fixtures exercise the visible detector contract."""

import csv
import io
import random
import unittest

try:
    from covercheck.engine import analyze_bytes, results_csv
except ModuleNotFoundError:
    analyze_bytes = results_csv = None


def vint(value):
    value &= (1 << 64) - 1
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    return bytes(out + bytes([value]))


def integer(number, value):
    return vint(number << 3) + vint(value)


def message(number, value):
    return vint((number << 3) | 2) + vint(len(value)) + value


def fixture(tiles, towers=(), template_id="fixture"):
    raw = message(2, template_id.encode())
    for x, y, z, tile_id in tiles:
        raw += message(6, b"".join(integer(i, v) for i, v in enumerate((x, y, z, tile_id), 1)))
    for x, y, direction in towers:
        raw += message(7, b"".join(integer(i, v) for i, v in enumerate((x, y, direction), 1)))
    return raw


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(analyze_bytes, "standalone analyzer is not implemented")

    def test_reports_adjacent_layers_one_based_and_all_ordinary_types(self):
        value = analyze_bytes(fixture([(3, -2, 0, -10), (3, -2, 1, 99), (3, -2, 2, -11)]))
        self.assertEqual((value["adjacent_count"], value["floating_count"], value["coordinate_count"]), (2, 0, 1))
        self.assertEqual([(i["lower_layer"], i["upper_layer"]) for i in value["issues"]], [(1, 2), (2, 3)])
        self.assertEqual(value["issues"][0], {"kind": "adjacent", "x": 3, "y": -2, "lower_layer": 1, "upper_layer": 2, "lower_index": 0, "upper_index": 1})

    def test_floating_quarter_half_and_full_middle_overlap_blocks(self):
        for x, y in [(1, 1), (1, 0), (0, 0)]:
            with self.subTest(x=x, y=y):
                value = analyze_bytes(fixture([(0, 0, 0, -10), (0, 0, 3, -10), (x, y, 1, -11)]))
                self.assertNotIn((0, 1), [(i["lower_index"], i["upper_index"]) for i in value["issues"]])

    def test_floating_edge_contact_and_unrelated_middle_tile_do_not_block(self):
        for x, y in [(2, 0), (0, 2), (2, 2), (4, 0)]:
            with self.subTest(x=x, y=y):
                value = analyze_bytes(fixture([(0, 0, 0, -10), (0, 0, 3, -10), (x, y, 1, -11)]))
                self.assertEqual(value["floating_count"], 1)

    def test_middle_layer_at_same_xy_prevents_cross_pair(self):
        value = analyze_bytes(fixture([(0, 0, 0, -10), (0, 0, 2, -10), (0, 0, 5, -10)]))
        self.assertEqual([(i["lower_layer"], i["upper_layer"]) for i in value["issues"]], [(1, 3), (3, 6)])

    def test_empty_intermediate_layers_keep_real_layer_numbers(self):
        value = analyze_bytes(fixture([(4, 7, 15, -10), (4, 7, 18, -11)]))
        self.assertEqual((value["adjacent_count"], value["floating_count"]), (0, 1))
        self.assertEqual((value["issues"][0]["lower_layer"], value["issues"][0]["upper_layer"]), (16, 19))

    def test_normal_tower_tiles_are_excluded_as_candidates(self):
        value = analyze_bytes(fixture([(0, 0, 0, -10), (0, 0, 1, -10), (5, 0, 0, -10), (5, 0, 1, -10)], [(0, 0, 0)]))
        self.assertEqual((value["tile_count"], value["tower_count"], value["adjacent_count"]), (4, 1, 1))
        self.assertEqual([t["tower_index"] for t in value["tiles"]], [0, 0, None, None])
        self.assertEqual(value["issues"][0]["x"], 5)

    def test_projected_tower_position_can_block_floating_pair(self):
        for direction, expected in [(1, 0), (3, 1)]:
            with self.subTest(direction=direction):
                value = analyze_bytes(fixture([(0, 0, 0, -10), (0, 0, 3, -10), (2, 0, 1, -10)], [(2, 0, direction)]))
                self.assertEqual(value["floating_count"], expected)

    def test_duplicate_same_layer_is_parseable_and_not_a_fullcover_pair(self):
        value = analyze_bytes(fixture([(0, 0, 0, -10), (0, 0, 0, -11), (0, 0, 1, 99)]))
        self.assertEqual(len(value['tiles']), 3)
        self.assertEqual([(i['lower_index'], i['upper_index']) for i in value['issues']], [(0, 2), (1, 2)])

    def test_duplicate_tower_anchor_keeps_descriptors_and_uses_first_for_projection(self):
        value = analyze_bytes(fixture([(0, 0, 0, -10), (0, 0, 1, -10)], [(0, 0, 0), (0, 0, 3)]))
        self.assertEqual(len(value['towers']), 2)
        self.assertEqual([t['tower_index'] for t in value['tiles']], [0, 0])
        self.assertEqual(value['issues'], [])

    def test_bad_wire_missing_id_and_invalid_direction_raise(self):
        for raw in [b"\x32\xff", b"", message(2, b"x") + message(6, integer(1, 0)), fixture([], [(0, 0, 9)])]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                analyze_bytes(raw)

    def test_fixed_seed_random_geometry_matches_independent_pair_reference(self):
        rng = random.Random(1863)
        for _ in range(80):
            points = rng.sample([(x, y, z) for x in range(-2, 5) for y in range(-2, 5) for z in range(5)], 30)
            raw = fixture([(*p, -10 if index % 2 else 12) for index, p in enumerate(points)])
            expected = []
            for lower_index, (x, y, low) in enumerate(points):
                for upper_index, (xx, yy, high) in enumerate(points):
                    if (x, y) != (xx, yy) or high <= low:
                        continue
                    if any(tx == x and ty == y and low < tz < high for tx, ty, tz in points):
                        continue
                    if high == low + 1:
                        expected.append(("adjacent", lower_index, upper_index))
                    elif not any(low < tz < high and abs(tx - x) < 2 and abs(ty - y) < 2 for tx, ty, tz in points):
                        expected.append(("floating", lower_index, upper_index))
            actual = analyze_bytes(raw)
            self.assertEqual(sorted((i["kind"], i["lower_index"], i["upper_index"]) for i in actual["issues"]), sorted(expected))

    def test_results_csv_has_bom_and_safe_cells(self):
        value = analyze_bytes(fixture([(0, 0, 0, -10), (0, 0, 1, -11)]), name="=formula")
        raw = results_csv([value])
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
        self.assertIn("相邻层全遮盖组数", rows[0])
        self.assertEqual(rows[1][0], "'=formula")


if __name__ == "__main__":
    unittest.main()
