"""Auditable grammars for deliberately non-mainstream key structures.

The normal generator may use sampled whole-board rollouts for ranking, but a
special preset is an export contract.  This module therefore uses conservative
static sufficient conditions: if a prior wave/cluster is a transitive ancestor
of a required barrier tile for every later type, no topological click order --
and consequently no legal game path -- can complete that later type first.

The condition is intentionally stronger than observing the desired order in a
handful of replays.  A compatible board is accepted with an auditable proof;
an incompatible board is rejected instead of being labelled "probably serial".
"""

from __future__ import annotations

import random
from typing import Mapping, Sequence

from .errors import InputValidationError, ProofLimitExceeded
from .geometry import GeometryGraph
from .solver import (
    CapacityProof,
    PuzzleProof,
    SolverLimits,
    analyze_puzzle,
    available_indices,
    validate_target,
)


SPECIAL_GRAMMAR_VERSION = "special-structure-grammar-v2"
SUSPENDED_EXPOSURE_BOUND_VERSION = "suspended-exposure-progress-span-v1"
ALL_TYPES_WAVE_SCHEMAS: tuple[
    tuple[str, tuple[tuple[str, ...], ...]], ...
] = (
    (
        "all-types-long-range-3-wave-v1",
        (
            ("C", "D", "E", "F"),
            ("G", "H", "I", "J"),
            ("K", "L", "M", "N"),
        ),
    ),
    (
        "all-types-long-range-4-wave-v1",
        (
            ("C", "D", "E"),
            ("F", "G", "H"),
            ("I", "J", "K"),
            ("L", "M", "N"),
        ),
    ),
)


def _wave_schema_candidates(
    preferred_wave_count: int | None,
) -> tuple[tuple[str, tuple[tuple[str, ...], ...]], ...]:
    if preferred_wave_count is None:
        return ALL_TYPES_WAVE_SCHEMAS
    if preferred_wave_count not in (3, 4):
        raise InputValidationError("全类型长程型的揭露波次只能是 3 或 4")
    return tuple(
        (schema_id, schema)
        for schema_id, schema in ALL_TYPES_WAVE_SCHEMAS
        if len(schema) == preferred_wave_count
    )


def _transitive_ancestor_masks(blocker_masks: Sequence[int]) -> tuple[int, ...]:
    """Return every direct/transitive blocker for each local tile."""

    masks = tuple(int(value) for value in blocker_masks)
    n = len(masks)
    allowed = (1 << n) - 1
    if any(value < 0 or value & ~allowed for value in masks):
        raise InputValidationError("特殊预设的遮挡掩码越界")
    closure = list(masks)
    # The source geometry is a Z-directed DAG.  A bounded fixed-point closure
    # still fails closed if a malformed caller introduces a cycle.
    for _ in range(n):
        changed = False
        for index, direct in enumerate(masks):
            expanded = closure[index]
            pending = direct
            while pending:
                bit = pending & -pending
                blocker = bit.bit_length() - 1
                expanded |= closure[blocker]
                pending ^= bit
            if expanded != closure[index]:
                closure[index] = expanded
                changed = True
        if not changed:
            break
    if any(mask & (1 << index) for index, mask in enumerate(closure)):
        raise InputValidationError("特殊预设的结构依赖存在环")
    return tuple(closure)


def _random_topological_order(
    blocker_masks: Sequence[int], rng: random.Random
) -> tuple[int, ...]:
    removed = 0
    result: list[int] = []
    while len(result) < len(blocker_masks):
        choices = available_indices(removed, blocker_masks)
        if not choices:
            return ()
        index = choices[rng.randrange(len(choices))]
        result.append(index)
        removed |= 1 << index
    return tuple(result)


