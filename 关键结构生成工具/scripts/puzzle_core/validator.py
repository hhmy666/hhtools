"""Independent, fail-closed validation of generated puzzle candidates."""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Tuple

from .combination import CombinationProof, CombinationUnitInput, analyze_combination
from .difficulty import (
    DIFFICULTY_TARGETS,
    DifficultyProfile,
    difficulty_profile_from_simulation,
    simulate_candidate_difficulty,
)
from .errors import CandidateValidationError
from .geometry import (
    CoverageAuditReport,
    audit_structure_coverage,
    build_static_geometry,
    overlap_area,
)
from .models import Layout, ResolvedOutputPlan, TowerMode
from .planning import derive_seed
from .rewrite import rewrite_template, verify_rewrite
from .search import (
    ALGORITHM_VERSION,
    EXPERIENCE_ARCHETYPES,
    LONG_RANGE_WAVE_COUNTS,
    ONLINE_STYLE_REFERENCES,
    SPECIAL_PRESETS,
    Candidate,
    GenerationOptions,
    MAJORITY_A_ON_TIE,
    PREFER_A_ELSE_B,
    STRUCTURE_ID_TO_TYPE,
    STRUCTURE_TYPE_TO_ID,
    candidate_structure_signature,
    evaluate_candidate_experience,
    generation_options_hash,
    structure_layer_span,
)
from .solver import PuzzleProof, analyze_puzzle, validate_target
from .special_presets import analyze_long_range_assignment
from .template_reader import LevelTemplate, parse_level_bytes
from .topology import (
    ExistingStructureIsolationReport,
    TopologyReport,
    TopologyUnit,
    analyze_existing_structure_isolation,
    analyze_topology,
)
from .tower_reader import TowerOnlyAllocation, allocate_only_mode


