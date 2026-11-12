"""Lossless A/B -> C--N level rewriting with a strict wire whitelist."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Mapping, Tuple

from .protobuf_wire import WireField, encode_varint, signed64
from .template_reader import LevelTemplate, parse_level_bytes


A_TILE = -10
B_TILE = -11
FIRST_STRUCTURE_TILE = -12  # C
LAST_STRUCTURE_TILE = -23  # N
SOURCE_TILE_IDS = frozenset((A_TILE, B_TILE))
STRUCTURE_TILE_IDS = frozenset(range(LAST_STRUCTURE_TILE, FIRST_STRUCTURE_TILE + 1))
_TEMPLATE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class RewriteError(ValueError):
    """Raised when a requested or observed rewrite exceeds the whitelist."""


@dataclass(frozen=True)
class RewriteVerification:
    source_sha256: str
    output_sha256: str
    template_id: str
    changed_tile_indices: Tuple[int, ...]


def _validate_template_id(template_id: str) -> bytes:
    if not isinstance(template_id, str):
        raise RewriteError("new TemplateId must be a string")
    if not template_id or not _TEMPLATE_ID_PATTERN.fullmatch(template_id):
        raise RewriteError(
            "new TemplateId may contain only letters, digits, underscore, and hyphen"
        )
    encoded = template_id.encode("utf-8")
    if len(encoded) > 255:
        raise RewriteError("new TemplateId exceeds 255 UTF-8 bytes")
    return encoded


def _normalize_replacements(
    level: LevelTemplate, replacements: Mapping[int, int]
) -> dict[int, int]:
    if not isinstance(replacements, Mapping):
        raise RewriteError("replacements must be a mapping of tile index to TileId")
    normalized = {}
    for tile_index, new_tile_id in replacements.items():
        if isinstance(tile_index, bool) or not isinstance(tile_index, int):
            raise RewriteError("replacement tile indices must be integers")
        if tile_index < 0 or tile_index >= len(level.tiles):
            raise RewriteError(f"replacement tile index {tile_index} is out of range")
        if isinstance(new_tile_id, bool) or not isinstance(new_tile_id, int):
            raise RewriteError(f"replacement for tile {tile_index} is not an integer")
        if new_tile_id not in STRUCTURE_TILE_IDS:
            raise RewriteError(
                f"replacement for tile {tile_index} must be C--N "
                f"({FIRST_STRUCTURE_TILE} through {LAST_STRUCTURE_TILE})"
            )
        old_tile_id = level.tiles[tile_index].tile_id
        if old_tile_id not in SOURCE_TILE_IDS:
            raise RewriteError(
                f"tile {tile_index} has TileId {old_tile_id}; only A/B may be replaced"
            )
        normalized[tile_index] = new_tile_id
    return normalized


def _rewrite_length_delimited(field: WireField, payload: bytes) -> bytes:
    length_raw = (
        field.length_raw if field.length == len(payload) else encode_varint(len(payload))
    )
    return field.tag_raw + length_raw + payload


def _rewrite_tile_payload(
    tile_fields: Tuple[WireField, ...], old_tile_id: int, new_tile_id: int
) -> bytes:
    if old_tile_id not in SOURCE_TILE_IDS:
        raise RewriteError("only A/B TileData may be rewritten")
    if new_tile_id not in STRUCTURE_TILE_IDS:
        raise RewriteError("target TileId must be C--N")

    parts = []
    found = 0
    for field in tile_fields:
        if field.number == 4:
            if field.wire_type != 0:
                raise RewriteError("TileId field must use varint wire type")
            parts.append(field.tag_raw + encode_varint(new_tile_id))
            found += 1
        else:
            parts.append(field.raw)
    if found != 1:
        raise RewriteError(f"TileData must have exactly one TileId; found {found}")
    return b"".join(parts)


def rewrite_template(
    level: LevelTemplate,
    new_template_id: str,
    replacements: Mapping[int, int],
    *,
    verify: bool = True,
) -> bytes:
    """Rewrite only TemplateId and requested A/B TileIds.

    No coordinate, Z, list order, tower, debuff, unknown field, tag encoding,
    or unselected TileData byte may change.  Verification is enabled by default
    and should remain enabled for every candidate and export path.
    """

    encoded_id = _validate_template_id(new_template_id)
    normalized = _normalize_replacements(level, replacements)
    tile_by_top_field = {tile.top_field_index: tile for tile in level.tiles}
    parts = []

    for top_field_index, field in enumerate(level.fields):
        if top_field_index == level.template_id_field_index:
            parts.append(_rewrite_length_delimited(field, encoded_id))
            continue
        tile = tile_by_top_field.get(top_field_index)
        if tile is not None and tile.index in normalized:
            nested_payload = _rewrite_tile_payload(
                tile.fields, tile.tile_id, normalized[tile.index]
            )
            parts.append(_rewrite_length_delimited(field, nested_payload))
            continue
        parts.append(field.raw)

    output = b"".join(parts)
    if verify:
        verify_rewrite(level, output, new_template_id, normalized)
    return output


def _field_signature(fields: Tuple[WireField, ...]) -> Tuple[Tuple[int, int], ...]:
    return tuple((field.number, field.wire_type) for field in fields)


def verify_rewrite(
    source: LevelTemplate,
    output_data: bytes | bytearray | memoryview,
    expected_template_id: str,
    replacements: Mapping[int, int],
) -> RewriteVerification:
    """Prove that output differences are exactly the approved wire fields."""

    _validate_template_id(expected_template_id)
    normalized = _normalize_replacements(source, replacements)
    if not isinstance(output_data, (bytes, bytearray, memoryview)):
        raise RewriteError("output data must be bytes-like")
    output_bytes = bytes(output_data)
    try:
        output = parse_level_bytes(output_bytes, source_name="<rewrite verification>")
    except ValueError as error:
        raise RewriteError(f"output cannot be parsed safely: {error}") from error

    if output.template_id != expected_template_id:
        raise RewriteError(
            f"output TemplateId {output.template_id!r} does not match "
            f"{expected_template_id!r}"
        )
    if len(output.fields) != len(source.fields):
        raise RewriteError("top-level field count changed")
    if _field_signature(output.fields) != _field_signature(source.fields):
        raise RewriteError("top-level field number/wire sequence changed")
    if len(output.tiles) != len(source.tiles):
        raise RewriteError("TileData count changed")

    source_tile_by_top = {
        tile.top_field_index: tile for tile in source.tiles
    }
    output_tile_by_top = {
        tile.top_field_index: tile for tile in output.tiles
    }
    changed = []
    for top_index, (source_field, output_field) in enumerate(
        zip(source.fields, output.fields)
    ):
        if source_field.tag_raw != output_field.tag_raw:
            raise RewriteError(f"top-level field {top_index} tag bytes changed")
        if top_index == source.template_id_field_index:
            if output_field.payload.decode("utf-8") != expected_template_id:
                raise RewriteError("TemplateId payload mismatch")
            continue

        source_tile = source_tile_by_top.get(top_index)
        output_tile = output_tile_by_top.get(top_index)
        if source_tile is None:
            if output_field.raw != source_field.raw:
                raise RewriteError(
                    f"non-Tile top-level field at index {top_index} changed"
                )
            continue
        if output_tile is None:
            raise RewriteError(f"TileData at top-level index {top_index} disappeared")

        if _field_signature(output_tile.fields) != _field_signature(source_tile.fields):
            raise RewriteError(
                f"TileData[{source_tile.index}] field number/wire sequence changed"
            )
        if len(output_tile.fields) != len(source_tile.fields):
            raise RewriteError(f"TileData[{source_tile.index}] field count changed")

        target_id = normalized.get(source_tile.index)
        for nested_index, (source_part, output_part) in enumerate(
            zip(source_tile.fields, output_tile.fields)
        ):
            if source_part.tag_raw != output_part.tag_raw:
                raise RewriteError(
                    f"TileData[{source_tile.index}] field {nested_index} tag changed"
                )
            if source_part.number == 4 and target_id is not None:
                if output_part.value is None or signed64(output_part.value) != target_id:
                    raise RewriteError(
                        f"TileData[{source_tile.index}] TileId is not requested value"
                    )
                continue
            if output_part.raw != source_part.raw:
                raise RewriteError(
                    f"TileData[{source_tile.index}] non-whitelisted field changed"
                )

        if target_id is None:
            if output_field.raw != source_field.raw:
                raise RewriteError(
                    f"unselected TileData[{source_tile.index}] raw bytes changed"
                )
        else:
            if source_tile.tile_id not in SOURCE_TILE_IDS:
                raise RewriteError(
                    f"TileData[{source_tile.index}] source is not A/B"
                )
            if output_tile.tile_id != target_id:
                raise RewriteError(
                    f"TileData[{source_tile.index}] typed TileId mismatch"
                )
            changed.append(source_tile.index)

    expected_changed = tuple(sorted(normalized))
    if tuple(changed) != expected_changed:
        raise RewriteError(
            f"changed TileData indices {tuple(changed)} do not match "
            f"requested {expected_changed}"
        )

    canonical_output = rewrite_template(
        source, expected_template_id, normalized, verify=False
    )
    if output_bytes != canonical_output:
        raise RewriteError(
            "output bytes differ from the deterministic whitelist rewrite"
        )

    return RewriteVerification(
        source_sha256=hashlib.sha256(source.data).hexdigest(),
        output_sha256=hashlib.sha256(output_bytes).hexdigest(),
        template_id=output.template_id,
        changed_tile_indices=tuple(changed),
    )


# Migration-friendly names matching the old one-file CLI.
rewrite_level = rewrite_template
verify_level_rewrite = verify_rewrite