def _local_masks(
    blocker_masks: Sequence[int], members: Sequence[int]
) -> tuple[int, ...]:
    ordered = tuple(sorted(members))
    local = {original: index for index, original in enumerate(ordered)}
    result = []
    for original in ordered:
        mask = 0
        direct = int(blocker_masks[original])
        for candidate, local_index in local.items():
            if direct & (1 << candidate):
                mask |= 1 << local_index
        result.append(mask)
    return tuple(result)


def _capacity_seven_safe(proof: PuzzleProof) -> bool:
    return any(
        item.capacity == 7 and item.complete_exists
        for item in proof.capacity_proofs
    )


def _multiply_capped(values: Sequence[int], limit: int) -> tuple[int, bool]:
    result = 1
    capped = False
    for value in values:
        result *= int(value)
        if result >= limit:
            result = limit
            capped = True
            break
    return result, capped


def _assemble_wave_proof(
    blocker_masks: tuple[int, ...],
    tile_types: tuple[str, ...],
    threshold_type: str,
    wave_members: Sequence[Sequence[int]],
    wave_proofs: Sequence[PuzzleProof],
    limits: SolverLimits,
) -> PuzzleProof:
    """Compose exact per-wave proofs after static dominance proves sequencing.

    Each type belongs to exactly one wave and every wave proof finishes with an
    empty bag.  The dominance contract prohibits a later type from completing
    before the prior wave is empty, so concatenating the exact per-wave paths
    is a valid global safe path without pretending that 36-tile exhaustive
    enumeration fitted in memory.
    """

    ordered_waves = [tuple(sorted(members)) for members in wave_members]

    def mapped_order(wave_index: int, order: Sequence[int]) -> tuple[int, ...]:
        members = ordered_waves[wave_index]
        return tuple(members[local] for local in order)

    recommended = tuple(
        index
        for wave_index, proof in enumerate(wave_proofs)
        for index in mapped_order(wave_index, proof.recommended_order)
    )
    theoretical_count, theoretical_capped = _multiply_capped(
        [proof.theoretical_complete_path_count for proof in wave_proofs],
        limits.max_path_count,
    )
    capacities: list[CapacityProof] = []
    for capacity in (7, 8):
        rows = [
            next(item for item in proof.capacity_proofs if item.capacity == capacity)
            for proof in wave_proofs
        ]
        complete_exists = all(item.complete_exists for item in rows)
        failure_reachable = any(item.failure_reachable for item in rows)
        if complete_exists and not failure_reachable:
            classification = "all_safe"
        elif complete_exists and failure_reachable:
            classification = "safe_and_failure_paths"
        elif failure_reachable:
            classification = "all_fail"
        else:
            classification = "no_complete_path"
        complete_count, count_capped = _multiply_capped(
            [item.complete_path_count for item in rows], limits.max_path_count
        )
        safe_prefix: list[int] = []
        shortest_failure: tuple[int, ...] | None = None
        for wave_index, row in enumerate(rows):
            if shortest_failure is None and row.shortest_failure_order is not None:
                shortest_failure = tuple(safe_prefix) + mapped_order(
                    wave_index, row.shortest_failure_order
                )
            if row.recommended_order is not None:
                safe_prefix.extend(mapped_order(wave_index, row.recommended_order))
        capacities.append(
            CapacityProof(
                capacity=capacity,
                states_explored=sum(item.states_explored for item in rows),
                complete_path_count=complete_count,
                path_count_capped=count_capped
                or any(item.path_count_capped for item in rows),
                complete_exists=complete_exists,
                failure_reachable=failure_reachable,
                minimum_failure_click=(
                    None if shortest_failure is None else len(shortest_failure)
                ),
                gate_reachable=rows[0].gate_reachable,
                classification=classification,
                instantaneous_peak=max(item.instantaneous_peak for item in rows),
                settled_peak=max(item.settled_peak for item in rows),
                recommended_order=recommended if complete_exists else None,
                shortest_failure_order=shortest_failure,
            )
        )
    violation_order = None
    violation_types: tuple[str, ...] = ()
    safe_prefix: list[int] = []
    for wave_index, proof in enumerate(wave_proofs):
        if violation_order is None and proof.singleton_violation_order is not None:
            violation_order = tuple(safe_prefix) + mapped_order(
                wave_index, proof.singleton_violation_order
            )
            violation_types = proof.singleton_violation_types
        safe_prefix.extend(mapped_order(wave_index, proof.recommended_order))
    first = wave_proofs[0]
    return PuzzleProof(
        tile_types=tile_types,
        blocker_masks=blocker_masks,
        threshold_type=threshold_type,
        states_explored=sum(proof.states_explored for proof in wave_proofs),
        theoretical_complete_path_count=theoretical_count,
        path_count_capped=theoretical_capped
        or any(proof.path_count_capped for proof in wave_proofs),
        threshold_steps=first.threshold_steps,
        literal_first_match_clicks=first.literal_first_match_clicks,
        singleton_rule_ok=all(proof.singleton_rule_ok for proof in wave_proofs),
        singleton_violation_order=violation_order,
        singleton_violation_types=violation_types,
        recommended_order=recommended,
        required_pre_gate_recycles=first.required_pre_gate_recycles,
        first_recycle_lead_tiles=first.first_recycle_lead_tiles,
        recommended_pre_gate_recycles=first.recommended_pre_gate_recycles,
        recommended_first_match_click=first.recommended_first_match_click,
        recommended_gate_click=first.recommended_gate_click,
        recommended_path_constraints_ok=all(
            proof.recommended_path_constraints_ok for proof in wave_proofs
        ),
        capacity_proofs=tuple(capacities),
    )


