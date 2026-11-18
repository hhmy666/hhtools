"""Audit and backtest a player policy from a local Redash ReplayInfo export.

The report is aggregate only. A backtest isn't a production model approval:
it covers only verified, static boards and the stated replay cohort.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from itertools import chain
from math import log
from pathlib import Path
from random import Random

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parents[2] / "ai模拟玩家行为"))

from puzzle_core.online_player_simulation import (  # noqa: E402
    FEATURE_COUNT,
    ChoiceObservation,
    PlayerBoard,
    PlayerPolicy,
    ReplayEvidence,
    fit_policy,
    observe_replay,
    simulate_players,
)
from restore_player_replay import (  # noqa: E402
    ConfigCatalog,
    DEFAULT_CONFIG_DIR,
    DEFAULT_TEMPLATE_DIR,
    ReplayRestorer,
    parse_replay,
)


@dataclass(frozen=True)
class Attempt:
    player: str
    template: str
    family: str
    board_id: tuple[str, ...]
    ratio: float
    replay: ReplayEvidence


def family_of(template: str) -> str:
    name = template.lower()
    name = re.sub(r"^(?:aipuzzle|newv\d+|lesst|not|nt)+", "", name)
    return re.sub(r"(?:v\d+|s\d+)+$", "", name)


def holdout(value: str, domain: str) -> bool:
    key = hashlib.sha256(f"{domain}:{value}".encode()).digest()
    return int.from_bytes(key[:4], "big") % 4 == 0


def bag_curve(raw: str) -> tuple[int, ...]:
    return tuple(map(int, raw if raw.isdigit() else re.findall(r"\d+", raw)))


def load_attempts(
    path: Path, config_dir: Path, template_dir: Path,
) -> tuple[list[Attempt], dict[tuple[str, ...], PlayerBoard], Counter[str], dict[str, int]]:
    catalog = ConfigCatalog(config_dir)
    restorer = ReplayRestorer(catalog, template_dir, SCRIPTS, 0)
    levels = {}
    boards = {}
    reasons: Counter[str] = Counter()
    results = []
    players, templates = set(), set()
    rows = 0
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            rows += 1
            user, name = row["user_id"], row["level_template_id"]
            players.add(user)
            templates.add(name)
            if not user or row["level_value_test"] not in ("1", "true"):
                reasons["outside_cohort"] += 1
                continue
            try:
                if name not in levels:
                    if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
                        raise ValueError("Invalid template id")
                    levels[name] = restorer.parse_level_file(template_dir / f"{name}_m.bytes")
                level = levels[name]
            except (OSError, ValueError):
                reasons["template_unavailable"] += 1
                continue
            structures = sum(-23 <= tile.tile_id <= -12 for tile in level.tiles)
            if not structures or level.tower_member_indices:
                reasons["tower_or_no_structure"] += 1
                continue
            if any(row[field] != "0" for field in (
                "unlimited_ingame_extra_space_time", "extra_space_booster_num",
            )):
                reasons["initial_capacity_unverified_or_boosted"] += 1
                continue
            if not all(row[field] for field in ("level_dup_para", "level_dup_para_id", "level_id")):
                reasons["missing_observed_config"] += 1
                continue
            board_id = tuple(row[k] for k in (
                "client_version", "level_index", "level_id", "level_template_id",
                "level_dup_para", "level_dup_para_id",
            ))
            if board_id not in boards:
                resolution = catalog.resolve_level(row, 1)
                restored = restorer.reconstruct(resolution)
                try:
                    boards[board_id] = (
                        PlayerBoard.from_level(level, [tile.tile_id for tile in restored.tiles])
                        if restored.status.startswith("已还原_使用输入实际配置") else None
                    )
                except ValueError:
                    boards[board_id] = None
            board = boards[board_id]
            if board is None:
                reasons["board_not_reconstructed"] += 1
                continue
            steps = (
                (step["action_id"], step["tile_nums"]) for step in parse_replay(row["replay_info"])
            )
            replay = observe_replay(board, steps, bag_curve(row["bag_tile_change_curve"]))
            if replay.status == "won" and not (
                row["level_status"] == "pass"
                and row["prop_use_count"] == row["level_revive_count"] == row["extra_space_num"] == "0"
            ):
                replay = ReplayEvidence((), "outcome_inconsistent")
            reasons[replay.status] += 1
            if replay.evidence:
                results.append(Attempt(
                    user, name, family_of(name), board_id, structures / len(level.tiles), replay,
                ))
    return results, {key: board for key, board in boards.items() if board is not None}, reasons, {
        "input_attempts": rows, "input_players": len(players), "input_templates": len(templates),
    }


def player_preference(choices: list[ChoiceObservation]) -> float:
    """Contrast match preference with reveal preference, controlling for options."""
    def score(code: int) -> int:
        return int(bool(code & 7)) - int(bool(code & 8))

    return sum(
        score(point.selected)
        - sum(n * score(code) for code, n in point.available) / sum(n for _, n in point.available)
        for point in choices
    ) / len(choices)


def fit_mixture(attempts: list[Attempt]) -> list[PlayerPolicy]:
    users: dict[str, list[ChoiceObservation]] = defaultdict(list)
    for item in attempts:
        users[item.player].extend(item.replay.evidence)
    ranked = sorted(
        (user for user, clicks in users.items() if len(clicks) >= 20),
        key=lambda user: (player_preference(users[user]), user),
    )
    if len(ranked) < 30:
        raise ValueError("至少需要 30 名有 20 次可复验点击的训练玩家")
    first, second = len(ranked) // 3, 2 * len(ranked) // 3
    policies = []
    for group in (ranked[:first], ranked[first:second], ranked[second:]):
        weight = len(group) / len(ranked)
        choices = list(chain.from_iterable(users[user] for user in group))
        regular = [point for point in choices if not point.high_pressure]
        pressure = [point for point in choices if point.high_pressure]
        overall = fit_policy(choices, weight=weight)
        common = fit_policy(regular, weight=weight) if len(regular) >= 200 else overall
        danger = fit_policy(pressure, weight=weight) if len(pressure) >= 200 else overall
        policies.append(PlayerPolicy(weight, common.coefficients, danger.coefficients))
    return policies


def choice_log_loss(choices: list[ChoiceObservation], policies: list[PlayerPolicy]) -> float | None:
    if not choices:
        return None
    priorities = {
        pressure: [(policy.weight, policy.priorities(high_pressure=pressure)) for policy in policies]
        for pressure in (False, True)
    }
    return sum(-log(sum(
        weight * priority[point.selected]
        / sum(n * priority[code] for code, n in point.available)
        for weight, priority in priorities[point.high_pressure]
    )) for point in choices) / len(choices)


def backtest(attempts: list[Attempt], boards: dict[tuple[str, ...], PlayerBoard]) -> dict:
    train = [a for a in attempts if not holdout(a.player, "player") and not holdout(a.family, "board")]
    test = [a for a in attempts if holdout(a.player, "player") and holdout(a.family, "board")]
    fitted = fit_mixture(train)
    uniform = [PlayerPolicy(1.0, (0.0,) * FEATURE_COUNT)]
    choices = list(chain.from_iterable(a.replay.evidence for a in test))
    terminal = [a for a in test if a.replay.status in ("won", "full")]
    # Reproduce the observed mix of boards in the held-out, fully traceable sample.
    weights = Counter(a.board_id for a in terminal)
    trials = 1000 if weights else 0
    assignments = Counter(Random(37).choices(list(weights), weights=list(weights.values()), k=trials)) if trials else {}
    def simulated(policies: list[PlayerPolicy]) -> float | None:
        return sum(
            simulate_players(boards[key], policies, trials=count, seed=40 + index)
            for index, (key, count) in enumerate(assignments.items())
        ) / trials if trials else None
    return {
        "train_players": len({a.player for a in train}),
        "train_families": len({a.family for a in train}),
        "train_attempts": len(train),
        "train_clicks": sum(len(a.replay.evidence) for a in train),
        "holdout_players": len({a.player for a in test}),
        "holdout_families": len({a.family for a in test}),
        "holdout_attempts": len(test),
        "holdout_clicks": len(choices),
        "holdout_traceable_outcomes": len(terminal),
        "holdout_observed_traceable_win_rate": sum(a.replay.status == "won" for a in terminal) / len(terminal) if terminal else None,
        "holdout_fit_win_rate": simulated(fitted),
        "holdout_uniform_win_rate": simulated(uniform),
        "holdout_fit_choice_log_loss": choice_log_loss(choices, fitted),
        "holdout_uniform_choice_log_loss": choice_log_loss(choices, uniform),
        "holdout_max_structure_ratio": max((a.ratio for a in terminal), default=None),
        "simulated_players_per_policy": trials,
        "policies": [asdict(policy) for policy in fitted],
        "status": "backtest_only",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("replay_csv", type=Path)
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    parser.add_argument("--template-dir", type=Path, default=DEFAULT_TEMPLATE_DIR)
    args = parser.parse_args()
    attempts, boards, reasons, scope = load_attempts(args.replay_csv, args.config_dir, args.template_dir)
    print(json.dumps({
        **scope,
        "replay_status_counts": reasons,
        "players_with_verified_prefix": len({a.player for a in attempts}),
        "templates_with_verified_prefix": len({a.template for a in attempts}),
        "verified_prefix_clicks": sum(len(a.replay.evidence) for a in attempts),
        "max_structure_ratio": max((a.ratio for a in attempts), default=None),
        **backtest(attempts, boards),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
