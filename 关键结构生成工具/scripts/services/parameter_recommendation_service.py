"""Read-only ulinkconfig lookup and DupPara recommendation service.

This module never edits ``config_level.csv`` or ``config_level_list.csv``.
It turns audited AI-puzzle export manifests into a review table that can later
be copied into the team's configuration workflow.
"""

from __future__ import annotations

import csv
import copy
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from puzzle_core.errors import InputValidationError
from puzzle_core.template_reader import parse_level_bytes

from .export_service import REGISTRY_DIRECTORY, REPORTS_DIRECTORY
from .import_service import load_template_id
from .path_service import is_within, real_path, validate_template_id
from .profile_service import Profile


MAX_CONFIG_BYTES = 64 * 1024 * 1024
CONFIG_LEVEL = "config_level.csv"
CONFIG_LEVEL_LIST = "config_level_list.csv"
RANDOM_SEED_GROUP_COLUMN = "randomSeed_groupID"
STRATEGY_GROUP_COLUMN = "StrategyGroupId"
DUP_COLUMNS = tuple(f"DupPara{index}" for index in range(1, 11))
STRATEGIES = {
    "fidelity": {
        "label": "保真副本参数",
        "description": (
            "分别从原 DupPara1–10 扣除本次实际替换的 A/B 结构种类；"
            "有原模板牌量证据时保留主牌种类底线，必要时少扣。"
            "每个副本原有的随机种子和塔幸运值保持不变。"
        ),
        "mode": "preserve_original_gradient",
        "is_default": True,
    },
    "balanced": {
        "label": "通用递减",
        "description": "副本2开始降低 A；副本3开始降低 B；之后每2个副本再降1。",
        "mode": "rebuild_gradient",
        "is_default": False,
    },
    "flat": {
        "label": "保持一致",
        "description": "副本2–10全部沿用新的副本1参数。",
        "mode": "rebuild_gradient",
        "is_default": False,
    },
    "aggressive": {
        "label": "每个副本递减",
        "description": "副本3开始降低 A，副本4开始降低 B；之后每个副本再降1。",
        "mode": "rebuild_gradient",
        "is_default": False,
    },
    "steady": {
        "label": "每2个副本递减",
        "description": "副本3开始降低 A，副本4开始降低 B；之后每2个副本再降1。",
        "mode": "rebuild_gradient",
        "is_default": False,
    },
    "gentle": {
        "label": "每3个副本递减",
        "description": "副本3开始降低 A，副本4开始降低 B；之后每3个副本再降1。",
        "mode": "rebuild_gradient",
        "is_default": False,
    },
}

_GENERATION_PARAMETER_LABELS = {
    "total_count_limit": "结构块数量上限",
    "total_percent_limit": "结构块数量百分比",
    "max_layer": "最高可用层",
    "max_layer_percent": "最高可用层百分比",
    "max_structure_layer_span": "结构块最大层间距",
    "required_pre_gate_recycles": "门槛前回收次数",
    "first_recycle_lead_tiles": "首次回收前铺垫块数",
}
_GENERATION_CHANGE_FIELDS = frozenset(_GENERATION_PARAMETER_LABELS)


def _resolve_config_source_template_id(
    source_template_id: str,
    available_template_ids: Iterable[str],
) -> tuple[str | None, str]:
    """Resolve one manifest source against config without fuzzy matching.

    The generation workflow intentionally names restored source files with one
    trailing ``clean`` marker, while the authoritative config row can retain
    the pre-restoration ID.  Exact matches always win.  The only accepted alias
    removes that single suffix and is used only when it identifies one exact,
    case-sensitive config ID.
    """

    source = str(source_template_id).strip()
    available = {str(value).strip() for value in available_template_ids if str(value).strip()}
    if source in available:
        return source, "exact"
    if not source.endswith("clean") or len(source) <= len("clean"):
        return None, "missing"
    alias = source[: -len("clean")]
    matches = [value for value in available if value == alias]
    if len(matches) == 1:
        return matches[0], "trailing_clean_alias"
    return None, "missing"


def _effective_raw_plans(
    request: Mapping[str, Any], template_key: str
) -> list[dict[str, Any]]:
    batch_plans = copy.deepcopy(list(request.get("batch_plans") or []))
    matching_rule = next(
        (
            value
            for value in request.get("template_rules") or []
            if isinstance(value, Mapping)
            and str(value.get("template_key", "")).strip() == template_key
        ),
        None,
    )
    if matching_rule is None or matching_rule.get("mode", "use_batch") == "use_batch":
        return batch_plans
    mode = str(matching_rule.get("mode", "use_batch"))
    local_plans = copy.deepcopy(list(matching_rule.get("plans") or []))
    if mode == "replace":
        return local_plans
    if mode == "append":
        return batch_plans + local_plans
    raise InputValidationError("当前模板未启用生成方案")


def _raw_plan_for_source(
    plans: Sequence[Mapping[str, Any]], source_plan_id: str
) -> dict[str, Any]:
    matches = [
        copy.deepcopy(dict(plan))
        for plan in plans
        if str(plan.get("plan_id", "")).strip() == source_plan_id
    ]
    if len(matches) != 1:
        raise InputValidationError(
            "无法唯一定位待调整方案",
            evidence={"source_plan_id": source_plan_id, "match_count": len(matches)},
        )
    return matches[0]


def _inherited_generation_value(
    request: Mapping[str, Any], plan: Mapping[str, Any], field: str
) -> Any:
    plan_generation = plan.get("generation")
    if isinstance(plan_generation, Mapping) and field in plan_generation:
        return plan_generation[field]
    defaults = request.get("generation_defaults")
    if isinstance(defaults, Mapping):
        return defaults.get(field)
    return None


def _inherited_unit_generation_value(
    request: Mapping[str, Any],
    plan: Mapping[str, Any],
    unit_index: int,
    field: str,
) -> Any:
    """Mirror config precedence for one unit-level recycle setting."""

    units = plan.get("units")
    if (
        isinstance(units, Sequence)
        and not isinstance(units, (str, bytes))
        and 0 <= unit_index < len(units)
        and isinstance(units[unit_index], Mapping)
    ):
        unit_generation = units[unit_index].get("generation")
        if isinstance(unit_generation, Mapping) and field in unit_generation:
            return unit_generation[field]
    plan_generation = plan.get("generation")
    if isinstance(plan_generation, Mapping):
        unit_defaults = plan_generation.get("unit_defaults")
        if isinstance(unit_defaults, Mapping) and field in unit_defaults:
            return unit_defaults[field]
    defaults = request.get("generation_defaults")
    if isinstance(defaults, Mapping):
        unit_defaults = defaults.get("unit_defaults")
        if isinstance(unit_defaults, Mapping) and field in unit_defaults:
            return unit_defaults[field]
    inherited = _inherited_generation_value(request, plan, field)
    if inherited is not None:
        return inherited
    return {
        "required_pre_gate_recycles": 0,
        "first_recycle_lead_tiles": 2,
    }.get(field)


