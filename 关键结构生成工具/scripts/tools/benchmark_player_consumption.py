"""Compare a static consumption baseline with simulated behavior on held-out families.

This offline benchmark uses already matched Redash cohorts and reconstructable
seven-slot, non-tower boards. It never writes an application model.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from math import expm1, log1p, sqrt
from pathlib import Path
from random import Random
from typing import Sequence

from audit_consumption_replay_join import CostCell, ParameterCell, csv_rows
from player_replay_training_data import PlayerReplayLoader, ReplaySource, template_fold
from puzzle_core.online_player_simulation import PlayerBoard, PlayerPolicy, _State, _CAPACITY, _PRESSURE_SLOTS
from puzzle_core.player_candidate_rollout import load_player_model
from restore_player_replay import DEFAULT_CONFIG_DIR, DEFAULT_TEMPLATE_DIR


@dataclass(frozen=True)
class Example:
    label: CostCell
    family: str
    static: tuple[float, ...]
    behavior: tuple[float, ...]

    def features(self, behavior: bool) -> tuple[float, ...]:
        return self.static + self.behavior if behavior else self.static


def load_cost_cells(path: Path, start: date, end: date, shard: int) -> dict[ParameterCell, CostCell]:
    labels: dict[ParameterCell, set[CostCell]] = defaultdict(set)
    for row in csv_rows((path,)):
        try:
            label = CostCell.from_row(row)
        except (KeyError, TypeError, ValueError):
            continue
        if (label.start, label.end, label.shard) == (start, end, shard):
            labels[label.parameter].add(label)
    return {key: next(iter(values)) for key, values in labels.items() if len(values) == 1}


def validate_replay_window(path: Path, start: date, end: date, shard: int) -> None:
    for row in csv_rows((path,)):
        event = date.fromisoformat(row["event_date"])
        if not start <= event <= end or row.get("cohort_shard", "") not in ("", str(shard)):
            raise ValueError("ReplayInfo 日期或分片与消耗查询口径不一致")


def static_features(cell: ParameterCell, board: PlayerBoard, ratio: float) -> tuple[float, ...]:
    total = len(board.labels)
    a_types, _, lucky, b_types = map(int, cell.parameter.split("|"))
    return (
        float(cell.product == "tile01g"), log1p(cell.position), log1p(total), ratio,
        len(set(board.labels)) / total, len({label for label in board.labels if label <= -12}),
        sum(mask == 0 for mask in board.blockers) / total,
        sum(mask.bit_count() for mask in board.blockers) / total,
        float(a_types), float(b_types), float(lucky),
        *(float(cell.duplicate == dup) for dup in range(2, 11)),
    )


def rollout_features(board: PlayerBoard, priorities: Sequence[tuple[tuple[float, ...], ...]]) -> tuple[float, ...]:
    """Use the same observable choice features for training and new boards."""
    wins = pressure = dead_pressure = peak = progress = 0.0
    for trial, trial_priorities in enumerate(priorities):
        # Share profiles and random streams across boards for a comparable
        # pressure statistic without requiring many rollouts at every setting.
        rng = Random(6401 + trial)
        state = _State(board)
        clicks = pressured_clicks = pressured_without_match = max_bag = 0
        while state.available and state.size < _CAPACITY:
            high_pressure = state.size >= _PRESSURE_SLOTS
            options = state.options()
            if high_pressure:
                pressured_clicks += 1
                # The choice representation marks any then-affordable match.
                pressured_without_match += not any(code & ((1 << 5) | 1) for code in options.values())
            probability = trial_priorities[int(high_pressure)]
            selected = rng.choices(tuple(options), [probability[code] for code in options.values()])[0]
            state.click(selected)
            clicks += 1
            max_bag = max(max_bag, state.size)
        wins += state.won
        pressure += pressured_clicks / max(clicks, 1)
        dead_pressure += pressured_without_match / max(pressured_clicks, 1)
        peak += max_bag / _CAPACITY
        progress += clicks / len(board.labels)
    return tuple(metric / len(priorities) for metric in (wins, pressure, dead_pressure, peak, progress))


def examples(args: argparse.Namespace, policies: tuple[PlayerPolicy, ...]) -> list[Example]:
    validate_replay_window(args.replay, args.cohort_start, args.cohort_end, args.cohort_shard)
    labels = load_cost_cells(args.cost_labels, args.cohort_start, args.cohort_end, args.cohort_shard)
    loader = PlayerReplayLoader(args.config_dir, args.template_dir)
    source = ReplaySource(args.replay, player_id_format="product_sha256")
    found: dict[ParameterCell, tuple[PlayerBoard, str, float]] = {}
    conflicts: set[ParameterCell] = set()
    for attempt in loader.iter_attempts([source]):
        product, _, _, position, level_id, template, parameter, duplicate = attempt.board_id
        key = ParameterCell(product, int(position), level_id, template, int(duplicate), parameter)
        label = labels.get(key)
        if label is None or label.entrants < args.minimum_entrants:
            continue
        record = (attempt.board, attempt.family, attempt.ratio)
        if key in found and found[key] != record:
            conflicts.add(key)
        else:
            found[key] = record
    if not 1 <= args.rollouts <= len(policies):
        raise ValueError("每个测试牌面的模拟人数应大于 0，不超过有效玩家档案数")
    sampled = Random(601).sample(policies, args.rollouts)
    priorities = tuple(
        (policy.priorities(), policy.priorities(high_pressure=True))
        for policy in sampled
    )
    return [
        Example(labels[key], family, static_features(key, board, ratio),
                rollout_features(board, priorities))
        for key, (board, family, ratio) in found.items() if key not in conflicts
    ]


def cholesky_solve(matrix: list[list[float]], target: list[float]) -> list[float]:
    """Solve the positive definite ridge system, including the intercept."""
    size = len(target)
    lower = [[0.0] * size for _ in target]
    for row in range(size):
        for col in range(row + 1):
            residual = matrix[row][col] - sum(lower[row][k] * lower[col][k] for k in range(col))
            lower[row][col] = sqrt(residual) if row == col else residual / lower[col][col]
    forward = [0.0] * size
    result = [0.0] * size
    for row in range(size):
        forward[row] = (target[row] - sum(lower[row][k] * forward[k] for k in range(row))) / lower[row][row]
    for row in reversed(range(size)):
        residual = forward[row] - sum(lower[k][row] * result[k] for k in range(row + 1, size))
        result[row] = residual / lower[row][row]
    return result


def benchmark(
    train: list[Example], test: list[Example], behavior: bool,
) -> tuple[dict[str, float], list[float]]:
    columns = tuple(zip(*(example.features(behavior) for example in train)))
    weights = [sqrt(example.label.player_levels) for example in train]
    total = sum(weights)
    means = [sum(value * weight for value, weight in zip(column, weights)) / total for column in columns]
    scales = [
        sqrt(sum(weight * (value - mean) ** 2 for value, weight in zip(column, weights)) / total)
        for column, mean in zip(columns, means)
    ]
    scales = [scale if scale >= 1e-8 else 1.0 for scale in scales]

    def design(example: Example) -> tuple[float, ...]:
        return (1.0, *((raw - mean) / scale for raw, mean, scale in zip(
            example.features(behavior), means, scales
        )))

    size = len(columns) + 1
    matrix = [[0.0] * size for _ in range(size)]
    target = [0.0] * size
    for example, weight in zip(train, weights):
        values = design(example)
        outcome = log1p(example.label.mean)
        for row, value in enumerate(values):
            target[row] += weight * value * outcome
            for col in range(row + 1):
                matrix[row][col] += weight * value * values[col]
    for row in range(1, size):
        matrix[row][row] += 10.0
    coefficients = cholesky_solve(matrix, target)
    training_max = max(example.label.mean for example in train)
    predictions = [
        max(0.0, expm1(min(log1p(training_max), sum(
            coef * value for coef, value in zip(coefficients, design(example))
        ))))
        for example in test
    ]
    errors = [abs(prediction - example.label.mean) for prediction, example in zip(predictions, test)]
    population = sum(item.label.player_levels for item in test)
    return {
        "cell_mae": sum(errors) / len(test),
        "player_level_weighted_mae": sum(
            error * example.label.player_levels for error, example in zip(errors, test)
        ) / population,
    }, errors


def family_bootstrap_gain(
    test: list[Example], baseline_errors: list[float], behavior_errors: list[float],
) -> tuple[float, float]:
    """Paired held-out MAE gain with template families as bootstrap units."""
    family_gain: Counter[str] = Counter()
    family_weight: Counter[str] = Counter()
    for example, baseline, behavior in zip(test, baseline_errors, behavior_errors):
        weight = example.label.player_levels
        family_gain[example.family] += weight * (baseline - behavior)
        family_weight[example.family] += weight
    families = sorted(family_weight)
    rng = Random(620)
    estimates = []
    for _ in range(1000):
        sample = rng.choices(families, k=len(families))
        estimates.append(
            sum(family_gain[family] for family in sample)
            / sum(family_weight[family] for family in sample)
        )
    estimates.sort()
    return estimates[25], estimates[974]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--cost-labels", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--cohort-shard", type=int, required=True)
    parser.add_argument("--cohort-start", type=date.fromisoformat, required=True)
    parser.add_argument("--cohort-end", type=date.fromisoformat, required=True)
    parser.add_argument("--minimum-entrants", type=int, default=3)
    parser.add_argument("--rollouts", type=int, default=24)
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    parser.add_argument("--template-dir", type=Path, default=DEFAULT_TEMPLATE_DIR)
    args = parser.parse_args()
    status, policies = load_player_model(args.model)
    rows = examples(args, policies)
    train = [row for row in rows if template_fold(row.family) != 0]
    test = [row for row in rows if template_fold(row.family) == 0]
    if not train or not test:
        parser.error("此查询窗口没有可验证的训练集与独立模板留出集")
    baseline, baseline_errors = benchmark(train, test, False)
    behavior, behavior_errors = benchmark(train, test, True)
    print(json.dumps({
        "behavior_model_status": status,
        "rollouts_per_cost_cell": args.rollouts,
        "minimum_entrants_per_cell": args.minimum_entrants,
        "train": {"cells": len(train), "families": len({row.family for row in train})},
        "holdout": {"cells": len(test), "families": len({row.family for row in test}),
                    "by_product": Counter(row.label.parameter.product for row in test),
                    "player_levels": sum(row.label.player_levels for row in test)},
        "static_baseline": baseline,
        "with_player_behavior": behavior,
        "held_out_weighted_mae_gain_95pct_family_bootstrap": family_bootstrap_gain(
            test, baseline_errors, behavior_errors,
        ),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
