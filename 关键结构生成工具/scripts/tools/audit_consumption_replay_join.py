"""Measure coverage of exact ReplayInfo/weighted-consumption training cells."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from math import isclose, isfinite
from pathlib import Path
from typing import Iterable, Mapping


COST_DEFINITION = "level_report_weighted_resources_v1"
PRODUCTS = ("tile01a", "tile01g")
DUP_PARAMETER = re.compile(r"[0-9]+\|-?[0-9]+\|[0-9]+\|[0-9]+")


@dataclass(frozen=True)
class ParameterCell:
    product: str
    position: int
    level_id: str
    template_id: str
    duplicate: int
    parameter: str

    @classmethod
    def from_row(cls, row: Mapping[str, str]) -> ParameterCell:
        product = (row["product"] or "").strip()
        position = int(row["level_index"])
        level_id = (row["level_id"] or "").strip()
        template_id = (row["level_template_id"] or "").strip()
        duplicate = int(row["level_dup_para_id"])
        parameter = (row["level_dup_para"] or "").strip()
        if (
            product not in PRODUCTS or position < 1 or not level_id or not template_id
            or level_id.lower() == "unknown" or duplicate not in range(1, 11)
            or DUP_PARAMETER.fullmatch(parameter) is None
        ):
            raise ValueError("无效产品、关卡或完整副本参数")
        return cls(product, position, level_id, template_id, duplicate, parameter)


@dataclass(frozen=True)
class CostCell:
    parameter: ParameterCell
    start: date
    end: date
    shard: int
    entrants: int
    player_levels: int
    attempts: int
    mean: float

    @classmethod
    def from_row(cls, row: Mapping[str, str]) -> CostCell:
        parameter = ParameterCell.from_row(row)
        start = date.fromisoformat(row["cohort_start"])
        end = date.fromisoformat(row["cohort_end"])
        shard = int(row["cohort_shard"])
        entrants = int(row["entrants"])
        player_levels = int(row["player_levels"])
        attempts = int(row["attempts"])
        covered = int(row["observed_cost_player_levels"])
        total = float(row["weighted_cost_total"])
        mean = float(row["mean_consumption"])
        if (
            row["cost_definition"] != COST_DEFINITION or start > end
            or not -1 <= shard <= 255 or entrants <= 0 or attempts < entrants
            or player_levels < entrants or covered != player_levels
            or not isfinite(mean) or mean < 0 or total < 0
            or not isclose(mean, total / player_levels, rel_tol=1e-9, abs_tol=1e-9)
        ):
            raise ValueError("消耗口径不符、人数不完整或汇总数值错误")
        return cls(parameter, start, end, shard, entrants, player_levels, attempts, mean)


def audit_coverage(
    replay_rows: Iterable[Mapping[str, str]],
    cost_rows: Iterable[Mapping[str, str]],
    *,
    replay_shard: int,
    replay_start: date,
    replay_end: date,
    minimum_replays: int,
) -> dict[str, object]:
    if not -1 <= replay_shard <= 255 or minimum_replays < 1:
        raise ValueError("抽样分片应在 -1 到 255 之间，每组最少回放数应为正数")
    if replay_start > replay_end:
        raise ValueError("回放起始日期晚于结束日期")

    labels: dict[ParameterCell, set[CostCell]] = defaultdict(set)
    cost_status: Counter[str] = Counter()
    for row in cost_rows:
        try:
            label = CostCell.from_row(row)
        except (KeyError, TypeError, ValueError):
            cost_status["missing_or_invalid_cost_label"] += 1
            continue
        if label.shard != replay_shard:
            cost_status["different_cohort_shard"] += 1
        elif (label.start, label.end) != (replay_start, replay_end):
            cost_status["different_cohort_window"] += 1
        elif label in labels[label.parameter]:
            cost_status["duplicate_identical_label"] += 1
        else:
            labels[label.parameter].add(label)
            cost_status["eligible_cost_label"] += 1

    replay_status: Counter[str] = Counter()
    replay_products: dict[str, Counter[str]] = defaultdict(Counter)
    matched: Counter[CostCell] = Counter()
    for row in replay_rows:
        try:
            cell = ParameterCell.from_row(row)
            event_date = date.fromisoformat(row["event_date"])
            if row.get("cohort_shard", "") not in ("", str(replay_shard)):
                raise ValueError("回放分片与元数据不一致")
            if not replay_start <= event_date <= replay_end:
                raise ValueError("回放日期超出声明的查询区间")
        except (KeyError, TypeError, ValueError):
            replay_status["invalid_replay_metadata"] += 1
            continue
        matches = labels.get(cell, ())
        if len(matches) == 1:
            matched[next(iter(matches))] += 1
            status = "matched"
        else:
            status = "conflicting_cost_label" if matches else "no_exact_cost_label"
        replay_status[status] += 1
        replay_products[cell.product][status] += 1

    sufficiently_sampled = [label for label, count in matched.items() if count >= minimum_replays]
    return {
        "cost_definition": COST_DEFINITION,
        "cohort_shard": replay_shard,
        "cohort_start": replay_start.isoformat(),
        "cohort_end": replay_end.isoformat(),
        "cost_rows_by_status": dict(cost_status),
        "replay_rows_by_status": dict(replay_status),
        "replay_rows_by_product_and_status": dict(replay_products),
        "distinct_matched_cost_labels": len(matched),
        "minimum_replays_per_cost_label": minimum_replays,
        "matched_cost_labels_with_minimum_replays": len(sufficiently_sampled),
        "matched_replays_with_minimum_replays": sum(matched[label] for label in sufficiently_sampled),
    }


def csv_rows(paths: Iterable[Path]) -> Iterable[dict[str, str]]:
    for path in paths:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            yield from csv.DictReader(stream)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", type=Path, nargs="+", required=True)
    parser.add_argument("--cost-labels", type=Path, nargs="+", required=True)
    parser.add_argument("--cohort-shard", type=int, required=True)
    parser.add_argument("--cohort-start", type=date.fromisoformat, required=True)
    parser.add_argument("--cohort-end", type=date.fromisoformat, required=True)
    parser.add_argument("--minimum-replays", type=int, default=20)
    args = parser.parse_args()
    try:
        report = audit_coverage(
            csv_rows(args.replay), csv_rows(args.cost_labels),
            replay_shard=args.cohort_shard, replay_start=args.cohort_start,
            replay_end=args.cohort_end, minimum_replays=args.minimum_replays,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    print()


if __name__ == "__main__":
    main()
