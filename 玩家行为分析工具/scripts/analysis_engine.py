"""Reusable analysis engine behind the local player-behaviour web tool.

Public entry points are intentionally server-friendly and JSON serialisable:

* ``preflight_player`` validates/merges one or more replay CSV exports.
* ``run_player_analysis`` runs the established v3 real-time behaviour pipeline
  and writes the five-sheet phase-one workbook.
* ``preflight_online`` validates the online level aggregate against phase one.
* ``run_online_analysis`` adds consumption/failed-attrition bands, T0-T7 tags
  and a 100-slot recommendation workbook.

The heavy replay reconstruction is vendored under ``scripts/analysis`` so the
tool is self-contained.  Runtime writes are restricted to the job directories
supplied by the caller.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from xlsx_writer import inspect_xlsx, write_stage1_workbook, write_stage2_workbook


ProgressCallback = Callable[..., None]
LogCallback = Callable[..., None]

ANALYSIS_DIR = Path(__file__).resolve().parent / "analysis"
DEFAULT_CONFIG_DIR = Path.home() / "ulinkconfig" / "csv"
DEFAULT_TEMPLATE_DIR = (
    Path.home() / "tile" / "Tile" / "Assets" / "Resources" / "TileMatchLevelData"
)
DEFAULT_TEMPLATE_READER_DIR = Path(
    ANALYSIS_DIR
)

PLAYER_REQUIRED_COLUMNS = {
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

ONLINE_REQUIRED_COLUMNS = {
    "level_index",
    "level_id",
    "level_template_id",
    "进入人数",
    "失败流失",
    "人均消耗",
}

REQUIRED_CONFIG_FILES = (
    "config_level.csv",
    "config_level_list.csv",
    "config_level_value_test.csv",
    "config_client_level_tile.csv",
    "config_client_tile.csv",
    "config_level_randomSeed_group.csv",
    "config_level_randomSeed.csv",
    # ConfigCatalog reads these while translating prop IDs.  Historical mode
    # must snapshot them too; otherwise a replay could silently mix a past
    # level layout with present-day asset definitions.
    "config_server_asset.csv",
    "config_client_asset_show.csv",
)

PLAYER_INSTANCE_NONEMPTY_COLUMNS = (
    "user_id",
    "level_index",
    "level_template_id",
    "level_dup_para_id",
    "level_dup_para",
)

NORMAL_OUTCOME_STATUSES = {
    "pass", "win", "success", "fail", "failed", "lose", "loss", "bag_full_fail",
}

ATTEMPT_ID_COLUMNS = [
    "user_id",
    "event_time",
    "level_index",
    "level_play_count",
    "level_template_id",
    "level_dup_para_id",
    "replay_info",
]

PRESSURE_ORDER = {
    "多数玩家遇到复合卡点": 4,
    "部分玩家遇到复合卡点": 3,
    "多数玩家遇到局部卡顿": 2,
    "部分玩家遇到局部卡顿": 1,
    "整体未见稳定高负担": 0,
}

PRESSURE_SHORT = {
    "多数玩家遇到复合卡点": "多数复合卡点",
    "部分玩家遇到复合卡点": "部分复合卡点",
    "多数玩家遇到局部卡顿": "多数局部卡顿",
    "部分玩家遇到局部卡顿": "部分局部卡顿",
    "整体未见稳定高负担": "未见稳定高负担",
}

TAG_DEFINITIONS = {
    "T0": ("顺畅过渡型", "未见稳定高负担，消耗和失败流失均不高"),
    "T1": ("非压力消耗型", "未见稳定高负担，但消耗偏高或极高"),
    "T2": ("轻度卡顿型", "部分玩家遇到局部卡顿，失败流失不高"),
    "T3": ("隐性流失型", "压力不高，但失败流失偏高或极高"),
    "T4": ("高压挑战型", "多数玩家遇到卡点，消耗和失败流失均不高"),
    "T5": ("有效消耗难关", "高压力且消耗偏高或极高，失败流失不高"),
    "T6": ("卡死流失型", "高压力、消耗不高，但失败流失偏高或极高"),
    "T7": ("超难高危型", "高压力，消耗和失败流失均偏高或极高"),
}

TAG_ACTIONS = {
    "T0": "保留，作为过渡或恢复关",
    "T1": "保留，复核消耗是否来自便利性使用",
    "T2": "保留，观察局部卡点是否符合预期",
    "T3": "建议排查：确认统计外卡点或关外流失原因",
    "T4": "保留，作为挑战关",
    "T5": "保留，优先安排在尾号5",
    "T6": "优先替换；若暂时保留，先降卡死点并放尾号5/9",
    "T7": "建议优化；降低失败流失后安排在尾号9",
}

# T0–T7保留为“过程压力×消耗×失败流失”的内部诊断码。策划主视图
# 使用下面更收敛的排关标签，避免把 T0 简单等同于“爽关”，也避免
# 将高压关拆成过多难以排关的名称。
PLANNER_TAG_DEFINITIONS = {
    "P0": ("轻松爽关", "稳定低负担玩家占比高，且未见稳定卡顿；优先放尾号0"),
    "P1": ("平淡过渡型", "结果风险不高，但也没有足够低负担证据支持爽感"),
    "P2": ("轻度卡顿型", "存在少量可复刻的局部卡顿或小谜题；优先放尾号2/3"),
    "P3": ("多谜题思考型", "多个局面需要思考或复合卡点稳定出现；优先放尾号4/8"),
    "P4": ("高压挑战型", "多数玩家稳定遭遇高负担，但失败流失尚未达高危；优先放尾号5"),
    "P5": ("超难高危型", "高压力且失败流失显著偏高，或高压、高消耗与高流失同时出现；适合尾号9并应先复核"),
    "PX": ("极端负面待优化", "多维高压力与极端失败流失同时稳定出现；建议先优化或替换，当前不给正式排位"),
}

PLANNER_TAG_ACTIONS = {
    "P0": "保留，作为每十关的爽感恢复点",
    "P1": "保留作过渡；若数量过多，建议增加记忆点或替换",
    "P2": "保留，放在轻度思考位",
    "P3": "保留，作为中高密度谜题位",
    "P4": "保留作挑战关，避免与高危关相邻",
    "P5": "超难候选；上线前复核失败热点与道具可解性",
    "PX": "已达到极端负面证据门槛；建议优化或替换，未完成处理前不进入正式新序列",
}


DEFAULT_SEQUENCE_STRATEGY = "dual_peak"

DEFAULT_LEVEL_LABEL_METHOD = "weighted_all_instances"
LEVEL_LABEL_METHODS: dict[str, dict[str, str]] = {
    "weighted_all_instances": {
        "code": "weighted_all_instances",
        "name": "全部首局实例玩家加权",
        "description": "按每个首局实例的玩家数加权，聚合为该进度位的整体行为标签。",
    },
    "dominant_online_duplicate": {
        "code": "dominant_online_duplicate",
        "name": "线上进入人数最多副本代表",
        "description": "取该关线上进入人数最多的副本，使用该副本首局实例的稳定标签作为关卡标签。",
    },
}

UNPLACED_PLANNER_CODES = {"PX", "PS"}

# Public contract consumed by the server/UI.  Keep codes stable: saved jobs and
# exported audits use them as reproducible algorithm identifiers.
SEQUENCE_STRATEGIES: dict[str, dict[str, Any]] = {
    "dual_peak": {
        "code": "dual_peak",
        "name": "双峰阶梯",
        "description": "沿用第一期方案：按风险升序切成十等份，尾号5承接难关、尾号9承接超难关，其余位置形成恢复与蓄压。",
        "bucket_to_digit": [0, 1, 2, 6, 3, 7, 4, 8, 5, 9],
        "ten_level_roles": ["恢复", "简单", "普通", "蓄压", "较难", "困难", "回落", "再蓄压", "难前铺垫", "超难"],
    },
    "spiral_flow": {
        "code": "spiral_flow",
        "name": "十关螺旋",
        "description": "以实时行为标签组成十关螺旋：0爽、2/3轻谜题、4/8多谜题、5高压挑战、9超难高危。",
        "bucket_to_digit": [0, 1, 6, 7, 2, 3, 4, 8, 5, 9],
        "ten_level_roles": ["爽", "过渡", "轻谜题", "轻谜题", "多谜题", "高压挑战", "过渡", "过渡", "多谜题", "超难高危"],
    },
    "single_ramp": {
        "code": "single_ramp",
        "name": "单峰渐进",
        "description": "每十关从尾号0到9持续抬升风险，尾号9达到峰值，下一组十关重新从低负担开始。",
        "bucket_to_digit": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
        "ten_level_roles": ["恢复", "简单", "偏简单", "普通", "普通", "普通难", "较难", "困难", "很难", "峰值"],
    },
    "diversity_guard": {
        "code": "diversity_guard",
        "name": "多样性保护",
        "description": "保留双峰阶梯的十个风险桶和尾号5/9角色，只在同一风险桶内部确定性调换，尽量减少相邻同模板及相邻T6/T7。",
        "bucket_to_digit": [0, 1, 2, 6, 3, 7, 4, 8, 5, 9],
        "ten_level_roles": ["恢复", "简单", "普通", "蓄压", "较难", "困难", "回落", "再蓄压", "难前铺垫", "超难"],
    },
}


class InputValidationError(ValueError):
    """Raised when a preflight has blocking errors."""

    def __init__(self, message: str, audit: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.audit = dict(audit or {})


def _notify(callback: ProgressCallback | None, progress: int, message: str) -> None:
    if callback is None:
        return
    try:
        callback(int(progress), message)
    except TypeError:
        callback({"progress": int(progress), "message": message})


def _log(callback: LogCallback | None, message: str) -> None:
    if callback is None:
        return
    try:
        callback(message)
    except TypeError:
        callback({"message": message})


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        missing = pd.isna(value)
        if isinstance(missing, (bool, np.bool_)) and bool(missing):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_json_safe(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _normalise_sources(source_files: Sequence[str | Path]) -> list[Path]:
    paths = [Path(path).expanduser().resolve() for path in source_files]
    if not paths:
        raise InputValidationError("请至少导入一个CSV文件")
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise InputValidationError(f"文件不存在: {missing}")
    return paths


def _read_csv(path: Path) -> pd.DataFrame:
    try:
        frame = pd.read_csv(path, dtype=str, keep_default_na=False, low_memory=False)
    except UnicodeDecodeError:
        frame = pd.read_csv(
            path, dtype=str, keep_default_na=False, low_memory=False, encoding="gb18030"
        )
    frame.columns = [str(column).lstrip("\ufeff").strip() for column in frame.columns]
    return frame


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_player_sources(
    source_files: Sequence[str | Path], work_dir: str | Path | None = None
) -> tuple[pd.DataFrame, dict[str, Any]]:
    paths = _normalise_sources(source_files)
    frames: list[pd.DataFrame] = []
    file_rows: list[dict[str, Any]] = []
    uid_sets: list[set[str]] = []
    effective_paths: list[Path] = []
    seen_digests: dict[str, str] = {}
    errors: list[str] = []
    warnings: list[str] = []
    normal_outcome_rows_before_replay_filter = 0
    skipped_empty_replay_rows = 0
    input_status_counts: dict[str, int] = {}
    reference_columns: set[str] | None = None
    for index, path in enumerate(paths):
        frame = _read_csv(path)
        digest = _file_digest(path)
        columns = set(frame.columns)
        missing = sorted(PLAYER_REQUIRED_COLUMNS - columns)
        if missing:
            errors.append(f"{path.name} 缺少必需列: {', '.join(missing)}")
        if reference_columns is None:
            reference_columns = columns
        elif columns != reference_columns:
            warnings.append(f"{path.name} 与第一个文件的列集不完全一致，已按列名对齐。")
        uid_set = set(frame.get("user_id", pd.Series(dtype=str)).astype(str)) - {""}
        level_values = pd.to_numeric(frame.get("level_index"), errors="coerce")
        file_rows.append(
            {
                "file_name": path.name,
                "path": str(path),
                "sha256": digest,
                "rows": int(len(frame)),
                "players": int(len(uid_set)),
                "level_count": int(level_values.nunique()),
                "level_min": int(level_values.min()) if level_values.notna().any() else None,
                "level_max": int(level_values.max()) if level_values.notna().any() else None,
                "column_count": int(len(frame.columns)),
                "missing_required_columns": missing,
                "duplicate_of": seen_digests.get(digest),
            }
        )
        if digest in seen_digests:
            warnings.append(f"{path.name} 与 {seen_digests[digest]} 完全相同，已忽略重复文件。")
            continue
        seen_digests[digest] = path.name
        uid_sets.append(uid_set)
        effective_paths.append(path)
        frame["__source_order"] = index
        frame["__source_row"] = np.arange(len(frame), dtype=int)
        frames.append(frame)

    overlaps: list[dict[str, Any]] = []
    for left_index in range(len(effective_paths)):
        for right_index in range(left_index + 1, len(effective_paths)):
            shared = uid_sets[left_index] & uid_sets[right_index]
            if shared:
                overlaps.append(
                    {
                        "left": effective_paths[left_index].name,
                        "right": effective_paths[right_index].name,
                        "overlap_player_count": len(shared),
                        "sample_user_ids": sorted(shared)[:10],
                    }
                )
    if overlaps:
        errors.append("不同导入文件存在重复UID；这会重复加权玩家并污染玩家内基线。请重新导出互不重复的批次。")

    if errors:
        merged = pd.DataFrame()
        duplicate_rows = 0
    else:
        merged = pd.concat(frames, ignore_index=True, sort=False)
        for column in PLAYER_REQUIRED_COLUMNS:
            merged[column] = merged[column].astype(str)
        duplicate_mask = merged.duplicated(ATTEMPT_ID_COLUMNS, keep="first")
        duplicate_rows = int(duplicate_mask.sum())
        merged = merged.loc[~duplicate_mask].copy()
        # Each query batch restarts cohort_player_rank; make it globally unique.
        uid_order = merged.drop_duplicates("user_id")["user_id"].tolist()
        rank_map = {uid: index for index, uid in enumerate(uid_order, start=1)}
        merged["cohort_player_rank"] = merged["user_id"].map(rank_map).astype(str)
        merged = merged.sort_values(
            ["cohort_player_rank", "level_index", "level_play_count", "event_time"],
            kind="stable",
        ).drop(columns=["__source_order", "__source_row"], errors="ignore")

        input_statuses = merged["level_status"].astype(str).str.strip().str.lower()
        input_status_counts = {
            str(key): int(value)
            for key, value in input_statuses.value_counts(dropna=False).items()
        }
        input_normal_outcome = input_statuses.isin(NORMAL_OUTCOME_STATUSES)
        normal_outcome_rows_before_replay_filter = int(input_normal_outcome.sum())
        if normal_outcome_rows_before_replay_filter == 0:
            errors.append(
                "未找到任何正常结算对局（pass/fail/bag_full_fail等）；"
                "全为exit/unknown的数据不能进行行为分层。"
            )
        replay_blank = merged["replay_info"].astype(str).str.strip().eq("")
        missing_normal_replay = input_normal_outcome & replay_blank
        skipped_empty_replay_rows = int(missing_normal_replay.sum())
        if skipped_empty_replay_rows:
            warnings.append(
                f"正常结算对局中有{skipped_empty_replay_rows}行replay_info为空；"
                "已跳过这些无法还原的行，其余有效对局仍会继续分析。"
            )
            merged = merged.loc[~missing_normal_replay].copy()

        level_text = merged["level_index"].astype(str).str.strip()
        numeric_level = pd.to_numeric(level_text, errors="coerce")
        integer_level = level_text.str.fullmatch(r"[+-]?\d+") & numeric_level.notna()
        if (~integer_level).any():
            errors.append(
                f"level_index存在{int((~integer_level).sum())}个非整数值；"
                "请保留原始整数关卡号。"
            )
        for column in PLAYER_INSTANCE_NONEMPTY_COLUMNS:
            empty = merged[column].astype(str).str.strip().eq("")
            if empty.any():
                errors.append(f"{column}存在{int(empty.sum())}行空值，无法构成可追溯关卡实例。")

        statuses = merged["level_status"].astype(str).str.strip().str.lower()
        normal_outcome = statuses.isin(NORMAL_OUTCOME_STATUSES)
        normal_outcome_rows = int(normal_outcome.sum())
        if normal_outcome_rows_before_replay_filter > 0 and normal_outcome_rows == 0:
            errors.append(
                "所有正常结算对局的replay_info均为空；跳过后没有可分析的实时过程。"
            )

    player_coverage: dict[str, Any] = {}
    if not merged.empty:
        coverage = merged.groupby("user_id")["level_index"].nunique()
        player_coverage = {
            "min": int(coverage.min()),
            "median": float(coverage.median()),
            "max": int(coverage.max()),
            "players_with_full_range": int((coverage == merged["level_index"].nunique()).sum()),
        }
        if coverage.min() < 20:
            warnings.append("至少一名玩家覆盖不足20个关卡号，玩家内基线稳定性可能偏低。")

    audit: dict[str, Any] = {
        "valid": not errors,
        "status": "error" if errors else ("warning" if warnings else "ok"),
        "errors": errors,
        "warnings": warnings,
        "files": file_rows,
        "source_file_count": len(paths),
        "effective_source_file_count": len(effective_paths),
        "raw_rows": int(sum(row["rows"] for row in file_rows)),
        "deduplicated_rows": duplicate_rows,
        "rows_after_deduplication": int(len(merged) + skipped_empty_replay_rows),
        "merged_rows": int(len(merged)),
        "unique_players": int(merged["user_id"].nunique()) if not merged.empty else 0,
        "unique_levels": int(merged["level_index"].nunique()) if not merged.empty else 0,
        "player_level_coverage": player_coverage,
        "uid_overlaps": overlaps,
        "normal_outcome_rows": normal_outcome_rows if not merged.empty else 0,
        "normal_outcome_rows_before_replay_filter": normal_outcome_rows_before_replay_filter,
        "skipped_empty_replay_rows": skipped_empty_replay_rows,
        "excluded_outcome_rows": int(len(merged) - normal_outcome_rows) if not merged.empty else 0,
        "level_status_counts": input_status_counts,
        "instance_key": "level_index+level_template_id+level_dup_para_id+canonical(DupPara)",
        "attempt_dedup_key": ATTEMPT_ID_COLUMNS,
    }
    if work_dir and not merged.empty:
        work = Path(work_dir)
        work.mkdir(parents=True, exist_ok=True)
        merged_path = work / "merged_player_input.csv"
        merged.to_csv(merged_path, index=False, encoding="utf-8-sig")
        audit["merged_csv_path"] = str(merged_path)
        _write_json(work / "player_input_audit.json", audit)
    return merged, audit


def _settings_path(settings: Mapping[str, Any] | None, key: str, default: Path) -> Path:
    aliases = {"config_dir": "config_path", "template_dir": "template_path"}
    value = (settings or {}).get(key) or (settings or {}).get(aliases.get(key, "")) or default
    return Path(str(value)).expanduser().resolve()


def _resolve_config_dir(settings: Mapping[str, Any] | None) -> Path:
    configured = _settings_path(settings, "config_dir", DEFAULT_CONFIG_DIR)
    if not (configured / "config_level.csv").is_file() and (configured / "csv/config_level.csv").is_file():
        return configured / "csv"
    return configured


def _git_revision_audit(config_dir: Path, settings: Mapping[str, Any] | None) -> dict[str, Any]:
    values = settings or {}
    history_ref = str(values.get("history_ref") or "").strip()
    history_mode = str(values.get("history_mode") or history_ref).strip().lower()
    fallback_ref = "" if history_ref.lower() in {"", "current", "working_tree", "latest", "historical", "default", "fixed"} else history_ref
    revisions = {
        "config_level_git_revision": str(values.get("config_level_git_revision") or fallback_ref).strip(),
        "level_tile_git_revision": str(values.get("level_tile_git_revision") or fallback_ref).strip(),
    }
    historical_requested = history_mode in {"historical", "history", "fixed", "snapshot"} or any(revisions.values())
    report: dict[str, Any] = {
        "enabled": historical_requested,
        "revisions": {},
        "files": {},
    }
    if not report["enabled"]:
        report["mode"] = "current_worktree"
        return report
    repo = config_dir.parent if config_dir.name == "csv" else config_dir
    report["mode"] = "historical_snapshot"
    report["repository"] = str(repo)
    for key, revision in revisions.items():
        if not revision:
            report["revisions"][key] = {"revision": "", "exists": False, "resolved_commit": ""}
            continue
        commit = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        report["revisions"][key] = {
            "revision": revision, "exists": commit.returncode == 0,
            "resolved_commit": commit.stdout.strip() if commit.returncode == 0 else "",
        }
    try:
        config_prefix = config_dir.relative_to(repo).as_posix().strip("/")
    except ValueError:
        config_prefix = "csv"
    for name in REQUIRED_CONFIG_FILES:
        revision_key = (
            "config_level_git_revision"
            if name == "config_level.csv"
            else "level_tile_git_revision"
        )
        revision = revisions[revision_key]
        git_path = f"{config_prefix}/{name}" if config_prefix else name
        file_check = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{revision}:{git_path}"],
            capture_output=True, check=False,
        ) if revision else None
        report["files"][name] = {
            "revision_key": revision_key,
            "revision": revision,
            "git_path": git_path,
            "exists": bool(file_check is not None and file_check.returncode == 0),
        }
    return report


def _materialize_config_snapshot(
    config_dir: Path,
    settings: Mapping[str, Any] | None,
    work_dir: str | Path,
    git_audit: Mapping[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Return a self-contained config directory and its immutable manifest.

    In historical mode every required file comes from Git: config_level.csv
    uses the configured A revision and every other table uses B.  The returned
    directory therefore cannot fall through to the current working tree.
    """
    report = dict(git_audit or _git_revision_audit(config_dir, settings))
    if report.get("mode") != "historical_snapshot":
        files = {
            name: {
                "revision": "current_worktree",
                "source_path": str(config_dir / name),
                "sha256": _file_digest(config_dir / name),
            }
            for name in REQUIRED_CONFIG_FILES
        }
        return config_dir, {
            "mode": "current_worktree",
            "config_dir": str(config_dir),
            "files": files,
        }

    failures: list[str] = []
    for key, item in report.get("revisions", {}).items():
        if not item.get("exists"):
            failures.append(f"{key}不存在或不是可用提交: {item.get('revision', '')}")
    for name, item in report.get("files", {}).items():
        if not item.get("exists"):
            failures.append(
                f"历史提交{item.get('revision', '')}缺少{item.get('git_path', name)}"
            )
    if failures:
        raise InputValidationError("无法生成完整历史配置快照", {"errors": failures, **report})

    repo = Path(str(report["repository"]))
    snapshot_dir = Path(work_dir) / "config_snapshot"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    manifest_files: dict[str, Any] = {}
    for name in REQUIRED_CONFIG_FILES:
        source_info = dict(report["files"][name])
        revision = str(source_info["revision"])
        git_path = str(source_info["git_path"])
        command = subprocess.run(
            ["git", "-C", str(repo), "show", f"{revision}:{git_path}"],
            capture_output=True,
            check=False,
        )
        if command.returncode != 0:
            message = command.stderr.decode("utf-8", errors="replace").strip()
            raise InputValidationError(
                f"历史配置快照读取失败: {name}: {message}",
                {"errors": [message], **report},
            )
        target = snapshot_dir / name
        target.write_bytes(command.stdout)
        manifest_files[name] = {
            "revision": revision,
            "resolved_commit": report["revisions"][source_info["revision_key"]].get("resolved_commit", ""),
            "git_path": git_path,
            "path": str(target),
            "sha256": hashlib.sha256(command.stdout).hexdigest(),
            "size_bytes": len(command.stdout),
        }
    manifest = {
        "mode": "historical_snapshot",
        "repository": str(repo),
        "config_dir": str(snapshot_dir),
        "files": manifest_files,
    }
    _write_json(snapshot_dir / "snapshot_manifest.json", manifest)
    return snapshot_dir, manifest


