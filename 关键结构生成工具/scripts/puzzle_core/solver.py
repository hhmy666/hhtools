"""Exact puzzle-unit solver and bag state simulator.

The business difficulty metric is the settled bag pressure immediately after the
gate tile enters the bag and before its triple is removed.  It is deliberately
kept separate from the click number of the first literal match.
"""

from __future__ import annotations

from collections import Counter, deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from itertools import combinations
from typing import Iterable, Sequence

from .errors import InputValidationError, ProofLimitExceeded, CandidateValidationError


STRUCTURE_TYPES = tuple(chr(code) for code in range(ord("C"), ord("N") + 1))

_proof_checkpoint = ContextVar('proof_checkpoint', default=None)


@contextmanager
def proof_checkpoints(callback):
    token = _proof_checkpoint.set(callback)
    try:
        yield
    finally:
        _proof_checkpoint.reset(token)


def _poll_proof():
    callback = _proof_checkpoint.get()
    if callback is not None:
        callback('assignment_attempt')


@dataclass(frozen=True)
class SolverLimits:
    max_states: int = 1_000_000
    max_path_count: int = 10**15


@dataclass(frozen=True)
class StepTrace:
    click_number: int
    tile_index: int
    tile_type: str
    bag_before: tuple[str, ...]
    bag_after_insert: tuple[str, ...]
    matched_type: str | None
    bag_after_settlement: tuple[str, ...]
    instantaneous_size: int
    settled_size: int
    gate_structure_step: int | None
    failed: bool


@dataclass(frozen=True)
class PathSimulation:
    capacity: int | None
    complete: bool
    failed_at: int | None
    gate_reached: bool
    gate_structure_step: int | None
    literal_first_match_click: int | None
    instantaneous_peak: int
    settled_peak: int
    remaining_bag: tuple[str, ...]
    steps: tuple[StepTrace, ...]

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["steps"] = [asdict(step) for step in self.steps]
        return result


@dataclass(frozen=True)
class CapacityProof:
    capacity: int
    states_explored: int
    complete_path_count: int
    path_count_capped: bool
    complete_exists: bool
    failure_reachable: bool
    minimum_failure_click: int | None
    gate_reachable: bool
    classification: str
    instantaneous_peak: int
    settled_peak: int
    recommended_order: tuple[int, ...] | None
    shortest_failure_order: tuple[int, ...] | None


@dataclass(frozen=True)
class PuzzleProof:
    tile_types: tuple[str, ...]
    blocker_masks: tuple[int, ...]
    threshold_type: str
    states_explored: int
    theoretical_complete_path_count: int
    path_count_capped: bool
    threshold_steps: tuple[int, ...]
    literal_first_match_clicks: tuple[int, ...]
    singleton_rule_ok: bool
    singleton_violation_order: tuple[int, ...] | None
    singleton_violation_types: tuple[str, ...]
    recommended_order: tuple[int, ...]
    required_pre_gate_recycles: int
    first_recycle_lead_tiles: int
    recommended_pre_gate_recycles: int
    recommended_first_match_click: int | None
    recommended_gate_click: int | None
    recommended_path_constraints_ok: bool
    capacity_proofs: tuple[CapacityProof, ...]

    @property
    def minimum_structure_step(self) -> int:
        return min(self.threshold_steps)

    @property
    def maximum_structure_step(self) -> int:
        return max(self.threshold_steps)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _bag_as_tiles(counts: Sequence[int], labels: Sequence[str]) -> tuple[str, ...]:
    return tuple(label for label, count in zip(labels, counts) for _ in range(count))


def _validate_graph(blocker_masks: Sequence[int], tile_types: Sequence[str]) -> None:
    if not tile_types:
        raise InputValidationError("谜题单元不能为空")
    if len(blocker_masks) != len(tile_types):
        raise InputValidationError("blocker_masks 与 tile_types 数量不一致")
    n = len(tile_types)
    allowed_mask = (1 << n) - 1
    for index, mask in enumerate(blocker_masks):
        if not isinstance(mask, int) or mask < 0 or mask & ~allowed_mask:
            raise InputValidationError(
                "遮挡掩码包含单元外索引", evidence={"tile_index": index, "mask": mask}
            )
        if mask & (1 << index):
            raise InputValidationError(
                "Tile 不能遮挡自身", evidence={"tile_index": index}
            )
    invalid = sorted(set(tile_types) - set(STRUCTURE_TYPES))
    if invalid:
        raise InputValidationError(
            "结构类型只能使用 C–N", evidence={"invalid_types": invalid}
        )
    bad_counts = {key: value for key, value in Counter(tile_types).items() if value % 3}
    if bad_counts:
        raise InputValidationError(
            "每种结构块数量必须是 3 的正整数倍", evidence={"counts": bad_counts}
        )
    removed = 0
    while removed != allowed_mask:
        available = [
            i
            for i, mask in enumerate(blocker_masks)
            if not (removed >> i) & 1 and not (mask & ~removed)
        ]
        if not available:
            raise InputValidationError("谜题单元遮挡关系存在环")
        for index in available:
            removed |= 1 << index


