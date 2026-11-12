"""Template model contracts, including metadata absent from golden samples."""

import hashlib
import json
import unittest
from pathlib import Path

from scripts.tests.project_fixtures import read_project_fixture
from scripts.tower_reducer.template_reader import decode_template, encode_template

TILE_A = bytes.fromhex("0802100320f6ffffffffffffffff0128073501020304")
TILE_B = bytes.fromhex("0804100520f5ffffffffffffffff01")
STACK = bytes.fromhex("080210031801")


def small_template(tiles):
    # The literal field bodies are deliberate, hand-checked protobuf fixtures.
    return (
        bytes.fromhex("0801120673616d706c651a046475706520122814")
        + b"".join(bytes((0x32, len(tile))) + tile for tile in tiles)
        + bytes((0x3a, len(STACK))) + STACK
        + bytes.fromhex("42020801782a8d0109080706")
    )


class TemplateReaderTests(unittest.TestCase):
    def test_golden_fixtures_match_manifest_and_round_trip_byte_exactly(self):
        manifest = json.loads(read_project_fixture("manifest.json"))
        self.assertEqual(len(manifest["fixtures"]), 10)
        for name, expected in manifest["fixtures"].items():
            with self.subTest(name=name):
                source_name = name[5:] if name.startswith("lessT") else name
                summary = manifest["field_summaries"][source_name]
                raw = read_project_fixture(name)
                self.assertEqual(len(raw), expected["size"])
                self.assertEqual(hashlib.sha256(raw).hexdigest(), expected["sha256"])
                document = decode_template(raw)
                self.assertEqual(document.template_id, expected["template_id"])
                self.assertEqual(len(document.tiles), expected["tiles"])
                self.assertEqual(len(document.stacks), expected["stacks"])
                self.assertEqual((document.width, document.height), tuple(summary["dimensions"]))
                self.assertEqual(
                    [(stack.x, stack.y, stack.direction) for stack in document.stacks],
                    [tuple(anchor) for anchor in summary["stack_anchors"]],
                )
                self.assertEqual(
                    (document.tiles[0].x, document.tiles[0].y, document.tiles[0].z),
                    tuple(summary["first_tile_xyz"]),
                )
                self.assertEqual({tile.tile_id for tile in document.tiles}, {-10})
                self.assertEqual(encode_template(document), raw)

    def test_b_only_and_mixed_tiles_read_signed_ids_without_changing_wire(self):
        for tiles, expected in (([TILE_B], [-11]), ([TILE_A, TILE_B], [-10, -11])):
            with self.subTest(expected=expected):
                raw = small_template(tiles)
                document = decode_template(raw)
                self.assertEqual([tile.tile_id for tile in document.tiles], expected)
                self.assertEqual(encode_template(document), raw)

    def test_model_reads_metadata_stack_and_tile_defaults(self):
        document = decode_template(small_template([TILE_A]))
        self.assertEqual((document.version, document.template_id, document.duplicate_id), (1, "sample", "dupe"))
        self.assertEqual((document.width, document.height), (18, 20))
        self.assertEqual((document.tiles[0].x, document.tiles[0].y, document.tiles[0].z), (2, 3, 0))
        self.assertEqual(document.tiles[0].debuff_data_index, 7)
        self.assertEqual((document.stacks[0].x, document.stacks[0].y, document.stacks[0].direction), (2, 3, 1))
        self.assertEqual(document.debuff_data, [bytes.fromhex("0801")])
        self.assertEqual([(f.number, f.wire_type) for f in document.fields[-2:]], [(15, 0), (17, 5)])
        self.assertEqual(document.tiles[0].fields[-1].raw, bytes.fromhex("3501020304"))

    def test_duplicate_scalar_fields_use_last_value_and_preserve_prior_wire(self):
        tile = bytes.fromhex(
            "08011003080218001801"
            "20f6ffffffffffffffff01"
            "20f5ffffffffffffffff01"
            "28042807"
        )
        stack = bytes.fromhex("08011003080218001801")
        raw = (
            bytes.fromhex("12016108010802120162")
            + bytes((0x32, len(tile))) + tile
            + bytes((0x3a, len(stack))) + stack
            + bytes.fromhex("782a")
        )
        document = decode_template(raw)
        self.assertEqual((document.template_id, document.version), ("b", 2))
        self.assertEqual(
            (document.tiles[0].x, document.tiles[0].z, document.tiles[0].tile_id, document.tiles[0].debuff_data_index),
            (2, 1, -11, 7),
        )
        self.assertEqual((document.stacks[0].x, document.stacks[0].direction), (2, 1))
        self.assertEqual(encode_template(document), raw)

        document.template_id = "c"
        document.tiles[0].x = 9
        document.tiles[0].z = 3
        document.tiles[0].tile_id = -10
        document.tiles[0].debuff_data_index = 8
        reread = decode_template(encode_template(document))
        self.assertEqual((reread.template_id, reread.tiles[0].x, reread.tiles[0].z), ("c", 9, 3))
        self.assertEqual((reread.tiles[0].tile_id, reread.tiles[0].debuff_data_index), (-10, 8))
        self.assertEqual(reread.fields[0].raw, bytes.fromhex("120161"))
        self.assertEqual(reread.fields[3].raw, bytes.fromhex("120163"))
        self.assertEqual(
            [reread.tiles[0].fields[i].raw for i in (0, 3, 5, 7)],
            [bytes.fromhex("0801"), bytes.fromhex("1800"),
             bytes.fromhex("20f6ffffffffffffffff01"), bytes.fromhex("2804")],
        )
        self.assertEqual(reread.fields[-1].raw, bytes.fromhex("782a"))

    def test_tile_id_and_debuff_index_edits_encode_and_read_back(self):
        document = decode_template(small_template([TILE_A, TILE_B]))
        document.tiles[0].tile_id = -11
        document.tiles[0].debuff_data_index = 9
        document.tiles[1].tile_id = -10
        document.tiles[1].debuff_data_index = 3
        reread = decode_template(encode_template(document))
        self.assertEqual([tile.tile_id for tile in reread.tiles], [-11, -10])
        self.assertEqual([tile.debuff_data_index for tile in reread.tiles], [9, 3])
        self.assertEqual(reread.tiles[0].fields[-1].raw, bytes.fromhex("3501020304"))
        self.assertEqual([f.raw for f in reread.fields[-2:]], [bytes.fromhex("782a"), bytes.fromhex("8d0109080706")])

    def test_mutating_id_and_coordinates_preserves_other_wire_slices(self):
        raw = small_template([TILE_A, TILE_B])
        document = decode_template(raw)
        document.template_id = "lessTsample"
        document.tiles[0].x = 6
        document.tiles[0].z = 2  # Missing Z field is appended when it becomes nonzero.
        encoded = encode_template(document)
        reread = decode_template(encoded)
        self.assertEqual(reread.template_id, "lessTsample")
        self.assertEqual((reread.tiles[0].x, reread.tiles[0].y, reread.tiles[0].z), (6, 3, 2))
        self.assertEqual(reread.tiles[0].tile_id, -10)
        self.assertEqual(reread.tiles[0].debuff_data_index, 7)
        self.assertEqual(reread.fields[-2].raw, bytes.fromhex("782a"))
        self.assertEqual(reread.fields[-1].raw, bytes.fromhex("8d0109080706"))
        self.assertEqual(reread.stacks[0].fields, document.stacks[0].fields)
        self.assertEqual(reread.tiles[1].fields, document.tiles[1].fields)
        self.assertEqual([f.number for f in reread.fields], [f.number for f in document.fields])

    def test_encoder_rejects_unsupported_metadata_edits_instead_of_discarding_them(self):
        for change in (
            lambda document: setattr(document, "width", 19),
            lambda document: setattr(document.stacks[0], "x", 9),
            lambda document: document.debuff_data.append(b"new"),
        ):
            document = decode_template(small_template([TILE_A]))
            with self.subTest(change=change):
                change(document)
                with self.assertRaises(ValueError):
                    encode_template(document)

    def test_encoder_rejects_tile_list_shape_changes_instead_of_discarding_them(self):
        document = decode_template(small_template([TILE_A, TILE_B]))
        document.tiles.pop()
        with self.assertRaises(ValueError):
            encode_template(document)


if __name__ == "__main__":
    unittest.main()