def _unit_index_from_error(error: Mapping[str, Any]) -> int | None:
    """Return the failing puzzle-unit index from a stable config error path."""

    match = re.search(r"\.units\[(\d+)\](?:\.|$)", str(error.get("path", "")))
    return None if match is None else int(match.group(1))


def _unit_step(plan: Mapping[str, Any], unit_index: int | None) -> int | None:
    if unit_index is None:
        unit_index = 0
    units = plan.get("units")
    if not isinstance(units, Sequence) or isinstance(units, (str, bytes)):
        return None
    if unit_index < 0 or unit_index >= len(units):
        return None
    unit = units[unit_index]
    if not isinstance(unit, Mapping):
        return None
    step = unit.get("step")
    if isinstance(step, int) and not isinstance(step, bool):
        return step
    return None


def _integer_from_message(message: str, patterns: Sequence[str]) -> int | None:
    for pattern in patterns:
        match = re.search(pattern, message)
        if match is not None:
            return int(match.group(1))
    return None


def build_generation_parameter_recommendations(
    config_payload: Mapping[str, Any],
    *,
    template_key: str,
    source_plan_id: str,
    tower_mode: str,
    error: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Build conservative per-template cards from concrete failure evidence.

    The cards start in ``pending`` state.  The app service performs the actual
    bounded trial and updates ``verified``; this pure function never claims a
    parameter change can generate a candidate without that trial.
    """

    if not error:
        return []
    error_code = str(error.get("code", ""))
    recycle_error_codes = {
        "insufficient_tiles_for_recycles",
        "first_recycle_lead_impossible",
    }
    if (
        error.get("failure_kind")
        not in {"parameter_conflict", "budget_exhausted"}
        and error_code not in recycle_error_codes
    ):
        return []
    plans = _effective_raw_plans(config_payload, template_key)
    plan = _raw_plan_for_source(plans, source_plan_id)
    evidence = error.get("evidence")
    evidence = dict(evidence) if isinstance(evidence, Mapping) else {}
    rejection_counts = evidence.get("rejection_counts")
    rejection_counts = (
        dict(rejection_counts) if isinstance(rejection_counts, Mapping) else {}
    )
    suggestions: list[tuple[str, Any, str, str]] = []
    message = str(error.get("message", ""))
    unit_index = _unit_index_from_error(error)
    step = _unit_step(plan, unit_index)

    # A recycle-rule contradiction is deterministic.  Prefer relaxing the
    # rule that caused it instead of widening unrelated layout limits.
    if error_code == "insufficient_tiles_for_recycles":
        recycle_before = (
            _inherited_unit_generation_value(
                config_payload,
                plan,
                0 if unit_index is None else unit_index,
                "required_pre_gate_recycles",
            )
        )
        if (
            isinstance(recycle_before, int)
            and not isinstance(recycle_before, bool)
            and recycle_before > 0
        ):
            available_tiles = _integer_from_message(
                message,
                (r"最多生成\s*(\d+)\s*个结构块", r"当前为\s*(\d+)\s*个"),
            )
            recycle_after = recycle_before - 1
            if step is not None and available_tiles is not None:
                recycle_after = min(
                    recycle_after,
                    max(0, (available_tiles - step) // 3),
                )
            suggestions.append(
                (
                    "required_pre_gate_recycles",
                    max(0, recycle_after),
                    "当前结构块数量不足以完成设定的门槛前回收次数。",
                    "仅减少当前失败模板的门槛前回收；路径变短，前段压力会降低。",
                )
            )
    elif error_code == "first_recycle_lead_impossible":
        scoped_unit_index = 0 if unit_index is None else unit_index
        lead_before = _inherited_unit_generation_value(
            config_payload, plan, scoped_unit_index, "first_recycle_lead_tiles"
        )
        recycle_count = _inherited_unit_generation_value(
            config_payload, plan, scoped_unit_index, "required_pre_gate_recycles"
        )
        if (
            isinstance(lead_before, int)
            and not isinstance(lead_before, bool)
            and lead_before > 0
        ):
            latest_click = _integer_from_message(
                message, (r"最迟需在第\s*(\d+)\s*次点击完成",)
            )
            maximum_lead = None
            if latest_click is not None:
                maximum_lead = max(0, latest_click - 3)
            elif (
                step is not None
                and isinstance(recycle_count, int)
                and not isinstance(recycle_count, bool)
            ):
                maximum_lead = max(0, step + 2 * recycle_count - 3)
            lead_after = lead_before - 1
            if maximum_lead is not None:
                lead_after = min(lead_after, maximum_lead)
            suggestions.append(
                (
                    "first_recycle_lead_tiles",
                    max(0, lead_after),
                    "首次回收的铺垫要求晚于当前谜题路径可实现的最晚时机。",
                    "仅缩短当前失败模板的首次回收铺垫；回收次数与 sN 门槛不变。",
                )
            )
    required = evidence.get("required")
    effective_limit = evidence.get("effective_limit")
    count_limit = _inherited_generation_value(
        config_payload, plan, "total_count_limit"
    )
    percent_limit = _inherited_generation_value(
        config_payload, plan, "total_percent_limit"
    )
    if isinstance(required, int) and isinstance(count_limit, int) and count_limit < required:
        suggestions.append(
            (
                "total_count_limit",
                required,
                "方案要求的结构块数量已超过当前上限。",
                "只放宽当前失败模板的容量，结构块数不变。",
            )
        )
    elif (
        isinstance(required, int)
        and isinstance(effective_limit, int)
        and effective_limit > 0
        and isinstance(percent_limit, (int, float))
    ):
        maximum = 50.0 if tower_mode == "disallow" else 100.0
        relaxed = min(
            maximum,
            max(float(percent_limit) + 5.0, float(percent_limit) * required / effective_limit),
        )
        if relaxed > float(percent_limit):
            suggestions.append(
                (
                    "total_percent_limit",
                    round(relaxed, 1),
                    "按当前百分比可用的坐标数不足。",
                    "可用位置增多；不更改谜题类型和候选数。",
                )
            )

    span_rejections = int(rejection_counts.get("structure_layer_span", 0) or 0)
    span_before = _inherited_generation_value(
        config_payload, plan, "max_structure_layer_span"
    )
    if (
        span_rejections > 0
        and isinstance(span_before, (int, float))
        and not isinstance(span_before, bool)
    ):
        suggestions.append(
            (
                "max_structure_layer_span",
                span_before + 1,
                "候选多次因结构块层间距上限被拒绝。",
                "允许最高与最低结构块多相差 1 层；其他规则不变。",
            )
        )

    geometry_rejections = sum(
        int(value or 0)
        for key, value in rejection_counts.items()
        if str(key) in {
            "geometry_pool",
            "preferred_coord_unavailable",
            "preferred_coord_count",
            "no_initial_entry",
        }
        or str(key).startswith("topology_")
    )
    should_relax_layer = (
        error.get("failure_kind") == "parameter_conflict"
        or geometry_rejections > 0
    )
    # Geometry conflicts often need a slightly taller search band.  Offer one
    # bounded alternative only when the request actually imposed that field.
    for field, increment, maximum in (
        ("max_layer_percent", 10, 100),
        ("max_layer", 1, None),
    ):
        if not should_relax_layer:
            break
        before = _inherited_generation_value(config_payload, plan, field)
        if not isinstance(before, (int, float)) or isinstance(before, bool):
            continue
        after = before + increment
        if maximum is not None:
            after = min(maximum, after)
        if after == before:
            continue
        suggestions.append(
            (
                field,
                after,
                "当前层数限制可能使合法坐标不足。",
                "可选位置增多，结构可能更高。",
            )
        )
        break

    cards: list[dict[str, Any]] = []
    seen_fields: set[str] = set()
    for field, after, reason, impact in suggestions:
        if field in seen_fields or len(cards) >= 2:
            continue
        seen_fields.add(field)
        change_unit_index = (
            (0 if unit_index is None else unit_index)
            if error_code in recycle_error_codes
            and field
            in {"required_pre_gate_recycles", "first_recycle_lead_tiles"}
            else None
        )
        before = (
            _inherited_unit_generation_value(
                config_payload, plan, change_unit_index, field
            )
            if change_unit_index is not None
            else _inherited_generation_value(config_payload, plan, field)
        )
        identity = hashlib.sha256(
            json.dumps(
                [
                    template_key,
                    source_plan_id,
                    change_unit_index,
                    field,
                    before,
                    after,
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16]
        cards.append(
            {
                "recommendation_id": f"adjust_{identity}",
                "template_key": template_key,
                "source_plan_id": source_plan_id,
                "field": field,
                "label": (
                    f"谜题 {change_unit_index + 1} · "
                    f'{_GENERATION_PARAMETER_LABELS[field]}'
                    if change_unit_index is not None
                    else _GENERATION_PARAMETER_LABELS[field]
                ),
                "unit_index": change_unit_index,
                "before": before,
                "after": after,
                "reason": reason,
                "impact": impact,
                "changes": {field: after},
                "verified": False,
                "trial_result": {
                    "status": "pending",
                    "message": "等待小预算试算。",
                },
            }
        )
    return cards


def apply_generation_parameter_changes(
    config_payload: Mapping[str, Any],
    *,
    template_key: str,
    source_plan_id: str,
    changes: Mapping[str, Any],
    unit_index: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return a draft that changes one template only, plus an exact diff."""

    unknown = sorted(set(changes) - _GENERATION_CHANGE_FIELDS)
    if unknown or not changes:
        raise InputValidationError(
            "参数建议包含不支持的字段",
            evidence={"fields": unknown},
        )
    updated = copy.deepcopy(dict(config_payload))
    effective_plans = _effective_raw_plans(updated, template_key)
    target = _raw_plan_for_source(effective_plans, source_plan_id)
    target_index = next(
        index
        for index, plan in enumerate(effective_plans)
        if str(plan.get("plan_id", "")).strip() == source_plan_id
    )
    if unit_index is not None and (
        isinstance(unit_index, bool) or not isinstance(unit_index, int) or unit_index < 0
    ):
        raise InputValidationError("参数建议的谜题索引无效")
    generation = target.get("generation")
    generation = (
        copy.deepcopy(dict(generation))
        if isinstance(generation, Mapping)
        else {}
    )
    raw_units = copy.deepcopy(list(target.get("units") or []))
    if unit_index is not None and unit_index >= len(raw_units):
        raise InputValidationError(
            "无法定位待调整的谜题单元",
            evidence={"unit_index": unit_index, "unit_count": len(raw_units)},
        )
    differences: list[dict[str, Any]] = []
    for field, after in changes.items():
        if unit_index is None:
            before = _inherited_generation_value(updated, target, field)
            generation[field] = copy.deepcopy(after)
        else:
            if field not in {
                "required_pre_gate_recycles",
                "first_recycle_lead_tiles",
            }:
                raise InputValidationError("该参数不支持按谜题单元调整")
            before = _inherited_unit_generation_value(
                updated, target, unit_index, field
            )
            raw_unit = raw_units[unit_index]
            if not isinstance(raw_unit, Mapping):
                raise InputValidationError("谜题单元配置格式错误")
            raw_unit = copy.deepcopy(dict(raw_unit))
            unit_generation = raw_unit.get("generation")
            unit_generation = (
                copy.deepcopy(dict(unit_generation))
                if isinstance(unit_generation, Mapping)
                else {}
            )
            unit_generation[field] = copy.deepcopy(after)
            raw_unit["generation"] = unit_generation
            raw_units[unit_index] = raw_unit
        differences.append(
            {
                "field": field,
                "label": (
                    f"谜题 {unit_index + 1} · "
                    f'{_GENERATION_PARAMETER_LABELS[field]}'
                    if unit_index is not None
                    else _GENERATION_PARAMETER_LABELS[field]
                ),
                "before": before,
                "after": after,
            }
        )
    if unit_index is None:
        target["generation"] = generation
    else:
        target["units"] = raw_units
    effective_plans[target_index] = target
    replacement_rule = {
        "template_key": template_key,
        "mode": "replace",
        "plans": effective_plans,
    }
    rules = list(updated.get("template_rules") or [])
    replaced = False
    for index, rule in enumerate(rules):
        if (
            isinstance(rule, Mapping)
            and str(rule.get("template_key", "")).strip() == template_key
        ):
            rules[index] = replacement_rule
            replaced = True
            break
    if not replaced:
        rules.append(replacement_rule)
    updated["template_rules"] = rules
    return updated, differences


def build_generation_probe_request(
    config_payload: Mapping[str, Any],
    *,
    template_key: str,
    source_plan_id: str,
    attempt_budget: int = 24,
) -> dict[str, Any]:
    """Create an isolated one-candidate request for a bounded real trial."""

    plans = _effective_raw_plans(config_payload, template_key)
    plan = _raw_plan_for_source(plans, source_plan_id)
    generation = plan.get("generation")
    generation = copy.deepcopy(dict(generation)) if isinstance(generation, Mapping) else {}
    generation["candidate_count"] = 1
    generation["attempt_budget"] = max(1, int(attempt_budget))
    plan["generation"] = generation
    probe = {
        "schema_version": config_payload.get("schema_version", 1),
        "template_keys": [template_key],
        "batch_plans": [plan],
        "template_rules": [],
    }
    if "batch_seed" in config_payload:
        probe["batch_seed"] = config_payload["batch_seed"]
    if isinstance(config_payload.get("generation_defaults"), Mapping):
        probe["generation_defaults"] = copy.deepcopy(
            dict(config_payload["generation_defaults"])
        )
    return probe


@dataclass(frozen=True)
class DupParam:
    a: int
    seed: str
    luck: str
    b: int

    def render(self) -> str:
        return f"{self.a}|{self.seed}|{self.luck}|{self.b}"


def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    safe_evidence = {"table": path.name}
    try:
        resolved = path.resolve(strict=True)
        is_valid_file = resolved.is_file()
        exceeds_limit = (
            is_valid_file and resolved.stat().st_size > MAX_CONFIG_BYTES
        )
    except OSError as exc:
        raise InputValidationError(
            "配置表读取失败",
            evidence={**safe_evidence, "reason": "config_table_unavailable"},
        ) from exc
    if not is_valid_file or exceeds_limit:
        raise InputValidationError(
            "配置表不存在或超过安全大小",
            evidence={**safe_evidence, "reason": "config_table_missing_or_too_large"},
        )
    try:
        with resolved.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.reader(stream))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise InputValidationError(
            "配置表读取失败",
            evidence={**safe_evidence, "reason": "config_table_read_failed"},
        ) from exc
    if len(rows) < 3:
        raise InputValidationError(
            "配置表缺少表头/类型/说明三行",
            evidence={**safe_evidence, "reason": "config_table_header_incomplete"},
        )
    header = [value.strip() for value in rows[0]]
    if len(header) != len(set(header)):
        raise InputValidationError(
            "配置表包含重复表头",
            evidence={**safe_evidence, "reason": "config_table_duplicate_header"},
        )
    width = len(header)
    return header, [row + [""] * max(0, width - len(row)) for row in rows[3:]]


