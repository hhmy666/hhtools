"""Versioned, JSON-ready DTOs for the local puzzle-tool interface.

This module is deliberately presentation-only: it never rewrites a template
and it does not re-run candidate search.  It turns the immutable source and
the already validated candidate into self-contained board, playback, and
comparison payloads.

Solver playback has a precise scope.  A unit proof describes the intrinsic
structure graph after external A/B tiles have been cleared.  It must therefore
not be presented as a complete whole-level playthrough.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

from puzzle_core.geometry import GeometryGraph, build_geometry, build_static_geometry
from puzzle_core.search import (
    A_TILE,
    B_TILE,
    STRUCTURE_ID_TO_TYPE,
    Candidate,
    Replacement,
    UnitCandidate,
)
from puzzle_core.solver import CapacityProof, PathSimulation, simulate_order
from puzzle_core.template_reader import LevelTemplate, Tile

from .import_service import LoadedTemplate
from .job_service import GeneratedCandidate


BOARD_DTO_VERSION = 1
TRACE_DTO_VERSION = 1
COMPARISON_DTO_VERSION = 1

TRACE_SCOPE = {
    "code": "structure_intrinsic_external_ab_cleared",
    "label": "结构单元内在求解轨迹",
    "assumption": (
        "该轨迹仅重放谜题单元内的结构块顺序；"
        "它假设位于单元外部、原本遮挡结构块的 A/B Tile 已被清除，"
        "不代表整关从开局到结束的完整点击路径。"
    ),
}


def _tile_type(tile_id: int) -> str:
    if tile_id == A_TILE:
        return "A"
    if tile_id == B_TILE:
        return "B"
    structure_type = STRUCTURE_ID_TO_TYPE.get(tile_id)
    if structure_type is not None:
        return structure_type
    return f"tile:{tile_id}"


def _bounds(level: LevelTemplate) -> dict[str, Any]:
    """Return both anchor and complete 2x2-footprint ranges."""

    if not level.tiles:
        return {
            "empty": True,
            "anchor": None,
            "footprint": None,
            "z": None,
            "layer": None,
        }
    xs = [tile.x for tile in level.tiles]
    ys = [tile.y for tile in level.tiles]
    zs = [tile.z for tile in level.tiles]
    return {
        "empty": False,
        "anchor": {
            "min_x": min(xs),
            "max_x": max(xs),
            "min_y": min(ys),
            "max_y": max(ys),
        },
        "footprint": {
            "min_x": min(xs),
            "max_x_exclusive": max(xs) + 2,
            "min_y": min(ys),
            "max_y_exclusive": max(ys) + 2,
            "width": max(xs) + 2 - min(xs),
            "height": max(ys) + 2 - min(ys),
        },
        "z": {"min": min(zs), "max": max(zs)},
        "layer": {"min": min(zs) + 1, "max": max(zs) + 1},
    }


def _source_metadata(source: LoadedTemplate) -> dict[str, Any]:
    level = source.level
    return {
        "template_id": level.template_id,
        "source_filename": source.source_filename,
        "input_mode": source.input_mode,
        "source_sha256": level.sha256,
        "tile_count": len(level.tiles),
        "max_layer": level.max_layer,
        "level_width": level.level_width,
        "level_height": level.level_height,
    }


def _tower_payload(level: LevelTemplate) -> list[dict[str, Any]]:
    return [
        {
            "tower_index": tower.index,
            "x": tower.x,
            "y": tower.y,
            "direction": tower.direction,
            "member_indices": list(tower.member_indices),
            "member_count": len(tower.member_indices),
        }
        for tower in level.towers
    ]


def _replacement_map(candidate: Candidate | None) -> dict[int, Replacement]:
    if candidate is None:
        return {}
    result: dict[int, Replacement] = {}
    for replacement in candidate.replacements:
        if replacement.tile_index in result:
            raise ValueError(
                f"candidate contains duplicate replacement index {replacement.tile_index}"
            )
        result[replacement.tile_index] = replacement
    return result


def _unit_index_map(candidate: Candidate | None) -> dict[int, str]:
    if candidate is None:
        return {}
    result: dict[int, str] = {}
    for unit in candidate.units:
        for tile_index in unit.selected_indices:
            previous = result.setdefault(tile_index, unit.unit_id)
            if previous != unit.unit_id:
                raise ValueError(
                    f"tile {tile_index} belongs to more than one puzzle unit"
                )
    return result


def _validate_candidate_source(level: LevelTemplate, candidate: Candidate) -> None:
    if candidate.source_template_id != level.template_id:
        raise ValueError("candidate source_template_id does not match loaded template")
    if candidate.source_sha256 != level.sha256:
        raise ValueError("candidate source_sha256 does not match loaded template")
    for replacement in candidate.replacements:
        if replacement.tile_index < 0 or replacement.tile_index >= len(level.tiles):
            raise ValueError("candidate replacement contains an out-of-range tile index")
        source_tile = level.tiles[replacement.tile_index]
        if (
            replacement.source_tile_id != source_tile.tile_id
            or (replacement.x, replacement.y, replacement.z)
            != (source_tile.x, source_tile.y, source_tile.z)
        ):
            raise ValueError("candidate replacement metadata does not match source tile")


def _tile_payload(
    tile: Tile,
    replacement: Replacement | None,
    unit_id: str | None,
    *,
    is_initially_visible: bool,
) -> dict[str, Any]:
    effective_tile_id = (
        tile.tile_id if replacement is None else replacement.structure_tile_id
    )
    return {
        "tile_index": tile.index,
        "x": tile.x,
        "y": tile.y,
        "z": tile.z,
        "layer": tile.layer,
        "source_tile_id": tile.tile_id,
        "source_type": _tile_type(tile.tile_id),
        "effective_tile_id": effective_tile_id,
        "effective_type": _tile_type(effective_tile_id),
        "tower_index": tile.tower_index,
        "is_tower_member": tile.is_tower_member,
        "is_initially_visible": is_initially_visible,
        "debuff_data_index": tile.debuff_data_index,
        "is_replacement": replacement is not None,
        "replacement": None
        if replacement is None
        else {
            "structure_type": replacement.structure_type,
            "group_id": replacement.group_id,
            "unit_id": unit_id,
        },
    }


def _relation_payload(
    level: LevelTemplate,
    graph: GeometryGraph,
    replacements: Mapping[int, Replacement],
    units_by_index: Mapping[int, str],
) -> list[dict[str, Any]]:
    result = []
    for relation in graph.relations:
        upper = level.tiles[relation.upper_index]
        lower = level.tiles[relation.lower_index]
        upper_replacement = replacements.get(relation.upper_index)
        lower_replacement = replacements.get(relation.lower_index)
        upper_effective_id = (
            upper.tile_id
            if upper_replacement is None
            else upper_replacement.structure_tile_id
        )
        lower_effective_id = (
            lower.tile_id
            if lower_replacement is None
            else lower_replacement.structure_tile_id
        )
        result.append(
            {
                "upper_index": relation.upper_index,
                "lower_index": relation.lower_index,
                "z_delta": relation.z_delta,
                "overlap_cells": relation.overlap_cells,
                "overlap_fraction": relation.overlap_fraction,
                "upper_effective_type": _tile_type(upper_effective_id),
                "lower_effective_type": _tile_type(lower_effective_id),
                "upper_unit_id": units_by_index.get(relation.upper_index),
                "lower_unit_id": units_by_index.get(relation.lower_index),
            }
        )
    return result


def _board_payload(
    source: LoadedTemplate,
    candidate: Candidate | None,
) -> dict[str, Any]:
    level = source.level
    if candidate is not None:
        _validate_candidate_source(level, candidate)
    replacements = _replacement_map(candidate)
    units_by_index = _unit_index_map(candidate)
    graph = build_static_geometry(level)
    complete_graph = build_geometry(level)
    kind = "source" if candidate is None else "candidate"
    payload: dict[str, Any] = {
        "schema_name": "tile-board",
        "schema_version": BOARD_DTO_VERSION,
        "kind": kind,
        "board_id": (
            f"source:{level.template_id}:{level.sha256[:12]}"
            if candidate is None
            else f"candidate:{candidate.candidate_id}"
        ),
        "source": _source_metadata(source),
        "bounds": _bounds(level),
        "tiles": [
            _tile_payload(
                tile,
                replacements.get(tile.index),
                units_by_index.get(tile.index),
                is_initially_visible=not complete_graph.direct_blockers[tile.index],
            )
            for tile in level.tiles
        ],
        "towers": _tower_payload(level),
        "static_relations": _relation_payload(
            level, graph, replacements, units_by_index
        ),
        "static_relation_scope": {
            "tower_members_excluded": True,
            "description": (
                "静态遮挡关系仅包含塔外 Tile；"
                "塔内 Tile 位置会实时变化，不声明固定顺序。"
            ),
        },
    }
    if candidate is not None:
        payload["candidate"] = {
            "candidate_id": candidate.candidate_id,
            "candidate_index": candidate.candidate_index,
            "plan_id": candidate.plan_id,
            "plan_signature": candidate.plan_signature,
            "quality_score": candidate.quality_score,
            "quality_metrics": dict(candidate.quality_metrics),
            "quality_summary": list(candidate.quality_summary),
            "risk_warnings": list(candidate.risk_warnings),
            "replacement_count": len(candidate.replacements),
            "unit_count": len(candidate.units),
            "is_tower_only": not candidate.units and candidate.tower_group is not None,
        }
    return payload


def build_source_board(source: LoadedTemplate) -> dict[str, Any]:
    """Build a complete, versioned source-board DTO."""

    return _board_payload(source, None)


def _step_payload(
    simulation: PathSimulation,
    unit: UnitCandidate,
    level: LevelTemplate,
) -> list[dict[str, Any]]:
    result = []
    for step in simulation.steps:
        if step.tile_index < 0 or step.tile_index >= len(unit.selected_indices):
            raise ValueError("proof order contains an out-of-range local tile index")
        global_index = unit.selected_indices[step.tile_index]
        tile = level.tiles[global_index]
        result.append(
            {
                "click_number": step.click_number,
                "unit_id": unit.unit_id,
                "local_tile_index": step.tile_index,
                "tile_index": global_index,
                "tile_type": step.tile_type,
                "coordinate": {
                    "x": tile.x,
                    "y": tile.y,
                    "z": tile.z,
                    "layer": tile.layer,
                    "tower_index": tile.tower_index,
                },
                "bag_before": list(step.bag_before),
                "bag_after_insert": list(step.bag_after_insert),
                "matched_type": step.matched_type,
                "bag_after_settlement": list(step.bag_after_settlement),
                "instantaneous_size": step.instantaneous_size,
                "settled_size": step.settled_size,
                "gate_structure_step": step.gate_structure_step,
                "is_gate_step": step.gate_structure_step is not None,
                "failed": step.failed,
            }
        )
    return result


def _simulation_payload(
    unit: UnitCandidate,
    level: LevelTemplate,
    *,
    capacity: int | None,
    order: Sequence[int] | None,
    proof_source: str,
    capacity_proof: CapacityProof | None = None,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "capacity": capacity,
        "proof_source": proof_source,
        "available": order is not None,
        "classification": (
            None if capacity_proof is None else capacity_proof.classification
        ),
        "complete_exists": (
            None if capacity_proof is None else capacity_proof.complete_exists
        ),
        "failure_reachable": (
            None if capacity_proof is None else capacity_proof.failure_reachable
        ),
        "minimum_failure_click": (
            None if capacity_proof is None else capacity_proof.minimum_failure_click
        ),
    }
    if order is None:
        base.update(
            {
                "order_local": None,
                "order_global": None,
                "complete": False,
                "failed_at": None,
                "gate_reached": False,
                "gate_structure_step": None,
                "literal_first_match_click": None,
                "instantaneous_peak": (
                    None
                    if capacity_proof is None
                    else capacity_proof.instantaneous_peak
                ),
                "settled_peak": (
                    None if capacity_proof is None else capacity_proof.settled_peak
                ),
                "remaining_bag": [],
                "steps": [],
                "unavailable_reason": "no_safe_complete_order",
            }
        )
        return base

    normalized_order = tuple(int(index) for index in order)
    simulation = simulate_order(
        normalized_order,
        unit.proof.tile_types,
        threshold_type=unit.threshold_type,
        capacity=capacity,
    )
    global_order = [unit.selected_indices[index] for index in normalized_order]
    base.update(
        {
            "order_local": list(normalized_order),
            "order_global": global_order,
            "complete": simulation.complete,
            "failed_at": simulation.failed_at,
            "gate_reached": simulation.gate_reached,
            "gate_structure_step": simulation.gate_structure_step,
            "literal_first_match_click": simulation.literal_first_match_click,
            "instantaneous_peak": simulation.instantaneous_peak,
            "settled_peak": simulation.settled_peak,
            "remaining_bag": list(simulation.remaining_bag),
            "steps": _step_payload(simulation, unit, level),
            "unavailable_reason": None,
        }
    )
    return base


def _unit_trace_payload(unit: UnitCandidate, level: LevelTemplate) -> dict[str, Any]:
    if len(unit.selected_indices) != len(unit.proof.tile_types):
        raise ValueError("unit selected_indices and proof tile_types have different sizes")
    if tuple(unit.assigned_types) != tuple(unit.proof.tile_types):
        raise ValueError("unit assigned_types do not match its proof tile_types")
    for global_index in unit.selected_indices:
        if global_index < 0 or global_index >= len(level.tiles):
            raise ValueError("unit contains an out-of-range global tile index")

    capacity_by_value = {
        proof.capacity: proof for proof in unit.proof.capacity_proofs
    }
    capacities = []
    for capacity in (7, 8):
        proof = capacity_by_value.get(capacity)
        capacities.append(
            {
                "capacity": capacity,
                "trace": None
                if proof is None
                else _simulation_payload(
                    unit,
                    level,
                    capacity=capacity,
                    order=proof.recommended_order,
                    proof_source="CapacityProof.recommended_order",
                    capacity_proof=proof,
                ),
                "proof_available": proof is not None,
            }
        )
    return {
        "unit_id": unit.unit_id,
        "role": unit.role,
        "target_step": unit.target_step,
        "threshold_type": unit.threshold_type,
        "required_pre_gate_recycles": unit.proof.required_pre_gate_recycles,
        "first_recycle_lead_tiles": unit.proof.first_recycle_lead_tiles,
        "recommended_pre_gate_recycles": (
            unit.proof.recommended_pre_gate_recycles
        ),
        "recommended_first_match_click": (
            unit.proof.recommended_first_match_click
        ),
        "recommended_gate_click": unit.proof.recommended_gate_click,
        "tile_count": len(unit.selected_indices),
        "local_to_global_indices": list(unit.selected_indices),
        "threshold_trace": _simulation_payload(
            unit,
            level,
            capacity=None,
            order=unit.proof.recommended_order,
            proof_source="PuzzleProof.recommended_order",
        ),
        "capacity_traces": capacities,
    }


def build_solver_traces(
    source: LoadedTemplate,
    generated: GeneratedCandidate,
) -> dict[str, Any]:
    """Build intrinsic per-unit gate and 7/8-capacity playback traces."""

    candidate = generated.candidate
    _validate_candidate_source(source.level, candidate)
    tower_only = not candidate.units and candidate.tower_group is not None
    return {
        "schema_name": "puzzle-solver-traces",
        "schema_version": TRACE_DTO_VERSION,
        "candidate_id": candidate.candidate_id,
        "available": bool(candidate.units),
        "unavailable_reason": (
            "tower_only_has_no_static_solution_order" if tower_only else None
        ),
        "scope": dict(TRACE_SCOPE),
        "units": [
            _unit_trace_payload(unit, source.level) for unit in candidate.units
        ],
    }


def build_candidate_board(
    source: LoadedTemplate,
    generated: GeneratedCandidate,
) -> dict[str, Any]:
    """Build a candidate board plus solver-player payload."""

    result = _board_payload(source, generated.candidate)
    validation = generated.validation
    result["validation_summary"] = {
        "passed": validation.passed,
        "technical_integrity_passed": validation.technical_integrity_passed,
        "manual_export_eligible": validation.manual_export_eligible,
        "auto_recommendation_eligible": (
            validation.auto_recommendation_eligible
        ),
        "replacement_count": validation.replacement_count,
        "replaced_a_count": validation.replaced_a_count,
        "replaced_b_count": validation.replaced_b_count,
        "structure_counts": dict(validation.structure_counts),
        "highest_structure_layer": validation.highest_structure_layer,
        "nested_a_count": validation.nested_a_count,
        "nested_b_count": validation.nested_b_count,
        "check_count": len(validation.checks),
        "failed_check_codes": [
            check.code for check in validation.checks if not check.passed
        ],
    }
    result["solver_traces"] = build_solver_traces(source, generated)
    return result


def _candidate_summary(generated: GeneratedCandidate) -> dict[str, Any]:
    candidate = generated.candidate
    validation = generated.validation
    return {
        "candidate_id": candidate.candidate_id,
        "candidate_index": candidate.candidate_index,
        "plan_id": candidate.plan_id,
        "plan_signature": candidate.plan_signature,
        "validation_passed": validation.passed,
        "technical_integrity_passed": validation.technical_integrity_passed,
        "manual_export_eligible": validation.manual_export_eligible,
        "auto_recommendation_eligible": (
            validation.auto_recommendation_eligible
        ),
        "replacement_count": validation.replacement_count,
        "replaced_a_count": validation.replaced_a_count,
        "replaced_b_count": validation.replaced_b_count,
        "structure_counts": dict(validation.structure_counts),
        "highest_structure_layer": validation.highest_structure_layer,
        "nested_a_count": validation.nested_a_count,
        "nested_b_count": validation.nested_b_count,
        "quality_score": candidate.quality_score,
        "quality_metrics": dict(candidate.quality_metrics),
        "quality_summary": list(candidate.quality_summary),
        "risk_warnings": list(candidate.risk_warnings),
        "units": [
            {
                "unit_id": unit.unit_id,
                "role": unit.role,
                "target_step": unit.target_step,
                "threshold_type": unit.threshold_type,
                "required_pre_gate_recycles": (
                    unit.proof.required_pre_gate_recycles
                ),
                "first_recycle_lead_tiles": (
                    unit.proof.first_recycle_lead_tiles
                ),
                "recommended_gate_click": unit.proof.recommended_gate_click,
                "structure_types": sorted(set(unit.assigned_types)),
                "tile_count": len(unit.selected_indices),
            }
            for unit in candidate.units
        ],
    }


def _replacement_signature(candidate: Candidate) -> dict[int, tuple[str, str]]:
    return {
        replacement.tile_index: (
            replacement.structure_type,
            replacement.group_id,
        )
        for replacement in candidate.replacements
    }


def _counter_delta(
    left: Iterable[str], right: Iterable[str]
) -> dict[str, int]:
    left_counts = Counter(left)
    right_counts = Counter(right)
    return {
        label: right_counts[label] - left_counts[label]
        for label in sorted(set(left_counts) | set(right_counts))
        if right_counts[label] != left_counts[label]
    }


def _pairwise_difference(
    left: GeneratedCandidate,
    right: GeneratedCandidate,
) -> dict[str, Any]:
    left_candidate = left.candidate
    right_candidate = right.candidate
    left_map = _replacement_signature(left_candidate)
    right_map = _replacement_signature(right_candidate)
    left_indices = set(left_map)
    right_indices = set(right_map)
    common = left_indices & right_indices
    only_left = sorted(left_indices - right_indices)
    only_right = sorted(right_indices - left_indices)
    changed_types = sorted(
        index for index in common if left_map[index][0] != right_map[index][0]
    )
    changed_groups = sorted(
        index for index in common if left_map[index][1] != right_map[index][1]
    )
    union_size = len(left_indices | right_indices)
    position_overlap_ratio = 1.0 if not union_size else len(common) / union_size
    quality_delta = right_candidate.quality_score - left_candidate.quality_score
    briefs = []
    position_delta = len(only_left) + len(only_right)
    if position_delta:
        briefs.append(f"替换位置相差 {position_delta} 格")
    else:
        briefs.append("替换位置相同")
    if changed_types:
        briefs.append(f"共同位置中 {len(changed_types)} 格结构类型不同")
    if changed_groups:
        briefs.append(f"{len(changed_groups)} 格的谜题单元归属不同")
    briefs.append(f"质量分变化 {quality_delta:+.3f}")
    return {
        "left_candidate_id": left_candidate.candidate_id,
        "right_candidate_id": right_candidate.candidate_id,
        "same_replacement_positions": left_indices == right_indices,
        "identical_layout": left_map == right_map,
        "common_position_count": len(common),
        "position_overlap_ratio": position_overlap_ratio,
        "only_left_indices": only_left,
        "only_right_indices": only_right,
        "changed_type_indices": changed_types,
        "changed_group_indices": changed_groups,
        "replacement_count_delta": len(right_map) - len(left_map),
        "structure_count_delta": _counter_delta(
            (value[0] for value in left_map.values()),
            (value[0] for value in right_map.values()),
        ),
        "quality_score_delta": quality_delta,
        "brief": briefs,
    }


def build_candidate_comparison(
    source: LoadedTemplate,
    generated_candidates: Sequence[GeneratedCandidate],
) -> dict[str, Any]:
    """Return scan-friendly summaries and all pairwise candidate differences."""

    candidates = tuple(generated_candidates)
    identifiers = [item.candidate.candidate_id for item in candidates]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("candidate comparison requires unique candidate IDs")
    for generated in candidates:
        _validate_candidate_source(source.level, generated.candidate)
    pairwise = []
    for left_index, left in enumerate(candidates):
        for right in candidates[left_index + 1 :]:
            pairwise.append(_pairwise_difference(left, right))
    return {
        "schema_name": "candidate-comparison",
        "schema_version": COMPARISON_DTO_VERSION,
        "template_id": source.level.template_id,
        "source_sha256": source.level.sha256,
        "candidate_count": len(candidates),
        "baseline_candidate_id": None if not candidates else identifiers[0],
        "candidates": [_candidate_summary(item) for item in candidates],
        "pairwise": pairwise,
    }


__all__ = [
    "BOARD_DTO_VERSION",
    "TRACE_DTO_VERSION",
    "COMPARISON_DTO_VERSION",
    "TRACE_SCOPE",
    "build_source_board",
    "build_candidate_board",
    "build_solver_traces",
    "build_candidate_comparison",
]
