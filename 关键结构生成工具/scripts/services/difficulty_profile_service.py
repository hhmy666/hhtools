"""Read-only discovery of one template's DupPara1–10 difficulty profile."""

from __future__ import annotations

import csv
from pathlib import Path

from puzzle_core.difficulty import DifficultyProfile, DuplicateParameter

from .profile_service import Profile


_DUP_COLUMNS = tuple(f"DupPara{index}" for index in range(1, 11))
_MAX_TABLE_BYTES = 32 * 1024 * 1024


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _unavailable_profiles(
    template_ids: tuple[str, ...], warning: str
) -> dict[str, DifficultyProfile]:
    return {
        template_id: DifficultyProfile(coverage="missing", warning=warning)
        for template_id in template_ids
    }


def load_difficulty_profiles(
    profile: Profile, template_ids: tuple[str, ...]
) -> dict[str, DifficultyProfile]:
    """Read every requested template in one bounded, read-only table scan."""

    requested = tuple(dict.fromkeys(str(value) for value in template_ids))
    if not requested:
        return {}
    if not profile.ulinkconfig_root:
        return _unavailable_profiles(
            requested,
            "未配置 ulinkconfig 路径，难度模拟将使用本地参考档。",
        )
    try:
        root = Path(profile.ulinkconfig_root).expanduser().resolve(strict=True)
    except OSError:
        return _unavailable_profiles(
            requested,
            "ulinkconfig 路径不可读，难度模拟将使用本地参考档。",
        )
    if not root.is_dir():
        return _unavailable_profiles(
            requested,
            "ulinkconfig 路径不是目录，难度模拟将使用本地参考档。",
        )

    requested_set = set(requested)
    matches: dict[
        str, list[tuple[Path, str, tuple[DuplicateParameter, ...]]]
    ] = {template_id: [] for template_id in requested}
    for raw_path in sorted(root.rglob("config_level.csv"))[:64]:
        try:
            path = raw_path.resolve(strict=True)
            if (
                raw_path.is_symlink()
                or not path.is_file()
                or not _inside(path, root)
                or path.stat().st_size > _MAX_TABLE_BYTES
            ):
                continue
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.reader(stream))
        except (OSError, UnicodeError, csv.Error):
            continue
        if len(rows) < 3:
            continue
        header = [value.strip() for value in rows[0]]
        columns = {name: index for index, name in enumerate(header)}
        if not {"LevelId", "TemplateId", *_DUP_COLUMNS}.issubset(columns):
            continue
        width = len(header)
        for raw in rows[3:]:
            row = raw + [""] * max(0, width - len(raw))
            template_id = str(row[columns["TemplateId"]]).strip()
            if template_id not in requested_set:
                continue
            try:
                duplicate_params = tuple(
                    DuplicateParameter.parse(str(row[columns[name]]).strip())
                    for name in _DUP_COLUMNS
                )
            except (TypeError, ValueError):
                continue
            matches[template_id].append(
                (
                    path.relative_to(root),
                    str(row[columns["LevelId"]]).strip(),
                    duplicate_params,
                )
            )

    result: dict[str, DifficultyProfile] = {}
    for template_id in requested:
        template_matches = matches[template_id]
        if not template_matches:
            result[template_id] = DifficultyProfile(
                coverage="missing",
                source={"template_id": template_id, "match_count": 0},
                warning=(
                    f"所有可读 config_level.csv 中均未找到 {template_id}，"
                    "难度模拟将使用本地参考档。"
                ),
            )
            continue
        distinct = {
            tuple((value.a, value.seed, value.luck, value.b) for value in item[2])
            for item in template_matches
        }
        source = {
            "template_id": template_id,
            "match_count": len(template_matches),
            "matches": [
                {"config_level_path": str(path), "level_id": level_id}
                for path, level_id, _ in template_matches
            ],
        }
        if len(distinct) != 1:
            result[template_id] = DifficultyProfile(
                coverage="ambiguous",
                source=source,
                warning=(
                    f"{template_id} 在 config_level 中存在互相冲突的 DupPara1–10，"
                    "未静默选择其中一条；难度模拟改用本地参考档。"
                ),
            )
            continue
        result[template_id] = DifficultyProfile(
            duplicate_params=template_matches[0][2],
            coverage="complete",
            source=source,
        )
    return result


def load_difficulty_profile(
    profile: Profile, template_id: str
) -> DifficultyProfile:
    """Compatibility wrapper for callers that need a single template."""

    return load_difficulty_profiles(profile, (template_id,))[template_id]


__all__ = ["load_difficulty_profile", "load_difficulty_profiles"]
