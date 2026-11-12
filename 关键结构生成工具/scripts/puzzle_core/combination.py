"""Exact validation for output plans containing at most two puzzle units.

Each unit is first proved in isolation by :mod:`puzzle_core.solver`.  Parallel
X/Y layouts validate both whole-unit orders and additionally search arbitrary
cross-unit interleavings for diagnostics.  Serial Z layouts validate only the
configured high-to-low order.  In every case the bag is one shared state and
is never artificially cleared when the active unit changes.

The five-singleton rule remains an isolated-unit rule.  This module therefore
does not reject a parallel plan merely because hypothetical cross-unit mixing
could create five singleton types.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import asdict, dataclass
from typing import Sequence, Tuple

from .errors import InputValidationError, ProofLimitExceeded
from .models import Layout, UnitRole
from .solver import (
    CapacityProof,
    PuzzleProof,
    SolverLimits,
    analyze_puzzle,
    available_indices,
    validate_target,
)


@dataclass(frozen=True)
class CombinationUnitInput:
    """A fixed puzzle unit in the plan's stable naming order."""

    unit_id: str
    role: UnitRole
    target_step: int
    blocker_masks: Tuple[int, ...]
    tile_types: Tuple[str, ...]
    threshold_type: str
    required_pre_gate_recycles: int = 0
    first_recycle_lead_tiles: int = 2


@dataclass(frozen=True)
class CombinationUnitProof:
    unit_id: str
    role: UnitRole
    target_step: int
    threshold_type: str
    gate_steps: Tuple[int, ...]
    minimum_gate_step: int
    maximum_gate_step: int
    target_ok: bool
    target_reasons: Tuple[str, ...]
    singleton_rule_ok: bool
    capacity_proofs: Tuple[CapacityProof, ...]
    puzzle_proof: PuzzleProof


@dataclass(frozen=True)
class OrderedCapacityProof:
    """Exact proof for completing whole units in one configured order."""

    capacity: int
    unit_order: Tuple[str, ...]
    states_explored: int
    complete_path_count: int
    path_count_capped: bool
    complete_exists: bool
    failure_reachable: bool
    minimum_failure_click: int | None
    classification: str
    instantaneous_peak: int
    settled_peak: int
    recommended_order: Tuple[Tuple[str, int], ...] | None
    shortest_failure_order: Tuple[Tuple[str, int], ...] | None


@dataclass(frozen=True)
class InterleavingCapacityProof:
    """Shared-bag proof/observation for arbitrary parallel-unit clicks."""

    capacity: int
    coverage: str
    states_explored: int
    complete_path_count: int
    path_count_capped: bool
    complete_exists: bool
    failure_reachable: bool
    minimum_failure_click: int | None
    result: str
    recommended_order: Tuple[Tuple[str, int], ...] | None
    shortest_failure_order: Tuple[Tuple[str, int], ...] | None


@dataclass(frozen=True)
class CombinationProof:
    layout: Layout
    naming_order: Tuple[str, ...]
    signature: str
    all_units_qualified: bool
    unit_proofs: Tuple[CombinationUnitProof, ...]
    ordered_capacity_proofs: Tuple[OrderedCapacityProof, ...]
    interleaving_coverage: str
    interleaving_result_at_capacity_7: str
    safe_interleaving_exists_at_capacity_7: bool | None
    shortest_interleaving_failure_at_capacity_7: Tuple[Tuple[str, int], ...] | None
    interleaving_capacity_proofs: Tuple[InterleavingCapacityProof, ...]
    singleton_rule_scope: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _normalize_layout(value: Layout | str) -> Layout:
    if isinstance(value, Layout):
        return value
    try:
        return Layout(str(value).lower())
    except ValueError as exc:
        raise InputValidationError(
            "多谜题布局只能是 x、y 或 z",
            evidence={"layout": str(value)},
        ) from exc


def _validate_roles(layout: Layout, units: Sequence[CombinationUnitInput]) -> None:
    if len(units) == 1:
        if layout is not Layout.SINGLE or units[0].role is not UnitRole.SINGLE:
            raise InputValidationError("单谜题必须使用 single 布局和 single 角色")
        return
    expected = {
        Layout.X: (UnitRole.LEFT, UnitRole.RIGHT),
        Layout.Y: (UnitRole.TOP, UnitRole.BOTTOM),
        Layout.Z: (UnitRole.HIGH, UnitRole.LOW),
    }.get(layout)
    actual = tuple(unit.role for unit in units)
    if expected is None or actual != expected:
        raise InputValidationError(
            "谜题单元角色与布局的稳定顺序不一致",
            evidence={
                "layout": layout.value,
                "expected_roles": tuple(item.value for item in expected or ()),
                "actual_roles": tuple(item.value for item in actual),
            },
        )