def _read_uploaded_csv(
    data: bytes, *, filename: str, table: str
) -> tuple[list[str], list[list[str]]]:
    """Read an optional in-memory config table without writing the upload."""

    if not str(filename).lower().endswith(".csv"):
        raise InputValidationError(f"{table} 必须是 CSV 文件")
    if not data or len(data) > MAX_CONFIG_BYTES:
        raise InputValidationError(f"{table} 上传内容为空或超过安全大小")
    try:
        decoded = data.decode("utf-8-sig")
        rows = list(csv.reader(decoded.splitlines()))
    except (UnicodeError, csv.Error) as exc:
        raise InputValidationError(f"{table} 上传内容无法解析") from exc
    if len(rows) < 3:
        raise InputValidationError(f"{table} 缺少表头/类型/说明三行")
    header = [value.strip() for value in rows[0]]
    if len(header) != len(set(header)):
        raise InputValidationError(f"{table} 包含重复表头")
    width = len(header)
    return header, [row + [""] * max(0, width - len(row)) for row in rows[3:]]


def _column_map(header: Sequence[str], required: Iterable[str], *, table: str) -> dict[str, int]:
    mapping = {name: index for index, name in enumerate(header)}
    missing = [name for name in required if name not in mapping]
    if missing:
        raise InputValidationError(
            f"{table} 缺少必需字段", evidence={"missing_columns": missing}
        )
    return mapping


