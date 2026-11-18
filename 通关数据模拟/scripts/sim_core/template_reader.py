"""Typed, read-only decoder for Tile Family level templates.

Only the small part of the protobuf schema required by puzzle generation is
interpreted.  All wire fields remain available so a later rewrite can preserve
unknown metadata exactly.  Tower membership follows the game runtime rule:
``StackData`` (top-level field 7) creates explicit XY anchors, and only tiles
whose own XY exactly matches one of those anchors are tower members.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional, Tuple

from .protobuf_wire import (
    DEFAULT_MAX_FIELDS,
    DEFAULT_MAX_MESSAGE_BYTES,
    WireField,
    fields_numbered,
    parse_fields,
    signed64,
)


class TemplateParseError(ValueError):
    """Raised when a valid wire stream violates the level-template schema."""


@dataclass(frozen=True)
class Tile:
    """One serialized ``TileData`` entry in original LevelData order."""

    index: int
    top_field_index: int
    x: int
    y: int
    z: int
    tile_id: int
    debuff_data_index: Optional[int]
    fields: Tuple[WireField, ...]
    raw: bytes
    tower_index: Optional[int] = None

    @property
    def layer(self) -> int:
        return self.z + 1

    @property
    def has_debuff_index(self) -> bool:
        return self.debuff_data_index is not None

    @property
    def is_tower_member(self) -> bool:
        return self.tower_index is not None


@dataclass(frozen=True)
class Tower:
    """One explicit top-level ``StackData`` anchor."""

    index: int
    top_field_index: int
    x: int
    y: int
    direction: int
    fields: Tuple[WireField, ...]
    raw: bytes
    member_indices: Tuple[int, ...] = ()

    @property
    def anchor(self) -> Tuple[int, int]:
        return self.x, self.y


@dataclass(frozen=True)
class LevelTemplate:
    """Lossless typed view of a level payload."""

    data: bytes
    fields: Tuple[WireField, ...]
    template_id: str
    template_id_field_index: int
    tiles: Tuple[Tile, ...]
    towers: Tuple[Tower, ...]
    version: Optional[int] = None
    duplicate_id: Optional[str] = None
    level_width: Optional[int] = None
    level_height: Optional[int] = None
    source_name: str = "<memory>"
    warnings: Tuple[str, ...] = ()

    @property
    def top(self) -> Tuple[WireField, ...]:
        """Compatibility alias for the parsed top-level field sequence."""

        return self.fields

    @property
    def tower_member_indices(self) -> frozenset[int]:
        return frozenset(
            tile.index for tile in self.tiles if tile.tower_index is not None
        )

    @property
    def non_tower_indices(self) -> frozenset[int]:
        return frozenset(
            tile.index for tile in self.tiles if tile.tower_index is None
        )

    @property
    def max_z(self) -> Optional[int]:
        return max((tile.z for tile in self.tiles), default=None)

    @property
    def max_layer(self) -> int:
        maximum = self.max_z
        return 0 if maximum is None else maximum + 1

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def tower_for_tile(self, tile_index: int) -> Optional[Tower]:
        tile = self.tiles[tile_index]
        return None if tile.tower_index is None else self.towers[tile.tower_index]


# Known LevelSaveData field types.  Unknown fields are preserved and accepted.
_LEVEL_WIRE_TYPES = {
    1: 0,  # Version
    2: 2,  # TemplateId
    3: 2,  # DuplicateId (deprecated)
    4: 0,  # LevelWidth
    5: 0,  # LevelHeight
    6: 2,  # repeated TileData
    7: 2,  # repeated TileTowerData / StackData
    8: 2,  # repeated DebuffData
}
_LEVEL_SINGULAR_FIELDS = (1, 2, 3, 4, 5)
_TILE_WIRE_TYPES = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
_TILE_REQUIRED_FIELDS = (1, 2, 4)
_TOWER_WIRE_TYPES = {1: 0, 2: 0, 3: 0}
_TOWER_REQUIRED_FIELDS = (1, 2)


def _schema_error(owner: str, message: str) -> TemplateParseError:
    return TemplateParseError(f"{owner}: {message}")


def _validate_known_wire_types(
    fields: Tuple[WireField, ...], expected: dict[int, int], owner: str
) -> None:
    for field in fields:
        wanted = expected.get(field.number)
        if wanted is not None and field.wire_type != wanted:
            raise _schema_error(
                owner,
                f"field {field.number} has wire type {field.wire_type}; "
                f"expected {wanted}",
            )


def _single_field(
    fields: Tuple[WireField, ...],
    number: int,
    owner: str,
    *,
    required: bool,
) -> Optional[WireField]:
    matches = fields_numbered(fields, number)
    if not matches:
        if required:
            raise _schema_error(owner, f"required field {number} is missing")
        return None
    if len(matches) != 1:
        raise _schema_error(
            owner, f"field {number} must occur exactly once; found {len(matches)}"
        )
    return matches[0]


def _int32(field: WireField, owner: str) -> int:
    if field.wire_type != 0 or field.value is None:
        raise _schema_error(owner, f"field {field.number} is not a varint")
    decoded = signed64(field.value)
    if decoded < -(1 << 31) or decoded > (1 << 31) - 1:
        raise _schema_error(
            owner, f"field {field.number} value is outside signed int32 range"
        )
    return decoded


def _optional_int32(
    fields: Tuple[WireField, ...], number: int, owner: str
) -> Optional[int]:
    field = _single_field(fields, number, owner, required=False)
    return None if field is None else _int32(field, owner)


def _utf8(field: WireField, owner: str, *, allow_empty: bool) -> str:
    if field.wire_type != 2:
        raise _schema_error(owner, f"field {field.number} is not length-delimited")
    try:
        value = field.payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise _schema_error(owner, f"field {field.number} is not valid UTF-8") from error
    if not allow_empty and not value:
        raise _schema_error(owner, f"field {field.number} may not be empty")
    if "\x00" in value:
        raise _schema_error(owner, f"field {field.number} contains a NUL character")
    return value


def _parse_tile(
    field: WireField,
    *,
    tile_index: int,
    top_field_index: int,
    max_fields: int,
) -> Tile:
    owner = f"TileData[{tile_index}]"
    nested = parse_fields(
        field.payload,
        max_message_bytes=len(field.payload),
        max_fields=max_fields,
    )
    _validate_known_wire_types(nested, _TILE_WIRE_TYPES, owner)
    values = {}
    for number in _TILE_REQUIRED_FIELDS:
        required_field = _single_field(nested, number, owner, required=True)
        assert required_field is not None
        values[number] = _int32(required_field, owner)
    # protobuf-net legitimately omits an int32 whose serialized value is its
    # default.  Real production templates therefore omit Z when Z == 0 even
    # though TileData marks the member required at the C# schema level.
    z = _optional_int32(nested, 3, owner)
    debuff = _optional_int32(nested, 5, owner)
    return Tile(
        index=tile_index,
        top_field_index=top_field_index,
        x=values[1],
        y=values[2],
        z=0 if z is None else z,
        tile_id=values[4],
        debuff_data_index=debuff,
        fields=nested,
        raw=field.raw,
    )


def _parse_tower(
    field: WireField,
    *,
    tower_index: int,
    top_field_index: int,
    max_fields: int,
) -> Tower:
    owner = f"StackData[{tower_index}]"
    nested = parse_fields(
        field.payload,
        max_message_bytes=len(field.payload),
        max_fields=max_fields,
    )
    _validate_known_wire_types(nested, _TOWER_WIRE_TYPES, owner)
    values = {}
    for number in _TOWER_REQUIRED_FIELDS:
        required_field = _single_field(nested, number, owner, required=True)
        assert required_field is not None
        values[number] = _int32(required_field, owner)
    # DirectionUp == 0 and is commonly absent on the wire.
    direction_value = _optional_int32(nested, 3, owner)
    direction = 0 if direction_value is None else direction_value
    if direction not in (0, 1, 2, 3):
        raise _schema_error(owner, f"direction must be 0, 1, 2, or 3; got {direction}")
    return Tower(
        index=tower_index,
        top_field_index=top_field_index,
        x=values[1],
        y=values[2],
        direction=direction,
        fields=nested,
        raw=field.raw,
    )


def parse_level_bytes(
    data: bytes | bytearray | memoryview,
    *,
    source_name: str = "<memory>",
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
    max_fields: int = DEFAULT_MAX_FIELDS,
) -> LevelTemplate:
    """Decode a level payload into a typed, lossless representation."""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("level data must be bytes-like")
    source = bytes(data)
    top = parse_fields(
        source,
        max_message_bytes=max_message_bytes,
        max_fields=max_fields,
    )
    _validate_known_wire_types(top, _LEVEL_WIRE_TYPES, "LevelSaveData")
    for number in _LEVEL_SINGULAR_FIELDS:
        _single_field(top, number, "LevelSaveData", required=(number == 2))

    template_field = _single_field(top, 2, "LevelSaveData", required=True)
    assert template_field is not None
    template_id = _utf8(template_field, "LevelSaveData", allow_empty=False)
    template_id_field_index = top.index(template_field)

    version = _optional_int32(top, 1, "LevelSaveData")
    width = _optional_int32(top, 4, "LevelSaveData")
    height = _optional_int32(top, 5, "LevelSaveData")
    duplicate_field = _single_field(top, 3, "LevelSaveData", required=False)
    duplicate_id = (
        None
        if duplicate_field is None
        else _utf8(duplicate_field, "LevelSaveData", allow_empty=True)
    )
    if width is not None and width < 0:
        raise _schema_error("LevelSaveData", "LevelWidth may not be negative")
    if height is not None and height < 0:
        raise _schema_error("LevelSaveData", "LevelHeight may not be negative")

    tiles = []
    towers = []
    for top_field_index, field in enumerate(top):
        if field.number == 6:
            tiles.append(
                _parse_tile(
                    field,
                    tile_index=len(tiles),
                    top_field_index=top_field_index,
                    max_fields=max_fields,
                )
            )
        elif field.number == 7:
            towers.append(
                _parse_tower(
                    field,
                    tower_index=len(towers),
                    top_field_index=top_field_index,
                    max_fields=max_fields,
                )
            )

    anchor_to_tower = {}
    warnings = []
    for tower in towers:
        if tower.anchor in anchor_to_tower:
            previous = anchor_to_tower[tower.anchor]
            warnings.append(
                f"duplicate StackData anchor {tower.anchor} at indices "
                f"{previous} and {tower.index}; runtime last entry wins"
            )
        # This intentionally mirrors LevelPlayData's array assignment order.
        anchor_to_tower[tower.anchor] = tower.index

    member_lists = [[] for _ in towers]
    associated_tiles = []
    for tile in tiles:
        tower_index = anchor_to_tower.get((tile.x, tile.y))
        associated = replace(tile, tower_index=tower_index)
        associated_tiles.append(associated)
        if tower_index is not None:
            member_lists[tower_index].append(tile.index)
    associated_towers = tuple(
        replace(tower, member_indices=tuple(member_lists[tower.index]))
        for tower in towers
    )

    return LevelTemplate(
        data=source,
        fields=top,
        template_id=template_id,
        template_id_field_index=template_id_field_index,
        tiles=tuple(associated_tiles),
        towers=associated_towers,
        version=version,
        duplicate_id=duplicate_id,
        level_width=width,
        level_height=height,
        source_name=str(source_name),
        warnings=tuple(warnings),
    )


def parse_level_file(
    path: str | Path,
    *,
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
    max_fields: int = DEFAULT_MAX_FIELDS,
) -> LevelTemplate:
    """Read and parse a template without ever opening it for writing."""

    source_path = Path(path)
    data = source_path.read_bytes()
    return parse_level_bytes(
        data,
        source_name=str(source_path),
        max_message_bytes=max_message_bytes,
        max_fields=max_fields,
    )


# A short compatibility name for migration from the legacy script.
parse_level = parse_level_file
