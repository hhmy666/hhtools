"""Safe ingestion and lightweight inspection of local CSV payloads."""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import io
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence


MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_FILE_COUNT = 6
MAX_PASTE_BYTES = 64 * 1024 * 1024
_MAX_ENCODED_BYTES = ((MAX_FILE_BYTES + 2) // 3) * 4 + 8
_SAFE_FILENAME = re.compile(r"^[^/\\\x00\r\n]{1,180}$")
DEFAULT_LEVEL_TILE_GIT_REVISION = "5022d099293ea280ee709b68d875817cc43ddebc"
DEFAULT_CONFIG_LEVEL_GIT_REVISION = "4eac5d2105d316e3b0666702f4ccd916ea925281"


class SourceValidationError(ValueError):
    """A source payload cannot be safely persisted or parsed."""

    def __init__(self, message: str, *, code: str = "invalid_source", details: Any = None):
        super().__init__(message)
        self.code = code
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.details is not None:
            payload["details"] = self.details
        return payload


def _safe_filename(value: object, *, fallback: str | None = None) -> str:
    name = str(value or "").strip()
    if not name and fallback:
        name = fallback
    if (
        not name
        or name in {".", ".."}
        or name.startswith(".")
        or not _SAFE_FILENAME.fullmatch(name)
        or Path(name).name != name
        or not name.lower().endswith(".csv")
    ):
        raise SourceValidationError(
            "文件名不安全或不是 CSV 文件",
            code="unsafe_filename",
            details={"name": name},
        )
    return name


def normalize_settings(
    raw: object,
    *,
    default_config_path: Path,
    default_template_path: Path,
) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise SourceValidationError("设置必须是 JSON 对象", code="invalid_settings")
    # Keep small forward-compatible scalar settings for the analysis engine,
    # while preventing an upload-sized object from being duplicated in state.
    settings: dict[str, Any] = {}
    if len(raw) > 64:
        raise SourceValidationError("设置项过多", code="invalid_settings")
    for key, value in raw.items():
        clean_key = str(key)
        if len(clean_key) > 80:
            raise SourceValidationError("设置名过长", code="invalid_settings")
        if isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, str) and len(value) > 4096:
                raise SourceValidationError(
                    f"设置 {clean_key} 过长", code="invalid_settings"
                )
            settings[clean_key] = value
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            values = list(value)
            if len(values) > 200 or any(not isinstance(item, (str, int, float, bool, type(None))) for item in values):
                raise SourceValidationError(
                    f"设置 {clean_key} 格式不支持", code="invalid_settings"
                )
            settings[clean_key] = values
        else:
            raise SourceValidationError(
                f"设置 {clean_key} 格式不支持", code="invalid_settings"
            )

    for key, default in (
        ("config_path", default_config_path),
        ("template_path", default_template_path),
    ):
        value = str(settings.get(key, default) or "").strip()
        if "\x00" in value or len(value) > 2048:
            raise SourceValidationError(f"{key} 路径无效", code="invalid_settings")
        settings[key] = str(Path(value).expanduser().resolve(strict=False)) if value else ""

    # The browser contract uses *_path.  Keep the engine's historical *_dir
    # aliases in the same immutable settings snapshot during migration.
    settings["config_dir"] = settings["config_path"]
    settings["template_dir"] = settings["template_path"]

    history_ref = str(settings.get("history_ref") or "historical").strip().lower()
    if history_ref in {"historical", "default", "fixed"}:
        settings["history_ref"] = "historical"
        settings["history_mode"] = "historical"
        settings["level_tile_git_revision"] = DEFAULT_LEVEL_TILE_GIT_REVISION
        settings["config_level_git_revision"] = DEFAULT_CONFIG_LEVEL_GIT_REVISION
    elif history_ref in {"current", "working_tree", "latest"}:
        settings["history_ref"] = "current"
        settings["history_mode"] = "current"
        settings["level_tile_git_revision"] = ""
        settings["config_level_git_revision"] = ""
    else:
        raise SourceValidationError(
            "history_ref 只能是 historical 或 current；不接受未审计的自定义 Git 引用。",
            code="invalid_history_ref",
        )
    return settings