def validate_ulinkconfig_root(path: str | os.PathLike[str]) -> Path:
    if not str(path).strip():
        raise InputValidationError("请先选择 ulinkconfig 目录")
    root = real_path(path)
    if not root.is_dir() or not os.access(root, os.R_OK):
        raise InputValidationError(
            "ulinkconfig 目录不存在或不可读", evidence={"path": str(root)}
        )
    return root


def _has_required_header(path: Path, required: Iterable[str]) -> bool:
    """Cheaply reject backup/encrypted pairs that the calculator cannot read."""
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file() or resolved.stat().st_size > MAX_CONFIG_BYTES:
            return False
        with resolved.open("r", encoding="utf-8-sig", newline="") as stream:
            header = [value.strip() for value in next(csv.reader(stream))]
    except (OSError, UnicodeError, csv.Error, StopIteration):
        return False
    return set(required).issubset(header)


def discover_config_sets(path: str | os.PathLike[str]) -> list[dict[str, str]]:
    root = validate_ulinkconfig_root(path)
    discovered: list[dict[str, str]] = []
    for config_path in sorted(root.rglob(CONFIG_LEVEL)):
        if len(discovered) >= 64:
            break
        directory = config_path.parent
        list_path = directory / CONFIG_LEVEL_LIST
        if not list_path.is_file() or config_path.is_symlink() or list_path.is_symlink():
            continue
        if not is_within(directory.resolve(strict=True), root):
            continue
        if not _has_required_header(config_path, ("LevelId", "TemplateId", *DUP_COLUMNS)):
            continue
        if not _has_required_header(list_path, ("LevelIndex", "LevelId")):
            continue
        relative = directory.relative_to(root).as_posix() or "."
        discovered.append(
            {
                "id": relative,
                "label": directory.name if relative != "." else root.name,
                "relative_path": relative,
            }
        )
    if not discovered:
        raise InputValidationError(
            "所选目录下未找到可解析且同目录成对的 config_level.csv 与 config_level_list.csv"
        )
    return discovered


def discover_unlaunched_config_sets(
    path: str | os.PathLike[str],
) -> list[dict[str, str]]:
    """Find readable config_level tables without requiring config_level_list."""

    root = validate_ulinkconfig_root(path)
    discovered: list[dict[str, str]] = []
    for config_path in sorted(root.rglob(CONFIG_LEVEL)):
        if len(discovered) >= 64:
            break
        directory = config_path.parent
        if config_path.is_symlink() or not is_within(directory.resolve(strict=True), root):
            continue
        if not _has_required_header(
            config_path, ("LevelId", "TemplateId", *DUP_COLUMNS)
        ):
            continue
        relative = directory.relative_to(root).as_posix() or "."
        discovered.append(
            {
                "id": relative,
                "label": directory.name if relative != "." else root.name,
                "relative_path": relative,
            }
        )
    if not discovered:
        raise InputValidationError(
            "所选目录下未找到可解析的 config_level.csv"
        )
    return discovered


def _resolve_config_set(root: Path, config_set_id: str) -> tuple[Path, Path]:
    known = {item["id"] for item in discover_config_sets(root)}
    if config_set_id not in known:
        raise InputValidationError(
            "配置集不属于当前 ulinkconfig 目录",
            evidence={"config_set_id": config_set_id},
        )
    directory = (root / config_set_id).resolve(strict=True) if config_set_id != "." else root
    if not is_within(directory, root):
        raise InputValidationError("配置集路径越过 ulinkconfig 根目录")
    return directory / CONFIG_LEVEL, directory / CONFIG_LEVEL_LIST


