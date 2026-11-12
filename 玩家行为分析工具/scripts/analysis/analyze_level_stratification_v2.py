#!/usr/bin/env python3
"""按实际内容实例与玩家真实路径生成关卡行为分层 v2。

主分析单位是：level_index + level_template_id + level_dup_para_id +
规范化 DupPara。level_id、版本、用户分组和配置哈希只保留为血缘/投放上下文，
不直接进入难度分。主路径指标来自玩家每次点击前的实际状态；只有花色、几何、
背包曲线逐步相符时才输出安全分支/窄路指标，无法唯一还原的局不会被猜测补齐。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd


sys.dont_write_bytecode = True
MODULE_DIR = Path(__file__).resolve().parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

import analyze_level_stratification as legacy  # noqa: E402


DEFAULT_CONFIG_DIR = Path.home() / "ulinkconfig" / "csv"
DEFAULT_TEMPLATE_DIR = (
    Path.home() / "tile" / "Tile" / "Assets" / "Resources" / "TileMatchLevelData"
)
DEFAULT_TEMPLATE_READER_DIR = Path(__file__).resolve().parent

EXTRA_SPACE_IDS = {508, 533, 601, 708, 801}
MANUAL_EXTRA_SPACE_IDS = {508}
AUTO_EXTRA_SPACE_IDS = {533, 708}
COMPAT_PREGAME_EXTRA_SPACE_IDS = {601, 801}
BOARD_SAFE_ACTION_IDS = {1, 2, *EXTRA_SPACE_IDS}

ATTEMPT_FILE = "01_对局路径特征.csv.gz"
PLAYER_INSTANCE_FILE = "02_玩家实例特征.csv"
INSTANCE_FILE = "03_实例分层结果.csv"
LEVEL_FILE = "04_关卡号汇总.csv"
HOTSPOT_FILE = "05_瓶颈局面热点.csv.gz"
TEMPLATE_FILE = "06_模板结构参考.csv"
QUALITY_FILE = "分析质量报告.json"
REPORT_FILE = "结论与口径.md"
WORKBOOK_FILE = "关卡实例行为分层_v2.xlsx"

PHASE_FIRST = "首局"
PHASE_RETRY = "非首局"
MIN_LABEL_SAMPLE = 5
GLOBAL_PRIOR_STRENGTH = 5.0


def finite(values: Iterable[Any]) -> np.ndarray:
    result = pd.to_numeric(pd.Series(list(values)), errors="coerce").to_numpy(float)
    return result[np.isfinite(result)]


def mean(values: Iterable[Any]) -> float:
    data = finite(values)
    return float(data.mean()) if len(data) else math.nan


def median(values: Iterable[Any]) -> float:
    data = finite(values)
    return float(np.median(data)) if len(data) else math.nan


def percentile(values: Iterable[Any], q: float) -> float:
    data = finite(values)
    return float(np.percentile(data, q)) if len(data) else math.nan


def ratio(numerator: float, denominator: float) -> float:
    return float(numerator) / float(denominator) if denominator else math.nan


def mean_confidence_interval(
    values: Iterable[Any], z_value: float = 1.6448536269514722
) -> tuple[float, float]:
    """玩家等权均值的近似90%区间，兼容非首局的0~1失败比例。"""

    data = finite(values)
    if not len(data):
        return math.nan, math.nan
    center = float(data.mean())
    if len(data) == 1:
        return 0.0, 1.0
    standard_error = float(data.std(ddof=1) / math.sqrt(len(data)))
    return (
        max(0.0, center - z_value * standard_error),
        min(1.0, center + z_value * standard_error),
    )


def hash_id(prefix: str, parts: Sequence[Any], length: int = 16) -> str:
    raw = json.dumps(list(parts), ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def normalized_dup_para(value: Any) -> str:
    parts = [part.strip() for part in legacy.clean_text(value).split("|")]
    return "|".join(parts)


def is_normal_outcome_status(value: Any) -> bool:
    status = legacy.clean_text(value).lower()
    return status in (legacy.PASS_STATUSES | legacy.FAIL_STATUSES)


def business_instance_parts(source: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        legacy.as_int(source.get("level_index"), -1),
        legacy.clean_text(source.get("level_template_id")),
        legacy.as_int(source.get("level_dup_para_id"), 0),
        normalized_dup_para(source.get("level_dup_para")),
    )


def classify_extra_space(steps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ids = [
        int(step["action_id"])
        for step in steps
        if step.get("valid") and step.get("action_id") in EXTRA_SPACE_IDS
    ]
    unique_ids = sorted(set(ids))
    manual = any(action_id in MANUAL_EXTRA_SPACE_IDS for action_id in unique_ids)
    automatic = any(action_id in AUTO_EXTRA_SPACE_IDS for action_id in unique_ids)
    compat = any(
        action_id in COMPAT_PREGAME_EXTRA_SPACE_IDS for action_id in unique_ids
    )
    categories = []
    if manual:
        categories.append("508普通局内")
    if 708 in unique_ids:
        categories.append("708限时权益自动应用")
    if 533 in unique_ids:
        categories.append("533活动被动扩容")
    if compat:
        categories.append("601/801兼容局前")
    return {
        "extra_space_action_ids": "|".join(map(str, unique_ids)),
        "extra_space_apply_count": len(ids),
        "manual_extra_space_used": int(manual),
        "auto_extra_space_applied": int(automatic),
        "compat_pregame_extra_space_recorded": int(compat),
        "known_extra_space_applied": int(bool(unique_ids)),
        "extra_space_source": "+".join(categories) if categories else "未记录扩容动作",
    }


def _available_indices(
    removed: frozenset[int] | set[int], blocker_sets: Sequence[frozenset[int]]
) -> tuple[int, ...]:
    return tuple(
        index
        for index, blockers in enumerate(blocker_sets)
        if index not in removed and blockers.issubset(removed)
    )


def _click_transition(
    tile_index: int,
    removed: frozenset[int],
    bag: tuple[tuple[int, int], ...],
    tile_ids: Sequence[int],
    capacity: int,
) -> tuple[frozenset[int], tuple[tuple[int, int], ...], bool, bool]:
    counts = Counter(dict(bag))
    tile_id = int(tile_ids[tile_index])
    counts[tile_id] += 1
    matched = counts[tile_id] == 3
    if matched:
        counts[tile_id] = 0
    next_bag = tuple(sorted((key, value) for key, value in counts.items() if value))
    failed = sum(value for _, value in next_bag) >= capacity
    return frozenset((*removed, tile_index)), next_bag, matched, failed


def surviving_first_action_count(
    removed: frozenset[int],
    bag: tuple[tuple[int, int], ...],
    tile_ids: Sequence[int],
    blocker_sets: Sequence[frozenset[int]],
    capacity: int,
) -> int:
    """当前剩余容量窗口内，能在满包前形成一次三消的首步 Tile 数。"""

    bag_size = sum(value for _, value in bag)
    search_depth = capacity - bag_size
    if search_depth <= 0:
        return 0

    def can_recycle(
        state_removed: frozenset[int],
        state_bag: tuple[tuple[int, int], ...],
        steps_left: int,
    ) -> bool:
        if steps_left <= 0:
            return False
        for candidate in _available_indices(state_removed, blocker_sets):
            next_removed, next_bag, matched, failed = _click_transition(
                candidate, state_removed, state_bag, tile_ids, capacity
            )
            if failed:
                continue
            if matched:
                return True
            if can_recycle(next_removed, next_bag, steps_left - 1):
                return True
        return False

    viable = 0
    for candidate in _available_indices(removed, blocker_sets):
        next_removed, next_bag, matched, failed = _click_transition(
            candidate, removed, bag, tile_ids, capacity
        )
        if failed:
            continue
        if matched or can_recycle(next_removed, next_bag, search_depth - 1):
            viable += 1
    return viable


def observed_path_metrics(
    steps: Sequence[Mapping[str, Any]],
    bag_points: Sequence[int],
    surface_points: Sequence[int],
    click_gaps: Sequence[float],
) -> dict[str, Any]:
    """用真实曲线构造全样本可用的路径代理，不猜道具后的未观测状态。"""

    capacity = 7
    click_index = 0
    state_mutated_after_last_click = False
    narrow_flags: list[bool] = []
    pressure_narrow_flags: list[bool] = []
    narrow_seconds = 0.0
    valid_decisions = 0
    max_capacity = 7
    capacity8_clicks = 0
    for step in steps:
        if not step.get("valid"):
            continue
        action_id = int(step["action_id"])
        if action_id in EXTRA_SPACE_IDS:
            capacity = 8
            max_capacity = 8
            continue
        if action_id not in {1, 2}:
            state_mutated_after_last_click = True
            continue
        if action_id == 2:
            state_mutated_after_last_click = True
            continue
        if capacity == 8:
            capacity8_clicks += 1
        if (
            click_index > 0
            and click_index - 1 < len(surface_points)
            and click_index - 1 < len(bag_points)
            and not state_mutated_after_last_click
        ):
            pre_surface = int(surface_points[click_index - 1])
            pre_bag = int(bag_points[click_index - 1])
            narrow = pre_surface <= 3
            pressure_narrow = narrow and pre_bag >= capacity - 2
            narrow_flags.append(narrow)
            pressure_narrow_flags.append(pressure_narrow)
            valid_decisions += 1
            if narrow and click_index < len(click_gaps):
                narrow_seconds += float(click_gaps[click_index])
        state_mutated_after_last_click = False
        click_index += 1

    return {
        "observed_decision_count": valid_decisions,
        "observed_surface_narrow_count": int(sum(narrow_flags)),
        "observed_surface_narrow_share": ratio(sum(narrow_flags), valid_decisions),
        "observed_surface_narrow_episode_count": len(
            legacy.threshold_onsets([int(flag) for flag in narrow_flags], 1)
        ),
        "observed_surface_narrow_longest_run": legacy.longest_true_run(narrow_flags),
        "observed_pressure_narrow_count": int(sum(pressure_narrow_flags)),
        "observed_pressure_narrow_share": ratio(
            sum(pressure_narrow_flags), valid_decisions
        ),
        "observed_surface_narrow_seconds_lower_bound": narrow_seconds,
        "max_known_bag_capacity": max_capacity,
        "capacity8_click_share": ratio(capacity8_clicks, click_index),
    }


def exact_path_metrics(
    source: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]],
    click_gaps: Sequence[float],
    bag_points: Sequence[int],
    surface_points: Sequence[int],
    instance: Any,
    template: Any,
    *,
    start_capacity: int = 7,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """沿真实点击路径计算几何瓶颈与高压容量窗口分支。

    每个已访问、非收尾局面都会计算实际可点击宽度；背包进入容量前三格时，
    再计算“在装满前先完成一次三消”的可存活首步宽度。这仍是局部窗口，
    不是对整局剩余解空间的全局求解。
    """

    valid_steps = [step for step in steps if step.get("valid")]
    action_ids = {int(step["action_id"]) for step in valid_steps}
    click_steps = [step for step in valid_steps if step.get("action_id") == 1]
    base = {
        "exact_path_eligible": 0,
        "pattern_mapping_verified": 0,
        "geometry_path_verified": 0,
        "exact_decision_count": 0,
        "exact_high_pressure_state_count": 0,
        "exact_narrow_state_count": 0,
        "exact_narrow_state_share": math.nan,
        "exact_narrow_episode_count": math.nan,
        "exact_narrow_seconds_lower_bound": math.nan,
        "exact_geometric_narrow_state_count": math.nan,
        "exact_geometric_narrow_episode_count": math.nan,
        "exact_geometric_narrow_seconds_lower_bound": math.nan,
        "exact_pressure_narrow_state_count": math.nan,
        "exact_pressure_narrow_episode_count": math.nan,
        "exact_pressure_narrow_seconds_lower_bound": math.nan,
        "exact_critical_state_count": math.nan,
        "exact_forced_loss_window_count": math.nan,
        "exact_any_narrow": math.nan,
        "exact_any_forced_loss_window": math.nan,
        "exact_persistent_narrow": math.nan,
        "exact_min_surviving_first_actions": math.nan,
        "exact_selected_nonviable_count": math.nan,
        "capacity_resolution": "",
        "capacity_sensitive_path": 0,
    }
    if (
        template.features.get("template_tower_count", 0) > 0
        or not action_ids.issubset(BOARD_SAFE_ACTION_IDS)
        or len(click_steps) != len(bag_points)
        or len(click_steps) != len(surface_points)
        or not click_steps
        or instance.features.get("concrete_pattern_tile_share") != 1.0
        or any(not step.get("tile_nums") for step in click_steps)
    ):
        return base, []

    tile_count = len(template.level.tiles)
    tile_ids = [
        int(instance.tile_by_num[index + 1].tile_id) for index in range(tile_count)
    ]
    if any(tile_id <= 0 for tile_id in tile_ids):
        return base, []
    blockers = template.graph.direct_blockers
    removed: frozenset[int] = frozenset()
    bag: tuple[tuple[int, int], ...] = ()
    capacity = start_capacity
    click_index = 0
    narrow_flags: list[bool] = []
    geometric_narrow_flags: list[bool] = []
    pressure_narrow_flags: list[bool] = []
    narrow_seconds = 0.0
    geometric_narrow_seconds = 0.0
    pressure_narrow_seconds = 0.0
    high_pressure_count = 0
    critical_count = 0
    forced_count = 0
    selected_nonviable = 0
    minimum_width = math.inf
    hotspots: list[dict[str, Any]] = []
    bag_match = True
    surface_match = True
    legal_match = True
    failed_before_last_click = False
    pending_full = False

    for step in valid_steps:
        action_id = int(step["action_id"])
        if action_id in EXTRA_SPACE_IDS:
            capacity = 8
            pending_full = sum(value for _, value in bag) >= capacity
            continue
        if pending_full and action_id == 1:
            failed_before_last_click = True
            break
        if action_id == 2:
            for tile_num in step.get("tile_nums") or []:
                tile_index = int(tile_num) - 1
                if 0 <= tile_index < tile_count:
                    removed = frozenset((*removed, tile_index))
            continue
        if action_id != 1:
            continue

        tile_num = int(step["tile_nums"][0])
        selected_index = tile_num - 1
        available = _available_indices(removed, blockers)
        if selected_index not in available:
            legal_match = False
            break
        bag_size = sum(value for _, value in bag)
        remaining_tiles = tile_count - len(removed)
        high_pressure = bag_size >= capacity - 2 and bag_size < capacity
        width = math.nan
        pressure_narrow = False
        forced = False
        critical = False
        geometric_narrow = remaining_tiles > 9 and len(available) <= 3
        if high_pressure and remaining_tiles > 9:
            high_pressure_count += 1
            width = surviving_first_action_count(
                removed, bag, tile_ids, blockers, capacity
            )
            minimum_width = min(minimum_width, width)
            pressure_narrow = width <= 2
            forced = width == 0
            critical = bag_size == capacity - 1 and width <= 2
            if pressure_narrow:
                pressure_narrow_seconds += (
                    float(click_gaps[click_index])
                    if click_index < len(click_gaps)
                    else 0.0
                )
            critical_count += int(critical)
            forced_count += int(forced)
        combined_narrow = geometric_narrow or pressure_narrow
        if geometric_narrow:
            geometric_narrow_seconds += (
                float(click_gaps[click_index])
                if click_index < len(click_gaps)
                else 0.0
            )
        if combined_narrow:
            narrow_seconds += (
                float(click_gaps[click_index])
                if click_index < len(click_gaps)
                else 0.0
            )

        next_removed, next_bag, _matched, _failed = _click_transition(
            selected_index, removed, bag, tile_ids, capacity
        )
        pending_full = _failed
        if high_pressure and remaining_tiles > 9:
            selected_viable = False
            if math.isfinite(float(width)):
                # 只需判断实际首步是否在容量窗口内可形成三消。
                nr, nb, matched, failed = _click_transition(
                    selected_index, removed, bag, tile_ids, capacity
                )
                if not failed:
                    if matched:
                        selected_viable = True
                    else:
                        remaining_depth = capacity - bag_size - 1

                        def suffix_can_recycle(
                            state_removed: frozenset[int],
                            state_bag: tuple[tuple[int, int], ...],
                            steps_left: int,
                        ) -> bool:
                            if steps_left <= 0:
                                return False
                            for candidate in _available_indices(
                                state_removed, blockers
                            ):
                                xr, xb, xm, xf = _click_transition(
                                    candidate,
                                    state_removed,
                                    state_bag,
                                    tile_ids,
                                    capacity,
                                )
                                if xf:
                                    continue
                                if xm or suffix_can_recycle(xr, xb, steps_left - 1):
                                    return True
                            return False

                        selected_viable = suffix_can_recycle(
                            nr, nb, remaining_depth
                        )
                selected_nonviable += int(not selected_viable)

        if combined_narrow or forced:
            tile = instance.tile_by_num.get(tile_num)
            hotspots.append(
                {
                    "source_row_no": legacy.as_int(source.get("source_row_no"), 0),
                    "user_id": legacy.clean_text(source.get("user_id")),
                    "event_time": legacy.clean_text(source.get("event_time")),
                    "level_index": legacy.as_int(source.get("level_index"), -1),
                    "level_template_id": legacy.clean_text(
                        source.get("level_template_id")
                    ),
                    "level_dup_para_id": legacy.as_int(
                        source.get("level_dup_para_id"), 0
                    ),
                    "level_dup_para": normalized_dup_para(
                        source.get("level_dup_para")
                    ),
                    "attempt_phase": (
                        PHASE_FIRST
                        if legacy.as_int(source.get("level_play_count"), 0) == 1
                        else PHASE_RETRY
                    ),
                    "click_no": click_index + 1,
                    "selected_tile_num": tile_num,
                    "selected_x": getattr(tile, "x", math.nan),
                    "selected_y": getattr(tile, "y", math.nan),
                    "selected_z": getattr(tile, "z", math.nan),
                    "click_gap_sec": (
                        float(click_gaps[click_index])
                        if click_index < len(click_gaps)
                        else math.nan
                    ),
                    "bag_size_before": bag_size,
                    "bag_capacity": capacity,
                    "remaining_tiles": remaining_tiles,
                    "legal_click_width": len(available),
                    "surviving_first_action_width": width,
                    "bottleneck_type": "+".join(
                        label
                        for label, active in (
                            ("几何窄路", geometric_narrow),
                            ("高压短窗口窄路", pressure_narrow),
                            ("无干预强制失败窗口", forced),
                        )
                        if active
                    ),
                    "is_narrow_state": int(combined_narrow),
                    "is_geometric_narrow": int(geometric_narrow),
                    "is_pressure_narrow": int(pressure_narrow),
                    "is_critical_state": int(critical),
                    "is_forced_loss_window": int(forced),
                }
            )

        removed, bag = next_removed, next_bag
        if sum(value for _, value in bag) != int(bag_points[click_index]):
            bag_match = False
        predicted_surface = len(_available_indices(removed, blockers))
        if predicted_surface != int(surface_points[click_index]):
            surface_match = False
        narrow_flags.append(combined_narrow)
        geometric_narrow_flags.append(geometric_narrow)
        pressure_narrow_flags.append(pressure_narrow)
        click_index += 1

    verified = (
        legal_match
        and bag_match
        and surface_match
        and not failed_before_last_click
        and click_index == len(click_steps)
    )
    if not verified:
        return base, []

    episodes = len(legacy.threshold_onsets([int(flag) for flag in narrow_flags], 1))
    geometric_episodes = len(
        legacy.threshold_onsets([int(flag) for flag in geometric_narrow_flags], 1)
    )
    pressure_episodes = len(
        legacy.threshold_onsets([int(flag) for flag in pressure_narrow_flags], 1)
    )
    persistent = episodes >= 3 or narrow_seconds >= 10.0
    base.update(
        {
            "exact_path_eligible": 1,
            "pattern_mapping_verified": 1,
            "geometry_path_verified": 1,
            "exact_decision_count": len(narrow_flags),
            "exact_high_pressure_state_count": high_pressure_count,
            "exact_narrow_state_count": int(sum(narrow_flags)),
            "exact_narrow_state_share": ratio(sum(narrow_flags), len(narrow_flags)),
            "exact_narrow_episode_count": episodes,
            "exact_narrow_seconds_lower_bound": narrow_seconds,
            "exact_geometric_narrow_state_count": int(
                sum(geometric_narrow_flags)
            ),
            "exact_geometric_narrow_episode_count": geometric_episodes,
            "exact_geometric_narrow_seconds_lower_bound": geometric_narrow_seconds,
            "exact_pressure_narrow_state_count": int(sum(pressure_narrow_flags)),
            "exact_pressure_narrow_episode_count": pressure_episodes,
            "exact_pressure_narrow_seconds_lower_bound": pressure_narrow_seconds,
            "exact_critical_state_count": critical_count,
            "exact_forced_loss_window_count": forced_count,
            "exact_any_narrow": int(any(narrow_flags)),
            "exact_any_forced_loss_window": int(forced_count > 0),
            "exact_persistent_narrow": int(persistent),
            "exact_min_surviving_first_actions": (
                minimum_width if math.isfinite(minimum_width) else math.nan
            ),
            "exact_selected_nonviable_count": selected_nonviable,
        }
    )
    return base, hotspots


EXACT_CAPACITY_COMPARISON_FIELDS = (
    "exact_any_narrow",
    "exact_any_forced_loss_window",
    "exact_persistent_narrow",
    "exact_narrow_episode_count",
    "exact_narrow_state_count",
    "exact_geometric_narrow_state_count",
    "exact_geometric_narrow_episode_count",
    "exact_geometric_narrow_seconds_lower_bound",
    "exact_pressure_narrow_state_count",
    "exact_pressure_narrow_episode_count",
    "exact_pressure_narrow_seconds_lower_bound",
    "exact_critical_state_count",
    "exact_forced_loss_window_count",
    "exact_min_surviving_first_actions",
)


def resolve_exact_capacity_metrics(
    source_row: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]],
    click_gaps: Sequence[float],
    bag_points: Sequence[int],
    surface_points: Sequence[int],
    instance: Any,
    template: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """显式处理533不可见导致的7/8格容量不确定性。"""

    valid_steps = [step for step in steps if step.get("valid")]
    first_click_position = next(
        (
            index
            for index, step in enumerate(valid_steps)
            if step.get("action_id") == 1
        ),
        len(valid_steps),
    )
    pre_click_ids = {
        int(step["action_id"])
        for step in valid_steps[:first_click_position]
        if step.get("action_id") in EXTRA_SPACE_IDS
    }
    recorded_extra_space_ids = {
        int(step["action_id"])
        for step in valid_steps
        if step.get("action_id") in EXTRA_SPACE_IDS
    }
    explicit_pre_click_capacity8 = bool(pre_click_ids)
    reaches_eight = any(value >= 8 for value in bag_points)
    continues_after_seven = any(
        value >= 7 and index < len(bag_points) - 1
        for index, value in enumerate(bag_points)
    )
    bag_full_events = legacy.as_int(source_row.get("bag_full_event_count"), 0)
    status = legacy.clean_text(source_row.get("level_status")).lower()
    confirmed_seven = (
        not pre_click_ids
        and bag_full_events > 0
        and bool(bag_points)
        and max(bag_points) == 7
        and status in legacy.FAIL_STATUSES
    )

    if explicit_pre_click_capacity8:
        result, hotspots = exact_path_metrics(
            source_row,
            steps,
            click_gaps,
            bag_points,
            surface_points,
            instance,
            template,
            start_capacity=8,
        )
        result["capacity_resolution"] = "8格_进局前扩容记录"
        return result, hotspots
    if recorded_extra_space_ids:
        result, hotspots = exact_path_metrics(
            source_row,
            steps,
            click_gaps,
            bag_points,
            surface_points,
            instance,
            template,
            start_capacity=7,
        )
        result["capacity_resolution"] = "7转8格_按复盘动作时点"
        return result, hotspots
    if reaches_eight or continues_after_seven:
        result, hotspots = exact_path_metrics(
            source_row,
            steps,
            click_gaps,
            bag_points,
            surface_points,
            instance,
            template,
            start_capacity=8,
        )
        result["capacity_resolution"] = "8格_曲线证明_按活动进局前生效"
        return result, hotspots
    if confirmed_seven:
        result, hotspots = exact_path_metrics(
            source_row,
            steps,
            click_gaps,
            bag_points,
            surface_points,
            instance,
            template,
            start_capacity=7,
        )
        result["capacity_resolution"] = "7格_满包事件正向证据"
        return result, hotspots

    seven, seven_hotspots = exact_path_metrics(
        source_row,
        steps,
        click_gaps,
        bag_points,
        surface_points,
        instance,
        template,
        start_capacity=7,
    )
    eight, eight_hotspots = exact_path_metrics(
        source_row,
        steps,
        click_gaps,
        bag_points,
        surface_points,
        instance,
        template,
        start_capacity=8,
    )
    if seven["exact_path_eligible"] and not eight["exact_path_eligible"]:
        seven["capacity_resolution"] = "7格_仅7格路径与结算一致"
        return seven, seven_hotspots
    if eight["exact_path_eligible"] and not seven["exact_path_eligible"]:
        eight["capacity_resolution"] = "8格_仅8格路径与结算一致"
        return eight, eight_hotspots
    if seven["exact_path_eligible"] and eight["exact_path_eligible"]:
        same = all(
            (
                seven[field] == eight[field]
                or (
                    isinstance(seven[field], float)
                    and isinstance(eight[field], float)
                    and math.isnan(seven[field])
                    and math.isnan(eight[field])
                )
            )
            for field in EXACT_CAPACITY_COMPARISON_FIELDS
        )
        if same:
            seven["capacity_resolution"] = "7/8格不确定_路径指标一致"
            return seven, seven_hotspots
        unresolved = dict(seven)
        unresolved["exact_path_eligible"] = 0
        unresolved["capacity_resolution"] = "7/8格不确定_路径指标分歧"
        unresolved["capacity_sensitive_path"] = 1
        for field in EXACT_CAPACITY_COMPARISON_FIELDS:
            values = finite([seven[field], eight[field]])
            unresolved[f"{field}_min"] = float(values.min()) if len(values) else math.nan
            unresolved[f"{field}_max"] = float(values.max()) if len(values) else math.nan
            unresolved[field] = math.nan
        return unresolved, []

    seven["capacity_resolution"] = "花色/几何/背包轨迹未通过验证"
    return seven, []


def enhance_attempts(
    source: pd.DataFrame,
    attempts: pd.DataFrame,
    structures: legacy.StructureFeatureCatalog,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    enhancements: list[dict[str, Any]] = []
    hotspot_rows: list[dict[str, Any]] = []
    for row_no, source_row in enumerate(source.to_dict("records"), start=1):
        source_row = dict(source_row)
        source_row["source_row_no"] = row_no
        steps = legacy.parse_replay(legacy.clean_text(source_row.get("replay_info")))
        timing = legacy.click_timing_features(steps)
        click_gaps = timing.pop("click_gaps_sec")
        timing.pop("click_steps")
        bag_points = legacy.parse_bag_curve(source_row.get("bag_tile_change_curve"))
        surface_points = legacy.parse_surface_curve(
            source_row.get("surface_normal_tile_change_curve")
        )
        instance = structures.instance_context(source_row)
        template = structures.template_context(
            legacy.clean_text(source_row.get("level_template_id"))
        )
        parts = business_instance_parts(source_row)
        business_id = hash_id("BI", parts)
        ordered_tiles = [instance.tile_by_num[key] for key in sorted(instance.tile_by_num)]
        concrete = bool(ordered_tiles) and all(tile.tile_id > 0 for tile in ordered_tiles)
        board_id = ""
        if concrete:
            board_id = hash_id(
                "BD",
                [
                    parts[0],
                    template.features.get("template_sha256"),
                    instance.features.get("random_algorithm"),
                    [
                        [tile.tile_num, tile.tile_id, tile.x, tile.y, tile.z, tile.tower_index]
                        for tile in ordered_tiles
                    ],
                ],
                length=20,
            )
        extra = classify_extra_space(steps)
        observed = observed_path_metrics(
            steps, bag_points, surface_points, click_gaps
        )
        exact, row_hotspots = resolve_exact_capacity_metrics(
            source_row,
            steps,
            click_gaps,
            bag_points,
            surface_points,
            instance,
            template,
        )
        phase = (
            PHASE_FIRST
            if legacy.as_int(source_row.get("level_play_count"), 0) == 1
            else PHASE_RETRY
        )
        record = {
            "business_instance_id": business_id,
            "business_instance_key": (
                f"{parts[0]}|{parts[1]}|dup{parts[2]}|{parts[3]}"
            ),
            "board_instance_id": board_id,
            "board_identity_confidence": (
                "可生成指纹_仍须背包曲线验证" if board_id else "最终图案不可唯一确定"
            ),
            "attempt_phase": phase,
            **extra,
            **observed,
            **exact,
        }
        enhancements.append(record)
        for hotspot in row_hotspots:
            hotspot["business_instance_id"] = business_id
            hotspot["business_instance_key"] = record["business_instance_key"]
            hotspot_rows.append(hotspot)

    enhanced = attempts.copy()
    extra_frame = pd.DataFrame(enhancements)
    if len(enhanced) != len(extra_frame):
        raise AssertionError("源数据与特征行数不一致")
    for column in extra_frame.columns:
        enhanced[column] = extra_frame[column].to_numpy()
    return enhanced, pd.DataFrame(hotspot_rows)


def weighted_share(group: pd.DataFrame, count_col: str, total_col: str) -> float:
    counts = pd.to_numeric(group[count_col], errors="coerce")
    totals = pd.to_numeric(group[total_col], errors="coerce")
    return ratio(float(counts.sum()), float(totals.sum()))


def aggregate_player_instances(attempts: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    keys = ["user_id", "business_instance_id", "attempt_phase"]
    for key, group in attempts.groupby(keys, sort=True, dropna=False):
        sample = group.iloc[0]
        exact = group[group["exact_path_eligible"] == 1]
        row = {
            "user_id": key[0],
            "business_instance_id": key[1],
            "attempt_phase": key[2],
            "cohort_player_rank": int(sample["cohort_player_rank"]),
            "level_index": int(sample["level_index"]),
            "level_template_id": sample["level_template_id"],
            "level_dup_para_id": int(sample["level_dup_para_id"]),
            "level_dup_para": sample["level_dup_para"],
            "business_instance_key": sample["business_instance_key"],
            "attempt_count": len(group),
            "failure_count": int(group["is_fail"].sum()),
            "pass_count": int(group["is_pass"].sum()),
            "fail_rate": mean(group["is_fail"]),
            "any_fail": int(group["is_fail"].max()),
            "pass_rate": mean(group["is_pass"]),
            "duration_sec_median": median(group["level_duration_winsor_sec"]),
            "duration_per_click_median": median(
                pd.to_numeric(group["level_duration_winsor_sec"], errors="coerce")
                / pd.to_numeric(group["click_count"], errors="coerce").replace(0, np.nan)
            ),
            "manual_extra_space_any": int(group["manual_extra_space_used"].max()),
            "auto_extra_space_any": int(group["auto_extra_space_applied"].max()),
            "known_extra_space_any": int(group["known_extra_space_applied"].max()),
            "manual_prop_action_count": int(
                (
                    pd.to_numeric(group["prop_action_count"], errors="coerce")
                    - pd.to_numeric(group["extra_space_apply_count"], errors="coerce")
                    + pd.to_numeric(group["manual_extra_space_used"], errors="coerce")
                ).clip(lower=0).sum()
            ),
            "observed_decision_count": int(group["observed_decision_count"].sum()),
            "observed_surface_narrow_share": weighted_share(
                group,
                "observed_surface_narrow_count",
                "observed_decision_count",
            ),
            "observed_pressure_narrow_share": weighted_share(
                group,
                "observed_pressure_narrow_count",
                "observed_decision_count",
            ),
            "observed_surface_narrow_episode_count": int(
                group["observed_surface_narrow_episode_count"].sum()
            ),
            "observed_surface_narrow_seconds": float(
                group["observed_surface_narrow_seconds_lower_bound"].sum()
            ),
            "exact_attempt_count": len(exact),
            "exact_any_narrow": (
                int(exact["exact_any_narrow"].max()) if len(exact) else math.nan
            ),
            "exact_any_forced_loss_window": (
                int(exact["exact_any_forced_loss_window"].max())
                if len(exact)
                else math.nan
            ),
            "exact_persistent_narrow": (
                int(exact["exact_persistent_narrow"].max())
                if len(exact)
                else math.nan
            ),
            "exact_narrow_episode_count": (
                int(exact["exact_narrow_episode_count"].sum())
                if len(exact)
                else math.nan
            ),
            "exact_narrow_seconds": (
                float(exact["exact_narrow_seconds_lower_bound"].sum())
                if len(exact)
                else math.nan
            ),
            "exact_critical_state_count": (
                int(exact["exact_critical_state_count"].sum())
                if len(exact)
                else math.nan
            ),
            "exact_forced_loss_window_count": (
                int(exact["exact_forced_loss_window_count"].sum())
                if len(exact)
                else math.nan
            ),
            "exact_narrow_state_share": (
                weighted_share(
                    exact,
                    "exact_narrow_state_count",
                    "exact_decision_count",
                )
                if len(exact)
                else math.nan
            ),
        }
        rows.append(row)
    result = pd.DataFrame(rows)
    for column in [
        "duration_per_click_median",
        "observed_surface_narrow_share",
        "observed_pressure_narrow_share",
        "observed_surface_narrow_episode_count",
        "observed_surface_narrow_seconds",
    ]:
        result[f"player_relative_{column}"] = legacy.leave_one_out_robust_z(
            result, column
        )
    return result.sort_values(
        ["cohort_player_rank", "level_index", "business_instance_id"]
    ).reset_index(drop=True)


def posterior_rate(successes: float, total: int, prior_rate: float) -> float:
    if total <= 0 or not math.isfinite(prior_rate):
        return math.nan
    return (successes + GLOBAL_PRIOR_STRENGTH * prior_rate) / (
        total + GLOBAL_PRIOR_STRENGTH
    )


def rate_band(rate: float, thresholds: Sequence[float]) -> int | None:
    if not math.isfinite(rate):
        return None
    for index, threshold in enumerate(thresholds, start=1):
        if rate < threshold:
            return index
    return len(thresholds) + 1


def phase_metrics(
    group: pd.DataFrame,
    phase: str,
    priors: Mapping[str, Mapping[str, float]],
    prefix: str,
) -> dict[str, Any]:
    subset = group[group["attempt_phase"] == phase]
    exact = subset[subset["exact_attempt_count"] > 0]
    outcome_n = len(subset)
    path_n = len(exact)
    fail_success = float(subset["fail_rate"].sum()) if outcome_n else 0.0
    any_narrow_success = float(exact["exact_any_narrow"].sum()) if path_n else 0.0
    forced_success = (
        float(exact["exact_any_forced_loss_window"].sum()) if path_n else 0.0
    )
    persistent_success = (
        float(exact["exact_persistent_narrow"].sum()) if path_n else 0.0
    )
    phase_prior = priors[phase]
    fail_eb = posterior_rate(fail_success, outcome_n, phase_prior["fail"])
    narrow_eb = posterior_rate(any_narrow_success, path_n, phase_prior["narrow"])
    forced_eb = posterior_rate(forced_success, path_n, phase_prior["forced"])
    persistent_eb = posterior_rate(
        persistent_success, path_n, phase_prior["persistent"]
    )
    fail_raw = ratio(fail_success, outcome_n)
    player_any_fail_rate = mean(subset["any_fail"])
    attempt_fail_rate = ratio(
        float(subset["failure_count"].sum()),
        float(subset["attempt_count"].sum()),
    )
    attempts_per_player_p50 = median(subset["attempt_count"])
    attempts_per_player_p90 = percentile(subset["attempt_count"], 90)
    narrow_raw = ratio(any_narrow_success, path_n)
    forced_raw = ratio(forced_success, path_n)
    persistent_raw = ratio(persistent_success, path_n)
    median_episodes = median(exact["exact_narrow_episode_count"])
    p75_seconds = percentile(exact["exact_narrow_seconds"], 75)
    # 分层用实际观测率和固定业务阈值；EB仅作为稳定估计另列，避免高全局
    # 先验把“0次强制窗口”的小样本实例硬推成高终局层。
    outcome_band = rate_band(fail_raw, [0.10, 0.25, 0.45])
    terminal_band = rate_band(forced_raw, [0.05, 0.15, 0.30])
    path_band = rate_band(persistent_raw, [0.15, 0.35, 0.60])
    if path_band is not None and (
        (math.isfinite(median_episodes) and median_episodes >= 3)
        or (math.isfinite(p75_seconds) and p75_seconds >= 10)
    ):
        path_band = max(path_band, 3)

    label_num: int | None = None
    label = "样本不足"
    if outcome_n >= MIN_LABEL_SAMPLE and path_n >= MIN_LABEL_SAMPLE:
        label_num = max(outcome_band or 1, terminal_band or 1, path_band or 1)
        names = {1: "低负担", 2: "中等负担", 3: "高负担", 4: "极高负担"}
        label = f"L{label_num}_{names[label_num]}"
    elif outcome_n >= MIN_LABEL_SAMPLE:
        label = f"结果层L{outcome_band}_路径待补"

    ci_low, ci_high = mean_confidence_interval(subset["fail_rate"])
    forced_ci_low, forced_ci_high = legacy.wilson_interval(
        int(forced_success), path_n
    )
    path_ci_low, path_ci_high = legacy.wilson_interval(
        int(persistent_success), path_n
    )
    stable = "样本不足"
    if outcome_n >= MIN_LABEL_SAMPLE and path_n >= MIN_LABEL_SAMPLE:
        possible_low = max(
            rate_band(ci_low, [0.10, 0.25, 0.45]) or 1,
            rate_band(forced_ci_low, [0.05, 0.15, 0.30]) or 1,
            rate_band(path_ci_low, [0.15, 0.35, 0.60]) or 1,
        )
        possible_high = max(
            rate_band(ci_high, [0.10, 0.25, 0.45]) or 1,
            rate_band(forced_ci_high, [0.05, 0.15, 0.30]) or 1,
            rate_band(path_ci_high, [0.15, 0.35, 0.60]) or 1,
        )
        stable = "同层稳定" if possible_low == possible_high else "跨层待补样"
    return {
        f"{prefix}_player_count": outcome_n,
        f"{prefix}_exact_path_player_count": path_n,
        f"{prefix}_fail_rate_raw": fail_raw,
        f"{prefix}_fail_rate_eb": fail_eb,
        f"{prefix}_fail_rate_ci_low": ci_low,
        f"{prefix}_fail_rate_ci_high": ci_high,
        f"{prefix}_player_any_fail_rate": player_any_fail_rate,
        f"{prefix}_attempt_fail_rate": attempt_fail_rate,
        f"{prefix}_attempts_per_player_p50": attempts_per_player_p50,
        f"{prefix}_attempts_per_player_p90": attempts_per_player_p90,
        f"{prefix}_any_narrow_rate_raw": narrow_raw,
        f"{prefix}_any_narrow_rate_eb": narrow_eb,
        f"{prefix}_forced_loss_window_rate_raw": forced_raw,
        f"{prefix}_forced_loss_window_rate_eb": forced_eb,
        f"{prefix}_forced_loss_window_ci_low": forced_ci_low,
        f"{prefix}_forced_loss_window_ci_high": forced_ci_high,
        f"{prefix}_persistent_narrow_rate_raw": persistent_raw,
        f"{prefix}_persistent_narrow_rate_eb": persistent_eb,
        f"{prefix}_persistent_narrow_ci_low": path_ci_low,
        f"{prefix}_persistent_narrow_ci_high": path_ci_high,
        f"{prefix}_median_narrow_episode_count": median_episodes,
        f"{prefix}_p75_narrow_seconds": p75_seconds,
        f"{prefix}_outcome_band": outcome_band,
        f"{prefix}_terminal_band": terminal_band,
        f"{prefix}_path_band": path_band,
        f"{prefix}_experience_band_num": label_num,
        f"{prefix}_experience_band": label,
        f"{prefix}_band_stability": stable,
    }


def build_priors(player_instances: pd.DataFrame) -> dict[str, dict[str, float]]:
    priors: dict[str, dict[str, float]] = {}
    for phase in (PHASE_FIRST, PHASE_RETRY):
        subset = player_instances[player_instances["attempt_phase"] == phase]
        exact = subset[subset["exact_attempt_count"] > 0]
        priors[phase] = {
            "fail": mean(subset["fail_rate"]),
            "narrow": mean(exact["exact_any_narrow"]),
            "forced": mean(exact["exact_any_forced_loss_window"]),
            "persistent": mean(exact["exact_persistent_narrow"]),
        }
    return priors


def reliability_label(count: int) -> str:
    if count >= 20:
        return "A_>=20人"
    if count >= 10:
        return "B_10-19人"
    if count >= 5:
        return "C_5-9人"
    return "D_<5人"


def aggregate_instances(
    attempts: pd.DataFrame, player_instances: pd.DataFrame
) -> tuple[pd.DataFrame, dict[str, dict[str, float]]]:
    priors = build_priors(player_instances)
    rows: list[dict[str, Any]] = []
    for instance_id, group in player_instances.groupby(
        "business_instance_id", sort=True
    ):
        attempt_group = attempts[attempts["business_instance_id"] == instance_id]
        sample = attempt_group.iloc[0]
        level_ids = sorted(
            value for value in attempt_group["level_id"].dropna().unique() if value
        )
        versions = sorted(
            value
            for value in attempt_group["client_version"].dropna().unique()
            if value
        )
        user_groups = sorted(
            value
            for value in attempt_group["user_group_tag"].dropna().unique()
            if value
        )
        fingerprints = sorted(
            value
            for value in attempt_group["board_instance_id"].dropna().unique()
            if value
        )
        first = phase_metrics(group, PHASE_FIRST, priors, "first")
        retry = phase_metrics(group, PHASE_RETRY, priors, "retry")
        if first["first_experience_band_num"] is not None:
            primary_phase = PHASE_FIRST
            primary_band = first["first_experience_band"]
            primary_num = first["first_experience_band_num"]
            primary_path_n = first["first_exact_path_player_count"]
        elif retry["retry_experience_band_num"] is not None:
            primary_phase = PHASE_RETRY
            primary_band = retry["retry_experience_band"]
            primary_num = retry["retry_experience_band_num"]
            primary_path_n = retry["retry_exact_path_player_count"]
        elif first["first_player_count"] >= MIN_LABEL_SAMPLE:
            primary_phase = PHASE_FIRST
            primary_band = first["first_experience_band"]
            primary_num = first["first_experience_band_num"]
            primary_path_n = first["first_exact_path_player_count"]
        elif retry["retry_player_count"] >= MIN_LABEL_SAMPLE:
            primary_phase = PHASE_RETRY
            primary_band = retry["retry_experience_band"]
            primary_num = retry["retry_experience_band_num"]
            primary_path_n = retry["retry_exact_path_player_count"]
        else:
            primary_phase = "样本不足"
            primary_band = "样本不足"
            primary_num = math.nan
            primary_path_n = 0
        tags = []
        prefix = "first" if primary_phase == PHASE_FIRST else "retry"
        if primary_phase in {PHASE_FIRST, PHASE_RETRY}:
            if (first if prefix == "first" else retry)[f"{prefix}_path_band"] in {3, 4}:
                tags.append("持续窄路")
            if (first if prefix == "first" else retry)[f"{prefix}_terminal_band"] in {3, 4}:
                tags.append("终局悬崖")
            chosen = first if prefix == "first" else retry
            if (
                chosen[f"{prefix}_path_band"] in {3, 4}
                and chosen[f"{prefix}_outcome_band"] in {1, 2}
            ):
                tags.append("可恢复窄路")
        if mean(attempt_group["auto_extra_space_applied"]) >= 0.25:
            tags.append("自动背包+1覆盖高")
        if mean(attempt_group["manual_extra_space_used"]) >= 0.15:
            tags.append("手动背包+1集中")
        row = {
            "business_instance_id": instance_id,
            "business_instance_key": sample["business_instance_key"],
            "level_index": int(sample["level_index"]),
            "level_template_id": sample["level_template_id"],
            "level_dup_para_id": int(sample["level_dup_para_id"]),
            "level_dup_para": sample["level_dup_para"],
            "level_id_count": len(level_ids),
            "level_id_values": "|".join(level_ids),
            "client_version_values": "|".join(versions),
            "user_group_values": "|".join(user_groups),
            "board_fingerprint_count": len(fingerprints),
            "board_instance_ids": "|".join(fingerprints),
            "board_identity_status": (
                "Exact候选_需逐局曲线验证"
                if len(fingerprints) == 1
                else "同配置存在多指纹"
                if len(fingerprints) > 1
                else "最终图案不可唯一确定"
            ),
            "attempt_count": len(attempt_group),
            "player_count": int(attempt_group["user_id"].nunique()),
            "exact_attempt_count": int(attempt_group["exact_path_eligible"].sum()),
            "pattern_mapping_verified_rate": mean(
                attempt_group["pattern_mapping_verified"]
            ),
            "manual_extra_space_attempt_rate": mean(
                attempt_group["manual_extra_space_used"]
            ),
            "auto_extra_space_attempt_rate": mean(
                attempt_group["auto_extra_space_applied"]
            ),
            "known_extra_space_attempt_rate": mean(
                attempt_group["known_extra_space_applied"]
            ),
            "observed_surface_narrow_share": weighted_share(
                attempt_group,
                "observed_surface_narrow_count",
                "observed_decision_count",
            ),
            "observed_pressure_narrow_share": weighted_share(
                attempt_group,
                "observed_pressure_narrow_count",
                "observed_decision_count",
            ),
            "primary_phase": primary_phase,
            "primary_experience_band_num": primary_num,
            "primary_experience_band": primary_band,
            "primary_path_reliability": reliability_label(int(primary_path_n)),
            "behavior_tags": "+".join(tags) if tags else "常规/待补样",
            **first,
            **retry,
        }
        rows.append(row)
    result = pd.DataFrame(rows).sort_values(
        ["level_index", "level_template_id", "level_dup_para_id", "level_dup_para"]
    )
    return result.reset_index(drop=True), priors


def aggregate_level_rollup(
    attempts: pd.DataFrame, instances: pd.DataFrame
) -> pd.DataFrame:
    rows = []
    for level_index, group in attempts.groupby("level_index", sort=True):
        inst = instances[instances["level_index"] == level_index]
        first = group[group["attempt_phase"] == PHASE_FIRST]
        retry = group[group["attempt_phase"] == PHASE_RETRY]
        exact_first = first[first["exact_path_eligible"] == 1]
        rows.append(
            {
                "level_index": int(level_index),
                "business_instance_count": int(group["business_instance_id"].nunique()),
                "template_count": int(group["level_template_id"].nunique()),
                "player_count": int(group["user_id"].nunique()),
                "attempt_count": len(group),
                "first_attempt_count": len(first),
                "retry_attempt_count": len(retry),
                "first_fail_rate": mean(first["is_fail"]),
                "retry_fail_rate": mean(retry["is_fail"]),
                "first_exact_path_attempt_count": len(exact_first),
                "first_any_narrow_rate": mean(exact_first["exact_any_narrow"]),
                "first_forced_loss_window_rate": mean(
                    exact_first["exact_any_forced_loss_window"]
                ),
                "first_persistent_narrow_rate": mean(
                    exact_first["exact_persistent_narrow"]
                ),
                "manual_extra_space_attempt_rate": mean(
                    group["manual_extra_space_used"]
                ),
                "auto_extra_space_attempt_rate": mean(
                    group["auto_extra_space_applied"]
                ),
                "labeled_instance_count": int(
                    inst["primary_experience_band_num"].notna().sum()
                ),
                "l4_instance_count": int(
                    (inst["primary_experience_band_num"] == 4).sum()
                ),
                "l3_instance_count": int(
                    (inst["primary_experience_band_num"] == 3).sum()
                ),
                "instance_band_mode": (
                    inst["primary_experience_band"].mode().iloc[0]
                    if not inst["primary_experience_band"].mode().empty
                    else ""
                ),
            }
        )
    return pd.DataFrame(rows)


def aggregate_hotspots(hotspots: pd.DataFrame) -> pd.DataFrame:
    if hotspots.empty:
        return hotspots
    keys = [
        "business_instance_id",
        "business_instance_key",
        "level_index",
        "level_template_id",
        "level_dup_para_id",
        "level_dup_para",
        "selected_tile_num",
        "selected_x",
        "selected_y",
        "selected_z",
    ]
    rows = []
    for key, group in hotspots.groupby(keys, sort=True, dropna=False):
        row = {column: value for column, value in zip(keys, key)}
        row.update(
            {
                "player_count": int(group["user_id"].nunique()),
                "state_count": len(group),
                "first_state_count": int((group["attempt_phase"] == PHASE_FIRST).sum()),
                "geometric_narrow_state_count": int(
                    group["is_geometric_narrow"].sum()
                ),
                "pressure_narrow_state_count": int(
                    group["is_pressure_narrow"].sum()
                ),
                "forced_loss_window_count": int(group["is_forced_loss_window"].sum()),
                "critical_state_count": int(group["is_critical_state"].sum()),
                "median_click_no": median(group["click_no"]),
                "median_click_gap_sec": median(group["click_gap_sec"]),
                "median_bag_size_before": median(group["bag_size_before"]),
                "median_legal_click_width": median(group["legal_click_width"]),
                "median_surviving_first_action_width": median(
                    group["surviving_first_action_width"]
                ),
            }
        )
        rows.append(row)
    return pd.DataFrame(rows).sort_values(
        ["forced_loss_window_count", "state_count", "player_count"],
        ascending=False,
    ).reset_index(drop=True)


def metric_dictionary() -> pd.DataFrame:
    rows = [
        ("business_instance_id", "实例", "用户指定四键的稳定哈希；不含level_id和用户分组。"),
        ("pattern_mapping_verified", "质量", "花色、几何、背包和表面积轨迹逐步一致才为1。"),
        ("legal_click_width", "路径", "玩家实际到达的每个非收尾局面中，当前合法可点击Tile数。"),
        ("surviving_first_action_width", "路径", "高压时当前容量窗口内能在满包前形成一次三消的首步Tile数。"),
        ("exact_any_narrow", "路径", "实际路径上出现几何可点<=3或高压可存活首步<=2的非收尾局面。"),
        ("exact_any_forced_loss_window", "终局", "当前容量下可存活首步=0；是无干预强制失败窗口，不等同全局无解。"),
        ("exact_persistent_narrow", "路径", "单局组合瓶颈episode>=3或瓶颈停留>=10秒。"),
        ("first_fail_rate_raw", "结果", "先算每位玩家在该实例/阶段的失败比例，再对玩家等权平均。"),
        ("first_attempt_fail_rate", "结果", "该实例首局阶段按对局计的失败率；与玩家等权口径并列。"),
        ("first_fail_rate_eb", "结果", "同phase全局先验、强度5的Beta式收缩均值；不向level_index父级收缩。"),
        ("primary_experience_band", "分层", "固定业务阈值，不按分位数配额；路径样本不足会明确待补。"),
        ("auto_extra_space_attempt_rate", "辅助上下文", "复盘记录708/533；708当前代码为自动应用，不算玩家主动道具选择。"),
        ("manual_extra_space_attempt_rate", "辅助上下文", "复盘记录508的对局比例。"),
        ("user_group_tag", "上下文", "用户分组，只作投放/DDA路由上下文，不作为实例身份或难度因子。"),
    ]
    return pd.DataFrame(rows, columns=["field", "dimension", "definition"])


def distribution_summary(instances: pd.DataFrame) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for phase in ("first", "retry"):
        label_col = f"{phase}_experience_band"
        numeric = instances[f"{phase}_experience_band_num"]
        result[phase] = {
            "band_counts": {
                str(key): int(value)
                for key, value in instances[label_col].value_counts().items()
            },
            "labeled_count": int(numeric.notna().sum()),
            "fail_rate_quantiles": {
                str(q): percentile(instances[f"{phase}_fail_rate_raw"], q)
                for q in (0, 10, 25, 50, 75, 90, 100)
            },
            "persistent_narrow_rate_quantiles": {
                str(q): percentile(
                    instances[f"{phase}_persistent_narrow_rate_raw"], q
                )
                for q in (0, 10, 25, 50, 75, 90, 100)
            },
            "forced_window_rate_quantiles": {
                str(q): percentile(
                    instances[f"{phase}_forced_loss_window_rate_raw"], q
                )
                for q in (0, 10, 25, 50, 75, 90, 100)
            },
        }
    return result


def build_quality(
    input_path: Path,
    source: pd.DataFrame,
    attempts: pd.DataFrame,
    player_instances: pd.DataFrame,
    instances: pd.DataFrame,
    levels: pd.DataFrame,
    hotspots: pd.DataFrame,
    templates: pd.DataFrame,
    structures: legacy.StructureFeatureCatalog,
    template_dir: Path,
    config_dir: Path,
    priors: Mapping[str, Mapping[str, float]],
) -> dict[str, Any]:
    template_paths = [
        template_dir / f"{template_id}_m.bytes"
        for template_id in sorted(source["level_template_id"].unique())
    ]
    exact = attempts[attempts["exact_path_eligible"] == 1]
    forced = exact[exact["exact_any_forced_loss_window"] == 1]
    no_forced = exact[exact["exact_any_forced_loss_window"] == 0]
    return {
        "title": "100–199关实际实例行为分层 v2",
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
            "excluded_non_outcome_attempts": int(
                source.attrs.get("excluded_non_outcome_attempts", 0)
            ),
            "excluded_status_counts": source.attrs.get(
                "excluded_status_counts", {}
            ),
            "attempts": len(attempts),
            "players": int(source["user_id"].nunique()),
            "level_indices": int(source["level_index"].nunique()),
            "business_instances": len(instances),
            "player_instances_by_phase": len(player_instances),
            "first_attempts": int((attempts["attempt_phase"] == PHASE_FIRST).sum()),
            "retry_attempts": int((attempts["attempt_phase"] == PHASE_RETRY).sum()),
            "exact_path_attempts": len(exact),
            "exact_first_attempts": int((exact["attempt_phase"] == PHASE_FIRST).sum()),
            "exact_retry_attempts": int((exact["attempt_phase"] == PHASE_RETRY).sum()),
            "forced_window_attempts": len(forced),
            "hotspot_rows": len(hotspots),
            "templates": len(templates),
            "first_labeled_instances": int(
                instances["first_experience_band_num"].notna().sum()
            ),
            "retry_labeled_instances": int(
                instances["retry_experience_band_num"].notna().sum()
            ),
        },
        "validation": {
            "forced_window_fail_rate": mean(forced["is_fail"]),
            "no_forced_window_fail_rate": mean(no_forced["is_fail"]),
            "exact_any_narrow_rate": mean(exact["exact_any_narrow"]),
            "exact_forced_window_rate": mean(
                exact["exact_any_forced_loss_window"]
            ),
            "exact_persistent_narrow_rate": mean(
                exact["exact_persistent_narrow"]
            ),
        },
        "capacity_resolution_counts": {
            str(key): int(value)
            for key, value in attempts["capacity_resolution"].value_counts().items()
        },
        "priors": priors,
        "thresholds": {
            "outcome_fail_rate": [0.10, 0.25, 0.45],
            "forced_loss_window_rate": [0.05, 0.15, 0.30],
            "persistent_narrow_rate": [0.15, 0.35, 0.60],
            "path_raise_rule": "窄路episode中位>=3或窄路时长P75>=10秒，路径层至少L3",
            "minimum_label_sample": MIN_LABEL_SAMPLE,
            "allocation": "固定阈值，无层级名额；允许99关同层、1关单独一层",
        },
        "distribution": distribution_summary(instances),
        "method": {
            "primary_unit": "level_index+template_id+dup_para_id+DupPara",
            "level_id_role": "血缘/规则版本校验，不进入业务实例键",
            "user_group_role": "用户分组与DDA路由上下文，不进入实例键或难度分",
            "difficulty_inputs": "玩家实际到达局面的几何宽度+高压短窗口可存活首步+玩家等权结果；模板静态指标不直接打分",
            "outcome_weighting": "先算玩家在实例/阶段内的失败比例，再对玩家等权；另列对局失败率、曾失败玩家率和尝试次数",
            "extra_space": "508记玩家局内使用；708/533记自动上下文并动态调整容量；活动533仍可能缺埋点",
            "phase": "首局与非首局分别估计；非首局不并回首局，也不丢弃",
            "eb_prior": f"同phase全局行为先验，强度{GLOBAL_PRIOR_STRENGTH:g}",
        },
        "limitations": [
            "只有无塔、无洗牌/移动类道具且花色、背包、表面积逐步一致的局，才能输出逐步路径宽度。",
            "当前CSV缺实际下发配置快照、运行时ReplaceTiles、C–N具体分配和塔/洗牌交换结果。",
            "当前版本覆盖玩家实际经过的几何瓶颈，并在高压状态搜索至下一次三消；尚不是整局剩余状态的全局解空间。",
            "无干预强制失败窗口只表示当前容量窗口内无法先三消，不等同整局全局无解。",
            "708在当前代码会自动应用；现有埋点不能精确区分进关、复活或IAP后的自动触发。",
            "活动533被动容量若未写replay，当前SQL无法还原，需新增容量/apply_mode埋点。",
            "样本是达到300关以上的Android高进度玩家面板，外推新手/付费群体需另取样。",
        ],
    }


def build_manifest(
    staging_dir: Path,
    output_dir: Path,
    quality: Mapping[str, Any],
    instances: pd.DataFrame,
    levels: pd.DataFrame,
    player_instances: pd.DataFrame,
    hotspots: pd.DataFrame,
    templates: pd.DataFrame,
) -> Path:
    staging_dir.mkdir(parents=True, exist_ok=True)
    frames = {
        "instances": instances,
        "levels": levels,
        "player_instances": player_instances,
        "hotspots": hotspots,
        "templates": templates,
        "dictionary": metric_dictionary(),
    }
    sheets = {}
    for name, frame in frames.items():
        path = staging_dir / f"{name}.ndjson"
        legacy.write_ndjson(path, frame, list(frame.columns))
        sheets[name] = {
            "path": str(path),
            "columns": list(frame.columns),
            "row_count": len(frame),
        }
    labeled = instances[instances["primary_experience_band_num"].notna()]
    priority = labeled.sort_values(
        [
            "primary_experience_band_num",
            "first_forced_loss_window_rate_eb",
            "first_persistent_narrow_rate_eb",
            "first_fail_rate_eb",
        ],
        ascending=False,
    ).head(15)
    priority_records = [
        {key: legacy.json_safe(value) for key, value in record.items()}
        for record in priority.to_dict("records")
    ]
    manifest = {
        "title": quality["title"],
        "quality": quality,
        "sheets": sheets,
        "priority_instances": priority_records,
        "output_workbook": str(output_dir / WORKBOOK_FILE),
    }
    path = staging_dir / "manifest_v2.json"
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return path


def write_report(
    path: Path, quality: Mapping[str, Any], instances: pd.DataFrame
) -> None:
    counts = quality["counts"]
    validation = quality["validation"]
    first_bands = quality["distribution"]["first"]["band_counts"]
    lines = [
        "# 关卡实例行为分层 v2",
        "",
        "## 结论",
        "",
        "- 主分析单位已改为用户确认的四键业务实例；level_id只作血缘校验。",
        "- 难度不再由模板静态特征或固定四分位决定，而由玩家实际经过的几何瓶颈、高压短窗口、强制失败窗口和结果共同描述。",
        "- 首局与非首局分别估计；708自动背包+1不再计为玩家主动道具使用，且会把当局容量按8格推进。",
        f"- 共{counts['business_instances']:,}个业务实例；可逐步验证路径的对局{counts['exact_path_attempts']:,}局。",
        f"- 已排除{counts['excluded_non_outcome_attempts']:,}局退出/未知状态，不把它们当成非失败。",
        f"- 精确路径样本中，出现强制失败窗口的失败率为{validation['forced_window_fail_rate']:.1%}，未出现时为{validation['no_forced_window_fail_rate']:.1%}。",
        "",
        "## 首局分层分布（固定阈值，不限名额）",
        "",
    ]
    for label, count in first_bands.items():
        lines.append(f"- {label}: {count}个实例")
    lines.extend(
        [
            "",
            "## 读取原则",
            "",
            "1. 优先看实例层，不把level_index汇总当作唯一的关。",
            "2. `结果层Lx_路径待补`表示结果样本够，但动态花色/塔/道具状态不足以验证路径，不能冒充完整难度层。",
            "3. 逐步路径覆盖实际访问状态；高压宽度搜索到下一次三消。`无干预强制失败窗口`不是全局无解，完整无解仍需全局求解器证明。",
            "4. 模板结构表只解释玩家为何遇到某种局面，不进入主分层打分。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def save_outputs(
    output_dir: Path,
    attempts: pd.DataFrame,
    player_instances: pd.DataFrame,
    instances: pd.DataFrame,
    levels: pd.DataFrame,
    hotspots: pd.DataFrame,
    templates: pd.DataFrame,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    attempts.to_csv(
        output_dir / ATTEMPT_FILE,
        index=False,
        encoding="utf-8-sig",
        compression="gzip",
    )
    player_instances.to_csv(
        output_dir / PLAYER_INSTANCE_FILE, index=False, encoding="utf-8-sig"
    )
    instances.to_csv(output_dir / INSTANCE_FILE, index=False, encoding="utf-8-sig")
    levels.to_csv(output_dir / LEVEL_FILE, index=False, encoding="utf-8-sig")
    hotspots.to_csv(
        output_dir / HOTSPOT_FILE,
        index=False,
        encoding="utf-8-sig",
        compression="gzip",
    )
    templates.to_csv(output_dir / TEMPLATE_FILE, index=False, encoding="utf-8-sig")


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
    valid_outcome = source["level_status"].map(is_normal_outcome_status)
    excluded_status_counts = {
        str(key or "空值"): int(value)
        for key, value in normalized_status[~valid_outcome].value_counts().items()
    }
    excluded_non_outcome_attempts = int((~valid_outcome).sum())
    source = source.loc[valid_outcome].copy()
    source.attrs["excluded_non_outcome_attempts"] = excluded_non_outcome_attempts
    source.attrs["excluded_status_counts"] = excluded_status_counts
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
    print("[1/6] 解析回放、配置与模板", flush=True)
    attempts, _legacy_hotspots = legacy.extract_attempts(source, structures)
    print("[2/6] 沿玩家真实路径计算动态瓶颈", flush=True)
    attempts, raw_hotspots = enhance_attempts(source, attempts, structures)
    print("[3/6] 聚合玩家×实例×首局/非首局", flush=True)
    player_instances = aggregate_player_instances(attempts)
    instances, priors = aggregate_instances(attempts, player_instances)
    levels = aggregate_level_rollup(attempts, instances)
    hotspots = aggregate_hotspots(raw_hotspots)
    templates = legacy.build_template_frame(structures)
    print("[4/6] 写出实例层结果", flush=True)
    save_outputs(
        output_dir,
        attempts,
        player_instances,
        instances,
        levels,
        hotspots,
        templates,
    )
    quality = build_quality(
        input_path,
        source,
        attempts,
        player_instances,
        instances,
        levels,
        hotspots,
        templates,
        structures,
        template_dir,
        config_dir,
        priors,
    )
    (output_dir / QUALITY_FILE).write_text(
        json.dumps(quality, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_report(output_dir / REPORT_FILE, quality, instances)
    print("[5/6] 生成工作簿清单", flush=True)
    manifest = build_manifest(
        staging_dir,
        output_dir,
        quality,
        instances,
        levels,
        player_instances,
        hotspots,
        templates,
    )
    print("[6/6] v2分析数据准备完成", flush=True)
    return {
        "manifest": str(manifest),
        "output_dir": str(output_dir),
        "workbook": str(output_dir / WORKBOOK_FILE),
        "counts": quality["counts"],
        "validation": quality["validation"],
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
        "--level-tile-git-revision",
        default=legacy.DEFAULT_LEVEL_TILE_GIT_REVISION,
    )
    result.add_argument(
        "--config-level-git-revision",
        default=legacy.DEFAULT_CONFIG_LEVEL_GIT_REVISION,
    )
    return result


def main() -> int:
    output = run(parser().parse_args())
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
