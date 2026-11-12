"""Loopback-only template inspection and memory-only editing/repair/export."""

from __future__ import annotations

import base64
import binascii
from collections import Counter, OrderedDict
from copy import deepcopy
import csv
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import threading
import unicodedata
from urllib.parse import parse_qs, unquote, urlsplit
import zipfile

from .repair import validate_board as _validate_import


HOST = "127.0.0.1"
DEFAULT_PORT = 8877
VERSION = "1.4.0"
SERVICE_ID = "tile-family.full-cover-checker"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTANCE_ID = hashlib.sha256(str(PROJECT_ROOT).encode("utf-8")).hexdigest()[:24]
DEFAULT_TEMPLATE_ROOT = None
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "输出模板"
MAX_FILES = 500
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_BODY_BYTES = 64 * 1024 * 1024
MAX_SAVED_TASKS = 10
MAX_SAVED_EXPORTS = 3
MAX_EDIT_TILES = 20000
_ID_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,159}\Z")


class ApiError(Exception):
    def __init__(self, status, code, message, **details):
        self.status, self.code, self.message = status, code, message
        self.details = details
        super().__init__(message)


def _existing_root(value):
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ApiError(400, "invalid_root", "请填写现有模板目录的完整路径")
    try:
        root = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise ApiError(400, "invalid_root", "模板目录不存在或无法访问")
    if not root.is_dir():
        raise ApiError(400, "invalid_root", "模板路径需要是目录")
    return root


def _safe_read(root, filename):
    """Open only the exact leaf under the selected directory, with a hard bound."""
    candidate = root / filename
    try:
        target = candidate.resolve(strict=True)
        target.relative_to(root)
        if target.parent != root or not target.is_file():
            raise ValueError("模板不是所选目录中的普通文件")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(str(candidate), flags)
        with os.fdopen(descriptor, "rb") as source:
            raw = source.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        raise ValueError("未找到模板：" + filename)
    except (OSError, RuntimeError):
        raise ValueError("无法读取模板（请检查文件权限及符号链接）：" + filename)
    except ValueError:
        raise ValueError("模板路径不在所选目录内：" + filename)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("模板超过单文件32 MiB限制")
    return raw


def _summary(rows, total):
    result = {"total": total, "issue": 0, "clean": 0, "error": 0,
              "adjacent": 0, "floating": 0}
    for row in rows:
        result[row["status"]] += 1
        result["adjacent"] += row.get("adjacent_count", 0)
        result["floating"] += row.get("floating_count", 0)
    return result


def _csv_safe(value):
    # Names and IDs can come from uploads; keep spreadsheets from executing them.
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def _name_key(value):
    return unicodedata.normalize("NFKC", value).casefold()


def _source_id(name):
    if name.lower().endswith("_m.bytes"):
        return name[:-8]
    if name.lower().endswith(".bytes"):
        return name[:-6]
    return name