def _resolve_unlaunched_config_set(root: Path, config_set_id: str) -> Path:
    known = {item["id"] for item in discover_unlaunched_config_sets(root)}
    if config_set_id not in known:
        raise InputValidationError(
            "配置集不属于当前 ulinkconfig 目录",
            evidence={"config_set_id": config_set_id},
        )
    directory = (
        (root / config_set_id).resolve(strict=True)
        if config_set_id != "."
        else root
    )
    if not is_within(directory, root):
        raise InputValidationError("配置集路径越过 ulinkconfig 根目录")
    return directory / CONFIG_LEVEL


def parse_dup_param(value: str) -> DupParam:
    parts = str(value).strip().split("|")
    if len(parts) != 4:
        raise InputValidationError(
            "DupPara 必须是 A种类数|随机种子|塔幸运值|B种类数",
            evidence={"value": value},
        )
    try:
        a = int(parts[0])
        b = int(parts[3])
    except ValueError as exc:
        raise InputValidationError(
            "DupPara 的 A/B 种类数必须是整数", evidence={"value": value}
        ) from exc
    if a < 0 or b < 0:
        raise InputValidationError(
            "DupPara 的 A/B 种类数不能为负数", evidence={"value": value}
        )
    return DupParam(a=a, seed=parts[1], luck=parts[2], b=b)


def _random_seed_group_id(
    row: Sequence[str], columns: Mapping[str, int]
) -> int:
    """Read the config-level allocator mode without guessing structured groups.

    Older test fixtures and historical tables can omit the column; that is the
    only case treated as the legacy default.  A present but malformed value is
    rejected so it can never silently select the legacy simulator.
    """

    if RANDOM_SEED_GROUP_COLUMN not in columns:
        return 0
    raw = str(row[columns[RANDOM_SEED_GROUP_COLUMN]]).strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError as exc:
        raise InputValidationError(
            "config_level.randomSeed_groupID 必须是非负整数",
            evidence={"value": raw},
        ) from exc
    if value < 0:
        raise InputValidationError(
            "config_level.randomSeed_groupID 必须是非负整数",
            evidence={"value": raw},
        )
    return value


def _decrement(value: int, amount: int, floor: int) -> int:
    # Preserve an already-lower starting value instead of silently raising it.
    return value if value <= floor else max(floor, value - amount)


def _fidelity_dup_params(
    originals: Sequence[DupParam],
    *,
    replaced_a_type_count: int,
    replaced_b_type_count: int,
    minimum_a: int | None = None,
    minimum_b: int | None = None,
) -> list[str]:
    """Adjust every original duplicate independently without rebuilding its gradient."""

    if len(originals) != len(DUP_COLUMNS):
        raise InputValidationError(
            "保真副本参数需要完整的 DupPara1–10",
            evidence={"dup_param_count": len(originals)},
        )
    if (
        isinstance(replaced_a_type_count, bool)
        or isinstance(replaced_b_type_count, bool)
        or not isinstance(replaced_a_type_count, int)
        or not isinstance(replaced_b_type_count, int)
        or replaced_a_type_count < 0
        or replaced_b_type_count < 0
    ):
        raise InputValidationError("实际替换的 A/B 结构种类数必须是非负整数")
    for label, minimum in (("A", minimum_a), ("B", minimum_b)):
        if minimum is not None and (isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0):
            raise InputValidationError(f"{label} 种类数底线必须是非负整数")

    recommended: list[str] = []
    for index, original in enumerate(originals, start=1):
        next_a = original.a - replaced_a_type_count
        next_b = original.b - replaced_b_type_count
        if minimum_a is not None:
            next_a = _decrement(original.a, replaced_a_type_count, minimum_a)
        if minimum_b is not None:
            next_b = _decrement(original.b, replaced_b_type_count, minimum_b)
        if next_a < 0 or next_b < 0:
            raise InputValidationError(
                f"结构种类数超过原 DupPara{index} 的 A/B 种类数，不能得到非负推荐值",
                evidence={
                    "dup_para": index,
                    "original_a": original.a,
                    "original_b": original.b,
                    "replaced_a_type_count": replaced_a_type_count,
                    "replaced_b_type_count": replaced_b_type_count,
                },
            )
        recommended.append(
            DupParam(
                a=next_a,
                seed=original.seed,
                luck=original.luck,
                b=next_b,
            ).render()
        )
    return recommended


def recommend_dup_params(
    source: DupParam | Sequence[DupParam],
    strategy: str,
    *,
    replaced_a_type_count: int = 0,
    replaced_b_type_count: int = 0,
    minimum_a: int | None = None,
    minimum_b: int | None = None,
) -> list[str]:
    if strategy not in STRATEGIES:
        raise InputValidationError(
            "未知的参数配置方式", evidence={"strategy": strategy}
        )
    if strategy == "fidelity":
        if isinstance(source, DupParam):
            raise InputValidationError(
                "保真副本参数不能仅从 DupPara1 重建，请提供完整的 DupPara1–10"
            )
        return _fidelity_dup_params(
            list(source),
            replaced_a_type_count=replaced_a_type_count,
            replaced_b_type_count=replaced_b_type_count,
            minimum_a=minimum_a,
            minimum_b=minimum_b,
        )

    if not isinstance(source, DupParam):
        raise InputValidationError("重做副本梯度需要 DupPara1 作为起点")
    first = source
    if strategy == "flat":
        return [first.render()] * 10

    if strategy == "balanced":
        start_a, start_b, step = 2, 3, 2
    else:
        start_a, start_b = 3, 4
        step = {"gentle": 3, "aggressive": 1, "steady": 2}[strategy]

    result = [first.render()]
    floor_a = 4 if minimum_a is None else minimum_a
    floor_b = 3 if minimum_b is None else minimum_b
    for duplicate in range(2, 11):
        a_decrease = 0 if duplicate < start_a else 1 + (duplicate - start_a) // step
        b_decrease = 0 if duplicate < start_b else 1 + (duplicate - start_b) // step
        result.append(
            DupParam(
                a=_decrement(first.a, a_decrease, floor_a),
                seed=first.seed,
                luck=first.luck,
                b=_decrement(first.b, b_decrease, floor_b),
            ).render()
        )
    return result


def _tokens(value: str) -> set[str]:
    text = str(value).strip()
    if not text:
        return set()
    return {token for token in re.split(r"[|;\s]+", text) if token}


def _online_positions(
    level_list_rows: Sequence[Sequence[str]],
    columns: Mapping[str, int],
    level_id: str,
    start: int | None = None,
    end: int | None = None,
) -> list[int]:
    positions = set()
    for row in level_list_rows:
        try:
            index = int(str(row[columns["LevelIndex"]]).strip())
        except (ValueError, IndexError):
            continue
        if start is not None and index < start:
            continue
        if end is not None and index > end:
            continue
        dda_type = str(row[columns["DDAType"]]).strip() if "DDAType" in columns else "0"
        identifier_columns = (
            [
                name
                for name in ("DDALevelId1", "DDALevelId2", "DDALevelId3")
                if name in columns
            ]
            if dda_type == "1"
            else ["LevelId"]
        )
        if any(level_id in _tokens(row[columns[name]]) for name in identifier_columns):
            positions.add(index)
    return sorted(positions)


