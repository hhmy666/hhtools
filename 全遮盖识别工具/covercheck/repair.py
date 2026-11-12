"""Metadata-preserving board edits and bounded conservative full-cover repair."""

from collections import Counter, defaultdict
from itertools import combinations
import time

from .engine import analyze_bytes, issue_text
from .wire import int32, parse_fields


SEARCH_NODE_LIMIT = 30000
SEARCH_SECONDS = 3.0


def _varint(value):
    value &= (1 << 64) - 1
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def _integer(number, value):
    return _varint(number << 3) + _varint(value)


def _message(number, value):
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _record_bytes(tile, names, original=None):
    if original is None:
        return b"".join(_integer(number, tile[name]) for number, name in names.items())
    fields = parse_fields(original)
    changed = {number for number, name in names.items()
               if int32(fields, number, default=0) != tile[name]}
    seen = set()
    result = []
    for field in fields:
        if field.number in changed:
            if field.number not in seen:
                result.append(_integer(field.number, tile[names[field.number]]))
            seen.add(field.number)
        else:
            result.append(field.raw)
    result.extend(_integer(number, tile[names[number]]) for number in sorted(changed - seen))
    return b"".join(result)


def _tile_bytes(tile, original=None):
    return _record_bytes(tile, {1: "x", 2: "y", 3: "z", 4: "tile_id"}, original)


def rewrite_tiles(raw, tiles, new_id=None, towers=None):
    """Rewrite board coordinates while retaining every unknown source field.

    Nonnegative ``index`` refers to a unique original TileData message; omitted,
    null and negative indices are additions. Semantic board checks belong to
    ``validate_board`` so the editor can represent an intermediate draft. Optional
    towers describe exactly the existing StackData indices; metadata cannot be
    added or removed, and omission preserves the original StackData bytes.
    """
    analysis = analyze_bytes(raw)
    fields = parse_fields(raw)
    originals = [field.value for field in fields if field.number == 6]
    original_towers = [field.value for field in fields if field.number == 7]
    tower_replacement = None
    if towers is not None:
        if not isinstance(towers, (list, tuple)) or len(towers) != len(original_towers):
            raise ValueError("towers需要与原模板塔描述逐一匹配，不能新增或删除塔")
        tower_replacement = {}
        for tower in towers:
            if not isinstance(tower, dict):
                raise ValueError("每座塔需要提供index、坐标和方向")
            index = tower.get("index")
            if type(index) is not int or not 0 <= index < len(original_towers) or index in tower_replacement:
                raise ValueError("塔index不存在或重复：{}".format(index))
            for name in ("x", "y", "direction"):
                value = tower.get(name)
                if type(value) is not int or not -(1 << 31) <= value < (1 << 31):
                    raise ValueError("塔{}需要是int32整数".format(name))
            if tower["direction"] not in (0, 1, 2, 3):
                raise ValueError("塔direction需要是0..3")
            tower_replacement[index] = _record_bytes(
                tower, {1: "x", 2: "y", 3: "direction"}, original_towers[index])
    if not isinstance(tiles, (list, tuple)):
        raise ValueError("tiles需要是牌数组")
    seen, replacement = set(), []
    for tile in tiles:
        if not isinstance(tile, dict):
            raise ValueError("每张牌需要提供坐标和牌型")
        for name in ("x", "y", "z", "tile_id"):
            value = tile.get(name)
            if type(value) is not int or not -(1 << 31) <= value < (1 << 31):
                raise ValueError("{}需要是int32整数".format(name))
        index = tile.get("index")
        if index is not None and type(index) is not int:
            raise ValueError("牌index需要是整数或null")
        if index is not None and index >= 0:
            if index >= len(originals) or index in seen:
                raise ValueError("牌index不存在或重复：{}".format(index))
            seen.add(index)
            original = originals[index]
        else:
            original = None
        replacement.append(_message(6, _tile_bytes(tile, original)))
    if new_id is not None and (not isinstance(new_id, str) or not new_id.strip()):
        raise ValueError("模板ID需要是非空文字")
    unchanged_tiles = len(tiles) == len(analysis["tiles"]) and all(
        tile.get("index") == index and all(tile[name] == analysis["tiles"][index][name]
                                           for name in ("x", "y", "z", "tile_id"))
        for index, tile in enumerate(tiles))
    unchanged_towers = tower_replacement is None or all(
        tower_replacement[index] == original for index, original in enumerate(original_towers))
    if unchanged_tiles and unchanged_towers:
        if new_id is None or new_id == analysis["template_id"]:
            return raw
        return b"".join(_message(2, new_id.encode("utf-8")) if field.number == 2 else field.raw
                        for field in fields)
    result, inserted, tower_index = [], False, 0
    for field in fields:
        if field.number == 6:
            if not inserted:
                result.extend(replacement)
                inserted = True
        elif field.number == 7 and tower_replacement is not None:
            body = tower_replacement[tower_index]
            result.append(field.raw if body == field.value else _message(7, body))
            tower_index += 1
        elif field.number == 2 and new_id is not None and new_id != analysis["template_id"]:
            result.append(_message(2, new_id.encode("utf-8")))
        else:
            result.append(field.raw)
    if not inserted:
        result.extend(replacement)
    return b"".join(result)


