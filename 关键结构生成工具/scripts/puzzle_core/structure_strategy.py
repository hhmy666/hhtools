"""Deterministic local replay of configured puzzle-structure reduction rules.

The authoritative product semantics come from Confluence page 277080406
(``【PRD】谜题结构降难度``), version 85.  The configuration grammar is:

``selection_mode | successful_strategy_count | strategy_id;...``

``selection_mode`` is 0 (disabled), 1 (ordered), or 2 (random order).  Failed
strategies are skipped and later entries fill the requested successful count.
The production client owns the exact random stream.  This module therefore
uses a stable, explicit local sampler so repeated reviews are reproducible;
the returned evidence never calls a random branch an exact client replay.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


STRUCTURE_TYPES = frozenset(chr(code) for code in range(ord("C"), ord("N") + 1))
STRATEGY_REPLAY_VERSION = "structure-reduction-prd-v85-local-sampler-v1"


@dataclass(frozen=True)
class StrategyRule:
    mode: int
    requested_count: int
    strategy_ids: tuple[int, ...]


def parse_strategy_rule(raw: str | None) -> StrategyRule:
    value = str(raw or "").strip()
    if not value:
        raise ValueError("结构降难策略配置为空")
    parts = value.split("|", 2)
    if len(parts) != 3:
        raise ValueError("结构降难策略必须是 应用规则|数量|策略列表")
    try:
        mode = int(parts[0])
        requested_count = int(parts[1])
        strategy_ids = tuple(
            int(item) for item in parts[2].split(";") if item.strip()
        )
    except ValueError as exc:
        raise ValueError("结构降难策略含非整数参数") from exc
    if mode not in (0, 1, 2):
        raise ValueError("结构降难策略应用规则只能是 0、1 或 2")
    if requested_count < 0:
        raise ValueError("结构降难策略数量不能为负数")
    if mode == 0:
        return StrategyRule(0, 0, ())
    if any(item < 1 or item > 8 for item in strategy_ids):
        raise ValueError("结构降难策略 ID 必须在 1–8 之间")
    if requested_count == 0:
        return StrategyRule(mode, 0, strategy_ids)
    if not strategy_ids:
        raise ValueError("启用结构降难时策略列表不能为空")
    return StrategyRule(mode, requested_count, strategy_ids)


def _rng(*parts: object) -> random.Random:
    digest = hashlib.sha256(
        "|".join(str(part) for part in parts).encode("utf-8")
    ).digest()
    return random.Random(int.from_bytes(digest[:8], "big", signed=False))


def _structure_types(symbols: Sequence[str]) -> list[str]:
    return sorted({value[2:] for value in symbols if value.startswith("S:")})


def _structure_layers(
    symbols: Sequence[str], z_by_index: Sequence[int]
) -> list[int]:
    return sorted(
        {z_by_index[index] for index, value in enumerate(symbols) if value.startswith("S:")},
        reverse=True,
    )


def _replace_structure_type(symbols: list[str], source: str, target: str) -> int:
    changed = 0
    for index, value in enumerate(symbols):
        if value == f"S:{source}":
            symbols[index] = target
            changed += 1
    return changed


def _apply_one(
    symbols: list[str],
    z_by_index: Sequence[int],
    strategy_id: int,
    sampler: random.Random,
) -> tuple[bool, dict[str, Any]]:
    types = _structure_types(symbols)
    layers = _structure_layers(symbols, z_by_index)
    detail: dict[str, Any] = {"strategy_id": strategy_id}

    if strategy_id in (1, 2):
        if len(layers) < 2:
            return False, {**detail, "reason": "谜题结构不足两层"}
        target_layer = layers[1]
        eligible = sorted(
            {
                symbols[index][2:]
                for index, z in enumerate(z_by_index)
                if z == target_layer and symbols[index].startswith("S:")
            }
        )
        if not eligible:
            return False, {**detail, "reason": "第二结构层没有结构块"}
        selected = sampler.choice(eligible)
        target = "A" if strategy_id == 1 else "B"
        changed = _replace_structure_type(symbols, selected, target)
        return True, {
            **detail,
            "selected_structure_type": selected,
            "target_placeholder": target,
            "changed_tile_count": changed,
        }

    if strategy_id in (3, 5):
        if not layers:
            return False, {**detail, "reason": "模板没有结构块"}
        target_layer = layers[0]
        eligible = sorted(
            {
                symbols[index][2:]
                for index, z in enumerate(z_by_index)
                if z == target_layer and symbols[index].startswith("S:")
            }
        )
        if not eligible:
            return False, {**detail, "reason": "最上结构层没有结构块"}
        selected = sampler.choice(eligible)
        target = "A" if strategy_id == 3 else "B"
        changed = _replace_structure_type(symbols, selected, target)
        return True, {
            **detail,
            "selected_structure_type": selected,
            "target_placeholder": target,
            "changed_tile_count": changed,
        }

    if strategy_id == 4:
        if not types:
            return False, {**detail, "reason": "模板没有可复用的结构块类型"}
        occupied_layers = sorted(set(z_by_index))
        # Wiki PRD v85: search the second-bottom layer first, then fall back
        # to the bottom layer.  One triplet must come from one layer; tiles
        # from the two layers cannot be pooled into a synthetic group.
        bottom_layers = list(reversed(occupied_layers[:2]))
        selected_indices: list[int] = []
        selected_placeholder = ""
        selected_layer: int | None = None
        for layer in bottom_layers:
            for placeholder in ("A", "B"):
                eligible = [
                    index
                    for index, (value, z) in enumerate(zip(symbols, z_by_index))
                    if value == placeholder and z == layer
                ]
                if len(eligible) < 3:
                    continue
                sampler.shuffle(eligible)
                selected_indices = sorted(eligible[:3])
                selected_placeholder = placeholder
                selected_layer = layer
                break
            if selected_indices:
                break
        if not selected_indices:
            return False, {
                **detail,
                "reason": "倒数第二层与最底层均无同层三张 A/B",
            }
        selected_type = sampler.choice(types)
        for index in selected_indices:
            symbols[index] = f"S:{selected_type}"
        return True, {
            **detail,
            "selected_placeholder": selected_placeholder,
            "selected_layer": selected_layer,
            "selected_structure_type": selected_type,
            "changed_tile_indices": selected_indices,
            "changed_tile_count": 3,
        }

    if strategy_id == 6:
        if len(types) < 2:
            return False, {**detail, "reason": "结构块类型不足两种"}
        source = sampler.choice(types)
        targets = [value for value in types if value != source]
        target = sampler.choice(targets)
        changed = _replace_structure_type(symbols, source, f"S:{target}")
        return True, {
            **detail,
            "selected_structure_type": source,
            "target_structure_type": target,
            "changed_tile_count": changed,
        }

    if strategy_id in (7, 8):
        if not types:
            return False, {**detail, "reason": "模板没有结构块"}
        a_count = sum(value == "A" for value in symbols)
        b_count = sum(value == "B" for value in symbols)
        if strategy_id == 7:
            target = "A" if a_count >= 12 else "B"
        else:
            target = "A" if b_count < 12 and a_count >= 12 else "B"
        changed = 0
        for index, value in enumerate(symbols):
            if value.startswith("S:"):
                symbols[index] = target
                changed += 1
        return True, {
            **detail,
            "target_placeholder": target,
            "a_tile_count_before": a_count,
            "b_tile_count_before": b_count,
            "changed_tile_count": changed,
        }

    return False, {**detail, "reason": "未知策略"}


def replay_structure_strategy(
    symbols: Sequence[str],
    z_by_index: Sequence[int],
    raw_rule: str | None,
    *,
    strategy_group_id: str,
    duplicate_index: int,
    duplicate_seed: str,
    board_role: str,
) -> tuple[tuple[str, ...], dict[str, Any]]:
    """Apply one duplicate's configured rule and return labels plus evidence."""

    if len(symbols) != len(z_by_index):
        raise ValueError("策略回放的标签数与层数不一致")
    rule = parse_strategy_rule(raw_rule)
    if rule.mode == 0 or rule.requested_count == 0:
        return tuple(symbols), {
            "status": "not_applicable",
            "version": STRATEGY_REPLAY_VERSION,
            "raw_rule": str(raw_rule or ""),
            "effective_strategies": [],
            "failed_strategies": [],
            "random_semantics": "none",
        }

    sampler = _rng(
        STRATEGY_REPLAY_VERSION,
        strategy_group_id,
        duplicate_index,
    )
    ordered = list(rule.strategy_ids)
    if rule.mode == 2:
        sampler.shuffle(ordered)
    output = list(symbols)
    effective: list[int] = []
    failed: list[int] = []
    attempts: list[Mapping[str, Any]] = []
    for strategy_id in ordered:
        if len(effective) >= rule.requested_count:
            break
        succeeded, detail = _apply_one(
            output, z_by_index, strategy_id, sampler
        )
        attempts.append({**detail, "succeeded": succeeded})
        if succeeded:
            effective.append(strategy_id)
        else:
            failed.append(strategy_id)
    return tuple(output), {
        "status": "replayed",
        "version": STRATEGY_REPLAY_VERSION,
        "raw_rule": str(raw_rule or ""),
        "selection_mode": "ordered" if rule.mode == 1 else "random",
        "requested_count": rule.requested_count,
        "candidate_order": ordered,
        "effective_strategies": effective,
        "failed_strategies": failed,
        "attempts": attempts,
        "random_semantics": (
            "stable_local_sample_not_exact_client_random_stream"
            if rule.mode == 2
            else "only_strategy_internal_choices_use_stable_local_sample"
        ),
        "paired_sampling": "source_and_candidate_share_local_random_stream",
        "dup_parameter_seed_used_for_strategy": False,
        "dup_parameter_seed": str(duplicate_seed),
        "board_role": board_role,
    }


__all__ = [
    "STRATEGY_REPLAY_VERSION",
    "StrategyRule",
    "parse_strategy_rule",
    "replay_structure_strategy",
]
