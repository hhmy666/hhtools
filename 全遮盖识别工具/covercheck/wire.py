"""Strict minimal protobuf reader; no dependency on another local tool."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Field:
    number: int
    wire_type: int
    value: object
    raw: bytes = field(default=b"", compare=False, repr=False)


def _varint(data, offset):
    start, value = offset, 0
    for index in range(10):
        if offset >= len(data):
            raise ValueError("truncated varint at offset {}".format(start))
        byte = data[offset]
        offset += 1
        if index == 9 and byte > 1:
            raise ValueError("uint64 varint overflow at offset {}".format(start))
        value |= (byte & 127) << (index * 7)
        if byte < 128:
            return value, offset
    raise ValueError("varint exceeds ten bytes at offset {}".format(start))


def parse_fields(data):
    if not isinstance(data, bytes):
        raise TypeError("protobuf message must be bytes")
    result, offset = [], 0
    while offset < len(data):
        start = offset
        tag, offset = _varint(data, offset)
        number, kind = tag >> 3, tag & 7
        if not 0 < number < (1 << 29) or kind not in (0, 1, 2, 5):
            raise ValueError("invalid or unsupported protobuf tag")
        if kind == 0:
            value, offset = _varint(data, offset)
        else:
            if kind == 2:
                length, offset = _varint(data, offset)
            else:
                length = 8 if kind == 1 else 4
            if length > len(data) - offset:
                raise ValueError("truncated field {}".format(number))
            value = data[offset:offset + length]
            offset += length
        result.append(Field(number, kind, value, data[start:offset]))
    return result


def scalar(fields, number, kind, *, required=False, default=None):
    matching = [field for field in fields if field.number == number]
    if not matching:
        if required:
            raise ValueError("required field {} is missing".format(number))
        return default
    if any(field.wire_type != kind for field in matching):
        raise ValueError("field {} has wrong wire type".format(number))
    if len({field.value for field in matching}) > 1:
        raise ValueError("field {} has conflicting duplicate values".format(number))
    return matching[-1].value


def int32(fields, number, *, required=False, default=None):
    value = scalar(fields, number, 0, required=required, default=default)
    if value is None:
        return None
    low = value & 0xffffffff
    signed = low - (1 << 32) if low & (1 << 31) else low
    if value > 0xffffffff and value != (signed & ((1 << 64) - 1)):
        raise ValueError("int32 field {} is not sign-extended".format(number))
    return signed
