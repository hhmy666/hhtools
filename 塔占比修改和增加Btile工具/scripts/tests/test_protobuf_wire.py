"""Wire-level contracts for lossless protobuf handling."""

import unittest

from scripts.tower_reducer.protobuf_wire import (
    decode_int32,
    encode_fields,
    encode_int32,
    parse_fields,
)


class ProtobufWireTests(unittest.TestCase):
    def test_all_supported_wire_types_keep_original_bytes_and_order(self):
        # Includes noncanonical key/value varints; normalization would corrupt a fixture.
        raw = bytes.fromhex(
            "88008100"  # field 1, varint 1
            "110102030405060708"  # field 2, fixed64
            "1a0300ff01"  # field 3, length-delimited
            "25aabbccdd"  # field 4, fixed32
        )
        fields = parse_fields(raw)
        self.assertEqual([(f.number, f.wire_type, f.value) for f in fields], [
            (1, 0, 1),
            (2, 1, bytes.fromhex("0102030405060708")),
            (3, 2, bytes.fromhex("00ff01")),
            (4, 5, bytes.fromhex("aabbccdd")),
        ])
        self.assertEqual(fields[0].raw, bytes.fromhex("88008100"))
        self.assertEqual(encode_fields(fields), raw)

    def test_signed_int32_round_trip_uses_protobuf_negative_encoding(self):
        self.assertEqual(encode_int32(-10), bytes.fromhex("f6ffffffffffffffff01"))
        self.assertEqual(decode_int32(18446744073709551606), -10)
        self.assertEqual(decode_int32(4294967295), -1)

    def test_truncated_and_invalid_wire_values_raise_value_error(self):
        for raw in (b"\x08\x80", b"\x11\x00", b"\x1a\x03\x00", b"\x25\x00", b"\x0b"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_fields(raw)


if __name__ == "__main__":
    unittest.main()
