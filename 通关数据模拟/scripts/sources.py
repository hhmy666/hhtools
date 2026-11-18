"""Read-only template resolution and local preference / model loading."""

from __future__ import annotations

import base64
import binascii
import json
import os
import tempfile
from pathlib import Path

from scenario_inputs import clean_id

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "运行数据"
MODELS = ROOT / "resources" / "models"
FALLBACK_REPOSITORY = Path.home() / "tileleveltemplate" / "Template"
STANDARD_RELATIVE = Path("Tile/Assets/Resources/TileMatchLevelData")
MAX_TEMPLATE_BYTES = 2 * 1024 * 1024
if __package__:
    from .sim_core.template_reader import parse_level_bytes
else:
    from sim_core.template_reader import parse_level_bytes


def _json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def model_path() -> Path:
    return MODELS / "online-player-model.json"


def default_repository() -> str:
    prefs = _json(DATA / "settings.json")
    if isinstance(prefs.get("repository"), str) and prefs["repository"]:
        return prefs["repository"]
    return str(FALLBACK_REPOSITORY) if FALLBACK_REPOSITORY.is_dir() else ""


def repository_directory(raw: str) -> Path:
    try:
        path = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("找不到模板仓库目录，请重新选择或核对路径。") from exc
    if not path.is_dir():
        raise ValueError("模板仓库路径需指向目录。")
    for nested in (path / STANDARD_RELATIVE, path / "Template"):
        if nested.is_dir():
            return nested.resolve(strict=True)
    return path


def remember_repository(path: Path):
    DATA.mkdir(exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".settings-", dir=DATA)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump({"repository": str(path)}, file, ensure_ascii=False)
        os.replace(temp, DATA / "settings.json")
    finally:
        Path(temp).unlink(missing_ok=True)


def upload_templates(raw: list) -> dict:
    result = {}
    if len(raw) > 100:
        raise ValueError("一次最多上传 100 个模板文件。")
    for index, row in enumerate(raw, 1):
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not isinstance(row.get("data"), str):
            raise ValueError(f"第 {index} 个模板上传文件格式有误。")
        name = row["name"]
        if not name.endswith(".bytes") or Path(name).name != name:
            raise ValueError("模板文件须为 .bytes。关卡配置请使用 CSV、TSV 或文本文件。")
        encoded = row["data"]
        if len(encoded) > ((MAX_TEMPLATE_BYTES + 2) // 3) * 4:
            raise ValueError(f"{name} 超过模板文件大小限制。")
        try:
            data = base64.b64decode(encoded, validate=True)
            level = parse_level_bytes(data, source_name=name)
            template_id = clean_id(level.template_id)
        except (ValueError, binascii.Error) as exc:
            raise ValueError(f"无法读取模板 {name}：{exc}") from exc
        if template_id in result and level.sha256 != result[template_id].sha256:
            raise ValueError(f"上传了内部 ID 同为 {template_id} 的不同模板。")
        result[template_id] = level
    return result


def read_template(template_id: str, directory: Path | None):
    if directory is None:
        raise ValueError(f"{template_id} 未上传，请选择含有它的模板仓库。")
    name = f"{clean_id(template_id)}_m.bytes"
    try:
        path = (directory / name).resolve(strict=True)
        if not path.is_relative_to(directory) or not path.is_file() or path.stat().st_size > MAX_TEMPLATE_BYTES:
            raise ValueError("文件不在所选目录内，或不是可读取的模板。")
        return parse_level_bytes(path.read_bytes(), source_name=name)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValueError(f"无法从仓库读取 {name}：{exc}") from exc
