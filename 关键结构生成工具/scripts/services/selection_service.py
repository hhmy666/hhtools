"""Strict review-selection boundary for formal exports.

Generation reports are review material, not trusted export payloads.  This
module accepts only stable business identifiers, finds those identifiers in a
fresh deterministic generation result, and reconstructs ``ExportSelection``
objects from the current source/config snapshots.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from puzzle_core.config_io import ResolvedBatchConfig
from puzzle_core.errors import InputValidationError

from .export_service import ExportSelection, inconclusive_evidence_record
from .job_service import GenerationBatchResult


SELECTION_SCHEMA_VERSION = 1
MAX_EXPORT_SELECTIONS = 500
_CANDIDATE_ID = re.compile(r"^candidate_[0-9a-f]{16}$")


@dataclass(frozen=True)
class CandidateChoice:
    template_key: str
    resolved_plan_id: str
    candidate_id: str
    online_level_number: str | int | None = None
    level_id: str | None = None
    manual_evidence_override: bool = False


def _duplicate_rejecting_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw_key, value in pairs:
        key = unicodedata.normalize("NFC", raw_key)
        if key in result:
            raise InputValidationError(
                "候选选择 JSON 包含重复字段",
                evidence={"field": key},
            )
        result[key] = value
    return result


def _strict_text(value: Any, *, path: str) -> str:
    if not isinstance(value, str):
        raise InputValidationError("候选选择字段必须是文本", json_path=path)
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized.encode("utf-8")) > 240:
        raise InputValidationError("候选选择字段为空或过长", json_path=path)
    return normalized


def parse_selection_payload(value: Any) -> tuple[CandidateChoice, ...]:
    if not isinstance(value, Mapping):
        raise InputValidationError("候选选择 JSON 根节点必须是对象", json_path="$")
    unknown = sorted(set(value) - {"schema_version", "selections"})
    if unknown:
        raise InputValidationError(
            "候选选择 JSON 包含未知字段",
            json_path=f"$.{unknown[0]}",
        )
    version = value.get("schema_version", SELECTION_SCHEMA_VERSION)
    if isinstance(version, bool) or version != SELECTION_SCHEMA_VERSION:
        raise InputValidationError("候选选择 JSON 版本不受支持", json_path="$.schema_version")
    raw_selections = value.get("selections")
    if not isinstance(raw_selections, list) or not raw_selections:
        raise InputValidationError("至少需要明确选择一个候选", json_path="$.selections")
    if len(raw_selections) > MAX_EXPORT_SELECTIONS:
        raise InputValidationError(
            "一次导出选择数超过安全上限",
            json_path="$.selections",
            evidence={"maximum": MAX_EXPORT_SELECTIONS},
        )

    result: list[CandidateChoice] = []
    identities: set[tuple[str, str, str]] = set()
    allowed = {
        "template_key",
        "resolved_plan_id",
        "candidate_id",
        "online_level_number",
        "level_id",
        "manual_evidence_override",
    }
    for index, raw in enumerate(raw_selections):
        path = f"$.selections[{index}]"
        if not isinstance(raw, Mapping):
            raise InputValidationError("候选选择项必须是对象", json_path=path)
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise InputValidationError(
                "候选选择项包含未知字段",
                json_path=f"{path}.{unknown[0]}",
            )
        template_key = _strict_text(raw.get("template_key"), path=f"{path}.template_key")
        plan_id = _strict_text(raw.get("resolved_plan_id"), path=f"{path}.resolved_plan_id")
        candidate_id = _strict_text(raw.get("candidate_id"), path=f"{path}.candidate_id")
        if not _CANDIDATE_ID.fullmatch(candidate_id):
            raise InputValidationError("候选 ID 格式不合法", json_path=f"{path}.candidate_id")
        identity = (template_key, plan_id, candidate_id)
        if identity in identities:
            raise InputValidationError("同一候选不能重复导出", json_path=path)
        identities.add(identity)

        online = raw.get("online_level_number")
        if online is not None and (
            isinstance(online, bool) or not isinstance(online, (str, int))
        ):
            raise InputValidationError(
                "线上关卡号必须是文本或整数",
                json_path=f"{path}.online_level_number",
            )
        level_id = raw.get("level_id")
        if level_id is not None:
            level_id = _strict_text(level_id, path=f"{path}.level_id")
        manual_evidence_override = raw.get("manual_evidence_override", False)
        if not isinstance(manual_evidence_override, bool):
            raise InputValidationError(
                "人工证据确认字段必须是布尔值",
                json_path=f"{path}.manual_evidence_override",
            )
        result.append(
            CandidateChoice(
                template_key=template_key,
                resolved_plan_id=plan_id,
                candidate_id=candidate_id,
                online_level_number=online,
                level_id=level_id,
                manual_evidence_override=manual_evidence_override,
            )
        )
    return tuple(result)


def loads_selection_payload(text: str) -> tuple[CandidateChoice, ...]:
    try:
        value = json.loads(
            text,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                InputValidationError(
                    "候选选择 JSON 不允许非有限数字",
                    evidence={"value": token},
                )
            ),
        )
    except InputValidationError:
        raise
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise InputValidationError("候选选择 JSON 无法解析") from exc
    return parse_selection_payload(value)


def load_selection_payload(path: Path) -> tuple[CandidateChoice, ...]:
    try:
        text = path.expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InputValidationError(
            "候选选择文件无法读取",
            evidence={"path": str(path)},
        ) from exc
    return loads_selection_payload(text)


def summarize_candidate_choices(
    choices: Sequence[CandidateChoice],
) -> dict[str, Any]:
    """Return a stable UI/API summary for zero or more reviewed choices.

    Selection state in the browser is allowed to contain no candidates.  The
    formal export boundary still rejects an empty ``selections`` array, but
    callers can use this helper while reviewing a partially completed batch.
    Multiple candidate IDs for the same template/plan pair are deliberately
    grouped as one work item instead of being mistaken for duplicate work.
    """

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for choice in choices:
        key = (choice.template_key, choice.resolved_plan_id)
        group = groups.setdefault(
            key,
            {
                "template_key": choice.template_key,
                "resolved_plan_id": choice.resolved_plan_id,
                "candidate_ids": [],
            },
        )
        group["candidate_ids"].append(choice.candidate_id)
    summarized_groups = []
    for group in groups.values():
        summarized_groups.append(
            {
                **group,
                "candidate_count": len(group["candidate_ids"]),
            }
        )
    return {
        "candidate_count": len(choices),
        "work_item_count": len(summarized_groups),
        "groups": summarized_groups,
    }


def resolve_export_selections(
    config: ResolvedBatchConfig,
    generated: GenerationBatchResult,
    choices: Sequence[CandidateChoice],
) -> tuple[ExportSelection, ...]:
    """Resolve reviewed IDs against a fresh, ordered generation result."""

    if len(config.work_items) != len(generated.items):
        raise InputValidationError("生成结果与当前配置快照数量不一致")
    work_by_key: dict[tuple[str, str], tuple[Any, Any]] = {}
    for work, item in zip(config.work_items, generated.items):
        key = (work.template_key, work.plan.plan_id)
        if key in work_by_key:
            raise InputValidationError("当前配置中存在含糊的已解析方案身份")
        if (item.template_key, item.plan_id) != key:
            raise InputValidationError("生成结果与当前配置顺序不一致")
        work_by_key[key] = (work, item)

    resolved: list[ExportSelection] = []
    for choice in choices:
        key = (choice.template_key, choice.resolved_plan_id)
        pair = work_by_key.get(key)
        if pair is None:
            raise InputValidationError(
                "所选候选不属于当前已解析配置",
                evidence={
                    "template_key": choice.template_key,
                    "resolved_plan_id": choice.resolved_plan_id,
                },
            )
        work, item = pair
        if item.status not in {"success", "partial"} or item.source is None:
            raise InputValidationError(
                "所选生成项本次重算没有可用候选，禁止导出",
                evidence={
                    "template_key": choice.template_key,
                    "resolved_plan_id": choice.resolved_plan_id,
                    "generation_error": item.error,
                },
            )
        matches = [
            candidate
            for candidate in item.candidates
            if candidate.candidate.candidate_id == choice.candidate_id
        ]
        if len(matches) != 1:
            raise InputValidationError(
                "当前源文件、配置、种子和算法未能唯一复现所选候选",
                evidence={
                    "template_key": choice.template_key,
                    "resolved_plan_id": choice.resolved_plan_id,
                    "candidate_id": choice.candidate_id,
                    "match_count": len(matches),
                },
            )
        candidate = matches[0]
        experience = candidate.candidate.experience_analysis
        difficulty = candidate.candidate.difficulty_simulation
        evidence_record = inconclusive_evidence_record(
            difficulty if isinstance(difficulty, Mapping) else {},
            experience if isinstance(experience, Mapping) else {},
            candidate_validation_passed=bool(candidate.validation.passed),
        )
        hard_status = evidence_record["axes"]["whole_board_completion"]
        override_record: Mapping[str, Any] | None = None
        if evidence_record["acknowledgement_required"]:
            if not choice.manual_evidence_override:
                raise InputValidationError(
                    "所选候选存在规则结论、难度风险或证据缺口；人工确认后可导出",
                    evidence={
                        "template_key": choice.template_key,
                        "resolved_plan_id": choice.resolved_plan_id,
                        "candidate_id": choice.candidate_id,
                        "hard_validation_status": hard_status,
                        "manual_evidence_override_required": True,
                        "manual_evidence_record": evidence_record,
                    },
                )
            override_record = evidence_record
        if candidate.generation_options is None:
            raise InputValidationError("所选候选缺少已锁定的精确生成参数")
        resolved.append(
            ExportSelection(
                source=item.source,
                plan=candidate.resolved_plan or work.plan,
                generation_options=candidate.generation_options,
                generated=candidate,
                generation_seed=candidate.candidate.candidate_seed,
                candidate_ordinal=candidate.candidate.candidate_index,
                original_plan_id=(candidate.resolved_plan or work.plan).source_plan_id,
                online_level_number=choice.online_level_number,
                level_id=choice.level_id,
                manual_evidence_override=override_record,
            )
        )
    return tuple(resolved)


__all__ = [
    "CandidateChoice",
    "MAX_EXPORT_SELECTIONS",
    "SELECTION_SCHEMA_VERSION",
    "load_selection_payload",
    "loads_selection_payload",
    "parse_selection_payload",
    "resolve_export_selections",
    "summarize_candidate_choices",
]
