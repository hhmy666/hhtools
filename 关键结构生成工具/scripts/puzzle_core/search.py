"""Deterministic candidate search for one or two puzzle units.

Every logical attempt owns a derived seed and is evaluated independently.  The
winner is selected only after the fixed attempt budget has completed, so later
parallel execution cannot change results through worker race timing.
"""

from __future__ import annotations

import json
import math
import random
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from itertools import combinations
from types import SimpleNamespace
from typing import Callable, Iterable, Mapping, Sequence

from .errors import (
    CandidateValidationError,
    InputValidationError,
    NoCandidateError,
    ProofLimitExceeded,
)
from .experience import evaluate_static_experience
from .difficulty import (
    DIFFICULTY_TARGETS,
    DifficultyProfile,
    simulate_candidate_difficulty,
)
from .geometry import (
    GeometryGraph,
    build_static_geometry,
    nesting_stats,
    overlap_area,
)
from .models import Layout, ResolvedOutputPlan, TowerMode, UnitRole
from .planning import canonical_json, config_hash, derive_seed, derive_seed_material
from .solver import (
    PuzzleProof,
    SolverLimits,
    analyze_puzzle,
    available_indices,
    minimum_gate_structure_step,
    proof_state_budget_exceeded,
    has_direct_gate_triple,
    proof_checkpoints,
    simulate_order,
    validate_target,
)
from .special_presets import (
    candidate_special_preset_evidence,
    find_long_range_assignment,
)
from .template_reader import LevelTemplate
from .topology import (
    TopologyUnit,
    analyze_existing_structure_isolation,
    analyze_topology,
)
from .tower_reader import allocate_only_mode


A_TILE = -10
B_TILE = -11
STRUCTURE_TYPE_TO_ID = {
    chr(ord("C") + offset): -12 - offset for offset in range(12)
}
STRUCTURE_ID_TO_TYPE = {value: key for key, value in STRUCTURE_TYPE_TO_ID.items()}
# The new majority source has its own explicit generation option/hash.  Keep
# the algorithm version unchanged so saved plans with explicit legacy source
# rules retain their seed and candidate replay identity.
ALGORITHM_VERSION = "puzzle-search-v10"
MAJORITY_A_ON_TIE = "majority_a_on_tie"
# Keep the legacy rule for explicit saved plans.  It selects eligible A first
# and falls back to B only when no A survives eligibility constraints.
PREFER_A_ELSE_B = "prefer_a_else_b"
EXPERIENCE_ARCHETYPES = ("auto", "intrinsic_gate", "board_reveal", "hybrid")
SPECIAL_PRESETS = ("none", "all_types_long_range", "serial_mechanism")
LONG_RANGE_WAVE_COUNTS = ("auto", "3", "4")
ONLINE_STYLE_REFERENCES = ("level_200_300_v1", "off")
_EXPERIENCE_STRATEGY_ORDER = (
    "random",
    "greedy",
    "local_optimal",
    "unlock",
    "adversarial",
)
EXPERIENCE_DIVERSITY_SCALE_VERSION = "level-200-300-mechanism-vector-v2"
ARCHETYPE_MATCH_POLICY_VERSION = "independent-archetype-baseline-v1"
REFERENCE_ARCHETYPE_TARGET_VERSION = "level-200-300-auto-target-v1"
# The no-C--N baseline is a frozen generation target, not a measurement of the
# candidate being ranked.  It reflects the mainstream 200--300 cohort: small
# structures have too little surface area for a stable reveal programme,
# mainstream 9/12-block structures prefer a hybrid mechanism, and the explicit
# long-range grammar targets board reveal.  Updating these weights requires a
# new version and a fresh cohort review.
_REFERENCE_ARCHETYPE_TARGETS: Mapping[str, Mapping[str, float | str]] = {
    "small": {
        "target": "intrinsic_gate",
        "intrinsic_gate": 1.0,
        "hybrid": 0.8,
        "board_reveal": 0.45,
    },
    "mainstream": {
        "target": "hybrid",
        "intrinsic_gate": 0.65,
        "hybrid": 1.0,
        "board_reveal": 0.8,
    },
    "all_types_long_range": {
        "target": "board_reveal",
        "intrinsic_gate": 0.25,
        "hybrid": 0.75,
        "board_reveal": 1.0,
    },
    "serial_mechanism": {
        "target": "hybrid",
        "intrinsic_gate": 0.75,
        "hybrid": 1.0,
        "board_reveal": 0.75,
    },
}
_REFERENCE_ARCHETYPE_COMPATIBILITY_THRESHOLD = 0.75
# Frozen median/robust-scale values for the mainstream 200--300 cohort.  The
# scale is max(IQR / 1.349, MAD * 1.4826, domain floor), frozen here so adding
# a candidate cannot change distances among candidates already generated.
_EXPERIENCE_DIVERSITY_FIELDS = (
    ("initial_clickable_ratio", 0.0, 0.20),
    ("direct_ab_cover_ratio", 1.0, 0.20),
    ("released_ab_per_structure", 0.67, 0.30),
    ("dependency_component_ratio", 0.60, 0.25),
    ("layer_span_ratio", 0.80, 0.25),
    ("online_style_distance", 0.35, 0.25),
)
_DIVERSITY_STATIC_WEIGHT = 0.35
_DIVERSITY_DYNAMIC_WEIGHT = 1.5
_DIVERSITY_CLUSTER_WEIGHT = 1.25
_DYNAMIC_CURVE_METRICS = (
    "current_clickable_structure_count",
    "cumulative_exposed_structure_count",
    "complete_structure_group_type_count",
    "dangling_one_type_count",
    "dangling_two_type_count",
)
_DYNAMIC_GLOBAL_BUCKETS = (
    "first_match_progress",
    "dangling_area",
    "path_spread",
    "median_reveal_strength",
    "worst_reveal_strength",
    "reveal_path_spread",
)


@dataclass(frozen=True)
class UnitGenerationOptions:
    type_count: int = 3
    groups_per_type: tuple[int, ...] = ()
    threshold_type: str | None = None
    allowed_types: tuple[str, ...] = tuple(STRUCTURE_TYPE_TO_ID)
    count_limit: int | None = None
    max_layer: int | None = None
    required_pre_gate_recycles: int = 0
    first_recycle_lead_tiles: int = 2
    preferred_coords: tuple[tuple[int, int, int], ...] = ()
    variable_groups_per_type: bool = False

    def normalized_groups(self) -> tuple[int, ...]:
        groups = self.groups_per_type or (1,) * self.type_count
        if len(groups) != self.type_count or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in groups
        ):
            raise InputValidationError(
                "groups_per_type 必须为每种结构块提供大于 0 的整数"
            )
        return tuple(groups)

    @property
    def tile_count(self) -> int:
        return 3 * sum(self.normalized_groups())


@dataclass(frozen=True)
class QualityOptions:
    position: str = "center"  # center | balanced | unrestricted
    symmetry: str = "medium"  # strong | medium | off
    density: str = "medium"  # compact | medium | dispersed
    difficulty_target: str = "fidelity"  # fidelity | standard | strong
    experience_archetype: str = "auto"
    special_preset: str = "none"
    long_range_wave_count: str = "auto"
    online_style_reference: str = "level_200_300_v1"


@dataclass(frozen=True)
class GenerationOptions:
    automatic_group_max_difference: int = 3
    unit_options: tuple[UnitGenerationOptions, ...] = ()
    tower_options: UnitGenerationOptions | None = None
    replacement_sources: tuple[str, ...] = (MAJORITY_A_ON_TIE,)
    allowed_types: tuple[str, ...] = tuple(STRUCTURE_TYPE_TO_ID)
    total_count_limit: int | None = None
    total_count_minimum: int = 0
    total_percent_minimum: float | None = None
    total_percent_limit: float | None = None
    max_layer: int | None = None
    max_layer_percent: float | None = None
    max_structure_layer_span: int | None = None
    min_same_type_layer_span: int = 0
    quality: QualityOptions = field(default_factory=QualityOptions)
    precision_mode: bool = False
    candidate_count: int = 8
    attempt_budget: int = 300
    assignment_attempts: int = 180
    solver_limits: SolverLimits = field(default_factory=SolverLimits)


@dataclass(frozen=True)
class Replacement:
    tile_index: int
    source_tile_id: int
    structure_type: str
    structure_tile_id: int
    x: int
    y: int
    z: int
    group_id: str
    tower_index: int | None


@dataclass(frozen=True)
class UnitCandidate:
    unit_id: str
    role: str
    target_step: int
    threshold_type: str
    selected_indices: tuple[int, ...]
    assigned_types: tuple[str, ...]
    blocker_masks: tuple[int, ...]
    proof: PuzzleProof
    nested_a_count: int
    nested_b_count: int
    quarter_cover_relations: int
    half_cover_relations: int
    full_cover_relations: int
    external_blocking_a_count: int
    external_blocking_b_count: int
    initial_entry_count: int
    influence_indices: tuple[int, ...]
    intrinsic_target_step: int | None = None

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["proof"] = self.proof.as_dict()
        return result


@dataclass(frozen=True)
class TowerGroupCandidate:
    selected_indices: tuple[int, ...]
    assigned_types: tuple[str, ...]
    tower_counts: tuple[tuple[int, int], ...]
    max_min_difference: int


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    candidate_index: int
    attempt_index: int
    attempt_seed: int
    candidate_seed: int
    algorithm_version: str
    source_template_id: str
    source_sha256: str
    plan_id: str
    plan_signature: str
    plan_config_hash: str
    generation_config_hash: str
    replacements: tuple[Replacement, ...]
    units: tuple[UnitCandidate, ...]
    tower_group: TowerGroupCandidate | None
    quality_score: float
    quality_metrics: Mapping[str, float]
    structure_signature: str
    difficulty_simulation: Mapping[str, object] = field(default_factory=dict)
    experience_analysis: Mapping[str, object] = field(default_factory=dict)
    experience_signature: str = ""
    warnings: tuple[str, ...] = ()
    quality_summary: tuple[str, ...] = ()
    risk_warnings: tuple[str, ...] = ()
    candidate_seed_sha256: str = ""
    candidate_seed_ordinal: int | None = None

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["units"] = [unit.as_dict() for unit in self.units]
        return result


@dataclass(frozen=True)
class SearchResult:
    candidates: tuple[Candidate, ...]
    attempts_completed: int
    rejection_counts: Mapping[str, int]
    candidate_seed: int
    algorithm_version: str = ALGORITHM_VERSION
    candidate_seed_sha256: str = ""
    candidate_seed_ordinal: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "attempts_completed": self.attempts_completed,
            "rejection_counts": dict(self.rejection_counts),
            "candidate_seed": self.candidate_seed,
            "candidate_seed_sha256": self.candidate_seed_sha256,
            "candidate_seed_ordinal": self.candidate_seed_ordinal,
            "algorithm_version": self.algorithm_version,
        }


def _preferred_long_range_wave_count(quality: QualityOptions) -> int | None:
    return (
        None
        if quality.long_range_wave_count == "auto"
        else int(quality.long_range_wave_count)
    )


def _validate_options(plan: ResolvedOutputPlan, options: GenerationOptions) -> None:
    if options.total_percent_minimum is not None:
        if isinstance(options.total_percent_minimum, bool) or not isinstance(options.total_percent_minimum, (int, float)) or not 0 <= options.total_percent_minimum <= 100:
            raise InputValidationError("结构块占比下限必须在 0–100 之间")
        if options.total_count_minimum != 0:
            raise InputValidationError("结构块数量下限与占比下限互斥")
    if isinstance(options.total_count_minimum, bool) or not isinstance(options.total_count_minimum, int) or options.total_count_minimum < 0:
        raise InputValidationError("结构块数量下限必须是非负整数")
    if not isinstance(options.precision_mode, bool):
        raise InputValidationError("precision_mode 必须为布尔值")
    if not 1 <= options.candidate_count <= 20:
        raise InputValidationError("candidate_count 必须在 1–20 之间")
    if options.attempt_budget < 1 or options.assignment_attempts < 1:
        raise InputValidationError("搜索预算必须为正整数")
    sources = tuple(str(value) for value in options.replacement_sources)
    normalized_sources = tuple(
        value.upper() if value.upper() in {"A", "B"} else value.lower()
        for value in sources
    )
    if (
        not normalized_sources
        or len(set(normalized_sources)) != len(normalized_sources)
        or set(normalized_sources)
        - {"A", "B", MAJORITY_A_ON_TIE, PREFER_A_ELSE_B}
        or (
            set(normalized_sources).intersection(
                {MAJORITY_A_ON_TIE, PREFER_A_ELSE_B}
            )
            and normalized_sources
            not in ((MAJORITY_A_ON_TIE,), (PREFER_A_ELSE_B,))
        )
    ):
        raise InputValidationError(
            "replacement_sources 只能选择 A、B、A+B 或 majority_a_on_tie"
        )
    allowed = tuple(str(value).upper() for value in options.allowed_types)
    if not allowed or len(set(allowed)) != len(allowed):
        raise InputValidationError("allowed_types 不能为空或重复")
    invalid = sorted(set(allowed) - set(STRUCTURE_TYPE_TO_ID))
    if invalid:
        raise InputValidationError(
            "结构类型只能使用 C–N", evidence={"invalid_types": invalid}
        )
    expected_units = 0 if plan.tower_mode is TowerMode.ONLY else len(plan.units)
    if len(options.unit_options) != expected_units:
        raise InputValidationError(
            "unit_options 数量必须与 ResolvedOutputPlan 的谜题单元一致"
        )
    if plan.tower_mode is TowerMode.ONLY and options.tower_options is None:
        raise InputValidationError("仅塔内方案必须配置 tower_options")
    if plan.tower_mode is TowerMode.DISALLOW and options.tower_options is not None:
        raise InputValidationError("禁止塔内方案不能配置 tower_options")
    if (
        options.total_percent_limit is not None
        and not 0 < options.total_percent_limit <= 100
    ):
        raise InputValidationError(
            "结构块数量占比上限必须大于 0 且不超过 100%"
        )
    for unit in options.unit_options + (() if options.tower_options is None else (options.tower_options,)):
        if not 1 <= unit.type_count <= 12:
            raise InputValidationError("每个结构组的结构种类数必须在 1–12 之间")
        unit.normalized_groups()
        unit_allowed = tuple(value.upper() for value in unit.allowed_types)
        if not unit_allowed or len(set(unit_allowed)) != len(unit_allowed):
            raise InputValidationError("单元 allowed_types 不能为空或重复")
        if set(unit_allowed) - set(allowed):
            raise InputValidationError("单元 allowed_types 不能超出全局 allowed_types")
        if unit.type_count > len(unit_allowed):
            raise InputValidationError("单元结构种类数超过其 allowed_types 容量")
        if unit.threshold_type and unit.threshold_type.upper() not in unit_allowed:
            raise InputValidationError("锁定门槛块必须位于单元 allowed_types 中")
        if unit.count_limit is not None:
            if unit.count_limit < 0:
                raise InputValidationError("单元 count_limit 不能为负数")
            if unit.tile_count > unit.count_limit:
                raise InputValidationError("单元结构块数超过 count_limit")
        if unit.max_layer is not None and unit.max_layer < 1:
            raise InputValidationError("单元 max_layer 必须大于 0")
        if (
            isinstance(unit.required_pre_gate_recycles, bool)
            or not isinstance(unit.required_pre_gate_recycles, int)
            or unit.required_pre_gate_recycles < 0
        ):
            raise InputValidationError("门槛前回收次数必须是非负整数")
        if (
            isinstance(unit.first_recycle_lead_tiles, bool)
            or not isinstance(unit.first_recycle_lead_tiles, int)
            or unit.first_recycle_lead_tiles < 0
        ):
            raise InputValidationError("首次回收前铺垫块数必须是非负整数")
    for resolved_unit, unit in zip(plan.units, options.unit_options):
        minimum_required = resolved_unit.step + 3 * unit.required_pre_gate_recycles
        if unit.tile_count < minimum_required:
            raise InputValidationError(
                (
                    f"s{resolved_unit.step} 且门槛前回收 "
                    f"{unit.required_pre_gate_recycles} 次，"
                    f"至少需要 {minimum_required} 个结构块；"
                    f"当前为 {unit.tile_count} 个"
                ),
                evidence={
                    "target_step": resolved_unit.step,
                    "required_pre_gate_recycles": (
                        unit.required_pre_gate_recycles
                    ),
                    "required_structure_count": minimum_required,
                    "actual_structure_count": unit.tile_count,
                },
            )
        latest_possible_first_recycle = (
            resolved_unit.step + 2 * unit.required_pre_gate_recycles
        )
        if (
            unit.required_pre_gate_recycles > 0
            and 3 + unit.first_recycle_lead_tiles
            > latest_possible_first_recycle
        ):
            raise InputValidationError(
                (
                    f"s{resolved_unit.step} 且门槛前回收 "
                    f"{unit.required_pre_gate_recycles} 次时，"
                    f"首次回收最迟需在第 "
                    f"{latest_possible_first_recycle} 次点击完成；"
                    f"当前铺垫要求会延后到第 "
                    f"{3 + unit.first_recycle_lead_tiles} 次"
                ),
                evidence={
                    "target_step": resolved_unit.step,
                    "required_pre_gate_recycles": (
                        unit.required_pre_gate_recycles
                    ),
                    "first_recycle_lead_tiles": (
                        unit.first_recycle_lead_tiles
                    ),
                    "latest_possible_first_recycle_click": (
                        latest_possible_first_recycle
                    ),
                },
            )
    if (
        options.max_structure_layer_span is not None
        and (
            isinstance(options.max_structure_layer_span, bool)
            or not isinstance(options.max_structure_layer_span, int)
            or options.max_structure_layer_span < 0
        )
    ):
        raise InputValidationError("结构块最大层间距必须是非负整数")
    if (isinstance(options.min_same_type_layer_span, bool)
            or not isinstance(options.min_same_type_layer_span, int)
            or options.min_same_type_layer_span < 0):
        raise InputValidationError("同种结构块最小间隔层必须是非负整数")
    if (options.max_structure_layer_span is not None
            and options.min_same_type_layer_span > options.max_structure_layer_span):
        raise InputValidationError("同种结构块最小间隔层不能超过结构块最大层间距")
    if options.quality.position not in {"center", "balanced", "unrestricted"}:
        raise InputValidationError("position 偏好不合法")
    if options.quality.symmetry not in {"strong", "medium", "off"}:
        raise InputValidationError("symmetry 偏好不合法")
    if options.quality.density not in {"compact", "medium", "dispersed"}:
        raise InputValidationError("density 偏好不合法")
    if options.quality.difficulty_target not in DIFFICULTY_TARGETS:
        raise InputValidationError("difficulty_target 必须是 fidelity、standard 或 strong")
    if options.quality.experience_archetype not in EXPERIENCE_ARCHETYPES:
        raise InputValidationError("experience_archetype 不合法")
    if options.quality.special_preset not in SPECIAL_PRESETS:
        raise InputValidationError("special_preset 不合法")
    if options.quality.long_range_wave_count not in LONG_RANGE_WAVE_COUNTS:
        raise InputValidationError("long_range_wave_count 必须是 auto、3 或 4")
    if (
        options.quality.special_preset != "all_types_long_range"
        and options.quality.long_range_wave_count != "auto"
    ):
        raise InputValidationError(
            "long_range_wave_count 只适用于 all_types_long_range"
        )
    if options.quality.special_preset == "all_types_long_range" and (
        len(plan.units) != 1
        or options.tower_options is not None
        or plan.layout is not Layout.SINGLE
    ):
        raise InputValidationError(
            "all_types_long_range 仅支持单个塔外结构单元"
        )
    if options.quality.special_preset == "serial_mechanism" and (
        len(plan.units) != 2
        or options.tower_options is not None
        or plan.layout is not Layout.Z
    ):
        raise InputValidationError(
            "serial_mechanism 仅支持 Z 向 high→low 双单元"
        )
    if options.quality.online_style_reference not in ONLINE_STYLE_REFERENCES:
        raise InputValidationError("online_style_reference 不合法")


