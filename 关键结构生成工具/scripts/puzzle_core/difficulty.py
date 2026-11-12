"""Deterministic, explicitly approximate difficulty simulation.

The production client owns the authoritative A/B materialisation result.  This
module can consume labels replayed from current local legacy-Seed configs, but
never labels its strategy rollouts as an exact win-rate forecast.
When all ten DupPara values are available we use them to build ten stable
synthetic boards and compare the candidate with the unchanged source template.
Missing or conflicting configuration falls back to a documented local profile
and is surfaced through ``coverage`` in every report.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from functools import lru_cache
from math import sqrt
from statistics import mean, median
from typing import Any, Mapping, Sequence

from .dynamic_reference import compare_dynamic_reference
from .geometry import build_geometry
from .template_reader import LevelTemplate


DIFFICULTY_TARGETS = ("fidelity", "standard", "strong")
SIMULATION_VERSION = "whole-board-strategy-rollout-v2"
COMMON_SEED_VERSION = "common-action-priority-v1"
MATERIALIZATION_VERSION = "paired-source-index-order-v2-no-luck"
STRATEGY_VECTOR_VERSION = "five-strategy-failure-vector-v1"
QUALIFICATION_POLICY_VERSION = "conservative-target-gates-v1"
TREND_POLICY_VERSION = "per-strategy-gradient-v1"
DISTANCE_POLICY_VERSION = "paired-per-strategy-distance-v2"
COMPLETION_WITNESS_VERSION = "existential-whole-board-path-v1"
_RANDOM_MIN_TRIALS = 12
_RANDOM_MAX_TRIALS = 24
_RANDOM_TRIAL_STEP = 4
_WILSON_TARGET_HALF_WIDTH = 0.18
_REVEAL_CURVE_BINS = 5
_STRATEGY_ORDER = (
    "random",
    "greedy",
    "local_optimal",
    "unlock",
    "adversarial",
)
_CONSTRUCTIVE_STRATEGIES = ("greedy", "local_optimal", "unlock")
_TARGET_POLICIES: Mapping[str, Mapping[str, object]] = {
    "fidelity": {
        "qualification_strategies": _STRATEGY_ORDER,
        "pressure_only_strategies": (),
        "maximum_observed_failure": 0.0,
        # No outcome-calibrated equivalence band exists yet.  Distance is a
        # primary ranking axis, but an arbitrary numeric cut-off must not be
        # presented as proof that two templates are or are not equivalent.
        "maximum_source_distance": None,
        "source_distance_policy": "ranking_only_until_calibrated",
    },
    "standard": {
        "qualification_strategies": (
            "random",
            *_CONSTRUCTIVE_STRATEGIES,
        ),
        "pressure_only_strategies": ("adversarial",),
        "required_constructive_passes_per_duplicate": 3,
        "maximum_random_failure_wilson95_upper": 0.40,
        "maximum_source_distance": None,
    },
    "strong": {
        "qualification_strategies": _CONSTRUCTIVE_STRATEGIES,
        "pressure_only_strategies": ("random", "adversarial"),
        "required_constructive_passes_per_duplicate": 1,
        "maximum_source_distance": None,
    },
}
_A_TILE = -10
_B_TILE = -11
_STRUCTURE_ID_TO_TYPE = {
    -12 - offset: chr(ord("C") + offset) for offset in range(12)
}


@dataclass(frozen=True)
class DuplicateParameter:
    a: int
    seed: str
    luck: str
    b: int

    @classmethod
    def parse(cls, value: str) -> "DuplicateParameter":
        parts = str(value).strip().split("|")
        if len(parts) != 4:
            raise ValueError("DupPara must contain A|seed|luck|B")
        a, b = int(parts[0]), int(parts[3])
        if a < 0 or b < 0:
            raise ValueError("DupPara A/B counts cannot be negative")
        return cls(a=a, seed=parts[1], luck=parts[2], b=b)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class DifficultyProfile:
    """Ten duplicate parameters plus traceable read coverage."""

    duplicate_params: tuple[DuplicateParameter, ...] = ()
    coverage: str = "missing"  # complete | fallback | ambiguous | missing
    source: Mapping[str, object] | None = None
    warning: str | None = None


def difficulty_profile_from_simulation(
    report: Mapping[str, object] | None,
) -> DifficultyProfile | None:
    """Rehydrate the immutable profile snapshot embedded in a candidate.

    This lets continuation and export validation remain reproducible even if
    the local config tables change after the candidate was generated.
    """

    if not isinstance(report, Mapping):
        return None
    raw_snapshot = report.get("profile_snapshot")
    if not isinstance(raw_snapshot, Sequence) or isinstance(
        raw_snapshot, (str, bytes)
    ):
        return None
    parsed: list[DuplicateParameter] = []
    try:
        for raw in raw_snapshot:
            if not isinstance(raw, Mapping):
                return None
            parsed.append(
                DuplicateParameter(
                    a=int(raw["a"]),
                    seed=str(raw["seed"]),
                    luck=str(raw["luck"]),
                    b=int(raw["b"]),
                )
            )
    except (KeyError, TypeError, ValueError):
        return None
    if len(parsed) != 10 or any(value.a < 0 or value.b < 0 for value in parsed):
        return None
    raw_source = report.get("source")
    source = dict(raw_source) if isinstance(raw_source, Mapping) else {}
    coverage = str(report.get("coverage") or "missing")
    if coverage not in {"complete", "fallback", "ambiguous", "missing"}:
        return None
    raw_warning = report.get("warning")
    return DifficultyProfile(
        duplicate_params=tuple(parsed),
        coverage=coverage,
        source=source,
        warning=None if raw_warning is None else str(raw_warning),
    )


@dataclass(frozen=True)
class _PathOutcome:
    complete: bool
    first_recycle_click: int | None
    first_full_bag_click: int | None
    settled_peak: int
    click_count: int
    expanded_states: int
    reveal_trace: tuple[Mapping[str, object], ...]
    structure_click_release_events: tuple[Mapping[str, object], ...]
    structure_match_events: tuple[Mapping[str, object], ...]
    dangling_one_area: int
    dangling_two_area: int
    dangling_one_max_duration: int
    dangling_two_max_duration: int
    cumulative_structure_exposed: int
    clicked_source_indices: tuple[int, ...]


def _stable_seed(*parts: object) -> int:
    payload = "\0".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _stable_order(values: Sequence[int], *parts: object) -> list[int]:
    """Return a runtime-independent deterministic order for source indices."""

    return sorted(
        values,
        key=lambda value: (_stable_seed(*parts, value), value),
    )


@lru_cache(maxsize=32768, typed=True)
def _action_priority(
    *,
    template_sha: str,
    duplicate_index: int,
    trial_id: int,
    decision_step: int,
    tile_index: int,
) -> int:
    """Shared action priority used by source and candidate rollouts.

    The key deliberately contains only versioned, source-stable identifiers.
    A candidate filters the common source action universe instead of drawing
    from its own available-list length, so a branch in one board does not
    shift every later random choice on the other board.
    The bounded cache shares these pure priorities across paired probes and
    Seed/Luck candidates without retaining boards or altering trial budgets.
    """

    return _stable_seed(
        SIMULATION_VERSION,
        template_sha,
        duplicate_index,
        trial_id,
        decision_step,
        tile_index,
    )


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, probability)) * (len(ordered) - 1)
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _distribution(values: Sequence[float], *, digits: int = 4) -> dict[str, object]:
    if not values:
        return {
            "count": 0,
            "median": None,
            "p10": None,
            "p90": None,
            "iqr": None,
            "min": None,
            "max": None,
        }
    numeric = [float(value) for value in values]
    q10 = _quantile(numeric, 0.10)
    q25 = _quantile(numeric, 0.25)
    q75 = _quantile(numeric, 0.75)
    q90 = _quantile(numeric, 0.90)

    def rounded(value: float | None) -> float | None:
        return None if value is None else round(value, digits)

    return {
        "count": len(numeric),
        "median": rounded(float(median(numeric))),
        "p10": rounded(q10),
        "p90": rounded(q90),
        "iqr": None if q25 is None or q75 is None else rounded(q75 - q25),
        "min": rounded(min(numeric)),
        "max": rounded(max(numeric)),
    }


def _wilson_interval(successes: int, trials: int) -> dict[str, object]:
    """95% Wilson interval for a sampled proportion."""

    if trials <= 0:
        return {
            "lower": None,
            "upper": None,
            "half_width": None,
            "confidence": 0.95,
        }
    probability = successes / trials
    z = 1.959963984540054
    z_squared = z * z
    denominator = 1.0 + z_squared / trials
    centre = (probability + z_squared / (2.0 * trials)) / denominator
    radius = (
        z
        * sqrt(
            probability * (1.0 - probability) / trials
            + z_squared / (4.0 * trials * trials)
        )
        / denominator
    )
    return {
        "lower": round(max(0.0, centre - radius), 4),
        "upper": round(min(1.0, centre + radius), 4),
        "half_width": round(radius, 4),
        "confidence": 0.95,
    }


def _fallback_parameters(level: LevelTemplate) -> tuple[DuplicateParameter, ...]:
    groups = max(2, len(level.tiles) // 3)
    first_a = max(1, round(groups * 0.6))
    first_b = max(1, groups - first_a)
    result = []
    for index in range(10):
        # A/B variety reduces gradually so the fallback still models the
        # intended duplicate direction without pretending to be live config.
        reduction = index // 2
        result.append(
            DuplicateParameter(
                a=max(1, first_a - reduction),
                seed=str(index + 1),
                luck="100",
                b=max(1, first_b - reduction),
            )
        )
    return tuple(result)


def _profile_parameters(
    level: LevelTemplate, profile: DifficultyProfile | None
) -> tuple[tuple[DuplicateParameter, ...], str, Mapping[str, object], str | None]:
    if profile is not None and len(profile.duplicate_params) == 10:
        return (
            profile.duplicate_params,
            profile.coverage,
            dict(profile.source or {}),
            profile.warning,
        )
    coverage = "missing" if profile is None else profile.coverage
    warning = (
        "未找到真实 DupPara1–10，已使用本地递减参考档；结果仅用于候选方向性排序。"
        if profile is None or not profile.warning
        else profile.warning
    )
    return _fallback_parameters(level), (
        "fallback" if coverage in {"complete", "missing"} else coverage
    ), dict((profile.source if profile else None) or {}), warning


def _materialized_types(
    level: LevelTemplate,
    replacements: Sequence[Any],
    duplicate: DuplicateParameter,
    duplicate_index: int,
) -> tuple[str, ...]:
    replacement_types = {
        int(item.tile_index): f"S:{str(item.structure_type).upper()}"
        for item in replacements
    }
    labels: list[str | None] = [None] * len(level.tiles)
    for index, value in replacement_types.items():
        labels[index] = value

    # Keep A and B source pools independent.  Shuffle the *full* source index
    # sequence before filtering replacements so the source and candidate use
    # the same relative assignment order wherever a position survives.
    for source_id, prefix, type_count in (
        (_A_TILE, "A", duplicate.a),
        (_B_TILE, "B", duplicate.b),
    ):
        # Order the full source pool first and only then filter candidate
        # replacements.  This is the materialisation half of the common-
        # random-numbers protocol: surviving source positions retain the same
        # relative order in both boards.
        shuffled = _stable_order(
            [tile.index for tile in level.tiles if tile.tile_id == source_id],
            MATERIALIZATION_VERSION,
            level.sha256,
            duplicate_index,
            duplicate.seed,
            prefix,
            "source-index",
        )
        shuffled = [index for index in shuffled if index not in replacement_types]
        pool = [f"{prefix}:{index}" for index in range(max(0, type_count))]
        if not pool:
            # A zero type-count can be legitimate after replacing every source
            # type.  Any surviving source positions make that profile
            # structurally inconsistent; retain an explicit sentinel so the
            # rollout fails visibly instead of silently borrowing the B pool.
            pool = [f"{prefix}:UNAVAILABLE"]
        group_order = sorted(
            pool,
            key=lambda label: (
                _stable_seed(
                    MATERIALIZATION_VERSION,
                    level.sha256,
                    duplicate_index,
                    duplicate.seed,
                    prefix,
                    "type-pool",
                    label,
                ),
                label,
            ),
        )
        for offset in range(0, len(shuffled), 3):
            label = group_order[(offset // 3) % len(group_order)]
            for tile_index in shuffled[offset : offset + 3]:
                labels[tile_index] = label

    for tile in level.tiles:
        if labels[tile.index] is not None:
            continue
        structure = _STRUCTURE_ID_TO_TYPE.get(tile.tile_id)
        labels[tile.index] = (
            f"S:{structure}" if structure is not None else f"T:{tile.tile_id}"
        )
    return tuple(str(value) for value in labels)


def _settle(bag: Counter[str], label: str) -> tuple[bool, int]:
    bag[label] += 1
    recycled = bag[label] == 3
    if recycled:
        del bag[label]
    return recycled, sum(bag.values())


def _simulate_path(
    blocker_masks: Sequence[int],
    labels: Sequence[str],
    *,
    strategy: str,
    seed: int | None = None,
    capacity: int = 7,
    source_indices: Sequence[int] | None = None,
    template_sha: str | None = None,
    duplicate_index: int = 1,
    trial_id: int = 0,
) -> _PathOutcome:
    """Run one deterministic whole-board strategy probe.

    ``seed`` remains accepted for private-call compatibility, but production
    rollouts use the versioned common action-priority key.  Source indices are
    stable across replacement candidates and therefore form the shared action
    universe required for paired comparison.
    """

    if len(blocker_masks) != len(labels):
        raise ValueError("blocker masks and labels must have equal length")
    if source_indices is None:
        source_indices = tuple(range(len(labels)))
    if len(source_indices) != len(labels):
        raise ValueError("source indices and labels must have equal length")
    if strategy not in {
        "random",
        "greedy",
        "local_optimal",
        "unlock",
        "adversarial",
    }:
        raise ValueError(f"unsupported path strategy: {strategy}")
    effective_sha = template_sha or f"legacy-seed:{seed or 0}"

    removed = 0
    complete_mask = (1 << len(labels)) - 1
    dependents: list[list[int]] = [[] for _ in labels]
    available_indices: set[int] = set()
    for index, required in enumerate(blocker_masks):
        if required == 0:
            available_indices.add(index)
        remaining = required & complete_mask
        while remaining:
            blocker_bit = remaining & -remaining
            dependents[blocker_bit.bit_length() - 1].append(index)
            remaining ^= blocker_bit

    def newly_unlocked(mask: int, selected: int) -> list[int]:
        # Removing one tile can only expose its direct dependents. A
        # dependent becomes clickable exactly when this is its last blocker.
        # Keep source order, including for reveal traces and score ties.
        selected_bit = 1 << selected
        return [
            index
            for index in dependents[selected]
            if not (mask >> index) & 1
            and blocker_masks[index] & ~mask == selected_bit
        ]

    bag: Counter[str] = Counter()
    first_recycle = None
    first_full = None
    peak = 0
    click = 0
    expanded_states = 0
    ever_exposed: set[int] = set()
    reveal_trace: list[Mapping[str, object]] = []
    click_release_events: list[Mapping[str, object]] = []
    match_events: list[Mapping[str, object]] = []
    clicked_source_indices: list[int] = []
    structure_labels = sorted({label for label in labels if label.startswith("S:")})
    dangling_one_area = 0
    dangling_two_area = 0
    dangling_one_runs = {label: 0 for label in structure_labels}
    dangling_two_runs = {label: 0 for label in structure_labels}
    dangling_one_max = {label: 0 for label in structure_labels}
    dangling_two_max = {label: 0 for label in structure_labels}

    def record_reveal(
        *,
        current_click: int,
        available: Sequence[int],
        newly_exposed: Sequence[int],
        selected_index: int | None,
    ) -> None:
        nonlocal dangling_one_area, dangling_two_area
        clickable_structures = [
            index for index in available if labels[index].startswith("S:")
        ]
        accessible = Counter(
            labels[index] for index in clickable_structures
        )
        for label, count in bag.items():
            if label.startswith("S:"):
                accessible[label] += count
        complete_groups = sum(count >= 3 for count in accessible.values())
        dangling_one = sum(count == 1 for count in accessible.values())
        dangling_two = sum(count == 2 for count in accessible.values())
        dangling_one_area += dangling_one
        dangling_two_area += dangling_two
        for label in structure_labels:
            count = accessible.get(label, 0)
            if count == 1:
                dangling_one_runs[label] += 1
                dangling_one_max[label] = max(
                    dangling_one_max[label], dangling_one_runs[label]
                )
            else:
                dangling_one_runs[label] = 0
            if count == 2:
                dangling_two_runs[label] += 1
                dangling_two_max[label] = max(
                    dangling_two_max[label], dangling_two_runs[label]
                )
            else:
                dangling_two_runs[label] = 0
        reveal_trace.append(
            {
                "click": current_click,
                "progress": round(current_click / max(1, len(labels)), 4),
                "clicked_source_index": (
                    None
                    if selected_index is None
                    else int(source_indices[selected_index])
                ),
                "clicked_label": (
                    None if selected_index is None else labels[selected_index]
                ),
                "current_clickable_structure_count": len(clickable_structures),
                "current_clickable_structure_source_indices": [
                    int(source_indices[index]) for index in clickable_structures
                ],
                "newly_exposed_structure_count": sum(
                    labels[index].startswith("S:") for index in newly_exposed
                ),
                "newly_exposed_structure_source_indices": [
                    int(source_indices[index])
                    for index in newly_exposed
                    if labels[index].startswith("S:")
                ],
                "cumulative_exposed_structure_count": sum(
                    labels[index].startswith("S:") for index in ever_exposed
                ),
                "complete_structure_group_type_count": complete_groups,
                "dangling_one_type_count": dangling_one,
                "dangling_two_type_count": dangling_two,
            }
        )

    initial_available = sorted(available_indices)
    initial_structures = {
        index for index in initial_available if labels[index].startswith("S:")
    }
    ever_exposed.update(initial_structures)
    record_reveal(
        current_click=0,
        available=initial_available,
        newly_exposed=tuple(initial_structures),
        selected_index=None,
    )

    def finish(complete: bool) -> _PathOutcome:
        return _PathOutcome(
            complete=complete,
            first_recycle_click=first_recycle,
            first_full_bag_click=first_full,
            settled_peak=peak,
            click_count=click,
            expanded_states=expanded_states,
            reveal_trace=tuple(reveal_trace),
            structure_click_release_events=tuple(click_release_events),
            structure_match_events=tuple(match_events),
            dangling_one_area=dangling_one_area,
            dangling_two_area=dangling_two_area,
            dangling_one_max_duration=max(dangling_one_max.values(), default=0),
            dangling_two_max_duration=max(dangling_two_max.values(), default=0),
            cumulative_structure_exposed=sum(
                labels[index].startswith("S:") for index in ever_exposed
            ),
            clicked_source_indices=tuple(clicked_source_indices),
        )

    while removed != complete_mask:
        available = sorted(available_indices)
        if not available:
            return finish(False)
        expanded_states += len(available)
        priority = lambda index: _action_priority(
            template_sha=effective_sha,
            duplicate_index=duplicate_index,
            trial_id=trial_id,
            decision_step=click,
            tile_index=int(source_indices[index]),
        )
        if strategy == "random":
            selected = min(available, key=lambda index: (priority(index), index))
        else:
            scored: list[tuple[tuple[int, ...], int]] = []
            for index in available:
                label = labels[index]
                after = Counter(bag)
                recycled, settled = _settle(after, label)
                newly_available = newly_unlocked(removed, index)
                unlocked = len(newly_available)
                unlocked_structure = sum(
                    labels[candidate].startswith("S:")
                    for candidate in newly_available
                )
                if strategy == "greedy":
                    key = (
                        0 if recycled else 1,
                        -bag.get(label, 0),
                        settled,
                        -unlocked,
                        priority(index),
                    )
                elif strategy == "local_optimal":
                    next_matching = sum(
                        1
                        for candidate in available
                        if candidate != index and labels[candidate] == label
                    )
                    key = (
                        settled,
                        0 if recycled else 1,
                        -unlocked,
                        -next_matching,
                        priority(index),
                    )
                elif strategy == "unlock":
                    key = (
                        -unlocked,
                        -unlocked_structure,
                        settled,
                        0 if recycled else 1,
                        priority(index),
                    )
                else:  # adversarial: maximise pressure, minimise relief.
                    key = (
                        -settled,
                        0 if not recycled else 1,
                        unlocked,
                        bag.get(label, 0),
                        priority(index),
                    )
                scored.append((key, index))
            selected = min(scored)[1]
        newly_available = newly_unlocked(removed, selected)
        click += 1
        clicked_source_indices.append(int(source_indices[selected]))
        removed |= 1 << selected
        recycled, settled = _settle(bag, labels[selected])
        peak = max(peak, settled)
        if recycled and first_recycle is None:
            first_recycle = click
        available_indices.remove(selected)
        available_indices.update(newly_available)
        newly_structures = {
            index
            for index in newly_available
            if labels[index].startswith("S:")
        }
        ever_exposed.update(newly_structures)
        if labels[selected].startswith("S:"):
            released_ab = sorted(
                int(source_indices[index])
                for index in newly_available
                if labels[index].startswith(("A:", "B:"))
            )
            click_release_events.append(
                {
                    "ordinal": len(click_release_events) + 1,
                    "click": click,
                    "progress": round(click / max(1, len(labels)), 4),
                    "structure_type": labels[selected][2:],
                    "completed_structure_match": recycled,
                    "released_ab_count": len(released_ab),
                    "released_ab_source_indices": released_ab,
                }
            )
        if recycled and labels[selected].startswith("S:"):
            match_events.append(
                {
                    "ordinal": len(match_events) + 1,
                    "click": click,
                    "progress": round(click / max(1, len(labels)), 4),
                    "structure_type": labels[selected][2:],
                }
            )
        record_reveal(
            current_click=click,
            available=sorted(available_indices),
            newly_exposed=tuple(newly_structures),
            selected_index=selected,
        )
        if settled >= capacity:
            first_full = click
            return finish(False)
    return finish(not bag)


def _reveal_summary(outcomes: Sequence[_PathOutcome]) -> dict[str, object]:
    metric_names = (
        "current_clickable_structure_count",
        "newly_exposed_structure_count",
        "cumulative_exposed_structure_count",
        "complete_structure_group_type_count",
        "dangling_one_type_count",
        "dangling_two_type_count",
    )
    bins: list[dict[str, list[float]]] = [
        {name: [] for name in metric_names} for _ in range(_REVEAL_CURVE_BINS)
    ]
    for outcome in outcomes:
        for point in outcome.reveal_trace:
            progress = float(point["progress"])
            bin_index = min(
                _REVEAL_CURVE_BINS - 1,
                int(progress * _REVEAL_CURVE_BINS),
            )
            for name in metric_names:
                bins[bin_index][name].append(float(point[name]))
    curve: list[dict[str, object]] = []
    for index, values in enumerate(bins):
        curve.append(
            {
                "progress_from": round(index / _REVEAL_CURVE_BINS, 2),
                "progress_to": round((index + 1) / _REVEAL_CURVE_BINS, 2),
                **{
                    name: _distribution(metric_values)
                    for name, metric_values in values.items()
                },
            }
        )
    match_events: dict[str, object] = {}
    for ordinal in range(1, 4):
        matching = [
            event
            for outcome in outcomes
            for event in outcome.structure_match_events
            if int(event["ordinal"]) == ordinal
        ]
        match_events[str(ordinal)] = {
            "click": _distribution(
                [float(event["click"]) for event in matching]
            ),
            "progress": _distribution(
                [float(event["progress"]) for event in matching]
            ),
        }
    click_release_events = [
        event
        for outcome in outcomes
        for event in outcome.structure_click_release_events
    ]
    completion_click_releases = [
        event
        for event in click_release_events
        if bool(event.get("completed_structure_match"))
    ]
    return {
        "curve_semantics": (
            "progress-binned distributions from real strategy paths; "
            "fields are not combined into a synthetic best/worst path"
        ),
        "curve": curve,
        "first_three_structure_matches": match_events,
        "release_semantics": {
            "structure_click_release": (
                "A/B tiles newly available immediately after every C-N click"
            ),
            "structure_match": (
                "the third same-type C-N click that completes one group; "
                "release is not attributed to the group as a whole"
            ),
        },
        "released_ab_per_structure_click": _distribution(
            [
                float(event["released_ab_count"])
                for event in click_release_events
            ]
        ),
        "released_ab_on_match_completion_click": _distribution(
            [
                float(event["released_ab_count"])
                for event in completion_click_releases
            ]
        ),
        "released_ab_total_per_path": _distribution(
            [
                float(
                    sum(
                        int(event["released_ab_count"])
                        for event in outcome.structure_click_release_events
                    )
                )
                for outcome in outcomes
            ]
        ),
        "structure_click_release_event_count": _distribution(
            [
                float(len(outcome.structure_click_release_events))
                for outcome in outcomes
            ]
        ),
        "dangling_one_area": _distribution(
            [float(item.dangling_one_area) for item in outcomes]
        ),
        "dangling_two_area": _distribution(
            [float(item.dangling_two_area) for item in outcomes]
        ),
        "dangling_one_max_duration": _distribution(
            [float(item.dangling_one_max_duration) for item in outcomes]
        ),
        "dangling_two_max_duration": _distribution(
            [float(item.dangling_two_max_duration) for item in outcomes]
        ),
        "cumulative_structure_exposed": _distribution(
            [float(item.cumulative_structure_exposed) for item in outcomes]
        ),
        "structure_matches_completed": _distribution(
            [float(len(item.structure_match_events)) for item in outcomes]
        ),
    }


def _strategy_summary(
    outcomes: Sequence[_PathOutcome],
    *,
    adaptive: bool = False,
) -> dict[str, object]:
    passes = sum(item.complete for item in outcomes)
    recycle_values = [
        item.first_recycle_click
        for item in outcomes
        if item.first_recycle_click is not None
    ]
    full_values = [
        item.first_full_bag_click
        for item in outcomes
        if item.first_full_bag_click is not None
    ]
    interval = _wilson_interval(passes, len(outcomes))
    return {
        "trials": len(outcomes),
        "pass_probability": round(passes / len(outcomes), 4),
        "pass_probability_wilson95": interval,
        "precision_reached": bool(
            not adaptive
            or (
                interval["half_width"] is not None
                and float(interval["half_width"]) <= _WILSON_TARGET_HALF_WIDTH
            )
        ),
        "adaptive_sampling": adaptive,
        "first_recycle_click": (
            None if not recycle_values else round(mean(recycle_values), 2)
        ),
        "first_full_bag_click": (
            None if not full_values else round(mean(full_values), 2)
        ),
        "settled_peak": max((item.settled_peak for item in outcomes), default=0),
        "completion_click": _distribution(
            [float(item.click_count) for item in outcomes if item.complete]
        ),
        "expanded_states": sum(item.expanded_states for item in outcomes),
        "reveal_curve": _reveal_summary(outcomes),
    }


def _failure_interval_from_pass(
    interval: Mapping[str, object] | None,
) -> dict[str, object]:
    """Invert a pass-probability interval without changing its confidence."""

    if not isinstance(interval, Mapping):
        return {
            "lower": None,
            "upper": None,
            "half_width": None,
            "confidence": 0.95,
        }
    lower = interval.get("lower")
    upper = interval.get("upper")
    return {
        "lower": None if upper is None else round(1.0 - float(upper), 4),
        "upper": None if lower is None else round(1.0 - float(lower), 4),
        "half_width": interval.get("half_width"),
        "confidence": interval.get("confidence", 0.95),
    }


def _strategy_failure_rates(row: Mapping[str, object]) -> dict[str, float]:
    """Return one failure observation per strategy, never trial-pooled."""

    raw_vector = row.get("strategy_failure_vector")
    raw_strategies = row.get("strategies")
    strategies = raw_strategies if isinstance(raw_strategies, Mapping) else {}
    rates: dict[str, float] = {}
    for strategy in _STRATEGY_ORDER:
        raw_value = (
            raw_vector.get(strategy)
            if isinstance(raw_vector, Mapping)
            else None
        )
        if isinstance(raw_value, (int, float)):
            rates[strategy] = min(1.0, max(0.0, float(raw_value)))
            continue
        summary = strategies.get(strategy)
        if not isinstance(summary, Mapping):
            continue
        pass_probability = summary.get("pass_probability")
        if isinstance(pass_probability, (int, float)):
            rates[strategy] = min(
                1.0, max(0.0, 1.0 - float(pass_probability))
            )
    return rates


def _strategy_failure_evidence(
    strategies: Mapping[str, Mapping[str, object]],
) -> tuple[dict[str, float], dict[str, object]]:
    rates = {
        strategy: round(
            1.0 - float(strategies[strategy]["pass_probability"]), 4
        )
        for strategy in _STRATEGY_ORDER
    }
    random_summary = strategies["random"]
    fixed = {
        strategy: {
            "observed_failure": bool(rates[strategy] > 0.0),
            "failure_rate": rates[strategy],
            "trials": int(strategies[strategy].get("trials", 1)),
            "evidence_kind": "deterministic_probe",
        }
        for strategy in _STRATEGY_ORDER
        if strategy != "random"
    }
    evidence = {
        "version": STRATEGY_VECTOR_VERSION,
        "strategy_order": list(_STRATEGY_ORDER),
        "aggregation_policy": "none_for_qualification_trend_or_distance",
        "random": {
            "observed_failure_rate": rates["random"],
            "failure_probability_wilson95": _failure_interval_from_pass(
                random_summary.get("pass_probability_wilson95")
                if isinstance(random_summary, Mapping)
                else None
            ),
            "trials": int(random_summary.get("trials", 0)),
            "evidence_kind": "adaptive_sample",
        },
        "fixed": fixed,
    }
    return rates, evidence


def _simulate_duplicate(
    level: LevelTemplate,
    replacements: Sequence[Any],
    duplicate: DuplicateParameter,
    duplicate_index: int,
    *,
    geometry: tuple[tuple[int, ...], tuple[int, ...]],
    materialized_labels: Sequence[str] | None = None,
) -> dict[str, object]:
    included, blocker_masks = geometry
    if materialized_labels is None:
        materialized = _materialized_types(
            level, replacements, duplicate, duplicate_index
        )
    else:
        materialized = tuple(materialized_labels)
        if len(materialized) != len(level.tiles):
            raise ValueError("materialized label count must match template tile count")
        if any(not isinstance(label, str) or not label for label in materialized):
            raise ValueError("materialized labels must be non-empty strings")
    labels = tuple(materialized[index] for index in included)
    def rollout(strategy: str, trial_id: int) -> _PathOutcome:
        return _simulate_path(
            blocker_masks,
            labels,
            strategy=strategy,
            source_indices=included,
            template_sha=level.sha256,
            duplicate_index=duplicate_index,
            trial_id=trial_id,
        )

    random_outcome_list: list[_PathOutcome] = []
    next_trial = 0
    while len(random_outcome_list) < _RANDOM_MAX_TRIALS:
        target_count = (
            _RANDOM_MIN_TRIALS
            if not random_outcome_list
            else min(
                _RANDOM_MAX_TRIALS,
                len(random_outcome_list) + _RANDOM_TRIAL_STEP,
            )
        )
        while len(random_outcome_list) < target_count:
            random_outcome_list.append(rollout("random", next_trial))
            next_trial += 1
        interval = _wilson_interval(
            sum(item.complete for item in random_outcome_list),
            len(random_outcome_list),
        )
        if (
            interval["half_width"] is not None
            and float(interval["half_width"]) <= _WILSON_TARGET_HALF_WIDTH
        ):
            break
    random_outcomes = tuple(random_outcome_list)
    greedy = (rollout("greedy", 1001),)
    local = (rollout("local_optimal", 1002),)
    unlock = (rollout("unlock", 1003),)
    adversarial = (rollout("adversarial", 1004),)
    strategies = {
        "random": _strategy_summary(random_outcomes, adaptive=True),
        "greedy": _strategy_summary(greedy),
        "local_optimal": _strategy_summary(local),
        "unlock": _strategy_summary(unlock),
        "adversarial": _strategy_summary(adversarial),
    }
    failure_vector, failure_evidence = _strategy_failure_evidence(strategies)
    named_outcomes = [
        *(('random', trial_id, outcome) for trial_id, outcome in enumerate(random_outcomes)),
        ('greedy', 1001, greedy[0]),
        ('local_optimal', 1002, local[0]),
        ('unlock', 1003, unlock[0]),
        ('adversarial', 1004, adversarial[0]),
    ]
    all_outcomes = tuple(outcome for _, _, outcome in named_outcomes)
    # A concrete completed sequence is an existential witness for this exact
    # synthetic duplicate.  It deliberately does not claim that every player
    # path is safe or that the stable-hash proxy reproduces the production
    # client's initial board.
    witness_priority = {
        'local_optimal': 0,
        'greedy': 1,
        'unlock': 2,
        'random': 3,
        'adversarial': 4,
    }
    complete_outcomes = sorted(
        (
            (strategy, trial_id, outcome)
            for strategy, trial_id, outcome in named_outcomes
            if outcome.complete
        ),
        key=lambda item: (witness_priority[item[0]], item[1]),
    )
    completion_witness = None
    if complete_outcomes:
        witness_strategy, witness_trial_id, witness_outcome = complete_outcomes[0]
        completion_witness = {
            "version": COMPLETION_WITNESS_VERSION,
            "status": "proved",
            "claim_scope": "existential_path_on_synthetic_duplicate",
            "strategy": witness_strategy,
            "trial_id": witness_trial_id,
            "click_count": witness_outcome.click_count,
            "clicked_source_indices": list(
                witness_outcome.clicked_source_indices
            ),
        }
    # Compatibility aggregates give each strategy exactly one vote.  The
    # adaptive random sample must not silently outweigh the four fixed probes.
    naked_pass = mean(
        float(strategies[strategy]["pass_probability"])
        for strategy in _STRATEGY_ORDER
    )
    failure_ratio = mean(failure_vector.values())
    recycle_values = [
        item.first_recycle_click
        for item in all_outcomes
        if item.first_recycle_click is not None
    ]
    full_values = [
        item.first_full_bag_click
        for item in all_outcomes
        if item.first_full_bag_click is not None
    ]
    per_strategy_compatibility_scores = []
    for strategy in _STRATEGY_ORDER:
        summary = strategies[strategy]
        full_click = summary.get("first_full_bag_click")
        first_full_pressure = (
            0.0
            if full_click is None
            else max(0.0, 1.0 - float(full_click) / max(1, len(labels)))
        )
        per_strategy_compatibility_scores.append(
            failure_vector[strategy] * 70.0
            + min(1.0, float(summary.get("settled_peak", 0.0)) / 7.0)
            * 20.0
            + first_full_pressure * 10.0
        )
    difficulty_score = min(
        100.0, mean(per_strategy_compatibility_scores)
    )
    random_precision = strategies["random"]["pass_probability_wilson95"]
    solver_status = (
        "disproved"
        if any(not item.complete for item in all_outcomes)
        else "limit_exceeded"
    )
    return {
        "duplicate_index": duplicate_index,
        "parameters": duplicate.as_dict(),
        "naked_pass_probability": round(naked_pass, 4),
        "capacity7_failure_path_ratio": round(failure_ratio, 4),
        "strategy_failure_vector": failure_vector,
        "strategy_failure_evidence": failure_evidence,
        "compatibility_aggregate": {
            "policy": "fixed_equal_weight_across_five_strategies",
            "used_for_qualification": False,
            "used_for_trend": False,
            "used_for_distance": False,
        },
        "first_recycle_click": (
            None if not recycle_values else min(recycle_values)
        ),
        "first_full_bag_click": None if not full_values else min(full_values),
        "difficulty_score": round(difficulty_score, 4),
        "strategies": strategies,
        "whole_board_reveal": _reveal_summary(all_outcomes),
        "path_coverage": {
            "mode": "sampled",
            "trial_count": len(all_outcomes),
            "random_trial_count": len(random_outcomes),
            "expanded_states": sum(item.expanded_states for item in all_outcomes),
            "strategies": list(strategies),
            "random_min_trials": _RANDOM_MIN_TRIALS,
            "random_max_trials": _RANDOM_MAX_TRIALS,
        },
        "statistical_precision": {
            "status": (
                "sufficient"
                if strategies["random"]["precision_reached"]
                else "inconclusive"
            ),
            "target_half_width": _WILSON_TARGET_HALF_WIDTH,
            "pass_probability_wilson95": random_precision,
            "resource_limit_reached": (
                len(random_outcomes) >= _RANDOM_MAX_TRIALS
                and not strategies["random"]["precision_reached"]
            ),
        },
        "random_trial_results": [item.complete for item in random_outcomes],
        "completion_witness": completion_witness,
        "completion_witness_status": (
            "proved" if completion_witness is not None else "inconclusive"
        ),
        "solver_status": solver_status,
        "solver_evidence_kind": "sampled_path_counterexample",
        "claim_scope": "sampled_paths_not_exhaustive_proof",
        "common_seed_version": COMMON_SEED_VERSION,
    }


def _optional_progress_distance(
    first: object,
    second: object,
    *,
    board_size: int,
) -> float:
    if first is None and second is None:
        return 0.0
    if first is None or second is None:
        return 1.0
    return min(1.0, abs(float(first) - float(second)) / max(1, board_size))


def _reveal_pressure_from_summary(
    reveal: object, *, board_size: int
) -> float:
    if not isinstance(reveal, Mapping):
        return 0.0
    one = reveal.get("dangling_one_area")
    two = reveal.get("dangling_two_area")
    one_median = (
        float(one.get("median") or 0.0) if isinstance(one, Mapping) else 0.0
    )
    two_median = (
        float(two.get("median") or 0.0) if isinstance(two, Mapping) else 0.0
    )
    # Two exposed cards create more immediate capacity pressure than one, but
    # retain both components in the public distance report below.
    return min(1.0, (one_median + 1.5 * two_median) / max(1, board_size))


def _strategy_reveal_pressures(
    row: Mapping[str, object], *, board_size: int
) -> dict[str, float]:
    raw_strategies = row.get("strategies")
    strategies = raw_strategies if isinstance(raw_strategies, Mapping) else {}
    return {
        strategy: _reveal_pressure_from_summary(
            summary.get("reveal_curve"), board_size=board_size
        )
        for strategy in _STRATEGY_ORDER
        for summary in [strategies.get(strategy)]
        if isinstance(summary, Mapping)
    }


def _strategy_metric_values(
    row: Mapping[str, object], metric: str
) -> dict[str, object]:
    raw_strategies = row.get("strategies")
    strategies = raw_strategies if isinstance(raw_strategies, Mapping) else {}
    return {
        strategy: summary.get(metric)
        for strategy in _STRATEGY_ORDER
        for summary in [strategies.get(strategy)]
        if isinstance(summary, Mapping)
    }


def _per_strategy_numeric_distance(
    candidate: Mapping[str, object],
    source: Mapping[str, object],
    *,
    scale: float = 1.0,
) -> dict[str, float]:
    distances: dict[str, float] = {}
    for strategy in _STRATEGY_ORDER:
        if strategy not in candidate or strategy not in source:
            distances[strategy] = 1.0
            continue
        distances[strategy] = min(
            1.0,
            abs(float(candidate[strategy]) - float(source[strategy]))
            / max(scale, 1e-9),
        )
    return distances


def _per_strategy_optional_progress_distance(
    candidate: Mapping[str, object],
    source: Mapping[str, object],
    *,
    board_size: int,
) -> dict[str, float]:
    distances: dict[str, float] = {}
    for strategy in _STRATEGY_ORDER:
        if strategy not in candidate or strategy not in source:
            distances[strategy] = 1.0
            continue
        distances[strategy] = _optional_progress_distance(
            candidate[strategy], source[strategy], board_size=board_size
        )
    return distances


@lru_cache(maxsize=16)
def _paired_bootstrap_draws(size: int) -> tuple[tuple[int, ...], ...]:
    """Reuse the exact deterministic draws across candidate comparisons."""
    return tuple(
        tuple(_stable_seed("paired-bootstrap-v1", size, replicate, draw) % size
              for draw in range(size))
        for replicate in range(256)
    )


def _paired_bootstrap_interval(
    candidate: Sequence[object],
    source: Sequence[object],
) -> dict[str, object]:
    size = min(len(candidate), len(source))
    if size <= 0:
        return {
            "status": "inconclusive",
            "paired_trials": 0,
            "mean_pass_difference": None,
            "interval95": [None, None],
            "method": "deterministic_paired_bootstrap_v1",
        }
    differences = [
        float(bool(candidate[index])) - float(bool(source[index]))
        for index in range(size)
    ]
    bootstrap_means: list[float] = []
    # Bound both cache entry count and entry size. Larger external trial sets
    # retain the original streaming calculation without retaining a large table.
    draws = _paired_bootstrap_draws(size) if size <= 64 else (
        tuple(_stable_seed("paired-bootstrap-v1", size, replicate, draw) % size
              for draw in range(size))
        for replicate in range(256)
    )
    for indices in draws:
        sample = [differences[index] for index in indices]
        bootstrap_means.append(mean(sample))
    return {
        "status": "available",
        "paired_trials": size,
        "mean_pass_difference": round(mean(differences), 4),
        "interval95": [
            round(float(_quantile(bootstrap_means, 0.025) or 0.0), 4),
            round(float(_quantile(bootstrap_means, 0.975) or 0.0), 4),
        ],
        "method": "deterministic_paired_bootstrap_v1",
    }


def _difficulty_distance(
    candidate_rows: Sequence[Mapping[str, object]],
    source_rows: Sequence[Mapping[str, object]],
    *,
    board_size: int,
    source_has_cn: bool,
    structure_count: int = 0,
    requested_archetype: str = "auto",
) -> dict[str, object]:
    duplicate_reports: list[dict[str, object]] = []
    component_names = (
        "strategy_failure",
        "bag_peak",
        "first_recycle_progress",
        "first_full_progress",
        "whole_board_reveal_pressure",
    )
    component_values: dict[str, list[float]] = {
        name: [] for name in component_names
    }
    per_strategy_component_values: dict[str, dict[str, list[float]]] = {
        name: {strategy: [] for strategy in _STRATEGY_ORDER}
        for name in component_names
    }
    reveal_baseline = "source_template" if source_has_cn else "reference_cohort"
    dynamic_reference = (
        None
        if source_has_cn
        else compare_dynamic_reference(
            candidate_rows,
            structure_count=structure_count,
            board_size=board_size,
            requested_archetype=requested_archetype,
        )
    )
    for fallback_index, (candidate, source) in enumerate(
        zip(candidate_rows, source_rows), 1
    ):
        strategy_distances = _per_strategy_numeric_distance(
            _strategy_failure_rates(candidate),
            _strategy_failure_rates(source),
        )
        bag_peak_distances = _per_strategy_numeric_distance(
            _strategy_metric_values(candidate, "settled_peak"),
            _strategy_metric_values(source, "settled_peak"),
            scale=7.0,
        )
        recycle_distances = _per_strategy_optional_progress_distance(
            _strategy_metric_values(candidate, "first_recycle_click"),
            _strategy_metric_values(source, "first_recycle_click"),
            board_size=board_size,
        )
        full_distances = _per_strategy_optional_progress_distance(
            _strategy_metric_values(candidate, "first_full_bag_click"),
            _strategy_metric_values(source, "first_full_bag_click"),
            board_size=board_size,
        )
        reveal_distances = (
            _per_strategy_numeric_distance(
                _strategy_reveal_pressures(candidate, board_size=board_size),
                _strategy_reveal_pressures(source, board_size=board_size),
            )
            if source_has_cn
            else {}
        )
        per_strategy = {
            "strategy_failure": strategy_distances,
            "bag_peak": bag_peak_distances,
            "first_recycle_progress": recycle_distances,
            "first_full_progress": full_distances,
            "whole_board_reveal_pressure": reveal_distances,
        }
        components = {
            name: max(values.values(), default=0.0)
            for name, values in per_strategy.items()
            if name != "whole_board_reveal_pressure" or source_has_cn
        }
        reference_duplicate_distance = None
        if isinstance(dynamic_reference, Mapping):
            raw_duplicate_distances = dynamic_reference.get(
                "per_duplicate_distance"
            )
            if isinstance(raw_duplicate_distances, Mapping):
                reference_duplicate_distance = raw_duplicate_distances.get(
                    f"{int(candidate.get('duplicate_index', fallback_index)):02d}"
                )
        if not source_has_cn and isinstance(
            reference_duplicate_distance, (int, float)
        ):
            components["whole_board_reveal_pressure"] = float(
                reference_duplicate_distance
            )
        for name, value in components.items():
            component_values[name].append(float(value))
        for name, values in per_strategy.items():
            for strategy, value in values.items():
                per_strategy_component_values[name][strategy].append(value)
        primary = max(components.values(), default=0.0)
        stable_tiebreak = mean(components.values()) if components else 0.0
        raw_candidate_trials = candidate.get("random_trial_results")
        raw_source_trials = source.get("random_trial_results")
        paired_interval = _paired_bootstrap_interval(
            raw_candidate_trials
            if isinstance(raw_candidate_trials, Sequence)
            and not isinstance(raw_candidate_trials, (str, bytes))
            else (),
            raw_source_trials
            if isinstance(raw_source_trials, Sequence)
            and not isinstance(raw_source_trials, (str, bytes))
            else (),
        )
        duplicate_reports.append(
            {
                "duplicate_index": int(
                    candidate.get("duplicate_index", fallback_index)
                ),
                "components": {
                    name: round(value, 4) for name, value in components.items()
                },
                "per_strategy_components": {
                    name: {
                        strategy: round(value, 4)
                        for strategy, value in values.items()
                    }
                    for name, values in per_strategy.items()
                    if name != "whole_board_reveal_pressure" or source_has_cn
                },
                "whole_board_reveal_comparison": {
                    "baseline": reveal_baseline,
                    "status": (
                        "compared"
                        if source_has_cn
                        else str(
                            dynamic_reference.get("status", "inconclusive")
                            if isinstance(dynamic_reference, Mapping)
                            else "inconclusive"
                        )
                    ),
                    "candidate_pressure_by_strategy": {
                        strategy: round(value, 4)
                        for strategy, value in _strategy_reveal_pressures(
                            candidate, board_size=board_size
                        ).items()
                    },
                    "source_pressure_by_strategy": (
                        {
                            strategy: round(value, 4)
                            for strategy, value in _strategy_reveal_pressures(
                                source, board_size=board_size
                            ).items()
                        }
                        if source_has_cn
                        else None
                    ),
                    "reference_distance": (
                        round(float(reference_duplicate_distance), 4)
                        if isinstance(
                            reference_duplicate_distance, (int, float)
                        )
                        else None
                    ),
                },
                "primary_distance": round(primary, 4),
                "stable_tiebreak": round(stable_tiebreak, 4),
                "paired_pass_difference": paired_interval,
                "legacy_score_difference": round(
                    abs(
                        float(candidate.get("difficulty_score", 0.0))
                        - float(source.get("difficulty_score", 0.0))
                    ),
                    4,
                ),
            }
        )
    worst = max(
        duplicate_reports,
        key=lambda value: (
            float(value["primary_distance"]),
            float(value["stable_tiebreak"]),
            -int(value["duplicate_index"]),
        ),
        default=None,
    )
    primary_distance = max(
        (float(value["primary_distance"]) for value in duplicate_reports),
        default=0.0,
    )
    stable_tiebreak = mean(
        [float(value["stable_tiebreak"]) for value in duplicate_reports]
    ) if duplicate_reports else 0.0
    candidate_curve = [
        float(value.get("difficulty_score", 0.0)) for value in candidate_rows
    ]
    source_curve = [
        float(value.get("difficulty_score", 0.0)) for value in source_rows
    ]
    curve_area_signed = (
        sum(candidate_curve) - sum(source_curve)
    ) / max(1.0, 100.0 * min(len(candidate_curve), len(source_curve)))
    return {
        "version": DISTANCE_POLICY_VERSION,
        "scale": "0_to_1_per_component",
        "comparison_policy": (
            "worst per-strategy difference inside each component, then worst "
            "duplicate primary distance; no trial-count pooling"
        ),
        "whole_board_reveal_baseline": {
            "mode": reveal_baseline,
            "source_has_cn": source_has_cn,
            "reference_cohort": (
                None
                if source_has_cn
                else "level_200_300_key_structure_v1"
            ),
            "source_comparison_used": source_has_cn,
            "dynamic_reference": dynamic_reference,
        },
        "primary_distance": round(primary_distance, 4),
        # ``total`` is a compatibility alias for ranking consumers.  It is
        # intentionally the worst-duplicate primary distance rather than a
        # mean that could hide one outlying duplicate.
        "total": round(primary_distance, 4),
        "stable_tiebreak": round(stable_tiebreak, 4),
        "component_means": {
            name: round(mean(values), 4) if values else None
            for name, values in component_values.items()
        },
        "per_strategy_component_vectors": {
            name: {
                strategy: [round(value, 4) for value in values]
                for strategy, values in strategy_values.items()
                if values
            }
            for name, strategy_values in per_strategy_component_values.items()
            if name != "whole_board_reveal_pressure" or source_has_cn
        },
        "reveal_reference_distance": dynamic_reference,
        "duplicate_difference_vector": [
            float(value["primary_distance"]) for value in duplicate_reports
        ],
        "strategy_failure_difference_vector": component_values[
            "strategy_failure"
        ],
        "curve_area_difference": {
            "signed": round(curve_area_signed, 4),
            "absolute": round(abs(curve_area_signed), 4),
            "scale": "difficulty_score_area_divided_by_100_and_duplicate_count",
        },
        "worst_duplicate": worst,
        "duplicates": duplicate_reports,
    }


def _absolute_gradient_violations(
    scores: Sequence[float],
    *,
    tolerance: float,
    strategy: str,
) -> list[dict[str, object]]:
    def exceeds(value: float) -> bool:
        return value > tolerance + 1e-9

    violations = [
        {
            "strategy": strategy,
            "from_duplicate": index,
            "to_duplicate": index + 1,
            "increase": round(scores[index] - scores[index - 1], 4),
            "kind": "absolute_reverse_gradient",
        }
        for index in range(1, len(scores))
        if exceeds(scores[index] - scores[index - 1])
    ]
    if len(scores) > 1 and exceeds(scores[-1] - scores[0]):
        violations.append(
            {
                "strategy": strategy,
                "from_duplicate": 1,
                "to_duplicate": len(scores),
                "increase": round(scores[-1] - scores[0], 4),
                "kind": "absolute_overall_reverse_gradient",
            }
        )
    return violations


def _failure_curve(
    rows: Sequence[Mapping[str, object]], strategy: str
) -> list[float] | None:
    curve: list[float] = []
    for row in rows:
        rates = _strategy_failure_rates(row)
        if strategy not in rates:
            return None
        curve.append(rates[strategy])
    return curve


def _trend_report(
    candidate_rows: Sequence[Mapping[str, object]],
    source_rows: Sequence[Mapping[str, object]],
    *,
    mode: str,
    tolerance: float = 0.05,
    qualification_strategies: Sequence[str] = _STRATEGY_ORDER,
) -> dict[str, object]:
    qualification_set = frozenset(qualification_strategies)
    unknown = qualification_set - frozenset(_STRATEGY_ORDER)
    if unknown:
        raise ValueError(f"unsupported qualification strategies: {sorted(unknown)}")
    strategy_reports: dict[str, dict[str, object]] = {}
    all_residuals: list[dict[str, object]] = []
    all_candidate_absolute: list[dict[str, object]] = []
    all_source_absolute: list[dict[str, object]] = []
    for strategy in _STRATEGY_ORDER:
        candidate_scores = _failure_curve(candidate_rows, strategy)
        source_scores = _failure_curve(source_rows, strategy)
        if candidate_scores is None or source_scores is None:
            missing = {
                "strategy": strategy,
                "kind": "missing_strategy_evidence",
            }
            strategy_reports[strategy] = {
                "status": "inconclusive",
                "candidate_failure_curve": candidate_scores,
                "source_failure_curve": source_scores,
                "violations": [missing],
                "residuals": [],
            }
            continue
        candidate_absolute = _absolute_gradient_violations(
            candidate_scores, tolerance=tolerance, strategy=strategy
        )
        source_absolute = _absolute_gradient_violations(
            source_scores, tolerance=tolerance, strategy=strategy
        )
        residuals: list[dict[str, object]] = []
        residual_violations: list[dict[str, object]] = []
        for index in range(1, min(len(candidate_scores), len(source_scores))):
            candidate_increase = (
                candidate_scores[index] - candidate_scores[index - 1]
            )
            source_increase = source_scores[index] - source_scores[index - 1]
            residual = candidate_increase - source_increase
            item = {
                "strategy": strategy,
                "from_duplicate": index,
                "to_duplicate": index + 1,
                "source_increase": round(source_increase, 4),
                "candidate_increase": round(candidate_increase, 4),
                "residual": round(residual, 4),
            }
            residuals.append(item)
            if candidate_increase > tolerance + 1e-9 and (
                source_increase <= tolerance + 1e-9
                or residual > tolerance + 1e-9
            ):
                residual_violations.append(
                    {
                        **item,
                        "kind": (
                            "new_reverse_gradient"
                            if source_increase <= tolerance
                            else "amplified_reverse_gradient"
                        ),
                    }
                )
        if len(candidate_scores) > 1 and len(source_scores) > 1:
            candidate_overall = candidate_scores[-1] - candidate_scores[0]
            source_overall = source_scores[-1] - source_scores[0]
            overall_residual = candidate_overall - source_overall
            if candidate_overall > tolerance + 1e-9 and (
                source_overall <= tolerance + 1e-9
                or overall_residual > tolerance + 1e-9
            ):
                residual_violations.append(
                    {
                        "strategy": strategy,
                        "from_duplicate": 1,
                        "to_duplicate": min(
                            len(candidate_scores), len(source_scores)
                        ),
                        "source_increase": round(source_overall, 4),
                        "candidate_increase": round(candidate_overall, 4),
                        "residual": round(overall_residual, 4),
                        "kind": (
                            "new_overall_reverse_gradient"
                            if source_overall <= tolerance
                            else "amplified_overall_reverse_gradient"
                        ),
                    }
                )
        strategy_violations = (
            candidate_absolute
            if mode == "rebuild_gradient"
            else residual_violations
        )
        all_residuals.extend(residuals)
        all_candidate_absolute.extend(candidate_absolute)
        all_source_absolute.extend(source_absolute)
        strategy_reports[strategy] = {
            "status": "available",
            "candidate_failure_curve": [
                round(value, 4) for value in candidate_scores
            ],
            "source_failure_curve": [round(value, 4) for value in source_scores],
            "violations": strategy_violations,
            "residuals": residuals,
            "curve_area_residual": round(
                sum(candidate_scores) - sum(source_scores), 4
            ),
            "candidate_absolute_violations": candidate_absolute,
            "source_absolute_violations": source_absolute,
        }
    violations = [
        violation
        for strategy in _STRATEGY_ORDER
        if strategy in qualification_set
        for violation in strategy_reports[strategy]["violations"]
    ]
    pressure_only_violations = [
        violation
        for strategy in _STRATEGY_ORDER
        if strategy not in qualification_set
        for violation in strategy_reports[strategy]["violations"]
    ]
    policy = (
        "absolute_monotonic_target"
        if mode == "rebuild_gradient"
        else "preserve_source_gradient_residual"
    )
    area_residuals = [
        float(report["curve_area_residual"])
        for report in strategy_reports.values()
        if report.get("status") == "available"
    ]
    return {
        # Compatibility field: in fidelity mode this means the candidate did
        # not add/amplify a reverse gradient, not that the source was monotonic.
        "becomes_easier": not violations,
        "safety_qualified": not violations,
        "mode": mode,
        "policy": policy,
        "version": TREND_POLICY_VERSION,
        "tolerance": tolerance,
        "unit": "failure_probability",
        "qualification_strategies": [
            strategy
            for strategy in _STRATEGY_ORDER
            if strategy in qualification_set
        ],
        "pressure_only_strategies": [
            strategy
            for strategy in _STRATEGY_ORDER
            if strategy not in qualification_set
        ],
        "strategy_reports": strategy_reports,
        "violations": violations,
        "pressure_only_violations": pressure_only_violations,
        "residuals": all_residuals,
        "curve_area_residual": (
            round(mean(area_residuals), 4) if area_residuals else None
        ),
        "curve_area_residual_by_strategy": {
            strategy: report.get("curve_area_residual")
            for strategy, report in strategy_reports.items()
        },
        "candidate_absolute_becomes_easier": not all_candidate_absolute,
        "candidate_absolute_violations": all_candidate_absolute,
        "source_absolute_becomes_easier": not all_source_absolute,
        "source_absolute_violations": all_source_absolute,
        "fidelity_safety_override": bool(
            mode != "rebuild_gradient"
            and all_source_absolute
            and not all_candidate_absolute
        ),
    }


def _random_failure_wilson_upper(row: Mapping[str, object]) -> float | None:
    raw_strategies = row.get("strategies")
    if not isinstance(raw_strategies, Mapping):
        return None
    random_summary = raw_strategies.get("random")
    if not isinstance(random_summary, Mapping):
        return None
    pass_interval = random_summary.get("pass_probability_wilson95")
    failure_interval = _failure_interval_from_pass(
        pass_interval if isinstance(pass_interval, Mapping) else None
    )
    value = failure_interval.get("upper")
    return None if value is None else float(value)


def _strategy_qualification_report(
    candidate_rows: Sequence[Mapping[str, object]],
    *,
    target: str,
    source_distance: float,
) -> dict[str, object]:
    """Apply versioned target gates without pooling strategy samples."""

    if target not in _TARGET_POLICIES:
        raise ValueError(f"unsupported difficulty target: {target}")
    policy = _TARGET_POLICIES[target]
    vectors = [_strategy_failure_rates(row) for row in candidate_rows]
    missing = [
        {
            "duplicate_index": index,
            "strategies": [
                strategy
                for strategy in _STRATEGY_ORDER
                if strategy not in vector
            ],
        }
        for index, vector in enumerate(vectors, 1)
        if len(vector) != len(_STRATEGY_ORDER)
    ]
    observed_failures = [
        {
            "duplicate_index": duplicate_index,
            "strategy": strategy,
            "failure_rate": round(rate, 4),
        }
        for duplicate_index, vector in enumerate(vectors, 1)
        for strategy, rate in vector.items()
        if rate > 0.0
    ]
    constructive_pass_counts = [
        sum(vector.get(strategy, 1.0) == 0.0 for strategy in _CONSTRUCTIVE_STRATEGIES)
        for vector in vectors
    ]
    random_wilson_uppers = [
        _random_failure_wilson_upper(row) for row in candidate_rows
    ]
    reasons: list[str] = []
    if missing:
        reasons.append("策略证据不完整，无法执行保守难度门槛")
    if target == "fidelity":
        if observed_failures:
            reasons.append("保真模式的五策略证据中已观察到失败路径")
    elif target == "standard":
        required = int(policy["required_constructive_passes_per_duplicate"])
        if any(value < required for value in constructive_pass_counts):
            reasons.append("标准模式要求每个副本的三个建设性策略均通过")
        maximum_upper = float(policy["maximum_random_failure_wilson95_upper"])
        if any(
            value is None or value > maximum_upper
            for value in random_wilson_uppers
        ):
            reasons.append("随机策略失败率的 Wilson 95% 上界过高")
    else:  # strong
        required = int(policy["required_constructive_passes_per_duplicate"])
        if any(value < required for value in constructive_pass_counts):
            reasons.append("强卡点模式每个副本至少保留一条建设性策略通路")
    maximum_distance = policy.get("maximum_source_distance")
    if maximum_distance is not None and source_distance > float(maximum_distance):
        reasons.append("与原模板的分策略预测难度距离过大")
    return {
        "version": QUALIFICATION_POLICY_VERSION,
        "target": target,
        "passed": not reasons,
        "policy": dict(policy),
        "strategy_order": list(_STRATEGY_ORDER),
        "constructive_strategies": list(_CONSTRUCTIVE_STRATEGIES),
        "strategy_failure_vectors": vectors,
        "observed_failures": observed_failures,
        "constructive_pass_counts_per_duplicate": constructive_pass_counts,
        "random_failure_wilson95_upper_per_duplicate": random_wilson_uppers,
        "source_distance": round(source_distance, 4),
        "source_distance_calibration": "unavailable",
        "missing_strategy_evidence": missing,
        "reasons": reasons,
    }


def _completion_witness_coverage(
    candidate_rows: Sequence[Mapping[str, object]],
) -> tuple[str, list[int]]:
    """Summarize existential whole-board witnesses without overclaiming.

    A missing witness means the finite probe set was insufficient; it is not
    a proof that no completing path exists.
    """

    missing = [
        int(row.get("duplicate_index", index))
        for index, row in enumerate(candidate_rows, 1)
        if row.get("completion_witness_status") != "proved"
        or not isinstance(row.get("completion_witness"), Mapping)
    ]
    return ("proved" if not missing else "inconclusive", missing)


def simulate_candidate_difficulty(
    level: LevelTemplate,
    replacements: Sequence[Any],
    *,
    target: str,
    profile: DifficultyProfile | None = None,
    source_duplicates: Sequence[Mapping[str, object]] | None = None,
    gradient_mode: str | None = None,
    requested_archetype: str = "auto",
) -> dict[str, object]:
    """Compare one candidate against the unchanged source over DupPara1–10."""

    if target not in DIFFICULTY_TARGETS:
        raise ValueError(f"unsupported difficulty target: {target}")
    parameters, coverage, source, warning = _profile_parameters(level, profile)
    graph = build_geometry(level, include_tower=True)
    included = tuple(sorted(graph.included_indices))
    source_has_cn = any(
        level.tiles[index].tile_id in _STRUCTURE_ID_TO_TYPE for index in included
    )
    structure_count = sum(
        level.tiles[index].tile_id in _STRUCTURE_ID_TO_TYPE for index in included
    ) + len(replacements)
    local_index = {source_index: offset for offset, source_index in enumerate(included)}
    blocker_masks = tuple(
        sum(
            1 << local_index[blocker]
            for blocker in graph.direct_blockers[source_index]
            if blocker in local_index
        )
        for source_index in included
    )
    geometry = (included, blocker_masks)
    replaced_a_types = {
        str(item.structure_type).upper()
        for item in replacements
        if int(item.source_tile_id) == _A_TILE
    }
    replaced_b_types = {
        str(item.structure_type).upper()
        for item in replacements
        if int(item.source_tile_id) == _B_TILE
    }
    adjusted_parameters: list[DuplicateParameter] = []
    parameter_underflow: list[dict[str, object]] = []
    for index, duplicate in enumerate(parameters, 1):
        raw_a = duplicate.a - len(replaced_a_types)
        raw_b = duplicate.b - len(replaced_b_types)
        if raw_a < 0 or raw_b < 0:
            parameter_underflow.append(
                {
                    "duplicate_index": index,
                    "raw_a": raw_a,
                    "raw_b": raw_b,
                }
            )
        adjusted_parameters.append(
            DuplicateParameter(
                a=max(0, raw_a),
                seed=duplicate.seed,
                luck=duplicate.luck,
                b=max(0, raw_b),
            )
        )
    candidate_rows = tuple(
        _simulate_duplicate(
            level,
            replacements,
            duplicate,
            index,
            geometry=geometry,
        )
        for index, duplicate in enumerate(adjusted_parameters, 1)
    )
    reusable_source_rows = (
        tuple(dict(row) for row in source_duplicates)
        if source_duplicates is not None and len(source_duplicates) == 10
        else ()
    )
    source_rows = (
        reusable_source_rows
        if reusable_source_rows
        else tuple(
            _simulate_duplicate(
                level,
                (),
                duplicate,
                index,
                geometry=geometry,
            )
            for index, duplicate in enumerate(parameters, 1)
        )
    )
    legacy_distances = [
        abs(float(candidate["difficulty_score"]) - float(original["difficulty_score"]))
        for candidate, original in zip(candidate_rows, source_rows)
    ]
    distance = round(mean(legacy_distances), 4)
    source_mode = str(
        source.get("parameter_strategy_mode")
        or source.get("strategy_mode")
        or "preserve_original_gradient"
    )
    effective_gradient_mode = gradient_mode or source_mode
    if effective_gradient_mode not in {
        "preserve_original_gradient",
        "rebuild_gradient",
    }:
        effective_gradient_mode = "preserve_original_gradient"
    source_report = {
        **source,
        "parameter_strategy_mode": effective_gradient_mode,
    }
    trend = _trend_report(
        candidate_rows,
        source_rows,
        mode=effective_gradient_mode,
        qualification_strategies=tuple(
            str(value)
            for value in _TARGET_POLICIES[target]["qualification_strategies"]
        ),
    )
    multidimensional_distance = _difficulty_distance(
        candidate_rows,
        source_rows,
        board_size=len(included),
        source_has_cn=source_has_cn,
        structure_count=structure_count,
        requested_archetype=requested_archetype,
    )
    strategy_qualification = _strategy_qualification_report(
        candidate_rows,
        target=target,
        source_distance=float(multidimensional_distance["primary_distance"]),
    )
    reasons: list[str] = []
    if not trend["becomes_easier"]:
        reasons.append(
            "候选新增或放大了副本反向梯度"
            if effective_gradient_mode != "rebuild_gradient"
            else "重做梯度模式下副本1→10未保持总体变易"
        )
    reasons.extend(str(value) for value in strategy_qualification["reasons"])
    if coverage != "complete":
        reasons.append("未使用唯一可确认的真实 DupPara1–10，仅作方向性排序")
    if parameter_underflow:
        reasons.append(
            "替换的 A/B 结构种类数超过原 DupPara 种类数，"
            "已按 0 模拟并降级"
        )
    reveal_reference = multidimensional_distance.get(
        "reveal_reference_distance"
    )
    if (
        not source_has_cn
        and replacements
        and (
            not isinstance(reveal_reference, Mapping)
            or reveal_reference.get("status") != "compared"
        )
    ):
        reasons.append(
            "缺少与当前结构规模兼容的线上整板揭露参考，不能证明揭露与释放节奏保真"
        )
    config_coverage = {
        "complete": "exact_unique",
        "ambiguous": "ambiguous",
        "fallback": "fallback",
        "missing": "missing",
    }.get(coverage, "missing")
    # Exact DupPara coverage proves which configuration was read, not how the
    # client materialised A/B tiles.  This module still uses a deterministic
    # stable-hash proxy; only a separately verified client-RNG path may claim
    # ``reconstructed`` or ``runtime_exact``.
    materialization_fidelity = "synthetic"
    paired_comparison = bool(
        all(
            str(row.get("common_seed_version") or "") == COMMON_SEED_VERSION
            for row in source_rows
        )
    )
    candidate_precision = [
        row.get("statistical_precision", {}) for row in candidate_rows
    ]
    source_precision = [
        row.get("statistical_precision", {}) for row in source_rows
    ]
    precision_reached = all(
        isinstance(value, Mapping) and value.get("status") == "sufficient"
        for value in (*candidate_precision, *source_precision)
    )
    half_widths = [
        float(interval["half_width"])
        for row in (*candidate_rows, *source_rows)
        if isinstance(row.get("statistical_precision"), Mapping)
        for interval in [
            row["statistical_precision"].get("pass_probability_wilson95")
        ]
        if isinstance(interval, Mapping) and interval.get("half_width") is not None
    ]
    solver_statuses = {
        str(row.get("solver_status") or "limit_exceeded")
        for row in candidate_rows
    }
    solver_status = (
        "disproved"
        if "disproved" in solver_statuses
        else "proved"
        if solver_statuses == {"proved"}
        else "limit_exceeded"
    )
    completion_witness_status, missing_completion_witnesses = (
        _completion_witness_coverage(candidate_rows)
    )
    evidence_coverage = {
        "config_coverage": config_coverage,
        "materialization_fidelity": materialization_fidelity,
        "path_coverage": {
            "mode": "sampled",
            "trial_count": sum(
                int(row.get("path_coverage", {}).get("trial_count", 0))
                for row in candidate_rows
                if isinstance(row.get("path_coverage"), Mapping)
            ),
            "expanded_states": sum(
                int(row.get("path_coverage", {}).get("expanded_states", 0))
                for row in candidate_rows
                if isinstance(row.get("path_coverage"), Mapping)
            ),
            "strategies": [
                "random",
                "greedy",
                "local_optimal",
                "unlock",
                "adversarial",
            ],
            "solver_limit": {
                "random_min_trials_per_duplicate": _RANDOM_MIN_TRIALS,
                "random_max_trials_per_duplicate": _RANDOM_MAX_TRIALS,
            },
        },
        "statistical_precision": {
            "status": "sufficient" if precision_reached else "inconclusive",
            "target_wilson_half_width": _WILSON_TARGET_HALF_WIDTH,
            "max_observed_wilson_half_width": (
                None if not half_widths else round(max(half_widths), 4)
            ),
        },
        "paired_comparison": paired_comparison,
        "common_seed_version": COMMON_SEED_VERSION,
        "materialization_version": MATERIALIZATION_VERSION,
        "simulation_version": SIMULATION_VERSION,
        "solver_status": solver_status,
        "solver_evidence_kind": "sampled_path_counterexample",
        "claim_scope": "sampled_paths_not_exhaustive_proof",
        "completion_witness_status": completion_witness_status,
        "completion_witness_version": COMPLETION_WITNESS_VERSION,
        "completion_witness_scope": (
            "existential_path_per_synthetic_duplicate"
        ),
        "missing_completion_witness_duplicate_indices": (
            missing_completion_witnesses
        ),
        "overall_status": "inconclusive",
        "claim_limit": (
            "synthetic initial board and sampled strategy evidence; "
            "not a client-runtime or all-path proof"
        ),
    }
    return {
        "target": target,
        "coverage": coverage,
        "source": source_report,
        "warning": warning,
        "method": SIMULATION_VERSION,
        "evidence_coverage": evidence_coverage,
        "profile_snapshot": [value.as_dict() for value in parameters],
        "parameter_adjustment": {
            "replaced_a_structure_type_count": len(replaced_a_types),
            "replaced_b_structure_type_count": len(replaced_b_types),
            "preserves_each_duplicate_seed_and_luck": True,
            "underflow": parameter_underflow,
        },
        "duplicates": list(candidate_rows),
        "source_duplicates": list(source_rows),
        "trend": trend,
        "difficulty_distance": multidimensional_distance,
        "qualification_evidence": strategy_qualification,
        "archetype_baseline": (
            "source_template" if source_has_cn else "reference_cohort"
        ),
        "requested_archetype_reference": requested_archetype,
        "source_difficulty_distance": distance,
        "qualified": not reasons,
        "downgrade_reasons": reasons,
    }


def compare_duplicate_difficulty_pair(
    level: LevelTemplate,
    replacements: Sequence[Any],
    *,
    source_duplicate: DuplicateParameter,
    candidate_duplicate: DuplicateParameter,
    duplicate_index: int,
    source_report: Mapping[str, object] | None = None,
    source_materialized_labels: Sequence[str] | None = None,
    candidate_materialized_labels: Sequence[str] | None = None,
    materialization_evidence: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Compare one explicit source/candidate DupPara pair.

    This is the small, public building block used by the consumption
    calibration service.  It deliberately reuses the same source template,
    blocker graph, source-index universe and action-priority protocol for both
    sides.  ``replacements`` are applied only to the candidate.  The helper
    remains a sampled-strategy proxy and does not predict consumption.  A
    caller may pass complete source/candidate label tuples produced by the
    current local-config legacy Seed simulation.  Both tuples are required
    together; omission preserves the historical stable-hash fallback.

    ``source_report`` may be supplied when evaluating several candidate
    parameters against the same source DupPara.  It must be a report returned
    by this helper (the value of its ``source`` key); malformed or cross-template
    values are rejected rather than silently reused.
    """

    if (
        isinstance(duplicate_index, bool)
        or not isinstance(duplicate_index, int)
        or not 1 <= duplicate_index <= 10
    ):
        raise ValueError("duplicate_index must be between 1 and 10")
    if (source_materialized_labels is None) != (
        candidate_materialized_labels is None
    ):
        raise ValueError(
            "source and candidate materialized labels must be supplied together"
        )

    supplied_materialization = source_materialized_labels is not None
    source_labels = (
        None
        if source_materialized_labels is None
        else tuple(source_materialized_labels)
    )
    candidate_labels = (
        None
        if candidate_materialized_labels is None
        else tuple(candidate_materialized_labels)
    )
    for name, labels in (
        ("source", source_labels),
        ("candidate", candidate_labels),
    ):
        if labels is None:
            continue
        if len(labels) != len(level.tiles):
            raise ValueError(
                f"{name} materialized label count must match template tile count"
            )
        if any(not isinstance(label, str) or not label for label in labels):
            raise ValueError(f"{name} materialized labels must be non-empty strings")

    source_label_sha256 = (
        None
        if source_labels is None
        else hashlib.sha256(
            json.dumps(source_labels, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
    )
    candidate_label_sha256 = (
        None
        if candidate_labels is None
        else hashlib.sha256(
            json.dumps(candidate_labels, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
    )
    source_materialization_metadata = (
        dict(materialization_evidence.get("source", {}))
        if isinstance(materialization_evidence, Mapping)
        and isinstance(materialization_evidence.get("source"), Mapping)
        else {}
    )
    source_materialization_metadata_sha256 = hashlib.sha256(
        json.dumps(
            source_materialization_metadata,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()

    graph = build_geometry(level, include_tower=True)
    included = tuple(sorted(graph.included_indices))
    local_index = {
        source_index: offset for offset, source_index in enumerate(included)
    }
    blocker_masks = tuple(
        sum(
            1 << local_index[blocker]
            for blocker in graph.direct_blockers[source_index]
            if blocker in local_index
        )
        for source_index in included
    )
    geometry = (included, blocker_masks)
    geometry_fingerprint = hashlib.sha256(
        repr((included, blocker_masks)).encode("utf-8")
    ).hexdigest()
    pair_context = {
        "template_sha256": level.sha256,
        "geometry_sha256": geometry_fingerprint,
        "simulation_version": SIMULATION_VERSION,
        "materialization_version": MATERIALIZATION_VERSION,
        "common_seed_version": COMMON_SEED_VERSION,
        "source_label_sha256": source_label_sha256,
        "source_materialization_metadata_sha256": (
            source_materialization_metadata_sha256
            if supplied_materialization
            else None
        ),
    }

    if source_report is None:
        source_row = {
            **_simulate_duplicate(
                level,
                (),
                source_duplicate,
                duplicate_index,
                geometry=geometry,
                materialized_labels=source_labels,
            ),
            "pair_context": pair_context,
        }
    else:
        source_row = dict(source_report)
        if int(source_row.get("duplicate_index", -1)) != duplicate_index:
            raise ValueError("source_report duplicate_index does not match")
        source_parameters = source_row.get("parameters")
        if not isinstance(source_parameters, Mapping) or any(
            str(source_parameters.get(name)) != str(expected)
            for name, expected in (
                ("a", source_duplicate.a),
                ("seed", source_duplicate.seed),
                ("luck", source_duplicate.luck),
                ("b", source_duplicate.b),
            )
        ):
            raise ValueError("source_report parameters do not match")
        if source_row.get("pair_context") != pair_context:
            raise ValueError(
                "source_report template, geometry, or simulation versions do not match"
            )

    candidate_row = _simulate_duplicate(
        level,
        replacements,
        candidate_duplicate,
        duplicate_index,
        geometry=geometry,
        materialized_labels=candidate_labels,
    )
    distance = _difficulty_distance(
        (candidate_row,),
        (source_row,),
        board_size=len(included),
        # For an explicit pair the requested baseline is the concrete source
        # board, including a zero C–N reveal curve when the source has no
        # structures.  The cohort reference remains appropriate for the
        # broader candidate-ranking API, but would answer a different question
        # here.
        source_has_cn=True,
        structure_count=len(replacements),
    )
    return {
        "schema_version": 1,
        "duplicate_index": duplicate_index,
        "source": source_row,
        "candidate": candidate_row,
        "difficulty_distance": distance,
        "legacy_score_difference": round(
            abs(
                float(candidate_row.get("difficulty_score", 0.0))
                - float(source_row.get("difficulty_score", 0.0))
            ),
            4,
        ),
        "evidence": {
            "materialization_fidelity": (
                "current_local_config_simulation"
                if supplied_materialization
                else "synthetic"
            ),
            "comparison": "paired_source_index_and_action_priority",
            "simulation_version": SIMULATION_VERSION,
            "materialization_version": MATERIALIZATION_VERSION,
            "common_seed_version": COMMON_SEED_VERSION,
            "template_sha256": level.sha256,
            "geometry_sha256": geometry_fingerprint,
            "source_label_sha256": source_label_sha256,
            "candidate_label_sha256": candidate_label_sha256,
            "materialization": (
                dict(materialization_evidence or {})
                if supplied_materialization
                else {
                    "mode": "synthetic",
                    "assumptions": [
                        "stable_hash_A_B_assignment",
                        "no_client_candidate_pool_replay",
                    ],
                }
            ),
            "claim_limit": (
                "directional sampled-strategy comparison using current local "
                "configuration when supplied; not a causal consumption estimate"
            ),
        },
    }


__all__ = [
    "COMMON_SEED_VERSION",
    "DIFFICULTY_TARGETS",
    "DISTANCE_POLICY_VERSION",
    "DifficultyProfile",
    "DuplicateParameter",
    "MATERIALIZATION_VERSION",
    "QUALIFICATION_POLICY_VERSION",
    "SIMULATION_VERSION",
    "STRATEGY_VECTOR_VERSION",
    "TREND_POLICY_VERSION",
    "difficulty_profile_from_simulation",
    "compare_duplicate_difficulty_pair",
    "simulate_candidate_difficulty",
]
