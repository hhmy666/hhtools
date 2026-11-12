"""Small protobuf wire reader that retains every source byte slice.

Only wire types 0, 1, 2 and 5 are supported. Deprecated groups are rejected.
No generated protobuf package or application schema is needed here.
"""

from dataclasses import dataclass
from typing import Tuple, Union


WireValue = Union[int, bytes]
MAX_UINT64 = (1 << 64) - 1


@dataclass(frozen=True)
class WireField:
    number: int
    wire_type: int
    value: WireValue
    raw: bytes
    tag_raw: bytes
    value_raw: bytes
    length_raw: bytes = b""

    @property
    def payload(self) -> bytes:
        if self.wire_type != 2:
            raise ValueError("field is not length-delimited")
        return self.value  # type: ignore[return-value]


def read_varint(data: bytes, offset: int = 0) -> Tuple[int, int, bytes]:
    """Read a uint64 varint; accept noncanonical encodings for lossless reads."""
    start = offset
    value = 0
    for index in range(10):
        if offset >= len(data):
            raise ValueError("truncated varint at offset {}".format(start))
        byte = data[offset]
        offset += 1
        if index == 9 and byte > 1:
            raise ValueError("uint64 varint overflow at offset {}".format(start))
        value |= (byte & 0x7f) << (7 * index)
        if byte < 0x80:
            return value, offset, data[start:offset]
    raise ValueError("varint exceeds ten bytes at offset {}".format(start))


def encode_varint(value: int) -> bytes:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_UINT64:
        raise ValueError("varint value must be a uint64")
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7f) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def decode_int32(value: int) -> int:
    if not 0 <= value <= MAX_UINT64:
        raise ValueError("int32 wire value outside uint64 range")
    low = value & 0xffffffff
    signed = low - (1 << 32) if low & (1 << 31) else low
    if value > 0xffffffff and value != (signed & MAX_UINT64):
        raise ValueError("int32 wire value is not sign-extended")
    return signed


def encode_int32(value: int) -> bytes:
    if isinstance(value, bool) or not isinstance(value, int) or not -(1 << 31) <= value < (1 << 31):
        raise ValueError("value outside signed int32 range")
    return encode_varint(value & MAX_UINT64)


def parse_fields(data: bytes) -> Tuple[WireField, ...]:
    if not isinstance(data, bytes):
        raise TypeError("protobuf message must be bytes")
    fields = []
    offset = 0
    while offset < len(data):
        start = offset
        tag, offset, tag_raw = read_varint(data, offset)
        number, wire_type = tag >> 3, tag & 7
        if not 0 < number < (1 << 29) or wire_type not in (0, 1, 2, 5):
            raise ValueError("invalid or unsupported protobuf tag at offset {}".format(start))
        length_raw = b""
        if wire_type == 0:
            value, offset, value_raw = read_varint(data, offset)
        elif wire_type == 2:
            length, offset, length_raw = read_varint(data, offset)
            if length > len(data) - offset:
                raise ValueError("truncated length-delimited field {}".format(number))
            value_raw = data[offset:offset + length]
            value = value_raw
            offset += length
        else:
            size = 8 if wire_type == 1 else 4
            if len(data) - offset < size:
                raise ValueError("truncated fixed field {}".format(number))
            value_raw = data[offset:offset + size]
            value = value_raw
            offset += size
        fields.append(WireField(number, wire_type, value, data[start:offset], tag_raw, value_raw, length_raw))
    return tuple(fields)


def encode_fields(fields: Tuple[WireField, ...]) -> bytes:
    return b"".join(field.raw for field in fields)


def replace_length_delimited(field: WireField, payload: bytes) -> bytes:
    if field.wire_type != 2:
        raise ValueError("field is not length-delimited")
    length_raw = field.length_raw if len(payload) == len(field.payload) else encode_varint(len(payload))
    return field.tag_raw + length_raw + payload
