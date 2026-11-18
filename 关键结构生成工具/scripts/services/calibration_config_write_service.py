"""Safely append or update target-consumption calibration rows in config_level."""

from __future__ import annotations

import csv
import errno
import hashlib
import os
import re
import shutil
import tempfile
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from puzzle_core.errors import InputValidationError
from services.parameter_recommendation_service import discover_config_sets
from services.profile_service import Profile

try:
    import fcntl
except ImportError:
    fcntl = None


CONFIG_LEVEL = "config_level.csv"
TOOL_ROOT = Path(__file__).resolve().parents[2]
DUP_COLUMNS = tuple(f"DupPara{index}" for index in range(1, 11))
_DUP_PATTERN = re.compile(r"^\d+\|\d+\|\d+\|\d+$")
_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]+")


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


def _parse_naming(naming):
    if not isinstance(naming, Mapping):
        raise InputValidationError("请选择关卡 ID 命名方式")
    mode = naming.get("mode")
    if mode in ("original_level", "output_template"):
        return mode, "newv1", ""
    if mode not in ("custom_template", "custom_level"):
        raise InputValidationError("关卡 ID 命名方式不合法")
    prefix, suffix = (str(naming.get(key) or "").strip() for key in ("prefix", "suffix"))
    if not (prefix or suffix):
        raise InputValidationError("自定义命名请填写前缀或后缀")
    if any(value and not _ID_PATTERN.fullmatch(value) for value in (prefix, suffix)):
        raise InputValidationError("关卡 ID 前后缀仅支持字母、数字、下划线、连字符和点")
    return mode, prefix, suffix


def _named_ids(calibration_rows, naming, occupied):
    mode, prefix, suffix = _parse_naming(naming)
    for item in calibration_rows:
        if not isinstance(item, Mapping):
            raise InputValidationError("校准结果行格式不合法")
        history_id = str(item.get("history_level_id") or "").strip()
        resolved_ids = {str(value).strip() for value in (item.get("level_ids") or []) if str(value).strip()}
        original_id = history_id or (next(iter(resolved_ids)) if len(resolved_ids) == 1 else "")
        if mode in ("original_level", "custom_level"):
            identifier = original_id
            if not identifier:
                raise InputValidationError(f"{item.get('output_template_id', '')} 缺少唯一的原关卡 ID")
        else:
            key = "output_template_id" if mode == "output_template" else "source_template_id"
            identifier = str(item.get(key) or "").strip()
        base = f"{prefix}{identifier}{suffix}"
        if not identifier or not _ID_PATTERN.fullmatch(base) or len(base) > 180:
            raise InputValidationError(f"{identifier or '当前模板'} 无法生成合法的关卡 ID")
        level_id, version = base, 2
        while level_id in occupied:
            level_id = f"{base}v{version}"
            version += 1
        if len(level_id) > 180:
            raise InputValidationError("追加版本号后的关卡 ID 超过 180 字符，请缩短前后缀")
        occupied.add(level_id)
        yield level_id, original_id, resolved_ids


def _config_level_path(profile, config_set_id):
    if not profile.ulinkconfig_root:
        raise InputValidationError("请先设置 ulinkconfig 目录")
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
    return root, selected_id, level_path


@contextmanager
def _config_write_lock(backup_root, level_path):
    if fcntl is None:
        raise InputValidationError("当前平台不支持配置写入锁，请导出草案")
    lock_name = hashlib.sha256(os.fsencode(level_path)).hexdigest()
    lock_path = backup_root / f"{lock_name}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "r+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield


def write_calibration_config(
    profile: Profile,
    *,
    config_set_id: str,
    calibration_rows: Sequence[Mapping[str, Any]],
    naming: Mapping[str, Any] | None = None,
    description: str | None = None,
    preview: bool = False,
) -> dict[str, Any]:
    """Preview or write reviewed config names using the same allocation rules."""
    if not calibration_rows or len(calibration_rows) > 500:
        raise InputValidationError("待写入校准结果数量不合法")
    if description is not None and (
        not isinstance(description, str) or len(description) > 500 or any(char in description for char in "\x00\r\n")
    ):
        raise InputValidationError("批量描述须为不超过 500 字的单行文字")
    root, selected_id, level_path = _config_level_path(profile, config_set_id)
    if preview:
        header, table_rows = _read_table(level_path)
        level_column = header.index("LevelId")
        names = list(_named_ids(calibration_rows, naming, {row[level_column].strip() for row in table_rows}))
        return {"config_set_id": selected_id, "level_ids": [item[0] for item in names]}
    tool_root = TOOL_ROOT.resolve(strict=False)
    backup_root = (tool_root / "运行数据" / "配置备份").resolve(strict=False)
    if tool_root.is_relative_to(root) or backup_root.is_relative_to(root):
        raise InputValidationError("工具备份目录不能位于配置仓库内；未修改配置")
    if not backup_root.is_relative_to(tool_root):
        raise InputValidationError("配置备份目录必须位于工具目录内，不能通过符号链接转到其他位置；未修改配置")
    backup_root.mkdir(parents=True, exist_ok=True)
    with _config_write_lock(backup_root, level_path):
        return _write_config_level(root, selected_id, level_path, calibration_rows, naming, description, tool_root, backup_root)


