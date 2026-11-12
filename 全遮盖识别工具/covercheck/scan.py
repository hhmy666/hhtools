"""Scan an explicit local CSV snapshot; source files are opened read-only."""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sys

from .engine import analyze_bytes, csv_bytes, issue_text


DEFAULT_REPORT_DIR = Path(__file__).resolve().parents[1] / "运行数据" / "报告"
METHOD = {
    "candidate": "排除StackData锚点XY上的全部塔牌，其余所有TileData牌型参与识别。",
    "adjacent": "同XY、Z差1；用户层号=Z+1。",
    "floating": "同XY、Z差至少2；仅按同XY实际连续Z配对。严格中间任一Tile与下牌2×2牌面存在正面积重叠即排除，1/4、1/2或全部重叠均阻挡；仅边缘相接不阻挡。",
    "tower_projection": "中间塔牌亦参与阻挡；投影=锚点XY+方向向量×(16/85)×Z，0上(0,-1)、1左(-1,0)、2下(0,1)、3右(1,0)。使用整数85倍坐标精确判定边缘。",
    "interpretation": "依照用户指定结构口径识别；不表示真实局内某时刻必然无遮拦或可点击。",
    "config_selection": "DDAType=1取DDALevelId1/2/3，其余取LevelId；各字段以|切分并去首尾空白；精确LevelId关联TemplateId。",
    "deduplication": "相同LevelIndex与LevelId去重；每个模板仅解码一次；模板配对总数不按关卡引用数重复累计。",
    "anomalies": "缺失、冲突、非法路径、不可读、模板内ID不符或解码失败均另列，不能当作未命中。",
    "coordinate_count": "非塔重叠坐标组数=至少命中一对的唯一XY数，不等于命中层对数。",
}


def _snapshot(path, required):
    raw = path.read_bytes()
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    if not rows:
        raise ValueError("empty CSV: {}".format(path))
    header = [value.strip() for value in rows[0]]
    if len(header) != len(set(header)) or not set(required).issubset(header):
        raise ValueError("missing or duplicate CSV columns in {}".format(path))
    start = 3 if len(rows) > 1 and rows[1] and rows[1][0].strip() in ("int", "string", "float", "array") else 1
    records = []
    for line, row in enumerate(rows[start:], start + 1):
        if not row or not any(value.strip() for value in row):
            continue
        if len(row) > len(header):
            raise ValueError("CSV row {} exceeds header in {}".format(line, path))
        row = row + [""] * (len(header) - len(row))
        records.append((line, {key: value.strip() for key, value in zip(header, row)}))
    return records, {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "data_rows": len(records)}


def _tokens(value):
    return [token.strip() for token in value.split("|") if token.strip()]