def available_indices(removed_mask: int, blocker_masks: Sequence[int]) -> tuple[int, ...]:
    remaining = ((1 << len(blocker_masks)) - 1) & ~removed_mask
    available = []
    while remaining:
        bit = remaining & -remaining
        remaining ^= bit
        index = bit.bit_length() - 1
        if not (blocker_masks[index] & ~removed_mask):
            available.append(index)
    return tuple(available)


@lru_cache(maxsize=65536)
def _click_counts(
    bag: tuple[int, ...], type_index: int, capacity: int | None
) -> tuple[tuple[int, ...], bool, int, int, bool]:
    changed = list(bag)
    changed[type_index] += 1
    instantaneous_size = sum(changed)
    matched = changed[type_index] == 3
    if matched:
        changed[type_index] = 0
    settled_size = sum(changed)
    failed = capacity is not None and settled_size >= capacity
    return tuple(changed), matched, instantaneous_size, settled_size, failed


def simulate_order(
    order: Sequence[int],
    tile_types: Sequence[str],
    *,
    threshold_type: str,
    capacity: int | None,
) -> PathSimulation:
    """Simulate a concrete order using the game's match-before-full rule."""

    labels = tuple(sorted(set(tile_types)))
    label_to_index = {label: index for index, label in enumerate(labels)}
    bag = (0,) * len(labels)
    traces: list[StepTrace] = []
    failed_at = None
    gate_step = None
    literal_first = None
    instantaneous_peak = 0
    settled_peak = 0
    for click_number, tile_index in enumerate(order, 1):
        if tile_index < 0 or tile_index >= len(tile_types):
            raise InputValidationError(
                "点击序列包含无效 Tile 索引", evidence={"tile_index": tile_index}
            )
        tile_type = tile_types[tile_index]
        before = _bag_as_tiles(bag, labels)
        changed = list(bag)
        changed[label_to_index[tile_type]] += 1
        after_insert = _bag_as_tiles(changed, labels)
        bag, matched, instant, settled, failed = _click_counts(
            bag, label_to_index[tile_type], capacity
        )
        matched_type = tile_type if matched else None
        current_gate = None
        if matched and literal_first is None:
            literal_first = click_number
        if matched and tile_type == threshold_type and gate_step is None:
            gate_step = instant
            current_gate = instant
        instantaneous_peak = max(instantaneous_peak, instant)
        settled_peak = max(settled_peak, settled)
        traces.append(
            StepTrace(
                click_number=click_number,
                tile_index=tile_index,
                tile_type=tile_type,
                bag_before=before,
                bag_after_insert=after_insert,
                matched_type=matched_type,
                bag_after_settlement=_bag_as_tiles(bag, labels),
                instantaneous_size=instant,
                settled_size=settled,
                gate_structure_step=current_gate,
                failed=failed,
            )
        )
        if failed:
            failed_at = click_number
            break
    complete = failed_at is None and len(traces) == len(tile_types) and not any(bag)
    return PathSimulation(
        capacity=capacity,
        complete=complete,
        failed_at=failed_at,
        gate_reached=gate_step is not None,
        gate_structure_step=gate_step,
        literal_first_match_click=literal_first,
        instantaneous_peak=instantaneous_peak,
        settled_peak=settled_peak,
        remaining_bag=_bag_as_tiles(bag, labels),
        steps=tuple(traces),
    )


class _StateBudget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.seen: set[tuple[int, tuple[int, ...]]] = set()

    def record(self, state: tuple[int, tuple[int, ...]]) -> None:
        previous_size = len(self.seen)
        self.seen.add(state)
        if len(self.seen) != previous_size and len(self.seen) % 512 == 0:
            _poll_proof()
        if len(self.seen) > self.limit:
            raise ProofLimitExceeded(
                "精确求解状态数超过上限，不能把部分搜索标为已证明",
                evidence={"max_states": self.limit},
            )