def _write_config_level(root, selected_id, level_path, calibration_rows, naming, description, tool_root, backup_root):
    original_stat = level_path.stat()
    header, table_rows = _read_table(level_path)
    columns = {name: header.index(name) for name in header}
    if description is not None and "Des" not in columns:
        raise InputValidationError("config_level.csv 缺少 Des 列")
    updated_count = 0
    appended_count = 0
    written_templates: list[str] = []
    output_counts = Counter(
        str(item.get("output_template_id", "")).strip()
        for item in calibration_rows if isinstance(item, Mapping)
    )
    written_level_ids: list[str] = []
    occupied = {row[columns["LevelId"]].strip() for row in table_rows}
    named_ids = list(_named_ids(calibration_rows, naming, occupied)) if naming is not None else None

    for index, item in enumerate(calibration_rows):
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
        history_level_id = str(item.get("history_level_id") or "").strip()
        output_rows = [row for row in table_rows if row[columns["TemplateId"]].strip() == output_id]
        base_level_id = _new_level_id(output_id)
        scoped = bool(item.get("history_level_split")) or output_counts[output_id] > 1 or any(
            row[columns["LevelId"]].strip().startswith(f"{base_level_id}_")
            for row in output_rows
        )
        if named_ids is not None:
            level_id, original_id, resolved_ids = named_ids[index]
            history_level_id = original_id
            source_level_ids = resolved_ids
            existing = []
            scoped = bool(original_id) or scoped
        elif scoped and not history_level_id:
            raise InputValidationError(f"{output_id} 对应多套校准结果，写入时必须提供各自的原关卡 ID")
        elif scoped:
            suffix = hashlib.sha256(history_level_id.encode("utf-8")).hexdigest()[:16]
            level_id = f"{base_level_id}_{suffix}"
            existing = [row for row in output_rows if row[columns["LevelId"]].strip() == level_id]
        else:
            level_id = base_level_id
            existing = output_rows
        if len(existing) > 1:
            raise InputValidationError(f"{output_id} 在 config_level 中重复，未自动覆盖")
        if existing:
            target = existing[0]
            updated_count += 1
        else:
            if scoped and history_level_id:
                source_level_ids = {history_level_id}
            source_candidates = [
                row for row in table_rows
                if row[columns["TemplateId"]].strip() == config_source_id
                and (not source_level_ids or row[columns["LevelId"]].strip() in source_level_ids)
            ]
            if not source_candidates and not scoped:
                source_candidates = [
                    row for row in table_rows
                    if row[columns["TemplateId"]].strip() == config_source_id
                ]
            if not source_candidates:
                raise InputValidationError(
                    f"未找到已解析配置源 {config_source_id} 的原关卡配置"
                    f"（{history_level_id or source_id}），未写入任何内容"
                )
            if (scoped or named_ids is not None) and len(source_candidates) != 1:
                raise InputValidationError(f"{history_level_id or config_source_id} 有多条原关卡配置，未写入任何内容")
            if any(row[columns["LevelId"]].strip() == level_id for row in table_rows):
                raise InputValidationError(f"目标关卡 ID {level_id} 已用于其他模板")
            target = list(source_candidates[0])
            target[columns["LevelId"]] = level_id
            target[columns["TemplateId"]] = output_id
            table_rows.append(target)
            appended_count += 1
        written_level_id = target[columns["LevelId"]].strip()
        if written_level_id in written_level_ids:
            raise InputValidationError(f"同一目标关卡 {written_level_id} 被选中多次，未写入任何内容")
        written_level_ids.append(written_level_id)
        for column, value in zip(DUP_COLUMNS, dup_params):
            target[columns[column]] = value
        strategy_group_id = str(item.get("strategy_group_id", "")).strip()
        if strategy_group_id and "StrategyGroupId" in columns:
            target[columns["StrategyGroupId"]] = strategy_group_id
        if description is not None:
            target[columns["Des"]] = description
        for remark_column in ("Remark", "Remarks", "Comment", "备注"):
            if remark_column in columns:
                target[columns[remark_column]] = "线上200-300关键结构模板洗白后ai生成"
                break
        written_templates.append(output_id)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
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
        current = level_path.stat()
        if (current.st_ino, current.st_size, current.st_mtime_ns) != (original_stat.st_ino, original_stat.st_size, original_stat.st_mtime_ns):
            raise InputValidationError("config_level 已被其他操作修改，请重新写入")
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
        "level_ids": written_level_ids,
    }


__all__ = ["write_calibration_config"]