def all_types_wave_contract(
    blocker_masks: Sequence[int],
    tile_types: Sequence[str],
    proof: PuzzleProof | None = None,
    *,
    preferred_wave_count: int | None = None,
) -> dict[str, object]:
    """Return a versioned 3/4-wave conservative dominance proof.

    The contract proves ordering and one capacity-7 exit per wave.  It does
    *not* claim a bound on how long one/two exposed tiles may remain suspended
    on the complete A/B board: that requires full-board reveal paths, which
    are intentionally outside this structure-only proof.
    """

    masks = tuple(int(value) for value in blocker_masks)
    types = tuple(str(value).upper() for value in tile_types)
    base: dict[str, object] = {
        "grammar_version": SPECIAL_GRAMMAR_VERSION,
        "proof_method": "conservative_static_ancestor_sufficient_condition",
        "entry_event": {
            "id": "wave_first_triplet_completed",
            "predicate": (
                "any structure type assigned to the wave has completed its "
                "three-tile match"
            ),
        },
        "completion_event": {
            "id": "wave_all_structures_completed",
            "predicate": "all C-N tiles assigned to the wave have been removed",
        },
        "proof_scope": (
            "every topological structure-removal order; legal game paths are "
            "a subset because bag constraints can only remove transitions"
        ),
        "suspended_exposure_bound": {
            "status": "not_proved",
            "metric_version": SUSPENDED_EXPOSURE_BOUND_VERSION,
            "definition": (
                "maximum consecutive whole-board progress steps with exactly "
                "one or two accessible tiles of the same unfinished C-N type"
            ),
            "maximum_progress_span": None,
            "limit_progress_span": None,
            "limit_id": None,
            "proof_scope": None,
            "reason": (
                "structure-only dominance cannot bound one/two-tile exposure "
                "duration across complete-board A/B removal paths"
            ),
        },
    }
    if (
        len(types) != 36
        or set(types) != set("CDEFGHIJKLMN")
        or any(types.count(label) != 3 for label in "CDEFGHIJKLMN")
    ):
        return {
            **base,
            "solver_status": "disproved",
            "wave_count": 0,
            "dominance_chain": False,
            "shortcut_count": None,
            "capacity_safe_exits": False,
            "reasons": ["twelve_types_times_three_required"],
        }
    ancestors = _transitive_ancestor_masks(masks)
    evaluated: list[dict[str, object]] = []
    for schema_id, schema in _wave_schema_candidates(preferred_wave_count):
        wave_members = tuple(
            tuple(index for index, label in enumerate(types) if label in wave)
            for wave in schema
        )
        shortcuts: list[dict[str, object]] = []
        barriers: dict[str, dict[str, int]] = {}
        for wave_index in range(1, len(schema)):
            previous = wave_members[wave_index - 1]
            previous_mask = sum(1 << index for index in previous)
            wave_barriers: dict[str, int] = {}
            for label in schema[wave_index]:
                members = [
                    index
                    for index in wave_members[wave_index]
                    if types[index] == label
                ]
                barrier = next(
                    (
                        index
                        for index in members
                        if ancestors[index] & previous_mask == previous_mask
                    ),
                    None,
                )
                if barrier is None:
                    shortcuts.append(
                        {
                            "from_wave": wave_index,
                            "to_wave": wave_index + 1,
                            "later_type": label,
                        }
                    )
                else:
                    wave_barriers[label] = barrier
            barriers[str(wave_index + 1)] = wave_barriers
        reverse_edges = 0
        for earlier_index, earlier_members in enumerate(wave_members):
            for later_members in wave_members[earlier_index + 1 :]:
                later_mask = sum(1 << index for index in later_members)
                reverse_edges += sum(
                    bool(ancestors[index] & later_mask)
                    for index in earlier_members
                )
        capacity_safe = proof is not None and _capacity_seven_safe(proof)
        dominance = not shortcuts and not reverse_edges
        status = "proved" if dominance and capacity_safe else "disproved"
        evaluated.append(
            {
                **base,
                "wave_schema_id": schema_id,
                "wave_schema_version": 1,
                "solver_status": status,
                "wave_count": len(schema),
                "wave_assignment": {
                    str(index + 1): {
                        "types": list(wave),
                        "tile_indices": list(wave_members[index]),
                    }
                    for index, wave in enumerate(schema)
                },
                "wave_dependency_dag": [
                    {"from_wave": index, "to_wave": index + 1}
                    for index in range(1, len(schema))
                ],
                "dominance_chain": dominance,
                "barrier_tiles": barriers,
                "shortcut_count": len(shortcuts),
                "shortcut_witnesses": shortcuts,
                "reverse_dependency": bool(reverse_edges),
                "reverse_dependency_count": reverse_edges,
                "capacity_safe_exits": capacity_safe,
                "capacity_proof_scope": (
                    "one exact capacity-7 safe exit per wave, composed at "
                    "empty-bag wave boundaries"
                ),
                "reasons": []
                if status == "proved"
                else [
                    reason
                    for reason, failed in (
                        (
                            "wave_completion_does_not_dominate_later_triplet",
                            bool(shortcuts),
                        ),
                        ("reverse_wave_dependency", bool(reverse_edges)),
                        ("capacity_safe_wave_exit_missing", not capacity_safe),
                    )
                    if failed
                ],
            }
        )
    selected = next(
        (contract for contract in evaluated if contract["solver_status"] == "proved"),
        min(
            evaluated,
            key=lambda contract: (
                int(contract["shortcut_count"]),
                int(contract["reverse_dependency_count"]),
                int(contract["wave_count"]),
            ),
        ),
    )
    return {
        **selected,
        "evaluated_wave_schema_ids": [
            str(contract["wave_schema_id"]) for contract in evaluated
        ],
    }