def _theoretical_proof(
    blocker_masks: tuple[int, ...],
    tile_types: tuple[str, ...],
    threshold_type: str,
    limits: SolverLimits,
    required_pre_gate_recycles: int,
    first_recycle_lead_tiles: int,
) -> tuple[
    int,
    bool,
    tuple[int, ...],
    tuple[int, ...],
    tuple[int, ...],
    tuple[int, ...] | None,
    tuple[str, ...],
    int,
    bool,
]:
    n = len(tile_types)
    full = (1 << n) - 1
    labels = tuple(sorted(set(tile_types)))
    label_to_index = {label: index for index, label in enumerate(labels)}
    type_indices = tuple(label_to_index[label] for label in tile_types)
    gate_index = label_to_index[threshold_type]
    budget = _StateBudget(limits.max_states)

    def transitions(removed: int, bag: tuple[int, ...]):
        for index in available_indices(removed, blocker_masks):
            changed, matched, instant, _settled, _failed = _click_counts(
                bag, type_indices[index], None
            )
            yield index, removed | (1 << index), changed, matched, instant

    @lru_cache(None)
    def count_finish(removed: int, bag: tuple[int, ...]) -> tuple[int, bool]:
        budget.record((removed, bag))
        if removed == full:
            return (1, False) if not any(bag) else (0, False)
        total = 0
        capped = False
        for _index, next_removed, next_bag, _matched, _instant in transitions(removed, bag):
            child, child_capped = count_finish(next_removed, next_bag)
            total += child
            capped = capped or child_capped
            if total >= limits.max_path_count:
                total = limits.max_path_count
                capped = True
                break
        return total, capped

    @lru_cache(None)
    def threshold_values(removed: int, bag: tuple[int, ...], without_recycle: bool = False) -> frozenset[int]:
        budget.record((removed, bag))
        result: set[int] = set()
        for index, next_removed, next_bag, matched, instant in transitions(removed, bag):
            if matched and type_indices[index] == gate_index:
                if count_finish(next_removed, next_bag)[0]:
                    result.add(instant)
            else:
                if without_recycle and matched:
                    continue
                result.update(threshold_values(next_removed, next_bag, without_recycle))
        return frozenset(result)

    @lru_cache(None)
    def literal_values(removed: int, bag: tuple[int, ...]) -> frozenset[int]:
        budget.record((removed, bag))
        depth = bin(removed).count("1")
        result: set[int] = set()
        for _index, next_removed, next_bag, matched, _instant in transitions(removed, bag):
            if matched:
                if count_finish(next_removed, next_bag)[0]:
                    result.add(depth + 1)
            else:
                result.update(literal_values(next_removed, next_bag))
        return frozenset(result)

    @lru_cache(None)
    def find_order(
        removed: int,
        bag: tuple[int, ...],
        desired_gate: int,
        gate_seen: bool,
        pre_gate_recycles: int,
        first_match_click: int | None,
        enforce_recycle_constraints: bool,
        capacity: int | None,
    ) -> tuple[int, ...] | None:
        if removed == full:
            if any(bag) or not gate_seen:
                return None
            if (
                enforce_recycle_constraints
                and pre_gate_recycles != required_pre_gate_recycles
            ):
                return None
            return ()
        click_number = bin(removed).count("1") + 1
        for index, next_removed, next_bag, matched, instant in transitions(removed, bag):
            # The recommended order is the path shown in the UI, so it must
            # itself be safe for the capacity required by this target.  The
            # game settles a triple before checking capacity; ``next_bag`` is
            # therefore the correct post-settlement value to constrain.
            if capacity is not None and sum(next_bag) >= capacity:
                continue
            next_seen = gate_seen
            next_recycles = pre_gate_recycles
            next_first_match = first_match_click
            if matched and next_first_match is None:
                next_first_match = click_number
                if (
                    enforce_recycle_constraints
                    and required_pre_gate_recycles > 0
                    and click_number < 3 + first_recycle_lead_tiles
                ):
                    continue
            if matched and type_indices[index] == gate_index and not gate_seen:
                if instant != desired_gate:
                    continue
                if (
                    enforce_recycle_constraints
                    and next_recycles != required_pre_gate_recycles
                ):
                    continue
                next_seen = True
            elif matched and not gate_seen:
                next_recycles += 1
                if (
                    enforce_recycle_constraints
                    and next_recycles > required_pre_gate_recycles
                ):
                    continue
            if not count_finish(next_removed, next_bag)[0]:
                continue
            suffix = find_order(
                next_removed,
                next_bag,
                desired_gate,
                next_seen,
                next_recycles,
                next_first_match,
                enforce_recycle_constraints,
                capacity,
            )
            if suffix is not None:
                return (index,) + suffix
        return None

    # Retain a singleton witness for diagnostics only, not acceptance.
    start = (0, (0,) * len(labels))
    queue = deque([start]) if len(labels) >= 5 else deque()
    predecessors: dict[
        tuple[int, tuple[int, ...]],
        tuple[tuple[int, tuple[int, ...]], int] | None,
    ] = {start: None}
    violation_state = None
    violation_types: tuple[str, ...] = ()
    while queue:
        removed, bag = queue.popleft()
        budget.record((removed, bag))
        for index, next_removed, next_bag, _matched, _instant in transitions(removed, bag):
            next_state = (next_removed, next_bag)
            if next_state not in predecessors:
                predecessors[next_state] = ((removed, bag), index)
                singleton_types = tuple(
                    label for label, count in zip(labels, next_bag) if count == 1
                )
                if len(singleton_types) >= 5:
                    violation_state = next_state
                    violation_types = singleton_types
                    queue.clear()
                    break
                queue.append(next_state)
        if violation_state is not None:
            break
    violation_order = None
    if violation_state is not None:
        reversed_order: list[int] = []
        cursor = violation_state
        while predecessors[cursor] is not None:
            previous, index = predecessors[cursor]  # type: ignore[misc]
            reversed_order.append(index)
            cursor = previous
        violation_order = tuple(reversed(reversed_order))

    start_bag = (0,) * len(labels)
    path_count, path_count_capped = count_finish(0, start_bag)
    if not path_count:
        raise InputValidationError("结构在无限背包下也没有完整解")
    permitted_thresholds = (threshold_values(0, start_bag, True)
                            if required_pre_gate_recycles == 0 else frozenset())
    # A longer route that first recycles another type may have a smaller bag
    # at the gate. It is not a shorter zero-recycle solution. Retain the raw
    # thresholds only when no constrained route exists, for invalid diagnostics.
    threshold = tuple(sorted(permitted_thresholds or threshold_values(0, start_bag)))
    literal = tuple(sorted(literal_values(0, start_bag)))
    if not threshold:
        raise InputValidationError("完整解中门槛块没有形成三消")
    desired = min(threshold)
    # s4-s7 must expose a 7-slot-safe recommendation; s8 must expose an
    # 8-slot-safe recommendation.  s9 deliberately proves pressure beyond
    # both capacities, so its gate trace remains capacity-free.
    recommended_capacity = 7 if desired <= 7 else (8 if desired == 8 else None)
    recommended = find_order(
        0,
        start_bag,
        desired,
        False,
        0,
        None,
        True,
        recommended_capacity,
    )
    recycle_constraints_ok = recommended is not None
    if recommended is None:
        recommended = find_order(
            0,
            start_bag,
            desired,
            False,
            0,
            None,
            False,
            recommended_capacity,
        )
    # Invalid candidates still need a diagnostic path.  If no safe
    # reconstruction exists, retain an unrestricted one without claiming that
    # the requested recommendation constraints were satisfied.
    if recommended is None:
        recommended = find_order(
            0,
            start_bag,
            desired,
            False,
            0,
            None,
            False,
            None,
        )
    if recommended is None:
        raise AssertionError("solver failed to reconstruct a proven complete path")
    return (
        path_count,
        path_count_capped,
        threshold,
        literal,
        recommended,
        violation_order,
        violation_types,
        len(budget.seen),
        recycle_constraints_ok,
    )


