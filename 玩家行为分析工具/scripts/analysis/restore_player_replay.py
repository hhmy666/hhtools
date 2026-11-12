#!/usr/bin/env python3
"""将 Redash 的 replay_info 还原为玩家局内操作时间线。

还原器同时支持：
1. 旧版“均分图案 + 3 轮洗牌”随机分配；
2. 2.08 Android 发布分支中 randomSeed_groupID 启用的结构化分配。

注意：本工具还原“初始 TileNum -> 坐标 -> 图案”与操作顺序，
不模拟撤回、磁铁、抓钩、洗牌、复活等 Handler 对局面的动态效果。
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import math
import re
import struct
import sys
from collections import Counter, defaultdict
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional


sys.dont_write_bytecode = True

RANDOM_ID_A = -10
RANDOM_ID_B = -11
RANDOM_PLACEHOLDER_IDS = {RANDOM_ID_A, RANDOM_ID_B}
GOLDEN_SHELL_TILE_ID = 1010
ELIMINATE_COUNT = 3
INTERVAL_CAP_MS = 120_000
EXTRA_SPACE_ACTION_IDS = {508, 601, 708, 801}

DEFAULT_CONFIG_DIR = Path.home() / "ulinkconfig" / "csv"
DEFAULT_TEMPLATE_DIR = Path.home() / "tileleveltemplate" / "Template"
DEFAULT_TEMPLATE_READER_DIR = Path(__file__).resolve().parent

REPLAY_STEP_RE = re.compile(
    r"^(?P<interval_ms>\d+)#(?P<action_id>\d+)"
    r"(?::(?P<tile_nums>\d+(?:-\d+)*))?$"
)

SUMMARY_FIELDS = [
    "user_id",
    "event_time",
    "client_version",
    "platform",
    "level_index",
    "level_id",
    "level_template_id",
    "level_dup_para_id",
    "level_dup_para",
    "level_value_test",
    "level_part_index",
    "level_status",
    "level_revive_count",
    "level_space_revive_count",
    "level_time_revive_count",
    "result_category",
    "bag_full_event_count",
    "bag_full_process",
    "used_extra_space",
    "extra_space_action_ids",
    "total_steps",
    "click_steps",
    "prop_steps",
    "elapsed_ms_lower_bound",
    "elapsed_time_lower_bound",
    "has_120s_interval",
    "capped_interval_count",
    "missing_click_target_count",
    "unmapped_target_count",
    "invalid_step_count",
    "resolved_level_id",
    "resolved_dup_para",
    "config_mapping_source",
    "random_algorithm",
    "reconstruction_status",
]

TIMELINE_FIELDS = [
    "user_id",
    "event_time",
    "client_version",
    "platform",
    "level_index",
    "level_template_id",
    "step_no",
    "click_no",
    "raw_step",
    "interval_ms",
    "elapsed_ms",
    "elapsed_time",
    "elapsed_may_be_lower_bound",
    "action_id",
    "action_name",
    "asset_name",
    "is_extra_space",
    "tile_nums",
    "initial_tile_patterns",
    "initial_positions",
    "reconstruction_status",
]

MAPPING_FIELDS = [
    "level_index",
    "level_template_id",
    "resolved_level_id",
    "level_dup_para_id",
    "resolved_dup_para",
    "config_mapping_source",
    "random_algorithm",
    "reconstruction_status",
    "tile_num",
    "initial_x",
    "initial_y",
    "initial_z",
    "initial_layer",
    "tower_index",
    "source_tile_id",
    "initial_tile_id_after_random",
    "tile_name",
    "tile_icon",
    "is_choke_pattern",
]

ACTION_FIELDS = [
    "action_id",
    "action_name",
    "asset_name",
    "step_count",
    "game_count",
    "is_extra_space",
]


def parse_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        return default


def parse_pipe_ints(value: Any) -> list[int]:
    if value is None:
        return []
    result: list[int] = []
    for item in str(value).split("|"):
        parsed = parse_int(item)
        if parsed is not None:
            result.append(parsed)
    return result


def is_truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "是"}


def format_elapsed_ms(value: int) -> str:
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def float32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def non_metadata_rows(
    rows: Iterable[dict[str, str]], numeric_key: str
) -> list[dict[str, str]]:
    return [row for row in rows if parse_int(row.get(numeric_key)) is not None]


class CustomRandom:
    """复刻 CustomRandom.cs 的 uint32 溢出语义。"""

    CONST_MZ = 362_436_069

    def __init__(self, seed: int = 521_288_629) -> None:
        self.set_seed(seed)

    def set_seed(self, seed: int) -> None:
        self.mw = seed & 0xFFFFFFFF
        self.mz = self.CONST_MZ

    def get_random(self, maximum: int) -> int:
        if maximum <= 0:
            raise ValueError(f"maximum must be positive, got {maximum}")
        self.mz = (
            36_969 * (self.mz & 65_535) + (self.mz >> 16)
        ) & 0xFFFFFFFF
        self.mw = (
            18_000 * (self.mw & 65_535) + (self.mw >> 16)
        ) & 0xFFFFFFFF
        combined = (((self.mz << 16) & 0xFFFFFFFF) + self.mw) & 0xFFFFFFFF
        return combined % maximum


def game_seed(seed: int) -> int:
    return (1_411_242_022 + seed) & 0xFFFFFFFF


@dataclass
class TileState:
    tile_num: int
    x: int
    y: int
    z: int
    source_tile_id: int
    tile_id: int
    tower_index: Optional[int]
    is_choke_pattern: bool = False

    @property
    def is_tower(self) -> bool:
        return self.tower_index is not None


@dataclass
class ConfigResolution:
    level_index: int
    template_id: str
    dup_para_id: int
    input_level_id: str = ""
    resolved_level_id: str = ""
    dup_para: str = ""
    source: str = ""
    config_row: Optional[dict[str, str]] = None
    candidate_ids: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class Reconstruction:
    resolution: ConfigResolution
    tiles: list[TileState]
    algorithm: str
    status: str
    template_path: str
    random_seed_group_id: int = 0
    random_seed_id: int = 0


@dataclass
class SeedConfig:
    seed_id: int
    group_id: int
    choke_tile_num: int
    choke_start_layer: int
    choke_layer_gap: int
    normal_layer_gap: int
    stage_percents: list[int]
    stage_kind_nums: list[int]


class ConfigCatalog:
    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir

        level_rows = csv_rows(config_dir / "config_level.csv")
        self.level_rows = [
            row
            for row in level_rows
            if row.get("LevelId") not in {"", "string", "LevelId"}
            and row.get("TemplateId") not in {"", "string", "TemplateId"}
        ]
        self.level_by_id = {row["LevelId"]: row for row in self.level_rows}
        self.levels_by_template: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in self.level_rows:
            self.levels_by_template[row["TemplateId"]].append(row)

        self.level_list_rows = non_metadata_rows(
            csv_rows(config_dir / "config_level_list.csv"), "LevelIndex"
        )
        self.level_list_by_index = {
            int(row["LevelIndex"]): row for row in self.level_list_rows
        }

        self.value_test_rows = non_metadata_rows(
            csv_rows(config_dir / "config_level_value_test.csv"), "TestLevelIndex"
        )
        self.value_tests_by_index: dict[int, list[str]] = defaultdict(list)
        for row in self.value_test_rows:
            index = int(row["TestLevelIndex"])
            for level_id in str(row.get("LevelIdList") or "").split("|"):
                level_id = level_id.strip()
                if level_id:
                    self.value_tests_by_index[index].append(level_id)

        self.level_tile_rows = non_metadata_rows(
            csv_rows(config_dir / "config_client_level_tile.csv"), "StartLevel"
        )
        self.level_tile_rows.sort(key=lambda row: int(row["StartLevel"]))

        tile_rows = non_metadata_rows(
            csv_rows(config_dir / "config_client_tile.csv"), "TileId"
        )
        self.tile_by_id = {int(row["TileId"]): row for row in tile_rows}
        # TileConfigManager.BaseTiles in the 2.08 client is an explicit list,
        # not every config_client_tile row whose IconType happens to be 0.
        # Keep the fallback pool byte-for-byte aligned with the client because
        # its size and order both affect seeded random allocation.
        base_tile_ids = (
            *range(1000, 1010),
            *range(1011, 1015),
            1300,
            1301,
            1302,
            1304,
            1313,
        )
        self.normal_base_tile_ids = [
            tile_id for tile_id in base_tile_ids if tile_id in self.tile_by_id
        ]

        group_rows = non_metadata_rows(
            csv_rows(config_dir / "config_level_randomSeed_group.csv"), "GroupID"
        )
        self.seed_group_by_id = {int(row["GroupID"]): row for row in group_rows}

        seed_rows = non_metadata_rows(
            csv_rows(config_dir / "config_level_randomSeed.csv"), "seedID"
        )
        self.seed_by_id = {int(row["seedID"]): row for row in seed_rows}

        server_assets = non_metadata_rows(
            csv_rows(config_dir / "config_server_asset.csv"), "AssetId"
        )
        show_assets = non_metadata_rows(
            csv_rows(config_dir / "config_client_asset_show.csv"), "AssetId"
        )
        self.server_asset_by_id = {
            int(row["AssetId"]): row for row in server_assets
        }
        self.show_asset_by_id = {int(row["AssetId"]): row for row in show_assets}

    def _row_matches_template(
        self, level_id: str, template_id: str
    ) -> Optional[dict[str, str]]:
        row = self.level_by_id.get(level_id)
        if row and row.get("TemplateId") == template_id:
            return row
        return None

    def _dup_field(self, dup_para_id: int) -> str:
        normalized = max(1, min(10, dup_para_id))
        return f"DupPara{normalized}"

    def resolve_level(self, source: dict[str, str], default_dup: int) -> ConfigResolution:
        level_index = parse_int(source.get("level_index"), -1) or -1
        template_id = str(source.get("level_template_id") or "").strip()
        dup_para_id = parse_int(source.get("level_dup_para_id"), default_dup)
        if dup_para_id is None or dup_para_id <= 0:
            dup_para_id = default_dup

        input_level_id = str(source.get("level_id") or "").strip()
        input_dup_para = str(source.get("level_dup_para") or "").strip()
        resolution = ConfigResolution(
            level_index=level_index,
            template_id=template_id,
            dup_para_id=dup_para_id,
            input_level_id=input_level_id,
        )

        chosen: Optional[dict[str, str]] = None
        if input_level_id:
            chosen = self._row_matches_template(input_level_id, template_id)
            if chosen:
                resolution.source = "input_level_id"
            else:
                resolution.error = "input level_id 在当前配置中不存在或模板不一致"

        if chosen is None and not input_level_id:
            value_test_candidates = [
                self._row_matches_template(level_id, template_id)
                for level_id in self.value_tests_by_index.get(level_index, [])
            ]
            value_test_candidates = [row for row in value_test_candidates if row]

            marked_value_test = is_truthy(source.get("level_value_test"))
            if marked_value_test and len(value_test_candidates) == 1:
                chosen = value_test_candidates[0]
                resolution.source = "config_level_value_test"

        # config_level_list is the authoritative current level routing.  A
        # similarly named config_level row can be a historical generation and
        # must not win merely because its name embeds the current level index.
        if chosen is None and not input_level_id:
            listed = self.level_list_by_index.get(level_index)
            if listed:
                listed_ids = [
                    listed.get("LevelId", ""),
                    listed.get("DDALevelId1", ""),
                    listed.get("DDALevelId2", ""),
                    listed.get("DDALevelId3", ""),
                ]
                listed_matches = {
                    row["LevelId"]: row
                    for level_id in listed_ids
                    if (row := self._row_matches_template(level_id, template_id))
                }
                if len(listed_matches) == 1:
                    chosen = next(iter(listed_matches.values()))
                    resolution.source = "config_level_list_inferred"

        if chosen is None and not input_level_id:
            value_test_candidates = [
                self._row_matches_template(level_id, template_id)
                for level_id in self.value_tests_by_index.get(level_index, [])
            ]
            value_test_candidates = [row for row in value_test_candidates if row]
            explicitly_not_value_test = (
                str(source.get("level_value_test") or "").strip() != ""
                and not is_truthy(source.get("level_value_test"))
            )
            if len(value_test_candidates) == 1 and not explicitly_not_value_test:
                chosen = value_test_candidates[0]
                resolution.source = "config_level_value_test_inferred"

        if chosen is None and not input_level_id:
            exact_id = f"list{level_index}r3v1{template_id}"
            exact_row = self._row_matches_template(exact_id, template_id)
            if exact_row is not None:
                chosen = exact_row
                resolution.source = "level_index_id_pattern_inferred"

        template_candidates = self.levels_by_template.get(template_id, [])
        resolution.candidate_ids = [row["LevelId"] for row in template_candidates]

        if chosen is None and input_dup_para:
            resolution.dup_para = input_dup_para
            resolution.source = "input_level_dup_para_only"
        elif chosen is None and template_candidates:
            dup_field = self._dup_field(dup_para_id)
            signatures = {
                (
                    row.get(dup_field, ""),
                    parse_int(row.get("randomSeed_groupID"), 0) or 0,
                )
                for row in template_candidates
            }
            if len(signatures) == 1:
                chosen = template_candidates[0]
                resolution.source = "template_unique_dup_and_seed_group_inferred"

        if chosen is not None:
            resolution.config_row = chosen
            resolution.resolved_level_id = chosen["LevelId"]
            resolution.dup_para = input_dup_para or str(
                chosen.get(self._dup_field(dup_para_id)) or ""
            ).strip()

        if not resolution.dup_para:
            if not resolution.error:
                resolution.error = "无法唯一确定 config_level 行与 DupPara"
        elif len(resolution.dup_para.split("|")) < 4:
            resolution.error = f"DupPara 格式异常: {resolution.dup_para}"

        return resolution

    def level_tile_pool(self, level_index: int) -> tuple[list[int], list[int]]:
        candidates = [
            row
            for row in self.level_tile_rows
            if int(row["StartLevel"]) <= level_index
        ]
        if not candidates:
            raise ValueError(f"关卡 {level_index} 没有可用的 config_client_level_tile 行")
        selected = candidates[-1]
        return (
            parse_pipe_ints(selected.get("TileList1")),
            parse_pipe_ints(selected.get("TileList2")),
        )

    def seed_config(
        self, config_row: Optional[dict[str, str]], dup_para_id: int
    ) -> Optional[SeedConfig]:
        if not config_row:
            return None
        group_id = parse_int(config_row.get("randomSeed_groupID"), 0) or 0
        if group_id <= 0:
            return None
        group_row = self.seed_group_by_id.get(group_id)
        if not group_row:
            raise ValueError(f"缺少 randomSeed group {group_id}")
        field_name = self._dup_field(dup_para_id)
        seed_id = parse_int(group_row.get(field_name), 0) or 0
        seed_row = self.seed_by_id.get(seed_id)
        if not seed_row:
            raise ValueError(f"缺少 randomSeed {seed_id}")
        return SeedConfig(
            seed_id=seed_id,
            group_id=group_id,
            choke_tile_num=parse_int(seed_row.get("ChokeTile_num"), 0) or 0,
            choke_start_layer=parse_int(seed_row.get("ChokeTile_startLayer"), 0)
            or 0,
            choke_layer_gap=parse_int(seed_row.get("ChokeTile_param"), 0) or 0,
            normal_layer_gap=parse_int(seed_row.get("NormalTile_param"), 0) or 0,
            stage_percents=parse_pipe_ints(seed_row.get("Stage_param")),
            stage_kind_nums=parse_pipe_ints(seed_row.get("TileKind_num")),
        )

    def action_info(self, action_id: int) -> tuple[str, str]:
        if action_id == 1:
            return "点击", ""
        if action_id == 2:
            return "局前 Booster", ""
        show = self.show_asset_by_id.get(action_id, {})
        server = self.server_asset_by_id.get(action_id, {})
        action_name = str(show.get("Des") or server.get("Des") or f"操作 {action_id}")
        asset_name = str(server.get("Name") or "")
        return action_name, asset_name


def select_candidate_patterns(
    catalog: ConfigCatalog,
    level_index: int,
    tile_type_count_a: int,
    tile_type_count_b: int,
    random_seed: int,
) -> list[int]:
    need = max(1, tile_type_count_a, tile_type_count_b)
    list1, list2 = catalog.level_tile_pool(level_index)
    normals = list(catalog.normal_base_tile_ids)
    selected: list[int] = []
    random = CustomRandom(game_seed(random_seed))

    while len(selected) < need and list1:
        tile_id = list1.pop(random.get_random(len(list1)))
        if tile_id in list2:
            list2.remove(tile_id)
        if tile_id in normals:
            normals.remove(tile_id)
        if tile_id in catalog.tile_by_id:
            selected.append(tile_id)

    while len(selected) < need and list2:
        tile_id = list2.pop(random.get_random(len(list2)))
        if tile_id in normals:
            normals.remove(tile_id)
        if tile_id in catalog.tile_by_id:
            selected.append(tile_id)

    while len(selected) < need and normals:
        selected.append(normals.pop(random.get_random(len(normals))))

    if len(selected) < need:
        raise ValueError(f"图案池不足: 需要 {need}，实际 {len(selected)}")
    return selected


def shuffled_legacy_types(
    candidates: list[int], tile_type_count: int, tile_count: int, seed: int
) -> list[int]:
    tile_type_count = max(1, tile_type_count)
    if len(candidates) < tile_type_count:
        return []

    types = sorted(candidates[:tile_type_count], reverse=True)
    group_count = tile_count // ELIMINATE_COUNT
    total_weight = len(types) * 100
    counts: dict[int, int] = {}
    assigned = 0
    for tile_id in types:
        ratio = float32(float32(100.0) / float32(float(total_weight)))
        current = math.floor(float32(ratio * float32(float(group_count))))
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
        return []
    for index in range(left):
        counts[types[index]] += 1

    result: list[int] = []
    for tile_id, count in counts.items():
        result.extend([tile_id] * count * ELIMINATE_COUNT)

    random = CustomRandom(game_seed(seed))
    for _ in range(3):
        for index in range(len(result)):
            other = random.get_random(len(result))
            result[index], result[other] = result[other], result[index]
    return result


def assign_legacy(
    tiles: list[TileState], candidates: list[int], count_a: int, count_b: int, seed: int
) -> None:
    for placeholder, count in ((RANDOM_ID_A, count_a), (RANDOM_ID_B, count_b)):
        targets = [tile for tile in tiles if tile.tile_id == placeholder]
        if not targets:
            continue
        assigned = shuffled_legacy_types(candidates, count, len(targets), seed)
        if len(assigned) < len(targets):
            continue
        for tile, tile_id in zip(targets, assigned):
            tile.tile_id = tile_id


def classify_patterns(
    candidates: list[int], tile_type_count: int, choke_count: int, random: CustomRandom
) -> tuple[list[int], list[int]]:
    pool = list(candidates[: min(tile_type_count, len(candidates))])
    choke_pool = [tile_id for tile_id in pool if tile_id != GOLDEN_SHELL_TILE_ID]
    actual = min(choke_count, len(choke_pool))
    choke: list[int] = []
    for _ in range(actual):
        choke.append(choke_pool.pop(random.get_random(len(choke_pool))))
    choke_set = set(choke)
    return choke, [tile_id for tile_id in pool if tile_id not in choke_set]


def distribute_to_layers(
    count: int,
    layers: list[int],
    capacity: dict[int, int],
    result: dict[int, int],
    random: CustomRandom,
) -> None:
    if not layers or count <= 0:
        return
    per_layer, remainder = divmod(count, len(layers))
    shuffled = list(layers)
    for index in range(len(shuffled) - 1, 0, -1):
        other = random.get_random(index + 1)
        shuffled[index], shuffled[other] = shuffled[other], shuffled[index]

    distributed = 0
    for index, layer in enumerate(shuffled):
        if distributed >= count:
            break
        allocation = per_layer + (1 if index < remainder else 0)
        allocation = min(allocation, capacity[layer])
        result[layer] = result.get(layer, 0) + allocation
        distributed += allocation

    if distributed < count:
        for layer in shuffled:
            if distributed >= count:
                break
            extra = capacity[layer] - result.get(layer, 0)
            if extra <= 0:
                continue
            addition = min(count - distributed, extra)
            result[layer] = result.get(layer, 0) + addition
            distributed += addition


def available_choke_layers(
    capacity: dict[int, int], max_z: int, exclude_top: int
) -> list[int]:
    threshold = max_z - exclude_top + 1
    return sorted(
        layer for layer, count in capacity.items() if layer <= threshold and count > 0
    )


def valid_gap_positions(layers: list[int], required_gap: int) -> list[tuple[int, int]]:
    adjacent = [
        (index, index + 1)
        for index in range(len(layers) - 1)
        if layers[index + 1] - layers[index] >= required_gap
    ]
    if adjacent:
        return adjacent
    return [
        (left, right)
        for left in range(len(layers) - 1)
        for right in range(left + 1, len(layers))
        if layers[right] - layers[left] >= required_gap
    ]


def split_not_divisible_by_three(
    total: int, random: CustomRandom
) -> tuple[int, int]:
    if total <= 1:
        return total, 0
    lower = 1 + random.get_random(total - 1)
    upper = total - lower
    for _ in range(total):
        if lower % 3 != 0 and upper % 3 != 0:
            break
        lower += 1
        if lower >= total:
            lower = 1
        upper = total - lower
    return lower, upper


def adjust_for_capacity(
    total: int, lower_capacity: int, upper_capacity: int
) -> tuple[int, int]:
    total_capacity = lower_capacity + upper_capacity
    if total_capacity < total:
        return -1, -1
    lower = total * lower_capacity // total_capacity
    upper = total - lower
    if lower > lower_capacity:
        lower = lower_capacity
        upper = total - lower
    if upper > upper_capacity:
        upper = upper_capacity
        lower = total - upper
    for _ in range(6):
        if lower % 3 != 0 and upper % 3 != 0:
            break
        if lower > 1 and upper < upper_capacity:
            lower -= 1
            upper += 1
        elif upper > 1 and lower < lower_capacity:
            upper -= 1
            lower += 1
        else:
            break
    return lower, upper


def try_choke_plan(
    total: int,
    layers: list[int],
    capacity: dict[int, int],
    required_gap: int,
    random: CustomRandom,
) -> Optional[dict[int, int]]:
    positions = valid_gap_positions(layers, required_gap)
    if not positions:
        return None
    lower_last, upper_first = positions[random.get_random(len(positions))]
    lower_layers = layers[: lower_last + 1]
    upper_layers = layers[upper_first:]
    lower_count, upper_count = split_not_divisible_by_three(total, random)
    lower_capacity = sum(capacity[layer] for layer in lower_layers)
    upper_capacity = sum(capacity[layer] for layer in upper_layers)
    if lower_count > lower_capacity or upper_count > upper_capacity:
        lower_count, upper_count = adjust_for_capacity(
            total, lower_capacity, upper_capacity
        )
        if lower_count < 0 or upper_count < 0:
            return None
    result: dict[int, int] = {}
    distribute_to_layers(lower_count, lower_layers, capacity, result, random)
    distribute_to_layers(upper_count, upper_layers, capacity, result, random)
    if sum(result.values()) < total:
        return None
    return result


def plan_choke_layers(
    total: int,
    capacity: dict[int, int],
    start_layer: int,
    layer_gap: int,
    max_z: int,
    random: CustomRandom,
) -> dict[int, int]:
    if total <= 0:
        return {}
    exclude_top = start_layer
    current_gap = layer_gap
    while True:
        layers = available_choke_layers(capacity, max_z, exclude_top)
        if not layers:
            if exclude_top > 0:
                exclude_top -= 1
                continue
            return {}
        result = try_choke_plan(total, layers, capacity, current_gap, random)
        if result is not None:
            return result
        if exclude_top > 0:
            exclude_top -= 1
        elif current_gap > 1:
            current_gap -= 1
        else:
            fallback: dict[int, int] = {}
            distribute_to_layers(total, layers, capacity, fallback, random)
            return fallback


def plan_consecutive_layers(
    total: int, capacity: dict[int, int], consecutive_layers: int
) -> tuple[dict[int, int], int]:
    if total <= 0:
        return {}, 0
    layers = sorted(layer for layer, count in capacity.items() if count > 0)
    if not layers:
        return {}, total
    target_count = max(1, consecutive_layers)
    target_count = min(target_count, total, len(layers))
    per_layer, remainder = divmod(total, target_count)
    result: dict[int, int] = {}
    overflow = 0
    for index in range(target_count):
        layer = layers[index]
        ideal = per_layer if index < target_count - remainder else per_layer + 1
        wanted = ideal + overflow
        actual = min(wanted, capacity[layer])
        if actual > 0:
            result[layer] = actual
        overflow = wanted - actual
    if overflow > 0:
        for layer in layers[target_count:]:
            if overflow <= 0:
                break
            actual = min(overflow, capacity[layer])
            if actual > 0:
                result[layer] = result.get(layer, 0) + actual
            overflow -= actual
    if overflow > 0:
        for layer in layers:
            if overflow <= 0:
                break
            available = capacity[layer] - result.get(layer, 0)
            if available <= 0:
                continue
            actual = min(overflow, available)
            result[layer] = result.get(layer, 0) + actual
            overflow -= actual
    return result, overflow


def pattern_quotas(
    total_tiles: int, choke: list[int], non_choke: list[int]
) -> dict[int, int]:
    patterns = list(choke) + list(non_choke)
    if not patterns:
        return {}
    groups = total_tiles // ELIMINATE_COUNT
    per_pattern, remainder = divmod(groups, len(patterns))
    return {
        tile_id: (per_pattern + (1 if index < remainder else 0))
        * ELIMINATE_COUNT
        for index, tile_id in enumerate(patterns)
    }


def group_patterns_by_stage(
    patterns: list[int], stage_percents: list[int]
) -> list[list[int]]:
    if not patterns:
        return [[]]
    if not stage_percents:
        return [list(patterns)]
    total = len(patterns)
    assigned = 0
    percent_sum = sum(stage_percents) or 100
    groups: list[list[int]] = []
    for index, percent in enumerate(stage_percents):
        if index == len(stage_percents) - 1:
            count = total - assigned
        else:
            count = (total * percent * 2 + percent_sum) // (percent_sum * 2)
            count = min(count, total - assigned)
        count = max(0, count)
        groups.append(patterns[assigned : assigned + count])
        assigned += count
    return groups


def shuffle_fisher_yates(items: list[Any], random: CustomRandom) -> None:
    for index in range(len(items) - 1, 0, -1):
        other = random.get_random(index + 1)
        items[index], items[other] = items[other], items[index]


def assign_layer_slots(
    sorted_tiles: list[TileState],
    assignments: list[tuple[int, dict[int, int], bool]],
    random: CustomRandom,
) -> None:
    layer_tiles: dict[int, list[TileState]] = {}
    for tile in sorted_tiles:
        layer_tiles.setdefault(tile.z, []).append(tile)
    layer_slots: dict[int, list[tuple[int, bool]]] = {}
    for pattern_id, layer_counts, is_choke in assignments:
        for layer, count in layer_counts.items():
            layer_slots.setdefault(layer, []).extend(
                [(pattern_id, is_choke)] * count
            )
    for slots in layer_slots.values():
        shuffle_fisher_yates(slots, random)
    for layer, tiles in layer_tiles.items():
        slots = layer_slots.get(layer, [])
        for tile, (pattern_id, is_choke) in zip(tiles, slots):
            tile.tile_id = pattern_id
            tile.is_choke_pattern = is_choke


def ensure_assigned_and_divisible(
    tiles: list[TileState], non_choke: list[int], random: CustomRandom
) -> None:
    if not non_choke:
        return
    pattern_tiles: dict[int, list[TileState]] = {}
    orphans: list[TileState] = []
    for tile in tiles:
        if tile.tile_id < 0:
            orphans.append(tile)
        else:
            pattern_tiles.setdefault(tile.tile_id, []).append(tile)
    for grouped_tiles in pattern_tiles.values():
        excess = len(grouped_tiles) % ELIMINATE_COUNT
        for index in range(excess):
            orphans.append(grouped_tiles[-1 - index])
    if not orphans:
        return
    cursor = 0
    for _ in range(len(orphans) // ELIMINATE_COUNT):
        pattern_id = non_choke[random.get_random(len(non_choke))]
        for _ in range(ELIMINATE_COUNT):
            orphans[cursor].tile_id = pattern_id
            orphans[cursor].is_choke_pattern = False
            cursor += 1
    if cursor < len(orphans):
        counts = Counter(tile.tile_id for tile in tiles if tile.tile_id > 0)
        best = non_choke[0]
        best_count = 0
        for pattern_id in non_choke:
            count = counts.get(pattern_id, 0)
            if count > best_count:
                best = pattern_id
                best_count = count
        for tile in orphans[cursor:]:
            tile.tile_id = best
            tile.is_choke_pattern = False


def allocate_structured_group(
    tiles: list[TileState],
    placeholder: int,
    choke: list[int],
    non_choke: list[int],
    config: SeedConfig,
    random: CustomRandom,
) -> None:
    formation = sorted(
        [
            tile
            for tile in tiles
            if tile.source_tile_id == placeholder and not tile.is_tower
        ],
        key=lambda tile: (tile.z, tile.y, tile.x),
    )
    tower = [
        tile
        for tile in tiles
        if tile.source_tile_id == placeholder and tile.is_tower
    ]
    if not formation:
        return

    capacity: dict[int, int] = {}
    for tile in formation:
        capacity[tile.z] = capacity.get(tile.z, 0) + 1
    max_z = max(capacity)
    effective_choke = choke if placeholder == RANDOM_ID_A else []
    quotas = pattern_quotas(len(formation) + len(tower), effective_choke, non_choke)
    remaining = dict(capacity)
    assignments: list[tuple[int, dict[int, int], bool]] = []

    for pattern_id in effective_choke:
        layer_counts = plan_choke_layers(
            quotas.get(pattern_id, 0),
            remaining,
            config.choke_start_layer,
            config.choke_layer_gap,
            max_z,
            random,
        )
        assignments.append((pattern_id, layer_counts, True))
        for layer, count in layer_counts.items():
            remaining[layer] -= count

    formation_remaining = sum(remaining.values())
    total_non_choke_quota = sum(quotas.get(item, 0) for item in non_choke)
    denominator = formation_remaining + len(tower)
    formation_total = (
        total_non_choke_quota * formation_remaining // denominator
        if denominator > 0
        else 0
    )
    if non_choke:
        per_pattern, formation_remainder = divmod(formation_total, len(non_choke))
    else:
        per_pattern, formation_remainder = 0, 0

    pattern_groups = group_patterns_by_stage(non_choke, config.stage_percents)
    shuffled_tower = list(tower)
    shuffle_fisher_yates(shuffled_tower, random)
    tower_cursor = 0
    normal_index = 0
    for group_index, group in enumerate(pattern_groups):
        consecutive = (
            config.stage_kind_nums[group_index]
            if group_index < len(config.stage_kind_nums)
            else 1
        )
        for pattern_id in group:
            quota = quotas.get(pattern_id, 0)
            formation_quota = per_pattern + (
                1 if normal_index < formation_remainder else 0
            )
            tower_quota = quota - formation_quota
            for _ in range(max(0, tower_quota)):
                if tower_cursor >= len(shuffled_tower):
                    break
                shuffled_tower[tower_cursor].tile_id = pattern_id
                shuffled_tower[tower_cursor].is_choke_pattern = False
                tower_cursor += 1

            layer_counts, overflow = plan_consecutive_layers(
                formation_quota, remaining, consecutive
            )
            for _ in range(overflow):
                if tower_cursor >= len(shuffled_tower):
                    break
                shuffled_tower[tower_cursor].tile_id = pattern_id
                shuffled_tower[tower_cursor].is_choke_pattern = False
                tower_cursor += 1
            assignments.append((pattern_id, layer_counts, False))
            for layer, count in layer_counts.items():
                remaining[layer] -= count
            normal_index += 1

    if non_choke:
        while tower_cursor < len(shuffled_tower):
            pattern_id = non_choke[random.get_random(len(non_choke))]
            shuffled_tower[tower_cursor].tile_id = pattern_id
            shuffled_tower[tower_cursor].is_choke_pattern = False
            tower_cursor += 1

    assign_layer_slots(formation, assignments, random)
    ensure_assigned_and_divisible(formation + shuffled_tower, non_choke, random)


def assign_structured(
    tiles: list[TileState],
    candidates: list[int],
    count_a: int,
    count_b: int,
    seed: int,
    config: SeedConfig,
) -> None:
    random = CustomRandom(game_seed(seed))
    choke, non_choke = classify_patterns(
        candidates, max(count_a, count_b), config.choke_tile_num, random
    )
    allocate_structured_group(
        tiles, RANDOM_ID_A, choke, non_choke, config, random
    )
    allocate_structured_group(
        tiles, RANDOM_ID_B, choke, non_choke, config, random
    )


class ReplayRestorer:
    def __init__(
        self,
        catalog: ConfigCatalog,
        template_dir: Path,
        template_reader_dir: Path,
        iap_cut_a: int,
    ) -> None:
        self.catalog = catalog
        self.template_dir = template_dir
        self.iap_cut_a = max(0, iap_cut_a)
        if str(template_reader_dir) not in sys.path:
            sys.path.insert(0, str(template_reader_dir))
        from puzzle_core.template_reader import parse_level_file

        self.parse_level_file = parse_level_file
        self.template_cache: dict[str, tuple[str, list[TileState]]] = {}
        self.reconstruction_cache: dict[tuple[Any, ...], Reconstruction] = {}

    def load_template(self, template_id: str) -> tuple[str, list[TileState]]:
        if template_id in self.template_cache:
            path, cached = self.template_cache[template_id]
            return path, [TileState(**vars(tile)) for tile in cached]
        path = self.template_dir / f"{template_id}_m.bytes"
        if not path.is_file():
            raise FileNotFoundError(f"模板不存在: {path}")
        level = self.parse_level_file(path)
        tiles = [
            TileState(
                tile_num=tile.index + 1,
                x=tile.x,
                y=tile.y,
                z=tile.z,
                source_tile_id=tile.tile_id,
                tile_id=tile.tile_id,
                tower_index=tile.tower_index,
            )
            for tile in level.tiles
        ]
        self.template_cache[template_id] = (
            str(path),
            [TileState(**vars(tile)) for tile in tiles],
        )
        return str(path), tiles

    def reconstruct(self, resolution: ConfigResolution) -> Reconstruction:
        cache_key = (
            resolution.level_index,
            resolution.template_id,
            resolution.resolved_level_id,
            resolution.dup_para_id,
            resolution.dup_para,
            resolution.source,
            resolution.error,
            self.iap_cut_a,
        )
        if cache_key in self.reconstruction_cache:
            cached = self.reconstruction_cache[cache_key]
            return Reconstruction(
                resolution=resolution,
                tiles=[TileState(**vars(tile)) for tile in cached.tiles],
                algorithm=cached.algorithm,
                status=cached.status,
                template_path=cached.template_path,
                random_seed_group_id=cached.random_seed_group_id,
                random_seed_id=cached.random_seed_id,
            )

        try:
            template_path, tiles = self.load_template(resolution.template_id)
        except Exception as error:  # 将单个模板问题留在质量列，不中断全批。
            result = Reconstruction(
                resolution=resolution,
                tiles=[],
                algorithm="未执行",
                status=f"未完成_模板解析失败: {error}",
                template_path="",
            )
            self.reconstruction_cache[cache_key] = result
            return result

        if resolution.error or not resolution.dup_para:
            result = Reconstruction(
                resolution=resolution,
                tiles=tiles,
                algorithm="未执行",
                status=f"未完成_{resolution.error or '配置不明'}",
                template_path=template_path,
            )
            self.reconstruction_cache[cache_key] = result
            return result

        parts = [parse_int(item) for item in resolution.dup_para.split("|")]
        if len(parts) < 4 or any(item is None for item in parts[:4]):
            result = Reconstruction(
                resolution=resolution,
                tiles=tiles,
                algorithm="未执行",
                status=f"未完成_DupPara异常: {resolution.dup_para}",
                template_path=template_path,
            )
            self.reconstruction_cache[cache_key] = result
            return result

        raw_count_a = max(1, int(parts[0]))
        count_a = max(1, raw_count_a - self.iap_cut_a)
        seed = int(parts[1])
        count_b = max(1, int(parts[3]))
        try:
            patterns = select_candidate_patterns(
                self.catalog,
                resolution.level_index,
                raw_count_a,
                count_b,
                seed,
            )
            seed_config = self.catalog.seed_config(
                resolution.config_row, resolution.dup_para_id
            )
            if seed_config is None:
                algorithm = "legacy_equal_weight_3x_shuffle"
                assign_legacy(tiles, patterns, count_a, count_b, seed)
                group_id = 0
                seed_id = 0
            else:
                algorithm = "structured_random_seed_allocator"
                assign_structured(
                    tiles, patterns, count_a, count_b, seed, seed_config
                )
                group_id = seed_config.group_id
                seed_id = seed_config.seed_id
        except Exception as error:
            result = Reconstruction(
                resolution=resolution,
                tiles=tiles,
                algorithm="执行失败",
                status=f"未完成_随机分配失败: {error}",
                template_path=template_path,
            )
            self.reconstruction_cache[cache_key] = result
            return result

        # -12 .. -23 are authored C .. N structure tiles, not unresolved
        # random placeholders.  Only A/B (-10/-11) are replaced by the random
        # assignment algorithms above.
        remaining = sum(tile.tile_id in RANDOM_PLACEHOLDER_IDS for tile in tiles)
        structure_group_count = sum(-23 <= tile.tile_id <= -12 for tile in tiles)
        if remaining:
            status = f"部分完成_剩余{remaining}个未确定占位Tile"
        elif resolution.source in {"input_level_id", "input_level_dup_para_only"}:
            status = "已还原_使用输入实际配置"
        else:
            status = "已还原_使用当前配置推断"
        if not remaining and structure_group_count:
            status += f"_保留{structure_group_count}个C-N结构牌"
        result = Reconstruction(
            resolution=resolution,
            tiles=tiles,
            algorithm=algorithm,
            status=status,
            template_path=template_path,
            random_seed_group_id=group_id,
            random_seed_id=seed_id,
        )
        self.reconstruction_cache[cache_key] = Reconstruction(
            resolution=resolution,
            tiles=[TileState(**vars(tile)) for tile in tiles],
            algorithm=algorithm,
            status=status,
            template_path=template_path,
            random_seed_group_id=group_id,
            random_seed_id=seed_id,
        )
        return result


def parse_replay(replay_info: str) -> list[dict[str, Any]]:
    elapsed = 0
    lower_bound = False
    steps: list[dict[str, Any]] = []
    for index, raw_step in enumerate(str(replay_info or "").split("|"), start=1):
        match = REPLAY_STEP_RE.fullmatch(raw_step.strip())
        if not match:
            steps.append(
                {
                    "step_no": index,
                    "raw_step": raw_step,
                    "valid": False,
                    "interval_ms": None,
                    "elapsed_ms": elapsed,
                    "elapsed_lower_bound": lower_bound,
                    "action_id": None,
                    "tile_nums": [],
                }
            )
            continue
        interval = int(match.group("interval_ms"))
        elapsed += interval
        if interval == INTERVAL_CAP_MS:
            lower_bound = True
        tile_text = match.group("tile_nums")
        tile_nums = [int(item) for item in tile_text.split("-")] if tile_text else []
        steps.append(
            {
                "step_no": index,
                "raw_step": raw_step,
                "valid": True,
                "interval_ms": interval,
                "elapsed_ms": elapsed,
                "elapsed_lower_bound": lower_bound,
                "action_id": int(match.group("action_id")),
                "tile_nums": tile_nums,
            }
        )
    return steps


def classify_result(source: dict[str, str]) -> str:
    supplied = str(source.get("result_category") or "").strip()
    if supplied:
        return supplied
    status = str(source.get("level_status") or "").strip().lower()
    bag_full_count = parse_int(source.get("bag_full_event_count"), 0) or 0
    space_revive_count = (
        parse_int(source.get("level_space_revive_count"), 0) or 0
    )
    if status in {"bag_full_fail", "bagfull_fail"}:
        return "背包满失败"
    if (
        status in {"pass", "win", "success"}
        and bag_full_count > 0
        and space_revive_count > 0
    ):
        return "背包满后复活并通关"
    if status in {"pass", "win", "success"}:
        return "通关"
    if (
        status in {"fail", "failed", "lose", "loss"}
        and bag_full_count > 0
        and space_revive_count > 0
    ):
        return "背包满后复活但最终失败"
    if status in {"fail", "failed", "lose", "loss"} and bag_full_count > 0:
        return "背包满相关失败"
    if status in {"fail", "failed", "lose", "loss"}:
        return "其他失败"
    if status in {"exit", "quit"}:
        return "退出"
    return "未提供" if not status else "未知"


def classify_bag_full_process(source: dict[str, str]) -> str:
    supplied = str(source.get("bag_full_process") or "").strip()
    if supplied:
        return supplied
    if "bag_full_event_count" not in source:
        return "未提供"
    bag_full_count = parse_int(source.get("bag_full_event_count"), 0) or 0
    if bag_full_count <= 0:
        return "未发生背包满"
    space_revive_count = (
        parse_int(source.get("level_space_revive_count"), 0) or 0
    )
    if space_revive_count > 0:
        return "背包满后空间复活/继续"
    revive_count = parse_int(source.get("level_revive_count"), 0) or 0
    time_revive_count = parse_int(source.get("level_time_revive_count"), 0) or 0
    if revive_count + time_revive_count > 0:
        return "背包满后发生其他或类型未知的复活"
    return "背包满后未记录复活"


def tile_text(
    tile_num: int,
    tile_by_num: dict[int, TileState],
    tile_meta: dict[int, dict[str, str]],
) -> tuple[str, str, bool]:
    tile = tile_by_num.get(tile_num)
    if tile is None:
        return f"{tile_num}=越界", f"{tile_num}=越界", False
    meta = tile_meta.get(tile.tile_id, {})
    if tile.tile_id > 0:
        name = str(meta.get("Des") or "未配置名称")
        icon = str(meta.get("Icon") or "")
        pattern = f"{tile_num}={tile.tile_id}/{name}/{icon}"
        mapped = bool(meta)
    else:
        if tile.tile_id == RANDOM_ID_A:
            placeholder = "A占位"
        elif tile.tile_id == RANDOM_ID_B:
            placeholder = "B占位"
        elif -23 <= tile.tile_id <= -12:
            placeholder = f"{chr(ord('C') + (-12 - tile.tile_id))}结构牌"
        else:
            placeholder = "未识别负数Tile"
        pattern = f"{tile_num}={tile.tile_id}/{placeholder}"
        mapped = False
    tower = "非塔" if tile.tower_index is None else f"塔{tile.tower_index + 1}"
    position = f"{tile_num}=({tile.x},{tile.y},{tile.z})/{tower}"
    return pattern, position, mapped


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.DictWriter(
            target,
            fieldnames=fields,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def write_ndjson(path: Path, rows: Iterable[list[Any]]) -> None:
    with path.open("w", encoding="utf-8") as target:
        for row in rows:
            target.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            target.write("\n")


def restore_file(args: argparse.Namespace) -> dict[str, Any]:
    input_path = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = (
        Path(args.staging_dir).expanduser().resolve() if args.staging_dir else None
    )
    if staging_dir:
        staging_dir.mkdir(parents=True, exist_ok=True)

    source_rows = csv_rows(input_path)
    required = {
        "user_id",
        "event_time",
        "client_version",
        "platform",
        "level_index",
        "level_template_id",
        "replay_info",
    }
    missing = required - set(source_rows[0] if source_rows else [])
    if missing:
        raise ValueError(f"输入 CSV 缺少字段: {', '.join(sorted(missing))}")

    catalog = ConfigCatalog(Path(args.config_dir).expanduser().resolve())
    restorer = ReplayRestorer(
        catalog=catalog,
        template_dir=Path(args.template_dir).expanduser().resolve(),
        template_reader_dir=Path(args.template_reader_dir).expanduser().resolve(),
        iap_cut_a=args.iap_cut_a,
    )

    summary_rows: list[dict[str, Any]] = []
    action_step_counts: Counter[int] = Counter()
    action_game_counts: Counter[int] = Counter()
    result_counts: Counter[str] = Counter()
    algorithm_game_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    level_game_counts: Counter[int] = Counter()
    extra_space_game_counts: Counter[int] = Counter()
    quality = Counter()
    mapping_keys_seen: set[tuple[Any, ...]] = set()
    mapping_rows: list[dict[str, Any]] = []

    timeline_path = output_dir / "玩家操作时间线.csv.gz"
    staging_handles: dict[int, Any] = {}
    staging_paths: dict[int, str] = {}
    staging_counts: Counter[int] = Counter()

    with ExitStack() as stack:
        timeline_binary = stack.enter_context(timeline_path.open("wb"))
        timeline_target = stack.enter_context(
            io.TextIOWrapper(
                gzip.GzipFile(
                    filename="",
                    mode="wb",
                    fileobj=timeline_binary,
                    mtime=0,
                ),
                encoding="utf-8-sig",
                newline="",
            )
        )
        timeline_writer = csv.DictWriter(
            timeline_target,
            fieldnames=TIMELINE_FIELDS,
            extrasaction="ignore",
            lineterminator="\n",
        )
        timeline_writer.writeheader()

        for source in source_rows:
            level_index = parse_int(source.get("level_index"), -1) or -1
            level_game_counts[level_index] += 1
            resolution = catalog.resolve_level(source, args.dup_index)
            reconstruction = restorer.reconstruct(resolution)
            algorithm_game_counts[reconstruction.algorithm] += 1
            status_counts[reconstruction.status] += 1
            tile_by_num = {tile.tile_num: tile for tile in reconstruction.tiles}

            mapping_key = (
                resolution.level_index,
                resolution.template_id,
                resolution.resolved_level_id,
                resolution.dup_para_id,
                resolution.dup_para,
                reconstruction.algorithm,
                reconstruction.status,
            )
            if mapping_key not in mapping_keys_seen:
                mapping_keys_seen.add(mapping_key)
                for tile in reconstruction.tiles:
                    meta = catalog.tile_by_id.get(tile.tile_id, {})
                    mapping_rows.append(
                        {
                            "level_index": resolution.level_index,
                            "level_template_id": resolution.template_id,
                            "resolved_level_id": resolution.resolved_level_id,
                            "level_dup_para_id": resolution.dup_para_id,
                            "resolved_dup_para": resolution.dup_para,
                            "config_mapping_source": resolution.source,
                            "random_algorithm": reconstruction.algorithm,
                            "reconstruction_status": reconstruction.status,
                            "tile_num": tile.tile_num,
                            "initial_x": tile.x,
                            "initial_y": tile.y,
                            "initial_z": tile.z,
                            "initial_layer": tile.z + 1,
                            "tower_index": ""
                            if tile.tower_index is None
                            else tile.tower_index + 1,
                            "source_tile_id": tile.source_tile_id,
                            "initial_tile_id_after_random": tile.tile_id,
                            "tile_name": meta.get("Des", ""),
                            "tile_icon": meta.get("Icon", ""),
                            "is_choke_pattern": "是"
                            if tile.is_choke_pattern
                            else "否",
                        }
                    )

            steps = parse_replay(source.get("replay_info", ""))
            click_no = 0
            click_steps = 0
            prop_steps = 0
            capped_count = 0
            missing_click_targets = 0
            unmapped_targets = 0
            invalid_steps = 0
            game_actions: set[int] = set()
            extra_ids: set[int] = set()

            if staging_dir and level_index not in staging_handles:
                path = staging_dir / f"timeline_{level_index}.ndjson"
                staging_paths[level_index] = str(path)
                staging_handles[level_index] = stack.enter_context(
                    path.open("w", encoding="utf-8")
                )

            for step in steps:
                if not step["valid"]:
                    invalid_steps += 1
                    action_name, asset_name = "无法解析", ""
                    action_id: Any = ""
                else:
                    action_id = int(step["action_id"])
                    game_actions.add(action_id)
                    action_step_counts[action_id] += 1
                    action_name, asset_name = catalog.action_info(action_id)
                    if action_id == 1:
                        click_no += 1
                        click_steps += 1
                        if not step["tile_nums"]:
                            missing_click_targets += 1
                    elif action_id != 2:
                        prop_steps += 1
                    if action_id in EXTRA_SPACE_ACTION_IDS:
                        extra_ids.add(action_id)
                    if step["interval_ms"] == INTERVAL_CAP_MS:
                        capped_count += 1

                patterns: list[str] = []
                positions: list[str] = []
                for tile_num in step["tile_nums"]:
                    pattern, position, mapped = tile_text(
                        tile_num, tile_by_num, catalog.tile_by_id
                    )
                    patterns.append(pattern)
                    positions.append(position)
                    if not mapped:
                        unmapped_targets += 1

                if action_id == 2 and step["tile_nums"]:
                    booster_ids = [
                        tile_by_num[tile_num].tile_id
                        for tile_num in step["tile_nums"]
                        if tile_num in tile_by_num
                    ]
                    quality["booster_target_steps_checked"] += 1
                    booster_mapping_complete = (
                        len(booster_ids) == len(step["tile_nums"])
                        and len(booster_ids) % ELIMINATE_COUNT == 0
                        and all(tile_id > 0 for tile_id in booster_ids)
                    )
                    if not booster_mapping_complete:
                        quality["booster_target_steps_unverifiable"] += 1
                    else:
                        triplets_match = all(
                            len(set(booster_ids[index : index + ELIMINATE_COUNT]))
                            == 1
                            for index in range(
                                0, len(booster_ids), ELIMINATE_COUNT
                            )
                        )
                        quality["booster_target_steps_verifiable"] += 1
                        quality["booster_target_steps_triplets_match"] += int(
                            triplets_match
                        )
                        quality["booster_target_steps_mismatch"] += int(
                            not triplets_match
                        )

                timeline_row = {
                    "user_id": source.get("user_id", ""),
                    "event_time": source.get("event_time", ""),
                    "client_version": source.get("client_version", ""),
                    "platform": source.get("platform", ""),
                    "level_index": level_index,
                    "level_template_id": source.get("level_template_id", ""),
                    "step_no": step["step_no"],
                    "click_no": click_no if action_id == 1 else "",
                    "raw_step": step["raw_step"],
                    "interval_ms": ""
                    if step["interval_ms"] is None
                    else step["interval_ms"],
                    "elapsed_ms": step["elapsed_ms"],
                    "elapsed_time": format_elapsed_ms(step["elapsed_ms"]),
                    "elapsed_may_be_lower_bound": "是"
                    if step["elapsed_lower_bound"]
                    else "否",
                    "action_id": action_id,
                    "action_name": action_name,
                    "asset_name": asset_name,
                    "is_extra_space": "是"
                    if action_id in EXTRA_SPACE_ACTION_IDS
                    else "否",
                    "tile_nums": "-".join(map(str, step["tile_nums"])),
                    "initial_tile_patterns": "; ".join(patterns),
                    "initial_positions": "; ".join(positions),
                    "reconstruction_status": reconstruction.status,
                }
                timeline_writer.writerow(timeline_row)
                quality["total_steps"] += 1
                if staging_dir:
                    staging_handles[level_index].write(
                        json.dumps(
                            [timeline_row[field] for field in TIMELINE_FIELDS],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    staging_counts[level_index] += 1

            for action_id in game_actions:
                action_game_counts[action_id] += 1
            for action_id in extra_ids:
                extra_space_game_counts[action_id] += 1

            used_extra_space = bool(extra_ids)
            elapsed = steps[-1]["elapsed_ms"] if steps else 0
            result_category = classify_result(source)
            bag_full_process = classify_bag_full_process(source)
            result_counts[result_category] += 1
            summary = {
                "user_id": source.get("user_id", ""),
                "event_time": source.get("event_time", ""),
                "client_version": source.get("client_version", ""),
                "platform": source.get("platform", ""),
                "level_index": level_index,
                "level_id": source.get("level_id", ""),
                "level_template_id": source.get("level_template_id", ""),
                "level_dup_para_id": source.get("level_dup_para_id", "")
                or resolution.dup_para_id,
                "level_dup_para": source.get("level_dup_para", ""),
                "level_value_test": source.get("level_value_test", ""),
                "level_part_index": source.get("level_part_index", ""),
                "level_status": source.get("level_status", ""),
                "level_revive_count": source.get("level_revive_count", ""),
                "level_space_revive_count": source.get(
                    "level_space_revive_count", ""
                ),
                "level_time_revive_count": source.get(
                    "level_time_revive_count", ""
                ),
                "result_category": result_category,
                "bag_full_event_count": source.get("bag_full_event_count", ""),
                "bag_full_process": bag_full_process,
                "used_extra_space": "是" if used_extra_space else "否",
                "extra_space_action_ids": "|".join(map(str, sorted(extra_ids))),
                "total_steps": len(steps),
                "click_steps": click_steps,
                "prop_steps": prop_steps,
                "elapsed_ms_lower_bound": elapsed,
                "elapsed_time_lower_bound": format_elapsed_ms(elapsed),
                "has_120s_interval": "是" if capped_count else "否",
                "capped_interval_count": capped_count,
                "missing_click_target_count": missing_click_targets,
                "unmapped_target_count": unmapped_targets,
                "invalid_step_count": invalid_steps,
                "resolved_level_id": resolution.resolved_level_id,
                "resolved_dup_para": resolution.dup_para,
                "config_mapping_source": resolution.source,
                "random_algorithm": reconstruction.algorithm,
                "reconstruction_status": reconstruction.status,
            }
            summary_rows.append(summary)
            quality["games"] += 1
            quality["games_used_extra_space"] += int(used_extra_space)
            quality["games_with_120s_interval"] += int(capped_count > 0)
            quality["capped_intervals"] += capped_count
            quality["missing_click_targets"] += missing_click_targets
            quality["unmapped_targets"] += unmapped_targets
            quality["invalid_steps"] += invalid_steps

    action_rows: list[dict[str, Any]] = []
    for action_id in sorted(action_step_counts):
        action_name, asset_name = catalog.action_info(action_id)
        action_rows.append(
            {
                "action_id": action_id,
                "action_name": action_name,
                "asset_name": asset_name,
                "step_count": action_step_counts[action_id],
                "game_count": action_game_counts[action_id],
                "is_extra_space": "是"
                if action_id in EXTRA_SPACE_ACTION_IDS
                else "否",
            }
        )

    summary_path = output_dir / "玩家局摘要.csv"
    mapping_path = output_dir / "TileNum模板映射.csv"
    action_path = output_dir / "操作类型汇总.csv"
    write_csv(summary_path, summary_rows, SUMMARY_FIELDS)
    write_csv(mapping_path, mapping_rows, MAPPING_FIELDS)
    write_csv(action_path, action_rows, ACTION_FIELDS)

    report = {
        "input_file": str(input_path),
        "config_dir": str(Path(args.config_dir).expanduser().resolve()),
        "template_dir": str(Path(args.template_dir).expanduser().resolve()),
        "default_dup_para_id": args.dup_index,
        "iap_cut_a": args.iap_cut_a,
        "quality": dict(sorted(quality.items())),
        "games_by_level": {str(key): value for key, value in sorted(level_game_counts.items())},
        "games_by_result": dict(sorted(result_counts.items())),
        "games_by_algorithm": dict(sorted(algorithm_game_counts.items())),
        "games_by_reconstruction_status": dict(sorted(status_counts.items())),
        "extra_space_games_by_action_id": {
            str(key): value for key, value in sorted(extra_space_game_counts.items())
        },
        "outputs": {
            "summary_csv": str(summary_path),
            "timeline_csv_gz": str(timeline_path),
            "mapping_csv": str(mapping_path),
            "action_csv": str(action_path),
        },
    }
    report_path = output_dir / "还原质量报告.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    if staging_dir:
        write_ndjson(
            staging_dir / "summary.ndjson",
            ([row[field] for field in SUMMARY_FIELDS] for row in summary_rows),
        )
        write_ndjson(
            staging_dir / "mapping.ndjson",
            ([row[field] for field in MAPPING_FIELDS] for row in mapping_rows),
        )
        write_ndjson(
            staging_dir / "actions.ndjson",
            ([row[field] for field in ACTION_FIELDS] for row in action_rows),
        )
        manifest = {
            "title": "玩家单关 TileNum 游玩过程还原",
            "source_file": str(input_path),
            "summary": report,
            "summary_fields": SUMMARY_FIELDS,
            "mapping_fields": MAPPING_FIELDS,
            "action_fields": ACTION_FIELDS,
            "timeline_fields": TIMELINE_FIELDS,
            "summary_ndjson": str(staging_dir / "summary.ndjson"),
            "mapping_ndjson": str(staging_dir / "mapping.ndjson"),
            "actions_ndjson": str(staging_dir / "actions.ndjson"),
            "timeline_sheets": [
                {
                    "level_index": level,
                    "path": staging_paths[level],
                    "row_count": staging_counts[level],
                }
                for level in sorted(staging_paths)
            ],
        }
        (staging_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Redash 导出 CSV")
    parser.add_argument("--output-dir", required=True, help="输出目录")
    parser.add_argument(
        "--config-dir", default=str(DEFAULT_CONFIG_DIR), help="ulinkconfig/csv 目录"
    )
    parser.add_argument(
        "--template-dir", default=str(DEFAULT_TEMPLATE_DIR), help="Template 目录"
    )
    parser.add_argument(
        "--template-reader-dir",
        default=str(DEFAULT_TEMPLATE_READER_DIR),
        help="包含 puzzle_core 的 scripts 目录",
    )
    parser.add_argument(
        "--dup-index",
        type=int,
        default=1,
        help="输入缺少 level_dup_para_id 时使用的固定副本（1 基）",
    )
    parser.add_argument(
        "--iap-cut-a",
        type=int,
        default=0,
        help="IAP 保护对 A 图案种类数的减少值；2.08 Android 配置为 0",
    )
    parser.add_argument(
        "--staging-dir",
        help="可选：为 artifact-tool 工作簿生成 NDJSON 中间数据",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = restore_file(args)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