def _constraint_tiles(level: LevelTemplate, tower_mode: TowerMode) -> tuple:
    """Return the population used by global percentage constraints.

    In ``disallow`` mode tower members cannot be selected, so both percentage
    denominators must describe the effective non-tower board rather than the
    serialized template including unreachable tower stacks.
    """

    if tower_mode is TowerMode.DISALLOW:
        return tuple(tile for tile in level.tiles if not tile.is_tower_member)
    return tuple(level.tiles)


def _effective_max_layer(
    level: LevelTemplate,
    options: GenerationOptions,
    tower_mode: TowerMode = TowerMode.DISALLOW,
) -> int:
    limits = []
    if options.max_layer is not None:
        if options.max_layer < 1:
            raise InputValidationError("max_layer 必须大于 0")
        limits.append(options.max_layer)
    if options.max_layer_percent is not None:
        if not 0 < options.max_layer_percent <= 100:
            raise InputValidationError("max_layer_percent 必须在 0–100 之间")
        effective_tiles = _constraint_tiles(level, tower_mode)
        effective_highest_layer = max(
            (tile.layer for tile in effective_tiles), default=0
        )
        limits.append(
            math.floor(effective_highest_layer * options.max_layer_percent / 100)
        )
    return min(limits) if limits else level.max_layer


def structure_layer_span(
    level: LevelTemplate, indices: Iterable[int]
) -> int | None:
    """Return ``highest layer - lowest layer`` for selected structures."""

    layers = tuple(level.tiles[index].layer for index in indices)
    return max(layers) - min(layers) if layers else None


def _effective_count_limit(
    level: LevelTemplate,
    options: GenerationOptions,
    tower_mode: TowerMode = TowerMode.DISALLOW,
) -> int:
    limits = []
    if options.total_count_limit is not None:
        if options.total_count_limit < 0:
            raise InputValidationError("total_count_limit 不能为负数")
        limits.append(options.total_count_limit)
    if options.total_percent_limit is not None:
        if not 0 < options.total_percent_limit <= 100:
            raise InputValidationError("total_percent_limit 必须在 0–100 之间")
        effective_tiles = _constraint_tiles(level, tower_mode)
        limits.append(
            math.floor(len(effective_tiles) * options.total_percent_limit / 100)
        )
    # Preserve the legacy unconstrained value.  The effective population only
    # changes the denominator of an explicitly configured percentage limit.
    raw = min(limits) if limits else len(level.tiles)
    return raw - raw % 3


def effective_structure_count_minimum(
    level: LevelTemplate,
    options: GenerationOptions,
    tower_mode: TowerMode = TowerMode.DISALLOW,
) -> int:
    if options.total_percent_minimum is not None:
        return math.ceil(len(_constraint_tiles(level, tower_mode)) * options.total_percent_minimum / 100)
    return options.total_count_minimum


def effective_structure_count_limit(
    level: LevelTemplate,
    options: GenerationOptions,
    tower_mode: TowerMode = TowerMode.DISALLOW,
) -> int:
    """Return the global structure-tile ceiling used by candidate search."""

    return _effective_count_limit(level, options, tower_mode)


