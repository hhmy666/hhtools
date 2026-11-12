"""Read-only target-consumption calibration for exported templates.

The service intentionally separates three evidence layers:

* source-template online aggregates supply the primary per-duplicate anchor;
* a versioned, bounded s5 empirical structure correction can be
  explicitly labeled as an unvalidated transfer estimate outside that band;
* same-duplicate/same-strategy-group neighbours remain contextual evidence,
  never the primary forecast or a causal Seed effect;
* the deterministic whole-board simulator uses current local legacy-Seed
  config inputs when available, otherwise a labeled synthetic fallback.  Both
  supply a directional structural distance, never a consumption forecast.

No function in this module writes ``config_level`` or any other configuration
table.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence

from puzzle_core.difficulty import (
    DuplicateParameter,
    compare_duplicate_difficulty_pair,
)
from puzzle_core.errors import InputValidationError, PuzzleToolError
from puzzle_core.geometry import build_geometry, build_static_geometry
from puzzle_core.legacy_materialization import (
    LEGACY_EVIDENCE_LEVEL,
    materialize_legacy_ab_from_config,
    parse_client_tile_config,
    parse_level_tile_config,
)
from puzzle_core.structure_strategy import replay_structure_strategy
from puzzle_core.solver import SolverLimits, analyze_puzzle
from puzzle_core.search import STRUCTURE_ID_TO_TYPE

from .import_service import load_template_id
from .parameter_recommendation_service import (
    DupParam,
    build_parameter_recommendations,
    discover_config_sets,
    manifest_replacement_type_counts,
    parse_dup_param,
    source_tile_counts,
)
from .path_service import is_within
from .profile_service import Profile


MAX_HISTORY_BYTES = 24 * 1024 * 1024
MAX_HISTORY_ROWS = 100_000
DEFAULT_STRATEGY_GROUP_ID = "1003"
DUPLICATE_OBJECTIVE_WEIGHTS = {
    1: 4.0,
    2: 4.0,
    3: 2.0,
    4: 2.0,
    **{index: 1.0 for index in range(5, 11)},
}
AB_PREFILTER_LIMIT_PER_DUPLICATE = 6
SEED_PREFILTER_LIMIT_PER_AB = 2
DP_SHORTLIST_LIMIT_PER_DUPLICATE = 12
STRUCTURE_CORRECTION_MODEL_PATH = (
    Path(__file__).resolve().parents[1]
    / "puzzle_core"
    / "data"
    / "consumption_structure_correction_20261027_v2.json"
)
STATIC_STRUCTURE_FEATURE_NAMES = frozenset(
    {
        "type_count",
        "replaced_a_fraction",
        "replaced_b_fraction",
        "source_a_fraction",
        "replacement_fraction",
        "external_a_per_structure",
        "external_b_per_structure",
        "initial_entry_per_structure",
        "blocker_edges_per_structure",
        "nonzero_masks_per_structure",
        "minimum_failure_click",
        "log_complete_paths",
    }
)
REQUIRED_HISTORY_COLUMNS = (
    "level_template_id",
    "level_dup",
    "level_dup_para_id",
    "进入人数",
    "人均消耗",
)
OPTIONAL_HISTORY_COLUMNS = (
    "strategy_group_id",
    "strategy_effective_list",
)


def _duplicate_objective_weight(duplicate_index: int) -> float:
    try:
        return DUPLICATE_OBJECTIVE_WEIGHTS[int(duplicate_index)]
    except (KeyError, TypeError, ValueError) as exc:
        raise InputValidationError("副本编号必须在 1–10") from exc


def _load_structure_correction_model() -> dict[str, Any] | None:
    """Load a checked-in, versioned coefficient artifact or fail closed."""

    try:
        payload = json.loads(STRUCTURE_CORRECTION_MODEL_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    required = {
        "model_id",
        "scope",
        "feature_order",
        "means",
        "scales",
        "coefficients",
        "intercept",
        "log_ratio_clip",
    }
    if not isinstance(payload, Mapping) or not required.issubset(payload):
        return None
    scope = payload.get("scope")
    required_scope = {
        "level_index_min",
        "level_index_max",
        "signatures",
        "structure_count_min",
        "structure_count_max",
        "strategy_groups_observed",
    }
    if not isinstance(scope, Mapping) or not required_scope.issubset(scope):
        return None
    feature_order = payload.get("feature_order")
    means = payload.get("means")
    scales = payload.get("scales")
    coefficients = payload.get("coefficients")
    if (
        not isinstance(feature_order, list)
        or not feature_order
        or len(feature_order) != len(set(feature_order))
        or not all(isinstance(name, str) and name for name in feature_order)
        or not isinstance(means, Mapping)
        or not isinstance(scales, Mapping)
        or not isinstance(coefficients, Mapping)
        or set(means) != set(feature_order)
        or set(scales) != set(feature_order)
        or set(coefficients) != set(feature_order)
    ):
        return None
    try:
        numeric_values = [
            float(payload["intercept"]),
            *(float(means[name]) for name in feature_order),
            *(float(scales[name]) for name in feature_order),
            *(float(coefficients[name]) for name in feature_order),
            *(float(value) for value in payload["log_ratio_clip"]),
        ]
    except (KeyError, TypeError, ValueError):
        return None
    if (
        len(payload["log_ratio_clip"]) != 2
        or not all(math.isfinite(value) for value in numeric_values)
        or any(abs(float(scales[name])) < 1e-12 for name in feature_order)
    ):
        return None
    return dict(payload)


def _capacity_seven_proof(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    units = manifest.get("puzzle_units")
    if not isinstance(units, list) or not units:
        return {}
    first = units[0] if isinstance(units[0], Mapping) else {}
    proof = first.get("proof") if isinstance(first, Mapping) else {}
    capacity_proofs = proof.get("capacity_proofs") if isinstance(proof, Mapping) else []
    if not isinstance(capacity_proofs, list):
        return {}
    return next(
        (
            item
            for item in capacity_proofs
            if isinstance(item, Mapping) and int(item.get("capacity") or 0) == 7
        ),
        {},
    )


def _empirical_static_structure_features(
    level: Any,
    replacements: Sequence[SimpleNamespace],
) -> dict[str, float]:
    """Reproduce the static feature extractor used to fit the checked-in model."""

    if not replacements:
        raise ValueError("结构校正特征至少需要一个替换位")
    replacement_by_index = {
        int(item.tile_index): str(item.structure_type).upper()
        for item in replacements
    }
    structure_indices = tuple(sorted(replacement_by_index))
    if len(structure_indices) != len(replacements):
        raise ValueError("结构校正特征不允许重复替换位")
    if any(index < 0 or index >= len(level.tiles) for index in structure_indices):
        raise ValueError("结构校正特征的替换位越界")
    labels = tuple(replacement_by_index[index] for index in structure_indices)
    if any(len(label) != 1 or not "C" <= label <= "N" for label in labels):
        raise ValueError("结构校正特征只支持 C–N")
    graph = build_static_geometry(level)
    if any(index not in graph.included_indices for index in structure_indices):
        raise ValueError("校准样本域不包含塔内结构")
    replaced_a = sum(
        int(level.tiles[index].tile_id) == -10 for index in structure_indices
    )
    replaced_b = sum(
        int(level.tiles[index].tile_id) == -11 for index in structure_indices
    )
    if replaced_a + replaced_b != len(structure_indices):
        raise ValueError("结构校正只支持从 A/B 占位牌替换的结构块")
    local = {source_index: index for index, source_index in enumerate(structure_indices)}
    blocker_masks = tuple(
        sum(
            1 << local[blocker]
            for blocker in graph.direct_blockers[source_index]
            if blocker in local
        )
        for source_index in structure_indices
    )
    structure_set = frozenset(structure_indices)
    external_a = {
        blocker
        for source_index in structure_indices
        for blocker in graph.transitive_blockers[source_index]
        if blocker not in structure_set and int(level.tiles[blocker].tile_id) == -10
    }
    external_b = {
        blocker
        for source_index in structure_indices
        for blocker in graph.transitive_blockers[source_index]
        if blocker not in structure_set and int(level.tiles[blocker].tile_id) == -11
    }
    proof = analyze_puzzle(
        blocker_masks,
        labels,
        threshold_type=sorted(set(labels))[0],
        capacities=(7,),
        limits=SolverLimits(max_states=1_000_000, max_path_count=10**15),
    )
    capacity = proof.capacity_proofs[0]
    return {
        "source_total": float(len(level.tiles)),
        "source_tile_a_count": float(
            sum(int(tile.tile_id) == -10 for tile in level.tiles)
        ),
        "source_tile_b_count": float(
            sum(int(tile.tile_id) == -11 for tile in level.tiles)
        ),
        "replacement_count": float(len(structure_indices)),
        "type_count": float(len(set(labels))),
        "replaced_a": float(replaced_a),
        "replaced_b": float(replaced_b),
        "external_a": float(len(external_a)),
        "external_b": float(len(external_b)),
        "initial_entry": float(
            sum(not graph.direct_blockers[index] for index in structure_indices)
        ),
        "blocker_edges": float(
            sum(bin(mask).count("1") for mask in blocker_masks)
        ),
        "nonzero_masks": float(sum(mask != 0 for mask in blocker_masks)),
        "min_failure_click": float(capacity.minimum_failure_click or 10),
        "complete_paths": float(capacity.complete_path_count),
    }


def _paired_board_features(source_level: Any, anchor_level: Any) -> dict[str, float]:
    """Compare the processed source with the board whose online target is observed."""
    source, anchor = source_level.tiles, anchor_level.tiles
    if not source or not anchor:
        raise ValueError("empty paired board")
    return {
        "source_tower_fraction": sum(t.is_tower_member for t in source) / len(source),
        "anchor_tower_fraction": sum(t.is_tower_member for t in anchor) / len(anchor),
        "anchor_structure_fraction": sum(t.tile_id in STRUCTURE_ID_TO_TYPE for t in anchor) / len(anchor),
        "log_source_tiles": math.log(len(source)),
        "log_tile_ratio": math.log(len(source) / len(anchor)),
        "source_max_layer": max(t.layer for t in source) / 10.0,
        "layer_delta": (max(t.layer for t in source) - max(t.layer for t in anchor)) / 10.0,
    }


def _structure_correction_features(
    manifest: Mapping[str, Any],
    *,
    target: float,
    original: DupParam,
    candidate: DupParam,
    duplicate_index: int,
    level_index: int,
    static_structure_features: Mapping[str, float] | None = None,
) -> dict[str, float]:
    validation = manifest.get("validation")
    validation = validation if isinstance(validation, Mapping) else {}
    source_counts = manifest.get("source_tile_counts")
    source_counts = source_counts if isinstance(source_counts, Mapping) else {}
    source_a = float(source_counts.get("A") or 0.0)
    source_b = float(source_counts.get("B") or 0.0)
    source_total = max(1.0, source_a + source_b)
    replacements = manifest.get("replacements")
    replacements = replacements if isinstance(replacements, list) else []
    replacement_count = float(
        validation.get("replacement_count") or len(replacements)
    )
    structure_counts = validation.get("structure_counts")
    structure_counts = structure_counts if isinstance(structure_counts, list) else []
    type_count = float(
        len(structure_counts)
        or len(
            {
                str(item.get("structure_type"))
                for item in replacements
                if isinstance(item, Mapping) and item.get("structure_type")
            }
        )
    )
    replaced_a = float(
        validation.get("replaced_a_count")
        or sum(
            1
            for item in replacements
            if isinstance(item, Mapping) and item.get("source_tile_id") == -10
        )
    )
    replaced_b = float(
        validation.get("replaced_b_count")
        or sum(
            1
            for item in replacements
            if isinstance(item, Mapping) and item.get("source_tile_id") == -11
        )
    )
    units = manifest.get("puzzle_units")
    units = units if isinstance(units, list) else []
    external_a = sum(
        float(item.get("external_blocking_a_count") or 0.0)
        for item in units
        if isinstance(item, Mapping)
    )
    external_b = sum(
        float(item.get("external_blocking_b_count") or 0.0)
        for item in units
        if isinstance(item, Mapping)
    )
    initial_entry = sum(
        float(item.get("initial_entry_count") or 0.0)
        for item in units
        if isinstance(item, Mapping)
    )
    intrinsic_steps = [
        float(item.get("intrinsic_target_step") or 0.0)
        for item in units
        if isinstance(item, Mapping)
    ]
    blocker_masks = [
        int(mask)
        for item in units
        if isinstance(item, Mapping)
        for mask in (item.get("blocker_masks") or [])
    ]
    capacity = _capacity_seven_proof(manifest)
    if static_structure_features is not None:
        source_total = max(
            1.0, float(static_structure_features.get("source_total") or 0.0)
        )
        source_a = float(
            static_structure_features.get("source_tile_a_count") or 0.0
        )
        source_b = float(
            static_structure_features.get("source_tile_b_count") or 0.0
        )
        replacement_count = float(
            static_structure_features.get("replacement_count") or 0.0
        )
        type_count = float(static_structure_features.get("type_count") or 0.0)
        replaced_a = float(static_structure_features.get("replaced_a") or 0.0)
        replaced_b = float(static_structure_features.get("replaced_b") or 0.0)
        external_a = float(static_structure_features.get("external_a") or 0.0)
        external_b = float(static_structure_features.get("external_b") or 0.0)
        initial_entry = float(
            static_structure_features.get("initial_entry") or 0.0
        )
        blocker_edges = float(
            static_structure_features.get("blocker_edges") or 0.0
        )
        nonzero_masks = float(
            static_structure_features.get("nonzero_masks") or 0.0
        )
        minimum_failure_click = float(
            static_structure_features.get("min_failure_click") or 10.0
        )
        complete_paths = float(
            static_structure_features.get("complete_paths") or 0.0
        )
    else:
        blocker_edges = float(
            sum(bin(mask).count("1") for mask in blocker_masks)
        )
        nonzero_masks = float(sum(mask != 0 for mask in blocker_masks))
        minimum_failure_click = float(
            capacity.get("minimum_failure_click") or 0.0
        )
        complete_paths = max(
            0.0, float(capacity.get("complete_path_count") or 0.0)
        )
    divisor = max(1.0, replacement_count)
    delta_a = float(candidate.a - original.a)
    delta_b = float(candidate.b - original.b)
    features = {
        "log_target": math.log1p(max(0.0, float(target))) / 8.0,
        "source_a": float(original.a) / 15.0,
        "source_b": float(original.b) / 15.0,
        "candidate_a": float(candidate.a) / 15.0,
        "candidate_b": float(candidate.b) / 15.0,
        "remaining_a_pressure": candidate.a * max(0, source_a - replaced_a) / source_total / 15.0,
        "remaining_b_pressure": candidate.b * max(0, source_b - replaced_b) / source_total / 15.0,
        "delta_a": delta_a,
        "delta_b": delta_b,
        "replacement_fraction": replacement_count / source_total,
        "type_count": type_count / 12.0,
        "replaced_a_fraction": replaced_a / source_total,
        "replaced_b_fraction": replaced_b / source_total,
        "source_a_fraction": source_a / source_total,
        "external_a_per_structure": external_a / divisor,
        "external_b_per_structure": external_b / divisor,
        "initial_entry_per_structure": initial_entry / divisor,
        "intrinsic_step": (
            sum(intrinsic_steps) / len(intrinsic_steps) / 10.0
            if intrinsic_steps
            else 0.0
        ),
        "blocker_edges_per_structure": (
            blocker_edges / divisor
        ),
        "nonzero_masks_per_structure": (
            nonzero_masks / divisor
        ),
        "minimum_failure_click": minimum_failure_click / 10.0,
        "log_complete_paths": math.log1p(complete_paths) / 15.0,
        "level_index": (float(level_index) - 200.0) / 100.0,
        "delta_a_x_duplicate": delta_a * float(duplicate_index) / 10.0,
        "delta_b_x_duplicate": delta_b * float(duplicate_index) / 10.0,
    }
    for name in ("source_tower_fraction", "anchor_tower_fraction", "anchor_structure_fraction",
                 "log_source_tiles", "log_tile_ratio", "source_max_layer", "layer_delta"):
        if static_structure_features is not None and name in static_structure_features:
            features[name] = float(static_structure_features[name])
    for index in range(1, 11):
        features[f"dup_{index}"] = 1.0 if duplicate_index == index else 0.0
    return features


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _finite_provenance(value: Any) -> Any:
    """Keep invalid numeric metadata from leaking NaN/Infinity into JSON."""
    if isinstance(value, Mapping):
        return {key: _finite_provenance(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_provenance(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _consumption_prediction(
    *,
    target: float | None,
    manifest: Mapping[str, Any],
    original: DupParam,
    candidate: DupParam,
    duplicate_index: int,
    level_index: int | None,
    model: Mapping[str, Any] | None,
    static_structure_features: Mapping[str, float] | None = None,
    strategy_group_id: str | None = None,
) -> dict[str, Any]:
    """Apply the unchanged bounded multiplier, labeling cross-band transfer."""
    scope = model.get("scope") if isinstance(model, Mapping) else {}
    scope = dict(scope) if isinstance(scope, Mapping) else {}
    model_id = str(model.get("model_id")) if isinstance(model, Mapping) else None
    target_value = _finite_number(target)
    if target_value is not None and target_value < 0:
        target_value = None
    transfer_reasons: list[str] = []

    def unavailable(failure_reasons: list[str]) -> dict[str, Any]:
        return {
            "value": None, "range": [None, None], "target": target_value,
            "difference": None, "source": "target_unavailable" if target_value is None else "consumption_estimate_unavailable",
            "estimate_mode": "unavailable", "model_id": model_id,
            "training_scope": _finite_provenance(scope), "transfer_reasons": list(transfer_reasons),
            "interval_kind": "unavailable", "level_index": level_index if isinstance(level_index, int) and not isinstance(level_index, bool) and level_index > 0 else None,
            "coverage_validated_for_current_scope": False,
            "structure_correction": {
                "applied": False, "in_scope": False, "training_scope_match": False,
                "model_id": model_id, "reasons": failure_reasons,
            },
            "claim_limit": "缺少有效目标、模型或结构特征，无法估算消耗；不以目标值冒充预估。",
        }

    if target_value is None:
        return unavailable(["source_target_unavailable" if target is None else "invalid_source_target"])
    reasons: list[str] = []
    if not isinstance(model, Mapping):
        reasons.append("model_unavailable")
    try:
        level_min, level_max = int(scope.get("level_index_min", 200)), int(scope.get("level_index_max", 300))
        structure_min, structure_max = int(scope.get("structure_count_min", 9)), int(scope.get("structure_count_max", 18))
        if level_min > level_max or structure_min > structure_max:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        return unavailable(["model_artifact_invalid"])
    feature_order = (
        [str(value) for value in model.get("feature_order", [])]
        if isinstance(model, Mapping)
        else []
    )
    if (
        STATIC_STRUCTURE_FEATURE_NAMES.intersection(feature_order)
        and static_structure_features is None
    ):
        reasons.append("static_feature_context_unavailable")
    supported_strategy_groups = scope.get("strategy_groups_observed")
    if isinstance(supported_strategy_groups, list) and supported_strategy_groups:
        if strategy_group_id is None:
            reasons.append("strategy_group_unavailable")
        elif str(strategy_group_id) not in {
            str(value) for value in supported_strategy_groups
        }:
            reasons.append("outside_strategy_group")
    if isinstance(level_index, bool) or not isinstance(level_index, int) or level_index < 1:
        reasons.append("level_index_unavailable")
    elif not level_min <= level_index <= level_max:
        transfer_reasons.append("outside_level_range")
    elif scope.get("level_bands_observed") and level_index // 100 not in scope["level_bands_observed"]:
        transfer_reasons.append("outside_observed_level_band")
    if scope.get("lucky_values") and str(candidate.luck) not in scope["lucky_values"]:
        reasons.append("unsupported_lucky_value")
    signatures = scope.get("signatures", ["s5"])
    if str(manifest.get("signature") or "") not in {
        str(value) for value in signatures
    }:
        reasons.append("unsupported_signature")
    validation = manifest.get("validation")
    validation = validation if isinstance(validation, Mapping) else {}
    replacements = manifest.get("replacements")
    replacements = replacements if isinstance(replacements, list) else []
    if any(
        not isinstance(item, Mapping)
        or not isinstance(item.get("structure_type"), str)
        or len(item["structure_type"]) != 1
        or not "C" <= item["structure_type"] <= "N"
        for item in replacements
    ):
        reasons.append("invalid_structure_type")
    if static_structure_features is not None and any(
        _finite_number(value) is None for value in static_structure_features.values()
    ):
        reasons.append("invalid_static_feature_context")
        return unavailable(reasons)
    try:
        manifest_structure_count = int(validation.get("replacement_count") or len(replacements))
        static_structure_count = int(static_structure_features.get("replacement_count") or 0) if static_structure_features is not None else None
    except (TypeError, ValueError, OverflowError):
        return unavailable(["invalid_structure_count"])
    if (
        static_structure_count is not None
        and manifest_structure_count != static_structure_count
    ):
        reasons.append("static_manifest_structure_count_mismatch")
    structure_count = (
        static_structure_count
        if static_structure_count is not None
        else manifest_structure_count
    )
    if not structure_min <= structure_count <= structure_max:
        reasons.append("outside_structure_count")
    algorithm_versions = scope.get("algorithm_versions")
    if isinstance(algorithm_versions, list) and algorithm_versions:
        if str(manifest.get("algorithm_version") or "") not in {
            str(value) for value in algorithm_versions
        }:
            reasons.append("unsupported_algorithm_version")
    if reasons:
        return unavailable(reasons)
    assert model is not None and level_index is not None
    try:
        features = _structure_correction_features(
            manifest, target=target_value, original=original, candidate=candidate,
            duplicate_index=duplicate_index, level_index=level_index,
            static_structure_features=static_structure_features,
        )
    except (TypeError, ValueError, OverflowError):
        return unavailable(["invalid_feature_context"])
    means = model.get("means") if isinstance(model.get("means"), Mapping) else {}
    scales = model.get("scales") if isinstance(model.get("scales"), Mapping) else {}
    coefficients = (
        model.get("coefficients")
        if isinstance(model.get("coefficients"), Mapping)
        else {}
    )
    missing = [name for name in feature_order if name not in features]
    if missing:
        return unavailable(["feature_schema_mismatch"])
    try:
        log_ratio = float(model.get("intercept") or 0.0)
        for name in feature_order:
            scale = float(scales.get(name, 1.0))
            feature = _finite_number(features[name])
            center = _finite_number(means.get(name, 0.0))
            coefficient = _finite_number(coefficients[name])
            if feature is None or center is None or coefficient is None or not math.isfinite(scale) or abs(scale) < 1e-12:
                raise ValueError
            normalized = (feature - center) / scale
            log_ratio += normalized * coefficient
        clip = [float(value) for value in model.get("log_ratio_clip", [-0.75, 0.75])]
        if len(clip) != 2 or not all(math.isfinite(value) for value in clip) or clip[0] > clip[1] or not math.isfinite(log_ratio):
            raise ValueError
        bounded_log_ratio = min(clip[1], max(clip[0], log_ratio))
        point = target_value * math.exp(bounded_log_ratio)
        if not math.isfinite(point):
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        return unavailable(["model_artifact_invalid"])
    offsets = model.get("prediction_interval_log_offsets", [None, None])
    if (
        isinstance(offsets, list)
        and len(offsets) == 2
        and all(_finite_number(value) is not None for value in offsets)
    ):
        try:
            interval = [target_value * math.exp(bounded_log_ratio + float(value)) for value in offsets]
            interval = [round(value, 4) for value in interval] if all(math.isfinite(value) for value in interval) else [None, None]
        except OverflowError:
            interval = [None, None]
    else:
        interval = [None, None]
        offsets = [None, None]
    return {
        "value": round(point, 4),
        "range": interval,
        "target": round(target_value, 4),
        "difference": round(point - target_value, 4),
        "source": "source_target_transfer_structure_estimate" if transfer_reasons else "source_target_empirical_structure_correction",
        "estimate_mode": "transfer_estimate" if transfer_reasons else "in_scope_model",
        "transfer_reasons": transfer_reasons, "training_scope": scope,
        "model_id": model_id, "level_index": level_index,
        "interval_kind": "training_residual_reference",
        "training_residual_log_offsets": offsets,
        "coverage_validated_for_current_scope": False,
        "structure_correction": {
            "applied": True,
            "in_scope": not bool(transfer_reasons),
            "training_scope_match": not bool(transfer_reasons),
            "model_id": model_id,
            "raw_log_ratio": round(log_ratio, 8),
            "bounded_log_ratio": round(bounded_log_ratio, 8),
            "bounded": bounded_log_ratio != log_ratio,
            "reasons": [],
            "seed_is_not_a_consumption_feature": True,
        },
        "claim_limit": (
            f"跨关段复用第 {level_min}–{level_max} 关测试倍率；区间仅为历史误差参考，不保证当前关段覆盖率。"
            if transfer_reasons else
            f"按第 {level_min}–{level_max} 关实测数据估算；区间为同源模板分组验证的误差参考，不保证线上消耗。"
        ),
    }


def _light_materialized_board_distance(
    level: Any,
    source_labels: Sequence[str],
    candidate_labels: Sequence[str],
) -> float:
    """Cheap Seed prefilter using positional and per-layer label agreement."""

    if len(source_labels) != len(candidate_labels) or len(source_labels) != len(level.tiles):
        raise ValueError("轻量 Seed 预筛的标签数与模板不一致")
    comparable = [
        index
        for index, (source, candidate) in enumerate(
            zip(source_labels, candidate_labels)
        )
        if not source.startswith("S:") and not candidate.startswith("S:")
    ]
    positional = (
        sum(source_labels[index] != candidate_labels[index] for index in comparable)
        / max(1, len(comparable))
    )
    layer_mismatch = 0.0
    layer_count = 0
    for layer in sorted({int(tile.z) for tile in level.tiles}):
        indices = [index for index, tile in enumerate(level.tiles) if int(tile.z) == layer]
        source_counts: dict[str, int] = {}
        candidate_counts: dict[str, int] = {}
        for index in indices:
            if not source_labels[index].startswith("S:"):
                source_counts[source_labels[index]] = source_counts.get(source_labels[index], 0) + 1
            if not candidate_labels[index].startswith("S:"):
                candidate_counts[candidate_labels[index]] = candidate_counts.get(candidate_labels[index], 0) + 1
        labels = set(source_counts) | set(candidate_counts)
        denominator = max(1, sum(source_counts.values()), sum(candidate_counts.values()))
        layer_mismatch += sum(
            abs(source_counts.get(label, 0) - candidate_counts.get(label, 0))
            for label in labels
        ) / (2.0 * denominator)
        layer_count += 1
    return round(0.6 * positional + 0.4 * layer_mismatch / max(1, layer_count), 8)


def _align_source_labels_if_geometry_compatible(
    source_level: Any,
    candidate_level: Any,
    source_labels: Sequence[str],
) -> tuple[tuple[str, ...] | None, dict[str, Any]]:
    """Align an online source board to the clean candidate index universe."""

    source_indices = [int(tile.index) for tile in source_level.tiles]
    candidate_indices = [int(tile.index) for tile in candidate_level.tiles]
    evidence: dict[str, Any] = {
        "source_template_id": str(source_level.template_id),
        "candidate_template_id": str(candidate_level.template_id),
        "source_sha256": str(source_level.sha256),
        "candidate_sha256": str(candidate_level.sha256),
        "source_contains_structures": any(
            -23 <= int(tile.tile_id) <= -12 for tile in source_level.tiles
        ),
    }
    if len(source_labels) != len(source_level.tiles):
        return None, {**evidence, "status": "incompatible", "reason": "label_count"}
    if len(set(source_indices)) != len(source_indices) or set(source_indices) != set(
        candidate_indices
    ):
        return None, {**evidence, "status": "incompatible", "reason": "tile_indices"}
    source_tiles = {int(tile.index): tile for tile in source_level.tiles}
    candidate_tiles = {int(tile.index): tile for tile in candidate_level.tiles}
    if any(
        (
            int(source_tiles[index].x),
            int(source_tiles[index].y),
            int(source_tiles[index].z),
            source_tiles[index].tower_index,
        )
        != (
            int(candidate_tiles[index].x),
            int(candidate_tiles[index].y),
            int(candidate_tiles[index].z),
            candidate_tiles[index].tower_index,
        )
        for index in source_indices
    ):
        return None, {**evidence, "status": "incompatible", "reason": "tile_geometry"}
    source_graph = build_geometry(source_level, include_tower=True)
    candidate_graph = build_geometry(candidate_level, include_tower=True)
    if source_graph.included_indices != candidate_graph.included_indices:
        return None, {
            **evidence,
            "status": "incompatible",
            "reason": "included_indices",
        }
    for index in source_graph.included_indices:
        if set(source_graph.direct_blockers[index]) != set(
            candidate_graph.direct_blockers[index]
        ):
            return None, {
                **evidence,
                "status": "incompatible",
                "reason": "blocker_graph",
            }
    labels_by_index = {
        int(tile.index): str(label)
        for tile, label in zip(source_level.tiles, source_labels)
    }
    return tuple(labels_by_index[index] for index in candidate_indices), {
        **evidence,
        "status": "compatible",
        "reason": None,
    }


def _prefilter_ab_candidates(
    candidates: Sequence[dict[str, Any]],
    *,
    original: DupParam,
    baseline: DupParam,
    required_anchors: Sequence[tuple[int, int]] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep the best bounded A/B set while retaining both safety anchors."""

    grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for candidate in candidates:
        parameter: DupParam = candidate["parameter"]
        grouped.setdefault((parameter.a, parameter.b), []).append(candidate)
    ranked_keys = sorted(
        grouped,
        key=lambda key: (
            min(_candidate_ranking_cost(item) for item in grouped[key]),
            abs(key[0] - baseline.a) + abs(key[1] - baseline.b),
            key,
        ),
    )
    selected_keys = list(ranked_keys[:AB_PREFILTER_LIMIT_PER_DUPLICATE])
    for anchor in (
        (baseline.a, baseline.b),
        (original.a, original.b),
        *required_anchors,
    ):
        if anchor in grouped and anchor not in selected_keys:
            selected_keys.append(anchor)
    selected_keys.sort(key=lambda key: (original.a - key[0] + original.b - key[1], -key[0], -key[1]))
    selected = [item for key in selected_keys for item in grouped[key]]
    seed_values = sorted(
        {str(item["parameter"].seed) for item in candidates},
        key=_seed_sort_key,
    )
    numeric_seeds = [
        int(seed) for seed in seed_values if re.fullmatch(r"-?[0-9]+", seed)
    ]
    return selected, {
        "feasible_ab_count": len(grouped),
        "selected_ab_count": len(selected_keys),
        "ab_prefilter_limit": AB_PREFILTER_LIMIT_PER_DUPLICATE,
        "enumerated_seed_count": len(seed_values),
        "enumerated_seed_values": seed_values,
        "seed_range": (
            [min(numeric_seeds), max(numeric_seeds)]
            if len(numeric_seeds) == len(seed_values) and numeric_seeds
            else [None, None]
        ),
        "light_prefilter_candidate_count": len(selected),
    }


