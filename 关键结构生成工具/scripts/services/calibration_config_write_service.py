"""Safely append or update target-consumption calibration rows in config_level."""

from __future__ import annotations

import csv
import errno
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from puzzle_core.errors import InputValidationError
from services.parameter_recommendation_service import discover_config_sets
from services.profile_service import Profile


CONFIG_LEVEL = "config_level.csv"
TOOL_ROOT = Path(__file__).resolve().parents[2]
DUP_COLUMNS = tuple(f"DupPara{index}" for index in range(1, 11))
_DUP_PATTERN = re.compile(r"^\d+\|\d+\|\d+\|\d+$")


def _new_level_id(template_id: str) -> str:
    suffix = template_id[len("aipuzzle") :] if template_id.startswith("aipuzzle") else template_id
    return f"newv1{suffix}"


def _read_table(path: Path) -> tuple[list[str], list[list[str]]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream)
            header = next(reader)
            rows = [list(row) for row in reader]
    except (OSError, UnicodeError, csv.Error, StopIteration) as exc:
        raise InputValidationError("config_level.csv 无法读取") from exc
    required = {"LevelId", "TemplateId", *DUP_COLUMNS}
    if not required.issubset(header):
        missing = "、".join(sorted(required.difference(header)))
        raise InputValidationError(f"config_level.csv 缺少字段：{missing}")
    width = len(header)
    for row in rows:
        if len(row) < width:
            row.extend([""] * (width - len(row)))
    return header, rows


def _recommended_dup_params(item: Mapping[str, Any]) -> list[str]:
    raw_duplicates = item.get("duplicates")
    if not isinstance(raw_duplicates, list):
        raise InputValidationError("校准结果缺少副本参数")
    by_index: dict[int, str] = {}
    for duplicate in raw_duplicates:
        if not isinstance(duplicate, Mapping):
            continue
        try:
            index = int(duplicate.get("duplicate_index"))
        except (TypeError, ValueError):
            continue
        value = str(duplicate.get("recommended_dup", "")).strip()
        if 1 <= index <= 10 and _DUP_PATTERN.fullmatch(value):
            by_index[index] = value
    if set(by_index) != set(range(1, 11)):
        raise InputValidationError("每个模板必须包含完整且合法的 DupPara1–10")
    return [by_index[index] for index in range(1, 11)]


def write_calibration_config(
    profile: Profile,
    *,
    config_set_id: str,
    calibration_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Only replace the reviewed config; keep backup/staging inside this tool."""

    if not calibration_rows or len(calibration_rows) > 500:
        raise InputValidationError("待写入校准结果数量不合法")
    root = Path(profile.ulinkconfig_root or "").expanduser().resolve(strict=True)
    config_sets = discover_config_sets(root)
    selected_id = str(config_set_id or "").strip()
    if not selected_id:
        selected_id = next((item["id"] for item in config_sets if item["id"] == "csv"), config_sets[0]["id"])
    selected = next((item for item in config_sets if item["id"] == selected_id), None)
    if selected is None:
        raise InputValidationError("目标配置集不存在，请刷新后重试")
    level_candidate = root / selected["relative_path"] / CONFIG_LEVEL
    if level_candidate.is_symlink():
        raise InputValidationError("不允许写入符号链接 config_level")
    level_path = level_candidate.resolve(strict=True)
    try:
        level_path.relative_to(root)
    except ValueError as exc:
        raise InputValidationError("config_level 路径越界") from exc
    header, table_rows = _read_table(level_path)
    columns = {name: header.index(name) for name in header}
    updated_count = 0
    appended_count = 0
    written_templates: list[str] = []

    for item in calibration_rows:
        if not isinstance(item, Mapping):
            raise InputValidationError("校准结果行格式不合法")
        source_id = str(item.get("source_template_id", "")).strip()
        config_source_id = str(
            item.get("config_source_template_id") or source_id
        ).strip()
        output_id = str(item.get("output_template_id", "")).strip()
        if (
            not source_id
            or not config_source_id
            or not output_id
            or len(output_id) > 180
        ):
            raise InputValidationError("校准结果缺少合法的原/新模板 ID")
        dup_params = _recommended_dup_params(item)
        source_level_ids = {
            str(value).strip() for value in (item.get("level_ids") or []) if str(value).strip()
        }
        existing = [row for row in table_rows if row[columns["TemplateId"]].strip() == output_id]
        if len(existing) > 1:
            raise InputValidationError(f"{output_id} 在 config_level 中重复，未自动覆盖")
        if existing:
            target = existing[0]
            updated_count += 1
        else:
            source_candidates = [
                row for row in table_rows
                if row[columns["TemplateId"]].strip() == config_source_id
                and (not source_level_ids or row[columns["LevelId"]].strip() in source_level_ids)
            ]
            if not source_candidates:
                source_candidates = [
                    row for row in table_rows
                    if row[columns["TemplateId"]].strip() == config_source_id
                ]
            if not source_candidates:
                raise InputValidationError(
                    f"未找到已解析配置源 {config_source_id} 的原关卡配置"
                    f"（manifest 原模板：{source_id}），未写入任何内容"
                )
            target = list(source_candidates[0])
            target[columns["LevelId"]] = _new_level_id(output_id)
            target[columns["TemplateId"]] = output_id
            table_rows.append(target)
            appended_count += 1
        for column, value in zip(DUP_COLUMNS, dup_params):
            target[columns[column]] = value
        strategy_group_id = str(item.get("strategy_group_id", "")).strip()
        if strategy_group_id and "StrategyGroupId" in columns:
            target[columns["StrategyGroupId"]] = strategy_group_id
        for remark_column in ("Remark", "Remarks", "Comment", "备注"):
            if remark_column in columns:
                target[columns[remark_column]] = "线上200-300关键结构模板洗白后ai生成"
                break
        written_templates.append(output_id)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    tool_root = TOOL_ROOT.resolve(strict=False)
    backup_root = (tool_root / "运行数据" / "配置备份").resolve(strict=False)
    if tool_root.is_relative_to(root) or backup_root.is_relative_to(root):
        raise InputValidationError("工具备份目录不能位于配置仓库内；未修改配置")
    if not backup_root.is_relative_to(tool_root):
        raise InputValidationError("配置备份目录必须位于工具目录内，不能通过符号链接转到其他位置；未修改配置")
    backup_root.mkdir(parents=True, exist_ok=True)
    write_directory = Path(tempfile.mkdtemp(prefix=f"{stamp}_", dir=backup_root))
    backup_path = write_directory / f"{level_path.name}.bak"
    shutil.copy2(level_path, backup_path)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8-sig", newline="", dir=write_directory,
            prefix=f".{level_path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            writer = csv.writer(stream, lineterminator="\r\n")
            writer.writerow(header)
            writer.writerows(table_rows)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copymode(level_path, temporary_path)
        os.replace(temporary_path, level_path)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise InputValidationError(
                "工具目录和配置目录不在同一磁盘，无法原子写入；未修改配置。请导出草案后手动导入。"
            ) from exc
        raise
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return {
        "config_set_id": selected_id,
        "config_level_path": str(level_path.relative_to(root)),
        "backup_path": str(backup_path),
        "backup_relative_path": str(backup_path.relative_to(tool_root)),
        "updated_count": updated_count + appended_count,
        "appended_count": appended_count,
        "replaced_count": updated_count,
        "templates": written_templates,
    }


__all__ = ["write_calibration_config"]
