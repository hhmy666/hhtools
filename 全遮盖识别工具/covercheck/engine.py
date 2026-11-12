"""Read-only full-cover detector using the user's footprint blocker rule.

All tiles at StackData anchors are normal tower tiles and excluded from the
candidate set. At each remaining XY, consecutive Z values form adjacent pairs
(gap 1) or floating candidates (gap >=2). Any positive-area overlap from an
intermediate tile excludes a floating pair; edge contact alone does not.
"""

import csv
import io

from .wire import int32, parse_fields, scalar


def analyze_bytes(raw: bytes, name: str = "") -> dict:
    fields = parse_fields(raw)
    template_raw = scalar(fields, 2, 2, required=True)
    try:
        template_id = template_raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("template ID is not UTF-8") from error
    if not template_id.strip():
        raise ValueError("template ID is empty")
    tiles, towers = [], []
    for field in fields:
        if field.number not in (6, 7):
            continue
        if field.wire_type != 2:
            raise ValueError("TileData/StackData has wrong wire type")
        nested = parse_fields(field.value)
        x, y = int32(nested, 1, required=True), int32(nested, 2, required=True)
        if field.number == 6:
            tiles.append({"index": len(tiles), "x": x, "y": y,
                          "z": int32(nested, 3, default=0),
                          "tile_id": int32(nested, 4, required=True), "tower_index": None})
        else:
            direction = int32(nested, 3, default=0)
            if direction not in (0, 1, 2, 3):
                raise ValueError("invalid tower direction {}".format(direction))
            towers.append({"index": len(towers), "x": x, "y": y, "direction": direction})
    tower_by_xy = {}
    for tower in towers:
        xy = tower["x"], tower["y"]
        tower_by_xy.setdefault(xy, tower["index"])
    ordinary_by_xy = {}
    for tile in tiles:
        xy = tile["x"], tile["y"]
        tile["tower_index"] = tower_by_xy.get(xy)
        if tile["tower_index"] is None:
            ordinary_by_xy.setdefault(xy, []).append(tile)
    # Coordinates are multiplied by 85 so the 16/85 tower shift and exact
    # edge contact stay integer-exact (no floating-point epsilon threshold).
    vectors = ((0, -1), (-1, 0), (0, 1), (1, 0))
    projected = []
    for tile in tiles:
        tower_index = tile["tower_index"]
        vx, vy = vectors[towers[tower_index]["direction"]] if tower_index is not None else (0, 0)
        projected.append((tile["x"] * 85 + vx * 16 * tile["z"],
                          tile["y"] * 85 + vy * 16 * tile["z"], tile["z"]))
    issues = []
    for (x, y), group in sorted(ordinary_by_xy.items()):
        layers = {}
        for tile in group:
            layers.setdefault(tile['z'], []).append(tile)
        levels = sorted(layers)
        pairs = ((lower, upper) for low, high in zip(levels, levels[1:])
                 for lower in layers[low] for upper in layers[high])
        for lower, upper in pairs:
            gap = upper["z"] - lower["z"]
            if gap > 1 and any(lower["z"] < z < upper["z"] and abs(px - x * 85) < 170
                               and abs(py - y * 85) < 170 for px, py, z in projected):
                continue
            issues.append({"kind": "adjacent" if gap == 1 else "floating", "x": x, "y": y,
                           "lower_layer": lower["z"] + 1, "upper_layer": upper["z"] + 1,
                           "lower_index": lower["index"], "upper_index": upper["index"]})
    adjacent = sum(issue["kind"] == "adjacent" for issue in issues)
    return {"name": name or template_id, "template_id": template_id, "tile_count": len(tiles),
            "tower_count": len(towers), "adjacent_count": adjacent,
            "floating_count": len(issues) - adjacent,
            "coordinate_count": len({(issue["x"], issue["y"]) for issue in issues}),
            "issues": issues, "tiles": tiles, "towers": towers}


def safe_csv_cell(value):
    text = "" if value is None else str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else text


def csv_bytes(headers, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([safe_csv_cell(value) if isinstance(value, str) else value for value in row])
    return stream.getvalue().encode("utf-8-sig")


def issue_text(issues):
    return "; ".join("({},{}) {}→{}层 {}".format(i["x"], i["y"], i["lower_layer"], i["upper_layer"],
                                               "相邻" if i["kind"] == "adjacent" else "隔层") for i in issues)


def results_csv(rows) -> bytes:
    """Export analyses (or per-file error dictionaries) without spreadsheet formulas."""
    headers = ["模板名称", "识别状态", "非塔重叠坐标组数", "相邻层全遮盖组数", "隔层悬空全遮盖组数", "模板另含塔", "坐标明细", "错误"]
    output = []
    for row in rows:
        error = row.get("error", "")
        output.append([row.get("name") or row.get("template_id", ""), "失败" if error else "命中" if row.get("issues") else "未命中",
                       "" if error else row.get("coordinate_count", 0), "" if error else row.get("adjacent_count", 0),
                       "" if error else row.get("floating_count", 0), "" if error else "是" if row.get("tower_count") else "否",
                       issue_text(row.get("issues", [])), error])
    return csv_bytes(headers, output)
