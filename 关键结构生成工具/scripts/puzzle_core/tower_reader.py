"""Tower diagnostics and tower-only structure allocation.

Tower membership is deliberately derived from explicit ``StackData`` anchors.
Repeated XY coordinates alone never create a tower.  When duplicate anchors
exist, the last serialized ``StackData`` record owns the matching tiles, which
mirrors ``LevelPlayData``'s runtime array assignment.

This module does not run the static puzzle solver.  A tower-only allocation
therefore reports structure steps and solution counts as not applicable.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Tuple

from .errors import InputValidationError
from .template_reader import LevelTemplate, Tile


A_TILE_ID = -10
B_TILE_ID = -11
STRUCTURE_TYPES: Tuple[str, ...] = tuple("CDEFGHIJKLMN")
STRUCTURE_TILE_IDS: Tuple[int, ...] = tuple(
    -12 - offset for offset in range(len(STRUCTURE_TYPES))
)
STRUCTURE_TILE_ID_BY_TYPE = dict(zip(STRUCTURE_TYPES, STRUCTURE_TILE_IDS))
STRUCTURE_TYPE_BY_TILE_ID = dict(zip(STRUCTURE_TILE_IDS, STRUCTURE_TYPES))


class TowerAllocationError(InputValidationError):
    """A stable validation failure raised before any template is rewritten."""

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        evidence: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.reason = reason
        super().__init__(
            message,
            json_path="tower_only",
            evidence={"reason": reason, **dict(evidence or {})},
        )


@dataclass(frozen=True)
class TowerWarning:
    """One machine-readable tower anomaly found during template inspection."""

    code: str
    message: str
    tower_indices: Tuple[int, ...] = ()
    tile_indices: Tuple[int, ...] = ()
    anchor: Optional[Tuple[int, int]] = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "tower_indices": list(self.tower_indices),
            "tile_indices": list(self.tile_indices),
        }
        if self.anchor is not None:
            result["anchor"] = list(self.anchor)
        return result


@dataclass(frozen=True)
class TowerMember:
    """The initial serialized state of one actual tower member."""

    tile_index: int
    x: int
    y: int
    z: int
    tile_id: int
    tile_type: Optional[str]
    debuff_data_index: Optional[int]

    @property
    def has_debuff_index(self) -> bool:
        return self.debuff_data_index is not None

    @property
    def is_ab(self) -> bool:
        return self.tile_id in (A_TILE_ID, B_TILE_ID)

    @property
    def is_existing_structure(self) -> bool:
        return self.tile_id in STRUCTURE_TYPE_BY_TILE_ID

    @property
    def is_replaceable_ab(self) -> bool:
        # protobuf-net may explicitly serialize the documented no-debuff
        # sentinel (-1).  Only a non-negative index points at real DebuffData.
        return self.is_ab and self.debuff_data_index in (None, -1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tile_index": self.tile_index,
            "x": self.x,
            "y": self.y,
            "z": self.z,
            "tile_id": self.tile_id,
            "tile_type": self.tile_type,
            "has_debuff_index": self.has_debuff_index,
            "debuff_data_index": self.debuff_data_index,
        }


@dataclass(frozen=True)
class StructurePresence:
    """Existing C-N members of one tower, grouped by type."""

    structure_type: str
    tile_id: int
    member_indices: Tuple[int, ...]

    @property
    def count(self) -> int:
        return len(self.member_indices)

    def as_dict(self) -> dict[str, Any]:
        return {
            "structure_type": self.structure_type,
            "tile_id": self.tile_id,
            "count": self.count,
            "member_indices": list(self.member_indices),
        }


@dataclass(frozen=True)
class TowerDiagnostic:
    """Actual members and anomalies for one explicit StackData record."""

    tower_index: int
    top_field_index: int
    anchor: Tuple[int, int]
    direction: int
    members: Tuple[TowerMember, ...]
    z_values: Tuple[int, ...]
    missing_z_values: Tuple[int, ...]
    duplicate_z_values: Tuple[int, ...]
    is_out_of_bounds: bool

    @property
    def member_indices(self) -> Tuple[int, ...]:
        return tuple(member.tile_index for member in self.members)

    @property
    def actual_member_count(self) -> int:
        return len(self.members)

    @property
    def is_empty(self) -> bool:
        return not self.members

    @property
    def has_z_gap(self) -> bool:
        return bool(self.missing_z_values)

    @property
    def a_member_indices(self) -> Tuple[int, ...]:
        return tuple(
            member.tile_index for member in self.members
            if member.tile_id == A_TILE_ID
        )

    @property
    def b_member_indices(self) -> Tuple[int, ...]:
        return tuple(
            member.tile_index for member in self.members
            if member.tile_id == B_TILE_ID
        )

    @property
    def a_count(self) -> int:
        return len(self.a_member_indices)

    @property
    def b_count(self) -> int:
        return len(self.b_member_indices)

    @property
    def replaceable_ab_indices(self) -> Tuple[int, ...]:
        return tuple(
            member.tile_index for member in self.members
            if member.is_replaceable_ab
        )

    @property
    def existing_structures(self) -> Tuple[StructurePresence, ...]:
        grouped: dict[str, list[int]] = defaultdict(list)
        for member in self.members:
            if member.tile_type in STRUCTURE_TYPES:
                grouped[member.tile_type].append(member.tile_index)
        return tuple(
            StructurePresence(
                structure_type=structure_type,
                tile_id=STRUCTURE_TILE_ID_BY_TYPE[structure_type],
                member_indices=tuple(grouped[structure_type]),
            )
            for structure_type in STRUCTURE_TYPES
            if grouped[structure_type]
        )

    @property
    def existing_structure_count(self) -> int:
        return sum(item.count for item in self.existing_structures)

    @property
    def existing_structure_counts(self) -> dict[str, int]:
        return {
            item.structure_type: item.count for item in self.existing_structures
        }

    @property
    def other_member_indices(self) -> Tuple[int, ...]:
        return tuple(
            member.tile_index for member in self.members
            if not member.is_ab and not member.is_existing_structure
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "tower_index": self.tower_index,
            "top_field_index": self.top_field_index,
            "anchor": list(self.anchor),
            "direction": self.direction,
            "actual_member_count": self.actual_member_count,
            "member_indices": list(self.member_indices),
            "members": [member.as_dict() for member in self.members],
            "a_count": self.a_count,
            "a_member_indices": list(self.a_member_indices),
            "b_count": self.b_count,
            "b_member_indices": list(self.b_member_indices),
            "replaceable_ab_indices": list(self.replaceable_ab_indices),
            "existing_structure_count": self.existing_structure_count,
            "existing_structures": [
                item.as_dict() for item in self.existing_structures
            ],
            "other_member_indices": list(self.other_member_indices),
            "is_empty": self.is_empty,
            "z_values": list(self.z_values),
            "missing_z_values": list(self.missing_z_values),
            "duplicate_z_values": list(self.duplicate_z_values),
            "has_z_gap": self.has_z_gap,
            "is_out_of_bounds": self.is_out_of_bounds,
        }


@dataclass(frozen=True)
class TowerReport:
    """Template-level tower diagnostics suitable for UI and JSON reports."""

    template_id: str
    source_name: str
    towers: Tuple[TowerDiagnostic, ...]
    warnings: Tuple[TowerWarning, ...]
    reader_warnings: Tuple[str, ...]
    tower_member_indices: frozenset[int]
    non_tower_indices: frozenset[int]

    @property
    def tower_count(self) -> int:
        return len(self.towers)

    @property
    def empty_tower_indices(self) -> Tuple[int, ...]:
        return tuple(tower.tower_index for tower in self.towers if tower.is_empty)

    @property
    def z_gap_tower_indices(self) -> Tuple[int, ...]:
        return tuple(
            tower.tower_index for tower in self.towers if tower.has_z_gap
        )

    @property
    def out_of_bounds_tower_indices(self) -> Tuple[int, ...]:
        return tuple(
            tower.tower_index for tower in self.towers
            if tower.is_out_of_bounds
        )

    def tower(self, tower_index: int) -> TowerDiagnostic:
        for item in self.towers:
            if item.tower_index == tower_index:
                return item
        raise KeyError(tower_index)

    def tower_index_for_tile(self, tile_index: int) -> Optional[int]:
        for tower in self.towers:
            if tile_index in tower.member_indices:
                return tower.tower_index
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "source_name": self.source_name,
            "tower_count": self.tower_count,
            "tower_member_indices": sorted(self.tower_member_indices),
            "non_tower_indices": sorted(self.non_tower_indices),
            "empty_tower_indices": list(self.empty_tower_indices),
            "z_gap_tower_indices": list(self.z_gap_tower_indices),
            "out_of_bounds_tower_indices": list(
                self.out_of_bounds_tower_indices
            ),
            "towers": [tower.as_dict() for tower in self.towers],
            "warnings": [warning.as_dict() for warning in self.warnings],
            "reader_warnings": list(self.reader_warnings),
        }


def _tile_type(tile_id: int) -> Optional[str]:
    if tile_id == A_TILE_ID:
        return "A"
    if tile_id == B_TILE_ID:
        return "B"
    return STRUCTURE_TYPE_BY_TILE_ID.get(tile_id)


def _tower_out_of_bounds(template: LevelTemplate, x: int, y: int) -> bool:
    if x < 0 or y < 0:
        return True
    if template.level_width is not None and x >= template.level_width:
        return True
    if template.level_height is not None and y >= template.level_height:
        return True
    return False


def diagnose_towers(template: LevelTemplate) -> TowerReport:
    """Inspect explicit towers and recompute exact initial membership.

    The association is intentionally recomputed rather than inferred from
    ``Tile.tower_index``.  This gives the diagnostic one auditable rule and
    preserves runtime last-record-wins behaviour for duplicate anchors.
    """

    anchor_records: dict[Tuple[int, int], list[int]] = defaultdict(list)
    owner_by_anchor: dict[Tuple[int, int], int] = {}
    for tower in template.towers:
        anchor_records[tower.anchor].append(tower.index)
        owner_by_anchor[tower.anchor] = tower.index

    member_tiles: dict[int, list[Tile]] = {
        tower.index: [] for tower in template.towers
    }
    actual_membership: dict[int, int] = {}
    for tile in template.tiles:
        tower_index = owner_by_anchor.get((tile.x, tile.y))
        if tower_index is None:
            continue
        member_tiles[tower_index].append(tile)
        actual_membership[tile.index] = tower_index

    warnings: list[TowerWarning] = []
    for anchor, tower_indices in anchor_records.items():
        if len(tower_indices) > 1:
            warnings.append(
                TowerWarning(
                    code="duplicate_stackdata_anchor",
                    message=(
                        f"StackData anchor {anchor} occurs at tower indices "
                        f"{tower_indices}; runtime ownership belongs to the "
                        "last serialized record"
                    ),
                    tower_indices=tuple(tower_indices),
                    anchor=anchor,
                )
            )

    diagnostics: list[TowerDiagnostic] = []
    for tower in template.towers:
        tiles = member_tiles[tower.index]
        members = tuple(
            TowerMember(
                tile_index=tile.index,
                x=tile.x,
                y=tile.y,
                z=tile.z,
                tile_id=tile.tile_id,
                tile_type=_tile_type(tile.tile_id),
                debuff_data_index=tile.debuff_data_index,
            )
            for tile in tiles
        )
        z_counter = Counter(member.z for member in members)
        z_values = tuple(sorted(z_counter.elements()))
        highest_z = max(z_counter, default=-1)
        missing_z_values = tuple(
            z for z in range(highest_z + 1) if z not in z_counter
        )
        duplicate_z_values = tuple(
            z for z in sorted(z_counter) if z_counter[z] > 1
        )
        out_of_bounds = _tower_out_of_bounds(template, tower.x, tower.y)
        diagnostic = TowerDiagnostic(
            tower_index=tower.index,
            top_field_index=tower.top_field_index,
            anchor=tower.anchor,
            direction=tower.direction,
            members=members,
            z_values=z_values,
            missing_z_values=missing_z_values,
            duplicate_z_values=duplicate_z_values,
            is_out_of_bounds=out_of_bounds,
        )
        diagnostics.append(diagnostic)

        if out_of_bounds:
            warnings.append(
                TowerWarning(
                    code="tower_anchor_out_of_bounds",
                    message=(
                        f"tower {tower.index} anchor {tower.anchor} is outside "
                        f"level bounds {template.level_width} x "
                        f"{template.level_height}"
                    ),
                    tower_indices=(tower.index,),
                    anchor=tower.anchor,
                )
            )
        if diagnostic.is_empty:
            warnings.append(
                TowerWarning(
                    code="empty_tower",
                    message=(
                        f"tower {tower.index} anchor {tower.anchor} has no "
                        "matching TileData records"
                    ),
                    tower_indices=(tower.index,),
                    anchor=tower.anchor,
                )
            )
        if missing_z_values:
            warnings.append(
                TowerWarning(
                    code="tower_z_gap",
                    message=(
                        f"tower {tower.index} is missing serialized Z values "
                        f"{list(missing_z_values)} between 0 and its maximum Z"
                    ),
                    tower_indices=(tower.index,),
                    tile_indices=diagnostic.member_indices,
                    anchor=tower.anchor,
                )
            )
        if duplicate_z_values:
            warnings.append(
                TowerWarning(
                    code="duplicate_tower_z",
                    message=(
                        f"tower {tower.index} repeats serialized Z values "
                        f"{list(duplicate_z_values)}"
                    ),
                    tower_indices=(tower.index,),
                    tile_indices=diagnostic.member_indices,
                    anchor=tower.anchor,
                )
            )
        negative_z_indices = tuple(
            member.tile_index for member in members if member.z < 0
        )
        if negative_z_indices:
            warnings.append(
                TowerWarning(
                    code="negative_tower_z",
                    message=f"tower {tower.index} contains negative Z values",
                    tower_indices=(tower.index,),
                    tile_indices=negative_z_indices,
                    anchor=tower.anchor,
                )
            )

    reader_membership = {
        tile.index: tile.tower_index
        for tile in template.tiles
        if tile.tower_index is not None
    }
    if reader_membership != actual_membership:
        differing = tuple(
            sorted(set(reader_membership) | set(actual_membership))
        )
        warnings.append(
            TowerWarning(
                code="reader_membership_mismatch",
                message=(
                    "pre-associated tile membership differs from exact "
                    "StackData-anchor membership; diagnostic values use the "
                    "exact anchor result"
                ),
                tile_indices=differing,
            )
        )

    all_tile_indices = frozenset(tile.index for tile in template.tiles)
    tower_member_indices = frozenset(actual_membership)
    return TowerReport(
        template_id=template.template_id,
        source_name=template.source_name,
        towers=tuple(diagnostics),
        warnings=tuple(warnings),
        reader_warnings=tuple(template.warnings),
        tower_member_indices=tower_member_indices,
        non_tower_indices=all_tile_indices - tower_member_indices,
    )


# ``analyze_towers`` is a readable compatibility name for callers that use
# "analysis" rather than "diagnostics" in their service layer.
analyze_towers = diagnose_towers


@dataclass(frozen=True)
class TowerOnlyAssignment:
    """One A/B-to-C-N replacement selected by the tower-only allocator."""

    tile_index: int
    tower_index: int
    structure_type: str
    tile_id: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "tile_index": self.tile_index,
            "tower_index": self.tower_index,
            "structure_type": self.structure_type,
            "tile_id": self.tile_id,
        }


@dataclass(frozen=True)
class TowerAllocationBucket:
    """Selected capacity and final assignments for one explicit tower."""

    tower_index: int
    anchor: Tuple[int, int]
    candidate_indices: Tuple[int, ...]
    assignments: Tuple[TowerOnlyAssignment, ...]

    @property
    def capacity(self) -> int:
        return len(self.candidate_indices)

    @property
    def assigned_count(self) -> int:
        return len(self.assignments)

    @property
    def unused_capacity(self) -> int:
        return self.capacity - self.assigned_count

    @property
    def structure_counts(self) -> dict[str, int]:
        return dict(Counter(item.structure_type for item in self.assignments))

    def as_dict(self) -> dict[str, Any]:
        return {
            "tower_index": self.tower_index,
            "anchor": list(self.anchor),
            "capacity": self.capacity,
            "candidate_indices": list(self.candidate_indices),
            "assigned_count": self.assigned_count,
            "unused_capacity": self.unused_capacity,
            "structure_counts": self.structure_counts,
            "assignments": [item.as_dict() for item in self.assignments],
        }


@dataclass(frozen=True)
class TowerOnlyAllocation:
    """A deterministic, capacity-safe tower-only distribution."""

    requested_structure_counts: Tuple[Tuple[str, int], ...]
    assignments: Tuple[TowerOnlyAssignment, ...]
    towers: Tuple[TowerAllocationBucket, ...]
    unused_candidate_indices: Tuple[int, ...]
    strict_balance_feasible: bool
    balanced_within_one: bool
    max_tower_count_difference: int
    imbalance_reason: Optional[str]
    structure_step_count: None = None
    complete_solution_count: None = None
    step_status: str = "N/A（塔内实时调控）"

    @property
    def total_requested(self) -> int:
        return sum(count for _, count in self.requested_structure_counts)

    @property
    def calculates_steps(self) -> bool:
        return False

    @property
    def assigned_indices(self) -> Tuple[int, ...]:
        return tuple(item.tile_index for item in self.assignments)

    @property
    def structure_type_tower_spread(self) -> dict[str, int]:
        towers_by_type: dict[str, set[int]] = defaultdict(set)
        for item in self.assignments:
            towers_by_type[item.structure_type].add(item.tower_index)
        return {
            structure_type: len(towers_by_type[structure_type])
            for structure_type, _ in self.requested_structure_counts
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_structure_counts": dict(self.requested_structure_counts),
            "total_requested": self.total_requested,
            "assignments": [item.as_dict() for item in self.assignments],
            "assigned_indices": list(self.assigned_indices),
            "unused_candidate_indices": list(self.unused_candidate_indices),
            "towers": [tower.as_dict() for tower in self.towers],
            "strict_balance_feasible": self.strict_balance_feasible,
            "balanced_within_one": self.balanced_within_one,
            "max_tower_count_difference": self.max_tower_count_difference,
            "imbalance_reason": self.imbalance_reason,
            "structure_step_count": None,
            "complete_solution_count": None,
            "step_status": self.step_status,
        }


def _normalize_structure_counts(
    structure_counts: Mapping[str | int, int],
) -> Tuple[Tuple[str, int], ...]:
    if not isinstance(structure_counts, Mapping) or not structure_counts:
        raise TowerAllocationError(
            "empty_structure_counts",
            "tower-only allocation requires at least one C-N structure type",
        )

    normalized: dict[str, int] = {}
    for raw_type, raw_count in structure_counts.items():
        if isinstance(raw_type, str):
            structure_type = raw_type.strip().upper()
        elif isinstance(raw_type, int) and not isinstance(raw_type, bool):
            structure_type = STRUCTURE_TYPE_BY_TILE_ID.get(raw_type, "")
        else:
            structure_type = ""
        if structure_type not in STRUCTURE_TYPES:
            raise TowerAllocationError(
                "invalid_structure_type",
                f"structure type {raw_type!r} is not one of C-N",
                evidence={"structure_type": raw_type},
            )
        if structure_type in normalized:
            raise TowerAllocationError(
                "duplicate_structure_type",
                f"structure type {structure_type} was supplied more than once",
                evidence={"structure_type": structure_type},
            )
        if isinstance(raw_count, bool) or not isinstance(raw_count, int):
            raise TowerAllocationError(
                "invalid_structure_count",
                f"count for {structure_type} must be an integer",
                evidence={"structure_type": structure_type, "count": raw_count},
            )
        if raw_count <= 0 or raw_count % 3 != 0:
            raise TowerAllocationError(
                "structure_count_not_multiple_of_three",
                f"count for {structure_type} must be a positive multiple of 3",
                evidence={"structure_type": structure_type, "count": raw_count},
            )
        normalized[structure_type] = raw_count

    return tuple(
        (structure_type, normalized[structure_type])
        for structure_type in STRUCTURE_TYPES
        if structure_type in normalized
    )


def _balanced_quotas(
    capacities: Mapping[int, int], total: int
) -> tuple[dict[int, int], bool]:
    """Water-fill capacities and state whether a difference <= 1 was feasible."""

    active = tuple(sorted(index for index, cap in capacities.items() if cap > 0))
    quotas = {index: 0 for index in capacities}
    if not active:
        return quotas, total == 0

    floor_share, remainder = divmod(total, len(active))
    strict_feasible = (
        all(capacities[index] >= floor_share for index in active)
        and sum(
            1 for index in active if capacities[index] >= floor_share + 1
        ) >= remainder
    )

    for _ in range(total):
        available = [
            index for index in active if quotas[index] < capacities[index]
        ]
        if not available:
            raise AssertionError("validated capacity was exhausted unexpectedly")
        chosen = min(available, key=lambda index: (quotas[index], index))
        quotas[chosen] += 1
    return quotas, strict_feasible


def _spread_types(
    requested: Tuple[Tuple[str, int], ...],
    quotas: Mapping[int, int],
) -> dict[int, list[str]]:
    """Fill tower quotas while first minimizing same-type concentration."""

    assigned: dict[int, list[str]] = {index: [] for index in quotas}
    per_type: dict[str, Counter[int]] = {
        structure_type: Counter() for structure_type, _ in requested
    }
    for structure_type, count in requested:
        for _ in range(count):
            available = [
                index
                for index, quota in quotas.items()
                if len(assigned[index]) < quota
            ]
            if not available:
                raise AssertionError("validated tower quotas were exhausted")
            chosen = min(
                available,
                key=lambda index: (
                    per_type[structure_type][index],
                    len(assigned[index]),
                    index,
                ),
            )
            assigned[chosen].append(structure_type)
            per_type[structure_type][chosen] += 1
    return assigned


def allocate_only_mode(
    template: LevelTemplate,
    selected_member_indices: Iterable[int],
    structure_counts: Mapping[str | int, int],
) -> TowerOnlyAllocation:
    """Distribute C-N replacements over selected tower-member A/B capacity.

    ``selected_member_indices`` is the already-filtered capacity offered by the
    caller.  It may be larger than the requested total; this function chooses a
    deterministic subset.  It never falls back to non-tower tiles and it never
    invokes the static structure-step or complete-solution solver.
    """

    requested = _normalize_structure_counts(structure_counts)
    requested_total = sum(count for _, count in requested)
    report = diagnose_towers(template)

    raw_selected = tuple(selected_member_indices)
    for index in raw_selected:
        if isinstance(index, bool) or not isinstance(index, int):
            raise TowerAllocationError(
                "invalid_selected_index",
                "selected tower member indices must be integers",
                evidence={"index": index},
            )
    if len(set(raw_selected)) != len(raw_selected):
        duplicates = tuple(
            sorted(index for index, count in Counter(raw_selected).items() if count > 1)
        )
        raise TowerAllocationError(
            "duplicate_selected_index",
            f"selected tower member indices contain duplicates: {duplicates}",
            evidence={"duplicates": duplicates},
        )

    tile_by_index = {tile.index: tile for tile in template.tiles}
    tower_by_index = {tower.tower_index: tower for tower in report.towers}
    candidates: dict[int, list[int]] = {
        tower.tower_index: [] for tower in report.towers
    }
    for index in sorted(raw_selected):
        tile = tile_by_index.get(index)
        if tile is None:
            raise TowerAllocationError(
                "selected_index_out_of_range",
                f"selected tile index {index} is not present in the template",
                evidence={"tile_index": index},
            )
        tower_index = report.tower_index_for_tile(index)
        if tower_index is None:
            raise TowerAllocationError(
                "selected_tile_not_in_tower",
                f"selected tile index {index} is not an explicit tower member",
                evidence={"tile_index": index},
            )
        tower = tower_by_index[tower_index]
        if tower.is_out_of_bounds:
            raise TowerAllocationError(
                "selected_tower_out_of_bounds",
                f"selected tile index {index} belongs to an out-of-bounds tower",
                evidence={
                    "tile_index": index,
                    "tower_index": tower_index,
                    "anchor": tower.anchor,
                },
            )
        if tile.tile_id not in (A_TILE_ID, B_TILE_ID):
            raise TowerAllocationError(
                "selected_tile_not_ab",
                f"selected tile index {index} is not an A/B Tile",
                evidence={"tile_index": index, "tile_id": tile.tile_id},
            )
        if tile.debuff_data_index not in (None, -1):
            raise TowerAllocationError(
                "selected_tile_requires_compatibility_check",
                f"selected tile index {index} has a DebuffDataIndex",
                evidence={
                    "tile_index": index,
                    "debuff_data_index": tile.debuff_data_index,
                },
            )
        candidates[tower_index].append(index)

    capacities = {index: len(indices) for index, indices in candidates.items()}
    total_capacity = sum(capacities.values())
    if total_capacity == 0:
        raise TowerAllocationError(
            "no_tower_capacity",
            "no selected replaceable A/B members exist in an explicit tower",
        )
    if requested_total > total_capacity:
        raise TowerAllocationError(
            "insufficient_tower_capacity",
            f"requested {requested_total} structure tiles but tower capacity is "
            f"only {total_capacity}",
            evidence={
                "requested": requested_total,
                "capacity": total_capacity,
                "per_tower_capacity": capacities,
            },
        )

    quotas, strict_feasible = _balanced_quotas(capacities, requested_total)
    types_by_tower = _spread_types(requested, quotas)
    assignment_by_tower: dict[int, list[TowerOnlyAssignment]] = {
        index: [] for index in candidates
    }
    used_indices: set[int] = set()
    for tower_index in sorted(candidates):
        chosen_indices = tuple(candidates[tower_index][: quotas[tower_index]])
        chosen_types = types_by_tower[tower_index]
        if len(chosen_indices) != len(chosen_types):
            raise AssertionError("tower quota and type allocation disagree")
        for tile_index, structure_type in zip(chosen_indices, chosen_types):
            assignment = TowerOnlyAssignment(
                tile_index=tile_index,
                tower_index=tower_index,
                structure_type=structure_type,
                tile_id=STRUCTURE_TILE_ID_BY_TYPE[structure_type],
            )
            assignment_by_tower[tower_index].append(assignment)
            used_indices.add(tile_index)

    buckets = tuple(
        TowerAllocationBucket(
            tower_index=tower.tower_index,
            anchor=tower.anchor,
            candidate_indices=tuple(candidates[tower.tower_index]),
            assignments=tuple(assignment_by_tower[tower.tower_index]),
        )
        for tower in report.towers
    )
    assignments = tuple(
        assignment
        for bucket in buckets
        for assignment in bucket.assignments
    )
    active_counts = [
        bucket.assigned_count for bucket in buckets if bucket.capacity > 0
    ]
    count_difference = (
        max(active_counts) - min(active_counts) if active_counts else 0
    )
    balanced = count_difference <= 1
    if balanced:
        imbalance_reason = None
    else:
        saturated = [
            bucket.tower_index
            for bucket in buckets
            if bucket.capacity == bucket.assigned_count
        ]
        imbalance_reason = (
            "strict equal distribution is impossible because selected capacity "
            f"is saturated in towers {saturated}"
        )

    unused = tuple(sorted(set(raw_selected) - used_indices))
    return TowerOnlyAllocation(
        requested_structure_counts=requested,
        assignments=assignments,
        towers=buckets,
        unused_candidate_indices=unused,
        strict_balance_feasible=strict_feasible,
        balanced_within_one=balanced,
        max_tower_count_difference=count_difference,
        imbalance_reason=imbalance_reason,
    )


# Alternate service-layer wording retained as a simple alias.
allocate_tower_only = allocate_only_mode