def preflight_player(
    source_files: Sequence[str | Path],
    settings: Mapping[str, Any] | None,
    work_dir: str | Path,
) -> dict[str, Any]:
    """Validate player exports, UID overlap and local reconstruction resources."""
    _merged, audit = _load_player_sources(source_files, work_dir)
    config_dir = _resolve_config_dir(settings)
    template_dir = _settings_path(settings, "template_dir", DEFAULT_TEMPLATE_DIR)
    reader_dir = _settings_path(settings, "template_reader_dir", DEFAULT_TEMPLATE_READER_DIR)
    resources = {
        "config_dir": {"path": str(config_dir), "exists": config_dir.is_dir()},
        "template_dir": {"path": str(template_dir), "exists": template_dir.is_dir()},
        "template_reader_dir": {"path": str(reader_dir), "exists": reader_dir.is_dir()},
    }
    audit["resources"] = resources
    for name, resource in resources.items():
        if not resource["exists"]:
            audit["errors"].append(f"{name}路径不存在: {resource['path']}")
    git_audit = _git_revision_audit(config_dir, settings)
    audit["git_snapshot"] = git_audit
    config_files = {
        name: {"path": str(config_dir / name), "exists": (config_dir / name).is_file()}
        for name in REQUIRED_CONFIG_FILES
    }
    audit["config_files"] = config_files
    missing_config = [name for name, item in config_files.items() if not item["exists"]]
    if missing_config and git_audit.get("mode") != "historical_snapshot":
        audit["errors"].append(f"配置目录缺少文件: {', '.join(missing_config)}")
    merged_path = audit.get("merged_csv_path")
    if merged_path and template_dir.is_dir():
        merged = _read_csv(Path(str(merged_path)))
        template_ids = sorted(set(merged["level_template_id"].astype(str)) - {""})
        missing_templates = [
            template_id for template_id in template_ids
            if not (template_dir / f"{template_id}_m.bytes").is_file()
        ]
        audit["template_coverage"] = {
            "required_template_count": len(template_ids),
            "found_template_count": len(template_ids) - len(missing_templates),
            "missing_template_ids": missing_templates[:100],
            "missing_template_count": len(missing_templates),
        }
        if missing_templates:
            audit["errors"].append(
                f"模板目录缺少{len(missing_templates)}个导入数据需要的模板；前20个: {missing_templates[:20]}"
            )
    if git_audit.get("enabled"):
        for key, item in git_audit.get("revisions", {}).items():
            if not item.get("exists"):
                audit["errors"].append(f"{key}不存在或不是可用提交: {item.get('revision')}")
        for name, item in git_audit.get("files", {}).items():
            if not item.get("exists"):
                audit["errors"].append(
                    f"历史提交{item.get('revision')}中缺少必需文件: {item.get('git_path', name)}"
                )
    if audit["errors"]:
        audit["status"] = "error"
        audit["valid"] = False
    elif audit["warnings"]:
        audit["status"] = "warning"
        audit["valid"] = True
    _write_json(Path(work_dir) / "player_input_audit.json", audit)
    return _json_safe(audit)


