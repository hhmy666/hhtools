"""Deterministic A/B assignment simulation for the legacy client seed path.

This pure implementation follows the inspected non-structured client branch
under explicit input assumptions: choose the latest level-tile row, draw a
candidate pool with CustomRandom, distribute complete triples, then restart the
same RNG for A and B and shuffle each three times.  It is evidence from the
current local configs, not a claim that their row/resource order matches every
runtime build. Structured ``randomSeed_groupID`` values are rejected, not
approximated.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import struct
from dataclasses import dataclass
from typing import Iterable, Sequence


LEGACY_MATERIALIZATION_VERSION = "legacy_client_pool_equal_weight_three_shuffle_v2"
LEGACY_EVIDENCE_LEVEL = "current_local_config_simulation"
LEGACY_CLIENT_SOURCE_REFS = (
    "LevelDupGenerator.cs:PrepareAllNeedTileIds",
    "LevelDupGenerator.cs:GetRandomTileTypeList",
    "LevelDupGenerator.cs:ShuffleList",
    "CustomRandom.cs:GetRandom",
)

# Copied from TileConfigManager.BaseTiles in the inspected local client revision. The
# config-only replay preserves config_client_tile.csv row order for this set.
LEGACY_CLIENT_BASE_TILE_IDS = frozenset(
    (
        1000, 1001, 1002, 1003, 1004, 1005, 1006, 1007, 1008, 1009,
        1011, 1012, 1013, 1014, 1300, 1301, 1302, 1304, 1313,
    )
)

_UINT32 = 0xFFFFFFFF
_ELIMINATE_COUNT = 3


def _float32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", float(value)))[0]


def legacy_game_seed(dup_seed: int) -> int:
    """Translate DupPara's seed to the uint32 seed used by CustomRandom."""
    if isinstance(dup_seed, bool) or not isinstance(dup_seed, int):
        raise TypeError("DupPara seed must be an integer")
    return (1_411_242_022 + dup_seed) & _UINT32


class LegacyRandom:
    """The client's uint32 multiply-with-carry generator."""

    def __init__(self, seed: int) -> None:
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise TypeError("RNG seed must be an integer")
        self._mw = seed & _UINT32
        self._mz = 362_436_069

    def get_random(self, maximum: int) -> int:
        if isinstance(maximum, bool) or not isinstance(maximum, int):
            raise TypeError("maximum must be an integer")
        if maximum <= 0:
            raise ValueError("maximum must be positive")
        self._mz = (36_969 * (self._mz & 65_535) + (self._mz >> 16)) & _UINT32
        self._mw = (18_000 * (self._mw & 65_535) + (self._mw >> 16)) & _UINT32
        combined = (((self._mz << 16) & _UINT32) + self._mw) & _UINT32
        return combined % maximum


@dataclass(frozen=True)
class LevelTilePoolRange:
    row_id: int
    start_level: int
    tile_list1: tuple[int, ...]
    tile_list2: tuple[int, ...]


@dataclass(frozen=True)
class ClientTileCatalog:
    available_tile_ids: tuple[int, ...]
    normal_tile_ids: tuple[int, ...]


@dataclass(frozen=True)
class LegacyPatternPoolSelection:
    level_index: int
    selected_row_id: int
    selected_start_level: int
    required_type_count: int
    pattern_pool: tuple[int, ...]
    level_tile_config_fingerprint: str
    client_tile_config_fingerprint: str
    selection_fingerprint: str
    assumptions: tuple[str, ...]


@dataclass(frozen=True)
class LegacyMaterialization:
    version: str
    evidence_level: str
    client_source_refs: tuple[str, ...]
    assumptions: tuple[str, ...]
    configuration_fingerprint: str
    level_tile_config_fingerprint: str | None
    client_tile_config_fingerprint: str | None
    pool_selection_fingerprint: str | None
    random_seed_group_id: int
    dup_seed: int
    game_seed: int
    iap_protect_cut_a_count: int
    effective_a_types: int
    effective_b_types: int
    pattern_pool: tuple[int, ...]
    selected_level_tile_id: int | None
    selected_level_start: int | None
    a_assignments: tuple[tuple[int, int], ...]
    b_assignments: tuple[tuple[int, int], ...]


def _parse_pipe_ints(raw: str) -> tuple[int, ...]:
    if not raw.strip():
        return ()
    values: list[int] = []
    for part in raw.split("|"):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError as exc:
            raise ValueError(f"invalid tile ID {part!r}") from exc
        if value <= 0:
            raise ValueError("tile IDs must be positive")
        values.append(value)
    return tuple(values)


