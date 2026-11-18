"""Evaluate an unseen candidate with policies fitted from real players."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from functools import lru_cache
from heapq import nsmallest
from math import isfinite, log
from pathlib import Path
from random import Random
from typing import Sequence

from .geometry import A_TILE, B_TILE
from .online_player_simulation import FEATURE_COUNT, FEATURE_VERSION, PlayerBoard, PlayerPolicy, simulate_players
from .template_reader import LevelTemplate


MIN_PLAYER_COUNT = 1000


@lru_cache(maxsize=2)
def _read_policy_file(path: Path, modified_ns: int, size: int) -> tuple[str, tuple[PlayerPolicy, ...]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("feature_version") != FEATURE_VERSION:
        raise ValueError("玩家策略模型和当前牌面特征版本不一致")
    profiles = data.get("player_policies")
    status = data.get("status")
    if status not in ("research", "validated") or not isinstance(profiles, list):
        raise ValueError("玩家策略模型缺少回测状态或策略档案")

    def coefficients(value) -> tuple[float, ...]:
        if not isinstance(value, list) or len(value) != FEATURE_COUNT or any(
            type(item) not in (float, int) or abs(item) > 20 or not isfinite(item) for item in value
        ):
            raise ValueError("玩家策略参数无效")
        return tuple(float(item) for item in value)

    policies = []
    for profile in profiles:
        if not isinstance(profile, dict):
            raise ValueError("玩家策略档案无效")
        weight = profile.get("weight", 1.0)
        if type(weight) not in (int, float) or not 0 < weight or not isfinite(weight):
            raise ValueError("玩家策略权重无效")
        pressure = profile.get("pressure_coefficients")
        policies.append(PlayerPolicy(
            float(weight), coefficients(profile.get("coefficients")),
            coefficients(pressure) if pressure is not None else None,
        ))
    if data.get("distinct_player_profiles") != len(policies):
        raise ValueError("玩家档案数量与训练记录不一致")
    return status, tuple(policies)


def load_player_model(path: Path) -> tuple[str, tuple[PlayerPolicy, ...]]:
    stat = path.stat()
    return _read_policy_file(path, stat.st_mtime_ns, stat.st_size)


@dataclass(frozen=True)
class CandidatePlayerPrediction:
    wins: int
    player_count: int
    scenario_count: int

    @property
    def win_rate(self) -> float:
        return self.wins / self.player_count


def deal_ab_labels(level: LevelTemplate, seed: int) -> tuple[int, ...]:
    """Deal only A/B slots. Each new pattern appears in one to four triples.

    The two pools use separate patterns. Existing positive pattern IDs and
    authored C–N IDs are preserved verbatim.
    """
    rng = Random(seed)
    labels = [tile.tile_id for tile in level.tiles]
    next_pattern = max([0, *labels]) + 1

    for pool in (A_TILE, B_TILE):
        indices = [index for index, label in enumerate(labels) if label == pool]
        group_count, remainder = divmod(len(indices), 3)
        if remainder:
            pool_name = "A" if pool == A_TILE else "B"
            raise ValueError(f"{pool_name} 牌数量需要是 3 的倍数")

        dealt = []
        while group_count:
            groups = rng.randint(1, min(4, group_count))
            dealt.extend([next_pattern] * (groups * 3))
            next_pattern += 1
            group_count -= groups
        rng.shuffle(dealt)
        for index, pattern in zip(indices, dealt):
            labels[index] = pattern

    return tuple(labels)


def _sample_profiles(
    policies: Sequence[PlayerPolicy], player_count: int, rng: Random
) -> list[int]:
    weights = [policy.weight for policy in policies]
    if any(not isfinite(weight) or weight <= 0 for weight in weights):
        raise ValueError("玩家抽样权重必须是有限正数")
    indices = range(len(policies))
    if all(weight == weights[0] for weight in weights):
        return rng.sample(indices, player_count)

    # Independent exponential clocks give a weighted sample without replacement.
    # Compare log-clocks to keep the ordering stable for extreme finite weights.
    def log_clock(index: int) -> float:
        uniform = max(rng.random(), 1e-300)
        return log(-log(uniform)) - log(weights[index])

    sampled = nsmallest(player_count, indices, key=log_clock)
    rng.shuffle(sampled)
    return sampled


def predict_player_candidate(
    level: LevelTemplate,
    policies: Sequence[PlayerPolicy],
    *,
    scenario_count: int = 10,
    player_count: int = 1200,
    seed: int = 0,
) -> CandidatePlayerPrediction:
    """Run one seven-slot game per distinct player profile, with equal deal weights.

    Callers supply one profile per source player. The candidate provides its
    own geometry and fixed patterns, including layouts absent from training.
    """
    if type(player_count) is not int or player_count < MIN_PLAYER_COUNT:
        raise ValueError(f"至少模拟 {MIN_PLAYER_COUNT} 名不同玩家")
    if type(scenario_count) is not int or scenario_count <= 0 or player_count % scenario_count:
        raise ValueError("牌面方案数必须为正整数，并均分所有模拟玩家")
    if type(seed) is not int:
        raise ValueError("随机种子必须为整数")
    if len(policies) < player_count:
        raise ValueError(f"需要 {player_count} 份不同玩家的策略档案，目前有 {len(policies)} 份")
    if level.tower_member_indices or any(tile.has_debuff_index for tile in level.tiles):
        raise ValueError("候选包含动态塔或特殊牌，当前七格模拟无法还原其规则")

    rng = Random(seed)
    sampled = _sample_profiles(policies, player_count, rng)
    players_per_board = player_count // scenario_count
    base_board = None
    wins = 0
    for start in range(0, player_count, players_per_board):
        labels = deal_ab_labels(level, rng.getrandbits(64))
        if base_board is None:
            base_board = PlayerBoard.from_level(level, labels)
            board = base_board
        else:
            # Geometry and fixed labels are identical across the hypothetical
            # deals. The deal itself guarantees valid triple counts in A and B.
            board = replace(base_board, labels=labels)
        for index in sampled[start:start + players_per_board]:
            wins += simulate_players(board, (policies[index],), trials=1, seed=rng.getrandbits(64))
    return CandidatePlayerPrediction(wins, player_count, scenario_count)