def _load_pipeline_modules() -> tuple[Any, Any, Any, Any]:
    if str(ANALYSIS_DIR) not in sys.path:
        sys.path.insert(0, str(ANALYSIS_DIR))
    import analyze_level_behavior_v3 as v3
    import analyze_level_stratification as legacy
    import analyze_level_stratification_v2 as v2
    import build_full_cohort_conclusion as full
    for module in (v3, legacy, v2, full):
        module_path = Path(module.__file__).resolve()
        if ANALYSIS_DIR not in module_path.parents:
            raise RuntimeError(f"分析模块未从工具内置目录加载: {module_path}")
    return v3, legacy, v2, full


def _sample_summary(frame: pd.DataFrame, normal_status: Callable[[Any], bool]) -> dict[str, Any]:
    valid = frame[frame["level_status"].map(normal_status)].copy()
    players = frame.drop_duplicates("user_id")
    replay = valid["replay_info"].fillna("").astype(str)
    def numeric(column: str) -> pd.Series:
        return pd.to_numeric(players.get(column, pd.Series(dtype=float)), errors="coerce")
    max_levels = numeric("cohort_max_level")
    max_lt = numeric("cohort_max_lt")
    observed_level_count = int(frame["level_index"].nunique())
    observed_coverage = frame.groupby("user_id")["level_index"].nunique()
    return {
        "players": int(frame["user_id"].nunique()),
        "level_indices": int(frame["level_index"].nunique()),
        "raw_attempt_rows": int(len(frame)),
        "valid_outcome_attempts": int(len(valid)),
        "excluded_exit_attempts": int(len(frame) - len(valid)),
        "decision_actions": int((replay.str.count(r"\|") + replay.ne("").astype(int)).sum()),
        "date_min": str(frame.get("event_date", pd.Series([""])).min()),
        "date_max": str(frame.get("event_date", pd.Series([""])).max()),
        "product": "、".join(sorted(frame.get("product", pd.Series(dtype=str)).dropna().astype(str).unique())),
        "platform": "、".join(sorted(frame.get("platform", pd.Series(dtype=str)).dropna().astype(str).unique())),
        "client_version_min": str(frame["client_version"].replace("", np.nan).dropna().min()),
        "client_version_max": str(frame["client_version"].replace("", np.nan).dropna().max()),
        "player_max_level_min": int(max_levels.min()) if max_levels.notna().any() else 0,
        "player_max_level_median": float(max_levels.median()) if max_levels.notna().any() else 0,
        "player_max_lt_min": int(max_lt.min()) if max_lt.notna().any() else 0,
        # Keep the legacy key for workbook compatibility, but calculate against
        # the actually imported target range rather than a hard-coded 100.
        "players_covering_all_100_levels": int((observed_coverage == observed_level_count).sum()),
        "target_level_count": observed_level_count,
        "target_level_min": int(pd.to_numeric(frame["level_index"], errors="coerce").min()),
        "target_level_max": int(pd.to_numeric(frame["level_index"], errors="coerce").max()),
    }


def estimate_sample_recommendation(
    instances: pd.DataFrame,
    levels: pd.DataFrame,
    current_players: int,
) -> dict[str, Any]:
    """Estimate the next cohort size from the current first-attempt instance mix.

    A level becomes classifiable when at least 70% of its player weight belongs
    to instances with at least 10 players.  The estimate keeps the observed
    instance allocation proportions, finds the smallest extra cohort that
    crosses that rule for every pending level, then adds a 25% allocation
    buffer and rounds up to a practical batch of ten players.
    """
    pending_levels = set(
        pd.to_numeric(
            levels.loc[
                levels.get("first_level_behavior_label", pd.Series(dtype=str))
                .astype(str)
                .eq("样本覆盖不足"),
                "level_index",
            ],
            errors="coerce",
        ).dropna().astype(int)
    )
    if not pending_levels:
        return {
            "next_action": "online_metrics",
            "pending_level_count": 0,
            "recommended_additional_players": 0,
            "recommended_total_players": int(current_players),
            "basis": "所有关卡的可分类实例玩家权重均已达70%",
        }

    first = instances[
        instances.get("attempt_phase", pd.Series(dtype=str)).astype(str).eq("首局")
        & pd.to_numeric(instances.get("level_index"), errors="coerce").isin(pending_levels)
    ].copy()
    per_level_need: dict[int, int] = {}
    search_limit = max(500, int(current_players) * 10)
    for level_index, frame in first.groupby("level_index"):
        counts = pd.to_numeric(frame.get("player_count"), errors="coerce").dropna()
        counts = counts[counts > 0].astype(float)
        total = float(counts.sum())
        if not total:
            continue
        for additional in range(search_limit + 1):
            expected = counts * (1.0 + additional / total)
            coverage = float(expected[expected >= 10.0].sum() / expected.sum())
            if coverage >= 0.70:
                per_level_need[int(level_index)] = additional
                break

    if per_level_need:
        raw_need = max(per_level_need.values())
    else:
        pending_share = len(pending_levels) / max(1, int(levels["level_index"].nunique()))
        expansion = 0.5 if pending_share >= 0.5 else 0.3 if pending_share >= 0.2 else 0.2
        raw_need = math.ceil(max(1, current_players) * expansion)
    recommended = max(20, int(math.ceil(raw_need * 1.25 / 10.0) * 10))
    return {
        "next_action": "add_player_sample",
        "pending_level_count": len(pending_levels),
        "recommended_additional_players": recommended,
        "recommended_total_players": int(current_players) + recommended,
        "raw_estimated_additional_players": int(raw_need),
        "allocation_buffer": 0.25,
        "basis": "按当前首局实例分配比例估算；实例至少10人且关卡可分类权重至少70%",
    }


def run_player_analysis(
    source_files: Sequence[str | Path],
    settings: Mapping[str, Any] | None,
    work_dir: str | Path,
    output_dir: str | Path,
    progress_callback: ProgressCallback | None = None,
    log_callback: LogCallback | None = None,
) -> dict[str, Any]:
    """Run phase one and write CSV evidence plus the five-sheet workbook."""
    work, output = Path(work_dir), Path(output_dir)
    work.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    _notify(progress_callback, 2, "正在校验玩家数据与UID重复")
    audit = preflight_player(source_files, settings, work)
    if audit.get("errors"):
        raise InputValidationError("玩家数据预检查未通过", audit)
    merged_path = Path(str(audit["merged_csv_path"]))
    source_config_dir = _resolve_config_dir(settings)
    config_dir, config_manifest = _materialize_config_snapshot(
        source_config_dir, settings, work, audit.get("git_snapshot")
    )
    audit["config_snapshot"] = config_manifest
    _write_json(work / "player_input_audit.json", audit)
    template_dir = _settings_path(settings, "template_dir", DEFAULT_TEMPLATE_DIR)
    reader_dir = _settings_path(settings, "template_reader_dir", DEFAULT_TEMPLATE_READER_DIR)
    v3, legacy, v2, full = _load_pipeline_modules()
    analysis_dir = work / "v3_analysis"
    staging_dir = work / "v3_staging"
    _notify(progress_callback, 8, "正在解析回放并还原每步局面")
    _log(log_callback, f"使用模板目录: {template_dir}")
    _log(log_callback, f"使用配置目录: {config_dir} ({config_manifest['mode']})")
    args = argparse.Namespace(
        input=str(merged_path), output_dir=str(analysis_dir), staging_dir=str(staging_dir),
        config_dir=str(config_dir), template_dir=str(template_dir), template_reader_dir=str(reader_dir),
        # Historical revisions were already materialized file-by-file above.
        # Empty values prevent the vendored pipeline from consulting Git again.
        level_tile_git_revision="",
        config_level_git_revision="",
    )
    v3_result = v3.run(args)
    _notify(progress_callback, 78, "正在建立玩家内基线与跨玩家稳定性")
    profiles = pd.read_csv(analysis_dir / v3.INSTANCE_FILE)
    evidence = pd.read_csv(analysis_dir / v3.DIMENSION_FILE)
    instances = full.build_all_cohort_instances(profiles, evidence)
    levels_long = full.build_level_summary(instances)
    levels = full.build_level_table(levels_long)
    metrics = full.build_metric_table(evidence)
    raw = _read_csv(merged_path)
    sample = _sample_summary(raw, v2.is_normal_outcome_status)
    summary = full.build_summary(sample, instances, levels_long, metrics, evidence)
    summary["sample_recommendation"] = estimate_sample_recommendation(
        instances, levels, int(sample["players"])
    )
    target_range = f"{sample['target_level_min']}至{sample['target_level_max']}关"
    if isinstance(summary.get("deployment_assessment"), Mapping):
        summary["deployment_assessment"]["current_range"] = (
            f"可用于{target_range}的行为分层和策划验收排序；其他范围需另行检查样本覆盖。"
        )
    summary["input_audit"] = audit
    summary["config_snapshot"] = config_manifest
    summary = _json_safe(summary)

    level_path = output / "关卡号分层.csv"
    instance_path = output / "实例分层.csv"
    metric_path = output / "行为维度稳定性.csv"
    summary_path = output / "分析摘要.json"
    workbook_path = output / "玩家行为分层结论.xlsx"
    levels.to_csv(level_path, index=False, encoding="utf-8-sig")
    instances.to_csv(instance_path, index=False, encoding="utf-8-sig")
    metrics.to_csv(metric_path, index=False, encoding="utf-8-sig")
    _write_json(summary_path, summary)
    _notify(progress_callback, 92, "正在写出五张表结论工作簿")
    write_stage1_workbook(
        workbook_path, levels=levels, instances=instances, metrics=metrics,
        summary=summary, title="玩家行为分层第一期结论",
        subtitle=f"{sample['players']}名玩家，{sample['date_min']}至{sample['date_max']}；四键实例与玩家内基线口径",
    )
    workbook_audit = inspect_xlsx(workbook_path)
    _notify(progress_callback, 100, "第一阶段分析完成")
    result = {
        "status": "completed",
        "summary": summary,
        "workbook_path": str(workbook_path),
        "levels_csv_path": str(level_path),
        "instances_csv_path": str(instance_path),
        "metrics_csv_path": str(metric_path),
        "summary_path": str(summary_path),
        "audit": audit,
        "config_snapshot": config_manifest,
        "workbook_audit": workbook_audit,
        "counts": v3_result.get("counts", {}),
        "coverage": v3_result.get("coverage", {}),
        "first_level_label_counts": summary.get("first_level_label_counts", {}),
    }
    _write_json(output / "result.json", result)
    return _json_safe(result)


def _find_nested(payload: Any, key: str) -> Any:
    if isinstance(payload, Mapping):
        if key in payload:
            return payload[key]
        for value in payload.values():
            found = _find_nested(value, key)
            if found is not None:
                return found
    return None


def _resolve_level_labels(player_job: Any) -> Path:
    if isinstance(player_job, (str, Path)):
        path = Path(player_job)
        if path.is_file():
            return path
    for key in ("levels_csv_path", "level_csv_path", "stage1_levels_csv"):
        value = _find_nested(player_job, key)
        if value:
            path = Path(str(value)).expanduser().resolve()
            if path.is_file():
                return path
    raise InputValidationError("未找到第一阶段的关卡号分层CSV。")


def _resolve_instance_labels(player_job: Any) -> Path | None:
    for key in ("instances_csv_path", "instance_csv_path", "stage1_instances_csv"):
        value = _find_nested(player_job, key)
        if value:
            path = Path(str(value)).expanduser().resolve()
            if path.is_file():
                return path
    level_path = _resolve_level_labels(player_job)
    sibling = level_path.with_name("实例分层.csv")
    return sibling if sibling.is_file() else None


def _parse_percent(series: pd.Series) -> pd.Series:
    text = series.astype(str).str.strip()
    percent_mask = text.str.endswith("%")
    numeric = pd.to_numeric(text.str.rstrip("%"), errors="coerce").astype(float)
    numeric.loc[percent_mask] = numeric.loc[percent_mask] / 100.0
    return numeric


