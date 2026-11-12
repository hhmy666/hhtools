"""Geometry, blocker graphs, and A/B nesting metrics for level templates."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Collection, Iterable, Mapping, Optional, Tuple

from .template_reader import LevelTemplate, Tile


A_TILE = -10
B_TILE = -11


class GeometryError(ValueError):
    """Raised for invalid tile-index sets or impossible geometry inputs."""


@dataclass(frozen=True, order=True)
class CoverageRelation:
    """A strict-Z projection relationship between two 2x2 tiles."""

    upper_index: int
    lower_index: int
    overlap_cells: int
    z_delta: int

    @property
    def overlap_fraction(self) -> float:
        return self.overlap_cells / 4.0

    @property
    def is_quarter(self) -> bool:
        return self.overlap_cells == 1

    @property
    def is_half(self) -> bool:
        return self.overlap_cells == 2

    @property
    def is_full(self) -> bool:
        return self.overlap_cells == 4


@dataclass(frozen=True)
class CoverageRelationDetail:
    """Self-contained evidence for one projection relationship.

    ``effective_type`` reflects the output candidate (for example, a source A
    position selected for replacement is reported as C).  ``source_tile_id``
    remains present so an auditor can still trace the relation to source bytes.
    """

    upper_index: int
    upper_source_tile_id: int
    upper_effective_type: str
    upper_xyz: Tuple[int, int, int]
    lower_index: int
    lower_source_tile_id: int
    lower_effective_type: str
    lower_xyz: Tuple[int, int, int]
    z_delta: int
    overlap_cells: int
    overlap_fraction: float
    selected_upper: bool
    selected_lower: bool
    category: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class JointFullCoverDetail:
    """A permitted union of two or more partial covers over one lower Tile."""

    lower_index: int
    lower_source_tile_id: int
    lower_effective_type: str
    lower_xyz: Tuple[int, int, int]
    contributor_indices: Tuple[int, ...]
    covered_cells: Tuple[Tuple[int, int], ...]
    contributor_relations: Tuple[CoverageRelationDetail, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CoverageAuditReport:
    """Formal relationship evidence for all newly selected structure Tiles."""

    selected_structure_indices: Tuple[int, ...]
    selected_static_structure_indices: Tuple[int, ...]
    selected_tower_structure_indices: Tuple[int, ...]
    static_relations: Tuple[CoverageRelationDetail, ...]
    adjacent_single_full_relations: Tuple[CoverageRelationDetail, ...]
    cross_layer_single_full_relations: Tuple[CoverageRelationDetail, ...]
    joint_full_covers: Tuple[JointFullCoverDetail, ...]
    tower_initial_full_relations: Tuple[CoverageRelationDetail, ...]

    @property
    def adjacent_single_full_count(self) -> int:
        return len(self.adjacent_single_full_relations)

    @property
    def cross_layer_single_full_count(self) -> int:
        return len(self.cross_layer_single_full_relations)

    @property
    def joint_full_cover_count(self) -> int:
        return len(self.joint_full_covers)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class GeometryGraph:
    """Complete blocker DAG represented from each tile to blockers above it."""

    direct_blockers: Tuple[frozenset[int], ...]
    transitive_blockers: Tuple[frozenset[int], ...]
    direct_blocked_tiles: Tuple[frozenset[int], ...]
    relations: Tuple[CoverageRelation, ...]
    included_indices: frozenset[int]

    def blockers_of(self, tile_index: int, *, transitive: bool = False) -> frozenset[int]:
        table = self.transitive_blockers if transitive else self.direct_blockers
        return table[tile_index]


@dataclass(frozen=True)
class NestingStats:
    """Direct geometry metrics for one hypothetical/static structure unit."""

    structure_indices: frozenset[int]
    nested_a_indices: frozenset[int]
    nested_b_indices: frozenset[int]
    quarter_relations: Tuple[CoverageRelation, ...]
    half_relations: Tuple[CoverageRelation, ...]
    full_relations: Tuple[CoverageRelation, ...]
    blocking_a_indices: frozenset[int]
    blocking_b_indices: frozenset[int]
    reverse_relations: Tuple[CoverageRelation, ...]
    joint_full_lower_indices: frozenset[int]

    @property
    def nested_a_count(self) -> int:
        return len(self.nested_a_indices)

    @property
    def nested_b_count(self) -> int:
        return len(self.nested_b_indices)

    @property
    def quarter_relation_count(self) -> int:
        return len(self.quarter_relations)

    @property
    def half_relation_count(self) -> int:
        return len(self.half_relations)

    @property
    def full_relation_count(self) -> int:
        return len(self.full_relations)

    @property
    def blocking_a_count(self) -> int:
        return len(self.blocking_a_indices)

    @property
    def blocking_b_count(self) -> int:
        return len(self.blocking_b_indices)


def footprint(tile: Tile) -> frozenset[Tuple[int, int]]:
    """Return the four integer grid cells occupied by the standard 2x2 tile."""

    return frozenset(
        (
            (tile.x, tile.y),
            (tile.x + 1, tile.y),
            (tile.x, tile.y + 1),
            (tile.x + 1, tile.y + 1),
        )
    )


def overlap_area(first: Tile, second: Tile) -> int:
    """Return projection overlap in grid cells: necessarily 0, 1, 2, or 4."""

    dx = abs(first.x - second.x)
    dy = abs(first.y - second.y)
    if dx >= 2 or dy >= 2:
        return 0
    return (2 - dx) * (2 - dy)


def overlap_fraction(first: Tile, second: Tile) -> float:
    return overlap_area(first, second) / 4.0


def layer_for_z(z: int) -> int:
    """Translate serialized zero-based Z to the product-facing layer number."""

    if isinstance(z, bool) or not isinstance(z, int):
        raise TypeError("z must be an integer")
    return z + 1


def layer(tile: Tile) -> int:
    return layer_for_z(tile.z)


def _normalize_indices(
    level: LevelTemplate,
    indices: Optional[Iterable[int]],
    *,
    name: str,
) -> frozenset[int]:
    if indices is None:
        return frozenset(range(len(level.tiles)))
    result = set()
    for raw_index in indices:
        if isinstance(raw_index, bool) or not isinstance(raw_index, int):
            raise GeometryError(f"{name} must contain only integer tile indices")
        if raw_index < 0 or raw_index >= len(level.tiles):
            raise GeometryError(
                f"{name} contains out-of-range tile index {raw_index}"
            )
        result.add(raw_index)
    return frozenset(result)


def build_geometry(
    level: LevelTemplate,
    *,
    include_tower: bool = True,
    indices: Optional[Iterable[int]] = None,
) -> GeometryGraph:
    """Build direct and transitive blockers for standard 2x2 footprints.

    Direct blockers are every strictly higher tile with non-zero projection
    overlap, matching the runtime cover test.  The closure retains dependencies
    through intermediate A/B or other tiles.  ``include_tower=False`` creates
    the tower-external static graph required by puzzle solving; tower movement
    must not be presented as a guaranteed static order.
    """

    included = _normalize_indices(level, indices, name="indices")
    if not include_tower:
        included = included.difference(level.tower_member_indices)

    tile_count = len(level.tiles)
    direct = [set() for _ in range(tile_count)]
    blocked = [set() for _ in range(tile_count)]
    relations = []

    # A tile occupies only four cells, so cell buckets avoid scanning every
    # pair in broad/mostly sparse boards.  A set de-duplicates pairs that share
    # two or four cells.
    cell_buckets: dict[Tuple[int, int], list[int]] = {}
    for tile_index in sorted(included):
        for cell in footprint(level.tiles[tile_index]):
            cell_buckets.setdefault(cell, []).append(tile_index)
    candidate_pairs = set()
    for bucket in cell_buckets.values():
        for offset, first_index in enumerate(bucket):
            for second_index in bucket[offset + 1 :]:
                candidate_pairs.add((first_index, second_index))

    for first_index, second_index in sorted(candidate_pairs):
        first = level.tiles[first_index]
        second = level.tiles[second_index]
        if first.z == second.z:
            continue
        area = overlap_area(first, second)
        if area == 0:
            continue
        if first.z > second.z:
            upper_index, lower_index = first_index, second_index
            upper, lower = first, second
        else:
            upper_index, lower_index = second_index, first_index
            upper, lower = second, first
        direct[lower_index].add(upper_index)
        blocked[upper_index].add(lower_index)
        relations.append(
            CoverageRelation(
                upper_index=upper_index,
                lower_index=lower_index,
                overlap_cells=area,
                z_delta=upper.z - lower.z,
            )
        )

    # Every edge points to a strictly greater Z, so descending-Z dynamic
    # programming computes the closure without recursion depth risk.
    transitive_lists = [frozenset() for _ in range(tile_count)]
    for tile_index in sorted(
        included, key=lambda index: (-level.tiles[index].z, index)
    ):
        result = set(direct[tile_index])
        for blocker_index in direct[tile_index]:
            result.update(transitive_lists[blocker_index])
        transitive_lists[tile_index] = frozenset(result)
    transitive = tuple(transitive_lists)
    return GeometryGraph(
        direct_blockers=tuple(frozenset(values) for values in direct),
        transitive_blockers=transitive,
        direct_blocked_tiles=tuple(frozenset(values) for values in blocked),
        relations=tuple(sorted(relations)),
        included_indices=included,
    )


def build_static_geometry(level: LevelTemplate) -> GeometryGraph:
    """Build the guaranteed tower-external blocker graph."""

    return build_geometry(level, include_tower=False)


def coverage_relation(
    level: LevelTemplate, first_index: int, second_index: int
) -> Optional[CoverageRelation]:
    """Return the strict-Z relation between two tiles, or ``None``."""

    selected = _normalize_indices(
        level, (first_index, second_index), name="tile indices"
    )
    if len(selected) != 2:
        return None
    first = level.tiles[first_index]
    second = level.tiles[second_index]
    if first.z == second.z:
        return None
    area = overlap_area(first, second)
    if not area:
        return None
    if first.z > second.z:
        upper_index, lower_index = first_index, second_index
        upper, lower = first, second
    else:
        upper_index, lower_index = second_index, first_index
        upper, lower = second, first
    return CoverageRelation(
        upper_index=upper_index,
        lower_index=lower_index,
        overlap_cells=area,
        z_delta=upper.z - lower.z,
    )


def nesting_stats(
    level: LevelTemplate,
    structure_indices: Iterable[int],
    *,
    tower_external_only: bool = True,
) -> NestingStats:
    """Count direct A/B nesting and reverse A/B blockers for one structure.

    The selected indices may still be A/B in the source template: they are
    interpreted as the positions that will become C--N.  Pure transitive
    dependencies are intentionally not counted as nesting.
    """

    structures = _normalize_indices(
        level, structure_indices, name="structure_indices"
    )
    if tower_external_only and structures.intersection(level.tower_member_indices):
        raise GeometryError(
            "tower-external nesting cannot include an explicit tower member"
        )

    nested_a = set()
    nested_b = set()
    blocking_a = set()
    blocking_b = set()
    quarters = []
    halves = []
    full = []
    reverse = []
    intersections_by_lower: dict[int, set[Tuple[int, int]]] = {}
    contributors_by_lower: dict[int, set[int]] = {}
    single_full_lower_indices: set[int] = set()

    tower_members = level.tower_member_indices
    for structure_index in sorted(structures):
        structure = level.tiles[structure_index]
        for other_index, other in enumerate(level.tiles):
            if other_index in structures or other.tile_id not in (A_TILE, B_TILE):
                continue
            if tower_external_only and other_index in tower_members:
                continue
            relation = coverage_relation(level, structure_index, other_index)
            if relation is None:
                continue

            if relation.upper_index == structure_index:
                if other.tile_id == A_TILE:
                    nested_a.add(other_index)
                else:
                    nested_b.add(other_index)
                if relation.overlap_cells == 1:
                    quarters.append(relation)
                elif relation.overlap_cells == 2:
                    halves.append(relation)
                elif relation.overlap_cells == 4:
                    full.append(relation)
                    single_full_lower_indices.add(other_index)
                intersections_by_lower.setdefault(other_index, set()).update(
                    footprint(structure).intersection(footprint(other))
                )
                contributors_by_lower.setdefault(other_index, set()).add(
                    structure_index
                )
            else:
                if other.tile_id == A_TILE:
                    blocking_a.add(other_index)
                else:
                    blocking_b.add(other_index)
                reverse.append(relation)

    joint_full = frozenset(
        lower_index
        for lower_index, covered_cells in intersections_by_lower.items()
        if len(covered_cells) == 4
        and len(contributors_by_lower.get(lower_index, ())) >= 2
        and lower_index not in single_full_lower_indices
    )
    return NestingStats(
        structure_indices=structures,
        nested_a_indices=frozenset(nested_a),
        nested_b_indices=frozenset(nested_b),
        quarter_relations=tuple(sorted(quarters)),
        half_relations=tuple(sorted(halves)),
        full_relations=tuple(sorted(full)),
        blocking_a_indices=frozenset(blocking_a),
        blocking_b_indices=frozenset(blocking_b),
        reverse_relations=tuple(sorted(reverse)),
        joint_full_lower_indices=joint_full,
    )


def tower_external_full_cover_relations(
    level: LevelTemplate,
    selected_indices: Optional[Collection[int]] = None,
) -> Tuple[CoverageRelation, ...]:
    """Diagnose all-depth single-tile 100% covers outside explicit towers.

    If ``selected_indices`` is supplied, a relation is returned when either end
    is selected.  This legacy diagnostic intentionally includes gaps of two or
    more layers; it is *not* the current product rejection rule.  Production
    acceptance must use :func:`adjacent_full_cover_relations`.
    """

    selected = (
        None
        if selected_indices is None
        else _normalize_indices(level, selected_indices, name="selected_indices")
    )
    if selected is not None and selected.intersection(level.tower_member_indices):
        raise GeometryError(
            "tower-external full-cover check cannot select a tower member"
        )
    graph = build_static_geometry(level)
    result = []
    for relation in graph.relations:
        if not relation.is_full:
            continue
        if selected is None or (
            relation.upper_index in selected or relation.lower_index in selected
        ):
            result.append(relation)
    return tuple(result)


# Legacy all-depth diagnostic alias; not the product rejection rule.
single_tile_full_cover_relations = tower_external_full_cover_relations


def adjacent_full_cover_relations(
    level: LevelTemplate,
    selected_structure_indices: Collection[int],
) -> Tuple[CoverageRelation, ...]:
    """Return adjacent-layer full covers involving a selected structure.

    The confirmed rule is adjacent-layer only but applies in both directions:
    a selected tower-external structure can neither cover the Tile at Z-1 nor
    be covered by the Tile at Z+1 at the same XY.  Same-projection relations
    spanning two or more layers remain diagnostics rather than rejection
    reasons.
    """

    selected = _normalize_indices(
        level,
        selected_structure_indices,
        name="selected_structure_indices",
    )
    if selected.intersection(level.tower_member_indices):
        raise GeometryError(
            "tower-external adjacent-full-cover check cannot select a tower member"
        )
    graph = build_static_geometry(level)
    return tuple(
        relation
        for relation in graph.relations
        if relation.is_full
        and relation.z_delta == 1
        and (
            relation.upper_index in selected
            or relation.lower_index in selected
        )
    )


def _effective_tile_type(
    tile: Tile, type_overrides: Mapping[int, str]
) -> str:
    override = type_overrides.get(tile.index)
    if override is not None:
        return str(override).upper()
    if tile.tile_id == A_TILE:
        return "A"
    if tile.tile_id == B_TILE:
        return "B"
    if -23 <= tile.tile_id <= -12:
        return chr(ord("C") + (-12 - tile.tile_id))
    return f"TileId:{tile.tile_id}"


def _relation_detail(
    level: LevelTemplate,
    relation: CoverageRelation,
    *,
    selected: frozenset[int],
    type_overrides: Mapping[int, str],
    category: str | None = None,
) -> CoverageRelationDetail:
    upper = level.tiles[relation.upper_index]
    lower = level.tiles[relation.lower_index]
    if category is None:
        if relation.is_full:
            category = (
                "adjacent_single_full"
                if relation.z_delta == 1
                else "cross_layer_single_full"
            )
        elif relation.is_half:
            category = "half_cover"
        elif relation.is_quarter:
            category = "quarter_cover"
        else:  # A 2x2 integer-grid footprint can only overlap by 1, 2, or 4.
            category = "partial_cover"
    return CoverageRelationDetail(
        upper_index=upper.index,
        upper_source_tile_id=upper.tile_id,
        upper_effective_type=_effective_tile_type(upper, type_overrides),
        upper_xyz=(upper.x, upper.y, upper.z),
        lower_index=lower.index,
        lower_source_tile_id=lower.tile_id,
        lower_effective_type=_effective_tile_type(lower, type_overrides),
        lower_xyz=(lower.x, lower.y, lower.z),
        z_delta=relation.z_delta,
        overlap_cells=relation.overlap_cells,
        overlap_fraction=relation.overlap_fraction,
        selected_upper=upper.index in selected,
        selected_lower=lower.index in selected,
        category=category,
    )


def audit_structure_coverage(
    level: LevelTemplate,
    selected_type_overrides: Mapping[int, str],
) -> CoverageAuditReport:
    """Build complete, output-aware coverage evidence for formal validation.

    Static relations include every quarter, half, adjacent full, and cross-layer
    full relation for which either endpoint is a newly selected tower-external
    structure.  A joint full cover is reported only when at least two partial
    upper Tiles collectively cover all four cells and no contributor already
    forms a single-Tile full cover.  Joint covers are informational and remain
    valid product geometry.

    Tower members are excluded from static acceptance geometry.  Their initial
    full-cover relations are nevertheless recorded separately because runtime
    tower movement means those relations are diagnostics, not rejection rules.
    """

    selected = _normalize_indices(
        level,
        selected_type_overrides.keys(),
        name="selected_type_overrides",
    )
    static_selected = selected.difference(level.tower_member_indices)
    tower_selected = selected.intersection(level.tower_member_indices)
    static_graph = build_static_geometry(level)

    relevant_relations = tuple(
        relation
        for relation in static_graph.relations
        if relation.upper_index in static_selected
        or relation.lower_index in static_selected
    )
    static_details = tuple(
        _relation_detail(
            level,
            relation,
            selected=selected,
            type_overrides=selected_type_overrides,
        )
        for relation in relevant_relations
    )
    adjacent = tuple(
        detail
        for relation, detail in zip(relevant_relations, static_details)
        if relation.is_full and relation.z_delta == 1
    )
    cross_layer = tuple(
        detail
        for relation, detail in zip(relevant_relations, static_details)
        if relation.is_full and relation.z_delta > 1
    )

    relations_by_lower: dict[int, list[CoverageRelation]] = {}
    for relation in static_graph.relations:
        relations_by_lower.setdefault(relation.lower_index, []).append(relation)
    joint_covers = []
    for lower_index, relations in sorted(relations_by_lower.items()):
        if any(relation.is_full for relation in relations):
            continue
        partial = tuple(relation for relation in relations if not relation.is_full)
        if len(partial) < 2:
            continue
        participating = lower_index in static_selected or any(
            relation.upper_index in static_selected for relation in partial
        )
        if not participating:
            continue
        lower = level.tiles[lower_index]
        covered_cells: set[Tuple[int, int]] = set()
        for relation in partial:
            covered_cells.update(
                footprint(level.tiles[relation.upper_index]).intersection(
                    footprint(lower)
                )
            )
        if covered_cells != set(footprint(lower)):
            continue
        contributors = tuple(
            sorted(partial, key=lambda value: (value.upper_index, value.z_delta))
        )
        joint_covers.append(
            JointFullCoverDetail(
                lower_index=lower.index,
                lower_source_tile_id=lower.tile_id,
                lower_effective_type=_effective_tile_type(
                    lower, selected_type_overrides
                ),
                lower_xyz=(lower.x, lower.y, lower.z),
                contributor_indices=tuple(
                    relation.upper_index for relation in contributors
                ),
                covered_cells=tuple(sorted(covered_cells)),
                contributor_relations=tuple(
                    _relation_detail(
                        level,
                        relation,
                        selected=selected,
                        type_overrides=selected_type_overrides,
                        category="joint_full_contributor",
                    )
                    for relation in contributors
                ),
            )
        )

    complete_graph = build_geometry(level, include_tower=True)
    tower_initial = tuple(
        _relation_detail(
            level,
            relation,
            selected=selected,
            type_overrides=selected_type_overrides,
            category="tower_initial_single_full",
        )
        for relation in complete_graph.relations
        if relation.is_full
        and (
            relation.upper_index in tower_selected
            or relation.lower_index in tower_selected
        )
    )
    return CoverageAuditReport(
        selected_structure_indices=tuple(sorted(selected)),
        selected_static_structure_indices=tuple(sorted(static_selected)),
        selected_tower_structure_indices=tuple(sorted(tower_selected)),
        static_relations=static_details,
        adjacent_single_full_relations=adjacent,
        cross_layer_single_full_relations=cross_layer,
        joint_full_covers=tuple(joint_covers),
        tower_initial_full_relations=tower_initial,
    )


# Migration alias for early callers of the requirements-pass name.
direct_lower_full_cover_relations = adjacent_full_cover_relations


__all__ = [
    "A_TILE",
    "B_TILE",
    "CoverageAuditReport",
    "CoverageRelation",
    "CoverageRelationDetail",
    "GeometryError",
    "GeometryGraph",
    "JointFullCoverDetail",
    "NestingStats",
    "adjacent_full_cover_relations",
    "audit_structure_coverage",
    "build_geometry",
    "build_static_geometry",
    "coverage_relation",
    "direct_lower_full_cover_relations",
    "footprint",
    "layer",
    "layer_for_z",
    "nesting_stats",
    "overlap_area",
    "overlap_fraction",
    "single_tile_full_cover_relations",
    "tower_external_full_cover_relations",
]
