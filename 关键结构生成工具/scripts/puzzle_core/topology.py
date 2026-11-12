"""Shared topology proof for one- and two-unit static puzzle layouts.

The searcher and the independent candidate validator both call this module so
that a search regression cannot silently weaken the formal-export boundary.
The graph points from a lower tile to every blocker above it; product-facing
``U1 -> U2`` therefore means that a U1 tile occurs in U2's blocker set.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Sequence, Tuple

from .geometry import GeometryGraph, nesting_stats, overlap_area
from .models import Layout, SerialDifficultyPolicy, UnitRole
from .template_reader import LevelTemplate


A_TILE = -10
B_TILE = -11


@dataclass(frozen=True)
class TopologyUnit:
    unit_id: str
    role: UnitRole
    target_step: int
    selected_indices: Tuple[int, ...]


@dataclass(frozen=True)
class UnitInfluenceReport:
    unit_id: str
    structure_indices: Tuple[int, ...]
    nested_a_indices: Tuple[int, ...]
    nested_b_indices: Tuple[int, ...]
    blocking_a_indices: Tuple[int, ...]
    blocking_b_indices: Tuple[int, ...]
    influence_indices: Tuple[int, ...]
    initial_entry_indices: Tuple[int, ...]
    quarter_cover_relations: int
    half_cover_relations: int
    full_cover_relations: int

    @property
    def initial_entry_count(self) -> int:
        return len(self.initial_entry_indices)


@dataclass(frozen=True)
class TopologyReport:
    layout: Layout
    passed: bool
    reasons: Tuple[str, ...]
    unit_reports: Tuple[UnitInfluenceReport, ...]
    projection_overlap_pairs: int
    direct_u1_to_u2: int
    direct_u2_to_u1: int
    transitive_u1_to_u2: int
    transitive_u2_to_u1: int
    influence_intersection_count: int
    shared_ab_count: int
    u2_entries_after_u1: Tuple[int, ...]
    u2_new_entries_after_u1: Tuple[int, ...]
    high_min_layer: int | None
    low_max_layer: int | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ExistingStructureUnitConflict:
    """Isolation diagnostics between one new unit and all legacy static C--N."""

    unit_id: str
    projection_overlap_pairs: int
    direct_existing_to_unit: int
    direct_unit_to_existing: int
    transitive_existing_to_unit: int
    transitive_unit_to_existing: int
    influence_intersection_count: int
    shared_ab_count: int

    @property
    def passed(self) -> bool:
        return not any(
            (
                self.projection_overlap_pairs,
                self.direct_existing_to_unit,
                self.direct_unit_to_existing,
                self.transitive_existing_to_unit,
                self.transitive_unit_to_existing,
                self.influence_intersection_count,
                self.shared_ab_count,
            )
        )


@dataclass(frozen=True)
class ExistingStructureIsolationReport:
    """Reusable proof for requirement 10.5.3(10)'s conservative isolation."""

    passed: bool
    existing_structure_indices: Tuple[int, ...]
    existing_influence: UnitInfluenceReport | None
    unit_conflicts: Tuple[ExistingStructureUnitConflict, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _board_geometry(level: LevelTemplate) -> tuple[float, float, float, float]:
    if level.level_width and level.level_height:
        return 0.0, float(level.level_width), 0.0, float(level.level_height)
    if not level.tiles:
        return 0.0, 2.0, 0.0, 2.0
    return (
        float(min(tile.x for tile in level.tiles)),
        float(max(tile.x + 2 for tile in level.tiles)),
        float(min(tile.y for tile in level.tiles)),
        float(max(tile.y + 2 for tile in level.tiles)),
    )


def _role_position_ok(
    level: LevelTemplate, role: UnitRole, indices: Sequence[int]
) -> bool:
    if role in (UnitRole.SINGLE, UnitRole.HIGH, UnitRole.LOW):
        return True
    min_x, max_x, min_y, max_y = _board_geometry(level)
    center_x = (min_x + max_x) / 2
    center_y = (min_y + max_y) / 2
    if role is UnitRole.LEFT:
        return all(level.tiles[index].x + 2 <= center_x for index in indices)
    if role is UnitRole.RIGHT:
        return all(level.tiles[index].x >= center_x for index in indices)
    if role is UnitRole.TOP:
        return all(level.tiles[index].y + 2 <= center_y for index in indices)
    if role is UnitRole.BOTTOM:
        return all(level.tiles[index].y >= center_y for index in indices)
    return False


def analyze_unit_influence(
    level: LevelTemplate,
    graph: GeometryGraph,
    unit: TopologyUnit,
    *,
    all_generated_indices: Sequence[int],
) -> UnitInfluenceReport:
    """Compute A/B ownership while excluding every generated structure member."""

    structures = frozenset(unit.selected_indices)
    generated = frozenset(all_generated_indices)
    raw = nesting_stats(level, structures)
    nested_a = raw.nested_a_indices.difference(generated)
    nested_b = raw.nested_b_indices.difference(generated)
    blocking = {
        blocker
        for index in structures
        for blocker in graph.transitive_blockers[index]
        if blocker not in generated
        and level.tiles[blocker].tile_id in (A_TILE, B_TILE)
    }
    blocking_a = frozenset(
        index for index in blocking if level.tiles[index].tile_id == A_TILE
    )
    blocking_b = frozenset(
        index for index in blocking if level.tiles[index].tile_id == B_TILE
    )
    influence = structures | nested_a | nested_b | blocking_a | blocking_b

    def relation_is_external(relation: object) -> bool:
        lower_index = getattr(relation, "lower_index")
        return lower_index not in generated

    return UnitInfluenceReport(
        unit_id=unit.unit_id,
        structure_indices=tuple(sorted(structures)),
        nested_a_indices=tuple(sorted(nested_a)),
        nested_b_indices=tuple(sorted(nested_b)),
        blocking_a_indices=tuple(sorted(blocking_a)),
        blocking_b_indices=tuple(sorted(blocking_b)),
        influence_indices=tuple(sorted(influence)),
        initial_entry_indices=tuple(
            sorted(index for index in structures if not graph.direct_blockers[index])
        ),
        quarter_cover_relations=sum(
            relation_is_external(relation) for relation in raw.quarter_relations
        ),
        half_cover_relations=sum(
            relation_is_external(relation) for relation in raw.half_relations
        ),
        full_cover_relations=sum(
            relation_is_external(relation) for relation in raw.full_relations
        ),
    )


def _edge_count(
    graph: GeometryGraph,
    blockers: frozenset[int],
    blocked: frozenset[int],
    *,
    transitive: bool,
) -> int:
    table = graph.transitive_blockers if transitive else graph.direct_blockers
    return sum(len(table[index].intersection(blockers)) for index in blocked)


def _ab_indices(report: UnitInfluenceReport) -> frozenset[int]:
    return frozenset(
        report.nested_a_indices
        + report.nested_b_indices
        + report.blocking_a_indices
        + report.blocking_b_indices
    )


def analyze_existing_structure_isolation(
    level: LevelTemplate,
    graph: GeometryGraph,
    units: Sequence[TopologyUnit],
    *,
    existing_structure_indices: Sequence[int],
) -> ExistingStructureIsolationReport:
    """Compare new units with pre-existing, tower-external C--N as one group.

    Type-set isolation is intentionally outside this geometry-only helper: the
    caller owns the planned labels while this proof owns source-template
    topology and A/B influence membership.
    """

    existing = tuple(sorted(set(existing_structure_indices)))
    if not existing:
        return ExistingStructureIsolationReport(True, (), None, ())
    if any(index < 0 or index >= len(level.tiles) for index in existing):
        raise ValueError("existing_structure_indices contains an out-of-range index")

    all_new = tuple(index for unit in units for index in unit.selected_indices)
    existing_unit = TopologyUnit(
        unit_id="__existing_static_structures__",
        role=UnitRole.SINGLE,
        target_step=0,
        selected_indices=existing,
    )
    # New generated positions must not be classified as legacy-owned A/B if a
    # caller supplies a hypothetical source view with mixed identifiers.
    excluded = existing + all_new
    existing_report = analyze_unit_influence(
        level,
        graph,
        existing_unit,
        all_generated_indices=excluded,
    )
    existing_set = frozenset(existing)
    existing_influence = frozenset(existing_report.influence_indices)
    existing_ab = _ab_indices(existing_report)
    conflicts = []
    for unit in units:
        unit_set = frozenset(unit.selected_indices)
        unit_report = analyze_unit_influence(
            level,
            graph,
            unit,
            all_generated_indices=excluded,
        )
        conflicts.append(
            ExistingStructureUnitConflict(
                unit_id=unit.unit_id,
                projection_overlap_pairs=sum(
                    bool(overlap_area(level.tiles[old], level.tiles[new]))
                    for old in existing_set
                    for new in unit_set
                ),
                direct_existing_to_unit=_edge_count(
                    graph, existing_set, unit_set, transitive=False
                ),
                direct_unit_to_existing=_edge_count(
                    graph, unit_set, existing_set, transitive=False
                ),
                transitive_existing_to_unit=_edge_count(
                    graph, existing_set, unit_set, transitive=True
                ),
                transitive_unit_to_existing=_edge_count(
                    graph, unit_set, existing_set, transitive=True
                ),
                influence_intersection_count=len(
                    existing_influence.intersection(unit_report.influence_indices)
                ),
                shared_ab_count=len(existing_ab.intersection(_ab_indices(unit_report))),
            )
        )
    fixed = tuple(conflicts)
    return ExistingStructureIsolationReport(
        passed=all(item.passed for item in fixed),
        existing_structure_indices=existing,
        existing_influence=existing_report,
        unit_conflicts=fixed,
    )


def analyze_topology(
    level: LevelTemplate,
    graph: GeometryGraph,
    layout: Layout,
    units: Sequence[TopologyUnit],
    *,
    serial_difficulty: SerialDifficultyPolicy = SerialDifficultyPolicy.HARDER_LOW,
) -> TopologyReport:
    """Return a complete hard-rule diagnosis for the configured unit topology."""

    fixed = tuple(units)
    reasons: list[str] = []
    expected_count = 1 if layout is Layout.SINGLE else 2
    if len(fixed) != expected_count:
        reasons.append("unit_count_mismatch")
    if not fixed or len(fixed) > 2:
        return TopologyReport(
            layout, False, tuple(reasons or ("invalid_unit_count",)), (), 0, 0, 0,
            0, 0, 0, 0, (), (), None, None
        )

    expected_roles = {
        Layout.SINGLE: (UnitRole.SINGLE,),
        Layout.X: (UnitRole.LEFT, UnitRole.RIGHT),
        Layout.Y: (UnitRole.TOP, UnitRole.BOTTOM),
        Layout.Z: (UnitRole.HIGH, UnitRole.LOW),
    }[layout]
    if tuple(unit.role for unit in fixed) != expected_roles:
        reasons.append("role_order_mismatch")
    if len({unit.unit_id for unit in fixed}) != len(fixed) or any(
        not unit.unit_id for unit in fixed
    ):
        reasons.append("unit_id_invalid")

    all_indices = tuple(index for unit in fixed for index in unit.selected_indices)
    if len(set(all_indices)) != len(all_indices):
        reasons.append("structure_members_overlap")
    if any(index < 0 or index >= len(level.tiles) for index in all_indices):
        reasons.append("structure_index_out_of_range")
        return TopologyReport(
            layout, False, tuple(dict.fromkeys(reasons)), (), 0, 0, 0, 0, 0, 0,
            0, (), (), None, None
        )
    if any(not unit.selected_indices for unit in fixed):
        reasons.append("empty_unit")
    for unit in fixed:
        if not _role_position_ok(level, unit.role, unit.selected_indices):
            reasons.append(f"role_position_mismatch:{unit.unit_id}")

    reports = tuple(
        analyze_unit_influence(
            level, graph, unit, all_generated_indices=all_indices
        )
        for unit in fixed
    )
    if layout is Layout.SINGLE or len(fixed) != 2:
        return TopologyReport(
            layout=layout,
            passed=not reasons,
            reasons=tuple(dict.fromkeys(reasons)),
            unit_reports=reports,
            projection_overlap_pairs=0,
            direct_u1_to_u2=0,
            direct_u2_to_u1=0,
            transitive_u1_to_u2=0,
            transitive_u2_to_u1=0,
            influence_intersection_count=0,
            shared_ab_count=0,
            u2_entries_after_u1=(),
            u2_new_entries_after_u1=(),
            high_min_layer=None,
            low_max_layer=None,
        )

    first_set = frozenset(fixed[0].selected_indices)
    second_set = frozenset(fixed[1].selected_indices)
    projection = sum(
        bool(overlap_area(level.tiles[first], level.tiles[second]))
        for first in first_set
        for second in second_set
    )
    direct_1_to_2 = _edge_count(
        graph, first_set, second_set, transitive=False
    )
    direct_2_to_1 = _edge_count(
        graph, second_set, first_set, transitive=False
    )
    transitive_1_to_2 = _edge_count(
        graph, first_set, second_set, transitive=True
    )
    transitive_2_to_1 = _edge_count(
        graph, second_set, first_set, transitive=True
    )
    influence_1 = frozenset(reports[0].influence_indices)
    influence_2 = frozenset(reports[1].influence_indices)
    influence_intersection = influence_1.intersection(influence_2)
    ab_1 = _ab_indices(reports[0])
    ab_2 = _ab_indices(reports[1])
    shared_ab = ab_1.intersection(ab_2)
    after_u1 = tuple(
        sorted(
            index
            for index in second_set
            if not graph.direct_blockers[index].difference(first_set)
        )
    )
    initial_u2 = frozenset(reports[1].initial_entry_indices)
    new_after_u1 = tuple(index for index in after_u1 if index not in initial_u2)
    high_min_layer = min(
        (level.tiles[index].layer for index in first_set), default=None
    )
    low_max_layer = max(
        (level.tiles[index].layer for index in second_set), default=None
    )

    if layout in (Layout.X, Layout.Y):
        if projection:
            reasons.append("parallel_projection_overlap")
        if direct_1_to_2 or direct_2_to_1:
            reasons.append("parallel_direct_dependency")
        if transitive_1_to_2 or transitive_2_to_1:
            reasons.append("parallel_transitive_dependency")
        if influence_intersection:
            reasons.append("parallel_influence_overlap")
        if not reports[0].initial_entry_count:
            reasons.append("u1_has_no_initial_entry")
        if not reports[1].initial_entry_count:
            reasons.append("u2_has_no_initial_entry")
    else:
        if high_min_layer is None or low_max_layer is None or high_min_layer < low_max_layer:
            reasons.append("serial_layer_bands_interleave")
        if any(
            overlap_area(level.tiles[high], level.tiles[low])
            and level.tiles[high].z <= level.tiles[low].z
            for high in first_set
            for low in second_set
        ):
            reasons.append("serial_overlap_not_strictly_higher")
        if direct_2_to_1 or transitive_2_to_1:
            reasons.append("serial_reverse_dependency")
        if not direct_1_to_2 and not transitive_1_to_2:
            reasons.append("serial_missing_forward_dependency")
        if shared_ab:
            reasons.append("serial_ambiguous_ab_ownership")
        if not reports[0].initial_entry_count:
            reasons.append("u1_has_no_initial_entry")
        if not after_u1:
            reasons.append("u2_has_no_entry_after_u1")
        if not new_after_u1:
            reasons.append("u2_has_no_new_entry_after_u1")
        high_step, low_step = fixed[0].target_step, fixed[1].target_step
        if (
            serial_difficulty is SerialDifficultyPolicy.HARDER_LOW
            and high_step > low_step
        ):
            reasons.append("serial_difficulty_order_mismatch")
        if (
            serial_difficulty is SerialDifficultyPolicy.EASIER_LOW
            and high_step < low_step
        ):
            reasons.append("serial_difficulty_order_mismatch")

    return TopologyReport(
        layout=layout,
        passed=not reasons,
        reasons=tuple(dict.fromkeys(reasons)),
        unit_reports=reports,
        projection_overlap_pairs=projection,
        direct_u1_to_u2=direct_1_to_2,
        direct_u2_to_u1=direct_2_to_1,
        transitive_u1_to_u2=transitive_1_to_2,
        transitive_u2_to_u1=transitive_2_to_1,
        influence_intersection_count=len(influence_intersection),
        shared_ab_count=len(shared_ab),
        u2_entries_after_u1=after_u1,
        u2_new_entries_after_u1=new_after_u1,
        high_min_layer=high_min_layer,
        low_max_layer=low_max_layer,
    )


__all__ = [
    "ExistingStructureIsolationReport",
    "ExistingStructureUnitConflict",
    "TopologyReport",
    "TopologyUnit",
    "UnitInfluenceReport",
    "analyze_existing_structure_isolation",
    "analyze_topology",
    "analyze_unit_influence",
]