def _capacity_proof(
    blocker_masks: tuple[int, ...],
    tile_types: tuple[str, ...],
    threshold_type: str,
    capacity: int,
    limits: SolverLimits,
) -> CapacityProof:
    labels = tuple(sorted(set(tile_types)))
    label_to_index = {label: index for index, label in enumerate(labels)}
    type_indices = tuple(label_to_index[label] for label in tile_types)
    gate_index = label_to_index[threshold_type]
    full = (1 << len(tile_types)) - 1
    start = (0, (0,) * len(labels))
    queue = deque([start])
    predecessor: dict[
        tuple[int, tuple[int, ...]],
        tuple[tuple[int, tuple[int, ...]], int] | None,
    ] = {start: None}
    complete_states: list[tuple[int, tuple[int, ...]]] = []
    complete_path_count: dict[tuple[int, tuple[int, ...]], int] = {start: 1}
    path_count_capped = False
    failure_order: tuple[int, ...] | None = None
    failure_reachable = False
    minimum_failure_click = None
    gate_reachable = False
    instantaneous_peak = 0
    settled_peak = 0

    def reconstruct(state: tuple[int, tuple[int, ...]]) -> tuple[int, ...]:
        order: list[int] = []
        cursor = state
        while predecessor[cursor] is not None:
            previous, index = predecessor[cursor]  # type: ignore[misc]
            order.append(index)
            cursor = previous
        return tuple(reversed(order))

    processed = 0
    while queue:
        if processed % 256 == 0:
            _poll_proof()
        processed += 1
        state = queue.popleft()
        if len(predecessor) > limits.max_states:
            raise ProofLimitExceeded(
                "容量求解状态数超过上限，不能把部分搜索标为已证明",
                evidence={"capacity": capacity, "max_states": limits.max_states},
            )
        removed, bag = state
        if removed == full:
            if not any(bag):
                complete_states.append(state)
            continue
        for index in available_indices(removed, blocker_masks):
            next_bag, matched, instant, settled, failed = _click_counts(
                bag, type_indices[index], capacity
            )
            instantaneous_peak = max(instantaneous_peak, instant)
            settled_peak = max(settled_peak, settled)
            if matched and type_indices[index] == gate_index:
                gate_reachable = True
            if failed:
                failure_reachable = True
                fail_click = bin(removed).count("1") + 1
                if minimum_failure_click is None or fail_click < minimum_failure_click:
                    minimum_failure_click = fail_click
                    failure_order = reconstruct(state) + (index,)
                continue
            next_state = (removed | (1 << index), next_bag)
            incoming = complete_path_count[state]
            updated = complete_path_count.get(next_state, 0) + incoming
            if updated >= limits.max_path_count:
                updated = limits.max_path_count
                path_count_capped = True
            complete_path_count[next_state] = updated
            if next_state not in predecessor:
                predecessor[next_state] = (state, index)
                queue.append(next_state)

    total_complete = sum(complete_path_count[state] for state in complete_states)
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
    recommended_order = reconstruct(complete_states[0]) if complete_states else None
    return CapacityProof(
        capacity=capacity,
        states_explored=len(predecessor),
        complete_path_count=total_complete,
        path_count_capped=path_count_capped,
        complete_exists=complete_exists,
        failure_reachable=failure_reachable,
        minimum_failure_click=minimum_failure_click,
        gate_reachable=gate_reachable,
        classification=classification,
        instantaneous_peak=instantaneous_peak,
        settled_peak=settled_peak,
        recommended_order=recommended_order,
        shortest_failure_order=failure_order,
    )