def analyze_long_range_assignment(
    blocker_masks: Sequence[int],
    tile_types: Sequence[str],
    *,
    threshold_type: str,
    target_step: int,
    limits: SolverLimits,
    required_pre_gate_recycles: int = 0,
    first_recycle_lead_tiles: int = 2,
    preferred_wave_count: int | None = None,
) -> tuple[PuzzleProof, dict[str, object]]:
    """Independently re-prove a fixed 36-tile all-types assignment."""

    masks = tuple(int(value) for value in blocker_masks)
    types = tuple(str(value).upper() for value in tile_types)
    if (
        len(types) != 36
        or set(types) != set("CDEFGHIJKLMN")
        or any(types.count(label) != 3 for label in "CDEFGHIJKLMN")
    ):
        raise InputValidationError("all_types_long_range 必须是 12 类×3 个")
    failures: list[dict[str, object]] = []
    for schema_id, schema in _wave_schema_candidates(preferred_wave_count):
        if threshold_type not in schema[0]:
            failures.append(
                {
                    "wave_schema_id": schema_id,
                    "reasons": ["threshold_type_outside_first_wave"],
                }
            )
            continue
        wave_members = tuple(
            tuple(index for index, label in enumerate(types) if label in wave)
            for wave in schema
        )
        wave_proofs: list[PuzzleProof] = []
        schema_failure: InputValidationError | ProofLimitExceeded | None = None
        for wave_index, members in enumerate(wave_members):
            ordered = tuple(sorted(members))
            local_types = tuple(types[index] for index in ordered)
            local_gate = (
                threshold_type if wave_index == 0 else schema[wave_index][0]
            )
            try:
                proof = analyze_puzzle(
                    _local_masks(masks, ordered),
                    local_types,
                    threshold_type=local_gate,
                    limits=limits,
                    required_pre_gate_recycles=(
                        required_pre_gate_recycles if wave_index == 0 else 0
                    ),
                    first_recycle_lead_tiles=first_recycle_lead_tiles,
                )
                if wave_index == 0:
                    valid, reasons = validate_target(
                        proof,
                        target_step,
                        required_pre_gate_recycles=required_pre_gate_recycles,
                        first_recycle_lead_tiles=first_recycle_lead_tiles,
                    )
                    if not valid:
                        raise InputValidationError(
                            "长程预设第 1 波不满足目标门槛",
                            evidence={"reasons": list(reasons)},
                        )
                if not _capacity_seven_safe(proof):
                    raise InputValidationError(
                        "长程预设存在无 7 格安全退出的波次"
                    )
            except (InputValidationError, ProofLimitExceeded) as error:
                schema_failure = error
                break
            wave_proofs.append(proof)
        if schema_failure is not None:
            failures.append(
                {
                    "wave_schema_id": schema_id,
                    "reasons": [str(schema_failure)],
                }
            )
            continue
        combined = _assemble_wave_proof(
            masks,
            types,
            threshold_type,
            wave_members,
            wave_proofs,
            limits,
        )
        contract = all_types_wave_contract(
            masks,
            types,
            combined,
            preferred_wave_count=len(schema),
        )
        if contract["solver_status"] == "proved":
            return combined, contract
        failures.append(
            {
                "wave_schema_id": schema_id,
                "reasons": list(contract.get("reasons", ())),
            }
        )
    raise InputValidationError(
        "长程预设未证明 3/4 波支配关系",
        evidence={"schema_failures": failures},
    )


