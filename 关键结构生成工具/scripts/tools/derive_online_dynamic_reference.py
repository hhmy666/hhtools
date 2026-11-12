#!/usr/bin/env python3
"""Rebuild the frozen 200--300 whole-board reveal reference.

This is an offline audit utility.  It reads the immutable template repository
and the matching config_level rows, gives every online template one vote, and
writes no source/config data.  The generated JSON is reviewed and committed so
the desktop tool never depends on either external path at runtime.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Mapping


SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SCRIPT_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from puzzle_core.difficulty import (  # noqa: E402
    DifficultyProfile,
    DuplicateParameter,
    simulate_candidate_difficulty,
)
from puzzle_core.dynamic_reference import (  # noqa: E402
    DYNAMIC_REFERENCE_VERSION,
    extract_dynamic_features,
)
from puzzle_core.experience import evaluate_static_experience  # noqa: E402
from puzzle_core.template_reader import parse_level_file  # noqa: E402


# User-audited online mapping.  The 36-block special template at level 203 is
# intentionally excluded from the mainstream reference and handled by its own
# all-types-long-range grammar.
COHORT = (
    (202, "newv1model7tl072moretile", "hybrid"),
    (205, "newv1hhmy141moretile", "hybrid"),
    (208, "newv1hhmy047moretile", "hybrid"),
    (210, "newv1hhmy008moretile", "hybrid"),
    (214, "newv1hhmy107moretile", "hybrid"),
    (215, "newv1hhmy054moretile", "hybrid"),
    (218, "newv1hhmy070moretile", "hybrid"),
    (221, "newv1hhmy074moretile", "board_reveal"),
    (222, "newv1hhmy020moretile", "hybrid"),
    (224, "newv1model6tl038moretile", "hybrid"),
    (228, "newv1model37tl027moretile", "hybrid"),
    (229, "newv1model44tl021", "hybrid"),
    (231, "newv1hhmy056moretile", "hybrid"),
    (232, "newv1model22tl012moretile", "hybrid"),
    (233, "newv1hhmy003v2", "hybrid"),
    (234, "newv1hhmy134moretile", "special_size"),
    (235, "newv1model45tl022", "hybrid"),
    (238, "newv1model34tl017", "hybrid"),
    (241, "newv1hhmy063moretile", "hybrid"),
    (242, "newv1model43tl033moretile", "hybrid"),
    (244, "newv1model19tl015moretile", "hybrid"),
    (248, "newv1hhmy038", "hybrid"),
    (249, "newv1model39tl009", "hybrid"),
    (251, "newv1hhmy059moretile", "hybrid"),
    (252, "newv1hhmy039", "hybrid"),
    (255, "newv1hhmy024", "hybrid"),
    (259, "newv1hhmy051", "hybrid"),
    (261, "newv1hhmy078moretile", "special_size"),
    (262, "newv1hhmy037", "hybrid"),
    (263, "newv1hhmy075moretile", "board_reveal"),
    (267, "newv1model37tl024moretile", "hybrid"),
    (271, "newv1hhmy010moretile", "hybrid"),
    (272, "newv1hhmy026", "special_size"),
    (273, "newv1hhmy058moretile", "hybrid"),
    (275, "newv1model14tl016", "hybrid"),
    (277, "newv1model27tl042moretile", "hybrid"),
    (281, "newv1hhmy005v2moretile", "hybrid"),
    (282, "newv1model22tl036", "board_reveal"),
    (283, "newv1hhmy055moretile", "hybrid"),
    (285, "newv1hhmy015", "hybrid"),
    (287, "newv1hhmy016moretile", "hybrid"),
    (291, "newv1model37tl004moretile", "hybrid"),
    (293, "newv1hhmy014moretile", "hybrid"),
    (297, "newv1hhmy053v2", "hybrid"),
)
EXCLUDED_SPECIAL = (203, "newv1hhmy046moretile")

_STATIC_METRIC_KEYS = (
    "structure_count",
    "dependency_component_count",
    "internal_dependency_relation_count",
    "initial_clickable_count",
    "clickable_after_clearing_ab_count",
    "direct_ab_cover_ratio",
    "transitive_ab_cover_ratio",
    "released_distinct_ab_count",
    "released_ab_per_structure",
    "layer_span",
    "touches_layer_1",
    "type_counts",
)
_VISUAL_BALANCE_KEYS = (
    "left_right_count_difference_ratio",
    "top_bottom_count_difference_ratio",
    "quadrants_occupied",
    "maximum_quadrant_share",
    "normalized_centroid_offset",
    "maximum_layer_share",
    "layer_share_stddev",
)


def _quantile(values: list[float], probability: float) -> float:
    if not values:
        raise ValueError("quantile requires at least one value")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _band(values: list[float], group_size: int) -> dict[str, object]:
    med = median(values)
    q25 = _quantile(values, 0.25)
    q75 = _quantile(values, 0.75)
    mad = median([abs(value - med) for value in values])
    return {
        "lower": round(q25, 8),
        "upper": round(q75, 8),
        "median": round(med, 8),
        "robust_scale": round(max((q75 - q25) / 1.349, mad * 1.4826, 0.05), 8),
        "sample_size": len(values),
        "support": round(len(values) / max(group_size, 1), 8),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def derive(template_dir: Path, config_path: Path) -> dict[str, object]:
    with config_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows_by_id: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in rows:
        rows_by_id[str(row.get("LevelId", ""))].append(row)

    members: list[dict[str, object]] = []
    group_features: dict[str, list[dict[str, float]]] = defaultdict(list)
    for level_index, online_id, archetype in COHORT:
        matches = rows_by_id.get(online_id, [])
        unique = {
            (
                str(row.get("TemplateId", "")),
                tuple(str(row.get(f"DupPara{index}", "")) for index in range(1, 11)),
            )
            for row in matches
        }
        if len(unique) != 1:
            raise RuntimeError(
                f"{online_id}: expected one unique config row, got {len(unique)}"
            )
        template_id, raw_parameters = next(iter(unique))
        template_path = template_dir / f"{template_id}_m.bytes"
        if not template_path.is_file():
            raise FileNotFoundError(template_path)
        level = parse_level_file(template_path)
        parameters = tuple(
            DuplicateParameter.parse(value) for value in raw_parameters
        )
        profile = DifficultyProfile(
            duplicate_params=parameters,
            coverage="complete",
            source={
                "mode": "offline_online_cohort_derivation",
                "online_level_index": level_index,
                "online_level_id": online_id,
            },
        )
        report = simulate_candidate_difficulty(
            level, (), target="fidelity", profile=profile
        )
        static = evaluate_static_experience(level)
        raw_static_metrics = static.get("metrics", {})
        raw_visual_balance = static.get("visual_balance", {})
        structure_count = sum(
            -23 <= tile.tile_id <= -12 for tile in level.tiles
        )
        features = extract_dynamic_features(
            report["duplicates"],
            structure_count=structure_count,
            board_size=len(level.tiles),
        )
        member = {
            "level_index": level_index,
            "online_level_id": online_id,
            "template_id": template_id,
            "template_sha256": level.sha256,
            "config_row_sha256": hashlib.sha256(
                "\0".join((online_id, template_id, *raw_parameters)).encode("utf-8")
            ).hexdigest(),
            "structure_count": structure_count,
            "archetype_stratum": archetype,
            "feature_count": len(features),
            # Keep the compact static inputs used to audit style_profiles.py
            # beside the dynamic reference.  Index lists and full graphs are
            # intentionally omitted; exact source/config hashes above are the
            # immutable provenance needed for a full offline rebuild.
            "static_reference": {
                "analysis_version": static.get("version"),
                "geometry_scope": static.get("geometry_scope"),
                "metrics": {
                    key: raw_static_metrics[key]
                    for key in _STATIC_METRIC_KEYS
                    if key in raw_static_metrics
                }
                if isinstance(raw_static_metrics, Mapping)
                else {},
                "visual_balance": {
                    key: raw_visual_balance[key]
                    for key in _VISUAL_BALANCE_KEYS
                    if key in raw_visual_balance
                }
                if isinstance(raw_visual_balance, Mapping)
                else {},
            },
        }
        members.append(member)
        if structure_count in (9, 12):
            group_features[f"{structure_count}/all"].append(features)
            group_features[f"{structure_count}/{archetype}"].append(features)

    groups: dict[str, object] = {}
    for group_name, template_features in sorted(group_features.items()):
        feature_values: dict[str, list[float]] = defaultdict(list)
        all_keys = sorted({key for values in template_features for key in values})
        for key in all_keys:
            for values in template_features:
                if key in values:
                    feature_values[key].append(float(values[key]))
        group_size = len(template_features)
        # Numeric features with little support are not stable references.
        # Presence features are always retained so missing reveal/match stages
        # remain evidence rather than silently disappearing.
        kept = {
            key: _band(values, group_size)
            for key, values in feature_values.items()
            if ".present." in key or len(values) / group_size >= 0.5
        }
        groups[group_name] = {
            "sample_size": group_size,
            "features": kept,
        }

    return {
        "version": DYNAMIC_REFERENCE_VERSION,
        "reference_cohort": "online_levels_200_300_key_structure_v1",
        "excluded_special": {
            "level_index": EXCLUDED_SPECIAL[0],
            "online_level_id": EXCLUDED_SPECIAL[1],
            "reason": "36-block all-types-long-range special grammar",
        },
        "derivation": {
            "template_vote": "one template per feature",
            "within_template": "median distributions retained per DupPara and strategy",
            "strategies": list(("random", "greedy", "local_optimal", "unlock", "adversarial")),
            "duplicate_count": 10,
            "progress_bins": 5,
            "band": "template q25-q75 with robust scale",
            "outcome_calibration": "unavailable",
            "template_dir_sha256_scope": "individual referenced files only",
            "config_file_sha256": _sha256(config_path),
            "static_reference_scope": (
                "compact per-template style inputs; full graph omitted"
            ),
        },
        "members": members,
        "groups": groups,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template-dir", type=Path, required=True)
    parser.add_argument("--config-level", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = derive(args.template_dir, args.config_level)
    args.output.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