def _safe_export_name(value):
    normalized = unicodedata.normalize("NFKC", value)
    if any(unicodedata.category(char).startswith("C") for char in normalized):
        raise ApiError(400, "invalid_pattern", "新模板名称不能包含控制符或无效文字")
    if (not value or value != value.strip() or value in (".", "..")
            or len(value.encode("utf-8")) > 220
            or normalized.endswith((".", " "))
            or any(ord(char) < 32 or ord(char) == 127 or char in '/\\<>:"|?*{}' for char in normalized)
            or normalized.lower().endswith(".bytes")
            or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", normalized)):
        raise ApiError(400, "invalid_pattern", "新模板名称需要是普通文件名，不能包含路径、保留名或.bytes后缀")
    return value


def _human_reason(error, fallback):
    reason = str(error) or type(error).__name__
    return reason if re.search(r"[\u4e00-\u9fff]", reason) else fallback


def export_csv(rows, detail):
    """Every input is represented, including clean templates and decode failures."""
    if detail not in ("coordinates", "templates"):
        raise ApiError(400, "invalid_detail", "导出类型需要是coordinates或templates")
    common = ["name", "status", "template_id", "tile_count", "tower_count",
              "adjacent_count", "floating_count", "coordinate_count"]
    coordinates = ["kind", "x", "y", "lower_layer", "upper_layer"]
    columns = common + (coordinates if detail == "coordinates" else []) + ["error"]
    labels = {"name": "文件名", "status": "评估状态", "template_id": "模板ID",
              "tile_count": "牌总数", "tower_count": "塔数量",
              "adjacent_count": "邻层一压一对数", "floating_count": "悬空全遮盖对数",
              "coordinate_count": "坐标组数", "kind": "全遮盖类型", "x": "X", "y": "Y",
              "lower_layer": "下方层", "upper_layer": "上方层", "error": "失败原因"}
    statuses = {"issue": "存在全遮盖", "clean": "未发现全遮盖", "error": "读取失败"}
    kinds = {"adjacent": "邻层一压一", "floating": "悬空全遮盖"}
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, [labels[key] for key in columns], lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        base = {key: row.get(key, "") for key in common + ["error"]}
        issues = row.get("issues", []) if detail == "coordinates" else []
        for issue in issues or [{}]:
            record = dict(base, **issue)
            record["status"] = statuses.get(record.get("status"), record.get("status", ""))
            record["kind"] = kinds.get(record.get("kind"), record.get("kind", ""))
            writer.writerow({labels[key]: _csv_safe(record.get(key, "")) for key in columns})
    return output.getvalue().encode("utf-8-sig")


class Application:
    def __init__(self, template_root=DEFAULT_TEMPLATE_ROOT, frontend_root=None):
        # A missing default directory is visible in context, not a startup crash:
        # file uploads remain usable and the user can select their actual root.
        self.template_root = Path(template_root).expanduser().resolve() if template_root else None
        self.frontend_root = Path(frontend_root or PROJECT_ROOT / "frontend").resolve()
        self.csrf_token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.tasks = OrderedDict()
        self.active_id = None

    def context(self):
        return {"version": VERSION, "service_id": SERVICE_ID, "instance_id": INSTANCE_ID,
                "template_root": str(self.template_root or ""),
                "template_root_exists": bool(self.template_root and self.template_root.is_dir()),
                "output_dir": str(DEFAULT_OUTPUT_ROOT), "template_dir": str(self.template_root or ""),
                "tile_dir": "",
                "csrf_token": self.csrf_token,
                "limits": {"files": MAX_FILES, "file_bytes": MAX_FILE_BYTES,
                           "body_bytes": MAX_BODY_BYTES, "active_tasks": 1}}

    def submit(self, payload):
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_request", "任务需要是JSON对象")
        mode = payload.get("mode")
        inputs = []
        root = None
        if mode == "ids":
            ids = payload.get("ids")
            if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_FILES:
                raise ApiError(400, "invalid_ids", "每批需要1至500个模板ID")
            for identifier in ids:
                if not isinstance(identifier, str) or not _ID_PATTERN.fullmatch(identifier):
                    raise ApiError(400, "invalid_id", "模板ID只允许字母、数字、下划线、短横线和点，且不能含路径")
                inputs.append((identifier, None))
            root = _existing_root(payload.get("template_root", self.template_root))
        elif mode == "files":
            files = payload.get("files")
            if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
                raise ApiError(400, "invalid_files", "每批需要1至500个模板文件")
            total_bytes = 0
            for item in files:
                if not isinstance(item, dict):
                    raise ApiError(400, "invalid_file", "上传文件项格式错误")
                name, encoded = item.get("name"), item.get("base64")
                if (not isinstance(name, str) or not name or len(name) > 255
                        or name in (".", "..") or any(character in name for character in "/\\\0\r\n")):
                    raise ApiError(400, "invalid_file", "上传文件名无效")
                if not isinstance(encoded, str):
                    raise ApiError(400, "invalid_base64", "上传文件内容需要是Base64文本")
                if len(encoded) > 4 * ((MAX_FILE_BYTES + 2) // 3):
                    raise ApiError(413, "file_too_large", "单文件不能超过32 MiB")
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error):
                    raise ApiError(400, "invalid_base64", "上传文件内容不是有效Base64")
                if len(raw) > MAX_FILE_BYTES:
                    raise ApiError(413, "file_too_large", "单文件不能超过32 MiB")
                total_bytes += len(raw)
                if total_bytes > MAX_BODY_BYTES:
                    raise ApiError(413, "batch_too_large", "本批文件合计超过64 MiB")
                inputs.append((name, raw))
        else:
            raise ApiError(400, "invalid_mode", "任务模式需要是ids或files")
        task_id = secrets.token_hex(12)
        task = {"task_id": task_id, "status": "running", "completed": 0,
                "total": len(inputs), "current": inputs[0][0], "rows": [],
                "states": [], "exports": OrderedDict(), "repair": None, "last_operation": None}
        with self.lock:
            if self.active_id is not None:
                raise ApiError(409, "task_running", "已有任务正在识别，已为你接回当前进度",
                               task_id=self.active_id)
            while len(self.tasks) >= MAX_SAVED_TASKS:
                disposable = next((key for key, saved in self.tasks.items() if not self._busy(saved)), None)
                if disposable is None:
                    raise ApiError(409, "tasks_busy", "已有任务正在修复或导出，请完成后再导入")
                del self.tasks[disposable]
            self.tasks[task_id] = task
            self.active_id = task_id
        threading.Thread(target=self._run, args=(task, inputs, root), daemon=True,
                         name="covercheck-" + task_id[:8]).start()
        return task_id

    def _run(self, task, inputs, root):
        try:
            from .engine import analyze_bytes
            for name, uploaded in inputs:
                with self.lock:
                    task["current"] = name
                raw = None
                validation_errors, validation_warnings = [], []
                exportable = False
                try:
                    raw = _safe_read(root, name + "_m.bytes") if root is not None else uploaded
                    row = dict(analyze_bytes(raw, name))
                    for tile in row["tiles"]:
                        tile["source_index"] = tile["index"]
                    row.update(name=name, status="issue" if row["issues"] else "clean")
                    checked = _validate_import(raw)
                    validation_errors = checked["errors"]
                    validation_warnings = checked["warnings"]
                    row["support_issues"] = checked["analysis"].get("support_issues", [])
                except Exception as error:
                    detail = str(error) or type(error).__name__
                    message = detail if re.search(r"[\u4e00-\u9fff]", detail) else "模板文件无法解析，请确认是关卡编辑器导出的 _m.bytes 文件。"
                    row = {"name": name, "status": "error", "error": message, "error_detail": detail}
                with self.lock:
                    task["rows"].append(row)
                    task["states"].append({"original_raw": raw, "current_raw": raw,
                        "original_board": deepcopy(row), "revision": 0, "edited": False, "undo_history": [],
                        "translation": {"dx": 0, "dy": 0},
                        "source_indices": [tile["index"] for tile in row.get("tiles", [])],
                        "repair_status": "not_run", "repair_reason": "", "repair_metrics": {}, "validation_errors": validation_errors,
                        "validation_warnings": validation_warnings,
                        "exportable": exportable})
                    task["completed"] += 1
            with self.lock:
                task["status"] = "succeeded"
        except Exception as error:
            with self.lock:
                task["status"] = "failed"
                task["error"] = "识别任务未能完成：" + (str(error) or type(error).__name__)
        finally:
            # Source/current bytes stay private in memory; no source is ever rewritten.
            with self.lock:
                task["current"] = ""
                self.active_id = None

    def snapshot(self, task_id):
        with self.lock:
            task = self._task(task_id)
            result = {key: deepcopy(value) for key, value in task.items()
                      if key not in ("rows", "states", "exports", "repair", "last_operation")}
            result["rows"] = []
            for index, row in enumerate(task["rows"]):
                public = {key: deepcopy(value) for key, value in row.items() if key not in ("tiles", "towers")}
                public.update(self._metadata(task["states"][index]))
                result["rows"].append(public)
            result["summary"] = _summary(task["rows"], task["total"])
            result["operation"] = self._operation(task)
            return result

    def _task(self, task_id):
        task = self.tasks.get(task_id)
        if task is None:
            raise ApiError(404, "task_not_found", "任务不存在或已从本次会话中移除，请重新识别")
        return task

    def row(self, task_id, index):
        with self.lock:
            return self._row(self._task(task_id), index)

    @staticmethod
    def _metadata(state):
        return dict(can_undo=bool(state.get("undo_history")), repair_metrics=deepcopy(state.get("repair_metrics", {})), translation=deepcopy(state.get("translation", {"dx": 0, "dy": 0})), **{
            key: deepcopy(state[key]) for key in ("revision", "edited", "repair_status",
                "repair_reason", "exportable", "validation_errors", "validation_warnings")})

    def _row(self, task, index):
        if index < 0 or index >= len(task["rows"]):
            raise ApiError(404, "row_not_found", "该结果尚不存在")
        row = deepcopy(task["rows"][index])
        row.update(self._metadata(task["states"][index]))
        row["original_board"] = deepcopy(task["states"][index]["original_board"])
        return row

    @staticmethod
    def _busy(task):
        if task["status"] == "running":
            return "task_running"
        if task.get("repair") and task["repair"]["status"] == "running":
            return "repair_running"
        if any(export["status"] == "running" for export in task.get("exports", {}).values()):
            return "export_running"
        return None

    @staticmethod
    def _operation(task):
        """Expose only the current/latest job's recoverable UI state, never bytes."""
        descriptor = task.get("last_operation")
        if task.get("repair") and task["repair"]["status"] == "running":
            descriptor = {"kind": "repair"}
        else:
            for export_id, job in reversed(task.get("exports", {}).items()):
                if job["status"] == "running":
                    descriptor = {"kind": "export", "export_id": export_id}
                    break
        if not descriptor:
            return None
        kind = descriptor["kind"]
        job = (task.get("repair") if kind == "repair" else
               task.get("exports", {}).get(descriptor.get("export_id")))
        if not job:
            return None
        fields = ("status", "total", "completed", "current", "rows", "export_id",
                  "files", "skipped", "summary", "error", "mode", "targets", "deliveries", "warnings")
        return dict(kind=kind, **{key: deepcopy(job[key]) for key in fields if key in job})

    def _ready(self, task):
        busy = self._busy(task)
        if busy:
            messages = {"task_running": "识别完成后可修改或导出", "repair_running": "自动修复正在进行，请完成后再操作",
                        "export_running": "新模板正在打包，请完成后再操作"}
            raise ApiError(409, busy, messages[busy], operation=self._operation(task))

    @staticmethod
    def _indices(task, payload):
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_request", "请求需要是JSON对象")
        indices = payload.get("indices", list(range(len(task["rows"]))))
        if (not isinstance(indices, list) or not indices
                or any(type(index) is not int or not 0 <= index < len(task["rows"]) for index in indices)
                or len(set(indices)) != len(indices)):
            raise ApiError(400, "invalid_indices", "请选择有效且不重复的结果行")
        return indices

    def edit(self, task_id, index, payload):
        from .repair import rewrite_tiles, validate_board
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_request", "编辑内容需要是JSON对象")
        tiles, revision = payload.get("tiles"), payload.get("revision")
        if type(revision) is not int or revision < 0:
            raise ApiError(400, "invalid_revision", "请提供当前模板的修订版本后保存")
        translation = payload.get("translation")
        if translation is not None and (not isinstance(translation, dict)
                or set(translation) != {"dx", "dy"}
                or any(type(translation[key]) is not int or abs(translation[key]) > 100000
                       for key in ("dx", "dy"))):
            raise ApiError(400, "invalid_translation", "平移偏移需要提供整数X/Y格数")
        if (not isinstance(tiles, list) or not 0 <= len(tiles) <= MAX_EDIT_TILES
                or any(not isinstance(tile, dict) or any(type(tile.get(key)) is not int
                    or not -(1 << 31) <= tile[key] < (1 << 31) for key in ("x", "y", "z", "tile_id")) for tile in tiles)):
            raise ApiError(400, "invalid_tiles", "牌数据需要包含整数坐标、层级及牌型，且不能超过20000张")
        with self.lock:
            task = self._task(task_id)
            self._ready(task)
            self._row(task, index)
            state = task["states"][index]
            if revision != state["revision"]:
                raise ApiError(409, "revision_conflict", "模板已更新，请重新打开最新版本后再保存", revision=state["revision"])
            if task["rows"][index]["status"] == "error":
                raise ApiError(422, "invalid_board", "原模板无法读取，不能编辑", errors=[task["rows"][index]["error"]])
            try:
                raw = (rewrite_tiles(state["current_raw"], tiles, towers=payload["towers"])
                       if "towers" in payload else rewrite_tiles(state["current_raw"], tiles))
                checked = validate_board(raw)
            except Exception as error:
                reason = _human_reason(error, "牌数据无法转换为有效模板，请检查坐标和牌型")
                raise ApiError(422, "invalid_board", reason, errors=[reason])
            if not checked["valid"]:
                raise ApiError(422, "invalid_board", "；".join(checked["errors"]), errors=checked["errors"])
            old_origins = state.get("source_indices", list(range(len(task["rows"][index]["tiles"]))))
            origins = [old_origins[tile["index"]] if tile.get("index") is not None and tile["index"] >= 0 else None
                       for tile in tiles]
            self._replace(task, index, raw, checked, "edited", source_indices=origins)
            if translation is not None:
                state["translation"] = dict(translation)
            return self._row(task, index)

    def undo_edit(self, task_id, index, payload):
        from .repair import validate_board
        revision = payload.get("revision") if isinstance(payload, dict) else None
        if type(revision) is not int or revision < 0:
            raise ApiError(400, "invalid_revision", "请提供当前模板的修订版本后撤回")
        with self.lock:
            task = self._task(task_id)
            self._ready(task)
            self._row(task, index)
            state = task["states"][index]
            if revision != state["revision"]:
                raise ApiError(409, "revision_conflict", "模板已更新，请重新打开最新版本后再撤回", revision=state["revision"])
            history = state.get("undo_history", [])
            if not history:
                raise ApiError(409, "nothing_to_undo", "没有可撤回的已保存操作")
            previous = history.pop()
            checked = validate_board(previous["raw"])
            # Undo may restore the original problem board; it must not pretend
            # that a formerly invalid source is now an exportable repair.
            self._replace(task, index, previous["raw"], checked, previous["repair_status"],
                          record_history=False, source_indices=previous["source_indices"])
            state["repair_reason"] = previous["repair_reason"]
            state["repair_metrics"] = deepcopy(previous.get("repair_metrics", {}))
            state["translation"] = deepcopy(previous.get("translation", {"dx": 0, "dy": 0}))
            state["revision"] += 1
            task["last_operation"] = None
            return self._row(task, index)

    def _replace(self, task, index, raw, checked, repair_status, record_history=True, source_indices=None):
        state = task["states"][index]
        old_origins = state.get("source_indices", list(range(len(task["rows"][index].get("tiles", [])))))
        if source_indices is None:
            source_indices = old_origins[:]
        if len(source_indices) != len(checked["analysis"]["tiles"]):
            raise ValueError("牌来源映射不完整，原模板已保留")
        if record_history and state["current_raw"] != raw:
            history = state.setdefault("undo_history", [])
            history.append({"raw": state["current_raw"], "repair_status": state["repair_status"],
                            "repair_reason": state["repair_reason"], "repair_metrics": deepcopy(state.get("repair_metrics", {})),
                            "translation": deepcopy(state.get("translation", {"dx": 0, "dy": 0})),
                            "source_indices": old_origins[:]})
            del history[:-50]
        analysis = dict(checked["analysis"])
        for tile, origin in zip(analysis["tiles"], source_indices):
            tile["source_index"] = origin
        analysis.update(name=task["rows"][index]["name"], status="issue" if analysis["issues"] else "clean")
        if record_history and (repair_status == "edited" or state["current_raw"] != raw):
            state["revision"] += 1
        if repair_status == "edited" or state["current_raw"] != raw:
            for job in task["exports"].values():
                if job["status"] == "succeeded":
                    job.update(status="stale", error="模板已修改，请重新导出当前版本")
                    job.pop("data", None)
        state.update(current_raw=raw, source_indices=source_indices[:], edited=raw != state["original_raw"], repair_status=repair_status,
                     repair_reason="", repair_metrics={}, exportable=checked["valid"] and raw != state["original_raw"],
                     validation_errors=list(checked["errors"]), validation_warnings=list(checked["warnings"]))
        task["rows"][index] = analysis
        if repair_status == "edited" and (task.get("last_operation") or {}).get("kind") == "export":
            # A refreshed page must not offer an older ZIP as the current revision.
            task["last_operation"] = None

    def repair(self, task_id, payload):
        with self.lock:
            task = self._task(task_id)
            self._ready(task)
            indices = self._indices(task, payload)
            task["repair"] = {"status": "running", "total": len(indices), "completed": 0, "current": "",
                              "rows": [{"index": index, "name": task["rows"][index]["name"], "status": "pending",
                                        "reason": "", "removed_count": 0, "added_count": 0} for index in indices]}
            task["last_operation"] = {"kind": "repair"}
            result = deepcopy(task["repair"])
        threading.Thread(target=self._run_repair, args=(task,), daemon=True,
                         name="repair-" + task_id[:8]).start()
        return result

    def _run_repair(self, task):
        job = task["repair"]
        try:
            from .repair import auto_repair, validate_board
            from .lowimpact import audit_repair
            for position in range(job["total"]):
                with self.lock:
                    item = job["rows"][position]
                    index = item["index"]
                    job["current"] = item["name"]
                    item["status"] = "running"
                    raw = task["states"][index]["current_raw"]
                    state = task["states"][index]
                    original_raw = state["original_raw"]
                    input_origins = state.get("source_indices", list(range(len(task["rows"][index].get("tiles", [])))))[:]
                try:
                    metrics = {}
                    if task["rows"][index]["status"] == "error" or raw is None:
                        raise ValueError("原模板无法读取，不能自动修复")
                    result = auto_repair(raw)
                    status = result.get("status")
                    if status in ("repaired", "unchanged"):
                        candidate = result.get("raw", raw)
                        checked = validate_board(candidate)
                        if not checked["valid"] or checked["analysis"]["issues"]:
                            raise ValueError("修复结果未通过独立核验，原模板已保留")
                        mapping = result.get("origin_indices") if status == "repaired" else list(range(len(input_origins)))
                        current_audit = audit_repair(raw, candidate, mapping)
                        if not current_audit["valid"]:
                            raise ValueError("修复未通过低扰动核验，当前修订已保留：" + "；".join(current_audit["errors"]))
                        origins = [input_origins[origin] if origin is not None else None for origin in mapping]
                        cumulative = audit_repair(original_raw, candidate, origins)
                        if not cumulative["valid"]:
                            raise ValueError("相对导入原模板的累计改动不满足限制，当前修订已保留：" + "；".join(cumulative["errors"]))
                        metrics = cumulative["metrics"]
                        metrics["strategy"] = result.get("strategy", "unchanged" if status == "unchanged" else "verified")
                        metrics["search_complete"] = bool(result.get("search_complete", False))
                        with self.lock:
                            self._replace(task, index, candidate, checked, status, source_indices=origins)
                    elif status != "unrepairable":
                        raise ValueError("自动修复未返回有效结果，原模板已保留")
                    if status == "repaired":
                        reason = ("低扰动修复已核验：问题牌改动{}张，其他普通牌改动{}张，塔牌改动{}张，总牌量{:+d}张。"
                                  "非塔表面与外轮廓保持；仅代表结构核验，不保证线上体验数据不变。".format(
                                      metrics["problem_changed_count"], metrics["hidden_changed_count"],
                                      metrics["tower_changed_count"], metrics["tile_delta"]))
                        if not metrics["search_complete"]:
                            reason += "已找到合规方案；限定搜索未穷尽，不声称全局最优。"
                    else:
                        reason = str(result.get("reason") or ("当前约束下未找到可用补位，原模板已保留" if status == "unrepairable" else ""))
                    removed, added = result.get("removed_count", 0), result.get("added_count", 0)
                except Exception as error:
                    status = "unrepairable"
                    reason = _human_reason(error, "自动修复失败，原模板已保留")
                    removed = added = 0
                    metrics = {}
                with self.lock:
                    state = task["states"][index]
                    state.update(repair_status=status, repair_reason=reason, repair_metrics=metrics)
                    item.update(status=status, reason=reason, removed_count=removed, added_count=added, metrics=deepcopy(metrics))
                    job["completed"] += 1
            with self.lock:
                job["status"] = "succeeded"
        except Exception as error:
            with self.lock:
                job.update(status="failed", error=_human_reason(error, "批量修复未完成，已完成的结果仍可使用"))
        finally:
            with self.lock:
                job["current"] = ""

    def repair_snapshot(self, task_id):
        with self.lock:
            task = self._task(task_id)
            return deepcopy(task["repair"] or {"status": "idle", "total": 0, "completed": 0, "current": "", "rows": []})

    def preview_delivery(self, task_id, payload):
        """Prepare a read-only, revision-bound delivery manifest asynchronously."""
        with self.lock:
            task = self._task(task_id)
            self._ready(task)
            indices = self._indices(task, payload)
            pattern = payload.get("pattern", "fixed{原模板ID}")
            if not isinstance(pattern, str):
                raise ApiError(400, "invalid_pattern", "请填写新模板命名格式")
            _safe_export_name(pattern.replace("{原模板ID}", "sample"))
            sync = payload.get("sync_targets", [])
            if (not isinstance(sync, list)
                    or any(type(value) is not str or value not in ("template", "tile") for value in sync)
                    or len(set(sync)) != len(sync)):
                raise ApiError(400, "invalid_targets", "请选择模板仓或编辑器仓作为同步目标")
            targets = {"output": payload.get("output_dir", str(DEFAULT_OUTPUT_ROOT))}
            for name in sync:
                targets[name] = payload.get(name + "_dir", str(self.template_root or "") if name == "template" else "")
            if any(not isinstance(value, str) or not value.strip() or not Path(value).is_absolute() for value in targets.values()):
                raise ApiError(400, "invalid_targets", "请填写导出与同步目录的绝对路径")
            # macOS exposes /var and /tmp as OS-owned aliases. Expand just
            # those prefixes; never resolve a user-supplied symlink directory.
            targets = {name: '/private' + value if value.startswith(('/var/', '/tmp/')) else value
                       for name, value in targets.items()}
            while len(task["exports"]) >= MAX_SAVED_EXPORTS:
                task["exports"].popitem(last=False)
            export_id = secrets.token_hex(12)
            job = {"export_id": export_id, "mode": "preview", "status": "running", "total": len(indices),
                   "completed": 0, "current": "", "files": [], "skipped": [], "rows": [], "targets": targets,
                   "summary": {"total": len(indices), "exported": 0, "skipped": 0}}
            task["exports"][export_id] = job
            task["last_operation"] = {"kind": "export", "export_id": export_id}
            result = deepcopy(job)
        threading.Thread(target=self._run_preview, args=(task, job, indices, targets, pattern), daemon=True).start()
        return result

    def _run_preview(self, task, job, indices, targets, pattern):
        try:
            from .delivery import plan_delivery
            from .repair import validate_board
            entries, revisions, warnings = [], {}, []
            for index in indices:
                with self.lock:
                    row, state = task["rows"][index], task["states"][index]
                    job["current"] = row["name"]
                    if row["status"] == "error" or not state["edited"]:
                        job["skipped"].append({"index": index, "name": row["name"],
                                               "reason": "模板无法读取" if row["status"] == "error" else "未修改原模板"})
                    else:
                        checked = validate_board(state["current_raw"])
                        try:
                            _safe_export_name(pattern.replace("{原模板ID}", row["template_id"]))
                            if not checked["valid"]:
                                raise ValueError("；".join(checked["errors"]))
                        except (ApiError, ValueError) as error:
                            job["skipped"].append({"index": index, "name": row["name"],
                                                   "reason": error.message if isinstance(error, ApiError) else str(error)})
                        else:
                            entries.append({"index": index, "name": row["name"],
                                            "template_id": row["template_id"], "raw": state["current_raw"]})
                            revisions[index] = state["revision"]
                            warnings.extend({"index": index, "name": row["name"], "message": value}
                                            for value in checked["warnings"])
                    job["completed"] += 1
            if not entries:
                raise ValueError("还没有已保存的修改。请先修改模板并保存，再预览导出。")
            reserved = [value for row in task["rows"] for value in
                        (row.get("template_id", ""), _source_id(row["name"])) if value]
            plan = plan_delivery(entries, targets, pattern, reserved_ids=reserved)
            with self.lock:
                job.update(status="succeeded", rows=plan["rows"], files=list(plan["files"]), targets=plan["targets"],
                           warnings=warnings, _plan=plan, _revisions=revisions)
                job["summary"].update(exported=len(plan["files"]), skipped=len(job["skipped"]))
        except Exception as error:
            with self.lock:
                job.update(status="failed", error=_human_reason(error, "预览导出失败，请检查目录与命名后重试"))
        finally:
            with self.lock:
                job["current"] = ""

    def write_delivery(self, task_id, export_id, payload):
        with self.lock:
            task = self._task(task_id)
            self._ready(task)
            job = self._export(task_id, export_id)
            if job["status"] != "succeeded" or job.get("mode") != "preview" or "_plan" not in job:
                raise ApiError(409, "preview_stale", "预览已变化或已写入，请重新预览导出")
            if any(task["states"][index]["revision"] != version for index, version in job["_revisions"].items()):
                raise ApiError(409, "preview_stale", "模板已修改，请重新预览导出")
            total = len(job["files"]) * len(job["targets"])
            job.update(mode="write", status="running", total=total, completed=0, current="", deliveries=[])
            result = deepcopy({key: value for key, value in job.items()
                               if key != "data" and not key.startswith("_")})
        threading.Thread(target=self._run_delivery, args=(job,), daemon=True).start()
        return result

    def _run_delivery(self, job):
        try:
            from .delivery import commit_delivery
            def progress(update):
                with self.lock:
                    job.update({key: update[key] for key in ("completed", "total", "current") if key in update})
            report = commit_delivery(job["_plan"], progress=progress)
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as package:
                for filename, raw in job["_plan"]["files"].items():
                    package.writestr(filename, raw)
            with self.lock:
                job.update(status="succeeded", data=buffer.getvalue(), deliveries=report["rows"],
                           completed=report["completed"], total=report["total"])
                if not report["ok"]:
                    job["error"] = "部分目录未写入，请查看逐目标结果；新模板ZIP仍可下载。"
        except Exception as error:
            with self.lock:
                job.update(status="failed", error=_human_reason(error, "写入失败，请重新预览后重试"))
        finally:
            with self.lock:
                job["current"] = ""

    def export(self, task_id, payload):
        with self.lock:
            task = self._task(task_id)
            self._ready(task)
            indices = self._indices(task, payload)
            pattern = payload.get("pattern", "fix{原模板ID}")
            if not isinstance(pattern, str):
                raise ApiError(400, "invalid_pattern", "请填写新模板命名格式")
            _safe_export_name(pattern.replace("{原模板ID}", "sample"))
            reserved = set()
            for row in task["rows"]:
                reserved.update(_name_key(value) for value in (row.get("template_id", ""), _source_id(row["name"])) if value)
            names = []
            for index in indices:
                row = task["rows"][index]
                identifier = row.get("template_id") or _source_id(row["name"])
                try:
                    desired = _safe_export_name(pattern.replace("{原模板ID}", identifier))
                except ApiError as error:
                    names.append((None, error.message))
                    continue
                candidate, version = desired, 1
                while _name_key(candidate) in reserved:
                    version += 1
                    candidate = desired + "_v" + str(version)
                try:
                    _safe_export_name(candidate)
                except ApiError as error:
                    names.append((None, error.message))
                    continue
                reserved.add(_name_key(candidate))
                names.append((candidate, ""))
            while len(task["exports"]) >= MAX_SAVED_EXPORTS:
                task["exports"].popitem(last=False)
            export_id = secrets.token_hex(12)
            job = {"export_id": export_id, "status": "running", "total": len(indices), "completed": 0,
                   "current": "", "files": [], "skipped": [], "summary": {"total": len(indices), "exported": 0, "skipped": 0}}
            task["exports"][export_id] = job
            task["last_operation"] = {"kind": "export", "export_id": export_id}
            result = deepcopy(job)
        threading.Thread(target=self._run_export, args=(task, job, indices, names), daemon=True,
                         name="export-" + export_id[:8]).start()
        return result

    def _run_export(self, task, job, indices, names):
        buffer = io.BytesIO()
        try:
            from .repair import rewrite_tiles, validate_board
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as package:
                for index, (new_id, naming_error) in zip(indices, names):
                    with self.lock:
                        row = task["rows"][index]
                        raw = task["states"][index]["current_raw"]
                        job["current"] = row["name"]
                    try:
                        if naming_error:
                            raise ValueError("新模板名称不安全，已跳过：" + naming_error)
                        if row["status"] == "error" or raw is None:
                            raise ValueError("原模板无法读取，未加入导出包")
                        if not task["states"][index]["edited"]:
                            raise ValueError("未修改原模板，未加入新模板导出")
                        checked = validate_board(raw)
                        if not checked["valid"]:
                            raise ValueError("模板未通过核验：" + "；".join(checked["errors"]))
                        candidate = rewrite_tiles(raw, checked["analysis"]["tiles"], new_id=new_id)
                        final = validate_board(candidate)
                        counts = Counter(tile["tile_id"] for tile in checked["analysis"]["tiles"])
                        if (not final["valid"] or final["analysis"]["template_id"] != new_id
                                or Counter(tile["tile_id"] for tile in final["analysis"]["tiles"]) != counts):
                            raise ValueError("命名后的模板未通过独立核验，未加入导出包")
                        filename = new_id + "_m.bytes"
                        package.writestr(filename, candidate)
                        with self.lock:
                            job["files"].append(filename)
                            job["summary"]["exported"] += 1
                    except Exception as error:
                        with self.lock:
                            job["skipped"].append({"index": index, "name": row["name"],
                                "reason": _human_reason(error, "模板未通过导出核验，已跳过")})
                            job["summary"]["skipped"] += 1
                    with self.lock:
                        job["completed"] += 1
            with self.lock:
                if job["files"]:
                    job.update(status="succeeded", data=buffer.getvalue())
                else:
                    job.update(status="failed", error="所选模板均未通过导出核验，请先修复或编辑")
        except Exception as error:
            with self.lock:
                job.update(status="failed", error=_human_reason(error, "新模板打包失败，请重试"))
        finally:
            with self.lock:
                job["current"] = ""

    def _export(self, task_id, export_id):
        job = self._task(task_id)["exports"].get(export_id)
        if job is None:
            raise ApiError(404, "export_not_found", "导出包不存在或已移除，请重新导出")
        return job

    def export_snapshot(self, task_id, export_id):
        with self.lock:
            return deepcopy({key: value for key, value in self._export(task_id, export_id).items()
                             if key != "data" and not key.startswith("_")})

    def download_export(self, task_id, export_id):
        with self.lock:
            job = self._export(task_id, export_id)
            if job["status"] == "running":
                raise ApiError(409, "export_running", "新模板仍在打包，请完成后下载")
            if job["status"] == "stale":
                raise ApiError(409, "export_stale", "模板已修改，请重新导出当前版本")
            if job["status"] != "succeeded" or "data" not in job:
                raise ApiError(409, "export_empty", job.get("error", "没有可导出的模板"))
            return job["data"]

    def download(self, task_id, detail):
        with self.lock:
            task = self._task(task_id)
            if task["status"] == "running":
                raise ApiError(409, "task_running", "识别完成后可下载完整结果")
            rows = list(task["rows"])
        return export_csv(rows, detail)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, application):
        super().__init__(address, RequestHandler)
        self.application = application