def _prefilter_seed_candidates(
    candidates: Sequence[dict[str, Any]],
    *,
    original: DupParam,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Let every Seed compete on the light board proxy, then bound full rollout."""

    grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for candidate in candidates:
        parameter: DupParam = candidate["parameter"]
        grouped.setdefault((parameter.a, parameter.b), []).append(candidate)
    selected: list[dict[str, Any]] = []
    anchored_ab_count = 0
    for key in sorted(grouped, reverse=True):
        values = sorted(
            grouped[key],
            key=lambda item: (
                float(item.get("light_seed_distance", float("inf"))),
                _candidate_sort_key(item, reference=original),
            ),
        )
        local = list(values[:SEED_PREFILTER_LIMIT_PER_AB])
        original_seed = next(
            (
                item
                for item in values
                if item["parameter"].seed == original.seed
            ),
            None,
        )
        if original_seed is not None and original_seed not in local:
            local.append(original_seed)
        if original_seed is not None:
            anchored_ab_count += 1
        local.sort(key=lambda item: (item["parameter"].seed != original.seed, _seed_sort_key(item["parameter"].seed)))
        selected.extend(local)
    seed_values = sorted(
        {str(item["parameter"].seed) for item in candidates},
        key=_seed_sort_key,
    )
    numeric_seeds = [
        int(seed) for seed in seed_values if re.fullmatch(r"-?[0-9]+", seed)
    ]
    return selected, {
        "seed_range": (
            [min(numeric_seeds), max(numeric_seeds)]
            if len(numeric_seeds) == len(seed_values) and numeric_seeds
            else [None, None]
        ),
        "seed_prefilter_limit_per_ab": SEED_PREFILTER_LIMIT_PER_AB,
        "original_seed_anchor_policy": "when_present_in_requested_seed_set",
        "original_seed_anchored_ab_count": anchored_ab_count,
        "original_seed_available_for_every_selected_ab": (
            anchored_ab_count == len(grouped)
        ),
        "full_simulation_candidate_count": len(selected),
        "full_simulation_upper_bound": len(grouped)
        * (SEED_PREFILTER_LIMIT_PER_AB + 1),
    }


def _fallback_reason(code: str, description: str) -> str:
    """Return a stable, UI-safe reason without filesystem details."""

    return f"{code}: {description}"


def _fallback_code(reason: str | None) -> str | None:
    if not reason:
        return None
    candidate = reason.partition(":")[0].strip()
    return candidate if re.fullmatch(r"[a-z0-9_]+", candidate) else None


def _materialization_failure_reason(
    error: Exception, *, random_seed_group_id: int
) -> str:
    if random_seed_group_id > 0:
        return _fallback_reason(
            "structured_random_seed_group_unsupported",
            f"randomSeed_groupID={random_seed_group_id} 尚不支持本地重放",
        )
    return _fallback_reason("legacy_materialization_failed", str(error)[:160])


@dataclass(frozen=True)
class HistoryRow:
    template_id: str
    duplicate_index: int
    parameter: DupParam
    strategy_group_id: str
    effective_strategies: tuple[str, ...]
    entrants: float
    consumption: float
    level_index: int | None = None
    level_id: str = ""


def _decode_history(data: bytes) -> str:
    if not data or len(data) > MAX_HISTORY_BYTES:
        raise InputValidationError(
            "历史副本数据文件为空或超过安全大小",
            evidence={"size": len(data), "max_size": MAX_HISTORY_BYTES},
        )
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise InputValidationError("历史副本数据必须是 UTF-8 或 GB18030 CSV")


def _number(value: object, *, field: str, row_number: int) -> float:
    normalized = str(value or "").strip().replace(",", "")
    try:
        parsed = float(normalized)
    except ValueError as exc:
        raise InputValidationError(
            f"历史数据第 {row_number} 行 {field} 不是数字"
        ) from exc
    if not math.isfinite(parsed):
        raise InputValidationError(
            f"历史数据第 {row_number} 行 {field} 不是有限数字"
        )
    return parsed


def _strategy_tokens(value: object) -> tuple[str, ...]:
    raw = str(value or "").strip()
    if not raw:
        return ()
    for separator in ("|", "；", ";", "，", ","):
        raw = raw.replace(separator, " ")
    return tuple(sorted({token for token in raw.split() if token}))


def _strategy_matches(observed_group_id: str, selected_group_id: str) -> bool:
    """Treat an omitted historical strategy group as unscoped history.

    StrategyGroupId describes the structure-reduction configuration, but many
    aggregate exports do not carry that column.  Such rows remain usable for
    template/duplicate consumption calibration; the selected UI strategy is
    an explicit simulation assumption rather than an observed CSV dimension.
    """

    return not observed_group_id or observed_group_id == selected_group_id


def _parse_consumption_history_with_coverage(
    data: bytes,
) -> tuple[list[HistoryRow], list[str], dict[str, Any]]:
    """Parse aggregate CSV and return usable rows plus explicit coverage."""

    text = _decode_history(data)
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = tuple(str(value or "").strip() for value in (reader.fieldnames or ()))
    duplicates = sorted(
        {name for name in fieldnames if fieldnames.count(name) > 1}
    )
    if duplicates:
        raise InputValidationError(
            "历史副本数据包含重复表头",
            evidence={"duplicate_columns": duplicates},
        )
    missing = [name for name in REQUIRED_HISTORY_COLUMNS if name not in fieldnames]
    if missing:
        raise InputValidationError(
            "历史副本数据缺少必需字段",
            evidence={"missing_columns": missing},
        )
    rows: list[HistoryRow] = []
    warnings: list[str] = []
    missing_optional = [
        name for name in OPTIONAL_HISTORY_COLUMNS if name not in fieldnames
    ]
    if "strategy_effective_list" in missing_optional:
        warnings.append(
            "历史数据未提供 strategy_effective_list；不展示历史生效策略明细。"
        )
    skipped = 0
    for offset, raw in enumerate(reader, start=2):
        if offset > MAX_HISTORY_ROWS + 1:
            raise InputValidationError(
                "历史副本数据行数超过安全上限",
                evidence={"max_rows": MAX_HISTORY_ROWS},
            )
        try:
            duplicate_index = int(str(raw["level_dup_para_id"]).strip())
            if not 1 <= duplicate_index <= 10:
                raise ValueError
            parameter = parse_dup_param(str(raw["level_dup"]).strip())
            entrants = _number(raw["进入人数"], field="进入人数", row_number=offset)
            consumption = _number(
                raw["人均消耗"], field="人均消耗", row_number=offset
            )
            if entrants <= 0 or consumption < 0:
                raise ValueError
        except (InputValidationError, ValueError, TypeError, KeyError):
            skipped += 1
            continue
        raw_level_index = str(raw.get("level_index") or "").strip()
        try:
            level_index = int(raw_level_index) if raw_level_index else None
        except ValueError:
            level_index = None
        rows.append(
            HistoryRow(
                template_id=str(raw["level_template_id"]).strip(),
                duplicate_index=duplicate_index,
                parameter=parameter,
                strategy_group_id=str(raw.get("strategy_group_id") or "").strip(),
                effective_strategies=_strategy_tokens(
                    raw.get("strategy_effective_list")
                ),
                entrants=entrants,
                consumption=consumption,
                level_index=level_index,
                level_id=str(raw.get("level_id") or "").strip(),
            )
        )
    if not rows:
        raise InputValidationError("历史副本数据没有可用行")
    if skipped:
        warnings.append(f"已跳过 {skipped} 行副本编号、参数或指标无效的数据。")
    return rows, warnings, {
        "sha256": hashlib.sha256(data).hexdigest(),
        "parsed_row_count": len(rows),
        "ignored_row_count": skipped,
        "covered_duplicate_indices": sorted(
            {row.duplicate_index for row in rows}
        ),
        "covered_duplicate_count": len(
            {row.duplicate_index for row in rows}
        ),
        "sample_entrants": int(sum(row.entrants for row in rows)),
        "missing_optional_columns": missing_optional,
        "unscoped_strategy_row_count": sum(
            1 for row in rows if not row.strategy_group_id
        ),
    }


def parse_consumption_history(data: bytes) -> tuple[list[HistoryRow], list[str]]:
    """Parse the aggregate CSV and retain all scientifically usable rows."""

    rows, warnings, _coverage = _parse_consumption_history_with_coverage(data)
    return rows, warnings


def _weighted_average(rows: Sequence[HistoryRow]) -> float | None:
    total_weight = sum(row.entrants for row in rows)
    if total_weight <= 0:
        return None
    return sum(row.consumption * row.entrants for row in rows) / total_weight


def _weighted_quantile(
    observations: Sequence[tuple[float, float]], probability: float
) -> float | None:
    usable = sorted(
        (float(value), max(0.0, float(weight)))
        for value, weight in observations
        if math.isfinite(float(value)) and float(weight) > 0
    )
    total = sum(weight for _, weight in usable)
    if not usable or total <= 0:
        return None
    threshold = max(0.0, min(1.0, probability)) * total
    cumulative = 0.0
    for value, weight in usable:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return usable[-1][0]


def _association_estimate(
    history: Sequence[HistoryRow],
    *,
    duplicate_index: int,
    strategy_group_id: str,
    candidate: DupParam,
    neighbour_limit: int = 12,
) -> dict[str, Any]:
    pool = tuple(
        row
        for row in history
        if row.duplicate_index == duplicate_index
        and _strategy_matches(row.strategy_group_id, strategy_group_id)
    )
    return _association_estimate_from_pool(
        pool,
        candidate=candidate,
        neighbour_limit=neighbour_limit,
    )


def _association_estimate_from_pool(
    pool: Sequence[HistoryRow],
    *,
    candidate: DupParam,
    neighbour_limit: int = 12,
) -> dict[str, Any]:
    """Estimate against rows already scoped to one duplicate and strategy.

    Keeping the hot calculation separate lets a calibration request index the
    history once instead of filtering the complete CSV for every candidate.
    """

    ranked = sorted(
        pool,
        key=lambda row: (
            abs(row.parameter.a - candidate.a)
            + abs(row.parameter.b - candidate.b)
            + (0.35 if row.parameter.seed != candidate.seed else 0.0),
            -row.entrants,
            row.template_id,
        ),
    )[:neighbour_limit]
    weighted: list[tuple[HistoryRow, float]] = []
    for row in ranked:
        distance = (
            abs(row.parameter.a - candidate.a)
            + abs(row.parameter.b - candidate.b)
            + (0.35 if row.parameter.seed != candidate.seed else 0.0)
        )
        # Square-root entrants prevents one large aggregate from erasing the
        # cross-template spread while still rewarding stronger observations.
        weight = math.sqrt(row.entrants) / (1.0 + distance)
        weighted.append((row, weight))
    total_weight = sum(weight for _, weight in weighted)
    if total_weight <= 0:
        return {
            "status": "unavailable",
            "point": None,
            "range": [None, None],
            "neighbour_count": 0,
            "entrant_count": 0,
            "method": "same-duplicate/same-strategy-group weighted neighbours",
        }
    point = sum(row.consumption * weight for row, weight in weighted) / total_weight
    low = _weighted_quantile(
        [(row.consumption, weight) for row, weight in weighted], 0.10
    )
    high = _weighted_quantile(
        [(row.consumption, weight) for row, weight in weighted], 0.90
    )
    return {
        "status": "association_only",
        "point": round(point, 4),
        "range": [
            None if low is None else round(low, 4),
            None if high is None else round(high, 4),
        ],
        "neighbour_count": len(weighted),
        "entrant_count": int(sum(row.entrants for row, _ in weighted)),
        "method": "same-duplicate/same-strategy-group weighted neighbours",
        "claim_limit": (
            "historical association only; template selection and Seed effects "
            "are confounded, so this is not a causal consumption forecast"
        ),
    }


def _index_association_history(
    history: Sequence[HistoryRow],
) -> dict[tuple[int, str], tuple[HistoryRow, ...]]:
    buckets: dict[tuple[int, str], list[HistoryRow]] = {}
    for row in history:
        buckets.setdefault(
            (row.duplicate_index, row.strategy_group_id), []
        ).append(row)
    return {key: tuple(rows) for key, rows in buckets.items()}


def _cached_association_estimate(
    index: Mapping[tuple[int, str], Sequence[HistoryRow]],
    cache: dict[tuple[int, str, int, int, str], dict[str, Any]],
    *,
    duplicate_index: int,
    strategy_group_id: str,
    candidate: DupParam,
) -> dict[str, Any]:
    """Return one estimate per unique duplicate/strategy/A/B/Seed query."""

    key = (
        duplicate_index,
        strategy_group_id,
        candidate.a,
        candidate.b,
        candidate.seed,
    )
    if key not in cache:
        scoped_rows = tuple(index.get((duplicate_index, strategy_group_id), ()))
        unscoped_rows = tuple(index.get((duplicate_index, ""), ()))
        cache[key] = _association_estimate_from_pool(
            scoped_rows + (() if not strategy_group_id else unscoped_rows),
            candidate=candidate,
        )
    # Callers attach the record to candidate-local payloads. Return a shallow
    # copy so a later presentation change cannot mutate the request cache.
    return dict(cache[key])


def _target_record(
    history: Sequence[HistoryRow],
    *,
    source_template_id: str,
    duplicate_index: int,
    strategy_group_id: str,
    override: float | None,
) -> dict[str, Any]:
    matching = [
        row
        for row in history
        if row.template_id == source_template_id
        and row.duplicate_index == duplicate_index
        and _strategy_matches(row.strategy_group_id, strategy_group_id)
    ]
    observed = _weighted_average(matching)
    target = override if override is not None else observed
    effective = sorted(
        {
            strategy
            for row in matching
            for strategy in row.effective_strategies
        }
    )
    return {
        "value": None if target is None else round(float(target), 4),
        "source": "user_override" if override is not None else "source_template_online",
        "source_observed_value": (
            None if observed is None else round(float(observed), 4)
        ),
        "sample_rows": len(matching),
        "sample_entrants": int(sum(row.entrants for row in matching)),
        "effective_strategies": effective,
    }


def _non_increasing_online_targets(records):
    """Flatten online consumption rebounds without changing uploaded data."""
    result = []
    previous = None
    for index, record in enumerate(records, start=1):
        current = dict(record)
        value = current.get("value")
        if (current.get("source") == "source_template_online"
                and value is not None and previous is not None
                and previous.get("value") is not None
                and value > previous["value"]):
            current.update(
                value=previous["value"], source="previous_duplicate_online_target",
                adjustment={
                    "reason": "online_consumption_increase",
                    "original_value": value, "effective_value": previous["value"],
                    "previous_duplicate_index": index - 1,
                    "anchor_duplicate_index": (previous.get("adjustment") or {}).get("anchor_duplicate_index", index - 1),
                },
            )
        result.append(current)
        previous = current if current.get("source") != "user_override" else None
    return result


def _observed_seeds(
    history: Sequence[HistoryRow],
    *,
    duplicate_index: int,
    strategy_group_id: str,
    original_seed: str,
) -> list[str]:
    entrants_by_seed: dict[str, float] = {}
    for row in history:
        if (
            row.duplicate_index == duplicate_index
            and _strategy_matches(row.strategy_group_id, strategy_group_id)
        ):
            # Experimental Seed search is deliberately restricted to observed,
            # production-shaped integer values.  The original Seed is still
            # retained below even when an old configuration used a non-integer
            # token; preserving the baseline is safer than rewriting it.
            if not re.fullmatch(r"[0-9]+", row.parameter.seed):
                continue
            try:
                parsed_seed = int(row.parameter.seed)
            except ValueError:
                continue
            if not 0 <= parsed_seed <= 2_147_483_647:
                continue
            entrants_by_seed[row.parameter.seed] = (
                entrants_by_seed.get(row.parameter.seed, 0.0) + row.entrants
            )
    ordered = sorted(
        entrants_by_seed,
        key=lambda seed: (-entrants_by_seed[seed], seed),
    )
    if original_seed not in ordered:
        ordered.insert(0, original_seed)
    return ordered


def _seed_search_values(
    history: Sequence[HistoryRow],
    *,
    duplicate_index: int,
    strategy_group_id: str,
    original: DupParam,
    mode: str,
) -> list[str]:
    """Resolve the requested Seed policy without assigning it causal meaning."""

    normalized = str(mode or "").strip().lower()
    if normalized == "preserve":
        return [original.seed]
    if normalized == "observed":
        return _observed_seeds(
            history,
            duplicate_index=duplicate_index,
            strategy_group_id=strategy_group_id,
            original_seed=original.seed,
        )
    if normalized == "joint":
        return [str(value) for value in range(32)]
    raise InputValidationError("Seed 方案只能是保留原 Seed、历史已观测或联合搜索")


def _seed_search_mode(
    *,
    duplicate_index: int,
    search_observed_seeds: bool,
    optimize_all_duplicate_seeds: bool,
    simulate_first_duplicate_seeds: bool,
    focused_recalculation: bool,
) -> str:
    if optimize_all_duplicate_seeds:
        return "joint"
    if focused_recalculation and duplicate_index == 1 and simulate_first_duplicate_seeds:
        return "joint"
    if search_observed_seeds:
        return "observed"
    return "preserve"


def _seed_sort_key(seed: str) -> tuple[int, int | str]:
    raw = str(seed)
    if re.fullmatch(r"-?[0-9]+", raw):
        return (0, int(raw))
    return (1, raw)


def _select_one_baseline_per_output(
    rows: Sequence[Mapping[str, Any]],
    history: Sequence[HistoryRow],
) -> tuple[list[Mapping[str, Any]], list[str]]:
    """Resolve duplicate config rows without emitting duplicate output templates."""

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("output_template_id") or ""), []).append(row)
    selected: list[Mapping[str, Any]] = []
    warnings: list[str] = []
    for output_id, choices in grouped.items():
        if len(choices) == 1:
            selected.append(
                {
                    **choices[0],
                    "baseline_resolution": {
                        "status": "unique",
                        "reason": "single_config_row",
                        "candidate_count": 1,
                        "selected_level_ids": list(
                            choices[0].get("level_ids", [])
                        ),
                        "demote_recommendation": False,
                    },
                }
            )
            continue
        source_id = str(
            choices[0].get("source_template_id") or ""
        )
        config_source_id = str(
            choices[0].get("config_source_template_id")
            or source_id
            or ""
        )
        source_aliases = {value for value in (source_id, config_source_id) if value}
        source_history = [
            row for row in history if row.template_id in source_aliases
        ]
        history_level_ids = {row.level_id for row in source_history if row.level_id}

        def evidence_key(row: Mapping[str, Any]) -> tuple[int, int, int, int]:
            level_ids = {str(value) for value in row.get("level_ids", [])}
            overlap = level_ids & history_level_ids
            scoped_history = (
                [item for item in source_history if item.level_id in level_ids]
                if overlap
                else source_history
            )
            history_parameters = {
                duplicate_index: {
                    item.parameter.render()
                    for item in scoped_history
                    if item.duplicate_index == duplicate_index
                }
                for duplicate_index in range(1, 11)
            }
            before = list(row.get("before_dup", []))
            complete_vector_match = len(before) == 10 and all(
                str(value) in history_parameters[index]
                for index, value in enumerate(before, start=1)
            )
            configured_group = str(row.get("strategy_group_id") or "")
            observed_groups = {
                item.strategy_group_id
                for item in scoped_history
                if item.strategy_group_id
            }
            strategy_match = bool(
                configured_group and configured_group in observed_groups
            )
            return (
                int(bool(overlap)),
                len(overlap),
                int(complete_vector_match),
                int(strategy_match),
            )

        best_evidence = max(evidence_key(row) for row in choices)
        candidates = [row for row in choices if evidence_key(row) == best_evidence]
        if best_evidence[0]:
            reason = "history_level_id"
        elif best_evidence[2]:
            reason = "history_complete_dup_match"
        elif best_evidence[3]:
            reason = "history_strategy_group_match"
        else:
            reason = "safe_deterministic_fallback"

        def selection_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
            level_ids = {str(value) for value in row.get("level_ids", [])}
            intersection_count = len(level_ids & history_level_ids)
            try:
                random_seed_group_id = int(row.get("random_seed_group_id") or 0)
            except (TypeError, ValueError):
                random_seed_group_id = 2_147_483_647
            return (
                -intersection_count,
                random_seed_group_id != 0,
                random_seed_group_id,
                sorted(level_ids),
                tuple(str(value) for value in row.get("before_dup", [])),
            )

        chosen = min(candidates, key=selection_key)
        ambiguous = len(candidates) > 1
        resolution = {
            "status": "ambiguous_deterministic" if ambiguous else "resolved",
            "reason": reason,
            "candidate_count": len(choices),
            "evidence_tied_candidate_count": len(candidates),
            "selected_level_ids": list(chosen.get("level_ids", [])),
            "history_level_ids": sorted(history_level_ids),
            "demote_recommendation": ambiguous,
        }
        selected.append({**chosen, "baseline_resolution": resolution})
        warnings.append(
            f"{output_id} 对应 {len(choices)} 套源配置；已按 {reason} "
            f"唯一选择 randomSeed_groupID="
            f"{chosen.get('random_seed_group_id', 0)}，未重复输出同一新模板。"
            + (
                " 仍存在多个同级候选，本行降级为方向性结果。"
                if ambiguous
                else ""
            )
        )
    return selected, warnings


def _resolve_calibration_template_id(
    manifest: Mapping[str, Any],
    available_template_ids: Sequence[str] | set[str],
    *,
    prefix: str = "",
    suffix: str = "",
) -> tuple[str | None, dict[str, Any]]:
    """Match complete history IDs while retaining the manifest's source identity."""

    source_id = str(manifest.get("source_template_id") or "").strip()
    output_id = str(manifest.get("output_template_id") or "").strip()
    output_name = output_id.removesuffix("_m.bytes")
    available = {str(value).strip() for value in available_template_ids if value}
    source_alias = source_id.removesuffix("clean")
    evidence: dict[str, Any] = {
        "source_template_id": source_id,
        "output_template_id": output_id,
        "prefix": prefix,
        "suffix": suffix,
        "status": "missing",
        "method": "unmatched",
        "matched_template_id": None,
    }
    signature = str(manifest.get("signature") or "").strip()
    generated_suffix = suffix or (
        signature if re.fullmatch(r"s[4-9]|tower", signature)
        else ""
    )

    def strip_generated_suffix(name: str) -> str | None:
        if generated_suffix:
            match = re.fullmatch(
                rf"(.+){re.escape(generated_suffix)}(?:v[0-9]+)?", name
            )
            return match.group(1) if match is not None else None
        match = re.fullmatch(r"(.+)(?:s[4-9]|tower)(?:v[0-9]+)?", name)
        return match.group(1) if match is not None else name

    normalized: list[str] = []
    if prefix or suffix:
        if prefix and not output_name.startswith(prefix):
            raise InputValidationError(f"新模板 {output_id} 不匹配指定前缀 {prefix}")
        stripped = output_name[len(prefix):] if prefix else output_name
        custom_id = strip_generated_suffix(stripped)
        if custom_id is None:
            raise InputValidationError(f"新模板 {output_id} 不匹配指定后缀 {suffix}")
        # An explicit prefix is an exact instruction, not a hint. Never
        # silently fall back to a different source/suffix after removing it.
        if prefix:
            if custom_id not in available:
                raise InputValidationError(
                    f"按填写的前后缀得到 {custom_id}，但 CSV 中没有该模板；请检查前后缀或上传对应数据"
                )
            return custom_id, {
                **evidence, "status": "matched", "method": "explicit_name_exact",
                "matched_template_id": custom_id,
            }
        normalized.append(custom_id)
    if not prefix and not suffix:
        for candidate in dict.fromkeys((source_id, source_alias)):
            if candidate in available:
                return candidate, {
                    **evidence, "status": "matched", "method": "source_exact",
                    "matched_template_id": candidate,
                }
    auto_id = strip_generated_suffix(output_name.removeprefix("aipuzzle"))
    if auto_id:
        normalized.append(auto_id)
    candidates = list(dict.fromkeys((source_id, source_alias, *normalized)))
    for candidate in normalized:
        if candidate in available:
            return candidate, {
                **evidence, "status": "matched", "method": "normalized_output_exact",
                "matched_template_id": candidate,
            }
    matches = sorted({
        template_id
        for template_id in available
        if any(candidate.endswith(template_id) for candidate in candidates)
    })
    if len(matches) > 1:
        raise InputValidationError(
            f"新模板 {output_id} 无法唯一匹配历史原模板：{'、'.join(matches)}；请填写更精确的前缀和后缀",
            evidence={"output_template_id": output_id, "matches": matches},
        )
    if matches:
        return matches[0], {
            **evidence, "status": "matched", "method": "unique_complete_suffix",
            "matched_template_id": matches[0],
        }
    return None, evidence


def match_calibration_templates(
    manifests: Sequence[Mapping[str, Any]],
    *,
    history_data: bytes,
    template_name_prefix: str = "",
    template_name_suffix: str = "",
) -> dict[str, Any]:
    """Preview the same CSV matches used by calibration, without simulation."""
    for value in (template_name_prefix, template_name_suffix):
        if not isinstance(value, str) or len(value) > 128 or any(c in value for c in ("/", "\\", "\n", "\r", "\0")):
            raise InputValidationError("模板名前后缀不能超过 128 个字，也不能包含路径或换行")
    history, _warnings = parse_consumption_history(history_data)
    available = {row.template_id for row in history}
    items = []
    for manifest in manifests:
        item = {"output_template_id": manifest.get("output_template_id"), "source_template_id": manifest.get("source_template_id")}
        try:
            matched, evidence = _resolve_calibration_template_id(
                manifest, available, prefix=template_name_prefix.strip(), suffix=template_name_suffix.strip(),
            )
            item.update(evidence)
            if matched is None:
                item["message"] = "CSV 中没有对应模板，请检查文件或指定前后缀"
        except InputValidationError as exc:
            matches = dict(exc.detail.evidence or {}).get("matches", [])
            item.update(status="ambiguous" if matches else "mismatch", matched_template_id=None, matches=matches, message=str(exc))
        items.append(item)
    return {"items": items, "matched_count": sum(item["status"] == "matched" for item in items), "total": len(items)}


def _resolve_target_history(
    history: Sequence[HistoryRow],
    *,
    source_template_id: str,
    config_source_template_id: str,
    selected_level_ids: set[str],
    canonical_template_id: str | None = None,
) -> tuple[list[HistoryRow], str, str]:
    """Resolve the source alias actually present in the uploaded history CSV."""

    candidate_ids = list(
        dict.fromkeys(
            value
            for value in (
                source_template_id, canonical_template_id, config_source_template_id
            )
            if value
        )
    )
    rows_by_id = {
        template_id: [
            row for row in history if row.template_id == template_id
        ]
        for template_id in candidate_ids
    }
    available_ids = [
        template_id for template_id in candidate_ids if rows_by_id[template_id]
    ]
    if not available_ids:
        fallback_id = config_source_template_id or source_template_id
        return [], fallback_id, "source_alias_unavailable"

    def evidence_key(template_id: str) -> tuple[int, int, int, int, int, int]:
        rows = rows_by_id[template_id]
        overlapping_levels = {
            row.level_id
            for row in rows
            if row.level_id and row.level_id in selected_level_ids
        }
        covered_duplicates = {row.duplicate_index for row in rows}
        return (
            len(overlapping_levels),
            int(len(covered_duplicates) == 10),
            len(covered_duplicates),
            int(sum(row.entrants for row in rows)),
            len(rows),
            int(template_id == source_template_id),
        )

    # A resolved CSV identity (including an explicit prefix) must not be
    # overridden by a processed source that happens to have more samples.
    resolved_id = canonical_template_id if canonical_template_id in available_ids else max(available_ids, key=evidence_key)
    resolved_rows = rows_by_id[resolved_id]
    level_scoped = [
        row
        for row in resolved_rows
        if row.level_id and row.level_id in selected_level_ids
    ]
    if level_scoped:
        return level_scoped, resolved_id, "selected_baseline_level_ids"
    return resolved_rows, resolved_id, "resolved_source_alias_fallback"


def _candidate_space(
    original: DupParam,
    baseline: DupParam,
    *,
    seeds: Sequence[str],
    additional_ab_pairs: Sequence[tuple[int, int]] = (),
) -> list[DupParam]:
    # The per-duplicate fidelity result is the mechanical lower bound and the
    # original parameter is the upper bound.  Do not let a global/fallback
    # anchor silently create values outside that reviewable interval.
    if baseline.a > original.a or baseline.b > original.b:
        return []
    a_values = range(original.a, baseline.a - 1, -1)
    b_values = range(original.b, baseline.b - 1, -1)
    ordered_seeds = sorted(dict.fromkeys(seeds), key=lambda seed: (seed != original.seed, _seed_sort_key(seed)))
    candidates = [
        DupParam(a=a, seed=seed, luck=original.luck, b=b)
        for a in a_values
        for b in b_values
        for seed in ordered_seeds
    ]
    existing = {(item.a, item.b, item.seed) for item in candidates}
    for a, b in additional_ab_pairs:
        if (
            a < baseline.a
            or b < baseline.b
            or a > original.a
            or b > original.b
        ):
            continue
        for seed in seeds:
            key = (a, b, seed)
            if key in existing:
                continue
            candidates.append(
                DupParam(a=a, seed=seed, luck=original.luck, b=b)
            )
            existing.add(key)
    return candidates


def _automatic_structure_search_bounds(originals, baselines, replaced_a_types, replaced_b_types):
    """Compute caps from distinct replacement types before any simulation.

    The mechanical fidelity value stays a displayed reference. It is not a
    lower bound for consumption fitting: 12 minus five types caps A at seven,
    while A=5 must remain searchable. Later duplicates retain their source
    upper bounds and the joint path still enforces the existing progression.
    """
    upper = list(originals)
    lower = list(baselines)
    if not (replaced_a_types or replaced_b_types):
        return upper, lower, None
    first = originals[0]
    a_cap, b_cap = first.a - replaced_a_types, first.b - replaced_b_types
    if a_cap < 0 or b_cap < 0:
        raise InputValidationError("首副本原参数不足以扣减实际替换的结构块种类数")
    upper[0] = DupParam(a_cap, first.seed, first.luck, b_cap)
    for index, (ceiling, base) in enumerate(zip(upper, baselines)):
        lower[index] = DupParam(
            min(base.a, ceiling.a, 4) if replaced_a_types else base.a,
            base.seed, base.luck,
            min(base.b, ceiling.b, 3) if replaced_b_types else base.b,
        )
    return upper, lower, {
        "mode": "automatic_structure_types",
        "source_a": first.a, "source_b": first.b,
        "replaced_a_type_count": replaced_a_types,
        "replaced_b_type_count": replaced_b_types,
        "a_max": a_cap, "b_max": b_cap,
    }


def _monotonic_baseline_anchor_pairs(
    originals: Sequence[DupParam],
    baselines: Sequence[DupParam],
) -> list[tuple[int, int]] | None:
    """Return the smallest no-rebound path that stays inside every A/B bound."""

    if len(originals) != len(baselines) or not originals:
        return None
    suffix_a = 0
    suffix_b = 0
    reversed_pairs: list[tuple[int, int]] = []
    for original, baseline in zip(reversed(originals), reversed(baselines)):
        if baseline.a > original.a or baseline.b > original.b:
            return None
        suffix_a = max(suffix_a, baseline.a)
        suffix_b = max(suffix_b, baseline.b)
        if suffix_a > original.a or suffix_b > original.b:
            return None
        reversed_pairs.append((suffix_a, suffix_b))
    return list(reversed(reversed_pairs))


def _association_loss(estimate: object, target: object) -> float:
    if not isinstance(estimate, (int, float)) or not isinstance(
        target, (int, float)
    ):
        return 3.0
    return abs(float(estimate) - float(target)) / max(1.0, abs(float(target)))


def _direction(estimate: object, target: object) -> str:
    if not isinstance(estimate, (int, float)) or not isinstance(
        target, (int, float)
    ):
        return "unavailable"
    tolerance = max(1.0, abs(float(target)) * 0.05)
    difference = float(estimate) - float(target)
    if abs(difference) <= tolerance:
        return "near_target_association"
    return "above_target_association" if difference > 0 else "below_target_association"


def _replacement_objects(manifest: Mapping[str, Any]) -> list[SimpleNamespace]:
    raw = manifest.get("replacements")
    if not isinstance(raw, list):
        raise InputValidationError("manifest 缺少 replacements")
    replacements: list[SimpleNamespace] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise InputValidationError(
                "manifest replacement 格式错误", evidence={"index": index}
            )
        try:
            replacements.append(
                SimpleNamespace(
                    tile_index=int(item["tile_index"]),
                    source_tile_id=int(item["source_tile_id"]),
                    structure_type=str(item["structure_type"]).upper(),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InputValidationError(
                "manifest replacement 缺少可校验字段",
                evidence={"index": index},
            ) from exc
    return replacements


def _strategy_group_config(
    profile: Profile, strategy_group_id: str, config_set_id: str
) -> dict[str, Any]:
    """Read raw InstanceId1–10 values without inventing runtime semantics."""

    if not profile.ulinkconfig_root:
        return {
            "status": "missing",
            "rules": [None] * 10,
            "source_paths": [],
            "warnings": ["ulinkconfig 未配置，无法读取策略组原始规则。"],
        }
    try:
        root = Path(profile.ulinkconfig_root).resolve(strict=True)
    except OSError:
        return {
            "status": "missing",
            "rules": [None] * 10,
            "source_paths": [],
            "warnings": ["ulinkconfig 目录不可读，无法读取策略组原始规则。"],
        }
    try:
        known_sets = {item["id"] for item in discover_config_sets(root)}
    except InputValidationError as exc:
        return {
            "status": "missing",
            "rules": [None] * 10,
            "source_paths": [],
            "warnings": [str(exc)],
        }
    if config_set_id not in known_sets:
        return {
            "status": "missing",
            "rules": [None] * 10,
            "source_paths": [],
            "warnings": ["选中配置集不在当前 ulinkconfig 可读白名单中。"],
        }
    selected_directory = (
        root
        if config_set_id == "."
        else (root / config_set_id).resolve(strict=True)
    )
    if not is_within(selected_directory, root):
        return {
            "status": "missing",
            "rules": [None] * 10,
            "source_paths": [],
            "warnings": ["选中配置集路径越过 ulinkconfig 根目录。"],
        }
    selected_path = selected_directory / "config_level_strategy_group.csv"
    fallback_warning: list[str] = []
    if selected_path.is_file() and not selected_path.is_symlink():
        candidate_paths = [selected_path]
    else:
        candidate_paths = sorted(root.rglob("config_level_strategy_group.csv"))[:64]
        fallback_warning.append(
            f"选中配置集 {config_set_id} 同目录缺少 config_level_strategy_group.csv；"
            "已回退为全局只读查找，需核对是否与目标版本一致。"
        )

    matches: list[tuple[str, tuple[str, ...]]] = []
    for path in candidate_paths:
        try:
            resolved = path.resolve(strict=True)
            if path.is_symlink() or not is_within(resolved, root):
                continue
            with resolved.open("r", encoding="utf-8-sig", newline="") as stream:
                reader = csv.reader(stream)
                header = [value.strip() for value in next(reader)]
                required = ["StrategyGroupId", *(f"InstanceId{i}" for i in range(1, 11))]
                if not set(required).issubset(header):
                    continue
                columns = {name: header.index(name) for name in required}
                # Skip type and description rows.
                next(reader, None)
                next(reader, None)
                for row in reader:
                    padded = row + [""] * max(0, len(header) - len(row))
                    if padded[columns["StrategyGroupId"]].strip() != strategy_group_id:
                        continue
                    rules = tuple(
                        padded[columns[f"InstanceId{i}"]].strip()
                        for i in range(1, 11)
                    )
                    matches.append((resolved.relative_to(root).as_posix(), rules))
        except (OSError, UnicodeError, csv.Error, StopIteration):
            continue
    if not matches:
        return {
            "status": "missing",
            "rules": [None] * 10,
            "source_paths": [],
            "warnings": fallback_warning + [
                f"未在当前 ulinkconfig 找到 StrategyGroupId={strategy_group_id} 的完整原始规则。"
            ],
        }
    unique = {rules for _path, rules in matches}
    warnings: list[str] = list(fallback_warning)
    status = "available"
    if len(unique) > 1:
        status = "conflicting"
        warnings.append(
            f"StrategyGroupId={strategy_group_id} 在多个配置表中的 InstanceId1–10 不一致，"
            "已保留冲突警告，不猜测当前线上值。"
        )
    rules = matches[0][1] if len(unique) == 1 else (None,) * 10
    return {
        "status": status,
        "rules": list(rules),
        "source_paths": [path for path, _rules in matches],
        "warnings": warnings,
        "claim_limit": (
            "InstanceId1–10 are replayed with Wiki PRD v85 semantics; random "
            "branches use a stable local sample, not the production client's "
            "exact runtime random stream"
        ),
    }


def _legacy_seed_config_context(
    profile: Profile, config_set_id: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Load current local seed inputs from the selected config-set directory."""

    if not profile.ulinkconfig_root:
        return None, _fallback_reason(
            "seed_config_root_missing", "ulinkconfig 未配置"
        )
    try:
        root = Path(profile.ulinkconfig_root).resolve(strict=True)
        known_sets = {item["id"] for item in discover_config_sets(root)}
        if config_set_id not in known_sets:
            return None, _fallback_reason(
                "seed_config_set_unavailable", "选中配置集不在当前可读范围"
            )
        directory = (
            root
            if config_set_id == "."
            else (root / config_set_id).resolve(strict=True)
        )
        if not is_within(directory, root):
            return None, _fallback_reason(
                "seed_config_set_outside_root", "选中配置集路径越界"
            )
        level_tile_path = directory / "config_client_level_tile.csv"
        client_tile_path = directory / "config_client_tile.csv"
        for path in (level_tile_path, client_tile_path):
            if not path.is_file() or path.is_symlink():
                return None, _fallback_reason(
                    "seed_config_table_missing",
                    f"{path.name} 不在选中配置集同目录",
                )
        level_tile_text = level_tile_path.read_text(encoding="utf-8-sig")
        client_tile_text = client_tile_path.read_text(encoding="utf-8-sig")
        return {
            "level_tile_rows": parse_level_tile_config(level_tile_text),
            "tile_catalog": parse_client_tile_config(client_tile_text),
            "source_paths": [
                str(level_tile_path.relative_to(root)),
                str(client_tile_path.relative_to(root)),
            ],
        }, None
    except OSError:
        # OSError.__str__ commonly embeds an absolute user path.  This reason
        # is rendered in the UI and exported to CSV, so expose only a stable
        # code and short actionable description.
        return None, _fallback_reason(
            "seed_config_read_failed", "Seed 配置目录或文件不可读"
        )
    except UnicodeError:
        return None, _fallback_reason(
            "seed_config_decode_failed", "Seed 配置文件编码无法解析"
        )
    except ValueError:
        return None, _fallback_reason(
            "seed_config_invalid", "Seed 配置内容无法解析"
        )
    except InputValidationError:
        return None, _fallback_reason(
            "seed_config_invalid", "Seed 配置集无法通过校验"
        )


def _csharp_seed(value: str) -> int:
    raw = str(value).strip()
    if not re.fullmatch(r"-?[0-9]+", raw):
        raise ValueError("RandomSeed 不是 C# int")
    parsed = int(raw)
    if not -(1 << 31) <= parsed <= (1 << 31) - 1:
        raise ValueError("RandomSeed 超出 C# int 范围")
    return parsed


def _whole_board_labels(
    level: Any,
    replacements: Sequence[SimpleNamespace],
    parameter: DupParam,
    *,
    level_index: int,
    config_context: Mapping[str, Any],
    candidate: bool,
    random_seed_group_id: int = 0,
    apply_structure_strategy: bool = False,
    strategy_group_id: str = "",
    strategy_rule_raw: str | None = None,
    duplicate_index: int = 1,
) -> tuple[tuple[str, ...], dict[str, Any]]:
    """Apply optional structure reduction, then materialize A/B labels."""

    replacement_by_index = (
        {int(item.tile_index): str(item.structure_type).upper() for item in replacements}
        if candidate
        else {}
    )
    symbolic: list[str] = []
    for tile in level.tiles:
        replacement = replacement_by_index.get(tile.index)
        if replacement is not None:
            symbolic.append(f"S:{replacement}")
        elif tile.tile_id == -10:
            symbolic.append("A")
        elif tile.tile_id == -11:
            symbolic.append("B")
        elif -23 <= tile.tile_id <= -12:
            symbolic.append(f"S:{chr(ord('C') + (-12 - tile.tile_id))}")
        else:
            symbolic.append(f"T:{tile.tile_id}")
    strategy_replay: dict[str, Any]
    if apply_structure_strategy:
        symbolic_tuple, strategy_replay = replay_structure_strategy(
            symbolic,
            [tile.z for tile in level.tiles],
            strategy_rule_raw,
            strategy_group_id=strategy_group_id,
            duplicate_index=duplicate_index,
            duplicate_seed=parameter.seed,
            board_role="candidate" if candidate else "source",
        )
        symbolic = list(symbolic_tuple)
    else:
        strategy_replay = {
            "status": "disabled",
            "raw_rule": str(strategy_rule_raw or ""),
            "effective_strategies": [],
            "failed_strategies": [],
            "random_semantics": "not_applied_by_user_choice",
        }
    a_positions = tuple(
        index for index, value in enumerate(symbolic) if value == "A"
    )
    b_positions = tuple(
        index for index, value in enumerate(symbolic) if value == "B"
    )
    materialized = materialize_legacy_ab_from_config(
        level_index=level_index,
        level_tile_rows=config_context["level_tile_rows"],
        tile_catalog=config_context["tile_catalog"],
        a_positions=a_positions,
        b_positions=b_positions,
        dup_a_types=parameter.a,
        dup_b_types=parameter.b,
        dup_seed=_csharp_seed(parameter.seed),
        # StrategyGroupId=1003 controls structure reduction, not this field.
        random_seed_group_id=random_seed_group_id,
        iap_protect_cut_a_count=0,
    )
    assignments = dict(materialized.a_assignments)
    assignments.update(materialized.b_assignments)
    labels: list[str] = []
    for tile, value in zip(level.tiles, symbolic):
        if tile.index in assignments:
            labels.append(f"T:{assignments[tile.index]}")
            continue
        labels.append(value if value.startswith(("S:", "T:")) else f"T:{value}")
    return tuple(labels), {
        "mode": LEGACY_EVIDENCE_LEVEL,
        "template_id": str(level.template_id),
        "template_sha256": str(level.sha256),
        "structure_label_count": sum(
            1 for value in labels if value.startswith("S:")
        ),
        "level_index": level_index,
        "algorithm_version": materialized.version,
        "pattern_pool": list(materialized.pattern_pool),
        "configuration_fingerprint": materialized.configuration_fingerprint,
        "level_tile_config_fingerprint": materialized.level_tile_config_fingerprint,
        "client_tile_config_fingerprint": materialized.client_tile_config_fingerprint,
        "pool_selection_fingerprint": materialized.pool_selection_fingerprint,
        "selected_level_tile_id": materialized.selected_level_tile_id,
        "selected_level_start": materialized.selected_level_start,
        "iap_protect_cut_a_count": materialized.iap_protect_cut_a_count,
        "random_seed_group_id": materialized.random_seed_group_id,
        "structure_strategy": strategy_replay,
        "assumptions": list(materialized.assumptions),
        "fallback_reason": None,
    }


def _synthetic_seed_metadata(
    *,
    level_index: int | None,
    reason: str,
    random_seed_group_id: int,
) -> dict[str, Any]:
    return {
        "mode": "synthetic",
        "status": (
            "unsupported"
            if _fallback_code(reason)
            == "structured_random_seed_group_unsupported"
            else "fallback"
        ),
        "level_index": level_index,
        "pattern_pool": None,
        "configuration_fingerprint": None,
        "level_tile_config_fingerprint": None,
        "client_tile_config_fingerprint": None,
        "pool_selection_fingerprint": None,
        "assumptions": ["stable_hash_A_B_assignment"],
        "random_seed_group_id": random_seed_group_id,
        "fallback_code": _fallback_code(reason),
        "fallback_reason": reason,
    }


def _evidence_level(target: Mapping[str, Any], association: Mapping[str, Any]) -> str:
    if target.get("value") is None or association.get("status") != "association_only":
        return "synthetic_only"
    neighbours = int(association.get("neighbour_count") or 0)
    entrants = int(association.get("entrant_count") or 0)
    if neighbours >= 8 and entrants >= 10_000:
        return "association_moderate"
    return "association_weak"


def _finite_distance(value: object) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.0 <= value <= 1.0
    ):
        return None
    return float(value)


def _candidate_ranking_cost(
    candidate: Mapping[str, Any], *, reference: DupParam | None = None
) -> tuple[int, float, int, float, int, float, int, int]:
    """Rank target error before simulation, gradient, Seed and mechanical ties.

    Missing primary evidence cannot beat a valid simulation by paying the
    old fixed failure penalty. Computable consumption error is independent of
    the simulation/gradient axis, so either cannot offset a worse estimate.
    Missing evidence is counted, never treated as a perfect zero distance.
    Secondary distance reuses the existing duplicate objective weight.
    No new numeric weights are applied.
    """
    parameter: DupParam = candidate["parameter"]
    reference = reference or candidate.get("ranking_reference") or candidate.get("original") or parameter
    baseline: DupParam = candidate.get("baseline") or reference
    difficulty = candidate.get("difficulty") or {}
    primary = _finite_distance(difficulty.get("distance"))
    secondary = (
        _finite_distance(difficulty.get("stable_tiebreak"))
        if primary is not None else None
    )
    weight = float(candidate.get("objective_weight") or 1.0)
    prediction = candidate.get("consumption_prediction") or {}
    point = _finite_number(prediction.get("value"))
    target = _finite_number(prediction.get("target"))
    estimated = bool(prediction.get("structure_correction", {}).get("applied")) and point is not None and target is not None and point >= 0 and target >= 0
    # Legacy helper callers only supply node_score. Runtime candidates carry
    # the independent simulation score so target error cannot be offset by it.
    main = candidate.get("simulation_score", candidate.get("node_score"))
    valid_main = (
        not isinstance(main, bool)
        and isinstance(main, (int, float)) and math.isfinite(main)
    )
    return (
        int(not estimated),
        weight * _association_loss(point, target) if estimated else 0.0,
        int(primary is None or not valid_main),
        float(main) if valid_main else float("inf"),
        int(secondary is None),
        secondary * weight
        if secondary is not None else 0.0,
        int(parameter.seed != reference.seed),
        abs(parameter.a - baseline.a) + abs(parameter.b - baseline.b),
    )


def _candidate_sort_key(
    candidate: Mapping[str, Any], *, reference: DupParam | None = None
) -> tuple[Any, ...]:
    parameter: DupParam = candidate["parameter"]
    return (
        *_candidate_ranking_cost(candidate, reference=reference),
        _seed_sort_key(parameter.seed),
        parameter.a,
        parameter.b,
        parameter.render(),
    )


def _transition_penalty(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> float:
    previous_original: DupParam = previous.get("ranking_reference") or previous["original"]
    current_original: DupParam = current.get("ranking_reference") or current["original"]
    previous_candidate: DupParam = previous["parameter"]
    current_candidate: DupParam = current["parameter"]
    source_a_step = previous_original.a - current_original.a
    source_b_step = previous_original.b - current_original.b
    candidate_a_step = previous_candidate.a - current_candidate.a
    candidate_b_step = previous_candidate.b - current_candidate.b
    if candidate_a_step < 0:
        return float("inf")
    if candidate_b_step < 0:
        return float("inf")
    penalty = 0.08 * (
        abs(source_a_step - candidate_a_step)
        + abs(source_b_step - candidate_b_step)
    )
    return penalty


def _select_monotonic_candidate_path(
    candidates_by_duplicate: Sequence[Sequence[Mapping[str, Any]]],
) -> list[int] | None:
    """Minimize the complete lexicographic cost across the entire path."""
    if not candidates_by_duplicate or any(not pool for pool in candidates_by_duplicate):
        return None
    costs: list[list[tuple[Any, ...]]] = []
    parents: list[list[int | None]] = []
    path_keys: list[list[tuple[Any, ...]]] = []
    for duplicate_index, choices in enumerate(candidates_by_duplicate):
        current_costs: list[tuple[Any, ...]] = []
        current_parents: list[int | None] = []
        current_keys: list[tuple[Any, ...]] = []
        for choice in choices:
            raw_cost = _candidate_ranking_cost(choice)
            # Decimal addition avoids arbitrary epsilon weights or float-sum
            # noise deciding a path before the explicit secondary criterion.
            cost = tuple(Decimal(str(value)) if index in (1, 3, 5) else value
                         for index, value in enumerate(raw_cost))
            parameter: DupParam = choice["parameter"]
            key = (_seed_sort_key(parameter.seed), parameter.a, parameter.b)
            if duplicate_index == 0:
                current_costs.append(cost)
                current_parents.append(None)
                current_keys.append((key,))
                continue
            transitions: dict[int, tuple[Any, ...]] = {}
            for parent_index, previous in enumerate(candidates_by_duplicate[duplicate_index - 1]):
                previous_cost = costs[-1][parent_index]
                penalty_value = _transition_penalty(previous, choice)
                # Reachability is a hard constraint, outside the ranking
                # tuple: a low missing-evidence count cannot legalize rebound.
                if not previous_cost[1].is_finite() or not previous_cost[3].is_finite() or not math.isfinite(penalty_value):
                    continue
                penalty = Decimal(str(penalty_value))
                transitions[parent_index] = tuple(
                    previous_cost[index] + cost[index] + (penalty if index == 3 else 0)
                    for index in range(len(cost))
                )
            if not transitions:
                current_costs.append((0, Decimal("Infinity"), 0, Decimal("Infinity"), 0, Decimal(0), 0, 0))
                current_parents.append(None)
                current_keys.append((key,))
                continue
            parent_index = min(
                transitions,
                key=lambda index: (transitions[index], path_keys[-1][index]),
            )
            current_costs.append(transitions[parent_index])
            current_parents.append(parent_index)
            current_keys.append((*path_keys[-1][parent_index], key))
        costs.append(current_costs)
        parents.append(current_parents)
        path_keys.append(current_keys)
    finite_indices = [index for index, cost in enumerate(costs[-1]) if cost[1].is_finite() and cost[3].is_finite()]
    if not finite_indices:
        return None
    selected_indices = [0] * len(candidates_by_duplicate)
    selected_indices[-1] = min(
        finite_indices, key=lambda index: (costs[-1][index], path_keys[-1][index])
    )
    for duplicate_index in range(len(candidates_by_duplicate) - 1, 0, -1):
        selected_indices[duplicate_index - 1] = int(
            parents[duplicate_index][selected_indices[duplicate_index]] or 0
        )
    return selected_indices


def _gradient_policy_evidence(
    used_infeasible_bounds_fallback: bool,
) -> dict[str, Any]:
    """Describe the path that was actually returned, not the intended policy."""

    if used_infeasible_bounds_fallback:
        return {
            "policy": (
                "fidelity-boundary fallback; monotonic bounds infeasible; "
                "weighted source-target best fit unavailable"
            ),
            "no_new_parameter_rebound": False,
            "infeasible_bounds_fallback": True,
        }
    return {
        "policy": (
            "dynamic-programming; hard no-new-rebound; "
            "weighted source-target best fit"
        ),
        "no_new_parameter_rebound": True,
        "infeasible_bounds_fallback": False,
    }


def _serialize_candidate(
    candidate: Mapping[str, Any],
    target: Mapping[str, Any],
    *,
    is_selected_best_fit: bool = True,
) -> dict[str, Any]:
    parameter: DupParam = candidate["parameter"]
    original: DupParam = candidate["original"]
    reference: DupParam = candidate.get("ranking_reference") or original
    association = dict(candidate["association"])
    prediction = dict(candidate.get("consumption_prediction") or {})
    difficulty = candidate.get("difficulty")
    point = prediction.get("value")
    target_value = target.get("value")
    expected_difference = (
        float(point) - float(target_value)
        if isinstance(point, (int, float)) and isinstance(target_value, (int, float))
        else None
    )
    relative_gap = (
        abs(expected_difference) / max(1.0, abs(float(target_value)))
        if expected_difference is not None and isinstance(target_value, (int, float))
        else None
    )
    correction = prediction.get("structure_correction")
    correction = correction if isinstance(correction, Mapping) else {}
    correction_applied = bool(correction.get("applied"))
    simulation_evidence_ready = bool(
        candidate.get("simulation_evidence_ready", True)
    )
    simulation_evidence_reasons = list(
        candidate.get("simulation_evidence_reasons") or []
    )
    if relative_gap is None:
        recommendation_status = "best_fit_insufficient_evidence"
        recommendation_label = "缺少历史目标" if target_value is None else "无法估算"
        is_recommended = False
    elif not correction_applied:
        recommendation_status = "best_fit_directional"
        recommendation_label = "仅供参考"
        is_recommended = False
    elif not simulation_evidence_ready:
        recommendation_status = "best_fit_directional"
        recommendation_label = "模拟未完成 · 需确认"
        is_recommended = False
    elif prediction.get("estimate_mode") == "transfer_estimate":
        recommendation_status = "transfer_estimate_close" if relative_gap <= 0.25 else "transfer_estimate_far"
        recommendation_label = "预估接近" if relative_gap <= 0.25 else "偏差较大"
        is_recommended = relative_gap <= 0.25
    elif relative_gap <= 0.25:
        recommendation_status = "recommended"
        recommendation_label = "推荐"
        is_recommended = True
    elif relative_gap <= 0.50:
        recommendation_status = "best_fit_review"
        recommendation_label = "需确认"
        is_recommended = False
    else:
        recommendation_status = "best_fit_outside_tolerance"
        recommendation_label = "偏差较大"
        is_recommended = False
    return {
        "dup_para": parameter.render(),
        "seed_changed": parameter.seed != reference.seed,
        "seed_experimental": parameter.seed != reference.seed,
        "luck_preserved": parameter.luck == reference.luck,
        "ranking_reference_dup": reference.render(),
        "consumption_anchor": dict(candidate.get("consumption_anchor") or {}),
        "historical_association": {
            **association,
            "direction_vs_target": _direction(
                association.get("point"), target.get("value")
            ),
            "ranking_role": "context_only",
        },
        "expected_consumption": {
            **prediction,
            "target": target_value,
            "difference": expected_difference,
        },
        "recommendation": {
            "status": recommendation_status,
            "label": recommendation_label,
            "is_recommended": is_recommended,
            "is_best_fit": is_selected_best_fit,
            "relative_gap": None if relative_gap is None else round(relative_gap, 6),
            "threshold": 0.25,
            "export_blocked": False,
            "simulation_evidence_ready": simulation_evidence_ready,
            "simulation_evidence_reasons": simulation_evidence_reasons,
            "estimate_mode": prediction.get("estimate_mode", "in_scope_model" if correction_applied else "unavailable"),
            "empirically_validated_for_scope": False,
        },
        "synthetic_difficulty": difficulty,
        "seed_simulation": dict(candidate.get("seed_simulation") or {}),
        "evidence_level": _evidence_level(target, association),
        "objective_weight": float(candidate.get("objective_weight") or 1.0),
        "ranking_score": round(float(candidate["node_score"]), 6),
        "ranking_cost": list(_candidate_ranking_cost(candidate)),
    }


def _recommendation_issue_counts(
    duplicate_rows: Sequence[Mapping[str, Any]],
) -> tuple[int, int, int]:
    """Count all non-recommended rows, material gaps, and evidence demotions."""

    recommendations = [
        row.get("recommendation", {})
        if isinstance(row.get("recommendation"), Mapping)
        else {}
        for row in duplicate_rows
    ]
    unqualified_count = sum(
        1 for item in recommendations if not item.get("is_recommended", False)
    )
    outside_tolerance_count = sum(
        1
        for item in recommendations
        if isinstance(item.get("relative_gap"), (int, float))
        and float(item["relative_gap"]) > 0.25
    )
    evidence_demoted_count = sum(
        1
        for item in recommendations
        if item.get("status")
        in {"best_fit_directional", "best_fit_insufficient_evidence"}
    )
    return unqualified_count, outside_tolerance_count, evidence_demoted_count


def calibrate_consumption_parameters(
    profile: Profile,
    manifests: Sequence[Mapping[str, Any]],
    *,
    config_set_id: str = "",
    level_start: int | None = None,
    level_end: int | None = None,
    history_data: bytes,
    history_filename: str,
    target_overrides: Mapping[str, Any] | None = None,
    search_observed_seeds: bool = False,
    strategy_group_id: str = DEFAULT_STRATEGY_GROUP_ID,
    strategy_group_mode: str = "source",
    config_level_data: bytes | None = None,
    config_level_filename: str | None = None,
    focus_output_template_id: str | None = None,
    first_duplicate_max: int | None = None,
    auto_first_duplicate_max: bool = False,
    simulate_first_duplicate_seeds: bool = False,
    consider_structure_strategy: bool = True,
    optimize_all_duplicate_seeds: bool = False,
    template_name_prefix: str = "",
    template_name_suffix: str = "",
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Return reviewable DupPara1–10 paths without mutating configuration."""

    if not str(history_filename).lower().endswith(".csv"):
        raise InputValidationError("历史副本数据必须是 CSV 文件")
    if not isinstance(search_observed_seeds, bool):
        raise InputValidationError("search_observed_seeds 必须是布尔值")
    if not isinstance(simulate_first_duplicate_seeds, bool):
        raise InputValidationError("simulate_first_duplicate_seeds 必须是布尔值")
    if not isinstance(consider_structure_strategy, bool):
        raise InputValidationError("consider_structure_strategy 必须是布尔值")
    if not isinstance(optimize_all_duplicate_seeds, bool):
        raise InputValidationError("optimize_all_duplicate_seeds 必须是布尔值")
    if not isinstance(auto_first_duplicate_max, bool):
        raise InputValidationError("auto_first_duplicate_max 必须是布尔值")
    for value in (template_name_prefix, template_name_suffix):
        if not isinstance(value, str) or len(value) > 128 or any(
            character in value for character in ("/", "\\", "\n", "\r", "\0")
        ):
            raise InputValidationError("模板名称前缀和后缀必须是合法的短文本")
    template_name_prefix = template_name_prefix.strip()
    template_name_suffix = template_name_suffix.strip()
    focus_output_id = str(focus_output_template_id or "").strip()
    if focus_output_id:
        if isinstance(first_duplicate_max, bool):
            raise InputValidationError("首副本最高参数必须是整数")
        try:
            first_duplicate_max = int(first_duplicate_max)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise InputValidationError("首副本最高参数必须是整数") from exc
        if first_duplicate_max < 0:
            raise InputValidationError("首副本最高参数不能为负数")
    elif first_duplicate_max is not None:
        raise InputValidationError("指定首副本最高参数时必须同时指定新模板 ID")
    progress: dict[str, Any] = {
        "phase": "preparing",
        "message": "正在读取历史数据与保真参数",
        "template_total": len(manifests),
        "template_completed": 0,
        "current_template": None,
        "duplicate_index": None,
        "duplicate_total": 10,
        "duplicate_completed": 0,
        "duplicate_total_all": len(manifests) * 10,
        "candidate_completed": 0,
        "candidate_total": 0,
    }

    def report_progress(**changes: Any) -> None:
        progress.update(changes)
        if progress_callback is not None:
            progress_callback(dict(progress))

    report_progress()
    history, parse_warnings, history_coverage = (
        _parse_consumption_history_with_coverage(history_data)
    )
    strategy_mode = str(strategy_group_mode or "source").strip().lower()
    if strategy_mode not in {"source", "custom"}:
        raise InputValidationError("结构降难策略来源只能是和原关卡一致或自定义")
    requested_group_id = str(strategy_group_id or "").strip()
    if strategy_mode == "custom" and not requested_group_id:
        raise InputValidationError("选择自定义策略时必须填写结构降难策略 ID")
    if requested_group_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", requested_group_id):
        raise InputValidationError("结构降难策略 ID 格式不合法")

    if not config_set_id:
        available_sets = discover_config_sets(profile.ulinkconfig_root or "")
        preferred = next((item for item in available_sets if item["id"] == "csv"), None)
        config_set_id = str((preferred or available_sets[0])["id"])
    observed_positions = [
        row.level_index for row in history if row.level_index is not None and row.level_index >= 1
    ]
    if level_start is None:
        level_start = min(observed_positions) if observed_positions else 1
    if level_end is None:
        level_end = max(observed_positions) if observed_positions else 2_147_483_647
    if level_start < 1 or level_end < level_start:
        raise InputValidationError("历史数据中的关卡范围不合法")

    association_history_index = _index_association_history(history)
    association_cache: dict[
        tuple[int, str, int, int, str], dict[str, Any]
    ] = {}
    history_coverage = {
        **history_coverage,
        "strategy_group_source": strategy_mode,
        "history_level_range": {"start": level_start, "end": level_end},
    }
    configured_strategy_cache: dict[str, dict[str, Any]] = {}
    seed_config_context, seed_config_fallback = _legacy_seed_config_context(
        profile, config_set_id
    )
    structure_correction_model = _load_structure_correction_model()

    parsed_overrides: dict[int, float] = {}
    if target_overrides is not None:
        if not isinstance(target_overrides, Mapping):
            raise InputValidationError("target_overrides 必须是副本编号到目标消耗的对象")
        for raw_index, raw_target in target_overrides.items():
            try:
                index = int(str(raw_index))
                target = float(raw_target)
            except (TypeError, ValueError) as exc:
                raise InputValidationError("目标消耗覆盖格式错误") from exc
            if not 1 <= index <= 10 or not math.isfinite(target) or target < 0:
                raise InputValidationError(
                    "目标消耗覆盖必须为副本1–10的非负数"
                )
            parsed_overrides[index] = target

    history_template_ids = {row.template_id for row in history}
    canonical_matches: dict[str, tuple[str | None, dict[str, Any]]] = {}
    calibration_manifests: list[dict[str, Any]] = []
    manifest_by_output = {
        str(item.get("output_template_id", "")): item for item in manifests
    }
    for manifest in manifests:
        output_id = str(manifest.get("output_template_id", ""))
        canonical_id, match_evidence = _resolve_calibration_template_id(
            manifest, history_template_ids,
            prefix=template_name_prefix, suffix=template_name_suffix,
        )
        canonical_matches[output_id] = canonical_id, match_evidence
        calibrated = dict(manifest)
        actual_source_id = str(manifest.get("source_template_id", ""))
        if canonical_id and canonical_id not in {
            actual_source_id, actual_source_id.removesuffix("clean")
        }:
            counts = source_tile_counts(profile, manifest)
            if counts is not None:
                calibrated["source_tile_counts"] = {"A": counts[0], "B": counts[1]}
            calibrated["source_template_id"] = canonical_id
        calibration_manifests.append(calibrated)
    baseline = build_parameter_recommendations(
        profile,
        calibration_manifests,
        config_set_id=config_set_id,
        level_start=level_start,
        level_end=level_end,
        strategy="fidelity",
        config_level_data=config_level_data,
        config_level_filename=config_level_filename,
        # Target-consumption calibration is an independent workflow.  The
        # historical CSV owns the simulation/model level index; a source row
        # remains usable even when the current config list has since moved it
        # outside that historical band.
        filter_by_level_range=False,
    )
    ready_outputs = {
        str(row.get("output_template_id", ""))
        for row in baseline.get("rows", []) if row.get("status") == "ready"
    }
    unmatched_manifests = [
        manifest for manifest in manifests
        if str(manifest.get("output_template_id", "")) not in ready_outputs
        and canonical_matches[str(manifest.get("output_template_id", ""))][0]
        not in {None, manifest.get("source_template_id")}
    ]
    if unmatched_manifests:
        fallback_baseline = build_parameter_recommendations(
            profile, unmatched_manifests, config_set_id=config_set_id,
            level_start=level_start, level_end=level_end, strategy="fidelity",
            config_level_data=config_level_data,
            config_level_filename=config_level_filename, filter_by_level_range=False,
        )
        fallback_outputs = {
            str(row.get("output_template_id", ""))
            for row in fallback_baseline.get("rows", []) if row.get("status") == "ready"
        }
        baseline["rows"] = [
            row for row in baseline.get("rows", [])
            if str(row.get("output_template_id", "")) not in fallback_outputs
        ] + [
            row for row in fallback_baseline.get("rows", [])
            if str(row.get("output_template_id", "")) in fallback_outputs
        ]
        baseline.setdefault("warnings", []).extend(fallback_baseline.get("warnings", []))
    processed_comparisons: dict[str, dict[str, Any]] = {}
    alias_manifests = [item for item in manifests if canonical_matches[str(item.get("output_template_id", ""))][0] not in {None, item.get("source_template_id")}]
    if alias_manifests:
        processed_result = build_parameter_recommendations(
            profile, alias_manifests, config_set_id=config_set_id,
            level_start=level_start, level_end=level_end, strategy="fidelity",
            config_level_data=config_level_data, config_level_filename=config_level_filename,
            filter_by_level_range=False,
        )
        for manifest in alias_manifests:
            output_id = str(manifest.get("output_template_id", ""))
            source_id = str(manifest.get("source_template_id", ""))
            # Only an exact config row for the processed source establishes
            # this extra bound; a clean-name alias is not a second config.
            choices = [row for row in processed_result.get("rows", []) if row.get("output_template_id") == output_id and row.get("status") == "ready" and row.get("config_source_template_id") == source_id]
            distinct = {tuple(row.get("before_dup", [])) for row in choices}
            if len(distinct) > 1:
                raise InputValidationError(f"{source_id} 在 config_level 中有多套不同副本参数，无法确定搜索上限；请先确认该模板应使用哪套配置")
            if choices:
                processed_comparisons[output_id] = choices[0]
    for row in baseline.get("rows", []):
        output_id = str(row.get("output_template_id", ""))
        manifest = manifest_by_output.get(output_id)
        if manifest is not None:
            actual_source = str(manifest.get("source_template_id", ""))
            if row.get("source_template_id") != actual_source:
                row["source_template_id"] = actual_source
                row["config_source_match"] = "canonical_history_alias"
    result_rows: list[dict[str, Any]] = []
    warnings = list(parse_warnings) + list(baseline.get("warnings", []))
    baseline_rows, baseline_selection_warnings = _select_one_baseline_per_output(
        baseline.get("rows", []), history
    )
    if focus_output_id:
        baseline_rows = [
            row for row in baseline_rows
            if str(row.get("output_template_id", "")) == focus_output_id
        ]
    report_progress(
        template_total=len(baseline_rows),
        duplicate_total_all=len(baseline_rows) * 10,
    )
    warnings.extend(baseline_selection_warnings)
    if strategy_mode == "custom" and requested_group_id != DEFAULT_STRATEGY_GROUP_ID:
        warnings.append(f"已按自定义结构降难策略 {requested_group_id} 计算。")
    if optimize_all_duplicate_seeds:
        warnings.append(
            "已启用十档联合搜索：每个副本先枚举可行 A/B 与 Seed 0–31，"
            "再对短名单执行整盘仿真，最后联合检查十档难度梯度。"
        )
    elif focus_output_id and simulate_first_duplicate_seeds:
        warnings.append(
            "本关重算的 DupPara1 联合搜索 Seed 0–31；其余副本按所选 Seed "
            "方案执行。Seed 只通过牌面仿真参与排序。"
        )
    elif search_observed_seeds:
        warnings.append(
            "已按所选方案枚举历史 CSV 中已观测 Seed 并保留原 Seed；"
            "Seed 只通过牌面仿真参与排序。"
        )
    else:
        warnings.append(
            "已按所选方案保留原 Seed；Seed 不作为降低人均消耗的因果变量。"
        )
    if structure_correction_model is None:
        warnings.append(
            "结构消耗校正模型不可用；本次以原模板各副本目标为保守基线，"
            "仍输出完整最接近参数。"
        )
    if seed_config_fallback:
        warnings.append(
            f"旧版 Seed 牌面仿真已回退为 synthetic：{seed_config_fallback}。"
        )
    if not consider_structure_strategy:
        warnings.append("本次按用户选择忽略结构降难策略，只评估原始结构盘面。")

    matching_focus_found = False
    for template_index, baseline_row in enumerate(baseline_rows):
        report_progress(
            phase="template_preparation",
            message="正在准备模板与结构证据",
            template_completed=template_index,
            current_template=str(baseline_row.get("output_template_id", "")),
            duplicate_index=None,
            duplicate_completed=template_index * 10,
            candidate_completed=0,
            candidate_total=0,
        )
        if focus_output_id:
            matching_focus_found = True
        if baseline_row.get("status") != "ready":
            warnings.append(
                f"{baseline_row.get('source_template_id', '未知模板')} 的保真基线不可用，已跳过消耗校准。"
            )
            continue
        source_id = str(baseline_row["source_template_id"])
        output_id = str(baseline_row["output_template_id"])
        config_source_id = str(
            baseline_row.get("config_source_template_id") or source_id
        ).strip()
        config_source_match = str(
            baseline_row.get("config_source_match") or "exact"
        ).strip()
        baseline_resolution = dict(
            baseline_row.get("baseline_resolution") or {}
        )
        selected_baseline_level_ids = {
            str(value)
            for value in baseline_row.get("level_ids", [])
            if str(value)
        }
        (
            target_history,
            target_history_template_id,
            target_history_scope,
        ) = _resolve_target_history(
            history,
            source_template_id=source_id,
            config_source_template_id=config_source_id,
            selected_level_ids=selected_baseline_level_ids,
            canonical_template_id=canonical_matches[output_id][0],
        )
        source_group_id = str(baseline_row.get("strategy_group_id") or "").strip()
        group_id = (
            requested_group_id
            if strategy_mode == "custom"
            else source_group_id or DEFAULT_STRATEGY_GROUP_ID
        )
        configured_strategy = configured_strategy_cache.get(group_id)
        if configured_strategy is None:
            configured_strategy = _strategy_group_config(
                profile, group_id, config_set_id
            )
            configured_strategy_cache[group_id] = configured_strategy
            if consider_structure_strategy:
                if configured_strategy.get("status") == "available":
                    warnings.append(
                        f"已在每档 A/B 与 Seed 仿真前回放结构降难策略组 {group_id}；"
                        "随机策略使用可复现的本地采样，不代表客户端某一局的唯一随机结果。"
                    )
                else:
                    warnings.append(
                        f"策略组 {group_id} 配置不可唯一读取；对应牌面会明确回退。"
                    )
        raw_random_seed_group_id = baseline_row.get("random_seed_group_id", 0)
        try:
            if isinstance(raw_random_seed_group_id, bool):
                raise ValueError
            random_seed_group_id = int(str(raw_random_seed_group_id).strip())
            if random_seed_group_id < 0:
                raise ValueError
            random_seed_group_reason = None
        except (TypeError, ValueError):
            random_seed_group_id = 0
            random_seed_group_reason = _fallback_reason(
                "random_seed_group_invalid",
                "config_level.randomSeed_groupID 不是非负整数",
            )
        manifest = manifest_by_output.get(output_id)
        if manifest is None:
            warnings.append(f"{output_id} 缺少本次选择的 manifest，已跳过。")
            continue
        loaded = load_template_id(profile, source_id)
        expected_sha = str(manifest.get("source_sha256", ""))
        if expected_sha and loaded.level.sha256 != expected_sha:
            warnings.append(f"{source_id} 当前源模板与导出快照不一致，已跳过。")
            continue
        online_source_level = loaded.level
        online_source_load_reason: str | None = None
        if config_source_id != source_id and config_source_match != "canonical_history_alias":
            try:
                online_source_level = load_template_id(
                    profile, config_source_id
                ).level
            except PuzzleToolError:
                online_source_level = None
                online_source_load_reason = _fallback_reason(
                    "online_source_template_unavailable",
                    "config_level 对应的原线上模板不可读",
                )
                warnings.append(
                    f"{source_id} 无法读取原线上模板 {config_source_id}；"
                    "整盘对比明确回退，不冒充原盘仿真。"
                )
        replacements = _replacement_objects(manifest)
        static_structure_features: dict[str, float] | None = None
        static_structure_feature_reason: str | None = None
        try:
            static_structure_features = _empirical_static_structure_features(
                loaded.level, replacements
            )
            # Consumption is anchored to the CSV template, which can differ from
            # both the processed source and the parameter-reference config row.
            anchor_id = target_history_template_id or source_id
            anchor_level = loaded.level if anchor_id == source_id else load_template_id(profile, anchor_id).level
            static_structure_features.update(_paired_board_features(loaded.level, anchor_level))
        except (PuzzleToolError, TypeError, ValueError, KeyError):
            static_structure_features = None
            static_structure_feature_reason = "static_feature_context_unavailable"
            validation = manifest.get("validation")
            validation = validation if isinstance(validation, Mapping) else {}
            replacement_count = int(
                validation.get("replacement_count") or len(replacements)
            )
            if (
                structure_correction_model is not None
                and str(manifest.get("signature") or "") == "s5"
                and 9 <= replacement_count <= 18
            ):
                warnings.append(
                    f"{source_id} 无法复算结构校正模型所需的静态特征；"
                    "本次无法估算消耗；保留线上目标和模拟结果供人工复核。"
                )
        config_positions: list[int] = []
        for value in baseline_row.get("online_positions", []):
            try:
                config_positions.append(int(value))
            except (TypeError, ValueError):
                continue
        config_positions = sorted(set(config_positions))
        history_positions = sorted(
            {
                int(row.level_index)
                for row in target_history
                if row.level_index is not None
            }
        )
        usable_positions = history_positions or config_positions
        selected_level_index = usable_positions[0] if usable_positions else None
        simulation_level_index_source = (
            "history_resolved_source_alias"
            if history_positions
            else "current_config_level_list_fallback"
            if config_positions
            else "unavailable"
        )
        position_warning = None
        if len(usable_positions) > 1:
            position_warning = (
                f"{source_id} 在{'历史 CSV' if history_positions else '当前配置'}"
                f"对应多个关卡位置 {usable_positions}；"
                f"Seed 牌面仿真使用首个位置 {selected_level_index}。"
            )
            warnings.append(position_warning)
        originals = [parse_dup_param(value) for value in baseline_row["before_dup"]]
        baselines = [
            parse_dup_param(value) for value in baseline_row["recommended_dup"]
        ]
        if len(originals) != 10 or len(baselines) != 10:
            warnings.append(f"{source_id} 缺少完整 DupPara1–10，已跳过。")
            continue
        processed_row = processed_comparisons.get(output_id)
        source_parameter_comparison: dict[str, Any] | None = None
        processed_cap: int | None = None
        ranking_references = list(originals)
        if processed_row and config_source_id != source_id:
            processed_originals = [parse_dup_param(value) for value in processed_row["before_dup"]]
            different = [index for index, (online, processed) in enumerate(zip(originals, processed_originals), start=1) if online.render() != processed.render()]
            source_parameter_comparison = {
                "status": "different" if different else "same",
                "processed_template_id": source_id,
                "online_template_id": canonical_matches[output_id][0] or config_source_id,
                "different_duplicates": different,
                "processed_dup": [value.render() for value in processed_originals],
                "online_dup": [value.render() for value in originals],
                "first_duplicate_max": None,
            }
            if different:
                processed_cap = max(processed_originals[0].a, processed_originals[0].b)
                source_parameter_comparison["first_duplicate_max"] = processed_cap
                # The replacement baseline must start from the parameters of
                # the actual processed source, not from the online source.
                baselines = [parse_dup_param(value) for value in processed_row["recommended_dup"]]
                ranking_references = list(processed_originals)
        # The canonical row supplies the historical target and online display.
        # Parameters and no-structure source simulation belong to the actual source.
        search_originals = list(ranking_references)
        if processed_cap is not None:
            first = search_originals[0]
            search_originals[0] = DupParam(min(first.a, processed_cap), first.seed, first.luck, min(first.b, processed_cap))
            if baselines[0].a > search_originals[0].a or baselines[0].b > search_originals[0].b:
                raise InputValidationError(f"{source_id} 的首副本搜索上限 {processed_cap} 低于可用参数下限，请检查两份模板的副本配置")
        search_baselines = list(baselines)
        automatic_bounds = None
        if auto_first_duplicate_max:
            replaced_a_types, replaced_b_types = manifest_replacement_type_counts(manifest)
            search_originals, search_baselines, automatic_bounds = _automatic_structure_search_bounds(
                search_originals, baselines, replaced_a_types, replaced_b_types,
            )
        monotonic_anchor_pairs = _monotonic_baseline_anchor_pairs(
            search_originals, search_baselines
        )
        monotonic_bounds_feasible = monotonic_anchor_pairs is not None
        if not monotonic_bounds_feasible:
            warnings.append(
                f"{source_id} 的逐档机械扣减下限与原参数上限之间"
                "不存在完整无回弹路径；仍输出十档保真边界供人工处理，"
                "不会用全局低值穿透任一档下限。"
            )
        focus_bounds: dict[str, Any] | None = None
        if focus_output_id or processed_cap is not None or automatic_bounds is not None:
            lower_bound = max(search_baselines[0].a, search_baselines[0].b)
            upper_bound = max(search_originals[0].a, search_originals[0].b)
            if focus_output_id and (first_duplicate_max is None or not lower_bound <= first_duplicate_max <= upper_bound):
                raise InputValidationError(
                    f"首副本最高参数必须在 {lower_bound}–{upper_bound} 之间；"
                    "下限按替换后的可用参数计算，上限不得超过源模板配置。"
                )
            focus_bounds = {
                "lower_bound": lower_bound,
                "upper_bound": upper_bound,
                "requested_max": first_duplicate_max if focus_output_id else None,
                "automatic_bounds": automatic_bounds,
            }

        candidates_by_duplicate: list[list[dict[str, Any]]] = []
        search_evidence_by_duplicate: list[dict[str, Any]] = []
        targets: list[dict[str, Any]] = []
        online_targets = _non_increasing_online_targets([
            _target_record(target_history, source_template_id=target_history_template_id,
                           duplicate_index=index, strategy_group_id=group_id, override=None)
            for index in range(1, 11)
        ])
        source_reports: dict[tuple[int, str], Mapping[str, object]] = {}
        seed_fallback_reasons: set[str] = set()
        for duplicate_index, (original, base) in enumerate(
            zip(originals, baselines), start=1
        ):
            ranking_reference = ranking_references[duplicate_index - 1]
            report_progress(
                phase="candidate_materialization",
                message="正在枚举 A/B 与 Seed 并生成牌面",
                duplicate_index=duplicate_index,
                candidate_completed=0,
                candidate_total=0,
            )
            monotonic_anchor = (
                monotonic_anchor_pairs[duplicate_index - 1]
                if monotonic_anchor_pairs is not None
                else None
            )
            target = _target_record(
                target_history,
                source_template_id=target_history_template_id,
                duplicate_index=duplicate_index,
                strategy_group_id=group_id,
                override=parsed_overrides.get(duplicate_index),
            ) if duplicate_index in parsed_overrides else dict(online_targets[duplicate_index - 1])
            targets.append(target)
            consumption_anchor = {
                "kind": "processed_source_assumed_equal_online" if processed_row and config_source_id != source_id else "canonical_online_target",
                "source_template_id": source_id,
                "target_template_id": target_history_template_id,
                "duplicate_index": duplicate_index,
                "source_dup": ranking_reference.render(),
                "online_dup": original.render(),
                "target_consumption": target.get("value"),
                "target_source": target.get("source"),
                "observed": not bool(processed_row and config_source_id != source_id) and target.get("source") == "source_template_online",
                "assumption_source": "user_authorized_working_assumption" if processed_row and config_source_id != source_id else None,
            }
            focus_first = bool(focus_output_id and duplicate_index == 1)
            search_base = search_baselines[duplicate_index - 1]
            # Seed has no fitted causal consumption coefficient.  Every
            # Seed admitted by the requested policy is nevertheless
            # materialized so the complete-board simulator can rank the
            # resulting board, not the numeric Seed token.
            duplicate_seed_mode = _seed_search_mode(
                duplicate_index=duplicate_index,
                search_observed_seeds=search_observed_seeds,
                optimize_all_duplicate_seeds=optimize_all_duplicate_seeds,
                simulate_first_duplicate_seeds=simulate_first_duplicate_seeds,
                focused_recalculation=bool(focus_output_id),
            )
            seeds = _seed_search_values(
                history,
                duplicate_index=duplicate_index,
                strategy_group_id=group_id,
                original=ranking_reference,
                mode=duplicate_seed_mode,
            )
            objective_weight = _duplicate_objective_weight(duplicate_index)
            raw_candidates: list[dict[str, Any]] = []
            for parameter in _candidate_space(
                search_originals[duplicate_index - 1],
                search_base,
                seeds=seeds,
                additional_ab_pairs=(monotonic_anchor,)
                if monotonic_anchor is not None
                else (),
            ):
                if focus_first:
                    candidate_max = max(parameter.a, parameter.b)
                    if (candidate_max > first_duplicate_max
                            or (not auto_first_duplicate_max and candidate_max != first_duplicate_max)):
                        continue
                association = _cached_association_estimate(
                    association_history_index,
                    association_cache,
                    duplicate_index=duplicate_index,
                    strategy_group_id=group_id,
                    candidate=parameter,
                )
                prediction = _consumption_prediction(
                    target=target.get("value"),
                    manifest=manifest,
                    original=ranking_reference,
                    candidate=parameter,
                    duplicate_index=duplicate_index,
                    level_index=selected_level_index,
                    model=structure_correction_model,
                    static_structure_features=static_structure_features,
                    strategy_group_id=group_id,
                )
                prediction_loss = _association_loss(
                    prediction.get("value"), target.get("value")
                )
                # The source duplicate target is the anchor. Historical
                # neighbours are retained as context only; Seed can win only
                # through its materialized full-board simulation.
                # Keep the real prediction and primary simulation objective.
                # The mechanical baseline is only a final tie-break, including
                # when an in-scope empirical prediction is available.
                node_score = objective_weight * prediction_loss
                raw_candidates.append(
                    {
                        "parameter": parameter,
                        "original": original,
                        "ranking_reference": ranking_reference,
                        "baseline": base,
                        "association": association,
                        "consumption_prediction": prediction,
                        "consumption_anchor": consumption_anchor,
                        "objective_weight": objective_weight,
                        "simulation_score": 0.0,
                        "node_score": node_score,
                    }
                )
            raw_candidates.sort(key=_candidate_sort_key)
            prefilter_candidates, search_evidence = _prefilter_ab_candidates(
                raw_candidates,
                original=ranking_reference,
                baseline=search_base,
                required_anchors=(monotonic_anchor,)
                if monotonic_anchor is not None
                else (),
            )
            search_evidence["monotonic_bounds_feasible"] = (
                monotonic_bounds_feasible
            )
            search_evidence["enumeration_policy"] = "source_parameters_then_decrements"
            search_evidence["source_starting_dup"] = ranking_reference.render()
            search_evidence["parameter_bounds"] = {
                "a": [search_base.a, search_originals[duplicate_index - 1].a],
                "b": [search_base.b, search_originals[duplicate_index - 1].b],
            }
            if automatic_bounds is not None:
                search_evidence["first_duplicate_automatic_bounds"] = automatic_bounds
            search_evidence["monotonic_anchor_pair"] = (
                list(monotonic_anchor) if monotonic_anchor is not None else None
            )
            source_materialized: tuple[tuple[str, ...], dict[str, Any]] | None = None
            source_geometry_evidence: dict[str, Any] | None = None
            source_materialization_reason: str | None = online_source_load_reason
            if online_source_level is None:
                pass
            elif random_seed_group_reason is not None:
                source_materialization_reason = random_seed_group_reason
            elif random_seed_group_id > 0 and (
                seed_config_context is None or selected_level_index is None
            ):
                source_materialization_reason = _fallback_reason(
                    "structured_random_seed_group_unsupported",
                    f"randomSeed_groupID={random_seed_group_id} 尚不支持本地重放",
                )
            elif seed_config_context is None:
                source_materialization_reason = seed_config_fallback or _fallback_reason(
                    "seed_config_unavailable", "Seed 配置不可用"
                )
            elif selected_level_index is None:
                source_materialization_reason = _fallback_reason(
                    "online_position_missing", "没有可用的线上关卡位置"
                )
            else:
                try:
                    source_materialized = _whole_board_labels(
                        online_source_level,
                        (),
                        ranking_reference,
                        level_index=selected_level_index,
                        config_context=seed_config_context,
                        candidate=False,
                        random_seed_group_id=random_seed_group_id,
                        apply_structure_strategy=consider_structure_strategy,
                        strategy_group_id=group_id,
                        strategy_rule_raw=configured_strategy["rules"][duplicate_index - 1],
                        duplicate_index=duplicate_index,
                    )
                    aligned_labels, source_geometry_evidence = (
                        _align_source_labels_if_geometry_compatible(
                            online_source_level,
                            loaded.level,
                            source_materialized[0],
                        )
                    )
                    if aligned_labels is None:
                        source_materialized = None
                        source_materialization_reason = _fallback_reason(
                            "online_source_geometry_incompatible",
                            "原线上模板与洗白模板的坐标、层级或遮挡图不一致",
                        )
                    else:
                        source_materialized = (
                            aligned_labels,
                            source_materialized[1],
                        )
                except (ValueError, TypeError, KeyError) as exc:
                    source_materialization_reason = _materialization_failure_reason(
                        exc, random_seed_group_id=random_seed_group_id
                    )
            for candidate in prefilter_candidates:
                parameter: DupParam = candidate["parameter"]
                source_labels: tuple[str, ...] | None = None
                candidate_labels: tuple[str, ...] | None = None
                pair_materialization: dict[str, Any] | None = None
                fallback_reason = source_materialization_reason
                if source_materialized is not None:
                    try:
                        candidate_materialized = _whole_board_labels(
                            loaded.level,
                            replacements,
                            parameter,
                            level_index=int(selected_level_index),
                            config_context=seed_config_context,
                            candidate=True,
                            random_seed_group_id=random_seed_group_id,
                            apply_structure_strategy=consider_structure_strategy,
                            strategy_group_id=group_id,
                            strategy_rule_raw=configured_strategy["rules"][duplicate_index - 1],
                            duplicate_index=duplicate_index,
                        )
                        source_labels, source_meta = source_materialized
                        candidate_labels, candidate_meta = candidate_materialized
                        pair_materialization = {
                            "mode": LEGACY_EVIDENCE_LEVEL,
                            "level_index": selected_level_index,
                            "config_paths": list(seed_config_context["source_paths"]),
                            "source": source_meta,
                            "candidate": candidate_meta,
                            "strategy_group_id": group_id,
                            "random_seed_group_id": random_seed_group_id,
                            "strategy_group_is_not_random_seed_group": True,
                            "structure_strategy_considered": consider_structure_strategy,
                            "source_structure_strategy": source_meta.get("structure_strategy"),
                            "candidate_structure_strategy": candidate_meta.get("structure_strategy"),
                            "online_source_template_id": config_source_id,
                        "source_board_template_id": (
                            source_id if config_source_match == "canonical_history_alias"
                            else config_source_id
                        ),
                            "clean_source_template_id": source_id,
                            "source_geometry": source_geometry_evidence,
                        }
                        candidate["seed_simulation"] = {
                            **candidate_meta,
                            "source": source_meta,
                            "config_paths": list(seed_config_context["source_paths"]),
                            "strategy_group_id": group_id,
                            "strategy_group_is_not_random_seed_group": True,
                            "structure_strategy_considered": consider_structure_strategy,
                            "structure_strategy": candidate_meta.get("structure_strategy"),
                        }
                        candidate["light_seed_distance"] = (
                            _light_materialized_board_distance(
                                loaded.level, source_labels, candidate_labels
                            )
                        )
                    except (ValueError, TypeError, KeyError) as exc:
                        fallback_reason = _materialization_failure_reason(
                            exc, random_seed_group_id=random_seed_group_id
                        )
                if pair_materialization is None:
                    reason = fallback_reason or _fallback_reason(
                        "seed_materialization_unavailable", "Seed 牌面仿真不可用"
                    )
                    seed_fallback_reasons.add(reason)
                    candidate["seed_simulation"] = _synthetic_seed_metadata(
                        level_index=selected_level_index,
                        reason=reason,
                        random_seed_group_id=random_seed_group_id,
                    )
                    candidate["light_seed_distance"] = float("inf")
                candidate["_source_labels"] = source_labels
                candidate["_candidate_labels"] = candidate_labels
                candidate["_pair_materialization"] = pair_materialization

            simulation_candidates, seed_search_evidence = (
                _prefilter_seed_candidates(
                    prefilter_candidates,
                    original=ranking_reference,
                )
            )
            search_evidence.update(seed_search_evidence)
            search_evidence["seed_search_mode"] = duplicate_seed_mode
            report_progress(
                phase="full_simulation",
                message="正在对候选执行完整策略仿真",
                candidate_total=len(simulation_candidates),
            )
            for candidate_index, candidate in enumerate(simulation_candidates, start=1):
                parameter = candidate["parameter"]
                source_labels = candidate.get("_source_labels")
                candidate_labels = candidate.get("_candidate_labels")
                pair_materialization = candidate.get("_pair_materialization")
                simulation_mode = str(candidate["seed_simulation"]["mode"])
                simulation_evidence_reasons: list[str] = []
                if baseline_resolution.get("demote_recommendation"):
                    simulation_evidence_reasons.append(
                        "baseline_config_resolution_ambiguous"
                    )
                if pair_materialization is None:
                    simulation_evidence_reasons.append(
                        str(
                            candidate["seed_simulation"].get("fallback_code")
                            or "seed_materialization_unavailable"
                        )
                    )
                if (
                    consider_structure_strategy
                    and configured_strategy.get("status") != "available"
                ):
                    simulation_evidence_reasons.append(
                        "structure_strategy_config_unavailable"
                    )
                try:
                    pair = compare_duplicate_difficulty_pair(
                        loaded.level,
                        replacements,
                        source_duplicate=DuplicateParameter(
                            a=ranking_reference.a,
                            seed=ranking_reference.seed,
                            luck=ranking_reference.luck,
                            b=ranking_reference.b,
                        ),
                        candidate_duplicate=DuplicateParameter(
                            a=parameter.a,
                            seed=parameter.seed,
                            luck=parameter.luck,
                            b=parameter.b,
                        ),
                        duplicate_index=duplicate_index,
                        source_report=source_reports.get(
                            (duplicate_index, simulation_mode)
                        ),
                        source_materialized_labels=source_labels,
                        candidate_materialized_labels=candidate_labels,
                        materialization_evidence=pair_materialization,
                    )
                    source_reports[(duplicate_index, simulation_mode)] = pair["source"]
                    distance = pair["difficulty_distance"].get(
                        "primary_distance"
                    )
                    distance = _finite_distance(distance)
                    candidate["difficulty"] = {
                        "distance": distance,
                        "stable_tiebreak": _finite_distance(
                            pair["difficulty_distance"].get("stable_tiebreak")
                        ),
                        "legacy_score_difference": pair[
                            "legacy_score_difference"
                        ],
                        "evidence": pair["evidence"],
                    }
                    if distance is not None:
                        candidate["simulation_score"] = objective_weight * 0.25 * float(distance)
                        candidate["node_score"] += (
                            objective_weight * 0.25 * float(distance)
                        )
                    else:
                        candidate["simulation_score"] = 0.5
                        candidate["node_score"] += 0.5
                        simulation_evidence_reasons.append("whole_board_difficulty_unavailable")
                except (ValueError, TypeError) as exc:
                    candidate["simulation_score"] = 0.5
                    candidate["difficulty"] = {
                        "distance": None,
                        "status": "unavailable",
                        "reason": str(exc),
                    }
                    candidate["node_score"] += 0.5
                    simulation_evidence_reasons.append(
                        "whole_board_difficulty_unavailable"
                    )
                candidate["simulation_evidence_reasons"] = sorted(
                    set(simulation_evidence_reasons)
                )
                candidate["simulation_evidence_ready"] = not bool(
                    simulation_evidence_reasons
                )
                report_progress(candidate_completed=candidate_index)
            simulation_candidates.sort(key=_candidate_sort_key)
            shortlist = list(
                simulation_candidates[:DP_SHORTLIST_LIMIT_PER_DUPLICATE]
            )
            # Preserve exact baseline/original anchors so a valid monotonic
            # ten-duplicate path always remains available to the DP solver.
            for anchor in (
                next(
                    (
                        item
                        for item in simulation_candidates
                        if item["parameter"].a == base.a
                        and item["parameter"].b == base.b
                        and item["parameter"].luck == base.luck
                    ),
                    None,
                ),
                next(
                    (
                        item
                        for item in simulation_candidates
                        if item["parameter"].a == ranking_reference.a
                        and item["parameter"].b == ranking_reference.b
                        and item["parameter"].luck == ranking_reference.luck
                    ),
                    None,
                ),
                next(
                    (
                        item
                        for item in simulation_candidates
                        if monotonic_anchor is not None
                        and (
                            item["parameter"].a,
                            item["parameter"].b,
                        )
                        == monotonic_anchor
                    ),
                    None,
                ),
            ):
                if anchor is not None and anchor not in shortlist:
                    shortlist.append(anchor)
            search_evidence["dp_shortlist_count"] = len(shortlist)
            search_evidence["dp_shortlist_base_limit"] = (
                DP_SHORTLIST_LIMIT_PER_DUPLICATE
            )
            search_evidence["all_seed_values_competed"] = (
                int(search_evidence.get("enumerated_seed_count") or 0) > 0
                and search_evidence.get("light_prefilter_candidate_count")
                == search_evidence.get("selected_ab_count")
                * search_evidence.get("enumerated_seed_count")
            )
            search_evidence_by_duplicate.append(search_evidence)
            candidates_by_duplicate.append(shortlist)
            report_progress(
                duplicate_completed=template_index * 10 + duplicate_index,
            )

        if len(candidates_by_duplicate) != 10 or any(
            not values for values in candidates_by_duplicate
        ):
            warnings.append(f"{source_id} 无法产生完整十档校准路径。")
            continue

        # Dynamic programming keeps the ten rows jointly close to the source
        # A/B progression instead of greedily fitting noisy observations one
        # duplicate at a time.
        report_progress(
            phase="joint_selection",
            message="正在联合检查十档参数梯度并整理审阅结果",
        )
        selected_indices = _select_monotonic_candidate_path(candidates_by_duplicate)
        used_infeasible_bounds_fallback = False
        if selected_indices is None:
            # The configured per-duplicate lower/upper bounds themselves can
            # be mutually incompatible.  Preserve those bounds and return the
            # full fidelity boundary for review; never tunnel below a later
            # duplicate's mechanical lower bound merely to manufacture a
            # monotonic path.
            used_infeasible_bounds_fallback = True
            selected_indices = [0] * 10
            for index, choices in enumerate(candidates_by_duplicate):
                eligible = [
                    (choice_index, choice)
                    for choice_index, choice in enumerate(choices)
                    if choice["parameter"].a == choice["baseline"].a
                    and choice["parameter"].b == choice["baseline"].b
                ]
                if not eligible:
                    eligible = list(enumerate(choices))
                chosen_index, chosen = min(
                    eligible,
                    key=lambda item: _candidate_sort_key(item[1]),
                )
                selected_indices[index] = chosen_index
            warnings.append(
                f"{source_id} 的常规 DP 前沿为空；已输出完整十档"
                "保真边界，但因上下限无法同时形成无回弹路径，"
                "全部降级为人工处理。"
            )
        duplicate_rows: list[dict[str, Any]] = []
        gradient_warnings: list[str] = []
        if used_infeasible_bounds_fallback:
            gradient_warnings.append(
                "逐档机械扣减下限与原参数上限无法组成无回弹路径；"
                "已保留十档边界值并降级为人工处理。"
            )
        for duplicate_index, (choices, selected_index, target) in enumerate(
            zip(candidates_by_duplicate, selected_indices, targets), start=1
        ):
            selected = choices[selected_index]
            if used_infeasible_bounds_fallback:
                selected["simulation_evidence_ready"] = False
                selected["simulation_evidence_reasons"] = sorted(
                    set(selected.get("simulation_evidence_reasons") or [])
                    | {"monotonic_parameter_bounds_infeasible"}
                )
            alternative_choices = [
                choice
                for index, choice in enumerate(choices)
                if index != selected_index
            ]
            retained_seed_alternative = next(
                (
                    choice
                    for choice in alternative_choices
                    if choice["parameter"].seed == choice["ranking_reference"].seed
                ),
                None,
            )
            changed_seed_alternative = next(
                (
                    choice
                    for choice in alternative_choices
                    if choice["parameter"].seed != choice["ranking_reference"].seed
                ),
                None,
            )
            chosen_alternatives = [
                choice
                for choice in (retained_seed_alternative, changed_seed_alternative)
                if choice is not None
            ]
            if len(chosen_alternatives) < 2:
                for choice in alternative_choices:
                    if choice not in chosen_alternatives:
                        chosen_alternatives.append(choice)
                    if len(chosen_alternatives) == 2:
                        break
            alternatives = [
                _serialize_candidate(
                    choice, target, is_selected_best_fit=False
                )
                for choice in chosen_alternatives[:2]
            ]
            original: DupParam = selected["original"]
            baseline_param: DupParam = selected["baseline"]
            serialized = _serialize_candidate(selected, target)
            if len(alternatives) < 2:
                gradient_warnings.append(
                    f"DupPara{duplicate_index} 在基线到原参数的可枚举范围内"
                    "不足两个不同备选。"
                )
            duplicate_rows.append(
                {
                    "duplicate_index": duplicate_index,
                    "objective_weight": _duplicate_objective_weight(duplicate_index),
                    "search_evidence": search_evidence_by_duplicate[
                        duplicate_index - 1
                    ],
                    "original_dup": original.render(),
                    "ranking_reference_dup": selected["ranking_reference"].render(),
                    "consumption_anchor": dict(selected["consumption_anchor"]),
                    "fidelity_baseline_dup": baseline_param.render(),
                    "recommended_dup": serialized["dup_para"],
                    "seed_changed": serialized["seed_changed"],
                    "luck_preserved": serialized["luck_preserved"],
                    "strategy_group_id": group_id,
                    "random_seed_group_id": random_seed_group_id,
                    "configured_strategy_rule": {
                        "raw": configured_strategy["rules"][duplicate_index - 1],
                        "status": configured_strategy["status"],
                        "claim_limit": configured_strategy.get("claim_limit"),
                    },
                    "observed_effective_strategies": target[
                        "effective_strategies"
                    ],
                    "target_consumption": target,
                    "historical_association": serialized["historical_association"],
                    "expected_consumption": serialized["expected_consumption"],
                    "recommendation": serialized["recommendation"],
                    "synthetic_difficulty": serialized["synthetic_difficulty"],
                    "seed_simulation": serialized["seed_simulation"],
                    "evidence_level": serialized["evidence_level"],
                    "alternatives": alternatives,
                }
            )

        for previous, current in zip(duplicate_rows, duplicate_rows[1:]):
            previous_new = parse_dup_param(previous["recommended_dup"])
            current_new = parse_dup_param(current["recommended_dup"])
            if previous_new.a < current_new.a or previous_new.b < current_new.b:
                gradient_warnings.append(
                    f"DupPara{previous['duplicate_index']}→{current['duplicate_index']} "
                    "的建议出现 A/B 回升；该路径不应被联合求解器选中。"
                )
        if (
            "strategy_effective_list"
            not in history_coverage.get("missing_optional_columns", [])
            and any(
                not row["observed_effective_strategies"]
                for row in duplicate_rows
            )
        ):
            gradient_warnings.append(
                f"历史数据中部分 {group_id} 行未记录 strategy_effective_list；"
                "结果保留策略组号，不猜测未观测的生效策略。"
            )
        (
            unqualified_count,
            outside_tolerance_count,
            evidence_demoted_count,
        ) = _recommendation_issue_counts(
            duplicate_rows
        )
        if outside_tolerance_count:
            gradient_warnings.append(
                f"{outside_tolerance_count}/10 个副本的校正预估与线上目标偏差超过 25%；"
                "仍已输出联合搜索后的最接近配置，可继续人工审阅或写入。"
            )
        if evidence_demoted_count:
            gradient_warnings.append(
                f"{evidence_demoted_count}/10 个副本因校正模型或仿真证据不完整"
                "未满足推荐条件；仍已输出当前最接近配置供人工复核。"
            )
        result_rows.append(
            {
                "source_template_id": source_id,
                "config_source_template_id": config_source_id,
                "config_source_match": config_source_match,
                "canonical_template_id": canonical_matches[output_id][0],
                "template_name_match": canonical_matches[output_id][1],
                "source_parameter_comparison": source_parameter_comparison,
                "consumption_anchor": {
                    key: duplicate_rows[0]["consumption_anchor"].get(key)
                    for key in ("kind", "source_template_id", "target_template_id", "observed", "assumption_source")
                },
                "output_template_id": output_id,
                "source_fingerprints": {
                    "online_source_template_id": config_source_id,
                    "source_board_template_id": (
                        source_id if config_source_match == "canonical_history_alias"
                        else config_source_id
                    ),
                    "online_source_template_sha256": (
                        online_source_level.sha256
                        if online_source_level is not None
                        and config_source_match != "canonical_history_alias"
                        else None
                    ),
                    "source_board_sha256": (
                        online_source_level.sha256
                        if online_source_level is not None else None
                    ),
                    "clean_source_template_id": source_id,
                    "source_template_sha256": loaded.level.sha256,
                    "manifest_source_sha256": manifest.get("source_sha256"),
                    "manifest_output_sha256": manifest.get("output_sha256"),
                },
                "baseline_resolution": {
                    **baseline_resolution,
                    "target_history_scope": target_history_scope,
                    "target_history_template_id": target_history_template_id,
                    "target_history_row_count": len(target_history),
                },
                "empirical_structure_features": {
                    "status": (
                        "runtime_exact"
                        if static_structure_features is not None
                        else "unavailable"
                    ),
                    "fallback_reason": static_structure_feature_reason,
                    "training_extractor_parity": (
                        static_structure_features is not None
                    ),
                },
                "level_ids": baseline_row.get("level_ids", []),
                "online_positions": baseline_row.get("online_positions", []),
                "seed_simulation_level_index": selected_level_index,
                "seed_simulation_level_index_source": (
                    simulation_level_index_source
                ),
                "seed_simulation_position_warning": position_warning,
                "seed_simulation_fallback_reasons": sorted(seed_fallback_reasons),
                "source_simulation_reference": {
                    "status": (
                        ("manifest_source_paired"
                         if config_source_match == "canonical_history_alias"
                         else "online_source_paired")
                        if online_source_level is not None
                        and not seed_fallback_reasons
                        else "partial_or_full_fallback"
                    ),
                    "online_source_template_id": config_source_id,
                    "source_board_template_id": (
                        source_id if config_source_match == "canonical_history_alias"
                        else config_source_id
                    ),
                    "clean_source_template_id": source_id,
                    "claim_limit": (
                        "使用 manifest 加工源阵型及加工源原副本参数配对仿真；加工源原盘消耗按用户假设锚定本次目标，不宣称线上实测或加工前重放。"
                        if config_source_match == "canonical_history_alias"
                        else "仅在原线上模板与洗白模板几何兼容时执行原盘与候选盘配对仿真。"
                    ),
                },
                "strategy_group_id": group_id,
                "random_seed_group_id": random_seed_group_id,
                "search_observed_seeds": search_observed_seeds,
                "requested_optimize_all_duplicate_seeds": optimize_all_duplicate_seeds,
                "optimize_all_duplicate_seeds": optimize_all_duplicate_seeds,
                "duplicates": duplicate_rows,
                "recommendation_summary": {
                    "recommended_count": 10 - unqualified_count,
                    "review_count": unqualified_count,
                    "all_recommended": unqualified_count == 0,
                },
                "gradient": {
                    **_gradient_policy_evidence(
                        used_infeasible_bounds_fallback
                    ),
                    "duplicate_objective_weights": {
                        str(index): weight
                        for index, weight in DUPLICATE_OBJECTIVE_WEIGHTS.items()
                    },
                    "warnings": gradient_warnings,
                },
                "first_duplicate_adjustment": (
                    {
                        **focus_bounds,
                        "seed_search": (
                            "0–31"
                            if simulate_first_duplicate_seeds
                            else (
                                "历史已观测 + 原 Seed"
                                if search_observed_seeds
                                else "原 Seed"
                            )
                        ),
                        "selected_dup": duplicate_rows[0]["recommended_dup"],
                        "expected_consumption": duplicate_rows[0]["expected_consumption"],
                    }
                    if focus_bounds is not None else None
                ),
            }
        )
        report_progress(template_completed=template_index + 1)

    if focus_output_id and not matching_focus_found:
        raise InputValidationError(f"未找到待重算的新模板：{focus_output_id}")

    seed_simulations = [
        duplicate.get("seed_simulation", {})
        for row in result_rows
        for duplicate in row.get("duplicates", [])
    ]
    simulation_modes = sorted(
        {
            str(item.get("mode"))
            for item in seed_simulations
            if item.get("mode")
        }
    )
    if len(simulation_modes) == 1:
        overall_simulation_mode = simulation_modes[0]
    elif simulation_modes:
        overall_simulation_mode = "mixed"
    else:
        overall_simulation_mode = (
            LEGACY_EVIDENCE_LEVEL
            if seed_config_context is not None
            else "synthetic"
        )
    configured_random_seed_group_ids = sorted(
        {
            int(row.get("random_seed_group_id", 0))
            for row in result_rows
        }
    )
    simulation_fallback_reasons = sorted(
        {
            str(item.get("fallback_reason"))
            for item in seed_simulations
            if item.get("fallback_reason")
        }
    )
    overall_fallback_reason = seed_config_fallback
    if len(simulation_fallback_reasons) == 1:
        overall_fallback_reason = simulation_fallback_reasons[0]

    strategy_group_ids = sorted(
        {str(row.get("strategy_group_id") or DEFAULT_STRATEGY_GROUP_ID) for row in result_rows}
    )
    scoped_history = [
        row
        for row in history
        if any(_strategy_matches(row.strategy_group_id, group) for group in strategy_group_ids)
    ]
    history_coverage.update(
        {
            "selected_strategy_group_id": (
                strategy_group_ids[0] if len(strategy_group_ids) == 1 else "source"
            ),
            "selected_strategy_group_ids": strategy_group_ids,
            "selected_group_row_count": len(scoped_history),
            "selected_group_sample_entrants": int(sum(row.entrants for row in scoped_history)),
            "selected_group_covered_duplicate_indices": sorted(
                {row.duplicate_index for row in scoped_history}
            ),
            "selected_group_covered_duplicate_count": len(
                {row.duplicate_index for row in scoped_history}
            ),
        }
    )
    strategy_configs = [configured_strategy_cache[group] for group in strategy_group_ids]
    strategy_available = bool(strategy_configs) and all(
        item.get("status") == "available" for item in strategy_configs
    )
    strategy_warnings = [
        warning for item in strategy_configs for warning in item.get("warnings", [])
    ]
    aggregate_strategy_config = {
        "status": "available" if strategy_available else "unavailable",
        "strategy_group_ids": strategy_group_ids,
        "source_paths": sorted(
            {path for item in strategy_configs for path in item.get("source_paths", [])}
        ),
    }
    requested_seed_mode = (
        "joint"
        if optimize_all_duplicate_seeds
        else "observed"
        if search_observed_seeds
        else "preserve"
    )
    effective_seed_mode = (
        f"focused_dup1_joint_then_{requested_seed_mode}"
        if focus_output_id
        and simulate_first_duplicate_seeds
        and not optimize_all_duplicate_seeds
        else requested_seed_mode
    )
    empirical_correction_applied = any(
        bool(
            duplicate.get("expected_consumption", {})
            .get("structure_correction", {})
            .get("applied")
        )
        for row in result_rows
        for duplicate in row.get("duplicates", [])
    )
    transfer_estimate_applied = any(
        duplicate.get("expected_consumption", {}).get("estimate_mode") == "transfer_estimate"
        for row in result_rows for duplicate in row.get("duplicates", [])
    )
    no_new_parameter_rebound = bool(result_rows) and all(
        bool(row.get("gradient", {}).get("no_new_parameter_rebound"))
        for row in result_rows
    )
    report_progress(
        phase="completed",
        message="校准审阅草案已完成",
        template_completed=len(baseline_rows),
        duplicate_completed=len(baseline_rows) * 10,
    )

    return {
        "schema_version": 1,
        "mode": "target_consumption_calibration",
        "auto_first_duplicate_max": auto_first_duplicate_max,
        "history_file": history_filename,
        "history_sha256": history_coverage["sha256"],
        "history_rows": len(history),
        "coverage": history_coverage,
        "strategy_group_id": strategy_group_ids[0] if len(strategy_group_ids) == 1 else "source",
        "strategy_group_ids": strategy_group_ids,
        "strategy_group_mode": strategy_mode,
        "config_set_id": config_set_id,
        "level_range": {"start": level_start, "end": level_end, "source": "history_csv" if observed_positions else "all_levels"},
        "consider_structure_strategy": consider_structure_strategy,
        "requested_optimize_all_duplicate_seeds": optimize_all_duplicate_seeds,
        "optimize_all_duplicate_seeds": optimize_all_duplicate_seeds,
        "calibration_objective": {
            "kind": "weighted_source_target_best_fit",
            "duplicate_weights": {
                str(index): weight
                for index, weight in DUPLICATE_OBJECTIVE_WEIGHTS.items()
            },
            "priority_order": ["DupPara1", "DupPara2", "DupPara3", "DupPara4", "DupPara5–10"],
            "no_new_parameter_rebound": no_new_parameter_rebound,
            "no_new_parameter_rebound_requirement": "required_when_bounds_feasible",
            "complete_path_returned_when_tolerance_missed": True,
            "ranking_cost_order": [
                "missing_estimate_count", "weighted_relative_target_gap",
                "missing_primary_count", "weighted_primary_plus_gradient",
                "missing_secondary_count", "weighted_stable_tiebreak",
                "seed_change_count", "mechanical_distance",
            ],
        },
        "consumption_prediction_model": {
            "status": (
                "available" if structure_correction_model is not None else "fallback"
            ),
            "model_id": (
                structure_correction_model.get("model_id")
                if structure_correction_model is not None
                else None
            ),
            "scope": (
                structure_correction_model.get("scope", {})
                if structure_correction_model is not None
                else {}
            ),
            "validation": (
                structure_correction_model.get("validation", {})
                if structure_correction_model is not None
                else {}
            ),
            "fallback": "unavailable_no_target_substitution",
            "training_scope": dict(structure_correction_model.get("scope", {})) if structure_correction_model is not None else {},
            "application_policy": "unchanged_bounded_multiplier_with_explicit_cross_band_transfer",
            "transfer_estimate_applied": transfer_estimate_applied,
            "historical_neighbours_role": "context_only",
            "seed_causal_coefficient": False,
            "applied_to_any_result": empirical_correction_applied,
        },
        "seed_search": {
            "mode": {
                "joint": "joint_ab_seed_0_31",
                "observed": "observed_seeds_plus_original",
                "preserve": "preserve_original_seed",
            }.get(effective_seed_mode, effective_seed_mode),
            "requested_mode": requested_seed_mode,
            "effective_mode": effective_seed_mode,
            "candidate_stage": "all_feasible_ab_times_requested_seed_set",
            "prefilter": {
                "ab_limit_per_duplicate": AB_PREFILTER_LIMIT_PER_DUPLICATE,
                "seed_limit_per_ab": SEED_PREFILTER_LIMIT_PER_AB,
                "all_requested_seeds_use_light_materialized_board_distance": True,
                "all_32_seeds_use_light_materialized_board_distance": (
                    optimize_all_duplicate_seeds
                ),
                "baseline_and_original_ab_anchors": True,
                "original_seed_anchor_policy": (
                    "when_present_in_requested_seed_set"
                ),
            },
            "full_simulation_scope": "bounded_after_ab_and_light_seed_prefilter",
            "full_simulation_upper_bound_per_duplicate": (
                (AB_PREFILTER_LIMIT_PER_DUPLICATE + 3)
                * (SEED_PREFILTER_LIMIT_PER_AB + 1)
            ),
            "dp_shortlist_limit_per_duplicate": (
                DP_SHORTLIST_LIMIT_PER_DUPLICATE + 3
            ),
            "path_solver": "ten-duplicate dynamic programming",
        },
        "strategy_group_config": aggregate_strategy_config,
        "seed_simulation_config": {
            "mode": overall_simulation_mode,
            "source_paths": (
                list(seed_config_context["source_paths"])
                if seed_config_context is not None
                else []
            ),
            "fallback_code": _fallback_code(overall_fallback_reason),
            "fallback_reason": overall_fallback_reason,
            "fallback_reasons": simulation_fallback_reasons,
            "online_position_policy": "first_sorted_online_position",
            "iap_protect_cut_a_count": 0,
            "random_seed_group_id": (
                configured_random_seed_group_ids[0]
                if len(configured_random_seed_group_ids) == 1
                else None
            ),
            "random_seed_group_ids": configured_random_seed_group_ids,
            "strategy_group_is_not_random_seed_group": True,
        },
        "rows": result_rows,
        "warnings": list(dict.fromkeys(warnings + strategy_warnings)),
        "source_tables": baseline.get("source_tables", {}),
        "access": "read_only",
        "config_mutation": False,
        "writes_performed": [],
        "claim_level": (
            "transfer_estimate" if transfer_estimate_applied else "bounded_empirical_beta"
            if empirical_correction_applied
            else "target_anchored_directional_beta"
        ),
        "seed_causal_identification": False,
        "structure_strategy_replay": {
            "enabled": consider_structure_strategy,
            "status": (
                "local_sampled_replay"
                if consider_structure_strategy
                and strategy_available
                else "disabled"
                if not consider_structure_strategy
                else "unavailable"
            ),
            "scope": "all_configured_strategy_groups",
            "execution_order": (
                "InstanceIdN ordered/random candidate order; failed strategies are "
                "skipped and remaining strategies backfill until requested count or exhaustion"
            ),
            "applied_before_ab_materialization": True,
            "exact_client_replay": False,
            "random_claim": "reproducible local sample; not exact client random stream",
            "wiki_page_id": "277080406",
            "wiki_version": 85,
        },
        "claim_limit": (
            "Recommendations anchor to each source duplicate target and use a bounded "
            "empirical correction, with explicitly labeled unvalidated transfer outside its training band. Historical neighbours "
            "are context, and Seed is ranked only by board simulation. Every row is a "
            "best-fit review candidate, not proof that a target value will be reached."
        ),
    }


__all__ = [
    "DEFAULT_STRATEGY_GROUP_ID",
    "HistoryRow",
    "calibrate_consumption_parameters",
    "parse_consumption_history",
]