@lru_cache(maxsize=128)
def proof_state_budget_exceeded(blocker_masks: tuple[int, ...], max_states: int) -> bool:
    """Prove budget exhaustion from geometry alone, never approximate acceptance.

    Each order ideal is a reachable removed-tile state. Unlimited-capacity
    proof visits every ideal, regardless of labels (bag counts are determined
    by removed tiles). Count ideals by including a pivot and its ancestors or
    excluding it and its descendants. A bounded diagnostic that cannot finish
    returns False and leaves the existing exact solver in charge.
    """
    n = len(blocker_masks)
    if n > 256:
        return False
    ancestors = [mask | (1 << i) for i, mask in enumerate(blocker_masks)]
    for k in range(n):
        for i in range(n):
            if ancestors[i] & (1 << k):
                ancestors[i] |= ancestors[k]
    descendants = [sum(1 << j for j in range(n) if ancestors[j] & (1 << i)) for i in range(n)]
    calls = 0

    @lru_cache(maxsize=None)
    def count(remaining: int) -> int:
        nonlocal calls
        calls += 1
        if calls % 512 == 0:
            _poll_proof()
        if calls > 20_000:
            raise TimeoutError
        if not remaining:
            return 1
        nodes = [i for i in range(n) if remaining & (1 << i)]
        if all(ancestors[i] & remaining == 1 << i for i in nodes):
            return min(max_states + 1, 1 << len(nodes))
        pivot = max(nodes, key=lambda i: min(bin(ancestors[i] & remaining).count("1"), bin(descendants[i] & remaining).count("1")))
        included = count(remaining & ~ancestors[pivot])
        if included > max_states:
            return max_states + 1
        return min(max_states + 1, included + count(remaining & ~descendants[pivot]))

    try:
        return count((1 << n) - 1) > max_states
    except TimeoutError:
        return False
    finally:
        count.cache_clear()


def has_direct_gate_triple(masks, types, gate, *, below_step=4, required_pre_gate_recycles=None):
    """A sufficient witness below the requested step (default: s3).

    The ancestor closure of three gate tiles is an ideal. If it contains
    exactly three gate tiles, remove a maximal gate tile last. The residues
    of all other types give its exact bag pressure. False remains unknown.
    """
    ancestors = [mask | (1 << i) for i, mask in enumerate(masks)]
    for j in range(len(types)):
        for i in range(len(types)):
            if ancestors[i] & (1 << j):
                ancestors[i] |= ancestors[j]
    label_masks = {label: sum(1 << i for i, value in enumerate(types) if value == label) for label in set(types)}
    for triple in combinations((i for i, value in enumerate(types) if value == gate), 3):
        closure = ancestors[triple[0]] | ancestors[triple[1]] | ancestors[triple[2]]
        if bin(closure & label_masks[gate]).count('1') != 3:
            continue
        if required_pre_gate_recycles == 0 and any(
            bin(closure & mask).count('1') >= 3
            for label, mask in label_masks.items() if label != gate
        ):
            continue
        if 3 + sum(bin(closure & mask).count('1') % 3 for label, mask in label_masks.items() if label != gate) < below_step:
            return True
    return False


def has_five_singleton_witness(masks, types):
    """Diagnostic reachability probe, never an acceptance/rejection rule.

    Walk real topological paths, preferring a previously empty bag type.
    A positive result is a concrete reachable five-singleton state. A miss
    is unknown. Neither result says whether an optimal solution is valid.
    """
    labels = tuple(sorted(set(types)))
    if len(labels) < 5:
        return False
    indices = tuple(labels.index(label) for label in types)
    for offset in range(len(labels)):
        removed = 0
        residues = [0] * len(labels)
        for _ in types:
            available = available_indices(removed, masks)
            if not available:
                break
            index = min(available, key=lambda i: (
                (0, 2, 1)[residues[indices[i]]],
                (indices[i] - offset) % len(labels), i,
            ))
            removed |= 1 << index
            label = indices[index]
            residues[label] = (residues[label] + 1) % 3
            if residues.count(1) >= 5:
                return True
    return False


@lru_cache(maxsize=65536)
def _merge_residues(a, b, bounded):
    values = tuple(x + y for x, y in zip(a, b))
    if any(value > 2 and bounded & (1 << i) for i, value in enumerate(values)):
        return None
    return tuple(value if bounded & (1 << i) else value % 3 for i, value in enumerate(values))