def _validate_unit_inputs(units: Sequence[CombinationUnitInput]) -> None:
    if not 1 <= len(units) <= 2:
        raise InputValidationError("一个成品方案只能包含 1 或 2 个谜题单元")
    identifiers = [unit.unit_id for unit in units]
    if any(not value for value in identifiers) or len(set(identifiers)) != len(identifiers):
        raise InputValidationError("谜题单元 ID 必须非空且互不重复")
    type_sets = [set(value.upper() for value in unit.tile_types) for unit in units]
    if len(type_sets) == 2:
        shared = sorted(type_sets[0] & type_sets[1])
        if shared:
            raise InputValidationError(
                "两个谜题单元必须使用互斥结构类型",
                evidence={"shared_types": shared},
            )


def _click(
    bag: Tuple[int, ...], type_index: int, capacity: int
) -> tuple[Tuple[int, ...], int, int, bool]:
    changed = list(bag)
    changed[type_index] += 1
    instantaneous = sum(changed)
    if changed[type_index] == 3:
        changed[type_index] = 0
    settled = sum(changed)
    return tuple(changed), instantaneous, settled, settled >= capacity


def _ordered_capacity_proof(
    units: Sequence[CombinationUnitInput],
    *,
    capacity: int,
    limits: SolverLimits,
) -> OrderedCapacityProof:
    labels = tuple(sorted({label.upper() for unit in units for label in unit.tile_types}))
    label_to_index = {label: index for index, label in enumerate(labels)}
    type_indices = tuple(
        tuple(label_to_index[label.upper()] for label in unit.tile_types) for unit in units
    )
    full_masks = tuple((1 << len(unit.tile_types)) - 1 for unit in units)
    start = (0, 0, (0,) * len(labels))
    queue = deque([start])
    predecessor: dict[
        tuple[int, int, Tuple[int, ...]],
        tuple[tuple[int, int, Tuple[int, ...]], tuple[str, int]] | None,
    ] = {start: None}
    path_counts: dict[tuple[int, int, Tuple[int, ...]], int] = {start: 1}
    complete_states: list[tuple[int, int, Tuple[int, ...]]] = []
    path_count_capped = False
    failure_reachable = False
    minimum_failure_click: int | None = None
    shortest_failure: tuple[tuple[str, int], ...] | None = None
    instantaneous_peak = 0
    settled_peak = 0

    def reconstruct(
        state: tuple[int, int, Tuple[int, ...]]
    ) -> tuple[tuple[str, int], ...]:
        result: list[tuple[str, int]] = []
        cursor = state
        while predecessor[cursor] is not None:
            previous, action = predecessor[cursor]  # type: ignore[misc]
            result.append(action)
            cursor = previous
        return tuple(reversed(result))

    while queue:
        state = queue.popleft()
        if len(predecessor) > limits.max_states:
            raise ProofLimitExceeded(
                "组合容量求解状态数超过上限，不能把部分搜索标为已证明",
                evidence={"capacity": capacity, "max_states": limits.max_states},
            )
        phase, removed, bag = state
        if phase == len(units):
            if not any(bag):
                complete_states.append(state)
            continue

        unit = units[phase]
        for tile_index in available_indices(removed, unit.blocker_masks):
            next_bag, instantaneous, settled, failed = _click(
                bag, type_indices[phase][tile_index], capacity
            )
            instantaneous_peak = max(instantaneous_peak, instantaneous)
            settled_peak = max(settled_peak, settled)
            action = (unit.unit_id, tile_index)
            if failed:
                failure_reachable = True
                candidate = reconstruct(state) + (action,)
                if minimum_failure_click is None or len(candidate) < minimum_failure_click:
                    minimum_failure_click = len(candidate)
                    shortest_failure = candidate
                continue

            next_removed = removed | (1 << tile_index)
            next_phase = phase
            if next_removed == full_masks[phase]:
                next_phase += 1
                next_removed = 0
            next_state = (next_phase, next_removed, next_bag)
            incoming = path_counts[state]
            updated = path_counts.get(next_state, 0) + incoming
            if updated >= limits.max_path_count:
                updated = limits.max_path_count
                path_count_capped = True
            path_counts[next_state] = updated
            if next_state not in predecessor:
                predecessor[next_state] = (state, action)
                queue.append(next_state)

    total_complete = sum(path_counts[state] for state in complete_states)
    if total_complete >= limits.max_path_count:
        total_complete = limits.max_path_count
        path_count_capped = True
    complete_exists = bool(complete_states)
    if complete_exists and not failure_reachable:
        classification = "all_safe"
    elif complete_exists and failure_reachable:
        classification = "safe_and_failure_paths"
    elif failure_reachable:
        classification = "all_fail"
    else:
        classification = "no_complete_path"
    recommended = reconstruct(complete_states[0]) if complete_states else None
    return OrderedCapacityProof(
        capacity=capacity,
        unit_order=tuple(unit.unit_id for unit in units),
        states_explored=len(predecessor),
        complete_path_count=total_complete,
        path_count_capped=path_count_capped,
        complete_exists=complete_exists,
        failure_reachable=failure_reachable,
        minimum_failure_click=minimum_failure_click,
        classification=classification,
        instantaneous_peak=instantaneous_peak,
        settled_peak=settled_peak,
        recommended_order=recommended,
        shortest_failure_order=shortest_failure,
    )


