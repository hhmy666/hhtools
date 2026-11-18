"""Consumption estimates from paired rollouts of learned player styles."""

import hashlib
import json
import sys
from dataclasses import dataclass
from functools import lru_cache
from importlib import import_module
from math import isfinite
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Callable

from puzzle_core.search import STRUCTURE_ID_TO_TYPE


PLAYER_TOOL = Path(__file__).resolve().parents[3] / "通关数据模拟"
PLAYER_TRIALS = 1000
PLAYER_RUNTIME = "key_structure_player_runtime"
STRUCTURE_TYPES_TO_ID = {name: tile_id for tile_id, name in STRUCTURE_ID_TO_TYPE.items()}
PLAY_METRICS = ("naked_win_rate", "assisted_win_rate", "mean_prop_uses", "mean_revives")


@lru_cache(maxsize=8)
def _read_model(path, version):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _label_id(label):
    if label.startswith("T:"):
        return int(label[2:])
    if label.startswith("S:") and label[2:] in STRUCTURE_TYPES_TO_ID:
        return STRUCTURE_TYPES_TO_ID[label[2:]]
    raise ValueError("玩家仿真需要已完成图案分配的牌面")


@dataclass
class PlayerConsumptionEstimator:
    rollout: Callable
    estimate_cost: Callable
    cost_model: dict
    entry_type: Callable
    resolve_fuuu: Callable
    config_directory: Path

    def predict(
        self, source_board, candidate_board, source_labels, candidate_labels,
        original, candidate, *, level_index, level_id, duplicate, observed_cost,
    ):
        level_min, level_max = self.cost_model["level_range"]
        if level_index is None:
            return None
        if observed_cost is None or not isfinite(observed_cost) or observed_cost <= 0:
            return None
        entropy_key = f"{level_id or source_board.template_id}:{level_index}:{duplicate}"
        entropy = int.from_bytes(hashlib.sha256(entropy_key.encode()).digest()[:8], "big")
        entry = self.entry_type(
            template_id=source_board.template_id, duplicate=duplicate, parameters=None,
            level_index=level_index, level_id=level_id,
        )
        entry, _ = self.resolve_fuuu(entry, self.config_directory)
        modeled_costs = []
        outcomes = {}
        for role, board, labels, parameter in (
            ("source", source_board, source_labels, original),
            ("candidate", candidate_board, candidate_labels, candidate),
        ):
            metrics = self.rollout(
                board, entry, SimpleNamespace(seed=int(parameter.seed), luck=int(parameter.luck)),
                PLAYER_TRIALS, entropy, materialized_labels=tuple(map(_label_id, labels)),
            )
            if metrics["player_styles"] < PLAYER_TRIALS:
                return None
            modeled_cost = self.estimate_cost(metrics, self.cost_model)
            if modeled_cost is None or not isfinite(modeled_cost) or modeled_cost < 0:
                return None
            modeled_costs.append(modeled_cost)
            outcomes[role] = {key: metrics[key] for key in PLAY_METRICS if key in metrics}
        source_cost, candidate_cost = modeled_costs
        if source_cost == 0:
            return None
        estimate = observed_cost * (candidate_cost / source_cost)
        if not isfinite(estimate):
            return None
        return {
            "value": round(estimate, 4),
            "source": "player_behavior_rollout",
            "estimate_mode": "player_rollout",
            "model_id": self.cost_model.get("version"),
            "training_scope": {"level_index_min": level_min, "level_index_max": level_max},
            "range": [None, None],
            "interval_kind": "unavailable",
            "claim_limit": "基于玩家仿真的原盘与候选盘消耗比例，按原副本线上实测值换算，需实测复核。",
            "player_rollout": {
                "trials_per_board": PLAYER_TRIALS,
                "player_styles": PLAYER_TRIALS,
                "original_modeled_cost": round(source_cost, 4),
                "candidate_modeled_cost": round(candidate_cost, 4),
                "original_observed_cost": observed_cost,
                "outside_training_range": not level_min <= level_index <= level_max,
                "fuuu": getattr(entry, "fuuu", None),
                "outcomes": outcomes,
            },
        }


def load_player_consumption_estimator(config_directory: Path) -> PlayerConsumptionEstimator:
    # Isolate the sibling tool's scripts from top-level service modules.
    runtime = ModuleType(PLAYER_RUNTIME)
    runtime.__path__ = [str(PLAYER_TOOL / "scripts")]
    sys.modules.setdefault(PLAYER_RUNTIME, runtime)
    simulation = import_module(f"{PLAYER_RUNTIME}.simulation")
    cost = import_module(f"{PLAYER_RUNTIME}.cost_model")
    fuuu = import_module(f"{PLAYER_RUNTIME}.fuuu")
    scenario = import_module(f"{PLAYER_RUNTIME}.scenario_inputs")

    data = PLAYER_TOOL / "resources" / "models"
    model_paths = {
        key: data / f"{name}.json"
        for key, name in (
            ("policy", "online-player-model"),
            ("behavior", "replay-behavior-model"),
            ("tower_choices", "tower-choice-model"),
            ("cost", "online-cost-model"),
        )
    }
    models = {
        key: _read_model(path, path.stat().st_mtime_ns)
        for key, path in model_paths.items()
    }
    if len(models["policy"]["player_policies"]) < PLAYER_TRIALS:
        raise ValueError("玩家仿真至少需要 1,000 种已学习的玩家风格")
    payload = {
        **models["policy"], "behavior": models["behavior"], "tower_choices": models["tower_choices"],
    }
    return PlayerConsumptionEstimator(
        simulation.build_engine(payload, config_directory=config_directory),
        cost.estimate, models["cost"], scenario.Entry, fuuu.enrich, config_directory,
    )