def path_audit(settings: Mapping[str, Any], *, require_paths: bool) -> dict[str, Any]:
    results: dict[str, Any] = {}
    errors: list[dict[str, str]] = []
    for key, label in (("config_path", "配置目录"), ("template_path", "模板目录")):
        value = str(settings.get(key) or "")
        path = Path(value)
        exists = bool(value) and path.exists()
        is_dir = path.is_dir() if exists else False
        readable = os.access(path, os.R_OK) if exists else False
        results[key] = {
            "path": value,
            "exists": exists,
            "is_directory": is_dir,
            "readable": readable,
            "status": "ready" if exists and is_dir and readable else "missing",
        }
        if require_paths and not (exists and is_dir and readable):
            errors.append(
                {
                    "code": f"{key}_unavailable",
                    "message": f"{label}不存在、不是目录或不可读：{path}" if value else f"请先到设置填写{label}",
                }
            )
    return {"paths": results, "errors": errors}


def _decode_uploaded_file(item: Mapping[str, Any], *, index: int) -> tuple[str, bytes]:
    name = _safe_filename(item.get("name"))
    encoded = item.get("content_base64", item.get("data_base64"))
    if not isinstance(encoded, str):
        raise SourceValidationError(
            f"{name} 缺少 content_base64", code="missing_file_content"
        )
    if len(encoded) > _MAX_ENCODED_BYTES:
        raise SourceValidationError(
            f"{name} 超过单文件 64 MB 限制", code="file_too_large"
        )
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SourceValidationError(
            f"{name} 的 Base64 内容无效", code="invalid_base64"
        ) from exc
    if not data:
        raise SourceValidationError(f"{name} 是空文件", code="empty_file")
    if len(data) > MAX_FILE_BYTES:
        raise SourceValidationError(
            f"{name} 超过单文件 64 MB 限制", code="file_too_large"
        )
    declared = item.get("size")
    if isinstance(declared, (int, float)) and int(declared) != len(data):
        raise SourceValidationError(
            f"{name} 的声明大小与实际内容不一致",
            code="file_size_mismatch",
            details={"declared": int(declared), "actual": len(data), "index": index},
        )
    return name, data


def _source_items(source: object, *, paste_name: str) -> list[tuple[str, bytes]]:
    if not isinstance(source, Mapping):
        raise SourceValidationError("缺少数据来源", code="missing_source")
    kind = str(source.get("kind") or "").strip().lower()
    if kind == "files":
        files = source.get("files")
        if not isinstance(files, list) or not files:
            raise SourceValidationError("请至少选择一个 CSV", code="missing_files")
        if len(files) > MAX_FILE_COUNT:
            raise SourceValidationError(
                f"一次最多导入 {MAX_FILE_COUNT} 个 CSV", code="too_many_files"
            )
        decoded: list[tuple[str, bytes]] = []
        seen: set[str] = set()
        for index, item in enumerate(files):
            if not isinstance(item, Mapping):
                raise SourceValidationError("文件条目格式无效", code="invalid_file_item")
            name, data = _decode_uploaded_file(item, index=index)
            canonical = name.casefold()
            if canonical in seen:
                raise SourceValidationError(
                    f"存在重复文件名：{name}", code="duplicate_filename"
                )
            seen.add(canonical)
            decoded.append((name, data))
        return decoded
    if kind == "paste":
        content = source.get("content")
        if not isinstance(content, str) or not content.strip():
            raise SourceValidationError("粘贴内容为空", code="empty_paste")
        data = content.encode("utf-8")
        if len(data) > MAX_PASTE_BYTES:
            raise SourceValidationError(
                "粘贴内容超过 64 MB 限制", code="paste_too_large"
            )
        return [(_safe_filename(source.get("name"), fallback=paste_name), data)]
    raise SourceValidationError(
        "source.kind 必须是 files 或 paste", code="invalid_source_kind"
    )


