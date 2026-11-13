#!/usr/bin/env python3
"""Local, same-origin web application for the key-structure generator.

The server intentionally binds to loopback only. Source templates stay
read-only. Explicit configuration writes create a backup and atomically replace
only the selected config_level table.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import math
import mimetypes
import os
import secrets
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlsplit


SCRIPT_ROOT = Path(__file__).resolve().parent
TOOL_ROOT = SCRIPT_ROOT.parent
FRONTEND_ROOT = TOOL_ROOT / "frontend"
ASSET_ROOT = FRONTEND_ROOT / "assets"
FONT_ROOT = ASSET_ROOT / "fonts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from puzzle_core.config_io import (  # noqa: E402
    ConfigError,
    ResolvedBatchConfig,
    loads_batch_request,
    resolve_batch_config,
)
from puzzle_core.errors import (  # noqa: E402
    InputValidationError,
    OperationCancelledError,
    PuzzleToolError,
)
from puzzle_core.planning import canonical_json, resolve_batch  # noqa: E402
from puzzle_core.search import ALGORITHM_VERSION  # noqa: E402
from puzzle_core.template_reader import parse_level_bytes  # noqa: E402
from services.export_service import (  # noqa: E402
    ExportRequest,
    export_selected,
    new_idempotency_key,
)
from services.import_service import (  # noqa: E402
    MAX_SOURCE_BYTES,
    LoadedTemplate,
    inspect_loaded_template,
    load_template_id,
)
from services.job_service import (  # noqa: E402
    JOB_FORMAT_VERSION,
    GenerationBatchResult,
    GenerationItemResult,
    generate_batch,
)
from services.parameter_recommendation_service import (  # noqa: E402
    STRATEGIES,
    apply_generation_parameter_changes,
    build_generation_parameter_recommendations,
    build_generation_probe_request,
    build_parameter_recommendations,
    discover_config_sets,
    discover_unlaunched_config_sets,
    load_export_manifest,
    load_uploaded_export_manifest,
    lookup_unlaunched_template_parameters,
)
from services.consumption_calibration_service import (  # noqa: E402
    MAX_HISTORY_BYTES,
    calibrate_consumption_parameters as build_consumption_calibration,
    match_calibration_templates,
)
from services.calibration_config_write_service import (  # noqa: E402
    write_calibration_config,
)
from services.desktop_service import open_directory  # noqa: E402
from services.profile_service import Profile, ProfileService  # noqa: E402
from services.selection_service import (  # noqa: E402
    CandidateChoice,
    parse_selection_payload,
    resolve_export_selections,
    summarize_candidate_choices,
)
from services.task_control import CooperativeTaskControl  # noqa: E402
from services.power_guard import prevent_idle_sleep  # noqa: E402
from services.ui_service import (  # noqa: E402
    build_candidate_board,
    build_candidate_comparison,
    build_source_board,
)


MAX_JSON_BODY = 96 * 1024 * 1024
def local_path_candidates():
    home = Path.home()
    roots = (home, home / "Projects", home / "Documents", home / "Desktop")
    template = next(
        (str(base / suffix) for suffix in (
            "tile/Tile/Assets/Resources/TileMatchLevelData", "tileleveltemplate/Template"
        ) for base in roots if (base / suffix).is_dir()), ""
    )
    config = next((str(base / "ulinkconfig") for base in roots
                   if (base / "ulinkconfig/csv/config_level.csv").is_file()), "")
    return {"template": template, "config": config}


APP_VERSION = "0.2.1"
MAX_BATCH_SEED = (1 << 64) - 1
MAX_TIMELINE_EVENTS = 100
MAX_SEARCH_BUDGET_MULTIPLIER = 16
RECOVERABLE_PREFLIGHT_CODES = frozenset(
    {
        "insufficient_tiles_for_recycles",
        "first_recycle_lead_impossible",
    }
)


def _json_ready(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _append_timeline_event(
    record: Any,
    event_type: str,
    *,
    status: str,
    message: str,
    details: Mapping[str, Any] | None = None,
    at: str | None = None,
    dedupe_scope: str | None = None,
    dedupe_value: Any = None,
) -> bool:
    """Append one observed state change to a bounded per-record timeline."""

    with record._events_lock:
        if dedupe_scope is not None:
            if record._event_dedupe.get(dedupe_scope) == dedupe_value:
                return False
            record._event_dedupe[dedupe_scope] = dedupe_value
        occurred_at = at or _now()
        # Sequence is the ordering authority. Also guard display order against
        # a local wall-clock adjustment while a task is running.
        if record._last_event_at is not None and occurred_at < record._last_event_at:
            occurred_at = record._last_event_at
        record._last_event_at = occurred_at
        record._event_sequence += 1
        record.events.append(
            {
                "sequence": record._event_sequence,
                "at": occurred_at,
                "type": event_type,
                "status": status,
                "message": message,
                "details": {} if details is None else dict(details),
            }
        )
        if len(record.events) > MAX_TIMELINE_EVENTS:
            del record.events[:-MAX_TIMELINE_EVENTS]
        return True


def _caused_by_os_error(error: Exception) -> OSError | None:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError):
            return current
        current = current.__cause__ or current.__context__
    return None


def _safe_os_error_reason(error: OSError) -> str:
    errno = getattr(error, "errno", None)
    return f"os_error_{errno}" if isinstance(errno, int) else "os_error"


def _error_payload(error: Exception) -> dict[str, Any]:
    os_error = _caused_by_os_error(error)
    if isinstance(error, ConfigError):
        payload = error.as_dict()
        if os_error is not None:
            payload["message"] = "本地配置文件无法读取"
            payload["reason"] = _safe_os_error_reason(os_error)
        return payload
    if isinstance(error, PuzzleToolError):
        payload = error.detail.as_dict()
        if os_error is not None:
            payload.pop("evidence", None)
            payload["message"] = "本地文件或目录无法读取"
            payload["reason"] = _safe_os_error_reason(os_error)
        return payload
    if isinstance(error, FileNotFoundError):
        return {
            "code": "not_found",
            "reason": "file_not_found",
            "message": "本地文件或目录不存在",
        }
    if isinstance(error, OSError):
        return {
            "code": "os_error",
            "reason": _safe_os_error_reason(error),
            "message": "本地文件操作失败",
        }
    return {"code": type(error).__name__, "message": str(error)}


def _duplicate_rejecting_json_object(
    pairs: Sequence[tuple[str, Any]],
) -> dict[str, Any]:
    """Build one JSON object without silently accepting repeated fields."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InputValidationError(
                "请求 JSON 包含重复字段",
                evidence={"field": key},
            )
        result[key] = value
    return result


def _required_decimal_int(
    payload: Mapping[str, Any],
    field_name: str,
    *,
    minimum: int | None = None,
) -> int:
    """Parse an API integer exactly; reject bools, floats and implicit coercion."""

    value = payload.get(field_name)
    if isinstance(value, bool):
        raise InputValidationError(
            f"{field_name} 必须是整数", evidence={"field": field_name}
        )
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        normalized = value.strip()
        if not normalized.isascii() or not normalized.isdecimal():
            raise InputValidationError(
                f"{field_name} 必须是十进制整数",
                evidence={"field": field_name},
            )
        parsed = int(normalized)
    else:
        raise InputValidationError(
            f"{field_name} 必须是整数", evidence={"field": field_name}
        )
    if minimum is not None and parsed < minimum:
        raise InputValidationError(
            f"{field_name} 不能小于 {minimum}",
            evidence={"field": field_name, "value": parsed},
        )
    return parsed


def _decode_file(item: Mapping[str, Any]) -> tuple[str, bytes]:
    name = str(item.get("name", "")).strip()
    encoded = item.get("data_base64")
    if not name.endswith("_m.bytes") or not isinstance(encoded, str):
        raise InputValidationError("上传模板必须包含 _m.bytes 文件名与数据")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise InputValidationError("上传模板的 Base64 数据无效") from exc
    if not data or len(data) > MAX_SOURCE_BYTES:
        raise InputValidationError(
            "上传模板大小不在安全范围内",
            evidence={"name": name, "size": len(data)},
        )
    return name, data


def _decode_csv_upload(
    item: Mapping[str, Any], *, label: str = "历史副本数据"
) -> tuple[str, bytes]:
    """Decode one in-memory CSV without retaining or writing the upload."""

    name = str(item.get("name", "")).strip()
    encoded = item.get("data_base64")
    if not name.lower().endswith(".csv") or not isinstance(encoded, str):
        raise InputValidationError(f"{label}必须包含 CSV 文件名与 Base64 数据")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise InputValidationError(f"{label}的 Base64 无效") from exc
    if not data or len(data) > MAX_HISTORY_BYTES:
        raise InputValidationError(
            f"{label}大小不在安全范围内",
            evidence={"name": name, "size": len(data)},
        )
    return name, data


def _loaded_from_bytes(name: str, data: bytes) -> LoadedTemplate:
    try:
        level = parse_level_bytes(data, source_name=name)
    except (ValueError, TypeError) as exc:
        raise InputValidationError(
            "模板 protobuf 解析失败", evidence={"source_filename": name}
        ) from exc
    return LoadedTemplate(
        level=level,
        input_mode="file",
        source_locator=name,
        source_filename=name,
    )


def _normalize_web_batch_request(value: Mapping[str, Any]) -> dict[str, Any]:
    """Accept an exact decimal uint64 seed without routing it through JS Number."""

    request = dict(value)
    seed = request.get("batch_seed")
    if isinstance(seed, str):
        normalized = seed.strip()
        if not normalized.isascii() or not normalized.isdecimal():
            raise InputValidationError("谜题生成种子必须是非负十进制整数")
        parsed = int(normalized)
        if parsed > MAX_BATCH_SEED:
            raise InputValidationError("谜题生成种子超过 uint64 上限")
        request["batch_seed"] = parsed
    return request


