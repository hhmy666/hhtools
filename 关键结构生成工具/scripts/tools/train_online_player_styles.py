"""Fit player choice profiles from paired, verified replay exports."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from random import Random
from tempfile import NamedTemporaryFile

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from player_replay_training_data import PlayerReplayLoader, ReplaySource, player_fold, template_fold
from puzzle_core.online_player_simulation import (
    FEATURE_COUNT,
    FEATURE_VERSION,
    ChoiceObservation,
    PlayerPolicy,
    simulate_players,
)

FEATURE_BITS = tuple(
    tuple(index for index in range(FEATURE_COUNT) if code & (1 << index))
    for code in range(1 << FEATURE_COUNT)
)


def choice_distribution(available, weights):
    denominator = sum(count * weights[code] for code, count in available)
    means = [0.0] * FEATURE_COUNT
    for code, count in available:
        probability = count * weights[code] / denominator
        for index in FEATURE_BITS[code]:
            means[index] += probability
    return denominator, tuple(means)


class ChoiceFit:
    """Online multinomial likelihood over the choices actually available."""

    def __init__(self):
        self.coefficients = [0.0] * FEATURE_COUNT
        self.gradient_squares = [1.0] * FEATURE_COUNT

    def update(self, choice: ChoiceObservation) -> None:
        if len(choice.available) < 2:
            return
        weights = {
            code: math.exp(sum(self.coefficients[index] for index in FEATURE_BITS[code]))
            for code, _ in choice.available
        }
        _, expectation = choice_distribution(choice.available, weights)
        selected = choice.selected
        for index, mean in enumerate(expectation):
            gradient = bool(selected & (1 << index)) - mean
            self.gradient_squares[index] += gradient * gradient
            delta = 0.45 * gradient / math.sqrt(self.gradient_squares[index])
            self.coefficients[index] = min(10.0, max(-10.0, self.coefficients[index] + delta))


class PlayerFit:
    """Personal offsets from the population model, shrunk for sparse evidence."""

    def __init__(self):
        self.clicks = [0, 0]
        self.gradients = [[0.0] * FEATURE_COUNT for _ in range(2)]
        self.curvature = [[0.0] * FEATURE_COUNT for _ in range(2)]

    def update(self, choice: ChoiceObservation, expected: tuple[float, ...]) -> None:
        if len(choice.available) < 2:
            return
        phase = int(choice.high_pressure)
        self.clicks[phase] += 1
        for index, mean in enumerate(expected):
            self.gradients[phase][index] += bool(choice.selected & (1 << index)) - mean
            self.curvature[phase][index] += mean * (1 - mean)

    def policy(self, baseline: PlayerPolicy) -> PlayerPolicy:
        def coefficients(population, phase):
            return tuple(
                value + max(-2.0, min(2.0, gradient / (12.0 + curvature)))
                for value, gradient, curvature in zip(
                    population, self.gradients[phase], self.curvature[phase],
                )
            )

        return PlayerPolicy(
            1.0, coefficients(baseline.coefficients, 0),
            coefficients(baseline.pressure_coefficients, 1),
        )


def partition(attempt):
    player = player_fold(*attempt.player_key) == 0
    family = template_fold(attempt.family) == 0
    if player and family:
        return "holdout"
    if player:
        return "tuning"
    if not player and not family:
        return "train"
    return "buffer"


def scaled_policy(policy: PlayerPolicy, concentration: float) -> PlayerPolicy:
    def scale(values):
        return tuple(max(-20, min(20, value * concentration)) for value in values)
    return PlayerPolicy(
        policy.weight, scale(policy.coefficients), scale(policy.pressure_coefficients),
    )


def evaluate_rollouts(boards, policies, count, seed):
    if not boards or not policies:
        return None, 0
    rng = Random(seed)
    count = min(count, len(policies))
    chosen_profiles = rng.sample(policies, count)
    chosen_boards = rng.choices(boards, k=count)
    wins = sum(
        simulate_players(board, (policy,), trials=1, seed=seed + index)
        for index, (board, policy) in enumerate(zip(chosen_boards, chosen_profiles))
    )
    return wins / count, count


def fit(loader, sources, limit):
    population = (ChoiceFit(), ChoiceFit())
    train_counts = Counter()
    finishing_choices = Counter()
    for row, attempt in enumerate(loader.iter_attempts(sources), start=1):
        if partition(attempt) == "train":
            train_counts["attempts"] += 1
            for choice in attempt.replay.evidence:
                population[int(choice.high_pressure)].update(choice)
                train_counts["clicks"] += 1
                if any(code & 1 for code, _ in choice.available):
                    context = (
                        "0–2" if choice.bag_size <= 2 else "3–4" if choice.bag_size <= 4 else "5–6",
                        "64+" if choice.visible_area >= 64 else "<64",
                        "8+" if choice.visible_pattern_count >= 8 else "<8",
                    )
                    finishing_choices[context, "available"] += 1
                    finishing_choices[context, "chosen"] += bool(choice.selected & 1)
        if limit and row >= limit:
            break
    train_audit = dict(loader.stats)
    baseline = PlayerPolicy(
        1.0, tuple(population[0].coefficients), tuple(population[1].coefficients),
    )
    weights = tuple(baseline.priorities(high_pressure=flag) for flag in (False, True))
    profiles: dict[tuple[str, str], PlayerFit] = {}
    evaluation_counts = {"holdout": Counter(), "tuning": Counter()}
    reservoirs = {"holdout": [], "tuning": []}
    rng = Random(191)
    total_log_loss = 0.0
    uniform_log_loss = 0.0

    @lru_cache(maxsize=32768)
    def distribution(phase, available):
        return choice_distribution(available, weights[phase])

    for row, attempt in enumerate(loader.iter_attempts(sources), start=1):
        group = partition(attempt)
        if group == "train":
            profile = profiles.setdefault(attempt.player_key, PlayerFit())
            for choice in attempt.replay.evidence:
                if len(choice.available) >= 2:
                    _, expected = distribution(int(choice.high_pressure), choice.available)
                    profile.update(choice, expected)
        elif group in evaluation_counts:
            counts = evaluation_counts[group]
            counts["attempts"] += 1
            if group == "holdout":
                for choice in attempt.replay.evidence:
                    phase = int(choice.high_pressure)
                    denominator, _ = distribution(phase, choice.available)
                    total_log_loss += math.log(denominator / weights[phase][choice.selected])
                    uniform_log_loss += math.log(sum(count for _, count in choice.available))
                    counts["clicks"] += 1
            if attempt.observed_naked_win is not None:
                counts["terminal"] += 1
                counts["wins"] += attempt.observed_naked_win
                reservoir = reservoirs[group]
                if len(reservoir) < 1200:
                    reservoir.append(attempt.board)
                else:
                    slot = rng.randrange(counts["terminal"])
                    if slot < len(reservoir):
                        reservoir[slot] = attempt.board
        if limit and row >= limit:
            break

    policies = [
        profile.policy(baseline)
        for profile in profiles.values() if sum(profile.clicks) >= 20
    ]
    # Tune choice concentration on a different set of players from fitting,
    # and a different set of template families from final evaluation.
    tuning = evaluation_counts["tuning"]
    tuning_rate = tuning["wins"] / tuning["terminal"] if tuning["terminal"] else None
    tested_concentrations = []
    concentration = 1.0
    if tuning["terminal"] >= 100 and policies:
        for value in (1.0, 1.25, 1.5, 1.8, 2.1, 2.5):
            trial_policies = [scaled_policy(policy, value) for policy in policies]
            rate, _ = evaluate_rollouts(reservoirs["tuning"], trial_policies, 400, 901)
            tested_concentrations.append((value, rate))
        concentration, _ = min(
            tested_concentrations, key=lambda choice: abs(choice[1] - tuning_rate),
        )
        policies = [scaled_policy(policy, concentration) for policy in policies]

    simulated_rate, trial_count = evaluate_rollouts(reservoirs["holdout"], policies, 1200, 9001)
    holdout_count = evaluation_counts["holdout"]
    clicks = holdout_count["clicks"]
    terminal = holdout_count["terminal"]
    metrics = {
        "status": "research",
        "feature_version": FEATURE_VERSION,
        "input_audit": train_audit,
        "train_replays": train_counts["attempts"],
        "train_verified_clicks": train_counts["clicks"],
        "finishing_choice_by_context": [
            {
                "bag_tiles": bag, "visible_grid_cells": area, "recognizable_patterns": patterns,
                "opportunities": count,
                "chosen": finishing_choices[(bag, area, patterns), "chosen"],
            }
            for ((bag, area, patterns), kind), count in sorted(finishing_choices.items())
            if kind == "available"
        ],
        "distinct_player_profiles": len(policies),
        "choice_concentration": concentration,
        "tuning_verified_outcomes": tuning["terminal"],
        "tuning_observed_win_rate": tuning_rate,
        "tuning_simulated_rates": tested_concentrations,
        "holdout_replays": holdout_count["attempts"],
        "holdout_verified_clicks": clicks,
        "holdout_choice_log_loss": total_log_loss / clicks if clicks else None,
        "holdout_uniform_choice_log_loss": uniform_log_loss / clicks if clicks else None,
        "holdout_verified_outcomes": terminal,
        "holdout_observed_win_rate": holdout_count["wins"] / terminal if terminal else None,
        "holdout_simulated_trials": trial_count,
        "holdout_simulated_win_rate": simulated_rate,
    }
    return metrics, policies


def main():
    from restore_player_replay import DEFAULT_CONFIG_DIR, DEFAULT_TEMPLATE_DIR

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path, nargs="+")
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    parser.add_argument("--template-dir", type=Path, default=DEFAULT_TEMPLATE_DIR)
    parser.add_argument("--default-product", choices=("tile01a", "tile01g"))
    parser.add_argument("--player-namespace", default="")
    parser.add_argument("--raw-player-ids", action="append", type=Path, default=[])
    parser.add_argument("--warehouse-player-ids", action="store_true")
    parser.add_argument("--model-out", type=Path)
    parser.add_argument("--limit-verified-replays", type=int)
    args = parser.parse_args()

    loader = PlayerReplayLoader(args.config_dir, args.template_dir)
    raw_paths = {path.resolve() for path in args.raw_player_ids}
    sources = []
    for path in args.csv:
        is_raw = path.resolve() in raw_paths
        id_format = "raw" if is_raw else "product_sha256" if args.warehouse_player_ids else "as_is"
        sources.append(ReplaySource(path, args.default_product, args.player_namespace, id_format))
    metrics, policies = fit(loader, sources, args.limit_verified_replays)
    if args.model_out and len(policies) > 1000:
        payload = {**metrics, "player_policies": [asdict(policy) for policy in policies]}
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=args.model_out.parent, delete=False,
        ) as output:
            temporary = Path(output.name)
            json.dump(payload, output, ensure_ascii=False, separators=(",", ":"))
        try:
            os.replace(temporary, args.model_out)
        finally:
            temporary.unlink(missing_ok=True)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
