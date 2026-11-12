#!/usr/bin/env python3
"""Loopback-only web server for the player behaviour analysis tool."""

from __future__ import annotations

import argparse
import csv
import errno
import hashlib
import http.client
import json
import math
import mimetypes
import re
import secrets
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from datetime import datetime
from email.utils import formatdate
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence


SCRIPT_ROOT = Path(__file__).resolve().parent
TOOL_ROOT = SCRIPT_ROOT.parent
FRONTEND_ROOT = TOOL_ROOT / "frontend"
RUNTIME_ROOT = TOOL_ROOT / "运行数据"
DRAFT_ROOT = RUNTIME_ROOT / "草稿"
JOB_ROOT = RUNTIME_ROOT / "任务"
OUTPUT_ROOT = TOOL_ROOT / "输出分析结果"
DEFAULT_CONFIG_PATH = Path.home() / "ulinkconfig" / "csv"
DEFAULT_TEMPLATE_PATH = (
    Path.home() / "tile" / "Tile" / "Assets" / "Resources" / "TileMatchLevelData"
)

if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from services.engine_adapter import (  # noqa: E402
    AnalysisEngineAdapter,
    EngineUnavailableError,
)
from services.source_service import (  # noqa: E402
    SourceValidationError,
    json_safe,
    normalize_settings,
    path_audit,
    persist_source,
)
from services.storage import (  # noqa: E402
    DraftRepository,
    JobRepository,
    atomic_write_json,
    now_iso,
)


APP_VERSION = "1.0.0"
APP_NAME = "玩家行为分析工具"
SERVICE_ID = "tile-family.player-behaviour-analysis-tool"
SERVICE_PROTOCOL = 1
MAX_JSON_BODY = 256 * 1024 * 1024
_JOB_ROUTE = re.compile(r"^/api/jobs/(?P<job>job-[A-Za-z0-9_-]+)$")
_ONLINE_ROUTE = re.compile(r"^/api/jobs/(?P<job>job-[A-Za-z0-9_-]+)/online$")
_DOWNLOAD_ROUTE = re.compile(
    r"^/api/download/(?P<job>job-[A-Za-z0-9_-]+)/(?P<artifact>[A-Za-z0-9_-]+)$"
)
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}
_ARTIFACT_SUFFIXES = {
    ".xlsx",
    ".csv",
    ".json",
    ".md",
    ".txt",
    ".zip",
    ".gz",
}


class ApiError(RuntimeError):
    def __init__(
        self,
        status: int,
        message: str,
        *,
        code: str = "request_error",
        details: Any = None,
    ):
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.details is not None:
            result["details"] = json_safe(self.details)
        return result


