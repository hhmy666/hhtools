#!/usr/bin/env python3
"""基于固定玩家面板生成关卡行为指标、分层与结构解释。

分析先将所有局合并成“玩家 × level_index”单元，再对关卡等权聚合，
避免失败重试多的关卡被重复加权。行为差异使用玩家内留一关中位数/MAD
做校正。第一版输出难度四层和多维标签，不把未经人工标注的结果命名为
“好/坏体验”。
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

import numpy as np
import pandas as pd


sys.dont_write_bytecode = True

MODULE_DIR = Path(__file__).resolve().parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from restore_player_replay import (  # noqa: E402
    DEFAULT_CONFIG_DIR,
    DEFAULT_TEMPLATE_DIR,
    DEFAULT_TEMPLATE_READER_DIR,
    EXTRA_SPACE_ACTION_IDS,
    INTERVAL_CAP_MS,
    RANDOM_ID_A,
    RANDOM_ID_B,
    ConfigCatalog,
    ReplayRestorer,
    parse_int,
    parse_replay,
)


PASS_STATUSES = {"pass", "win", "success"}
FAIL_STATUSES = {"fail", "failed", "lose", "loss", "bag_full_fail"}
CN_TILE_MIN = -23
CN_TILE_MAX = -12
PRESSURE_THRESHOLD = 5
DANGER_THRESHOLD = 6
FULL_THRESHOLD = 7
DURATION_WINSOR_SEC = 1_800.0
TAG_THRESHOLD = 75.0
INSTANCE_PRIOR_N = 10.0
DEFAULT_LEVEL_TILE_GIT_REVISION = "5022d099293ea280ee709b68d875817cc43ddebc"
DEFAULT_CONFIG_LEVEL_GIT_REVISION = "4eac5d2105d316e3b0666702f4ccd916ea925281"

ATTEMPT_FILE = "01_对局特征.csv.gz"
PLAYER_LEVEL_FILE = "02_玩家关卡特征.csv"
LEVEL_FILE = "03_关卡分层结果.csv"
INSTANCE_FILE = "04_配置实例画像.csv"
TEMPLATE_FILE = "05_模板结构特征.csv"
HOTSPOT_FILE = "06_位置行为热点.csv.gz"
QUALITY_FILE = "分析质量报告.json"
REPORT_FILE = "初版结论.md"


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def as_int(value: Any, default: int = 0) -> int:
    parsed = parse_int(value)
    return default if parsed is None else int(parsed)


def as_float(value: Any, default: float = math.nan) -> float:
    text = clean_text(value)
    if not text:
        return default
    try:
        result = float(text)
    except ValueError:
        return default
    return result if math.isfinite(result) else default


def safe_ratio(numerator: float, denominator: float) -> float:
    if not denominator:
        return math.nan
    return float(numerator) / float(denominator)


def finite_values(values: Iterable[Any]) -> np.ndarray:
    numeric = pd.to_numeric(pd.Series(list(values)), errors="coerce").to_numpy(
        dtype=float
    )
    return numeric[np.isfinite(numeric)]


def nanmedian(values: Iterable[Any]) -> float:
    data = finite_values(values)
    return float(np.median(data)) if len(data) else math.nan


def nanpercentile(values: Iterable[Any], percentile: float) -> float:
    data = finite_values(values)
    return float(np.percentile(data, percentile)) if len(data) else math.nan


def nanmean(values: Iterable[Any]) -> float:
    data = finite_values(values)
    return float(np.mean(data)) if len(data) else math.nan


def longest_true_run(values: Sequence[bool]) -> int:
    best = 0
    current = 0
    for value in values:
        if value:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def threshold_onsets(values: Sequence[int], threshold: int) -> list[int]:
    result = []
    previous = -math.inf
    for index, value in enumerate(values):
        if value >= threshold and previous < threshold:
            result.append(index)
        previous = value
    return result


def pressure_recoveries(values: Sequence[int]) -> int:
    """从高压（>=5）回落到安全区（<=3）的次数。"""

    armed = False
    recoveries = 0
    for value in values:
        if value >= PRESSURE_THRESHOLD:
            armed = True
        elif armed and value <= 3:
            recoveries += 1
            armed = False
    return recoveries


def parse_bag_curve(value: Any) -> list[int]:
    text = clean_text(value)
    if not text:
        return []
    if re.fullmatch(r"\d+", text):
        return [int(character) for character in text]
    return [int(item) for item in re.findall(r"\d+", text)]


def parse_surface_curve(value: Any) -> list[int]:
    text = clean_text(value)
    if not text:
        return []
    if "-" in text or "|" in text or "," in text:
        return [int(item) for item in re.findall(r"\d+", text)]
    return [int(character) for character in text]


def shannon_entropy(values: Sequence[int]) -> float:
    if not values:
        return math.nan
    counts = Counter(values)
    total = sum(counts.values())
    return float(
        -sum((count / total) * math.log2(count / total) for count in counts.values())
    )


def robust_scale(values: pd.Series) -> pd.Series:
    """对一列做稳健 z-score；MAD 为 0 时依次回退 IQR 和标准差。"""

    numeric = pd.to_numeric(values, errors="coerce").astype(float)
    valid = numeric[np.isfinite(numeric)]
    result = pd.Series(np.nan, index=values.index, dtype=float)
    if valid.empty:
        return result
    center = float(valid.median())
    mad = float((valid - center).abs().median())
    scale = 1.4826 * mad
    if not math.isfinite(scale) or scale <= 1e-12:
        q25, q75 = valid.quantile([0.25, 0.75])
        scale = float(q75 - q25) / 1.349
    if not math.isfinite(scale) or scale <= 1e-12:
        scale = float(valid.std(ddof=0))
    if not math.isfinite(scale) or scale <= 1e-12:
        result.loc[valid.index] = 0.0
        return result
    result.loc[valid.index] = ((valid - center) / scale).clip(-5, 5)
    return result


def percentile_score(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce").astype(float)
    return numeric.rank(method="average", pct=True, na_option="keep") * 100.0


def kmeans_three(
    values: np.ndarray,
    *,
    seed: int = 20_260_916,
    starts: int = 80,
    max_iterations: int = 200,
) -> tuple[np.ndarray, np.ndarray, float]:
    """小样本可复现 K=3。返回簇、中心和平均 silhouette。"""

    matrix = np.asarray(values, dtype=float)
    if matrix.ndim != 2 or len(matrix) < 3 or not np.isfinite(matrix).all():
        raise ValueError("K=3 需要至少3行且不含缺失值")
    rng = np.random.default_rng(seed)
    best_labels: Optional[np.ndarray] = None
    best_centers: Optional[np.ndarray] = None
    best_inertia = math.inf
    for _ in range(starts):
        centers = matrix[rng.choice(len(matrix), size=3, replace=False)].copy()
        labels = np.zeros(len(matrix), dtype=int)
        for _ in range(max_iterations):
            distances = ((matrix[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
            next_labels = distances.argmin(axis=1)
            if np.array_equal(labels, next_labels):
                labels = next_labels
                break
            labels = next_labels
            next_centers = []
            valid = True
            for cluster in range(3):
                members = matrix[labels == cluster]
                if len(members) == 0:
                    valid = False
                    break
                next_centers.append(members.mean(axis=0))
            if not valid:
                break
            centers = np.vstack(next_centers)
        if any(np.sum(labels == cluster) == 0 for cluster in range(3)):
            continue
        centers = np.vstack([matrix[labels == cluster].mean(axis=0) for cluster in range(3)])
        inertia = float(((matrix - centers[labels]) ** 2).sum())
        if inertia < best_inertia:
            best_inertia = inertia
            best_labels = labels.copy()
            best_centers = centers.copy()
    if best_labels is None or best_centers is None:
        raise ValueError("K=3 未找到有效划分")

    pairwise = np.sqrt(
        ((matrix[:, None, :] - matrix[None, :, :]) ** 2).sum(axis=2)
    )
    silhouettes = []
    for index, cluster in enumerate(best_labels):
        same = np.where(best_labels == cluster)[0]
        same = same[same != index]
        a = float(pairwise[index, same].mean()) if len(same) else 0.0
        b = min(
            float(pairwise[index, best_labels == other].mean())
            for other in range(3)
            if other != cluster
        )
        denominator = max(a, b)
        silhouettes.append((b - a) / denominator if denominator else 0.0)
    return best_labels, best_centers, float(np.mean(silhouettes))


def leave_one_out_robust_z(
    frame: pd.DataFrame, value_column: str, group_column: str = "user_id"
) -> pd.Series:
    """用同一玩家除当前关外的关卡建立中位数/MAD基线。"""

    values = pd.to_numeric(frame[value_column], errors="coerce").to_numpy(float)
    result = np.full(len(frame), np.nan, dtype=float)
    positions = pd.Series(np.arange(len(frame)), index=frame.index)
    for _, index_labels in frame.groupby(group_column, sort=False).groups.items():
        locs = positions.loc[list(index_labels)].to_numpy(dtype=int)
        group_values = values[locs]
        for local_index, global_index in enumerate(locs):
            current = group_values[local_index]
            if not math.isfinite(current):
                continue
            base = np.delete(group_values, local_index)
            base = base[np.isfinite(base)]
            if len(base) < 5:
                continue
            center = float(np.median(base))
            mad = float(np.median(np.abs(base - center)))
            scale = 1.4826 * mad
            if not math.isfinite(scale) or scale <= 1e-12:
                q25, q75 = np.percentile(base, [25, 75])
                scale = float(q75 - q25) / 1.349
            if not math.isfinite(scale) or scale <= 1e-12:
                scale = float(np.std(base))
            if not math.isfinite(scale) or scale <= 1e-12:
                result[global_index] = 0.0
            else:
                result[global_index] = float(
                    np.clip((current - center) / scale, -5.0, 5.0)
                )
    return pd.Series(result, index=frame.index, dtype=float)


def leave_one_out_centered(
    frame: pd.DataFrame, value_column: str, group_column: str = "user_id"
) -> pd.Series:
    values = pd.to_numeric(frame[value_column], errors="coerce").astype(float)
    grouped_sum = values.groupby(frame[group_column]).transform("sum")
    grouped_count = values.notna().groupby(frame[group_column]).transform("sum")
    other_count = grouped_count - values.notna().astype(int)
    other_sum = grouped_sum - values.fillna(0.0)
    baseline = other_sum / other_count.replace(0, np.nan)
    return values - baseline


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total <= 0:
        return math.nan, math.nan
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(
            proportion * (1 - proportion) / total
            + z * z / (4 * total * total)
        )
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def combined_sha256(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(file_sha256(path)))
    return digest.hexdigest()


def json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if not math.isfinite(float(value)) else float(value)
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if pd.isna(value):
        return None
    return value


def write_ndjson(path: Path, frame: pd.DataFrame, columns: Sequence[str]) -> None:
    with path.open("w", encoding="utf-8") as target:
        for values in frame.loc[:, list(columns)].itertuples(index=False, name=None):
            row = [json_safe(value) for value in values]
            target.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            target.write("\n")


@dataclass
class TemplateContext:
    level: Any
    graph: Any
    static_graph: Any
    features: dict[str, Any]


@dataclass
class InstanceContext:
    features: dict[str, Any]
    tile_by_num: dict[int, Any]


class StructureFeatureCatalog:
    def __init__(
        self,
        config_dir: Path,
        template_dir: Path,
        template_reader_dir: Path,
        level_tile_git_revision: str = "",
        config_level_git_revision: str = "",
    ) -> None:
        self.config = ConfigCatalog(config_dir)
        self.config_level_source = "current_worktree"
        self.config_level_sha256 = file_sha256(config_dir / "config_level.csv")
        self.config_level_git_revision = clean_text(config_level_git_revision)
        if self.config_level_git_revision:
            config_repo = config_dir.parent
            process = subprocess.run(
                [
                    "git",
                    "-C",
                    str(config_repo),
                    "show",
                    f"{self.config_level_git_revision}:csv/config_level.csv",
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            snapshot_text = process.stdout
            rows = list(csv.DictReader(io.StringIO(snapshot_text)))
            self.config.level_rows = [
                row
                for row in rows
                if row.get("LevelId") not in {"", "string", "LevelId"}
                and row.get("TemplateId") not in {"", "string", "TemplateId"}
            ]
            self.config.level_by_id = {
                row["LevelId"]: row for row in self.config.level_rows
            }
            self.config.levels_by_template = defaultdict(list)
            for row in self.config.level_rows:
                self.config.levels_by_template[row["TemplateId"]].append(row)
            self.config_level_source = f"git:{self.config_level_git_revision}"
            self.config_level_sha256 = hashlib.sha256(
                snapshot_text.encode("utf-8")
            ).hexdigest()
        self.level_tile_source = "current_worktree"
        self.level_tile_sha256 = file_sha256(
            config_dir / "config_client_level_tile.csv"
        )
        self.level_tile_git_revision = clean_text(level_tile_git_revision)
        if self.level_tile_git_revision:
            config_repo = config_dir.parent
            process = subprocess.run(
                [
                    "git",
                    "-C",
                    str(config_repo),
                    "show",
                    f"{self.level_tile_git_revision}:csv/config_client_level_tile.csv",
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            snapshot_text = process.stdout
            rows = list(csv.DictReader(io.StringIO(snapshot_text)))
            self.config.level_tile_rows = [
                row
                for row in rows
                if parse_int(row.get("StartLevel")) is not None
            ]
            self.config.level_tile_rows.sort(key=lambda row: int(row["StartLevel"]))
            self.level_tile_source = f"git:{self.level_tile_git_revision}"
            self.level_tile_sha256 = hashlib.sha256(
                snapshot_text.encode("utf-8")
            ).hexdigest()
        self.restorer = ReplayRestorer(
            self.config,
            template_dir,
            template_reader_dir,
            iap_cut_a=0,
        )
        if str(template_reader_dir) not in sys.path:
            sys.path.insert(0, str(template_reader_dir))
        from puzzle_core.geometry import build_geometry, build_static_geometry
        from puzzle_core.template_reader import parse_level_file

        self.build_geometry = build_geometry
        self.build_static_geometry = build_static_geometry
        self.parse_level_file = parse_level_file
        self.template_dir = template_dir
        self.template_cache: dict[str, TemplateContext] = {}
        self.instance_cache: dict[tuple[Any, ...], InstanceContext] = {}

    def template_context(self, template_id: str) -> TemplateContext:
        if template_id in self.template_cache:
            return self.template_cache[template_id]
        path = self.template_dir / f"{template_id}_m.bytes"
        level = self.parse_level_file(path)
        graph = self.build_geometry(level, include_tower=True)
        static_graph = self.build_static_geometry(level)
        tiles = list(level.tiles)
        tile_count = len(tiles)
        tower_count = len(level.tower_member_indices)
        direct_counts = [len(graph.direct_blockers[index]) for index in range(tile_count)]
        transitive_counts = [
            len(graph.transitive_blockers[index]) for index in range(tile_count)
        ]
        initial_clickable = [index for index, count in enumerate(direct_counts) if count == 0]
        static_clickable = [
            index
            for index in static_graph.included_indices
            if not static_graph.direct_blockers[index]
        ]
        occupied_cells = set()
        for tile in tiles:
            occupied_cells.update(
                {
                    (tile.x, tile.y),
                    (tile.x + 1, tile.y),
                    (tile.x, tile.y + 1),
                    (tile.x + 1, tile.y + 1),
                }
            )
        xs = [tile.x for tile in tiles]
        ys = [tile.y for tile in tiles]
        layer_counts = Counter(tile.z + 1 for tile in tiles)
        source_counts = Counter(tile.tile_id for tile in tiles)
        relation_counts = Counter(relation.overlap_cells for relation in graph.relations)
        features = {
            "level_template_id": template_id,
            "template_sha256": file_sha256(path),
            "template_tile_count": tile_count,
            "template_tower_count": tower_count,
            "template_tower_share": safe_ratio(tower_count, tile_count),
            "template_max_layer": level.max_layer,
            "template_layer_count": len(layer_counts),
            "template_layer_tile_cv": (
                float(np.std(list(layer_counts.values())))
                / float(np.mean(list(layer_counts.values())))
                if layer_counts and np.mean(list(layer_counts.values()))
                else 0.0
            ),
            "template_board_cell_area": len(occupied_cells),
            "template_x_span": (max(xs) - min(xs) + 2) if xs else 0,
            "template_y_span": (max(ys) - min(ys) + 2) if ys else 0,
            "template_initial_clickable_count": len(initial_clickable),
            "template_initial_clickable_share": safe_ratio(
                len(initial_clickable), tile_count
            ),
            "template_static_clickable_count": len(static_clickable),
            "template_direct_cover_relations": len(graph.relations),
            "template_cover_relations_per_tile": safe_ratio(
                len(graph.relations), tile_count
            ),
            "template_quarter_cover_relations": relation_counts.get(1, 0),
            "template_half_cover_relations": relation_counts.get(2, 0),
            "template_full_cover_relations": relation_counts.get(4, 0),
            "template_mean_direct_blockers": nanmean(direct_counts),
            "template_p90_direct_blockers": nanpercentile(direct_counts, 90),
            "template_max_direct_blockers": max(direct_counts, default=0),
            "template_mean_transitive_blockers": nanmean(transitive_counts),
            "template_p90_transitive_blockers": nanpercentile(
                transitive_counts, 90
            ),
            "template_max_transitive_blockers": max(transitive_counts, default=0),
            "template_a_tile_count": source_counts.get(RANDOM_ID_A, 0),
            "template_b_tile_count": source_counts.get(RANDOM_ID_B, 0),
            "template_cn_tile_count": sum(
                count
                for tile_id, count in source_counts.items()
                if CN_TILE_MIN <= tile_id <= CN_TILE_MAX
            ),
            "template_cn_group_count": len(
                [
                    tile_id
                    for tile_id in source_counts
                    if CN_TILE_MIN <= tile_id <= CN_TILE_MAX
                ]
            ),
        }
        context = TemplateContext(level, graph, static_graph, features)
        self.template_cache[template_id] = context
        return context

    @staticmethod
    def instance_key(source: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            as_int(source.get("level_index"), -1),
            clean_text(source.get("level_id")),
            clean_text(source.get("level_template_id")),
            as_int(source.get("level_dup_para_id"), 1),
            clean_text(source.get("level_dup_para")),
        )

    def instance_context(self, source: Mapping[str, Any]) -> InstanceContext:
        key = self.instance_key(source)
        if key in self.instance_cache:
            return self.instance_cache[key]
        level_index, level_id, template_id, dup_id, dup_para = key
        template = self.template_context(template_id)
        resolution = self.config.resolve_level(
            {
                "level_index": str(level_index),
                "level_id": level_id,
                "level_template_id": template_id,
                "level_dup_para_id": str(dup_id),
                "level_dup_para": dup_para,
                "level_value_test": clean_text(source.get("level_value_test")),
            },
            dup_id,
        )
        historical_config_fallback = 0
        client_version = clean_text(source.get("client_version"))
        version_numbers = [as_int(item, 0) for item in client_version.split(".")]
        while len(version_numbers) < 2:
            version_numbers.append(0)
        legacy_client = tuple(version_numbers[:2]) < (2, 8)
        if resolution.error and dup_para and legacy_client:
            # 历史 level_id 可能已被当前 config_level 改写或删除。
            # 2.08 前客户端使用旧版A/B分配，行内 template + DupPara
            # 已足够重建，不应让滚动配置阻断历史分析。
            resolution.error = ""
            resolution.source = "input_level_dup_para_only"
            historical_config_fallback = 1
        reconstructed = self.restorer.reconstruct(resolution)
        tile_by_num = {tile.tile_num: tile for tile in reconstructed.tiles}
        parts = [as_int(item, 0) for item in dup_para.split("|")]
        while len(parts) < 4:
            parts.append(0)

        clickable_indices = [
            index
            for index in range(len(template.level.tiles))
            if not template.graph.direct_blockers[index]
        ]
        assigned_tiles = list(reconstructed.tiles)
        concrete_patterns = [tile.tile_id for tile in assigned_tiles if tile.tile_id > 0]
        clickable_patterns = [
            assigned_tiles[index].tile_id
            for index in clickable_indices
            if index < len(assigned_tiles) and assigned_tiles[index].tile_id > 0
        ]
        clickable_counts = Counter(clickable_patterns)
        unresolved_ab = sum(
            tile.tile_id in {RANDOM_ID_A, RANDOM_ID_B} for tile in assigned_tiles
        )
        cn_count = sum(
            CN_TILE_MIN <= tile.tile_id <= CN_TILE_MAX for tile in assigned_tiles
        )
        features = {
            "level_index": level_index,
            "level_id": level_id,
            "level_template_id": template_id,
            "level_dup_para_id": dup_id,
            "level_dup_para": dup_para,
            "dup_a_type_count": parts[0],
            "dup_random_seed": parts[1],
            "dup_tower_luck": parts[2],
            "dup_b_type_count": parts[3],
            "config_mapping_source": resolution.source,
            "resolved_level_id": resolution.resolved_level_id,
            "random_algorithm": reconstructed.algorithm,
            "reconstruction_status": reconstructed.status,
            "historical_config_fallback": historical_config_fallback,
            "concrete_pattern_tile_share": safe_ratio(
                len(concrete_patterns), len(assigned_tiles)
            ),
            "unresolved_ab_tile_count": unresolved_ab,
            "preserved_cn_tile_count": cn_count,
            "has_cn_structure": int(cn_count > 0),
            "tower_dynamic_required": int(
                template.features["template_tower_count"] > 0
            ),
            "iap_cut_a_assumption": 0,
            "pattern_mapping_confidence": (
                "C_结构可信_C-N具体图案未知"
                if cn_count
                else "B_高_假设IAP未减A花色"
            ),
            "assigned_pattern_count": len(set(concrete_patterns)),
            "assigned_pattern_entropy": shannon_entropy(concrete_patterns),
            "initial_clickable_pattern_count": len(clickable_counts),
            "initial_immediate_triple_pattern_count": sum(
                count >= 3 for count in clickable_counts.values()
            ),
            "initial_pair_pattern_count": sum(
                count == 2 for count in clickable_counts.values()
            ),
            "initial_singleton_pattern_count": sum(
                count == 1 for count in clickable_counts.values()
            ),
        }
        features.update(template.features)
        result = InstanceContext(features, tile_by_num)
        self.instance_cache[key] = result
        return result


def click_timing_features(steps: list[dict[str, Any]]) -> dict[str, Any]:
    clicks = [
        step
        for step in steps
        if step.get("valid") and step.get("action_id") == 1
    ]
    elapsed = [int(step["elapsed_ms"]) for step in clicks]
    gaps_ms = []
    previous = 0
    for current in elapsed:
        gaps_ms.append(max(0, current - previous))
        previous = current
    gaps = np.array(gaps_ms, dtype=float) / 1000.0
    count = len(gaps)
    thirds = np.array_split(gaps, 3) if count else [np.array([])] * 3
    capped_count = sum(
        step.get("valid") and step.get("interval_ms") == INTERVAL_CAP_MS
        for step in steps
    )
    return {
        "click_count": len(clicks),
        "first_click_sec": float(gaps[0]) if count else math.nan,
        "median_click_gap_sec": float(np.median(gaps)) if count else math.nan,
        "p90_click_gap_sec": float(np.percentile(gaps, 90)) if count else math.nan,
        "max_click_gap_sec": float(np.max(gaps)) if count else math.nan,
        "long_pause_3s_count": int(np.sum(gaps >= 3.0)) if count else 0,
        "long_pause_5s_count": int(np.sum(gaps >= 5.0)) if count else 0,
        "long_pause_10s_count": int(np.sum(gaps >= 10.0)) if count else 0,
        "long_pause_3s_share": safe_ratio(int(np.sum(gaps >= 3.0)), count),
        "long_pause_5s_share": safe_ratio(int(np.sum(gaps >= 5.0)), count),
        "long_pause_10s_share": safe_ratio(int(np.sum(gaps >= 10.0)), count),
        "early_median_gap_sec": (
            float(np.median(thirds[0])) if len(thirds[0]) else math.nan
        ),
        "middle_median_gap_sec": (
            float(np.median(thirds[1])) if len(thirds[1]) else math.nan
        ),
        "late_median_gap_sec": (
            float(np.median(thirds[2])) if len(thirds[2]) else math.nan
        ),
        "late_minus_early_gap_sec": (
            float(np.median(thirds[2]) - np.median(thirds[0]))
            if len(thirds[0]) and len(thirds[2])
            else math.nan
        ),
        "capped_interval_count": int(capped_count),
        "timing_right_censored": int(capped_count > 0),
        "click_steps": clicks,
        "click_gaps_sec": gaps.tolist(),
    }


def curve_features(
    bag: list[int], surface: list[int], click_count: int
) -> dict[str, Any]:
    aligned = int(len(bag) == click_count and len(surface) == click_count)
    bag_points = bag[:click_count]
    surface_points = surface[:click_count]
    pressure_flags = [value >= PRESSURE_THRESHOLD for value in bag_points]
    danger_flags = [value >= DANGER_THRESHOLD for value in bag_points]
    full_flags = [value >= FULL_THRESHOLD for value in bag_points]
    low_surface_3 = [value <= 3 for value in surface_points]
    low_surface_5 = [value <= 5 for value in surface_points]
    surface_deltas = np.diff(surface_points) if len(surface_points) >= 2 else np.array([])
    surface_thirds = (
        np.array_split(np.asarray(surface_points, dtype=float), 3)
        if surface_points
        else [np.array([])] * 3
    )
    onsets5 = threshold_onsets(bag_points, PRESSURE_THRESHOLD)
    onsets6 = threshold_onsets(bag_points, DANGER_THRESHOLD)
    onsets7 = threshold_onsets(bag_points, FULL_THRESHOLD)
    return {
        "curve_click_aligned": aligned,
        "bag_point_count": len(bag_points),
        "bag_max": max(bag_points, default=math.nan),
        "bag_mean": nanmean(bag_points),
        "bag_ge5_clicks": sum(pressure_flags),
        "bag_ge6_clicks": sum(danger_flags),
        "bag_ge7_clicks": sum(full_flags),
        "bag_ge5_share": safe_ratio(sum(pressure_flags), len(bag_points)),
        "bag_ge6_share": safe_ratio(sum(danger_flags), len(bag_points)),
        "bag_ge7_share": safe_ratio(sum(full_flags), len(bag_points)),
        "bag_ge5_longest_run": longest_true_run(pressure_flags),
        "bag_ge6_longest_run": longest_true_run(danger_flags),
        "bag_pressure_recovery_count": pressure_recoveries(bag_points),
        "first_bag5_click_no": onsets5[0] + 1 if onsets5 else math.nan,
        "first_bag6_click_no": onsets6[0] + 1 if onsets6 else math.nan,
        "first_bag7_click_no": onsets7[0] + 1 if onsets7 else math.nan,
        "first_bag5_progress": (
            safe_ratio(onsets5[0] + 1, click_count) if onsets5 else math.nan
        ),
        "first_bag6_progress": (
            safe_ratio(onsets6[0] + 1, click_count) if onsets6 else math.nan
        ),
        "first_bag7_progress": (
            safe_ratio(onsets7[0] + 1, click_count) if onsets7 else math.nan
        ),
        "surface_point_count": len(surface_points),
        "surface_first": surface_points[0] if surface_points else math.nan,
        "surface_median": nanmedian(surface_points),
        "surface_min": min(surface_points, default=math.nan),
        "surface_max": max(surface_points, default=math.nan),
        "surface_le3_clicks": sum(low_surface_3),
        "surface_le5_clicks": sum(low_surface_5),
        "surface_le3_share": safe_ratio(sum(low_surface_3), len(surface_points)),
        "surface_le5_share": safe_ratio(sum(low_surface_5), len(surface_points)),
        "surface_release_click_share": (
            safe_ratio(int(np.sum(surface_deltas > 0)), len(surface_deltas))
            if len(surface_deltas)
            else math.nan
        ),
        "surface_mean_change": (
            float(np.mean(surface_deltas)) if len(surface_deltas) else math.nan
        ),
        "surface_late_minus_early": (
            float(np.mean(surface_thirds[2]) - np.mean(surface_thirds[0]))
            if len(surface_thirds[0]) and len(surface_thirds[2])
            else math.nan
        ),
        "bag_points": bag_points,
        "surface_points": surface_points,
        "bag5_onsets": onsets5,
        "bag6_onsets": onsets6,
        "bag7_onsets": onsets7,
    }


def base_attempt_fields(source: Mapping[str, Any], row_no: int) -> dict[str, Any]:
    status = clean_text(source.get("level_status")).lower()
    result = clean_text(source.get("result_category"))
    return {
        "attempt_row_no": row_no,
        "cohort_player_rank": as_int(source.get("cohort_player_rank"), 0),
        "user_id": clean_text(source.get("user_id")),
        "event_date": clean_text(source.get("event_date")),
        "event_time": clean_text(source.get("event_time")),
        "client_version": clean_text(source.get("client_version")),
        "platform": clean_text(source.get("platform")),
        "user_group_tag": clean_text(source.get("user_group_tag")),
        "level_index": as_int(source.get("level_index"), -1),
        "level_id": clean_text(source.get("level_id")),
        "level_template_id": clean_text(source.get("level_template_id")),
        "level_play_count": as_int(source.get("level_play_count"), 0),
        "level_dup_para_id": as_int(source.get("level_dup_para_id"), 0),
        "level_dup_para": clean_text(source.get("level_dup_para")),
        "level_value_test": as_int(source.get("level_value_test"), 0),
        "level_part_index": as_int(source.get("level_part_index"), 0),
        "level_status": status,
        "result_category": result,
        "is_pass": int(status in PASS_STATUSES),
        "is_fail": int(status in FAIL_STATUSES),
        "is_exit": int(status in {"exit", "quit"}),
        "level_duration_sec": as_float(source.get("level_duration_sec")),
        "level_duration_winsor_sec": min(
            as_float(source.get("level_duration_sec"), 0.0),
            DURATION_WINSOR_SEC,
        ),
        "level_progress": as_float(source.get("level_progress")),
        "level_match_count": as_int(source.get("level_match_count"), 0),
        "reported_prop_use_count": as_int(source.get("prop_use_count"), 0),
        "reported_bag_full_event_count": as_int(
            source.get("bag_full_event_count"), 0
        ),
        "used_extra_space": int(clean_text(source.get("used_extra_space")) == "是"),
        "bag_full_process": clean_text(source.get("bag_full_process")),
    }


def extract_attempts(
    source: pd.DataFrame,
    structures: StructureFeatureCatalog,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    attempt_rows: list[dict[str, Any]] = []
    hotspots: dict[tuple[Any, ...], dict[str, Any]] = {}
    template_attempts: Counter[tuple[int, str]] = Counter()

    for row_no, source_row in enumerate(source.to_dict("records"), start=1):
        base = base_attempt_fields(source_row, row_no)
        steps = parse_replay(clean_text(source_row.get("replay_info")))
        timing = click_timing_features(steps)
        clicks = timing.pop("click_steps")
        click_gaps = timing.pop("click_gaps_sec")
        bag = parse_bag_curve(source_row.get("bag_tile_change_curve"))
        surface = parse_surface_curve(
            source_row.get("surface_normal_tile_change_curve")
        )
        curves = curve_features(bag, surface, timing["click_count"])
        bag_points = curves.pop("bag_points")
        surface_points = curves.pop("surface_points")
        onsets5 = set(curves.pop("bag5_onsets"))
        onsets6 = set(curves.pop("bag6_onsets"))
        onsets7 = set(curves.pop("bag7_onsets"))
        instance = structures.instance_context(source_row)

        valid_actions = [step for step in steps if step.get("valid")]
        prop_steps = [
            step
            for step in valid_actions
            if step.get("action_id") not in {1, 2}
        ]
        extra_steps = [
            step
            for step in prop_steps
            if step.get("action_id") in EXTRA_SPACE_ACTION_IDS
        ]
        first_prop = prop_steps[0] if prop_steps else None
        first_extra = extra_steps[0] if extra_steps else None
        booster_target_count = sum(
            len(step.get("tile_nums") or [])
            for step in valid_actions
            if step.get("action_id") == 2
        )

        def click_progress_at(step: Optional[dict[str, Any]]) -> float:
            if step is None or not timing["click_count"]:
                return math.nan
            clicks_before = sum(
                candidate.get("valid")
                and candidate.get("action_id") == 1
                and candidate.get("step_no", 0) < step.get("step_no", 0)
                for candidate in steps
            )
            return safe_ratio(clicks_before, timing["click_count"])

        missing_click_target_count = sum(not step.get("tile_nums") for step in clicks)
        replay_target_count = sum(
            len(step.get("tile_nums") or []) for step in valid_actions
        )
        unmapped_target_count = sum(
            tile_num not in instance.tile_by_num
            for step in valid_actions
            for tile_num in (step.get("tile_nums") or [])
        )
        replay_elapsed_sec = (
            max((step.get("elapsed_ms", 0) for step in valid_actions), default=0)
            / 1000.0
        )
        attempt = {
            **base,
            **timing,
            **curves,
            **instance.features,
            "replay_elapsed_sec_lower_bound": replay_elapsed_sec,
            "replay_step_count": len(valid_actions),
            "invalid_replay_step_count": len(steps) - len(valid_actions),
            "missing_click_target_count": missing_click_target_count,
            "timing_eligible": int(
                timing["capped_interval_count"] == 0
                and curves["curve_click_aligned"] == 1
                and timing["click_count"] > 0
            ),
            "prop_action_count": len(prop_steps),
            "prop_action_type_count": len(
                {step.get("action_id") for step in prop_steps}
            ),
            "any_prop": int(bool(prop_steps)),
            "extra_space_action_count": len(extra_steps),
            "first_prop_progress": click_progress_at(first_prop),
            "first_extra_space_progress": click_progress_at(first_extra),
            "booster_target_count": booster_target_count,
            "replay_target_count": replay_target_count,
            "unmapped_target_count": unmapped_target_count,
        }
        median_gap = as_float(attempt.get("median_click_gap_sec"))
        p90_gap = as_float(attempt.get("p90_click_gap_sec"))
        tile_count = as_float(attempt.get("template_tile_count"))
        attempt["click_burst_log_ratio"] = (
            math.log(p90_gap / median_gap)
            if math.isfinite(median_gap)
            and math.isfinite(p90_gap)
            and median_gap > 0
            and p90_gap > 0
            else math.nan
        )
        attempt["surface_initial_share"] = (
            safe_ratio(as_float(attempt.get("surface_first")), tile_count)
            if math.isfinite(tile_count) and tile_count > 0
            else math.nan
        )
        attempt["surface_reveal_activity"] = as_float(
            attempt.get("surface_release_click_share")
        )
        attempt["surface_reveal_shift"] = (
            safe_ratio(as_float(attempt.get("surface_late_minus_early")), tile_count)
            if math.isfinite(tile_count) and tile_count > 0
            else math.nan
        )

        template_context = structures.template_context(base["level_template_id"])
        static_eligible = int(
            attempt.get("template_tower_count", 0) == 0
            and not prop_steps
            and missing_click_target_count == 0
            and unmapped_target_count == 0
            and attempt["curve_click_aligned"] == 1
            and len(clicks) > 0
        )
        surface_match = math.nan
        illegal_clicks = math.nan
        static_point_count = 0
        if static_eligible:
            removed: set[int] = {
                int(tile_num) - 1
                for step in valid_actions
                if step.get("action_id") == 2
                for tile_num in (step.get("tile_nums") or [])
            }
            predicted = []
            illegal = 0
            for click in clicks:
                tile_num = int(click["tile_nums"][0])
                tile_index = tile_num - 1
                blockers = template_context.graph.direct_blockers[tile_index]
                if not blockers.issubset(removed):
                    illegal += 1
                removed.add(tile_index)
                available = sum(
                    index not in removed
                    and template_context.graph.direct_blockers[index].issubset(removed)
                    for index in range(len(template_context.level.tiles))
                )
                predicted.append(available)
            static_point_count = len(predicted)
            surface_match = int(predicted == surface_points)
            illegal_clicks = illegal
        attempt["static_surface_validation_eligible"] = static_eligible
        attempt["static_surface_validation_match"] = surface_match
        attempt["static_surface_validation_point_count"] = static_point_count
        attempt["static_illegal_blocked_click_count"] = illegal_clicks

        for threshold in (5, 6, 7):
            click_no = attempt.get(f"first_bag{threshold}_click_no")
            if math.isfinite(as_float(click_no)):
                index = int(click_no) - 1
                tile_nums = clicks[index].get("tile_nums") if index < len(clicks) else []
                attempt[f"first_bag{threshold}_tile_num"] = (
                    tile_nums[0] if tile_nums else math.nan
                )
            else:
                attempt[f"first_bag{threshold}_tile_num"] = math.nan

        if click_gaps:
            longest_index = int(np.argmax(click_gaps))
            longest_targets = clicks[longest_index].get("tile_nums") or []
            attempt["longest_pause_tile_num"] = (
                longest_targets[0] if longest_targets else math.nan
            )
        else:
            attempt["longest_pause_tile_num"] = math.nan
        if clicks:
            final_targets = clicks[-1].get("tile_nums") or []
            attempt["final_click_tile_num"] = (
                final_targets[0] if final_targets else math.nan
            )
        else:
            attempt["final_click_tile_num"] = math.nan
        attempt_rows.append(attempt)

        template_key = (base["level_index"], base["level_template_id"])
        template_attempts[template_key] += 1
        last_tile_num: Optional[int] = None
        click_index = 0
        longest_pause_index = int(np.argmax(click_gaps)) if click_gaps else -1
        for step in valid_actions:
            action_id = step.get("action_id")
            if action_id == 1:
                targets = step.get("tile_nums") or []
                tile_num = int(targets[0]) if targets else None
                if tile_num is not None:
                    tile = instance.tile_by_num.get(tile_num)
                    key = (
                        base["level_index"],
                        base["level_template_id"],
                        tile_num,
                    )
                    record = hotspots.setdefault(
                        key,
                        {
                            "level_index": base["level_index"],
                            "level_template_id": base["level_template_id"],
                            "tile_num": tile_num,
                            "initial_x": getattr(tile, "x", math.nan),
                            "initial_y": getattr(tile, "y", math.nan),
                            "initial_z": getattr(tile, "z", math.nan),
                            "initial_layer": (
                                getattr(tile, "z", -1) + 1 if tile is not None else math.nan
                            ),
                            "tower_index": (
                                getattr(tile, "tower_index", math.nan)
                                if tile is not None
                                else math.nan
                            ),
                            "click_count": 0,
                            "pressure5_onset_count": 0,
                            "pressure6_onset_count": 0,
                            "bag7_onset_count": 0,
                            "long_pause10_before_count": 0,
                            "longest_pause_before_count": 0,
                            "prop_after_count": 0,
                            "extra_space_after_count": 0,
                            "failed_attempt_final_click_count": 0,
                            "pattern_counts": Counter(),
                        },
                    )
                    record["click_count"] += 1
                    if click_index in onsets5:
                        record["pressure5_onset_count"] += 1
                    if click_index in onsets6:
                        record["pressure6_onset_count"] += 1
                    if click_index in onsets7:
                        record["bag7_onset_count"] += 1
                    if click_index < len(click_gaps) and click_gaps[click_index] >= 10:
                        record["long_pause10_before_count"] += 1
                    if click_index == longest_pause_index:
                        record["longest_pause_before_count"] += 1
                    if base["is_fail"] and click_index == len(clicks) - 1:
                        record["failed_attempt_final_click_count"] += 1
                    if tile is not None:
                        record["pattern_counts"][int(tile.tile_id)] += 1
                    last_tile_num = tile_num
                click_index += 1
            elif action_id not in {1, 2} and last_tile_num is not None:
                key = (
                    base["level_index"],
                    base["level_template_id"],
                    last_tile_num,
                )
                if key in hotspots:
                    hotspots[key]["prop_after_count"] += 1
                    if action_id in EXTRA_SPACE_ACTION_IDS:
                        hotspots[key]["extra_space_after_count"] += 1

    attempt_frame = pd.DataFrame(attempt_rows)
    hotspot_rows = []
    for key, record in hotspots.items():
        patterns = record.pop("pattern_counts")
        mode_pattern, mode_count = (patterns.most_common(1)[0] if patterns else (None, 0))
        attempts = template_attempts[(key[0], key[1])]
        record.update(
            {
                "template_attempt_count": attempts,
                "click_rate_per_attempt": safe_ratio(record["click_count"], attempts),
                "pressure5_onset_rate_per_attempt": safe_ratio(
                    record["pressure5_onset_count"], attempts
                ),
                "pressure6_onset_rate_per_attempt": safe_ratio(
                    record["pressure6_onset_count"], attempts
                ),
                "bag7_onset_rate_per_attempt": safe_ratio(
                    record["bag7_onset_count"], attempts
                ),
                "long_pause10_rate_per_attempt": safe_ratio(
                    record["long_pause10_before_count"], attempts
                ),
                "prop_after_rate_per_attempt": safe_ratio(
                    record["prop_after_count"], attempts
                ),
                "extra_space_after_rate_per_attempt": safe_ratio(
                    record["extra_space_after_count"], attempts
                ),
                "initial_pattern_mode": mode_pattern,
                "initial_pattern_mode_share": safe_ratio(mode_count, record["click_count"]),
            }
        )
        hotspot_rows.append(record)
    hotspot_frame = pd.DataFrame(hotspot_rows)
    if not hotspot_frame.empty:
        hotspot_frame = hotspot_frame.sort_values(
            ["level_index", "level_template_id", "pressure5_onset_count", "tile_num"],
            ascending=[True, True, False, True],
        ).reset_index(drop=True)
    return attempt_frame, hotspot_frame


def weighted_attempt_share(group: pd.DataFrame, count_column: str, total_column: str) -> float:
    numerator = pd.to_numeric(group[count_column], errors="coerce").sum()
    denominator = pd.to_numeric(group[total_column], errors="coerce").sum()
    return safe_ratio(float(numerator), float(denominator))


def aggregate_player_levels(attempts: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    attempts = attempts.sort_values(
        ["cohort_player_rank", "level_index", "level_play_count", "event_time"]
    )
    for (user_id, level_index), group in attempts.groupby(
        ["user_id", "level_index"], sort=True
    ):
        group = group.sort_values(["level_play_count", "event_time"])
        explicit = group[group["level_play_count"] == 1]
        anchor = explicit.iloc[0] if not explicit.empty else group.iloc[0]
        pass_rows = group[group["is_pass"] == 1]
        pass_row = pass_rows.iloc[-1] if not pass_rows.empty else None
        play_counts = sorted(
            set(pd.to_numeric(group["level_play_count"], errors="coerce").dropna().astype(int))
        )
        expected = list(range(1, max(play_counts) + 1)) if play_counts else []
        timing_anchor_ok = bool(anchor.get("timing_eligible", 0)) and not explicit.empty

        row = {
            "cohort_player_rank": int(group["cohort_player_rank"].iloc[0]),
            "user_id": user_id,
            "level_index": int(level_index),
            "explicit_first_attempt": int(not explicit.empty),
            "min_observed_play_count": min(play_counts) if play_counts else math.nan,
            "max_observed_play_count": max(play_counts) if play_counts else math.nan,
            "play_count_sequence_complete": int(play_counts == expected),
            "observed_attempts": len(group),
            "observed_extra_attempts": max(0, len(group) - 1),
            "observed_failure_count": int(group["is_fail"].sum()),
            "any_observed_failure": int(group["is_fail"].sum() > 0),
            "observed_pass": int(group["is_pass"].sum() > 0),
            "first_fail": (
                int(anchor["is_pass"] == 0) if not explicit.empty else math.nan
            ),
            "first_status": anchor["level_status"] if not explicit.empty else "",
            "first_result_category": (
                anchor["result_category"] if not explicit.empty else ""
            ),
            "total_duration_winsor_sec": float(group["level_duration_winsor_sec"].sum()),
            "pass_duration_winsor_sec": (
                float(pass_row["level_duration_winsor_sec"])
                if pass_row is not None
                else math.nan
            ),
            "first_duration_winsor_sec": (
                float(anchor["level_duration_winsor_sec"])
                if not explicit.empty
                else math.nan
            ),
            "total_click_count": int(group["click_count"].sum()),
            "total_prop_action_count": int(group["prop_action_count"].sum()),
            "prop_actions_per_attempt": safe_ratio(
                int(group["prop_action_count"].sum()), len(group)
            ),
            "any_prop": int(group["any_prop"].max()),
            "any_extra_space": int(group["used_extra_space"].max()),
            "bag_full_any": int(
                group["reported_bag_full_event_count"].sum() > 0
                or pd.to_numeric(group["bag_max"], errors="coerce").max() >= FULL_THRESHOLD
            ),
            "bag_max": float(pd.to_numeric(group["bag_max"], errors="coerce").max()),
            "bag_ge5_share": weighted_attempt_share(
                group, "bag_ge5_clicks", "bag_point_count"
            ),
            "bag_ge6_share": weighted_attempt_share(
                group, "bag_ge6_clicks", "bag_point_count"
            ),
            "bag_ge7_share": weighted_attempt_share(
                group, "bag_ge7_clicks", "bag_point_count"
            ),
            "surface_le3_share": weighted_attempt_share(
                group, "surface_le3_clicks", "surface_point_count"
            ),
            "surface_le5_share": weighted_attempt_share(
                group, "surface_le5_clicks", "surface_point_count"
            ),
            "first_click_sec": (
                float(anchor["first_click_sec"]) if timing_anchor_ok else math.nan
            ),
            "median_click_gap_sec": (
                float(anchor["median_click_gap_sec"]) if timing_anchor_ok else math.nan
            ),
            "p90_click_gap_sec": (
                float(anchor["p90_click_gap_sec"]) if timing_anchor_ok else math.nan
            ),
            "long_pause_10s_share": (
                float(anchor["long_pause_10s_share"]) if timing_anchor_ok else math.nan
            ),
            "late_minus_early_gap_sec": (
                float(anchor["late_minus_early_gap_sec"])
                if timing_anchor_ok
                else math.nan
            ),
            "click_burst_log_ratio": (
                float(anchor["click_burst_log_ratio"])
                if timing_anchor_ok
                else math.nan
            ),
            "surface_initial_share": (
                float(anchor["surface_initial_share"])
                if not explicit.empty
                else math.nan
            ),
            "surface_reveal_activity": (
                float(anchor["surface_reveal_activity"])
                if not explicit.empty
                else math.nan
            ),
            "surface_reveal_shift": (
                float(anchor["surface_reveal_shift"])
                if not explicit.empty
                else math.nan
            ),
            "timing_anchor_eligible": int(timing_anchor_ok),
            "anchor_level_id": anchor["level_id"],
            "anchor_level_template_id": anchor["level_template_id"],
            "anchor_level_dup_para_id": int(anchor["level_dup_para_id"]),
            "anchor_level_dup_para": anchor["level_dup_para"],
            "anchor_client_version": anchor["client_version"],
            "anchor_user_group_tag": anchor["user_group_tag"],
            "anchor_level_value_test": int(anchor["level_value_test"]),
        }
        structure_columns = [
            column
            for column in attempts.columns
            if column.startswith("template_")
            or column
            in {
                "dup_a_type_count",
                "dup_random_seed",
                "dup_tower_luck",
                "dup_b_type_count",
                "assigned_pattern_count",
                "initial_clickable_pattern_count",
                "initial_immediate_triple_pattern_count",
                "initial_pair_pattern_count",
                "initial_singleton_pattern_count",
                "concrete_pattern_tile_share",
                "reconstruction_status",
                "random_algorithm",
            }
        ]
        for column in structure_columns:
            row[column] = anchor.get(column)
        rows.append(row)

    frame = pd.DataFrame(rows).sort_values(
        ["cohort_player_rank", "level_index"]
    ).reset_index(drop=True)
    frame["log_observed_attempts"] = np.log1p(frame["observed_attempts"].astype(float))
    frame["log_total_duration"] = np.log1p(frame["total_duration_winsor_sec"].astype(float))
    frame["log_first_click"] = np.log1p(frame["first_click_sec"].astype(float))
    frame["log_median_gap"] = np.log1p(frame["median_click_gap_sec"].astype(float))
    frame["log_p90_gap"] = np.log1p(frame["p90_click_gap_sec"].astype(float))

    robust_columns = [
        "log_observed_attempts",
        "log_total_duration",
        "bag_ge5_share",
        "bag_max",
        "log_first_click",
        "log_median_gap",
        "log_p90_gap",
        "long_pause_10s_share",
        "late_minus_early_gap_sec",
        "surface_le3_share",
        "prop_actions_per_attempt",
        "click_burst_log_ratio",
        "surface_initial_share",
        "surface_reveal_activity",
        "surface_reveal_shift",
    ]
    for column in robust_columns:
        frame[f"loo_rz_{column}"] = leave_one_out_robust_z(frame, column)
    for column in ["first_fail", "bag_full_any", "any_prop", "any_extra_space"]:
        frame[f"loo_centered_{column}"] = leave_one_out_centered(frame, column)

    load_columns = [
        "loo_rz_log_observed_attempts",
        "loo_rz_log_total_duration",
        "loo_rz_bag_ge5_share",
        "loo_rz_prop_actions_per_attempt",
    ]
    frame["player_experience_load"] = frame[load_columns].mean(axis=1, skipna=True)
    return frame


def binary_entropy(rate: float) -> float:
    if not math.isfinite(rate) or rate <= 0 or rate >= 1:
        return 0.0 if math.isfinite(rate) else math.nan
    return float(-(rate * math.log2(rate) + (1 - rate) * math.log2(1 - rate)))


def aggregate_levels(player_levels: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for level_index, group in player_levels.groupby("level_index", sort=True):
        explicit = group[group["explicit_first_attempt"] == 1]
        failures = int(explicit["first_fail"].sum())
        n_first = len(explicit)
        ci_low, ci_high = wilson_interval(failures, n_first)

        def mean_column(column: str) -> float:
            return nanmean(group[column])

        def iqr_column(column: str) -> float:
            values = finite_values(group[column])
            return (
                float(np.percentile(values, 75) - np.percentile(values, 25))
                if len(values)
                else math.nan
            )

        row = {
            "level_index": int(level_index),
            "player_count": int(group["user_id"].nunique()),
            "explicit_first_player_count": n_first,
            "timing_eligible_player_count": int(group["timing_anchor_eligible"].sum()),
            "observed_attempt_count": int(group["observed_attempts"].sum()),
            "observed_extra_attempts_per_player": mean_column(
                "observed_extra_attempts"
            ),
            "players_with_observed_failure_rate": mean_column(
                "any_observed_failure"
            ),
            "first_fail_rate": safe_ratio(failures, n_first),
            "first_fail_rate_ci_low": ci_low,
            "first_fail_rate_ci_high": ci_high,
            "observed_pass_rate": mean_column("observed_pass"),
            "median_total_duration_sec": nanmedian(group["total_duration_winsor_sec"]),
            "p75_total_duration_sec": nanpercentile(
                group["total_duration_winsor_sec"], 75
            ),
            "median_first_duration_sec": nanmedian(group["first_duration_winsor_sec"]),
            "bag_full_player_rate": mean_column("bag_full_any"),
            "median_bag_max": nanmedian(group["bag_max"]),
            "median_bag_ge5_share": nanmedian(group["bag_ge5_share"]),
            "median_bag_ge6_share": nanmedian(group["bag_ge6_share"]),
            "median_surface_le3_share": nanmedian(group["surface_le3_share"]),
            "median_first_click_sec": nanmedian(group["first_click_sec"]),
            "median_click_gap_sec": nanmedian(group["median_click_gap_sec"]),
            "median_p90_click_gap_sec": nanmedian(group["p90_click_gap_sec"]),
            "median_long_pause_10s_share": nanmedian(group["long_pause_10s_share"]),
            "median_late_minus_early_gap_sec": nanmedian(
                group["late_minus_early_gap_sec"]
            ),
            "median_click_burst_log_ratio": nanmedian(
                group["click_burst_log_ratio"]
            ),
            "median_surface_initial_share": nanmedian(
                group["surface_initial_share"]
            ),
            "median_surface_reveal_activity": nanmedian(
                group["surface_reveal_activity"]
            ),
            "median_surface_reveal_shift": nanmedian(
                group["surface_reveal_shift"]
            ),
            "prop_user_rate": mean_column("any_prop"),
            "extra_space_user_rate": mean_column("any_extra_space"),
            "median_prop_actions_per_attempt": nanmedian(
                group["prop_actions_per_attempt"]
            ),
            "level_id_count": int(group["anchor_level_id"].nunique()),
            "template_count": int(group["anchor_level_template_id"].nunique()),
            "dup_para_count": int(
                group[
                    ["anchor_level_dup_para_id", "anchor_level_dup_para"]
                ].drop_duplicates().shape[0]
            ),
            "client_version_count": int(group["anchor_client_version"].nunique()),
            "user_group_count": int(group["anchor_user_group_tag"].nunique()),
            "experience_load_iqr": iqr_column("player_experience_load"),
            "duration_residual_iqr": iqr_column("loo_rz_log_total_duration"),
            "pressure_residual_iqr": iqr_column("loo_rz_bag_ge5_share"),
            "first_result_entropy": binary_entropy(safe_ratio(failures, n_first)),
        }
        residual_columns = [
            "loo_rz_log_observed_attempts",
            "loo_rz_log_total_duration",
            "loo_rz_bag_ge5_share",
            "loo_rz_bag_max",
            "loo_rz_log_first_click",
            "loo_rz_log_median_gap",
            "loo_rz_log_p90_gap",
            "loo_rz_long_pause_10s_share",
            "loo_rz_late_minus_early_gap_sec",
            "loo_rz_surface_le3_share",
            "loo_rz_prop_actions_per_attempt",
            "loo_rz_click_burst_log_ratio",
            "loo_rz_surface_initial_share",
            "loo_rz_surface_reveal_activity",
            "loo_rz_surface_reveal_shift",
            "loo_centered_first_fail",
            "loo_centered_bag_full_any",
            "loo_centered_any_prop",
            "loo_centered_any_extra_space",
        ]
        for column in residual_columns:
            row[f"mean_{column}"] = mean_column(column)

        structure_columns = [
            column
            for column in player_levels.columns
            if column.startswith("template_")
            or column
            in {
                "dup_a_type_count",
                "dup_b_type_count",
                "assigned_pattern_count",
                "initial_clickable_pattern_count",
                "initial_immediate_triple_pattern_count",
                "initial_pair_pattern_count",
                "initial_singleton_pattern_count",
                "concrete_pattern_tile_share",
            }
        ]
        for column in structure_columns:
            row[f"median_{column}"] = nanmedian(group[column])
        rows.append(row)

    levels = pd.DataFrame(rows).sort_values("level_index").reset_index(drop=True)
    dimensions = {
        "difficulty": [
            "mean_loo_centered_first_fail",
            "mean_loo_rz_log_observed_attempts",
            "mean_loo_rz_log_total_duration",
        ],
        "pressure": [
            "mean_loo_centered_bag_full_any",
            "mean_loo_rz_bag_ge5_share",
            "mean_loo_rz_bag_max",
        ],
        "comprehension": [
            "mean_loo_rz_log_first_click",
            "mean_loo_rz_log_p90_gap",
            "mean_loo_rz_long_pause_10s_share",
        ],
        "rhythm": [
            "mean_loo_rz_log_median_gap",
            "mean_loo_rz_late_minus_early_gap_sec",
            "mean_loo_rz_surface_le3_share",
        ],
        "tool_dependency": [
            "mean_loo_centered_any_prop",
            "mean_loo_centered_any_extra_space",
            "mean_loo_rz_prop_actions_per_attempt",
        ],
        "disagreement": [
            "experience_load_iqr",
            "duration_residual_iqr",
            "pressure_residual_iqr",
            "first_result_entropy",
        ],
    }
    for dimension, components in dimensions.items():
        standardized = []
        for component in components:
            scaled_name = f"z_{component}"
            levels[scaled_name] = robust_scale(levels[component])
            standardized.append(scaled_name)
        levels[f"{dimension}_raw"] = levels[standardized].mean(axis=1, skipna=True)
        levels[f"{dimension}_score"] = percentile_score(levels[f"{dimension}_raw"])

    def difficulty_band(score: float) -> str:
        if score <= 25:
            return "L1_低负担"
        if score <= 50:
            return "L2_中等负担"
        if score <= 75:
            return "L3_高负担"
        return "L4_极高负担"

    levels["difficulty_band"] = levels["difficulty_score"].map(difficulty_band)

    tag_fields = [
        ("pressure_score", "高背包压力"),
        ("comprehension_score", "高思考停顿"),
        ("rhythm_score", "节奏拖尾"),
        ("tool_dependency_score", "高道具依赖"),
        ("disagreement_score", "玩家分歧"),
    ]

    def profile_tags(row: pd.Series) -> str:
        tags = [label for column, label in tag_fields if row[column] >= TAG_THRESHOLD]
        if tags:
            return "+".join(tags)
        if row["difficulty_score"] <= 25 and row["pressure_score"] <= 50:
            return "低压流畅"
        return "常规型"

    levels["behavior_profile"] = levels.apply(profile_tags, axis=1)
    score_columns = [
        "difficulty_score",
        "pressure_score",
        "comprehension_score",
        "rhythm_score",
        "tool_dependency_score",
        "disagreement_score",
    ]
    levels["high_signal_dimension_count"] = (levels[score_columns] >= TAG_THRESHOLD).sum(
        axis=1
    )
    levels["review_priority_score"] = levels[score_columns].apply(
        lambda row: float(np.mean(np.sort(row.to_numpy(float))[-3:])), axis=1
    )

    cluster_axes = {
        "behavior_pressure_axis": "mean_loo_rz_bag_ge5_share",
        "behavior_slow_tempo_axis": "mean_loo_rz_log_median_gap",
        "behavior_burst_axis": "mean_loo_rz_click_burst_log_ratio",
        "behavior_late_slowdown_axis": "mean_loo_rz_late_minus_early_gap_sec",
        "behavior_initial_visibility_axis": "mean_loo_rz_surface_initial_share",
        "behavior_reveal_activity_axis": "mean_loo_rz_surface_reveal_activity",
        "behavior_reveal_shift_axis": "mean_loo_rz_surface_reveal_shift",
    }
    for output, source in cluster_axes.items():
        levels[output] = robust_scale(levels[source]).fillna(0.0)
    cluster_columns = list(cluster_axes)
    labels, centers, silhouette = kmeans_three(
        levels[cluster_columns].to_numpy(float)
    )
    t3_index = int(
        np.argmax(
            centers[:, 0]
            - centers[:, 1]
            + centers[:, 2]
            - centers[:, 3]
            + centers[:, 5]
            + centers[:, 6]
        )
    )
    remaining = [index for index in range(3) if index != t3_index]
    t1_index = max(
        remaining,
        key=lambda index: (
            centers[index, 4]
            - centers[index, 5]
            - centers[index, 6]
            - centers[index, 0]
        ),
    )
    t2_index = next(index for index in remaining if index != t1_index)
    cluster_names = {
        t1_index: "T1_前显收束后段放慢型",
        t2_index: "T2_低初显渐展平稳型",
        t3_index: "T3_高压快速揭示爆发型",
    }
    levels["behavior_cluster"] = [cluster_names[int(label)] for label in labels]
    distances = np.sqrt(
        (
            levels[cluster_columns].to_numpy(float)[:, None, :]
            - centers[None, :, :]
        )
        ** 2
    ).sum(axis=2)
    sorted_distances = np.sort(distances, axis=1)
    levels["behavior_cluster_confidence"] = 1.0 - (
        sorted_distances[:, 0] / np.maximum(sorted_distances[:, 1], 1e-12)
    )
    levels["behavior_cluster_borderline"] = (
        levels["behavior_cluster_confidence"] < 0.15
    ).astype(int)
    levels["behavior_cluster_silhouette"] = silhouette
    return levels


INSTANCE_KEYS = [
    "level_index",
    "level_id",
    "level_template_id",
    "level_dup_para_id",
    "level_dup_para",
]


def aggregate_instances(attempts: pd.DataFrame, levels: pd.DataFrame) -> pd.DataFrame:
    level_parent = levels.set_index("level_index")
    rows = []
    for key, group in attempts.groupby(INSTANCE_KEYS, sort=True, dropna=False):
        level_index = int(key[0])
        parent_fail = float(
            pd.to_numeric(
                attempts.loc[attempts["level_index"] == level_index, "is_fail"],
                errors="coerce",
            ).mean()
        )
        parent_log_duration = nanmean(
            np.log1p(
                pd.to_numeric(
                    attempts.loc[
                        attempts["level_index"] == level_index,
                        "level_duration_winsor_sec",
                    ],
                    errors="coerce",
                )
            )
        )
        parent_pressure = nanmean(
            attempts.loc[attempts["level_index"] == level_index, "bag_ge5_share"]
        )
        parent_prop = float(
            pd.to_numeric(
                attempts.loc[attempts["level_index"] == level_index, "any_prop"],
                errors="coerce",
            ).mean()
        )
        n = len(group)
        n_players = int(group["user_id"].nunique())
        raw_fail = float(group["is_fail"].mean())
        raw_log_duration = nanmean(
            np.log1p(pd.to_numeric(group["level_duration_winsor_sec"], errors="coerce"))
        )
        raw_pressure = nanmean(group["bag_ge5_share"])
        raw_prop = float(group["any_prop"].mean())

        def shrink(raw: float, parent: float) -> float:
            return (n_players * raw + INSTANCE_PRIOR_N * parent) / (
                n_players + INSTANCE_PRIOR_N
            )

        sample = group.iloc[0]
        row = {column: value for column, value in zip(INSTANCE_KEYS, key)}
        row.update(
            {
                "attempt_count": n,
                "player_count": n_players,
                "explicit_first_attempt_count": int(
                    (group["level_play_count"] == 1).sum()
                ),
                "pass_rate": float(group["is_pass"].mean()),
                "fail_rate": raw_fail,
                "median_duration_sec": nanmedian(group["level_duration_winsor_sec"]),
                "median_bag_ge5_share": nanmedian(group["bag_ge5_share"]),
                "bag_full_attempt_rate": float(
                    (
                        (group["reported_bag_full_event_count"] > 0)
                        | (pd.to_numeric(group["bag_max"], errors="coerce") >= FULL_THRESHOLD)
                    ).mean()
                ),
                "prop_attempt_rate": raw_prop,
                "extra_space_attempt_rate": float(group["used_extra_space"].mean()),
                "shrunken_fail_rate": shrink(raw_fail, parent_fail),
                "shrunken_log_duration": shrink(raw_log_duration, parent_log_duration),
                "shrunken_bag_ge5_share": shrink(raw_pressure, parent_pressure),
                "shrunken_prop_rate": shrink(raw_prop, parent_prop),
                "fail_rate_delta_vs_level": shrink(raw_fail, parent_fail) - parent_fail,
                "log_duration_delta_vs_level": shrink(
                    raw_log_duration, parent_log_duration
                )
                - parent_log_duration,
                "bag_pressure_delta_vs_level": shrink(raw_pressure, parent_pressure)
                - parent_pressure,
                "prop_rate_delta_vs_level": shrink(raw_prop, parent_prop) - parent_prop,
                "reliability": (
                    "A_>=20人"
                    if n_players >= 20
                    else "B_10-19人"
                    if n_players >= 10
                    else "C_5-9人"
                    if n_players >= 5
                    else "D_<5人仅观察"
                ),
                "level_difficulty_band": level_parent.loc[
                    level_index, "difficulty_band"
                ],
                "level_behavior_profile": level_parent.loc[
                    level_index, "behavior_profile"
                ],
            }
        )
        carry_columns = [
            column
            for column in attempts.columns
            if column.startswith("template_")
            or column
            in {
                "dup_a_type_count",
                "dup_random_seed",
                "dup_tower_luck",
                "dup_b_type_count",
                "config_mapping_source",
                "resolved_level_id",
                "random_algorithm",
                "reconstruction_status",
                "concrete_pattern_tile_share",
                "unresolved_ab_tile_count",
                "preserved_cn_tile_count",
                "has_cn_structure",
                "tower_dynamic_required",
                "iap_cut_a_assumption",
                "pattern_mapping_confidence",
                "assigned_pattern_count",
                "assigned_pattern_entropy",
                "initial_clickable_pattern_count",
                "initial_immediate_triple_pattern_count",
                "initial_pair_pattern_count",
                "initial_singleton_pattern_count",
            }
        ]
        for column in carry_columns:
            row[column] = sample.get(column)
        rows.append(row)
    frame = pd.DataFrame(rows)
    delta_components = [
        "fail_rate_delta_vs_level",
        "log_duration_delta_vs_level",
        "bag_pressure_delta_vs_level",
        "prop_rate_delta_vs_level",
    ]
    scaled = []
    for column in delta_components:
        scaled_column = f"z_{column}"
        frame[scaled_column] = robust_scale(frame[column])
        scaled.append(scaled_column)
    frame["instance_load_delta_raw"] = frame[scaled].mean(axis=1, skipna=True)
    frame["instance_load_delta_percentile"] = percentile_score(
        frame["instance_load_delta_raw"]
    )

    def delta_label(row: pd.Series) -> str:
        if row["player_count"] < 5:
            return "样本不足"
        if row["instance_load_delta_percentile"] >= 80:
            return "相对偏难"
        if row["instance_load_delta_percentile"] <= 20:
            return "相对偏易"
        return "接近关卡整体"

    frame["instance_delta_label"] = frame.apply(delta_label, axis=1)
    return frame.sort_values(INSTANCE_KEYS).reset_index(drop=True)


def build_template_frame(structures: StructureFeatureCatalog) -> pd.DataFrame:
    rows = [context.features for context in structures.template_cache.values()]
    return pd.DataFrame(rows).sort_values("level_template_id").reset_index(drop=True)


def structure_correlations(levels: pd.DataFrame) -> list[dict[str, Any]]:
    structure_columns = [
        column
        for column in levels.columns
        if column.startswith("median_template_")
        or column
        in {
            "median_dup_a_type_count",
            "median_dup_b_type_count",
            "median_assigned_pattern_count",
            "median_initial_clickable_pattern_count",
            "median_initial_immediate_triple_pattern_count",
            "median_initial_pair_pattern_count",
            "median_initial_singleton_pattern_count",
        }
    ]
    score_columns = [
        "difficulty_score",
        "pressure_score",
        "comprehension_score",
        "rhythm_score",
        "tool_dependency_score",
        "disagreement_score",
    ]
    rows = []
    for structure in structure_columns:
        for score in score_columns:
            pair = levels[[structure, score]].dropna()
            if len(pair) < 20 or pair[structure].nunique() < 2:
                continue
            correlation = pair[structure].rank().corr(pair[score].rank())
            rows.append(
                {
                    "structure_metric": structure,
                    "behavior_score": score,
                    "spearman_correlation": float(correlation),
                    "absolute_correlation": abs(float(correlation)),
                    "level_count": len(pair),
                }
            )
    return sorted(rows, key=lambda row: row["absolute_correlation"], reverse=True)


def metric_dictionary() -> list[dict[str, str]]:
    return [
        {
            "field": "difficulty_score",
            "dimension": "难度",
            "definition": "玩家内校正后的首局失败、观测局数和总耗时等权合成，再转为100关百分位。",
            "direction": "越高表示实际负担越高",
        },
        {
            "field": "pressure_score",
            "dimension": "压力",
            "definition": "背包满玩家比例、背包>=5点位占比和最大背包数的玩家内残差。",
            "direction": "越高表示背包压力越集中",
        },
        {
            "field": "comprehension_score",
            "dimension": "可理解性代理",
            "definition": "显式首局的首次点击、P90点击间隔和10秒长停顿占比。",
            "direction": "越高表示需要更多观察/思考，不等同于绝对卡顿",
        },
        {
            "field": "rhythm_score",
            "dimension": "节奏阻滞",
            "definition": "显式首局中位点击间隔、后段相对前段减速、表面牌<=3的占比。",
            "direction": "越高表示后段或低表面阶段更慢",
        },
        {
            "field": "tool_dependency_score",
            "dimension": "道具依赖",
            "definition": "使用任意道具、背包+1以及单局道具数的玩家内残差。",
            "direction": "越高表示真实玩法更依赖道具",
        },
        {
            "field": "disagreement_score",
            "dimension": "分歧度",
            "definition": "玩家负担、耗时、背包压力的四分位距和首局结果熵。",
            "direction": "越高表示同一关对不同玩家的反应差异越大",
        },
        {
            "field": "difficulty_band",
            "dimension": "主分层",
            "definition": "按difficulty_score在100关内的四分位划分L1–L4，每层约25关。",
            "direction": "L4是当前样本中负担最高的25%",
        },
        {
            "field": "behavior_profile",
            "dimension": "多维标签",
            "definition": "任一非难度维度达到75百分位即输出对应标签，可多选。",
            "direction": "用于解释难在哪里，不是好/坏体验结论",
        },
        {
            "field": "behavior_cluster",
            "dimension": "行为工作层",
            "definition": "使用压力、速度、爆发、后段减速、初始表面、揭示活跃和前后表面变化7轴做可复现K=3。",
            "direction": "行为类型与难度层独立；边界关保留低置信标记",
        },
        {
            "field": "first_fail_rate",
            "dimension": "原始指标",
            "definition": "仅使用明确存在level_play_count=1的玩家计算，并输出Wilson 95%区间。",
            "direction": "显式首局失败率",
        },
        {
            "field": "instance_load_delta_percentile",
            "dimension": "配置实例",
            "definition": "具体模板+副本+参数相对父级level_index的负担差，向父级收缩10个玩家先验后转百分位。",
            "direction": "样本<5人时只作观察",
        },
    ]


def output_columns(frame: pd.DataFrame, preferred: Sequence[str]) -> list[str]:
    result = [column for column in preferred if column in frame.columns]
    result.extend(column for column in frame.columns if column not in result)
    return result


def write_report(
    path: Path,
    levels: pd.DataFrame,
    quality: Mapping[str, Any],
    correlations: Sequence[Mapping[str, Any]],
) -> None:
    top_difficulty = levels.nlargest(10, "difficulty_score")
    top_pressure = levels.nlargest(10, "pressure_score")
    top_pause = levels.nlargest(10, "comprehension_score")
    lines = [
        "# 100–199关行为分层初版结论",
        "",
        "## 结论",
        "",
        "这份结果已经可用于第一版关卡分层。主分层是当前100关内的相对负担四分位，",
        "同时保留高背包压力、高思考停顿、节奏拖尾、高道具依赖和玩家分歧标签。",
        "这些标签是行为画像，尚不代表“好/坏体验”；需要后续少量策划标注完成语义校准。",
        "",
        "## 最高负担关卡",
        "",
    ]
    for row in top_difficulty.itertuples(index=False):
        lines.append(
            f"- {row.level_index}关：难度{row.difficulty_score:.0f}，"
            f"压力{row.pressure_score:.0f}，{row.behavior_profile}，"
            f"首局失败率{row.first_fail_rate:.1%}。"
        )
    lines.extend(["", "## 高背包压力关卡", ""])
    for row in top_pressure.itertuples(index=False):
        lines.append(
            f"- {row.level_index}关：压力{row.pressure_score:.0f}，"
            f"背包满玩家占比{row.bag_full_player_rate:.1%}，"
            f"背包>=5点位中位占比{row.median_bag_ge5_share:.1%}。"
        )
    lines.extend(["", "## 高思考停顿关卡", ""])
    for row in top_pause.itertuples(index=False):
        lines.append(
            f"- {row.level_index}关：可理解性代理{row.comprehension_score:.0f}，"
            f"首次点击中位{row.median_first_click_sec:.1f}秒，"
            f"点击间隔P90中位{row.median_p90_click_gap_sec:.1f}秒。"
        )
    lines.extend(
        [
            "",
            "## 数据边界",
            "",
            f"- 样本：{quality['counts']['players']}名到300关以上的Android玩家，"
            f"{quality['counts']['levels']}关，{quality['counts']['attempts']}局。",
            f"- 明确首局：{quality['counts']['explicit_first_player_levels']}/"
            f"{quality['counts']['player_levels']}个玩家×关卡单元。",
            f"- 时间右删失：{quality['counts']['timing_right_censored_attempts']}局含120秒上限间隔，"
            "不进入思考时长基线。",
            "- 结构指标是初始静态模板与当局A/B图案分配；塔动态换牌、C–N无固定种子分配、",
            "  道具Handler和局面求解尚未进入第一版。",
            "- 分层是100–199关内的相对分层，不能直接外推到新玩家或全部版本。",
        ]
    )
    if correlations:
        lines.extend(["", "## 结构与行为的探索相关", ""])
        for item in correlations[:8]:
            lines.append(
                f"- `{item['structure_metric']}` 与 `{item['behavior_score']}` "
                f"Spearman={item['spearman_correlation']:.2f}（n={item['level_count']}）。"
            )
        lines.append("以上是相关线索，不解释为因果。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_source(source: pd.DataFrame) -> None:
    required = {
        "cohort_player_rank",
        "user_id",
        "event_time",
        "client_version",
        "platform",
        "level_index",
        "level_id",
        "level_template_id",
        "level_play_count",
        "level_dup_para_id",
        "level_dup_para",
        "level_status",
        "level_duration_sec",
        "bag_tile_change_curve",
        "surface_normal_tile_change_curve",
        "replay_info",
    }
    missing = required.difference(source.columns)
    if missing:
        raise ValueError(f"输入缺少必需列: {sorted(missing)}")
    level_count = source["level_index"].nunique()
    player_count = source["user_id"].nunique()
    if player_count < 2 or level_count < 2:
        raise ValueError("分层至少需要2名玩家和2个关卡")


def quality_report(
    input_path: Path,
    config_dir: Path,
    template_dir: Path,
    source: pd.DataFrame,
    attempts: pd.DataFrame,
    player_levels: pd.DataFrame,
    levels: pd.DataFrame,
    instances: pd.DataFrame,
    templates: pd.DataFrame,
    hotspots: pd.DataFrame,
    correlations: Sequence[Mapping[str, Any]],
    output_dir: Path,
    structures: StructureFeatureCatalog,
) -> dict[str, Any]:
    template_paths = [
        template_dir / f"{template_id}_m.bytes"
        for template_id in sorted(source["level_template_id"].unique())
    ]
    reconstruction_counts = (
        attempts["reconstruction_status"].value_counts(dropna=False).to_dict()
    )
    return {
        "title": "100-199关行为分层初版",
        "source": {
            "input_file_name": input_path.name,
            "input_sha256": file_sha256(input_path),
            "date_min": str(source["event_date"].min()),
            "date_max": str(source["event_date"].max()),
            "config_files": {
                name: file_sha256(config_dir / name)
                for name in [
                    "config_level.csv",
                    "config_client_level_tile.csv",
                    "config_client_tile.csv",
                ]
            },
            "level_tile_pool_source": structures.level_tile_source,
            "level_tile_pool_sha256": structures.level_tile_sha256,
            "config_level_source": structures.config_level_source,
            "config_level_sha256": structures.config_level_sha256,
            "relevant_templates_combined_sha256": combined_sha256(template_paths),
        },
        "counts": {
            "source_rows": len(source),
            "attempts": len(attempts),
            "players": int(source["user_id"].nunique()),
            "levels": int(source["level_index"].nunique()),
            "player_levels": len(player_levels),
            "explicit_first_player_levels": int(
                player_levels["explicit_first_attempt"].sum()
            ),
            "timing_eligible_player_levels": int(
                player_levels["timing_anchor_eligible"].sum()
            ),
            "timing_right_censored_attempts": int(
                attempts["timing_right_censored"].sum()
            ),
            "curve_mismatch_attempts": int(
                (attempts["curve_click_aligned"] == 0).sum()
            ),
            "missing_click_targets": int(
                attempts["missing_click_target_count"].sum()
            ),
            "replay_targets": int(attempts["replay_target_count"].sum()),
            "unmapped_replay_targets": int(attempts["unmapped_target_count"].sum()),
            "static_surface_validation_attempts": int(
                attempts["static_surface_validation_eligible"].sum()
            ),
            "static_surface_validation_matched_attempts": int(
                pd.to_numeric(
                    attempts["static_surface_validation_match"], errors="coerce"
                ).sum()
            ),
            "static_surface_validation_points": int(
                attempts["static_surface_validation_point_count"].sum()
            ),
            "static_illegal_blocked_clicks": int(
                pd.to_numeric(
                    attempts["static_illegal_blocked_click_count"], errors="coerce"
                ).sum()
            ),
            "instances": len(instances),
            "templates": len(templates),
            "hotspot_rows": len(hotspots),
        },
        "reconstruction_status_counts": {
            str(key): int(value) for key, value in reconstruction_counts.items()
        },
        "method": {
            "analysis_unit": "先聚合玩家×level_index，再以玩家等权聚合关卡",
            "player_baseline": "留一关中位数/MAD，MAD=0时回退IQR/标准差",
            "difficulty_bands": "difficulty_score的100关四分位",
            "profile_tags": "非难度维度>=75百分位",
            "instance_shrinkage_prior_n": INSTANCE_PRIOR_N,
            "behavior_cluster": {
                "algorithm": "deterministic k-means, k=3, 80 starts",
                "axes": [
                    "pressure",
                    "slow_tempo",
                    "burst",
                    "late_slowdown",
                    "initial_visibility",
                    "reveal_activity",
                    "reveal_shift",
                ],
                "silhouette": float(levels["behavior_cluster_silhouette"].iloc[0]),
                "distribution": {
                    str(key): int(value)
                    for key, value in levels["behavior_cluster"].value_counts().items()
                },
            },
        },
        "limitations": [
            "样本全部为Android且已到300关以上，是高进度幸存玩家面板。",
            "明确首局缺失的玩家×关卡不进入首局失败与思考时长指标。",
            "含120000ms步骤的复盘时间是右删失下界，不进入玩家时间基线。",
            "C-N结构牌在旧客户端使用无固定种子分配，只保留结构组身份。",
            "第一版没有动态模拟塔、道具Handler和无道具可解性。",
            "相关性只用于选择后续假设，不解释为因果。",
        ],
        "top_structure_correlations": list(correlations[:20]),
        "outputs": {
            "attempt_features": str(output_dir / ATTEMPT_FILE),
            "player_level_features": str(output_dir / PLAYER_LEVEL_FILE),
            "level_stratification": str(output_dir / LEVEL_FILE),
            "instance_profiles": str(output_dir / INSTANCE_FILE),
            "template_features": str(output_dir / TEMPLATE_FILE),
            "position_hotspots": str(output_dir / HOTSPOT_FILE),
            "report": str(output_dir / REPORT_FILE),
        },
    }


def build_workbook_manifest(
    staging_dir: Path,
    output_dir: Path,
    quality: Mapping[str, Any],
    levels: pd.DataFrame,
    instances: pd.DataFrame,
    templates: pd.DataFrame,
    hotspots: pd.DataFrame,
    player_levels: pd.DataFrame,
    correlations: Sequence[Mapping[str, Any]],
) -> Path:
    staging_dir.mkdir(parents=True, exist_ok=True)
    workbook_frames = {
        "levels": levels,
        "instances": instances,
        "templates": templates,
        "hotspots": hotspots,
        "player_levels": player_levels,
        "correlations": pd.DataFrame(correlations),
        "dictionary": pd.DataFrame(metric_dictionary()),
    }
    sheets = {}
    for name, frame in workbook_frames.items():
        ndjson_path = staging_dir / f"{name}.ndjson"
        columns = list(frame.columns)
        write_ndjson(ndjson_path, frame, columns)
        sheets[name] = {
            "path": str(ndjson_path),
            "columns": columns,
            "row_count": len(frame),
        }
    band_summary = (
        levels.groupby("difficulty_band", sort=True)
        .agg(
            level_count=("level_index", "count"),
            median_first_fail_rate=("first_fail_rate", "median"),
            median_extra_space_rate=("extra_space_user_rate", "median"),
        )
        .reset_index()
        .to_dict("records")
    )
    profile_summary = (
        levels.groupby("behavior_profile", sort=True)
        .agg(
            level_count=("level_index", "count"),
            median_difficulty=("difficulty_score", "median"),
            median_pressure=("pressure_score", "median"),
            median_tool_dependency=("tool_dependency_score", "median"),
        )
        .reset_index()
        .sort_values(["level_count", "behavior_profile"], ascending=[False, True])
        .to_dict("records")
    )
    cluster_summary = (
        levels.groupby("behavior_cluster", sort=True)
        .agg(
            level_count=("level_index", "count"),
            median_difficulty=("difficulty_score", "median"),
            median_pressure=("pressure_score", "median"),
            median_confidence=("behavior_cluster_confidence", "median"),
        )
        .reset_index()
        .to_dict("records")
    )
    priority_columns = [
        "level_index",
        "difficulty_band",
        "behavior_profile",
        "difficulty_score",
        "pressure_score",
        "comprehension_score",
        "tool_dependency_score",
        "disagreement_score",
        "first_fail_rate",
        "review_priority_score",
    ]
    priority = (
        levels.sort_values(
            ["high_signal_dimension_count", "review_priority_score"],
            ascending=False,
        )
        .head(12)[priority_columns]
        .to_dict("records")
    )
    manifest = {
        "title": quality["title"],
        "source_file_name": quality["source"]["input_file_name"],
        "quality": quality,
        "sheets": sheets,
        "band_summary": band_summary,
        "profile_summary": profile_summary,
        "cluster_summary": cluster_summary,
        "priority_levels": priority,
        "output_workbook": str(output_dir / "关卡分层报告.xlsx"),
    }
    path = staging_dir / "manifest.json"
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def save_outputs(
    output_dir: Path,
    attempts: pd.DataFrame,
    player_levels: pd.DataFrame,
    levels: pd.DataFrame,
    instances: pd.DataFrame,
    templates: pd.DataFrame,
    hotspots: pd.DataFrame,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    attempts.to_csv(
        output_dir / ATTEMPT_FILE,
        index=False,
        encoding="utf-8-sig",
        compression="gzip",
    )
    player_levels.to_csv(
        output_dir / PLAYER_LEVEL_FILE, index=False, encoding="utf-8-sig"
    )
    levels.to_csv(output_dir / LEVEL_FILE, index=False, encoding="utf-8-sig")
    instances.to_csv(output_dir / INSTANCE_FILE, index=False, encoding="utf-8-sig")
    templates.to_csv(output_dir / TEMPLATE_FILE, index=False, encoding="utf-8-sig")
    hotspots.to_csv(
        output_dir / HOTSPOT_FILE,
        index=False,
        encoding="utf-8-sig",
        compression="gzip",
    )


def run_analysis(args: argparse.Namespace) -> dict[str, Any]:
    input_path = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    config_dir = Path(args.config_dir).expanduser().resolve()
    template_dir = Path(args.template_dir).expanduser().resolve()
    template_reader_dir = Path(args.template_reader_dir).expanduser().resolve()
    staging_dir = (
        Path(args.staging_dir).expanduser().resolve()
        if args.staging_dir
        else output_dir / "_workbook_staging"
    )

    source = pd.read_csv(input_path, dtype=str, keep_default_na=False)
    validate_source(source)
    source["level_index"] = pd.to_numeric(source["level_index"], errors="raise").astype(int)
    structures = StructureFeatureCatalog(
        config_dir,
        template_dir,
        template_reader_dir,
        args.level_tile_git_revision,
        args.config_level_git_revision,
    )
    print(f"[1/6] 解析 {len(source):,} 局回放与初始结构", flush=True)
    attempts, hotspots = extract_attempts(source, structures)
    print("[2/6] 合并玩家×关卡并计算留一关基线", flush=True)
    player_levels = aggregate_player_levels(attempts)
    print("[3/6] 生成关卡六维坐标与难度四层", flush=True)
    levels = aggregate_levels(player_levels)
    print("[4/6] 生成配置实例收缩画像", flush=True)
    instances = aggregate_instances(attempts, levels)
    templates = build_template_frame(structures)
    correlations = structure_correlations(levels)
    print("[5/6] 写出 CSV 与质量报告", flush=True)
    save_outputs(
        output_dir,
        attempts,
        player_levels,
        levels,
        instances,
        templates,
        hotspots,
    )
    quality = quality_report(
        input_path,
        config_dir,
        template_dir,
        source,
        attempts,
        player_levels,
        levels,
        instances,
        templates,
        hotspots,
        correlations,
        output_dir,
        structures,
    )
    (output_dir / QUALITY_FILE).write_text(
        json.dumps(quality, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_report(output_dir / REPORT_FILE, levels, quality, correlations)
    manifest_path = build_workbook_manifest(
        staging_dir,
        output_dir,
        quality,
        levels,
        instances,
        templates,
        hotspots,
        player_levels,
        correlations,
    )
    print("[6/6] 分析数据准备完成", flush=True)
    return {
        "manifest": str(manifest_path),
        "output_dir": str(output_dir),
        "workbook": str(output_dir / "关卡分层报告.xlsx"),
        "counts": quality["counts"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Redash导出CSV")
    parser.add_argument("--output-dir", required=True, help="结果输出目录")
    parser.add_argument(
        "--staging-dir", help="工作簿NDJSON中间数据目录"
    )
    parser.add_argument("--config-dir", default=str(DEFAULT_CONFIG_DIR))
    parser.add_argument("--template-dir", default=str(DEFAULT_TEMPLATE_DIR))
    parser.add_argument(
        "--template-reader-dir", default=str(DEFAULT_TEMPLATE_READER_DIR)
    )
    parser.add_argument(
        "--level-tile-git-revision",
        default=DEFAULT_LEVEL_TILE_GIT_REVISION,
        help=(
            "config_client_level_tile.csv 历史快照提交；"
            "空字符串表示使用当前工作区"
        ),
    )
    parser.add_argument(
        "--config-level-git-revision",
        default=DEFAULT_CONFIG_LEVEL_GIT_REVISION,
        help=(
            "config_level.csv 历史快照提交；"
            "空字符串表示使用当前工作区"
        ),
    )
    return parser


def main() -> int:
    result = run_analysis(build_parser().parse_args())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
