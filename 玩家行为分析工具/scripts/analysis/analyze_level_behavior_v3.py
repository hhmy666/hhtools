#!/usr/bin/env python3
"""按实时可见局面、玩家内差异与跨玩家复刻分析关卡行为 v3。

本版不先把关卡压成单一难度分。它沿每局真实 replay 重建玩家在动作前看到的
局面，把“至少露出一个 1/4 格的可见牌”和“四格全露出的可点击牌”分开，
再把每个玩家在某个业务实例上的表现与该玩家其余关卡比较。最终实例证据来自
多名玩家是否重复出现同方向差异，而不是样本分位数或模板静态特征。

业务实例键固定为：level_index + level_template_id + level_dup_para_id +
规范化 DupPara。level_id、版本、用户分组只保留为血缘与稳健性上下文。
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd


sys.dont_write_bytecode = True
MODULE_DIR = Path(__file__).resolve().parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

import analyze_level_stratification as legacy  # noqa: E402
import analyze_level_stratification_v2 as v2  # noqa: E402


DEFAULT_CONFIG_DIR = v2.DEFAULT_CONFIG_DIR
DEFAULT_TEMPLATE_DIR = v2.DEFAULT_TEMPLATE_DIR
DEFAULT_TEMPLATE_READER_DIR = v2.DEFAULT_TEMPLATE_READER_DIR

PHASE_FIRST = v2.PHASE_FIRST
PHASE_RETRY = v2.PHASE_RETRY
EXTRA_SPACE_IDS = v2.EXTRA_SPACE_IDS
MANUAL_EXTRA_SPACE_IDS = v2.MANUAL_EXTRA_SPACE_IDS
AUTO_EXTRA_SPACE_IDS = v2.AUTO_EXTRA_SPACE_IDS

DECISION_FILE = "01_实时决策状态.csv.gz"
ATTEMPT_FILE = "02_对局行为特征.csv.gz"
PLAYER_INSTANCE_FILE = "03_玩家实例相对表现.csv.gz"
DIMENSION_FILE = "04_实例维度复刻证据.csv"
INSTANCE_FILE = "05_实例行为画像.csv"
LEVEL_FILE = "06_关卡号复刻汇总.csv"
HOTSPOT_FILE = "07_实时卡点热点.csv.gz"
QUALITY_FILE = "分析质量报告.json"
REPORT_FILE = "结论与口径.md"
WORKBOOK_FILE = "关卡实时行为分层_v3.xlsx"

INTERVAL_CAP_MS = 120000
EPSILON = 1e-9
MIN_REPLICATION_SAMPLE = 5
CONFIRMED_REPLICATION_SAMPLE = 10


def finite(values: Iterable[Any]) -> np.ndarray:
    values = pd.to_numeric(pd.Series(list(values)), errors="coerce").to_numpy(float)
    return values[np.isfinite(values)]


def ratio(numerator: float, denominator: float) -> float:
    return float(numerator) / float(denominator) if denominator else math.nan


def mean(values: Iterable[Any]) -> float:
    data = finite(values)
    return float(data.mean()) if len(data) else math.nan


def median(values: Iterable[Any]) -> float:
    data = finite(values)
    return float(np.median(data)) if len(data) else math.nan


def percentile(values: Iterable[Any], q: float) -> float:
    data = finite(values)
    return float(np.percentile(data, q)) if len(data) else math.nan


def entropy_from_counts(counts: Mapping[int, int]) -> float:
    positive = np.asarray([value for value in counts.values() if value > 0], dtype=float)
    if not len(positive):
        return 0.0
    probabilities = positive / positive.sum()
    return float(-(probabilities * np.log2(probabilities)).sum())


def normalized_dup_para(value: Any) -> str:
    return v2.normalized_dup_para(value)


def business_instance_parts(source: Mapping[str, Any]) -> tuple[Any, ...]:
    return v2.business_instance_parts(source)


def hash_id(prefix: str, parts: Sequence[Any], length: int = 16) -> str:
    return v2.hash_id(prefix, parts, length=length)


def threshold_episode_count(flags: Sequence[bool]) -> int:
    return len(legacy.threshold_onsets([int(value) for value in flags], 1))


def wilson_lower(successes: int, total: int, z_value: float = 1.6448536269514722) -> float:
    if total <= 0:
        return math.nan
    p = successes / total
    denominator = 1.0 + z_value * z_value / total
    center = p + z_value * z_value / (2.0 * total)
    radius = z_value * math.sqrt(
        p * (1.0 - p) / total + z_value * z_value / (4.0 * total * total)
    )
    return max(0.0, (center - radius) / denominator)


@dataclass(frozen=True)
class BoardGeometry:
    """模板几何：每张牌四个 1/4 格分别有哪些更高牌覆盖。"""

    direct_blockers: tuple[frozenset[int], ...]
    quarter_blockers: tuple[tuple[frozenset[int], ...], ...]
    blocked_tiles: tuple[frozenset[int], ...]
    tile_count: int

    @classmethod
    def from_template(cls, template: Any) -> "BoardGeometry":
        tiles = list(template.level.tiles)
        footprints = [
            (
                (tile.x, tile.y),
                (tile.x + 1, tile.y),
                (tile.x, tile.y + 1),
                (tile.x + 1, tile.y + 1),
            )
            for tile in tiles
        ]
        cell_to_indices: dict[tuple[int, int], list[int]] = {}
        for index, cells in enumerate(footprints):
            for cell in cells:
                cell_to_indices.setdefault(cell, []).append(index)
        quarter_blockers: list[tuple[frozenset[int], ...]] = []
        for lower_index, cells in enumerate(footprints):
            lower_z = tiles[lower_index].z
            quarter_blockers.append(
                tuple(
                    frozenset(
                        upper_index
                        for upper_index in cell_to_indices[cell]
                        if tiles[upper_index].z > lower_z
                    )
                    for cell in cells
                )
            )
        return cls(
            direct_blockers=tuple(template.graph.direct_blockers),
            quarter_blockers=tuple(quarter_blockers),
            blocked_tiles=tuple(template.graph.direct_blocked_tiles),
            tile_count=len(tiles),
        )

    def state(
        self, removed: frozenset[int]
    ) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...], dict[int, int]]:
        clickable: list[int] = []
        visible: list[int] = []
        hidden: list[int] = []
        quarter_count: dict[int, int] = {}
        for index in range(self.tile_count):
            if index in removed:
                continue
            quarters = sum(
                blockers.issubset(removed)
                for blockers in self.quarter_blockers[index]
            )
            quarter_count[index] = quarters
            if quarters:
                visible.append(index)
            else:
                hidden.append(index)
            if self.direct_blockers[index].issubset(removed):
                clickable.append(index)
        return tuple(clickable), tuple(visible), tuple(hidden), quarter_count


def transition(
    tile_index: int,
    removed: frozenset[int],
    bag: tuple[tuple[int, int], ...],
    tile_ids: Sequence[int],
) -> tuple[frozenset[int], tuple[tuple[int, int], ...], bool]:
    counts = Counter(dict(bag))
    tile_id = int(tile_ids[tile_index])
    counts[tile_id] += 1
    matched = counts[tile_id] == 3
    if matched:
        counts[tile_id] = 0
    next_bag = tuple(sorted((key, value) for key, value in counts.items() if value))
    return frozenset((*removed, tile_index)), next_bag, matched


def surviving_first_actions(
    removed: frozenset[int],
    bag: tuple[tuple[int, int], ...],
    tile_ids: Sequence[int],
    blockers: Sequence[frozenset[int]],
    capacity: int,
    allowed_indices: frozenset[int] | None = None,
) -> frozenset[int]:
    """在装满前先形成一次三消的可存活首步。

    allowed_indices=None 表示允许未来新揭露牌（全知短窗）；传入当前可见集合时，
    搜索只使用玩家当前已经看见的牌（玩家可见短窗）。
    """

    initial_bag_size = sum(value for _, value in bag)
    max_steps = capacity - initial_bag_size
    if max_steps <= 0:
        return frozenset()
    allowed = (
        frozenset(range(len(tile_ids)))
        if allowed_indices is None
        else frozenset(allowed_indices)
    )

    @lru_cache(maxsize=None)
    def can_recycle(
        state_removed: frozenset[int],
        state_bag: tuple[tuple[int, int], ...],
        steps_left: int,
    ) -> bool:
        if steps_left <= 0:
            return False
        available = (
            index
            for index, candidate_blockers in enumerate(blockers)
            if index in allowed
            and index not in state_removed
            and candidate_blockers.issubset(state_removed)
        )
        for candidate in available:
            next_removed, next_bag, matched = transition(
                candidate, state_removed, state_bag, tile_ids
            )
            if sum(value for _, value in next_bag) >= capacity:
                continue
            if matched or can_recycle(next_removed, next_bag, steps_left - 1):
                return True
        return False

    result: set[int] = set()
    for candidate, candidate_blockers in enumerate(blockers):
        if (
            candidate not in allowed
            or candidate in removed
            or not candidate_blockers.issubset(removed)
        ):
            continue
        next_removed, next_bag, matched = transition(
            candidate, removed, bag, tile_ids
        )
        if sum(value for _, value in next_bag) >= capacity:
            continue
        if matched or can_recycle(next_removed, next_bag, max_steps - 1):
            result.add(candidate)
    return frozenset(result)


def capacity_bounds(
    source: Mapping[str, Any], steps: Sequence[Mapping[str, Any]], bag_points: Sequence[int]
) -> tuple[int, int, str]:
    """返回进局时容量上下界；扩容动作发生后由事件循环统一改成8。"""

    valid_steps = [step for step in steps if step.get("valid")]
    first_click_pos = next(
        (
            index
            for index, step in enumerate(valid_steps)
            if step.get("action_id") == 1
        ),
        len(valid_steps),
    )
    pre_click_extra = any(
        step.get("action_id") in EXTRA_SPACE_IDS
        for step in valid_steps[:first_click_pos]
    )
    if pre_click_extra:
        return 8, 8, "8格_首击前记录扩容"
    observation = legacy.clean_text(source.get("bag_capacity_observation"))
    positive = legacy.clean_text(source.get("capacity_8_positive_evidence")) == "是"
    if "达到8格" in observation or (positive and not any(
        step.get("action_id") in EXTRA_SPACE_IDS for step in valid_steps
    )):
        return 8, 8, "8格_曲线正向证据"
    status = legacy.clean_text(source.get("level_status")).lower()
    bag_full = legacy.as_int(source.get("bag_full_event_count"), 0)
    if bag_points and max(bag_points) == 7 and bag_full > 0 and status in legacy.FAIL_STATUSES:
        return 7, 7, "7格_满包失败正向证据"
    return 7, 8, "7/8格不确定_可能未上报活动533"


def pattern_counts(indices: Iterable[int], tile_ids: Sequence[int]) -> Counter[int]:
    return Counter(
        int(tile_ids[index])
        for index in indices
        if 0 <= index < len(tile_ids) and int(tile_ids[index]) > 0
    )


def reconcile_bag_after_click(
    before: Counter[int],
    observed_size_after: int,
    selected_pattern: int,
    click_index: int,
) -> tuple[tuple[int, int], ...]:
    """塔内换色导致点估计不符时，用实际背包大小维持可继续的花色点估计。"""

    counts = Counter(before)
    before_size = sum(counts.values())
    delta = observed_size_after - before_size
    if delta == -2:
        candidates = [pattern for pattern, count in counts.items() if count == 2]
        chosen = (
            selected_pattern
            if selected_pattern in candidates
            else candidates[0]
            if candidates
            else None
        )
        if chosen is not None:
            counts.pop(chosen, None)
    elif delta == 1:
        chosen = selected_pattern
        if chosen <= 0 or counts.get(chosen, 0) >= 2:
            chosen = 1_000_000 + click_index
        counts[chosen] += 1
    while sum(counts.values()) > observed_size_after and counts:
        pattern = next(iter(counts))
        counts[pattern] -= 1
        if counts[pattern] <= 0:
            counts.pop(pattern, None)
    while sum(counts.values()) < observed_size_after:
        counts[1_000_000 + click_index + sum(counts.values())] += 1
    return tuple(sorted(counts.items()))


def solution_widths(
    bag_counts: Counter[int],
    visible_counts: Counter[int],
    clickable_counts: Counter[int],
    capacity: int,
) -> dict[str, int]:
    bag_size = sum(bag_counts.values())
    free_slots = max(0, capacity - bag_size)
    visible_combination = 0
    visible_safe = 0
    visible_solution_tiles = 0
    immediate_actions = 0
    for pattern in set(bag_counts) | set(visible_counts):
        need = max(0, 3 - bag_counts.get(pattern, 0))
        visible = visible_counts.get(pattern, 0)
        if visible >= need and need > 0:
            visible_combination += 1
            visible_solution_tiles += visible
            if need <= free_slots:
                visible_safe += 1
        if bag_counts.get(pattern, 0) >= 2:
            immediate_actions += clickable_counts.get(pattern, 0)
    return {
        "visible_combination_pattern_count": visible_combination,
        "visible_safe_pattern_count": visible_safe,
        "visible_solution_tile_count": visible_solution_tiles,
        "immediate_triple_action_count": immediate_actions,
    }


def action_origin(action_id: int) -> str:
    if action_id == 1:
        return "玩家点击"
    if action_id == 2:
        return "局前Booster"
    if action_id in MANUAL_EXTRA_SPACE_IDS:
        return "玩家手动道具"
    if action_id in AUTO_EXTRA_SPACE_IDS:
        return "自动背包+1"
    if action_id in EXTRA_SPACE_IDS:
        return "扩容兼容动作"
    return "玩家道具"


def player_decision(action_id: int) -> int:
    return int(
        action_id == 1
        or action_id in MANUAL_EXTRA_SPACE_IDS
        or (action_id not in {1, 2} and action_id not in AUTO_EXTRA_SPACE_IDS)
    )


DECISION_COLUMNS = [
    "source_row_no",
    "user_id",
    "event_time",
    "level_index",
    "level_template_id",
    "level_dup_para_id",
    "level_dup_para",
    "business_instance_id",
    "attempt_phase",
    "level_status",
    "step_no",
    "click_no_before",
    "progress_before",
    "action_id",
    "action_origin",
    "is_player_decision",
    "interval_ms",
    "decision_dwell_sec",
    "interval_right_censored",
    "elapsed_sec",
    "target_tile_nums",
    "selected_tile_num",
    "selected_pattern_id",
    "geometry_state_valid",
    "pattern_state_point_valid",
    "tower_pattern_uncertainty",
    "cn_pattern_unknown",
    "bag_size_before",
    "bag_capacity_min",
    "bag_capacity_max",
    "free_slots_min",
    "free_slots_max",
    "bag_distinct_pattern_count",
    "bag_singleton_pattern_count",
    "bag_pair_pattern_count",
    "bag_pattern_entropy",
    "remaining_tile_count",
    "clickable_tile_count",
    "visible_tile_count_25pct",
    "partially_visible_tile_count",
    "fully_hidden_tile_count",
    "visible_quarter_count",
    "visible_pattern_count",
    "clickable_pattern_count",
    "bag_visible_pattern_overlap",
    "bag_clickable_pattern_overlap",
    "visible_combination_pattern_count",
    "visible_no_solution",
    "visible_safe_pattern_count_cap_min",
    "visible_safe_pattern_count_cap_max",
    "immediate_triple_action_count",
    "visible_first_action_width_cap_min",
    "visible_first_action_width_cap_max",
    "omniscient_first_action_width_cap_min",
    "omniscient_first_action_width_cap_max",
    "hidden_information_dependency",
    "selected_completes_triple",
    "selected_matches_bag",
    "selected_visible_solution_pattern",
    "selected_is_visible_safe_cap_min",
    "selected_is_visible_safe_cap_max",
    "selected_new_clickable_count",
    "selected_new_visible_count",
    "selected_visible_quarter_gain",
    "selected_branch_width_change",
    "is_notable_state",
]


def blank_state_record() -> dict[str, Any]:
    return {column: math.nan for column in DECISION_COLUMNS}


def analyze_attempt(
    source: Mapping[str, Any],
    row_no: int,
    base_attempt: Mapping[str, Any],
    structures: legacy.StructureFeatureCatalog,
    geometry_cache: dict[str, BoardGeometry],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """重建一局的动作前状态，并返回单局汇总与动作状态行。"""

    steps = legacy.parse_replay(legacy.clean_text(source.get("replay_info")))
    valid_steps = [step for step in steps if step.get("valid")]
    click_steps = [step for step in valid_steps if step.get("action_id") == 1]
    bag_points = legacy.parse_bag_curve(source.get("bag_tile_change_curve"))
    surface_points = legacy.parse_surface_curve(
        source.get("surface_normal_tile_change_curve")
    )
    template_id = legacy.clean_text(source.get("level_template_id"))
    template = structures.template_context(template_id)
    instance = structures.instance_context(source)
    geometry = geometry_cache.setdefault(
        template_id, BoardGeometry.from_template(template)
    )
    tile_ids = tuple(
        int(getattr(instance.tile_by_num.get(index + 1), "tile_id", 0))
        for index in range(geometry.tile_count)
    )
    cn_unknown = int(any(tile_id <= 0 for tile_id in tile_ids))
    tower_indices = frozenset(template.level.tower_member_indices)
    capacity_min, capacity_max, capacity_resolution = capacity_bounds(
        source, valid_steps, bag_points
    )
    parts = business_instance_parts(source)
    business_id = hash_id("BI", parts)
    business_key = f"{parts[0]}|{parts[1]}|dup{parts[2]}|{parts[3]}"
    phase = (
        PHASE_FIRST
        if legacy.as_int(source.get("level_play_count"), 0) == 1
        else PHASE_RETRY
    )

    removed: frozenset[int] = frozenset()
    bag: tuple[tuple[int, int], ...] = ()
    position_to_identity = list(range(geometry.tile_count))
    identity_to_position = list(range(geometry.tile_count))
    geometry_valid = True
    pattern_valid = not cn_unknown
    tower_triggered = False
    click_index = 0
    decision_rows: list[dict[str, Any]] = []

    visible_no_solution_flags: list[bool] = []
    visible_narrow_flags: list[bool] = []
    pressure_possible_flags: list[bool] = []
    pressure_certain_flags: list[bool] = []
    pressure_visible_no_solution_flags: list[bool] = []
    forced_visible_flags: list[bool] = []
    hidden_dependency_flags: list[bool] = []
    selected_nonviable_flags: list[bool] = []
    selected_immediate_flags: list[bool] = []
    selected_bag_match_flags: list[bool] = []
    click_intervals: list[float] = []
    visible_no_solution_intervals: list[float] = []
    normal_solution_intervals: list[float] = []
    partial_visible_shares: list[float] = []
    new_clickable_counts: list[float] = []
    new_visible_counts: list[float] = []
    notable_count = 0
    exact_geometry_clicks = 0
    pattern_point_clicks = 0
    manual_prop_count = 0
    auto_space_count = 0
    manual_prop_at_visible_no_solution = 0
    unknown_board_prop_count = 0
    surface_validation_mismatch_count = 0
    bag_validation_mismatch_count = 0
    illegal_click_count = 0

    for step in valid_steps:
        action_id = int(step["action_id"])
        state_geometry_valid = geometry_valid
        state_pattern_valid = pattern_valid and state_geometry_valid
        state = blank_state_record()
        state.update(
            {
                "source_row_no": row_no,
                "user_id": legacy.clean_text(source.get("user_id")),
                "event_time": legacy.clean_text(source.get("event_time")),
                "level_index": legacy.as_int(source.get("level_index"), -1),
                "level_template_id": template_id,
                "level_dup_para_id": legacy.as_int(
                    source.get("level_dup_para_id"), 0
                ),
                "level_dup_para": normalized_dup_para(
                    source.get("level_dup_para")
                ),
                "business_instance_id": business_id,
                "attempt_phase": phase,
                "level_status": legacy.clean_text(source.get("level_status")),
                "step_no": int(step.get("step_no", 0)),
                "click_no_before": click_index,
                "progress_before": ratio(click_index, len(click_steps)),
                "action_id": action_id,
                "action_origin": action_origin(action_id),
                "is_player_decision": player_decision(action_id),
                "interval_ms": int(step.get("interval_ms", 0)),
                "decision_dwell_sec": float(step.get("interval_ms", 0)) / 1000.0,
                "interval_right_censored": int(
                    int(step.get("interval_ms", 0)) >= INTERVAL_CAP_MS
                ),
                "elapsed_sec": float(step.get("elapsed_ms", 0)) / 1000.0,
                "target_tile_nums": "-".join(
                    str(value) for value in (step.get("tile_nums") or [])
                ),
                "geometry_state_valid": int(state_geometry_valid),
                "pattern_state_point_valid": int(state_pattern_valid),
                "tower_pattern_uncertainty": int(tower_triggered),
                "cn_pattern_unknown": cn_unknown,
                "bag_size_before": (
                    int(bag_points[click_index - 1])
                    if click_index > 0 and click_index - 1 < len(bag_points)
                    else 0
                ),
                "bag_capacity_min": capacity_min,
                "bag_capacity_max": capacity_max,
            }
        )
        bag_size_before = int(state["bag_size_before"])
        state["free_slots_min"] = capacity_min - bag_size_before
        state["free_slots_max"] = capacity_max - bag_size_before

        clickable: tuple[int, ...] = ()
        visible: tuple[int, ...] = ()
        quarter_count: dict[int, int] = {}
        bag_counts = Counter(dict(bag)) if state_pattern_valid else Counter()
        visible_counts: Counter[int] = Counter()
        clickable_counts: Counter[int] = Counter()
        safe_min: frozenset[int] = frozenset()
        safe_max: frozenset[int] = frozenset()
        all_safe_min: frozenset[int] = frozenset()
        all_safe_max: frozenset[int] = frozenset()
        visible_solution_patterns: set[int] = set()
        selected_identity: int | None = None
        selected_position: int | None = None
        current_tile_ids = tuple(
            tile_ids[position_to_identity[position]]
            for position in range(geometry.tile_count)
        )

        if state_geometry_valid:
            clickable, visible, hidden, quarter_count = geometry.state(removed)
            if action_id == 1 and step.get("tile_nums"):
                selected_identity = int(step["tile_nums"][0]) - 1
                if 0 <= selected_identity < geometry.tile_count:
                    selected_position = identity_to_position[selected_identity]
                    expected_surface = (
                        int(surface_points[click_index])
                        if click_index < len(surface_points)
                        else None
                    )
                    current_surface_matches = (
                        selected_position in clickable
                        and (
                            expected_surface is None
                            or len(
                                geometry.state(
                                    frozenset((*removed, selected_position))
                                )[0]
                            )
                            == expected_surface
                        )
                    )
                    if not current_surface_matches:
                        tower_group = template.level.tiles[selected_identity].tower_index
                        candidates = [
                            position
                            for position in clickable
                            if tower_group is not None
                            and template.level.tiles[position].tower_index
                            == tower_group
                        ]
                        matching = [
                            position
                            for position in candidates
                            if expected_surface is None
                            or len(
                                geometry.state(frozenset((*removed, position)))[0]
                            )
                            == expected_surface
                        ]
                        if not matching and tower_triggered:
                            matching = [
                                position
                                for position in clickable
                                if expected_surface is None
                                or len(
                                    geometry.state(
                                        frozenset((*removed, position))
                                    )[0]
                                )
                                == expected_surface
                            ]
                        if candidates or matching:
                            chosen = (matching or candidates)[0]
                            old_position = identity_to_position[selected_identity]
                            displaced_identity = position_to_identity[chosen]
                            position_to_identity[chosen] = selected_identity
                            identity_to_position[selected_identity] = chosen
                            position_to_identity[old_position] = displaced_identity
                            identity_to_position[displaced_identity] = old_position
                            selected_position = chosen
                            tower_triggered = True
                            state["tower_pattern_uncertainty"] = 1
                            current_tile_ids = tuple(
                                tile_ids[position_to_identity[position]]
                                for position in range(geometry.tile_count)
                            )
            remaining = geometry.tile_count - len(removed)
            partial = sum(quarter_count[index] in {1, 2, 3} for index in visible)
            visible_quarters = sum(quarter_count.values())
            state.update(
                {
                    "remaining_tile_count": remaining,
                    "clickable_tile_count": len(clickable),
                    "visible_tile_count_25pct": len(visible),
                    "partially_visible_tile_count": partial,
                    "fully_hidden_tile_count": len(hidden),
                    "visible_quarter_count": visible_quarters,
                }
            )
            partial_visible_shares.append(ratio(partial, remaining))
            if state_pattern_valid:
                visible_counts = pattern_counts(visible, current_tile_ids)
                clickable_counts = pattern_counts(clickable, current_tile_ids)
                state.update(
                    {
                        "bag_distinct_pattern_count": len(bag_counts),
                        "bag_singleton_pattern_count": sum(
                            value == 1 for value in bag_counts.values()
                        ),
                        "bag_pair_pattern_count": sum(
                            value == 2 for value in bag_counts.values()
                        ),
                        "bag_pattern_entropy": entropy_from_counts(bag_counts),
                        "visible_pattern_count": len(visible_counts),
                        "clickable_pattern_count": len(clickable_counts),
                        "bag_visible_pattern_overlap": len(
                            set(bag_counts).intersection(visible_counts)
                        ),
                        "bag_clickable_pattern_overlap": len(
                            set(bag_counts).intersection(clickable_counts)
                        ),
                    }
                )
                low = solution_widths(
                    bag_counts, visible_counts, clickable_counts, capacity_min
                )
                high = solution_widths(
                    bag_counts, visible_counts, clickable_counts, capacity_max
                )
                visible_solution_patterns = {
                    pattern
                    for pattern in set(bag_counts) | set(visible_counts)
                    if visible_counts.get(pattern, 0)
                    >= max(1, 3 - bag_counts.get(pattern, 0))
                }
                state.update(
                    {
                        "visible_combination_pattern_count": low[
                            "visible_combination_pattern_count"
                        ],
                        "visible_no_solution": int(
                            low["visible_combination_pattern_count"] == 0
                        ),
                        "visible_safe_pattern_count_cap_min": low[
                            "visible_safe_pattern_count"
                        ],
                        "visible_safe_pattern_count_cap_max": high[
                            "visible_safe_pattern_count"
                        ],
                        "immediate_triple_action_count": low[
                            "immediate_triple_action_count"
                        ],
                    }
                )
                # 高压阶段才展开短窗搜索，避免把大量宽松局面做指数搜索。
                if bag_size_before >= 5 and clickable:
                    allowed = frozenset(visible)
                    safe_min = surviving_first_actions(
                        removed,
                        bag,
                        current_tile_ids,
                        geometry.direct_blockers,
                        capacity_min,
                        allowed,
                    )
                    all_safe_min = surviving_first_actions(
                        removed,
                        bag,
                        current_tile_ids,
                        geometry.direct_blockers,
                        capacity_min,
                        None,
                    )
                    if capacity_max == capacity_min:
                        safe_max = safe_min
                        all_safe_max = all_safe_min
                    else:
                        safe_max = surviving_first_actions(
                            removed,
                            bag,
                            current_tile_ids,
                            geometry.direct_blockers,
                            capacity_max,
                            allowed,
                        )
                        all_safe_max = surviving_first_actions(
                            removed,
                            bag,
                            current_tile_ids,
                            geometry.direct_blockers,
                            capacity_max,
                            None,
                        )
                    state.update(
                        {
                            "visible_first_action_width_cap_min": len(safe_min),
                            "visible_first_action_width_cap_max": len(safe_max),
                            "omniscient_first_action_width_cap_min": len(
                                all_safe_min
                            ),
                            "omniscient_first_action_width_cap_max": len(
                                all_safe_max
                            ),
                            "hidden_information_dependency": int(
                                not safe_min and bool(all_safe_min)
                            ),
                        }
                    )

        selected_index: int | None = selected_position
        selected_pattern = 0
        if action_id == 1:
            targets = step.get("tile_nums") or []
            state["selected_tile_num"] = int(targets[0]) if targets else math.nan
            if selected_identity is None and targets:
                selected_identity = int(targets[0]) - 1
                if 0 <= selected_identity < geometry.tile_count:
                    selected_index = identity_to_position[selected_identity]
            if (
                selected_identity is not None
                and 0 <= selected_identity < len(tile_ids)
                and tile_ids[selected_identity] > 0
            ):
                selected_pattern = int(tile_ids[selected_identity])
                state["selected_pattern_id"] = selected_pattern
            if state_geometry_valid and (
                selected_index is None or selected_index not in clickable
            ):
                illegal_click_count += 1
                geometry_valid = False
            elif state_geometry_valid:
                state["selected_matches_bag"] = int(
                    state_pattern_valid and bag_counts.get(selected_pattern, 0) > 0
                )
                state["selected_visible_solution_pattern"] = int(
                    state_pattern_valid
                    and selected_pattern in visible_solution_patterns
                )
                state["selected_is_visible_safe_cap_min"] = (
                    int(selected_index in safe_min)
                    if bag_size_before >= 5 and state_pattern_valid
                    else math.nan
                )
                state["selected_is_visible_safe_cap_max"] = (
                    int(selected_index in safe_max)
                    if bag_size_before >= 5 and state_pattern_valid
                    else math.nan
                )
                if state_pattern_valid:
                    next_removed, next_bag, matched = transition(
                        selected_index, removed, bag, current_tile_ids
                    )
                    state["selected_completes_triple"] = int(matched)
                else:
                    next_removed, next_bag, matched = (
                        frozenset((*removed, selected_index)),
                        bag,
                        False,
                    )
                next_clickable, next_visible, _next_hidden, next_quarters = geometry.state(
                    next_removed
                )
                newly_clickable = set(next_clickable).difference(
                    set(clickable).difference({selected_index})
                )
                newly_visible = set(next_visible).difference(
                    set(visible).difference({selected_index})
                )
                quarter_gain = (
                    sum(next_quarters.values())
                    - sum(quarter_count.values())
                    + quarter_count.get(selected_index, 0)
                )
                state.update(
                    {
                        "selected_new_clickable_count": len(newly_clickable),
                        "selected_new_visible_count": len(newly_visible),
                        "selected_visible_quarter_gain": quarter_gain,
                        "selected_branch_width_change": (
                            len(next_clickable) - len(clickable) + 1
                        ),
                    }
                )
                new_clickable_counts.append(len(newly_clickable))
                new_visible_counts.append(len(newly_visible))

        visible_no_solution = state.get("visible_no_solution") == 1
        visible_width = state.get("visible_first_action_width_cap_min")
        forced_visible = (
            isinstance(visible_width, (int, float))
            and math.isfinite(float(visible_width))
            and int(visible_width) == 0
        )
        is_pressure_possible = bag_size_before >= capacity_min - 2
        is_pressure_certain = bag_size_before >= capacity_max - 2
        dwell = float(state["decision_dwell_sec"])
        is_notable = bool(
            action_id not in {1, 2}
            or visible_no_solution
            or forced_visible
            or is_pressure_possible
            or dwell >= 5.0
        )
        state["is_notable_state"] = int(is_notable)
        notable_count += int(is_notable)
        decision_rows.append(state)

        if action_id == 1:
            exact_geometry_clicks += int(state_geometry_valid)
            pattern_point_clicks += int(state_pattern_valid)
            click_intervals.append(dwell)
            if state_pattern_valid:
                visible_no_solution_flags.append(visible_no_solution)
                visible_narrow_flags.append(
                    legacy.as_int(
                        state.get("visible_combination_pattern_count"), 999
                    )
                    <= 1
                )
                if visible_no_solution:
                    visible_no_solution_intervals.append(dwell)
                else:
                    normal_solution_intervals.append(dwell)
            pressure_possible_flags.append(is_pressure_possible)
            pressure_certain_flags.append(is_pressure_certain)
            pressure_visible_no_solution_flags.append(
                bool(is_pressure_possible and visible_no_solution)
            )
            if bag_size_before >= 5 and state_pattern_valid:
                forced_visible_flags.append(forced_visible)
                hidden_dependency_flags.append(
                    state.get("hidden_information_dependency") == 1
                )
                selected_safe = state.get("selected_is_visible_safe_cap_min")
                if math.isfinite(float(selected_safe)):
                    selected_nonviable_flags.append(
                        int(selected_safe) == 0 and len(safe_min) > 0
                    )
            if state_pattern_valid:
                selected_immediate_flags.append(
                    state.get("selected_completes_triple") == 1
                )
                selected_bag_match_flags.append(state.get("selected_matches_bag") == 1)

            if selected_index is not None and state_geometry_valid:
                removed = frozenset((*removed, selected_index))
                if state_pattern_valid and selected_pattern > 0:
                    removed, bag, _matched = transition(
                        selected_index,
                        frozenset(index for index in removed if index != selected_index),
                        bag,
                        current_tile_ids,
                    )
                if (
                    selected_index in tower_indices
                    or selected_identity in tower_indices
                ):
                    tower_triggered = True
            click_index += 1
            if click_index <= len(surface_points) and geometry_valid:
                predicted_surface = len(geometry.state(removed)[0])
                if predicted_surface != int(surface_points[click_index - 1]):
                    surface_validation_mismatch_count += 1
                    geometry_valid = False
            if click_index <= len(bag_points) and state_pattern_valid:
                predicted_bag = sum(value for _, value in bag)
                if predicted_bag != int(bag_points[click_index - 1]):
                    bag_validation_mismatch_count += 1
                    if tower_indices:
                        tower_triggered = True
                        bag = reconcile_bag_after_click(
                            bag_counts,
                            int(bag_points[click_index - 1]),
                            selected_pattern,
                            click_index,
                        )
                    else:
                        pattern_valid = False
        elif action_id == 2:
            for tile_num in step.get("tile_nums") or []:
                tile_identity = int(tile_num) - 1
                if 0 <= tile_identity < geometry.tile_count:
                    tile_position = identity_to_position[tile_identity]
                    removed = frozenset((*removed, tile_position))
        elif action_id in EXTRA_SPACE_IDS:
            capacity_min = 8
            capacity_max = 8
            auto_space_count += int(action_id in AUTO_EXTRA_SPACE_IDS)
            manual_prop_count += int(action_id in MANUAL_EXTRA_SPACE_IDS)
            manual_prop_at_visible_no_solution += int(
                action_id in MANUAL_EXTRA_SPACE_IDS and visible_no_solution
            )
        else:
            manual_prop_count += 1
            manual_prop_at_visible_no_solution += int(visible_no_solution)
            unknown_board_prop_count += 1
            # 入口映射未模拟时，记录道具使用前的真实局面；之后只保留埋点曲线。
            geometry_valid = False
            pattern_valid = False

    click_array = np.asarray(click_intervals, dtype=float)
    thirds = np.array_split(click_array, 3) if len(click_array) else [np.array([])] * 3
    pattern_state_count = len(visible_no_solution_flags)
    pressure_count = sum(pressure_possible_flags)
    visible_no_solution_count = sum(visible_no_solution_flags)
    pressure_recovery_count = legacy.pressure_recoveries(bag_points)
    update = {
        "business_instance_id": business_id,
        "business_instance_key": business_key,
        "attempt_phase": phase,
        "capacity_resolution_v3": capacity_resolution,
        "capacity_initial_min": capacity_bounds(source, valid_steps, bag_points)[0],
        "capacity_initial_max": capacity_bounds(source, valid_steps, bag_points)[1],
        "cn_pattern_unknown": cn_unknown,
        "tower_template": int(bool(tower_indices)),
        "tower_pattern_triggered": int(tower_triggered),
        "action_state_count": len(decision_rows),
        "notable_state_count": notable_count,
        "exact_geometry_click_count_v3": exact_geometry_clicks,
        "pattern_point_click_count_v3": pattern_point_clicks,
        "geometry_click_coverage_v3": ratio(exact_geometry_clicks, len(click_steps)),
        "pattern_point_coverage_v3": ratio(pattern_point_clicks, len(click_steps)),
        "illegal_click_count_v3": illegal_click_count,
        "surface_validation_mismatch_count_v3": surface_validation_mismatch_count,
        "bag_validation_mismatch_count_v3": bag_validation_mismatch_count,
        "unknown_board_prop_count": unknown_board_prop_count,
        "direct_click_dwell_median_sec": median(click_intervals),
        "direct_click_dwell_p90_sec": percentile(click_intervals, 90),
        "direct_click_long_pause_3s_share": ratio(
            sum(value >= 3.0 for value in click_intervals), len(click_intervals)
        ),
        "direct_click_long_pause_10s_share": ratio(
            sum(value >= 10.0 for value in click_intervals), len(click_intervals)
        ),
        "direct_late_minus_early_gap_sec": (
            float(np.median(thirds[2]) - np.median(thirds[0]))
            if len(thirds[0]) and len(thirds[2])
            else math.nan
        ),
        "visible_no_solution_state_count": visible_no_solution_count,
        "visible_no_solution_share": ratio(
            visible_no_solution_count, pattern_state_count
        ),
        "visible_no_solution_episode_count": threshold_episode_count(
            visible_no_solution_flags
        ),
        "visible_no_solution_per_100_clicks": 100.0
        * ratio(visible_no_solution_count, len(click_steps)),
        "visible_narrow_share": ratio(sum(visible_narrow_flags), pattern_state_count),
        "visible_no_solution_dwell_median_sec": median(
            visible_no_solution_intervals
        ),
        "visible_solution_dwell_median_sec": median(normal_solution_intervals),
        "visible_no_solution_dwell_ratio": ratio(
            median(visible_no_solution_intervals),
            median(normal_solution_intervals),
        ),
        "pressure_possible_share": ratio(
            sum(pressure_possible_flags), len(click_steps)
        ),
        "pressure_certain_share": ratio(
            sum(pressure_certain_flags), len(click_steps)
        ),
        "pressure_visible_no_solution_share": ratio(
            sum(pressure_visible_no_solution_flags), pressure_count
        ),
        "visible_forced_short_window_share": ratio(
            sum(forced_visible_flags), len(forced_visible_flags)
        ),
        "hidden_information_dependency_share": ratio(
            sum(hidden_dependency_flags), len(hidden_dependency_flags)
        ),
        "selected_nonviable_share": ratio(
            sum(selected_nonviable_flags), len(selected_nonviable_flags)
        ),
        "selected_immediate_triple_share": ratio(
            sum(selected_immediate_flags), len(selected_immediate_flags)
        ),
        "selected_bag_match_share": ratio(
            sum(selected_bag_match_flags), len(selected_bag_match_flags)
        ),
        "partial_visible_share_median": median(partial_visible_shares),
        "selected_new_clickable_median": median(new_clickable_counts),
        "selected_new_visible_median": median(new_visible_counts),
        "pressure_recovery_count": pressure_recovery_count,
        "pressure_recovery_rate": ratio(pressure_recovery_count, pressure_count),
        "manual_prop_count_v3": manual_prop_count,
        "manual_prop_per_100_clicks": 100.0
        * ratio(manual_prop_count, len(click_steps)),
        "manual_prop_at_visible_no_solution_count": manual_prop_at_visible_no_solution,
        "auto_extra_space_event_count": auto_space_count,
        "interval_capped_action_count": sum(
            int(step.get("interval_ms", 0)) >= INTERVAL_CAP_MS for step in valid_steps
        ),
    }
    return update, decision_rows


def write_decision_header(target: Any) -> csv.DictWriter:
    writer = csv.DictWriter(target, fieldnames=DECISION_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    return writer


def enhance_attempts_v3(
    source: pd.DataFrame,
    attempts: pd.DataFrame,
    structures: legacy.StructureFeatureCatalog,
    decision_path: Path,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    """流式写出全部动作状态，只在内存保留卡点状态用于聚合。"""

    decision_path.parent.mkdir(parents=True, exist_ok=True)
    geometry_cache: dict[str, BoardGeometry] = {}
    updates: list[dict[str, Any]] = []
    notable_rows: list[dict[str, Any]] = []
    action_count = 0
    notable_count = 0
    with gzip.open(decision_path, "wt", encoding="utf-8-sig", newline="") as target:
        writer = write_decision_header(target)
        for row_no, (source_row, attempt_row) in enumerate(
            zip(source.to_dict("records"), attempts.to_dict("records")), start=1
        ):
            update, decisions = analyze_attempt(
                source_row, row_no, attempt_row, structures, geometry_cache
            )
            updates.append(update)
            for row in decisions:
                writer.writerow({key: legacy.json_safe(value) for key, value in row.items()})
                action_count += 1
                if row.get("is_notable_state") == 1:
                    notable_rows.append(row)
                    notable_count += 1
            if row_no % 1000 == 0:
                print(f"  已重建 {row_no:,}/{len(source):,} 局", flush=True)
    enhanced = attempts.copy()
    update_frame = pd.DataFrame(updates)
    if len(enhanced) != len(update_frame):
        raise AssertionError("源数据与v3特征行数不一致")
    for column in update_frame.columns:
        enhanced[column] = update_frame[column].to_numpy()
    return enhanced, pd.DataFrame(notable_rows), {
        "decision_action_rows": action_count,
        "notable_action_rows": notable_count,
    }


METRIC_SPECS: tuple[dict[str, Any], ...] = (
    {
        "metric": "fail_rate",
        "dimension": "结果",
        "label": "失败",
        "kind": "proportion",
        "orientation": 1,
    },
    {
        "metric": "attempt_count",
        "dimension": "结果",
        "label": "尝试负担",
        "kind": "count",
        "orientation": 1,
    },
    {
        "metric": "duration_per_click_median",
        "dimension": "节奏",
        "label": "每点击耗时",
        "kind": "time",
        "orientation": 1,
    },
    {
        "metric": "direct_late_minus_early_gap_sec",
        "dimension": "节奏",
        "label": "后段减速",
        "kind": "signed",
        "orientation": 1,
    },
    {
        "metric": "visible_no_solution_share",
        "dimension": "路径压缩",
        "label": "可见无解占比",
        "kind": "proportion",
        "orientation": 1,
    },
    {
        "metric": "visible_narrow_share",
        "dimension": "路径压缩",
        "label": "可见解花色<=1占比",
        "kind": "proportion",
        "orientation": 1,
    },
    {
        "metric": "visible_forced_short_window_share",
        "dimension": "路径压缩",
        "label": "高压可见短窗无路",
        "kind": "proportion",
        "orientation": 1,
    },
    {
        "metric": "hidden_information_dependency_share",
        "dimension": "可理解性",
        "label": "依赖隐藏信息",
        "kind": "proportion",
        "orientation": 1,
    },
    {
        "metric": "pressure_certain_share",
        "dimension": "背包压力",
        "label": "确定高压占比",
        "kind": "proportion",
        "orientation": 1,
    },
    {
        "metric": "pressure_visible_no_solution_share",
        "dimension": "背包压力",
        "label": "高压且可见无解",
        "kind": "proportion",
        "orientation": 1,
    },
    {
        "metric": "visible_no_solution_dwell_median_sec",
        "dimension": "认知负荷",
        "label": "可见无解思考时长",
        "kind": "time",
        "orientation": 1,
    },
    {
        "metric": "selected_nonviable_share",
        "dimension": "选择质量",
        "label": "有替代时选择非安全路径",
        "kind": "proportion",
        "orientation": 1,
    },
    {
        "metric": "pressure_recovery_rate",
        "dimension": "恢复韧性",
        "label": "高压恢复率",
        "kind": "proportion",
        "orientation": -1,
    },
    {
        "metric": "manual_prop_per_100_clicks",
        "dimension": "道具依赖",
        "label": "每百点击手动道具",
        "kind": "count",
        "orientation": 1,
    },
    {
        "metric": "partial_visible_share_median",
        "dimension": "信息复杂度",
        "label": "半露牌负荷",
        "kind": "proportion",
        "orientation": 0,
    },
    {
        "metric": "selected_new_visible_median",
        "dimension": "探索策略",
        "label": "单步新露出",
        "kind": "count",
        "orientation": 0,
    },
    {
        "metric": "selected_immediate_triple_share",
        "dimension": "消除策略",
        "label": "即时三消选择",
        "kind": "proportion",
        "orientation": 0,
    },
)


def aggregate_player_instances(attempts: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    keys = ["user_id", "business_instance_id", "attempt_phase"]
    median_columns = [
        "direct_late_minus_early_gap_sec",
        "visible_no_solution_share",
        "visible_narrow_share",
        "visible_forced_short_window_share",
        "hidden_information_dependency_share",
        "pressure_possible_share",
        "pressure_certain_share",
        "pressure_visible_no_solution_share",
        "visible_no_solution_dwell_median_sec",
        "visible_no_solution_dwell_ratio",
        "selected_nonviable_share",
        "pressure_recovery_rate",
        "manual_prop_per_100_clicks",
        "partial_visible_share_median",
        "selected_new_clickable_median",
        "selected_new_visible_median",
        "selected_immediate_triple_share",
        "selected_bag_match_share",
        "geometry_click_coverage_v3",
        "pattern_point_coverage_v3",
    ]
    for key, group in attempts.groupby(keys, sort=True, dropna=False):
        sample = group.iloc[0]
        clicks = pd.to_numeric(group["click_count"], errors="coerce")
        durations = pd.to_numeric(
            group["level_duration_winsor_sec"], errors="coerce"
        )
        row: dict[str, Any] = {
            "user_id": key[0],
            "business_instance_id": key[1],
            "attempt_phase": key[2],
            "cohort_player_rank": int(sample["cohort_player_rank"]),
            "level_index": int(sample["level_index"]),
            "level_template_id": sample["level_template_id"],
            "level_dup_para_id": int(sample["level_dup_para_id"]),
            "level_dup_para": sample["level_dup_para"],
            "business_instance_key": sample["business_instance_key"],
            "client_version": sample["client_version"],
            "user_group_tag": sample["user_group_tag"],
            "attempt_count": len(group),
            "click_count": int(clicks.sum()),
            "fail_rate": mean(group["is_fail"]),
            "pass_rate": mean(group["is_pass"]),
            "duration_per_click_median": median(
                durations / clicks.replace(0, np.nan)
            ),
            "visible_no_solution_per_100_clicks": median(
                group["visible_no_solution_per_100_clicks"]
            ),
            "visible_no_solution_episode_per_100_clicks": 100.0
            * ratio(
                float(
                    pd.to_numeric(
                        group["visible_no_solution_episode_count"],
                        errors="coerce",
                    ).sum()
                ),
                float(clicks.sum()),
            ),
            "manual_prop_per_attempt": mean(group["manual_prop_count_v3"]),
            "auto_extra_space_attempt_rate": mean(
                pd.to_numeric(
                    group["auto_extra_space_event_count"], errors="coerce"
                )
                > 0
            ),
            "tower_attempt_rate": mean(group["tower_template"]),
            "cn_attempt_rate": mean(group["cn_pattern_unknown"]),
        }
        for column in median_columns:
            row[column] = median(group[column])
        rows.append(row)
    return pd.DataFrame(rows).sort_values(
        ["cohort_player_rank", "level_index", "attempt_phase", "business_instance_id"]
    ).reset_index(drop=True)


def transform_metric(values: pd.Series, kind: str) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce").astype(float)
    if kind == "proportion":
        clipped = numeric.clip(0.01, 0.99)
        return np.log(clipped / (1.0 - clipped))
    if kind in {"count", "time"}:
        return np.log1p(numeric.clip(lower=0.0))
    if kind == "signed":
        return np.sign(numeric) * np.log1p(np.abs(numeric))
    return numeric


def robust_scale(values: Iterable[Any]) -> float:
    data = finite(values)
    if len(data) < 2:
        return math.nan
    center = float(np.median(data))
    mad = float(np.median(np.abs(data - center))) * 1.4826
    if mad > EPSILON:
        return mad
    q25, q75 = np.percentile(data, [25, 75])
    iqr_scale = float((q75 - q25) / 1.349)
    if iqr_scale > EPSILON:
        return iqr_scale
    standard = float(np.std(data, ddof=1))
    return standard if standard > EPSILON else math.nan


def add_player_relative_features(player_instances: pd.DataFrame) -> pd.DataFrame:
    result = player_instances.copy()
    for spec in METRIC_SPECS:
        metric = spec["metric"]
        transformed = transform_metric(result[metric], spec["kind"])
        result[f"_{metric}_analysis"] = transformed
        result[f"{metric}_player_baseline"] = math.nan
        result[f"{metric}_player_z"] = math.nan
        result[f"{metric}_local_baseline"] = math.nan
        result[f"{metric}_local_z"] = math.nan

        phase_scales = {
            phase: robust_scale(transformed[result["attempt_phase"] == phase])
            for phase in result["attempt_phase"].unique()
        }
        for (_user_id, phase), indices in result.groupby(
            ["user_id", "attempt_phase"], sort=False
        ).groups.items():
            index_list = list(indices)
            group = result.loc[index_list]
            group_values = pd.to_numeric(
                group[f"_{metric}_analysis"], errors="coerce"
            )
            group_raw = pd.to_numeric(group[metric], errors="coerce")
            for index in index_list:
                current_level = int(result.at[index, "level_index"])
                others_mask = group["level_index"].astype(int) != current_level
                others = group_values[others_mask]
                others_raw = group_raw[others_mask]
                current = pd.to_numeric(
                    pd.Series([result.at[index, f"_{metric}_analysis"]]),
                    errors="coerce",
                ).iloc[0]
                if not math.isfinite(float(current)) or others.notna().sum() < 5:
                    continue
                baseline = float(others.median())
                raw_baseline = float(others_raw.median())
                scale = robust_scale(others)
                global_scale = phase_scales.get(phase, math.nan)
                if not math.isfinite(scale) or scale < EPSILON:
                    scale = global_scale
                elif math.isfinite(global_scale):
                    scale = max(scale, global_scale * 0.10)
                if math.isfinite(scale) and scale > EPSILON:
                    result.at[index, f"{metric}_player_baseline"] = raw_baseline
                    result.at[index, f"{metric}_player_z"] = (
                        float(current) - baseline
                    ) / scale

                local_mask = others_mask & (
                    (group["level_index"].astype(int) - current_level).abs() <= 5
                )
                local = group_values[local_mask]
                local_raw = group_raw[local_mask]
                if local.notna().sum() >= 4:
                    local_baseline = float(local.median())
                    local_scale = robust_scale(local)
                    if not math.isfinite(local_scale) or local_scale < EPSILON:
                        local_scale = scale
                    result.at[index, f"{metric}_local_baseline"] = float(
                        local_raw.median()
                    )
                    if math.isfinite(local_scale) and local_scale > EPSILON:
                        result.at[index, f"{metric}_local_z"] = (
                            float(current) - local_baseline
                        ) / local_scale
    analysis_columns = [column for column in result if column.startswith("_")]
    return result.drop(columns=analysis_columns)


def stable_seed(parts: Sequence[Any]) -> int:
    raw = json.dumps(
        [legacy.json_safe(value) for value in parts],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return int(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8], 16)


def bootstrap_median_interval(
    values: np.ndarray, seed: int, confidence: float = 0.90, repeats: int = 300
) -> tuple[float, float]:
    if not len(values):
        return math.nan, math.nan
    if len(values) == 1:
        return float(values[0]), float(values[0])
    rng = np.random.default_rng(seed)
    sampled = rng.choice(values, size=(repeats, len(values)), replace=True)
    medians = np.median(sampled, axis=1)
    tail = (1.0 - confidence) / 2.0
    return float(np.quantile(medians, tail)), float(np.quantile(medians, 1.0 - tail))


def split_half_direction_rate(values: np.ndarray, seed: int, repeats: int = 100) -> float:
    if len(values) < 6:
        return math.nan
    rng = np.random.default_rng(seed)
    agree = 0
    valid = 0
    for _ in range(repeats):
        order = rng.permutation(len(values))
        split = len(values) // 2
        left = float(np.median(values[order[:split]]))
        right = float(np.median(values[order[split:]]))
        if abs(left) <= EPSILON or abs(right) <= EPSILON:
            continue
        valid += 1
        agree += int(math.copysign(1.0, left) == math.copysign(1.0, right))
    return ratio(agree, valid)


def loo_sign_flip_rate(values: np.ndarray) -> float:
    if len(values) < 3:
        return math.nan
    center = float(np.median(values))
    if abs(center) <= EPSILON:
        return 1.0
    sign = math.copysign(1.0, center)
    flips = 0
    for index in range(len(values)):
        reduced = np.delete(values, index)
        reduced_center = float(np.median(reduced))
        flips += int(
            abs(reduced_center) <= EPSILON
            or math.copysign(1.0, reduced_center) != sign
        )
    return flips / len(values)


def aggregate_dimension_evidence(
    player_instances: pd.DataFrame,
    group_columns: Sequence[str],
    scope: str,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    grouped = player_instances.groupby(list(group_columns), sort=True, dropna=False)
    for key, group in grouped:
        key_tuple = key if isinstance(key, tuple) else (key,)
        key_values = dict(zip(group_columns, key_tuple))
        for spec in METRIC_SPECS:
            metric = spec["metric"]
            raw_z = finite(group[f"{metric}_player_z"])
            if not len(raw_z):
                continue
            orientation = int(spec["orientation"])
            evidence_values = raw_z * orientation if orientation else raw_z
            center = float(np.median(evidence_values))
            direction_sign = 1 if center >= 0 else -1
            agreement_count = int(np.sum(evidence_values * direction_sign > 0))
            agreement = ratio(agreement_count, len(evidence_values))
            lower = wilson_lower(agreement_count, len(evidence_values))
            seed = stable_seed([scope, *key_tuple, metric])
            ci_low, ci_high = bootstrap_median_interval(evidence_values, seed)
            split_rate = split_half_direction_rate(evidence_values, seed + 1)
            loo_flip = loo_sign_flip_rate(evidence_values)
            ci_excludes_zero = (ci_low > 0) or (ci_high < 0)
            status = "未形成稳定差异"
            if (
                len(evidence_values) >= 20
                and abs(center) >= 0.5
                and agreement >= 0.70
                and lower > 0.50
                and ci_excludes_zero
                and (not math.isfinite(split_rate) or split_rate >= 0.90)
            ):
                status = "高置信复刻"
            elif (
                len(evidence_values) >= CONFIRMED_REPLICATION_SAMPLE
                and abs(center) >= 0.5
                and agreement >= 0.70
                and lower > 0.50
                and ci_excludes_zero
                and (not math.isfinite(split_rate) or split_rate >= 0.80)
            ):
                status = "已复刻"
            elif (
                len(evidence_values) >= MIN_REPLICATION_SAMPLE
                and abs(center) >= 0.5
                and agreement >= 0.70
            ):
                status = "探索信号"
            elif len(evidence_values) < MIN_REPLICATION_SAMPLE:
                status = "样本不足"

            if orientation:
                effect_direction = (
                    "高于本人基线_体验负担增加"
                    if center > 0
                    else "低于本人基线_体验负担降低"
                )
            else:
                effect_direction = (
                    "高于本人基线" if center > 0 else "低于本人基线"
                )
            row = {
                **key_values,
                "scope": scope,
                "dimension": spec["dimension"],
                "metric": metric,
                "metric_label": spec["label"],
                "burden_orientation": orientation,
                "player_count": len(evidence_values),
                "raw_metric_median": median(group[metric]),
                "player_relative_effect_median": center,
                "player_relative_effect_q25": percentile(evidence_values, 25),
                "player_relative_effect_q75": percentile(evidence_values, 75),
                "bootstrap90_low": ci_low,
                "bootstrap90_high": ci_high,
                "direction_agreement": agreement,
                "direction_agreement_wilson90_low": lower,
                "split_half_direction_agreement": split_rate,
                "leave_one_player_out_sign_flip_rate": loo_flip,
                "effect_direction": effect_direction,
                "replication_status": status,
            }
            rows.append(row)
    return pd.DataFrame(rows)


def reliability_label(player_count: int) -> str:
    if player_count >= 20:
        return "A_>=20人"
    if player_count >= 10:
        return "B_10-19人"
    if player_count >= 5:
        return "C_5-9人"
    return "D_<5人"


def build_profiles(
    player_instances: pd.DataFrame,
    evidence: pd.DataFrame,
    group_columns: Sequence[str],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, group in player_instances.groupby(
        list(group_columns), sort=True, dropna=False
    ):
        key_tuple = key if isinstance(key, tuple) else (key,)
        key_values = dict(zip(group_columns, key_tuple))
        mask = np.ones(len(evidence), dtype=bool)
        for column, value in key_values.items():
            mask &= evidence[column].astype(str).to_numpy() == str(value)
        sub = evidence.loc[mask]
        replicated = sub[sub["replication_status"].isin(["已复刻", "高置信复刻"])]
        high = replicated[
            (replicated["burden_orientation"] != 0)
            & (replicated["player_relative_effect_median"] > 0)
        ]
        low = replicated[
            (replicated["burden_orientation"] != 0)
            & (replicated["player_relative_effect_median"] < 0)
        ]
        tags: list[str] = []
        high_dimensions = set(high["dimension"])
        if "路径压缩" in high_dimensions:
            tags.append("路径持续压缩")
        if "背包压力" in high_dimensions:
            tags.append("持续高压")
        if {"认知负荷", "选择质量"}.intersection(high_dimensions):
            tags.append("认知/选择陷阱")
        if "道具依赖" in high_dimensions:
            tags.append("手动道具依赖")
        if "恢复韧性" in high_dimensions:
            tags.append("恢复困难")
        if "节奏" in high_dimensions:
            tags.append("节奏变慢")
        if "结果" in high_dimensions:
            tags.append("结果负担高")
        if not tags:
            tags.append("未见稳定高负担")
        sample = group.iloc[0]
        row = {
            **key_values,
            "level_index": int(sample["level_index"]),
            "attempt_phase": sample["attempt_phase"],
            "player_count": int(group["user_id"].nunique()),
            "sample_reliability": reliability_label(
                int(group["user_id"].nunique())
            ),
            "replicated_dimension_count": len(replicated),
            "replicated_high_burden_count": len(high),
            "replicated_low_burden_count": len(low),
            "replicated_high_metrics": "|".join(high["metric_label"].tolist()),
            "replicated_low_metrics": "|".join(low["metric_label"].tolist()),
            "behavior_tags": "+".join(tags),
            "fail_rate_median": median(group["fail_rate"]),
            "duration_per_click_median": median(group["duration_per_click_median"]),
            "visible_no_solution_share_median": median(
                group["visible_no_solution_share"]
            ),
            "pressure_certain_share_median": median(
                group["pressure_certain_share"]
            ),
            "manual_prop_per_100_clicks_median": median(
                group["manual_prop_per_100_clicks"]
            ),
            "geometry_click_coverage_median": median(
                group["geometry_click_coverage_v3"]
            ),
            "pattern_point_coverage_median": median(
                group["pattern_point_coverage_v3"]
            ),
        }
        if "business_instance_id" in group_columns:
            row.update(
                {
                    "business_instance_key": sample["business_instance_key"],
                    "level_template_id": sample["level_template_id"],
                    "level_dup_para_id": int(sample["level_dup_para_id"]),
                    "level_dup_para": sample["level_dup_para"],
                    "tower_attempt_rate": mean(group["tower_attempt_rate"]),
                    "cn_attempt_rate": mean(group["cn_attempt_rate"]),
                }
            )
        rows.append(row)
    return pd.DataFrame(rows).sort_values(
        [column for column in ("level_index", "attempt_phase", "business_instance_id") if column in rows[0]]
        if rows
        else []
    ).reset_index(drop=True)


def aggregate_hotspots(notable: pd.DataFrame) -> pd.DataFrame:
    if notable.empty:
        return notable
    data = notable.copy()
    data = data[data["geometry_state_valid"] == 1].copy()
    if data.empty:
        return data
    data["progress_decile"] = (
        pd.to_numeric(data["progress_before"], errors="coerce")
        .fillna(0)
        .clip(0, 0.999999)
        .mul(10)
        .astype(int)
        .add(1)
    )
    keys = [
        "business_instance_id",
        "level_index",
        "level_template_id",
        "level_dup_para_id",
        "level_dup_para",
        "attempt_phase",
        "progress_decile",
        "action_id",
        "selected_tile_num",
    ]
    rows: list[dict[str, Any]] = []
    for key, group in data.groupby(keys, sort=True, dropna=False):
        row = dict(zip(keys, key))
        row.update(
            {
                "player_count": int(group["user_id"].nunique()),
                "state_count": len(group),
                "visible_no_solution_rate": mean(group["visible_no_solution"]),
                "pressure_state_rate": mean(
                    pd.to_numeric(group["bag_size_before"], errors="coerce")
                    >= pd.to_numeric(group["bag_capacity_min"], errors="coerce") - 2
                ),
                "median_decision_dwell_sec": median(group["decision_dwell_sec"]),
                "median_bag_size_before": median(group["bag_size_before"]),
                "median_clickable_tile_count": median(group["clickable_tile_count"]),
                "median_visible_tile_count": median(
                    group["visible_tile_count_25pct"]
                ),
                "median_visible_solution_width": median(
                    group["visible_combination_pattern_count"]
                ),
                "median_visible_first_action_width": median(
                    group["visible_first_action_width_cap_min"]
                ),
                "manual_prop_state_count": int(
                    group["action_origin"].isin(["玩家手动道具", "玩家道具"]).sum()
                ),
                "auto_extra_space_state_count": int(
                    (group["action_origin"] == "自动背包+1").sum()
                ),
            }
        )
        rows.append(row)
    return pd.DataFrame(rows).sort_values(
        ["visible_no_solution_rate", "state_count", "player_count"],
        ascending=False,
    ).reset_index(drop=True)


def metric_dictionary() -> pd.DataFrame:
    fixed = [
        (
            "visible_tile_count_25pct",
            "实时局面",
            "剩余Tile中至少一个1/4格没有被更高Z牌覆盖的数量；这是玩家可见信息集。",
        ),
        (
            "clickable_tile_count",
            "实时局面",
            "四个1/4格都没有更高Z牌覆盖、此刻可合法点击的Tile数量。",
        ),
        (
            "visible_no_solution",
            "路径压缩",
            "背包花色+当前可见牌中，没有任何花色能凑满3张；是可见信息无解，不等同全局无解。",
        ),
        (
            "visible_first_action_width_cap_min/max",
            "路径压缩",
            "只使用当前已看见的牌，在容量窗口内先形成一次三消的可存活首步数量上下界。",
        ),
        (
            "omniscient_first_action_width_cap_min/max",
            "路径压缩",
            "允许后续新揭露牌时，在容量窗口内先形成一次三消的真实短窗首步数量上下界。",
        ),
        (
            "hidden_information_dependency",
            "可理解性",
            "玩家可见短窗无路、但允许隐藏牌后存在短窗路径；表示依赖尚未揭露的信息。",
        ),
        (
            "decision_dwell_sec",
            "节奏",
            "直接取当前动作自己的interval_ms/1000；不把中间道具或自动708的时间并入下一点击。",
        ),
        (
            "business_instance_id",
            "分析单位",
            "level_index+template_id+dup_para_id+规范化DupPara的稳定哈希。",
        ),
        (
            "player_relative_effect_median",
            "复刻证据",
            "每位玩家相对其余关卡个人基线的稳健残差，再在实例内取玩家中位数。",
        ),
        (
            "replication_status",
            "复刻证据",
            "固定证据门槛：样本数、效应、同向率、Bootstrap区间和随机对半方向一致性；不按分位数配额。",
        ),
    ]
    rows = [
        {"field": field, "dimension": dimension, "definition": definition}
        for field, dimension, definition in fixed
    ]
    for spec in METRIC_SPECS:
        rows.append(
            {
                "field": spec["metric"],
                "dimension": spec["dimension"],
                "definition": (
                    f"玩家内比较维度：{spec['label']}；"
                    + (
                        "数值升高解释为体验负担增加。"
                        if spec["orientation"] == 1
                        else "数值降低解释为体验负担增加。"
                        if spec["orientation"] == -1
                        else "仅描述行为方向，不直接解释为难度。"
                    )
                ),
            }
        )
    return pd.DataFrame(rows)


def audit_click_timing_definition(source: pd.DataFrame) -> dict[str, Any]:
    """量化旧“相邻点击差”口径吞入中间动作时间的影响。"""

    affected_attempts = 0
    overcounts: list[float] = []
    for replay_info in source["replay_info"]:
        steps = [
            step
            for step in legacy.parse_replay(legacy.clean_text(replay_info))
            if step.get("valid")
        ]
        previous_click_elapsed_ms = 0
        attempt_affected = False
        for step in steps:
            if int(step["action_id"]) != 1:
                continue
            elapsed_ms = int(step.get("elapsed_ms", 0))
            old_gap_ms = max(0, elapsed_ms - previous_click_elapsed_ms)
            direct_gap_ms = max(0, int(step.get("interval_ms", 0)))
            previous_click_elapsed_ms = elapsed_ms
            overcount_ms = old_gap_ms - direct_gap_ms
            if overcount_ms > 0:
                attempt_affected = True
                overcounts.append(overcount_ms / 1000.0)
        affected_attempts += int(attempt_affected)

    return {
        "affected_attempts": affected_attempts,
        "affected_clicks": len(overcounts),
        "affected_click_overcount_median_sec": median(overcounts),
        "affected_click_overcount_p90_sec": percentile(overcounts, 90),
    }


def build_quality(
    input_path: Path,
    source: pd.DataFrame,
    attempts: pd.DataFrame,
    player_instances: pd.DataFrame,
    dimension_evidence: pd.DataFrame,
    instance_profiles: pd.DataFrame,
    level_profiles: pd.DataFrame,
    hotspots: pd.DataFrame,
    stream_counts: Mapping[str, int],
    structures: legacy.StructureFeatureCatalog,
    template_dir: Path,
    config_dir: Path,
) -> dict[str, Any]:
    timing_audit = audit_click_timing_definition(source)
    exact_geometry_attempt = (
        pd.to_numeric(attempts["geometry_click_coverage_v3"], errors="coerce")
        == 1.0
    )
    full_pattern_point_attempt = (
        pd.to_numeric(attempts["pattern_point_coverage_v3"], errors="coerce")
        == 1.0
    )
    replicated = dimension_evidence[
        dimension_evidence["replication_status"].isin(
            ["已复刻", "高置信复刻"]
        )
    ]
    template_paths = [
        template_dir / f"{template_id}_m.bytes"
        for template_id in sorted(source["level_template_id"].unique())
    ]
    return {
        "title": "100–199关实时玩家行为分层 v3",
        "source": {
            "input_file_name": input_path.name,
            "input_sha256": legacy.file_sha256(input_path),
            "date_min": str(source["event_date"].min()),
            "date_max": str(source["event_date"].max()),
            "template_dir": str(template_dir),
            "template_combined_sha256": legacy.combined_sha256(template_paths),
            "config_level_source": structures.config_level_source,
            "config_level_sha256": structures.config_level_sha256,
            "level_tile_source": structures.level_tile_source,
            "level_tile_sha256": structures.level_tile_sha256,
            "config_client_tile_sha256": legacy.file_sha256(
                config_dir / "config_client_tile.csv"
            ),
        },
        "counts": {
            "source_rows_read": int(
                len(source) + source.attrs.get("excluded_non_outcome_attempts", 0)
            ),
            "normal_outcome_attempts": len(source),
            "excluded_non_outcome_attempts": int(
                source.attrs.get("excluded_non_outcome_attempts", 0)
            ),
            "players": int(source["user_id"].nunique()),
            "level_indices": int(source["level_index"].nunique()),
            "business_instances": int(attempts["business_instance_id"].nunique()),
            "player_instance_phase_rows": len(player_instances),
            "decision_action_rows": int(stream_counts["decision_action_rows"]),
            "notable_action_rows": int(stream_counts["notable_action_rows"]),
            "full_geometry_attempts": int(exact_geometry_attempt.sum()),
            "full_pattern_point_attempts": int(full_pattern_point_attempt.sum()),
            "tower_attempts": int(attempts["tower_template"].sum()),
            "cn_attempts": int(attempts["cn_pattern_unknown"].sum()),
            "capacity_unknown_attempts": int(
                (
                    pd.to_numeric(attempts["capacity_initial_min"], errors="coerce")
                    != pd.to_numeric(
                        attempts["capacity_initial_max"], errors="coerce"
                    )
                ).sum()
            ),
            "timing_definition_affected_attempts": timing_audit[
                "affected_attempts"
            ],
            "timing_definition_affected_clicks": timing_audit[
                "affected_clicks"
            ],
            "replicated_instance_dimension_rows": len(replicated),
            "instance_profile_rows": len(instance_profiles),
            "level_profile_rows": len(level_profiles),
            "hotspot_rows": len(hotspots),
        },
        "coverage": {
            "geometry_click_share": ratio(
                float(attempts["exact_geometry_click_count_v3"].sum()),
                float(attempts["click_count"].sum()),
            ),
            "pattern_click_share": ratio(
                float(attempts["pattern_point_click_count_v3"].sum()),
                float(attempts["click_count"].sum()),
            ),
            "cn_attempt_share": mean(attempts["cn_pattern_unknown"]),
            "tower_attempt_share": mean(attempts["tower_template"]),
            "median_visible_no_solution_share": median(
                attempts["visible_no_solution_share"]
            ),
            "timing_old_minus_direct_affected_click_median_sec": timing_audit[
                "affected_click_overcount_median_sec"
            ],
            "timing_old_minus_direct_affected_click_p90_sec": timing_audit[
                "affected_click_overcount_p90_sec"
            ],
        },
        "replication_status_counts": {
            str(key): int(value)
            for key, value in dimension_evidence[
                "replication_status"
            ].value_counts().items()
        },
        "method": {
            "primary_unit": "level_index+template_id+dup_para_id+canonical(DupPara)",
            "visible_definition": "四个2x2 footprint格中至少一个未被更高Z剩余Tile覆盖",
            "clickable_definition": "四个footprint格均未被更高Z剩余Tile覆盖",
            "visible_no_solution": "背包+当前可见Tile没有任何花色可凑满3张；不是全局无解",
            "time_definition": "每个动作直接使用自己的interval_ms；自动708不计玩家思考",
            "player_baseline": "同phase、排除同level_index后的玩家其余关卡中位数/MAD；另列前后±5关局部基线",
            "replication": "玩家残差中位数+方向一致率+Wilson下界+Bootstrap区间+100次随机对半方向一致率",
            "allocation": "固定证据门槛；不使用四分位名额，不限制各层数量",
            "phase": "首局和非首局分别计算；非首局不并回首局",
            "static_template_role": "只用于还原可见/可点击/花色关系，不直接进入分层",
        },
        "limitations": [
            "可见无解是玩家当前信息集无解，不等同从完整隐藏状态出发的全局无道具无解。",
            "短窗路径只搜索到背包装满前的下一次三消；完整全局路径数、容错步数和路径熵待状态引擎加入全局求解器。",
            "塔内运行时互换不影响几何可见集合；发生塔触发后的花色仍按配置映射给点估计，并显式标记tower_pattern_uncertainty。",
            "hhmy143的C–N具体图案不可由当前旧版种子唯一还原；这16局只保留几何、时间和观测曲线指标。",
            "容量未知局同时输出7/8格上下界；不会把未上报活动533误当玩家主动道具。",
            "移动/洗牌等未模拟道具发生前的局面可用，发生后降级到埋点曲线，不猜测牌面。",
            "样本是达到300关以上的Android高进度玩家；外推新手、iOS或付费人群需另取样复刻。",
        ],
    }


def write_report(
    path: Path,
    quality: Mapping[str, Any],
    dimension_evidence: pd.DataFrame,
    instance_profiles: pd.DataFrame,
) -> None:
    counts = quality["counts"]
    coverage = quality["coverage"]
    top = dimension_evidence[
        dimension_evidence["replication_status"].isin(
            ["已复刻", "高置信复刻"]
        )
        & (dimension_evidence["burden_orientation"] != 0)
        & (dimension_evidence["player_relative_effect_median"] > 0)
    ].sort_values(
        ["player_relative_effect_median", "player_count"], ascending=False
    )
    lines = [
        "# 关卡实时行为分层 v3",
        "",
        "## 结论",
        "",
        "- 分层主轴已改成：同一玩家跨关差异，再检查其他玩家是否复刻同方向差异。",
        "- 不再先产出一个总难度分；实例保留路径压缩、背包压力、认知负荷、节奏、选择质量、恢复、道具依赖和结果的多维坐标。",
        "- 玩家可见牌按至少一个1/4角露出计算；可点击牌仍要求四角全露出。",
        "- 每步思考时间直接取该动作自己的interval_ms；自动708不算玩家主动决策。",
        f"- 正常结算{counts['normal_outcome_attempts']:,}局、{counts['players']}名玩家、{counts['business_instances']:,}个业务实例。",
        f"- 几何状态覆盖全部点击的对局{counts['full_geometry_attempts']:,}局；花色点估计覆盖全部点击的对局{counts['full_pattern_point_attempts']:,}局。",
        f"- C–N仅{counts['cn_attempts']}局；塔局不再整体排除。",
        f"- 旧口径把中间道具/自动动作的时间并入下一次点击：共影响{counts['timing_definition_affected_attempts']:,}局、{counts['timing_definition_affected_clicks']:,}次点击；受影响点击被高估的中位数为{coverage['timing_old_minus_direct_affected_click_median_sec']:.3f}秒，P90为{coverage['timing_old_minus_direct_affected_click_p90_sec']:.3f}秒。",
        "",
        "## 已复刻的实例×行为维度",
        "",
    ]
    for record in top.head(15).to_dict("records"):
        lines.append(
            "- "
            f"{record['business_instance_id']} / {record['attempt_phase']} / "
            f"{record['metric_label']}：玩家残差中位{record['player_relative_effect_median']:.2f}，"
            f"同向{record['direction_agreement']:.0%}，n={record['player_count']}。"
        )
    if top.empty:
        lines.append("- 当前固定门槛下暂无已复刻的高负担维度；可查看探索信号，不应强行分层。")
    lines.extend(
        [
            "",
            "## 读取原则",
            "",
            "1. 先看05实例行为画像中的行为标签，再到04维度复刻证据核对具体效应和稳定性。",
            "2. `探索信号`不等同已确认；`已复刻`要求至少10名玩家、效应不小、方向一致且Bootstrap区间不跨0。",
            "3. 首局与非首局分别读取。非首局样本较少时允许只形成探索信号，不用首局结果替代。",
            "4. `可见无解`是感知层指标；是否真正全局无解仍要等完整状态引擎与全局求解器。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def save_outputs(
    output_dir: Path,
    attempts: pd.DataFrame,
    player_instances: pd.DataFrame,
    dimension_evidence: pd.DataFrame,
    instance_profiles: pd.DataFrame,
    level_profiles: pd.DataFrame,
    hotspots: pd.DataFrame,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    attempts.to_csv(
        output_dir / ATTEMPT_FILE,
        index=False,
        encoding="utf-8-sig",
        compression="gzip",
    )
    player_instances.to_csv(
        output_dir / PLAYER_INSTANCE_FILE,
        index=False,
        encoding="utf-8-sig",
        compression="gzip",
    )
    dimension_evidence.to_csv(
        output_dir / DIMENSION_FILE, index=False, encoding="utf-8-sig"
    )
    instance_profiles.to_csv(
        output_dir / INSTANCE_FILE, index=False, encoding="utf-8-sig"
    )
    level_profiles.to_csv(
        output_dir / LEVEL_FILE, index=False, encoding="utf-8-sig"
    )
    hotspots.to_csv(
        output_dir / HOTSPOT_FILE,
        index=False,
        encoding="utf-8-sig",
        compression="gzip",
    )


def build_manifest(
    staging_dir: Path,
    output_dir: Path,
    quality: Mapping[str, Any],
    instance_profiles: pd.DataFrame,
    level_profiles: pd.DataFrame,
    dimension_evidence: pd.DataFrame,
    hotspots: pd.DataFrame,
) -> Path:
    staging_dir.mkdir(parents=True, exist_ok=True)
    signals = dimension_evidence[
        dimension_evidence["replication_status"].isin(
            ["探索信号", "已复刻", "高置信复刻"]
        )
    ].copy()
    frames = {
        "instances": instance_profiles,
        "levels": level_profiles,
        "evidence": signals,
        "hotspots": hotspots.head(1000),
        "dictionary": metric_dictionary(),
    }
    sheets: dict[str, Any] = {}
    for name, frame in frames.items():
        path = staging_dir / f"{name}.ndjson"
        legacy.write_ndjson(path, frame, list(frame.columns))
        sheets[name] = {
            "path": str(path),
            "columns": list(frame.columns),
            "row_count": len(frame),
        }
    manifest = {
        "title": quality["title"],
        "quality": quality,
        "sheets": sheets,
        "output_workbook": str(output_dir / WORKBOOK_FILE),
    }
    path = staging_dir / "manifest_v3.json"
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return path


def run(args: argparse.Namespace) -> dict[str, Any]:
    input_path = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    config_dir = Path(args.config_dir).expanduser().resolve()
    template_dir = Path(args.template_dir).expanduser().resolve()
    reader_dir = Path(args.template_reader_dir).expanduser().resolve()
    staging_dir = (
        Path(args.staging_dir).expanduser().resolve()
        if args.staging_dir
        else output_dir / "_workbook_staging"
    )
    source = pd.read_csv(input_path, dtype=str, keep_default_na=False)
    legacy.validate_source(source)
    normalized_status = source["level_status"].astype(str).str.strip().str.lower()
    valid_outcome = source["level_status"].map(v2.is_normal_outcome_status)
    source.attrs["excluded_non_outcome_attempts"] = int((~valid_outcome).sum())
    source.attrs["excluded_status_counts"] = {
        str(key or "空值"): int(value)
        for key, value in normalized_status[~valid_outcome].value_counts().items()
    }
    source = source.loc[valid_outcome].copy()
    source["level_index"] = pd.to_numeric(
        source["level_index"], errors="raise"
    ).astype(int)
    structures = legacy.StructureFeatureCatalog(
        config_dir,
        template_dir,
        reader_dir,
        args.level_tile_git_revision,
        args.config_level_git_revision,
    )
    print("[1/7] 解析基础回放、配置和模板", flush=True)
    attempts, _legacy_hotspots = legacy.extract_attempts(source, structures)
    print("[2/7] 重建1/4可见、可点击、背包与动作前状态", flush=True)
    attempts, notable, stream_counts = enhance_attempts_v3(
        source, attempts, structures, output_dir / DECISION_FILE
    )
    print("[3/7] 聚合玩家×业务实例×首局/非首局", flush=True)
    player_instances = aggregate_player_instances(attempts)
    player_instances = add_player_relative_features(player_instances)
    print("[4/7] 检验实例差异是否被其他玩家复刻", flush=True)
    instance_evidence = aggregate_dimension_evidence(
        player_instances,
        ["business_instance_id", "attempt_phase"],
        "业务实例",
    )
    instance_profiles = build_profiles(
        player_instances,
        instance_evidence,
        ["business_instance_id", "attempt_phase"],
    )
    level_evidence = aggregate_dimension_evidence(
        player_instances, ["level_index", "attempt_phase"], "关卡号"
    )
    level_profiles = build_profiles(
        player_instances, level_evidence, ["level_index", "attempt_phase"]
    )
    hotspots = aggregate_hotspots(notable)
    print("[5/7] 写出明细、残差、复刻证据和卡点", flush=True)
    save_outputs(
        output_dir,
        attempts,
        player_instances,
        instance_evidence,
        instance_profiles,
        level_profiles,
        hotspots,
    )
    quality = build_quality(
        input_path,
        source,
        attempts,
        player_instances,
        instance_evidence,
        instance_profiles,
        level_profiles,
        hotspots,
        stream_counts,
        structures,
        template_dir,
        config_dir,
    )
    (output_dir / QUALITY_FILE).write_text(
        json.dumps(quality, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    write_report(
        output_dir / REPORT_FILE, quality, instance_evidence, instance_profiles
    )
    print("[6/7] 生成工作簿清单", flush=True)
    manifest = build_manifest(
        staging_dir,
        output_dir,
        quality,
        instance_profiles,
        level_profiles,
        instance_evidence,
        hotspots,
    )
    print("[7/7] v3分析数据准备完成", flush=True)
    return {
        "manifest": str(manifest),
        "output_dir": str(output_dir),
        "workbook": str(output_dir / WORKBOOK_FILE),
        "counts": quality["counts"],
        "coverage": quality["coverage"],
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--input", required=True)
    result.add_argument("--output-dir", required=True)
    result.add_argument("--staging-dir")
    result.add_argument("--config-dir", default=str(DEFAULT_CONFIG_DIR))
    result.add_argument("--template-dir", default=str(DEFAULT_TEMPLATE_DIR))
    result.add_argument(
        "--template-reader-dir", default=str(DEFAULT_TEMPLATE_READER_DIR)
    )
    result.add_argument(
        "--level-tile-git-revision", default=legacy.DEFAULT_LEVEL_TILE_GIT_REVISION
    )
    result.add_argument(
        "--config-level-git-revision", default=legacy.DEFAULT_CONFIG_LEVEL_GIT_REVISION
    )
    return result


def main() -> int:
    output = run(parser().parse_args())
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
