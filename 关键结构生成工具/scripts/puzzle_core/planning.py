"""Deterministic normalization of raw puzzle plans and batch overrides."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import unicodedata
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple, Type, TypeVar

from .models import (
    ExpansionPolicy,
    Layout,
    PlanningError,
    RawBatchRequest,
    RawOutputPlan,
    ResolvedBatch,
    ResolvedOutputPlan,
    ResolvedPuzzleUnit,
    ResolvedTemplatePlans,
    SerialDifficultyPolicy,
    StepKind,
    StepSpec,
    TemplatePlanMode,
    TemplatePlanRule,
    TowerMode,
    UnitRole,
)
from .limits import (
    MAX_BATCH_PLANS,
    MAX_BATCH_TEMPLATES,
    MAX_LOCAL_PLANS_PER_TEMPLATE,
    MAX_PLAN_DEFINITIONS,
    MAX_RESOLVED_WORK_ITEMS,
    MAX_TEMPLATE_RULES,
)
from .naming import step_signature


_E = TypeVar("_E", bound=Enum)


def _as_enum(value: Any, enum_type: Type[_E], path: str) -> _E:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise PlanningError(
            "invalid_enum", f"expected one of: {allowed}", path
        ) from exc


def _to_primitive(value: Any) -> Any:
    if isinstance(value, Enum):
        return _to_primitive(value.value)
    if is_dataclass(value):
        return {
            field.name: _to_primitive(getattr(value, field.name))
            for field in fields(value)
            if not (
                field.metadata.get("canonical_omit_if_none", False)
                and getattr(value, field.name) is None
            )
            and not (
                field.metadata.get("canonical_omit_if_default", False)
                and getattr(value, field.name) == field.default
            )
        }
    if isinstance(value, Mapping):
        normalized: Dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = unicodedata.normalize("NFC", str(key))
            if normalized_key in normalized:
                raise PlanningError(
                    "duplicate_canonical_key",
                    "mapping keys collide after Unicode normalization",
                )
            normalized[normalized_key] = _to_primitive(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_to_primitive(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_to_primitive(item) for item in value]
        return sorted(items, key=lambda item: canonical_json(item))
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, float) and not math.isfinite(value):
        raise PlanningError(
            "non_finite_number", "NaN and Infinity are not canonical values"
        )
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise PlanningError(
        "unsupported_canonical_type",
        f"cannot canonicalize value of type {type(value).__name__}",
    )


def canonical_json(value: Any) -> str:
    """Return the cross-process canonical JSON representation used for hashes."""

    return json.dumps(
        _to_primitive(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def config_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DerivedSeed:
    """A PRNG-compatible 64-bit seed plus its complete audit digest.

    Python's ``random.Random`` accepts the integer value, while the full
    SHA-256 digest prevents the audit trail from silently discarding 192 bits
    of the deterministic derivation identity.
    """

    value: int
    sha256: str


def derive_seed_material(*parts: Any) -> DerivedSeed:
    digest = hashlib.sha256(canonical_json(parts).encode("utf-8")).digest()
    return DerivedSeed(
        value=int.from_bytes(digest[:8], byteorder="big", signed=False),
        sha256=digest.hex(),
    )


def derive_seed(*parts: Any) -> int:
    return derive_seed_material(*parts).value


def derive_candidate_seed_material(
    batch_seed: int,
    source_sha: str,
    plan: ResolvedOutputPlan,
    candidate_index: int,
    algorithm_version: str,
) -> DerivedSeed:
    """Derive one candidate-slot seed, including its explicit ordinal."""

    if isinstance(candidate_index, bool) or not isinstance(candidate_index, int):
        raise PlanningError(
            "invalid_candidate_index",
            "candidate index must be a non-negative integer",
            "candidate_index",
        )
    if candidate_index < 0:
        raise PlanningError(
            "invalid_candidate_index",
            "candidate index must be a non-negative integer",
            "candidate_index",
        )
    return derive_seed_material(
        "candidate_seed_v2",
        batch_seed,
        source_sha,
        plan.config_hash,
        candidate_index,
        algorithm_version,
    )


def derive_candidate_seed(
    batch_seed: int,
    source_sha: str,
    plan: ResolvedOutputPlan,
    candidate_index: int,
    algorithm_version: str,
) -> int:
    return derive_candidate_seed_material(
        batch_seed,
        source_sha,
        plan,
        candidate_index,
        algorithm_version,
    ).value


def _require_step_integer(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlanningError(
            "invalid_step", "structure step must be an integer", path
        )
    if not 4 <= value <= 9:
        raise PlanningError(
            "step_out_of_range", "structure step must be between 4 and 9", path
        )
    return value


def _step_domain(
    spec: StepSpec,
    *,
    batch_seed: int,
    template_key: str,
    plan_payload: Any,
    unit_index: int,
    path: str,
) -> Tuple[int, ...]:
    kind = _as_enum(spec.kind, StepKind, f"{path}.kind")
    policy = _as_enum(spec.policy, ExpansionPolicy, f"{path}.policy")

    if kind is StepKind.EXACT:
        if spec.value is None:
            raise PlanningError(
                "missing_step", "exact step requires value", f"{path}.value"
            )
        value = _require_step_integer(spec.value, f"{path}.value")
        if spec.minimum is not None or spec.maximum is not None:
            raise PlanningError(
                "conflicting_step_fields",
                "exact step cannot also define minimum or maximum",
                path,
            )
        return (value,)

    if spec.value is not None:
        raise PlanningError(
            "conflicting_step_fields",
            f"{kind.value} step uses minimum/maximum, not value",
            path,
        )
    if spec.minimum is None or spec.maximum is None:
        raise PlanningError(
            "missing_step_range",
            f"{kind.value} step requires minimum and maximum",
            path,
        )
    minimum = _require_step_integer(spec.minimum, f"{path}.minimum")
    maximum = _require_step_integer(spec.maximum, f"{path}.maximum")
    if minimum > maximum:
        raise PlanningError(
            "invalid_step_range", "minimum cannot exceed maximum", path
        )

    values = tuple(range(minimum, maximum + 1))
    if policy is ExpansionPolicy.EXPAND_EACH:
        return values

    choice_seed = derive_seed(
        "step_choice_v1",
        batch_seed,
        template_key,
        plan_payload,
        unit_index,
        kind.value,
    )
    return (values[choice_seed % len(values)],)


def _raw_plan_payload(plan: RawOutputPlan) -> Mapping[str, Any]:
    """Semantic raw payload; the human-facing plan ID is intentionally omitted."""

    return {
        "tower_mode": plan.tower_mode,
        "layout": plan.layout,
        "serial_difficulty": plan.serial_difficulty,
        "units": plan.units,
    }


def _unit_roles(layout: Layout, unit_count: int) -> Tuple[UnitRole, ...]:
    if unit_count == 1:
        return (UnitRole.SINGLE,)
    if layout is Layout.X:
        return (UnitRole.LEFT, UnitRole.RIGHT)
    if layout is Layout.Y:
        return (UnitRole.TOP, UnitRole.BOTTOM)
    if layout is Layout.Z:
        # This order is also the naming order, keeping the lowest unit last.
        return (UnitRole.HIGH, UnitRole.LOW)
    raise PlanningError(
        "missing_layout", "two-unit plans require x, y, or z layout", "layout"
    )


def _serial_steps_allowed(
    steps: Sequence[int], policy: SerialDifficultyPolicy
) -> bool:
    if len(steps) != 2:
        return True
    high, low = steps
    if policy is SerialDifficultyPolicy.HARDER_LOW:
        return high <= low
    if policy is SerialDifficultyPolicy.EASIER_LOW:
        return high >= low
    return True


def _resolved_plan_semantics(plan: ResolvedOutputPlan) -> Mapping[str, Any]:
    # Serial ordering is meaningful only for a Z plan.  Ignoring it elsewhere
    # prevents two otherwise identical plans from evading duplicate detection
    # through a disabled field.
    serial_difficulty = (
        plan.serial_difficulty if plan.layout is Layout.Z else None
    )
    return {
        "tower_mode": plan.tower_mode,
        "layout": plan.layout,
        "serial_difficulty": serial_difficulty,
        "units": [
            {"role": unit.role, "step": unit.step} for unit in plan.units
        ],
    }


def _required_text(value: Any, code: str, message: str, path: str) -> str:
    if not isinstance(value, str):
        raise PlanningError(code, message, path)
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized:
        raise PlanningError(code, message, path)
    return normalized


def normalize_output_plan(
    raw_plan: RawOutputPlan,
    *,
    batch_seed: int,
    template_key: str,
) -> Tuple[ResolvedOutputPlan, ...]:
    """Resolve one raw plan into one or more immutable output plans."""

    plan_id = _required_text(
        raw_plan.plan_id, "missing_plan_id", "plan ID is required", "plan_id"
    )

    tower_mode = _as_enum(raw_plan.tower_mode, TowerMode, "tower_mode")
    serial_policy = _as_enum(
        raw_plan.serial_difficulty,
        SerialDifficultyPolicy,
        "serial_difficulty",
    )
    if raw_plan.units is None:
        raise PlanningError(
            "missing_units", "static plans require one or two puzzle units", "units"
        )
    raw_units = tuple(raw_plan.units)

    if tower_mode is TowerMode.ONLY:
        if raw_units:
            raise PlanningError(
                "tower_has_units",
                "tower-only plans cannot define puzzle units or steps",
                "units",
            )
        if raw_plan.layout is not None:
            raise PlanningError(
                "tower_has_layout",
                "tower-only plans cannot define a puzzle layout",
                "layout",
            )
        provisional = ResolvedOutputPlan(
            plan_id=plan_id,
            source_plan_id=plan_id,
            tower_mode=tower_mode,
            layout=None,
            serial_difficulty=serial_policy,
            units=(),
            signature="tower",
            config_hash="",
        )
        digest = config_hash(_resolved_plan_semantics(provisional))
        return (
            ResolvedOutputPlan(
                **{
                    **provisional.__dict__,
                    "config_hash": digest,
                }
            ),
        )

    unit_count = len(raw_units)
    if unit_count == 0:
        raise PlanningError(
            "missing_units", "static plans require one or two puzzle units", "units"
        )
    if unit_count > 2:
        raise PlanningError(
            "too_many_units", "a plan may contain at most two puzzle units", "units"
        )

    if unit_count == 1:
        if raw_plan.layout is None:
            layout = Layout.SINGLE
        else:
            layout = _as_enum(raw_plan.layout, Layout, "layout")
            if layout is not Layout.SINGLE:
                raise PlanningError(
                    "invalid_single_layout",
                    "one-unit plans use single layout",
                    "layout",
                )
    else:
        if raw_plan.layout is None:
            raise PlanningError(
                "missing_layout", "two-unit plans require x, y, or z layout", "layout"
            )
        layout = _as_enum(raw_plan.layout, Layout, "layout")
        if layout is Layout.SINGLE:
            raise PlanningError(
                "invalid_multi_layout",
                "two-unit plans require x, y, or z layout",
                "layout",
            )

    normalized_unit_ids = []
    for index, unit in enumerate(raw_units):
        if not isinstance(unit.unit_id, str):
            raise PlanningError(
                "invalid_unit_id", "puzzle unit ID must be text", f"units[{index}].unit_id"
            )
        normalized_unit_ids.append(
            unicodedata.normalize("NFC", unit.unit_id).strip()
        )
    raw_ids = [unit_id for unit_id in normalized_unit_ids if unit_id]
    if len(raw_ids) != len(set(raw_ids)):
        raise PlanningError(
            "duplicate_unit_id", "puzzle unit IDs must be unique", "units"
        )

    plan_payload = _raw_plan_payload(raw_plan)
    step_domains = []
    for index, unit in enumerate(raw_units):
        if unit.step is None:
            raise PlanningError(
                "missing_step",
                "every puzzle unit requires a structure step specification",
                f"units[{index}].step",
            )
        step_domains.append(
            _step_domain(
                unit.step,
                batch_seed=batch_seed,
                template_key=template_key,
                plan_payload=plan_payload,
                unit_index=index,
                path=f"units[{index}].step",
            )
        )

    roles = _unit_roles(layout, unit_count)
    step_combinations = list(itertools.product(*step_domains))
    if layout is Layout.Z:
        step_combinations = [
            combination
            for combination in step_combinations
            if _serial_steps_allowed(combination, serial_policy)
        ]
        if not step_combinations:
            raise PlanningError(
                "serial_difficulty_conflict",
                "no step combination satisfies the selected serial difficulty order",
                "serial_difficulty",
            )

    resolved = []
    expanded = len(step_combinations) > 1
    for combination in step_combinations:
        units = tuple(
            ResolvedPuzzleUnit(
                unit_id=normalized_unit_ids[index] or f"U{index + 1}",
                role=roles[index],
                step=step,
            )
            for index, step in enumerate(combination)
        )
        provisional = ResolvedOutputPlan(
            plan_id=plan_id,
            source_plan_id=plan_id,
            tower_mode=tower_mode,
            layout=layout,
            serial_difficulty=serial_policy,
            units=units,
            signature="",
            config_hash="",
        )
        signature = step_signature(provisional)
        resolved_id = f"{plan_id}@{signature}" if expanded else plan_id
        with_signature = ResolvedOutputPlan(
            **{
                **provisional.__dict__,
                "plan_id": resolved_id,
                "signature": signature,
            }
        )
        digest = config_hash(_resolved_plan_semantics(with_signature))
        resolved.append(
            ResolvedOutputPlan(
                **{
                    **with_signature.__dict__,
                    "config_hash": digest,
                }
            )
        )
    return tuple(resolved)


def _validate_plan_ids(plans: Iterable[RawOutputPlan], path: str) -> None:
    seen = set()
    for index, plan in enumerate(plans):
        normalized = _required_text(
            plan.plan_id,
            "missing_plan_id",
            "plan ID is required",
            f"{path}[{index}].plan_id",
        )
        if normalized in seen:
            raise PlanningError(
                "duplicate_plan_id",
                f"plan ID {normalized!r} appears more than once",
                f"{path}[{index}].plan_id",
            )
        seen.add(normalized)


def _rule_for_template(
    rule: TemplatePlanRule, index: int
) -> Tuple[str, TemplatePlanMode, Tuple[RawOutputPlan, ...]]:
    key = _required_text(
        rule.template_key,
        "missing_template_key",
        "template rule requires a template key",
        f"template_rules[{index}].template_key",
    )
    mode = _as_enum(rule.mode, TemplatePlanMode, f"template_rules[{index}].mode")
    plans = tuple(rule.plans)
    if mode in (TemplatePlanMode.USE_BATCH, TemplatePlanMode.SKIP) and plans:
        raise PlanningError(
            "unexpected_local_plans",
            f"{mode.value} rules cannot contain local plans",
            f"template_rules[{index}].plans",
        )
    if mode in (TemplatePlanMode.REPLACE, TemplatePlanMode.APPEND) and not plans:
        raise PlanningError(
            "missing_local_plans",
            f"{mode.value} rules require at least one local plan",
            f"template_rules[{index}].plans",
        )
    _validate_plan_ids(plans, f"template_rules[{index}].plans")
    return key, mode, plans


def resolve_batch(request: RawBatchRequest) -> ResolvedBatch:
    """Apply per-template batch modes and normalize every effective plan."""

    if request.template_keys is None or not request.template_keys:
        raise PlanningError(
            "missing_templates", "at least one non-empty template key is required"
        )
    if len(request.template_keys) > MAX_BATCH_TEMPLATES:
        raise PlanningError(
            "template_limit_exceeded",
            f"at most {MAX_BATCH_TEMPLATES} templates are allowed",
            "template_keys",
        )
    template_keys = tuple(
        _required_text(
            key,
            "missing_template_key",
            "template key is required",
            f"template_keys[{index}]",
        )
        for index, key in enumerate(request.template_keys)
    )
    if len(template_keys) != len(set(template_keys)):
        raise PlanningError(
            "duplicate_template", "template keys must be unique", "template_keys"
        )

    batch_plans = tuple(request.batch_plans)
    if not batch_plans:
        raise PlanningError(
            "missing_batch_plans", "at least one batch plan is required", "batch_plans"
        )
    if len(batch_plans) > MAX_BATCH_PLANS:
        raise PlanningError(
            "plan_limit_exceeded",
            f"at most {MAX_BATCH_PLANS} batch plans are allowed",
            "batch_plans",
        )
    _validate_plan_ids(batch_plans, "batch_plans")

    if len(request.template_rules) > MAX_TEMPLATE_RULES:
        raise PlanningError(
            "template_rule_limit_exceeded",
            f"at most {MAX_TEMPLATE_RULES} template rules are allowed",
            "template_rules",
        )
    plan_definition_count = len(batch_plans) + sum(
        len(rule.plans) for rule in request.template_rules
    )
    if plan_definition_count > MAX_PLAN_DEFINITIONS:
        raise PlanningError(
            "plan_definition_limit_exceeded",
            f"at most {MAX_PLAN_DEFINITIONS} plans may be defined in one request",
            "template_rules",
        )

    rules: Dict[str, Tuple[TemplatePlanMode, Tuple[RawOutputPlan, ...]]] = {}
    for index, rule in enumerate(request.template_rules):
        if len(rule.plans) > MAX_LOCAL_PLANS_PER_TEMPLATE:
            raise PlanningError(
                "plan_limit_exceeded",
                (
                    "at most "
                    f"{MAX_LOCAL_PLANS_PER_TEMPLATE} local plans are allowed per template"
                ),
                f"template_rules[{index}].plans",
            )
        key, mode, plans = _rule_for_template(rule, index)
        if key not in template_keys:
            raise PlanningError(
                "unknown_template_rule",
                f"rule references unknown template {key!r}",
                f"template_rules[{index}].template_key",
            )
        if key in rules:
            raise PlanningError(
                "duplicate_template_rule",
                f"template {key!r} has more than one rule",
                f"template_rules[{index}]",
            )
        rules[key] = (mode, plans)

    resolved_templates = []
    resolved_work_item_count = 0
    for template_key in template_keys:
        mode, local_plans = rules.get(
            template_key, (TemplatePlanMode.USE_BATCH, ())
        )
        if mode is TemplatePlanMode.SKIP:
            resolved_templates.append(
                ResolvedTemplatePlans(template_key=template_key, plans=(), skipped=True)
            )
            continue
        if mode is TemplatePlanMode.USE_BATCH:
            effective_plans = batch_plans
        elif mode is TemplatePlanMode.REPLACE:
            effective_plans = local_plans
        else:
            effective_plans = batch_plans + local_plans

        _validate_plan_ids(effective_plans, f"templates[{template_key}].plans")
        resolved_plans = []
        semantic_hashes: Dict[str, str] = {}
        for raw_plan in effective_plans:
            for plan in normalize_output_plan(
                raw_plan,
                batch_seed=request.batch_seed,
                template_key=template_key,
            ):
                semantic_digest = config_hash(_resolved_plan_semantics(plan))
                prior_id = semantic_hashes.get(semantic_digest)
                if prior_id is not None:
                    raise PlanningError(
                        "duplicate_plan",
                        (
                            f"plans {prior_id!r} and {plan.plan_id!r} resolve to "
                            "the same output semantics"
                        ),
                        f"templates[{template_key}].plans",
                    )
                semantic_hashes[semantic_digest] = plan.plan_id
                resolved_plans.append(plan)
                resolved_work_item_count += 1
                if resolved_work_item_count > MAX_RESOLVED_WORK_ITEMS:
                    raise PlanningError(
                        "work_item_limit_exceeded",
                        (
                            "expanded request exceeds the hard limit of "
                            f"{MAX_RESOLVED_WORK_ITEMS} work items"
                        ),
                        f"templates[{template_key}].plans",
                    )

        resolved_templates.append(
            ResolvedTemplatePlans(
                template_key=template_key,
                plans=tuple(resolved_plans),
                skipped=False,
            )
        )

    provisional = ResolvedBatch(
        batch_seed=request.batch_seed,
        templates=tuple(resolved_templates),
        config_hash="",
    )
    return ResolvedBatch(
        batch_seed=provisional.batch_seed,
        templates=provisional.templates,
        config_hash=config_hash(
            {
                "batch_seed": provisional.batch_seed,
                "templates": provisional.templates,
            }
        ),
    )