def _interleaving_capacity_proof(
    units: Sequence[CombinationUnitInput],
    *,
    capacity: int,
    limits: SolverLimits,
) -> InterleavingCapacityProof:
    """Explore arbitrary clicks across two independent parallel units.

    Hitting ``max_states`` intentionally degrades the result to ``bounded``;
    observations remain useful, but are never promoted to an exhaustive proof.
    The per-unit five-singleton rule is deliberately not re-applied globally.
    """

    labels = tuple(
        sorted({label.upper() for unit in units for label in unit.tile_types})
    )
    label_to_index = {label: index for index, label in enumerate(labels)}
    type_indices = tuple(
        tuple(label_to_index[label.upper()] for label in unit.tile_types)
        for unit in units
    )
    full_masks = tuple((1 << len(unit.tile_types)) - 1 for unit in units)
    start = ((0,) * len(units), (0,) * len(labels))
    queue = deque([start])
    predecessor: dict[
        tuple[Tuple[int, ...], Tuple[int, ...]],
        tuple[
            tuple[Tuple[int, ...], Tuple[int, ...]],
            tuple[str, int],
        ]
        | None,
    ] = {start: None}
    path_counts: dict[tuple[Tuple[int, ...], Tuple[int, ...]], int] = {
        start: 1
    }
    complete_states: list[tuple[Tuple[int, ...], Tuple[int, ...]]] = []
    path_count_capped = False
    bounded = False
    failure_reachable = False
    minimum_failure_click: int | None = None
    shortest_failure: tuple[tuple[str, int], ...] | None = None

    def reconstruct(
        state: tuple[Tuple[int, ...], Tuple[int, ...]]
    ) -> tuple[tuple[str, int], ...]:
        result: list[tuple[str, int]] = []
        cursor = state
        while predecessor[cursor] is not None:
            previous, action = predecessor[cursor]  # type: ignore[misc]
            result.append(action)
            cursor = previous
        return tuple(reversed(result))

    while queue:
        state = queue.popleft()
        removed_masks, bag = state
        if removed_masks == full_masks:
            if not any(bag):
                complete_states.append(state)
            continue
        for unit_index, unit in enumerate(units):
            removed = removed_masks[unit_index]
            if removed == full_masks[unit_index]:
                continue
            for tile_index in available_indices(removed, unit.blocker_masks):
                next_bag, _instantaneous, _settled, failed = _click(
                    bag, type_indices[unit_index][tile_index], capacity
                )
                action = (unit.unit_id, tile_index)
                if failed:
                    failure_reachable = True
                    candidate = reconstruct(state) + (action,)
                    if (
                        minimum_failure_click is None
                        or len(candidate) < minimum_failure_click
                    ):
                        minimum_failure_click = len(candidate)
                        shortest_failure = candidate
                    continue

                changed_removed = list(removed_masks)
                changed_removed[unit_index] |= 1 << tile_index
                next_state = (tuple(changed_removed), next_bag)
                incoming = path_counts[state]
                updated = path_counts.get(next_state, 0) + incoming
                if updated >= limits.max_path_count:
                    updated = limits.max_path_count
                    path_count_capped = True
                if next_state in predecessor:
                    path_counts[next_state] = updated
                    continue
                if len(predecessor) >= limits.max_states:
                    bounded = True
                    continue
                predecessor[next_state] = (state, action)
                path_counts[next_state] = updated
                queue.append(next_state)

    total_complete = sum(path_counts[state] for state in complete_states)
    if total_complete >= limits.max_path_count:
        total_complete = limits.max_path_count
        path_count_capped = True
    complete_exists = bool(complete_states)
    coverage = "bounded" if bounded else "exhaustive"
    if not bounded and complete_exists and not failure_reachable:
        result = "all_safe"
    elif not bounded and complete_exists and failure_reachable:
        result = "safe_and_failure_paths"
    elif not bounded and failure_reachable:
        result = "no_safe_path"
    elif not bounded:
        result = "no_complete_path"
    elif complete_exists and failure_reachable:
        result = "safe_and_failure_observed"
    elif complete_exists:
        result = "safe_path_observed"
    elif failure_reachable:
        result = "failure_path_observed"
    else:
        result = "unknown"
    return InterleavingCapacityProof(
        capacity=capacity,
        coverage=coverage,
        states_explored=len(predecessor),
        complete_path_count=total_complete,
        path_count_capped=path_count_capped,
        complete_exists=complete_exists,
        failure_reachable=failure_reachable,
        minimum_failure_click=minimum_failure_click,
        result=result,
        recommended_order=(
            reconstruct(complete_states[0]) if complete_states else None
        ),
        shortest_failure_order=shortest_failure,
    )