def _compact_positions(positions: Sequence[int]) -> str:
    """Render every online position while compressing consecutive level ranges."""
    ordered = sorted(set(positions))
    if not ordered:
        return ""
    ranges: list[str] = []
    range_start = range_end = ordered[0]
    for value in ordered[1:]:
        if value == range_end + 1:
            range_end = value
            continue
        ranges.append(
            str(range_start) if range_start == range_end else f"{range_start}–{range_end}"
        )
        range_start = range_end = value
    ranges.append(
        str(range_start) if range_start == range_end else f"{range_start}–{range_end}"
    )
    return "、".join(ranges)


def load_export_manifest(profile: Profile, output_template_id: str) -> dict[str, Any]:
    safe_id = validate_template_id(output_template_id)
    output_root = real_path(profile.output_root)
    bytes_path = (output_root / f"{safe_id}_m.bytes").resolve(strict=True)
    if not is_within(bytes_path, output_root) or not bytes_path.is_file():
        raise InputValidationError(
            "输出目录中找不到该新模板", evidence={"output_template_id": safe_id}
        )
    registry_path = output_root / REPORTS_DIRECTORY / REGISTRY_DIRECTORY / f"{safe_id}.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        manifest_path = (output_root / registry["manifest_relative_path"]).resolve(strict=True)
        if not is_within(manifest_path, output_root):
            raise ValueError("manifest escaped output root")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise InputValidationError(
            "该模板缺少可验证的导出 manifest，无法判断原 A/B 替换来源",
            evidence={"output_template_id": safe_id},
        ) from exc
    digest = hashlib.sha256(bytes_path.read_bytes()).hexdigest()
    if (
        manifest.get("output_template_id") != safe_id
        or manifest.get("output_sha256") != digest
        or not isinstance(manifest.get("replacements"), list)
    ):
        raise InputValidationError("新模板与导出 manifest 不一致，已停止计算")
    return manifest


def load_uploaded_export_manifest(
    profile: Profile, filename: str, data: bytes
) -> dict[str, Any]:
    if not str(filename).endswith("_m.bytes"):
        raise InputValidationError("上传模板文件名必须以 _m.bytes 结尾")
    try:
        level = parse_level_bytes(data, source_name=filename)
    except (ValueError, TypeError) as exc:
        raise InputValidationError("上传的新模板无法解析") from exc
    manifest = load_export_manifest(profile, level.template_id)
    if hashlib.sha256(data).hexdigest() != manifest.get("output_sha256"):
        raise InputValidationError(
            "上传文件与当前输出目录内的导出 manifest 不匹配，不能猜测替换来源"
        )
    return manifest


def manifest_replacement_type_counts(manifest: Mapping[str, Any]) -> tuple[int, int]:
    replacements = manifest.get("replacements")
    if not isinstance(replacements, list):
        raise InputValidationError("manifest 缺少 replacements")
    a_types = {
        str(item.get("structure_type"))
        for item in replacements
        if isinstance(item, Mapping) and item.get("source_tile_id") == -10
    }
    b_types = {
        str(item.get("structure_type"))
        for item in replacements
        if isinstance(item, Mapping) and item.get("source_tile_id") == -11
    }
    return len(a_types), len(b_types)


def manifest_replacement_tile_counts(manifest: Mapping[str, Any]) -> tuple[int, int]:
    replacements = manifest.get("replacements")
    if not isinstance(replacements, list):
        raise InputValidationError("manifest 缺少 replacements")
    return (
        sum(
            1
            for item in replacements
            if isinstance(item, Mapping) and item.get("source_tile_id") == -10
        ),
        sum(
            1
            for item in replacements
            if isinstance(item, Mapping) and item.get("source_tile_id") == -11
        ),
    )


def source_tile_counts(
    profile: Profile, manifest: Mapping[str, Any]
) -> tuple[int, int] | None:
    """Use export-time source inventory, or verify an older source by its hash.

    A generated template's A/B quantities are not the source quantities.  If
    an old export lacks the inventory and its original source is unavailable,
    do not infer the dominant side from DupPara or the replacement list.
    """

    stored = manifest.get("source_tile_counts")
    if isinstance(stored, Mapping):
        a, b = stored.get("A"), stored.get("B")
        if all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in (a, b)):
            return a, b
    source_id = str(manifest.get("source_template_id", "")).strip()
    source_sha = str(manifest.get("source_sha256", "")).strip()
    if not source_id or not source_sha:
        return None
    try:
        level = load_template_id(profile, source_id).level
    except (InputValidationError, OSError):
        return None
    if level.sha256 != source_sha:
        return None
    return (
        sum(tile.tile_id == -10 for tile in level.tiles),
        sum(tile.tile_id == -11 for tile in level.tiles),
    )