def _load_online_sources(source_files: Sequence[str | Path]) -> tuple[pd.DataFrame, dict[str, Any]]:
    paths = _normalise_sources(source_files)
    frames: list[pd.DataFrame] = []
    files: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []
    for path in paths:
        frame = _read_csv(path)
        missing = sorted(ONLINE_REQUIRED_COLUMNS - set(frame.columns))
        if missing:
            errors.append(f"{path.name} 缺少必需列: {', '.join(missing)}")
        files.append({"file_name": path.name, "rows": len(frame), "columns": len(frame.columns), "missing_required_columns": missing, "sha256": _file_digest(path)})
        frames.append(frame)
    if errors:
        merged = pd.DataFrame()
        duplicates = 0
    else:
        merged = pd.concat(frames, ignore_index=True, sort=False)
        before = len(merged)
        merged = merged.drop_duplicates().copy()
        duplicates = before - len(merged)
        merged["level_index"] = pd.to_numeric(merged["level_index"], errors="coerce")
        merged["进入人数"] = pd.to_numeric(merged["进入人数"].astype(str).str.replace(",", "", regex=False), errors="coerce")
        merged["人均消耗"] = pd.to_numeric(merged["人均消耗"], errors="coerce")
        merged["失败流失"] = _parse_percent(merged["失败流失"])
        invalid = merged[["level_index", "进入人数", "人均消耗", "失败流失"]].isna().any(axis=1)
        if invalid.any():
            errors.append(f"线上关卡数据有{int(invalid.sum())}行关键数值无法解析。")
        noninteger_levels = merged["level_index"].notna() & (merged["level_index"] % 1 != 0)
        if noninteger_levels.any():
            errors.append(f"线上关卡数据有{int(noninteger_levels.sum())}行level_index不是整数。")
        nonpositive = merged["进入人数"].fillna(0) <= 0
        if nonpositive.any():
            errors.append(f"线上关卡数据有{int(nonpositive.sum())}行进入人数不大于0。")
        negative_consume = merged["人均消耗"].notna() & (merged["人均消耗"] < 0)
        if negative_consume.any():
            errors.append(f"线上关卡数据有{int(negative_consume.sum())}行人均消耗小于0。")
        invalid_fail_loss = merged["失败流失"].notna() & ~merged["失败流失"].between(0, 1)
        if invalid_fail_loss.any():
            errors.append(f"线上关卡数据有{int(invalid_fail_loss.sum())}行失败流失不在0–1范围内。")
        empty_ids = (
            merged["level_id"].fillna("").astype(str).str.strip().eq("")
            | merged["level_template_id"].fillna("").astype(str).str.strip().eq("")
        )
        if empty_ids.any():
            errors.append(f"线上关卡数据有{int(empty_ids.sum())}行关卡ID或模板ID为空。")

        # The dashboard can expose several aggregation dimensions at once.
        # Combining multiple groups would double-count the same players, so one
        # export must represent one fixed slice for each present dimension.
        for column in (
            "user_info",
            "strategy_group_id",
            "strategy_effective_list",
            "strategy_failed_list",
        ):
            if column not in merged.columns:
                continue
            values = sorted(
                value for value in merged[column].fillna("").astype(str).str.strip().unique()
                if value
            )
            if len(values) > 1:
                preview = ", ".join(values[:5])
                suffix = "…" if len(values) > 5 else ""
                errors.append(
                    f"线上关卡数据的{column}混入{len(values)}种聚合口径（{preview}{suffix}）；"
                    "请在看板中固定为单一筛选后重新导出。"
                )
    audit = {
        "valid": not errors,
        "status": "error" if errors else ("warning" if warnings else "ok"),
        "errors": errors, "warnings": warnings, "files": files,
        "source_file_count": len(paths), "raw_rows": sum(item["rows"] for item in files),
        "deduplicated_rows": duplicates, "merged_rows": len(merged),
        "unique_levels": int(merged["level_index"].nunique()) if not merged.empty else 0,
    }
    return merged, audit


LEVEL_LIST_ID_COLUMNS = ("LevelId", "DDALevelId1", "DDALevelId2", "DDALevelId3")


def _read_level_list_rows(path: Path) -> tuple[list[list[str]], bool]:
    raw = path.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text))), has_bom


def _audit_level_list_source(path: Path, target_levels: Sequence[int]) -> dict[str, Any]:
    errors: list[str] = []
    report: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "errors": errors,
        "required_columns": ["LevelIndex", *LEVEL_LIST_ID_COLUMNS],
    }
    if not path.is_file():
        errors.append(f"缺少线上路由配置: {path}")
        return report
    try:
        rows, has_bom = _read_level_list_rows(path)
    except (OSError, UnicodeError, csv.Error) as exc:
        errors.append(f"无法读取config_level_list.csv: {exc}")
        return report
    report.update(
        {
            "sha256": _file_digest(path),
            "total_row_count": len(rows),
            "has_utf8_bom": has_bom,
        }
    )
    if len(rows) < 3:
        errors.append("config_level_list.csv至少应包含表头、类型行和中文说明行。")
        return report
    header = rows[0]
    report["column_count"] = len(header)
    report["columns"] = header
    missing_columns = [
        column for column in ("LevelIndex", *LEVEL_LIST_ID_COLUMNS)
        if column not in header
    ]
    if missing_columns:
        errors.append(f"config_level_list.csv缺少列: {', '.join(missing_columns)}")
        return report
    level_index_column = header.index("LevelIndex")
    required_max_column = max(
        header.index(column) for column in ("LevelIndex", *LEVEL_LIST_ID_COLUMNS)
    )
    level_rows: dict[int, int] = {}
    duplicate_levels: list[int] = []
    for row_index, row in enumerate(rows[3:], start=3):
        try:
            level_index = int(str(row[level_index_column]).strip())
        except (ValueError, IndexError):
            continue
        if len(row) <= required_max_column:
            errors.append(
                f"config_level_list.csv的LevelIndex={level_index}行列数不足，无法读取四列路由ID。"
            )
            continue
        if level_index in level_rows:
            duplicate_levels.append(level_index)
        else:
            level_rows[level_index] = row_index
    if duplicate_levels:
        errors.append(
            f"config_level_list.csv存在重复LevelIndex: {sorted(set(duplicate_levels))[:20]}"
        )
    expected = {int(value) for value in target_levels}
    missing_levels = sorted(expected - set(level_rows))
    report.update(
        {
            "data_row_count": max(0, len(rows) - 3),
            "target_level_count": len(expected),
            "missing_target_levels": missing_levels,
        }
    )
    if missing_levels:
        errors.append(
            f"config_level_list.csv缺少{len(missing_levels)}个目标关卡；前20个: {missing_levels[:20]}"
        )
    return report


def build_level_list_reorder_copy(
    source_path: str | Path,
    mapping_rows: Sequence[Mapping[str, Any]],
    output_dir: str | Path,
    sequence_strategy: str,
) -> dict[str, Any]:
    """Write a reordered copy; unresolved PX/PS source positions stay blank."""
    if sequence_strategy not in SEQUENCE_STRATEGIES:
        raise ValueError(f"未知排关方案: {sequence_strategy}")
    source = Path(source_path).expanduser().resolve()
    mappings = [dict(row) for row in mapping_rows]
    if len(mappings) != 100:
        raise ValueError(f"配置重排需要100行映射，当前为{len(mappings)}行。")
    original_levels = [int(row["original_level_index"]) for row in mappings]
    placed_mappings = [row for row in mappings if row.get("recommended_level_index") not in (None, "")]
    unresolved_mappings = [row for row in mappings if row.get("recommended_level_index") in (None, "")]
    target_levels = [int(row["recommended_level_index"]) for row in placed_mappings]
    reserved_positions = {int(row["original_level_index"]) for row in unresolved_mappings}
    if len(set(original_levels)) != 100:
        raise ValueError("配置重排要求100个原位置各自唯一。")
    if len(set(target_levels)) != len(target_levels):
        raise ValueError("配置重排存在重复目标位置。")
    expected_targets = set(original_levels) - reserved_positions
    if set(target_levels) != expected_targets:
        raise ValueError(
            "已排位目标集合必须等于100关范围扣除待优化关原位，"
            f"当前差异: {sorted(expected_targets ^ set(target_levels))[:20]}"
        )
    source_audit = _audit_level_list_source(source, original_levels)
    if source_audit["errors"]:
        raise InputValidationError("线上路由配置未通过检查", source_audit)
    rows, has_bom = _read_level_list_rows(source)
    original_rows = [list(row) for row in rows]
    output_rows = [list(row) for row in rows]
    header = rows[0]
    level_col = header.index("LevelIndex")
    id_indices = {column: header.index(column) for column in LEVEL_LIST_ID_COLUMNS}
    level_to_row: dict[int, int] = {}
    for row_index, row in enumerate(rows[3:], start=3):
        try:
            level_to_row[int(row[level_col])] = row_index
        except (ValueError, IndexError):
            continue
    snapshot = {
        level: tuple(rows[level_to_row[level]][id_indices[column]] for column in LEVEL_LIST_ID_COLUMNS)
        for level in original_levels
    }
    before_multisets = {
        column: Counter(snapshot[int(row["original_level_index"])][offset] for row in placed_mappings)
        for offset, column in enumerate(LEVEL_LIST_ID_COLUMNS)
    }
    before_quartets = Counter(snapshot[int(row["original_level_index"])] for row in placed_mappings)
    audit_rows: list[dict[str, Any]] = []
    # Every target position in the requested 100-level range is cleared first.
    # Valid levels are then filled back in; unresolved source positions remain
    # intentionally blank so the copy is operationally honest and editable.
    for target in original_levels:
        target_row_index = level_to_row[target]
        for column in LEVEL_LIST_ID_COLUMNS:
            output_rows[target_row_index][id_indices[column]] = ""

    for mapping in placed_mappings:
        original = int(mapping["original_level_index"])
        target = int(mapping["recommended_level_index"])
        target_row_index = level_to_row[target]
        target_before = tuple(
            rows[target_row_index][id_indices[column]] for column in LEVEL_LIST_ID_COLUMNS
        )
        source_values = snapshot[original]
        for offset, column in enumerate(LEVEL_LIST_ID_COLUMNS):
            output_rows[target_row_index][id_indices[column]] = source_values[offset]
        audit_row: dict[str, Any] = {
            "original_level_index": original,
            "recommended_level_index": target,
            "mapping_status": "已排位",
            "planner_tag": mapping.get("planner_tag", ""),
            "sequence_strategy_code": sequence_strategy,
            "sequence_strategy_name": SEQUENCE_STRATEGIES[sequence_strategy]["name"],
        }
        for offset, column in enumerate(LEVEL_LIST_ID_COLUMNS):
            audit_row[f"source_{column}"] = source_values[offset]
            audit_row[f"target_before_{column}"] = target_before[offset]
            audit_row[f"target_after_{column}"] = source_values[offset]
        audit_rows.append(audit_row)

    for mapping in unresolved_mappings:
        original = int(mapping["original_level_index"])
        source_values = snapshot[original]
        audit_row = {
            "original_level_index": original,
            "recommended_level_index": None,
            "reserved_blank_level_index": original,
            "mapping_status": "待优化/补样，原位留空",
            "planner_tag": mapping.get("planner_tag", ""),
            "sequence_strategy_code": sequence_strategy,
            "sequence_strategy_name": SEQUENCE_STRATEGIES[sequence_strategy]["name"],
        }
        target_before = snapshot[original]
        for offset, column in enumerate(LEVEL_LIST_ID_COLUMNS):
            audit_row[f"source_{column}"] = source_values[offset]
            audit_row[f"target_before_{column}"] = target_before[offset]
            audit_row[f"target_after_{column}"] = ""
        audit_rows.append(audit_row)

    # Header, type row and Chinese-description row are immutable.  Every
    # non-routing column across the complete file must also remain untouched.
    if output_rows[:3] != original_rows[:3]:
        raise AssertionError("config_level_list元信息行被意外修改")
    protected_indices = set(id_indices.values())
    for row_index, (before_row, after_row) in enumerate(zip(original_rows, output_rows)):
        width = max(len(before_row), len(after_row))
        for column_index in range(width):
            if column_index in protected_indices:
                continue
            before_value = before_row[column_index] if column_index < len(before_row) else ""
            after_value = after_row[column_index] if column_index < len(after_row) else ""
            if before_value != after_value:
                raise AssertionError(
                    f"配置第{row_index + 1}行第{column_index + 1}列发生非授权变化"
                )
    after_snapshot = {
        level: tuple(
            output_rows[level_to_row[level]][id_indices[column]]
            for column in LEVEL_LIST_ID_COLUMNS
        )
        for level in target_levels
    }
    for offset, column in enumerate(LEVEL_LIST_ID_COLUMNS):
        after_multiset = Counter(after_snapshot[level][offset] for level in target_levels)
        if after_multiset != before_multisets[column]:
            raise AssertionError(f"{column}多重集在重排后不守恒")
    if Counter(after_snapshot.values()) != before_quartets:
        raise AssertionError("四列路由ID组合的多重集在重排后不守恒")
    for level in reserved_positions:
        values = tuple(
            output_rows[level_to_row[level]][id_indices[column]]
            for column in LEVEL_LIST_ID_COLUMNS
        )
        if any(values):
            raise AssertionError(f"待优化关占位 {level} 未留空")

    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    level_min, level_max = min(original_levels), max(original_levels)
    copy_path = target_dir / (
        f"config_level_list_{level_min}-{level_max}_{sequence_strategy}_重排副本"
        f"{'_含待优化空位' if reserved_positions else ''}.csv"
    )
    audit_path = target_dir / (
        f"config_level_list_{level_min}-{level_max}_{sequence_strategy}_映射审计.csv"
    )
    encoding = "utf-8-sig" if has_bom else "utf-8"
    with copy_path.open("w", encoding=encoding, newline="") as target:
        writer = csv.writer(target, lineterminator="\n")
        writer.writerows(output_rows)
    audit_frame = pd.DataFrame(audit_rows).sort_values(
        ["recommended_level_index", "original_level_index"], na_position="last"
    )
    audit_frame.to_csv(audit_path, index=False, encoding="utf-8-sig")
    source_sha_after = _file_digest(source)
    source_unchanged = source_sha_after == source_audit["sha256"]
    if not source_unchanged:
        raise AssertionError("源config_level_list.csv在导出过程中发生变化")
    metadata = {
        "source_path": str(source),
        "source_sha256": source_audit["sha256"],
        "source_unchanged": source_unchanged,
        "source_total_row_count": source_audit["total_row_count"],
        "source_data_row_count": source_audit["data_row_count"],
        "source_column_count": source_audit["column_count"],
        "source_columns": source_audit["columns"],
        "replaced_columns": list(LEVEL_LIST_ID_COLUMNS),
        "mapped_level_count": len(placed_mappings),
        "blank_position_count": len(reserved_positions),
        "blank_positions": sorted(reserved_positions),
        "unresolved_source_levels": sorted(reserved_positions),
        "level_range": f"{level_min}-{level_max}",
        "original_positions_unique": len(set(original_levels)) == 100,
        "target_positions_unique": len(set(target_levels)) == len(target_levels),
        "position_sets_equal": set(target_levels) == expected_targets,
        "id_multisets_conserved": True,
        "metadata_rows_preserved": output_rows[:3] == original_rows[:3],
        "other_columns_preserved": True,
        "copy_sha256": _file_digest(copy_path),
    }
    return {
        "level_list_copy_path": str(copy_path),
        "level_list_audit_path": str(audit_path),
        "metadata": metadata,
        "audit_rows": _json_safe(audit_rows),
    }