def _browser_batch_request(value: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize an editable request without losing a uint64 seed in JS."""

    request = copy.deepcopy(dict(value))
    if "batch_seed" in request:
        request["batch_seed"] = str(request["batch_seed"])
    return request


def _normalize_retry_scope(
    value: Mapping[str, Any],
    *,
    field_name: str,
) -> dict[str, str]:
    template_key = str(value.get("template_key", "")).strip()
    plan_id = str(
        value.get(
            "plan_id",
            value.get("source_plan_id", value.get("resolved_plan_id", "")),
        )
    ).strip()
    if not template_key:
        raise InputValidationError(f"{field_name} 缺少模板 ID")
    return {"template_key": template_key, "plan_id": plan_id}


def _retry_scope_matches(
    scope: Mapping[str, str],
    *,
    template_key: str,
    plan_id: str,
    source_plan_id: str | None = None,
) -> bool:
    scoped_plan = str(scope.get("plan_id", ""))
    return str(scope.get("template_key", "")) == template_key and (
        not scoped_plan
        or scoped_plan == plan_id
        or (source_plan_id is not None and scoped_plan == source_plan_id)
    )


def _normalize_parameter_override(
    value: Mapping[str, Any],
    *,
    field_name: str,
) -> dict[str, Any]:
    scope = _normalize_retry_scope(value, field_name=field_name)
    if not scope["plan_id"]:
        raise InputValidationError(f"{field_name} 缺少方案 ID")
    changes = value.get("changes")
    if not isinstance(changes, Mapping) or not changes:
        raise InputValidationError(f"{field_name} 缺少参数修改")
    raw_unit_index = value.get("unit_index")
    if raw_unit_index is not None and (
        isinstance(raw_unit_index, bool)
        or not isinstance(raw_unit_index, int)
        or raw_unit_index < 0
    ):
        raise InputValidationError(f"{field_name} 的谜题索引无效")
    return {
        **scope,
        "changes": copy.deepcopy(dict(changes)),
        **({"unit_index": raw_unit_index} if raw_unit_index is not None else {}),
    }


def _dedupe_retry_scopes(
    values: list[dict[str, str]],
) -> tuple[dict[str, str], ...]:
    """Canonicalize scopes while preserving their user-facing order."""

    normalized: list[dict[str, str]] = []
    for value in values:
        template_key = value["template_key"]
        plan_id = value["plan_id"]
        if not plan_id:
            normalized = [
                scope
                for scope in normalized
                if scope["template_key"] != template_key
            ]
            normalized.append(dict(value))
            continue
        if any(
            scope["template_key"] == template_key and not scope["plan_id"]
            for scope in normalized
        ):
            continue
        if value not in normalized:
            normalized.append(dict(value))
    return tuple(normalized)


def _plan_payload(config: ResolvedBatchConfig) -> dict[str, Any]:
    return {
        "source_config_hash": config.source_config_hash,
        # JSON/JavaScript Number cannot exactly represent every uint64 value.
        "batch_seed": str(config.batch.batch_seed),
        "batch_seed_source": config.batch_seed_source,
        "batch": asdict(config.batch),
        "work_items": [
            {
                "template_key": item.template_key,
                "plan": asdict(item.plan),
                "generation_options_preview": asdict(item.generation_options),
                "parameter_source": item.parameter_source,
            }
            for item in config.work_items
        ],
    }


@dataclass
class ImportRecord:
    import_id: str
    created_at: str
    input_mode: str
    sources: dict[str, LoadedTemplate]


@dataclass
class JobRecord:
    job_id: str
    created_at: str
    import_id: str
    config_payload: dict[str, Any]
    config: ResolvedBatchConfig
    sources: dict[str, LoadedTemplate]
    status: str = "queued"
    started_at: str | None = None
    completed_at: str | None = None
    last_progress_at: str | None = None
    result: GenerationBatchResult | None = None
    error: dict[str, Any] | None = None
    presentation: dict[str, Any] | None = None
    export_result: dict[str, Any] | None = None
    exported_manifests: list[dict[str, Any]] = field(default_factory=list)
    progress_completed: int = 0
    current_work_item: dict[str, Any] | None = None
    parent_job_id: str | None = None
    retry_scope: dict[str, str] | None = None
    retry_scopes: tuple[dict[str, str], ...] = ()
    parameter_overrides: tuple[dict[str, Any], ...] = ()
    retry_statuses: tuple[str, ...] = ("failed", "cancelled", "pending")
    excluded_retry_failure_kinds: tuple[str, ...] = ("parameter_conflict",)
    attempt_budget_multiplier: int = 1
    cumulative_elapsed_seconds: float = 0.0
    last_run_elapsed_seconds: float = 0.0
    retained_candidate_count: int = 0
    parameter_recommendations: list[dict[str, Any]] = field(default_factory=list)
    finish_partial_requested: bool = False
    control: CooperativeTaskControl = field(default_factory=CooperativeTaskControl)
    worker: threading.Thread | None = field(default=None, repr=False)
    recommendation_worker: threading.Thread | None = field(default=None, repr=False)
    revision: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    _events_lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False, compare=False
    )
    _event_sequence: int = field(default=0, repr=False, compare=False)
    _last_event_at: str | None = field(default=None, repr=False, compare=False)
    _event_dedupe: dict[str, Any] = field(
        default_factory=dict, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        self.add_event(
            "queued",
            status="queued",
            message="生成任务已排队",
            details={"work_item_count": len(self.config.work_items)},
            at=self.created_at,
        )

    def add_event(
        self,
        event_type: str,
        *,
        status: str,
        message: str,
        details: Mapping[str, Any] | None = None,
        at: str | None = None,
        dedupe_scope: str | None = None,
        dedupe_value: Any = None,
    ) -> bool:
        return _append_timeline_event(
            self,
            event_type,
            status=status,
            message=message,
            details=details,
            at=at,
            dedupe_scope=dedupe_scope,
            dedupe_value=dedupe_value,
        )

    def event_snapshot(self) -> list[dict[str, Any]]:
        with self._events_lock:
            return [
                dict(event, details=dict(event["details"])) for event in self.events
            ]

    def summary(self) -> dict[str, Any]:
        result_summary = None
        if self.result is not None:
            result_summary = self.result.as_dict().get("summary")
        control = self.control.snapshot()
        visible_status = self.status
        if self.status in {"queued", "running", "pause_requested", "paused"}:
            if control.state in {"pause_requested", "paused"}:
                visible_status = control.state
            elif control.state == "cancel_requested":
                visible_status = "cancel_requested"
        total_work_items = len(self.config.work_items)
        completed_work_items = min(self.progress_completed, total_work_items)
        candidate_target_count = sum(
            item.generation_options.candidate_count
            for item in self.config.work_items
        )
        candidate_found_count = (
            0
            if self.result is None
            else sum(len(item.candidates) for item in self.result.items)
        )
        budget_exhausted = bool(
            self.result
            and any(
                item.error
                and item.error.get("failure_kind") == "budget_exhausted"
                for item in self.result.items
            )
        )
        generic_retryable = self.result is None or any(
            item.status in {"cancelled", "pending"}
            or (
                item.status in {"failed", "partial"}
                and (
                    not item.error
                    or item.error.get("failure_kind")
                    not in {"parameter_conflict", "budget_exhausted"}
                )
            )
            for item in (() if self.result is None else self.result.items)
        )
        recommendations_pending = any(
            (item.get("trial_result") or {}).get("status") == "pending"
            for item in self.parameter_recommendations
        )
        recommendation_verified_count = sum(
            bool(item.get("verified"))
            for item in self.parameter_recommendations
        )
        can_use_partial_results = bool(
            self.result and any(item.candidates for item in self.result.items)
        ) and self.status in {
            "running",
            "pause_requested",
            "paused",
            "partial",
            "cancelled",
        }
        search_limit_reached = bool(
            budget_exhausted
            and self.attempt_budget_multiplier >= MAX_SEARCH_BUDGET_MULTIPLIER
        )
        result_items = () if self.result is None else self.result.items
        result_by_key = {
            (item.template_key, item.plan_id): item for item in result_items
            if hasattr(item, "template_key") and hasattr(item, "plan_id")
        }
        budget_items: list[dict[str, Any]] = []
        resume_items: list[dict[str, Any]] = []
        cumulative_attempts_completed = sum(
            int(getattr(item, "attempts_completed", 0) or 0)
            for item in result_items
        )
        for work_index, work in enumerate(self.config.work_items):
            item = result_by_key.get((work.template_key, work.plan.plan_id))
            if (
                item is None
                and work_index < len(result_items)
                and not hasattr(result_items[work_index], "template_key")
            ):
                # Compatibility for older/local test snapshots that predate
                # explicit work-item identity fields.
                item = result_items[work_index]
            target = int(work.generation_options.candidate_count)
            candidates = () if item is None else tuple(item.candidates)
            base_budget = int(work.generation_options.attempt_budget)
            current_budget = base_budget * self.attempt_budget_multiplier
            item_error = {} if item is None else (item.error or {})
            completed = 0 if item is None else getattr(item, 'shared_attempts_completed', None)
            if completed is None:
                completed = int(getattr(item, 'attempts_completed', 0) or 0)
            unfinished_slots = ([{"attempts_completed": completed}]
                                if len(candidates) < target else [])
            if unfinished_slots:
                failure_kind = str(item_error.get("failure_kind") or "")
                blocked_reason = None
                if failure_kind == "parameter_conflict":
                    blocked_reason = "parameter_conflict"
                elif (
                    failure_kind == "budget_exhausted"
                    and self.attempt_budget_multiplier
                    >= MAX_SEARCH_BUDGET_MULTIPLIER
                ):
                    blocked_reason = "search_budget_limit_reached"
                resume_items.append(
                    {
                        "template_key": work.template_key,
                        "plan_id": work.plan.plan_id,
                        "status": "pending" if item is None else item.status,
                        "failure_kind": failure_kind or None,
                        "resumable": blocked_reason is None,
                        "blocked_reason": blocked_reason,
                        "unfinished_candidate_slots": max(0, target - len(candidates)),
                    }
                )
                budget_items.append(
                    {
                        "template_key": work.template_key,
                        "plan_id": work.plan.plan_id,
                        "base_attempt_budget": base_budget,
                        "current_attempt_budget": current_budget,
                        "attempts_completed": completed,
                        "scope": "per_work_item",
                        "unfinished_slots": unfinished_slots,
                        "resumable": blocked_reason is None,
                        "blocked_reason": blocked_reason,
                    }
                )
        recommended_next_multiplier = (
            min(
                MAX_SEARCH_BUDGET_MULTIPLIER,
                max(2, self.attempt_budget_multiplier * 2),
            )
            if budget_exhausted and not search_limit_reached
            else self.attempt_budget_multiplier
        )
        recommended_budget_items: list[dict[str, Any]] = []
        next_incremental_attempts = 0
        for item in budget_items:
            if not item["resumable"]:
                # Keep blocked items in the current-budget/resume-state views,
                # but never count them as work planned for the next run.
                continue
            next_budget = (
                int(item["base_attempt_budget"]) * recommended_next_multiplier
            )
            per_slot = []
            for slot in item["unfinished_slots"]:
                incremental = max(
                    0, next_budget - int(slot["attempts_completed"])
                )
                next_incremental_attempts += incremental
                per_slot.append(
                    {
                        **slot,
                        "additional_attempts": incremental,
                    }
                )
            recommended_budget_items.append(
                {
                    "template_key": item["template_key"],
                    "plan_id": item["plan_id"],
                    "recommended_next_attempt_budget": next_budget,
                    "additional_attempts": sum(slot['additional_attempts'] for slot in per_slot),
                    "scope": "per_work_item",
                    "unfinished_slots": per_slot,
                }
            )
        if (
            cumulative_attempts_completed > 0
            and self.cumulative_elapsed_seconds > 0
            and next_incremental_attempts > 0
        ):
            seconds_per_attempt = (
                self.cumulative_elapsed_seconds / cumulative_attempts_completed
            )
            estimated_seconds = seconds_per_attempt * next_incremental_attempts
            duration_estimate: dict[str, Any] = {
                "status": "available",
                "kind": "heuristic",
                "low_seconds": round(max(0.1, estimated_seconds * 0.75), 1),
                "high_seconds": round(max(0.2, estimated_seconds * 1.5), 1),
                "basis": {
                    "observed_elapsed_seconds": round(
                        self.cumulative_elapsed_seconds, 3
                    ),
                    "observed_attempts": cumulative_attempts_completed,
                    "planned_additional_attempts": next_incremental_attempts,
                    "scope": "same_task_wall_clock_per_attempt",
                },
                "message": "根据本任务已有耗时与尝试数粗略估算，不包含暂停后的系统负载变化。",
            }
        else:
            duration_estimate = {
                "status": "unavailable",
                "reason": (
                    "尚无足够的本任务真实耗时/尝试记录，无法可靠估算。"
                    if next_incremental_attempts > 0
                    else "当前没有可执行的新增搜索区间。"
                ),
            }
        probability_estimate = {
            "status": "unavailable",
            "reason": "找到足够候选即停止，当前记录不能可靠预测续搜成功率。",
        }
        can_resume_unfinished = (
            any(item["resumable"] for item in resume_items)
            and self.status in {"partial", "failed", "cancelled"}
            and (self.worker is None or not self.worker.is_alive())
        )
        summary_retry_scopes = self.retry_scopes or (
            () if self.retry_scope is None else (self.retry_scope,)
        )
        return {
            "job_id": self.job_id,
            "parent_job_id": self.parent_job_id,
            "retry_scope": None if self.retry_scope is None else dict(self.retry_scope),
            "retry_scopes": [dict(scope) for scope in summary_retry_scopes],
            "parameter_overrides": [
                {
                    **override,
                    "changes": dict(override.get("changes") or {}),
                }
                for override in self.parameter_overrides
            ],
            "import_id": self.import_id,
            "batch_request": _browser_batch_request(self.config_payload),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "last_progress_at": self.last_progress_at,
            "status": visible_status,
            "revision": self.revision,
            "control": control.as_dict(),
            "actions": {
                "can_pause": control.can_pause and self.status in {"queued", "running"},
                "can_resume": control.can_resume,
                "can_cancel": control.can_cancel
                and self.status not in {"completed", "partial", "failed", "cancelled"},
                "can_retry": can_resume_unfinished,
                "can_continue_search": can_resume_unfinished,
                "can_use_partial_results": can_use_partial_results,
                "can_finish_partial": can_use_partial_results
                and self.status in {"running", "pause_requested", "paused"},
                "can_resume_unfinished": can_resume_unfinished,
                "resume_unfinished": {
                    "id": "resume_unfinished",
                    "enabled": can_resume_unfinished,
                    "legacy_aliases": ["continue_search", "retry"],
                    "budget_parameter": "attempt_budget_multiplier",
                    "default_attempt_budget_multiplier": recommended_next_multiplier,
                    "message": "仅续跑未完成候选槽，已找到的候选保留。",
                },
            },
            "template_count": len(self.sources),
            "work_item_count": total_work_items,
            "progress": {
                "completed_work_items": completed_work_items,
                "total_work_items": total_work_items,
                "percent": (
                    100
                    if total_work_items == 0
                    else round(completed_work_items * 100 / total_work_items)
                ),
                "current_work_item": (
                    None
                    if self.current_work_item is None
                    else dict(self.current_work_item)
                ),
            },
            "search": {
                "round": {
                    "retained_candidate_count": self.retained_candidate_count,
                    "new_candidate_count": max(0, candidate_found_count - self.retained_candidate_count),
                },
                "candidate_found_count": candidate_found_count,
                "candidate_target_count": candidate_target_count,
                "percent": (
                    100
                    if candidate_target_count == 0
                    else round(candidate_found_count * 100 / candidate_target_count)
                ),
                "attempt_budget_multiplier": self.attempt_budget_multiplier,
                "limit_reached": search_limit_reached,
                "budget": {
                    "scope": "per_work_item",
                    "scope_description": "每个成品项共用搜索上限，找到足够候选即结束；续搜保留已有候选，仅执行新增次数。",
                    "current_attempt_budget_by_item": budget_items,
                    "cumulative_attempts_completed": cumulative_attempts_completed,
                    "recommended_next_multiplier": recommended_next_multiplier,
                    "recommended_next_attempt_budget_by_item": recommended_budget_items,
                    "limit_multiplier": MAX_SEARCH_BUDGET_MULTIPLIER,
                    "resume_items": resume_items,
                },
                "estimated_additional_duration": duration_estimate,
                "success_probability": probability_estimate,
            },
            "result_summary": result_summary,
            "parameter_recommendations": [
                {
                    **item,
                    "changes": dict(item.get("changes") or {}),
                    "trial_result": dict(item.get("trial_result") or {}),
                }
                for item in self.parameter_recommendations
            ],
            "recommendations_pending": recommendations_pending,
            "recommendation_summary": {
                "status": (
                    "pending"
                    if recommendations_pending
                    else "ready"
                    if self.parameter_recommendations
                    else "not_applicable"
                ),
                "count": len(self.parameter_recommendations),
                "verified_count": recommendation_verified_count,
            },
            "error": self.error,
            "exported": bool(self.export_result),
            "events": self.event_snapshot(),
        }


@dataclass
class ExportRunRecord:
    export_id: str
    job_id: str
    created_at: str
    idempotency_key: str
    payload: dict[str, Any]
    output_directory: str = ""
    profile_snapshot: Profile | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    locked_selections: tuple[CandidateChoice, ...] = field(default_factory=tuple)
    status: str = "queued"
    started_at: str | None = None
    completed_at: str | None = None
    last_progress_at: str | None = None
    result: dict[str, Any] | None = None
    exported_manifests: list[dict[str, Any]] = field(default_factory=list)
    parameter_configured_at: str | None = None
    error: dict[str, Any] | None = None
    control: CooperativeTaskControl = field(default_factory=CooperativeTaskControl)
    worker: threading.Thread | None = field(default=None, repr=False)
    revision: int = 0
    progress_completed: int = 0
    progress_total: int = 0
    progress_item: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    _events_lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False, compare=False
    )
    _event_sequence: int = field(default=0, repr=False, compare=False)
    _last_event_at: str | None = field(default=None, repr=False, compare=False)
    _event_dedupe: dict[str, Any] = field(
        default_factory=dict, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        self.add_event(
            "queued",
            status="queued",
            message="导出任务已排队",
            details={"selection_count": len(self.locked_selections)},
            at=self.created_at,
        )

    def add_event(
        self,
        event_type: str,
        *,
        status: str,
        message: str,
        details: Mapping[str, Any] | None = None,
        at: str | None = None,
        dedupe_scope: str | None = None,
        dedupe_value: Any = None,
    ) -> bool:
        return _append_timeline_event(
            self,
            event_type,
            status=status,
            message=message,
            details=details,
            at=at,
            dedupe_scope=dedupe_scope,
            dedupe_value=dedupe_value,
        )

    def event_snapshot(self) -> list[dict[str, Any]]:
        with self._events_lock:
            return [
                dict(event, details=dict(event["details"])) for event in self.events
            ]

    def summary(self) -> dict[str, Any]:
        control = self.control.snapshot()
        visible_status = self.status
        if self.status in {"queued", "running", "pause_requested", "paused"}:
            if control.state in {"pause_requested", "paused"}:
                visible_status = control.state
            elif control.state == "committing":
                visible_status = "committing"
            elif control.state == "cancel_requested":
                visible_status = "cancel_requested"
        return {
            "export_id": self.export_id,
            # Parameter configuration is anchored to one immutable export
            # run, not to the job's latest export.  Exposing the source id and
            # marker lets recovered screens keep that provenance explicit.
            "source_export_id": self.export_id,
            "job_id": self.job_id,
            "idempotency_key": self.idempotency_key,
            "output_directory": self.output_directory,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "last_progress_at": self.last_progress_at,
            "status": visible_status,
            "revision": self.revision,
            "progress": {
                "completed": self.progress_completed,
                "total": self.progress_total,
                "current_item": self.progress_item,
            },
            "locked_selections": [
                {
                    "template_key": choice.template_key,
                    "resolved_plan_id": choice.resolved_plan_id,
                    "candidate_id": choice.candidate_id,
                    "online_level_number": choice.online_level_number,
                    "level_id": choice.level_id,
                    "manual_evidence_override": choice.manual_evidence_override,
                }
                for choice in self.locked_selections
            ],
            "selection_summary": summarize_candidate_choices(
                self.locked_selections
            ),
            "manifest_count": len(self.exported_manifests),
            "parameter_configured": self.parameter_configured_at is not None,
            "parameter_configured_at": self.parameter_configured_at,
            "control": control.as_dict(),
            "actions": {
                "can_pause": control.can_pause and self.status in {"queued", "running"},
                "can_resume": control.can_resume,
                "can_cancel": control.can_cancel
                and self.status not in {"completed", "failed", "cancelled"},
                "can_retry": self.status in {"failed", "cancelled"}
                and (self.worker is None or not self.worker.is_alive()),
            },
            "result": self.result,
            "error": self.error,
            "events": self.event_snapshot(),
        }


@dataclass
class GenerationPreflightRecord:
    """Transient context for applying a verified pre-generation adjustment."""

    preflight_id: str
    created_at: str
    import_id: str
    config_payload: dict[str, Any]
    sources: dict[str, LoadedTemplate]
    error: dict[str, Any]
    parameter_recommendations: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        verified_count = sum(
            item.get("verified") is True
            and (item.get("trial_result") or {}).get("status") == "passed"
            for item in self.parameter_recommendations
        )
        return {
            "valid": False,
            "preflight_id": self.preflight_id,
            "created_at": self.created_at,
            "import_id": self.import_id,
            "batch_request": _browser_batch_request(self.config_payload),
            "error": dict(self.error),
            "parameter_recommendations": [
                {
                    **item,
                    "changes": dict(item.get("changes") or {}),
                    "trial_result": dict(item.get("trial_result") or {}),
                }
                for item in self.parameter_recommendations
            ],
            # Trials are deliberately synchronous and bounded so the browser
            # receives a settled card together with the preflight failure.
            "recommendations_pending": False,
            "recommendation_summary": {
                "status": (
                    "ready" if self.parameter_recommendations else "not_applicable"
                ),
                "count": len(self.parameter_recommendations),
                "verified_count": verified_count,
            },
        }


@dataclass
class ConsumptionCalibrationRecord:
    job_id: str
    request_fingerprint: str
    created_at: str
    created_monotonic: float
    progress: dict[str, Any]
    status: str = "queued"
    started_at: str | None = None
    completed_at: str | None = None
    completed_monotonic: float | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    def summary(self) -> dict[str, Any]:
        return copy.deepcopy({
            "job_id": self.job_id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_s": round(
                (self.completed_monotonic or time.monotonic())
                - self.created_monotonic,
                3,
            ),
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
        })


class AppState:
    def __init__(self, profile_service: ProfileService) -> None:
        self.profile_service = profile_service
        self.csrf_token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.imports: dict[str, ImportRecord] = {}
        self.jobs: dict[str, JobRecord] = {}
        self.exports: dict[str, ExportRunRecord] = {}
        self.generation_preflights: dict[str, GenerationPreflightRecord] = {}
        self.parameter_selections: dict[str, dict[str, Any]] = {}
        self.consumption_calibration_jobs: dict[str, ConsumptionCalibrationRecord] = {}
        self.calibration_contexts: dict[str, dict[str, Any]] = {}
        self.generation_recommendation_undos: dict[str, dict[str, Any]] = {}
        self.generation_preflight_undos: dict[str, dict[str, Any]] = {}

    def profile(self) -> Profile:
        profile = self.profile_service.active_profile()
        if profile is None:
            profile = self.profile_service.initialize(TOOL_ROOT)
        return profile

    def profile_status(self) -> dict[str, Any]:
        profile = self.profile()
        return self.profile_service.status(profile.profile_id)

    def import_templates(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        mode = str(payload.get("mode", "template_ids"))
        sources: dict[str, LoadedTemplate] = {}
        if mode == "template_ids":
            values = payload.get("template_ids")
            if not isinstance(values, list) or not values:
                raise InputValidationError("请至少输入一个模板 ID")
            profile = self.profile()
            for raw in values:
                key = str(raw).strip()
                if key in sources:
                    continue
                sources[key] = load_template_id(profile, key)
        elif mode == "files":
            values = payload.get("files")
            if not isinstance(values, list) or not values:
                raise InputValidationError("请至少上传一个 _m.bytes 文件")
            for raw in values:
                if not isinstance(raw, Mapping):
                    raise InputValidationError("上传文件项格式错误")
                name, data = _decode_file(raw)
                loaded = _loaded_from_bytes(name, data)
                key = loaded.level.template_id
                if key in sources:
                    raise InputValidationError(
                        "同一模板不能重复导入", evidence={"template_id": key}
                    )
                sources[key] = loaded
        else:
            raise InputValidationError("未知的模板导入方式")
        import_id = f"import_{uuid.uuid4().hex}"
        record = ImportRecord(import_id, _now(), mode, sources)
        with self.lock:
            self.imports[import_id] = record
        return {
            "import_id": import_id,
            "input_mode": mode,
            "items": [
                {
                    "template_key": key,
                    "inspection": inspect_loaded_template(source).as_dict(),
                    "board": build_source_board(source),
                }
                for key, source in sources.items()
            ],
        }

    @staticmethod
    def _preflight_scope(
        parsed: Any, error: ConfigError
    ) -> tuple[str, str, str, str]:
        """Locate the exact template and source plan named by a config error."""

        matched = re.search(
            r"templates\[([^\]]+)\]\.([^\.\[]+)(?:\.|$)", error.path
        )
        if matched is None:
            raise InputValidationError(
                "无法定位需调整的模板与方案",
                evidence=error.as_dict(),
            )
        template_key, resolved_plan_id = matched.groups()
        planned = resolve_batch(parsed.request)
        for template in planned.templates:
            if template.template_key != template_key:
                continue
            for plan in template.plans:
                if plan.plan_id == resolved_plan_id:
                    return (
                        template_key,
                        plan.source_plan_id,
                        plan.plan_id,
                        plan.tower_mode.value,
                    )
        raise InputValidationError(
            "无法定位需调整的生成方案",
            evidence={
                **error.as_dict(),
                "template_key": template_key,
                "resolved_plan_id": resolved_plan_id,
            },
        )

    @staticmethod
    def _apply_preflight_card(
        config_payload: Mapping[str, Any], card: Mapping[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Apply every unit-scoped edit represented by one preflight card."""

        template_key = str(card["template_key"])
        source_plan_id = str(card["source_plan_id"])
        raw_adjustments = card.get("unit_adjustments")
        if isinstance(raw_adjustments, list) and raw_adjustments:
            updated = copy.deepcopy(dict(config_payload))
            differences: list[dict[str, Any]] = []
            for adjustment in raw_adjustments:
                if not isinstance(adjustment, Mapping):
                    raise InputValidationError("参数建议的谜题调整格式错误")
                updated, current_differences = apply_generation_parameter_changes(
                    updated,
                    template_key=template_key,
                    source_plan_id=source_plan_id,
                    changes=dict(adjustment.get("changes") or {}),
                    unit_index=adjustment.get("unit_index"),
                )
                differences.extend(current_differences)
            return updated, differences
        return apply_generation_parameter_changes(
            config_payload,
            template_key=template_key,
            source_plan_id=source_plan_id,
            changes=dict(card.get("changes") or {}),
            unit_index=card.get("unit_index"),
        )

    @classmethod
    def _expand_preflight_card(
        cls,
        config_payload: Mapping[str, Any],
        initial_card: Mapping[str, Any],
        _initial_error: ConfigError,
    ) -> dict[str, Any]:
        """Collect the minimum edits needed by every conflicting unit.

        A plan-level default can invalidate more than one puzzle unit.  Config
        resolution reports those units one at a time, so repeatedly resolve an
        isolated one-template probe and gather each conservative unit edit.
        This never widens the change beyond the failing template/source plan.
        """

        template_key = str(initial_card["template_key"])
        source_plan_id = str(initial_card["source_plan_id"])
        adjusted = copy.deepcopy(dict(config_payload))
        current_card = copy.deepcopy(dict(initial_card))
        adjustments: list[dict[str, Any]] = []
        seen: set[tuple[int | None, str, str]] = set()
        # A request is already bounded by config limits; this lower local cap
        # also protects preflight from malformed repeated-error cycles.
        for _ in range(64):
            field = str(current_card.get("field", ""))
            unit_index = current_card.get("unit_index")
            after_key = canonical_json(current_card.get("after"))
            identity = (unit_index, field, after_key)
            if identity in seen:
                break
            seen.add(identity)
            adjusted, differences = apply_generation_parameter_changes(
                adjusted,
                template_key=template_key,
                source_plan_id=source_plan_id,
                changes=dict(current_card.get("changes") or {}),
                unit_index=unit_index,
            )
            difference = differences[0]
            adjustments.append(
                {
                    "unit_index": unit_index,
                    "field": field,
                    "before": difference["before"],
                    "after": difference["after"],
                    "changes": dict(current_card.get("changes") or {}),
                }
            )
            probe_payload = build_generation_probe_request(
                adjusted,
                template_key=template_key,
                source_plan_id=source_plan_id,
                attempt_budget=24,
            )
            probe_parsed = loads_batch_request(
                json.dumps(probe_payload, ensure_ascii=False)
            )
            try:
                resolve_batch_config(probe_parsed)
                break
            except ConfigError as next_error:
                if next_error.code not in RECOVERABLE_PREFLIGHT_CODES:
                    break
                (
                    next_template_key,
                    next_source_plan_id,
                    _next_resolved_plan_id,
                    next_tower_mode,
                ) = cls._preflight_scope(probe_parsed, next_error)
                if (
                    next_template_key != template_key
                    or next_source_plan_id != source_plan_id
                ):
                    break
                next_error_payload = {
                    **next_error.as_dict(),
                    "failure_kind": "parameter_conflict",
                }
                next_cards = build_generation_parameter_recommendations(
                    adjusted,
                    template_key=template_key,
                    source_plan_id=source_plan_id,
                    tower_mode=next_tower_mode,
                    error=next_error_payload,
                )
                expected_field = {
                    "insufficient_tiles_for_recycles": (
                        "required_pre_gate_recycles"
                    ),
                    "first_recycle_lead_impossible": "first_recycle_lead_tiles",
                }[next_error.code]
                current_card = next(
                    (
                        copy.deepcopy(dict(card))
                        for card in next_cards
                        if card.get("field") == expected_field
                    ),
                    {},
                )
                if not current_card:
                    break

        expanded = copy.deepcopy(dict(initial_card))
        expanded["unit_adjustments"] = adjustments
        affected_units = sorted(
            {
                int(item["unit_index"]) + 1
                for item in adjustments
                if isinstance(item.get("unit_index"), int)
                and not isinstance(item.get("unit_index"), bool)
            }
        )
        expanded["affected_units"] = affected_units
        if len(adjustments) > 1:
            unit_copy = "、".join(f"谜题 {value}" for value in affected_units)
            expanded["title"] = f"推荐调整：{unit_copy} 的回收规则"
            expanded["reason"] = (
                f"{unit_copy} 均与当前回收参数冲突，"
                "需一次应用各自的最小可行值。"
            )
            expanded["impact"] = (
                "只调整当前失败模板的当前方案；"
                "其他模板、方案与谜题参数不变。"
            )
        identity = hashlib.sha256(
            canonical_json(adjustments).encode("utf-8")
        ).hexdigest()[:16]
        expanded["recommendation_id"] = f"preflight_{identity}"
        return expanded

    @staticmethod
    def _verify_preflight_card(
        *,
        profile: Profile,
        config_payload: Mapping[str, Any],
        source: LoadedTemplate | None,
        card: Mapping[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        """Run one real, bounded search before a preflight card is applicable."""

        attempt_budget = 24
        try:
            if source is None:
                raise InputValidationError("当前失败模板快照已失效")
            template_key = str(card["template_key"])
            source_plan_id = str(card["source_plan_id"])
            adjusted, _differences = AppState._apply_preflight_card(
                config_payload, card
            )
            probe_payload = build_generation_probe_request(
                adjusted,
                template_key=template_key,
                source_plan_id=source_plan_id,
                attempt_budget=attempt_budget,
            )
            probe_config = resolve_batch_config(
                loads_batch_request(json.dumps(probe_payload, ensure_ascii=False))
            )
            trial = generate_batch(
                probe_config,
                profile,
                loaded_inputs={template_key: source},
            )
            found = sum(len(item.candidates) for item in trial.items)
            verified = found > 0
            return verified, {
                "status": "passed" if verified else "not_found",
                "attempt_budget": attempt_budget,
                "candidate_count": found,
                "message": (
                    f"小预算试算已找到 {found} 个合格候选。"
                    if verified
                    else "小预算试算未命中，不标记为已验证可生成。"
                ),
            }
        except Exception as exc:
            return False, {
                "status": "error",
                "attempt_budget": attempt_budget,
                "candidate_count": 0,
                "message": "小预算试算未通过。",
                "error": _error_payload(exc),
            }

    def resolve_plan(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = payload.get("batch_request", payload)
        if not isinstance(request, Mapping):
            raise InputValidationError("批次配置必须是 JSON 对象")
        request = _normalize_web_batch_request(request)
        parsed = loads_batch_request(json.dumps(request, ensure_ascii=False))
        locked_request = copy.deepcopy(dict(request))
        locked_request["batch_seed"] = parsed.request.batch_seed
        try:
            config = resolve_batch_config(parsed)
        except ConfigError as error:
            if error.code not in RECOVERABLE_PREFLIGHT_CODES:
                raise
            import_id = str(payload.get("import_id", "")).strip()
            with self.lock:
                imported = self.imports.get(import_id)
            if imported is None:
                raise InputValidationError(
                    "请先导入模板，再试算参数调整建议",
                    evidence=error.as_dict(),
                )
            configured_keys = set(parsed.request.template_keys)
            if configured_keys != set(imported.sources):
                raise InputValidationError(
                    "批次配置中的模板与已导入模板不一致",
                    evidence={
                        "configured": sorted(configured_keys),
                        "imported": sorted(imported.sources),
                    },
                )
            (
                template_key,
                source_plan_id,
                resolved_plan_id,
                tower_mode,
            ) = self._preflight_scope(parsed, error)
            error_payload: dict[str, Any] = {
                **error.as_dict(),
                "failure_kind": "parameter_conflict",
                "recommended_action": {
                    "id": "apply_verified_recommendation",
                    "label": "应用已验证参数建议",
                },
            }
            cards = build_generation_parameter_recommendations(
                locked_request,
                template_key=template_key,
                source_plan_id=source_plan_id,
                tower_mode=tower_mode,
                error=error_payload,
            )
            expected_field = {
                "insufficient_tiles_for_recycles": "required_pre_gate_recycles",
                "first_recycle_lead_impossible": "first_recycle_lead_tiles",
            }[error.code]
            cards = [
                self._expand_preflight_card(locked_request, card, error)
                for card in cards
                if card.get("field") == expected_field
            ][:1]
            for card in cards:
                card["resolved_plan_id"] = resolved_plan_id
                verified, trial_result = self._verify_preflight_card(
                    profile=self.profile(),
                    config_payload=locked_request,
                    source=imported.sources.get(template_key),
                    card=card,
                )
                card["verified"] = verified
                card["trial_result"] = trial_result
            if not any(card.get("verified") is True for card in cards):
                error_payload["recommended_action"] = {
                    "id": "adjust_parameters",
                    "label": "手动调整当前模板参数",
                }
            record = GenerationPreflightRecord(
                preflight_id=f"preflight_{uuid.uuid4().hex}",
                created_at=_now(),
                import_id=import_id,
                config_payload=locked_request,
                sources=dict(imported.sources),
                error=error_payload,
                parameter_recommendations=cards,
            )
            with self.lock:
                self.generation_preflights[record.preflight_id] = record
                while len(self.generation_preflights) > 100:
                    self.generation_preflights.pop(next(iter(self.generation_preflights)))
            return record.summary()
        return {"valid": True, **_plan_payload(config)}

    def get_generation_preflight(
        self, preflight_id: str
    ) -> GenerationPreflightRecord:
        with self.lock:
            record = self.generation_preflights.get(preflight_id)
        if record is None:
            raise InputValidationError("参数预检记录不存在或已失效")
        return record

    def apply_preflight_generation_recommendation(
        self, preflight_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Return an editable draft for one verified preflight recommendation."""

        record = self.get_generation_preflight(preflight_id)
        recommendation_id = str(payload.get("recommendation_id", "")).strip()
        card = next(
            (
                item
                for item in record.parameter_recommendations
                if item.get("recommendation_id") == recommendation_id
            ),
            None,
        )
        if card is None:
            raise InputValidationError("参数建议不存在或已失效")
        trial_result = dict(card.get("trial_result") or {})
        trial_status = str(trial_result.get("status", ""))
        if card.get("verified") is not True or trial_status != "passed":
            raise InputValidationError(
                "参数建议尚未验证可生成，不能一键应用",
                evidence={
                    "recommendation_id": recommendation_id,
                    "verified": bool(card.get("verified")),
                    "trial_status": trial_status or "unknown",
                },
            )
        template_key = str(card["template_key"])
        before_request = copy.deepcopy(record.config_payload)
        after_request, differences = self._apply_preflight_card(
            before_request, card
        )
        # A second imported template may have its own preflight conflict.  The
        # selected card is already proven by its isolated trial, so allow that
        # next recoverable conflict to be handled in another preflight pass.
        try:
            resolve_batch_config(
                loads_batch_request(json.dumps(after_request, ensure_ascii=False))
            )
        except ConfigError as error:
            if error.code not in RECOVERABLE_PREFLIGHT_CODES:
                raise
        undo_token = f"preflight_undo_{uuid.uuid4().hex}"
        undo_record = {
            "preflight_id": preflight_id,
            "recommendation_id": recommendation_id,
            "template_key": template_key,
            "before_request": before_request,
            "after_request": after_request,
            "differences": differences,
        }
        with self.lock:
            self.generation_preflight_undos[undo_token] = undo_record
            while len(self.generation_preflight_undos) > 100:
                self.generation_preflight_undos.pop(
                    next(iter(self.generation_preflight_undos))
                )
        return {
            "scope": "current_failed_template",
            "preflight_id": preflight_id,
            "import_id": record.import_id,
            "template_key": template_key,
            "recommendation_id": recommendation_id,
            "batch_request": _browser_batch_request(after_request),
            "differences": differences,
            "undo_token": undo_token,
            "verified": True,
            "trial_result": trial_result,
        }

    def undo_preflight_generation_recommendation(
        self, preflight_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        undo_token = str(payload.get("undo_token", "")).strip()
        with self.lock:
            record = self.generation_preflight_undos.get(undo_token)
            if record is None or record.get("preflight_id") != preflight_id:
                raise InputValidationError("撤销记录不存在或已失效")
            self.generation_preflight_undos.pop(undo_token, None)
        return {
            "scope": "current_failed_template",
            "preflight_id": preflight_id,
            "template_key": record["template_key"],
            "recommendation_id": record["recommendation_id"],
            "batch_request": _browser_batch_request(record["before_request"]),
            "differences": [
                {
                    **difference,
                    "before": difference["after"],
                    "after": difference["before"],
                }
                for difference in record["differences"]
            ],
            "undone": True,
        }

    def create_job(self, payload: Mapping[str, Any]) -> JobRecord:
        import_id = str(payload.get("import_id", ""))
        parent_job_id = str(payload.get("parent_job_id", "")).strip() or None

        raw_retry_scopes = payload.get("retry_scopes")
        parsed_retry_scopes: list[dict[str, str]] = []
        if raw_retry_scopes is not None:
            if not isinstance(raw_retry_scopes, (list, tuple)):
                raise InputValidationError("重试范围列表格式无效")
            if not raw_retry_scopes:
                raise InputValidationError("重试范围列表不能为空")
            for index, raw_scope in enumerate(raw_retry_scopes):
                if not isinstance(raw_scope, Mapping):
                    raise InputValidationError(
                        f"重试范围 {index + 1} 格式无效"
                    )
                parsed_retry_scopes.append(
                    _normalize_retry_scope(
                        raw_scope,
                        field_name=f"重试范围 {index + 1}",
                    )
                )
        raw_retry_scope = payload.get("retry_scope")
        legacy_retry_scope: dict[str, str] | None = None
        if raw_retry_scope is not None:
            if not isinstance(raw_retry_scope, Mapping):
                raise InputValidationError("重试范围格式无效")
            legacy_retry_scope = _normalize_retry_scope(
                raw_retry_scope,
                field_name="重试范围",
            )
            if parsed_retry_scopes:
                canonical_list = _dedupe_retry_scopes(parsed_retry_scopes)
                if canonical_list != (legacy_retry_scope,):
                    raise InputValidationError(
                        "retry_scope 与 retry_scopes 不一致，多范围请只传 retry_scopes"
                    )
            if not parsed_retry_scopes:
                parsed_retry_scopes.append(legacy_retry_scope)

        raw_parameter_overrides = payload.get(
            "parameter_overrides", payload.get("template_overrides")
        )
        parsed_parameter_overrides: list[dict[str, Any]] = []
        if raw_parameter_overrides is not None:
            if not isinstance(raw_parameter_overrides, (list, tuple)):
                raise InputValidationError("参数覆盖列表格式无效")
            if not raw_parameter_overrides:
                raise InputValidationError("参数覆盖列表不能为空")
            for index, raw_override in enumerate(raw_parameter_overrides):
                if not isinstance(raw_override, Mapping):
                    raise InputValidationError(
                        f"参数覆盖 {index + 1} 格式无效"
                    )
                parsed_parameter_overrides.append(
                    _normalize_parameter_override(
                        raw_override,
                        field_name=f"参数覆盖 {index + 1}",
                    )
                )

        with self.lock:
            imported = self.imports.get(import_id)
            parent_job = self.jobs.get(parent_job_id) if parent_job_id else None
        if imported is None:
            raise InputValidationError("导入批次已失效，请重新解析模板")
        if parent_job_id and parent_job is None:
            raise InputValidationError("原任务已失效，无法继承已有候选")
        if (parsed_retry_scopes or parsed_parameter_overrides) and parent_job is None:
            raise InputValidationError("局部参数调整缺少原任务")

        raw_request = payload.get("batch_request")
        if isinstance(raw_request, Mapping):
            submitted_request = _normalize_web_batch_request(raw_request)
        elif parent_job is not None:
            submitted_request = copy.deepcopy(parent_job.config_payload)
        else:
            raise InputValidationError("缺少批次配置")

        if parent_job is not None:
            if parent_job.import_id != import_id:
                raise InputValidationError("原任务与当前导入批次不一致")
            # Scoped adjustments keep the original seed so unaffected
            # candidates remain exactly reproducible.
            submitted_request["batch_seed"] = parent_job.config.batch.batch_seed

        if parsed_retry_scopes:
            def override_is_in_scope(override: Mapping[str, Any]) -> bool:
                if parent_job is None:
                    return False
                for work in parent_job.config.work_items:
                    if work.template_key != override["template_key"]:
                        continue
                    if override["plan_id"] not in {
                        work.plan.plan_id,
                        work.plan.source_plan_id,
                    }:
                        continue
                    if any(
                        _retry_scope_matches(
                            scope,
                            template_key=work.template_key,
                            plan_id=work.plan.plan_id,
                            source_plan_id=work.plan.source_plan_id,
                        )
                        for scope in parsed_retry_scopes
                    ):
                        return True
                return False

            selected_overrides = [
                override
                for override in parsed_parameter_overrides
                if override_is_in_scope(override)
            ]
            if len(selected_overrides) != len(parsed_parameter_overrides):
                raise InputValidationError(
                    "parameter_overrides 包含重试范围之外的模板或方案"
                )
        else:
            selected_overrides = list(parsed_parameter_overrides)
            parsed_retry_scopes.extend(
                {
                    "template_key": override["template_key"],
                    "plan_id": override["plan_id"],
                }
                for override in selected_overrides
            )
        retry_scopes = _dedupe_retry_scopes(parsed_retry_scopes)

        if parsed_parameter_overrides:
            # Parameter overrides are applied to the parent's locked request so
            # they cannot become a second, contradictory configuration truth.
            if parent_job is None:  # guarded above; keeps type narrowing clear
                raise InputValidationError("参数覆盖缺少原任务")
            request = copy.deepcopy(parent_job.config_payload)
            request["batch_seed"] = parent_job.config.batch.batch_seed
            for override in selected_overrides:
                request, _ = apply_generation_parameter_changes(
                    request,
                    template_key=override["template_key"],
                    source_plan_id=override["plan_id"],
                    changes=override["changes"],
                    unit_index=override.get("unit_index"),
                )
            if isinstance(raw_request, Mapping):
                submitted_config = resolve_batch_config(
                    loads_batch_request(
                        json.dumps(submitted_request, ensure_ascii=False)
                    )
                )
                derived_config = resolve_batch_config(
                    loads_batch_request(json.dumps(request, ensure_ascii=False))
                )
                if submitted_config.source_config_hash != derived_config.source_config_hash:
                    raise InputValidationError(
                        "batch_request 与 parameter_overrides 的最终配置不一致"
                    )
        else:
            request = submitted_request

        config = resolve_batch_config(
            loads_batch_request(json.dumps(request, ensure_ascii=False))
        )
        configured = tuple(config.batch.templates)
        expected = {item.template_key for item in configured}
        if expected != set(imported.sources):
            raise InputValidationError(
                "批次配置中的模板与已导入模板不一致",
                evidence={"configured": sorted(expected), "imported": sorted(imported.sources)},
            )
        for scope in retry_scopes:
            if scope["template_key"] not in expected:
                raise InputValidationError("重试范围中的模板不在当前批次内")
            if scope["plan_id"] and not any(
                _retry_scope_matches(
                    scope,
                    template_key=work.template_key,
                    plan_id=work.plan.plan_id,
                    source_plan_id=work.plan.source_plan_id,
                )
                for work in config.work_items
            ):
                raise InputValidationError(
                    "重试范围中的方案不在当前模板内",
                    evidence=dict(scope),
                )

        if parent_job is not None and retry_scopes:
            def work_identity(work: Any) -> tuple[str, str, str]:
                return (
                    work.template_key,
                    work.plan.source_plan_id,
                    work.plan.plan_id,
                )

            parent_work_by_identity = {
                work_identity(work): work for work in parent_job.config.work_items
            }
            child_work_by_identity = {
                work_identity(work): work for work in config.work_items
            }
            for identity in set(parent_work_by_identity) | set(child_work_by_identity):
                parent_work = parent_work_by_identity.get(identity)
                child_work = child_work_by_identity.get(identity)
                changed = (
                    parent_work is None
                    or child_work is None
                    or parent_work.plan.signature != child_work.plan.signature
                    or parent_work.generation_options
                    != child_work.generation_options
                )
                if not changed:
                    continue
                template_key, source_plan_id, plan_id = identity
                if not any(
                    _retry_scope_matches(
                        scope,
                        template_key=template_key,
                        plan_id=plan_id,
                        source_plan_id=source_plan_id,
                    )
                    for scope in retry_scopes
                ):
                    raise InputValidationError(
                        "batch_request 包含重试范围之外的配置修改",
                        evidence={
                            "template_key": template_key,
                            "plan_id": source_plan_id or plan_id,
                        },
                    )

            if parent_job.result is not None:
                parent_items_by_key = {
                    (item.template_key, item.plan_id, item.plan_signature): item
                    for item in parent_job.result.items
                }
                for work in config.work_items:
                    if not any(
                        _retry_scope_matches(
                            scope,
                            template_key=work.template_key,
                            plan_id=work.plan.plan_id,
                            source_plan_id=work.plan.source_plan_id,
                        )
                        for scope in retry_scopes
                    ):
                        continue
                    parent_item = parent_items_by_key.get(
                        (
                            work.template_key,
                            work.plan.plan_id,
                            work.plan.signature,
                        )
                    )
                    retained_count = (
                        0 if parent_item is None else len(parent_item.candidates)
                    )
                    requested_count = work.generation_options.candidate_count
                    if requested_count < retained_count:
                        raise InputValidationError(
                            "候选数不能小于该模板已保留的候选数",
                            evidence={
                                "template_key": work.template_key,
                                "plan_id": work.plan.source_plan_id
                                or work.plan.plan_id,
                                "retained_candidate_count": retained_count,
                                "requested_candidate_count": requested_count,
                            },
                        )

        raw_multiplier = payload.get("attempt_budget_multiplier")
        if raw_multiplier is None:
            attempt_budget_multiplier = (
                parent_job.attempt_budget_multiplier if parent_job is not None else 1
            )
        elif (
            isinstance(raw_multiplier, bool)
            or not isinstance(raw_multiplier, int)
            or raw_multiplier < 1
            or raw_multiplier > MAX_SEARCH_BUDGET_MULTIPLIER
        ):
            raise InputValidationError(
                "attempt_budget_multiplier 必须在 1–16 之间"
            )
        else:
            attempt_budget_multiplier = raw_multiplier

        locked_request = copy.deepcopy(dict(request))
        # A seed omitted by the browser is generated during resolution.  Lock
        # that exact value into every recommendation probe and returned draft;
        # otherwise applying a suggestion could silently create a new puzzle.
        locked_request["batch_seed"] = config.batch.batch_seed
        job = JobRecord(
            job_id=f"job_{uuid.uuid4().hex}",
            created_at=_now(),
            import_id=import_id,
            config_payload=locked_request,
            config=config,
            sources=dict(imported.sources),
            parent_job_id=parent_job_id,
            retry_scope=(
                dict(retry_scopes[0]) if len(retry_scopes) == 1 else None
            ),
            retry_scopes=tuple(dict(scope) for scope in retry_scopes),
            parameter_overrides=tuple(
                copy.deepcopy(override) for override in selected_overrides
            ),
            retry_statuses=() if parent_job_id else ("failed", "cancelled", "pending"),
            attempt_budget_multiplier=attempt_budget_multiplier,
        )
        if parent_job is not None and parent_job.result is not None:
            job.result = self._compatible_parent_result(job, parent_job.result)
            job.retained_candidate_count = sum(len(item.candidates) for item in job.result.items)
            job.presentation = self._present_result(job, job.result)
            job.progress_completed = self._completed_work_item_count(job.result)
        with self.lock:
            self.jobs[job.job_id] = job
        worker = threading.Thread(
            target=self._run_job,
            args=(job.job_id,),
            daemon=True,
        )
        with self.lock:
            job.worker = worker
        worker.start()
        return job

    def _compatible_parent_result(
        self,
        job: JobRecord,
        parent_result: GenerationBatchResult,
    ) -> GenerationBatchResult:
        """Carry parent progress into a scoped derived job.

        Generation-option changes alter how *new* candidates are searched, but
        must not erase candidates the user has already accepted as useful work.
        Rows with the same resolved plan identity therefore keep their existing
        candidates.  A scoped row is handed back to ``generate_batch`` through
        ``retry_work_item_keys`` so only its still-missing candidate slots are
        searched under the child configuration.
        """

        work_by_key = {
            (item.template_key, item.plan.source_plan_id): item
            for item in job.config.work_items
        }
        parent_job = (
            self.jobs.get(job.parent_job_id) if job.parent_job_id else None
        )
        parent_work_by_key = {
            (item.template_key, item.plan.source_plan_id): item
            for item in (
                () if parent_job is None else parent_job.config.work_items
            )
        }
        scopes = job.retry_scopes or (
            (() if job.retry_scope is None else (job.retry_scope,))
        )
        retained: list[GenerationItemResult] = []
        for item in parent_result.items:
            key = (item.template_key, item.source_plan_id or item.plan_id)
            work = work_by_key.get(key)
            parent_work = parent_work_by_key.get(key)
            if work is None:
                # The source-plan identity is the stable row identity across a
                # parameter-edit continuation.  Removed plans are not carried.
                continue
            # Recommendation cards are scoped to the source plan shown in the
            # configuration UI.  One source plan may resolve into multiple
            # concrete work items, all of which must be regenerated after the
            # local change; every other template/plan row is inherited exactly.
            is_scoped = any(
                _retry_scope_matches(
                    scope,
                    template_key=item.template_key,
                    plan_id=item.plan_id,
                    source_plan_id=work.plan.source_plan_id,
                )
                for scope in scopes
            )
            options_changed = bool(
                parent_work is not None
                and parent_work.generation_options != work.generation_options
            )
            if options_changed and not is_scoped:
                # create_job rejects out-of-scope changes.  This defensive
                # branch keeps manually restored/legacy snapshots fail-closed.
                continue
            target_count = work.generation_options.candidate_count
            effective_attempt_budget = (
                work.generation_options.attempt_budget
                * job.attempt_budget_multiplier
            )
            needs_adaptation = is_scoped and (
                options_changed
                or item.target_candidate_count != target_count
                or item.effective_attempt_budget_per_slot
                != effective_attempt_budget
                or len(item.candidates) < target_count
            )
            if not needs_adaptation:
                # Preserve exact objects for unaffected rows.  Besides avoiding
                # needless metadata churn, this makes it explicit that those
                # candidates were neither regenerated nor re-ranked.
                retained.append(item)
                continue
            carried_candidates = item.candidates
            found_count = len(carried_candidates)
            if found_count >= target_count:
                carried_status = "success"
                carried_error = None
            elif is_scoped:
                # This row is queued to fill its remaining candidate slots.  It
                # is not a completed failure while the derived job is running.
                carried_status = "pending"
                carried_error = None
            elif found_count:
                carried_status = "partial"
                carried_error = item.error
            else:
                carried_status = item.status
                carried_error = item.error

            # Attempt offsets belong to a particular option set.  Reusing them
            # after a scoped parameter change would skip the first part of the
            # *new* search space (and can make a same-budget adjustment perform
            # zero work).  Preserve the cumulative attempt count for telemetry,
            # but restart only the missing slot windows at offset zero.
            slot_attempts = (
                {}
                if options_changed and is_scoped
                else dict(item.slot_attempts_completed)
            )
            retained.append(
                replace(
                    item,
                    plan_id=work.plan.plan_id,
                    plan_signature=work.plan.signature,
                    source_plan_id=work.plan.source_plan_id,
                    parameter_source=work.parameter_source,
                    candidates=carried_candidates,
                    target_candidate_count=target_count,
                    status=carried_status,
                    error=carried_error,
                    effective_attempt_budget_per_slot=effective_attempt_budget,
                    slot_attempts_completed=slot_attempts,
                    shared_attempts_completed=(0 if options_changed and is_scoped
                                               else item.shared_attempts_completed),
                )
            )
        return replace(
            parent_result,
            batch_seed=job.config.batch.batch_seed,
            resolved_batch_hash=job.config.batch.config_hash,
            source_config_hash=job.config.source_config_hash,
            items=tuple(retained),
            run_status="running",
            batch_seed_source=job.config.batch_seed_source,
            locked_config_snapshot=(
                json.loads(job.config.normalized_config_json)
                if job.config.normalized_config_json
                else None
            ),
            total_work_item_count=len(job.config.work_items),
            total_candidate_target_count=sum(
                work.generation_options.candidate_count
                for work in job.config.work_items
            ),
        )

    @staticmethod
    def _queued_cancel_result(
        job: JobRecord,
        error: OperationCancelledError,
    ) -> GenerationBatchResult:
        """Build a complete terminal snapshot for a job cancelled before work."""

        error_payload = {
            **_error_payload(error),
            "failure_kind": "user_stopped",
            "recommended_action": {
                "id": "retry",
                "label": "重新运行",
            },
        }
        rows = tuple(
            GenerationItemResult(
                template_key=work.template_key,
                plan_id=work.plan.plan_id,
                source_plan_id=work.plan.source_plan_id,
                plan_signature=work.plan.signature,
                parameter_source=work.parameter_source,
                generation_seed=0,
                source=None,
                inspection=None,
                candidates=(),
                attempts_completed=0,
                rejection_counts={},
                target_candidate_count=work.generation_options.candidate_count,
                status="cancelled",
                error=error_payload,
                effective_attempt_budget_per_slot=(
                    work.generation_options.attempt_budget
                    * job.attempt_budget_multiplier
                ),
            )
            for work in job.config.work_items
        )
        return GenerationBatchResult(
            format_version=JOB_FORMAT_VERSION,
            batch_seed=job.config.batch.batch_seed,
            resolved_batch_hash=job.config.batch.config_hash,
            source_config_hash=job.config.source_config_hash,
            algorithm_version=ALGORITHM_VERSION,
            items=rows,
            run_status="cancelled",
            batch_seed_source=job.config.batch_seed_source,
            locked_config_snapshot=(
                json.loads(job.config.normalized_config_json)
                if job.config.normalized_config_json
                else None
            ),
            total_work_item_count=len(job.config.work_items),
            total_candidate_target_count=sum(
                work.generation_options.candidate_count
                for work in job.config.work_items
            ),
        )

    def _record_job_progress(
        self, job: JobRecord, value: GenerationBatchResult
    ) -> None:
        """Record only changed work-item observations from service callbacks."""

        for item in value.items:
            error = item.error or {}
            evidence = error.get("evidence")
            if (
                error.get("failure_kind") == "budget_exhausted"
                and item.status not in {"partial", "failed"}
            ):
                # A candidate slot may finish before the work item does. Do
                # not turn every exhausted slot into an identical user-facing
                # warning; report the outcome once at the terminal snapshot.
                continue
            exhausted_slot = (
                dict(evidence)
                if error.get("failure_kind") == "budget_exhausted"
                and isinstance(evidence, Mapping)
                and evidence.get("slot_budget_exhausted") is True
                and item.status in {"partial", "failed"}
                else None
            )
            if exhausted_slot is not None:
                candidate_index = int(exhausted_slot["candidate_index"])
                attempt_budget = int(exhausted_slot["attempt_budget"])
                scope = (
                    f"candidate_budget_exhausted:{item.template_key}:"
                    f"{item.plan_id}:{item.plan_signature}:"
                    f"{attempt_budget}"
                )
                signature: Any = "exhausted"
                message = (
                    f"工作项 {item.template_key}/{item.plan_id} "
                    f"已耗尽当前固定搜索预算"
                )
            else:
                scope = (
                    f"work_item:{item.template_key}:{item.plan_id}:"
                    f"{item.plan_signature}"
                )
                signature = (
                    item.status,
                    len(item.candidates),
                    item.attempts_completed,
                    canonical_json(item.error),
                    canonical_json(item.search_progress),
                )
                message = (
                    f"工作项 {item.template_key}/{item.plan_id} "
                    f"更新为 {item.status}"
                )
                if item.search_progress:
                    progress = item.search_progress
                    signature = (item.status, len(item.candidates),
                                 int(progress.get("attempts_searched", 0)) // 1000,
                                 bool(progress.get("exhausted")), canonical_json(item.error))
                    message = (
                        f"{item.template_key} · 已找到 {len(item.candidates)}/{item.target_candidate_count} 个："
                        f"已搜索 {progress['attempts_searched']} / {progress['attempt_budget']} 次，"
                        "正在筛选和验证"
                    )
                    if progress.get('exhausted'):
                        message = (
                            f"{item.template_key} · 本项 {progress['attempt_budget']} 次搜索已结束，"
                            f"已保留 {len(item.candidates)} 个，还缺 {max(0, item.target_candidate_count - len(item.candidates))} 个"
                        )
            job.add_event(
                "candidate_search_progress" if item.search_progress else "work_item_progress",
                status=item.status,
                message=message,
                details={
                    "template_key": item.template_key,
                    "plan_id": item.plan_id,
                    "plan_signature": item.plan_signature,
                    "candidate_count": len(item.candidates),
                    "attempts_completed": item.attempts_completed,
                    "search_progress": dict(item.search_progress),
                    "error": None if item.error is None else dict(item.error),
                    "candidate_index": (
                        None
                        if exhausted_slot is None
                        else int(exhausted_slot["candidate_index"])
                    ),
                    "attempt_budget": (
                        None
                        if exhausted_slot is None
                        else int(exhausted_slot["attempt_budget"])
                    ),
                    "observed_item_count": len(value.items),
                    "work_item_count": len(job.config.work_items),
                },
                dedupe_scope=scope,
                dedupe_value=signature,
            )

    @staticmethod
    def _completed_work_item_count(value: GenerationBatchResult) -> int:
        """Count terminal rows without treating cancellation placeholders as done."""

        return sum(
            item.status in {"success", "partial", "failed", "cancelled"}
            for item in value.items
        )

    def _set_current_work_item(
        self, job: JobRecord, index: int | None
    ) -> None:
        """Expose the work item currently entering generation.

        The search service publishes at safe work-item boundaries.  Therefore
        the next configured item becomes current immediately after the prior
        result is published, including before the first candidate search.
        """

        if index is None or index >= len(job.config.work_items):
            job.current_work_item = None
            return
        work = job.config.work_items[index]
        current = {
            "index": index + 1,
            "total": len(job.config.work_items),
            "template_key": work.template_key,
            "plan_id": work.plan.plan_id,
            "plan_signature": work.plan.signature,
        }
        if job.current_work_item == current:
            return
        job.current_work_item = current
        job.add_event(
            "work_item_started",
            status="running",
            message=(
                f"开始处理 {work.template_key}/{work.plan.signature}"
            ),
            details=current,
        )

    def _prepare_generation_recommendations(
        self, job_id: str, result: GenerationBatchResult
    ) -> None:
        """Publish pending cards, then verify them in a bounded daemon trial."""

        with self.lock:
            job = self.jobs[job_id]
            work_by_key = {
                (work.template_key, work.plan.plan_id): work
                for work in job.config.work_items
            }
            cards: list[dict[str, Any]] = []
            for item in result.items:
                work = work_by_key.get((item.template_key, item.plan_id))
                if work is None or item.error is None:
                    continue
                try:
                    item_cards = build_generation_parameter_recommendations(
                        job.config_payload,
                        template_key=item.template_key,
                        source_plan_id=work.plan.source_plan_id,
                        tower_mode=work.plan.tower_mode.value,
                        error=item.error,
                    )
                    resolved_suffix = hashlib.sha256(
                        item.plan_id.encode("utf-8")
                    ).hexdigest()[:8]
                    for card in item_cards:
                        card["resolved_plan_id"] = item.plan_id
                        card["recommendation_id"] = (
                            f'{card["recommendation_id"]}_{resolved_suffix}'
                        )
                    cards.extend(item_cards)
                except InputValidationError:
                    # A recommendation is optional assistance; failure to build
                    # one must never hide otherwise valid generation results.
                    continue
            job.parameter_recommendations = cards
            if not cards:
                return
            job.revision += 1
            worker = threading.Thread(
                target=self._run_generation_recommendation_trials,
                args=(job_id,),
                daemon=True,
            )
            job.recommendation_worker = worker
        worker.start()

    def _run_generation_recommendation_trials(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs[job_id]
            recommendation_ids = [
                str(item["recommendation_id"])
                for item in job.parameter_recommendations
            ]
        for recommendation_id in recommendation_ids:
            with self.lock:
                job = self.jobs[job_id]
                card = next(
                    (
                        item
                        for item in job.parameter_recommendations
                        if item.get("recommendation_id") == recommendation_id
                    ),
                    None,
                )
                if card is None:
                    continue
                template_key = str(card["template_key"])
                source_plan_id = str(card["source_plan_id"])
                source = job.sources.get(template_key)
                config_payload = dict(job.config_payload)
                changes = dict(card.get("changes") or {})
                unit_index = card.get("unit_index")
            try:
                adjusted, _differences = apply_generation_parameter_changes(
                    config_payload,
                    template_key=template_key,
                    source_plan_id=source_plan_id,
                    changes=changes,
                    unit_index=unit_index,
                )
                probe_payload = build_generation_probe_request(
                    adjusted,
                    template_key=template_key,
                    source_plan_id=source_plan_id,
                    attempt_budget=24,
                )
                probe_config = resolve_batch_config(
                    loads_batch_request(
                        json.dumps(probe_payload, ensure_ascii=False)
                    )
                )
                if source is None:
                    raise InputValidationError("当前失败模板快照已失效")
                trial = generate_batch(
                    probe_config,
                    self.profile(),
                    loaded_inputs={template_key: source},
                )
                found = sum(len(item.candidates) for item in trial.items)
                verified = found > 0
                trial_result = {
                    "status": "passed" if verified else "not_found",
                    "attempt_budget": 24,
                    "candidate_count": found,
                    "message": (
                        f"小预算试算已找到 {found} 个合格候选。"
                        if verified
                        else "小预算试算未命中，不标记为已验证可生成。"
                    ),
                }
            except Exception as exc:
                verified = False
                trial_result = {
                    "status": "error",
                    "attempt_budget": 24,
                    "candidate_count": 0,
                    "message": "小预算试算未通过。",
                    "error": _error_payload(exc),
                }
            with self.lock:
                job = self.jobs.get(job_id)
                if job is None:
                    return
                current = next(
                    (
                        item
                        for item in job.parameter_recommendations
                        if item.get("recommendation_id") == recommendation_id
                    ),
                    None,
                )
                if current is None:
                    continue
                current["verified"] = verified
                current["trial_result"] = trial_result
                is_unverified_span_opportunity = (
                    current.get("field") == "max_structure_layer_span"
                    and trial_result.get("status") == "not_found"
                )
                current["applicable"] = bool(
                    verified or is_unverified_span_opportunity
                )
                current["trial_verified"] = verified
                current["opportunity_only"] = is_unverified_span_opportunity
                current["action"] = (
                    {
                        "id": "apply_and_continue",
                        "label": "应用该参数并继续运行",
                        "message": (
                            "小预算未命中，仅说明放宽层间距会增加可选结构，"
                            "不代表已证明可生成。"
                            if is_unverified_span_opportunity
                            else "参数建议已通过小预算试算。"
                        ),
                    }
                    if verified or is_unverified_span_opportunity
                    else None
                )
                job.revision += 1
                job.add_event(
                    "parameter_trial",
                    status="passed" if verified else "not_found",
                    message=(
                        "参数建议已通过小预算试算"
                        if verified
                        else "参数建议的小预算试算未命中"
                    ),
                    details={
                        "recommendation_id": recommendation_id,
                        "template_key": template_key,
                        "trial_result": trial_result,
                    },
                )

    @prevent_idle_sleep
    def _run_job(self, job_id: str) -> None:
        run_started_monotonic = time.monotonic()
        with self.lock:
            job = self.jobs[job_id]
            if job.status == "cancelled" or job.control.state == "cancelled":
                # A queued job can be cancelled synchronously before its worker
                # gets CPU time.  Never let that late-starting worker revive it.
                job.worker = None
                return
            # Parent inheritance bootstraps a derived job once. Later budget
            # continuations belong to this job and must use its earned result.
            inherit_parent = bool(job.parent_job_id and job.started_at is None)
            parent = self.jobs.get(job.parent_job_id) if inherit_parent else None
            parent_worker = None if parent is None else parent.worker
            if parent is not None and parent_worker is not None and parent_worker.is_alive():
                job.add_event(
                    "waiting_parent",
                    status="queued",
                    message="等待原任务完成，以便继承已有候选",
                    details={"parent_job_id": parent.job_id},
                )
        while parent_worker is not None and parent_worker.is_alive():
            parent_worker.join(timeout=0.05)
            with self.lock:
                job = self.jobs[job_id]
                if job.status == "cancelled" or job.control.state == "cancelled":
                    job.worker = None
                    return
        with self.lock:
            job = self.jobs[job_id]
            if job.status == "cancelled" or job.control.state == "cancelled":
                job.worker = None
                return
            if inherit_parent:
                parent = self.jobs.get(job.parent_job_id)
                if parent is None or parent.result is None:
                    completed_at = _now()
                    job.error = _error_payload(
                        InputValidationError("原任务没有可继承的候选结果")
                    )
                    job.completed_at = completed_at
                    job.last_progress_at = completed_at
                    job.status = "failed"
                    job.worker = None
                    job.revision += 1
                    job.add_event(
                        "failed",
                        status="failed",
                        message="无法继承原任务结果",
                        details={"error": job.error},
                        at=completed_at,
                    )
                    return
                job.result = self._compatible_parent_result(job, parent.result)
                job.retained_candidate_count = sum(len(item.candidates) for item in job.result.items)
                job.presentation = self._present_result(job, job.result)
                job.progress_completed = self._completed_work_item_count(job.result)
                job.add_event(
                    "parent_result_inherited",
                    status="running",
                    message="已继承原任务候选，仅重跑已调整模板",
                    details={
                        "parent_job_id": parent.job_id,
                        "retained_work_items": len(job.result.items),
                        "retry_scope": (
                            None
                            if job.retry_scope is None
                            else dict(job.retry_scope)
                        ),
                        "retry_scopes": [
                            dict(scope)
                            for scope in (
                                job.retry_scopes
                                or (
                                    ()
                                    if job.retry_scope is None
                                    else (job.retry_scope,)
                                )
                            )
                        ],
                    },
                )
            job.status = "running"
            started_at = _now()
            job.started_at = started_at
            job.last_progress_at = started_at
            job.completed_at = None
            job.error = None
            job.progress_completed = max(job.progress_completed, self._completed_work_item_count(job.result) if job.result else 0)
            job.current_work_item = None
            job.parameter_recommendations = []
            job.revision += 1
            job.add_event(
                "started",
                status="running",
                message="生成任务已开始",
                details={"work_item_count": len(job.config.work_items)},
                at=started_at,
            )
            self._set_current_work_item(job, 0)
        try:
            def progress(value: GenerationBatchResult) -> None:
                with self.lock:
                    job.result = value
                    job.presentation = self._present_result(job, value)
                    job.last_progress_at = _now()
                    # A previously processed partial/failed row becomes running
                    # on continuation; it must not erase the task's baseline.
                    job.progress_completed = max(job.progress_completed, self._completed_work_item_count(value))
                    job.revision += 1
                    self._record_job_progress(job, value)
                    next_index = None
                    if value.run_status == "running":
                        # Snapshots retain later completed rows on continuation;
                        # their last row is not the currently searched item.
                        next_index = next((index for index, row in enumerate(value.items)
                                           if row.status == "running"), None)
                    self._set_current_work_item(job, next_index)

            active_retry_scopes = job.retry_scopes or (
                () if job.retry_scope is None else (job.retry_scope,)
            )
            retry_work_item_keys = tuple(
                (work.template_key, work.plan.plan_id, work.plan.signature)
                for work in job.config.work_items
                if any(
                    _retry_scope_matches(
                        scope,
                        template_key=work.template_key,
                        plan_id=work.plan.plan_id,
                        source_plan_id=work.plan.source_plan_id,
                    )
                    for scope in active_retry_scopes
                )
            )
            result = generate_batch(
                job.config,
                self.profile(),
                loaded_inputs=job.sources,
                control=job.control,
                previous_result=job.result,
                retry_statuses=job.retry_statuses,
                retry_work_item_keys=retry_work_item_keys,
                excluded_retry_failure_kinds=job.excluded_retry_failure_kinds,
                progress_callback=progress,
                attempt_budget_multiplier=job.attempt_budget_multiplier,
            )
            with self.lock:
                finish_partial_requested = job.finish_partial_requested
            if (
                finish_partial_requested
                and result.run_status != "completed"
                and any(item.candidates for item in result.items)
            ):
                # Preserve the explicit "use what has been found" intent even
                # if a lower-level cancellation path labelled a retained row
                # as cancelled.  Export still validates every selected ID.
                result = replace(
                    result,
                    items=tuple(
                        replace(item, status="partial")
                        if item.candidates
                        and item.status not in {"success", "partial"}
                        else item
                        for item in result.items
                    ),
                    run_status="partial",
                )
            presentation = self._present_result(job, result)
            # Publish recommendation cards before exposing the terminal job
            # state.  A client that observes completion therefore never misses
            # the cards; recommendations_pending tells it whether bounded
            # verification trials still need polling.
            try:
                self._prepare_generation_recommendations(job_id, result)
            except Exception as recommendation_error:
                with self.lock:
                    job.add_event(
                        "parameter_recommendation_unavailable",
                        status="unavailable",
                        message="未能生成参数调整建议",
                        details={"error": _error_payload(recommendation_error)},
                    )
            with self.lock:
                completed_at = _now()
                run_elapsed = max(0.0, time.monotonic() - run_started_monotonic)
                job.last_run_elapsed_seconds = run_elapsed
                job.cumulative_elapsed_seconds += run_elapsed
                job.result = result
                job.progress_completed = max(job.progress_completed, self._completed_work_item_count(result))
                self._record_job_progress(job, result)
                self._set_current_work_item(job, None)
                job.presentation = presentation
                job.completed_at = completed_at
                job.last_progress_at = completed_at
                job.revision += 1
                result_summary = result.as_dict()["summary"]
                job.add_event(
                    result.run_status,
                    status=result.run_status,
                    message={
                        "completed": "生成任务已完成",
                        "partial": "生成任务部分完成",
                        "failed": "生成任务失败",
                        "cancelled": "生成任务已取消",
                    }.get(result.run_status, f"生成任务结束：{result.run_status}"),
                    details=result_summary,
                    at=completed_at,
                )
                # Publish the terminal status last.  Direct observers that see
                # it can therefore also rely on the final event and timestamps
                # already being present.
                job.worker = None
                job.status = result.run_status
        except Exception as exc:  # task state must remain inspectable
            with self.lock:
                completed_at = _now()
                run_elapsed = max(0.0, time.monotonic() - run_started_monotonic)
                job.last_run_elapsed_seconds = run_elapsed
                job.cumulative_elapsed_seconds += run_elapsed
                job.error = _error_payload(exc)
                job.current_work_item = None
                job.completed_at = completed_at
                job.last_progress_at = completed_at
                job.revision += 1
                job.add_event(
                    "failed",
                    status="failed",
                    message="生成任务异常终止",
                    details={"error": job.error},
                    at=completed_at,
                )
                job.worker = None
                job.status = "failed"

    def control_job(
        self,
        job_id: str,
        action: str,
        options: Mapping[str, Any] | None = None,
    ) -> JobRecord:
        job = self.get_job(job_id)
        normalized = str(action).strip().lower()
        control_options = dict(options or {})
        with self.lock:
            if normalized == "pause":
                accepted = job.control.request_pause()
                if accepted:
                    job.status = "pause_requested"
                    job.add_event(
                        "pause",
                        status="pause_requested",
                        message="生成任务已接受暂停请求",
                        details={"control_state": job.control.state},
                    )
            elif normalized == "resume":
                accepted = job.control.resume()
                if accepted:
                    job.status = "running"
                    job.add_event(
                        "resume",
                        status="running",
                        message="生成任务已恢复",
                        details={"control_state": job.control.state},
                    )
            elif normalized in {"cancel", "finish_partial"}:
                queued_without_candidates = (
                    job.status == "queued"
                    and job.started_at is None
                    and not (
                        job.result
                        and any(item.candidates for item in job.result.items)
                    )
                )
                accepted = job.control.request_cancel()
                if accepted:
                    job.finish_partial_requested = normalized == "finish_partial"
                    job.status = "cancel_requested"
                    job.add_event(
                        "cancel",
                        status="cancel_requested",
                        message=(
                            "将在安全检查点结束，并保留已找到候选"
                            if normalized == "finish_partial"
                            else "生成任务已接受取消请求"
                        ),
                        details={
                            "control_state": job.control.state,
                            "retain_partial_results": normalized == "finish_partial",
                        },
                    )
                    if queued_without_candidates:
                        try:
                            job.control.checkpoint("queued_cancel")
                        except OperationCancelledError as exc:
                            result = self._queued_cancel_result(job, exc)
                        else:  # pragma: no cover - request_cancel guarantees raise
                            raise RuntimeError("queued cancellation checkpoint did not stop")
                        completed_at = _now()
                        job.result = result
                        job.presentation = self._present_result(job, result)
                        job.progress_completed = self._completed_work_item_count(result)
                        job.current_work_item = None
                        job.completed_at = completed_at
                        job.last_progress_at = completed_at
                        job.error = None
                        job.revision += 1
                        job.add_event(
                            "cancelled",
                            status="cancelled",
                            message="生成任务已取消",
                            details=result.as_dict()["summary"],
                            at=completed_at,
                        )
                        job.worker = None
                        # Publish the terminal status last so direct observers
                        # also see its result, timestamps and terminal event.
                        job.status = "cancelled"
            elif normalized in {"retry", "continue_search", "resume_unfinished"}:
                requested_action = normalized
                if job.status not in {"partial", "failed", "cancelled"}:
                    raise InputValidationError("当前任务状态不允许续跑未完成项")
                if job.worker is not None and job.worker.is_alive():
                    raise InputValidationError("旧任务线程尚未结束，暂不能续跑")
                resumable_items = (
                    []
                    if job.result is None
                    else [
                        item
                        for item in job.result.items
                        if item.status in {"failed", "partial", "cancelled", "pending"}
                        and (
                            not item.error
                            or (
                                item.error.get("failure_kind")
                                != "parameter_conflict"
                                and not (
                                    item.error.get("failure_kind")
                                    == "budget_exhausted"
                                    and job.attempt_budget_multiplier
                                    >= MAX_SEARCH_BUDGET_MULTIPLIER
                                )
                            )
                        )
                    ]
                )
                if job.result is not None and not resumable_items:
                    if any(
                        item.error
                        and item.error.get("failure_kind") == "budget_exhausted"
                        for item in job.result.items
                    ) and (
                        job.attempt_budget_multiplier
                        >= MAX_SEARCH_BUDGET_MULTIPLIER
                    ):
                        raise InputValidationError(
                            "已达本次搜索上限，请应用参数建议或手动调整当前模板"
                        )
                    raise InputValidationError(
                        "当前未完成项均需调整参数，不能用原参数续跑"
                    )
                has_budget_exhaustion = any(
                    item.error
                    and item.error.get("failure_kind") == "budget_exhausted"
                    for item in resumable_items
                )
                raw_multiplier = control_options.get(
                    "attempt_budget_multiplier",
                    control_options.get(
                        "search_budget_multiplier",
                        control_options.get("budget_multiplier"),
                    ),
                )
                if raw_multiplier is None and "attempt_budget_per_slot" in control_options:
                    raw_budget = control_options.get("attempt_budget_per_slot")
                    if isinstance(raw_budget, bool) or not isinstance(raw_budget, int):
                        raise InputValidationError(
                            "attempt_budget_per_slot 必须是正整数"
                        )
                    bases = {
                        int(work.generation_options.attempt_budget)
                        for work in job.config.work_items
                        if any(
                            item.template_key == work.template_key
                            and item.plan_id == work.plan.plan_id
                            for item in resumable_items
                        )
                    }
                    if len(bases) != 1:
                        raise InputValidationError(
                            "多个未完成项的基础预算不同，请使用 attempt_budget_multiplier",
                            evidence={"base_attempt_budgets": sorted(bases)},
                        )
                    base_budget = next(iter(bases))
                    raw_multiplier = math.ceil(raw_budget / base_budget)
                if raw_multiplier is None:
                    next_multiplier = (
                        min(
                            MAX_SEARCH_BUDGET_MULTIPLIER,
                            max(2, job.attempt_budget_multiplier * 2),
                        )
                        if has_budget_exhaustion
                        else job.attempt_budget_multiplier
                    )
                else:
                    if (
                        isinstance(raw_multiplier, bool)
                        or not isinstance(raw_multiplier, int)
                        or raw_multiplier < 1
                        or raw_multiplier > MAX_SEARCH_BUDGET_MULTIPLIER
                    ):
                        raise InputValidationError(
                            "attempt_budget_multiplier 必须在 1–16 之间"
                        )
                    next_multiplier = raw_multiplier
                if (
                    has_budget_exhaustion
                    and job.attempt_budget_multiplier >= MAX_SEARCH_BUDGET_MULTIPLIER
                ):
                    raise InputValidationError(
                        "已达本次搜索上限，请应用参数建议或手动调整当前模板"
                    )
                if has_budget_exhaustion and (
                    next_multiplier <= job.attempt_budget_multiplier
                ):
                    raise InputValidationError(
                        "预算已耗尽，续跑时的预算档位必须高于当前档位",
                        evidence={
                            "current_attempt_budget_multiplier": job.attempt_budget_multiplier,
                            "maximum_attempt_budget_multiplier": MAX_SEARCH_BUDGET_MULTIPLIER,
                        },
                    )
                job.control = CooperativeTaskControl()
                job.status = "queued"
                job.completed_at = None
                job.last_progress_at = None
                job.retained_candidate_count = sum(
                    len(item.candidates) for item in job.result.items
                ) if job.result else 0
                job.error = None
                job.presentation = None
                job.progress_completed = max(job.progress_completed, self._completed_work_item_count(job.result) if job.result else 0)
                job.current_work_item = None
                job.retry_statuses = ("failed", "partial", "cancelled", "pending")
                job.excluded_retry_failure_kinds = (
                    ("parameter_conflict", "budget_exhausted")
                    if job.attempt_budget_multiplier
                    >= MAX_SEARCH_BUDGET_MULTIPLIER
                    else ("parameter_conflict",)
                )
                job.finish_partial_requested = False
                job.attempt_budget_multiplier = next_multiplier
                accepted = True
                job.add_event(
                    requested_action,
                    status="queued",
                    message="已保留现有候选，仅续跑未完成候选槽",
                    details={
                        "canonical_action": "resume_unfinished",
                        "requested_action": requested_action,
                        "attempt_budget_multiplier": job.attempt_budget_multiplier,
                        "previous_result_retained": job.result is not None,
                    },
                )
                worker = threading.Thread(
                    target=self._run_job,
                    args=(job.job_id,),
                    daemon=True,
                )
                job.worker = worker
                worker.start()
            else:
                raise InputValidationError("不支持的任务控制动作")
            if not accepted:
                raise InputValidationError("当前任务状态不允许该操作")
            job.revision += 1
        return job

    def apply_generation_recommendation(
        self, job_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Apply one supported card to a returned draft, scoped to one template.

        Most cards still require a passed bounded trial.  Relaxing
        ``max_structure_layer_span`` is the one deliberate exception: a
        ``not_found`` trial can be applied as an explicitly unverified
        opportunity because it monotonically widens the legal layout set.
        """

        job = self.get_job(job_id)
        recommendation_id = str(payload.get("recommendation_id", "")).strip()
        with self.lock:
            card = next(
                (
                    item
                    for item in job.parameter_recommendations
                    if item.get("recommendation_id") == recommendation_id
                ),
                None,
            )
            if card is None:
                raise InputValidationError("参数建议不存在或已失效")
            trial_result = dict(card.get("trial_result") or {})
            trial_status = str(trial_result.get("status", ""))
            unverified_span_opportunity = (
                card.get("field") == "max_structure_layer_span"
                and card.get("verified") is not True
                and trial_status == "not_found"
            )
            if (
                not unverified_span_opportunity
                and (card.get("verified") is not True or trial_status != "passed")
            ):
                messages = {
                    "pending": "参数建议仍在小预算试算中，完成前不能一键应用",
                    "not_found": "小预算试算未找到合格候选，该建议不能一键应用",
                    "error": "参数建议试算未通过，该建议不能一键应用",
                }
                raise InputValidationError(
                    messages.get(
                        trial_status,
                        "参数建议尚未验证可生成，不能一键应用",
                    ),
                    evidence={
                        "recommendation_id": recommendation_id,
                        "verified": bool(card.get("verified")),
                        "trial_status": trial_status or "unknown",
                    },
                )
            template_key = str(card["template_key"])
            source_plan_id = str(card["source_plan_id"])
            changes = dict(card.get("changes") or {})
            unit_index = card.get("unit_index")
            before_request = copy.deepcopy(job.config_payload)
        after_request, differences = apply_generation_parameter_changes(
            before_request,
            template_key=template_key,
            source_plan_id=source_plan_id,
            changes=changes,
            unit_index=unit_index,
        )
        # Resolve before returning so the browser never receives an invalid
        # "applied" draft.
        resolve_batch_config(
            loads_batch_request(json.dumps(after_request, ensure_ascii=False))
        )
        undo_token = f"undo_{uuid.uuid4().hex}"
        undo_record = {
            "job_id": job_id,
            "recommendation_id": recommendation_id,
            "template_key": template_key,
            "before_request": before_request,
            "after_request": after_request,
            "differences": differences,
        }
        with self.lock:
            self.generation_recommendation_undos[undo_token] = undo_record
            # Keep this local, transient undo registry bounded.
            while len(self.generation_recommendation_undos) > 100:
                self.generation_recommendation_undos.pop(
                    next(iter(self.generation_recommendation_undos))
                )
        return {
            "scope": "current_failed_template",
            "import_id": job.import_id,
            "template_key": template_key,
            "recommendation_id": recommendation_id,
            "batch_request": _browser_batch_request(after_request),
            "differences": differences,
            "undo_token": undo_token,
            "verified": bool(card.get("verified")),
            "trial_verified": bool(card.get("verified")),
            "applicable": True,
            "opportunity_only": unverified_span_opportunity,
            "application_semantics": (
                "unverified_opportunity"
                if unverified_span_opportunity
                else "verified_change"
            ),
            "message": (
                "已应用层间距调整；该变更仅提高命中机会，尚未证明可生成。"
                if unverified_span_opportunity
                else "已应用通过小预算试算的参数调整。"
            ),
            "continue_request": {
                "method": "POST",
                "path": "/api/v1/jobs",
                "body": {
                    "import_id": job.import_id,
                    "batch_request": _browser_batch_request(after_request),
                    "parent_job_id": job.job_id,
                    "retry_scope": {
                        "template_key": template_key,
                        "plan_id": source_plan_id,
                    },
                    "retry_scopes": [
                        {
                            "template_key": template_key,
                            "plan_id": source_plan_id,
                        }
                    ],
                    "parameter_overrides": [
                        {
                            "template_key": template_key,
                            "plan_id": source_plan_id,
                            "changes": copy.deepcopy(changes),
                            **(
                                {"unit_index": unit_index}
                                if unit_index is not None
                                else {}
                            ),
                        }
                    ],
                    "attempt_budget_multiplier": job.attempt_budget_multiplier,
                },
            },
            "trial_result": dict(card.get("trial_result") or {}),
        }

    def undo_generation_recommendation(
        self, job_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        undo_token = str(payload.get("undo_token", "")).strip()
        with self.lock:
            record = self.generation_recommendation_undos.get(undo_token)
            if record is None or record.get("job_id") != job_id:
                raise InputValidationError("撤销记录不存在或已失效")
            self.generation_recommendation_undos.pop(undo_token, None)
        return {
            "scope": "current_failed_template",
            "template_key": record["template_key"],
            "recommendation_id": record["recommendation_id"],
            "batch_request": _browser_batch_request(record["before_request"]),
            "differences": [
                {
                    **difference,
                    "before": difference["after"],
                    "after": difference["before"],
                }
                for difference in record["differences"]
            ],
            "undone": True,
        }

    def _present_result(
        self, job: JobRecord, result: GenerationBatchResult
    ) -> dict[str, Any]:
        work_by_key = {
            (work.template_key, work.plan.plan_id, work.plan.signature): work
            for work in job.config.work_items
        }
        work_by_resolved_id: dict[tuple[str, str], list[Any]] = {}
        for work in job.config.work_items:
            work_by_resolved_id.setdefault(
                (work.template_key, work.plan.plan_id), []
            ).append(work)
        items: list[dict[str, Any]] = []
        for item in result.items:
            payload = item.as_dict()
            work = work_by_key.get(
                (item.template_key, item.plan_id, item.plan_signature)
            )
            if work is None:
                matches = work_by_resolved_id.get(
                    (item.template_key, item.plan_id), []
                )
                work = matches[0] if len(matches) == 1 else None
            payload["source_plan_id"] = (
                None if work is None else work.plan.source_plan_id
            )
            error = payload.get("error")
            failure_kind = (
                str(error.get("failure_kind") or "")
                if isinstance(error, Mapping)
                else ""
            )
            incomplete = item.status in {
                "failed",
                "partial",
                "cancelled",
                "pending",
            }
            blocked_reason = None
            if failure_kind == "parameter_conflict":
                blocked_reason = "parameter_conflict"
            elif (
                failure_kind == "budget_exhausted"
                and job.attempt_budget_multiplier
                >= MAX_SEARCH_BUDGET_MULTIPLIER
            ):
                blocked_reason = "search_budget_limit_reached"
            payload["resume"] = {
                "resumable": incomplete and blocked_reason is None,
                "blocked_reason": blocked_reason,
            }
            if (
                job.attempt_budget_multiplier >= MAX_SEARCH_BUDGET_MULTIPLIER
                and isinstance(error, Mapping)
                and error.get("failure_kind") == "budget_exhausted"
            ):
                payload["error"] = {
                    **dict(error),
                    "search_limit_reached": True,
                    "recommended_action": {
                        "id": "adjust_parameters",
                        "label": "调整当前模板参数",
                        "message": "已达本次搜索上限，建议调整参数后再生成。",
                    },
                }
            serialized_candidates = list(payload.get("candidates") or [])
            payload["candidates"] = []
            if item.source is not None:
                for index, generated in enumerate(item.candidates):
                    candidate_payload = (
                        dict(serialized_candidates[index])
                        if index < len(serialized_candidates)
                        else generated.as_dict()
                    )
                    candidate_payload["board"] = build_candidate_board(
                        item.source, generated
                    )
                    payload["candidates"].append(candidate_payload)
                payload["comparison"] = build_candidate_comparison(
                    item.source, item.candidates
                )
            items.append(payload)
        base = result.as_dict()
        # Preserve the complete seed in browser state for copy/lock/replay.
        base["batch_seed"] = str(result.batch_seed)
        base["items"] = items
        return base

    def job_source_preview(self, job_id: str, template_key: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        source = job.sources.get(template_key)
        if source is None:
            raise InputValidationError("该模板不属于当前任务的原模板快照")
        return {
            "template_key": template_key,
            "inspection": inspect_loaded_template(source).as_dict(),
            "board": build_source_board(source),
        }

    def get_job(self, job_id: str) -> JobRecord:
        with self.lock:
            job = self.jobs.get(job_id)
        if job is None:
            raise InputValidationError("任务不存在或本地服务已重启")
        return job

    def export_job(self, job_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job.result is None or job.status not in {"completed", "partial"}:
            raise InputValidationError("候选生成尚未完成")
        selection_value = {
            "schema_version": 1,
            "selections": payload.get("selections"),
        }
        choices = parse_selection_payload(selection_value)
        # Recreate candidates from the locked config and in-memory source bytes;
        # the browser never supplies trusted replacements or validation objects.
        regenerated = generate_batch(
            job.config,
            self.profile(),
            loaded_inputs=job.sources,
            previous_result=job.result,
            retry_statuses=(),
        )
        selections = resolve_export_selections(job.config, regenerated, choices)
        key = str(payload.get("idempotency_key") or new_idempotency_key())
        result = export_selected(
            ExportRequest(
                profile=self.profile(),
                selections=selections,
                idempotency_key=key,
                allow_duplicate_as_new_version=bool(
                    payload.get("allow_duplicate_as_new_version", False)
                ),
            )
        )
        result_payload = result.as_dict()
        result_payload["selection_summary"] = summarize_candidate_choices(
            choices
        )
        manifests = [
            load_export_manifest(self.profile(), item["output_template_id"])
            for item in result_payload["outputs"]
        ]
        with self.lock:
            job.export_result = result_payload
            job.exported_manifests = manifests
        return result_payload

    def create_export_run(
        self, job_id: str, payload: Mapping[str, Any]
    ) -> ExportRunRecord:
        job = self.get_job(job_id)
        if job.result is None or job.status not in {"completed", "partial"}:
            raise InputValidationError("候选生成尚未完成")
        # Validate the untrusted selection shape before accepting background
        # work; candidate content is still reconstructed from backend state.
        choices = parse_selection_payload(
            {"schema_version": 1, "selections": payload.get("selections")}
        )
        profile = self.profile()
        record = ExportRunRecord(
            export_id=f"export_{uuid.uuid4().hex}",
            job_id=job_id,
            created_at=_now(),
            idempotency_key=str(
                payload.get("idempotency_key") or new_idempotency_key()
            ),
            payload=dict(payload),
            output_directory=str(Path(profile.output_root).resolve()),
            profile_snapshot=profile,
            locked_selections=choices,
            progress_total=len(choices),
        )
        with self.lock:
            self.exports[record.export_id] = record
            worker = threading.Thread(
                target=self._run_export,
                args=(record.export_id,),
                daemon=True,
            )
            record.worker = worker
        worker.start()
        return record

    def _record_export_rebuild_progress(
        self, record: ExportRunRecord, job: JobRecord, value: GenerationBatchResult
    ) -> None:
        last_item = value.items[-1] if value.items else None
        signature = (
            value.run_status,
            len(value.items),
            None if last_item is None else last_item.template_key,
            None if last_item is None else last_item.plan_id,
            None if last_item is None else last_item.status,
            None if last_item is None else len(last_item.candidates),
            None if last_item is None else last_item.attempts_completed,
        )
        record.add_event(
            "rebuild_progress",
            status=value.run_status,
            message=(
                "导出前候选重建进度已更新"
                if last_item is None
                else f"已重建 {last_item.template_key}/{last_item.plan_id}"
            ),
            details={
                "completed_work_items": len(value.items),
                "work_item_count": len(job.config.work_items),
                "template_key": None if last_item is None else last_item.template_key,
                "plan_id": None if last_item is None else last_item.plan_id,
                "item_status": None if last_item is None else last_item.status,
                "candidate_count": 0
                if last_item is None
                else len(last_item.candidates),
                "attempts_completed": 0
                if last_item is None
                else last_item.attempts_completed,
            },
            dedupe_scope="rebuild_progress",
            dedupe_value=signature,
        )

    def _run_export(self, export_id: str) -> None:
        with self.lock:
            record = self.exports[export_id]
            job = self.jobs[record.job_id]
            export_profile = record.profile_snapshot
            if export_profile is None:
                export_profile = self.profile()
                if record.output_directory:
                    export_profile = replace(
                        export_profile,
                        output_root=record.output_directory,
                    )
                record.profile_snapshot = export_profile
            record.status = "running"
            started_at = _now()
            record.started_at = started_at
            record.last_progress_at = started_at
            record.completed_at = None
            record.error = None
            record.result = None
            record.exported_manifests = []
            record.parameter_configured_at = None
            record.progress_completed = 0
            record.progress_item = None
            record.revision += 1
            record.add_event(
                "started",
                status="running",
                message="导出任务已开始，正在重建锁定候选",
                details={
                    "selection_count": len(record.locked_selections),
                    "phase": "rebuild",
                },
                at=started_at,
            )
        try:
            choices = record.locked_selections

            def rebuild_progress(value: GenerationBatchResult) -> None:
                with self.lock:
                    record.last_progress_at = _now()
                    record.revision += 1
                    self._record_export_rebuild_progress(record, job, value)

            regenerated = generate_batch(
                job.config,
                export_profile,
                loaded_inputs=job.sources,
                control=record.control,
                previous_result=job.result,
                retry_statuses=(),
                mark_control_completed=False,
                progress_callback=rebuild_progress,
            )
            if regenerated.run_status == "cancelled":
                raise OperationCancelledError("导出前候选重建已取消")
            with self.lock:
                record.add_event(
                    "rebuild_completed",
                    status=regenerated.run_status,
                    message="导出前候选重建已完成",
                    details=regenerated.as_dict()["summary"],
                )
            selections = resolve_export_selections(
                job.config,
                regenerated,
                choices,
            )

            def export_progress(completed: int, total: int, current_item: str) -> None:
                with self.lock:
                    record.progress_completed = completed
                    record.progress_total = total
                    record.progress_item = current_item
                    record.last_progress_at = _now()
                    record.revision += 1
                    signature = (completed, total, current_item)
                    record.add_event(
                        "candidate_export_progress",
                        status="running",
                        message=f"候选导出进度 {completed}/{total}",
                        details={
                            "completed": completed,
                            "total": total,
                            "candidate_id": current_item,
                        },
                        dedupe_scope="candidate_export_progress",
                        dedupe_value=signature,
                    )

            result = export_selected(
                ExportRequest(
                    profile=export_profile,
                    selections=selections,
                    idempotency_key=record.idempotency_key,
                    allow_duplicate_as_new_version=bool(
                        record.payload.get(
                            "allow_duplicate_as_new_version",
                            False,
                        )
                    ),
                ),
                task_control=record.control,
                progress_callback=export_progress,
            )
            result_payload = result.as_dict()
            result_payload["selection_summary"] = summarize_candidate_choices(
                choices
            )
            with self.lock:
                record.add_event(
                    "commit",
                    status=str(result_payload.get("state", "completed")),
                    message="导出事务已提交",
                    details={
                        "transaction_state": result_payload.get("state"),
                        "batch_id": result_payload.get("batch_id"),
                        "output_count": len(result_payload.get("outputs", [])),
                        "recovered": bool(result_payload.get("recovered", False)),
                    },
                )
            manifests = [
                load_export_manifest(
                    export_profile, item["output_template_id"]
                )
                for item in result_payload["outputs"]
            ]
            with self.lock:
                completed_at = _now()
                record.result = result_payload
                record.status = "completed"
                record.completed_at = completed_at
                record.last_progress_at = completed_at
                record.progress_completed = record.progress_total
                record.progress_item = None
                record.revision += 1
                record.exported_manifests = copy.deepcopy(manifests)
                job.export_result = result_payload
                # Retain the legacy latest-export cache for old readers.  New
                # parameter configuration paths resolve the exact export run.
                job.exported_manifests = copy.deepcopy(manifests)
                record.add_event(
                    "completed",
                    status="completed",
                    message="导出任务已完成",
                    details={
                        "output_count": len(result_payload.get("outputs", [])),
                        "batch_id": result_payload.get("batch_id"),
                    },
                    at=completed_at,
                )
        except OperationCancelledError as exc:
            with self.lock:
                completed_at = _now()
                record.status = "cancelled"
                record.error = _error_payload(exc)
                record.completed_at = completed_at
                record.last_progress_at = completed_at
                record.revision += 1
                record.add_event(
                    "cancelled",
                    status="cancelled",
                    message="导出任务已取消",
                    details={"error": record.error},
                    at=completed_at,
                )
        except Exception as exc:
            with self.lock:
                completed_at = _now()
                record.status = "failed"
                record.error = _error_payload(exc)
                record.completed_at = completed_at
                record.last_progress_at = completed_at
                record.revision += 1
                control = record.control.snapshot()
                record.add_event(
                    "failed",
                    status="failed",
                    message="导出任务失败",
                    details={
                        "error": record.error,
                        "control_state": control.state,
                        "phase": control.phase,
                    },
                    at=completed_at,
                )

    def get_export_run(self, export_id: str) -> ExportRunRecord:
        with self.lock:
            record = self.exports.get(export_id)
        if record is None:
            raise InputValidationError("导出任务不存在或本地服务已重启")
        return record

    def open_export_output_directory(self, export_id: str) -> dict[str, str]:
        record = self.get_export_run(export_id)
        with self.lock:
            if record.status != "completed" or record.result is None:
                raise InputValidationError("导出完成后才能打开输出目录")
            output_directory = str(record.output_directory).strip()
        if not output_directory:
            raise InputValidationError("该导出记录没有可用的输出目录")
        opened = open_directory(Path(output_directory))
        return {"export_id": record.export_id, "path": str(opened)}

    def control_export(self, export_id: str, action: str) -> ExportRunRecord:
        record = self.get_export_run(export_id)
        normalized = str(action).strip().lower()
        with self.lock:
            if normalized == "pause":
                accepted = record.control.request_pause()
                if accepted:
                    record.status = "pause_requested"
                    record.add_event(
                        "pause",
                        status="pause_requested",
                        message="导出任务已接受暂停请求",
                        details={"control_state": record.control.state},
                    )
            elif normalized == "resume":
                accepted = record.control.resume()
                if accepted:
                    record.status = "running"
                    record.add_event(
                        "resume",
                        status="running",
                        message="导出任务已恢复",
                        details={"control_state": record.control.state},
                    )
            elif normalized == "cancel":
                accepted = record.control.request_cancel()
                if accepted:
                    record.status = "cancel_requested"
                    record.add_event(
                        "cancel",
                        status="cancel_requested",
                        message="导出任务已接受取消请求",
                        details={"control_state": record.control.state},
                    )
            elif normalized == "retry":
                if record.status not in {"failed", "cancelled"}:
                    raise InputValidationError("当前导出状态不允许重试")
                if record.worker is not None and record.worker.is_alive():
                    raise InputValidationError("旧导出线程尚未结束，暂不能重试")
                record.control = CooperativeTaskControl()
                record.status = "queued"
                record.completed_at = None
                record.last_progress_at = None
                record.error = None
                accepted = True
                record.add_event(
                    "retry",
                    status="queued",
                    message="导出任务已接受重试",
                    details={"idempotency_key": record.idempotency_key},
                )
                record.add_event(
                    "queued",
                    status="queued",
                    message="导出重试已排队",
                    details={"selection_count": len(record.locked_selections)},
                )
                worker = threading.Thread(
                    target=self._run_export,
                    args=(record.export_id,),
                    daemon=True,
                )
                record.worker = worker
                worker.start()
            else:
                raise InputValidationError("不支持的导出控制动作")
            if not accepted:
                if record.control.state == "committing":
                    raise InputValidationError(
                        "导出已进入事务提交阶段，不能暂停或取消"
                    )
                raise InputValidationError("当前导出状态不允许该操作")
            record.revision += 1
        return record

    def history(self) -> list[dict[str, Any]]:
        with self.lock:
            jobs = list(self.jobs.values())
        return [job.summary() for job in reversed(jobs[-100:])]

    def export_history(self) -> list[dict[str, Any]]:
        with self.lock:
            exports = list(self.exports.values())
        return [record.summary() for record in reversed(exports[-100:])]

    def resolve_parameter_templates(
        self, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        source = str(payload.get("source", "output_ids"))
        manifests: list[dict[str, Any]] = []
        source_export_id: str | None = None
        source_job_id: str | None = None
        parameter_configured_at: str | None = None
        if source == "recent_export":
            requested_export_id = str(payload.get("export_id", "")).strip()
            requested_job_id = str(payload.get("job_id", "")).strip()
            export: ExportRunRecord | None = None
            if requested_export_id:
                export = self.get_export_run(requested_export_id)
                if requested_job_id and requested_job_id != export.job_id:
                    raise InputValidationError("export_id 不属于指定任务")
            elif requested_job_id:
                # Backward compatibility for the previous browser contract.
                # Never guess "the latest" when several immutable exports
                # belong to the same job: the caller must pin an export_id.
                with self.lock:
                    matching_exports = [
                        item
                        for item in self.exports.values()
                        if item.job_id == requested_job_id
                        and item.status == "completed"
                        and item.exported_manifests
                    ]
                if len(matching_exports) == 1:
                    export = matching_exports[0]
                elif len(matching_exports) > 1:
                    raise InputValidationError(
                        "该任务有多条已完成导出，请刷新页面后按 export_id 重新选择"
                    )
                else:
                    # Historical synchronous exports predate ExportRunRecord.
                    # They remain readable when the job cache is unambiguous,
                    # but cannot participate in export-bound completion state.
                    job = self.get_job(requested_job_id)
                    if not job.exported_manifests:
                        raise InputValidationError("该任务尚未完成验证导出")
                    manifests = copy.deepcopy(job.exported_manifests)
                    source_job_id = job.job_id
            else:
                raise InputValidationError(
                    "请指定要配置参数的导出记录 export_id"
                )
            if export is not None:
                if export.status != "completed" or not export.exported_manifests:
                    raise InputValidationError("该导出记录尚未完成验证导出")
                source_export_id = export.export_id
                source_job_id = export.job_id
                manifests = copy.deepcopy(export.exported_manifests)
                parameter_configured_at = export.parameter_configured_at
        elif source == "output_ids":
            values = payload.get("template_ids")
            if not isinstance(values, list) or not values:
                raise InputValidationError("请至少输入一个已导出的 aipuzzle 模板 ID")
            manifests = [
                load_export_manifest(self.profile(), str(value).strip()) for value in values
            ]
        elif source == "files":
            values = payload.get("files")
            if not isinstance(values, list) or not values:
                raise InputValidationError("请至少上传一个已导出的 _m.bytes")
            for raw in values:
                if not isinstance(raw, Mapping):
                    raise InputValidationError("上传文件项格式错误")
                name, data = _decode_file(raw)
                manifests.append(load_uploaded_export_manifest(self.profile(), name, data))
        else:
            raise InputValidationError("未知的新模板导入方式")
        selection_id = f"params_{uuid.uuid4().hex}"
        with self.lock:
            self.parameter_selections[selection_id] = {
                "manifests": copy.deepcopy(manifests),
                "source_export_id": source_export_id,
                "source_job_id": source_job_id,
            }
        return {
            "selection_id": selection_id,
            "source_export_id": source_export_id,
            "source_job_id": source_job_id,
            "parameter_configured": parameter_configured_at is not None,
            "parameter_configured_at": parameter_configured_at,
            "items": [
                {
                    "source_template_id": item["source_template_id"],
                    "output_template_id": item["output_template_id"],
                    "signature": item.get("signature"),
                    "replacement_count": len(item.get("replacements", [])),
                }
                for item in manifests
            ],
        }

    def calculate_parameters(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        selection_id = str(payload.get("selection_id", ""))
        with self.lock:
            selection = self.parameter_selections.get(selection_id)
        if selection is None:
            raise InputValidationError("新模板选择已失效，请重新导入")
        manifests = selection["manifests"]
        source_export_id = selection.get("source_export_id")
        source_job_id = selection.get("source_job_id")
        result = build_parameter_recommendations(
            self.profile(),
            manifests,
            config_set_id=str(payload.get("config_set_id", "")),
            level_start=_required_decimal_int(payload, "level_start", minimum=1),
            level_end=_required_decimal_int(payload, "level_end", minimum=1),
            strategy=str(payload.get("strategy", "fidelity")),
        )
        parameter_configured_at: str | None = None
        if source_export_id is not None:
            with self.lock:
                export = self.exports.get(str(source_export_id))
                if export is None:
                    raise InputValidationError("导出记录已失效，请重新选择")
                if source_job_id and export.job_id != source_job_id:
                    raise InputValidationError("导出记录与原任务绑定已失效，请重新选择")
                if export.status != "completed":
                    raise InputValidationError("导出记录状态已变化，请重新选择")
                if export.parameter_configured_at is None:
                    export.parameter_configured_at = _now()
                    export.revision += 1
                    export.add_event(
                        "parameter_configured",
                        status="completed",
                        message="新模板参数已完成计算",
                        details={
                            "selection_id": selection_id,
                            "config_set_id": str(payload.get("config_set_id", "")),
                            "strategy": str(payload.get("strategy", "fidelity")),
                        },
                        at=export.parameter_configured_at,
                    )
                parameter_configured_at = export.parameter_configured_at
        return {
            **result,
            "selection_id": selection_id,
            "source_export_id": source_export_id,
            "source_job_id": source_job_id,
            "parameter_configured": parameter_configured_at is not None,
            "parameter_configured_at": parameter_configured_at,
        }

    def calibrate_consumption_parameters(
        self, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Build a target-consumption DupPara review table."""

        profile, manifests, kwargs, result_context = (
            self._prepare_consumption_calibration(payload)
        )
        return self._bind_calibration_context(profile, {
            **build_consumption_calibration(profile, manifests, **kwargs),
            **result_context,
        })

    def _bind_calibration_context(
        self, profile: Profile, result: Mapping[str, Any]
    ) -> dict[str, Any]:
        context_id = f"calibration_context_{uuid.uuid4().hex}"
        context = {
            "context_id": context_id,
            "profile_id": profile.profile_id,
            "ulinkconfig_root": (
                str(Path(profile.ulinkconfig_root).expanduser().resolve(strict=False))
                if profile.ulinkconfig_root else None
            ),
            "config_set_id": str(result.get("config_set_id", "")),
        }
        with self.lock:
            self.calibration_contexts[context_id] = context
        return {
            **result,
            "calibration_context": dict(context),
            "rows": [
                {**row, "calibration_context_id": context_id}
                for row in result.get("rows", [])
            ],
        }

    def match_consumption_templates(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        selection_id = str(payload.get("selection_id", "")).strip()
        with self.lock:
            selection = copy.deepcopy(self.parameter_selections.get(selection_id))
        if selection is None:
            raise InputValidationError("新模板选择已失效，请重新确认模板")
        history_file = payload.get("history_file")
        if not isinstance(history_file, Mapping):
            raise InputValidationError("请上传历史副本数据 CSV")
        _name, data = _decode_csv_upload(history_file)
        return match_calibration_templates(
            selection["manifests"], history_data=data,
            template_name_prefix=payload.get("template_name_prefix", ""),
            template_name_suffix=payload.get("template_name_suffix", ""),
        )

    def _prepare_consumption_calibration(
        self, payload: Mapping[str, Any]
    ) -> tuple[Profile, list[dict[str, Any]], dict[str, Any], dict[str, Any]]:

        parameter_strategy = str(
            payload.get("parameter_strategy", "fidelity")
        ).strip()
        if parameter_strategy != "fidelity":
            raise InputValidationError(
                "目标消耗校准仅支持保真副本参数，请重新计算后再校准"
            )
        selection_id = str(payload.get("selection_id", "")).strip()
        with self.lock:
            selection = copy.deepcopy(self.parameter_selections.get(selection_id))
        if selection is None:
            raise InputValidationError("新模板选择已失效，请重新导入")
        history_file = payload.get("history_file")
        if not isinstance(history_file, Mapping):
            raise InputValidationError("请上传历史副本数据 CSV")
        history_filename, history_data = _decode_csv_upload(history_file)
        config_level_filename: str | None = None
        config_level_data: bytes | None = None
        config_level_file = payload.get("config_level_file")
        if config_level_file is not None:
            if not isinstance(config_level_file, Mapping):
                raise InputValidationError("config_level_file 必须是文件对象")
            config_level_filename, config_level_data = _decode_csv_upload(
                config_level_file, label="config_level"
            )
        raw_overrides = payload.get("target_overrides")
        if raw_overrides is not None and not isinstance(raw_overrides, Mapping):
            raise InputValidationError("target_overrides 必须是对象")
        kwargs = dict(
            config_set_id=str(payload.get("config_set_id", "")),
            level_start=(
                _required_decimal_int(payload, "level_start", minimum=1)
                if payload.get("level_start") is not None else None
            ),
            level_end=(
                _required_decimal_int(payload, "level_end", minimum=1)
                if payload.get("level_end") is not None else None
            ),
            history_data=history_data,
            history_filename=history_filename,
            target_overrides=raw_overrides,
            search_observed_seeds=payload.get("search_observed_seeds", False),
            strategy_group_id=str(
                payload.get("strategy_group_id", "")
            ),
            strategy_group_mode=str(payload.get("strategy_group_mode", "source")),
            config_level_data=config_level_data,
            config_level_filename=config_level_filename,
            focus_output_template_id=(
                str(payload.get("focus_output_template_id", "")).strip() or None
            ),
            first_duplicate_max=payload.get("first_duplicate_max"),
            auto_first_duplicate_max=payload.get("auto_first_duplicate_max", False),
            simulate_first_duplicate_seeds=payload.get(
                "simulate_first_duplicate_seeds", False
            ),
            consider_structure_strategy=payload.get(
                "consider_structure_strategy", True
            ),
            optimize_all_duplicate_seeds=payload.get(
                "optimize_all_duplicate_seeds", False
            ),
            template_name_prefix=payload.get("template_name_prefix", ""),
            template_name_suffix=payload.get("template_name_suffix", ""),
        )
        return self.profile(), selection["manifests"], kwargs, {
            "selection_id": selection_id,
            "source_export_id": selection.get("source_export_id"),
            "source_job_id": selection.get("source_job_id"),
        }

    def create_consumption_calibration_job(
        self, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Start a read-only calibration without holding an HTTP request open."""

        profile, manifests, kwargs, result_context = (
            self._prepare_consumption_calibration(payload)
        )
        fingerprint = hashlib.sha256(canonical_json({
            "payload": payload,
            "profile": profile.as_dict(),
            "manifests": manifests,
            "selection_context": result_context,
        }).encode("utf-8")).hexdigest()
        focus = kwargs.get("focus_output_template_id")
        total = sum(
            not focus or item.get("output_template_id") == focus
            for item in manifests
        )
        with self.lock:
            for existing in self.consumption_calibration_jobs.values():
                if (
                    existing.request_fingerprint == fingerprint
                    and existing.status in {"queued", "running"}
                ):
                    return existing.summary()
            job_id = f"calibration_{uuid.uuid4().hex}"
            record = ConsumptionCalibrationRecord(
                job_id=job_id,
                request_fingerprint=fingerprint,
                created_at=_now(),
                created_monotonic=time.monotonic(),
                progress={
                    "phase": "preparing",
                    "message": "校准任务已排队，准备读取数据",
                    "template_total": total,
                    "template_completed": 0,
                    "current_template": None,
                    "duplicate_index": None,
                    "duplicate_total": 10,
                    "duplicate_completed": 0,
                    "duplicate_total_all": total * 10,
                    "candidate_completed": 0,
                    "candidate_total": 0,
                },
            )
            self.consumption_calibration_jobs[job_id] = record
            initial = record.summary()
        threading.Thread(
            target=self._run_consumption_calibration,
            args=(job_id, profile, manifests, kwargs, result_context),
            name=f"consumption-{job_id}",
            daemon=True,
        ).start()
        return initial

    def consumption_calibration_job(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            record = self.consumption_calibration_jobs.get(job_id)
            if record is None:
                raise InputValidationError("校准任务不存在或会话已失效，请重新生成")
            return record.summary()

    def _run_consumption_calibration(
        self,
        job_id: str,
        profile: Profile,
        manifests: Sequence[Mapping[str, Any]],
        kwargs: Mapping[str, Any],
        result_context: Mapping[str, Any],
    ) -> None:
        with self.lock:
            record = self.consumption_calibration_jobs[job_id]
            record.status = "running"
            record.started_at = _now()

        def progress(value: dict[str, Any]) -> None:
            with self.lock:
                record.progress.update(value)

        try:
            result = build_consumption_calibration(
                profile, manifests, **kwargs, progress_callback=progress
            )
            with self.lock:
                record.result = self._bind_calibration_context(
                    profile, {**result, **result_context}
                )
                record.status = "completed"
                record.completed_at = _now()
                record.completed_monotonic = time.monotonic()
                record.progress.update({
                    "phase": "completed",
                    "message": "校准审阅草案已完成",
                    "template_completed": record.progress["template_total"],
                    "duplicate_completed": record.progress["duplicate_total_all"],
                })
        except Exception as exc:
            with self.lock:
                record.status = "failed"
                record.error = _error_payload(exc)
                record.completed_at = _now()
                record.completed_monotonic = time.monotonic()

    def write_consumption_calibration_config(
        self, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Write the currently reviewed calibration rows to config_level."""

        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise InputValidationError("rows 必须是校准结果数组")
        profile = self.profile()
        context_ids = {
            str(row.get("calibration_context_id"))
            for row in rows
            if isinstance(row, Mapping) and row.get("calibration_context_id")
        }
        provided_context = payload.get("calibration_context")
        if provided_context is not None:
            if not isinstance(provided_context, Mapping) or not provided_context.get("context_id"):
                raise InputValidationError("校准草案来源已失效，请重新生成")
            context_ids.add(str(provided_context["context_id"]))
        selected_config_id = str(payload.get("config_set_id", "")).strip()
        current_root = (
            str(Path(profile.ulinkconfig_root).expanduser().resolve(strict=False))
            if profile.ulinkconfig_root else None
        )
        if context_ids and not selected_config_id:
            sets = discover_config_sets(profile.ulinkconfig_root or "")
            selected_config_id = next(
                (item["id"] for item in sets if item["id"] == "csv"),
                sets[0]["id"],
            )
        with self.lock:
            for context_id in context_ids:
                context = self.calibration_contexts.get(context_id)
                if context is None:
                    raise InputValidationError("校准草案来源已失效，请重新生成")
                if (
                    context["profile_id"] != profile.profile_id
                    or context["ulinkconfig_root"] != current_root
                ):
                    raise InputValidationError("配置环境已变化，请在当前环境重新生成校准草案")
                if context["config_set_id"] != selected_config_id:
                    raise InputValidationError("目标配置集已变化，请重新生成校准草案")
        return write_calibration_config(
            profile,
            config_set_id=selected_config_id,
            calibration_rows=rows,
        )

    def lookup_unlaunched_parameters(
        self, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        selection_id = str(payload.get("selection_id", "")).strip()
        source_export_id: str | None = None
        source_job_id: str | None = None
        if selection_id:
            with self.lock:
                selection = self.parameter_selections.get(selection_id)
            if selection is None:
                raise InputValidationError("新模板选择已失效，请重新导入")
            manifests = selection["manifests"]
            template_ids = [
                str(item.get("source_template_id", "")).strip()
                for item in manifests
                if str(item.get("source_template_id", "")).strip()
            ]
            source_export_id = selection.get("source_export_id")
            source_job_id = selection.get("source_job_id")
        else:
            template_ids = payload.get("template_ids")
            if (
                not isinstance(template_ids, Sequence)
                or isinstance(template_ids, (str, bytes))
            ):
                raise InputValidationError("模板 ID 列表格式错误")

        result = lookup_unlaunched_template_parameters(
            self.profile(),
            [str(value) for value in template_ids],
            config_set_id=(str(payload.get("config_set_id", "")).strip() or None),
        )
        parameter_configured_at: str | None = None
        if source_export_id is not None:
            with self.lock:
                export = self.exports.get(str(source_export_id))
                if export is None:
                    raise InputValidationError("导出记录已失效，请重新选择")
                if source_job_id and export.job_id != source_job_id:
                    raise InputValidationError("导出记录与原任务绑定已失效，请重新选择")
                if export.status != "completed":
                    raise InputValidationError("导出记录状态已变化，请重新选择")
                if export.parameter_configured_at is None:
                    export.parameter_configured_at = _now()
                    export.revision += 1
                    export.add_event(
                        "parameter_configured",
                        status="completed",
                        message="未上线模板参数索引已完成",
                        details={"selection_id": selection_id, "mode": "unlaunched"},
                        at=export.parameter_configured_at,
                    )
                parameter_configured_at = export.parameter_configured_at
        return {
            **result,
            "selection_id": selection_id or None,
            "source_export_id": source_export_id,
            "source_job_id": source_job_id,
            "parameter_configured": parameter_configured_at is not None,
            "parameter_configured_at": parameter_configured_at,
        }

    def reports(self) -> list[dict[str, Any]]:
        root = Path(self.profile().output_root) / "验证报告与生成记录"
        if not root.is_dir():
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(root.rglob("*"), key=lambda value: value.stat().st_mtime, reverse=True):
            if len(items) >= 120:
                break
            if not path.is_file() or path.suffix.lower() not in {".json", ".md", ".csv"}:
                continue
            relative = path.relative_to(root).as_posix()
            items.append(
                {
                    "name": path.name,
                    "relative_path": relative,
                    "kind": path.suffix.lower().lstrip("."),
                    "size": path.stat().st_size,
                    "updated_at": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
                }
            )
        return items


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: AppState):
        super().__init__(address, RequestHandler)
        self.state = state


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "KeyStructureLocal/0.2"

    @property
    def app(self) -> AppState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), format % args))

    def _headers(self, content_type: str, length: int, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        data = json.dumps(
            _json_ready(payload), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self._headers("application/json; charset=utf-8", len(data), int(status))
        self.wfile.write(data)

    def _send_file(self, path: Path) -> None:
        data = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._headers(f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type, len(data), HTTPStatus.OK)
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise InputValidationError("Content-Length 无效") from exc
        if length <= 0 or length > MAX_JSON_BODY:
            raise InputValidationError("请求体为空或超过安全上限")
        try:
            value = json.loads(
                self.rfile.read(length).decode("utf-8"),
                object_pairs_hook=_duplicate_rejecting_json_object,
            )
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise InputValidationError("请求 JSON 无法解析") from exc
        if not isinstance(value, dict):
            raise InputValidationError("请求 JSON 根节点必须是对象")
        return value

    def _check_local(self) -> None:
        host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise InputValidationError("仅允许本机同源访问")

    def _check_csrf(self) -> None:
        if not secrets.compare_digest(
            self.headers.get("X-CSRF-Token", ""), self.app.csrf_token
        ):
            raise InputValidationError("页面会话已失效，请刷新后重试")

    def _route_error(self, error: Exception) -> None:
        status = HTTPStatus.BAD_REQUEST
        if isinstance(error, FileNotFoundError):
            status = HTTPStatus.NOT_FOUND
        self._send_json({"ok": False, "error": _error_payload(error)}, status)

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._check_local()
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            if path in {"/", "/index.html"}:
                self._send_file(FRONTEND_ROOT / "index.html")
                return
            static = {
                "/assets/styles.css": ASSET_ROOT / "styles.css",
                "/assets/app.js": ASSET_ROOT / "app.js",
            }
            if path in static:
                self._send_file(static[path])
                return
            if path.startswith("/assets/fonts/"):
                font_name = path.removeprefix("/assets/fonts/")
                # Fonts are the only files exposed from this directory.  A
                # single basename check keeps traversal attempts and bundled
                # licence/source files outside the HTTP surface.
                if (
                    font_name
                    and "/" not in font_name
                    and "\\" not in font_name
                    and Path(font_name).suffix.lower() == ".woff2"
                ):
                    font_path = FONT_ROOT / font_name
                    if font_path.is_file():
                        self._send_file(font_path)
                        return
            if path == "/api/v1/bootstrap":
                self._send_json(
                    {
                        "ok": True,
                        "app_version": APP_VERSION,
                        "csrf_token": self.app.csrf_token,
                        "path_candidates": local_path_candidates(),
                        "profile_status": self.app.profile_status(),
                        "jobs": self.app.history(),
                        "exports": self.app.export_history(),
                        "strategies": STRATEGIES,
                    }
                )
                return
            if path == "/api/v1/profile":
                self._send_json({"ok": True, **self.app.profile_status()})
                return
            if path == "/api/v1/jobs":
                self._send_json({"ok": True, "jobs": self.app.history()})
                return
            if path == "/api/v1/exports":
                self._send_json(
                    {"ok": True, "exports": self.app.export_history()}
                )
                return
            if path.startswith("/api/v1/preflights/"):
                preflight_id = path.removeprefix("/api/v1/preflights/").strip("/")
                self._send_json(
                    {
                        "ok": True,
                        **self.app.get_generation_preflight(preflight_id).summary(),
                    }
                )
                return
            if path.startswith("/api/v1/exports/"):
                export_id = path.removeprefix("/api/v1/exports/").strip("/")
                self._send_json(
                    {"ok": True, **self.app.get_export_run(export_id).summary()}
                )
                return
            if path.startswith("/api/v1/jobs/") and "/source/" in path:
                job_id, template_key = path.removeprefix("/api/v1/jobs/").split("/source/", 1)
                self._send_json({"ok": True, **self.app.job_source_preview(job_id, template_key)})
                return
            if path.startswith("/api/v1/jobs/"):
                job_id = path.removeprefix("/api/v1/jobs/").strip("/")
                job = self.app.get_job(job_id)
                payload = {"ok": True, **job.summary()}
                if job.presentation is not None:
                    payload["result"] = job.presentation
                if job.export_result is not None:
                    payload["export_result"] = job.export_result
                self._send_json(payload)
                return
            if path == "/api/v1/reports":
                self._send_json({"ok": True, "reports": self.app.reports()})
                return
            if path == "/api/v1/parameters/config-sets":
                profile = self.app.profile()
                online_values: list[dict[str, str]] = []
                unlaunched_values: list[dict[str, str]] = []
                if profile.ulinkconfig_root:
                    try:
                        online_values = discover_config_sets(profile.ulinkconfig_root)
                    except InputValidationError:
                        pass
                    try:
                        unlaunched_values = discover_unlaunched_config_sets(
                            profile.ulinkconfig_root
                        )
                    except InputValidationError:
                        pass
                self._send_json(
                    {
                        "ok": True,
                        "config_sets": online_values,
                        "unlaunched_config_sets": unlaunched_values,
                    }
                )
                return
            if path.startswith("/api/v1/parameters/consumption-calibration-jobs/"):
                job_id = path.removeprefix(
                    "/api/v1/parameters/consumption-calibration-jobs/"
                ).strip("/")
                self._send_json({
                    "ok": True,
                    **self.app.consumption_calibration_job(job_id),
                })
                return
            self._send_json(
                {"ok": False, "error": {"code": "not_found", "message": "接口不存在"}},
                HTTPStatus.NOT_FOUND,
            )
        except Exception as exc:
            self._route_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._check_local()
            self._check_csrf()
            path = unquote(urlsplit(self.path).path)
            body = self._body()
            if path == "/api/v1/profile/source":
                current = self.app.profile()
                changed = self.app.profile_service.configure_source(
                    current.profile_id,
                    Path(str(body.get("tile_repo_root", ""))),
                    Path(str(body["template_dir"])) if body.get("template_dir") else None,
                )
                self._send_json({"ok": True, **self.app.profile_service.status(changed.profile_id)})
                return
            if path == "/api/v1/profile/output":
                current = self.app.profile()
                changed = self.app.profile_service.configure_output(
                    current.profile_id, Path(str(body.get("output_root", "")))
                )
                self._send_json({"ok": True, **self.app.profile_service.status(changed.profile_id)})
                return
            if path == "/api/v1/profile/ulinkconfig":
                current = self.app.profile()
                changed = self.app.profile_service.configure_ulinkconfig(
                    current.profile_id, Path(str(body.get("ulinkconfig_root", "")))
                )
                self._send_json({"ok": True, **self.app.profile_service.status(changed.profile_id)})
                return
            if path == "/api/v1/dialog/directory":
                purpose = str(body.get("purpose", "directory"))
                prompts = {
                    "source": "选择 tile 仓库目录",
                    "output": "选择输出目录",
                    "ulinkconfig": "选择 ulinkconfig 目录",
                }
                prompt = prompts.get(purpose, "选择目录")
                if sys.platform != "darwin":
                    raise InputValidationError("当前系统请直接粘贴目录路径")
                script = f'POSIX path of (choose folder with prompt "{prompt}")'
                completed = subprocess.run(
                    ["osascript", "-e", script],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if completed.returncode != 0:
                    self._send_json({"ok": True, "cancelled": True})
                else:
                    self._send_json({"ok": True, "path": completed.stdout.strip()})
                return
            if path == "/api/v1/templates/inspect":
                self._send_json({"ok": True, **self.app.import_templates(body)})
                return
            if path == "/api/v1/plans/resolve":
                self._send_json({"ok": True, **self.app.resolve_plan(body)})
                return
            if path.startswith("/api/v1/preflights/") and path.endswith(
                "/recommendations/apply"
            ):
                preflight_id = (
                    path.removeprefix("/api/v1/preflights/")
                    .removesuffix("/recommendations/apply")
                    .strip("/")
                )
                self._send_json(
                    {
                        "ok": True,
                        **self.app.apply_preflight_generation_recommendation(
                            preflight_id, body
                        ),
                    }
                )
                return
            if path.startswith("/api/v1/preflights/") and path.endswith(
                "/recommendations/undo"
            ):
                preflight_id = (
                    path.removeprefix("/api/v1/preflights/")
                    .removesuffix("/recommendations/undo")
                    .strip("/")
                )
                self._send_json(
                    {
                        "ok": True,
                        **self.app.undo_preflight_generation_recommendation(
                            preflight_id, body
                        ),
                    }
                )
                return
            if path == "/api/v1/jobs":
                job = self.app.create_job(body)
                self._send_json({"ok": True, **job.summary()}, HTTPStatus.ACCEPTED)
                return
            if path.startswith("/api/v1/jobs/") and path.endswith("/control"):
                job_id = (
                    path.removeprefix("/api/v1/jobs/")
                    .removesuffix("/control")
                    .strip("/")
                )
                job = self.app.control_job(
                    job_id,
                    str(body.get("action", "")),
                    body,
                )
                self._send_json(
                    {"ok": True, **job.summary()},
                    HTTPStatus.ACCEPTED,
                )
                return
            if path.startswith("/api/v1/jobs/") and path.endswith(
                "/recommendations/apply"
            ):
                job_id = (
                    path.removeprefix("/api/v1/jobs/")
                    .removesuffix("/recommendations/apply")
                    .strip("/")
                )
                self._send_json(
                    {
                        "ok": True,
                        **self.app.apply_generation_recommendation(job_id, body),
                    }
                )
                return
            if path.startswith("/api/v1/jobs/") and path.endswith(
                "/recommendations/undo"
            ):
                job_id = (
                    path.removeprefix("/api/v1/jobs/")
                    .removesuffix("/recommendations/undo")
                    .strip("/")
                )
                self._send_json(
                    {
                        "ok": True,
                        **self.app.undo_generation_recommendation(job_id, body),
                    }
                )
                return
            if path.startswith("/api/v1/jobs/") and path.endswith("/exports"):
                job_id = (
                    path.removeprefix("/api/v1/jobs/")
                    .removesuffix("/exports")
                    .strip("/")
                )
                record = self.app.create_export_run(job_id, body)
                self._send_json(
                    {"ok": True, **record.summary()},
                    HTTPStatus.ACCEPTED,
                )
                return
            if path.startswith("/api/v1/exports/") and path.endswith("/control"):
                export_id = (
                    path.removeprefix("/api/v1/exports/")
                    .removesuffix("/control")
                    .strip("/")
                )
                record = self.app.control_export(
                    export_id,
                    str(body.get("action", "")),
                )
                self._send_json(
                    {"ok": True, **record.summary()},
                    HTTPStatus.ACCEPTED,
                )
                return
            if path.startswith("/api/v1/exports/") and path.endswith(
                "/open-directory"
            ):
                export_id = (
                    path.removeprefix("/api/v1/exports/")
                    .removesuffix("/open-directory")
                    .strip("/")
                )
                self._send_json(
                    {
                        "ok": True,
                        **self.app.open_export_output_directory(export_id),
                    }
                )
                return
            if path.startswith("/api/v1/jobs/") and path.endswith("/export"):
                job_id = path.removeprefix("/api/v1/jobs/").removesuffix("/export").strip("/")
                self._send_json({"ok": True, **self.app.export_job(job_id, body)})
                return
            if path == "/api/v1/parameters/resolve":
                self._send_json({"ok": True, **self.app.resolve_parameter_templates(body)})
                return
            if path == "/api/v1/parameters/calculate":
                self._send_json({"ok": True, **self.app.calculate_parameters(body)})
                return
            if path == "/api/v1/parameters/calibrate-consumption":
                self._send_json(
                    {
                        "ok": True,
                        **self.app.calibrate_consumption_parameters(body),
                    }
                )
                return
            if path == "/api/v1/parameters/match-consumption-templates":
                self._send_json({"ok": True, **self.app.match_consumption_templates(body)})
                return
            if path == "/api/v1/parameters/consumption-calibration-jobs":
                self._send_json(
                    {"ok": True, **self.app.create_consumption_calibration_job(body)},
                    HTTPStatus.ACCEPTED,
                )
                return
            if path == "/api/v1/parameters/write-calibration-config":
                self._send_json(
                    {
                        "ok": True,
                        **self.app.write_consumption_calibration_config(body),
                    }
                )
                return
            if path == "/api/v1/parameters/unlaunched-index":
                self._send_json(
                    {"ok": True, **self.app.lookup_unlaunched_parameters(body)}
                )
                return
            self._send_json(
                {"ok": False, "error": {"code": "not_found", "message": "接口不存在"}},
                HTTPStatus.NOT_FOUND,
            )
        except Exception as exc:
            self._route_error(exc)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动关键结构生成工具本地网页")
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1",))
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--open", action="store_true", dest="open_browser")
    parser.add_argument("--no-open", action="store_false", dest="open_browser")
    parser.set_defaults(open_browser=False)
    parser.add_argument("--settings", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    service = ProfileService(args.settings) if args.settings else ProfileService()
    state = AppState(service)
    server = AppServer((args.host, args.port), state)
    address = f"http://{args.host}:{server.server_address[1]}/"
    print(f"关键结构生成工具已启动：{address}", flush=True)
    if args.open_browser:
        threading.Timer(0.35, lambda: webbrowser.open(address)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