def feasible_structure_count_limit(
    level: LevelTemplate,
    options: GenerationOptions,
    tower_mode: TowerMode = TowerMode.DISALLOW,
) -> int:
    """Bound random group allocation by legal positions, not just board size.

    This is a necessary bound, not a claim that a placement/proof exists.
    Multi-unit role and topology constraints are still checked during search.
    """
    graph = build_static_geometry(level)
    available = 0
    if options.unit_options:
        pool = _base_eligible(level, graph, options, tower_membership=False, tower_mode=tower_mode)
        available += sum(any(unit.max_layer is None or level.tiles[i].layer <= unit.max_layer
                             for unit in options.unit_options) for i in pool)
    if options.tower_options is not None:
        pool = _base_eligible(level, graph, options, tower_membership=True, tower_mode=tower_mode)
        available += sum(options.tower_options.max_layer is None or
                         level.tiles[i].layer <= options.tower_options.max_layer for i in pool)
    limit = min(_effective_count_limit(level, options, tower_mode), available // 3 * 3)
    minimum = effective_structure_count_minimum(level, options, tower_mode)
    if minimum > limit:
        raise NoCandidateError(
            f"结构数量下限要求至少 {(minimum + 2) // 3 * 3} 格，但当前规则最多允许 {limit} 格；增加搜索次数无效，请调整数量下限或可用位置限制",
            evidence={"minimum": minimum, "available": available, "effective_limit": limit,
                      "rejection_counts": {"structure_position_capacity": 1}},
        )
    return limit


def _board_geometry(level: LevelTemplate) -> tuple[float, float, float, float]:
    if level.level_width and level.level_height:
        return 0.0, float(level.level_width), 0.0, float(level.level_height)
    if not level.tiles:
        return 0.0, 2.0, 0.0, 2.0
    min_x = min(tile.x for tile in level.tiles)
    max_x = max(tile.x + 2 for tile in level.tiles)
    min_y = min(tile.y for tile in level.tiles)
    max_y = max(tile.y + 2 for tile in level.tiles)
    return float(min_x), float(max_x), float(min_y), float(max_y)


def _source_ids(options: GenerationOptions) -> frozenset[int]:
    if _uses_majority_source_rule(options) or _uses_legacy_a_priority(options):
        return frozenset((A_TILE, B_TILE))
    labels = {str(value).upper() for value in options.replacement_sources}
    return frozenset(
        value
        for label, value in (("A", A_TILE), ("B", B_TILE))
        if label in labels
    )


def _uses_majority_source_rule(options: GenerationOptions) -> bool:
    return (
        len(options.replacement_sources) == 1
        and str(options.replacement_sources[0]).lower() == MAJORITY_A_ON_TIE
    )


def _uses_legacy_a_priority(options: GenerationOptions) -> bool:
    return (
        len(options.replacement_sources) == 1
        and str(options.replacement_sources[0]).lower() == PREFER_A_ELSE_B
    )


def _majority_source_tile_id(level: LevelTemplate) -> int:
    """Choose from the original template's actual A/B Tile totals.

    Eligibility constraints are deliberately applied after this decision.  A
    majority source that has too few valid positions must therefore fail
    honestly instead of silently mixing in or falling back to the minority.
    """

    a_count = sum(tile.tile_id == A_TILE for tile in level.tiles)
    b_count = sum(tile.tile_id == B_TILE for tile in level.tiles)
    return A_TILE if a_count >= b_count else B_TILE


def _base_eligible(
    level: LevelTemplate,
    graph: GeometryGraph,
    options: GenerationOptions,
    *,
    tower_membership: bool,
    tower_mode: TowerMode = TowerMode.DISALLOW,
) -> tuple[int, ...]:
    max_layer = _effective_max_layer(level, options, tower_mode)
    source_ids = _source_ids(options)
    existing = {
        tile.index
        for tile in level.tiles
        if tile.tile_id in STRUCTURE_ID_TO_TYPE
    }
    forbidden_adjacent_indices = {
        endpoint
        for relation in graph.relations
        if relation.is_full and relation.z_delta == 1
        for endpoint in (relation.upper_index, relation.lower_index)
    }
    result = []
    for tile in level.tiles:
        if tile.tile_id not in source_ids:
            continue
        if tile.debuff_data_index not in (None, -1):
            continue
        if tile.layer > max_layer:
            continue
        if tile.is_tower_member != tower_membership:
            continue
        if not tower_membership and tile.index in forbidden_adjacent_indices:
            continue
        if not tower_membership and existing:
            # V1 conservative isolation from pre-existing C--N.
            if graph.transitive_blockers[tile.index].intersection(existing):
                continue
            if any(tile.index in graph.transitive_blockers[index] for index in existing):
                continue
            if any(overlap_area(tile, level.tiles[index]) for index in existing):
                continue
        result.append(tile.index)
    if _uses_majority_source_rule(options):
        selected_source_id = _majority_source_tile_id(level)
        result = [
            index
            for index in result
            if level.tiles[index].tile_id == selected_source_id
        ]
    elif _uses_legacy_a_priority(options):
        preferred = tuple(
            index for index in result if level.tiles[index].tile_id == A_TILE
        )
        if preferred:
            return tuple(sorted(preferred))
        result = [index for index in result if level.tiles[index].tile_id == B_TILE]
    return tuple(sorted(result))


def _role_pool(
    level: LevelTemplate,
    pool: Sequence[int],
    role: UnitRole,
    *,
    z_boundary: int | None = None,
) -> tuple[int, ...]:
    min_x, max_x, min_y, max_y = _board_geometry(level)
    center_x = (min_x + max_x) / 2
    center_y = (min_y + max_y) / 2
    result = []
    for index in pool:
        tile = level.tiles[index]
        if role is UnitRole.LEFT and tile.x + 2 > center_x:
            continue
        if role is UnitRole.RIGHT and tile.x < center_x:
            continue
        if role is UnitRole.TOP and tile.y + 2 > center_y:
            continue
        if role is UnitRole.BOTTOM and tile.y < center_y:
            continue
        if role is UnitRole.HIGH and z_boundary is not None and tile.z <= z_boundary:
            continue
        if role is UnitRole.LOW and z_boundary is not None and tile.z > z_boundary:
            continue
        result.append(index)
    return tuple(result)


def _choose_z_boundary(
    level: LevelTemplate, pool: Sequence[int], high_size: int, low_size: int
) -> int:
    values = sorted({level.tiles[index].z for index in pool})
    feasible = [
        boundary
        for boundary in values[:-1]
        if sum(level.tiles[index].z > boundary for index in pool) >= high_size
        and sum(level.tiles[index].z <= boundary for index in pool) >= low_size
    ]
    if not feasible:
        raise NoCandidateError("没有能容纳两个串行谜题单元的非穿插 Z 层带")
    middle = (values[0] + values[-1]) / 2
    return min(feasible, key=lambda value: (abs(value - middle), value))


def _random_topological_order(blocker_masks: Sequence[int], rng: random.Random) -> tuple[int, ...]:
    removed = 0
    order = []
    while len(order) < len(blocker_masks):
        choices = available_indices(removed, blocker_masks)
        if not choices:
            return ()
        index = choices[rng.randrange(len(choices))]
        order.append(index)
        removed |= 1 << index
    return tuple(order)


def _grow_selection(
    level: LevelTemplate,
    graph: GeometryGraph,
    pool: Sequence[int],
    size: int,
    role: UnitRole,
    quality: QualityOptions,
    rng: random.Random,
    required_ancestors: Sequence[int] = (),
) -> tuple[int, ...]:
    if size > len(pool):
        return ()
    min_x, max_x, min_y, max_y = _board_geometry(level)
    target_x = (min_x + max_x) / 2 - 1
    target_y = (min_y + max_y) / 2 - 1
    if role is UnitRole.LEFT:
        target_x = (min_x + target_x) / 2
    elif role is UnitRole.RIGHT:
        target_x = (max_x + target_x) / 2 - 1
    elif role is UnitRole.TOP:
        target_y = (min_y + target_y) / 2
    elif role is UnitRole.BOTTOM:
        target_y = (max_y + target_y) / 2 - 1

    lowers_by_upper: dict[int, set[int]] = defaultdict(set)
    for relation in graph.relations:
        lowers_by_upper[relation.upper_index].add(relation.lower_index)

    def external_coupling(index: int, current: set[int]) -> tuple[int, int]:
        covered_by_ab = sum(
            level.tiles[blocker].tile_id in {A_TILE, B_TILE}
            for blocker in graph.direct_blockers[index]
            if blocker not in current
        )
        releases_ab = sum(
            level.tiles[lower].tile_id in {A_TILE, B_TILE}
            for lower in lowers_by_upper.get(index, ())
            if lower not in current
        )
        return covered_by_ab, releases_ab

    reveal_or_hybrid = quality.experience_archetype in {
        "auto", "board_reveal", "hybrid"
    }
    ranked_seeds = sorted(
        pool,
        key=lambda index: (
            -len(
                graph.transitive_blockers[index].intersection(
                    required_ancestors
                )
            ),
            -sum(external_coupling(index, set()))
            if reveal_or_hybrid
            else 0,
            (level.tiles[index].x - target_x) ** 2
            + (level.tiles[index].y - target_y) ** 2,
            -level.tiles[index].z,
            index,
        ),
    )
    seed_window = ranked_seeds[: min(36, len(ranked_seeds))]
    selected = {seed_window[rng.randrange(len(seed_window))]}
    while len(selected) < size:
        scored = []
        for index in pool:
            if index in selected:
                continue
            tile = level.tiles[index]
            direct_links = sum(
                other in graph.direct_blockers[index]
                or index in graph.direct_blockers[other]
                for other in selected
            )
            transitive_links = sum(
                other in graph.transitive_blockers[index]
                or index in graph.transitive_blockers[other]
                for other in selected
            )
            nearest = min(
                math.dist(
                    (tile.x, tile.y, tile.z),
                    (
                        level.tiles[other].x,
                        level.tiles[other].y,
                        level.tiles[other].z,
                    ),
                )
                for other in selected
            )
            center = (tile.x - target_x) ** 2 + (tile.y - target_y) ** 2
            covered_by_ab, releases_ab = external_coupling(index, selected)
            serial_ancestor_count = len(
                graph.transitive_blockers[index].intersection(
                    required_ancestors
                )
            )
            density_term = nearest if quality.density == "compact" else 0.0
            if quality.density == "dispersed":
                density_term = -nearest
            if quality.experience_archetype == "board_reveal":
                # Start a new cluster near each third boundary, otherwise grow
                # locally.  A/B cover and release are both rewarded because
                # the online reference rhythm is bidirectional.
                desired_links = 0 if len(selected) in {3, 6, 9} else 1
                score = (
                    center * (0.01 if quality.position != "unrestricted" else 0.0)
                    + abs(direct_links - desired_links) * 1.3
                    + max(0, direct_links - 2) * 1.5
                    - covered_by_ab * 2.2
                    - releases_ab * 1.6
                    - min(transitive_links, 3) * 0.15
                )
            elif quality.experience_archetype == "hybrid":
                score = (
                    center * (0.02 if quality.position != "unrestricted" else 0.0)
                    + density_term * 0.15
                    - direct_links * 2.0
                    - transitive_links * 0.25
                    - covered_by_ab * 1.2
                    - releases_ab * 1.0
                )
                if direct_links == 0:
                    score += 1.5
            else:
                score = (
                    center * (0.04 if quality.position != "unrestricted" else 0.0)
                    + density_term * 0.35
                    - direct_links * 4.0
                    - transitive_links * 0.6
                )
                if quality.experience_archetype == "auto":
                    score -= covered_by_ab * 0.8 + releases_ab * 0.6
                if direct_links == 0:
                    score += 4.0
            if quality.special_preset == "all_types_long_range":
                existing_layers = [level.tiles[other].z for other in selected]
                prospective_span = max(existing_layers + [tile.z]) - min(
                    existing_layers + [tile.z]
                )
                score -= prospective_span * 0.8
            if required_ancestors:
                score -= serial_ancestor_count * 8.0
                if serial_ancestor_count == len(set(required_ancestors)):
                    score -= 40.0
            scored.append((score, rng.random(), index))
        if not scored:
            return ()
        scored.sort()
        window = scored[: min(18, len(scored))]
        selected.add(window[rng.randrange(len(window))][2])
    return tuple(sorted(selected))


def _restricted_masks(
    selected: Sequence[int], graph: GeometryGraph
) -> tuple[int, ...]:
    """Build the intrinsic-unit DAG after external A/B are treated as clear.

    ``direct_blockers`` already contains every higher Tile whose own footprint
    overlaps the lower Tile, regardless of layer gap.  Taking the full graph's
    transitive closure here would incorrectly preserve paths that run through
    an external A/B Tile which this evaluation snapshot explicitly removes.
    Dependencies that run through another selected structure arise naturally
    from that structure's own direct edge during solving.
    """

    ordered = tuple(sorted(selected))
    local = {original: index for index, original in enumerate(ordered)}
    masks = []
    for original in ordered:
        mask = 0
        for blocker in sorted(graph.direct_blockers[original].intersection(local)):
            mask |= 1 << local[blocker]
        masks.append(mask)
    return tuple(masks)


def _layer_window_pools(level, pool, maximum_span, required_count):
    """Enumerate distinct, capacious windows before expensive assignment.

    Every legal selection is contained in the window starting at its lowest
    occupied layer. Empty/undersized windows cannot produce that selection.
    """
    if maximum_span is None or not pool:
        return (pool,)
    layers = {index: level.tiles[index].layer for index in pool}
    if max(layers.values()) - min(layers.values()) <= maximum_span:
        return (pool,)
    windows = dict.fromkeys(
        tuple(index for index in pool if low <= layers[index] <= low + maximum_span)
        for low in sorted(set(layers.values()))
    )
    return tuple(window for window in windows if len(window) >= required_count)


def _allocate_labels(
    unit_options: Sequence[UnitGenerationOptions],
    global_allowed: Sequence[str],
    existing_types: Iterable[str],
    rng: random.Random,
) -> tuple[tuple[str, ...], ...]:
    available = [value.upper() for value in global_allowed if value.upper() not in set(existing_types)]
    allocations = []
    used = set()
    for option in unit_options:
        unit_allowed = [
            value.upper()
            for value in option.allowed_types
            if value.upper() in available and value.upper() not in used
        ]
        locked = option.threshold_type.upper() if option.threshold_type else None
        if locked and locked not in unit_allowed:
            raise NoCandidateError("锁定的门槛块已被既有结构或其他单元占用")
        if len(unit_allowed) < option.type_count:
            raise NoCandidateError("C–N 全局互斥类型预算不足")
        shuffled = sorted(unit_allowed)
        rng.shuffle(shuffled)
        selected = [locked] if locked else []
        selected.extend(value for value in shuffled if value != locked)
        labels = tuple(selected[: option.type_count])
        allocations.append(labels)
        used.update(labels)
    return tuple(allocations)


def _guided_zero_recycle_assignment(
    masks, labels, groups, target, locked_gate, rng, limits, layers,
    minimum_span, checkpoint, first_recycle_lead_tiles=2,
):
    """Repair a bounded number of assignments, then require the exact proof.

    Swaps preserve all type counts. Ancestor closures give the earliest
    zero-recycle gate triple: optional clicks cannot lower its pressure
    without a recycle. This objective avoids random rejection sampling.
    """
    if not 4 <= target <= 7 or len(masks) > 96:
        return None
    # A single group's three gate tiles must all span the configured layers.
    # In deep templates this can force too many prerequisite clicks. A gate
    # with two groups can place an early triple and use its other tiles to
    # satisfy the same per-type span, without relaxing either constraint.
    eligible_gates = [i for i, count in enumerate(groups) if 2 <= count <= 4]
    gate = (labels.index(locked_gate.upper()) if locked_gate else
            eligible_gates[rng.randrange(len(eligible_gates))]
            if minimum_span and eligible_gates else
            min(range(len(labels)), key=lambda i: groups[i]))
    if groups[gate] * 3 > 12:
        return None
    ancestors = [mask | (1 << i) for i, mask in enumerate(masks)]
    for j in range(len(masks)):
        for i in range(len(masks)):
            if ancestors[i] & (1 << j):
                ancestors[i] |= ancestors[j]
    triples = tuple(combinations(range(groups[gate] * 3), 3))

    def objective(assignment):
        bits = [0] * len(labels)
        lows, highs = [float('inf')] * len(labels), [float('-inf')] * len(labels)
        positions = []
        for i, label in enumerate(assignment):
            bits[label] |= 1 << i
            if minimum_span:
                lows[label] = min(lows[label], layers[i])
                highs[label] = max(highs[label], layers[i])
            if label == gate:
                positions.append(i)
        span_cost = (sum(max(0, minimum_span - (high - low))
                         for low, high in zip(lows, highs)) if minimum_span else 0)
        minimum, premature = len(masks) + 1, 0
        for x, y, z in triples:
            closure = ancestors[positions[x]] | ancestors[positions[y]] | ancestors[positions[z]]
            if bin(closure & bits[gate]).count('1') != 3:
                continue
            other_counts = [bin(closure & bit).count('1')
                            for label, bit in enumerate(bits) if label != gate]
            if any(count >= 3 for count in other_counts):
                continue
            pressure = 3 + sum(other_counts)
            minimum = min(minimum, pressure)
            premature += max(0, target - pressure)
        return span_cost * 10 + premature * 3 + abs(minimum - target)

    proof_attempts = 0
    tested = set()
    for _restart in range(2):
        assigned = [i for i, count in enumerate(groups) for _ in range(3 * count)]
        rng.shuffle(assigned)
        value = objective(assigned)
        for step in range(400):
            if checkpoint is not None and step % 20 == 0:
                checkpoint('assignment_attempt')
            x, y = rng.sample(range(len(assigned)), 2)
            if assigned[x] == assigned[y]:
                continue
            assigned[x], assigned[y] = assigned[y], assigned[x]
            score = objective(assigned)
            if score == 0 and tuple(assigned) not in tested:
                tested.add(tuple(assigned))
                proof_attempts += 1
                types = tuple(labels[i] for i in assigned)
                try:
                    with proof_checkpoints(checkpoint):
                        proof = analyze_puzzle(masks, types, threshold_type=labels[gate],
                                               limits=limits, reject_invalid_target=target,
                                               first_recycle_lead_tiles=first_recycle_lead_tiles)
                    if validate_target(proof, target)[0]:
                        return types, labels[gate], proof
                except (CandidateValidationError, ProofLimitExceeded):
                    pass
                if proof_attempts >= 2:
                    return None
            temperature = max(.1, 2 * (1 - step / 400))
            if score <= value or rng.random() < math.exp((value - score) / temperature):
                value = score
            else:
                assigned[x], assigned[y] = assigned[y], assigned[x]
    return None


def _find_type_assignment(
    blocker_masks: tuple[int, ...],
    labels: tuple[str, ...],
    groups: tuple[int, ...],
    target: int,
    locked_gate: str | None,
    rng: random.Random,
    assignment_attempts: int,
    solver_limits: SolverLimits,
    required_pre_gate_recycles: int = 0,
    first_recycle_lead_tiles: int = 2,
    cooperative_checkpoint: Callable[[str], None] | None = None,
    required_label_barrier_indices: frozenset[int] | None = None,
    assignment_diagnostics: Counter[str] | None = None,
    tile_layers: tuple[int, ...] = (),
    min_same_type_layer_span: int = 0,
) -> tuple[tuple[str, ...], str, PuzzleProof] | None:
    if min_same_type_layer_span:
        if len(tile_layers) != len(blocker_masks):
            raise InputValidationError("同种结构块层间距检查缺少完整层信息")
        if max(tile_layers, default=0) - min(tile_layers, default=0) < min_same_type_layer_span:
            if assignment_diagnostics is not None:
                assignment_diagnostics["same_type_layer_span"] += 1
            return None
    # Every assignment uses this same geometry and must pass the full proof.
    # If geometry alone exceeds its budget, shuffling labels cannot repair it.
    compressed_proof = len(blocker_masks) > 24 and len(labels) <= len(STRUCTURE_TYPE_TO_ID) and required_pre_gate_recycles == 0
    with proof_checkpoints(cooperative_checkpoint):
        geometry_exceeds_budget = (
            not compressed_proof
            and proof_state_budget_exceeded(blocker_masks, solver_limits.max_states)
        )
    if geometry_exceeds_budget:
        if assignment_diagnostics is not None:
            assignment_diagnostics['assignment_geometry_proof_budget'] += 1
        return None
    if (len(blocker_masks) > 24 and len(labels) >= 5
            and required_pre_gate_recycles == 0 and required_label_barrier_indices is None):
        guided = _guided_zero_recycle_assignment(
            blocker_masks, labels, groups, target, locked_gate, rng, solver_limits,
            tile_layers, min_same_type_layer_span, cooperative_checkpoint,
            first_recycle_lead_tiles,
        )
        if guided is not None:
            return guided
    multiset = [label for label, group_count in zip(labels, groups) for _ in range(3 * group_count)]
    gates = (locked_gate.upper(),) if locked_gate else labels
    tested_assignments: set[tuple[tuple[int, ...], int]] = set()
    for _assignment_index in range(assignment_attempts):
        if cooperative_checkpoint is not None:
            cooperative_checkpoint("assignment_attempt")
        order = _random_topological_order(blocker_masks, rng)
        if not order:
            return None
        sequence = list(multiset)
        rng.shuffle(sequence)
        gate = gates[rng.randrange(len(gates))]
        if (len(sequence) > 24 and len(labels) >= 5 and required_pre_gate_recycles == 0
                and _assignment_index % 2 == 0):
            # Construct a valid intended first gate instead of relying on a
            # random shuffle to hit it. Whole-type runs after the gate reduce
            # simultaneous singleton types; exact graph proof still decides.
            others = [label for label in labels if label != gate]
            rng.shuffle(others)
            lead = [label for label in others for _ in range(2)][:target - 3]
            if len(lead) == target - 3:
                if _assignment_index % 4 == 2:
                    # Keep exposure waves together: a random topological
                    # walk can expose later labels while early labels remain.
                    wave_order = []
                    removed = 0
                    while len(wave_order) < len(blocker_masks):
                        wave = list(available_indices(removed, blocker_masks))
                        rng.shuffle(wave)
                        wave_order.extend(wave)
                        for index in wave:
                            removed |= 1 << index
                    order = tuple(wave_order)
                prefix = [gate, gate, *lead, gate]
                remaining = Counter(multiset)
                remaining.subtract(prefix)
                tails = list(dict.fromkeys([gate, *lead, *others]))
                sequence = prefix + [label for label in tails for _ in range(remaining[label])]
        assigned = [""] * len(sequence)
        for tile_index, label in zip(order, sequence):
            assigned[tile_index] = label
        if min_same_type_layer_span:
            # Check the cheap geometric constraint before allocating a full
            # simulation trace. One pass replaces two scans per type.
            spans: dict[str, tuple[int, int]] = {}
            for label, layer in zip(assigned, tile_layers):
                low, high = spans.get(label, (layer, layer))
                spans[label] = (min(low, layer), max(high, layer))
            if any(high - low < min_same_type_layer_span for low, high in spans.values()):
                if assignment_diagnostics is not None:
                    assignment_diagnostics["same_type_layer_span"] += 1
                continue
        intended = simulate_order(
            tuple(range(len(sequence))), sequence, threshold_type=gate, capacity=None
        )
        if intended.gate_structure_step != target:
            continue
        intended_gate_click = next(
            (
                step.click_number
                for step in intended.steps
                if step.gate_structure_step is not None
            ),
            None,
        )
        intended_pre_gate_recycles = sum(
            step.matched_type is not None
            and (
                intended_gate_click is None
                or step.click_number < intended_gate_click
            )
            for step in intended.steps
        )
        if intended_pre_gate_recycles != required_pre_gate_recycles:
            continue
        if required_pre_gate_recycles == 0:
            if (
                intended.literal_first_match_click != target
                or intended_gate_click != target
            ):
                continue
        elif (
            intended.literal_first_match_click is None
            or intended.literal_first_match_click < 3 + first_recycle_lead_tiles
        ):
            continue
        if required_label_barrier_indices is not None and any(
            not any(
                assigned[index] == label
                for index in required_label_barrier_indices
            )
            for label in labels
        ):
            continue
        names: dict[str, int] = {}
        partition = tuple(names.setdefault(label, len(names)) for label in assigned)
        assignment_identity = (partition, names[gate])
        if assignment_identity in tested_assignments:
            continue
        tested_assignments.add(assignment_identity)
        if compressed_proof and target > 3 and has_direct_gate_triple(blocker_masks, assigned, gate, below_step=target, required_pre_gate_recycles=required_pre_gate_recycles):
            if assignment_diagnostics is not None:
                assignment_diagnostics['assignment_gate_step_mismatch'] += 1
            continue
        if not compressed_proof:
            try:
                minimum = minimum_gate_structure_step(
                    blocker_masks,
                    assigned,
                    threshold_type=gate,
                    max_states=min(solver_limits.max_states, 250_000),
                    required_pre_gate_recycles=required_pre_gate_recycles,
                )
            except ProofLimitExceeded:
                continue
            if minimum != target:
                continue
        try:
            with proof_checkpoints(cooperative_checkpoint):
                proof = analyze_puzzle(
                    blocker_masks,
                    assigned,
                    threshold_type=gate,
                    limits=solver_limits,
                    required_pre_gate_recycles=required_pre_gate_recycles,
                    first_recycle_lead_tiles=first_recycle_lead_tiles,
                    reject_invalid_target=target,
                )
        except (ProofLimitExceeded, CandidateValidationError) as exc:
            if assignment_diagnostics is not None:
                reason = (exc.detail.evidence or {}).get('reason', 'proof_budget')
                assignment_diagnostics['assignment_' + reason] += 1
            continue
        if target == 3:
            by_capacity = {item.capacity: item for item in proof.capacity_proofs}
            capacity_seven = by_capacity.get(7)
            valid = bool(
                proof.minimum_structure_step == 3
                and proof.recommended_path_constraints_ok
                and capacity_seven is not None
                and capacity_seven.complete_exists
            )
        else:
            valid, _reasons = validate_target(
                proof,
                target,
                required_pre_gate_recycles=required_pre_gate_recycles,
                first_recycle_lead_tiles=first_recycle_lead_tiles,
            )
        if valid:
            return tuple(assigned), gate, proof
    return None


def _intrinsic_targets(
    requested_target: int,
    archetype: str,
    attempt_index: int,
) -> tuple[int, ...]:
    """Style may change layout, never the user's requested optimal step."""
    return (requested_target,)


def _quality_score(
    level: LevelTemplate,
    graph: GeometryGraph,
    unit_indices: Sequence[Sequence[int]],
    quality: QualityOptions,
) -> tuple[float, dict[str, float], tuple[str, ...], tuple[str, ...]]:
    """Return a low-weight geometric tie-break with raw audit metrics.

    Whole-board experience, online-style distance and mechanism pressure are
    evaluated independently after the bounded shortlist is built.  In
    particular, nesting and cross-layer overlap are *not* monotonically
    penalised here: the reference cohort uses moderate two-way A/B coupling as
    a deliberate reveal/release mechanism.
    """

    all_indices = [index for unit in unit_indices for index in unit]
    min_x, max_x, min_y, max_y = _board_geometry(level)
    center_x = (min_x + max_x) / 2 - 1
    center_y = (min_y + max_y) / 2 - 1
    centroid_x = sum(level.tiles[index].x for index in all_indices) / len(all_indices)
    centroid_y = sum(level.tiles[index].y for index in all_indices) / len(all_indices)
    center_distance = math.dist((centroid_x, centroid_y), (center_x, center_y))
    points = {
        (level.tiles[index].x, level.tiles[index].y, level.tiles[index].z)
        for index in all_indices
    }
    mirror_x = min_x + max_x - 2
    mirror_y = min_y + max_y - 2
    symmetry_hits = max(
        sum((mirror_x - x, y, z) in points for x, y, z in points),
        sum((x, mirror_y - y, z) in points for x, y, z in points),
        sum((mirror_x - x, mirror_y - y, z) in points for x, y, z in points),
    )
    symmetry_ratio = symmetry_hits / len(points)
    distances = []
    for offset, first in enumerate(all_indices):
        for second in all_indices[offset + 1 :]:
            distances.append(
                math.dist(
                    (level.tiles[first].x, level.tiles[first].y),
                    (level.tiles[second].x, level.tiles[second].y),
                )
            )
    average_distance = sum(distances) / len(distances) if distances else 0.0
    center_weight = 0.0 if quality.position == "unrestricted" else 0.10
    symmetry_weight = {"off": 0.0, "medium": 0.10, "strong": 0.25}[quality.symmetry]
    density_score = 0.0
    if quality.density == "compact":
        density_score = -average_distance
    elif quality.density == "dispersed":
        density_score = average_distance
    center_layout_contribution = -center_distance * center_weight
    symmetry_layout_contribution = symmetry_ratio * symmetry_weight
    density_layout_contribution = density_score * 0.05
    layout_score = (
        center_layout_contribution
        + symmetry_layout_contribution
        + density_layout_contribution
    )

    selected = frozenset(all_indices)
    structure_count = len(selected)
    nesting = nesting_stats(level, selected)
    nested_a_count = len(nesting.nested_a_indices)
    nested_b_count = len(nesting.nested_b_indices)
    unique_nesting_count = nested_a_count + nested_b_count
    cover_relations = (
        nesting.quarter_relations
        + nesting.half_relations
        + nesting.full_relations
    )
    participating_indices = {
        relation.upper_index
        for relation in cover_relations
        if relation.upper_index in selected
    }
    unique_nesting_load = unique_nesting_count / structure_count
    ab_cover_relation_density = len(cover_relations) / structure_count
    repeated_ab_cover_load = max(
        0, len(cover_relations) - unique_nesting_count
    ) / structure_count
    structure_participation_rate = len(participating_indices) / structure_count

    vertical_relations = tuple(
        relation
        for relation in graph.relations
        if relation.upper_index in selected and relation.lower_index in selected
    )
    vertical_overlap_pair_density = len(vertical_relations) / structure_count
    vertical_mean_layer_gap = (
        sum(relation.z_delta for relation in vertical_relations)
        / len(vertical_relations)
        if vertical_relations
        else 0.0
    )
    vertical_cross_layer_share = (
        sum(relation.z_delta >= 2 for relation in vertical_relations)
        / len(vertical_relations)
        if vertical_relations
        else 0.0
    )
    columns: dict[tuple[int, int], set[int]] = defaultdict(set)
    for index in selected:
        tile = level.tiles[index]
        columns[(tile.x, tile.y)].add(tile.z)
    same_column_gap_count = sum(
        max(z_values) - min(z_values) + 1 - len(z_values)
        for z_values in columns.values()
        if len(z_values) > 1
    )
    same_column_gap_per_block = same_column_gap_count / structure_count
    structure_layers = [level.tiles[index].layer for index in selected]
    structure_layer_span = max(structure_layers) - min(structure_layers)

    # Kept as zero-valued compatibility fields for restored historical tasks.
    # New code consumes experience_analysis.simulation_pressure instead.
    size_calibration = 0.0
    repeated_ab_cover_penalty_contribution = 0.0
    unique_nesting_penalty_contribution = 0.0
    structure_participation_penalty_contribution = 0.0
    nesting_penalty = 0.0
    vertical_overlap_penalty_contribution = 0.0
    vertical_layer_gap_penalty_contribution = 0.0
    vertical_cross_layer_penalty_contribution = 0.0
    same_column_gap_penalty_contribution = 0.0
    vertical_penalty = 0.0
    experience_risk_penalty = 0.0
    vertical_signal_count = 0
    score = layout_score

    summary = (
        "布局分仅作最终打平；推荐优先看硬校验、原型、机制压力与难度距离。",
        (
            f"静态审计：{structure_count} 个结构块，{len(cover_relations)} 条 A/B 压层关系，"
            f"层间距 {structure_layer_span}。"
        ),
    )
    risk_warnings: list[str] = []

    return score, {
        "center_distance": center_distance,
        "symmetry_ratio": symmetry_ratio,
        "average_pair_distance": average_distance,
        "center_layout_contribution": center_layout_contribution,
        "symmetry_layout_contribution": symmetry_layout_contribution,
        "density_layout_contribution": density_layout_contribution,
        "layout_score": layout_score,
        "experience_risk_penalty": experience_risk_penalty,
        "nesting_risk_penalty": nesting_penalty,
        "repeated_ab_cover_penalty": (
            repeated_ab_cover_penalty_contribution
        ),
        "unique_nesting_penalty": unique_nesting_penalty_contribution,
        "structure_participation_penalty": (
            structure_participation_penalty_contribution
        ),
        "vertical_risk_penalty": vertical_penalty,
        "vertical_overlap_penalty": vertical_overlap_penalty_contribution,
        "vertical_layer_gap_penalty": vertical_layer_gap_penalty_contribution,
        "vertical_cross_layer_penalty": (
            vertical_cross_layer_penalty_contribution
        ),
        "same_column_gap_penalty": same_column_gap_penalty_contribution,
        "unique_nesting_load": unique_nesting_load,
        "ab_cover_relation_density": ab_cover_relation_density,
        "repeated_ab_cover_load": repeated_ab_cover_load,
        "structure_participation_rate": structure_participation_rate,
        "vertical_overlap_pair_density": vertical_overlap_pair_density,
        "vertical_mean_layer_gap": vertical_mean_layer_gap,
        "vertical_cross_layer_share": vertical_cross_layer_share,
        "same_column_gap_per_block": same_column_gap_per_block,
        "structure_layer_span": float(structure_layer_span),
        "experience_risk_size_calibration": size_calibration,
        "vertical_warning_signal_count": float(vertical_signal_count),
    }, summary, tuple(risk_warnings)


def _tower_selection(
    level: LevelTemplate,
    pool: Sequence[int],
    labels: Sequence[str],
    groups: Sequence[int],
    rng: random.Random,
) -> tuple[tuple[int, ...], tuple[str, ...], TowerGroupCandidate] | None:
    del rng  # label allocation is seeded upstream; placement is deterministic.
    counts = {label: 3 * count for label, count in zip(labels, groups)}
    try:
        allocation = allocate_only_mode(level, pool, counts)
    except InputValidationError:
        return None
    selected = tuple(item.tile_index for item in allocation.assignments)
    assignments = tuple(item.structure_type for item in allocation.assignments)
    tower_counts = tuple(
        (bucket.tower_index, bucket.assigned_count)
        for bucket in allocation.towers
        if bucket.assigned_count
    )
    group = TowerGroupCandidate(
        selected,
        assignments,
        tower_counts,
        allocation.max_tower_count_difference,
    )
    return selected, assignments, group


def generation_options_hash(options: GenerationOptions) -> str:
    """Stable identity of all search-affecting generation parameters."""

    payload = asdict(options)
    if options.min_same_type_layer_span == 0:
        payload.pop("min_same_type_layer_span")
    if options.automatic_group_max_difference == 3:
        payload.pop("automatic_group_max_difference")
    if options.total_count_minimum == 0:
        payload.pop("total_count_minimum")
    if options.total_percent_minimum in (None, 0):
        payload.pop("total_percent_minimum")
    for unit in payload.get("unit_options", ()):
        if not unit.get("variable_groups_per_type"):
            unit.pop("variable_groups_per_type", None)
    tower = payload.get("tower_options")
    if isinstance(tower, dict) and not tower.get("variable_groups_per_type"):
        tower.pop("variable_groups_per_type", None)
    if not options.precision_mode:
        # Preserve the historical candidate identity and derived attempt
        # sequence for ordinary requests, including explicit false.
        payload.pop("precision_mode")
    return config_hash(payload)


def canonical_structure_identity(signature: str) -> str:
    """Deduplicate global label permutations without changing saved identities.

    Coordinates and same-type partitions remain exact. Existing candidate IDs
    and wire-validation signatures stay untouched, including old snapshots.
    """
    try:
        payload = json.loads(signature)
        names: dict[str, int] = {}
        for group in sorted(payload, key=lambda item: item["group"]):
            group["tiles"].sort(key=lambda row: tuple(row[:3]))
            for row in group["tiles"]:
                label = row[3]
                if label not in names:
                    names[label] = len(names)
                row[3] = names[label]
        return canonical_json(sorted(payload, key=lambda item: item["group"]))
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return signature


def candidate_structure_signature(
    level: LevelTemplate,
    unit_payloads: Sequence[tuple[Sequence[int], Sequence[str]]],
    tower_payload: tuple[Sequence[int], Sequence[str]] | None,
) -> str:
    payload = []
    for unit_index, (indices, types) in enumerate(unit_payloads):
        payload.append(
            {
                "group": f"U{unit_index + 1}",
                "tiles": [
                    [level.tiles[index].x, level.tiles[index].y, level.tiles[index].z, label]
                    for index, label in zip(indices, types)
                ],
            }
        )
    if tower_payload:
        payload.append(
            {
                "group": "tower",
                "tiles": [
                    [level.tiles[index].x, level.tiles[index].y, level.tiles[index].z, label]
                    for index, label in zip(*tower_payload)
                ],
            }
        )
    return canonical_json(payload)


def _median_from_distribution(value: object) -> float | None:
    if not isinstance(value, Mapping):
        return None
    raw = value.get("median")
    try:
        return None if raw is None else float(raw)
    except (TypeError, ValueError):
        return None


def _median(values: Sequence[float]) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _strategy_reveal_strength(
    reveal: Mapping[str, object],
    *,
    expected_structure_count: int,
) -> tuple[float, dict[str, float]]:
    """Derive one strategy's reveal mechanism strength without trial weighting.

    The score combines staged exposure, first-match timing, A/B release, and
    dangling exposure.  It is a mechanism descriptor rather than calibrated
    player risk.  Every strategy contributes one score regardless of how many
    random trials were used to estimate its distributions.
    """

    raw_curve = reveal.get("curve")
    curve = (
        [row for row in raw_curve if isinstance(row, Mapping)]
        if isinstance(raw_curve, Sequence)
        and not isinstance(raw_curve, (str, bytes, bytearray))
        else []
    )
    exposed_values = [
        value
        for row in curve
        for value in [
            _median_from_distribution(row.get("cumulative_exposed_structure_count"))
        ]
        if value is not None
    ]
    observed_total = max(exposed_values, default=0.0)
    denominator = max(float(expected_structure_count), observed_total, 1.0)
    components: dict[str, float] = {}
    if exposed_values:
        early = exposed_values[0]
        late = exposed_values[-1]
        components["initially_hidden_share"] = max(
            0.0, min(1.0, 1.0 - early / denominator)
        )
        components["staged_exposure_growth"] = max(
            0.0, min(1.0, (late - early) / denominator)
        )

    raw_matches = reveal.get("first_three_structure_matches")
    first = raw_matches.get("1") if isinstance(raw_matches, Mapping) else None
    first_progress = (
        _median_from_distribution(first.get("progress"))
        if isinstance(first, Mapping)
        else None
    )
    if first_progress is not None:
        components["first_match_progress"] = max(
            0.0, min(1.0, first_progress)
        )

    release_total = _median_from_distribution(
        reveal.get("released_ab_total_per_path")
    )
    if release_total is not None:
        components["ab_release_share"] = max(
            0.0, min(1.0, release_total / denominator)
        )

    dangling_one = _median_from_distribution(reveal.get("dangling_one_area"))
    dangling_two = _median_from_distribution(reveal.get("dangling_two_area"))
    if dangling_one is not None or dangling_two is not None:
        components["dangling_exposure_pressure"] = max(
            0.0,
            min(
                1.0,
                ((dangling_one or 0.0) + 1.5 * (dangling_two or 0.0))
                / denominator,
            ),
        )

    weights = {
        "initially_hidden_share": 0.25,
        "staged_exposure_growth": 0.25,
        "first_match_progress": 0.20,
        "ab_release_share": 0.20,
        "dangling_exposure_pressure": 0.10,
    }
    available_weight = sum(weights[name] for name in components)
    strength = (
        sum(components[name] * weights[name] for name in components)
        / available_weight
        if available_weight
        else 0.0
    )
    return round(strength, 6), {
        name: round(value, 6) for name, value in components.items()
    }


def _experience_dynamic_inputs(
    candidate: Candidate,
    simulation: Mapping[str, object] | None,
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    if not simulation:
        return None, {
            "intrinsic_gate_step": max(
                (
                    unit.intrinsic_target_step or unit.target_step
                    for unit in candidate.units
                ),
                default=3,
            )
        }
    raw_duplicates = simulation.get("duplicates")
    duplicates = (
        [item for item in raw_duplicates if isinstance(item, Mapping)]
        if isinstance(raw_duplicates, Sequence)
        and not isinstance(raw_duplicates, (str, bytes))
        else []
    )
    # The legacy aggregate gives each strategy one compatibility vote and is
    # intentionally excluded from qualification, distance, and recommendation.
    # Mechanism pressure therefore consumes the explicit per-strategy vector.
    failure_rates: list[float] = []
    per_duplicate_path_spread: list[dict[str, object]] = []
    per_duplicate_strategy_reveal: list[dict[str, object]] = []
    reveal_strength_values: list[float] = []
    reveal_spreads: list[float] = []
    reveal_rows: list[Mapping[str, object]] = []
    dangling_areas: list[float] = []
    first_match_progress: list[float] = []
    expected_structure_count = max(
        len(getattr(candidate, "replacements", ())),
        sum(len(getattr(unit, "selected_indices", ())) for unit in candidate.units),
        1,
    )
    for duplicate_offset, item in enumerate(duplicates, 1):
        raw_failure_vector = item.get("strategy_failure_vector")
        if isinstance(raw_failure_vector, Mapping):
            failure_rates.extend(
                float(value)
                for value in raw_failure_vector.values()
                if isinstance(value, (int, float))
            )
        strategies = item.get("strategies")
        if isinstance(strategies, Mapping):
            probability_vector = {
                strategy: float(summary["pass_probability"])
                for strategy in _EXPERIENCE_STRATEGY_ORDER
                for summary in [strategies.get(strategy)]
                if isinstance(summary, Mapping)
                and isinstance(summary.get("pass_probability"), (int, float))
            }
            duplicate_spread = (
                max(probability_vector.values())
                - min(probability_vector.values())
                if probability_vector
                else 0.0
            )
            per_duplicate_path_spread.append(
                {
                    "duplicate_index": int(
                        item.get("duplicate_index", duplicate_offset)
                    ),
                    "strategy_pass_probability": {
                        name: round(value, 6)
                        for name, value in probability_vector.items()
                    },
                    "spread": round(duplicate_spread, 6),
                }
            )

            strategy_reveals: dict[str, object] = {}
            duplicate_strengths: list[float] = []
            for strategy in _EXPERIENCE_STRATEGY_ORDER:
                summary = strategies.get(strategy)
                strategy_reveal = (
                    summary.get("reveal_curve")
                    if isinstance(summary, Mapping)
                    else None
                )
                if not isinstance(strategy_reveal, Mapping):
                    continue
                strength, components = _strategy_reveal_strength(
                    strategy_reveal,
                    expected_structure_count=expected_structure_count,
                )
                strategy_reveals[strategy] = {
                    "reveal_strength": strength,
                    "components": components,
                    # Retain the strategy-local curve so experience
                    # fingerprinting can distinguish mechanism shapes while
                    # still giving every strategy exactly one vote.
                    "reveal_curve": dict(strategy_reveal),
                }
                duplicate_strengths.append(strength)
                reveal_strength_values.append(strength)
                dangling = sum(
                    value or 0.0
                    for value in (
                        _median_from_distribution(
                            strategy_reveal.get("dangling_one_area")
                        ),
                        _median_from_distribution(
                            strategy_reveal.get("dangling_two_area")
                        ),
                    )
                )
                dangling_areas.append(dangling)
                matches = strategy_reveal.get("first_three_structure_matches")
                first = matches.get("1") if isinstance(matches, Mapping) else None
                progress = (
                    _median_from_distribution(first.get("progress"))
                    if isinstance(first, Mapping)
                    else None
                )
                if progress is not None:
                    first_match_progress.append(progress)
            if strategy_reveals:
                reveal_spread = max(duplicate_strengths) - min(
                    duplicate_strengths
                )
                reveal_spreads.append(reveal_spread)
                per_duplicate_strategy_reveal.append(
                    {
                        "duplicate_index": int(
                            item.get("duplicate_index", duplicate_offset)
                        ),
                        "strategies": strategy_reveals,
                        "median_reveal_strength": round(
                            _median(duplicate_strengths), 6
                        ),
                        "worst_reveal_strength": round(
                            max(duplicate_strengths), 6
                        ),
                        "reveal_strength_spread": round(reveal_spread, 6),
                    }
                )
        reveal = item.get("whole_board_reveal")
        if not isinstance(reveal, Mapping):
            continue
        reveal_rows.append(reveal)
    path_spreads = [
        float(item["spread"]) for item in per_duplicate_path_spread
    ]
    path_spread = max(path_spreads, default=0.0)
    median_path_spread = _median(path_spreads)
    median_reveal_strength = _median(reveal_strength_values)
    worst_reveal_strength = max(reveal_strength_values, default=0.0)
    reveal_path_spread = max(reveal_spreads, default=0.0)
    reveals: dict[str, object] = {
        "version": "multi-path-reveal-v2",
        "aggregation_policy": "each_strategy_one_vote_per_duplicate_v1",
        # Retained for detailed UI evidence only.  Classification, pressure,
        # signature, and ranking consume the equal-weight strategy vectors.
        "per_duplicate": reveal_rows,
        "pooled_duplicate_rows_used_for_scoring": False,
        "per_duplicate_strategy_reveal": per_duplicate_strategy_reveal,
        "dangling_area": max(dangling_areas, default=0.0),
        "path_spread": path_spread,
        "per_duplicate_path_spread": per_duplicate_path_spread,
        "median_path_spread": median_path_spread,
        "reveal_strength": median_reveal_strength,
        "median_reveal_strength": median_reveal_strength,
        "worst_reveal_strength": worst_reveal_strength,
        "reveal_path_spread": reveal_path_spread,
        "first_match_progress": (
            _median(first_match_progress)
            if first_match_progress
            else 0.0
        ),
    }
    difficulty = {
        "intrinsic_gate_step": max(
            (
                unit.intrinsic_target_step or unit.target_step
                for unit in candidate.units
            ),
            default=3,
        ),
        "worst_failure_rate": max(failure_rates, default=0.0),
        "path_spread": path_spread,
        "per_duplicate_path_spread": per_duplicate_path_spread,
        "median_path_spread": median_path_spread,
        "archetype_baseline": str(
            simulation.get("archetype_baseline", "reference_cohort")
        ),
    }
    return reveals, difficulty


def _reference_archetype_target(
    *, level: LevelTemplate, structure_count: int, special_preset: str
) -> dict[str, object]:
    """Return a frozen no-C--N target without inspecting candidate geometry."""

    if special_preset in {"all_types_long_range", "serial_mechanism"}:
        target_group = special_preset
    elif structure_count <= 6:
        target_group = "small"
    else:
        target_group = "mainstream"
    policy = _REFERENCE_ARCHETYPE_TARGETS[target_group]
    base_compatibility = {
        name: float(policy[name])
        for name in ("intrinsic_gate", "board_reveal", "hybrid")
    }
    graph = build_static_geometry(level)
    source_ab = tuple(
        index
        for index in sorted(graph.included_indices)
        if level.tiles[index].tile_id in {A_TILE, B_TILE}
    )
    count = max(len(source_ab), 1)
    covered_ratio = sum(
        bool(graph.direct_blockers[index]) for index in source_ab
    ) / count
    release_ratio = sum(
        bool(graph.direct_blocked_tiles[index]) for index in source_ab
    ) / count
    bridge_ratio = sum(
        bool(graph.direct_blockers[index])
        and bool(graph.direct_blocked_tiles[index])
        for index in source_ab
    ) / count
    layer_span = (
        max(level.tiles[index].z for index in source_ab)
        - min(level.tiles[index].z for index in source_ab)
        if source_ab
        else 0
    )
    topology = {
        "source_ab_count": len(source_ab),
        "covered_ratio": round(covered_ratio, 6),
        "release_ratio": round(release_ratio, 6),
        "bridge_ratio": round(bridge_ratio, 6),
        "layer_span": layer_span,
    }
    # Special grammars and very small targets retain their explicit frozen
    # target.  Mainstream auto uses only source-board topology (never candidate
    # placement) to weight the cohort prior.
    if target_group == "mainstream":
        reveal_opportunity = (
            covered_ratio + release_ratio + min(layer_span / 4.0, 1.0)
        ) / 3.0
        hybrid_opportunity = (
            reveal_opportunity + min(bridge_ratio / 0.25, 1.0)
        ) / 2.0
        intrinsic_opportunity = 1.0 - 0.5 * reveal_opportunity
        opportunity = {
            "intrinsic_gate": intrinsic_opportunity,
            "board_reveal": reveal_opportunity,
            "hybrid": hybrid_opportunity,
        }
        raw_compatibility = {
            name: round(
                base_compatibility[name] * (0.6 + 0.4 * opportunity[name]),
                6,
            )
            for name in base_compatibility
        }
        target_archetype = max(
            raw_compatibility,
            key=lambda name: (
                raw_compatibility[name],
                name == "hybrid",
                name,
            ),
        )
        normalization = max(raw_compatibility.values(), default=1.0) or 1.0
        compatibility = {
            name: round(value / normalization, 6)
            for name, value in raw_compatibility.items()
        }
    else:
        compatibility = base_compatibility
        target_archetype = str(policy["target"])
    return {
        "baseline": "reference_cohort",
        "target_source": "versioned_generation_target",
        "target_version": REFERENCE_ARCHETYPE_TARGET_VERSION,
        "reference_cohort": "level_200_300_key_structure_v1",
        "target_group": target_group,
        "target_archetype": target_archetype,
        "base_compatibility_scores": base_compatibility,
        "compatibility_scores": compatibility,
        "compatible_archetypes": [
            name
            for name, score in compatibility.items()
            if score >= _REFERENCE_ARCHETYPE_COMPATIBILITY_THRESHOLD
        ],
        "compatibility_threshold": (
            _REFERENCE_ARCHETYPE_COMPATIBILITY_THRESHOLD
        ),
        "inputs": {
            "target_structure_count": int(structure_count),
            "special_preset": special_preset,
            "source_board_topology": topology,
        },
        "candidate_features_used_to_choose_target": False,
    }


def _source_template_archetype(
    level: LevelTemplate,
    simulation: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """Classify existing source C--N independently from candidate replacements."""

    graph = build_static_geometry(level)
    source_structure_indices = tuple(
        index
        for index in sorted(graph.included_indices)
        if level.tiles[index].tile_id in STRUCTURE_ID_TO_TYPE
    )
    if not source_structure_indices:
        return None

    source_reveals: dict[str, object] | None = None
    source_difficulty: dict[str, object] | None = None
    if simulation:
        source_duplicates = simulation.get("source_duplicates")
        if (
            isinstance(source_duplicates, Sequence)
            and not isinstance(source_duplicates, (str, bytes, bytearray))
        ):
            source_reveals, source_difficulty = _experience_dynamic_inputs(
                SimpleNamespace(units=(), replacements=()),
                {
                    "duplicates": source_duplicates,
                    "archetype_baseline": "source_template",
                },
            )
    source_report = evaluate_static_experience(
        level,
        requested_archetype="auto",
        reveals=source_reveals,
        difficulty=source_difficulty,
    )
    raw = source_report.get("archetype")
    source_archetype = dict(raw) if isinstance(raw, Mapping) else {}
    return {
        "baseline": "source_template",
        "target_source": "independent_source_template_classification",
        "source_template_id": level.template_id,
        "source_sha256": level.sha256,
        "source_structure_count": len(source_structure_indices),
        "target_archetype": source_archetype.get("primary"),
        "classification": source_archetype,
        "candidate_features_used_to_choose_target": False,
    }


def _archetype_match_evidence(
    *,
    level: LevelTemplate,
    candidate_archetype: Mapping[str, object],
    requested_archetype: str,
    structure_count: int,
    special_preset: str,
    simulation: Mapping[str, object] | None,
) -> dict[str, object]:
    """Compare candidate classification with an independent target baseline."""

    candidate_scores = candidate_archetype.get("scores")
    scores = (
        {
            name: float(candidate_scores.get(name, 0.0))
            for name in ("intrinsic_gate", "board_reveal", "hybrid")
        }
        if isinstance(candidate_scores, Mapping)
        else {name: 0.0 for name in ("intrinsic_gate", "board_reveal", "hybrid")}
    )
    candidate_status = str(
        candidate_archetype.get("classification_status", "uncertain")
    )
    candidate_primary = candidate_archetype.get("primary")
    candidate_automatic = candidate_archetype.get("automatic_primary")

    if requested_archetype != "auto":
        target: dict[str, object] = {
            "baseline": "user_selection",
            "target_source": "explicit_user_selection",
            "target_archetype": requested_archetype,
            "compatible_archetypes": [requested_archetype],
            "compatibility_scores": {
                name: 1.0 if name == requested_archetype else 0.0
                for name in scores
            },
            "candidate_features_used_to_choose_target": False,
        }
    else:
        source_target = _source_template_archetype(level, simulation)
    if requested_archetype == "auto" and source_target is not None:
        target = source_target
        source_classification = source_target.get("classification")
        source_status = (
            str(source_classification.get("classification_status", "uncertain"))
            if isinstance(source_classification, Mapping)
            else "uncertain"
        )
        source_primary = source_target.get("target_archetype")
        target["compatible_archetypes"] = (
            [source_primary]
            if source_status == "classified" and source_primary in scores
            else []
        )
        target["compatibility_scores"] = {
            name: 1.0 if name == source_primary else 0.0 for name in scores
        }
    elif requested_archetype == "auto":
        target = _reference_archetype_target(
            level=level,
            structure_count=structure_count,
            special_preset=special_preset,
        )

    target_archetype = target.get("target_archetype")
    compatibility_scores = target.get("compatibility_scores")
    compatibility = (
        {
            name: float(compatibility_scores.get(name, 0.0))
            for name in scores
        }
        if isinstance(compatibility_scores, Mapping)
        else {name: 0.0 for name in scores}
    )
    target_available = target_archetype in scores
    match_score = scores[str(target_archetype)] if target_available else 0.0
    candidate_compatibility = (
        compatibility.get(str(candidate_automatic), 0.0)
        if candidate_status == "classified"
        else 0.0
    )
    compatible = (
        candidate_status == "classified"
        and candidate_compatibility
        >= _REFERENCE_ARCHETYPE_COMPATIBILITY_THRESHOLD
    )
    matches_target = (
        candidate_status == "classified"
        and target_available
        and candidate_primary == target_archetype
    )
    if not target_available or candidate_status != "classified":
        status = "inconclusive"
    elif matches_target:
        status = "matched"
    elif compatible:
        status = "compatible"
    else:
        status = "not_matched"
    return {
        "version": ARCHETYPE_MATCH_POLICY_VERSION,
        "status": status,
        "baseline": target.get("baseline"),
        "target_source": target.get("target_source"),
        "target_version": target.get("target_version"),
        "reference_cohort": target.get("reference_cohort"),
        "target_group": target.get("target_group"),
        "target_archetype": target_archetype,
        "compatible_archetypes": list(
            target.get("compatible_archetypes", ())
        ),
        "compatibility_scores": compatibility,
        "compatibility_threshold": (
            _REFERENCE_ARCHETYPE_COMPATIBILITY_THRESHOLD
        ),
        "matches_target": matches_target,
        "compatible": compatible,
        "match_score": round(match_score, 6),
        "candidate_compatibility": round(candidate_compatibility, 6),
        "candidate_classification": {
            "status": candidate_status,
            "primary": candidate_primary,
            "automatic_primary": candidate_automatic,
            "confidence": candidate_archetype.get(
                "classification_confidence"
            ),
        },
        "source_template_classification": target.get("classification"),
        "source_template_id": target.get("source_template_id"),
        "source_sha256": target.get("source_sha256"),
        "source_structure_count": target.get("source_structure_count"),
        "inputs": target.get("inputs"),
        "candidate_features_used_to_choose_target": False,
        "claim_limit": (
            "archetype compatibility is a versioned mechanism target, not a "
            "player outcome probability"
        ),
    }


def evaluate_candidate_experience(
    level: LevelTemplate,
    candidate: Candidate,
    quality: QualityOptions,
    simulation: Mapping[str, object] | None = None,
) -> dict[str, object]:
    reveals, difficulty = _experience_dynamic_inputs(candidate, simulation)
    special_evidence = (
        candidate_special_preset_evidence(
            build_static_geometry(level),
            candidate,
            preferred_wave_count=(
                _preferred_long_range_wave_count(quality)
                if quality.special_preset == "all_types_long_range"
                else None
            ),
        )
        if quality.special_preset != "none"
        else {}
    )
    if special_evidence:
        reveals = {**(reveals or {}), **special_evidence}
    assignments = {
        replacement.tile_index: replacement.structure_type
        for replacement in candidate.replacements
    }
    report = evaluate_static_experience(
        level,
        tuple(sorted(assignments)),
        assignments,
        requested_archetype=quality.experience_archetype,
        reveals=reveals,
        difficulty=difficulty,
        requested_special_preset=quality.special_preset,
    )
    report = dict(report)
    archetype = dict(report.get("archetype", {}))
    requested = str(archetype.get("requested", "auto"))
    archetype_match = _archetype_match_evidence(
        level=level,
        candidate_archetype=archetype,
        requested_archetype=requested,
        structure_count=len(assignments),
        special_preset=quality.special_preset,
        simulation=simulation,
    )
    # Retain compatibility keys for older readers, but source them from the
    # independent match contract.  In auto mode these no longer compare a
    # candidate with its own automatic label.
    archetype["baseline"] = archetype_match.get("baseline")
    archetype["target_archetype"] = archetype_match.get("target_archetype")
    archetype["requested_match_score"] = float(
        archetype_match.get("match_score", 0.0)
    )
    archetype["requested_matches_automatic"] = bool(
        archetype_match.get("matches_target")
    )
    archetype["match_score"] = float(archetype_match.get("match_score", 0.0))
    report["archetype"] = archetype
    report["archetype_match"] = archetype_match
    style = dict(report.get("online_style_distance", {}))
    style["total"] = (
        0.0
        if quality.online_style_reference == "off"
        else float(style.get("distance", 0.0))
    )
    if quality.online_style_reference == "off":
        style["status"] = "off"
    report["online_style_distance"] = style
    special_presets = report.get("special_presets")
    selected_special: Mapping[str, object] = {}
    if quality.special_preset != "none" and isinstance(special_presets, Mapping):
        raw_special = special_presets.get(quality.special_preset)
        if isinstance(raw_special, Mapping):
            selected_special = raw_special
    special_status = str(selected_special.get("status", "matched"))
    static_status = (
        "proved"
        if quality.special_preset == "none" or special_status == "matched"
        else "disproved"
        if special_status == "rejected"
        else "inconclusive"
    )
    report["special_preset"] = (
        {"requested": "none", "status": "not_applicable"}
        if quality.special_preset == "none"
        else {"requested": quality.special_preset, **dict(selected_special)}
    )
    simulation_evidence = (
        simulation.get("evidence_coverage")
        if isinstance(simulation, Mapping)
        else None
    )
    witness_status = (
        str(
            simulation_evidence.get(
                "completion_witness_status", "inconclusive"
            )
        )
        if isinstance(simulation_evidence, Mapping)
        else "inconclusive"
    )
    if static_status == "disproved":
        hard_status = "disproved"
    elif static_status == "proved" and witness_status == "proved":
        hard_status = "proved"
    else:
        hard_status = "inconclusive"
    hard_reasons = list(selected_special.get("reasons", ()))
    if witness_status != "proved":
        missing = (
            simulation_evidence.get(
                "missing_completion_witness_duplicate_indices", ()
            )
            if isinstance(simulation_evidence, Mapping)
            else ()
        )
        hard_reasons.append(
            "尚未为每个建模副本证明至少一条可复验的整盘完成路径"
            + (f"（缺少副本 {list(missing)}）" if missing else "")
        )
    report["hard_validation"] = {
        "status": hard_status,
        "passed": hard_status == "proved",
        "reasons": hard_reasons,
        "static_rule_status": static_status,
        "whole_board_completion_witness_status": witness_status,
        "scope": (
            "exact_structure_rules_and_existential_whole_board_witness_"
            "on_reconstructed_duplicates"
        ),
        "claim_limit": (
            "不声明所有玩家路径安全，也不等同客户端运行时精确物化"
        ),
    }
    evidence = dict(report.get("evidence_coverage", {}))
    if isinstance(simulation_evidence, Mapping):
        evidence.update(simulation_evidence)
        evidence["overall"] = str(
            simulation_evidence.get("overall_status", "inconclusive")
        )
    else:
        evidence["overall"] = "inconclusive"
    if hard_status == "disproved":
        evidence["overall"] = "reject"
    elif hard_status == "inconclusive":
        evidence["overall"] = "inconclusive"
    report["evidence_coverage"] = evidence
    report["reveal_curve"] = reveals or {
        "status": "unavailable",
        "reason": "dynamic_path_evidence_not_run",
    }
    report["difficulty_distance"] = (
        dict(simulation.get("difficulty_distance", {}))
        if isinstance(simulation, Mapping)
        and isinstance(simulation.get("difficulty_distance"), Mapping)
        else {}
    )
    return report


def _robust_standardize(value: float, center: float, scale: float) -> float:
    return max(-4.0, min(4.0, (float(value) - center) / max(scale, 1e-9)))


def _normalized_bucket(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, min(1.0, float(value) / 4.0))


def _mean_bucket(values: Iterable[object]) -> float:
    normalized = [
        bucket
        for value in values
        for bucket in [_normalized_bucket(value)]
        if bucket is not None
    ]
    return sum(normalized) / len(normalized) if normalized else 0.0


def _dynamic_reveal_diversity_vector(
    report: Mapping[str, object],
) -> tuple[float, ...]:
    """Flatten the existing five-stage signature into a fixed mechanism vector.

    Every DupPara/strategy retains one vote.  Values are already frozen
    interval buckets from ``experience.py``; averaging those buckets keeps the
    vector independent of simulation trial counts while preserving stage
    shape, suspended one/two-tile areas, first-three-match rhythm, and release
    behavior.
    """

    raw_evidence = report.get("experience_signature_evidence")
    evidence = raw_evidence if isinstance(raw_evidence, Mapping) else {}
    raw_reveal = evidence.get("reveal_buckets")
    reveal = raw_reveal if isinstance(raw_reveal, Mapping) else {}
    raw_full = reveal.get("full_curve_and_release")
    duplicate_rows = (
        [item for item in raw_full if isinstance(item, Mapping)]
        if isinstance(raw_full, Sequence)
        and not isinstance(raw_full, (str, bytes, bytearray))
        else []
    )

    compact_curves: list[Mapping[str, object]] = []
    strategy_strengths: dict[str, list[object]] = {
        strategy: [] for strategy in _EXPERIENCE_STRATEGY_ORDER
    }
    duplicate_summary: dict[str, list[object]] = {
        "median_reveal_strength": [],
        "worst_reveal_strength": [],
        "reveal_strength_spread": [],
    }
    for duplicate in duplicate_rows:
        raw_strategy_curves = duplicate.get("strategy_curve_and_release")
        if isinstance(raw_strategy_curves, Mapping):
            ordered_names = tuple(_EXPERIENCE_STRATEGY_ORDER) + tuple(
                sorted(
                    str(name)
                    for name in raw_strategy_curves
                    if str(name) not in _EXPERIENCE_STRATEGY_ORDER
                )
            )
            for name in ordered_names:
                curve = raw_strategy_curves.get(name)
                if isinstance(curve, Mapping):
                    compact_curves.append(curve)
            raw_strengths = duplicate.get("strategy_reveal_strength")
            if isinstance(raw_strengths, Mapping):
                for strategy in _EXPERIENCE_STRATEGY_ORDER:
                    strategy_strengths[strategy].append(
                        raw_strengths.get(strategy)
                    )
            for name in duplicate_summary:
                duplicate_summary[name].append(duplicate.get(name))
        elif "curve" in duplicate:
            # Compatibility path for signatures produced from legacy pooled
            # per-duplicate reveal rows.
            compact_curves.append(duplicate)

    values: list[float] = [1.0 if duplicate_rows else 0.0]
    for stage in range(5):
        for metric in _DYNAMIC_CURVE_METRICS:
            stage_values: list[object] = []
            for compact in compact_curves:
                raw_curve = compact.get("curve")
                if not isinstance(raw_curve, Sequence) or isinstance(
                    raw_curve, (str, bytes, bytearray)
                ):
                    continue
                if stage >= len(raw_curve) or not isinstance(
                    raw_curve[stage], Mapping
                ):
                    continue
                stage_values.append(raw_curve[stage].get(metric))
            values.append(_mean_bucket(stage_values))

    for ordinal in range(3):
        values.append(
            _mean_bucket(
                compact.get("match_progress", ())[ordinal]
                for compact in compact_curves
                if isinstance(compact.get("match_progress"), Sequence)
                and not isinstance(
                    compact.get("match_progress"),
                    (str, bytes, bytearray),
                )
                and len(compact.get("match_progress", ())) > ordinal
            )
        )
    for name in (
        "release_per_click",
        "release_on_completion",
        "release_total",
    ):
        values.append(_mean_bucket(compact.get(name) for compact in compact_curves))

    raw_dynamic = reveal.get("dynamic")
    dynamic = raw_dynamic if isinstance(raw_dynamic, Mapping) else {}
    values.extend(
        _mean_bucket((dynamic.get(name),)) for name in _DYNAMIC_GLOBAL_BUCKETS
    )
    raw_spreads = reveal.get("per_duplicate_path_spread")
    spreads = (
        list(raw_spreads)
        if isinstance(raw_spreads, Sequence)
        and not isinstance(raw_spreads, (str, bytes, bytearray))
        else []
    )
    values.extend(
        (
            _mean_bucket(spreads),
            max(
                (
                    value
                    for item in spreads
                    for value in [_normalized_bucket(item)]
                    if value is not None
                ),
                default=0.0,
            ),
        )
    )
    values.extend(
        _mean_bucket(strategy_strengths[strategy])
        for strategy in _EXPERIENCE_STRATEGY_ORDER
    )
    values.extend(
        _mean_bucket(duplicate_summary[name])
        for name in (
            "median_reveal_strength",
            "worst_reveal_strength",
            "reveal_strength_spread",
        )
    )
    return tuple(values)


def _cluster_topology_diversity_vector(
    report: Mapping[str, object],
) -> tuple[float, ...]:
    """Return label-invariant cluster/DAG shape and relation features."""

    raw_graph = report.get("composite_cluster_graph")
    graph = raw_graph if isinstance(raw_graph, Mapping) else {}
    raw_clusters = graph.get("clusters")
    clusters = (
        [item for item in raw_clusters if isinstance(item, Mapping)]
        if isinstance(raw_clusters, Sequence)
        and not isinstance(raw_clusters, (str, bytes, bytearray))
        else []
    )
    node_count = len(clusters)
    source_ids: list[object] = []
    for offset, cluster in enumerate(clusters, 1):
        source_id = cluster.get("cluster_id", offset)
        if source_id in source_ids:
            source_id = ("row", offset)
        source_ids.append(source_id)
    position_by_source = {
        source_id: position for position, source_id in enumerate(source_ids)
    }
    edges: set[tuple[int, int]] = set()
    raw_dag = graph.get("dependency_dag")
    if isinstance(raw_dag, Sequence) and not isinstance(
        raw_dag, (str, bytes, bytearray)
    ):
        for edge in raw_dag:
            if not isinstance(edge, Mapping):
                continue
            source = position_by_source.get(edge.get("from_cluster"))
            target = position_by_source.get(edge.get("to_cluster"))
            if source is not None and target is not None and source != target:
                edges.add((source, target))

    indegree = [0] * node_count
    outdegree = [0] * node_count
    reachability = [set() for _ in range(node_count)]
    for source, target in edges:
        outdegree[source] += 1
        indegree[target] += 1
        reachability[source].add(target)
    for _ in range(node_count):
        for source in range(node_count):
            expanded = set(reachability[source])
            for target in tuple(reachability[source]):
                expanded.update(reachability[target])
            reachability[source] = expanded

    depths = [0] * node_count
    for _ in range(max(node_count - 1, 0)):
        next_depths = list(depths)
        for source, target in edges:
            next_depths[target] = max(next_depths[target], depths[source] + 1)
        depths = next_depths

    raw_metrics = report.get("metrics")
    metrics = raw_metrics if isinstance(raw_metrics, Mapping) else {}
    structure_count = max(
        1.0,
        float(
            metrics.get(
                "structure_count",
                sum(
                    float(cluster.get("size", 0.0)) for cluster in clusters
                ),
            )
        ),
    )
    sizes = [float(cluster.get("size", 0.0)) for cluster in clusters]
    pair_total = max(node_count * (node_count - 1) / 2.0, 1.0)
    possible_edges = max(node_count * (node_count - 1), 1)
    raw_relations = graph.get("pair_relation_counts")
    relations = raw_relations if isinstance(raw_relations, Mapping) else {}

    values = [
        1.0 if clusters else 0.0,
        min(1.0, node_count / structure_count),
        len(edges) / possible_edges,
        sum(len(targets) for targets in reachability) / possible_edges,
        (
            sum(value == 0 for value in indegree) / node_count
            if node_count
            else 0.0
        ),
        (
            sum(value == 0 for value in outdegree) / node_count
            if node_count
            else 0.0
        ),
        max(depths, default=0) / max(node_count - 1, 1),
        min(1.0, float(relations.get("parallel", 0.0)) / pair_total),
        min(1.0, float(relations.get("overlap", 0.0)) / pair_total),
        min(1.0, float(relations.get("serial", 0.0)) / pair_total),
        max(sizes, default=0.0) / structure_count,
        min(sizes, default=0.0) / structure_count,
        (
            math.sqrt(
                sum((size - sum(sizes) / len(sizes)) ** 2 for size in sizes)
                / len(sizes)
            )
            / structure_count
            if sizes
            else 0.0
        ),
    ]
    for degrees in (indegree, outdegree):
        values.extend(
            (
                sum(value == bucket for value in degrees) / node_count
                if node_count
                else 0.0
            )
            for bucket in range(3)
        )
        values.append(
            sum(value >= 3 for value in degrees) / node_count
            if node_count
            else 0.0
        )
    return tuple(values)


def _experience_mechanism_fingerprint(candidate: Candidate) -> str:
    """Return a coordinate-free dynamic/topology stratum for diverse picking."""

    report = candidate.experience_analysis
    if not isinstance(report, Mapping):
        return "{}"
    raw_evidence = report.get("experience_signature_evidence")
    evidence = raw_evidence if isinstance(raw_evidence, Mapping) else {}
    canonical_clusters = evidence.get("composite_cluster_graph")
    reveal = evidence.get("reveal_buckets")
    payload = {
        "composite_cluster_graph": (
            canonical_clusters
            if isinstance(canonical_clusters, Mapping)
            else list(_cluster_topology_diversity_vector(report))
        ),
        "reveal_buckets": (
            reveal
            if isinstance(reveal, Mapping)
            else list(_dynamic_reveal_diversity_vector(report))
        ),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _experience_vector(
    candidate: Candidate, *, include_style: bool = True
) -> tuple[float, ...]:
    report = candidate.experience_analysis
    report = report if isinstance(report, Mapping) else {}
    raw_metrics = report.get("metrics")
    metrics_available = isinstance(raw_metrics, Mapping)
    metrics = raw_metrics if metrics_available else {}
    style = report.get("online_style_distance")
    count = max(1.0, float(metrics.get("structure_count", 1.0)))
    raw = {
        "initial_clickable_ratio": (
            float(metrics.get("initial_clickable_count", 0.0)) / count
        ),
        "direct_ab_cover_ratio": float(
            metrics.get("direct_ab_cover_ratio", 0.0)
        ),
        "released_ab_per_structure": float(
            metrics.get("released_ab_per_structure", 0.0)
        ),
        "dependency_component_ratio": min(
            1.0,
            float(metrics.get("dependency_component_count", 0.0)) / 5.0,
        ),
        "layer_span_ratio": min(
            1.0, float(metrics.get("layer_span", 0.0)) / 5.0
        ),
        "online_style_distance": (
            float(style.get("total", style.get("distance", 0.0)))
            if isinstance(style, Mapping)
            else 0.0
        ),
    }
    static_values = (
        tuple(
            _robust_standardize(raw[name], center, scale)
            * _DIVERSITY_STATIC_WEIGHT
            for name, center, scale in _EXPERIENCE_DIVERSITY_FIELDS
        )
        if metrics_available
        else (0.0,) * len(_EXPERIENCE_DIVERSITY_FIELDS)
    )
    if not include_style:
        static_values = static_values[:-1]
    dynamic_values = tuple(
        value * _DIVERSITY_DYNAMIC_WEIGHT
        for value in _dynamic_reveal_diversity_vector(report)
    )
    topology_values = tuple(
        value * _DIVERSITY_CLUSTER_WEIGHT
        for value in _cluster_topology_diversity_vector(report)
    )
    return dynamic_values + topology_values + static_values


def _select_difficulty_evaluation_pool(
    candidates: Sequence[Candidate], limit: int
) -> list[Candidate]:
    """Keep an archetype-stratified, topology-diverse simulation pool.

    This preselection intentionally excludes online-style and layout scores;
    those are later ranking stages and must not decide who receives the more
    expensive DupPara simulation.
    """

    ordered = sorted(
        candidates,
        key=lambda item: (
            item.structure_signature,
            item.attempt_index,
            item.attempt_seed,
        ),
    )
    if len(ordered) <= limit:
        return ordered
    selected: list[Candidate] = []
    seen_archetypes: set[str] = set()
    for candidate in ordered:
        archetype = candidate.experience_analysis.get("archetype", {})
        automatic = (
            archetype.get("automatic_primary")
            if isinstance(archetype, Mapping)
            else None
        )
        classification = (
            archetype.get("classification_status")
            if isinstance(archetype, Mapping)
            else None
        )
        stratum = (
            "uncertain"
            if classification == "uncertain" or automatic in (None, "")
            else str(automatic)
        )
        if stratum not in seen_archetypes:
            selected.append(candidate)
            seen_archetypes.add(stratum)
        if len(selected) >= limit:
            return selected
    remaining = [candidate for candidate in ordered if candidate not in selected]
    # These immutable inputs do not change while selecting. Scope the caches
    # to this pool so candidate reports are released when selection returns.
    vectors = {id(item): _experience_vector(item, include_style=False)
               for item in ordered}
    mechanisms = {id(item): _experience_mechanism_fingerprint(item)
                  for item in ordered}
    while remaining and len(selected) < limit:
        selected_vectors = [
            vectors[id(item)] for item in selected
        ]
        selected_mechanisms = {
            mechanisms[id(item)] for item in selected
        }

        def minimum_distance(item: Candidate) -> float:
            vector = vectors[id(item)]
            return min(
                sum(
                    (first - second) ** 2
                    for first, second in zip(vector, other)
                )
                for other in selected_vectors
            )

        winner = max(
            enumerate(remaining),
            key=lambda pair: (
                mechanisms[id(pair[1])]
                not in selected_mechanisms,
                minimum_distance(pair[1]),
                -pair[0],
                pair[1].experience_signature,
            ),
        )[0]
        selected.append(remaining.pop(winner))
    return selected


def _select_experience_diverse(
    ranked: Sequence[Candidate], limit: int
) -> list[Candidate]:
    unique: list[Candidate] = []
    seen = set()
    for candidate in ranked:
        signature = candidate.experience_signature or candidate.structure_signature
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(candidate)
    if len(unique) <= limit:
        return unique
    selected = [unique[0]]
    remaining = unique[1:]
    vectors = {id(item): _experience_vector(item) for item in unique}
    mechanisms = {id(item): _experience_mechanism_fingerprint(item)
                  for item in unique}
    while remaining and len(selected) < limit:
        selected_vectors = [vectors[id(item)] for item in selected]
        selected_mechanisms = {
            mechanisms[id(item)] for item in selected
        }

        def minimum_distance(item: Candidate) -> float:
            vector = vectors[id(item)]
            return min(
                sum((first - second) ** 2 for first, second in zip(vector, other))
                for other in selected_vectors
            )

        winner = max(
            enumerate(remaining),
            key=lambda pair: (
                mechanisms[id(pair[1])]
                not in selected_mechanisms,
                minimum_distance(pair[1]),
                -pair[0],
                pair[1].experience_signature,
            ),
        )[0]
        selected.append(remaining.pop(winner))
    return selected


def search_candidates(
    level: LevelTemplate,
    plan: ResolvedOutputPlan,
    options: GenerationOptions,
    *,
    candidate_seed: int,
    candidate_seed_sha256: str | None = None,
    candidate_index: int | None = None,
    candidate_seed_ordinal: int | None = None,
    exclude_structure_signatures: Iterable[str] = (),
    exclude_experience_signatures: Iterable[str] = (),
    attempt_start_index: int = 0,
    attempt_count: int | None = None,
    difficulty_profile: DifficultyProfile | None = None,
    cooperative_checkpoint: Callable[[str], None] | None = None,
    final_candidate_acceptance: Callable[[Candidate], bool] | None = None,
    search_progress: Callable[[int, Mapping[str, int]], None] | None = None,
) -> SearchResult:
    """Run one deterministic attempt window and return its best candidates.

    ``options.attempt_budget`` remains the total search ceiling recorded in
    candidate identity and validation.  Continuations may set
    ``attempt_start_index``/``attempt_count`` to inspect only the newly added
    range instead of replaying attempts that were already exhausted.
    """

    _validate_options(plan, options)
    if candidate_seed_ordinal is None:
        candidate_seed_ordinal = candidate_index
    if candidate_seed_ordinal is not None and (
        isinstance(candidate_seed_ordinal, bool)
        or not isinstance(candidate_seed_ordinal, int)
        or candidate_seed_ordinal < 1
    ):
        raise InputValidationError("candidate_seed_ordinal 必须为正整数")
    if isinstance(candidate_seed, bool) or not isinstance(candidate_seed, int):
        raise InputValidationError("candidate_seed 必须为非负整数")
    if candidate_seed < 0:
        raise InputValidationError("candidate_seed 必须为非负整数")
    if candidate_index is not None:
        if (
            isinstance(candidate_index, bool)
            or not isinstance(candidate_index, int)
            or candidate_index < 1
            or candidate_index > options.candidate_count
        ):
            raise InputValidationError("candidate_index 必须位于候选数量范围内")
    if (
        isinstance(attempt_start_index, bool)
        or not isinstance(attempt_start_index, int)
        or attempt_start_index < 0
        or attempt_start_index > options.attempt_budget
    ):
        raise InputValidationError(
            "attempt_start_index 必须位于 0 与搜索总预算之间"
        )
    if attempt_count is None:
        attempt_count = options.attempt_budget - attempt_start_index
    if (
        isinstance(attempt_count, bool)
        or not isinstance(attempt_count, int)
        or attempt_count < 1
        or attempt_start_index + attempt_count > options.attempt_budget
    ):
        raise InputValidationError(
            "attempt_count 必须是未超过搜索总预算的正整数"
        )
    excluded_signatures = frozenset(exclude_structure_signatures)
    if any(
        not isinstance(signature, str) or not signature
        for signature in excluded_signatures
    ):
        raise InputValidationError(
            "exclude_structure_signatures 必须为非空字符串集合"
        )
    excluded_signatures = frozenset(canonical_structure_identity(value) for value in excluded_signatures)
    excluded_experience_signatures = frozenset(exclude_experience_signatures)
    if any(
        not isinstance(signature, str) or not signature
        for signature in excluded_experience_signatures
    ):
        raise InputValidationError(
            "exclude_experience_signatures 必须为非空字符串集合"
        )
    if candidate_seed_sha256 is None:
        candidate_seed_sha256 = derive_seed_material(
            "externally_supplied_candidate_seed_v1", candidate_seed
        ).sha256
    if (
        not isinstance(candidate_seed_sha256, str)
        or len(candidate_seed_sha256) != 64
        or any(value not in "0123456789abcdef" for value in candidate_seed_sha256)
    ):
        raise InputValidationError("candidate_seed_sha256 必须是小写完整 SHA-256")
    if options.precision_mode and (
        difficulty_profile is None
        or difficulty_profile.coverage != "complete"
        or len(difficulty_profile.duplicate_params) != 10
    ):
        # The difficulty model unconditionally rejects every candidate when
        # the original DupPara1–10 is not uniquely known.  This invariant is
        # independent of search attempts, seeds and replacement layouts.
        raise NoCandidateError(
            "精准模式缺少唯一可确认的原关卡 DupPara1–10，无法证明难度目标；请调整配置",
            evidence={
                "profile_coverage": (
                    "missing" if difficulty_profile is None else difficulty_profile.coverage
                ),
                "profile_parameter_count": (
                    0 if difficulty_profile is None
                    else len(difficulty_profile.duplicate_params)
                ),
                "rejection_counts": {"precision_profile_coverage": 1},
            },
        )
    graph = build_static_geometry(level)
    options_hash = generation_options_hash(options)
    existing_types = {
        STRUCTURE_ID_TO_TYPE[tile.tile_id]
        for tile in level.tiles
        if tile.tile_id in STRUCTURE_ID_TO_TYPE
    }
    existing_static_indices = tuple(
        tile.index
        for tile in level.tiles
        if tile.tile_id in STRUCTURE_ID_TO_TYPE and not tile.is_tower_member
    )
    generation_groups = list(options.unit_options)
    if options.tower_options is not None:
        generation_groups.append(options.tower_options)
    required_total = sum(group.tile_count for group in generation_groups)
    minimum_count = effective_structure_count_minimum(level, options, plan.tower_mode)
    if minimum_count > _effective_count_limit(level, options, plan.tower_mode):
        raise NoCandidateError("结构块数量下限超过有效上限", evidence={"minimum": minimum_count, "effective_limit": _effective_count_limit(level, options, plan.tower_mode)})
    if required_total < minimum_count:
        raise NoCandidateError(
            "新增结构块数量未达到数量下限",
            evidence={"required": required_total, "minimum": minimum_count},
        )
    if required_total > _effective_count_limit(level, options, plan.tower_mode):
        raise NoCandidateError(
            "新增结构块数量超过数量/百分比上限",
            evidence={
                "required": required_total,
                "effective_limit": _effective_count_limit(
                    level, options, plan.tower_mode
                ),
            },
        )
    static_pool = _base_eligible(
        level,
        graph,
        options,
        tower_membership=False,
        tower_mode=plan.tower_mode,
    )
    tower_pool = _base_eligible(
        level,
        graph,
        options,
        tower_membership=True,
        tower_mode=plan.tower_mode,
    )
    selection_pools = (static_pool,)
    if (options.tower_options is None
            and not any(unit.preferred_coords for unit in options.unit_options)):
        bounded_pools = _layer_window_pools(
            level, static_pool, options.max_structure_layer_span,
            sum(unit.tile_count for unit in options.unit_options),
        )
        if bounded_pools:
            selection_pools = bounded_pools
    z_boundary = None
    if plan.layout is Layout.Z:
        z_boundary = _choose_z_boundary(
            level,
            static_pool,
            options.unit_options[0].tile_count,
            options.unit_options[1].tile_count,
        )

    accepted: dict[str, Candidate] = {}
    rejections: Counter[str] = Counter()
    retained_rule_failure_codes: set[str] = set()
    attempt_stop_index = attempt_start_index + attempt_count
    for attempt_index in range(attempt_start_index, attempt_stop_index):
        if search_progress is not None:
            search_progress(attempt_index - attempt_start_index, dict(rejections))
        if cooperative_checkpoint is not None:
            cooperative_checkpoint("search_attempt")
        # Product-rule failures discovered after a concrete set of source
        # Tiles has been chosen do not make the wire rewrite unsafe.  Keep the
        # attempt alive so it can become a reviewable candidate; independent
        # validation will reproduce these conclusions and mark it as not
        # automatically recommendable.  Failures that leave us without a
        # complete assignment/proof still cannot form a Candidate and remain
        # ordinary search rejections below.
        rule_failure_codes: set[str] = set()
        attempt_seed = derive_seed(
            candidate_seed, "attempt", attempt_index, ALGORITHM_VERSION
        )
        rng = random.Random(attempt_seed)
        selection_pool = (selection_pools[0] if len(selection_pools) == 1
                          else selection_pools[rng.randrange(len(selection_pools))])
        try:
            label_allocations = _allocate_labels(
                generation_groups, options.allowed_types, existing_types, rng
            )
        except NoCandidateError:
            raise
        selected_by_unit = []
        unit_payloads = []
        used_indices: set[int] = set()
        failed = False
        for unit_index, (unit_spec, unit_options) in enumerate(
            zip(plan.units, options.unit_options)
        ):
            role_pool = tuple(
                index
                for index in _role_pool(
                    level, selection_pool, unit_spec.role, z_boundary=z_boundary
                )
                if index not in used_indices
                and (
                    unit_options.max_layer is None
                    or level.tiles[index].layer <= unit_options.max_layer
                )
            )
            if unit_options.preferred_coords:
                by_coord = {
                    (level.tiles[index].x, level.tiles[index].y, level.tiles[index].z): index
                    for index in role_pool
                }
                if any(coord not in by_coord for coord in unit_options.preferred_coords):
                    rejections["preferred_coord_unavailable"] += 1
                    failed = True
                    break
                selected = tuple(sorted(by_coord[coord] for coord in unit_options.preferred_coords))
                if len(selected) != unit_options.tile_count:
                    rejections["preferred_coord_count"] += 1
                    failed = True
                    break
            else:
                selected = _grow_selection(
                    level,
                    graph,
                    role_pool,
                    unit_options.tile_count,
                    unit_spec.role,
                    options.quality,
                    rng,
                    (
                        selected_by_unit[0]
                        if options.quality.special_preset == "serial_mechanism"
                        and unit_index == 1
                        and selected_by_unit
                        else ()
                    ),
                )
            if not selected:
                rejections["geometry_pool"] += 1
                failed = True
                break
            used_indices.update(selected)
            selected_by_unit.append(selected)
        if failed:
            continue
        # Reject an already-too-tall static selection before topology analysis
        # and exact path solving.  Adding a tower group cannot reduce the
        # highest-minus-lowest span, so this is a safe and substantially
        # cheaper early check; the complete selection is checked again below.
        selected_static_indices = tuple(
            index for selected in selected_by_unit for index in selected
        )
        if (
            options.max_structure_layer_span is not None
            and selected_static_indices
        ):
            selected_static_span = structure_layer_span(
                level, selected_static_indices
            )
            assert selected_static_span is not None
            if selected_static_span > options.max_structure_layer_span:
                rejections["rule_warning_structure_layer_span"] += 1
                if "structure_layer_span" in retained_rule_failure_codes:
                    continue
                rule_failure_codes.add("structure_layer_span")
        topology_units = tuple(
            TopologyUnit(
                unit_id=unit.unit_id,
                role=unit.role,
                target_step=unit.step,
                selected_indices=tuple(selected),
            )
            for unit, selected in zip(plan.units, selected_by_unit)
        )
        topology = None
        if topology_units:
            assert plan.layout is not None
            topology = analyze_topology(
                level,
                graph,
                plan.layout,
                topology_units,
                serial_difficulty=plan.serial_difficulty,
            )
            if not topology.passed:
                for reason in topology.reasons:
                    rejections[f"rule_warning_topology_{reason}"] += 1
                if "unit_topology" in retained_rule_failure_codes:
                    continue
                rule_failure_codes.add("unit_topology")
        legacy_isolation = analyze_existing_structure_isolation(
            level,
            graph,
            topology_units,
            existing_structure_indices=existing_static_indices,
        )
        if not legacy_isolation.passed:
            rejections["rule_warning_existing_structure_conflict"] += 1
            if "existing_structure_isolation" in retained_rule_failure_codes:
                continue
            rule_failure_codes.add("existing_structure_isolation")

        unit_candidates = []
        for unit_index, (unit_spec, unit_options, selected) in enumerate(
            zip(plan.units, options.unit_options, selected_by_unit)
        ):
            masks = _restricted_masks(selected, graph)
            allocation = label_allocations[unit_index]
            assigned = None
            intrinsic_target = unit_spec.step
            target_attempts = (
                (unit_spec.step,)
                if options.quality.special_preset == "all_types_long_range"
                else _intrinsic_targets(
                    unit_spec.step,
                    options.quality.experience_archetype,
                    attempt_index,
                )
            )
            # An explicit pre-gate recycle count is a hard puzzle contract,
            # not a ranking preference.  Reveal-style s3 exploration is only
            # valid for units without that contract; otherwise it could admit
            # a candidate whose board rhythm looks good while silently
            # dropping the configured recycle gate.
            if unit_options.required_pre_gate_recycles > 0:
                target_attempts = (unit_spec.step,)
            attempts_per_target = max(
                1, options.assignment_attempts // len(target_attempts)
            )
            for candidate_target in target_attempts:
                if options.quality.special_preset == "all_types_long_range":
                    long_range = find_long_range_assignment(
                        masks,
                        allocation,
                        target_step=candidate_target,
                        rng=rng,
                        assignment_attempts=attempts_per_target,
                        limits=options.solver_limits,
                        required_pre_gate_recycles=(
                            unit_options.required_pre_gate_recycles
                        ),
                        first_recycle_lead_tiles=(
                            unit_options.first_recycle_lead_tiles
                        ),
                        preferred_wave_count=_preferred_long_range_wave_count(
                            options.quality
                        ),
                    )
                    candidate_assigned = (
                        None
                        if long_range is None
                        else (long_range[0], long_range[1], long_range[2])
                    )
                else:
                    serial_barriers: frozenset[int] | None = None
                    if (
                        options.quality.special_preset == "serial_mechanism"
                        and unit_index == 1
                        and selected_by_unit
                    ):
                        prerequisite = frozenset(selected_by_unit[0])
                        serial_barriers = frozenset(
                            local_index
                            for local_index, original_index in enumerate(
                                sorted(selected)
                            )
                            if prerequisite.issubset(
                                graph.transitive_blockers[original_index]
                            )
                        )
                    candidate_assigned = _find_type_assignment(
                        masks,
                        allocation,
                        unit_options.normalized_groups(),
                        candidate_target,
                        unit_options.threshold_type,
                        rng,
                        attempts_per_target,
                        options.solver_limits,
                        unit_options.required_pre_gate_recycles,
                        unit_options.first_recycle_lead_tiles,
                        cooperative_checkpoint,
                        serial_barriers,
                        rejections,
                        tile_layers=tuple(level.tiles[index].layer for index in sorted(selected)),
                        min_same_type_layer_span=options.min_same_type_layer_span,
                    )
                if candidate_assigned is not None:
                    assigned = candidate_assigned
                    intrinsic_target = candidate_target
                    break
            if assigned is None:
                rejections["type_assignment_or_proof"] += 1
                failed = True
                break
            types, threshold, proof = assigned
            assert topology is not None
            topology_report = topology.unit_reports[unit_index]
            influence = set(topology_report.influence_indices)
            entries = topology_report.initial_entry_count
            if len(plan.units) == 2 and plan.layout in (Layout.X, Layout.Y) and not entries:
                rejections["rule_warning_no_initial_entry"] += 1
                if "no_initial_entry" in retained_rule_failure_codes:
                    failed = True
                    break
                rule_failure_codes.add("no_initial_entry")
            unit_candidates.append(
                UnitCandidate(
                    unit_id=unit_spec.unit_id,
                    role=unit_spec.role.value,
                    target_step=unit_spec.step,
                    threshold_type=threshold,
                    selected_indices=tuple(sorted(selected)),
                    assigned_types=types,
                    blocker_masks=masks,
                    proof=proof,
                    nested_a_count=len(topology_report.nested_a_indices),
                    nested_b_count=len(topology_report.nested_b_indices),
                    quarter_cover_relations=topology_report.quarter_cover_relations,
                    half_cover_relations=topology_report.half_cover_relations,
                    full_cover_relations=topology_report.full_cover_relations,
                    external_blocking_a_count=len(topology_report.blocking_a_indices),
                    external_blocking_b_count=len(topology_report.blocking_b_indices),
                    initial_entry_count=entries,
                    influence_indices=tuple(sorted(influence)),
                    intrinsic_target_step=intrinsic_target,
                )
            )
            unit_payloads.append((tuple(sorted(selected)), types))
        if failed:
            continue

        tower_payload = None
        tower_group = None
        if options.tower_options is not None:
            tower_index = len(options.unit_options)
            tower_result = _tower_selection(
                level,
                tuple(
                    index
                    for index in tower_pool
                    if options.tower_options.max_layer is None
                    or level.tiles[index].layer <= options.tower_options.max_layer
                ),
                label_allocations[tower_index],
                options.tower_options.normalized_groups(),
                rng,
            )
            if tower_result is None:
                rejections["tower_capacity"] += 1
                continue
            tower_indices, tower_types, tower_group = tower_result
            tower_payload = (tower_indices, tower_types)

        selected_structure_indices = tuple(
            index
            for selected in selected_by_unit
            for index in selected
        ) + (() if tower_payload is None else tuple(tower_payload[0]))
        if (
            options.max_structure_layer_span is not None
            and selected_structure_indices
        ):
            actual_layer_span = structure_layer_span(
                level, selected_structure_indices
            )
            assert actual_layer_span is not None
            if actual_layer_span > options.max_structure_layer_span:
                if "structure_layer_span" not in rule_failure_codes:
                    rejections["rule_warning_structure_layer_span"] += 1
                if "structure_layer_span" in retained_rule_failure_codes:
                    continue
                rule_failure_codes.add("structure_layer_span")

        if options.min_same_type_layer_span:
            layers_by_type: dict[str, list[int]] = {}
            for payload_indices, payload_types in unit_payloads + ([] if tower_payload is None else [tower_payload]):
                for index, label in zip(payload_indices, payload_types):
                    layers_by_type.setdefault(label, []).append(level.tiles[index].layer)
            if any(max(layers) - min(layers) < options.min_same_type_layer_span for layers in layers_by_type.values()):
                rejections["same_type_layer_span"] += 1
                continue

        replacements = []
        for unit_index, unit in enumerate(unit_candidates):
            for original_index, label in zip(unit.selected_indices, unit.assigned_types):
                tile = level.tiles[original_index]
                replacements.append(
                    Replacement(
                        tile_index=original_index,
                        source_tile_id=tile.tile_id,
                        structure_type=label,
                        structure_tile_id=STRUCTURE_TYPE_TO_ID[label],
                        x=tile.x,
                        y=tile.y,
                        z=tile.z,
                        group_id=unit.unit_id,
                        tower_index=tile.tower_index,
                    )
                )
        if tower_payload:
            for original_index, label in zip(*tower_payload):
                tile = level.tiles[original_index]
                replacements.append(
                    Replacement(
                        tile_index=original_index,
                        source_tile_id=tile.tile_id,
                        structure_type=label,
                        structure_tile_id=STRUCTURE_TYPE_TO_ID[label],
                        x=tile.x,
                        y=tile.y,
                        z=tile.z,
                        group_id="tower",
                        tower_index=tile.tower_index,
                    )
                )
        signature = candidate_structure_signature(level, unit_payloads, tower_payload)
        identity = canonical_structure_identity(signature)
        if identity in excluded_signatures:
            rejections["excluded_candidate"] += 1
            continue
        if identity in accepted:
            rejections["duplicate_candidate"] += 1
            continue
        if unit_candidates:
            score, metrics, quality_summary, risk_warnings = _quality_score(
                level,
                graph,
                [unit.selected_indices for unit in unit_candidates],
                options.quality,
            )
        else:
            score = 0.0
            metrics = {
                "layout_score": 0.0,
                "experience_risk_penalty": 0.0,
                "tower_balance": float(
                    tower_group.max_min_difference if tower_group else 0
                ),
            }
            quality_summary = ("仅塔内方案不计算静态嵌套与立体压叠风险。",)
            risk_warnings = ()
        candidate_id = "candidate_" + format(
            derive_seed(
                level.sha256,
                plan.config_hash,
                options_hash,
                candidate_seed,
                signature,
                ALGORITHM_VERSION,
            ),
            "016x",
        )
        candidate = Candidate(
            candidate_id=candidate_id,
            candidate_index=0,
            attempt_index=attempt_index,
            attempt_seed=attempt_seed,
            candidate_seed=candidate_seed,
            algorithm_version=ALGORITHM_VERSION,
            source_template_id=level.template_id,
            source_sha256=level.sha256,
            plan_id=plan.plan_id,
            plan_signature=plan.signature,
            plan_config_hash=plan.config_hash,
            generation_config_hash=options_hash,
            replacements=tuple(sorted(replacements, key=lambda item: item.tile_index)),
            units=tuple(unit_candidates),
            tower_group=tower_group,
            quality_score=score,
            quality_metrics=metrics,
            structure_signature=signature,
            warnings=tuple(getattr(level, "warnings", ())),
            quality_summary=quality_summary,
            risk_warnings=risk_warnings,
            candidate_seed_sha256=candidate_seed_sha256,
            candidate_seed_ordinal=candidate_seed_ordinal,
        )
        experience = evaluate_candidate_experience(
            level, candidate, options.quality
        )
        if (
            options.quality.special_preset != "none"
            and experience.get("special_preset", {}).get("status")
            == "rejected"
        ):
            rejections["rule_warning_special_preset_proof"] += 1
            rule_failure_codes.add("special_preset_proof")
        metrics = dict(candidate.quality_metrics)
        metrics["search_rule_failure_count"] = float(len(rule_failure_codes))
        candidate = Candidate(
            **{
                **candidate.__dict__,
                "quality_metrics": metrics,
                "experience_analysis": experience,
                "experience_signature": str(
                    experience.get("experience_signature", "")
                ),
            }
        )
        accepted[identity] = candidate
        retained_rule_failure_codes.update(rule_failure_codes)

    if search_progress is not None:
        search_progress(attempt_count, dict(rejections))
    # Difficulty is part of the primary recommendation order.  Keep a bounded
    # pool for responsiveness, but select it only by hard legality, archetype
    # strata and topology/mechanism diversity.  Style and layout are later
    # ranking stages and cannot eliminate a candidate before difficulty runs.
    shortlist_limit = max(
        12,
        (
            (1 if candidate_index is not None else options.candidate_count)
            + len(excluded_experience_signatures)
        )
        * 3,
    )
    preliminary = _select_difficulty_evaluation_pool(
        tuple(accepted.values()), shortlist_limit
    )
    if options.precision_mode:
        # The bounded shortlist is only an ordering preference in precision
        # mode.  Every accepted layout remains eligible for simulation, so a
        # qualified layout outside the first twelve cannot be missed.
        shortlisted = {item.structure_signature for item in preliminary}
        preliminary.extend(
            item
            for item in sorted(
                accepted.values(),
                key=lambda candidate: (
                    candidate.structure_signature,
                    candidate.attempt_index,
                    candidate.attempt_seed,
                ),
            )
            if item.structure_signature not in shortlisted
        )
    elif len(accepted) > len(preliminary):
        rejections["difficulty_preselection_dropped"] += (
            len(accepted) - len(preliminary)
        )
    if not preliminary:
        raise NoCandidateError(
            (
                "精准模式预算内无符合全部参数、难度目标与中低机制压力的候选"
                if options.precision_mode
                else "固定搜索预算内没有找到满足全部硬规则的候选"
            ),
            evidence={
                "attempt_budget": options.attempt_budget,
                "attempt_start_index": attempt_start_index,
                "attempt_count": attempt_count,
                "rejection_counts": dict(rejections),
            },
        )
    baseline = simulate_candidate_difficulty(
        level,
        (),
        target=options.quality.difficulty_target,
        profile=difficulty_profile,
        requested_archetype=options.quality.experience_archetype,
    )
    source_duplicates = baseline["source_duplicates"]
    enriched: list[Candidate] = []
    qualified_signatures: set[str] = set()
    required_qualified_count = (
        1 if candidate_index is not None else options.candidate_count
    )
    for candidate in preliminary:
        if options.precision_mode and cooperative_checkpoint is not None:
            cooperative_checkpoint("precision_candidate_validation")
        if options.precision_mode and float(
            candidate.quality_metrics.get("search_rule_failure_count", 0.0)
        ):
            rejections["precision_rule_failure"] += 1
            continue
        if options.precision_mode:
            replaced_a_types = {
                replacement.structure_type.upper()
                for replacement in candidate.replacements
                if replacement.source_tile_id == A_TILE
            }
            replaced_b_types = {
                replacement.structure_type.upper()
                for replacement in candidate.replacements
                if replacement.source_tile_id == B_TILE
            }
            if any(
                duplicate.a < len(replaced_a_types)
                or duplicate.b < len(replaced_b_types)
                for duplicate in difficulty_profile.duplicate_params
            ):
                rejections["precision_parameter_underflow"] += 1
                continue
        simulation = simulate_candidate_difficulty(
            level,
            candidate.replacements,
            target=options.quality.difficulty_target,
            profile=difficulty_profile,
            source_duplicates=source_duplicates,
            requested_archetype=options.quality.experience_archetype,
        )
        metrics = dict(candidate.quality_metrics)
        metrics["source_difficulty_distance"] = float(
            simulation["source_difficulty_distance"]
        )
        metrics["difficulty_trend_violation_count"] = float(
            len(simulation["trend"]["violations"])
        )
        experience = evaluate_candidate_experience(
            level, candidate, options.quality, simulation
        )
        item = Candidate(
            **{
                **candidate.__dict__,
                "quality_metrics": metrics,
                "difficulty_simulation": simulation,
                "experience_analysis": experience,
                "experience_signature": str(
                    experience.get("experience_signature", "")
                ),
            }
        )
        if options.precision_mode:
            trend = simulation.get("trend", {})
            if not isinstance(trend, Mapping):
                trend = {}
            rejection_reasons = []
            if experience.get("hard_validation", {}).get("status") != "proved":
                rejection_reasons.append("precision_hard_validation")
            if not bool(simulation.get("qualified")):
                rejection_reasons.append("precision_difficulty_target")
            if not bool(
                trend.get("safety_qualified", trend.get("becomes_easier", False))
            ):
                rejection_reasons.append("precision_trend")
            if experience.get("simulation_pressure", {}).get("level") not in {
                "low",
                "medium",
            }:
                rejection_reasons.append("precision_pressure")
            if rejection_reasons:
                rejections.update(rejection_reasons)
                continue
            validation_candidate = Candidate(
                **{
                    **item.__dict__,
                    "candidate_index": (
                        candidate_index if candidate_index is not None else 1
                    ),
                }
            )
            try:
                if final_candidate_acceptance is None:
                    # Import locally because independent validation also uses
                    # search data structures and hash rules.
                    from .validator import validate_candidate

                    independently_eligible = validate_candidate(
                        level,
                        plan,
                        options,
                        validation_candidate,
                        difficulty_profile=difficulty_profile,
                        require_exportable_experience=False,
                    ).auto_recommendation_eligible
                else:
                    independently_eligible = bool(
                        final_candidate_acceptance(validation_candidate)
                    )
            except CandidateValidationError:
                independently_eligible = False
            if not independently_eligible:
                rejections["precision_independent_validation"] += 1
                continue
        enriched.append(item)
        if options.precision_mode:
            signature = item.experience_signature or item.structure_signature
            if signature not in excluded_experience_signatures:
                qualified_signatures.add(signature)
            if len(qualified_signatures) >= required_qualified_count:
                break
    if options.precision_mode:
        if not enriched:
            raise NoCandidateError(
                "精准模式预算内无符合全部参数、难度目标与中低机制压力的候选",
                evidence={
                    "attempt_budget": options.attempt_budget,
                    "attempt_start_index": attempt_start_index,
                    "attempt_count": attempt_count,
                    "rejection_counts": dict(rejections),
                },
            )
    ranked_all = sorted(
        enriched,
        key=lambda item: (
            # Rule-failed candidates remain visible for deliberate manual
            # export, but a rule-qualified candidate always wins automatic
            # ranking when one exists.
            0
            if not float(
                item.quality_metrics.get("search_rule_failure_count", 0.0)
            )
            else 1,
            float(item.quality_metrics.get("search_rule_failure_count", 0.0)),
            0
            if item.experience_analysis.get("hard_validation", {}).get("status")
            == "proved"
            else 1,
            {
                "matched": 0,
                "compatible": 1,
                "inconclusive": 2,
                "not_matched": 3,
            }.get(
                str(
                    item.experience_analysis.get("archetype_match", {}).get(
                        "status", "inconclusive"
                    )
                ),
                2,
            ),
            -float(
                item.experience_analysis.get("archetype_match", {}).get(
                    "match_score", 0.0
                )
            ),
            {
                "low": 0,
                "medium": 1,
                "high": 2,
                "inconclusive": 3,
            }.get(
                str(
                    item.experience_analysis.get("simulation_pressure", {}).get(
                        "level", "inconclusive"
                    )
                ),
                3,
            ),
            0
            if bool(
                item.difficulty_simulation.get("trend", {}).get(
                    "safety_qualified",
                    item.difficulty_simulation.get("trend", {}).get(
                        "becomes_easier"
                    ),
                )
            )
            else 1,
            0 if bool(item.difficulty_simulation.get("qualified")) else 1,
            float(
                item.difficulty_simulation.get("difficulty_distance", {}).get(
                    "primary_distance",
                    item.difficulty_simulation.get(
                        "source_difficulty_distance", float("inf")
                    ),
                )
            ),
            float(
                item.difficulty_simulation.get("difficulty_distance", {}).get(
                    "stable_tiebreak", 0.0
                )
            ),
            float(
                item.experience_analysis.get("online_style_distance", {}).get(
                    "total", 0.0
                )
            ),
            -item.quality_score,
            item.structure_signature,
            item.attempt_index,
            item.attempt_seed,
        ),
    )
    available_ranked = [
        item
        for item in ranked_all
        if (item.experience_signature or item.structure_signature)
        not in excluded_experience_signatures
    ]
    excluded_experience_count = len(ranked_all) - len(available_ranked)
    if excluded_experience_count:
        rejections["excluded_experience_signature"] += excluded_experience_count
    if not available_ranked:
        raise NoCandidateError(
            "固定搜索预算内没有找到更多不同体验的候选",
            evidence={
                "attempt_budget": options.attempt_budget,
                "attempt_start_index": attempt_start_index,
                "attempt_count": attempt_count,
                "rejection_counts": dict(rejections),
            },
        )
    ranked = _select_experience_diverse(
        available_ranked,
        1 if candidate_index is not None else options.candidate_count,
    )
    normalized = tuple(
        Candidate(
            **{
                **candidate.__dict__,
                "candidate_index": candidate_index
                if candidate_index is not None
                else index,
            }
        )
        for index, candidate in enumerate(ranked, 1)
    )
    return SearchResult(
        candidates=normalized,
        attempts_completed=attempt_count,
        rejection_counts=dict(rejections),
        candidate_seed=candidate_seed,
        candidate_seed_sha256=candidate_seed_sha256,
        candidate_seed_ordinal=candidate_seed_ordinal,
    )
