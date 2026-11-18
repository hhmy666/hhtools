"""Fit a small tower-choice correction using corroborated pre-prop replay segments.

A click contributes only when both tower and non-tower choices are available.
A prefix is excluded in full if any click before the first prop or replay end
conflicts with the reconstructed tile identity, tray size, or surface count.
The model contains aggregate counts, corrections and evaluation metrics only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from random import Random
from tempfile import NamedTemporaryFile

from fit_replay_behavior import STEP
from fuuu import CONFIG, enrich
from scenario_inputs import Entry, Parameters
from sources import default_repository, model_path, read_template, repository_directory
from simulation import OldLuck, PlayerBoard, State, LegacyRandom, _deal, _geometry, _local_config, _priority_tables

BAG_HIGH = 5
TOWER_LOW_MAX = 12
MIN_REPLAY_CLICKS = 8
MIN_FIT_ATTEMPTS = 12
L2_PENALTY = 5.0
VERSION = "old-L-tower-choice-log-offset-v1"


def _family(template):
    template = re.sub(r"^(?:aipuzzle|newv[0-9]+|lesst|not|nt)+", "", template.lower())
    return re.sub(r"(?:v[0-9]+|s[0-9]+)+$", "", template)


def _holdout(family):
    return int.from_bytes(hashlib.sha256(f"tower-v1:{family}".encode()).digest()[:4], "big") % 5 == 0


@dataclass(frozen=True)
class Choice:
    bag_high: bool
    tower_low: bool
    baseline_log_odds: float
    chose_tower: bool

    @property
    def context(self):
        return self.bag_high, self.tower_low


@dataclass
class Replay:
    family: str
    player: tuple[str, str]
    choices: list[Choice]
    verified_clicks: int
    level_index: int


def _reference_tables(model):
    policies = model.get("player_policies", ())
    if not policies:
        raise ValueError("需要现有的真人点击模型。")
    weights = [float(policy["weight"]) for policy in policies]
    weight_total = sum(weights)
    phase_coefficients = []
    for high in (False, True):
        coefficients = [
            (policy.get("pressure_coefficients") if high else None) or policy["coefficients"]
            for policy in policies
        ]
        phase_coefficients.append(tuple(
            sum(weight * value for weight, value in zip(weights, column)) / weight_total
            for column in zip(*coefficients)
        ))
    return tuple(_priority_tables(values) for values in phase_coefficients)


def _choice(state, remaining_tower, selected, reference):
    bag_high = state.size >= BAG_HIGH
    low, high = reference[bag_high]
    tower_weight = other_weight = 0.0
    for index, code in state.choices().items():
        weight = low[code & 127] * high[code >> 7]
        if state.tower_for[index] is not None:
            tower_weight += weight
        else:
            other_weight += weight
    if not (tower_weight and other_weight):
        return None
    return Choice(
        bag_high=bag_high,
        tower_low=remaining_tower <= TOWER_LOW_MAX,
        baseline_log_odds=math.log(tower_weight / other_weight),
        chose_tower=state.tower_for[selected] is not None,
    )


def _verified_choices(row, data, entry, factors, tower_enabled, hidden_type, stages, reference):
    labels, geometry = data
    blockers, dependents, face_masks, positions, tower_for, towers = geometry
    board = PlayerBoard(labels.copy(), blockers, dependents, face_masks, positions)
    state = State(board, towers, tower_for, stages)
    remaining_tower = sum(index is not None for index in tower_for)
    lucky = OldLuck(
        LegacyRandom(1611232022 + entry.parameters.seed), *factors,
        entry.parameters.luck, 0, tower_enabled, hidden_type,
    )
    bag = row["bag_tile_change_curve"].strip()
    try:
        surface = [int(item) for item in row["surface_normal_tile_change_curve"].split("-")]
    except ValueError:
        return [], 0, "surface_missing"
    if not bag.isascii() or not bag.isdecimal() or len(surface) != len(bag):
        return [], 0, "curves_unaligned"
    if max(bag) > "7":
        return [], 0, "larger_tray"

    verified = 0
    choices = []
    for raw in row["replay_info"].split("|"):
        match = STEP.fullmatch(raw)
        if match is None:
            return [], verified, "invalid_step"
        if match.group(2) != "1":
            break
        _, separator, tile_number = raw.partition(":")
        if not separator or not tile_number.isdigit() or verified >= len(bag):
            return [], verified, "invalid_click"
        identity = int(tile_number) - 1
        if not 0 <= identity < len(state.slot_for_identity):
            return [], verified, "invalid_tile_identity"
        position = state.slot_for_identity[identity]
        if position not in state.available or state.size >= state.capacity:
            return [], verified, "not_clickable"
        observation = _choice(state, remaining_tower, position, reference)
        state.play(position, lucky)
        if int(bag[verified]) != state.size:
            return [], verified, "tray_differs"
        if surface[verified] != len(state.available):
            return [], verified, "surface_differs"
        if observation is not None:
            choices.append(observation)
        remaining_tower -= state.tower_for[position] is not None
        verified += 1

    if verified < MIN_REPLAY_CLICKS or not choices:
        return [], verified, "insufficient_corroborated_choices"
    return choices, verified, "accepted"


def _metadata():
    with (CONFIG / "config_level.csv").open(encoding="utf-8-sig", newline="") as stream:
        return {row["LevelId"]: row for row in csv.DictReader(stream)}


def collect(replay_paths, repository, reference):
    metadata = _metadata()
    default_factors, fuuu, ranges, catalog, tower_enabled, hidden_type, stages = _local_config()
    loaded = []
    stats = Counter()
    seen = set()

    @lru_cache(maxsize=512)
    def board_data(template, index, params):
        level = read_template(template, repository)
        if not level.towers:
            return None, "without_tower"
        if any(tile.has_debuff_index for tile in level.tiles):
            return None, "debuff"
        return (_deal(level, params, index, catalog, ranges), _geometry(level)), None

    for path in replay_paths:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                stats["input_rows"] += 1
                player = (row["product"], row["user_id"])
                raw = row["replay_info"]
                key = (player, row["level_id"], row["level_start_time"], raw)
                if key in seen:
                    stats["duplicate"] += 1
                    continue
                seen.add(key)
                if not all(player) or row["replay_variants"] not in ("", "1"):
                    stats["ambiguous"] += 1
                    continue
                if any(row.get(name) not in (None, "", "0") for name in (
                    "extra_space_booster_num", "unlimited_ingame_extra_space_time",
                )):
                    stats["larger_tray"] += 1
                    continue
                level_id, template = row["level_id"], row["level_template_id"]
                config = metadata.get(level_id)
                if not config or config.get("TemplateId") != template:
                    stats["local_config_missing_or_differs"] += 1
                    continue
                if any(config.get(field) not in (None, "", "0") for field in (
                    "StrategyGroupId", "RandomSeed_groupID", "randomSeed_groupID",
                )):
                    stats["dynamic_strategy"] += 1
                    continue
                try:
                    level_index = int(row["level_index"])
                    duplicate = int(row["level_dup_para_id"])
                    params = Parameters(*(int(item) for item in row["level_dup_para"].split("|")))
                except (TypeError, ValueError):
                    stats["invalid_params"] += 1
                    continue
                if not 300 <= level_index <= 1500:
                    stats["outside_requested_levels"] += 1
                    continue
                if config.get(f"DupPara{duplicate}") != row["level_dup_para"]:
                    stats["dup_parameters_differ"] += 1
                    continue
                try:
                    data, reason = board_data(template, level_index, params)
                except (OSError, ValueError):
                    stats["board_missing_or_unsupported"] += 1
                    continue
                if reason:
                    stats[reason] += 1
                    continue
                entry, note = enrich(Entry(template, duplicate, params, level_id=level_id, level_index=level_index))
                if note:
                    stats["default_fuuu"] += 1
                choices, verified, status = _verified_choices(
                    row, data, entry, fuuu.get(entry.fuuu, default_factors), tower_enabled, hidden_type, stages, reference,
                )
                stats[status] += 1
                if choices:
                    loaded.append(Replay(_family(template), player, choices, verified, level_index))
    return loaded, stats


def _sigmoid(value):
    return 1 / (1 + math.exp(-value))


def _fit_offset(rows):
    offset = 0.0
    for _ in range(16):
        gradient = L2_PENALTY * offset
        curvature = L2_PENALTY
        for choice, weight in rows:
            probability = _sigmoid(choice.baseline_log_odds + offset)
            gradient += weight * (probability - choice.chose_tower)
            curvature += weight * probability * (1 - probability)
        change = gradient / curvature
        offset -= change
        if abs(change) < 1e-7:
            break
    return max(-2.0, min(2.0, offset))


def _offsets(replays):
    contexts = defaultdict(list)
    attempt_counts = Counter()
    for replay in replays:
        weight = 1 / len(replay.choices)
        for choice in replay.choices:
            contexts[choice.context].append((choice, weight))
        attempt_counts.update({choice.context for choice in replay.choices})
    return [
        {
            "bag_high": bag_high,
            "tower_low": tower_low,
            "n": len(rows),
            "attempts": attempt_counts[(bag_high, tower_low)],
            "log_multiplier": _fit_offset(rows) if attempt_counts[(bag_high, tower_low)] >= MIN_FIT_ATTEMPTS else 0.0,
        }
        for (bag_high, tower_low), rows in sorted(contexts.items())
    ]


def _counts(replays):
    return {
        "replays": len(replays),
        "players": len({replay.player for replay in replays}),
        "template_families": len({replay.family for replay in replays}),
        "corroborated_clicks": sum(replay.verified_clicks for replay in replays),
        "mixed_tower_choices": sum(len(replay.choices) for replay in replays),
    }


def _loss(replay, offsets):
    terms = []
    for choice in replay.choices:
        value = choice.baseline_log_odds + offsets.get(choice.context, 0.0)
        signed_value = -value if choice.chose_tower else value
        terms.append(math.log1p(math.exp(signed_value)))
    return sum(terms) / len(terms)


def _heldout_losses(holdout, offsets):
    blocks = defaultdict(list)
    baseline_total = calibrated_total = 0.0
    for replay in holdout:
        baseline = _loss(replay, {})
        calibrated = _loss(replay, offsets)
        baseline_total += baseline
        calibrated_total += calibrated
        blocks[replay.family].append(calibrated - baseline)
    aggregates = [(sum(differences), len(differences)) for differences in blocks.values()]
    rng = Random(617)
    changes = []
    for _ in range(2000):
        sample = rng.choices(aggregates, k=len(aggregates))
        changes.append(sum(difference for difference, _ in sample) / sum(size for _, size in sample))
    changes.sort()
    return {
        "baseline_binary_log_loss": baseline_total / len(holdout),
        "calibrated_binary_log_loss": calibrated_total / len(holdout),
        "calibrated_minus_baseline_family_bootstrap_95pct": [changes[50], changes[1949]],
    }


def fit(replay_paths, repository, model):
    replays, audit = collect(replay_paths, repository, _reference_tables(model))
    holdout = [replay for replay in replays if _holdout(replay.family)]
    holdout_players = {replay.player for replay in holdout}
    train = [replay for replay in replays if not _holdout(replay.family) and replay.player not in holdout_players]
    if len(train) < 50 or len(holdout) < 25:
        raise ValueError(f"可用的玩家与模板独立样本不足（拟合 {len(train)}、留出 {len(holdout)}）。")
    fitted_offsets = _offsets(train)
    offsets = {(item["bag_high"], item["tower_low"]): item["log_multiplier"] for item in fitted_offsets}
    return {
        "version": VERSION,
        "feature_version": model["feature_version"],
        "source_files": [path.name for path in replay_paths],
        "level_range": [min(replay.level_index for replay in replays), max(replay.level_index for replay in replays)],
        "bag_high_min": BAG_HIGH,
        "tower_low_max": TOWER_LOW_MAX,
        "training": _counts(train),
        "holdout": {
            **_counts(holdout),
            **_heldout_losses(holdout, offsets),
            "sampling_unit": "replay",
            "split": "disjoint template families and player identifiers",
        },
        "audit": dict(audit),
        "bias_by_context": fitted_offsets,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("replays", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--template-dir", default=default_repository())
    parser.add_argument("--player-model", type=Path, default=model_path())
    args = parser.parse_args()
    result = fit(
        args.replays, repository_directory(args.template_dir),
        json.loads(args.player_model.read_text(encoding="utf-8")),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    holdout = result["holdout"]
    if holdout["calibrated_binary_log_loss"] >= holdout["baseline_binary_log_loss"]:
        parser.exit(2, "留出集未改善，未启用新校正。\n")
    with NamedTemporaryFile(mode="w", dir=args.output.parent, encoding="utf-8", delete=False) as stream:
        temp = Path(stream.name)
        json.dump(result, stream, ensure_ascii=False, separators=(",", ":"))
    try:
        temp.replace(args.output)
    finally:
        temp.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
