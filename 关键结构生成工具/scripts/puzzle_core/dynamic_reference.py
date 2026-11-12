"""Frozen whole-board reveal references for the audited online cohort.

The source template often contains only A/B tiles, so a generated C--N
candidate cannot be compared with an empty source reveal curve.  This module
compares it with a versioned, offline-derived reference instead.  Every
template contributes one vote per DupPara/strategy feature; random trial
counts are never pooled across templates.

The reference is descriptive rather than outcome-calibrated.  It is used for
ranking and evidence coverage, never as a claim about exact win rate.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Mapping, Sequence


DYNAMIC_REFERENCE_VERSION = "level-200-300-whole-board-reveal-v1"
DYNAMIC_REFERENCE_FILE = Path(__file__).with_name(
    "online_dynamic_reference.json"
)
MIN_ARCHETYPE_REFERENCE_SAMPLE = 8
_STRATEGIES = (
    "random",
    "greedy",
    "local_optimal",
    "unlock",
    "adversarial",
)
_CURVE_METRICS = (
    "current_clickable_structure_count",
    "newly_exposed_structure_count",
    "cumulative_exposed_structure_count",
    "complete_structure_group_type_count",
    "dangling_one_type_count",
    "dangling_two_type_count",
)
_DISTRIBUTION_METRICS = (
    "dangling_one_area",
    "dangling_two_area",
    "dangling_one_max_duration",
    "dangling_two_max_duration",
    "released_ab_per_structure_click",
    "released_ab_on_match_completion_click",
    "released_ab_total_per_path",
)


def _distribution_median(value: object) -> float | None:
    if not isinstance(value, Mapping):
        return None
    raw = value.get("median")
    if isinstance(raw, (int, float)):
        return float(raw)
    return None


def _family_for_key(key: str) -> str:
    if ".curve." in key:
        return "five_stage_curve"
    if ".match." in key:
        return "first_three_matches"
    if ".dangling_" in key:
        return "dangling_exposure"
    if ".released_" in key:
        return "ab_release"
    if ".present." in key:
        return "evidence_presence"
    return "other"


def extract_dynamic_features(
    rows: Sequence[Mapping[str, object]],
    *,
    structure_count: int,
    board_size: int,
) -> dict[str, float]:
    """Return DupPara1--10 x five-strategy normalized reveal features."""

    count_scale = max(1.0, float(structure_count))
    type_scale = max(1.0, count_scale / 3.0)
    board_scale = max(1.0, float(board_size))
    features: dict[str, float] = {}
    for fallback_index, row in enumerate(rows, 1):
        duplicate_index = int(row.get("duplicate_index", fallback_index))
        raw_strategies = row.get("strategies")
        strategies = (
            raw_strategies if isinstance(raw_strategies, Mapping) else {}
        )
        for strategy in _STRATEGIES:
            prefix = f"dup{duplicate_index:02d}.{strategy}"
            summary = strategies.get(strategy)
            reveal = (
                summary.get("reveal_curve")
                if isinstance(summary, Mapping)
                else None
            )
            if not isinstance(reveal, Mapping):
                features[f"{prefix}.present.reveal"] = 0.0
                continue
            features[f"{prefix}.present.reveal"] = 1.0
            raw_curve = reveal.get("curve")
            curve = (
                [value for value in raw_curve if isinstance(value, Mapping)]
                if isinstance(raw_curve, Sequence)
                and not isinstance(raw_curve, (str, bytes, bytearray))
                else []
            )
            for bin_index in range(5):
                curve_row = curve[bin_index] if bin_index < len(curve) else {}
                for metric in _CURVE_METRICS:
                    value = _distribution_median(curve_row.get(metric))
                    key = f"{prefix}.curve.{bin_index + 1}.{metric}"
                    features[f"{prefix}.present.curve.{bin_index + 1}.{metric}"] = (
                        1.0 if value is not None else 0.0
                    )
                    if value is None:
                        continue
                    scale = (
                        type_scale
                        if metric
                        in {
                            "complete_structure_group_type_count",
                            "dangling_one_type_count",
                            "dangling_two_type_count",
                        }
                        else count_scale
                    )
                    features[key] = value / scale

            raw_matches = reveal.get("first_three_structure_matches")
            matches = raw_matches if isinstance(raw_matches, Mapping) else {}
            for ordinal in ("1", "2", "3"):
                match = matches.get(ordinal)
                progress = (
                    _distribution_median(match.get("progress"))
                    if isinstance(match, Mapping)
                    else None
                )
                features[f"{prefix}.present.match.{ordinal}"] = (
                    1.0 if progress is not None else 0.0
                )
                if progress is not None:
                    features[f"{prefix}.match.{ordinal}.progress"] = progress

            for metric in _DISTRIBUTION_METRICS:
                value = _distribution_median(reveal.get(metric))
                features[f"{prefix}.present.{metric}"] = (
                    1.0 if value is not None else 0.0
                )
                if value is None:
                    continue
                # Areas, durations and released A/B are path-integrated counts.
                # Board-size normalization makes 9/12-block strata comparable
                # without pretending the raw counts are probabilities.
                features[f"{prefix}.{metric}"] = value / board_scale
    return features


def _load_manifest() -> dict[str, object]:
    try:
        payload = json.loads(DYNAMIC_REFERENCE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if payload.get("version") != DYNAMIC_REFERENCE_VERSION:
        return {}
    return payload


def _blend_bands(
    parent: Mapping[str, object],
    child: Mapping[str, object],
    weight: float,
) -> dict[str, object]:
    parent_features = parent.get("features")
    child_features = child.get("features")
    if not isinstance(parent_features, Mapping) or not isinstance(
        child_features, Mapping
    ):
        return dict(parent)
    blended: dict[str, object] = {}
    for key, raw_parent in parent_features.items():
        raw_child = child_features.get(key)
        if not isinstance(raw_parent, Mapping):
            continue
        if not isinstance(raw_child, Mapping):
            blended[key] = dict(raw_parent)
            continue
        row: dict[str, object] = {}
        for field in ("lower", "upper", "median", "robust_scale"):
            parent_value = raw_parent.get(field)
            child_value = raw_child.get(field)
            if isinstance(parent_value, (int, float)) and isinstance(
                child_value, (int, float)
            ):
                row[field] = (1.0 - weight) * float(parent_value) + weight * float(
                    child_value
                )
            elif isinstance(parent_value, (int, float)):
                row[field] = float(parent_value)
        row["sample_size"] = int(raw_child.get("sample_size", 0))
        row["support"] = float(raw_child.get("support", 0.0))
        blended[key] = row
    return {
        **dict(parent),
        "features": blended,
        "sample_size": int(child.get("sample_size", 0)),
    }


def _select_reference(
    structure_count: int, requested_archetype: str
) -> tuple[Mapping[str, object] | None, dict[str, object]]:
    manifest = _load_manifest()
    raw_members = manifest.get("members")
    members = (
        raw_members
        if isinstance(raw_members, Sequence)
        and not isinstance(raw_members, (str, bytes, bytearray))
        else ()
    )
    raw_excluded = manifest.get("excluded_special")
    provenance = {
        "reference_cohort": manifest.get("reference_cohort"),
        "reference_member_count": len(members),
        "excluded_special": (
            dict(raw_excluded) if isinstance(raw_excluded, Mapping) else None
        ),
    }
    raw_groups = manifest.get("groups")
    groups = raw_groups if isinstance(raw_groups, Mapping) else {}
    parent_key = f"{structure_count}/all"
    parent = groups.get(parent_key)
    if not isinstance(parent, Mapping):
        return None, {
            "status": "inconclusive",
            **provenance,
            "requested_group": f"{structure_count}/{requested_archetype}",
            "effective_group": None,
            "reason": "no_exact_size_dynamic_reference",
        }
    if requested_archetype in ("", "auto", "uncertain"):
        return parent, {
            "status": "size_reference",
            **provenance,
            "requested_group": f"{structure_count}/auto",
            "effective_group": parent_key,
            "reason": "auto_uses_independent_size_cohort",
        }
    child_key = f"{structure_count}/{requested_archetype}"
    child = groups.get(child_key)
    if not isinstance(child, Mapping):
        return parent, {
            "status": "size_reference_fallback",
            **provenance,
            "requested_group": child_key,
            "effective_group": parent_key,
            "reason": "archetype_dynamic_reference_unavailable",
        }
    sample_size = int(child.get("sample_size", 0))
    if sample_size < MIN_ARCHETYPE_REFERENCE_SAMPLE:
        weight = sample_size / MIN_ARCHETYPE_REFERENCE_SAMPLE
        return _blend_bands(parent, child, weight), {
            "status": "shrunk_size_archetype_reference",
            **provenance,
            "requested_group": child_key,
            "effective_group": f"{child_key}+{parent_key}",
            "reason": "archetype_sample_below_minimum",
            "shrinkage_weight": round(weight, 6),
        }
    return child, {
        "status": "exact_size_archetype_reference",
        **provenance,
        "requested_group": child_key,
        "effective_group": child_key,
        "reason": None,
        "shrinkage_weight": 1.0,
    }


def compare_dynamic_reference(
    rows: Sequence[Mapping[str, object]],
    *,
    structure_count: int,
    board_size: int,
    requested_archetype: str = "auto",
) -> dict[str, object]:
    """Compare one candidate with a frozen online reveal/release reference."""

    reference, selection = _select_reference(
        structure_count, requested_archetype
    )
    reference_selection_status = selection.get("status", "inconclusive")
    if reference is None:
        return {
            "version": DYNAMIC_REFERENCE_VERSION,
            **selection,
            "reference_selection_status": reference_selection_status,
            "primary_distance": None,
            "stable_tiebreak": None,
            "coverage": 0.0,
            "claim": "descriptive_reference_not_outcome_calibration",
        }
    raw_bands = reference.get("features")
    bands = raw_bands if isinstance(raw_bands, Mapping) else {}
    candidate = extract_dynamic_features(
        rows,
        structure_count=structure_count,
        board_size=board_size,
    )
    family_values: dict[str, list[float]] = defaultdict(list)
    duplicate_values: dict[str, list[float]] = defaultdict(list)
    strategy_values: dict[str, list[float]] = defaultdict(list)
    missing: list[str] = []
    compared = 0
    for key, raw_band in bands.items():
        if not isinstance(raw_band, Mapping):
            continue
        value = candidate.get(str(key))
        if value is None:
            missing.append(str(key))
            continue
        lower = raw_band.get("lower")
        upper = raw_band.get("upper")
        scale = raw_band.get("robust_scale")
        if not all(isinstance(item, (int, float)) for item in (lower, upper, scale)):
            continue
        numeric = float(value)
        if numeric < float(lower):
            raw_distance = (float(lower) - numeric) / max(float(scale), 0.05)
        elif numeric > float(upper):
            raw_distance = (numeric - float(upper)) / max(float(scale), 0.05)
        else:
            raw_distance = 0.0
        distance = min(1.0, raw_distance / 3.0)
        family_values[_family_for_key(str(key))].append(distance)
        parts = str(key).split(".")
        if parts and parts[0].startswith("dup"):
            duplicate_values[parts[0][3:]].append(distance)
        if len(parts) > 1 and parts[1] in _STRATEGIES:
            strategy_values[parts[1]].append(distance)
        compared += 1
    total_reference_features = max(1, len(bands))
    coverage = compared / total_reference_features
    family_distances = {
        name: sum(values) / len(values)
        for name, values in family_values.items()
        if values
    }
    primary = max(family_distances.values(), default=None)
    all_values = [value for values in family_values.values() for value in values]
    stable = sum(all_values) / len(all_values) if all_values else None
    status = "compared" if primary is not None and coverage >= 0.9 else "inconclusive"
    return {
        "version": DYNAMIC_REFERENCE_VERSION,
        **selection,
        "status": status,
        # Keep reference selection and comparison completeness separate.  A
        # small archetype stratum may be correctly shrunk to its size cohort
        # while the candidate evidence itself is still complete (or vice
        # versa); collapsing both into one status made the report impossible
        # to audit.
        "reference_selection_status": reference_selection_status,
        "reference_sample_size": int(reference.get("sample_size", 0)),
        "primary_distance": None if primary is None else round(primary, 6),
        "stable_tiebreak": None if stable is None else round(stable, 6),
        "family_distances": {
            name: round(value, 6)
            for name, value in sorted(family_distances.items())
        },
        "per_duplicate_distance": {
            key: round(sum(values) / len(values), 6)
            for key, values in sorted(duplicate_values.items())
            if values
        },
        "per_strategy_distance": {
            key: round(sum(values) / len(values), 6)
            for key, values in sorted(strategy_values.items())
            if values
        },
        "coverage": round(coverage, 6),
        "compared_feature_count": compared,
        "reference_feature_count": len(bands),
        "missing_feature_count": len(missing),
        "missing_feature_examples": missing[:12],
        "normalization": "structure_types_and_board_size",
        "aggregation": "one_template_vote_per_duppara_strategy_feature",
        "claim": "descriptive_reference_not_outcome_calibration",
    }


__all__ = [
    "DYNAMIC_REFERENCE_VERSION",
    "compare_dynamic_reference",
    "extract_dynamic_features",
]