def _analyze_small_alphabet(masks, types, gate, capacities, limits, first_recycle_lead_tiles, reject_invalid_target=None):
    """Exact order-ideal DP; return None when capacity needs path search.

    Every ideal extends to a complete topological order; total type counts
    are divisible by three. Store residues (or counts <=2 before a match),
    together with a witness, instead of enumerating all removed-tile sets.
    This identity holds for every supported alphabet size. The residue
    domain grows as 3**k, so alphabets above the legacy five-type boundary
    also charge each convolution to the proof budget, including cache hits.
    No sampling or difficulty/validation relaxation is involved. Capacity
    cases not proved universally safe still fall back to exact path search.
    """
    n = len(types)
    full = (1 << n) - 1
    labels = tuple(sorted(set(types)))
    k = len(labels)
    gate_index = labels.index(gate)
    ancestors = [mask | (1 << i) for i, mask in enumerate(masks)]
    for j in range(n):
        for i in range(n):
            if ancestors[i] & (1 << j):
                ancestors[i] |= ancestors[j]
    descendants = [sum(1 << j for j in range(n) if ancestors[j] & (1 << i)) for i in range(n)]
    label_masks = tuple(sum(1 << i for i, value in enumerate(types) if value == label) for label in labels)
    work = 0

    def tick(amount=1):
        nonlocal work
        previous = work
        work += amount
        if previous // 128 != work // 128:
            _poll_proof()
        if work > limits.max_states:
            raise ProofLimitExceeded('压缩精确求解状态数超过上限', evidence={'max_states': limits.max_states})

    def budgeted_add(a, b, bounded):
        # Counting only ideal subproblems misses the Cartesian product inside
        # each convolution. Charge before allocation/merge and poll through
        # tick(), so large alphabets cannot evade either cancellation or the
        # existing work ceiling. Preserve legacy <=5 proof accounting.
        tick()
        return _merge_residues(a, b, bounded)

    add = budgeted_add if k > 5 else _merge_residues
    residue_domain_size = 3 ** k

    @lru_cache(maxsize=8192)
    def counts(bits):
        return tuple(bin(bits & mask).count('1') for mask in label_masks)

    @lru_cache(None)
    def ideals(remaining, bounded):
        tick()
        if not remaining:
            return {(0,) * k: 0}
        # Independent components combine by a small residue convolution,
        # rather than expanding the Cartesian product of their tile subsets.
        component = remaining & -remaining
        frontier = component
        while frontier:
            bit = frontier & -frontier
            frontier ^= bit
            index = bit.bit_length() - 1
            added = (ancestors[index] | descendants[index]) & remaining & ~component
            component |= added
            frontier |= added
        if component != remaining:
            result = {}
            for a, wa in ideals(component, bounded).items():
                for b, wb in ideals(remaining & ~component, bounded).items():
                    merged = add(a, b, bounded)
                    if merged is not None:
                        result.setdefault(merged, wa | wb)
                        if len(result) == residue_domain_size:
                            return result
            return result
        nodes = [i for i in range(n) if remaining & (1 << i)]
        pivot = max(nodes, key=lambda i: min(bin(ancestors[i] & remaining).count('1'),
                                             bin(descendants[i] & remaining).count('1')))
        included = ancestors[pivot] & remaining
        excluded = ideals(remaining & ~descendants[pivot], bounded)
        if len(excluded) == residue_domain_size:
            return excluded
        fixed = counts(included)
        if any(fixed[i] > 2 for i in range(k) if bounded & (1 << i)):
            return excluded
        # Residue maps are read-only after a subproblem returns. Reuse them
        # above when no merge is needed; otherwise bound copied entries too.
        if k > 5:
            tick(len(excluded))
        result = dict(excluded)
        for values, witness in ideals(remaining & ~included, bounded).items():
            merged = add(values, fixed, bounded)
            if merged is not None:
                result.setdefault(merged, witness | included)
                if len(result) == residue_domain_size:
                    return result
        return result

    def before_click(index, bounded):
        included = ancestors[index] & ~(1 << index)
        remaining = full & ~included & ~descendants[index]
        fixed = counts(included)
        if any(fixed[i] > 2 for i in range(k) if bounded & (1 << i)):
            return
        for values, witness in ideals(remaining, bounded).items():
            merged = add(values, fixed, bounded)
            if merged is not None:
                yield merged, witness | included

    residues = ideals(full, 0)
    peak = max(map(sum, residues))
    violation = next(((values, witness) for values, witness in residues.items()
                      if sum(value == 1 for value in values) >= 5), None)
    # Singleton states describe possible mistakes, not the optimal solution.
    # Keep their witness as diagnostics; never reject a candidate for them.
    threshold_witnesses = {}
    constrained_witnesses = {}
    literal = set()
    for index, label in enumerate(types):
        label_index = labels.index(label)
        if label == gate:
            for values, witness in before_click(index, 1 << gate_index):
                if values[gate_index] == 2:
                    threshold_witnesses.setdefault(sum(values) + 1, (witness, index))
        for values, witness in before_click(index, (1 << k) - 1):
            if values[label_index] == 2:
                step = sum(values) + 1
                literal.add(step)
                if label == gate:
                    constrained_witnesses.setdefault(step, (witness, index))
    if constrained_witnesses:
        threshold_witnesses = constrained_witnesses
    desired = min(threshold_witnesses)
    if reject_invalid_target is not None and desired != reject_invalid_target:
        ideals.cache_clear()
        counts.cache_clear()
        raise CandidateValidationError('已证明门槛步数不匹配，跳过其余验证', evidence={'reason': 'gate_step_mismatch'})
    recommended_capacity = 7 if desired <= 7 else (8 if desired == 8 else None)
    if (any(capacity <= peak for capacity in capacities)
            or (k > 5 and recommended_capacity is not None
                and recommended_capacity <= peak)):
        ideals.cache_clear()
        counts.cache_clear()
        return None
    constraints_ok = desired in constrained_witnesses
    prefix, gate_tile = (constrained_witnesses if constraints_ok else threshold_witnesses)[desired]

    def extend(removed, target):
        order = []
        while removed != target:
            index = next(i for i in available_indices(removed, masks) if target & (1 << i))
            order.append(index)
            removed |= 1 << index
        return tuple(order)

    recommended = extend(0, prefix) + (gate_tile,) + extend(prefix | (1 << gate_tile), full)
    simulation = simulate_order(recommended, types, threshold_type=gate, capacity=None)
    gate_click = next(step.click_number for step in simulation.steps if step.gate_structure_step is not None)
    pre_recycles = sum(step.matched_type is not None and step.click_number < gate_click for step in simulation.steps)

    @lru_cache(None)
    def paths(removed):
        tick()
        if removed == full:
            return 1
        total = 0
        for index in available_indices(removed, masks):
            total += paths(removed | (1 << index))
            if total >= limits.max_path_count:
                return limits.max_path_count
        return total

    path_count = paths(0)
    capped = path_count >= limits.max_path_count
    # Every reachable ideal has pressure below capacity, so all paths are safe.
    capacity_proofs = tuple(CapacityProof(
        capacity=capacity, states_explored=work, complete_path_count=path_count,
        path_count_capped=capped, complete_exists=True, failure_reachable=False,
        minimum_failure_click=None, gate_reachable=True, classification='all_safe',
        instantaneous_peak=peak + 1, settled_peak=peak,
        recommended_order=recommended, shortest_failure_order=None,
    ) for capacity in capacities)
    result = PuzzleProof(
        tile_types=types, blocker_masks=masks, threshold_type=gate, states_explored=work,
        theoretical_complete_path_count=path_count, path_count_capped=capped,
        threshold_steps=tuple(sorted(threshold_witnesses)), literal_first_match_clicks=tuple(sorted(literal)),
        singleton_rule_ok=violation is None,
        singleton_violation_order=None if violation is None else extend(0, violation[1]),
        singleton_violation_types=() if violation is None else tuple(label for label, value in zip(labels, violation[0]) if value == 1),
        recommended_order=recommended, required_pre_gate_recycles=0,
        first_recycle_lead_tiles=first_recycle_lead_tiles, recommended_pre_gate_recycles=pre_recycles,
        recommended_first_match_click=simulation.literal_first_match_click,
        recommended_gate_click=gate_click, recommended_path_constraints_ok=constraints_ok,
        capacity_proofs=capacity_proofs,
    )
    ideals.cache_clear()
    paths.cache_clear()
    counts.cache_clear()
    return result