def _geometry(analysis):
    vectors = ((0, -1), (-1, 0), (0, 1), (1, 0))
    result = []
    for tile in analysis["tiles"]:
        tower = tile["tower_index"]
        vx, vy = vectors[analysis["towers"][tower]["direction"]] if tower is not None else (0, 0)
        result.append((tile, tile["x"] * 85 + vx * 16 * tile["z"],
                       tile["y"] * 85 + vy * 16 * tile["z"]))
    return result


def _overlaps(left, right):
    return abs(left[1] - right[1]) < 170 and abs(left[2] - right[2]) < 170


def _location(tile):
    return "({},{})第{}层（Z={}）".format(tile["x"], tile["y"], tile["z"] + 1, tile["z"])


def _unsupported(geometry):
    by_z = defaultdict(list)
    for item in geometry:
        by_z[item[0]["z"]].append(item)
    return [item[0] for item in geometry if item[0]["z"] > 0
            and not any(_overlaps(item, below) for below in by_z[item[0]["z"] - 1])]


def _new_unsupported(geometry, original):
    """Existing unsupported source identities may stay at exactly the same pose."""
    baseline = {tile['index']: tile for tile in _unsupported(_geometry(original))}
    return [tile for tile in _unsupported(geometry)
            if tile.get('index') not in baseline or any(
                tile[key] != baseline[tile['index']][key] for key in ('x', 'y', 'z', 'tile_id'))]


def validate_board(raw):
    """Require valid wire and grouped counts; geometry is advisory."""
    return _validate_board(raw)


def _validate_board(raw, support_exempt=()):
    """Retain the historical private signature and enforce grouping for saves."""
    try:
        analysis = analyze_bytes(raw)
    except (ValueError, TypeError) as error:
        return {"valid": False, "errors": ["模板无法解析：{}".format(error)],
                "analysis": None, "warnings": []}
    errors, warnings = [], []
    geometry = _geometry(analysis)
    by_z = defaultdict(list)
    for item in geometry:
        by_z[item[0]["z"]].append(item)
    for layer in by_z.values():
        for index, left in enumerate(layer):
            for right in layer[index + 1:]:
                if _overlaps(left, right):
                    warnings.append("同层正面积冲突：{} 与 {}".format(_location(left[0]), _location(right[0])))
    unsupported = _unsupported(geometry)
    analysis['support_issues'] = [dict(index=tile['index'], x=tile['x'], y=tile['y'], z=tile['z'],
                                       layer=tile['z'] + 1, kind='unsupported') for tile in unsupported]
    warnings.extend("悬空：{}没有紧邻下层的正面积承接".format(_location(tile)) for tile in unsupported)
    tower_xy = Counter((tower['x'], tower['y']) for tower in analysis['towers'])
    warnings.extend('塔描述坐标重复：({},{})'.format(x, y) for (x, y), count in tower_xy.items() if count > 1)
    counts = Counter(tile["tile_id"] for tile in analysis["tiles"])
    if len(analysis["tiles"]) % 3:
        errors.append("全部牌共{}张，未按三张成组（含塔牌）".format(len(analysis["tiles"])))
    for tile_id, name in ((-10, "A"), (-11, "B")):
        if counts[tile_id] % 3:
            errors.append("{}牌共{}张，未按三张成组（含塔牌）".format(name, counts[tile_id]))
    if analysis['issues']:
        warnings.append("仍有全遮盖：" + issue_text(analysis["issues"]))
    return {"valid": not errors, "errors": errors, "analysis": analysis, "warnings": warnings}


def _covered(item, higher):
    """Exact rectangle-union coverage, retaining integer 16/85 tower projection."""
    left, right, bottom, top = item[1] - 85, item[1] + 85, item[2] - 85, item[2] + 85
    rectangles = [(max(left, other[1] - 85), min(right, other[1] + 85),
                   max(bottom, other[2] - 85), min(top, other[2] + 85))
                  for other in higher if _overlaps(item, other)]
    if not rectangles:
        return False
    edges = sorted({left, right} | {x for rect in rectangles for x in rect[:2]})
    for x0, x1 in zip(edges, edges[1:]):
        intervals = sorted((rect[2], rect[3]) for rect in rectangles if rect[0] < x1 and rect[1] > x0)
        end = bottom
        for y0, y1 in intervals:
            if y0 > end:
                break
            end = max(end, y1)
        if end < top:
            return False
    return True