def lookup_unlaunched_template_parameters(
    profile: Profile,
    template_ids: Sequence[str],
    *,
    config_set_id: str | None = None,
) -> dict[str, Any]:
    """Read existing DupPara values from every available config_level table.

    Duplicate template IDs are deliberately returned row-by-row so callers can
    review every matching LevelId instead of silently choosing one definition.
    ``config_set_id`` remains available for older callers, while the product UI
    intentionally searches every readable table and no longer exposes an
    internal configuration-set picker.
    """

    if isinstance(template_ids, (str, bytes)) or not template_ids:
        raise InputValidationError("请至少输入一个模板 ID")
    if len(template_ids) > 500:
        raise InputValidationError("单次最多查询 500 个模板 ID")
    requested: list[str] = []
    repeated_inputs: list[str] = []
    seen: set[str] = set()
    for raw in template_ids:
        safe_id = validate_template_id(str(raw).strip())
        if safe_id in seen:
            if safe_id not in repeated_inputs:
                repeated_inputs.append(safe_id)
            continue
        seen.add(safe_id)
        requested.append(safe_id)

    root = validate_ulinkconfig_root(profile.ulinkconfig_root or "")
    discovered = discover_unlaunched_config_sets(root)
    level_paths: list[tuple[str, Path]] = []
    if config_set_id:
        level_paths.append(
            (config_set_id, _resolve_unlaunched_config_set(root, config_set_id))
        )
    else:
        for item in discovered:
            level_path = (root / item["relative_path"] / CONFIG_LEVEL).resolve(
                strict=True
            )
            if not is_within(level_path, root):
                raise InputValidationError("config_level 路径越界")
            level_paths.append((item["id"], level_path))

    matches: dict[str, list[dict[str, Any]]] = {
        template_id: [] for template_id in requested
    }
    for source_set_id, level_path in level_paths:
        level_header, level_rows = _read_csv(level_path)
        columns = _column_map(
            level_header,
            ("LevelId", "TemplateId", *DUP_COLUMNS),
            table=CONFIG_LEVEL,
        )
        if RANDOM_SEED_GROUP_COLUMN in level_header:
            columns[RANDOM_SEED_GROUP_COLUMN] = level_header.index(
                RANDOM_SEED_GROUP_COLUMN
            )
        for raw in level_rows:
            template_id = str(raw[columns["TemplateId"]]).strip()
            if template_id not in matches:
                continue
            matches[template_id].append(
                {
                    "template_id": template_id,
                    "level_id": str(raw[columns["LevelId"]]).strip(),
                    "config_set_id": source_set_id,
                    "config_level_path": str(level_path.relative_to(root)),
                    "random_seed_group_id": _random_seed_group_id(raw, columns),
                    "dup_params": [
                        str(raw[columns[column]]).strip() for column in DUP_COLUMNS
                    ],
                }
            )

    rows: list[dict[str, Any]] = []
    duplicate_template_ids: list[str] = []
    missing_template_ids: list[str] = []
    for template_id in requested:
        template_matches = matches[template_id]
        count = len(template_matches)
        if count == 0:
            missing_template_ids.append(template_id)
            continue
        if count > 1:
            duplicate_template_ids.append(template_id)
        for item in template_matches:
            rows.append(
                {
                    **item,
                    "match_count": count,
                    "is_duplicate": count > 1,
                    "status": "duplicate" if count > 1 else "unique",
                }
            )

    warnings: list[str] = []
    if repeated_inputs:
        warnings.append(
            "输入中的重复模板 ID 已合并：" + "、".join(repeated_inputs)
        )
    if missing_template_ids:
        warnings.append(
            "所有可用 config_level 中均未找到："
            + "、".join(missing_template_ids)
        )
    if duplicate_template_ids:
        warnings.append(
            "以下模板 ID 在 config_level 中有多条配置，已全部展示："
            + "、".join(duplicate_template_ids)
        )
    return {
        "schema_version": 1,
        "config_set_id": config_set_id,
        "config_set_ids": [item[0] for item in level_paths],
        "requested_template_ids": requested,
        "rows": rows,
        "duplicate_template_ids": duplicate_template_ids,
        "missing_template_ids": missing_template_ids,
        "warnings": warnings,
        "source_tables": {
            "config_level": [str(path.relative_to(root)) for _, path in level_paths],
            "access": "read_only",
        },
    }


