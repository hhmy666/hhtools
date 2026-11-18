"""Fit an empirical consumption calibration to matched replay/online cost cells."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from tempfile import NamedTemporaryFile

if __package__:
    from .fit_replay_behavior import AUTOMATIC_SPACE, count, PROPS, STEP
else:
    from fit_replay_behavior import AUTOMATIC_SPACE, count, PROPS, STEP

FEATURES = ("naked_win_rate", "assisted_win_rate", "mean_prop_uses", "mean_revives")
WINS = {"pass", "success", "win"}
LOSSES = {"fail", "lose", "failed"}
CELL_FIELDS = ("product", "level_index", "level_id", "level_template_id", "level_dup_para_id", "level_dup_para")
REVIVES = ("level_revive_count", "level_space_revive_count", "level_time_revive_count")
EXTRA = ("extra_space_booster_num", "unlimited_ingame_extra_space_time")


def cell(row):
    return tuple(row.get(key) or "" for key in CELL_FIELDS)


def fold(template):
    return hashlib.sha256(template.lower().encode()).digest()[0] % 5


def load_labels(paths, level_min, level_max, min_users):
    labels = {}
    for path in paths:
        with path.open(encoding="utf-8-sig", newline="") as file:
            for row in csv.DictReader(file):
                if not level_min <= count(row, "level_index") <= level_max:
                    continue
                users = count(row, "entrants")
                covered = count(row, "observed_cost_entrants") or count(row, "observed_cost_player_levels")
                if users < min_users or covered < users * .85:
                    continue
                try:
                    cost = float(row["mean_consumption"])
                except (TypeError, ValueError, KeyError):
                    continue
                if math.isfinite(cost) and cost >= 0:
                    key = cell(row)
                    if key not in labels or users > labels[key][1]:
                        labels[key] = (cost, users)
    return labels


def read_matches(paths, labels, minimum_replays):
    grouped = defaultdict(Counter)
    dedup = set()
    for path in paths:
        with path.open(encoding="utf-8-sig", newline="") as file:
            for row in csv.DictReader(file):
                key = cell(row)
                if key not in labels:
                    continue
                tag = (key, row.get("user_id"), row.get("event_time"), row.get("replay_info"))
                if tag in dedup or not row.get("replay_info"):
                    continue
                dedup.add(tag)
                status = (row.get("level_status") or "").lower()
                if status not in WINS | LOSSES or row.get("prop_use_count") in (None, ""):
                    continue
                recorded_assets = [int(step[2]) for s in row["replay_info"].split("|") if (step := STEP.fullmatch(s))]
                asset_actions = sum(asset in PROPS for asset in recorded_assets)
                props = max(asset_actions, count(row, "prop_use_count"))
                revives = max(count(row, k) for k in REVIVES)
                stats = grouped[key]
                stats["n"] += 1
                stats["props"] += props
                stats["revives"] += revives
                stats["wins"] += status in WINS
                stats["naked"] += status in WINS and not (props or revives or any(asset in AUTOMATIC_SPACE for asset in recorded_assets) or any(count(row, k) for k in EXTRA))
    output = []
    for key, stats in grouped.items():
        n = stats["n"]
        if n >= minimum_replays:
            x = (1.0, stats["naked"]/n, stats["wins"]/n, stats["props"]/n, stats["revives"]/n)
            cost, users = labels[key]
            weight = min(50, math.sqrt(users * n) / 3)
            output.append((key, x, cost, weight, n))
    return output


def solve(matrix, target):
    """Solve the small positive definite ridge system by pivoted elimination."""
    aug = [list(row) + [y] for row, y in zip(matrix, target)]
    for col in range(len(aug)):
        pivot = max(range(col, len(aug)), key=lambda r: abs(aug[r][col]))
        aug[col], aug[pivot] = aug[pivot], aug[col]
        norm = aug[col][col]
        aug[col] = [value / norm for value in aug[col]]
        for row in range(len(aug)):
            if row != col:
                mult = aug[row][col]
                aug[row] = [value - mult * ref for value, ref in zip(aug[row], aug[col])]
    return [row[-1] for row in aug]


def coefficients(data):
    size = len(FEATURES) + 1
    matrix = [[0.0] * size for _ in range(size)]
    target = [0.0] * size
    for _, x, cost, weight, _ in data:
        y = math.log1p(cost)
        for i in range(size):
            target[i] += weight * x[i] * y
            for j in range(size):
                matrix[i][j] += weight * x[i] * x[j]
    for i in range(1, size):
        matrix[i][i] += 10.0
    return solve(matrix, target)


def estimate(metrics, model):
    if not model or any(not isinstance(metrics.get(name), (int, float)) for name in FEATURES):
        return None
    if any(not math.isfinite(metrics[name]) or metrics[name] < 0 for name in FEATURES):
        return None
    x = list(metrics[name] for name in FEATURES)
    bounds = model["feature_bounds"]
    # Tile configurations far outside the evidence are reported as such rather
    # than making an unbounded exponential extrapolation.
    z = [min(high, max(low, value)) for value, (low, high) in zip(x, bounds)]
    return _predict((1.0, *z), model["coefficients"], model["log_retransform"])


def _predict(x, beta, retransform):
    return max(0.0, math.exp(sum(a * b for a, b in zip(x, beta))) * retransform - 1)


def _smearing(rows, beta):
    ratios = []
    for _, x, cost, _, _ in rows:
        ratios.append(math.exp(max(-3.0, min(3.0, math.log1p(cost) - sum(a*b for a,b in zip(x, beta))))))
    return statistics.fmean(ratios)


def fit(replays, labels, level_min, level_max):
    joined = read_matches(replays, load_labels(labels, level_min, level_max, 5), 5)
    if len(joined) < 25:
        raise ValueError(f"只有 {len(joined)} 个严格匹配的消费样本格，无法拟合。")
    error, naive = [], []
    for split in range(5):
        test = [r for r in joined if fold(r[0][3]) == split]
        train = [r for r in joined if fold(r[0][3]) != split]
        beta = coefficients(train)
        smear = _smearing(train, beta)
        baseline = statistics.median(r[2] for r in train)
        for _, x, y, _, _ in test:
            error.append(abs(_predict(x, beta, smear)-y))
            naive.append(abs(baseline-y))
    beta = coefficients(joined)
    return {
        "version": "online-cost-by-replay-summary-v1",
        "features": FEATURES,
        "coefficients": beta,
        "feature_bounds": [[min(r[1][i] for r in joined), max(r[1][i] for r in joined)] for i in range(1, len(FEATURES)+1)],
        "log_retransform": _smearing(joined, beta),
        "joined_cells": len(joined),
        "joined_replays": sum(r[4] for r in joined),
        "level_range": [min(int(r[0][1]) for r in joined), max(int(r[0][1]) for r in joined)],
        "heldout_mae_observed_features": statistics.fmean(error),
        "heldout_mae_baseline": statistics.fmean(naive),
        "cost_definition": "level_report_weighted_resources_v1",
        "source_labels": [path.name for path in labels],
        "source_replays": [path.name for path in replays],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("replays", nargs="+", type=Path)
    parser.add_argument("--labels", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--min-level", type=int, default=300)
    parser.add_argument("--max-level", type=int, default=1500)
    args = parser.parse_args()
    payload = fit(args.replays, args.labels, args.min_level, args.max_level)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.output.parent, delete=False) as stream:
        tmp = Path(stream.name)
        json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
    try:
        tmp.replace(args.output)
    finally:
        tmp.unlink(missing_ok=True)
    print(json.dumps({key: val for key, val in payload.items() if key not in {"coefficients","feature_bounds","source_replays","features"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