class _BudgetExceeded(Exception):
    pass


class _Budget:
    def __init__(self):
        self.nodes = 0
        self.deadline = time.monotonic() + SEARCH_SECONDS

    def step(self):
        self.nodes += 1
        if self.nodes > SEARCH_NODE_LIMIT or time.monotonic() >= self.deadline:
            raise _BudgetExceeded


def _slots(analysis, budget, existing_layers=None):
    """Enumerate the stated finite placement domain, without relaxing geometry."""
    geometry = _geometry(analysis)
    by_z = defaultdict(list)
    for item in geometry:
        by_z[item[0]["z"]].append(item)
    tower_xy = {(tower["x"], tower["y"]) for tower in analysis["towers"]}
    ordinary = defaultdict(set)
    for tile in analysis["tiles"]:
        if tile["tower_index"] is None:
            ordinary[tile["x"], tile["y"]].add(tile["z"])
    candidates = []
    for z in sorted(by_z if existing_layers is None else existing_layers):
        if z <= 0 or not by_z.get(z - 1) or not by_z.get(z + 1):
            continue
        higher = [item for item in geometry if item[0]["z"] > z]
        for y in range(2, 17):
            for x in range(2, 15):
                budget.step()
                if (x, y) in tower_xy or ordinary[x, y] & {z - 1, z, z + 1}:
                    continue
                tile = {"index": None, "x": x, "y": y, "z": z, "tile_id": 0, "tower_index": None}
                item = (tile, x * 85, y * 85)
                if (any(_overlaps(item, other) for other in by_z[z])
                        or not any(_overlaps(item, other) for other in by_z[z - 1])
                        or not any(_overlaps(item, other) for other in by_z[z + 1])
                        or not _covered(item, higher)):
                    continue
                candidates.append(item)
    return candidates


def _compatible(left, right):
    lt, rt = left[0], right[0]
    if lt["z"] == rt["z"] and _overlaps(left, right):
        return False
    return not (lt["x"] == rt["x"] and lt["y"] == rt["y"] and abs(lt["z"] - rt["z"]) <= 1)


def _visible_signature(item, geometry):
    """Canonical exact visible regions of one face, in projected 1/85 units."""
    left, right, bottom, top = item[1] - 85, item[1] + 85, item[2] - 85, item[2] + 85
    rectangles = [(max(left, other[1] - 85), min(right, other[1] + 85),
                   max(bottom, other[2] - 85), min(top, other[2] + 85))
                  for other in geometry if other[0]["z"] > item[0]["z"] and _overlaps(item, other)]
    edges = sorted({left, right} | {x for rect in rectangles for x in rect[:2]})
    result = []
    for x0, x1 in zip(edges, edges[1:]):
        intervals = sorted((rect[2], rect[3]) for rect in rectangles if rect[0] < x1 and rect[1] > x0)
        exposed, end = [], bottom
        for y0, y1 in intervals:
            if y0 > end:
                exposed.append((end, y0))
            end = max(end, y1)
        if end < top:
            exposed.append((end, top))
        exposed = tuple(exposed)
        if result and result[-1][2] == exposed:
            result[-1] = (result[-1][0], x1, exposed)
        else:
            result.append((x0, x1, exposed))
    return tuple(result)


def _surface_safe(before, after):
    """Tower expansion may change its own outline, never retained ordinary faces.

    ``before`` is the original board minus problem uppers: the lower face at
    those same coordinates is intentionally allowed to become exposed.
    """
    ordinary = {item[0]["index"]: item for item in after if item[0]["tower_index"] is None
                and item[0].get("index") is not None}
    return all(item[0]["index"] in ordinary and
               _visible_signature(item, before) == _visible_signature(ordinary[item[0]["index"]], after)
               for item in before if item[0]["tower_index"] is None)