def _decode_csv(data: bytes, *, name: str) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise SourceValidationError(
        f"{name} 不是可识别的 UTF-8/GB18030 CSV",
        code="unsupported_encoding",
    )


def inspect_csv_bytes(name: str, data: bytes) -> dict[str, Any]:
    text, encoding = _decode_csv(data, name=name)
    sample = text[:65536]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        raw_header = next(reader)
    except StopIteration as exc:
        raise SourceValidationError(f"{name} 没有表头", code="missing_header") from exc
    header = [str(value).strip().lstrip("\ufeff") for value in raw_header]
    if not header or not any(header):
        raise SourceValidationError(f"{name} 没有有效表头", code="missing_header")
    normalized = [value.casefold() for value in header if value]
    duplicates = sorted({value for value in normalized if normalized.count(value) > 1})
    if duplicates:
        raise SourceValidationError(
            f"{name} 表头存在重复列",
            code="duplicate_columns",
            details={"columns": duplicates},
        )
    row_count = 0
    malformed_row: int | None = None
    expected = len(header)
    try:
        for row_number, row in enumerate(reader, start=2):
            if not row or not any(str(value).strip() for value in row):
                continue
            row_count += 1
            if malformed_row is None and len(row) != expected:
                malformed_row = row_number
    except csv.Error as exc:
        raise SourceValidationError(
            f"{name} CSV 解析失败：{exc}", code="csv_parse_error"
        ) from exc
    return {
        "name": name,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "encoding": encoding,
        "delimiter": "tab" if delimiter == "\t" else delimiter,
        "columns": header,
        "column_count": len(header),
        "row_count": row_count,
        "first_malformed_row": malformed_row,
    }


def persist_source(
    source: object,
    *,
    destination: Path,
    paste_name: str,
) -> tuple[list[Path], dict[str, Any]]:
    destination.mkdir(parents=True, exist_ok=False)
    items = _source_items(source, paste_name=paste_name)
    paths: list[Path] = []
    audits: list[dict[str, Any]] = []
    schemas: set[tuple[str, ...]] = set()
    for index, (name, data) in enumerate(items, start=1):
        audit = inspect_csv_bytes(name, data)
        stored_name = f"{index:02d}_{name}"
        target = destination / stored_name
        target.write_bytes(data)
        paths.append(target)
        audit["stored_name"] = stored_name
        audits.append(audit)
        schemas.add(tuple(str(value).casefold() for value in audit["columns"]))
    source_audit = {
        "valid": len(schemas) == 1 and all(item["row_count"] > 0 for item in audits),
        "kind": str(source.get("kind")) if isinstance(source, Mapping) else "",
        "file_count": len(audits),
        "total_bytes": sum(item["size"] for item in audits),
        "total_rows": sum(item["row_count"] for item in audits),
        "schema_consistent": len(schemas) == 1,
        "files": audits,
        "errors": [],
        "warnings": [],
    }
    if len(schemas) != 1:
        source_audit["errors"].append(
            {
                "code": "schema_mismatch",
                "message": "多个 CSV 的表头不一致，请使用相同查询版本重新导出。",
            }
        )
    for item in audits:
        if item["row_count"] == 0:
            source_audit["errors"].append(
                {"code": "empty_csv", "message": f"{item['name']} 没有数据行。"}
            )
        if item["first_malformed_row"] is not None:
            source_audit["errors"].append(
                {
                    "code": "column_count_mismatch",
                    "message": (
                        f"{item['name']} 第 {item['first_malformed_row']} 行列数与表头不同；"
                        "为避免字段错位，已停止导入。"
                    ),
                }
            )
    source_audit["valid"] = not source_audit["errors"]
    return paths, source_audit


def json_safe(value: Any) -> Any:
    """Convert common engine return values to durable JSON data."""

    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [json_safe(item) for item in value]
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        return json_safe(as_dict())
    scalar = getattr(value, "item", None)
    if callable(scalar):
        try:
            return json_safe(scalar())
        except (TypeError, ValueError):
            pass
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return str(value)
    return value