def analyze_combination(
    units: Sequence[CombinationUnitInput],
    *,
    layout: Layout | str,
    capacities: Sequence[int] = (7, 8),
    limits: SolverLimits | None = None,
    _unit_proofs: Sequence[PuzzleProof] | None = None,
) -> CombinationProof:
    """Prove one or two fixed units and their allowed whole-unit orders.

    ``units`` must already be in the stable naming order: left-to-right for X,
    top-to-bottom for Y, and high-to-low for Z.  For X/Y both whole-unit orders
    are checked.  For Z only high-to-low is legal.  No click interleaving is
    searched in this first-version validator.
    """

    fixed_units = tuple(units)
    _validate_unit_inputs(fixed_units)
    fixed_layout = _normalize_layout(layout)
    _validate_roles(fixed_layout, fixed_units)
    actual_limits = limits or SolverLimits()
    normalized_capacities = tuple(int(value) for value in capacities)
    if not normalized_capacities or any(value <= 0 for value in normalized_capacities):
        raise InputValidationError("背包容量必须是正整数")
    if len(set(normalized_capacities)) != len(normalized_capacities):
        raise InputValidationError("背包容量不能重复")
    if _unit_proofs is not None and len(_unit_proofs) != len(fixed_units):
        raise InputValidationError("独立单元证明数量与组合输入不一致")

    unit_results: list[CombinationUnitProof] = []
    for index, unit in enumerate(fixed_units):
        # The independent validator already recomputed these from source
        # geometry. Reuse only that local result, never a candidate's proof.
        proof = _unit_proofs[index] if _unit_proofs is not None else analyze_puzzle(
            unit.blocker_masks,
            unit.tile_types,
            threshold_type=unit.threshold_type,
            capacities=normalized_capacities,
            limits=actual_limits,
            required_pre_gate_recycles=unit.required_pre_gate_recycles,
            first_recycle_lead_tiles=unit.first_recycle_lead_tiles,
        )
        if (proof.blocker_masks != tuple(unit.blocker_masks)
                or proof.tile_types != tuple(unit.tile_types)
                or proof.threshold_type != unit.threshold_type
                or proof.required_pre_gate_recycles != unit.required_pre_gate_recycles
                or proof.first_recycle_lead_tiles != unit.first_recycle_lead_tiles
                or tuple(p.capacity for p in proof.capacity_proofs) != normalized_capacities):
            raise InputValidationError("独立单元证明与组合输入不一致")
        if unit.target_step == 3:
            by_capacity = {item.capacity: item for item in proof.capacity_proofs}
            capacity_seven = by_capacity.get(7)
            target_ok = bool(
                proof.minimum_structure_step == 3
                and proof.recommended_path_constraints_ok
                and capacity_seven is not None
                and capacity_seven.complete_exists
            )
            reasons = () if target_ok else ("board_reveal_intrinsic_s3_invalid",)
        else:
            target_ok, reasons = validate_target(
                proof,
                unit.target_step,
                required_pre_gate_recycles=unit.required_pre_gate_recycles,
                first_recycle_lead_tiles=unit.first_recycle_lead_tiles,
            )
        unit_results.append(
            CombinationUnitProof(
                unit_id=unit.unit_id,
                role=unit.role,
                target_step=unit.target_step,
                threshold_type=proof.threshold_type,
                gate_steps=proof.threshold_steps,
                minimum_gate_step=proof.minimum_structure_step,
                maximum_gate_step=proof.maximum_structure_step,
                target_ok=target_ok,
                target_reasons=reasons,
                singleton_rule_ok=proof.singleton_rule_ok,
                capacity_proofs=proof.capacity_proofs,
                puzzle_proof=proof,
            )
        )

    ordered_inputs: list[tuple[CombinationUnitInput, ...]]
    if len(fixed_units) == 1 or fixed_layout is Layout.Z:
        ordered_inputs = [fixed_units]
    else:
        ordered_inputs = [fixed_units, tuple(reversed(fixed_units))]
    if len(fixed_units) == 1:
        # A single unit and its whole-unit order are the identical shared-bag
        # problem. Reuse the freshly independently recomputed proof, including
        # failure paths; do not enumerate its removed-tile subsets a second time.
        unit_id = fixed_units[0].unit_id
        ordered_results = tuple(OrderedCapacityProof(
            capacity=proof.capacity, unit_order=(unit_id,), states_explored=proof.states_explored,
            complete_path_count=proof.complete_path_count, path_count_capped=proof.path_count_capped,
            complete_exists=proof.complete_exists, failure_reachable=proof.failure_reachable,
            minimum_failure_click=proof.minimum_failure_click, classification=proof.classification,
            instantaneous_peak=proof.instantaneous_peak, settled_peak=proof.settled_peak,
            recommended_order=None if proof.recommended_order is None else tuple((unit_id, i) for i in proof.recommended_order),
            shortest_failure_order=None if proof.shortest_failure_order is None else tuple((unit_id, i) for i in proof.shortest_failure_order),
        ) for proof in unit_results[0].capacity_proofs)
    else:
        ordered_results = tuple(
            _ordered_capacity_proof(order, capacity=capacity, limits=actual_limits)
            for order in ordered_inputs
            for capacity in normalized_capacities
        )
    if len(fixed_units) == 2 and fixed_layout in (Layout.X, Layout.Y):
        interleaving_results = tuple(
            _interleaving_capacity_proof(
                fixed_units,
                capacity=capacity,
                limits=actual_limits,
            )
            for capacity in normalized_capacities
        )
        interleaving_coverage = (
            "exhaustive"
            if all(item.coverage == "exhaustive" for item in interleaving_results)
            else "bounded"
        )
        seven = next(
            (item for item in interleaving_results if item.capacity == 7), None
        )
        interleaving_result = seven.result if seven is not None else "unknown"
        safe_interleaving = seven.complete_exists if seven is not None else None
        shortest_interleaving_failure = (
            seven.shortest_failure_order if seven is not None else None
        )
    else:
        interleaving_results = ()
        interleaving_coverage = "not_applicable"
        interleaving_result = "not_applicable"
        safe_interleaving = None
        shortest_interleaving_failure = None
    signature = "".join(f"s{unit.target_step}" for unit in fixed_units)
    return CombinationProof(
        layout=fixed_layout,
        naming_order=tuple(unit.unit_id for unit in fixed_units),
        signature=signature,
        all_units_qualified=all(item.target_ok for item in unit_results),
        unit_proofs=tuple(unit_results),
        ordered_capacity_proofs=ordered_results,
        interleaving_coverage=interleaving_coverage,
        interleaving_result_at_capacity_7=interleaving_result,
        safe_interleaving_exists_at_capacity_7=safe_interleaving,
        shortest_interleaving_failure_at_capacity_7=shortest_interleaving_failure,
        interleaving_capacity_proofs=interleaving_results,
        singleton_rule_scope="per_unit_only",
    )