def preflight_online(
    source_files: Sequence[str | Path],
    settings: Mapping[str, Any] | None,
    player_job: Any,
    work_dir: str | Path,
) -> dict[str, Any]:
    """Validate online aggregates and their one-to-one level overlap with stage one."""
    online, audit = _load_online_sources(source_files)
    strategy_code = str((settings or {}).get("sequence_strategy") or DEFAULT_SEQUENCE_STRATEGY).strip()
    label_method = str(
        (settings or {}).get("level_label_method") or DEFAULT_LEVEL_LABEL_METHOD
    ).strip()
    audit["sequence_strategy"] = strategy_code
    audit["level_label_method"] = label_method
    audit["available_sequence_strategies"] = [
        {
            "code": code,
            "name": spec["name"],
            "description": spec["description"],
            "bucket_to_digit": list(spec["bucket_to_digit"]),
            "ten_level_roles": list(spec["ten_level_roles"]),
        }
        for code, spec in SEQUENCE_STRATEGIES.items()
    ]
    if strategy_code not in SEQUENCE_STRATEGIES:
        audit["errors"].append(
            f"sequence_strategy={strategy_code!r} 无效；可选值: {', '.join(SEQUENCE_STRATEGIES)}"
        )
    else:
        audit["sequence_strategy_name"] = SEQUENCE_STRATEGIES[strategy_code]["name"]
    audit["available_level_label_methods"] = list(LEVEL_LABEL_METHODS.values())
    if label_method not in LEVEL_LABEL_METHODS:
        audit["errors"].append(
            f"level_label_method={label_method!r} 无效；可选值: {', '.join(LEVEL_LABEL_METHODS)}"
        )
    else:
        audit["level_label_method_name"] = LEVEL_LABEL_METHODS[label_method]["name"]
    try:
        level_path = _resolve_level_labels(player_job)
        labels = _read_csv(level_path)
        required_labels = {"level_index", "first_level_behavior_label"}
        missing = sorted(required_labels - set(labels.columns))
        if missing:
            audit["errors"].append(f"第一阶段分层表缺少列: {', '.join(missing)}")
        else:
            online_levels = set(pd.to_numeric(online["level_index"], errors="coerce").dropna().astype(int)) if not online.empty else set()
            label_levels = set(pd.to_numeric(labels["level_index"], errors="coerce").dropna().astype(int))
            common = online_levels & label_levels
            audit["stage1_levels_csv_path"] = str(level_path)
            audit["matching_level_count"] = len(common)
            audit["online_only_levels"] = sorted(online_levels - label_levels)
            audit["stage1_only_levels"] = sorted(label_levels - online_levels)
            relevant_labels = labels.loc[
                pd.to_numeric(labels["level_index"], errors="coerce").isin(common),
                "first_level_behavior_label",
            ].fillna("").astype(str).str.strip()
            unknown_label_counts = {
                label: int(count)
                for label, count in relevant_labels.value_counts(dropna=False).items()
                if label not in PRESSURE_ORDER
            }
            audit["unclassified_stage1_label_counts"] = unknown_label_counts
            if unknown_label_counts:
                display = "、".join(
                    f"{label or '空标签'} {count}关"
                    for label, count in unknown_label_counts.items()
                )
                audit["errors"].append(
                    "第一阶段仍有无法用于三维分类的关卡标签（"
                    f"{display}）；请补充互不重复的玩家批次后重新运行表1。"
                )
            if audit["online_only_levels"]:
                audit["warnings"].append(
                    f"线上表多出{len(audit['online_only_levels'])}个第一阶段范围外的关卡，将不进入本次推荐序列。"
                )
            if audit["stage1_only_levels"]:
                audit["warnings"].append(
                    f"第一阶段有{len(audit['stage1_only_levels'])}个关卡未在线上表中找到。"
                )
            if len(common) != 100:
                audit["errors"].append(f"两阶段完整匹配{len(common)}关；推荐序列需要恰好100个共同关卡。")
            elif sorted(common) != list(range(min(common), min(common) + 100)):
                audit["errors"].append("两阶段虽匹配100关，但关卡号不连续；推荐序列需要连续100关。")
            if len(common) == 100:
                level_list_audit = _audit_level_list_source(
                    _resolve_config_dir(settings) / "config_level_list.csv",
                    sorted(common),
                )
                audit["level_list_source"] = level_list_audit
                audit["errors"].extend(level_list_audit["errors"])
    except InputValidationError as exc:
        audit["errors"].append(str(exc))
    if audit["errors"]:
        audit["status"] = "error"
        audit["valid"] = False
    elif audit["warnings"]:
        audit["status"] = "warning"
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    if not online.empty:
        merged_path = work / "merged_online_input.csv"
        online.to_csv(merged_path, index=False, encoding="utf-8-sig")
        audit["merged_csv_path"] = str(merged_path)
    _write_json(work / "online_input_audit.json", audit)
    return _json_safe(audit)


def weighted_mean(frame: pd.DataFrame, column: str) -> float:
    values = pd.to_numeric(frame[column], errors="coerce").to_numpy(float)
    weights = pd.to_numeric(frame["进入人数"], errors="coerce").to_numpy(float)
    valid = np.isfinite(values) & np.isfinite(weights) & (weights > 0)
    if not valid.any():
        return math.nan
    return float(np.average(values[valid], weights=weights[valid]))


def robust_center_scale(values: Iterable[Any]) -> tuple[float, float]:
    numeric = pd.to_numeric(pd.Series(list(values)), errors="coerce").dropna().to_numpy(float)
    if not len(numeric):
        raise ValueError("稳健基线没有有效数值")
    center = float(np.median(numeric))
    mad = float(np.median(np.abs(numeric - center)))
    scale = 1.4826 * mad
    if not np.isfinite(scale) or scale <= 0:
        scale = float(np.std(numeric, ddof=0))
    if not np.isfinite(scale) or scale <= 0:
        scale = 1.0
    return center, scale


def robust_band(z_value: float) -> str:
    if z_value <= -2:
        return "极低"
    if z_value <= -1:
        return "偏低"
    if z_value < 1:
        return "正常"
    if z_value < 2:
        return "偏高"
    return "极高"


def named_tag(pressure_order: int, consume_band: str, fail_band: str) -> str:
    consume_high = consume_band in {"偏高", "极高"}
    fail_high = fail_band in {"偏高", "极高"}
    if pressure_order >= 2 and fail_high and consume_high:
        return "T7"
    if pressure_order >= 2 and fail_high:
        return "T6"
    if pressure_order >= 2 and consume_high:
        return "T5"
    if pressure_order >= 2:
        return "T4"
    if fail_high:
        return "T3"
    if pressure_order == 1:
        return "T2"
    if consume_high:
        return "T1"
    return "T0"


def planner_pressure_tag(row: Mapping[str, Any]) -> str:
    """Collapse diagnostic T-codes into a planner-facing pressure/placement tag.

    The split intentionally uses real-time behaviour shares first.  Outcome
    bands only separate a useful challenge from a harmful/high-risk result.
    In particular, T0 is *not* treated as a synonym for a fun/easy level.
    """
    tag_code = str(row.get("tag_code", ""))
    pressure_order = int(row.get("pressure_order", 0) or 0)
    friction = float(row.get("first_any_friction_share", 0) or 0)
    compound = float(row.get("first_compound_high_burden_share", 0) or 0)
    low_burden = float(row.get("first_L1_share", 0) or 0)
    l4_share = float(row.get("first_L4_share", 0) or 0)
    l3_share = float(row.get("first_L3_share", 0) or 0)
    fail_z = float(row.get("fail_z", 0) or 0)
    consume_z = float(row.get("consume_z", 0) or 0)
    behavior_label = str(row.get("first_level_behavior_label", ""))

    # PX is deliberately narrow: a high outcome alone, or a high-pressure
    # proxy alone, does not prove an extreme negative experience.  Levels that
    # do not cross this joint evidence gate keep a normal P0-P5 placement and
    # expose the anomaly in ``risk_note`` for designer review.
    extreme_negative = (
        fail_z >= 3.0
        and friction >= 0.50
        and (
            pressure_order >= 4
            or compound >= 0.50
            or (l4_share + l3_share) >= 0.55
        )
    )
    if extreme_negative:
        return "PX"
    if tag_code == "T3":
        return "P2"
    if tag_code in {"T6", "T7"}:
        return "P5"

    if tag_code in {"T4", "T5"}:
        # A broad but not majority compound signal is a thinking/puzzle level;
        # majority/multi-dimensional burden is the actual challenge tier.
        if pressure_order >= 4 or (l4_share + l3_share) >= 0.50 or friction >= 0.60:
            return "P4"
        return "P3"

    if (
        "复合卡点" in behavior_label
        or compound >= 0.18
        or (friction >= 0.30 and pressure_order >= 1)
    ):
        return "P3"
    if tag_code == "T2" or friction >= 0.08:
        return "P2"

    # A true recovery/satisfying level needs positive low-burden evidence.
    # Merely failing to detect friction (L0) is classified as plain transition.
    if low_burden >= 0.45 and friction <= 0.08 and fail_z < 1 and consume_z < 1:
        return "P0"
    return "P1"


def planner_risk_note(row: Mapping[str, Any], planner_code: str | None = None) -> str:
    """Describe review risks without silently removing a level from sequence.

    These are evidence notes, not causal conclusions.  Prop ineffectiveness,
    global deadlock and tower-gamble death require their own action-level
    reconstruction and are never inferred from aggregate consume/loss alone.
    """
    tag_code = str(row.get("tag_code", ""))
    fail_z = float(row.get("fail_z", 0) or 0)
    consume_z = float(row.get("consume_z", 0) or 0)
    friction = float(row.get("first_any_friction_share", 0) or 0)
    compound = float(row.get("first_compound_high_burden_share", 0) or 0)
    l4_share = float(row.get("first_L4_share", 0) or 0)
    l3_share = float(row.get("first_L3_share", 0) or 0)
    code = planner_code or planner_pressure_tag(row)
    if code == "PX":
        return (
            "多维高压力与极端失败流失同时稳定出现，已达到暂不排位门槛；"
            "建议复核卡点位置、道具后1/3/5步缓解、塔牌终局和无道具可行路径。"
        )
    if tag_code == "T6":
        return (
            "高压力并伴随较高失败流失，但尚不足以确认极端负面；已按超难高危型正常排位。"
            "建议重点复核路径收窄、背包高压持续时长及道具后是否复压。"
        )
    if tag_code == "T7":
        return (
            "高压力、高消耗与较高失败流失并存；已按超难高危型正常排位。"
            "建议确认消耗是否有效缓解局面，以及失败是否集中在同一终局。"
        )
    if tag_code == "T3" or fail_z >= 2.0:
        return (
            "失败流失明显偏高，但实时过程压力证据不足；已按行为特征正常排位。"
            "建议核对统计外卡点、异常退出或关外流失原因。"
        )
    if friction >= 0.50 or compound >= 0.25 or (l4_share + l3_share) >= 0.35:
        return "过程压力较集中；建议结合卡点位置、塔牌节奏与道具后复压情况复核。"
    if consume_z >= 2.0:
        return "消耗显著偏高但过程压力未同步升高；建议判断是否属于便利性消费。"
    return ""


def planner_tag_name(code: str) -> str:
    if code == "PS":
        return "PS 样本不足"
    name, _definition = PLANNER_TAG_DEFINITIONS[code]
    return f"{code} {name}"


def planner_slot_guidance(code: str) -> str:
    return {
        "P0": "适合尾号0",
        "P1": "过渡位，优先尾号1/6/7",
        "P2": "适合尾号2/3",
        "P3": "适合尾号4/8",
        "P4": "适合尾号5",
        "P5": "适合尾号9",
        "PX": "极端负面，建议优化且不排位",
        "PS": "先补样，原位留空",
    }[code]


def _sequence_plan(strategy_code: str) -> list[dict[str, Any]]:
    spec = SEQUENCE_STRATEGIES[strategy_code]
    bucket_by_digit = {
        int(digit): bucket for bucket, digit in enumerate(spec["bucket_to_digit"])
    }
    return [
        {
            "digit": digit,
            "role": spec["ten_level_roles"][digit],
            "risk_bucket": bucket_by_digit[digit],
            "risk_rank": f"{bucket_by_digit[digit] + 1}/10",
        }
        for digit in range(10)
    ]


