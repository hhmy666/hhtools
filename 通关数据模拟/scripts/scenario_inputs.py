"""Interpret config_level and pasted columns without guessing unmatched pairs."""

from __future__ import annotations

import csv
import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,126}\Z")
PARAMETER = re.compile(r"(?<![\w.\-])(\d{1,10})\s*\|\s*(\d{1,10})\s*\|\s*(\d{1,10})\s*\|\s*(\d{1,10})(?![\w.\-])")
RULE = re.compile(r"^[|\s:-]+$")
HEADERS = {"模板", "模板 id", "模板id", "参数", "关卡参数", "副本参数", "templateid", "duppara", "duppara1"}


@dataclass(frozen=True)
class Parameters:
    a_kinds: int
    seed: int
    luck: int
    b_kinds: int

    def __post_init__(self):
        if not (0 <= self.a_kinds <= 500 and 0 <= self.b_kinds <= 500
                and 0 <= self.seed <= 2_147_483_647 and 0 <= self.luck <= 2_147_483_647):
            raise ValueError("副本参数超出范围：图案种类数 0–500，Seed 0–2147483647，幸运值须为非负整数。")

    def __str__(self):
        return f"{self.a_kinds}|{self.seed}|{self.luck}|{self.b_kinds}"


@dataclass(frozen=True)
class Entry:
    template_id: str
    duplicate: int
    parameters: Parameters | None
    level_id: str | None = None
    strategy_id: int | None = None
    seed_group: int | None = None
    level_index: int | None = None
    fuuu: int | None = None


def clean_id(token: str) -> str:
    token = token.strip().strip("`").strip()
    if token.endswith("_m.bytes"):
        token = token[:-8]
    if not ID.fullmatch(token) or token in {".", ".."}:
        raise ValueError("模板 ID 只能使用字母、数字、点、下划线和短横线。")
    return token


def _normalize_header(cell: str) -> str:
    return re.sub(r"[\s_]+", "", cell.strip().lstrip("\ufeff").lower())


def _config_entries(text: str) -> list[Entry] | None:
    for delimiter in ("\t", ",", ";", "|"):
        records = iter(csv.reader(text.splitlines(), delimiter=delimiter))
        for header in records:
            names = [_normalize_header(cell) for cell in header]
            if "templateid" not in names or not any(
                name == "levelid" or re.fullmatch(r"duppara\d+", name) for name in names
            ):
                continue
            template_index = names.index("templateid")
            indexes = [(int(name[7:]), i) for i, name in enumerate(names)
                       if re.fullmatch(r"duppara[1-9]\d*", name)]
            out = []
            for row in records:
                def cell(i):
                    return row[i].strip() if i < len(row) else ""

                token = cell(template_index)
                if not token or token.lower() == "string" or token in {"模板 id", "模板ID", "模板id", "模板"}:
                    continue
                template_id = clean_id(token)
                level_id = (cell(names.index("levelid")) or None) if "levelid" in names else None
                extras = {}
                for col, key in (("strategygroupid", "strategy_id"), ("randomseedgroupid", "seed_group"), ("levelindex", "level_index")):
                    value = cell(names.index(col)) if col in names else ""
                    if value.isdecimal():
                        extras[key] = int(value)
                configured = []
                for duplicate, index in sorted(indexes):
                    value = cell(index).strip("`")
                    if not value:
                        continue
                    match = PARAMETER.fullmatch(value)
                    if not match:
                        raise ValueError(f"{template_id} 的副本 {duplicate} 参数无效，应为 A|Seed|幸运值|B。")
                    configured.append(Entry(template_id, duplicate, Parameters(*map(int, match.groups())), level_id, **extras))
                out.extend(configured or [Entry(template_id, 1, None, level_id, **extras)])
            if not out:
                raise ValueError("配置表中没有可用的 TemplateId。")
            return out
    return None


def parse_entries(text: str, upload_ids: Iterable[str] = ()) -> list[Entry]:
    text = text.lstrip("\ufeff").replace("\\|", "|").replace("｜", "|")
    uploaded = [clean_id(token) for token in upload_ids]
    configs = _config_entries(text)
    if configs is not None:
        ids = {entry.template_id for entry in configs}
        return configs + [Entry(token, 1, None) for token in uploaded if token not in ids]

    aligned: list[tuple[str, Parameters | None]] = []
    loose: list[Parameters] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or RULE.fullmatch(line) or line.strip("| `").casefold() in HEADERS:
            continue
        matches = list(PARAMETER.finditer(line))
        if not matches and line.strip("| `").count("|") >= 2 and line.strip("| `")[:1].isdecimal():
            raise ValueError("副本参数每组应正好包含四段整数：A|Seed|幸运值|B。")
        values = [Parameters(*map(int, match.groups())) for match in matches]
        tokens = []
        for raw in re.split(r'[\s,，;；|]+', PARAMETER.sub(" ", line)):
            part = raw.strip('"`')
            if not part or part.casefold() in HEADERS:
                continue
            if part.isdecimal() and values:
                raise ValueError("副本参数每组应正好包含四段：A|Seed|幸运值|B。")
            tokens.append(clean_id(part))
        if values and tokens:
            if len(values) != len(tokens):
                raise ValueError("同一行模板和副本参数的数量对不上。")
            aligned.extend(zip(tokens, values))
        elif tokens:
            aligned.extend((token, None) for token in tokens)
        else:
            loose.extend(values)
    ids = {token for token, _ in aligned}
    aligned.extend((token, None) for token in uploaded if token not in ids)
    if not aligned:
        raise ValueError("请输入模板 ID，或上传模板文件。")
    if loose:
        locations = [i for i, (_, params) in enumerate(aligned) if params is None]
        if len(locations) != len(loose):
            raise ValueError(f"有 {len(locations)} 个待配对模板 ID，但有 {len(loose)} 行独立副本参数。请让两列数量一致，或把参数贴在对应模板同一行。")
        for i, value in zip(locations, loose):
            aligned[i] = (aligned[i][0], value)
    counts = Counter()
    out = []
    for token, value in aligned:
        counts[token] += 1
        out.append(Entry(token, counts[token], value))
    return out