def _config_rows(csv_text: str) -> list[dict[str, str]]:
    """Read a ulink CSV while ignoring its schema and description rows."""
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    rows: list[dict[str, str]] = []
    for row in reader:
        if not row or not any((value or "").strip() for value in row.values()):
            continue
        first_value = next(iter(row.values()), "") or ""
        try:
            int(first_value)
        except ValueError:
            continue
        rows.append({key: value or "" for key, value in row.items() if key is not None})
    return rows


def parse_level_tile_config(csv_text: str) -> tuple[LevelTilePoolRange, ...]:
    """Parse config_client_level_tile CSV text without I/O."""
    result: list[LevelTilePoolRange] = []
    for row in _config_rows(csv_text):
        try:
            result.append(
                LevelTilePoolRange(
                    row_id=int(row["Id"]),
                    start_level=int(row["StartLevel"]),
                    tile_list1=_parse_pipe_ints(row.get("TileList1", "")),
                    tile_list2=_parse_pipe_ints(row.get("TileList2", "")),
                )
            )
        except KeyError as exc:
            raise ValueError(f"missing level-tile config column {exc.args[0]}") from exc
    if not result:
        raise ValueError("level-tile config contains no data rows")
    return tuple(result)


def parse_client_tile_config(csv_text: str) -> ClientTileCatalog:
    """Parse available tiles and preserve CSV order for current base normals."""
    available: list[int] = []
    seen: set[int] = set()
    for row in _config_rows(csv_text):
        try:
            tile_id = int(row["TileId"])
        except KeyError as exc:
            raise ValueError("missing client-tile config column TileId") from exc
        if tile_id <= 0 or tile_id in seen:
            continue
        seen.add(tile_id)
        available.append(tile_id)
    if not available:
        raise ValueError("client-tile config contains no data rows")
    normals = tuple(tile_id for tile_id in available if tile_id in LEGACY_CLIENT_BASE_TILE_IDS)
    return ClientTileCatalog(available_tile_ids=tuple(available), normal_tile_ids=normals)


def _ordered_unique_ints(
    values: Iterable[int], name: str, *, positive: bool
) -> tuple[int, ...]:
    result = tuple(values)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in result):
        raise TypeError(f"{name} must contain integer IDs")
    if positive and any(value <= 0 for value in result):
        raise ValueError(f"{name} must contain positive IDs")
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicate IDs")
    return result