def _merge_audits(base: Mapping[str, Any], engine: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve engine-specific evidence while retaining transport checks."""

    merged = dict(json_safe(base))
    engine_safe = dict(json_safe(engine))
    base_errors = list(merged.get("errors") or [])
    base_warnings = list(merged.get("warnings") or [])
    merged.update(engine_safe)
    merged["errors"] = base_errors + list(engine_safe.get("errors") or [])
    merged["warnings"] = base_warnings + list(engine_safe.get("warnings") or [])
    merged["valid"] = bool(base.get("valid", False)) and bool(
        engine_safe.get("valid", True)
    ) and not merged["errors"]
    return merged


def _public_job(state: Mapping[str, Any]) -> dict[str, Any]:
    artifacts = []
    for item in state.get("artifacts") or []:
        if not isinstance(item, Mapping):
            continue
        public = {
            key: json_safe(value)
            for key, value in item.items()
            if key not in {"relative_path", "absolute_path"}
        }
        if public.get("id"):
            public["download_url"] = (
                f"/api/download/{urllib.parse.quote(str(state.get('job_id')))}"
                f"/{urllib.parse.quote(str(public['id']))}"
            )
        artifacts.append(public)
    sources: dict[str, Any] = {}
    for kind, value in (state.get("source") or {}).items():
        if not isinstance(value, Mapping):
            continue
        sources[str(kind)] = {
            key: json_safe(item)
            for key, item in value.items()
            if key in {"files", "audit", "draft_id"}
        }
    summary = dict(json_safe(state.get("summary") or {}))
    engine_player = (state.get("engine_results") or {}).get("player")
    if isinstance(engine_player, Mapping):
        player_summary = dict(summary.get("player") or {})
        for key in ("first_level_label_counts", "coverage", "audit", "workbook_audit"):
            if key not in player_summary and key in engine_player:
                player_summary[key] = json_safe(engine_player[key])
        if "sample_recommendation" not in player_summary:
            legacy_recommendation = _legacy_sample_recommendation(state, engine_player)
            if legacy_recommendation:
                player_summary["sample_recommendation"] = legacy_recommendation
        summary["player"] = player_summary
    return {
        "job_id": state.get("job_id"),
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "status": state.get("status"),
        "stage": state.get("stage"),
        "progress": json_safe(state.get("progress") or {}),
        "logs": json_safe(state.get("logs") or []),
        "summary": summary,
        "previews": json_safe(state.get("previews") or {}),
        "artifacts": artifacts,
        "source": sources,
        "error": json_safe(state.get("error")),
    }


def _legacy_sample_recommendation(
    state: Mapping[str, Any], engine_player: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Backfill the sample recommendation for completed jobs made by older builds."""

    job_id = str(state.get("job_id") or "")
    allowed_root = (OUTPUT_ROOT / job_id).resolve()

    def trusted_csv(key: str) -> Path | None:
        value = engine_player.get(key)
        if not value:
            return None
        try:
            path = Path(str(value)).resolve(strict=True)
            path.relative_to(allowed_root)
        except (OSError, ValueError):
            return None
        return path if path.is_file() and path.suffix.lower() == ".csv" else None

    instances_path = trusted_csv("instances_csv_path")
    levels_path = trusted_csv("levels_csv_path")
    if instances_path is None or levels_path is None:
        return None
    try:
        with levels_path.open("r", encoding="utf-8-sig", newline="") as handle:
            pending_levels = {
                int(float(row["level_index"]))
                for row in csv.DictReader(handle)
                if row.get("first_level_behavior_label") == "样本覆盖不足"
            }
        player_summary = (state.get("summary") or {}).get("player") or {}
        current_players = int(
            player_summary.get("players")
            or player_summary.get("counts", {}).get("players")
            or (state.get("source") or {}).get("player", {}).get("audit", {}).get("unique_players")
            or 0
        )
        if not pending_levels:
            return {
                "next_action": "online_metrics",
                "pending_level_count": 0,
                "recommended_additional_players": 0,
                "recommended_total_players": current_players,
                "basis": "所有关卡的可分类实例玩家权重均已达70%",
            }
        counts_by_level: dict[int, list[float]] = {}
        with instances_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("attempt_phase") != "首局":
                    continue
                level_index = int(float(row.get("level_index") or 0))
                player_count = float(row.get("player_count") or 0)
                if level_index in pending_levels and player_count > 0:
                    counts_by_level.setdefault(level_index, []).append(player_count)
        search_limit = max(500, current_players * 10)
        per_level_need: dict[int, int] = {}
        for level_index, counts in counts_by_level.items():
            total = sum(counts)
            for additional in range(search_limit + 1):
                scale = 1.0 + additional / total
                expected = [count * scale for count in counts]
                coverage = sum(count for count in expected if count >= 10.0) / sum(expected)
                if coverage >= 0.70:
                    per_level_need[level_index] = additional
                    break
        raw_need = max(per_level_need.values()) if per_level_need else max(20, math.ceil(current_players * 0.5))
        recommended = max(20, int(math.ceil(raw_need * 1.25 / 10.0) * 10))
        return {
            "next_action": "add_player_sample",
            "pending_level_count": len(pending_levels),
            "recommended_additional_players": recommended,
            "recommended_total_players": current_players + recommended,
            "raw_estimated_additional_players": raw_need,
            "allocation_buffer": 0.25,
            "basis": "按当前首局实例分配比例估算；实例至少10人且关卡可分类权重至少70%",
        }
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _decode_csv_preview(path: Path, limit: int = 5000) -> dict[str, Any] | None:
    if path.suffix.lower() != ".csv" or not path.is_file():
        return None
    if path.stat().st_size > 16 * 1024 * 1024:
        return None
    data = path.read_bytes()
    text: str | None = None
    encoding = ""
    for candidate in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = data.decode(candidate)
            encoding = candidate
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return None
    try:
        dialect = csv.Sniffer().sniff(text[:65536], delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(text.splitlines(), dialect=dialect)
    try:
        columns = [str(value)[:200] for value in next(reader)]
    except StopIteration:
        return None
    rows: list[list[str]] = []
    for row in reader:
        if len(rows) >= limit:
            break
        rows.append([str(value)[:500] for value in row])
    return {
        "columns": columns,
        "rows": rows,
        "shown_rows": len(rows),
        "encoding": encoding,
        "truncated": next(reader, None) is not None,
    }


def _artifact_label(key: str, filename: str) -> str:
    labels = {
        "workbook_path": "Excel 结论工作簿",
        "levels_csv_path": "关卡分层明细",
        "instances_csv_path": "关卡实例分层明细",
        "metrics_csv_path": "行为维度稳定性明细",
        "summary_path": "分析摘要",
        "recommendation_csv_path": "三维分类与推荐序列",
        "duplicate_labels_csv_path": "关卡副本压力标签明细",
        "audit_csv_path": "分类审计明细",
        "metadata_path": "分类口径元数据",
        "config_level_list_copy_path": "重排后的 config_level_list 副本",
        "config_level_list_audit_path": "配置重排映射审计",
        "level_list_copy_path": "重排后的 config_level_list 副本",
        "level_list_audit_path": "配置重排映射审计",
    }
    return labels.get(key, Path(filename).stem)


def _artifact_kind(key: str, filename: str, stage: str) -> str:
    """Return a stable semantic identifier independent of localized filenames."""

    key_text = str(key).casefold()
    file_text = str(filename).casefold()
    exact = {
        "levels_csv_path": "behavior_levels",
        "instances_csv_path": "behavior_instances",
        "metrics_csv_path": "behavior_metrics",
        "summary_path": "behavior_summary" if stage == "player" else "classification_metadata",
        "recommendation_csv_path": "classification_recommendation",
        "duplicate_labels_csv_path": "classification_duplicates",
        "audit_csv_path": "classification_audit",
        "metadata_path": "classification_metadata",
        "config_level_list_copy_path": "config_level_list_copy",
        "config_level_list_audit_path": "config_level_list_audit",
        "level_list_copy_path": "config_level_list_copy",
        "level_list_audit_path": "config_level_list_audit",
    }
    if key_text in exact:
        return exact[key_text]
    combined = f"{key_text} {file_text}"
    if "config_level_list" in combined or "配置重排" in combined:
        if "audit" in combined or "审计" in combined or "映射" in combined:
            return "config_level_list_audit"
        return "config_level_list_copy"
    if file_text.endswith(".xlsx"):
        return "behavior_workbook" if stage == "player" else "classification_workbook"
    if stage == "player":
        if "实例" in combined or "instance" in combined:
            return "behavior_instances"
        if "行为维度" in combined or "metric" in combined or "稳定性" in combined:
            return "behavior_metrics"
        if "关卡号分层" in combined or "level" in key_text:
            return "behavior_levels"
        if "summary" in combined or "摘要" in combined or file_text.endswith(".json"):
            return "behavior_summary"
    else:
        if "副本标签" in combined or "duplicate" in combined:
            return "classification_duplicates"
        if "recommend" in combined or "推荐序列" in combined:
            return "classification_recommendation"
        if "audit" in combined or "审计" in combined:
            return "classification_audit"
        if "metadata" in combined or "口径" in combined or file_text.endswith(".json"):
            return "classification_metadata"
    digest = hashlib.sha256(f"{stage}:{key_text}:{file_text}".encode("utf-8")).hexdigest()[:12]
    return f"{stage}_artifact_{digest}"


def _candidate_artifacts(result: Mapping[str, Any]) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    raw = result.get("artifacts")
    if isinstance(raw, Mapping):
        found.extend((str(key), value) for key, value in raw.items())
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        for index, value in enumerate(raw):
            if isinstance(value, Mapping):
                found.append((str(value.get("label") or value.get("id") or f"artifact_{index}"), value))
            else:
                found.append((f"artifact_{index}", value))
    for key, value in result.items():
        if key.endswith(("_path", "_file")) and value:
            found.append((str(key), value))
    return found


def _normalize_artifacts(
    job_id: str,
    result: Mapping[str, Any],
    repository: JobRepository,
    stage: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    output_dir = repository.output_directory(job_id).resolve()
    candidates = _candidate_artifacts(result)
    # Also capture engine-created result files even if an older engine omitted
    # every path field.  Do this only as a true fallback: scanning the shared
    # output directory during phase two would relabel phase-one files.
    if not candidates:
        for path in output_dir.rglob("*"):
            if path.is_file() and not path.name.startswith("."):
                candidates.append((path.stem, path))
    artifacts: list[dict[str, Any]] = []
    previews: dict[str, Any] = {}
    seen: set[str] = set()
    for key, raw in candidates:
        label = _artifact_label(key, str(raw))
        path_value = raw
        if isinstance(raw, Mapping):
            path_value = raw.get("path", raw.get("file", raw.get("filename")))
            label = str(raw.get("label") or label)
        if not isinstance(path_value, (str, Path)):
            continue
        path = Path(path_value)
        if not path.is_absolute():
            path = output_dir / path
        try:
            resolved = path.resolve(strict=True)
            relative = resolved.relative_to(output_dir)
        except (OSError, ValueError):
            continue
        if not resolved.is_file() or resolved.suffix.lower() not in _ARTIFACT_SUFFIXES:
            continue
        relative_text = relative.as_posix()
        if relative_text in seen:
            continue
        seen.add(relative_text)
        artifact_id = "a-" + hashlib.sha256(relative_text.encode("utf-8")).hexdigest()[:16]
        content_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        item = {
            "id": artifact_id,
            "kind": _artifact_kind(key, resolved.name, stage),
            "label": label,
            "filename": resolved.name,
            "size": resolved.stat().st_size,
            "content_type": content_type,
            "relative_path": relative_text,
        }
        artifacts.append(item)
        preview = _decode_csv_preview(resolved)
        if preview is not None:
            previews[artifact_id] = {"title": label, **preview}
    return artifacts, previews


class Application:
    def __init__(
        self,
        *,
        tool_root: Path = TOOL_ROOT,
        engine: AnalysisEngineAdapter | None = None,
    ):
        self.tool_root = tool_root.resolve()
        runtime_root = self.tool_root / "运行数据"
        self.drafts = DraftRepository(runtime_root / "草稿")
        self.jobs = JobRepository(runtime_root / "任务", self.tool_root / "输出分析结果")
        self.engine = engine or AnalysisEngineAdapter()
        self.csrf_token = secrets.token_urlsafe(32)
        self._threads: dict[str, threading.Thread] = {}
        self._threads_lock = threading.RLock()
        self.jobs.discover(recover_interrupted=True)

    def bootstrap(self) -> dict[str, Any]:
        recent = [_public_job(item) for item in self.jobs.discover(limit=10, recover_interrupted=False)]
        config_exists = DEFAULT_CONFIG_PATH.is_dir()
        template_exists = DEFAULT_TEMPLATE_PATH.is_dir()
        demo_root = self.tool_root / "运行数据" / "示例"
        demos = sorted(path.name for path in demo_root.glob("*.csv")) if demo_root.is_dir() else []
        return {
            "app": {"name": APP_NAME, "version": APP_VERSION},
            "csrf_token": self.csrf_token,
            "status": {
                "server": "ready",
                "local_only": True,
                "engine": self.engine.describe(),
            },
            "paths": {
                "config_path": {"value": str(DEFAULT_CONFIG_PATH) if config_exists else "", "ready": config_exists},
                "template_path": {"value": str(DEFAULT_TEMPLATE_PATH) if template_exists else "", "ready": template_exists},
                "history_ref": {"value": "historical", "ready": True},
                "history": {
                    "path": "固定历史配置快照（本轮审计提交）",
                    "exists": True,
                    "mode": "historical",
                },
                "output_path": {"value": str(self.jobs.output_root), "ready": True},
            },
            "demo": {
                "available": bool(demos),
                "files": demos,
                "message": "示例数据仅在本机存在时显示。",
            },
            "recent_jobs": recent,
            "limits": {
                "json_body_bytes": MAX_JSON_BODY,
                "file_bytes": 64 * 1024 * 1024,
                "file_count": 6,
            },
        }

    def preflight_player(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._preflight("player", payload, job_id=None)

    def preflight_online(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        job_id = str(payload.get("job_id") or "").strip()
        if not job_id:
            raise ApiError(400, "线上数据预检缺少 job_id", code="missing_job_id")
        state = self.jobs.load(job_id)
        if state.get("status") in {"queued", "running"}:
            raise ApiError(409, "任务正在运行，请完成后再导入线上指标。", code="job_busy")
        if not state.get("engine_results", {}).get("player") and state.get("stage") != "player_completed":
            raise ApiError(
                409,
                "请先完成玩家行为分析，再导入线上指标。",
                code="player_stage_incomplete",
            )
        return self._preflight("online", payload, job_id=job_id, player_job=state)

    def _preflight(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        job_id: str | None,
        player_job: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        settings = normalize_settings(
            payload.get("settings"),
            default_config_path=DEFAULT_CONFIG_PATH,
            default_template_path=DEFAULT_TEMPLATE_PATH,
        )
        # The parser implementation is bundled with this tool and is not a
        # user-selectable executable/code path.
        settings["template_reader_dir"] = str(SCRIPT_ROOT / "analysis")
        draft_id, draft_dir = self.drafts.allocate(kind)
        try:
            source_files, source_audit = persist_source(
                payload.get("source"),
                destination=draft_dir / "input",
                paste_name=f"{kind}_pasted.csv",
            )
            paths = path_audit(settings, require_paths=(kind == "player"))
            base_audit = {
                "valid": bool(source_audit["valid"]) and not paths["errors"],
                "source": source_audit,
                "paths": paths["paths"],
                "history": {
                    "mode": settings.get("history_mode"),
                    "level_tile_git_revision": settings.get("level_tile_git_revision"),
                    "config_level_git_revision": settings.get("config_level_git_revision"),
                },
                "errors": list(source_audit["errors"]) + list(paths["errors"]),
                "warnings": list(source_audit["warnings"]),
            }
            kwargs = {
                "source_files": source_files,
                "settings": settings,
                "work_dir": draft_dir,
                "player_job": player_job,
            }
            engine_audit = (
                self.engine.preflight_player(**kwargs)
                if kind == "player"
                else self.engine.preflight_online(**kwargs)
            )
            audit = _merge_audits(base_audit, engine_audit)
            record = {
                "draft_id": draft_id,
                "kind": kind,
                "job_id": job_id,
                "created_at": now_iso(),
                "settings": settings,
                "source_files": [str(path) for path in source_files],
                "audit": audit,
            }
            self.drafts.save(draft_id, record)
            return {"draft_id": draft_id, "audit": audit}
        except Exception:
            # Keep structural-error diagnostics in the allocated draft without
            # ever echoing uploaded content to the caller.
            error_record = {
                "draft_id": draft_id,
                "kind": kind,
                "job_id": job_id,
                "created_at": now_iso(),
                "settings": settings,
                "source_files": [],
                "audit": {"valid": False, "errors": [{"code": "preflight_failed", "message": "预检未完成"}]},
            }
            self.drafts.save(draft_id, error_record)
            raise

    def start_player(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        draft_id = str(payload.get("draft_id") or "").strip()
        draft = self.drafts.load(draft_id)
        if draft.get("kind") != "player":
            raise ApiError(400, "草稿不是玩家数据预检结果", code="wrong_draft_kind")
        if not draft.get("audit", {}).get("valid"):
            raise ApiError(409, "数据未通过预检，不能开始分析。", code="preflight_invalid")
        state = self.jobs.create_from_draft(draft, self.drafts.directory(draft_id))
        job_id = str(state["job_id"])
        self._spawn(job_id, lambda: self._run_player(job_id))
        return {"job_id": job_id}

    def attach_online_preflight(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        # Preflight is deliberately side-effect free for an existing job.  The
        # exact approved draft is attached only when the online run starts.
        return self.preflight_online(payload)

    def start_online(self, job_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        draft_id = str(payload.get("draft_id") or "").strip()
        draft = self.drafts.load(draft_id)
        if draft.get("kind") != "online" or draft.get("job_id") != job_id:
            raise ApiError(400, "线上数据草稿与当前任务不匹配", code="draft_job_mismatch")
        if not draft.get("audit", {}).get("valid"):
            raise ApiError(409, "线上数据未通过预检。", code="preflight_invalid")
        with self._threads_lock:
            state = self.jobs.load(job_id)
            if state.get("status") in {"queued", "running"}:
                raise ApiError(409, "任务正在运行，请稍后再试。", code="job_busy")
            if not state.get("engine_results", {}).get("player"):
                raise ApiError(409, "玩家行为阶段尚未完成。", code="player_stage_incomplete")
            # Revalidate the immutable draft at action time. This prevents a
            # draft created by an older local build from bypassing newly added
            # coverage or configuration checks after the service is upgraded.
            fresh_engine_audit = self.engine.preflight_online(
                source_files=[Path(path) for path in draft.get("source_files") or []],
                settings=draft.get("settings") or {},
                player_job=state,
                work_dir=self.drafts.directory(draft_id),
            )
            refreshed_audit = _merge_audits(draft.get("audit") or {}, fresh_engine_audit)
            draft = {**draft, "audit": refreshed_audit, "validated_at": now_iso()}
            self.drafts.save(draft_id, draft)
            if not refreshed_audit.get("valid"):
                raise ApiError(
                    409,
                    "线上数据或第一阶段覆盖已不满足当前口径，请重新预检。",
                    code="preflight_stale_or_invalid",
                    details={"errors": refreshed_audit.get("errors") or []},
                )
            # Bind the exact approved snapshot immediately before queuing it.
            self.jobs.attach_online_draft(job_id, draft, self.drafts.directory(draft_id))

            def queue(state_value: dict[str, Any]) -> None:
                state_value.update({"status": "queued", "stage": "online", "error": None})
                state_value["progress"] = {
                    "phase": "queued",
                    "current": 0,
                    "total": None,
                    "percent": None,
                    "message": "线上指标已就绪，等待三维分类。",
                }

            self.jobs.update(job_id, queue)
            self._spawn(job_id, lambda: self._run_online(job_id))
        return {"job_id": job_id}

    def _spawn(self, job_id: str, target: Callable[[], None]) -> None:
        def wrapped() -> None:
            try:
                target()
            finally:
                with self._threads_lock:
                    self._threads.pop(job_id, None)

        with self._threads_lock:
            existing = self._threads.get(job_id)
            if existing is not None and existing.is_alive():
                raise ApiError(409, "任务已在运行", code="job_busy")
            thread = threading.Thread(
                target=wrapped, name=f"analysis-{job_id}", daemon=True
            )
            self._threads[job_id] = thread
            thread.start()

    def _callbacks(self, job_id: str, stage: str) -> tuple[Callable[..., None], Callable[..., None]]:
        def progress(*args: Any, **kwargs: Any) -> None:
            percent: float | None = None
            message = ""
            phase = str(kwargs.get("phase") or stage)
            current = kwargs.get("current")
            total = kwargs.get("total")
            if args and isinstance(args[0], Mapping):
                value = args[0]
                percent = value.get("percent")
                current = value.get("current", current)
                total = value.get("total", total)
                message = str(value.get("message") or "")
                phase = str(value.get("phase") or phase)
            elif args:
                if isinstance(args[0], (int, float)):
                    percent = float(args[0])
                elif args[0] is not None:
                    message = str(args[0])
                if len(args) > 1 and args[1] is not None:
                    message = str(args[1])
                if len(args) > 2:
                    current = args[1]
                    total = args[2]
                    if len(args) > 3:
                        message = str(args[3])
            if kwargs.get("percent") is not None:
                percent = float(kwargs["percent"])
            if kwargs.get("message") is not None:
                message = str(kwargs["message"])
            if percent is not None:
                percent = max(0.0, min(100.0, percent))

            def mutate(state: dict[str, Any]) -> None:
                state["progress"] = {
                    "phase": phase,
                    "current": current,
                    "total": total,
                    "percent": percent,
                    "message": " ".join(message.split())[:1000],
                }

            self.jobs.update(job_id, mutate)

        def log(*args: Any, **kwargs: Any) -> None:
            message = kwargs.get("message")
            if message is None and args:
                message = args[0]
            level = str(kwargs.get("level") or (args[1] if len(args) > 1 else "info"))
            if message is not None:
                self.jobs.append_log(job_id, str(message), level=level)

        return progress, log

    def _mark_running(self, job_id: str, stage: str, message: str) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> None:
            state.update({"status": "running", "stage": stage, "error": None})
            state["progress"] = {
                "phase": stage,
                "current": 0,
                "total": None,
                "percent": 0,
                "message": message,
            }

        state = self.jobs.update(job_id, mutate)
        self.jobs.append_log(job_id, message)
        return state

    def _run_player(self, job_id: str) -> None:
        stage = "player"
        try:
            state = self._mark_running(job_id, stage, "开始还原回放并计算实时行为指标。")
            progress, log = self._callbacks(job_id, stage)
            result = self.engine.run_player(
                source_files=[Path(path) for path in state["source_files"]["player"]],
                settings=state["settings"]["player"],
                work_dir=self.jobs.directory(job_id),
                output_dir=self.jobs.output_directory(job_id),
                progress_callback=progress,
                log_callback=log,
            )
            self._complete_stage(job_id, stage, result)
        except Exception as exc:
            self._fail_stage(job_id, stage, exc)

    def _run_online(self, job_id: str) -> None:
        stage = "online"
        try:
            state = self._mark_running(job_id, stage, "开始聚合线上消耗与失败流失，生成三维分类。")
            progress, log = self._callbacks(job_id, stage)
            result = self.engine.run_online(
                source_files=[Path(path) for path in state["source_files"]["online"]],
                settings=state["settings"]["online"],
                player_job=state,
                work_dir=self.jobs.directory(job_id),
                output_dir=self.jobs.output_directory(job_id),
                progress_callback=progress,
                log_callback=log,
            )
            self._complete_stage(job_id, stage, result)
        except Exception as exc:
            self._fail_stage(job_id, stage, exc)

    def _complete_stage(self, job_id: str, stage: str, result: Mapping[str, Any]) -> None:
        safe_result = dict(json_safe(result))
        artifacts, generated_previews = _normalize_artifacts(job_id, safe_result, self.jobs, stage)
        result_summary = safe_result.get("summary")
        if not isinstance(result_summary, Mapping):
            result_summary = safe_result.get("counts") if isinstance(safe_result.get("counts"), Mapping) else {}
        if stage == "player":
            result_summary = dict(result_summary)
            for key in ("first_level_label_counts", "coverage", "audit", "workbook_audit"):
                if key in safe_result:
                    result_summary[key] = safe_result[key]
        if stage == "online" and isinstance(safe_result.get("metadata"), Mapping):
            result_summary = {**dict(result_summary), **dict(safe_result["metadata"])}
        result_previews = safe_result.get("previews")
        if not isinstance(result_previews, Mapping):
            result_previews = {}

        def mutate(state: dict[str, Any]) -> None:
            state["status"] = "succeeded"
            state["stage"] = f"{stage}_completed"
            state["progress"] = {
                "phase": f"{stage}_completed",
                "current": 1,
                "total": 1,
                "percent": 100,
                "message": "分析完成，结果已写入本地输出目录。",
            }
            state.setdefault("engine_results", {})[stage] = safe_result
            if stage == "player":
                state["summary"] = {"player": dict(result_summary)}
            else:
                state.setdefault("summary", {})["online"] = dict(result_summary)
            state.setdefault("previews", {}).update(json_safe(result_previews))
            state["previews"].update(generated_previews)
            existing = {
                str(item.get("relative_path")): item
                for item in state.get("artifacts") or []
                if isinstance(item, Mapping)
            }
            for artifact in artifacts:
                existing[str(artifact["relative_path"])] = artifact
            state["artifacts"] = list(existing.values())
            state["error"] = None

        self.jobs.update(job_id, mutate)
        self.jobs.append_log(job_id, "分析完成，可预览并下载结果。")

    def _fail_stage(self, job_id: str, stage: str, error: Exception) -> None:
        trace_path = self.jobs.directory(job_id) / f"{stage}_traceback.txt"
        trace_path.write_text(traceback.format_exc(), encoding="utf-8")
        message = " ".join(str(error).split())[:1200] or "未知错误"
        code = "engine_unavailable" if isinstance(error, EngineUnavailableError) else type(error).__name__

        def mutate(state: dict[str, Any]) -> None:
            state["status"] = "failed"
            state["stage"] = f"{stage}_failed"
            state["error"] = {"code": code, "message": message, "stage": stage}
            state["progress"] = {
                "phase": f"{stage}_failed",
                "current": None,
                "total": None,
                "percent": None,
                "message": message,
            }

        self.jobs.update(job_id, mutate)
        self.jobs.append_log(job_id, message, level="error")

    def artifact_path(self, job_id: str, artifact_id: str) -> tuple[Path, Mapping[str, Any]]:
        state = self.jobs.load(job_id)
        item = next(
            (
                value
                for value in state.get("artifacts") or []
                if isinstance(value, Mapping) and value.get("id") == artifact_id
            ),
            None,
        )
        if item is None:
            raise FileNotFoundError("结果文件不存在")
        output_dir = self.jobs.output_directory(job_id).resolve()
        path = (output_dir / str(item.get("relative_path"))).resolve(strict=True)
        try:
            path.relative_to(output_dir)
        except ValueError as exc:
            raise FileNotFoundError("结果文件越界") from exc
        if not path.is_file():
            raise FileNotFoundError("结果文件不存在")
        return path, item


class AnalysisRequestHandler(BaseHTTPRequestHandler):
    server_version = "PlayerBehaviourLocal/1.0"

    @property
    def application(self) -> Application:
        return self.server.application  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        try:
            path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
            if path == "/api/bootstrap":
                self._send_json(200, self.application.bootstrap())
                return
            if path == "/api/health":
                self._send_json(
                    200,
                    {
                        "status": "ready",
                        "version": APP_VERSION,
                        "app": APP_NAME,
                        "service_id": SERVICE_ID,
                        "protocol": SERVICE_PROTOCOL,
                    },
                )
                return
            match = _JOB_ROUTE.fullmatch(path)
            if match:
                self._send_json(200, _public_job(self.application.jobs.load(match["job"])))
                return
            match = _DOWNLOAD_ROUTE.fullmatch(path)
            if match:
                self._send_download(match["job"], match["artifact"])
                return
            self._send_static(path, head_only=False)
        except Exception as exc:
            self._handle_exception(exc)

    def do_HEAD(self) -> None:  # noqa: N802
        try:
            path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
            self._send_static(path, head_only=True)
        except Exception as exc:
            self._handle_exception(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._check_csrf()
            payload = self._read_json()
            path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
            if path == "/api/preflight/player":
                self._send_json(200, self.application.preflight_player(payload))
                return
            if path == "/api/jobs/player":
                self._send_json(202, self.application.start_player(payload))
                return
            if path == "/api/preflight/online":
                self._send_json(200, self.application.attach_online_preflight(payload))
                return
            match = _ONLINE_ROUTE.fullmatch(path)
            if match:
                self._send_json(202, self.application.start_online(match["job"], payload))
                return
            raise ApiError(404, "接口不存在", code="not_found")
        except Exception as exc:
            self._handle_exception(exc)

    def _check_csrf(self) -> None:
        supplied = self.headers.get("X-CSRF-Token", "")
        if not supplied or not secrets.compare_digest(supplied, self.application.csrf_token):
            raise ApiError(403, "CSRF 校验失败，请刷新页面后重试。", code="csrf_failed")
        fetch_site = self.headers.get("Sec-Fetch-Site", "").lower()
        if fetch_site == "cross-site":
            raise ApiError(403, "拒绝跨站请求", code="cross_site_request")

    def _read_json(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ApiError(415, "请使用 application/json", code="unsupported_media_type")
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "")
        except ValueError as exc:
            raise ApiError(411, "缺少有效的 Content-Length", code="length_required") from exc
        if length <= 0:
            raise ApiError(400, "请求内容为空", code="empty_body")
        if length > MAX_JSON_BODY:
            raise ApiError(413, "请求超过 256 MB 限制", code="body_too_large")
        data = self.rfile.read(length)
        if len(data) != length:
            raise ApiError(400, "请求内容未完整接收", code="incomplete_body")
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, "JSON 格式无效", code="invalid_json") from exc
        if not isinstance(payload, dict):
            raise ApiError(400, "JSON 根节点必须是对象", code="invalid_json_root")
        return payload

    def _send_json(self, status: int, value: Any) -> None:
        data = json.dumps(json_safe(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_download(self, job_id: str, artifact_id: str) -> None:
        path, item = self.application.artifact_path(job_id, artifact_id)
        filename = str(item.get("filename") or path.name)
        ascii_name = "".join(char if char.isascii() and char not in '"\\' else "_" for char in filename)
        ascii_name = ascii_name or "download"
        encoded_name = urllib.parse.quote(filename, safe="")
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Type", str(item.get("content_type") or "application/octet-stream"))
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header(
            "Content-Disposition",
            f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}",
        )
        self.end_headers()
        with path.open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                self.wfile.write(block)

    def _send_static(self, request_path: str, *, head_only: bool) -> None:
        if request_path in {"", "/"}:
            path = self.application.tool_root / "index.html"
            if not path.is_file():
                path = self.application.tool_root / "frontend" / "index.html"
        elif request_path == "/favicon.ico":
            self.send_response(204)
            self._security_headers()
            self.end_headers()
            return
        else:
            relative = request_path.lstrip("/")
            if relative.startswith("assets/"):
                path = self.application.tool_root / "frontend" / relative
            else:
                path = self.application.tool_root / relative
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.application.tool_root)
        except (OSError, ValueError) as exc:
            raise ApiError(404, "页面不存在", code="not_found") from exc
        if not resolved.is_file() or resolved.suffix.lower() not in _STATIC_TYPES:
            raise ApiError(404, "页面不存在", code="not_found")
        data = resolved.read_bytes()
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Type", _STATIC_TYPES[resolved.suffix.lower()])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Last-Modified", formatdate(resolved.stat().st_mtime, usegmt=True))
        # This is a local, mutable tool; always revalidate assets so an update
        # cannot leave the browser running an older JS/CSS contract for an hour.
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
            "form-action 'self'; script-src 'self'; style-src 'self'; font-src 'self'; "
            "img-src 'self' data:; connect-src 'self'",
        )

    def _handle_exception(self, error: Exception) -> None:
        if isinstance(error, ApiError):
            status, payload = error.status, error.as_dict()
        elif isinstance(error, SourceValidationError):
            status, payload = 400, error.as_dict()
        elif isinstance(error, FileNotFoundError):
            status, payload = 404, {"code": "not_found", "message": str(error)}
        elif isinstance(error, (ValueError, TypeError)):
            status, payload = 400, {"code": "invalid_request", "message": str(error)}
        else:
            status = 500
            payload = {"code": "internal_error", "message": "本地服务处理失败，请查看终端日志。"}
            traceback.print_exc()
        try:
            self._send_json(
                status,
                {
                    "error": payload,
                    "code": payload.get("code"),
                    "message": payload.get("message"),
                },
            )
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format: str, *args: Any) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        sys.stderr.write(f"[{timestamp}] {self.address_string()} {format % args}\n")


class LocalThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], application: Any = None):
        self.application = application
        super().__init__(server_address, AnalysisRequestHandler)


def _loopback_host(value: str) -> str:
    normalized = str(value).strip().lower()
    if normalized in {"127.0.0.1", "localhost"}:
        return "127.0.0.1"
    raise argparse.ArgumentTypeError("出于数据安全，服务只能绑定 localhost/127.0.0.1")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="玩家行为分析工具本地服务")
    parser.add_argument("--host", type=_loopback_host, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--open", action="store_true", dest="open_browser", help="启动后打开浏览器")
    group.add_argument("--no-open", action="store_false", dest="open_browser", help="启动后不打开浏览器")
    parser.set_defaults(open_browser=False)
    return parser


def _server_url(host: str, port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def _read_local_json(
    host: str,
    port: int,
    path: str,
    *,
    timeout: float,
    max_bytes: int,
) -> Optional[tuple[Mapping[str, Any], str]]:
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != HTTPStatus.OK:
            return None
        content_type = response.getheader("Content-Type", "").lower()
        if not content_type.startswith("application/json"):
            return None
        content_length = response.getheader("Content-Length")
        if content_length and int(content_length) > max_bytes:
            return None
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            return None
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, Mapping):
            return None
        return payload, response.getheader("Server", "")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    finally:
        connection.close()


def _is_existing_tool(host: str, port: int, *, timeout: float = 0.8) -> bool:
    """Return True only when the occupied address is this local tool."""

    health_response = _read_local_json(
        host,
        port,
        "/api/health",
        timeout=timeout,
        max_bytes=4096,
    )
    if not health_response:
        return False
    health, server_header = health_response
    if health.get("status") != "ready":
        return False
    if (
        health.get("service_id") == SERVICE_ID
        and health.get("protocol") == SERVICE_PROTOCOL
    ):
        return True
    # Narrow compatibility path for the already-running 1.0.0 service that
    # predates SERVICE_ID.  The direct client never follows redirects.
    return bool(
        server_header.split(" ", 1)[0] == "PlayerBehaviourLocal/1.0"
        and dict(health) == {"status": "ready", "version": APP_VERSION}
    )


def _wait_for_existing_tool(host: str, port: int) -> bool:
    for attempt in range(3):
        if _is_existing_tool(host, port, timeout=0.45):
            return True
        if attempt < 2:
            time.sleep(0.15)
    return False


def _safe_open_browser(url: str) -> bool:
    try:
        opened = bool(webbrowser.open(url))
    except (OSError, webbrowser.Error):
        opened = False
    if not opened:
        print(f"未能自动打开浏览器，请手动访问：{url}", flush=True)
    return opened


def _reuse_existing_tool(url: str, *, open_browser: bool) -> int:
    print(f"检测到玩家行为分析工具已经在运行：{url}", flush=True)
    print("已复用现有服务，无需重复启动。", flush=True)
    if open_browser:
        _safe_open_browser(url)
    return 0


def serve(*, host: str, port: int, open_browser: bool) -> int:
    if not 0 <= port <= 65535:
        raise ValueError("端口必须在 0–65535 之间")
    requested_url = _server_url(host, port) if port else ""
    try:
        server = LocalThreadingHTTPServer((host, port))
    except OSError as error:
        if error.errno != errno.EADDRINUSE:
            raise
        if requested_url and _wait_for_existing_tool(host, port):
            return _reuse_existing_tool(requested_url, open_browser=open_browser)
        print(
            f"无法启动：端口 {port} 已被其他程序占用。",
            file=sys.stderr,
            flush=True,
        )
        print(
            "请关闭占用该端口的程序，或在终端使用 --port 指定其他端口。",
            file=sys.stderr,
            flush=True,
        )
        return 2
    try:
        server.application = Application()
    except BaseException:
        server.server_close()
        raise
    actual_port = int(server.server_address[1])
    url = _server_url(host, actual_port)
    print(f"玩家行为分析工具已启动：{url}", flush=True)
    print("数据仅在本机处理。按 Ctrl+C 停止服务。", flush=True)
    if open_browser:
        threading.Timer(0.35, lambda: _safe_open_browser(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\n正在停止本地服务……", flush=True)
    finally:
        server.server_close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return serve(host=args.host, port=args.port, open_browser=args.open_browser)


if __name__ == "__main__":
    raise SystemExit(main())