def sequence_adjacency_metrics(frame: pd.DataFrame) -> dict[str, int]:
    """Count adjacent repetition risks in the recommended sequence."""
    ordered = frame[frame["recommended_level_index"].notna()].sort_values(
        "recommended_level_index", kind="stable"
    )
    templates = (
        ordered["level_template_id"]
        if "level_template_id" in ordered
        else pd.Series("", index=ordered.index)
    ).fillna("").astype(str).tolist()
    tags = (
        ordered["tag_code"]
        if "tag_code" in ordered
        else pd.Series("", index=ordered.index)
    ).fillna("").astype(str).tolist()
    same_template = sum(
        bool(left) and left == right for left, right in zip(templates, templates[1:])
    )
    high_risk_tags = {"T6", "T7"}
    adjacent_high_risk = sum(
        left in high_risk_tags and right in high_risk_tags
        for left, right in zip(tags, tags[1:])
    )
    return {
        "adjacent_same_template_count": int(same_template),
        "adjacent_t6_t7_count": int(adjacent_high_risk),
    }


def _diversity_guard(result: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int], dict[str, int]]:
    """Pareto-improve adjacency counts using only within-bucket swaps."""
    output = result.copy()
    before = sequence_adjacency_metrics(output)
    current = dict(before)
    # Each accepted swap is non-worsening on both requested metrics and strict
    # on at least one.  Counts are non-negative, so the loop terminates.
    for _iteration in range(200):
        best: tuple[tuple[Any, ...], int, int, dict[str, int]] | None = None
        for bucket in range(10):
            indices = sorted(
                output.index[output["sequence_risk_bucket"].eq(bucket)].tolist(),
                key=lambda index: int(output.at[index, "level_index"]),
            )
            for left_offset, left in enumerate(indices):
                for right in indices[left_offset + 1 :]:
                    output.at[left, "recommended_level_index"], output.at[right, "recommended_level_index"] = (
                        output.at[right, "recommended_level_index"],
                        output.at[left, "recommended_level_index"],
                    )
                    candidate = sequence_adjacency_metrics(output)
                    acceptable = (
                        candidate["adjacent_same_template_count"] <= current["adjacent_same_template_count"]
                        and candidate["adjacent_t6_t7_count"] <= current["adjacent_t6_t7_count"]
                        and candidate != current
                    )
                    if acceptable:
                        signature = tuple(
                            output.sort_values("recommended_level_index", kind="stable")["level_index"].astype(int)
                        )
                        key = (
                            candidate["adjacent_same_template_count"],
                            candidate["adjacent_t6_t7_count"],
                            signature,
                        )
                        if best is None or key < best[0]:
                            best = (key, left, right, candidate)
                    output.at[left, "recommended_level_index"], output.at[right, "recommended_level_index"] = (
                        output.at[right, "recommended_level_index"],
                        output.at[left, "recommended_level_index"],
                    )
        if best is None:
            break
        _key, left, right, current = best
        output.at[left, "recommended_level_index"], output.at[right, "recommended_level_index"] = (
            output.at[right, "recommended_level_index"],
            output.at[left, "recommended_level_index"],
        )
    after = sequence_adjacency_metrics(output)
    if (
        after["adjacent_same_template_count"] > before["adjacent_same_template_count"]
        or after["adjacent_t6_t7_count"] > before["adjacent_t6_t7_count"]
    ):
        raise AssertionError("多样性保护不得劣化相邻重复指标")
    return output, before, after


def assign_recommended_positions(
    frame: pd.DataFrame,
    sequence_strategy: str = DEFAULT_SEQUENCE_STRATEGY,
) -> pd.DataFrame:
    """Assign valid levels to unique positions and keep PX/PS positions blank.

    Within one planner label, source levels are always consumed in ascending
    order.  The spiral-flow strategy additionally uses semantic target
    priorities: P0 first takes every tail-0 slot, then the earliest remaining
    slot; the other labels use their documented preferred tail digits.
    """
    if sequence_strategy not in SEQUENCE_STRATEGIES:
        raise ValueError(
            f"未知排关方案 {sequence_strategy!r}；可选值: {', '.join(SEQUENCE_STRATEGIES)}"
        )
    result = frame.copy().reset_index(drop=True)
    if len(result) != 100 or result["level_index"].nunique() != 100:
        raise ValueError("尾号节奏分配要求恰好100个唯一关卡")
    level_values = sorted(result["level_index"].astype(int).tolist())
    start = level_values[0]
    if level_values != list(range(start, start + 100)):
        raise ValueError("推荐序列要求100个连续关卡号")
    has_explicit_planner_tags = "planner_tag_code" in result
    if not has_explicit_planner_tags:
        # Backward-compatible direct use by historical scripts/tests.  Only
        # fully classified planner rows may create intentional blank slots.
        result["planner_tag_code"] = result["tag_code"].map({
            "T0": "P0", "T1": "P1", "T2": "P2", "T3": "P2",
            "T4": "P3", "T5": "P4", "T6": "P5", "T7": "P5",
        })
    planner_risk = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4, "P5": 5, "PX": 6, "PS": 6}
    base_risk = (
        result["planner_tag_code"].map(planner_risk)
        if "planner_tag_code" in result
        else result["tag_code"].str[1:].astype(int)
    )
    # Production planner rows use label order only; detailed z-scores remain
    # in the audit and must not reorder levels inside one label.  Historical
    # direct callers without planner tags retain the legacy diagnostic score.
    if has_explicit_planner_tags:
        result["sequence_score"] = base_risk.astype(float) * 100
    else:
        result["sequence_score"] = (
            result["tag_code"].str[1:].astype(float) * 100
            + result["pressure_order"] * 10
            + result["consume_z"].clip(-2, 3) * 2
            + result["fail_z"].clip(-2, 3) * 3
        )
    digit_by_rising_difficulty = list(
        SEQUENCE_STRATEGIES[sequence_strategy]["bucket_to_digit"]
    )
    unplaced_mask = result["planner_tag_code"].isin(UNPLACED_PLANNER_CODES)
    reserved_positions = set(result.loc[unplaced_mask, "level_index"].astype(int))
    available_positions = set(level_values) - reserved_positions
    valid = result.loc[~unplaced_mask].sort_values(
        ["sequence_score", "level_index"], kind="stable"
    )
    position_map: dict[int, int] = {}

    if not has_explicit_planner_tags:
        ranked = result.sort_values(["sequence_score", "level_index"], kind="stable").reset_index(drop=True)
        for bucket_index, digit in enumerate(digit_by_rising_difficulty):
            source_levels = sorted(
                ranked.iloc[bucket_index * 10 : (bucket_index + 1) * 10]["level_index"].astype(int)
            )
            positions = list(range(start + digit, start + 100, 10))
            for level_index, position in zip(source_levels, positions):
                position_map[level_index] = position
                available_positions.remove(position)
    elif sequence_strategy == "spiral_flow":
        preferred_digits = {
            "P0": [0], "P1": [1, 6, 7], "P2": [2, 3],
            "P3": [4, 8], "P4": [5], "P5": [9],
        }
        for code in ("P0", "P1", "P2", "P3", "P4", "P5"):
            source_levels = sorted(
                valid.loc[valid["planner_tag_code"].eq(code), "level_index"].astype(int)
            )
            digit_order = preferred_digits[code]
            preferred = sorted(
                (position for position in available_positions if position % 10 in digit_order),
                key=lambda position: (digit_order.index(position % 10), position),
            )
            overflow = sorted(available_positions - set(preferred))
            candidates = preferred + overflow
            if len(candidates) < len(source_levels):
                raise AssertionError(f"{code}可用排位不足")
            for level_index, position in zip(source_levels, candidates):
                position_map[level_index] = position
                available_positions.remove(position)
    else:
        # Other strategies retain their named risk-bucket order.  Removing a
        # reserved PX/PS position leaves an intentional blank rather than
        # blocking all valid levels from being exported.
        target_priority = [
            position
            for digit in digit_by_rising_difficulty
            for position in range(start + digit, start + 100, 10)
            if position in available_positions
        ]
        for level_index, position in zip(valid["level_index"].astype(int), target_priority):
            position_map[level_index] = position

    if available_positions - set(position_map.values()):
        raise AssertionError("推荐序列存在未分配的非保留位置")
    result["recommended_level_index"] = pd.array(
        result["level_index"].map(position_map), dtype="Int64"
    )
    digit_to_bucket = {digit: bucket for bucket, digit in enumerate(digit_by_rising_difficulty)}
    result["sequence_risk_bucket"] = pd.array(
        result["recommended_level_index"].map(
            lambda value: digit_to_bucket[int(value) % 10] if pd.notna(value) else pd.NA
        ),
        dtype="Int64",
    )
    result["sequence_slot_digit"] = pd.array(
        result["recommended_level_index"].map(
            lambda value: int(value) % 10 if pd.notna(value) else pd.NA
        ),
        dtype="Int64",
    )
    roles = SEQUENCE_STRATEGIES[sequence_strategy]["ten_level_roles"]
    result["sequence_role"] = result["sequence_slot_digit"].map(
        lambda digit: roles[int(digit)] if pd.notna(digit) else "留空"
    )
    result["sequence_strategy_code"] = sequence_strategy
    result["sequence_strategy_name"] = SEQUENCE_STRATEGIES[sequence_strategy]["name"]
    diversity_before = sequence_adjacency_metrics(result)
    diversity_after = dict(diversity_before)
    # The former diversity swap could invert two source levels carrying the
    # same label.  Stable same-label ordering is now a stronger contract, so
    # diversity_guard reports the baseline adjacency metrics without swapping.
    if result["recommended_level_index"].dropna().nunique() != len(valid):
        raise AssertionError("推荐序列存在重复位置")
    if set(result.loc[unplaced_mask, "level_index"].astype(int)) != reserved_positions:
        raise AssertionError("待优化关的留空位置不稳定")
    result["placement_status"] = np.where(unplaced_mask, "待优化/补样，原位留空", "已排位")
    result.attrs["sequence_diversity_before"] = diversity_before
    result.attrs["sequence_diversity_after"] = diversity_after
    return result