def _sha256_json(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def select_level_tile_range(
    level_index: int, rows: Sequence[LevelTilePoolRange]
) -> LevelTilePoolRange:
    """Select the row with the greatest StartLevel not above level_index."""
    if isinstance(level_index, bool) or not isinstance(level_index, int):
        raise TypeError("level_index must be an integer")
    eligible = [row for row in rows if row.start_level <= level_index]
    if not eligible:
        raise ValueError(f"no level-tile row starts at or before level {level_index}")
    return max(eligible, key=lambda row: row.start_level)


def select_legacy_pattern_pool(
    *,
    level_index: int,
    dup_a_types: int,
    dup_b_types: int,
    dup_seed: int,
    level_tile_rows: Sequence[LevelTilePoolRange],
    available_tile_ids: Sequence[int],
    normal_tile_ids: Sequence[int],
) -> LegacyPatternPoolSelection:
    """Simulate PrepareAllNeedTileIds and return its unique ordered pool."""
    for name, count in (("dup_a_types", dup_a_types), ("dup_b_types", dup_b_types)):
        if isinstance(count, bool) or not isinstance(count, int):
            raise TypeError(f"{name} must be an integer")
        if count < 0:
            raise ValueError(f"{name} must not be negative")
    game_seed = legacy_game_seed(dup_seed)
    available_tuple = _ordered_unique_ints(
        available_tile_ids, "available_tile_ids", positive=True
    )
    available = set(available_tuple)
    normals = list(_ordered_unique_ints(normal_tile_ids, "normal_tile_ids", positive=True))
    if any(tile_id not in available for tile_id in normals):
        raise ValueError("normal_tile_ids must be present in available_tile_ids")
    if not level_tile_rows:
        raise ValueError("level_tile_rows must not be empty")

    row = select_level_tile_range(level_index, level_tile_rows)
    required = max(1, dup_a_types, dup_b_types)
    list1 = list(row.tile_list1)
    list2 = list(row.tile_list2)
    selected: list[int] = []
    selected_set: set[int] = set()
    random = LegacyRandom(game_seed)

    def add_configured(tile_id: int) -> None:
        if tile_id in normals:
            normals.remove(tile_id)
        if tile_id in available and tile_id not in selected_set:
            selected.append(tile_id)
            selected_set.add(tile_id)

    while len(selected) < required and list1:
        tile_id = list1.pop(random.get_random(len(list1)))
        if tile_id in list2:
            list2.remove(tile_id)
        add_configured(tile_id)

    while len(selected) < required and list2:
        tile_id = list2.pop(random.get_random(len(list2)))
        add_configured(tile_id)

    while len(selected) < required and normals:
        tile_id = normals.pop(random.get_random(len(normals)))
        if tile_id not in selected_set:
            selected.append(tile_id)
            selected_set.add(tile_id)

    if len(selected) < required:
        raise ValueError(
            f"candidate pools provide {len(selected)} unique IDs, fewer than required {required}"
        )

    level_rows_fingerprint = _sha256_json(
        [
            [item.row_id, item.start_level, list(item.tile_list1), list(item.tile_list2)]
            for item in level_tile_rows
        ]
    )
    tile_config_fingerprint = _sha256_json(
        {"available_order": list(available_tuple), "normal_order": list(normal_tile_ids)}
    )
    selection_fingerprint = _sha256_json(
        {
            "version": LEGACY_MATERIALIZATION_VERSION,
            "level_index": level_index,
            "selected_row": [row.row_id, row.start_level],
            "a": dup_a_types,
            "b": dup_b_types,
            "dup_seed": dup_seed,
            "pool": selected,
            "level_tile_config": level_rows_fingerprint,
            "client_tile_config": tile_config_fingerprint,
        }
    )
    return LegacyPatternPoolSelection(
        level_index=level_index,
        selected_row_id=row.row_id,
        selected_start_level=row.start_level,
        required_type_count=required,
        pattern_pool=tuple(selected),
        level_tile_config_fingerprint=level_rows_fingerprint,
        client_tile_config_fingerprint=tile_config_fingerprint,
        selection_fingerprint=selection_fingerprint,
        assumptions=(
            "normal_tile_ids_are_in_client_runtime_order",
            "configured_tile_ids_are_available_in_the_supplied_catalog",
        ),
    )


def _shuffled_pattern_ids(
    pattern_pool: tuple[int, ...], type_count: int, position_count: int, dup_seed: int
) -> tuple[int, ...]:
    if position_count == 0:
        return ()
    effective_count = max(1, type_count)
    if len(pattern_pool) < effective_count:
        raise ValueError(
            f"pattern pool has {len(pattern_pool)} IDs, fewer than the required {effective_count}"
        )
    types = sorted(pattern_pool[:effective_count], reverse=True)
    group_count = position_count // _ELIMINATE_COUNT
    total_weight = len(types) * 100
    counts: dict[int, int] = {}
    assigned = 0
    for tile_id in types:
        ratio = _float32(_float32(100.0) / _float32(float(total_weight)))
        current = math.floor(_float32(ratio * _float32(float(group_count))))
        if current == 0:
            current = 1
        if assigned + current > group_count:
            current = group_count - assigned
        if current <= 0:
            break
        counts[tile_id] = current
        assigned += current
    left = group_count - assigned
    if len(types) < left:
        raise ValueError("legacy allocation cannot distribute all three-tile groups")
    for index in range(left):
        counts[types[index]] += 1
    result: list[int] = []
    for tile_id, count in counts.items():
        result.extend([tile_id] * count * _ELIMINATE_COUNT)

    random = LegacyRandom(legacy_game_seed(dup_seed))
    for _ in range(3):
        for index in range(len(result)):
            other = random.get_random(len(result))
            result[index], result[other] = result[other], result[index]
    return tuple(result)


def materialize_legacy_ab(
    *,
    pattern_pool: Sequence[int],
    a_positions: Sequence[int],
    b_positions: Sequence[int],
    dup_a_types: int,
    dup_b_types: int,
    dup_seed: int,
    random_seed_group_id: int = 0,
    iap_protect_cut_a_count: int = 0,
    pool_selection: LegacyPatternPoolSelection | None = None,
) -> LegacyMaterialization:
    """Assign initial IDs to legacy A/B placeholders without I/O."""
    if isinstance(random_seed_group_id, bool) or not isinstance(random_seed_group_id, int):
        raise TypeError("randomSeed_groupID must be an integer")
    if random_seed_group_id > 0:
        raise ValueError("structured randomSeed_groupID > 0 is not supported")
    for name, count in (
        ("dup_a_types", dup_a_types),
        ("dup_b_types", dup_b_types),
        ("iap_protect_cut_a_count", iap_protect_cut_a_count),
    ):
        if isinstance(count, bool) or not isinstance(count, int):
            raise TypeError(f"{name} must be an integer")
        if count < 0:
            raise ValueError(f"{name} must not be negative")
    game_seed = legacy_game_seed(dup_seed)
    pool = _ordered_unique_ints(pattern_pool, "pattern_pool", positive=True)
    if pool_selection is not None and pool != pool_selection.pattern_pool:
        raise ValueError("pattern_pool does not match pool_selection evidence")
    a = _ordered_unique_ints(a_positions, "a_positions", positive=False)
    b = _ordered_unique_ints(b_positions, "b_positions", positive=False)
    if set(a) & set(b):
        raise ValueError("A and B position IDs must be disjoint")
    if len(a) % _ELIMINATE_COUNT or len(b) % _ELIMINATE_COUNT:
        raise ValueError("A and B position counts must each be multiples of three")

    reduced_a_types = dup_a_types - iap_protect_cut_a_count
    a_ids = _shuffled_pattern_ids(pool, reduced_a_types, len(a), dup_seed)
    b_ids = _shuffled_pattern_ids(pool, dup_b_types, len(b), dup_seed)
    config_fingerprint = _sha256_json(
        {
            "version": LEGACY_MATERIALIZATION_VERSION,
            "pool_selection": None if pool_selection is None else pool_selection.selection_fingerprint,
            "pool": list(pool),
            "a_positions": list(a),
            "b_positions": list(b),
            "dup_a_types": dup_a_types,
            "dup_b_types": dup_b_types,
            "dup_seed": dup_seed,
            "iap_cut_a": iap_protect_cut_a_count,
            "random_seed_group_id": random_seed_group_id,
        }
    )
    return LegacyMaterialization(
        version=LEGACY_MATERIALIZATION_VERSION,
        evidence_level=LEGACY_EVIDENCE_LEVEL,
        client_source_refs=LEGACY_CLIENT_SOURCE_REFS,
        assumptions=(
            "a_b_positions_are_in_template_serialization_order",
            "iap_protection_is_the_explicit_cut_value_defaulting_to_zero",
            "structured_random_seed_group_is_not_active",
        ) + (() if pool_selection is None else pool_selection.assumptions),
        configuration_fingerprint=config_fingerprint,
        level_tile_config_fingerprint=(
            None
            if pool_selection is None
            else pool_selection.level_tile_config_fingerprint
        ),
        client_tile_config_fingerprint=(
            None
            if pool_selection is None
            else pool_selection.client_tile_config_fingerprint
        ),
        pool_selection_fingerprint=(
            None if pool_selection is None else pool_selection.selection_fingerprint
        ),
        random_seed_group_id=random_seed_group_id,
        dup_seed=dup_seed,
        game_seed=game_seed,
        iap_protect_cut_a_count=iap_protect_cut_a_count,
        effective_a_types=max(1, reduced_a_types) if a else 0,
        effective_b_types=max(1, dup_b_types) if b else 0,
        pattern_pool=pool,
        selected_level_tile_id=None if pool_selection is None else pool_selection.selected_row_id,
        selected_level_start=None if pool_selection is None else pool_selection.selected_start_level,
        a_assignments=tuple(zip(a, a_ids)),
        b_assignments=tuple(zip(b, b_ids)),
    )


def materialize_legacy_ab_from_config(
    *,
    level_index: int,
    level_tile_rows: Sequence[LevelTilePoolRange],
    tile_catalog: ClientTileCatalog,
    a_positions: Sequence[int],
    b_positions: Sequence[int],
    dup_a_types: int,
    dup_b_types: int,
    dup_seed: int,
    random_seed_group_id: int = 0,
    iap_protect_cut_a_count: int = 0,
) -> LegacyMaterialization:
    """Select the client pool and materialize A/B in one pure operation."""
    if isinstance(random_seed_group_id, bool) or not isinstance(random_seed_group_id, int):
        raise TypeError("randomSeed_groupID must be an integer")
    if random_seed_group_id > 0:
        raise ValueError("structured randomSeed_groupID > 0 is not supported")
    selection = select_legacy_pattern_pool(
        level_index=level_index,
        dup_a_types=dup_a_types,
        dup_b_types=dup_b_types,
        dup_seed=dup_seed,
        level_tile_rows=level_tile_rows,
        available_tile_ids=tile_catalog.available_tile_ids,
        normal_tile_ids=tile_catalog.normal_tile_ids,
    )
    return materialize_legacy_ab(
        pattern_pool=selection.pattern_pool,
        a_positions=a_positions,
        b_positions=b_positions,
        dup_a_types=dup_a_types,
        dup_b_types=dup_b_types,
        dup_seed=dup_seed,
        random_seed_group_id=random_seed_group_id,
        iap_protect_cut_a_count=iap_protect_cut_a_count,
        pool_selection=selection,
    )