def create_server(address=(HOST, DEFAULT_PORT), template_root=DEFAULT_TEMPLATE_ROOT, frontend_root=None):
    if address[0] not in (HOST, "localhost"):
        raise ValueError("本地工具只能绑定127.0.0.1")
    return LocalServer((HOST, address[1]), Application(template_root, frontend_root))


class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        # Progress polling would otherwise overwhelm the launcher's terminal.
        pass

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_OPTIONS(self):
        self._handle("OPTIONS")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    @property
    def app(self):
        return self.server.application

    def _handle(self, method):
        try:
            self._validate_request(method)
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            if path.startswith("/api/"):
                self._api(method, path, parse_qs(parsed.query))
            elif method == "GET":
                self._static(path)
            else:
                raise ApiError(405, "method_not_allowed", "该地址不支持此请求方法")
        except ApiError as error:
            self._json(error.status, {"error": {"code": error.code, "message": error.message,
                                               **error.details}})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            self._json(500, {"error": {"code": "internal_error", "message": "本地服务处理失败，请重试"}})

    def _validate_request(self, method):
        port = self.server.server_port
        hosts = {"127.0.0.1:{}".format(port), "localhost:{}".format(port)}
        if self.headers.get("Host") not in hosts:
            raise ApiError(403, "host_rejected", "请通过当前本地服务地址访问")
        if method == "GET":
            return
        origins = {"http://" + host for host in hosts}
        origin = self.headers.get("Origin")
        referer = self.headers.get("Referer", "")
        if origin not in origins and not (origin is None and any(
                referer == allowed or referer.startswith(allowed + "/") for allowed in origins)):
            raise ApiError(403, "origin_rejected", "已拒绝跨站请求，请刷新工具页面")
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            raise ApiError(403, "origin_rejected", "已拒绝跨站请求")
        if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), self.app.csrf_token):
            raise ApiError(403, "csrf_failed", "页面令牌已失效，请刷新页面")

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(400, "invalid_body", "不支持分块请求")
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            raise ApiError(400, "invalid_body", "请求长度无效")
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            raise ApiError(413, "body_too_large", "本批请求超过64 MiB，请减少文件后重试")
        if length < 1:
            raise ApiError(400, "invalid_body", "请求内容为空")
        if self.headers.get_content_type() != "application/json":
            raise ApiError(415, "invalid_content_type", "请使用JSON提交任务")
        self.connection.settimeout(20)
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ApiError(400, "invalid_body", "请求内容不完整")
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise ApiError(400, "invalid_json", "请求不是有效UTF-8 JSON")

    def _api(self, method, path, query):
        if method == "GET" and path == "/api/health":
            self._json(200, {"service_id": SERVICE_ID, "instance_id": INSTANCE_ID, "version": VERSION})
            return
        if method == "GET" and path == "/api/context":
            self._json(200, self.app.context())
            return
        if path == "/api/tasks" and method == "POST":
            self._json(202, {"task_id": self.app.submit(self._body())})
            return
        edit = re.fullmatch(r"/api/tasks/([a-f0-9]{24})/rows/(\d+)/edit", path)
        undo = re.fullmatch(r"/api/tasks/([a-f0-9]{24})/rows/(\d+)/undo", path)
        repair = re.fullmatch(r"/api/tasks/([a-f0-9]{24})/repair", path)
        delivery_preview = re.fullmatch(r"/api/tasks/([a-f0-9]{24})/export/preview", path)
        delivery_write = re.fullmatch(r"/api/tasks/([a-f0-9]{24})/export/([a-f0-9]{24})/write", path)
        export = re.fullmatch(r"/api/tasks/([a-f0-9]{24})/export(?:/([a-f0-9]{24})(/status)?)?", path)
        if edit and method == "POST":
            self._json(200, self.app.edit(edit.group(1), int(edit.group(2)), self._body()))
            return
        if undo and method == "POST":
            self._json(200, self.app.undo_edit(undo.group(1), int(undo.group(2)), self._body()))
            return
        if repair and method in ("GET", "POST"):
            if method == "POST":
                self._json(202, self.app.repair(repair.group(1), self._body()))
            else:
                self._json(200, self.app.repair_snapshot(repair.group(1)))
            return
        if delivery_preview and method == "POST":
            self._json(202, self.app.preview_delivery(delivery_preview.group(1), self._body()))
            return
        if delivery_write and method == "POST":
            self._json(202, self.app.write_delivery(delivery_write.group(1), delivery_write.group(2), self._body()))
            return
        if export:
            task_id, export_id, status = export.groups()
            if method == "POST" and export_id is None:
                self._json(202, self.app.export(task_id, self._body()))
                return
            if method == "GET" and export_id:
                if status:
                    self._json(200, self.app.export_snapshot(task_id, export_id))
                else:
                    self._respond(200, self.app.download_export(task_id, export_id), "application/zip", {
                        "Content-Disposition": 'attachment; filename="covercheck-{}-new-templates.zip"'.format(task_id[:8])})
                return
        match = re.fullmatch(r"/api/tasks/([a-f0-9]{24})(?:/(download|errors\.json|rows/(\d+)))?", path)
        if method != "GET":
            raise ApiError(405, "method_not_allowed", "该接口不支持此请求方法")
        if match is None:
            raise ApiError(404, "not_found", "接口或任务不存在")
        task_id, action, index = match.groups()
        if action == "download":
            detail = query.get("detail", ["coordinates"])[0]
            data = self.app.download(task_id, detail)
            self._respond(200, data, "text/csv; charset=utf-8", {
                "Content-Disposition": 'attachment; filename="covercheck-{}-{}.csv"'.format(task_id[:8], detail)})
        elif action == "errors.json":
            task = self.app.snapshot(task_id)
            errors = [{"row_index": i, "name": row["name"], "error": row["error"]}
                      for i, row in enumerate(task["rows"]) if row["status"] == "error"]
            self._json(200, {"task_id": task_id, "errors": errors, "summary": task["summary"]}, {
                "Content-Disposition": 'attachment; filename="covercheck-{}-errors.json"'.format(task_id[:8])})
        elif index is not None:
            self._json(200, self.app.row(task_id, int(index)))
        else:
            self._json(200, self.app.snapshot(task_id))

    def _static(self, path):
        relative = "index.html" if path == "/" else path.lstrip("/")
        try:
            target = (self.app.frontend_root / relative).resolve(strict=True)
            target.relative_to(self.app.frontend_root)
            if not target.is_file():
                raise ValueError()
            data = target.read_bytes()
        except (OSError, ValueError, RuntimeError):
            raise ApiError(404, "not_found", "页面或资源不存在")
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        self._respond(200, data, content_type)

    def _json(self, status, payload, headers=None):
        self._respond(status, json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                      "application/json; charset=utf-8", headers)

    def _respond(self, status, data, content_type, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)
