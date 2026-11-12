"""Trusted-root validation and source resolution.

Only this module turns a Profile directory into a template path.  Normal job
payloads therefore carry template IDs, not arbitrary filesystem paths.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from puzzle_core.errors import InputValidationError


TEMPLATE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
STANDARD_TEMPLATE_RELATIVE = Path("Tile/Assets/Resources/TileMatchLevelData")


@dataclass(frozen=True)
class PathStatus:
    path: str | None
    configured: bool
    exists: bool
    readable: bool
    writable: bool
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "configured": self.configured,
            "exists": self.exists,
            "readable": self.readable,
            "writable": self.writable,
            "message": self.message,
        }


def validate_template_id(template_id: str) -> str:
    value = str(template_id).strip()
    if not value or not TEMPLATE_ID_PATTERN.fullmatch(value):
        raise InputValidationError(
            "模板 ID 只能包含英文字母、数字、下划线和连字符",
            evidence={"template_id": value},
        )
    if value in {".", ".."} or len(value.encode("utf-8")) > 180:
        raise InputValidationError(
            "模板 ID 不合法或过长", evidence={"template_id": value}
        )
    return value


def real_path(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def validate_source_roots(
    tile_repo_root: str | os.PathLike[str],
    template_dir: str | os.PathLike[str] | None = None,
) -> tuple[Path, Path]:
    repo = real_path(tile_repo_root)
    templates = real_path(template_dir) if template_dir else repo / STANDARD_TEMPLATE_RELATIVE
    templates = templates.resolve(strict=False)
    if not repo.is_dir():
        raise InputValidationError(
            "Tile 仓库目录不存在", evidence={"tile_repo_root": str(repo)}
        )
    if not templates.is_dir():
        raise InputValidationError(
            "模板目录不存在", evidence={"template_dir": str(templates)}
        )
    if not is_within(templates, repo):
        raise InputValidationError(
            "模板目录必须位于 Tile 仓库内",
            evidence={"tile_repo_root": str(repo), "template_dir": str(templates)},
        )
    if not os.access(repo, os.R_OK) or not os.access(templates, os.R_OK):
        raise InputValidationError("Tile 仓库或模板目录不可读")
    return repo, templates


def validate_output_root(
    output_root: str | os.PathLike[str],
    *,
    tile_repo_root: str | os.PathLike[str] | None = None,
    template_dir: str | os.PathLike[str] | None = None,
    create: bool = False,
) -> Path:
    output = real_path(output_root)
    if create:
        output.mkdir(parents=True, exist_ok=True)
    if not output.is_dir():
        raise InputValidationError(
            "输出目录不存在", evidence={"output_root": str(output)}
        )
    for label, source in (
        ("tile_repo_root", tile_repo_root),
        ("template_dir", template_dir),
    ):
        if source is None:
            continue
        source_path = real_path(source)
        if is_within(output, source_path) or is_within(source_path, output):
            raise InputValidationError(
                "输出目录不能与只读源目录互相包含",
                evidence={"output_root": str(output), label: str(source_path)},
            )
    if not os.access(output, os.W_OK):
        raise InputValidationError(
            "输出目录不可写", evidence={"output_root": str(output)}
        )
    return output


def resolve_template_path(template_dir: str | os.PathLike[str], template_id: str) -> Path:
    templates = real_path(template_dir)
    safe_id = validate_template_id(template_id)
    candidate = (templates / f"{safe_id}_m.bytes").resolve(strict=False)
    if not is_within(candidate, templates):
        raise InputValidationError("模板路径越过可信模板目录")
    if not candidate.is_file():
        raise FileNotFoundError(f"找不到模板文件：{safe_id}_m.bytes")
    if candidate.is_symlink():
        resolved = candidate.resolve(strict=True)
        if not is_within(resolved, templates):
            raise InputValidationError("模板符号链接越过可信模板目录")
        candidate = resolved
    if not os.access(candidate, os.R_OK):
        raise InputValidationError("模板文件不可读")
    return candidate


def inspect_path(path: str | os.PathLike[str] | None) -> PathStatus:
    if path is None:
        return PathStatus(None, False, False, False, False, "未配置")
    resolved = real_path(path)
    exists = resolved.exists()
    return PathStatus(
        str(resolved),
        True,
        exists,
        exists and os.access(resolved, os.R_OK),
        exists and os.access(resolved, os.W_OK),
        "可用" if exists else "路径不存在或已移动",
    )
