"""Small, lossless protobuf wire-format reader used by the puzzle tool.

The level files are protobuf-net payloads, but the generator must preserve
unknown fields and their original byte representation.  This module therefore
parses fields without applying a schema and retains every raw slice.  It is not
a general protobuf implementation: groups are deliberately rejected and only
the four wire types used by modern protobuf messages are accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Tuple


MAX_UINT64 = (1 << 64) - 1
MAX_FIELD_NUMBER = (1 << 29) - 1
DEFAULT_MAX_MESSAGE_BYTES = 128 * 1024 * 1024
DEFAULT_MAX_FIELDS = 2_000_000


class ProtobufWireError(ValueError):
    """Raised when bytes are not a safe, supported protobuf wire stream."""


@dataclass(frozen=True)
class WireField:
    """One wire field, including exact source bytes and byte offsets.

    ``value`` is populated for varint, fixed64, and fixed32 fields.  Fixed
    values are interpreted as unsigned little-endian integers.  ``payload`` is
    populated for length-delimited fields.  Offsets are relative to the byte
    string passed to :func:`parse_fields`.
    """

    number: int
    wire_type: int
    start: int
    end: int
    tag_raw: bytes
    raw: bytes
    value: Optional[int] = None
    value_raw: bytes = b""
    value_start: Optional[int] = None
    value_end: Optional[int] = None
    length: Optional[int] = None
    length_raw: bytes = b""
    length_start: Optional[int] = None
    length_end: Optional[int] = None
    payload: bytes = b""
    payload_start: Optional[int] = None
    payload_end: Optional[int] = None

    @property
    def wire(self) -> int:
        """Compatibility alias for callers that use protobuf's short name."""

        return self.wire_type


def _as_bytes(data: bytes | bytearray | memoryview) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, (bytearray, memoryview)):
        return bytes(data)
    raise TypeError("protobuf data must be bytes-like")


def read_varint(
    data: bytes | bytearray | memoryview,
    offset: int = 0,
    *,
    context: str = "varint",
) -> Tuple[int, int, bytes]:
    """Read one unsigned protobuf varint with strict bounds/overflow checks."""

    payload = _as_bytes(data)
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise TypeError("offset must be an integer")
    if offset < 0 or offset > len(payload):
        raise ProtobufWireError(f"{context}: offset {offset} is out of bounds")

    start = offset
    value = 0
    for byte_index in range(10):
        if offset >= len(payload):
            raise ProtobufWireError(
                f"{context}: truncated varint at byte offset {start}"
            )
        current = payload[offset]
        offset += 1

        # A uint64 varint has at most one payload bit in its tenth byte.
        if byte_index == 9 and (current & 0xFE):
            raise ProtobufWireError(
                f"{context}: uint64 varint overflow at byte offset {start}"
            )
        value |= (current & 0x7F) << (7 * byte_index)
        if current < 0x80:
            return value, offset, payload[start:offset]

    # The tenth byte had its continuation bit set (caught by 0xFE above), so
    # this is defensive and documents that eleven-byte varints are forbidden.
    raise ProtobufWireError(
        f"{context}: varint exceeds ten bytes at byte offset {start}"
    )


def encode_varint(value: int) -> bytes:
    """Encode an unsigned uint64 or a signed int64 using protobuf varint form."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("varint value must be an integer")
    if value < -(1 << 63) or value > MAX_UINT64:
        raise ValueError("varint value is outside the supported 64-bit range")
    if value < 0:
        value &= MAX_UINT64

    encoded = bytearray()
    while value >= 0x80:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def signed64(value: int) -> int:
    """Interpret an unsigned uint64 wire value as protobuf-net signed int."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("wire value must be an integer")
    if value < 0 or value > MAX_UINT64:
        raise ValueError("wire value is outside uint64 range")
    return value - (1 << 64) if value & (1 << 63) else value


