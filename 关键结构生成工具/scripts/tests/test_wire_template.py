"""Regression tests for lossless protobuf parsing and template rewriting."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.protobuf_wire import (  # noqa: E402
    ProtobufWireError,
    encode_varint,
    parse_fields,
    read_varint,
    rebuild,
)
from puzzle_core.rewrite import (  # noqa: E402
    RewriteError,
    rewrite_template,
    verify_rewrite,
)
from puzzle_core.template_reader import (  # noqa: E402
    TemplateParseError,
    parse_level_bytes,
)


def varint_field(number: int, value: int) -> bytes:
    return encode_varint(number << 3) + encode_varint(value)


def bytes_field(number: int, payload: bytes) -> bytes:
    return encode_varint((number << 3) | 2) + encode_varint(len(payload)) + payload


def fixed64_field(number: int, value: int) -> bytes:
    return encode_varint((number << 3) | 1) + value.to_bytes(8, "little")


def fixed32_field(number: int, value: int) -> bytes:
    return encode_varint((number << 3) | 5) + value.to_bytes(4, "little")


def tile_payload(
    x: int,
    y: int,
    z: int,
    tile_id: int,
    *,
    omit_zero_z: bool = False,
    extra: bytes = b"",
) -> bytes:
    parts = [varint_field(1, x), varint_field(2, y)]
    if not (omit_zero_z and z == 0):
        parts.append(varint_field(3, z))
    parts.extend((varint_field(4, tile_id), extra))
    return b"".join(parts)


def tower_payload(x: int, y: int, direction: int, *, omit_up: bool = False) -> bytes:
    parts = [varint_field(1, x), varint_field(2, y)]
    if not (omit_up and direction == 0):
        parts.append(varint_field(3, direction))
    return b"".join(parts)


def level_payload(
    template_id: str,
    tiles: list[bytes],
    *,
    towers: list[bytes] | None = None,
    extra_top: bytes = b"",
) -> bytes:
    parts = [
        varint_field(1, 1),
        bytes_field(2, template_id.encode("utf-8")),
        varint_field(4, 20),
        varint_field(5, 20),
    ]
    parts.extend(bytes_field(6, tile) for tile in tiles)
    parts.extend(bytes_field(7, tower) for tower in (towers or []))
    parts.append(extra_top)
    return b"".join(parts)


class ProtobufWireTests(unittest.TestCase):
    def test_reads_and_rebuilds_all_supported_wire_types(self) -> None:
        data = b"".join(
            (
                varint_field(1, 300),
                fixed64_field(2, 0x0807060504030201),
                bytes_field(3, b"abc\x00"),
                fixed32_field(4, 0xA1B2C3D4),
            )
        )
        fields = parse_fields(data)
        self.assertEqual([field.wire_type for field in fields], [0, 1, 2, 5])
        self.assertEqual(fields[0].value, 300)
        self.assertEqual(fields[1].value, 0x0807060504030201)
        self.assertEqual(fields[2].payload, b"abc\x00")
        self.assertEqual(fields[3].value, 0xA1B2C3D4)
        self.assertEqual(rebuild(fields), data)

    def test_rejects_truncated_and_overflowing_varints(self) -> None:
        with self.assertRaisesRegex(ProtobufWireError, "truncated varint"):
            read_varint(b"\x80")
        with self.assertRaisesRegex(ProtobufWireError, "overflow"):
            read_varint(b"\x80" * 9 + b"\x02")

    def test_rejects_truncated_fixed_and_length_delimited_fields(self) -> None:
        with self.assertRaisesRegex(ProtobufWireError, "truncated fixed64"):
            parse_fields(b"\x09\x00")
        with self.assertRaisesRegex(ProtobufWireError, "declared length"):
            parse_fields(b"\x12\x05ab")
        with self.assertRaisesRegex(ProtobufWireError, "truncated fixed32"):
            parse_fields(b"\x1d\x00\x00")

    def test_rejects_groups_invalid_wire_and_field_zero(self) -> None:
        with self.assertRaisesRegex(ProtobufWireError, "group wire type 3"):
            parse_fields(b"\x0b")
        with self.assertRaisesRegex(ProtobufWireError, "group wire type 4"):
            parse_fields(b"\x0c")
        with self.assertRaisesRegex(ProtobufWireError, "invalid protobuf wire type 6"):
            parse_fields(b"\x0e")
        with self.assertRaisesRegex(ProtobufWireError, "field number zero"):
            parse_fields(b"\x00")


class TemplateReaderTests(unittest.TestCase):
    def test_parses_typed_template_defaults_and_explicit_tower_members(self) -> None:
        source = level_payload(
            "hr_test",
            [
                tile_payload(5, 7, 0, -10, omit_zero_z=True),
                tile_payload(5, 7, 4, -11),
                tile_payload(5, 8, 0, -10, omit_zero_z=True),
            ],
            towers=[tower_payload(5, 7, 0, omit_up=True)],
            extra_top=fixed32_field(30, 0x12345678),
        )
        level = parse_level_bytes(source, source_name="fixture_m.bytes")

        self.assertEqual(level.template_id, "hr_test")
        self.assertEqual(level.source_name, "fixture_m.bytes")
        self.assertEqual(level.tiles[0].z, 0)
        self.assertEqual(level.tiles[0].layer, 1)
        self.assertEqual(level.towers[0].direction, 0)
        self.assertEqual(level.towers[0].member_indices, (0, 1))
        self.assertEqual(level.tower_member_indices, frozenset((0, 1)))
        self.assertIsNone(level.tiles[2].tower_index)
        self.assertEqual(level.max_z, 4)
        self.assertEqual(level.max_layer, 5)

    def test_duplicate_tower_anchor_is_diagnostic_and_runtime_last_wins(self) -> None:
        source = level_payload(
            "duplicate_anchor",
            [tile_payload(3, 4, 0, -10)],
            towers=[tower_payload(3, 4, 1), tower_payload(3, 4, 2)],
        )
        level = parse_level_bytes(source)
        self.assertEqual(level.tiles[0].tower_index, 1)
        self.assertEqual(level.towers[0].member_indices, ())
        self.assertEqual(level.towers[1].member_indices, (0,))
        self.assertEqual(len(level.warnings), 1)
        self.assertIn("last entry wins", level.warnings[0])

    def test_rejects_missing_or_duplicate_tile_required_fields(self) -> None:
        missing_id = varint_field(1, 1) + varint_field(2, 2)
        with self.assertRaisesRegex(TemplateParseError, "required field 4 is missing"):
            parse_level_bytes(level_payload("missing", [missing_id]))

        duplicate_id = tile_payload(1, 2, 0, -10) + varint_field(4, -11)
        with self.assertRaisesRegex(TemplateParseError, "field 4 must occur exactly once"):
            parse_level_bytes(level_payload("duplicate", [duplicate_id]))

    def test_rejects_duplicate_template_id_and_known_wrong_wire_type(self) -> None:
        tile = tile_payload(1, 2, 0, -10)
        duplicate = level_payload("one", [tile]) + bytes_field(2, b"two")
        with self.assertRaisesRegex(TemplateParseError, "field 2 must occur exactly once"):
            parse_level_bytes(duplicate)

        wrong_type = varint_field(2, 7) + bytes_field(6, tile)
        with self.assertRaisesRegex(TemplateParseError, "field 2 has wire type 0"):
            parse_level_bytes(wrong_type)


class RewriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source_bytes = level_payload(
            "source01",
            [
                tile_payload(1, 2, 0, -10, extra=bytes_field(99, b"unknown")),
                tile_payload(3, 4, 1, -11),
                tile_payload(7, 8, 2, -12),
            ],
            extra_top=fixed64_field(40, 0x0102030405060708),
        )
        self.source = parse_level_bytes(self.source_bytes)

    def test_rewrite_changes_only_template_id_and_selected_tile_id(self) -> None:
        output_bytes = rewrite_template(
            self.source, "aipuzzlesource01s5", {0: -13, 1: -23}
        )
        result = verify_rewrite(
            self.source,
            output_bytes,
            "aipuzzlesource01s5",
            {0: -13, 1: -23},
        )
        output = parse_level_bytes(output_bytes)
        self.assertEqual(output.template_id, "aipuzzlesource01s5")
        self.assertEqual([tile.tile_id for tile in output.tiles], [-13, -23, -12])
        self.assertEqual(result.changed_tile_indices, (0, 1))
        self.assertNotEqual(result.source_sha256, result.output_sha256)

        # Unknown top metadata and the unselected TileData remain byte-identical.
        self.assertEqual(output.fields[-1].raw, self.source.fields[-1].raw)
        self.assertEqual(output.tiles[2].raw, self.source.tiles[2].raw)
        source_unknown = [f.raw for f in self.source.tiles[0].fields if f.number == 99]
        output_unknown = [f.raw for f in output.tiles[0].fields if f.number == 99]
        self.assertEqual(output_unknown, source_unknown)

    def test_rewrite_rejects_non_ab_source_and_non_cn_target(self) -> None:
        with self.assertRaisesRegex(RewriteError, "only A/B"):
            rewrite_template(self.source, "valid_id", {2: -13})
        with self.assertRaisesRegex(RewriteError, "must be C--N"):
            rewrite_template(self.source, "valid_id", {0: -24})
        with self.assertRaisesRegex(RewriteError, "out of range"):
            rewrite_template(self.source, "valid_id", {99: -12})

    def test_verifier_rejects_an_unapproved_coordinate_change(self) -> None:
        malicious = level_payload(
            "aipuzzlesource01s5",
            [
                tile_payload(2, 2, 0, -13, extra=bytes_field(99, b"unknown")),
                tile_payload(3, 4, 1, -11),
                tile_payload(7, 8, 2, -12),
            ],
            extra_top=fixed64_field(40, 0x0102030405060708),
        )
        with self.assertRaisesRegex(RewriteError, "non-whitelisted field changed"):
            verify_rewrite(
                self.source,
                malicious,
                "aipuzzlesource01s5",
                {0: -13},
            )


if __name__ == "__main__":
    unittest.main()
