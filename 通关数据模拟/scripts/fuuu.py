"""Resolve the reported FUUU profile from the read-only ulinkconfig tables."""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

if __package__:
    from .config_paths import discover_config, require_config, normalize_config
else:
    from config_paths import discover_config, require_config, normalize_config

CONFIG = discover_config()
DDA_COLUMNS = ("LevelId", "DDALevelId1", "DDALevelId2", "DDALevelId3")


def _rows(path):
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            yield from csv.DictReader(stream)
    except OSError:
        return


@lru_cache(maxsize=4)
def _lookups(config, versions):
    fuuu = {}
    for row in _rows(config / "config_client_level_fuuu_dup.csv"):
        tag = row.get("LevelFuuuDup") or ""
        if not tag.isdecimal():
            continue
        # Mirrors GetReportFuuuValue: missing/invalid arrays yield reported 0.
        arrays = [row.get(key) or "" for key in ("TowerHelpGameProgressParam", "TowerHelpBagTileParam")]
        valid = all(value and all(part.lstrip("-").isdecimal() for part in value.split("|")) for value in arrays)
        fuuu[int(tag)] = int(tag) if valid else 0

    levels = defaultdict(set)
    for row in _rows(config / "config_level_list.csv"):
        number = row.get("LevelIndex") or ""
        tag = row.get("LevelFuuuDup") or ""
        if not number.isdecimal():
            continue
        report_value = fuuu.get(int(tag), 0) if tag.isdecimal() else 0
        columns = DDA_COLUMNS if row.get("DDAType") == "1" else DDA_COLUMNS[:1]
        for column in columns:
            for code in (row.get(column) or "").split("|"):
                if code.strip():
                    levels[code.strip()].add((int(number), report_value))

    templates = defaultdict(set)
    for row in _rows(config / "config_level.csv"):
        template = row.get("TemplateId") or ""
        code = row.get("LevelId") or ""
        if code in levels and template:
            templates[template].update(levels[code])
    return levels, templates, fuuu


def enrich(entry, config=None):
    config = normalize_config(config) if config is not None else require_config()
    versions = []
    for name in ("config_client_level_fuuu_dup.csv", "config_level_list.csv", "config_level.csv"):
        try:
            versions.append((config / name).stat().st_mtime_ns)
        except OSError:
            versions.append(None)
    levels, templates, profiles = _lookups(config, tuple(versions))
    if not profiles:
        return entry, "未找到本机 FUUU 配置；换牌只能使用客户端内置默认值。"
    matches = levels.get(entry.level_id, ()) if entry.level_id else templates.get(entry.template_id, ())
    if entry.level_index is not None:
        matches = {match for match in matches if match[0] == entry.level_index}
    report_values = {value for _, value in matches}
    if len(report_values) != 1:
        first = profiles.get(1, next(iter(profiles.values())))
        reason = "没有唯一可用的线上档位" if matches else "未在关卡列表中匹配"
        return replace(entry, fuuu=first), f"{entry.level_id or entry.template_id} {reason}；使用本地幸运值第 1 档。"
    numbers = {number for number, _ in matches}
    return replace(entry, fuuu=next(iter(report_values)),
                   level_index=entry.level_index or (next(iter(numbers)) if len(numbers) == 1 else None)), None