def build_parameter_recommendations(
    profile: Profile,
    manifests: Sequence[Mapping[str, Any]],
    *,
    config_set_id: str,
    level_start: int,
    level_end: int,
    strategy: str,
    config_level_data: bytes | None = None,
    config_level_filename: str | None = None,
    filter_by_level_range: bool = True,
) -> dict[str, Any]:
    if isinstance(level_start, bool) or isinstance(level_end, bool):
        raise InputValidationError("关卡区间必须是整数")
    if level_start < 1 or level_end < level_start:
        raise InputValidationError("关卡区间不合法")
    if not isinstance(filter_by_level_range, bool):
        raise InputValidationError("是否按关卡区间过滤必须是布尔值")
    root = validate_ulinkconfig_root(profile.ulinkconfig_root or "")
    level_path, list_path = _resolve_config_set(root, config_set_id)
    if config_level_data is not None:
        level_header, level_rows = _read_uploaded_csv(
            config_level_data,
            filename=config_level_filename or "config_level.csv",
            table=CONFIG_LEVEL,
        )
        level_source = f"上传：{config_level_filename or 'config_level.csv'}"
    else:
        level_header, level_rows = _read_csv(level_path)
        level_source = str(level_path.relative_to(root))
    list_header, list_rows = _read_csv(list_path)
    level_columns = _column_map(
        level_header,
        ("LevelId", "TemplateId", *DUP_COLUMNS),
        table=CONFIG_LEVEL,
    )
    if RANDOM_SEED_GROUP_COLUMN in level_header:
        level_columns[RANDOM_SEED_GROUP_COLUMN] = level_header.index(
            RANDOM_SEED_GROUP_COLUMN
        )
    if STRATEGY_GROUP_COLUMN in level_header:
        level_columns[STRATEGY_GROUP_COLUMN] = level_header.index(
            STRATEGY_GROUP_COLUMN
        )
    list_columns = _column_map(
        list_header,
        ("LevelIndex", "LevelId"),
        table=CONFIG_LEVEL_LIST,
    )
    available_source_template_ids = {
        str(raw[level_columns["TemplateId"]]).strip()
        for raw in level_rows
        if str(raw[level_columns["TemplateId"]]).strip()
    }

    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    for manifest in manifests:
        source_id = str(manifest.get("source_template_id", "")).strip()
        output_id = str(manifest.get("output_template_id", "")).strip()
        if not source_id or not output_id:
            raise InputValidationError("导出 manifest 缺少原/新模板 ID")
        config_source_id, config_source_match = _resolve_config_source_template_id(
            source_id, available_source_template_ids
        )
        replaced_a_types, replaced_b_types = manifest_replacement_type_counts(manifest)
        replaced_a_tiles, replaced_b_tiles = manifest_replacement_tile_counts(manifest)
        source_counts = source_tile_counts(profile, manifest)
        if source_counts is None:
            minimum_a = minimum_b = None
        elif source_counts[0] >= source_counts[1]:
            minimum_a, minimum_b = (
                min(4, max(0, source_counts[0] - replaced_a_tiles) // 3),
                min(3, max(0, source_counts[1] - replaced_b_tiles) // 3),
            )
        else:
            minimum_a, minimum_b = (
                min(3, max(0, source_counts[0] - replaced_a_tiles) // 3),
                min(4, max(0, source_counts[1] - replaced_b_tiles) // 3),
            )
        matched = 0
        ranged = 0
        all_positions: set[int] = set()
        grouped: dict[tuple[str, ...], dict[str, Any]] = {}
        for raw in level_rows:
            if (
                config_source_id is None
                or str(raw[level_columns["TemplateId"]]).strip() != config_source_id
            ):
                continue
            matched += 1
            level_id = str(raw[level_columns["LevelId"]]).strip()
            before = [str(raw[level_columns[name]]).strip() for name in DUP_COLUMNS]
            level_positions = _online_positions(list_rows, list_columns, level_id)
            all_positions.update(level_positions)
            positions = (
                [
                    position
                    for position in level_positions
                    if level_start <= position <= level_end
                ]
                if filter_by_level_range
                else list(level_positions)
            )
            if filter_by_level_range and not positions:
                continue
            ranged += 1
            random_seed_group_id = _random_seed_group_id(raw, level_columns)
            # Equal DupPara values can still use different allocation modes;
            # merging them would discard the structured-seed boundary.
            source_strategy_group_id = (
                str(raw[level_columns[STRATEGY_GROUP_COLUMN]]).strip()
                if STRATEGY_GROUP_COLUMN in level_columns
                else ""
            )
            key = (*before, str(random_seed_group_id), source_strategy_group_id)
            group = grouped.setdefault(
                key,
                {
                    "before": before,
                    "random_seed_group_id": random_seed_group_id,
                    "strategy_group_id": source_strategy_group_id,
                    "level_ids": set(),
                    "positions": set(),
                },
            )
            group["level_ids"].add(level_id)
            group["positions"].update(positions)

        if grouped and source_counts is None:
            warnings.append(
                f"{source_id} 缺少导出快照或经源文件哈希核对的原模板 A/B 牌量；"
                "不能判定主牌侧，以下仅按实际替换种类机械扣减，不应用 4/3 经验底线。"
            )
        for group in grouped.values():
            before = list(group["before"])
            row: dict[str, Any] = {
                "source_template_id": source_id,
                "config_source_template_id": config_source_id,
                "config_source_match": config_source_match,
                "output_template_id": output_id,
                "level_ids": sorted(group["level_ids"]),
                "online_positions": sorted(group["positions"]),
                "before_dup": before,
                "random_seed_group_id": group["random_seed_group_id"],
                "strategy_group_id": group.get("strategy_group_id", ""),
                "replaced_a_type_count": replaced_a_types,
                "replaced_b_type_count": replaced_b_types,
                "replaced_a_tile_count": replaced_a_tiles,
                "replaced_b_tile_count": replaced_b_tiles,
                "source_a_tile_count": source_counts[0] if source_counts else None,
                "source_b_tile_count": source_counts[1] if source_counts else None,
                "remaining_a_group_capacity": (
                    max(0, source_counts[0] - replaced_a_tiles) // 3
                    if source_counts else None
                ),
                "remaining_b_group_capacity": (
                    max(0, source_counts[1] - replaced_b_tiles) // 3
                    if source_counts else None
                ),
                "source_majority_side": (
                    "A" if source_counts[0] >= source_counts[1] else "B"
                ) if source_counts else None,
                "minimum_a_types": minimum_a,
                "minimum_b_types": minimum_b,
                "partial_deductions": [],
                "recommended_dup": [],
                "status": "ready",
                "message": "",
            }
            try:
                originals = [parse_dup_param(value) for value in before]
                if strategy == "fidelity":
                    row["recommended_dup"] = recommend_dup_params(
                        originals,
                        strategy,
                        replaced_a_type_count=replaced_a_types,
                        replaced_b_type_count=replaced_b_types,
                        minimum_a=minimum_a,
                        minimum_b=minimum_b,
                    )
                else:
                    original_first = originals[0]
                    next_a = (
                        _decrement(original_first.a, replaced_a_types, minimum_a)
                        if minimum_a is not None else original_first.a - replaced_a_types
                    )
                    next_b = (
                        _decrement(original_first.b, replaced_b_types, minimum_b)
                        if minimum_b is not None else original_first.b - replaced_b_types
                    )
                    if next_a < 0 or next_b < 0:
                        raise InputValidationError(
                            "结构种类数超过原副本1的 A/B 种类数，不能得到非负推荐值"
                        )
                    first = DupParam(
                        a=next_a,
                        seed=original_first.seed,
                        luck=original_first.luck,
                        b=next_b,
                    )
                    rebuilt = recommend_dup_params(
                        first, strategy, minimum_a=minimum_a, minimum_b=minimum_b
                    )
                    # Rebuilding A/B difficulty progression does not imply
                    # replacing the original per-duplicate Seed or Lucky.
                    row["recommended_dup"] = [
                        DupParam(
                            a=parse_dup_param(value).a,
                            seed=original.seed,
                            luck=original.luck,
                            b=parse_dup_param(value).b,
                        ).render()
                        for original, value in zip(originals, rebuilt)
                    ]
                if strategy == "fidelity":
                    row["partial_deductions"] = [
                        {
                            "dup_para": index,
                            "planned_a": replaced_a_types,
                            "applied_a": original.a - parse_dup_param(value).a,
                            "planned_b": replaced_b_types,
                            "applied_b": original.b - parse_dup_param(value).b,
                        }
                        for index, (original, value) in enumerate(
                            zip(originals, row["recommended_dup"]), start=1
                        )
                    ]
                    if any(
                        part["planned_a"] > part["applied_a"]
                        or part["planned_b"] > part["applied_b"]
                        for part in row["partial_deductions"]
                    ):
                        row["message"] = (
                            f"已按原模板牌量确定主牌侧，并在 {minimum_a}/{minimum_b} "
                            "种类底线处少扣；Seed/Luck 逐副本保留。"
                        )
            except InputValidationError as exc:
                row["status"] = "error"
                row["message"] = str(exc)
            rows.append(row)
        if config_source_match == "trailing_clean_alias":
            warnings.append(
                f"{source_id} 未命中同名配置，已使用唯一尾缀 clean 别名 {config_source_id}；"
                "历史目标按 CSV 中可用的原模板别名解析，首次写入使用该配置源 ID，"
                "manifest 原模板 ID 同时保留作审计。"
            )
        if not matched:
            warnings.append(f"{source_id} 未在 {config_set_id}/{CONFIG_LEVEL} 中找到")
        elif not ranged:
            actual_usage = _compact_positions(sorted(all_positions))
            if actual_usage:
                warnings.append(
                    f"{source_id} 在 config_level 中存在，但未用于 "
                    f"{level_start}–{level_end} 关；实际用于：{actual_usage} 关"
                )
            else:
                warnings.append(
                    f"{source_id} 在 config_level 中存在，但未在 config_level_list "
                    "中找到实际使用关卡"
                )

    return {
        "schema_version": 1,
        "config_set_id": config_set_id,
        "level_range": {"start": level_start, "end": level_end},
        "level_range_filter_applied": filter_by_level_range,
        "strategy": {"id": strategy, **STRATEGIES[strategy]},
        "rows": rows,
        "warnings": warnings,
        "source_tables": {
            "config_level": level_source,
            "config_level_list": str(list_path.relative_to(root)),
            "access": "read_only",
        },
    }


__all__ = [
    "DUP_COLUMNS",
    "STRATEGIES",
    "apply_generation_parameter_changes",
    "build_generation_parameter_recommendations",
    "build_generation_probe_request",
    "build_parameter_recommendations",
    "discover_config_sets",
    "discover_unlaunched_config_sets",
    "lookup_unlaunched_template_parameters",
    "load_export_manifest",
    "load_uploaded_export_manifest",
    "manifest_replacement_type_counts",
    "parse_dup_param",
    "recommend_dup_params",
    "validate_ulinkconfig_root",
]