def parse_fields(
    data: bytes | bytearray | memoryview,
    *,
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
    max_fields: int = DEFAULT_MAX_FIELDS,
) -> Tuple[WireField, ...]:
    """Parse a protobuf message while preserving every field's exact bytes.

    Supported wire types are 0 (varint), 1 (fixed64), 2
    (length-delimited), and 5 (fixed32).  Deprecated group wire types 3 and 4
    are rejected explicitly rather than skipped because safe group skipping
    requires recursive matching semantics this lossless editor does not need.
    """

    payload = _as_bytes(data)
    if isinstance(max_message_bytes, bool) or not isinstance(max_message_bytes, int):
        raise TypeError("max_message_bytes must be an integer")
    if isinstance(max_fields, bool) or not isinstance(max_fields, int):
        raise TypeError("max_fields must be an integer")
    if max_message_bytes < 0 or max_fields < 0:
        raise ValueError("parser limits must be non-negative")
    if len(payload) > max_message_bytes:
        raise ProtobufWireError(
            f"message is {len(payload)} bytes; limit is {max_message_bytes}"
        )

    fields = []
    position = 0
    while position < len(payload):
        if len(fields) >= max_fields:
            raise ProtobufWireError(f"message exceeds field limit {max_fields}")

        start = position
        tag, position, tag_raw = read_varint(
            payload, position, context="field tag"
        )
        number = tag >> 3
        wire_type = tag & 0x07
        if number == 0:
            raise ProtobufWireError(
                f"field number zero is invalid at byte offset {start}"
            )
        if number > MAX_FIELD_NUMBER:
            raise ProtobufWireError(
                f"field number {number} exceeds protobuf maximum"
            )
        if wire_type in (3, 4):
            raise ProtobufWireError(
                f"unsupported protobuf group wire type {wire_type} "
                f"for field {number} at byte offset {start}"
            )
        if wire_type not in (0, 1, 2, 5):
            raise ProtobufWireError(
                f"invalid protobuf wire type {wire_type} "
                f"for field {number} at byte offset {start}"
            )

        value: Optional[int] = None
        value_raw = b""
        value_start: Optional[int] = None
        value_end: Optional[int] = None
        length: Optional[int] = None
        length_raw = b""
        length_start: Optional[int] = None
        length_end: Optional[int] = None
        field_payload = b""
        payload_start: Optional[int] = None
        payload_end: Optional[int] = None

        if wire_type == 0:
            value_start = position
            value, position, value_raw = read_varint(
                payload, position, context=f"field {number} value"
            )
            value_end = position
        elif wire_type == 1:
            value_start = position
            value_end = position + 8
            if value_end > len(payload):
                raise ProtobufWireError(
                    f"field {number}: truncated fixed64 at byte offset {position}"
                )
            value_raw = payload[value_start:value_end]
            value = int.from_bytes(value_raw, "little", signed=False)
            position = value_end
        elif wire_type == 2:
            length_start = position
            length, position, length_raw = read_varint(
                payload, position, context=f"field {number} length"
            )
            length_end = position
            # Subtraction avoids constructing a potentially overflowing end in
            # runtimes with fixed-width integers and gives a clearer error.
            remaining = len(payload) - position
            if length > remaining:
                raise ProtobufWireError(
                    f"field {number}: declared length {length} exceeds "
                    f"remaining {remaining} bytes at byte offset {position}"
                )
            payload_start = position
            payload_end = position + length
            field_payload = payload[payload_start:payload_end]
            position = payload_end
        else:  # wire_type == 5
            value_start = position
            value_end = position + 4
            if value_end > len(payload):
                raise ProtobufWireError(
                    f"field {number}: truncated fixed32 at byte offset {position}"
                )
            value_raw = payload[value_start:value_end]
            value = int.from_bytes(value_raw, "little", signed=False)
            position = value_end

        fields.append(
            WireField(
                number=number,
                wire_type=wire_type,
                start=start,
                end=position,
                tag_raw=tag_raw,
                raw=payload[start:position],
                value=value,
                value_raw=value_raw,
                value_start=value_start,
                value_end=value_end,
                length=length,
                length_raw=length_raw,
                length_start=length_start,
                length_end=length_end,
                payload=field_payload,
                payload_start=payload_start,
                payload_end=payload_end,
            )
        )

    return tuple(fields)


def fields_numbered(
    fields: Iterable[WireField], number: int
) -> Tuple[WireField, ...]:
    """Return all fields with ``number`` without hiding duplicates."""

    return tuple(field for field in fields if field.number == number)


def rebuild(fields: Iterable[WireField]) -> bytes:
    """Reassemble an unmodified field sequence byte-for-byte."""

    return b"".join(field.raw for field in fields)
