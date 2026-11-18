"""Aggregate de-identified replay timing and prop decisions by visible context.

The output includes only aggregate counts and quantiles. Source rows and user
identifiers are never copied into the simulator directory.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from statistics import median
from collections import Counter, defaultdict
from pathlib import Path
from tempfile import NamedTemporaryFile

STEP = re.compile(r"(\d+)#(\d+)(?::\d+(?:-\d+)*)?\Z")
# UserActionRecorder writes runtime asset IDs, not the bot's enum.
PROPS = frozenset({502, 503, 505, 506, 507, 508, 509, 510, 511, 512, 513, 514, 515, 516, 517})
AUTOMATIC_SPACE = frozenset({601, 708, 801})
QUANTILES = (.10, .25, .50, .75, .90, .95, .99)


def count(row, name):
    try:
        return max(0, int(row.get(name, "") or 0))
    except (ValueError, TypeError):
        return 0


def surface_bin(value):
    return -1 if value is None else 0 if value <= 8 else 1 if value <= 15 else 2


def timing_quantiles(histogram):
    total = sum(histogram.values())
    targets = [max(1, round(q * total)) for q in QUANTILES]
    output = []
    running = 0
    for milliseconds, frequency in sorted(histogram.items()):
        running += frequency
        while len(output) < len(targets) and running >= targets[len(output)]:
            output.append(milliseconds)
    return output


def fit(paths, level_min=300, level_max=1500):
    buckets = defaultdict(Counter)
    prop = defaultdict(Counter)
    revive = defaultdict(Counter)
    audit = Counter()
    dates, products, levels = set(), set(), set()
    duration_ratios = []
    seen = set()
    for path in paths:
        with path.open(encoding="utf-8-sig", newline="") as file:
            for row in csv.DictReader(file):
                audit["input_rows"] += 1
                level = count(row, "level_index")
                if not level_min <= level <= level_max:
                    continue
                raw = row.get("replay_info") or ""
                bag = row.get("bag_tile_change_curve") or ""
                surface_text = row.get("surface_normal_tile_change_curve") or ""
                if not raw or not bag.isascii() or not bag.isdecimal():
                    audit["missing_replay_or_bag"] += 1
                    continue
                key = (row.get("product"), row.get("user_id"), row.get("event_time"), row.get("level_id"), raw)
                if key in seen:
                    audit["duplicates"] += 1
                    continue
                seen.add(key)
                parsed = [STEP.fullmatch(s) for s in raw.split("|")]
                if not all(parsed):
                    audit["malformed_replay"] += 1
                    continue
                actions = [(int(x[1]), int(x[2])) for x in parsed]
                click_count = sum(action == 1 for _, action in actions)
                if len(bag) != click_count:
                    audit["bag_misaligned"] += 1
                    continue
                if ("8" in bag or "9" in bag or any(action in AUTOMATIC_SPACE for _, action in actions) or any(count(row, key) for key in ("extra_space_booster_num", "unlimited_ingame_extra_space_time"))):
                    audit["extra_space"] += 1
                    continue
                try:
                    surface = tuple(map(int, surface_text.split("-")))
                except ValueError:
                    surface = ()
                if len(surface) != click_count:
                    surface = ()
                audit["used_rows"] += 1
                seconds = count(row, "level_duration")
                click_seconds = sum(interval for interval, action in actions if action == 1) / 1000
                if seconds and click_seconds and all(interval < 120_000 for interval, _ in actions):
                    ratio = seconds / click_seconds
                    if .25 <= ratio <= 4:
                        duration_ratios.append(ratio)
                dates.add(row.get("event_date") or "")
                products.add(row.get("product") or "")
                levels.add(level)
                attempt = count(row, "level_play_count")
                retry = 0 if attempt <= 1 else 1 if attempt <= 3 else 2
                status = (row.get("level_status") or "").lower()
                if status in ("fail", "lose", "failed") or any(
                    count(row, key) for key in ("level_revive_count","level_space_revive_count","level_time_revive_count")
                ):
                    revive[retry]["opportunities"] += 1
                    if any(count(row, key) for key in ("level_revive_count","level_space_revive_count","level_time_revive_count")):
                        revive[retry]["uses"] += 1
                index = -1
                # After a prop the old board state is no longer a valid input
                # to the next decision; resume conditioning after its first click.
                context_fresh = True
                for action_index, (interval, action) in enumerate(actions):
                    if action != 1:
                        context_fresh = False
                        continue
                    index += 1
                    if index and context_fresh and 0 < interval < 120_000:
                        b = int(bag[index - 1])
                        s = surface_bin(surface[index - 1] if surface else None)
                        # Fixed bins keep memory bounded without distorting the tail.
                        interval = min(119_980, max(1, ((interval + 10) // 20) * 20))
                        buckets[(b, s)][interval] += 1
                        if s != -1:
                            buckets[(b, -1)][interval] += 1
                        audit["timed_clicks"] += 1
                    if action_index + 1 < len(actions):
                        b = int(bag[index])
                        s = surface_bin(surface[index] if surface else None)
                        nxt = actions[action_index + 1][1]
                        if nxt == 1 or nxt in PROPS:
                            for slot in ([(b, s, retry), (b, -1, retry)] if s != -1 else [(b, -1, retry)]):
                                prop[slot]["opportunities"] += 1
                                if nxt in PROPS:
                                    prop[slot]["uses"] += 1
                                    prop[slot][f"asset_{nxt}"] += 1
                    context_fresh = True
    return {
        "version": "replay-tempo-prop-v1",
        "audit": dict(audit),
        "dates": [min(dates - {""}, default=""), max(dates, default="")],
        "products": sorted(products - {""}),
        "levels": [min(levels, default=None), max(levels, default=None)],
        "foreground_time_ratio": median(duration_ratios) if duration_ratios else 1.0,
        "foreground_time_samples": len(duration_ratios),
        "click_intervals_ms": [
            {"bag": b, "surface": surface, "quantiles": timing_quantiles(stats), "clicks": sum(stats.values())}
            for (b, surface), stats in sorted(buckets.items()) if sum(stats.values()) >= 30
        ],
        "prop_trigger": [
            {"bag": b, "surface": surface, "retry": retry, **stats, "uses": stats["uses"]}
            for (b, surface, retry), stats in sorted(prop.items()) if stats["opportunities"] >= 30
        ],
        "revive_accept": [
            {"retry": retry, **stats, "uses": stats["uses"]} for retry, stats in sorted(revive.items())
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--min-level", type=int, default=300)
    parser.add_argument("--max-level", type=int, default=1500)
    args = parser.parse_args()
    fitted = fit(args.files, args.min_level, args.max_level)
    if not fitted["click_intervals_ms"]:
        parser.error("no replay timing evidence")
    with NamedTemporaryFile(mode="w", dir=args.output.parent, encoding="utf-8", delete=False) as stream:
        path = Path(stream.name)
        json.dump(fitted, stream, ensure_ascii=False, separators=(",", ":"))
    try:
        path.replace(args.output)
    finally:
        path.unlink(missing_ok=True)
    print(json.dumps({key: fitted[key] for key in ("audit", "dates", "products", "levels")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