@dataclass(frozen=True)
class ValidationCheck:
    code: str
    passed: bool
    message: str
    evidence: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StructureInventoryReport:
    """Source/output Tile totals and C--N inventory after replacement."""

    source_total_tile_count: int
    output_total_tile_count: int
    source_existing_structure_count: int
    source_existing_structure_counts: Tuple[Tuple[str, int], ...]
    new_structure_count: int
    new_structure_counts: Tuple[Tuple[str, int], ...]
    output_structure_count: int
    output_structure_counts: Tuple[Tuple[str, int], ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EffectiveConstraintReport:
    """Resolved layer/count limits together with the observed candidate values."""

    template_total_tile_count: int
    template_highest_layer: int
    percentage_scope: str
    percentage_tile_count: int
    percentage_highest_layer: int
    configured_count_limit: int | None
    configured_count_percent: float | None
    effective_structure_count_limit: int
    actual_new_structure_count: int
    count_limit_passed: bool
    configured_max_layer: int | None
    configured_max_layer_percent: float | None
    effective_max_structure_layer: int
    actual_highest_structure_layer: int | None
    layer_limit_passed: bool
    configured_max_structure_layer_span: int | None
    actual_structure_layer_span: int | None
    structure_layer_span_passed: bool
    configured_count_minimum: int = 0
    configured_percent_minimum: float | None = None
    effective_structure_count_minimum: int = 0
    count_minimum_passed: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TileExternalBlockers:
    structure_index: int
    external_direct_blocker_indices: Tuple[int, ...]


@dataclass(frozen=True)
class UnitAccessReport:
    """Auditable initial entries and source-template blockers outside a unit."""

    unit_id: str
    structure_indices: Tuple[int, ...]
    initial_entry_indices: Tuple[int, ...]
    external_direct_blockers_by_tile: Tuple[TileExternalBlockers, ...]
    external_direct_blocker_indices: Tuple[int, ...]
    external_transitive_blocker_indices: Tuple[int, ...]
    external_blocking_a_indices: Tuple[int, ...]
    external_blocking_b_indices: Tuple[int, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CombinationConclusion:
    """Stable, compact acceptance conclusion derived from the full proof."""

    layout: str
    unit_count: int
    contains_pressure_unit: bool
    all_units_qualified: bool
    capacity_7_allowed_order_count: int
    capacity_7_all_allowed_orders_complete: bool
    completion_requirement: str
    acceptance_basis: str
    interleaving_coverage: str
    interleaving_result_at_capacity_7: str | None
    safe_interleaving_exists_at_capacity_7: bool | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CandidateValidationReport:
    candidate_id: str
    source_template_id: str
    source_sha256: str
    validation_template_id: str
    validation_output_sha256: str
    passed: bool
    technical_integrity_passed: bool
    manual_export_eligible: bool
    auto_recommendation_eligible: bool
    checks: Tuple[ValidationCheck, ...]
    rule_failures: Tuple[ValidationCheck, ...]
    replacement_count: int
    replaced_a_count: int
    replaced_b_count: int
    structure_counts: Tuple[Tuple[str, int], ...]
    tower_structure_count: int
    non_tower_structure_count: int
    highest_structure_layer: int | None
    nested_a_count: int
    nested_b_count: int
    initial_entry_count: int
    external_blocker_count: int
    inventory: StructureInventoryReport
    effective_constraints: EffectiveConstraintReport
    coverage_audit: CoverageAuditReport
    unit_access_reports: Tuple[UnitAccessReport, ...]
    combination_proof: CombinationProof | None
    combination_conclusion: CombinationConclusion | None
    topology_report: TopologyReport | None
    existing_structure_isolation: ExistingStructureIsolationReport | None
    unit_proofs: Tuple[PuzzleProof, ...]
    tower_allocation: TowerOnlyAllocation | None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["checks"] = [check.as_dict() for check in self.checks]
        payload["rule_failures"] = [
            check.as_dict() for check in self.rule_failures
        ]
        payload["combination_proof"] = (
            None if self.combination_proof is None else self.combination_proof.as_dict()
        )
        payload["topology_report"] = (
            None if self.topology_report is None else self.topology_report.as_dict()
        )
        payload["existing_structure_isolation"] = (
            None
            if self.existing_structure_isolation is None
            else self.existing_structure_isolation.as_dict()
        )
        payload["unit_proofs"] = [proof.as_dict() for proof in self.unit_proofs]
        payload["inventory"] = self.inventory.as_dict()
        payload["effective_constraints"] = self.effective_constraints.as_dict()
        payload["coverage_audit"] = self.coverage_audit.as_dict()
        payload["unit_access_reports"] = [
            report.as_dict() for report in self.unit_access_reports
        ]
        payload["combination_conclusion"] = (
            None
            if self.combination_conclusion is None
            else self.combination_conclusion.as_dict()
        )
        payload["tower_allocation"] = (
            None if self.tower_allocation is None else self.tower_allocation.as_dict()
        )
        return payload


def _fail(code: str, message: str, **evidence: Any) -> None:
    raise CandidateValidationError(
        message,
        evidence={"check": code, **evidence},
    )


def _constraint_tiles(level: LevelTemplate, tower_mode: TowerMode) -> tuple:
    if tower_mode is TowerMode.DISALLOW:
        return tuple(tile for tile in level.tiles if not tile.is_tower_member)
    return tuple(level.tiles)


def _effective_layer_limit(
    level: LevelTemplate,
    options: GenerationOptions,
    tower_mode: TowerMode = TowerMode.DISALLOW,
) -> int:
    limits = []
    if options.max_layer is not None:
        limits.append(options.max_layer)
    if options.max_layer_percent is not None:
        highest = max(
            (tile.layer for tile in _constraint_tiles(level, tower_mode)),
            default=0,
        )
        limits.append(math.floor(highest * options.max_layer_percent / 100))
    return min(limits) if limits else level.max_layer


def _effective_count_limit(
    level: LevelTemplate,
    options: GenerationOptions,
    tower_mode: TowerMode = TowerMode.DISALLOW,
) -> int:
    limits = []
    if options.total_count_limit is not None:
        limits.append(options.total_count_limit)
    if options.total_percent_limit is not None:
        limits.append(
            math.floor(
                len(_constraint_tiles(level, tower_mode))
                * options.total_percent_limit
                / 100
            )
        )
    raw = min(limits) if limits else len(level.tiles)
    return raw - raw % 3


def _effective_count_minimum(
    level: LevelTemplate,
    options: GenerationOptions,
    tower_mode: TowerMode = TowerMode.DISALLOW,
) -> int:
    # Independently recompute the percentage using the export validator's scope.
    if options.total_percent_minimum is not None:
        return math.ceil(len(_constraint_tiles(level, tower_mode)) * options.total_percent_minimum / 100)
    return options.total_count_minimum


def _source_ids(options: GenerationOptions) -> frozenset[int]:
    mapping = {"A": -10, "B": -11}
    raw_labels = tuple(str(value) for value in options.replacement_sources)
    labels = tuple(
        value.upper() if value.upper() in mapping else value.lower()
        for value in raw_labels
    )
    if labels in ((MAJORITY_A_ON_TIE,), (PREFER_A_ELSE_B,)):
        return frozenset(mapping.values())
    if not labels or len(set(labels)) != len(labels) or set(labels) - set(mapping):
        _fail(
            "replacement_sources",
            "replacement_sources 只能为 A、B、A+B 或 majority_a_on_tie",
        )
    return frozenset(mapping[label] for label in labels)


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
    """Independently derive the source from original A/B Tile totals."""

    a_count = sum(tile.tile_id == -10 for tile in level.tiles)
    b_count = sum(tile.tile_id == -11 for tile in level.tiles)
    return -10 if a_count >= b_count else -11


def _eligible_source_pool(
    level: LevelTemplate,
    graph: Any,
    options: GenerationOptions,
    *,
    tower_membership: bool,
    tower_mode: TowerMode,
) -> tuple[int, ...]:
    """Independently reconstruct search eligibility for the source rule."""

    max_layer = _effective_layer_limit(level, options, tower_mode)
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
    eligible = []
    for tile in level.tiles:
        if tile.tile_id not in source_ids:
            continue
        if tile.debuff_data_index not in (None, -1):
            continue
        if tile.layer > max_layer or tile.is_tower_member != tower_membership:
            continue
        if not tower_membership and tile.index in forbidden_adjacent_indices:
            continue
        if not tower_membership and existing:
            if graph.transitive_blockers[tile.index].intersection(existing):
                continue
            if any(
                tile.index in graph.transitive_blockers[index]
                for index in existing
            ):
                continue
            if any(
                overlap_area(tile, level.tiles[index]) for index in existing
            ):
                continue
        eligible.append(tile.index)
    if _uses_majority_source_rule(options):
        selected_source_id = _majority_source_tile_id(level)
        eligible = [
            index
            for index in eligible
            if level.tiles[index].tile_id == selected_source_id
        ]
    elif _uses_legacy_a_priority(options):
        preferred = tuple(
            index for index in eligible if level.tiles[index].tile_id == -10
        )
        if preferred:
            return tuple(sorted(preferred))
        eligible = [index for index in eligible if level.tiles[index].tile_id == -11]
    return tuple(sorted(eligible))


def _validate_group_options(
    labels: Tuple[str, ...],
    option: Any,
    *,
    group_id: str,
    global_allowed: frozenset[str],
) -> Counter[str]:
    normalized = tuple(str(value).upper() for value in labels)
    if not normalized:
        _fail("empty_structure_group", "结构组不能为空", group_id=group_id)
    unit_allowed = tuple(str(value).upper() for value in option.allowed_types)
    if (
        not unit_allowed
        or len(set(unit_allowed)) != len(unit_allowed)
        or set(unit_allowed) - global_allowed
    ):
        _fail("unit_allowed_types", "结构组 allowed_types 不合法", group_id=group_id)
    counts = Counter(normalized)
    if set(counts) - set(unit_allowed):
        _fail(
            "unit_allowed_types",
            "候选使用了结构组白名单之外的类型",
            group_id=group_id,
            types=sorted(set(counts) - set(unit_allowed)),
        )
    if len(counts) != option.type_count:
        _fail(
            "unit_type_count",
            "结构组实际类型数与已解析参数不一致",
            group_id=group_id,
            expected=option.type_count,
            actual=len(counts),
        )
    try:
        expected_groups = tuple(option.normalized_groups())
    except Exception as exc:
        _fail("groups_per_type", "结构组数量参数不合法", group_id=group_id, error=str(exc))
    actual_counts = tuple(sorted(counts.values()))
    expected_counts = tuple(sorted(3 * value for value in expected_groups))
    if actual_counts != expected_counts:
        _fail(
            "groups_per_type",
            "每种结构块数量与 groups_per_type 不一致",
            group_id=group_id,
            expected=list(expected_counts),
            actual=list(actual_counts),
        )
    if option.threshold_type is not None:
        threshold = str(option.threshold_type).upper()
        if threshold not in counts:
            _fail("threshold_type", "锁定的门槛块未被使用", group_id=group_id)
        if expected_groups and counts[threshold] != 3 * expected_groups[0]:
            _fail(
                "threshold_group_count",
                "锁定门槛块数量与第一个 groups_per_type 参数不一致",
                group_id=group_id,
            )
    if option.count_limit is not None and len(normalized) > option.count_limit:
        _fail("unit_count_limit", "结构组超过自身结构块数上限", group_id=group_id)
    return counts


def validate_candidate(
    level: LevelTemplate,
    plan: ResolvedOutputPlan,
    options: GenerationOptions,
    candidate: Candidate,
    *,
    difficulty_profile: DifficultyProfile | None = None,
    require_exportable_experience: bool = True,
) -> CandidateValidationReport:
    """Recompute every export-critical invariant from source bytes.

    Search results are treated as untrusted input.  This function reconstructs
    geometry and solver proofs, performs an in-memory whitelist rewrite, and
    parses the result again.  Identity, payload consistency and rewrite
    integrity always fail closed.  With ``require_exportable_experience=False``
    independently reproduced product-rule failures are returned as failed
    checks so a caller can require an explicit manual acknowledgement without
    making a corrupt candidate exportable.
    """

    checks: list[ValidationCheck] = []
    candidate_rule_failures: list[ValidationCheck] = []

    def product_rule_failure(
        code: str,
        message: str,
        **evidence: Any,
    ) -> None:
        if require_exportable_experience:
            _fail(code, message, **evidence)
        failure = ValidationCheck(
            code,
            False,
            message,
            evidence or None,
        )
        checks.append(failure)
        candidate_rule_failures.append(failure)
    if not 1 <= options.candidate_count <= 20:
        _fail("candidate_count", "candidate_count 必须在 1–20 之间")
    if options.attempt_budget < 1 or options.assignment_attempts < 1:
        _fail("search_budget", "搜索预算必须为正整数")
    # These helpers also reject negative counts and percentages/layers outside
    # the documented ranges.  Validate them even when the concrete candidate
    # would happen to remain below the resulting limit.
    if options.total_percent_limit is not None and not (
        0 < options.total_percent_limit <= 100
    ):
        _fail("total_percent_limit", "total_percent_limit 必须在 0–100 之间")
    if options.max_layer_percent is not None and not (
        0 < options.max_layer_percent <= 100
    ):
        _fail("max_layer_percent", "max_layer_percent 必须在 0–100 之间")
    if options.total_count_limit is not None and options.total_count_limit < 0:
        _fail("total_count_limit", "total_count_limit 不能为负数")
    if isinstance(options.total_count_minimum, bool) or not isinstance(options.total_count_minimum, int) or options.total_count_minimum < 0:
        _fail("count_minimum", "结构块数量下限必须是非负整数")
    if options.total_percent_minimum is not None:
        if isinstance(options.total_percent_minimum, bool) or not isinstance(options.total_percent_minimum, (int, float)) or not 0 <= options.total_percent_minimum <= 100:
            _fail("count_minimum", "结构块占比下限必须在 0–100 之间")
        if options.total_count_minimum != 0:
            _fail("count_minimum", "结构块数量下限与占比下限互斥")
    if options.max_layer is not None and options.max_layer < 1:
        _fail("max_layer", "max_layer 必须大于 0")
    effective_count_limit = _effective_count_limit(level, options, plan.tower_mode)
    effective_count_minimum = _effective_count_minimum(level, options, plan.tower_mode)
    if effective_count_minimum > effective_count_limit:
        _fail("count_minimum", "结构块数量下限超过有效上限", minimum=effective_count_minimum, effective_limit=effective_count_limit)
    effective_layer_limit = _effective_layer_limit(level, options, plan.tower_mode)
    if options.quality.position not in {"center", "balanced", "unrestricted"}:
        _fail("quality_position", "position 偏好不合法")
    if options.quality.symmetry not in {"strong", "medium", "off"}:
        _fail("quality_symmetry", "symmetry 偏好不合法")
    if options.quality.density not in {"compact", "medium", "dispersed"}:
        _fail("quality_density", "density 偏好不合法")
    if options.quality.difficulty_target not in DIFFICULTY_TARGETS:
        _fail(
            "difficulty_target",
            "difficulty_target 必须是 fidelity、standard 或 strong",
        )
    if options.quality.experience_archetype not in EXPERIENCE_ARCHETYPES:
        _fail("experience_archetype", "experience_archetype 不合法")
    if options.quality.special_preset not in SPECIAL_PRESETS:
        _fail("special_preset", "special_preset 不合法")
    if options.quality.long_range_wave_count not in LONG_RANGE_WAVE_COUNTS:
        _fail(
            "long_range_wave_count",
            "long_range_wave_count 必须是 auto、3 或 4",
        )
    if (
        options.quality.special_preset != "all_types_long_range"
        and options.quality.long_range_wave_count != "auto"
    ):
        _fail(
            "special_preset_variant_scope",
            "long_range_wave_count 只适用于 all_types_long_range",
        )
    if options.quality.special_preset == "all_types_long_range" and (
        len(plan.units) != 1
        or options.tower_options is not None
        or plan.layout is not Layout.SINGLE
    ):
        _fail(
            "special_preset_plan_shape",
            "all_types_long_range 仅支持单个塔外结构单元",
        )
    if options.quality.special_preset == "serial_mechanism" and (
        len(plan.units) != 2
        or options.tower_options is not None
        or plan.layout is not Layout.Z
    ):
        _fail(
            "special_preset_plan_shape",
            "serial_mechanism 仅支持 Z 向 high→low 双单元",
        )
    if options.quality.online_style_reference not in ONLINE_STYLE_REFERENCES:
        _fail("online_style_reference", "online_style_reference 不合法")
    global_allowed_values = tuple(str(value).upper() for value in options.allowed_types)
    global_allowed = frozenset(global_allowed_values)
    if (
        not global_allowed
        or len(global_allowed) != len(global_allowed_values)
        or global_allowed - frozenset(STRUCTURE_TYPE_TO_ID)
    ):
        _fail("allowed_types", "全局 allowed_types 必须是不重复的 C–N")
    source_ids = _source_ids(options)
    graph = build_static_geometry(level)
    eligible_source_indices = set(
        _eligible_source_pool(
            level,
            graph,
            options,
            tower_membership=False,
            tower_mode=plan.tower_mode,
        )
    )
    eligible_source_indices.update(
        _eligible_source_pool(
            level,
            graph,
            options,
            tower_membership=True,
            tower_mode=plan.tower_mode,
        )
    )
    if len(options.unit_options) != len(plan.units):
        _fail("unit_options", "单元参数数量与已解析方案不一致")
    if options.candidate_count < 1 or candidate.candidate_index not in range(
        1, options.candidate_count + 1
    ):
        _fail("candidate_index", "候选序号超出已解析的候选数量")
    if candidate.attempt_index not in range(options.attempt_budget):
        _fail("attempt_index", "候选 attempt_index 超出固定搜索预算")

    options_hash = generation_options_hash(options)
    identity_mismatches = []
    if candidate.source_sha256 != level.sha256:
        identity_mismatches.append("source_sha256")
    if candidate.source_template_id != level.template_id:
        identity_mismatches.append("source_template_id")
    if candidate.plan_id != plan.plan_id:
        identity_mismatches.append("plan_id")
    if candidate.plan_signature != plan.signature:
        identity_mismatches.append("plan_signature")
    if candidate.plan_config_hash != plan.config_hash:
        identity_mismatches.append("plan_config_hash")
    if candidate.generation_config_hash != options_hash:
        identity_mismatches.append("generation_config_hash")
    if candidate.algorithm_version != ALGORITHM_VERSION:
        identity_mismatches.append("algorithm_version")
    seed_digest = candidate.candidate_seed_sha256
    if (
        not isinstance(seed_digest, str)
        or len(seed_digest) != 64
        or any(value not in "0123456789abcdef" for value in seed_digest)
        or (
            candidate.candidate_seed_ordinal is not None
            and int(seed_digest[:16], 16) != candidate.candidate_seed
        )
    ):
        identity_mismatches.append("candidate_seed_sha256")
    if candidate.candidate_seed_ordinal is not None and (
        isinstance(candidate.candidate_seed_ordinal, bool)
        or not isinstance(candidate.candidate_seed_ordinal, int)
        or candidate.candidate_seed_ordinal < 1
    ):
        identity_mismatches.append("candidate_seed_ordinal")
    expected_attempt_seed = derive_seed(
        candidate.candidate_seed,
        "attempt",
        candidate.attempt_index,
        ALGORITHM_VERSION,
    )
    if candidate.attempt_seed != expected_attempt_seed:
        identity_mismatches.append("attempt_seed")
    if identity_mismatches:
        _fail(
            "candidate_identity",
            "候选与当前源文件、方案或已解析生成参数不一致",
            fields=identity_mismatches,
        )

    if tuple(unit.unit_id for unit in candidate.units) != tuple(
        unit.unit_id for unit in plan.units
    ):
        _fail("unit_order", "候选单元未按已解析空间角色顺序绑定")
    if (options.tower_options is None) != (candidate.tower_group is None):
        _fail("tower_group", "候选塔分布组与生成参数不一致")

    expected_members: dict[int, tuple[str, str]] = {}
    group_counts: dict[str, Counter[str]] = {}
    used_types: set[str] = set()
    for resolved, option, unit in zip(
        plan.units, options.unit_options, candidate.units
    ):
        if unit.role != resolved.role.value or unit.target_step != resolved.step:
            _fail(
                "unit_plan_binding",
                "候选单元的角色或目标步数与已解析方案不一致",
                unit_id=unit.unit_id,
            )
        selected = tuple(unit.selected_indices)
        labels = tuple(str(value).upper() for value in unit.assigned_types)
        if selected != tuple(sorted(selected)) or len(selected) != len(set(selected)):
            _fail("unit_member_order", "候选单元成员必须唯一且按 Tile 索引排序")
        if len(selected) != len(labels):
            _fail("unit_assignment", "候选单元成员与类型分配数量不一致")
        counts = _validate_group_options(
            labels,
            option,
            group_id=unit.unit_id,
            global_allowed=global_allowed,
        )
        if option.threshold_type is not None and unit.threshold_type != str(
            option.threshold_type
        ).upper():
            _fail("threshold_type", "候选未使用已锁定的门槛块", unit_id=unit.unit_id)
        if unit.threshold_type not in counts:
            _fail("threshold_type", "候选门槛块不在单元类型中", unit_id=unit.unit_id)
        shared = used_types.intersection(counts)
        if shared:
            _fail("group_type_exclusivity", "不同结构组使用了相同类型", shared_types=sorted(shared))
        used_types.update(counts)
        group_counts[unit.unit_id] = counts
        for index, label in zip(selected, labels):
            if index in expected_members:
                _fail("structure_member_bijection", "一个 Tile 被多个结构组声明")
            expected_members[index] = (unit.unit_id, label)

    tower_payload: tuple[Tuple[int, ...], Tuple[str, ...]] | None = None
    if candidate.tower_group is not None:
        assert options.tower_options is not None
        tower_indices = tuple(candidate.tower_group.selected_indices)
        tower_labels = tuple(str(value).upper() for value in candidate.tower_group.assigned_types)
        if len(tower_indices) != len(set(tower_indices)) or len(tower_indices) != len(tower_labels):
            _fail("tower_group", "塔分布组成员重复或类型数量不一致")
        counts = _validate_group_options(
            tower_labels,
            options.tower_options,
            group_id="tower",
            global_allowed=global_allowed,
        )
        shared = used_types.intersection(counts)
        if shared:
            _fail("group_type_exclusivity", "塔分布组与谜题单元类型不互斥", shared_types=sorted(shared))
        used_types.update(counts)
        group_counts["tower"] = counts
        for index, label in zip(tower_indices, tower_labels):
            if index in expected_members:
                _fail("structure_member_bijection", "一个 Tile 被多个结构组声明")
            expected_members[index] = ("tower", label)
        tower_payload = (tower_indices, tower_labels)

    replacements = tuple(candidate.replacements)
    indices = [item.tile_index for item in replacements]
    if len(indices) != len(set(indices)):
        _fail("duplicate_replacement", "候选包含重复替换位置")
    if set(indices) != set(expected_members):
        _fail(
            "structure_member_bijection",
            "替换清单与谜题单元/塔分布组声明不是严格双射",
            undeclared_replacements=sorted(set(indices) - set(expected_members)),
            missing_replacements=sorted(set(expected_members) - set(indices)),
        )
    by_index = {item.tile_index: item for item in replacements}
    for item in replacements:
        if item.tile_index < 0 or item.tile_index >= len(level.tiles):
            _fail("replacement_index", "替换位置越界", tile_index=item.tile_index)
        if (item.group_id, item.structure_type) != expected_members[item.tile_index]:
            _fail("structure_member_bijection", "替换分组或类型与结构组声明不一致")
        source = level.tiles[item.tile_index]
        if source.tile_id not in source_ids or source.tile_id != item.source_tile_id:
            _fail("replacement_source", "替换来源不属于已配置的 A/B 白名单")
        if (
            (_uses_majority_source_rule(options) or _uses_legacy_a_priority(options))
            and item.tile_index not in eligible_source_indices
        ):
            product_rule_failure(
                "replacement_source_priority",
                "替换来源不符合已配置的自动来源选择规则",
                tile_index=item.tile_index,
            )
        if source.debuff_data_index not in (None, -1):
            _fail("replacement_debuff", "指向真实 DebuffData 的 Tile 不可替换")
        if item.structure_tile_id != STRUCTURE_TYPE_TO_ID.get(item.structure_type):
            _fail("replacement_target", "结构字母与 TileId 不一致")
        if (source.x, source.y, source.z) != (item.x, item.y, item.z):
            _fail("replacement_coordinate", "候选坐标与源 Tile 不一致")
        if source.tower_index != item.tower_index:
            _fail("replacement_tower", "候选塔归属与显式 StackData 不一致")

    expected_total = sum(option.tile_count for option in options.unit_options)
    if options.tower_options is not None:
        expected_total += options.tower_options.tile_count
    if len(replacements) != expected_total:
        _fail("replacement_count", "替换总数与已解析结构组参数不一致")
    if len(replacements) < effective_count_minimum:
        _fail("count_minimum", "新增结构块数量未达到数量下限", actual=len(replacements), minimum=effective_count_minimum)
    if len(replacements) > effective_count_limit:
        product_rule_failure(
            "count_limit",
            "新增结构块数量超过有效上限",
            actual=len(replacements),
            effective_limit=effective_count_limit,
        )
    highest_layer = max((level.tiles[index].layer for index in indices), default=None)
    if highest_layer is not None and highest_layer > effective_layer_limit:
        product_rule_failure(
            "layer_limit",
            "结构块最高层超过有效上限",
            actual=highest_layer,
            effective_limit=effective_layer_limit,
        )
    actual_layer_span = structure_layer_span(level, indices)
    if (
        options.max_structure_layer_span is not None
        and actual_layer_span is not None
        and actual_layer_span > options.max_structure_layer_span
    ):
        product_rule_failure(
            "structure_layer_span",
            "结构块最高层与最低层的间距超过配置上限",
            actual=actual_layer_span,
            configured_max=options.max_structure_layer_span,
        )

    if options.min_same_type_layer_span:
        layers_by_type: dict[str, list[int]] = {}
        for item in replacements:
            layers_by_type.setdefault(item.structure_type, []).append(level.tiles[item.tile_index].layer)
        for label, layers in layers_by_type.items():
            span = max(layers) - min(layers)
            if span < options.min_same_type_layer_span:
                _fail("same_type_layer_span", "同种结构块最高层与最低层的间距未达到配置下限",
                      structure_type=label, actual=span, configured_min=options.min_same_type_layer_span)

    existing_types = {
        STRUCTURE_ID_TO_TYPE[tile.tile_id]
        for tile in level.tiles
        if tile.tile_id in STRUCTURE_ID_TO_TYPE
    }
    if existing_types.intersection(used_types):
        product_rule_failure(
            "existing_structure_type_isolation",
            "本次新结构与源模板已有 C–N 共用类型",
            shared_types=sorted(existing_types.intersection(used_types)),
        )

    unit_payloads = [
        (tuple(unit.selected_indices), tuple(unit.assigned_types))
        for unit in candidate.units
    ]
    expected_signature = candidate_structure_signature(
        level, unit_payloads, tower_payload
    )
    expected_candidate_id = "candidate_" + format(
        derive_seed(
            level.sha256,
            plan.config_hash,
            options_hash,
            candidate.candidate_seed,
            expected_signature,
            ALGORITHM_VERSION,
        ),
        "016x",
    )
    if (
        candidate.structure_signature != expected_signature
        or candidate.candidate_id != expected_candidate_id
    ):
        _fail("candidate_content_identity", "候选 ID 或结构签名与实际成员不一致")
    if tuple(candidate.warnings) != tuple(level.warnings):
        _fail("candidate_warnings", "候选携带的源模板警告快照不一致")
    checks.append(
        ValidationCheck(
            "candidate_identity",
            True,
            "候选已绑定当前源模板、方案、生成参数、种子和算法",
        )
    )

    checks.append(ValidationCheck("replacement_whitelist", True, "全部替换均为同坐标合法 A/B→C–N"))
    checks.append(ValidationCheck("structure_groups", True, "替换与结构组严格双射，数量与类型互斥通过"))

    tower_count = sum(level.tiles[index].is_tower_member for index in indices)
    static_count = len(indices) - tower_count
    if plan.tower_mode is TowerMode.DISALLOW and (
        tower_count or options.tower_options is not None
    ):
        _fail("tower_mode", "禁止塔内模式出现了塔分布参数或塔成员替换")
    if plan.tower_mode is TowerMode.ONLY and (static_count or candidate.units):
        _fail("tower_mode", "仅塔内模式包含塔外谜题单元")
    if plan.tower_mode is TowerMode.ONLY and (
        not tower_count or options.tower_options is None or plan.units
    ):
        _fail("tower_mode", "仅塔内模式没有生成塔内结构块")
    if any(
        level.tiles[index].is_tower_member
        for unit in candidate.units
        for index in unit.selected_indices
    ):
        _fail("tower_mode", "静态谜题单元不能包含塔成员")
    if candidate.tower_group is not None and any(
        not level.tiles[index].is_tower_member
        for index in candidate.tower_group.selected_indices
    ):
        _fail("tower_group", "塔分布组包含非塔成员")

    coverage_audit = audit_structure_coverage(
        level,
        {item.tile_index: item.structure_type for item in replacements},
    )
    if coverage_audit.adjacent_single_full_count:
        product_rule_failure(
            "adjacent_full_cover",
            "塔外静态结构块与相邻一层 Tile 形成了单块 100% 覆盖",
            relation_count=coverage_audit.adjacent_single_full_count,
            relations=[
                relation.as_dict()
                for relation in coverage_audit.adjacent_single_full_relations
            ],
        )
    else:
        checks.append(
            ValidationCheck(
                "adjacent_full_cover",
                True,
                "塔外新结构块的相邻层单块全遮关系为 0；跨层与联合全遮仅记录",
                {
                    "adjacent_single_full_count": 0,
                    "cross_layer_single_full_count": (
                        coverage_audit.cross_layer_single_full_count
                    ),
                    "joint_full_cover_count": coverage_audit.joint_full_cover_count,
                    "tower_initial_full_count": len(
                        coverage_audit.tower_initial_full_relations
                    ),
                },
            )
        )

    combination_inputs = []
    topology_inputs = []
    unit_proofs: list[PuzzleProof] = []
    nested_a: set[int] = set()
    nested_b: set[int] = set()
    for resolved, unit_options, unit in zip(
        plan.units, options.unit_options, candidate.units
    ):
        selected = tuple(unit.selected_indices)
        local = {original: local_index for local_index, original in enumerate(sorted(selected))}
        masks = []
        for original in sorted(selected):
            mask = 0
            # The structure-intrinsic snapshot removes external A/B.  Use
            # actual structure-to-structure projection edges, not closure
            # paths whose intermediate node may be one of those removed A/B.
            for blocker in graph.direct_blockers[original].intersection(local):
                mask |= 1 << local[blocker]
            masks.append(mask)
        labels = tuple(by_index[index].structure_type for index in sorted(selected))
        if unit_options.preferred_coords:
            actual_coords = tuple(
                (level.tiles[index].x, level.tiles[index].y, level.tiles[index].z)
                for index in sorted(selected)
            )
            expected_coords = tuple(sorted(unit_options.preferred_coords))
            if tuple(sorted(actual_coords)) != expected_coords:
                product_rule_failure(
                    "preferred_coords",
                    "候选单元未严格使用已锁定坐标",
                    unit_id=unit.unit_id,
                )
        unit_highest_layer = max(level.tiles[index].layer for index in selected)
        if (
            unit_options.max_layer is not None
            and unit_highest_layer > unit_options.max_layer
        ):
            product_rule_failure(
                "unit_layer_limit",
                "谜题单元超过自身最高层限制",
                unit_id=unit.unit_id,
                actual=unit_highest_layer,
                limit=unit_options.max_layer,
            )
        intrinsic_target = unit.intrinsic_target_step or resolved.step
        if intrinsic_target != resolved.step:
            _fail("unit_target_step", "候选的最优结构步数目标与方案不一致",
                  requested_step=resolved.step, intrinsic_target=intrinsic_target)
        effective_pre_gate_recycles = (
            0
            if intrinsic_target == 3
            else unit_options.required_pre_gate_recycles
        )
        if options.quality.special_preset == "all_types_long_range":
            proof, _special_contract = analyze_long_range_assignment(
                masks,
                labels,
                threshold_type=unit.threshold_type,
                target_step=intrinsic_target,
                limits=options.solver_limits,
                required_pre_gate_recycles=effective_pre_gate_recycles,
                first_recycle_lead_tiles=(
                    unit_options.first_recycle_lead_tiles
                ),
                preferred_wave_count=(
                    None
                    if options.quality.long_range_wave_count == "auto"
                    else int(options.quality.long_range_wave_count)
                ),
            )
        else:
            proof = analyze_puzzle(
                masks,
                labels,
                threshold_type=unit.threshold_type,
                limits=options.solver_limits,
                required_pre_gate_recycles=effective_pre_gate_recycles,
                first_recycle_lead_tiles=unit_options.first_recycle_lead_tiles,
            )
        if tuple(unit.blocker_masks) != tuple(masks):
            _fail("unit_blocker_masks", "候选携带的单元遮挡掩码与独立重算不一致")
        if tuple(unit.assigned_types) != labels:
            _fail("unit_assignments", "候选携带的单元类型顺序与替换清单不一致")
        if unit.proof != proof:
            _fail("unit_proof_snapshot", "候选携带的单元证明与独立重算不一致")
        if options.quality.special_preset == "all_types_long_range":
            # ``analyze_long_range_assignment`` has already applied the
            # ordinary target contract to wave 1 and exact capacity-7 checks
            # to every wave.  Its composed proof intentionally represents
            # empty-bag wave boundaries, so applying the monolithic
            # 36-tile ``validate_target`` contract a second time would mix two
            # different proof semantics.
            valid, reasons = True, ()
        elif intrinsic_target == 3:
            by_capacity = {item.capacity: item for item in proof.capacity_proofs}
            capacity_seven = by_capacity.get(7)
            valid = bool(
                proof.minimum_structure_step == 3
                and proof.recommended_path_constraints_ok
                and capacity_seven is not None
                and capacity_seven.complete_exists
            )
            reasons = () if valid else ("board_reveal_intrinsic_s3_invalid",)
        else:
            valid, reasons = validate_target(
                proof,
                intrinsic_target,
                required_pre_gate_recycles=effective_pre_gate_recycles,
                first_recycle_lead_tiles=unit_options.first_recycle_lead_tiles,
            )
        if not valid:
            product_rule_failure(
                "unit_solver",
                "独立复算后谜题单元不满足目标结构步数或背包规则",
                unit_id=unit.unit_id,
                reasons=list(reasons),
            )
        unit_proofs.append(proof)
        combination_inputs.append(
            CombinationUnitInput(
                unit_id=unit.unit_id,
                role=resolved.role,
                target_step=intrinsic_target,
                blocker_masks=tuple(masks),
                tile_types=labels,
                threshold_type=unit.threshold_type,
                required_pre_gate_recycles=effective_pre_gate_recycles,
                first_recycle_lead_tiles=(
                    unit_options.first_recycle_lead_tiles
                ),
            )
        )
        topology_inputs.append(
            TopologyUnit(
                unit_id=unit.unit_id,
                role=resolved.role,
                target_step=resolved.step,
                selected_indices=tuple(sorted(selected)),
            )
        )
    checks.append(ValidationCheck("static_units", True, "静态谜题单元已独立精确复算"))

    topology: TopologyReport | None = None
    legacy_isolation: ExistingStructureIsolationReport | None = None
    unit_access_reports: list[UnitAccessReport] = []
    if topology_inputs:
        if plan.layout is None:
            _fail("layout", "静态谜题方案缺少布局")
        topology = analyze_topology(
            level,
            graph,
            plan.layout,
            topology_inputs,
            serial_difficulty=plan.serial_difficulty,
        )
        if not topology.passed:
            product_rule_failure(
                "unit_topology",
                "独立复算后谜题单元拓扑不满足布局硬规则",
                reasons=list(topology.reasons),
                topology=topology.as_dict(),
            )
        for unit, unit_report in zip(candidate.units, topology.unit_reports):
            expected_stats = (
                len(unit_report.nested_a_indices),
                len(unit_report.nested_b_indices),
                unit_report.quarter_cover_relations,
                unit_report.half_cover_relations,
                unit_report.full_cover_relations,
                len(unit_report.blocking_a_indices),
                len(unit_report.blocking_b_indices),
                unit_report.initial_entry_count,
                tuple(unit_report.influence_indices),
            )
            actual_stats = (
                unit.nested_a_count,
                unit.nested_b_count,
                unit.quarter_cover_relations,
                unit.half_cover_relations,
                unit.full_cover_relations,
                unit.external_blocking_a_count,
                unit.external_blocking_b_count,
                unit.initial_entry_count,
                tuple(unit.influence_indices),
            )
            if actual_stats != expected_stats:
                _fail(
                    "unit_geometry_snapshot",
                    "候选携带的单元嵌套/遮挡统计与独立重算不一致",
                    unit_id=unit.unit_id,
                )
            nested_a.update(unit_report.nested_a_indices)
            nested_b.update(unit_report.nested_b_indices)
        if topology.passed:
            checks.append(
                ValidationCheck(
                    "unit_topology",
                    True,
                    "谜题单元布局、依赖、入口与 A/B 归属复算通过",
                    topology.as_dict(),
                )
            )
        existing_static_indices = tuple(
            tile.index
            for tile in level.tiles
            if tile.tile_id in STRUCTURE_ID_TO_TYPE and not tile.is_tower_member
        )
        legacy_isolation = analyze_existing_structure_isolation(
            level,
            graph,
            topology_inputs,
            existing_structure_indices=existing_static_indices,
        )
        if not legacy_isolation.passed:
            product_rule_failure(
                "existing_structure_isolation",
                "新谜题单元与源模板既有塔外 C–N 未完全隔离",
                isolation=legacy_isolation.as_dict(),
            )
        if legacy_isolation.passed:
            checks.append(
                ValidationCheck(
                    "existing_structure_isolation",
                    True,
                    "新谜题单元与源模板既有塔外 C–N 拓扑隔离复算通过",
                    legacy_isolation.as_dict(),
                )
            )

        all_generated_static = frozenset(
            index for unit in topology_inputs for index in unit.selected_indices
        )
        for topology_unit, influence in zip(
            topology_inputs, topology.unit_reports
        ):
            structures = frozenset(topology_unit.selected_indices)
            per_tile = tuple(
                TileExternalBlockers(
                    structure_index=index,
                    external_direct_blocker_indices=tuple(
                        sorted(
                            graph.direct_blockers[index].difference(
                                all_generated_static
                            )
                        )
                    ),
                )
                for index in sorted(structures)
            )
            direct_external = frozenset(
                blocker
                for detail in per_tile
                for blocker in detail.external_direct_blocker_indices
            )
            transitive_external = frozenset(
                blocker
                for index in structures
                for blocker in graph.transitive_blockers[index]
                if blocker not in all_generated_static
            )
            unit_access_reports.append(
                UnitAccessReport(
                    unit_id=topology_unit.unit_id,
                    structure_indices=tuple(sorted(structures)),
                    initial_entry_indices=influence.initial_entry_indices,
                    external_direct_blockers_by_tile=per_tile,
                    external_direct_blocker_indices=tuple(
                        sorted(direct_external)
                    ),
                    external_transitive_blocker_indices=tuple(
                        sorted(transitive_external)
                    ),
                    external_blocking_a_indices=influence.blocking_a_indices,
                    external_blocking_b_indices=influence.blocking_b_indices,
                )
            )
        checks.append(
            ValidationCheck(
                "unit_access",
                True,
                "各谜题单元的初始入口与外部直接/传递阻挡已逐 Tile 记录",
                {
                    "units": [
                        report.as_dict() for report in unit_access_reports
                    ]
                },
            )
        )

    tower_allocation: TowerOnlyAllocation | None = None
    if candidate.tower_group is not None and options.tower_options is not None:
        tower_selected = candidate.tower_group.selected_indices
        if (
            options.tower_options.count_limit is not None
            and len(tower_selected) > options.tower_options.count_limit
        ):
            product_rule_failure(
                "tower_count_limit",
                "塔分布组超过自身结构块数上限",
                actual=len(tower_selected),
                limit=options.tower_options.count_limit,
            )
        if options.tower_options.max_layer is not None and any(
            level.tiles[index].layer > options.tower_options.max_layer
            for index in tower_selected
        ):
            product_rule_failure(
                "tower_layer_limit",
                "塔分布组超过初始序列化最高层限制",
                limit=options.tower_options.max_layer,
            )

        tower_layer_limit = effective_layer_limit
        if options.tower_options.max_layer is not None:
            tower_layer_limit = min(
                tower_layer_limit, options.tower_options.max_layer
            )
        tower_pool = tuple(
            tile.index
            for tile in level.tiles
            if tile.tile_id in source_ids
            and tile.debuff_data_index in (None, -1)
            and tile.is_tower_member
            and tile.layer <= tower_layer_limit
        )
        requested_counts = Counter(candidate.tower_group.assigned_types)
        try:
            tower_allocation = allocate_only_mode(
                level, tower_pool, requested_counts
            )
        except Exception as exc:
            _fail(
                "tower_allocation",
                "独立复算塔分布失败",
                error=str(exc),
            )
        expected_tower_indices = tuple(
            item.tile_index for item in tower_allocation.assignments
        )
        expected_tower_types = tuple(
            item.structure_type for item in tower_allocation.assignments
        )
        expected_tower_counts = tuple(
            (bucket.tower_index, bucket.assigned_count)
            for bucket in tower_allocation.towers
            if bucket.assigned_count
        )
        if (
            tuple(candidate.tower_group.selected_indices) != expected_tower_indices
            or tuple(candidate.tower_group.assigned_types) != expected_tower_types
            or tuple(candidate.tower_group.tower_counts) != expected_tower_counts
            or candidate.tower_group.max_min_difference
            != tower_allocation.max_tower_count_difference
        ):
            _fail(
                "tower_allocation_snapshot",
                "候选携带的塔内均匀分布与独立复算不一致",
            )
        checks.append(
            ValidationCheck(
                "tower_allocation",
                True,
                "塔成员归属与均匀分布已按 StackData 独立复算",
                tower_allocation.as_dict(),
            )
        )

    combination = None
    combination_conclusion: CombinationConclusion | None = None
    if combination_inputs and options.quality.special_preset != "all_types_long_range":
        assert plan.layout is not None
        combination = analyze_combination(
            combination_inputs,
            layout=plan.layout,
            limits=options.solver_limits,
            _unit_proofs=tuple(unit_proofs),
        )
        if not combination.all_units_qualified:
            product_rule_failure(
                "combination",
                "组合复算包含不合格单元",
            )
        capacity_seven = tuple(
            proof
            for proof in combination.ordered_capacity_proofs
            if proof.capacity == 7
        )
        if all(item.target_step <= 7 for item in combination_inputs):
            if not capacity_seven or any(
                not proof.complete_exists for proof in capacity_seven
            ):
                product_rule_failure(
                    "combination_completion",
                    "7 格背包下的允许整单元顺序未全部存在完成路径",
                )
            combination_message = "共享 7 格背包的允许整单元顺序均存在完成路径"
            completion_requirement = "all_allowed_whole_unit_orders_complete_at_capacity_7"
            acceptance_basis = "unit_targets_and_capacity_7_whole_unit_orders"
        else:
            # s8/s9 are pressure structures by design.  Their individual
            # proofs own the failure/consumption semantics; a missing full
            # completion at capacity 7 must not be mislabeled as a failed
            # candidate or as a complete combination proof.
            combination_message = (
                "包含 s8/s9 高压单元：单元时序已验证，"
                "组合完成性不做过度承诺"
            )
            completion_requirement = "not_required_for_pressure_units_s8_s9"
            acceptance_basis = "individual_pressure_semantics_and_topology"
        combination_conclusion = CombinationConclusion(
            layout=plan.layout.value,
            unit_count=len(combination_inputs),
            contains_pressure_unit=any(
                item.target_step >= 8 for item in combination_inputs
            ),
            all_units_qualified=combination.all_units_qualified,
            capacity_7_allowed_order_count=len(capacity_seven),
            capacity_7_all_allowed_orders_complete=bool(capacity_seven)
            and all(proof.complete_exists for proof in capacity_seven),
            completion_requirement=completion_requirement,
            acceptance_basis=acceptance_basis,
            interleaving_coverage=combination.interleaving_coverage,
            interleaving_result_at_capacity_7=(
                combination.interleaving_result_at_capacity_7
            ),
            safe_interleaving_exists_at_capacity_7=(
                combination.safe_interleaving_exists_at_capacity_7
            ),
        )
        checks.append(
            ValidationCheck(
                "combination",
                True,
                combination_message,
                combination_conclusion.as_dict(),
            )
        )
    elif combination_inputs:
        checks.append(
            ValidationCheck(
                "combination",
                True,
                "36 块特例按已证明波次边界逐波复算，不伪装为全局穷举",
                {
                    "acceptance_basis": (
                        "static_wave_dominance_and_exact_per_wave_capacity7"
                    ),
                    "special_preset": "all_types_long_range",
                },
            )
        )

    rewrite_id = "validation_" + hashlib.sha256(candidate.candidate_id.encode("utf-8")).hexdigest()[:16]
    replacement_ids = {
        item.tile_index: item.structure_tile_id for item in replacements
    }
    output = rewrite_template(level, rewrite_id, replacement_ids)
    verification = verify_rewrite(level, output, rewrite_id, replacement_ids)
    reparsed = parse_level_bytes(output, source_name="<candidate-validation>")
    if reparsed.template_id != rewrite_id or len(reparsed.tiles) != len(level.tiles):
        _fail("rewrite_roundtrip", "内存改写后的模板无法一致重读")
    checks.append(ValidationCheck("wire_rewrite", True, "wire 白名单改写与重读验证通过"))

    structure_counts = Counter(item.structure_type for item in replacements)
    existing_structure_counts = Counter(
        STRUCTURE_ID_TO_TYPE[tile.tile_id]
        for tile in level.tiles
        if tile.tile_id in STRUCTURE_ID_TO_TYPE
    )
    output_structure_counts = Counter(
        STRUCTURE_ID_TO_TYPE[tile.tile_id]
        for tile in reparsed.tiles
        if tile.tile_id in STRUCTURE_ID_TO_TYPE
    )
    expected_output_structure_counts = existing_structure_counts + structure_counts
    if output_structure_counts != expected_output_structure_counts:
        _fail(
            "output_structure_inventory",
            "重读后的输出 C–N 总量与既有结构加本次替换不一致",
            expected=sorted(expected_output_structure_counts.items()),
            actual=sorted(output_structure_counts.items()),
        )
    inventory = StructureInventoryReport(
        source_total_tile_count=len(level.tiles),
        output_total_tile_count=len(reparsed.tiles),
        source_existing_structure_count=sum(existing_structure_counts.values()),
        source_existing_structure_counts=tuple(
            sorted(existing_structure_counts.items())
        ),
        new_structure_count=len(replacements),
        new_structure_counts=tuple(sorted(structure_counts.items())),
        output_structure_count=sum(output_structure_counts.values()),
        output_structure_counts=tuple(sorted(output_structure_counts.items())),
    )
    effective_constraints = EffectiveConstraintReport(
        template_total_tile_count=len(level.tiles),
        template_highest_layer=level.max_layer,
        percentage_scope=(
            "non_tower_tiles"
            if plan.tower_mode is TowerMode.DISALLOW
            else "all_tiles"
        ),
        percentage_tile_count=len(_constraint_tiles(level, plan.tower_mode)),
        percentage_highest_layer=max(
            (
                tile.layer
                for tile in _constraint_tiles(level, plan.tower_mode)
            ),
            default=0,
        ),
        configured_count_limit=options.total_count_limit,
        configured_count_minimum=options.total_count_minimum,
        configured_percent_minimum=options.total_percent_minimum,
        effective_structure_count_minimum=effective_count_minimum,
        count_minimum_passed=len(replacements) >= effective_count_minimum,
        configured_count_percent=options.total_percent_limit,
        effective_structure_count_limit=effective_count_limit,
        actual_new_structure_count=len(replacements),
        count_limit_passed=len(replacements) <= effective_count_limit,
        configured_max_layer=options.max_layer,
        configured_max_layer_percent=options.max_layer_percent,
        effective_max_structure_layer=effective_layer_limit,
        actual_highest_structure_layer=highest_layer,
        layer_limit_passed=(
            highest_layer is None or highest_layer <= effective_layer_limit
        ),
        configured_max_structure_layer_span=(
            options.max_structure_layer_span
        ),
        actual_structure_layer_span=actual_layer_span,
        structure_layer_span_passed=(
            options.max_structure_layer_span is None
            or actual_layer_span is None
            or actual_layer_span <= options.max_structure_layer_span
        ),
    )
    checks.append(
        ValidationCheck(
            "structure_inventory",
            True,
            "源模板既有 C–N、新增 C–N、输出 C–N 与 Tile 总量已重读核对",
            inventory.as_dict(),
        )
    )
    checks.append(
        ValidationCheck(
            "effective_constraints",
            (
                effective_constraints.count_limit_passed
                and effective_constraints.layer_limit_passed
                and effective_constraints.structure_layer_span_passed
            ),
            (
                "实际结构数量、最高层和最大层间距未超过解析后的有效上限"
                if (
                    effective_constraints.count_limit_passed
                    and effective_constraints.layer_limit_passed
                    and effective_constraints.structure_layer_span_passed
                )
                else "实际结构数量、最高层或最大层间距超过配置规则；仅可人工确认导出"
            ),
            effective_constraints.as_dict(),
        )
    )

    effective_difficulty_profile = (
        difficulty_profile
        if difficulty_profile is not None
        else difficulty_profile_from_simulation(candidate.difficulty_simulation)
    )
    expected_difficulty = simulate_candidate_difficulty(
        level,
        replacements,
        target=options.quality.difficulty_target,
        profile=effective_difficulty_profile,
        requested_archetype=options.quality.experience_archetype,
    )
    if dict(candidate.difficulty_simulation) != expected_difficulty:
        _fail(
            "difficulty_simulation",
            "候选携带的 DupPara1–10 难度模拟与独立重算结果不一致",
        )
    trend_report = expected_difficulty.get("trend")
    trend_safe = bool(
        trend_report.get(
            "safety_qualified", trend_report.get("becomes_easier", False)
        )
        if isinstance(trend_report, Mapping)
        else False
    )
    difficulty_qualified = bool(expected_difficulty.get("qualified"))
    checks.append(
        ValidationCheck(
            "difficulty_simulation",
            difficulty_qualified and trend_safe,
            (
                "DupPara1–10 策略模拟已独立重算，难度与梯度符合自动推荐条件"
                if difficulty_qualified and trend_safe
                else "DupPara1–10 策略模拟已独立重算，但难度或梯度未达标；仅可人工确认导出"
            ),
            {
                "target": options.quality.difficulty_target,
                "coverage": expected_difficulty["coverage"],
                "source_difficulty_distance": expected_difficulty[
                    "source_difficulty_distance"
                ],
                "becomes_easier": expected_difficulty["trend"][
                    "becomes_easier"
                ],
                "qualified": expected_difficulty["qualified"],
                "trend_safe": trend_safe,
                "auto_recommendation_eligible": (
                    difficulty_qualified and trend_safe
                ),
            },
        )
    )

    expected_experience = evaluate_candidate_experience(
        level,
        candidate,
        options.quality,
        expected_difficulty,
    )
    if dict(candidate.experience_analysis) != expected_experience:
        _fail(
            "experience_analysis",
            "候选携带的整板揭露、耦合、风格与机制压力证据和独立重算不一致",
        )
    if candidate.experience_signature != str(
        expected_experience.get("experience_signature", "")
    ):
        _fail("experience_signature", "候选体验指纹与独立重算不一致")
    hard_status = str(
        expected_experience.get("hard_validation", {}).get(
            "status", "inconclusive"
        )
    )
    # A disproved or incomplete experience conclusion is a product review
    # result, not evidence that the candidate payload or rewritten template is
    # corrupt.  Callers that explicitly request the legacy strict gate still
    # receive an exception; generation and export review use ``False`` so the
    # conclusion can be shown to the user and explicitly accepted.
    if require_exportable_experience and hard_status != "proved":
        _fail(
            "experience_hard_validation",
            (
                "整板体验或特殊语法硬校验已被反证"
                if hard_status == "disproved"
                else "整板体验证据不足，未达到自动推荐条件"
            ),
            evidence_coverage=expected_experience.get("evidence_coverage"),
        )
    if require_exportable_experience and (
        not difficulty_qualified or not trend_safe
    ):
        _fail(
            "difficulty_target",
            "候选未达到本次选择的难度目标或副本梯度自动推荐条件",
            target=options.quality.difficulty_target,
            qualified=difficulty_qualified,
            trend_safe=trend_safe,
            downgrade_reasons=expected_difficulty.get("downgrade_reasons", ()),
        )
    checks.append(
        ValidationCheck(
            "experience_analysis",
            hard_status != "disproved",
            (
                "整板体验证据已独立重算并证明通过"
                if hard_status == "proved"
                else "整板体验结论已独立重算；不自动推荐，用户确认提示后仍可导出"
            ),
            {
                "auto_recommendation_eligible": hard_status == "proved",
                "hard_validation": expected_experience.get("hard_validation"),
                "archetype": expected_experience.get("archetype"),
                "archetype_match": expected_experience.get(
                    "archetype_match"
                ),
                "online_style_distance": expected_experience.get(
                    "online_style_distance"
                ),
                "simulation_pressure": expected_experience.get(
                    "simulation_pressure"
                ),
                "evidence_coverage": expected_experience.get(
                    "evidence_coverage"
                ),
            },
        )
    )

    initial_entry_indices = {
        index
        for report in unit_access_reports
        for index in report.initial_entry_indices
    }
    external_blocker_indices = {
        index
        for report in unit_access_reports
        for index in report.external_transitive_blocker_indices
    }
    replaced_a = sum(item.source_tile_id == -10 for item in replacements)
    replaced_b = sum(item.source_tile_id == -11 for item in replacements)
    rule_failures = tuple(check for check in checks if not check.passed)
    auto_recommendation_eligible = bool(
        not rule_failures
        and hard_status == "proved"
        and difficulty_qualified
        and trend_safe
    )
    return CandidateValidationReport(
        candidate_id=candidate.candidate_id,
        source_template_id=level.template_id,
        source_sha256=level.sha256,
        validation_template_id=rewrite_id,
        validation_output_sha256=verification.output_sha256,
        # ``passed`` remains the candidate-rule axis used by selection/export.
        # Difficulty and evidence conclusions have their own axes and remain
        # in ``rule_failures``/``auto_recommendation_eligible`` without being
        # double-counted as payload or candidate-rule corruption.
        passed=not candidate_rule_failures and hard_status != "disproved",
        technical_integrity_passed=True,
        manual_export_eligible=True,
        auto_recommendation_eligible=auto_recommendation_eligible,
        checks=tuple(checks),
        rule_failures=rule_failures,
        replacement_count=len(replacements),
        replaced_a_count=replaced_a,
        replaced_b_count=replaced_b,
        structure_counts=tuple(sorted(structure_counts.items())),
        tower_structure_count=tower_count,
        non_tower_structure_count=static_count,
        highest_structure_layer=highest_layer,
        nested_a_count=len(nested_a),
        nested_b_count=len(nested_b),
        initial_entry_count=len(initial_entry_indices),
        external_blocker_count=len(external_blocker_indices),
        inventory=inventory,
        effective_constraints=effective_constraints,
        coverage_audit=coverage_audit,
        unit_access_reports=tuple(unit_access_reports),
        combination_proof=combination,
        combination_conclusion=combination_conclusion,
        topology_report=topology,
        existing_structure_isolation=legacy_isolation,
        unit_proofs=tuple(unit_proofs),
        tower_allocation=tower_allocation,
    )
