"""Decode Tile Family templates and rewrite selected business fields losslessly."""

from typing import Dict, Optional, Tuple

from .models import Stack, TemplateDocument, Tile
from .protobuf_wire import WireField, decode_int32, encode_int32, encode_varint, parse_fields, replace_length_delimited


def _one(fields: Tuple[WireField, ...], number: int, wire_type: int, required: bool = False) -> Optional[WireField]:
    matches = [field for field in fields if field.number == number]
    if required and not matches:
        raise ValueError("field {} is required".format(number))
    if matches and matches[-1].wire_type != wire_type:
        raise ValueError("field {} has wrong wire type".format(number))
    return matches[-1] if matches else None


def _int(fields: Tuple[WireField, ...], number: int, required: bool = False, default: Optional[int] = None) -> Optional[int]:
    field = _one(fields, number, 0, required)
    return decode_int32(field.value) if field else default  # type: ignore[arg-type]


def _string(fields: Tuple[WireField, ...], number: int, required: bool = False) -> Optional[str]:
    field = _one(fields, number, 2, required)
    return field.payload.decode("utf-8") if field else None


def decode_template(raw: bytes) -> TemplateDocument:
    fields = parse_fields(raw)
    template_id = _string(fields, 2, True)
    assert template_id is not None
    tiles = []
    stacks = []
    debuff_data = []
    template_id_field_index = -1
    for top_index, field in enumerate(fields):
        if field.number == 2:
            template_id_field_index = top_index
        elif field.number == 6:
            if field.wire_type != 2:
                raise ValueError("TileData has wrong wire type")
            nested = parse_fields(field.payload)
            x, y, z = _int(nested, 1, True), _int(nested, 2, True), _int(nested, 3, default=0)
            tile_id = _int(nested, 4, True)
            debuff_index = _int(nested, 5)
            assert x is not None and y is not None and z is not None and tile_id is not None
            tiles.append(Tile(len(tiles), top_index, x, y, z, tile_id, debuff_index, nested,
                              (x, y, z, tile_id, debuff_index)))
        elif field.number == 7:
            if field.wire_type != 2:
                raise ValueError("StackData has wrong wire type")
            nested = parse_fields(field.payload)
            x, y, direction = _int(nested, 1, True), _int(nested, 2, True), _int(nested, 3, default=0)
            assert x is not None and y is not None and direction is not None
            stacks.append(Stack(len(stacks), top_index, x, y, direction, nested))
        elif field.number == 8:
            if field.wire_type != 2:
                raise ValueError("DebuffData has wrong wire type")
            debuff_data.append(field.payload)
    version, duplicate_id, width, height = _int(fields, 1), _string(fields, 3), _int(fields, 4), _int(fields, 5)
    return TemplateDocument(
        raw=raw, fields=fields, template_id=template_id, template_id_field_index=template_id_field_index,
        tiles=tiles, stacks=stacks, debuff_data=debuff_data, version=version,
        duplicate_id=duplicate_id, width=width, height=height,
        original_template_id=template_id,
        original_metadata=(version, duplicate_id, width, height),
        original_tile_positions=tuple(tile.top_field_index for tile in tiles),
        original_stack_values=tuple((stack.top_field_index, stack.x, stack.y, stack.direction) for stack in stacks),
        original_debuff_data=tuple(debuff_data),
    )


def _encode_tile(tile: Tile) -> bytes:
    wanted: Dict[int, Optional[int]] = {1: tile.x, 2: tile.y, 3: tile.z, 4: tile.tile_id, 5: tile.debuff_data_index}
    original: Dict[int, Optional[int]] = dict(zip((1, 2, 3, 4, 5), tile.original_values))
    last_field_index = {field.number: index for index, field in enumerate(tile.fields) if field.number in wanted}
    parts = []
    seen = set()
    for index, field in enumerate(tile.fields):
        number = field.number
        if number in wanted and wanted[number] != original[number] and (
            index == last_field_index[number] or wanted[number] is None
        ):
            if field.wire_type != 0:
                raise ValueError("TileData field {} has wrong wire type".format(number))
            if wanted[number] is not None:
                parts.append(field.tag_raw + encode_int32(wanted[number]))  # type: ignore[arg-type]
            seen.add(number)
        else:
            parts.append(field.raw)
            seen.add(number)
    for number in (3, 5):
        if number not in seen and wanted[number] is not None and wanted[number] != original[number]:
            parts.append(encode_varint(number << 3) + encode_int32(wanted[number]))  # type: ignore[arg-type]
    return b"".join(parts)


def encode_template(document: TemplateDocument) -> bytes:
    if (document.version, document.duplicate_id, document.width, document.height) != document.original_metadata:
        raise ValueError("metadata edits are not supported")
    if tuple(tile.top_field_index for tile in document.tiles) != document.original_tile_positions:
        raise ValueError("TileData entries cannot be added, removed or reordered")
    if tuple((stack.top_field_index, stack.x, stack.y, stack.direction) for stack in document.stacks) != document.original_stack_values:
        raise ValueError("StackData edits are not supported")
    if tuple(document.debuff_data) != document.original_debuff_data:
        raise ValueError("DebuffData edits are not supported")
    if document.template_id == document.original_template_id and all(
        (tile.x, tile.y, tile.z, tile.tile_id, tile.debuff_data_index) == tile.original_values
        for tile in document.tiles
    ):
        return document.raw
    tile_by_top = {tile.top_field_index: tile for tile in document.tiles}
    parts = []
    for index, field in enumerate(document.fields):
        if index == document.template_id_field_index and document.template_id != document.original_template_id:
            parts.append(replace_length_delimited(field, document.template_id.encode("utf-8")))
        elif index in tile_by_top:
            tile = tile_by_top[index]
            if (tile.x, tile.y, tile.z, tile.tile_id, tile.debuff_data_index) == tile.original_values:
                parts.append(field.raw)
            else:
                parts.append(replace_length_delimited(field, _encode_tile(tile)))
        else:
            parts.append(field.raw)
    return b"".join(parts)