def _tower_plans(analysis, target, budget):
    """Finite growth allocations; equal heights stay equal, strict ranks stay strict.

    Rank is the existing number of tiles, not an imposed ratio. Prefer the
    smallest final maximum height, so 2/4/2 plus five becomes 4/5/4.
    Empty descriptors cannot start new towers. Gaps in existing towers survive.
    """
    tower_tiles = defaultdict(list)
    for tile in analysis["tiles"]:
        if tile["tower_index"] is not None:
            tower_tiles[tile["tower_index"]].append(tile)
    groups = defaultdict(list)
    for index, tiles in tower_tiles.items():
        groups[len(tiles)].append(index)
    groups = sorted(groups.items())
    plans = []

    def visit(position, remaining, previous, growth):
        budget.step()
        if position == len(groups):
            used = target - remaining
            if used:
                heights = [height + amount for (height, _), amount in zip(groups, growth)]
                plans.append(((remaining, max(heights), sum(h * h * len(group)
                              for h, (_, group) in zip(heights, groups))), tuple(growth)))
            return
        height, indices = groups[position]
        max_growth = min([remaining // len(indices)] +
                         [255 - max(tile["z"] for tile in tower_tiles[index]) for index in indices])
        for amount in range(max(0, previous + 1 - height), max_growth + 1):
            visit(position + 1, remaining - amount * len(indices), height + amount, growth + [amount])

    if groups:
        visit(0, target, -1, [])
    for _, growth in sorted(plans):
        amounts = {index: amount for (_, indices), amount in zip(groups, growth) for index in indices}
        additions = []
        for tower in analysis["towers"]:
            index = tower["index"]
            if not amounts.get(index):
                continue
            start = max(tile["z"] for tile in tower_tiles[index]) + 1
            additions.extend({"index": None, "x": tower["x"], "y": tower["y"], "z": z,
                              "tile_id": 0, "tower_index": index}
                             for z in range(start, start + amounts[index]))
        yield additions


def _removal_variants(original, upper_indices, budget):
    """Only layers entirely made of removed uppers may be compressed away."""
    kept = [tile for tile in original["tiles"] if tile["index"] not in upper_indices]
    yield kept, ()
    by_z = defaultdict(list)
    for tile in original["tiles"]:
        by_z[tile["z"]].append(tile)
    layers = sorted(z for z, tiles in by_z.items() if all(tile["index"] in upper_indices for tile in tiles))
    for size in range(len(layers), 0, -1):
        for compressed in combinations(layers, size):
            budget.step()
            yield [dict(tile, z=tile["z"] - sum(z < tile["z"] for z in compressed)) for tile in kept], compressed


def _search(raw, kept, removed, candidates, budget, towers, before, tower_additions=()):
    """Backtrack over combinations; tile types do not change footprint geometry."""
    chosen, next_index = [], 0
    target = len(removed) - len(tower_additions)
    while True:
        budget.step()
        if len(chosen) == target:
            additions = [dict(tile, index=removed[position]["index"], tile_id=removed[position]["tile_id"])
                         for position, tile in enumerate(list(tower_additions) + [candidates[index][0] for index in chosen])]
            if not _surface_safe(before, _geometry({"tiles": kept + additions, "towers": towers})):
                if not chosen:
                    return None
                next_index = chosen.pop() + 1
                continue
            candidate_raw = rewrite_tiles(raw, kept + additions)
            checked = validate_board(candidate_raw)
            if checked["valid"] and not checked["analysis"]["issues"]:
                return candidate_raw, [tile["index"] for tile in kept + additions]
            if not chosen:
                return None
            next_index = chosen.pop() + 1
        elif len(candidates) - next_index < target - len(chosen):
            if not chosen:
                return None
            next_index = chosen.pop() + 1
        else:
            index = next_index
            next_index += 1
            if all(_compatible(candidates[index], candidates[other]) for other in chosen):
                chosen.append(index)
                next_index = index + 1


def auto_repair(raw):
    """Run bounded low-disturbance search, then independently verify its bytes."""
    from .lowimpact import audit_repair
    from .repair_search import search_repair

    checked = validate_board(raw)
    if checked["analysis"] is None:
        return {"status": "unrepairable", "raw": raw, "reason": "原模板无法解析，已保留原样",
                "removed_count": 0, "added_count": 0, "search_complete": True}
    result = search_repair(raw, checked["analysis"])
    if result["status"] not in ("repaired", "unchanged"):
        if checked["errors"]:
            result["reason"] += "；原有问题仍需解决：" + "；".join(checked["errors"][:3])
        return result
    audited = audit_repair(raw, result.get("raw", raw), result.get("origin_indices"))
    if not audited["valid"]:
        return {"status": "unrepairable", "raw": raw,
                "reason": "最终低扰动核验未通过，原模板已保留：" + "；".join(audited["errors"]),
                "removed_count": 0, "added_count": 0, "search_complete": result.get("search_complete", False)}
    result["metrics"] = dict(result.get("metrics", {}), **audited["metrics"])
    return result