def _clean_join_text(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()


def build_duplicate_labels(
    online: pd.DataFrame,
    instances: pd.DataFrame | None,
    level_results: pd.DataFrame,
    consume_center: float,
    consume_scale: float,
    fail_center: float,
    fail_scale: float,
) -> list[dict[str, Any]]:
    """Build one planner-facing record for every level/duplicate/attempt phase."""
    if instances is None or instances.empty:
        return []
    required = {
        "level_index", "level_template_id", "level_dup_para_id", "level_dup_para",
        "attempt_phase", "behavior_layer", "player_count",
    }
    if required - set(instances.columns):
        return []

    source = online.copy()
    target_levels = set(level_results["level_index"].astype(int))
    source = source[pd.to_numeric(source["level_index"], errors="coerce").isin(target_levels)].copy()
    if "level_dup" not in source.columns:
        source["level_dup"] = ""
    keys = ["level_index", "level_id", "level_template_id", "level_dup_para_id", "level_dup"]
    aggregate_rows: list[dict[str, Any]] = []
    for values, group in source.groupby(keys, dropna=False, sort=True):
        weight = pd.to_numeric(group["进入人数"], errors="coerce").fillna(0)
        total = float(weight.sum())
        consume_values = pd.to_numeric(group["人均消耗"], errors="coerce")
        fail_values = pd.to_numeric(group["失败流失"], errors="coerce")
        consume = float(np.average(consume_values.fillna(0), weights=weight)) if total > 0 else math.nan
        fail_loss = float(np.average(fail_values.fillna(0), weights=weight)) if total > 0 else math.nan
        aggregate_rows.append({
            "level_index": int(values[0]),
            "level_id": _clean_join_text(values[1]),
            "level_template_id": _clean_join_text(values[2]),
            "level_dup_para_id": _clean_join_text(values[3]),
            "level_dup_para": _clean_join_text(values[4]),
            "enter_users": int(total),
            "consume": consume,
            "fail_loss": fail_loss,
        })
    duplicates = pd.DataFrame(aggregate_rows)
    instance = instances.copy()
    instance["level_index"] = pd.to_numeric(instance["level_index"], errors="coerce").astype("Int64")
    instance = instance[instance["level_index"].notna()].copy()
    for column in ("level_template_id", "level_dup_para_id", "level_dup_para"):
        instance[column] = instance[column].map(_clean_join_text)
        duplicates[column] = duplicates[column].map(_clean_join_text)
    instance = instance.sort_values(
        ["level_index", "attempt_phase", "player_count"], ascending=[True, True, False]
    ).drop_duplicates(
        ["level_index", "level_template_id", "level_dup_para_id", "level_dup_para", "attempt_phase"],
        keep="first",
    )
    keep = [
        "level_index", "level_template_id", "level_dup_para_id", "level_dup_para",
        "attempt_phase", "behavior_layer", "layer_stability", "player_count",
        "primary_high_axes", "descriptive_behavior",
    ]
    keep = [column for column in keep if column in instance.columns]
    merged = duplicates.merge(
        instance[keep],
        on=["level_index", "level_template_id", "level_dup_para_id", "level_dup_para"],
        how="left",
    )
    level_map = level_results.set_index("level_index")
    merged["consume_z"] = (merged["consume"] - consume_center) / consume_scale
    merged["fail_z"] = (merged["fail_loss"] - fail_center) / fail_scale
    merged["consume_band"] = merged["consume_z"].map(robust_band)
    merged["fail_band"] = merged["fail_z"].map(robust_band)

    layer_to_tag = {
        "L4_": "P4", "L3_": "P3", "L2_": "P2",
        "L1_": "P0", "L0_": "P1", "P_": "PS",
    }

    def duplicate_tag(record: pd.Series) -> str:
        layer = str(record.get("behavior_layer", ""))
        if not layer or layer == "nan":
            return "PS"
        pressure = next((code for prefix, code in layer_to_tag.items() if layer.startswith(prefix)), "PS")
        # Sparse duplicate samples stay explicitly unclassified.  Online
        # outcome bands must not turn missing real-time behaviour evidence
        # into a false negative-experience label.
        if pressure == "PS":
            return "PS"
        pressure_order = {"P0": 0, "P1": 0, "P2": 1, "P3": 3, "P4": 4, "PS": 0}[pressure]
        diagnostic = named_tag(pressure_order, str(record["consume_band"]), str(record["fail_band"]))
        # A single duplicate is withheld only when its strongest behaviour
        # layer is multi-dimensional high burden and loss is an extreme
        # outlier.  Other anomalies remain placed and are written as notes.
        if pressure == "P4" and float(record["fail_z"]) >= 3.0:
            return "PX"
        if diagnostic == "T3":
            return "P2"
        if diagnostic in {"T6", "T7"}:
            return "P5"
        if diagnostic in {"T4", "T5"} and pressure == "P3":
            return "P3"
        return pressure

    merged["duplicate_planner_code"] = merged.apply(duplicate_tag, axis=1)
    def duplicate_risk_note(record: pd.Series) -> str:
        code = str(record.get("duplicate_planner_code", "PS"))
        fail_z = float(record.get("fail_z", 0) or 0)
        consume_z = float(record.get("consume_z", 0) or 0)
        layer = str(record.get("behavior_layer", ""))
        if code == "PX":
            return "该副本为多维高负担且失败流失达到极端异常；建议先优化或替换。"
        if code == "P5":
            return "该副本高压力并伴随较高失败流失；正常进入超难位，但建议复核终局卡点与道具缓解。"
        if fail_z >= 2.0:
            return "该副本失败流失极高，但尚无充分多维负面证据；按行为层正常排位并保留复核提示。"
        if layer.startswith(("L3_", "L4_")):
            return "该副本过程压力较集中；建议复核卡点位置、塔牌节奏与道具后复压。"
        if consume_z >= 2.0:
            return "该副本消耗显著偏高；建议判断是否属于便利性消费。"
        return ""

    merged["duplicate_risk_note"] = merged.apply(duplicate_risk_note, axis=1)
    definitions = dict(PLANNER_TAG_DEFINITIONS)
    definitions["PS"] = ("样本不足", "该副本未匹配到可稳定分层的玩家样本")
    slot = {code: planner_slot_guidance(code) for code in PLANNER_TAG_DEFINITIONS}
    slot["PS"] = "先补样，不排位"
    rows: list[dict[str, Any]] = []
    for record in merged.sort_values(
        ["level_index", "level_dup_para_id", "attempt_phase"], kind="stable"
    ).to_dict(orient="records"):
        code = str(record.get("duplicate_planner_code", "PS"))
        level_code = str(level_map.at[int(record["level_index"]), "planner_tag_code"])
        rows.append({
            "level_index": int(record["level_index"]),
            "level_id": record.get("level_id", ""),
            "level_template_id": record.get("level_template_id", ""),
            "level_dup_para_id": record.get("level_dup_para_id", ""),
            "level_dup_para": record.get("level_dup_para", ""),
            "attempt_phase": record.get("attempt_phase", "未匹配"),
            "player_count": record.get("player_count"),
            "behavior_layer": record.get("behavior_layer", "未匹配"),
            "layer_stability": record.get("layer_stability", ""),
            "primary_high_axes": record.get("primary_high_axes", ""),
            "duplicate_planner_code": code,
            "duplicate_planner_tag": f"{code} {definitions[code][0]}",
            "duplicate_risk_note": record.get("duplicate_risk_note", ""),
            "level_planner_tag": planner_tag_name(level_code),
            "slot_guidance": slot[code],
            "enter_users": int(record.get("enter_users", 0) or 0),
            "consume": record.get("consume"),
            "consume_band": record.get("consume_band"),
            "fail_loss": record.get("fail_loss"),
            "fail_band": record.get("fail_band"),
            "action": PLANNER_TAG_ACTIONS.get(code, "补充样本后再判断"),
        })
    return _json_safe(rows)


def apply_level_label_method(
    levels: pd.DataFrame,
    duplicate_rows: Sequence[Mapping[str, Any]],
    method: str,
) -> pd.DataFrame:
    """Attach both aggregation methods and activate the requested one."""
    if method not in LEVEL_LABEL_METHODS:
        raise ValueError(
            f"未知关卡标签聚合方式 {method!r}；可选值: {', '.join(LEVEL_LABEL_METHODS)}"
        )
    result = levels.copy()
    result["weighted_planner_tag_code"] = result["planner_tag_code"]
    result["weighted_planner_tag"] = result["planner_tag"]
    if "risk_note" not in result:
        result["risk_note"] = ""
    result["weighted_risk_note"] = result["risk_note"]
    result["dominant_duplicate_planner_code"] = "PS"
    result["dominant_duplicate_planner_tag"] = planner_tag_name("PS")
    result["dominant_duplicate_risk_note"] = "该副本实时行为样本不足，需补样后判断。"
    result["dominant_duplicate_id"] = ""
    result["dominant_duplicate_para"] = ""
    result["dominant_duplicate_enter_users"] = 0
    result["dominant_duplicate_enter_share"] = 0.0

    duplicate = pd.DataFrame(list(duplicate_rows))
    if not duplicate.empty:
        first = duplicate.loc[duplicate["attempt_phase"].astype(str).eq("首局")].copy()
        if not first.empty:
            first["enter_users"] = pd.to_numeric(first["enter_users"], errors="coerce").fillna(0)
            first["dup_sort"] = pd.to_numeric(first["level_dup_para_id"], errors="coerce").fillna(10**9)
            dominant = (
                first.sort_values(
                    ["level_index", "enter_users", "dup_sort", "level_dup_para_id"],
                    ascending=[True, False, True, True],
                    kind="stable",
                )
                .drop_duplicates("level_index", keep="first")
                .set_index("level_index")
            )
            for index, record in result.iterrows():
                level_index = int(record["level_index"])
                if level_index not in dominant.index:
                    continue
                source = dominant.loc[level_index]
                code = str(source.get("duplicate_planner_code") or "PS")
                result.at[index, "dominant_duplicate_planner_code"] = code
                result.at[index, "dominant_duplicate_planner_tag"] = planner_tag_name(code)
                result.at[index, "dominant_duplicate_risk_note"] = str(
                    source.get("duplicate_risk_note") or ""
                )
                result.at[index, "dominant_duplicate_id"] = _clean_join_text(source.get("level_dup_para_id"))
                result.at[index, "dominant_duplicate_para"] = _clean_join_text(source.get("level_dup_para"))
                entrants = int(source.get("enter_users", 0) or 0)
                result.at[index, "dominant_duplicate_enter_users"] = entrants
                total = int(record.get("enter_users", 0) or 0)
                result.at[index, "dominant_duplicate_enter_share"] = entrants / total if total else 0.0

    if method == "dominant_online_duplicate":
        result["planner_tag_code"] = result["dominant_duplicate_planner_code"]
        result["planner_tag"] = result["dominant_duplicate_planner_tag"]
        result["risk_note"] = result["dominant_duplicate_risk_note"]
    result["level_label_method_code"] = method
    result["level_label_method_name"] = LEVEL_LABEL_METHODS[method]["name"]
    result["slot_guidance"] = result["planner_tag_code"].map(planner_slot_guidance)
    result["action"] = result["planner_tag_code"].map(
        lambda code: PLANNER_TAG_ACTIONS.get(code, "补充该副本样本后再排位")
    )
    result["extreme_negative_evidence"] = result["planner_tag_code"].eq("PX")
    return result


def analyze_online_levels(
    online: pd.DataFrame,
    labels: pd.DataFrame,
    sequence_strategy: str = DEFAULT_SEQUENCE_STRATEGY,
    instances: pd.DataFrame | None = None,
    level_label_method: str = DEFAULT_LEVEL_LABEL_METHOD,
) -> dict[str, Any]:
    """Pure stage-two calculation used by the UI and unit tests."""
    records: list[dict[str, Any]] = []
    for level_index, group in online.groupby("level_index", sort=True):
        pair_weights = (
            group.groupby(["level_id", "level_template_id"], dropna=False)["进入人数"]
            .sum().sort_values(ascending=False)
        )
        level_id, template_id = pair_weights.index[0]
        records.append({
            "level_index": int(level_index), "level_id": str(level_id),
            "level_template_id": str(template_id),
            "enter_users": int(pd.to_numeric(group["进入人数"], errors="coerce").sum()),
            "consume": weighted_mean(group, "人均消耗"),
            "fail_loss": weighted_mean(group, "失败流失"),
        })
    levels = pd.DataFrame(records)
    label_columns = [
        "level_index", "first_level_behavior_label",
        "first_any_friction_share", "first_compound_high_burden_share",
        "first_L4_share", "first_L3_share", "first_L2_share",
        "first_L1_share", "first_L0_share", "first_main_high_burden_axes",
    ]
    available_label_columns = [column for column in label_columns if column in labels.columns]
    labels = labels[available_label_columns].copy()
    for column in label_columns:
        if column not in labels.columns:
            labels[column] = "" if column in {"first_level_behavior_label", "first_main_high_burden_axes"} else 0.0
    labels["level_index"] = pd.to_numeric(labels["level_index"], errors="raise").astype(int)
    levels = levels.merge(labels, on="level_index", how="inner", validate="one_to_one")
    if len(levels) != 100:
        raise ValueError(f"第一、二阶段仅匹配{len(levels)}关，无法生成100关推荐序列。")
    levels["pressure_order"] = levels["first_level_behavior_label"].map(PRESSURE_ORDER)
    if levels["pressure_order"].isna().any():
        unknown = sorted(levels.loc[levels["pressure_order"].isna(), "first_level_behavior_label"].unique())
        raise ValueError(f"存在未识别的压力标签: {unknown}")
    consume_center, consume_scale = robust_center_scale(levels["consume"])
    fail_center, fail_scale = robust_center_scale(levels["fail_loss"])
    levels["consume_z"] = (levels["consume"] - consume_center) / consume_scale
    levels["fail_z"] = (levels["fail_loss"] - fail_center) / fail_scale
    levels["consume_band"] = levels["consume_z"].map(robust_band)
    levels["fail_band"] = levels["fail_z"].map(robust_band)
    levels["classification_label"] = (
        levels["first_level_behavior_label"] + "×消耗" + levels["consume_band"]
        + "×失败流失" + levels["fail_band"]
    )
    levels["tag_code"] = levels.apply(
        lambda row: named_tag(int(row["pressure_order"]), str(row["consume_band"]), str(row["fail_band"])), axis=1
    )
    levels["tag_name"] = levels["tag_code"].map(lambda code: TAG_DEFINITIONS[code][0])
    levels["named_tag"] = levels["tag_code"] + " " + levels["tag_name"]
    levels["planner_tag_code"] = levels.apply(planner_pressure_tag, axis=1)
    levels["planner_tag"] = levels["planner_tag_code"].map(planner_tag_name)
    levels["risk_note"] = levels.apply(
        lambda row: planner_risk_note(row, str(row["planner_tag_code"])), axis=1
    )
    levels["slot_guidance"] = levels["planner_tag_code"].map(planner_slot_guidance)
    levels["feature_summary"] = (
        levels["first_level_behavior_label"].map(PRESSURE_SHORT)
        + "；消耗" + levels["consume_band"] + "；失败流失" + levels["fail_band"]
    )
    levels["diagnostic_action"] = levels["tag_code"].map(TAG_ACTIONS)
    levels["action"] = levels["planner_tag_code"].map(PLANNER_TAG_ACTIONS)
    duplicate_rows = build_duplicate_labels(
        online, instances, levels, consume_center, consume_scale, fail_center, fail_scale
    )
    levels = apply_level_label_method(levels, duplicate_rows, level_label_method)
    levels = assign_recommended_positions(levels, sequence_strategy)
    diversity_before = dict(levels.attrs.get("sequence_diversity_before", {}))
    diversity_after = dict(levels.attrs.get("sequence_diversity_after", {}))
    tag_counts = Counter(levels["tag_code"])
    planner_tag_counts = Counter(levels["planner_tag_code"])
    level_min, level_max = int(levels["level_index"].min()), int(levels["level_index"].max())
    rows = [{
        "original_level_index": int(row.level_index), "level_id": row.level_id,
        "level_template_id": row.level_template_id, "planner_tag": row.planner_tag,
        "planner_tag_code": row.planner_tag_code, "slot_guidance": row.slot_guidance,
        "named_tag": row.named_tag, "tag_code": row.tag_code, "feature_summary": row.feature_summary,
        "action": row.action, "risk_note": row.risk_note,
        "extreme_negative_evidence": bool(row.extreme_negative_evidence),
        "classification_label": row.classification_label,
        "level_label_method_code": row.level_label_method_code,
        "level_label_method_name": row.level_label_method_name,
        "weighted_planner_tag": row.weighted_planner_tag,
        "weighted_risk_note": row.weighted_risk_note,
        "dominant_duplicate_planner_tag": row.dominant_duplicate_planner_tag,
        "dominant_duplicate_risk_note": row.dominant_duplicate_risk_note,
        "dominant_duplicate_id": row.dominant_duplicate_id,
        "dominant_duplicate_para": row.dominant_duplicate_para,
        "dominant_duplicate_enter_users": int(row.dominant_duplicate_enter_users),
        "dominant_duplicate_enter_share": float(row.dominant_duplicate_enter_share),
        "recommended_level_index": (
            None if pd.isna(row.recommended_level_index) else int(row.recommended_level_index)
        ),
        "candidate_level_index": None if pd.isna(row.recommended_level_index) else int(row.recommended_level_index),
        "placement_status": row.placement_status,
    } for row in levels.sort_values(["recommended_level_index", "level_index"], na_position="last").itertuples(index=False)]
    audit_columns = [
        "level_index", "level_id", "level_template_id", "first_level_behavior_label",
        "pressure_order", "enter_users", "consume", "consume_z", "consume_band",
        "fail_loss", "fail_z", "fail_band", "tag_code", "named_tag",
        "planner_tag_code", "planner_tag", "slot_guidance", "action", "risk_note",
        "extreme_negative_evidence", "diagnostic_action",
        "level_label_method_code", "level_label_method_name",
        "weighted_planner_tag_code", "weighted_planner_tag", "weighted_risk_note",
        "dominant_duplicate_planner_code", "dominant_duplicate_planner_tag",
        "dominant_duplicate_risk_note",
        "dominant_duplicate_id", "dominant_duplicate_para",
        "dominant_duplicate_enter_users", "dominant_duplicate_enter_share",
        "first_any_friction_share", "first_compound_high_burden_share",
        "first_L4_share", "first_L3_share", "first_L2_share", "first_L1_share", "first_L0_share",
        "sequence_score",
        "sequence_risk_bucket", "sequence_slot_digit", "sequence_role",
        "sequence_strategy_code", "sequence_strategy_name",
        "recommended_level_index", "placement_status", "classification_label",
    ]
    audit_rows = levels.sort_values("level_index")[audit_columns].to_dict(orient="records")
    metadata = {
        "level_range": f"{level_min}-{level_max}", "level_count": len(levels),
        "consume_center": consume_center, "consume_scale": consume_scale,
        "fail_loss_center": fail_center, "fail_loss_scale": fail_scale,
        "band_rule": "稳健z值≤-2极低，(-2,-1]偏低，(-1,1)正常，[1,2)偏高，≥2极高",
        "sequence_strategy_code": sequence_strategy,
        "sequence_strategy_name": SEQUENCE_STRATEGIES[sequence_strategy]["name"],
        "sequence_strategy_description": SEQUENCE_STRATEGIES[sequence_strategy]["description"],
        "sequence_bucket_to_digit": list(SEQUENCE_STRATEGIES[sequence_strategy]["bucket_to_digit"]),
        "sequence_ten_level_roles": list(SEQUENCE_STRATEGIES[sequence_strategy]["ten_level_roles"]),
        "sequence_ten_level_plan": _sequence_plan(sequence_strategy),
        "sequence_cycle_display": " → ".join(
            f"尾号{item['digit']}·{item['role']}" for item in _sequence_plan(sequence_strategy)
        ),
        "sequence_bucket_to_digit_display": "、".join(
            f"风险桶{bucket + 1}→尾号{digit}"
            for bucket, digit in enumerate(SEQUENCE_STRATEGIES[sequence_strategy]["bucket_to_digit"])
        ),
        "sequence_rule": SEQUENCE_STRATEGIES[sequence_strategy]["description"],
        "level_label_method_code": level_label_method,
        "level_label_method_name": LEVEL_LABEL_METHODS[level_label_method]["name"],
        "level_label_method_description": LEVEL_LABEL_METHODS[level_label_method]["description"],
        "sequence_diversity_before": diversity_before,
        "sequence_diversity_after": diversity_after,
        "raw_combination_count": int(levels["classification_label"].nunique()),
        "named_tag_count": len(PLANNER_TAG_DEFINITIONS),
        "diagnostic_tag_count": 8,
        "optimize_count": int(planner_tag_counts.get("PX", 0)),
        "formal_sequence_ready": sum(planner_tag_counts.get(code, 0) for code in UNPLACED_PLANNER_CODES) == 0,
        "partial_sequence_ready": True,
        "blank_position_count": int(sum(planner_tag_counts.get(code, 0) for code in UNPLACED_PLANNER_CODES)),
        "tag_definitions": [
            {"code": code, "name": name, "definition": definition, "count": int(tag_counts.get(code, 0))}
            for code, (name, definition) in TAG_DEFINITIONS.items()
        ],
        "planner_tag_definitions": [
            {"code": code, "name": name, "definition": definition, "count": int(planner_tag_counts.get(code, 0))}
            for code, (name, definition) in PLANNER_TAG_DEFINITIONS.items()
        ] + ([{
            "code": "PS", "name": "样本不足",
            "definition": "标签来源副本未形成稳定行为标签；原位留空并先补样",
            "count": int(planner_tag_counts.get("PS", 0)),
        }] if planner_tag_counts.get("PS", 0) else []),
    }
    final_tag_by_level = levels.set_index("level_index")["planner_tag"].to_dict()
    selected_dup_by_level = levels.set_index("level_index")["dominant_duplicate_id"].astype(str).to_dict()
    for duplicate_row in duplicate_rows:
        level_index = int(duplicate_row["level_index"])
        duplicate_row["level_planner_tag"] = final_tag_by_level.get(level_index, "")
        duplicate_row["is_dominant_online_duplicate"] = int(
            str(duplicate_row.get("attempt_phase")) == "首局"
            and str(duplicate_row.get("level_dup_para_id")) == selected_dup_by_level.get(level_index, "")
        )
    return _json_safe({
        "metadata": metadata,
        "rows": rows,
        "audit_rows": audit_rows,
        "duplicate_rows": duplicate_rows,
    })


def run_online_analysis(
    source_files: Sequence[str | Path],
    settings: Mapping[str, Any] | None,
    player_job: Any,
    work_dir: str | Path,
    output_dir: str | Path,
    progress_callback: ProgressCallback | None = None,
    log_callback: LogCallback | None = None,
) -> dict[str, Any]:
    """Run phase two and write the seven-column recommendation workbook."""
    work, output = Path(work_dir), Path(output_dir)
    work.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    _notify(progress_callback, 5, "正在校验线上关卡数据与第一阶段范围")
    audit = preflight_online(source_files, settings, player_job, work)
    if audit.get("errors"):
        raise InputValidationError("线上关卡数据预检查未通过", audit)
    online = _read_csv(Path(str(audit["merged_csv_path"])))
    online["level_index"] = pd.to_numeric(online["level_index"], errors="raise").astype(int)
    online["进入人数"] = pd.to_numeric(online["进入人数"], errors="raise")
    online["人均消耗"] = pd.to_numeric(online["人均消耗"], errors="raise")
    online["失败流失"] = _parse_percent(online["失败流失"])
    labels_path = _resolve_level_labels(player_job)
    labels = _read_csv(labels_path)
    instances_path = _resolve_instance_labels(player_job)
    instances = _read_csv(instances_path) if instances_path else None
    sequence_strategy = str(audit.get("sequence_strategy") or DEFAULT_SEQUENCE_STRATEGY)
    level_label_method = str(
        audit.get("level_label_method") or DEFAULT_LEVEL_LABEL_METHOD
    )
    _notify(progress_callback, 35, "正在按进入人数加权聚合消耗与失败流失")
    payload = analyze_online_levels(
        online, labels, sequence_strategy, instances, level_label_method
    )
    _log(log_callback, f"人均消耗稳健中心: {payload['metadata']['consume_center']:.4f}")
    _log(log_callback, f"失败流失稳健中心: {payload['metadata']['fail_loss_center']:.4%}")
    _log(log_callback, f"排关方案: {payload['metadata']['sequence_strategy_name']} ({sequence_strategy})")
    _log(log_callback, f"关卡标签聚合: {payload['metadata']['level_label_method_name']} ({level_label_method})")
    _notify(progress_callback, 65, f"正在按{payload['metadata']['sequence_strategy_name']}生成推荐序列")
    recommendation = pd.DataFrame(payload["rows"])
    visible = recommendation.rename(columns={
        "original_level_index": "原关卡号", "level_id": "关卡ID",
        "level_template_id": "模板ID", "planner_tag": "排关压力标签",
        "slot_guidance": "适合位置", "named_tag": "内部三维诊断",
        "feature_summary": "三维证据", "risk_note": "风险备注", "action": "建议处理",
        "recommended_level_index": "新推荐序列后关卡号",
        "level_label_method_name": "最终标签口径",
        "weighted_planner_tag": "全部首局实例加权标签",
        "dominant_duplicate_planner_tag": "主副本代表标签",
        "dominant_duplicate_id": "标签来源副本号",
        "dominant_duplicate_para": "标签来源副本参数",
        "dominant_duplicate_enter_users": "来源副本进入人数",
        "dominant_duplicate_enter_share": "来源副本进入占比",
        "placement_status": "排位状态",
    })[["原关卡号", "关卡ID", "模板ID", "最终标签口径", "排关压力标签",
         "全部首局实例加权标签", "主副本代表标签", "标签来源副本号", "标签来源副本参数",
         "来源副本进入人数", "来源副本进入占比", "适合位置", "内部三维诊断", "三维证据", "风险备注", "建议处理",
         "排位状态", "新推荐序列后关卡号"]]
    recommendation_path = output / "关卡分类与推荐序列.csv"
    audit_path = output / "三维分类分析审计.csv"
    duplicate_path = output / "关卡副本标签.csv"
    workbook_path = output / f"{payload['metadata']['level_range']}关关卡分类与推荐序列.xlsx"
    metadata_path = output / "三维分类口径.json"
    visible.to_csv(recommendation_path, index=False, encoding="utf-8-sig")
    pd.DataFrame(payload["audit_rows"]).to_csv(audit_path, index=False, encoding="utf-8-sig")
    duplicate_visible = pd.DataFrame(payload.get("duplicate_rows", [])).rename(columns={
        "level_index": "关卡号", "level_id": "关卡ID", "level_template_id": "模板ID",
        "level_dup_para_id": "副本号", "level_dup_para": "副本参数",
        "attempt_phase": "对局阶段", "player_count": "玩家数",
        "behavior_layer": "实例行为层", "layer_stability": "稳定性",
        "primary_high_axes": "主要高负担维度", "duplicate_planner_tag": "副本压力标签",
        "level_planner_tag": "关卡综合压力标签", "slot_guidance": "适合位置",
        "enter_users": "线上进入人数", "consume": "人均消耗", "consume_band": "消耗档位",
        "fail_loss": "失败流失", "fail_band": "流失档位", "action": "建议处理",
        "duplicate_risk_note": "副本风险备注",
    })
    duplicate_visible.to_csv(duplicate_path, index=False, encoding="utf-8-sig")
    _notify(progress_callback, 80, "正在生成config_level_list重排副本")
    level_list_source = Path(str(audit["level_list_source"]["path"]))
    level_list_export: dict[str, Any] | None = build_level_list_reorder_copy(
        level_list_source,
        payload["rows"],
        output,
        sequence_strategy,
    )
    payload["metadata"]["level_list_source"] = level_list_export["metadata"]
    payload["level_list_audit_rows"] = level_list_export["audit_rows"]
    if payload["metadata"].get("blank_position_count"):
        _log(
            log_callback,
            f"共{payload['metadata']['blank_position_count']}关待优化或补样；"
            "其原位在配置副本中留空，其余合理关已正常排位。",
        )
    _write_json(metadata_path, payload["metadata"])
    _notify(progress_callback, 90, "正在写出推荐序列与分析审计工作簿")
    write_stage2_workbook(workbook_path, payload)
    workbook_audit = inspect_xlsx(workbook_path)
    _notify(progress_callback, 100, "第二阶段分析完成")
    result = {
        "status": "completed", "workbook_path": str(workbook_path),
        "recommendation_csv_path": str(recommendation_path), "audit_csv_path": str(audit_path),
        "duplicate_labels_csv_path": str(duplicate_path),
        "metadata_path": str(metadata_path), "metadata": payload["metadata"],
        "audit": audit, "workbook_audit": workbook_audit,
    }
    if level_list_export is not None:
        result.update({
            "level_list_copy_path": level_list_export["level_list_copy_path"],
            "level_list_audit_path": level_list_export["level_list_audit_path"],
            "level_list_metadata": level_list_export["metadata"],
        })
    _write_json(output / "result.json", result)
    return _json_safe(result)


__all__ = [
    "InputValidationError", "preflight_player", "run_player_analysis",
    "preflight_online", "run_online_analysis", "robust_center_scale", "robust_band",
    "named_tag", "assign_recommended_positions", "analyze_online_levels",
    "estimate_sample_recommendation",
    "sequence_adjacency_metrics", "build_level_list_reorder_copy",
    "SEQUENCE_STRATEGIES", "DEFAULT_SEQUENCE_STRATEGY",
]