def analyze_puzzle(
    blocker_masks: Sequence[int],
    tile_types: Sequence[str],
    *,
    threshold_type: str,
    capacities: Iterable[int] = (7, 8),
    limits: SolverLimits | None = None,
    required_pre_gate_recycles: int = 0,
    first_recycle_lead_tiles: int = 2,
    reject_invalid_target: int | None = None,
) -> PuzzleProof:
    """Exhaustively prove a single static puzzle unit.

    A result is returned only after the complete state space fits within the
    configured proof budget.  Budget exhaustion raises instead of silently
    downgrading a partial search to a proof.
    """

    masks = tuple(int(mask) for mask in blocker_masks)
    types = tuple(str(value).upper() for value in tile_types)
    gate = str(threshold_type).upper()
    _validate_graph(masks, types)
    if gate not in set(types):
        raise InputValidationError("门槛块类型不在谜题单元中")
    if (
        isinstance(required_pre_gate_recycles, bool)
        or not isinstance(required_pre_gate_recycles, int)
        or required_pre_gate_recycles < 0
    ):
        raise InputValidationError("门槛前回收次数必须是非负整数")
    if (
        isinstance(first_recycle_lead_tiles, bool)
        or not isinstance(first_recycle_lead_tiles, int)
        or first_recycle_lead_tiles < 0
    ):
        raise InputValidationError("首次回收前铺垫块数必须是非负整数")
    actual_limits = limits or SolverLimits()
    capacities = tuple(int(value) for value in capacities)
    if (len(types) > 24 and len(set(types)) <= len(STRUCTURE_TYPES)
            and required_pre_gate_recycles == 0):
        compressed = _analyze_small_alphabet(masks, types, gate, capacities, actual_limits, first_recycle_lead_tiles, reject_invalid_target)
        if compressed is not None:
            return compressed
    if proof_state_budget_exceeded(masks, actual_limits.max_states):
        raise ProofLimitExceeded(
            "当前结构几何的必需证明状态数超过上限",
            evidence={"max_states": actual_limits.max_states, "geometry_preflight": True},
        )
    (
        path_count,
        count_capped,
        threshold_steps,
        literal_steps,
        recommended,
        violation_order,
        violation_types,
        states_explored,
        recycle_constraints_ok,
    ) = _theoretical_proof(
        masks,
        types,
        gate,
        actual_limits,
        required_pre_gate_recycles,
        first_recycle_lead_tiles,
    )
    recommended_simulation = simulate_order(
        recommended,
        types,
        threshold_type=gate,
        capacity=None,
    )
    recommended_gate_click = next(
        (
            step.click_number
            for step in recommended_simulation.steps
            if step.gate_structure_step is not None
        ),
        None,
    )
    recommended_pre_gate_recycles = sum(
        step.matched_type is not None
        and (
            recommended_gate_click is None
            or step.click_number < recommended_gate_click
        )
        for step in recommended_simulation.steps
    )
    capacity_results = []
    unrestricted_capacity_proof = None
    for capacity in capacities:
        capacity = int(capacity)
        # A settled bag has at most two tiles of each type. All capacities
        # above that bound traverse exactly the same states, paths and peaks.
        unrestricted = capacity > 2 * len(set(types))
        if unrestricted and unrestricted_capacity_proof is not None:
            result = replace(unrestricted_capacity_proof, capacity=capacity)
        else:
            result = _capacity_proof(masks, types, gate, capacity, actual_limits)
            if unrestricted:
                unrestricted_capacity_proof = result
        capacity_results.append(result)
    capacity_proofs = tuple(capacity_results)
    return PuzzleProof(
        tile_types=types,
        blocker_masks=masks,
        threshold_type=gate,
        states_explored=states_explored,
        theoretical_complete_path_count=path_count,
        path_count_capped=count_capped,
        threshold_steps=threshold_steps,
        literal_first_match_clicks=literal_steps,
        singleton_rule_ok=violation_order is None,
        singleton_violation_order=violation_order,
        singleton_violation_types=violation_types,
        recommended_order=recommended,
        required_pre_gate_recycles=required_pre_gate_recycles,
        first_recycle_lead_tiles=first_recycle_lead_tiles,
        recommended_pre_gate_recycles=recommended_pre_gate_recycles,
        recommended_first_match_click=(
            recommended_simulation.literal_first_match_click
        ),
        recommended_gate_click=recommended_gate_click,
        recommended_path_constraints_ok=recycle_constraints_ok,
        capacity_proofs=capacity_proofs,
    )


