"""Static whole-board experience evidence for key-structure candidates.

This module deliberately owns no generation policy.  It converts the existing
geometry DAG plus a candidate's index/type overrides into auditable experience
features that search, validation, and the UI can consume.  Dynamic path
evidence may be attached when available, but static evidence never pretends to
prove a reveal wave, a serial mechanism, or player-outcome risk.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .geometry import (
    A_TILE,
    B_TILE,
    GeometryGraph,
    build_static_geometry,
)
from .style_profiles import (
    GLOBAL_SOFT_BANDS,
    ONLINE_STYLE_PROFILE_VERSION,
    ONLINE_STYLE_REFERENCE_COHORT,
    SizeStyleProfile,
    profile_for_structure_count,
    select_style_profile,
    soft_band_distance,
)
from .special_presets import (
    SPECIAL_GRAMMAR_VERSION,
    SUSPENDED_EXPOSURE_BOUND_VERSION,
)
from .template_reader import LevelTemplate


EXPERIENCE_ANALYSIS_VERSION = "whole-board-experience-v3"
EXPERIENCE_SIGNATURE_VERSION = "experience-signature-v6"
EXPERIENCE_SIGNATURE_BINNING_VERSION = (
    "level-200-300-five-interval-bins-v1"
)
# Frozen mechanism bins used by the experience fingerprint.  Every entry has
# at most four boundaries, and therefore yields at most five intervals.  The
# boundaries are intentionally independent of the current candidate pool so
# adding or removing sibling candidates cannot change an existing signature.
EXPERIENCE_SIGNATURE_BIN_BOUNDARIES: Mapping[str, tuple[float, ...]] = {
    "count": (0.0, 1.0, 3.0, 6.0),
    "progress": (0.15, 0.35, 0.65, 0.85),
    "release_per_click": (0.0, 1.0, 3.0, 6.0),
    "release_total": (0.0, 3.0, 8.0, 15.0),
    "strength": (0.2, 0.4, 0.6, 0.8),
    "spread": (0.05, 0.15, 0.35, 0.6),
    "cover_ratio": (0.25, 0.5, 0.75, 0.9),
    "transitive_cover_ratio": (0.25, 0.5, 0.75, 0.97),
    "release_density": (0.15, 0.31, 0.94, 1.25),
    "initial_count": (0.0, 1.0, 2.0, 4.0),
    "post_ab_count": (1.0, 3.0, 5.0, 8.0),
}
COMPOSITE_CLUSTER_GRAPH_VERSION = "composite-cluster-graph-v1"
COMPOSITE_CLUSTER_CANONICALIZATION_VERSION = (
    "attributed-directed-refinement-v1"
)
MECHANISM_PRESSURE_SIGNAL_VERSION = "mechanism-pressure-signals-v1"
ARCHETYPE_CLASSIFICATION_MIN_CONFIDENCE = 0.55
ARCHETYPES = ("intrinsic_gate", "board_reveal", "hybrid")
SPECIAL_PRESETS = ("all_types_long_range", "serial_mechanism")
_STRUCTURE_ID_TO_TYPE = {
    -12 - offset: chr(ord("C") + offset) for offset in range(12)
}
_STRUCTURE_TYPES = frozenset(_STRUCTURE_ID_TO_TYPE.values())


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, float(value)))


def _round(value: float, digits: int = 6) -> float:
    return round(float(value), digits)


def _effective_type(
    level: LevelTemplate, tile_index: int, overrides: Mapping[int, str]
) -> str:
    override = overrides.get(tile_index)
    if override is not None:
        return override
    tile_id = level.tiles[tile_index].tile_id
    if tile_id == A_TILE:
        return "A"
    if tile_id == B_TILE:
        return "B"
    return _STRUCTURE_ID_TO_TYPE.get(tile_id, f"TileId:{tile_id}")


def _normalize_assignment(
    level: LevelTemplate,
    chosen_indices: Iterable[int],
    type_assignment: Mapping[int, str] | Sequence[str] | None,
) -> tuple[tuple[int, ...], dict[int, str]]:
    chosen = tuple(chosen_indices)
    if len(set(chosen)) != len(chosen):
        raise ValueError("chosen_indices contains duplicate tile indices")
    for tile_index in chosen:
        if isinstance(tile_index, bool) or not isinstance(tile_index, int):
            raise TypeError("chosen_indices must contain integer tile indices")
        if tile_index < 0 or tile_index >= len(level.tiles):
            raise ValueError(f"chosen_indices contains out-of-range index {tile_index}")

    if type_assignment is None:
        assignment = {}
        for tile_index in chosen:
            structure_type = _STRUCTURE_ID_TO_TYPE.get(
                level.tiles[tile_index].tile_id
            )
            if structure_type is None:
                raise ValueError(
                    "type_assignment is required when a chosen source tile is A/B"
                )
            assignment[tile_index] = structure_type
    elif isinstance(type_assignment, Mapping):
        assignment = {}
        for tile_index, structure_type in type_assignment.items():
            if isinstance(tile_index, bool) or not isinstance(tile_index, int):
                raise TypeError("mapping type_assignment keys must be tile indices")
            if tile_index < 0 or tile_index >= len(level.tiles):
                raise ValueError(
                    f"type_assignment contains out-of-range index {tile_index}"
                )
            assignment[tile_index] = str(structure_type).upper()
        if not chosen:
            chosen = tuple(sorted(assignment))
        if set(assignment) != set(chosen):
            raise ValueError(
                "mapping type_assignment keys must exactly match chosen_indices"
            )
    elif isinstance(type_assignment, Sequence) and not isinstance(
        type_assignment, (str, bytes, bytearray)
    ):
        if len(type_assignment) != len(chosen):
            raise ValueError(
                "sequence type_assignment must align one-to-one with chosen_indices"
            )
        assignment = {
            tile_index: str(structure_type).upper()
            for tile_index, structure_type in zip(chosen, type_assignment)
        }
    else:
        raise TypeError("type_assignment must be a mapping, sequence, or None")

    invalid = sorted(set(assignment.values()) - _STRUCTURE_TYPES)
    if invalid:
        raise ValueError(f"type_assignment contains unsupported types: {invalid}")
    return chosen, assignment


def _connected_components(
    structure_indices: frozenset[int], graph: GeometryGraph
) -> tuple[tuple[int, ...], ...]:
    neighbors: dict[int, set[int]] = {
        tile_index: set() for tile_index in structure_indices
    }
    for relation in graph.relations:
        if (
            relation.upper_index in structure_indices
            and relation.lower_index in structure_indices
        ):
            neighbors[relation.upper_index].add(relation.lower_index)
            neighbors[relation.lower_index].add(relation.upper_index)

    remaining = set(structure_indices)
    components = []
    while remaining:
        root = min(remaining)
        stack = [root]
        component = set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            stack.extend(sorted(neighbors[current] - component, reverse=True))
        remaining.difference_update(component)
        components.append(tuple(sorted(component)))
    return tuple(sorted(components, key=lambda values: (-len(values), values)))


def _axis_footprint_gap(first: int, second: int) -> int:
    """Empty grid cells between two two-cell-wide projected footprints."""

    return max(0, abs(first - second) - 2)


def _components_from_edges(
    structure_indices: frozenset[int], edges: Iterable[tuple[int, int]]
) -> tuple[tuple[int, ...], ...]:
    neighbors = {index: set() for index in structure_indices}
    for first, second in edges:
        neighbors[first].add(second)
        neighbors[second].add(first)
    remaining = set(structure_indices)
    result: list[tuple[int, ...]] = []
    while remaining:
        root = min(remaining)
        stack = [root]
        members = set()
        while stack:
            current = stack.pop()
            if current in members:
                continue
            members.add(current)
            stack.extend(sorted(neighbors[current] - members, reverse=True))
        remaining.difference_update(members)
        result.append(tuple(sorted(members)))
    return tuple(sorted(result, key=lambda values: (min(values), len(values))))


def _strong_components(
    node_count: int, edges: Iterable[tuple[int, int]]
) -> tuple[tuple[int, ...], ...]:
    """Return deterministic SCCs for condensation into a real cluster DAG."""

    neighbors = {index: set() for index in range(node_count)}
    reverse = {index: set() for index in range(node_count)}
    for source, target in edges:
        neighbors[source].add(target)
        reverse[target].add(source)

    seen: set[int] = set()
    order: list[int] = []

    def visit(node: int) -> None:
        seen.add(node)
        for target in sorted(neighbors[node]):
            if target not in seen:
                visit(target)
        order.append(node)

    for node in range(node_count):
        if node not in seen:
            visit(node)

    seen.clear()
    result: list[tuple[int, ...]] = []

    def collect(node: int, members: set[int]) -> None:
        seen.add(node)
        members.add(node)
        for source in sorted(reverse[node]):
            if source not in seen:
                collect(source, members)

    for node in reversed(order):
        if node in seen:
            continue
        members: set[int] = set()
        collect(node, members)
        result.append(tuple(sorted(members)))
    return tuple(sorted(result, key=lambda values: min(values)))


def _composite_cluster_graph(
    level: LevelTemplate,
    graph: GeometryGraph,
    effective_types: Mapping[int, str],
    structure_indices: frozenset[int],
) -> dict[str, object]:
    """Build versioned spatial clusters and their dependency condensation DAG.

    Cluster membership combines direct C--N obstruction, same-plane projection
    adjacency and near-3D adjacency.  Inter-cluster dependencies come from the
    full blocker closure, so an A/B bridge can express ``cluster A -> A/B ->
    cluster B``.  Any cycle introduced by contracting spatially interleaved
    regions is condensed before the public DAG is emitted.
    """

    indices = tuple(sorted(structure_indices))
    membership_edges: list[dict[str, object]] = []
    undirected_edges: set[tuple[int, int]] = set()
    direct_pairs = {
        tuple(sorted((relation.upper_index, relation.lower_index)))
        for relation in graph.relations
        if relation.upper_index in structure_indices
        and relation.lower_index in structure_indices
    }
    for offset, first_index in enumerate(indices):
        first = level.tiles[first_index]
        for second_index in indices[offset + 1 :]:
            second = level.tiles[second_index]
            pair = (first_index, second_index)
            z_gap = abs(first.z - second.z)
            x_gap = _axis_footprint_gap(first.x, second.x)
            y_gap = _axis_footprint_gap(first.y, second.y)
            kinds: list[str] = []
            if pair in direct_pairs:
                kinds.append("direct_obstruction")
            if first.z == second.z and x_gap == 0 and y_gap == 0:
                kinds.append("projection_neighbor")
            if z_gap <= 1 and x_gap <= 1 and y_gap <= 1:
                kinds.append("spatial_neighbor")
            if not kinds:
                continue
            undirected_edges.add(pair)
            membership_edges.append(
                {
                    "first_index": first_index,
                    "second_index": second_index,
                    "kinds": kinds,
                    "projection_gap": [x_gap, y_gap],
                    "layer_gap": z_gap,
                }
            )

    clusters = _components_from_edges(structure_indices, undirected_edges)

    def dependency_edges_for(
        cluster_values: Sequence[Sequence[int]],
    ) -> set[tuple[int, int]]:
        cluster_by_member = {
            member: cluster_index
            for cluster_index, members in enumerate(cluster_values)
            for member in members
        }
        dependencies: set[tuple[int, int]] = set()
        for lower in structure_indices:
            lower_cluster = cluster_by_member[lower]
            for upper in graph.transitive_blockers[lower].intersection(
                structure_indices
            ):
                upper_cluster = cluster_by_member[upper]
                if upper_cluster != lower_cluster:
                    dependencies.add((upper_cluster, lower_cluster))
        return dependencies

    dependency_edges = dependency_edges_for(clusters)
    strong = _strong_components(len(clusters), dependency_edges)
    if any(len(component) > 1 for component in strong):
        clusters = tuple(
            tuple(
                sorted(
                    member
                    for cluster_index in strong_component
                    for member in clusters[cluster_index]
                )
            )
            for strong_component in strong
        )
        clusters = tuple(sorted(clusters, key=lambda values: min(values)))
        dependency_edges = dependency_edges_for(clusters)

    cluster_rows: list[dict[str, object]] = []
    for cluster_index, members in enumerate(clusters, start=1):
        xs = [level.tiles[index].x for index in members]
        ys = [level.tiles[index].y for index in members]
        zs = [level.tiles[index].z for index in members]
        cluster_rows.append(
            {
                "cluster_id": cluster_index,
                "members": list(members),
                "size": len(members),
                "type_multiplicities": sorted(
                    Counter(effective_types[index] for index in members).values()
                ),
                "layers": sorted(set(zs)),
                "projection_bounds": {
                    "min_x": min(xs),
                    "max_x": max(xs) + 1,
                    "min_y": min(ys),
                    "max_y": max(ys) + 1,
                },
            }
        )

    dag_rows = [
        {"from_cluster": source + 1, "to_cluster": target + 1}
        for source, target in sorted(dependency_edges)
    ]
    reachability = {index: set() for index in range(len(clusters))}
    for source, target in dependency_edges:
        reachability[source].add(target)
    for pivot in range(len(clusters)):
        for source in range(len(clusters)):
            if pivot in reachability[source]:
                reachability[source].update(reachability[pivot])

    pair_relations: list[dict[str, object]] = []
    relation_counts = Counter()
    for first_index, first in enumerate(cluster_rows):
        first_bounds = first["projection_bounds"]
        for second_index in range(first_index + 1, len(cluster_rows)):
            second = cluster_rows[second_index]
            second_bounds = second["projection_bounds"]
            if second_index in reachability[first_index]:
                relation = "serial"
                direction = [first_index + 1, second_index + 1]
            elif first_index in reachability[second_index]:
                relation = "serial"
                direction = [second_index + 1, first_index + 1]
            else:
                x_overlap = not (
                    first_bounds["max_x"] < second_bounds["min_x"]
                    or second_bounds["max_x"] < first_bounds["min_x"]
                )
                y_overlap = not (
                    first_bounds["max_y"] < second_bounds["min_y"]
                    or second_bounds["max_y"] < first_bounds["min_y"]
                )
                relation = "overlap" if x_overlap and y_overlap else "parallel"
                direction = None
            relation_counts[relation] += 1
            pair_relations.append(
                {
                    "first_cluster": first_index + 1,
                    "second_cluster": second_index + 1,
                    "relation": relation,
                    "direction": direction,
                }
            )

    return {
        "version": COMPOSITE_CLUSTER_GRAPH_VERSION,
        "definition": {
            "membership": [
                "direct_cn_obstruction",
                "same_layer_projection_adjacency",
                "near_3d_adjacency",
            ],
            "dependency": "transitive_cn_blocker_order_including_ab_bridges",
            "pair_relations": ["parallel", "overlap", "serial"],
        },
        "cluster_count": len(clusters),
        "clusters": cluster_rows,
        "membership_edges": membership_edges,
        "dependency_dag": dag_rows,
        "pair_relations": pair_relations,
        "pair_relation_counts": {
            name: int(relation_counts.get(name, 0))
            for name in ("parallel", "overlap", "serial")
        },
    }


def _visual_balance(
    level: LevelTemplate,
    graph: GeometryGraph,
    structure_indices: frozenset[int],
) -> dict[str, object]:
    if not structure_indices:
        return {
            "status": "not_applicable",
            "left_right_count_difference_ratio": 0.0,
            "top_bottom_count_difference_ratio": 0.0,
            "quadrants_occupied": 0,
            "maximum_quadrant_share": 0.0,
            "normalized_centroid_offset": 0.0,
            "maximum_layer_share": 0.0,
            "layer_share_stddev": 0.0,
        }

    board_indices = graph.included_indices or structure_indices
    board_x = [level.tiles[index].x + 0.5 for index in board_indices]
    board_y = [level.tiles[index].y + 0.5 for index in board_indices]
    min_x, max_x = min(board_x), max(board_x)
    min_y, max_y = min(board_y), max(board_y)
    center_x = (min_x + max_x) / 2.0
    center_y = (min_y + max_y) / 2.0
    half_width = max((max_x - min_x) / 2.0, 1.0)
    half_height = max((max_y - min_y) / 2.0, 1.0)

    structure_x = [level.tiles[index].x + 0.5 for index in structure_indices]
    structure_y = [level.tiles[index].y + 0.5 for index in structure_indices]
    count = len(structure_indices)
    left = sum(value < center_x for value in structure_x)
    right = sum(value > center_x for value in structure_x)
    top = sum(value < center_y for value in structure_y)
    bottom = sum(value > center_y for value in structure_y)
    quadrants = Counter(
        (
            "right" if x >= center_x else "left",
            "bottom" if y >= center_y else "top",
        )
        for x, y in zip(structure_x, structure_y)
    )
    centroid_x = sum(structure_x) / count
    centroid_y = sum(structure_y) / count
    normalized_centroid_offset = math.hypot(
        (centroid_x - center_x) / half_width,
        (centroid_y - center_y) / half_height,
    )
    layer_counts = Counter(level.tiles[index].z for index in structure_indices)
    layer_shares = [value / count for value in layer_counts.values()]
    mean_share = sum(layer_shares) / len(layer_shares)
    share_stddev = math.sqrt(
        sum((value - mean_share) ** 2 for value in layer_shares)
        / len(layer_shares)
    )
    return {
        "status": "available",
        "left_right_count_difference_ratio": _round(abs(left - right) / count),
        "top_bottom_count_difference_ratio": _round(abs(top - bottom) / count),
        "quadrants_occupied": len(quadrants),
        "maximum_quadrant_share": _round(max(quadrants.values()) / count),
        "normalized_centroid_offset": _round(normalized_centroid_offset),
        "maximum_layer_share": _round(max(layer_shares)),
        "layer_share_stddev": _round(share_stddev),
        "reference_frame": "non_tower_tile_bounding_box",
    }


def _dependency_and_coupling_metrics(
    level: LevelTemplate,
    graph: GeometryGraph,
    effective_types: Mapping[int, str],
    structure_indices: frozenset[int],
) -> tuple[dict[str, object], tuple[tuple[int, ...], ...]]:
    ab_indices = frozenset(
        index
        for index in graph.included_indices
        if effective_types[index] in ("A", "B")
    )
    direct_covered = set()
    transitive_covered = set()
    initial_clickable = set()
    clickable_after_clearing_ab = set()
    internal_relations = []
    released_ab = set()
    release_relations = []

    for structure_index in structure_indices:
        direct_blockers = graph.direct_blockers[structure_index]
        transitive_blockers = graph.transitive_blockers[structure_index]
        if direct_blockers.intersection(ab_indices):
            direct_covered.add(structure_index)
        if transitive_blockers.intersection(ab_indices):
            transitive_covered.add(structure_index)
        if not direct_blockers:
            initial_clickable.add(structure_index)
        remaining_after_ab = {
            blocker
            for blocker in direct_blockers
            if effective_types.get(blocker) not in ("A", "B")
        }
        if not remaining_after_ab:
            clickable_after_clearing_ab.add(structure_index)

    for relation in graph.relations:
        upper = relation.upper_index
        lower = relation.lower_index
        if upper in structure_indices and lower in structure_indices:
            internal_relations.append(relation)
        elif upper in structure_indices and lower in ab_indices:
            released_ab.add(lower)
            release_relations.append(relation)

    components = _connected_components(structure_indices, graph)
    bidirectional = direct_covered.intersection(
        relation.upper_index for relation in release_relations
    )
    count = len(structure_indices)
    return (
        {
            "structure_count": count,
            "dependency_component_count": len(components),
            "dependency_component_sizes": [len(values) for values in components],
            "internal_dependency_relation_count": len(internal_relations),
            "initial_clickable_count": len(initial_clickable),
            "clickable_after_clearing_ab_count": len(
                clickable_after_clearing_ab
            ),
            "direct_ab_covered_structure_count": len(direct_covered),
            "direct_ab_cover_ratio": _round(len(direct_covered) / count)
            if count
            else 0.0,
            "transitive_ab_covered_structure_count": len(transitive_covered),
            "transitive_ab_cover_ratio": _round(
                len(transitive_covered) / count
            )
            if count
            else 0.0,
            "released_distinct_ab_count": len(released_ab),
            "structure_to_ab_relation_count": len(release_relations),
            "released_ab_per_structure": _round(len(released_ab) / count)
            if count
            else 0.0,
            "structure_to_ab_relations_per_structure": _round(
                len(release_relations) / count
            )
            if count
            else 0.0,
            "bidirectionally_coupled_structure_ratio": _round(
                len(bidirectional) / count
            )
            if count
            else 0.0,
            "initial_clickable_indices": sorted(initial_clickable),
            "clickable_after_clearing_ab_indices": sorted(
                clickable_after_clearing_ab
            ),
            "released_ab_indices": sorted(released_ab),
            "layer_span": (
                max(level.tiles[index].z for index in structure_indices)
                - min(level.tiles[index].z for index in structure_indices)
            )
            if count
            else 0,
            "touches_layer_1": any(
                level.tiles[index].z == 0 for index in structure_indices
            ),
        },
        components,
    )


def coupling_target_distance(
    metrics: Mapping[str, object], structure_count: int
) -> dict[str, object]:
    """Compute a U-shaped coupling-style distance, never a risk score."""

    profile = profile_for_structure_count(structure_count)
    components: dict[str, float] = {}
    if profile is not None:
        components.update(
            {
                "internal_dependency_relations": soft_band_distance(
                    float(metrics["internal_dependency_relation_count"]),
                    profile.internal_dependency_relations,
                ),
                "direct_ab_cover_ratio": soft_band_distance(
                    float(metrics["direct_ab_cover_ratio"]),
                    profile.direct_ab_cover_ratio,
                ),
                "transitive_ab_cover_ratio": soft_band_distance(
                    float(metrics["transitive_ab_cover_ratio"]),
                    profile.transitive_ab_cover_ratio,
                ),
                "released_distinct_ab": soft_band_distance(
                    float(metrics["released_distinct_ab_count"]),
                    profile.released_distinct_ab,
                ),
            }
        )
    components["released_ab_per_structure"] = soft_band_distance(
        float(metrics["released_ab_per_structure"]),
        GLOBAL_SOFT_BANDS["released_ab_per_structure"],
    )
    distance = sum(components.values()) / len(components)
    return {
        "distance": _round(distance),
        "components": {name: _round(value) for name, value in components.items()},
        "interpretation": "online_style_only_not_player_risk",
        "shape": "target_band_u_distance",
    }


def _online_style_distance(
    metrics: Mapping[str, object],
    type_counts: Counter[str],
    visual_balance: Mapping[str, object],
    archetype: Mapping[str, object],
) -> dict[str, object]:
    count = int(metrics["structure_count"])
    classified = archetype.get("primary")
    selection = select_style_profile(
        count, str(classified) if classified in ARCHETYPES else None
    )
    profile = selection.profile
    components: dict[str, float] = {}
    if profile is not None:
        components = {
            "dependency_components": soft_band_distance(
                float(metrics["dependency_component_count"]),
                profile.dependency_components,
            ),
            "internal_dependency_relations": soft_band_distance(
                float(metrics["internal_dependency_relation_count"]),
                profile.internal_dependency_relations,
            ),
            "initial_clickable": soft_band_distance(
                float(metrics["initial_clickable_count"]),
                profile.initial_clickable,
            ),
            "clickable_after_clearing_ab": soft_band_distance(
                float(metrics["clickable_after_clearing_ab_count"]),
                profile.clickable_after_clearing_ab,
            ),
            "direct_ab_cover_ratio": soft_band_distance(
                float(metrics["direct_ab_cover_ratio"]),
                profile.direct_ab_cover_ratio,
            ),
            "transitive_ab_cover_ratio": soft_band_distance(
                float(metrics["transitive_ab_cover_ratio"]),
                profile.transitive_ab_cover_ratio,
            ),
            "released_distinct_ab": soft_band_distance(
                float(metrics["released_distinct_ab_count"]),
                profile.released_distinct_ab,
            ),
            "type_count": abs(len(type_counts) - profile.type_count)
            / max(profile.type_count, 1),
            "copies_per_type": (
                sum(
                    abs(value - profile.copies_per_type)
                    for value in type_counts.values()
                )
                / max(count, 1)
            ),
            "left_right_balance": soft_band_distance(
                float(visual_balance["left_right_count_difference_ratio"]),
                profile.left_right_difference_ratio,
            ),
            "top_bottom_balance": soft_band_distance(
                float(visual_balance["top_bottom_count_difference_ratio"]),
                profile.top_bottom_difference_ratio,
            ),
            "quadrant_coverage": soft_band_distance(
                float(visual_balance["quadrants_occupied"]),
                profile.quadrants_occupied,
            ),
            "quadrant_concentration": soft_band_distance(
                float(visual_balance["maximum_quadrant_share"]),
                profile.maximum_quadrant_share,
            ),
            "size_normalized_centroid_offset": soft_band_distance(
                float(visual_balance["normalized_centroid_offset"]),
                profile.normalized_centroid_offset,
            ),
            "maximum_layer_share": soft_band_distance(
                float(visual_balance["maximum_layer_share"]),
                profile.maximum_layer_share,
            ),
            "layer_share_variation": soft_band_distance(
                float(visual_balance["layer_share_stddev"]),
                profile.layer_share_stddev,
            ),
        }

    components.update(
        {
            "layer_span": soft_band_distance(
                float(metrics["layer_span"]), GLOBAL_SOFT_BANDS["layer_span"]
            ),
            "released_ab_per_structure": soft_band_distance(
                float(metrics["released_ab_per_structure"]),
                GLOBAL_SOFT_BANDS["released_ab_per_structure"],
            ),
            "layer_1_contact": 0.0 if metrics["touches_layer_1"] else 1.0,
        }
    )
    if profile is None:
        components["normalized_centroid_offset"] = soft_band_distance(
            float(visual_balance["normalized_centroid_offset"]),
            GLOBAL_SOFT_BANDS["normalized_centroid_offset_preferred"],
        )
    distance = sum(components.values()) / len(components) if components else 0.0
    return {
        "status": selection.status,
        "profile_version": ONLINE_STYLE_PROFILE_VERSION,
        "reference_cohort": ONLINE_STYLE_REFERENCE_COHORT,
        "reference_group": selection.effective_group,
        "requested_reference_group": selection.requested_group,
        "sample_size": selection.stratum_sample_size,
        "effective_sample_size": selection.effective_sample_size,
        "reference_confidence": selection.confidence,
        "shrinkage_weight": _round(selection.shrinkage_weight),
        "fallback_reason": selection.fallback_reason,
        "distance": _round(distance),
        "components": {name: _round(value) for name, value in components.items()},
        "calibration": "descriptive_soft_style_not_causal_risk",
    }


def _archetype_classification(
    metrics: Mapping[str, object],
    requested_archetype: str,
    reveals: Mapping[str, object] | None,
    difficulty: Mapping[str, object] | None,
) -> dict[str, object]:
    if requested_archetype not in ("auto", *ARCHETYPES):
        raise ValueError(
            "requested_archetype must be auto, intrinsic_gate, board_reveal, or hybrid"
        )
    count = max(int(metrics["structure_count"]), 1)
    profile = profile_for_structure_count(count)
    intrinsic_reference = (
        (profile.internal_dependency_relations.lower / count)
        if profile is not None
        else 0.65
    )
    intrinsic_strength = _clamp(
        (float(metrics["internal_dependency_relation_count"]) / count)
        / max(intrinsic_reference, 0.25)
    )
    if difficulty is not None and isinstance(
        difficulty.get("intrinsic_gate_step"), (int, float)
    ):
        gate_step = float(difficulty["intrinsic_gate_step"])
        intrinsic_strength = max(intrinsic_strength, _clamp((gate_step - 3) / 3))

    initial = float(metrics["initial_clickable_count"])
    initial_score = 1.0 if initial <= 1 else _clamp(1.0 - (initial - 1) / count)
    direct_score = _clamp(float(metrics["direct_ab_cover_ratio"]) / 0.889)
    transitive_score = _clamp(
        float(metrics["transitive_ab_cover_ratio"]) / 0.967
    )
    release_score = _clamp(float(metrics["released_ab_per_structure"]) / 0.31)
    static_reveal_strength = (
        initial_score + direct_score + transitive_score + release_score
    ) / 4.0
    dynamic_reveal_strength = None
    if reveals is not None and isinstance(reveals.get("reveal_strength"), (int, float)):
        dynamic_reveal_strength = _clamp(float(reveals["reveal_strength"]))
    reveal_strength = (
        static_reveal_strength
        if dynamic_reveal_strength is None
        else 0.4 * static_reveal_strength + 0.6 * dynamic_reveal_strength
    )

    scores = {
        "intrinsic_gate": intrinsic_strength * (1.0 - 0.55 * reveal_strength),
        "board_reveal": reveal_strength * (1.0 - 0.55 * intrinsic_strength),
        "hybrid": math.sqrt(max(0.0, intrinsic_strength * reveal_strength)),
    }
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    automatic = ranked[0][0]
    top = ranked[0][1]
    second = ranked[1][1]
    confidence = _clamp(0.6 * top + 0.4 * max(0.0, top - second))
    classification_status = (
        "classified"
        if confidence >= ARCHETYPE_CLASSIFICATION_MIN_CONFIDENCE
        else "uncertain"
    )
    explicitly_requested = requested_archetype != "auto"
    requested_match_score = scores[
        automatic if requested_archetype == "auto" else requested_archetype
    ]
    requested_matches_automatic = (
        classification_status == "classified"
        and (
            requested_archetype == "auto"
            or requested_archetype == automatic
        )
    )
    baseline = "reference_cohort"
    if difficulty is not None and difficulty.get("archetype_baseline") in {
        "reference_cohort",
        "source_template",
    }:
        baseline = str(difficulty["archetype_baseline"])
    return {
        "requested": requested_archetype,
        # A low-margin/low-strength result is explicitly unclassified.  The
        # diagnostic winner and top two remain available for inspection, but
        # recommendation/UI must not present them as a definite prototype.
        "primary": automatic if classification_status == "classified" else None,
        "automatic_primary": automatic,
        "classification_status": classification_status,
        "top2": [
            {"archetype": name, "score": _round(score)}
            for name, score in ranked[:2]
        ],
        "scores": {name: _round(value) for name, value in scores.items()},
        "match_score": (
            _round(requested_match_score)
            if classification_status == "classified"
            else 0.0
        ),
        "requested_match_score": _round(requested_match_score),
        "requested_matches_automatic": requested_matches_automatic,
        "classification_confidence": _round(confidence),
        "confidence_status": (
            "low" if classification_status == "uncertain" else "usable"
        ),
        "minimum_confidence": ARCHETYPE_CLASSIFICATION_MIN_CONFIDENCE,
        "explicitly_requested": explicitly_requested,
        "intrinsic_strength": _round(intrinsic_strength),
        "reveal_strength": _round(reveal_strength),
        "static_reveal_strength": _round(static_reveal_strength),
        "dynamic_reveal_strength": (
            None
            if dynamic_reveal_strength is None
            else _round(dynamic_reveal_strength)
        ),
        "baseline": baseline,
    }


def _simulation_pressure(
    metrics: Mapping[str, object],
    reveals: Mapping[str, object] | None,
    difficulty: Mapping[str, object] | None,
) -> dict[str, object]:
    signals = []
    count = int(metrics["structure_count"])
    if count:
        initial = int(metrics["initial_clickable_count"])
        after_ab = int(metrics["clickable_after_clearing_ab_count"])
        if initial >= max(3, count // 2):
            signals.append("many_structures_initially_clickable")
        if after_ab <= 1:
            signals.append("few_entries_after_clearing_ab")
    if reveals is not None:
        dangling = reveals.get("dangling_area")
        if isinstance(dangling, (int, float)) and dangling > 0:
            signals.append("dynamic_dangling_exposure_observed")

    failure_rate = None
    path_spread = None
    if difficulty is not None:
        for key in ("worst_failure_rate", "sample_failure_rate", "failure_rate"):
            value = difficulty.get(key)
            if isinstance(value, (int, float)):
                failure_rate = _clamp(float(value))
                break
        value = difficulty.get("path_spread")
        if isinstance(value, (int, float)):
            path_spread = max(0.0, float(value))
    worst_reveal_strength = None
    reveal_path_spread = None
    if reveals is not None:
        value = reveals.get("worst_reveal_strength")
        if isinstance(value, (int, float)):
            worst_reveal_strength = _clamp(float(value))
        value = reveals.get("reveal_path_spread")
        if isinstance(value, (int, float)):
            reveal_path_spread = _clamp(float(value))
    if failure_rate is not None and failure_rate > 0:
        signals.append("sampled_strategy_failures_observed")
    if path_spread is not None and path_spread > 0:
        signals.append("strategy_path_dispersion_observed")
    if reveal_path_spread is not None and reveal_path_spread > 0:
        signals.append("strategy_reveal_dispersion_observed")
    if worst_reveal_strength is not None and worst_reveal_strength > 0.85:
        signals.append("extreme_dynamic_reveal_pressure_observed")
    if (
        failure_rate is None
        and path_spread is None
        and worst_reveal_strength is None
        and reveal_path_spread is None
    ):
        return {
            "status": "inconclusive",
            "level": "inconclusive",
            "signals": signals,
            "signal_version": MECHANISM_PRESSURE_SIGNAL_VERSION,
            "reason": "dynamic_path_evidence_unavailable",
            "calibration": "mechanism_direction_only_not_observed_player_risk",
        }

    extreme_reveal_pressure = (
        0.0
        if worst_reveal_strength is None
        else _clamp((worst_reveal_strength - 0.85) / 0.15)
    )
    numeric_pressure = max(
        failure_rate or 0.0,
        min(path_spread or 0.0, 1.0),
        reveal_path_spread or 0.0,
        extreme_reveal_pressure,
    )
    level = "low" if numeric_pressure <= 0.05 else "medium"
    if numeric_pressure > 0.20:
        level = "high"
    return {
        "status": "directional",
        "level": level,
        "signals": signals,
        "signal_version": MECHANISM_PRESSURE_SIGNAL_VERSION,
        "sample_failure_rate": None
        if failure_rate is None
        else _round(failure_rate),
        "path_spread": None if path_spread is None else _round(path_spread),
        "per_duplicate_path_spread": (
            list(difficulty.get("per_duplicate_path_spread", ()))
            if difficulty is not None
            and isinstance(difficulty.get("per_duplicate_path_spread"), Sequence)
            and not isinstance(
                difficulty.get("per_duplicate_path_spread"),
                (str, bytes, bytearray),
            )
            else []
        ),
        "median_path_spread": (
            _round(float(difficulty["median_path_spread"]))
            if difficulty is not None
            and isinstance(difficulty.get("median_path_spread"), (int, float))
            else None
        ),
        "median_reveal_strength": (
            _round(float(reveals["median_reveal_strength"]))
            if reveals is not None
            and isinstance(reveals.get("median_reveal_strength"), (int, float))
            else None
        ),
        "worst_reveal_strength": (
            None
            if worst_reveal_strength is None
            else _round(worst_reveal_strength)
        ),
        "reveal_path_spread": (
            None if reveal_path_spread is None else _round(reveal_path_spread)
        ),
        "calibration": "mechanism_direction_only_not_observed_player_risk",
    }


def _evidence_for_preset(
    reveals: Mapping[str, object] | None, preset: str
) -> Mapping[str, object]:
    if reveals is None:
        return {}
    nested = reveals.get(preset)
    return nested if isinstance(nested, Mapping) else reveals


def _all_types_long_range_status(
    type_counts: Counter[str],
    metrics: Mapping[str, object],
    reveals: Mapping[str, object] | None,
    requested: bool,
) -> dict[str, object]:
    composition_match = (
        len(type_counts) == 12
        and set(type_counts) == _STRUCTURE_TYPES
        and all(value == 3 for value in type_counts.values())
    )
    long_range = int(metrics["layer_span"]) >= 4
    recognized = composition_match and long_range
    base = {
        "preset": "all_types_long_range",
        "recognized": recognized,
        "recognizer": {
            "twelve_types_times_three": composition_match,
            "long_range_layer_span_at_least_four": long_range,
            "layer_span_definition": "max_z_minus_min_z",
        },
        "proof_kind": "state_transition_wave_dominator_proof",
    }
    if not recognized:
        return {
            **base,
            "status": "rejected" if requested else "not_applicable",
            "reasons": ["static_grammar_not_matched"],
        }

    evidence = _evidence_for_preset(reveals, "all_types_long_range")
    solver_status = evidence.get("solver_status")
    wave_count = evidence.get("wave_count")
    dominance = evidence.get("dominance_chain")
    shortcut_count = evidence.get("shortcut_count")
    capacity_safe = evidence.get("capacity_safe_exits")
    suspended_bound = evidence.get("suspended_exposure_bound")
    bound_status = (
        str(suspended_bound.get("status", "not_proved"))
        if isinstance(suspended_bound, Mapping)
        else "not_proved"
    )
    bound_maximum = (
        suspended_bound.get("maximum_progress_span")
        if isinstance(suspended_bound, Mapping)
        else None
    )
    bound_limit = (
        suspended_bound.get("limit_progress_span")
        if isinstance(suspended_bound, Mapping)
        else None
    )
    bound_within_limit = bool(
        isinstance(bound_maximum, int)
        and not isinstance(bound_maximum, bool)
        and bound_maximum >= 0
        and isinstance(bound_limit, int)
        and not isinstance(bound_limit, bool)
        and bound_limit >= 0
        and bound_maximum <= bound_limit
    )
    bound_metadata_valid = bool(
        isinstance(suspended_bound, Mapping)
        and suspended_bound.get("metric_version")
        == SUSPENDED_EXPOSURE_BOUND_VERSION
        and suspended_bound.get("proof_scope")
        in {
            "all_whole_board_legal_paths",
            "all_whole_board_topological_paths",
        }
        and isinstance(suspended_bound.get("limit_id"), str)
        and bool(str(suspended_bound.get("limit_id", "")).strip())
    )
    entry_event = evidence.get("entry_event")
    completion_event = evidence.get("completion_event")
    schema_valid = bool(
        evidence.get("grammar_version") == SPECIAL_GRAMMAR_VERSION
        and isinstance(evidence.get("wave_assignment"), Mapping)
        and isinstance(evidence.get("wave_dependency_dag"), Sequence)
        and isinstance(entry_event, Mapping)
        and entry_event.get("id") == "wave_first_triplet_completed"
        and isinstance(completion_event, Mapping)
        and completion_event.get("id") == "wave_all_structures_completed"
    )
    if solver_status in ("disproved", "failed") or (
        isinstance(wave_count, int) and wave_count not in (3, 4)
    ) or shortcut_count not in (None, 0) or dominance is False or capacity_safe is False:
        return {
            **base,
            **dict(evidence),
            "status": "rejected",
            "wave_count": wave_count,
            "reasons": ["dynamic_wave_grammar_disproved"],
        }
    if bound_status in ("disproved", "violated") or (
        bound_status == "proved"
        and isinstance(bound_maximum, int)
        and not isinstance(bound_maximum, bool)
        and isinstance(bound_limit, int)
        and not isinstance(bound_limit, bool)
        and bound_maximum > bound_limit
    ):
        return {
            **base,
            **dict(evidence),
            "status": "rejected",
            "wave_count": wave_count,
            "reasons": ["suspended_exposure_bound_violated"],
        }
    if not (
        solver_status == "proved"
        and wave_count in (3, 4)
        and dominance is True
        and shortcut_count == 0
        and capacity_safe is True
        and schema_valid
    ):
        return {
            **base,
            **dict(evidence),
            "status": "inconclusive",
            "wave_count": wave_count,
            "reasons": ["dynamic_wave_proof_missing_or_limited"],
        }
    # Wave dominance and a capacity-safe exit do not bound how long one or
    # two exposed C--N tiles may remain suspended while unrelated A/B tiles
    # are removed.  Sampled strategy traces are useful pressure evidence but
    # cannot prove a universal upper bound.  The special preset therefore
    # fails closed until an auditable whole-board proof supplies both the
    # proven maximum and its versioned acceptance limit.
    if (
        bound_status != "proved"
        or not bound_within_limit
        or not bound_metadata_valid
    ):
        return {
            **base,
            **dict(evidence),
            "status": "inconclusive",
            "wave_count": wave_count,
            "reasons": ["suspended_exposure_bound_not_proved"],
        }
    return {
        **base,
        **dict(evidence),
        "status": "matched",
        "wave_count": wave_count,
        "reasons": [],
    }


def _serial_mechanism_status(
    metrics: Mapping[str, object],
    reveals: Mapping[str, object] | None,
    requested: bool,
) -> dict[str, object]:
    evidence = _evidence_for_preset(reveals, "serial_mechanism")
    cluster_order = evidence.get("cluster_order")
    dynamic_cluster_count = (
        len(cluster_order)
        if isinstance(cluster_order, Sequence)
        and not isinstance(cluster_order, (str, bytes, bytearray))
        else evidence.get("cluster_count")
    )
    cluster_count = (
        int(dynamic_cluster_count)
        if isinstance(dynamic_cluster_count, int)
        else int(metrics["dependency_component_count"])
    )
    recognized = cluster_count >= 2
    base = {
        "preset": "serial_mechanism",
        "recognized": recognized,
        "recognizer": {"cluster_count": cluster_count, "minimum": 2},
        "proof_kind": "state_transition_cluster_dominator_proof",
    }
    if not recognized:
        return {
            **base,
            "status": "rejected" if requested else "not_applicable",
            "reasons": ["fewer_than_two_clusters"],
        }
    solver_status = evidence.get("solver_status")
    dominance = evidence.get("dominance_chain")
    shortcut_count = evidence.get("shortcut_count")
    capacity_safe = evidence.get("capacity_safe_segments")
    reverse_dependency = evidence.get("reverse_dependency")
    entry_event = evidence.get("entry_event")
    completion_event = evidence.get("completion_event")
    schema_valid = bool(
        evidence.get("grammar_version") == SPECIAL_GRAMMAR_VERSION
        and isinstance(evidence.get("cluster_assignment"), Mapping)
        and isinstance(evidence.get("cluster_dependency_dag"), Sequence)
        and isinstance(entry_event, Mapping)
        and entry_event.get("id") == "cluster_first_triplet_completed"
        and isinstance(completion_event, Mapping)
        and completion_event.get("id") == "cluster_all_structures_completed"
    )
    if (
        solver_status in ("disproved", "failed")
        or dominance is False
        or shortcut_count not in (None, 0)
        or capacity_safe is False
        or reverse_dependency is True
    ):
        return {
            **base,
            **dict(evidence),
            "status": "rejected",
            "reasons": ["serial_dominance_or_shortcut_rule_failed"],
        }
    if not (
        solver_status == "proved"
        and dominance is True
        and shortcut_count == 0
        and capacity_safe is True
        and reverse_dependency is False
        and schema_valid
    ):
        return {
            **base,
            **dict(evidence),
            "status": "inconclusive",
            "reasons": ["serial_state_transition_proof_missing_or_limited"],
        }
    return {**base, **dict(evidence), "status": "matched", "reasons": []}


def _bucket(value: float, boundaries: Sequence[float]) -> int:
    return sum(float(value) > boundary for boundary in boundaries)


def _canonical_structure_types(
    level: LevelTemplate,
    graph: GeometryGraph,
    effective_types: Mapping[int, str],
    structure_indices: frozenset[int],
    *,
    min_z: int,
) -> dict[str, str]:
    """Canonicalize C--N labels while retaining their equality partition.

    Candidate diversity is about mechanism and reveal topology, not whether
    an otherwise identical group happened to be named C or J.  Source tile
    indices are not stable identifiers either, so type labels are refined from
    relative layers plus their typed incoming/outgoing dependency roles.
    """

    members: dict[str, list[int]] = defaultdict(list)
    for index in structure_indices:
        members[effective_types[index]].append(index)
    type_names = sorted(members)
    internal_edges: dict[str, list[tuple[int, int, int]]] = {
        name: [] for name in type_names
    }
    incoming: dict[str, list[tuple[str, int, int, int]]] = {
        name: [] for name in type_names
    }
    outgoing: dict[str, list[tuple[str, int, int, int]]] = {
        name: [] for name in type_names
    }
    for relation in graph.relations:
        if (
            relation.upper_index not in structure_indices
            or relation.lower_index not in structure_indices
        ):
            continue
        upper_type = effective_types[relation.upper_index]
        lower_type = effective_types[relation.lower_index]
        edge_shape = (
            level.tiles[relation.upper_index].z - min_z,
            level.tiles[relation.lower_index].z - min_z,
            relation.overlap_cells,
        )
        if upper_type == lower_type:
            internal_edges[upper_type].append(edge_shape)
        else:
            outgoing[upper_type].append((lower_type, *edge_shape))
            incoming[lower_type].append((upper_type, *edge_shape))

    base_attributes = {
        name: {
            "count": len(members[name]),
            "relative_layer_histogram": sorted(
                Counter(
                    level.tiles[index].z - min_z for index in members[name]
                ).items()
            ),
            "internal_edges": sorted(internal_edges[name]),
        }
        for name in type_names
    }

    def stable_digest(value: object) -> str:
        canonical = json.dumps(
            value, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    labels = {
        name: stable_digest(base_attributes[name]) for name in type_names
    }
    for _ in range(len(type_names)):
        labels = {
            name: stable_digest(
                {
                    "attribute": base_attributes[name],
                    "incoming": sorted(
                        (labels[source], upper_z, lower_z, overlap)
                        for source, upper_z, lower_z, overlap in incoming[name]
                    ),
                    "outgoing": sorted(
                        (labels[target], upper_z, lower_z, overlap)
                        for target, upper_z, lower_z, overlap in outgoing[name]
                    ),
                }
            )
            for name in type_names
        }
    return labels


def _distribution_bucket(
    value: object, boundaries: Sequence[float]
) -> int | None:
    if not isinstance(value, Mapping):
        return None
    median = value.get("median")
    if not isinstance(median, (int, float)):
        return None
    return _bucket(float(median), boundaries)


def _compact_reveal_curve(
    reveal: Mapping[str, object],
) -> dict[str, object]:
    """Build the versioned five-bin mechanism fingerprint for one strategy."""

    metric_names = (
        "current_clickable_structure_count",
        "cumulative_exposed_structure_count",
        "complete_structure_group_type_count",
        "dangling_one_type_count",
        "dangling_two_type_count",
    )
    count_boundaries = EXPERIENCE_SIGNATURE_BIN_BOUNDARIES["count"]
    raw_curve = reveal.get("curve")
    curve: list[dict[str, object]] = []
    if isinstance(raw_curve, Sequence) and not isinstance(
        raw_curve, (str, bytes, bytearray)
    ):
        for row in raw_curve:
            if not isinstance(row, Mapping):
                continue
            curve.append(
                {
                    name: _distribution_bucket(row.get(name), count_boundaries)
                    for name in metric_names
                }
            )
    raw_matches = reveal.get("first_three_structure_matches")
    match_progress = []
    for ordinal in ("1", "2", "3"):
        match = (
            raw_matches.get(ordinal)
            if isinstance(raw_matches, Mapping)
            else None
        )
        match_progress.append(
            _distribution_bucket(
                match.get("progress") if isinstance(match, Mapping) else None,
                EXPERIENCE_SIGNATURE_BIN_BOUNDARIES["progress"],
            )
        )
    return {
        "curve": curve,
        "match_progress": match_progress,
        "release_per_click": _distribution_bucket(
            reveal.get("released_ab_per_structure_click"),
            EXPERIENCE_SIGNATURE_BIN_BOUNDARIES["release_per_click"],
        ),
        "release_on_completion": _distribution_bucket(
            reveal.get("released_ab_on_match_completion_click"),
            EXPERIENCE_SIGNATURE_BIN_BOUNDARIES["release_per_click"],
        ),
        "release_total": _distribution_bucket(
            reveal.get("released_ab_total_per_path"),
            EXPERIENCE_SIGNATURE_BIN_BOUNDARIES["release_total"],
        ),
    }


def _dynamic_reveal_signature(
    reveals: Mapping[str, object] | None,
) -> list[dict[str, object]] | None:
    """Compress every duplicate's five-bin reveal and release mechanism.

    This deliberately records the complete progress-bin shape rather than a
    single first-match scalar.  It prevents ten coordinate-near candidates
    with the same reveal/release experience from masquerading as diversity.
    """

    if not isinstance(reveals, Mapping):
        return None
    raw_strategy_duplicates = reveals.get("per_duplicate_strategy_reveal")
    if isinstance(raw_strategy_duplicates, Sequence) and not isinstance(
        raw_strategy_duplicates, (str, bytes, bytearray)
    ):
        equal_weight_result: list[dict[str, object]] = []
        for duplicate in raw_strategy_duplicates:
            if not isinstance(duplicate, Mapping):
                continue
            raw_strategies = duplicate.get("strategies")
            strategies = (
                raw_strategies if isinstance(raw_strategies, Mapping) else {}
            )
            equal_weight_result.append(
                {
                    "duplicate_index": duplicate.get("duplicate_index"),
                    "strategy_reveal_strength": {
                        str(name): _bucket(
                            float(value["reveal_strength"]),
                            EXPERIENCE_SIGNATURE_BIN_BOUNDARIES["strength"],
                        )
                        for name, value in sorted(strategies.items())
                        if isinstance(value, Mapping)
                        and isinstance(value.get("reveal_strength"), (int, float))
                    },
                    "strategy_curve_and_release": {
                        str(name): _compact_reveal_curve(value["reveal_curve"])
                        for name, value in sorted(strategies.items())
                        if isinstance(value, Mapping)
                        and isinstance(value.get("reveal_curve"), Mapping)
                    },
                    "median_reveal_strength": (
                        _bucket(
                            float(duplicate["median_reveal_strength"]),
                            EXPERIENCE_SIGNATURE_BIN_BOUNDARIES["strength"],
                        )
                        if isinstance(
                            duplicate.get("median_reveal_strength"),
                            (int, float),
                        )
                        else None
                    ),
                    "worst_reveal_strength": (
                        _bucket(
                            float(duplicate["worst_reveal_strength"]),
                            EXPERIENCE_SIGNATURE_BIN_BOUNDARIES["strength"],
                        )
                        if isinstance(
                            duplicate.get("worst_reveal_strength"),
                            (int, float),
                        )
                        else None
                    ),
                    "reveal_strength_spread": (
                        _bucket(
                            float(duplicate["reveal_strength_spread"]),
                            EXPERIENCE_SIGNATURE_BIN_BOUNDARIES["spread"],
                        )
                        if isinstance(
                            duplicate.get("reveal_strength_spread"),
                            (int, float),
                        )
                        else None
                    ),
                }
            )
        if equal_weight_result:
            return equal_weight_result
    raw_duplicates = reveals.get("per_duplicate")
    if not isinstance(raw_duplicates, Sequence) or isinstance(
        raw_duplicates, (str, bytes, bytearray)
    ):
        return None
    result: list[dict[str, object]] = []
    for duplicate in raw_duplicates:
        if not isinstance(duplicate, Mapping):
            continue
        result.append(_compact_reveal_curve(duplicate))
    return result or None


def _canonical_composite_cluster_signature(
    composite_clusters: Mapping[str, object],
    *,
    min_z: int,
) -> dict[str, object]:
    """Serialize an attributed cluster DAG without retaining source labels.

    ``cluster_id`` is an implementation detail assigned from source tile
    order.  It must not split otherwise identical experiences.  Each node is
    therefore initialized from its own mechanism attributes and repeatedly
    refined from the multisets of predecessor/successor labels.  The final
    payload contains only stable labels, attributes, and labeled edge
    multiplicities, so permuting cluster rows or renumbering every DAG endpoint
    cannot affect the signature.

    Equal labels are intentionally retained for structurally symmetric nodes.
    Their edges are represented as a multiset, which is both permutation
    invariant and preserves how many dependencies connect each stable class.
    """

    raw_clusters = composite_clusters.get("clusters", ())
    clusters = (
        [item for item in raw_clusters if isinstance(item, Mapping)]
        if isinstance(raw_clusters, Sequence)
        and not isinstance(raw_clusters, (str, bytes, bytearray))
        else []
    )
    source_ids: list[object] = []
    for offset, cluster in enumerate(clusters, 1):
        source_id = cluster.get("cluster_id", offset)
        # A malformed duplicate source label cannot safely address a DAG.  Use
        # a row-local sentinel only for lookup; it never enters the payload.
        if source_id in source_ids:
            source_id = ("row", offset)
        source_ids.append(source_id)
    position_by_source = {
        source_id: position for position, source_id in enumerate(source_ids)
    }

    attributes: list[dict[str, object]] = []
    for cluster in clusters:
        raw_layers = cluster.get("layers", ())
        layers = (
            sorted(
                int(layer) - min_z
                for layer in raw_layers
                if isinstance(layer, (int, float))
            )
            if isinstance(raw_layers, Sequence)
            and not isinstance(raw_layers, (str, bytes, bytearray))
            else []
        )
        raw_multiplicities = cluster.get("type_multiplicities", ())
        multiplicities = (
            sorted(
                int(value)
                for value in raw_multiplicities
                if isinstance(value, (int, float))
            )
            if isinstance(raw_multiplicities, Sequence)
            and not isinstance(raw_multiplicities, (str, bytes, bytearray))
            else []
        )
        attributes.append(
            {
                "size": int(cluster.get("size", sum(multiplicities))),
                "type_multiplicities": multiplicities,
                "relative_layers": layers,
            }
        )

    incoming: list[list[int]] = [[] for _ in clusters]
    outgoing: list[list[int]] = [[] for _ in clusters]
    dependency_edges: list[tuple[int, int]] = []
    raw_dag = composite_clusters.get("dependency_dag", ())
    if isinstance(raw_dag, Sequence) and not isinstance(
        raw_dag, (str, bytes, bytearray)
    ):
        for edge in raw_dag:
            if not isinstance(edge, Mapping):
                continue
            source = position_by_source.get(edge.get("from_cluster"))
            target = position_by_source.get(edge.get("to_cluster"))
            if source is None or target is None or source == target:
                continue
            dependency_edges.append((source, target))
            outgoing[source].append(target)
            incoming[target].append(source)

    def stable_digest(value: object) -> str:
        serialized = json.dumps(
            value, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    labels = [stable_digest(attribute) for attribute in attributes]
    refinement_rounds = len(clusters)
    for _ in range(refinement_rounds):
        labels = [
            stable_digest(
                {
                    "attribute": attributes[position],
                    "incoming": sorted(labels[source] for source in incoming[position]),
                    "outgoing": sorted(labels[target] for target in outgoing[position]),
                }
            )
            for position in range(len(clusters))
        ]

    canonical_nodes = sorted(
        (
            {
                "stable_label": labels[position],
                "attributes": attributes[position],
            }
            for position in range(len(clusters))
        ),
        key=lambda value: json.dumps(
            value, sort_keys=True, separators=(",", ":")
        ),
    )
    canonical_edges = sorted(
        (
            {
                "from_label": labels[source],
                "to_label": labels[target],
            }
            for source, target in dependency_edges
        ),
        key=lambda value: (value["from_label"], value["to_label"]),
    )
    return {
        "version": composite_clusters.get("version"),
        "canonicalization_version": (
            COMPOSITE_CLUSTER_CANONICALIZATION_VERSION
        ),
        "refinement_rounds": refinement_rounds,
        "clusters": canonical_nodes,
        "dependency_edges": canonical_edges,
        "pair_relation_counts": composite_clusters.get(
            "pair_relation_counts", {}
        ),
    }


def _experience_signature(
    level: LevelTemplate,
    graph: GeometryGraph,
    effective_types: Mapping[int, str],
    structure_indices: frozenset[int],
    metrics: Mapping[str, object],
    components: Sequence[Sequence[int]],
    composite_clusters: Mapping[str, object],
    reveals: Mapping[str, object] | None,
) -> tuple[str, dict[str, object]]:
    if structure_indices:
        min_z = min(level.tiles[index].z for index in structure_indices)
    else:
        min_z = 0
    layer_histogram = Counter(
        level.tiles[index].z - min_z for index in structure_indices
    )
    canonical_types = _canonical_structure_types(
        level,
        graph,
        effective_types,
        structure_indices,
        min_z=min_z,
    )
    component_payloads = []
    for component in components:
        member_set = frozenset(component)
        edge_count = sum(
            relation.upper_index in member_set and relation.lower_index in member_set
            for relation in graph.relations
        )
        component_payloads.append(
            {
                "size": len(component),
                "edges": edge_count,
                "types": sorted(
                    Counter(effective_types[index] for index in component).values()
                ),
                "layer_histogram": sorted(
                    Counter(
                        level.tiles[index].z - min_z for index in component
                    ).items()
                ),
            }
        )
    component_payloads.sort(
        key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"))
    )
    edge_layers = sorted(
        (
            level.tiles[relation.upper_index].z - min_z,
            level.tiles[relation.lower_index].z - min_z,
            canonical_types[effective_types[relation.upper_index]],
            canonical_types[effective_types[relation.lower_index]],
            (
                effective_types[relation.upper_index]
                == effective_types[relation.lower_index]
            ),
            relation.overlap_cells,
        )
        for relation in graph.relations
        if relation.upper_index in structure_indices
        and relation.lower_index in structure_indices
    )
    dynamic_bucket = None
    if reveals is not None:
        dynamic_bucket = {
            key: _bucket(
                float(reveals[key]),
                EXPERIENCE_SIGNATURE_BIN_BOUNDARIES["progress"],
            )
            for key in (
                "first_match_progress",
                "dangling_area",
                "path_spread",
                "median_reveal_strength",
                "worst_reveal_strength",
                "reveal_path_spread",
            )
            if isinstance(reveals.get(key), (int, float))
        }
    payload = {
        "version": EXPERIENCE_SIGNATURE_VERSION,
        "binning_version": EXPERIENCE_SIGNATURE_BINNING_VERSION,
        "binning_manifest": {
            name: list(boundaries)
            for name, boundaries in sorted(
                EXPERIENCE_SIGNATURE_BIN_BOUNDARIES.items()
            )
        },
        "structure_count": len(structure_indices),
        "type_multiplicities": sorted(
            Counter(effective_types[index] for index in structure_indices).values()
        ),
        "relative_layer_histogram": sorted(layer_histogram.items()),
        "components": component_payloads,
        "composite_cluster_graph": _canonical_composite_cluster_signature(
            composite_clusters,
            min_z=min_z,
        ),
        "internal_dependency_edge_layers": edge_layers,
        "coupling_buckets": {
            "direct_cover": _bucket(
                float(metrics["direct_ab_cover_ratio"]),
                EXPERIENCE_SIGNATURE_BIN_BOUNDARIES["cover_ratio"],
            ),
            "transitive_cover": _bucket(
                float(metrics["transitive_ab_cover_ratio"]),
                EXPERIENCE_SIGNATURE_BIN_BOUNDARIES[
                    "transitive_cover_ratio"
                ],
            ),
            "release_density": _bucket(
                float(metrics["released_ab_per_structure"]),
                EXPERIENCE_SIGNATURE_BIN_BOUNDARIES["release_density"],
            ),
        },
        "reveal_buckets": {
            "initial": _bucket(
                float(metrics["initial_clickable_count"]),
                EXPERIENCE_SIGNATURE_BIN_BOUNDARIES["initial_count"],
            ),
            "after_clearing_ab": _bucket(
                float(metrics["clickable_after_clearing_ab_count"]),
                EXPERIENCE_SIGNATURE_BIN_BOUNDARIES["post_ab_count"],
            ),
            "dynamic": dynamic_bucket,
            "per_duplicate_path_spread": [
                _bucket(
                    float(item["spread"]),
                    EXPERIENCE_SIGNATURE_BIN_BOUNDARIES["spread"],
                )
                for item in reveals.get("per_duplicate_path_spread", ())
                if isinstance(item, Mapping)
                and isinstance(item.get("spread"), (int, float))
            ]
            if isinstance(reveals, Mapping)
            else [],
            "full_curve_and_release": _dynamic_reveal_signature(reveals),
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{EXPERIENCE_SIGNATURE_VERSION}:{digest}", payload


def evaluate_static_experience(
    level: LevelTemplate,
    chosen_indices: Iterable[int] = (),
    type_assignment: Mapping[int, str] | Sequence[str] | None = None,
    *,
    requested_archetype: str = "auto",
    reveals: Mapping[str, object] | None = None,
    difficulty: Mapping[str, object] | None = None,
    requested_special_preset: str | None = None,
) -> dict[str, object]:
    """Evaluate static whole-board experience features for one candidate.

    ``chosen_indices`` are source positions that become C--N.  A mapping
    assignment is keyed by tile index; a sequence assignment aligns with the
    given index order.  Existing C--N tiles are included automatically.  The
    analysis uses the guaranteed tower-external geometry graph, reporting any
    excluded chosen tower members in evidence coverage.
    """

    if requested_special_preset not in (None, "none", *SPECIAL_PRESETS):
        raise ValueError(
            "requested_special_preset must be none, all_types_long_range, or serial_mechanism"
        )
    chosen, overrides = _normalize_assignment(
        level, chosen_indices, type_assignment
    )
    graph = build_static_geometry(level)
    effective_types = {
        index: _effective_type(level, index, overrides)
        for index in range(len(level.tiles))
    }
    structure_indices = frozenset(
        index
        for index in graph.included_indices
        if effective_types[index] in _STRUCTURE_TYPES
    )
    metrics, components = _dependency_and_coupling_metrics(
        level, graph, effective_types, structure_indices
    )
    layer_histogram = Counter(
        level.tiles[index].z for index in structure_indices
    )
    metrics["layer_histogram"] = {
        str(layer + 1): layer_histogram[layer] for layer in sorted(layer_histogram)
    }
    type_counts = Counter(effective_types[index] for index in structure_indices)
    metrics["type_counts"] = dict(sorted(type_counts.items()))
    composite_clusters = _composite_cluster_graph(
        level, graph, effective_types, structure_indices
    )
    metrics["composite_cluster_count"] = int(
        composite_clusters["cluster_count"]
    )
    metrics["composite_cluster_relation_counts"] = dict(
        composite_clusters["pair_relation_counts"]
    )
    balance = _visual_balance(level, graph, structure_indices)
    coupling = coupling_target_distance(metrics, len(structure_indices))
    archetype = _archetype_classification(
        metrics, requested_archetype, reveals, difficulty
    )
    style = _online_style_distance(metrics, type_counts, balance, archetype)
    pressure = _simulation_pressure(metrics, reveals, difficulty)
    requested = requested_special_preset not in (None, "none")
    special_presets = {
        "all_types_long_range": _all_types_long_range_status(
            type_counts,
            metrics,
            reveals,
            requested and requested_special_preset == "all_types_long_range",
        ),
        "serial_mechanism": _serial_mechanism_status(
            metrics,
            reveals,
            requested and requested_special_preset == "serial_mechanism",
        ),
    }
    signature, signature_evidence = _experience_signature(
        level,
        graph,
        effective_types,
        structure_indices,
        metrics,
        components,
        composite_clusters,
        reveals,
    )
    ignored_tower_chosen = sorted(
        set(chosen).intersection(level.tower_member_indices)
    )
    return {
        "version": EXPERIENCE_ANALYSIS_VERSION,
        "source_template_id": level.template_id,
        "source_sha256": level.sha256,
        "geometry_scope": "tower_external_static",
        "metrics": metrics,
        "dependency_clusters": {
            "definition": "undirected_components_of_direct_cn_geometry_dependencies",
            "count": len(components),
            "sizes": [len(component) for component in components],
            "members": [list(component) for component in components],
        },
        "composite_cluster_graph": composite_clusters,
        "coupling_target_distance": coupling,
        "visual_balance": balance,
        "archetype": archetype,
        "online_style_distance": style,
        "simulation_pressure": pressure,
        "observed_risk": {
            "status": "unavailable",
            "reason": "no_versioned_outcome_calibration_model",
        },
        "special_presets": special_presets,
        "experience_signature": signature,
        "experience_signature_evidence": signature_evidence,
        "evidence_coverage": {
            "static_geometry": "complete" if not ignored_tower_chosen else "partial",
            "tower_members_excluded": ignored_tower_chosen,
            "dynamic_reveal": "provided" if reveals is not None else "unavailable",
            "difficulty_paths": "provided"
            if difficulty is not None
            else "unavailable",
            "risk_calibration": "unavailable",
        },
    }


__all__ = [
    "ARCHETYPES",
    "ARCHETYPE_CLASSIFICATION_MIN_CONFIDENCE",
    "COMPOSITE_CLUSTER_CANONICALIZATION_VERSION",
    "COMPOSITE_CLUSTER_GRAPH_VERSION",
    "EXPERIENCE_ANALYSIS_VERSION",
    "EXPERIENCE_SIGNATURE_VERSION",
    "EXPERIENCE_SIGNATURE_BINNING_VERSION",
    "EXPERIENCE_SIGNATURE_BIN_BOUNDARIES",
    "SPECIAL_PRESETS",
    "coupling_target_distance",
    "evaluate_static_experience",
]