def find_long_range_assignment(
    blocker_masks: Sequence[int],
    labels: Sequence[str],
    *,
    target_step: int,
    rng: random.Random,
    assignment_attempts: int,
    limits: SolverLimits,
    required_pre_gate_recycles: int = 0,
    first_recycle_lead_tiles: int = 2,
    preferred_wave_count: int | None = None,
) -> tuple[tuple[str, ...], str, PuzzleProof, dict[str, object]] | None:
    """Generate a fixed-schema 3/4-wave assignment and prove it exactly."""

    masks = tuple(int(value) for value in blocker_masks)
    normalized_labels = tuple(str(value).upper() for value in labels)
    if len(masks) != 36 or set(normalized_labels) != set("CDEFGHIJKLMN"):
        return None
    ancestors = _transitive_ancestor_masks(masks)
    schemas = _wave_schema_candidates(preferred_wave_count)
    attempts_per_schema = max(1, assignment_attempts // len(schemas))
    for _schema_id, schema in schemas:
        wave_size = 36 // len(schema)
        for _ in range(attempts_per_schema):
            order = _random_topological_order(masks, rng)
            if not order:
                return None
            wave_members = tuple(
                tuple(order[offset : offset + wave_size])
                for offset in range(0, 36, wave_size)
            )
            assigned = [""] * 36
            possible = True
            for wave_index, (wave_labels, members) in enumerate(
                zip(schema, wave_members)
            ):
                available_members = list(members)
                rng.shuffle(available_members)
                if wave_index:
                    previous_mask = sum(
                        1 << index for index in wave_members[wave_index - 1]
                    )
                    barriers = [
                        index
                        for index in available_members
                        if ancestors[index] & previous_mask == previous_mask
                    ]
                    if len(barriers) < len(wave_labels):
                        possible = False
                        break
                    rng.shuffle(barriers)
                    chosen_barriers = barriers[: len(wave_labels)]
                else:
                    chosen_barriers = available_members[: len(wave_labels)]
                for label, index in zip(wave_labels, chosen_barriers):
                    assigned[index] = label
                remaining = [index for index in available_members if not assigned[index]]
                remaining_labels = [label for label in wave_labels for _ in range(2)]
                rng.shuffle(remaining_labels)
                for index, label in zip(remaining, remaining_labels):
                    assigned[index] = label
            if not possible or any(not value for value in assigned):
                continue
            first_wave_labels = schema[0]
            gates = list(first_wave_labels)
            rng.shuffle(gates)
            for gate in gates:
                try:
                    proof, contract = analyze_long_range_assignment(
                        masks,
                        assigned,
                        threshold_type=gate,
                        target_step=target_step,
                        limits=limits,
                        required_pre_gate_recycles=required_pre_gate_recycles,
                        first_recycle_lead_tiles=first_recycle_lead_tiles,
                        preferred_wave_count=len(schema),
                    )
                except (InputValidationError, ProofLimitExceeded):
                    continue
                return tuple(assigned), gate, proof, contract
    return None


def serial_mechanism_contract(
    graph: GeometryGraph,
    units: Sequence[object],
) -> dict[str, object]:
    """Prove a two-cluster serial mechanism via a conservative ancestor test."""

    base: dict[str, object] = {
        "grammar_version": SPECIAL_GRAMMAR_VERSION,
        "proof_method": "conservative_static_ancestor_sufficient_condition",
        "entry_event": {
            "id": "cluster_first_triplet_completed",
            "predicate": (
                "any structure type assigned to the cluster has completed its "
                "three-tile match"
            ),
        },
        "completion_event": {
            "id": "cluster_all_structures_completed",
            "predicate": "all C-N tiles assigned to the cluster have been removed",
        },
        "proof_scope": (
            "every topological structure-removal order; legal game paths are "
            "a subset because bag constraints can only remove transitions"
        ),
    }
    if len(units) != 2 or tuple(str(getattr(unit, "role", "")) for unit in units) != (
        "high",
        "low",
    ):
        return {
            **base,
            "solver_status": "disproved",
            "cluster_count": len(units),
            "dominance_chain": False,
            "shortcut_count": None,
            "capacity_safe_segments": False,
            "reverse_dependency": None,
            "reasons": ["z_layout_high_low_units_required"],
        }
    high, low = units
    high_indices = tuple(int(value) for value in getattr(high, "selected_indices"))
    low_indices = tuple(int(value) for value in getattr(low, "selected_indices"))
    high_mask = frozenset(high_indices)
    low_mask = frozenset(low_indices)
    low_types = tuple(str(value) for value in getattr(low, "assigned_types"))
    by_type: dict[str, list[int]] = {}
    for index, label in zip(low_indices, low_types):
        by_type.setdefault(label, []).append(index)
    barriers: dict[str, int] = {}
    shortcuts: list[str] = []
    for label, members in sorted(by_type.items()):
        barrier = next(
            (
                index
                for index in members
                if high_mask.issubset(graph.transitive_blockers[index])
            ),
            None,
        )
        if barrier is None:
            shortcuts.append(label)
        else:
            barriers[label] = barrier
    reverse_count = sum(
        bool(graph.transitive_blockers[index].intersection(low_mask))
        for index in high_indices
    )
    capacity_safe = all(
        _capacity_seven_safe(getattr(unit, "proof")) for unit in units
    )
    dominance = not shortcuts and not reverse_count
    status = "proved" if dominance and capacity_safe else "disproved"
    return {
        **base,
        "solver_status": status,
        "cluster_count": 2,
        "cluster_order": [
            str(getattr(high, "unit_id")),
            str(getattr(low, "unit_id")),
        ],
        "cluster_assignment": {
            str(getattr(high, "unit_id")): list(high_indices),
            str(getattr(low, "unit_id")): list(low_indices),
        },
        "cluster_dependency_dag": [
            {
                "from_cluster": str(getattr(high, "unit_id")),
                "to_cluster": str(getattr(low, "unit_id")),
            }
        ],
        "dominance_chain": dominance,
        "barrier_tiles": barriers,
        "shortcut_count": len(shortcuts),
        "shortcut_types": shortcuts,
        "capacity_safe_segments": capacity_safe,
        "capacity_proof_scope": "one exact capacity-7 safe path per cluster",
        "reverse_dependency": bool(reverse_count),
        "reverse_dependency_count": reverse_count,
        "reasons": []
        if status == "proved"
        else [
            reason
            for reason, failed in (
                ("prior_cluster_completion_does_not_dominate_later_triplet", bool(shortcuts)),
                ("reverse_cluster_dependency", bool(reverse_count)),
                ("capacity_safe_segment_missing", not capacity_safe),
            )
            if failed
        ],
    }


def candidate_special_preset_evidence(
    graph: GeometryGraph,
    candidate: object,
    *,
    preferred_wave_count: int | None = None,
) -> dict[str, object]:
    """Derive evidence from immutable candidate members; never trust UI input."""

    units = tuple(getattr(candidate, "units", ()))
    result: dict[str, object] = {}
    if len(units) == 1 and len(getattr(units[0], "assigned_types", ())) == 36:
        unit = units[0]
        contract = all_types_wave_contract(
            getattr(unit, "blocker_masks"),
            getattr(unit, "assigned_types"),
            getattr(unit, "proof"),
            preferred_wave_count=preferred_wave_count,
        )
        selected = tuple(getattr(unit, "selected_indices"))
        wave_assignment = contract.get("wave_assignment")
        if isinstance(wave_assignment, Mapping):
            contract["wave_assignment"] = {
                str(wave): {
                    **dict(payload),
                    "tile_indices": [
                        selected[int(index)]
                        for index in payload.get("tile_indices", ())
                    ],
                }
                for wave, payload in wave_assignment.items()
                if isinstance(payload, Mapping)
            }
        barriers = contract.get("barrier_tiles")
        if isinstance(barriers, Mapping):
            contract["barrier_tiles"] = {
                str(wave): {
                    str(label): selected[int(index)]
                    for label, index in payload.items()
                }
                for wave, payload in barriers.items()
                if isinstance(payload, Mapping)
            }
        contract["tile_index_scope"] = "source_template_indices"
        result["all_types_long_range"] = contract
    if len(units) == 2:
        result["serial_mechanism"] = serial_mechanism_contract(graph, units)
    return result


__all__ = [
    "ALL_TYPES_WAVE_SCHEMAS",
    "SPECIAL_GRAMMAR_VERSION",
    "SUSPENDED_EXPOSURE_BOUND_VERSION",
    "all_types_wave_contract",
    "analyze_long_range_assignment",
    "candidate_special_preset_evidence",
    "find_long_range_assignment",
    "serial_mechanism_contract",
]
