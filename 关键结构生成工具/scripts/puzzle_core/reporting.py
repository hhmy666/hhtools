"""Pure renderers for authoritative and human-readable generation reports.

This module deliberately performs no filesystem I/O.  Callers provide plain
``Mapping``/``Sequence`` values and decide where the returned ``str`` or
``bytes`` is persisted.  A single comparison-table mapping always represents
one formal output template; the renderer never folds several variants into one
row.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping, Sequence
from typing import Any, Dict, Iterable, Tuple

from .planning import canonical_json


FIXED_CSV_HEADERS: Tuple[str, ...] = (
    "线上关卡号",
    "关卡 ID",
    "模板 ID",
    "新的模板 ID",
    "替换 A Tile 数量",
    "替换 B Tile 数量",
    "使用的结构块",
    "替换后的结构块数量",
    "谜题结构完整解首消步数",
)

AUDIT_CSV_HEADERS: Tuple[str, ...] = (
    "源文件 SHA-256",
    "输出文件 SHA-256",
    "谜题生成种子",
    "校验状态",
    "失败原因",
    "塔内结构块数量",
    "塔外结构块数量",
    "被夹 A Tile 唯一数量",
    "被夹 B Tile 唯一数量",
    "源模板 Tile 总数量",
    "源模板既有结构块数量",
    "输出结构块总数量",
    "有效结构块数量上限",
    "有效最高层限制",
    "结构块实际最高层",
    "相邻层单块全遮数量",
    "跨层单块全遮诊断数量",
    "联合全遮数量",
    "初始入口数量",
    "外部阻挡 Tile 唯一数量",
    "组合验证结论",
)

CSV_HEADERS: Tuple[str, ...] = FIXED_CSV_HEADERS + AUDIT_CSV_HEADERS

TOWER_STEP_TEXT = "N/A（塔内实时调控）"


class ReportingError(ValueError):
    """Raised when a plain report record is structurally ambiguous."""


_FIELD_ALIASES: Dict[str, Tuple[str, ...]] = {
    "线上关卡号": ("online_level_number", "online_level", "level_number"),
    "关卡 ID": ("level_id",),
    "模板 ID": ("source_template_id", "template_id"),
    "新的模板 ID": ("output_template_id", "new_template_id"),
    "替换 A Tile 数量": ("replaced_a_count", "replacement_a_count"),
    "替换 B Tile 数量": ("replaced_b_count", "replacement_b_count"),
    "使用的结构块": ("structure_types", "new_structure_types"),
    "替换后的结构块数量": (
        "new_structure_count",
        "replaced_structure_count",
        "new_structure_counts",
    ),
    "谜题结构完整解首消步数": ("puzzle_steps", "structure_steps"),
    "源文件 SHA-256": ("source_sha256", "source_sha"),
    "输出文件 SHA-256": ("output_sha256", "output_sha"),
    "谜题生成种子": ("generation_seed", "seed"),
    "校验状态": ("validation_status", "status"),
    "失败原因": ("failure_reason", "error"),
    "塔内结构块数量": ("tower_structure_count", "tower_inside_count"),
    "塔外结构块数量": ("non_tower_structure_count", "tower_outside_count"),
    "被夹 A Tile 唯一数量": ("nested_a_count", "covered_a_count"),
    "被夹 B Tile 唯一数量": ("nested_b_count", "covered_b_count"),
    "源模板 Tile 总数量": ("source_total_tile_count",),
    "源模板既有结构块数量": (
        "source_existing_structure_count",
    ),
    "输出结构块总数量": ("output_structure_count",),
    "有效结构块数量上限": ("effective_structure_count_limit",),
    "有效最高层限制": ("effective_max_structure_layer",),
    "结构块实际最高层": ("highest_structure_layer",),
    "相邻层单块全遮数量": ("adjacent_single_full_count",),
    "跨层单块全遮诊断数量": (
        "cross_layer_single_full_count",
    ),
    "联合全遮数量": ("joint_full_cover_count",),
    "初始入口数量": ("initial_entry_count",),
    "外部阻挡 Tile 唯一数量": ("external_blocker_count",),
    "组合验证结论": ("combination_conclusion_text",),
}

_STEP_SIGNATURE_PATTERN = re.compile(r"^s([4-9])(?:s([4-9]))?$")
_SUCCESS_STATUSES = frozenset(
    (
        "success",
        "passed",
        "valid",
        "verified",
        "ok",
        "成功",
        "通过",
        "已验证",
    )
)
_MANUAL_REVIEW_STATUSES = frozenset(
    (
        "通过（证据不足，已人工确认导出）",
        "规则结论未通过（用户确认导出）",
        "未达推荐条件（用户确认导出）",
        "依据不完整（用户确认导出）",
    )
)
_FAILURE_STATUSES = frozenset(
    ("failure", "failed", "invalid", "error", "失败", "未通过", "校验失败")
)


def _is_plain_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    )


def _require_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReportingError(f"{context} must be a Mapping")
    return value


def _require_rows(rows: Any) -> Tuple[Mapping[str, Any], ...]:
    if not _is_plain_sequence(rows):
        raise ReportingError("rows must be a Sequence of Mapping records")
    result = []
    for index, row in enumerate(rows):
        result.append(_require_mapping(row, f"rows[{index}]"))
    return tuple(result)


def _lookup(record: Mapping[str, Any], header: str, default: Any = "") -> Any:
    if header in record:
        return record[header]
    for key in _FIELD_ALIASES[header]:
        if key in record:
            return record[key]
    return default


def _lookup_any(record: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in record:
            return record[key]
    return None


def _tower_only(record: Mapping[str, Any]) -> bool:
    if record.get("is_tower_only") is True:
        return True
    tower_mode = record.get("tower_mode")
    if isinstance(tower_mode, str) and tower_mode.strip().lower() in {
        "only",
        "tower_only",
        "tower-only",
        "仅塔内生成",
    }:
        return True
    signature = _lookup_any(record, ("signature", "step_signature"))
    return isinstance(signature, str) and signature.strip().lower() == "tower"


def _unit_step(unit: Any, index: int) -> Tuple[str, Any]:
    if isinstance(unit, Mapping):
        unit_id = _lookup_any(unit, ("unit_id", "id", "name")) or f"U{index + 1}"
        step = _lookup_any(
            unit,
            (
                "actual_structure_step",
                "structure_step",
                "actual_step",
                "step",
            ),
        )
    else:
        unit_id = f"U{index + 1}"
        step = unit
    if step is None or step == "":
        raise ReportingError(f"puzzle unit {index + 1} is missing its actual step")
    return str(unit_id), step


def _format_unit_steps(units: Sequence[Any]) -> str:
    if len(units) not in (1, 2):
        raise ReportingError("puzzle step report requires one or two units")
    normalized = [_unit_step(unit, index) for index, unit in enumerate(units)]
    if len(normalized) == 1:
        return _scalar_text(normalized[0][1])
    return "；".join(
        f"{unit_id}={_scalar_text(step)}" for unit_id, step in normalized
    )


def _format_puzzle_steps(record: Mapping[str, Any]) -> str:
    if _tower_only(record):
        return TOWER_STEP_TEXT

    units = _lookup_any(record, ("puzzle_units", "units"))
    if units is not None:
        if not _is_plain_sequence(units):
            raise ReportingError("puzzle_units must be a Sequence")
        return _format_unit_steps(units)

    raw_steps = _lookup(record, "谜题结构完整解首消步数", None)
    if raw_steps is not None and raw_steps != "":
        if _is_plain_sequence(raw_steps):
            return _format_unit_steps(raw_steps)
        return _scalar_text(raw_steps)

    signature = _lookup_any(record, ("signature", "step_signature"))
    if isinstance(signature, str):
        match = _STEP_SIGNATURE_PATTERN.fullmatch(signature.strip().lower())
        if match:
            steps = [int(value) for value in match.groups() if value is not None]
            return _format_unit_steps(steps)
    return ""


def _scalar_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Mapping) or _is_plain_sequence(value):
        return canonical_json(value)
    return str(value)


def _format_structure_types(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if _is_plain_sequence(value):
        return "、".join(_scalar_text(item) for item in value)
    return _scalar_text(value)


def _format_structure_count(record: Mapping[str, Any]) -> str:
    value = _lookup(record, "替换后的结构块数量", None)
    if isinstance(value, Mapping):
        counts = tuple(value.values())
        if all(
            isinstance(item, int) and not isinstance(item, bool) for item in counts
        ):
            return str(sum(counts))
    if value is not None and value != "":
        return _scalar_text(value)

    replaced_a = _lookup(record, "替换 A Tile 数量", None)
    replaced_b = _lookup(record, "替换 B Tile 数量", None)
    if all(
        isinstance(item, int) and not isinstance(item, bool)
        for item in (replaced_a, replaced_b)
    ):
        return str(replaced_a + replaced_b)
    return ""


def _nested_count(record: Mapping[str, Any], tile_type: str) -> Any:
    header = f"被夹 {tile_type} Tile 唯一数量"
    direct = _lookup(record, header, None)
    if direct is not None:
        return direct
    nested = _lookup_any(record, ("nested_counts", "covered_counts"))
    if isinstance(nested, Mapping):
        return nested.get(tile_type, nested.get(tile_type.lower(), ""))
    return ""


def _tower_count(record: Mapping[str, Any], inside: bool) -> Any:
    header = "塔内结构块数量" if inside else "塔外结构块数量"
    direct = _lookup(record, header, None)
    if direct is not None:
        return direct
    counts = _lookup_any(record, ("tower_counts", "structure_location_counts"))
    if isinstance(counts, Mapping):
        keys = ("inside", "tower") if inside else ("outside", "non_tower")
        value = _lookup_any(counts, keys)
        return "" if value is None else value
    return ""


def _normalized_csv_row(record: Mapping[str, Any]) -> Dict[str, str]:
    # A nested outputs list would make the row-to-template relationship
    # ambiguous.  Expansion belongs in the caller, before rendering.
    outputs = record.get("outputs")
    if outputs is not None:
        raise ReportingError(
            "each row must describe one formal template; expand outputs before rendering"
        )

    result = {header: _scalar_text(_lookup(record, header)) for header in CSV_HEADERS}
    result["使用的结构块"] = _format_structure_types(
        _lookup(record, "使用的结构块", None)
    )
    result["替换后的结构块数量"] = _format_structure_count(record)
    result["谜题结构完整解首消步数"] = _format_puzzle_steps(record)
    result["塔内结构块数量"] = _scalar_text(_tower_count(record, True))
    result["塔外结构块数量"] = _scalar_text(_tower_count(record, False))
    result["被夹 A Tile 唯一数量"] = _scalar_text(_nested_count(record, "A"))
    result["被夹 B Tile 唯一数量"] = _scalar_text(_nested_count(record, "B"))
    return result


def render_authoritative_json(payload: Any) -> bytes:
    """Render a canonical JSON authority record as UTF-8 without a BOM.

    No trailing newline is added, so hashing the returned bytes is stable.
    """

    if not isinstance(payload, Mapping) and not _is_plain_sequence(payload):
        raise ReportingError("authoritative JSON root must be a Mapping or Sequence")
    return canonical_json(payload).encode("utf-8")


def render_comparison_csv(rows: Sequence[Mapping[str, Any]]) -> bytes:
    """Render the product comparison table as Excel-friendly UTF-8-SIG CSV."""

    records = _require_rows(rows)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=CSV_HEADERS,
        extrasaction="ignore",
        lineterminator="\r\n",
    )
    writer.writeheader()
    for record in records:
        writer.writerow(_normalized_csv_row(record))
    return output.getvalue().encode("utf-8-sig")


def _markdown_cell(value: Any) -> str:
    text = _scalar_text(value)
    return (
        text.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", "<br>")
        .replace("\r", "<br>")
        .replace("\n", "<br>")
    )


def _status_kind(status: str, failure_reason: str) -> str:
    normalized = status.strip().lower()
    if normalized in _MANUAL_REVIEW_STATUSES:
        return "manual_review"
    if normalized in _FAILURE_STATUSES or failure_reason:
        return "failed"
    if normalized in _SUCCESS_STATUSES:
        return "passed"
    return "other"


def render_batch_markdown(
    batch: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> str:
    """Render a compact human-readable batch summary in Markdown."""

    batch_record = _require_mapping(batch, "batch")
    records = _require_rows(rows)
    normalized_rows = tuple(_normalized_csv_row(record) for record in records)

    passed = 0
    failed = 0
    manual_review = 0
    tower = 0
    for source, normalized in zip(records, normalized_rows):
        kind = _status_kind(normalized["校验状态"], normalized["失败原因"])
        passed += kind == "passed"
        failed += kind == "failed"
        manual_review += kind == "manual_review"
        tower += _tower_only(source)

    batch_id = _lookup_any(batch_record, ("batch_id", "批次 ID")) or "未提供"
    batch_status = _lookup_any(batch_record, ("status", "batch_status", "批次状态"))
    created_at = _lookup_any(batch_record, ("created_at", "completed_at", "生成时间"))

    lines = [
        "# AI 谜题结构生成批次摘要",
        "",
        f"- 批次 ID：{_markdown_cell(batch_id)}",
        f"- 正式模板：{len(normalized_rows)}",
        f"- 校验通过：{passed}",
        f"- 校验失败：{failed}",
        f"- 人工确认导出（非推荐）：{manual_review}",
        f"- 仅塔内：{tower}",
    ]
    if batch_status is not None:
        lines.append(f"- 批次状态：{_markdown_cell(batch_status)}")
    if created_at is not None:
        lines.append(f"- 生成时间：{_markdown_cell(created_at)}")

    lines.extend(
        (
            "",
            "## 输出明细",
            "",
            "| 模板 ID | 新的模板 ID | 谜题结构完整解首消步数 | 校验状态 | 失败原因 |",
            "| --- | --- | --- | --- | --- |",
        )
    )
    for row in normalized_rows:
        cells = (
            row["模板 ID"],
            row["新的模板 ID"],
            row["谜题结构完整解首消步数"],
            row["校验状态"],
            row["失败原因"],
        )
        lines.append("| " + " | ".join(_markdown_cell(cell) for cell in cells) + " |")
    overrides = [
        (record, record.get("manual_evidence_override"))
        for record in records
        if isinstance(record.get("manual_evidence_override"), Mapping)
    ]
    if overrides:
        lines.extend(("", "## 人工确认的非推荐结论", ""))
        for record, override in overrides:
            axes = override.get("axes", {})
            missing = override.get("inconclusive_axes", ())
            disproved = override.get("disproved_axes", ())
            lines.append(
                f"- {_markdown_cell(record.get('output_template_id', '未知模板'))}："
                f"已人工确认；证据状态 {_markdown_cell(axes)}；"
                f"未通过轴 {_markdown_cell(disproved)}；"
                f"未完整轴 {_markdown_cell(missing)}。"
            )
    return "\n".join(lines) + "\n"