def minimum_gate_structure_step(
    blocker_masks: Sequence[int],
    tile_types: Sequence[str],
    *,
    threshold_type: str,
    max_states: int = 250_000,
    required_pre_gate_recycles: int | None = None,
) -> int | None:
    """Fast exact pre-filter for the first gate match pressure.

    With unlimited capacity and per-type counts divisible by three, every
    topological prefix can be extended to a complete order.  The search can
    therefore stop a branch when the first gate triple appears.  Full candidate
    acceptance still requires :func:`analyze_puzzle`.
    """

    masks = tuple(int(mask) for mask in blocker_masks)
    types = tuple(str(value).upper() for value in tile_types)
    gate = str(threshold_type).upper()
    _validate_graph(masks, types)
    if gate not in set(types):
        raise InputValidationError("门槛块类型不在谜题单元中")
    labels = tuple(sorted(set(types)))
    label_to_index = {label: index for index, label in enumerate(labels)}
    indices = tuple(label_to_index[label] for label in types)
    gate_index = label_to_index[gate]
    start = (0, (0,) * len(labels))
    stack = [start]
    seen = {start}
    minimum = None
    while stack:
        removed, bag = stack.pop()
        for tile_index in available_indices(removed, masks):
            next_bag, matched, instant, _settled, _failed = _click_counts(
                bag, indices[tile_index], None
            )
            if matched and indices[tile_index] == gate_index:
                minimum = instant if minimum is None else min(minimum, instant)
                continue
            if matched and required_pre_gate_recycles == 0:
                continue
            next_state = (removed | (1 << tile_index), next_bag)
            if next_state not in seen:
                seen.add(next_state)
                if len(seen) > max_states:
                    raise ProofLimitExceeded(
                        "门槛块预筛状态数超过上限",
                        evidence={"max_states": max_states},
                    )
                stack.append(next_state)
    return minimum


def validate_target(
    proof: PuzzleProof,
    target: int,
    *,
    required_pre_gate_recycles: int | None = None,
    first_recycle_lead_tiles: int | None = None,
) -> tuple[bool, tuple[str, ...]]:
    """Apply the confirmed 4–9 product rules to an exact proof."""

    reasons: list[str] = []
    expected_recycles = (
        proof.required_pre_gate_recycles
        if required_pre_gate_recycles is None
        else required_pre_gate_recycles
    )
    expected_lead = (
        proof.first_recycle_lead_tiles
        if first_recycle_lead_tiles is None
        else first_recycle_lead_tiles
    )
    if target not in range(4, 10):
        reasons.append("target_out_of_range")
    if proof.minimum_structure_step != target:
        reasons.append("minimum_structure_step_mismatch")
    if (
        proof.required_pre_gate_recycles != expected_recycles
        or proof.first_recycle_lead_tiles != expected_lead
    ):
        reasons.append("recommended_path_constraint_snapshot_mismatch")
    if not proof.recommended_path_constraints_ok:
        reasons.append("no_recommended_path_with_recycle_constraints")
    if proof.recommended_pre_gate_recycles != expected_recycles:
        reasons.append("pre_gate_recycle_count_mismatch")
    if expected_recycles == 0:
        if (
            proof.recommended_first_match_click != target
            or proof.recommended_gate_click != target
        ):
            reasons.append("first_match_not_at_target_step")
    elif (
        proof.recommended_first_match_click is None
        or proof.recommended_first_match_click < 3 + expected_lead
    ):
        reasons.append("first_recycle_before_lead_requirement")
    by_capacity = {item.capacity: item for item in proof.capacity_proofs}
    seven = by_capacity.get(7)
    eight = by_capacity.get(8)
    if target <= 7:
        if seven is None or not seven.complete_exists:
            reasons.append("no_complete_path_with_capacity_7")
    elif target == 8:
        if seven is None or seven.classification != "all_fail":
            reasons.append("capacity_7_not_guaranteed_pressure")
        if eight is None or not eight.complete_exists:
            reasons.append("no_complete_path_with_capacity_8")
    elif target == 9:
        if seven is None or seven.classification != "all_fail":
            reasons.append("capacity_7_not_guaranteed_pressure")
        if eight is None or eight.classification != "all_fail":
            reasons.append("capacity_8_not_guaranteed_pressure")
    return not reasons, tuple(reasons)
