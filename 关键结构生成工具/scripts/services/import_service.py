"""Read-only template loading and inspection for both supported input paths."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from puzzle_core.errors import InputValidationError, SourceParseError
from puzzle_core.geometry import build_static_geometry
from puzzle_core.search import STRUCTURE_ID_TO_TYPE
from puzzle_core.template_reader import (
    LevelTemplate,
    TemplateParseError,
    parse_level_bytes,
)
from puzzle_core.tower_reader import diagnose_towers

from .path_service import resolve_template_path, validate_source_roots
from .profile_service import Profile


MAX_SOURCE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class LoadedTemplate:
    level: LevelTemplate
    input_mode: str
    source_locator: str
    source_filename: str


@dataclass(frozen=True)
class TemplateInspection:
    template_id: str
    source_filename: str
    input_mode: str
    source_sha256: str
    byte_size: int
    tile_count: int
    a_count: int
    b_count: int
    existing_structure_counts: tuple[tuple[str, int], ...]
    max_z: int | None
    max_layer: int
    level_width: int | None
    level_height: int | None
    tower_count: int
    tower_member_count: int
    non_tower_tile_count: int
    non_tower_max_layer: int
    tower_a_count: int
    tower_b_count: int
    static_a_count: int
    static_b_count: int
    static_relation_count: int
    warnings: tuple[str, ...]
    tower_report: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_source(path: Path) -> bytes:
    source = path.expanduser().resolve(strict=True)
    if not source.is_file():
        raise InputValidationError("输入模板不是普通文件", evidence={"path": str(source)})
    size = source.stat().st_size
    if size <= 0 or size > MAX_SOURCE_BYTES:
        raise InputValidationError(
            "模板文件大小不在安全范围内",
            evidence={"size": size, "max_size": MAX_SOURCE_BYTES},
        )
    try:
        return source.read_bytes()
    except OSError as exc:
        raise InputValidationError("模板文件不可读", evidence={"path": str(source)}) from exc


def load_template_id(profile: Profile, template_id: str) -> LoadedTemplate:
    """Resolve exactly ``<template_id>_m.bytes`` under the configured root."""

    if (
        not profile.source_configured
        or profile.tile_repo_root is None
        or profile.template_dir is None
    ):
        raise InputValidationError("当前 Profile 尚未配置 Tile 仓库和模板目录")
    # Profile paths are durable preferences, not durable trust decisions.  A
    # directory can move, be replaced, lose permissions, or be edited between
    # runs; re-establish containment and readability before every ID lookup.
    _repo, templates = validate_source_roots(
        profile.tile_repo_root,
        profile.template_dir,
    )
    path = resolve_template_path(templates, template_id)
    data = _read_source(path)
    try:
        level = parse_level_bytes(data, source_name=path.name)
    except (TemplateParseError, ValueError) as exc:
        raise SourceParseError(
            "模板 protobuf 解析失败",
            evidence={"template_id": template_id, "source_filename": path.name},
        ) from exc
    if level.template_id != template_id:
        raise SourceParseError(
            "模板文件名与内部 TemplateId 不一致",
            evidence={
                "requested_template_id": template_id,
                "internal_template_id": level.template_id,
                "source_filename": path.name,
            },
        )
    return LoadedTemplate(
        level=level,
        input_mode="template_id",
        source_locator=path.name,
        source_filename=path.name,
    )


def load_template_file(path: Path, *, expected_template_id: str | None = None) -> LoadedTemplate:
    """Load a user-selected `_m.bytes` without modifying or retaining its path."""

    source = path.expanduser().resolve(strict=True)
    if not source.name.endswith("_m.bytes"):
        raise InputValidationError("拖入文件名必须以 _m.bytes 结尾")
    data = _read_source(source)
    try:
        level = parse_level_bytes(data, source_name=source.name)
    except (TemplateParseError, ValueError) as exc:
        raise SourceParseError(
            "模板 protobuf 解析失败",
            evidence={"source_filename": source.name},
        ) from exc
    if expected_template_id is not None and level.template_id != expected_template_id:
        raise SourceParseError(
            "拖入模板的内部 TemplateId 与批次模板键不一致",
            evidence={
                "expected_template_id": expected_template_id,
                "internal_template_id": level.template_id,
                "source_filename": source.name,
            },
        )
    return LoadedTemplate(
        level=level,
        input_mode="file",
        source_locator=source.name,
        source_filename=source.name,
    )


def inspect_loaded_template(source: LoadedTemplate) -> TemplateInspection:
    level = source.level
    counts = Counter(tile.tile_id for tile in level.tiles)
    existing = Counter(
        STRUCTURE_ID_TO_TYPE[tile.tile_id]
        for tile in level.tiles
        if tile.tile_id in STRUCTURE_ID_TO_TYPE
    )
    tower_report = diagnose_towers(level)
    static = build_static_geometry(level)
    tower_members = level.tower_member_indices
    return TemplateInspection(
        template_id=level.template_id,
        source_filename=source.source_filename,
        input_mode=source.input_mode,
        source_sha256=level.sha256,
        byte_size=len(level.data),
        tile_count=len(level.tiles),
        a_count=counts[-10],
        b_count=counts[-11],
        existing_structure_counts=tuple(sorted(existing.items())),
        max_z=level.max_z,
        max_layer=level.max_layer,
        level_width=level.level_width,
        level_height=level.level_height,
        tower_count=len(level.towers),
        tower_member_count=len(tower_members),
        non_tower_tile_count=len(level.tiles) - len(tower_members),
        non_tower_max_layer=max(
            (tile.z + 1 for tile in level.tiles if tile.index not in tower_members),
            default=0,
        ),
        tower_a_count=sum(
            tile.index in tower_members and tile.tile_id == -10 for tile in level.tiles
        ),
        tower_b_count=sum(
            tile.index in tower_members and tile.tile_id == -11 for tile in level.tiles
        ),
        static_a_count=sum(
            tile.index not in tower_members and tile.tile_id == -10 for tile in level.tiles
        ),
        static_b_count=sum(
            tile.index not in tower_members and tile.tile_id == -11 for tile in level.tiles
        ),
        static_relation_count=len(static.relations),
        warnings=tuple(level.warnings),
        tower_report=tower_report.as_dict(),
    )
