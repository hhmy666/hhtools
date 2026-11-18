"""Search configured A/B group ranges with bounded, reproducible sampling."""

from __future__ import annotations

import random
from collections import Counter

from scenario_inputs import Parameters


def int_field(value, name, lo, hi, default=None):
    if value is None or value == "":
        if default is not None:
            return default
        raise ValueError(f"请填写{name}。")
    if isinstance(value, bool):
        raise ValueError(f"{name}须为整数。")
    try:
        number = int(value)
        if isinstance(value, float) and value != number:
            raise ValueError
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{name}须为整数。") from exc
    if not lo <= number <= hi:
        raise ValueError(f"{name}范围为 {lo}–{hi}。")
    return number


def _bounds(ranges, key, name, low, high, defaults):
    begin = int_field(ranges.get(f"{key}_min"), f"{name}下限", low, high, defaults[0])
    end = int_field(ranges.get(f"{key}_max"), f"{name}上限", low, high, defaults[1])
    if begin > end:
        raise ValueError(f"{name}下限不能高于上限。")
    return begin, end


def _kind_range(count, lo, hi, name):
    if count == 0:
        return 0, 0
    if count % 3:
        raise ValueError(f"{name}模板格数 {count} 不是 3 的倍数，无法按三消分组推荐。")
    groups = count // 3
    low, high = (groups + hi - 1) // hi, groups // lo
    if low > high:
        raise ValueError(f"{name}有 {groups} 组，无法满足每种 {lo}–{hi} 组的设置。")
    return low, high


def options(level, ranges, entropy, limit=12):
    sizes = Counter(tile.tile_id for tile in level.tiles)
    a = _kind_range(sizes[-10], *_bounds(ranges, "a", "A 每种组数", 1, 100, (1, 4)), "A")
    b = _kind_range(sizes[-11], *_bounds(ranges, "b", "B 每种组数", 1, 100, (1, 4)), "B")
    # The old client resolves playable pattern IDs against a finite pool. A/B
    # candidates outside this range would be silently clamped by the game.
    a = a[0], min(a[1], 19)
    b = b[0], min(b[1], 19)
    if a[0] > a[1] or b[0] > b[1]:
        raise ValueError("在指定的单图案组数范围内，所需种类数超过旧版图案池的搜索范围。")
    luck = _bounds(ranges, "luck", "幸运值", 0, 500, (100, 100))
    seed = _bounds(ranges, "seed", "Seed", 0, 31, (0, 31))
    rng = random.Random(entropy)
    found = [
        Parameters(a[0], rng.randint(*seed), luck[1], b[0]),
        Parameters(a[1], rng.randint(*seed), luck[0], b[1]),
        Parameters((a[0]+a[1])//2, rng.randint(*seed), (luck[0]+luck[1])//2, (b[0]+b[1])//2),
    ]
    for _ in range(limit * 4):
        if len(set(found)) >= limit:
            break
        found.append(Parameters(rng.randint(*a), rng.randint(*seed), rng.randint(*luck), rng.randint(*b)))
    return list(dict.fromkeys(found))[:limit]