def scan_sources(level_list, levels, templates, output_dir=DEFAULT_REPORT_DIR, progress=None):
    level_list, levels = Path(level_list).resolve(), Path(levels).resolve()
    templates, output_dir = Path(templates).resolve(), Path(output_dir).resolve()
    list_records, list_source = _snapshot(level_list, ("LevelIndex", "DDAType", "LevelId", "DDALevelId1", "DDALevelId2", "DDALevelId3"))
    level_records, level_source = _snapshot(levels, ("LevelId", "TemplateId"))
    exceptions = []

    def anomaly(kind, detail, *, index="", level_id="", template_id="", line=""):
        exceptions.append({"kind": kind, "level_index": index, "level_id": level_id,
                           "template_id": template_id, "line": line, "detail": str(detail)})

    by_index = {}
    for line, row in list_records:
        by_index.setdefault(row["LevelIndex"], []).append((line, row))
    refs = []
    for index, versions in by_index.items():
        if not index or not index.isdigit() or int(index) < 1:
            anomaly("invalid_level_index", "LevelIndex应为正整数", index=index, line=versions[0][0])
            continue
        signatures = {tuple(sorted(row.items())) for _, row in versions}
        if len(signatures) > 1:
            anomaly("conflicting_level_index", "同LevelIndex存在不同配置，未选择任一版本", index=index,
                    line="|".join(str(line) for line, _ in versions))
            continue
        line, row = versions[0]
        try:
            dda = int(row["DDAType"])
        except ValueError:
            anomaly("invalid_dda_type", "DDAType不是整数", index=index, line=line)
            continue
        columns = ("DDALevelId1", "DDALevelId2", "DDALevelId3") if dda == 1 else ("LevelId",)
        selected = list(dict.fromkeys(token for column in columns for token in _tokens(row[column])))
        if not selected:
            anomaly("empty_active_level", "当前分支没有LevelId", index=index, line=line)
        refs.extend({"level_index": index, "level_id": token, "line": line} for token in selected)
    by_id = {}
    for line, row in level_records:
        by_id.setdefault(row["LevelId"], []).append((line, row))
    template_refs = {}
    for ref in refs:
        matches = by_id.get(ref["level_id"], [])
        args = {"index": ref["level_index"], "level_id": ref["level_id"], "line": ref["line"]}
        if not matches:
            anomaly("missing_level", "config_level.csv无精确LevelId", **args)
        elif len({tuple(sorted(row.items())) for _, row in matches}) > 1:
            anomaly("conflicting_level", "同LevelId存在不同配置，未选择任一版本", **args)
        else:
            template_id = matches[0][1]["TemplateId"]
            if not template_id:
                anomaly("empty_template_id", "TemplateId为空", **args)
            else:
                template_refs.setdefault(template_id, []).append(ref)
    results, template_sources = {}, []
    total = len(template_refs)
    for completed, (template_id, associated) in enumerate(template_refs.items(), 1):
        relative = Path(template_id + "_m.bytes")
        candidate = templates / relative
        details = {"template_id": template_id, "level_index": "|".join(dict.fromkeys(ref["level_index"] for ref in associated)),
                   "level_id": "|".join(dict.fromkeys(ref["level_id"] for ref in associated))}

        def template_anomaly(kind, detail):
            anomaly(kind, detail, index=details["level_index"], level_id=details["level_id"], template_id=template_id)

        if (not template_id or template_id in (".", "..") or "/" in template_id or "\\" in template_id or "\0" in template_id
                or candidate.resolve().parent != templates):
            template_anomaly("unsafe_template_path", "模板ID或软链接离开指定模板目录")
        elif not candidate.is_file():
            template_anomaly("missing_template", str(candidate))
        else:
            try:
                raw = candidate.read_bytes()
            except OSError as error:
                template_anomaly("unreadable_template", error)
            else:
                template_sources.append({"template_id": template_id, "path": str(candidate),
                                         "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
                try:
                    result = analyze_bytes(raw, template_id)
                except (ValueError, TypeError) as error:
                    template_anomaly("decode_error", error)
                else:
                    if result["template_id"] != template_id:
                        template_anomaly("template_id_mismatch", "文件期望ID={}，内容ID={}".format(template_id, result["template_id"]))
                    else:
                        results[template_id] = result
        if progress:
            progress(completed, total, template_id)
    hit_rows, coordinate_rows = [], []
    for template_id, result in results.items():
        if not result["issues"]:
            continue
        for ref in template_refs[template_id]:
            hit_rows.append([template_id, ref["level_index"], ref["level_id"], result["coordinate_count"],
                             "是" if result["tower_count"] else "否", result["adjacent_count"], result["floating_count"],
                             issue_text(result["issues"])])
        for issue in result["issues"]:
            coordinate_rows.append([template_id, "相邻层全遮盖" if issue["kind"] == "adjacent" else "隔层悬空全遮盖",
                                    issue["x"], issue["y"], issue["lower_layer"], issue["upper_layer"],
                                    issue["lower_index"], issue["upper_index"]])
    hit_rows.sort(key=lambda row: (int(row[1]), row[2], row[0]))
    coordinate_rows.sort(key=lambda row: (row[0], row[2], row[3], row[4], row[5]))
    exception_rows = [[a["kind"], a["level_index"], a["level_id"], a["template_id"], a["line"], a["detail"]] for a in exceptions]
    reports = {"hit_levels": "命中关卡明细.csv", "coordinates": "模板坐标明细.csv", "exceptions": "扫描异常.csv", "manifest": "来源与口径.json"}
    totals = {"list_rows": len(list_records), "active_level_references": len(refs), "referenced_unique_level_ids": len({r["level_id"] for r in refs}),
              "referenced_templates": len(template_refs), "scanned_templates": len(results),
              "hit_templates": sum(bool(result["issues"]) for result in results.values()),
              "clean_templates": sum(not result["issues"] for result in results.values()),
              "hit_level_rows": len(hit_rows), "hit_level_indices": len({row[1] for row in hit_rows}),
              "adjacent_pairs": sum(result["adjacent_count"] for result in results.values()),
              "floating_pairs": sum(result["floating_count"] for result in results.values()),
              "coordinate_groups": sum(result["coordinate_count"] for result in results.values()),
              "exception_count": len(exceptions)}
    manifest = {"generated_at": datetime.now(timezone.utc).isoformat(), "scope": "用户提供的本地配置快照与模板目录，不能据此推断当前服务器部署状态",
                "sources": {"level_list": list_source, "levels": level_source, "templates_root": str(templates), "templates": template_sources},
                "methodology": METHOD, "totals": totals, "reports": reports, "exceptions": exceptions}
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / reports["hit_levels"]).write_bytes(csv_bytes(["模板名称", "LevelIndex", "LevelId", "非塔重叠坐标组数", "模板另含塔", "相邻层全遮盖组数", "隔层悬空全遮盖组数", "坐标明细"], hit_rows))
    (output_dir / reports["coordinates"]).write_bytes(csv_bytes(["模板名称", "识别类型", "X", "Y", "下层（从1开始）", "上层（从1开始）", "下层Tile索引（从0开始）", "上层Tile索引（从0开始）"], coordinate_rows))
    (output_dir / reports["exceptions"]).write_bytes(csv_bytes(["异常类型", "LevelIndex", "LevelId", "模板名称", "来源行", "说明"], exception_rows))
    (output_dir / reports["manifest"]).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--level-list", required=True, type=Path)
    parser.add_argument("--levels", required=True, type=Path)
    parser.add_argument("--templates", required=True, type=Path)
    parser.add_argument("--output-dir", default=DEFAULT_REPORT_DIR, type=Path)
    args = parser.parse_args(argv)
    try:
        result = scan_sources(args.level_list, args.levels, args.templates, args.output_dir)
    except (ValueError, OSError, UnicodeError, csv.Error) as error:
        parser.exit(2, "扫描失败：{}\n".format(error))
    print(json.dumps({"totals": result["totals"], "output_dir": str(args.output_dir.resolve())}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
