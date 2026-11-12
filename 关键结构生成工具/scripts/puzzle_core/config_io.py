"""Strict JSON configuration boundary for headless batch generation.

The wire format intentionally separates planning fields from search fields:
``planning`` normalizes steps and per-template inheritance, while this module
keeps the matching generation parameters attached to every resolved plan.
Unknown fields and ambiguous coercions are rejected before any work item can be
created.
"""

from __future__ import annotations

import itertools
import json
import random
import secrets
import unicodedata
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

from .models import (
    ExpansionPolicy,
    Layout,
    PlanningError,
    RawBatchRequest,
    RawOutputPlan,
    RawPuzzleUnit,
    ResolvedBatch,
    ResolvedOutputPlan,
    SerialDifficultyPolicy,
    StepKind,
    StepSpec,
    TemplatePlanMode,
    TemplatePlanRule,
    TowerMode,
)
from .limits import (
    MAX_BATCH_PLANS,
    MAX_BATCH_TEMPLATES,
    MAX_LOCAL_PLANS_PER_TEMPLATE,
    MAX_PLAN_DEFINITIONS,
    MAX_RESOLVED_WORK_ITEMS,
    MAX_TEMPLATE_RULES,
)
from .planning import canonical_json, config_hash, derive_seed, resolve_batch
from .search import (
    EXPERIENCE_ARCHETYPES,
    LONG_RANGE_WAVE_COUNTS,
    MAJORITY_A_ON_TIE,
    ONLINE_STYLE_REFERENCES,
    PREFER_A_ELSE_B,
    SPECIAL_PRESETS,
    GenerationOptions,
    QualityOptions,
    UnitGenerationOptions,
)
from .solver import SolverLimits


SCHEMA_VERSION = 1
MAX_ATTEMPT_BUDGET = 100_000
_STRUCTURE_TYPES = tuple(chr(value) for value in range(ord("C"), ord("N") + 1))


class ConfigError(ValueError):
    """Stable, machine-readable error raised at the JSON boundary."""

    def __init__(self, code: str, message: str, path: str = "$") -> None:
        self.code = code
        self.message = message
        self.path = path
        super().__init__(f"{code} at {path}: {message}")

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "path": self.path}


@dataclass(frozen=True)
class IntegerChoice:
    """An exact integer or a deterministic choice from an inclusive range."""

    kind: str = "exact"
    value: Optional[int] = 3
    minimum: Optional[int] = None
    maximum: Optional[int] = None

    def domain(self, path: str) -> Tuple[int, ...]:
        if self.kind == "exact":
            if self.value is None:
                raise ConfigError("missing_integer_value", "exact requires value", path)
            return (self.value,)
        if self.minimum is None or self.maximum is None:
            raise ConfigError(
                "missing_integer_range", "range/random requires minimum and maximum", path
            )
        return tuple(range(self.minimum, self.maximum + 1))


@dataclass(frozen=True)
class UnitGenerationSpec:
    type_count: IntegerChoice = field(default_factory=IntegerChoice)
    groups_per_type: Tuple[int, ...] = ()
    uniform_groups_per_type: Optional[int] = field(
        default=None,
        metadata={"canonical_omit_if_none": True},
    )
    threshold_type: Optional[str] = None
    allowed_types: Optional[Tuple[str, ...]] = None
    count_limit: Optional[int] = None
    max_layer: Optional[int] = None
    required_pre_gate_recycles: Optional[int] = None
    first_recycle_lead_tiles: Optional[int] = None
    preferred_coords: Tuple[Tuple[int, int, int], ...] = ()


@dataclass(frozen=True)
class PlanGenerationSpec:
    automatic_group_max_difference: int = field(default=3, metadata={"canonical_omit_if_default": True})
    replacement_sources: Tuple[str, ...] = (MAJORITY_A_ON_TIE,)
    allowed_types: Tuple[str, ...] = _STRUCTURE_TYPES
    total_count_limit: Optional[int] = None
    total_count_minimum: int = field(default=0, metadata={"canonical_omit_if_default": True})
    total_percent_minimum: Optional[float] = field(default=None, metadata={"canonical_omit_if_none": True})
    total_percent_limit: Optional[float] = None
    max_layer: Optional[int] = None
    max_layer_percent: Optional[float] = None
    max_structure_layer_span: Optional[int] = None
    min_same_type_layer_span: int = field(default=0, metadata={"canonical_omit_if_default": True})
    required_pre_gate_recycles: int = 0
    first_recycle_lead_tiles: int = 2
    quality: QualityOptions = field(default_factory=QualityOptions)
    precision_mode: bool = field(
        default=False, metadata={"canonical_omit_if_default": True}
    )
    candidate_count: int = 6
    attempt_budget: int = 2000
    assignment_attempts: int = 180
    solver_limits: SolverLimits = field(default_factory=SolverLimits)
    unit_defaults: UnitGenerationSpec = field(default_factory=UnitGenerationSpec)


@dataclass(frozen=True)
class PlanParameterBinding:
    plan_id: str
    generation: PlanGenerationSpec
    unit_specs: Tuple[UnitGenerationSpec, ...]
    tower_spec: Optional[UnitGenerationSpec]
    parameter_source: str


@dataclass(frozen=True)
class ParsedBatchConfig:
    request: RawBatchRequest
    batch_bindings: Tuple[PlanParameterBinding, ...]
    template_bindings: Tuple[Tuple[str, Tuple[PlanParameterBinding, ...]], ...]
    config_hash: str
    batch_seed_source: str = "provided"
    normalized_config_json: str = ""


@dataclass(frozen=True)
class GenerationWorkItem:
    template_key: str
    plan: ResolvedOutputPlan
    generation_options: GenerationOptions
    parameter_source: str
    parameter_binding: Optional[PlanParameterBinding] = None
    parameter_config_hash: str = ""
    uses_independent_parameters: Optional[bool] = None
    configured_parameters: Optional[Mapping[str, Any]] = None
    uniform_parameters: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class ResolvedBatchConfig:
    batch: ResolvedBatch
    work_items: Tuple[GenerationWorkItem, ...]
    source_config_hash: str
    batch_seed_source: str = "provided"
    normalized_config_json: str = ""


def _object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError("expected_object", "expected a JSON object", path)
    invalid_keys = [key for key in value if not isinstance(key, str)]
    if invalid_keys:
        raise ConfigError("invalid_object_key", "JSON object keys must be text", path)
    return value


def _array(value: Any, path: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ConfigError("expected_array", "expected a JSON array", path)
    return value


def _bounded_array(value: Any, path: str, *, maximum: int) -> Sequence[Any]:
    result = _array(value, path)
    if len(result) > maximum:
        raise ConfigError(
            "array_too_large",
            f"array may contain at most {maximum} items",
            path,
        )
    return result


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(
            "unknown_field", f"unknown field {unknown[0]!r}", f"{path}.{unknown[0]}"
        )


def _text(value: Any, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str):
        raise ConfigError("expected_string", "expected text", path)
    result = unicodedata.normalize("NFC", value).strip()
    if nonempty and not result:
        raise ConfigError("empty_string", "value cannot be empty", path)
    return result


def _integer(
    value: Any,
    path: str,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError("expected_integer", "expected an integer", path)
    if minimum is not None and value < minimum:
        raise ConfigError("integer_too_small", f"must be at least {minimum}", path)
    if maximum is not None and value > maximum:
        raise ConfigError("integer_too_large", f"must be at most {maximum}", path)
    return value


def _number(
    value: Any,
    path: str,
    *,
    exclusive_minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("expected_number", "expected a number", path)
    result = float(value)
    if result != result or result in (float("inf"), float("-inf")):
        raise ConfigError("non_finite_number", "number must be finite", path)
    if exclusive_minimum is not None and result <= exclusive_minimum:
        raise ConfigError(
            "number_too_small", f"must be greater than {exclusive_minimum:g}", path
        )
    if maximum is not None and result > maximum:
        raise ConfigError("number_too_large", f"must be at most {maximum:g}", path)
    return result


def _enum(value: Any, allowed: Sequence[str], path: str) -> str:
    result = _text(value, path)
    if result not in allowed:
        raise ConfigError(
            "invalid_enum", f"expected one of: {', '.join(allowed)}", path
        )
    return result


def _parse_step(value: Any, path: str) -> StepSpec:
    if isinstance(value, int) and not isinstance(value, bool):
        return StepSpec.exact(_integer(value, path, minimum=4, maximum=9))
    obj = _object(value, path)
    _reject_unknown(obj, {"kind", "value", "minimum", "maximum", "policy"}, path)
    kind = _enum(obj.get("kind", "exact"), tuple(item.value for item in StepKind), f"{path}.kind")
    policy = _enum(
        obj.get("policy", "choose_one"),
        tuple(item.value for item in ExpansionPolicy),
        f"{path}.policy",
    )
    if kind == "exact":
        if "value" not in obj:
            raise ConfigError("missing_field", "exact step requires value", f"{path}.value")
        if "minimum" in obj or "maximum" in obj:
            raise ConfigError("conflicting_fields", "exact step cannot define a range", path)
        return StepSpec(
            StepKind.EXACT,
            value=_integer(obj["value"], f"{path}.value", minimum=4, maximum=9),
            policy=ExpansionPolicy(policy),
        )
    if "value" in obj:
        raise ConfigError("conflicting_fields", f"{kind} step cannot define value", path)
    if "minimum" not in obj or "maximum" not in obj:
        raise ConfigError("missing_field", f"{kind} step requires minimum and maximum", path)
    minimum = _integer(obj["minimum"], f"{path}.minimum", minimum=4, maximum=9)
    maximum = _integer(obj["maximum"], f"{path}.maximum", minimum=4, maximum=9)
    if minimum > maximum:
        raise ConfigError("invalid_range", "minimum cannot exceed maximum", path)
    return StepSpec(
        StepKind(kind),
        minimum=minimum,
        maximum=maximum,
        policy=ExpansionPolicy(policy),
    )


def _parse_integer_choice(value: Any, path: str) -> IntegerChoice:
    if isinstance(value, int) and not isinstance(value, bool):
        return IntegerChoice(value=_integer(value, path, minimum=1, maximum=12))
    obj = _object(value, path)
    _reject_unknown(obj, {"kind", "value", "minimum", "maximum"}, path)
    kind = _enum(obj.get("kind", "exact"), ("exact", "range", "random"), f"{path}.kind")
    if kind == "exact":
        if "value" not in obj:
            raise ConfigError("missing_field", "exact type count requires value", f"{path}.value")
        if "minimum" in obj or "maximum" in obj:
            raise ConfigError("conflicting_fields", "exact type count cannot define a range", path)
        return IntegerChoice(
            kind="exact",
            value=_integer(obj["value"], f"{path}.value", minimum=1, maximum=12),
        )
    if "value" in obj:
        raise ConfigError("conflicting_fields", f"{kind} cannot define value", path)
    default_minimum, default_maximum = (3, 7) if kind == "random" else (None, None)
    if "minimum" not in obj and default_minimum is None:
        raise ConfigError("missing_field", "range requires minimum", f"{path}.minimum")
    if "maximum" not in obj and default_maximum is None:
        raise ConfigError("missing_field", "range requires maximum", f"{path}.maximum")
    minimum = _integer(obj.get("minimum", default_minimum), f"{path}.minimum", minimum=1, maximum=12)
    maximum = _integer(obj.get("maximum", default_maximum), f"{path}.maximum", minimum=1, maximum=12)
    if minimum > maximum:
        raise ConfigError("invalid_range", "minimum cannot exceed maximum", path)
    return IntegerChoice(kind=kind, value=None, minimum=minimum, maximum=maximum)


def _parse_type_list(value: Any, path: str) -> Tuple[str, ...]:
    values = _array(value, path)
    if not values:
        raise ConfigError("empty_array", "at least one structure type is required", path)
    result = tuple(_text(item, f"{path}[{index}]") for index, item in enumerate(values))
    invalid = sorted(set(result) - set(_STRUCTURE_TYPES))
    if invalid:
        raise ConfigError("invalid_structure_type", "structure types must be C-N", path)
    if len(set(result)) != len(result):
        raise ConfigError("duplicate_value", "structure types cannot repeat", path)
    return result


def _parse_unit_partial(value: Any, path: str) -> dict[str, Any]:
    obj = _object(value, path)
    allowed = {
        "type_count", "groups_per_type", "uniform_groups_per_type",
        "threshold_type", "allowed_types",
        "count_limit", "max_layer", "required_pre_gate_recycles",
        "first_recycle_lead_tiles", "preferred_coords",
    }
    _reject_unknown(obj, allowed, path)
    if "groups_per_type" in obj and "uniform_groups_per_type" in obj:
        raise ConfigError(
            "conflicting_group_modes",
            "groups_per_type and uniform_groups_per_type cannot be used together",
            path,
        )
    result: dict[str, Any] = {}
    if "type_count" in obj:
        result["type_count"] = _parse_integer_choice(obj["type_count"], f"{path}.type_count")
    if "groups_per_type" in obj:
        groups = _array(obj["groups_per_type"], f"{path}.groups_per_type")
        if not groups:
            raise ConfigError(
                "empty_array",
                "groups_per_type must contain at least one group count",
                f"{path}.groups_per_type",
            )
        result["groups_per_type"] = tuple(
            _integer(item, f"{path}.groups_per_type[{index}]", minimum=1)
            for index, item in enumerate(groups)
        )
    if "uniform_groups_per_type" in obj:
        result["uniform_groups_per_type"] = _integer(
            obj["uniform_groups_per_type"],
            f"{path}.uniform_groups_per_type",
            minimum=0,
        )
    if "threshold_type" in obj:
        threshold = _text(obj["threshold_type"], f"{path}.threshold_type")
        if threshold not in _STRUCTURE_TYPES:
            raise ConfigError("invalid_structure_type", "threshold type must be C-N", f"{path}.threshold_type")
        result["threshold_type"] = threshold
    if "allowed_types" in obj:
        result["allowed_types"] = _parse_type_list(obj["allowed_types"], f"{path}.allowed_types")
    if "count_limit" in obj:
        result["count_limit"] = _integer(obj["count_limit"], f"{path}.count_limit", minimum=0)
    if "max_layer" in obj:
        result["max_layer"] = _integer(obj["max_layer"], f"{path}.max_layer", minimum=1)
    if "required_pre_gate_recycles" in obj:
        result["required_pre_gate_recycles"] = _integer(
            obj["required_pre_gate_recycles"],
            f"{path}.required_pre_gate_recycles",
            minimum=0,
        )
    if "first_recycle_lead_tiles" in obj:
        result["first_recycle_lead_tiles"] = _integer(
            obj["first_recycle_lead_tiles"],
            f"{path}.first_recycle_lead_tiles",
            minimum=0,
        )
    if "preferred_coords" in obj:
        coords = _array(obj["preferred_coords"], f"{path}.preferred_coords")
        parsed_coords = []
        for index, coord in enumerate(coords):
            values = _array(coord, f"{path}.preferred_coords[{index}]")
            if len(values) != 3:
                raise ConfigError("invalid_coordinate", "coordinate must be [x, y, z]", f"{path}.preferred_coords[{index}]")
            parsed_coords.append(tuple(_integer(item, f"{path}.preferred_coords[{index}][{axis}]") for axis, item in enumerate(values)))
        if len(set(parsed_coords)) != len(parsed_coords):
            raise ConfigError("duplicate_value", "preferred coordinates cannot repeat", f"{path}.preferred_coords")
        result["preferred_coords"] = tuple(parsed_coords)
    return result


def _unit_from_parts(base: UnitGenerationSpec, partial: Mapping[str, Any]) -> UnitGenerationSpec:
    values = asdict(base)
    values.update(partial)
    if "groups_per_type" in partial:
        values["uniform_groups_per_type"] = None
    elif "uniform_groups_per_type" in partial:
        values["groups_per_type"] = ()
    # asdict converted the nested choice to a dict when it came from the base.
    if isinstance(values["type_count"], dict):
        values["type_count"] = IntegerChoice(**values["type_count"])
    return UnitGenerationSpec(**values)


def _parse_quality_partial(value: Any, path: str) -> dict[str, str]:
    obj = _object(value, path)
    _reject_unknown(
        obj,
        {
            "position", "symmetry", "density", "difficulty_target",
            "experience_archetype", "special_preset", "long_range_wave_count",
            "online_style_reference",
        },
        path,
    )
    result: dict[str, str] = {}
    if "position" in obj:
        result["position"] = _enum(obj["position"], ("center", "balanced", "unrestricted"), f"{path}.position")
    if "symmetry" in obj:
        result["symmetry"] = _enum(obj["symmetry"], ("strong", "medium", "off"), f"{path}.symmetry")
    if "density" in obj:
        result["density"] = _enum(obj["density"], ("compact", "medium", "dispersed"), f"{path}.density")
    if "difficulty_target" in obj:
        result["difficulty_target"] = _enum(
            obj["difficulty_target"],
            ("fidelity", "standard", "strong"),
            f"{path}.difficulty_target",
        )
    if "experience_archetype" in obj:
        result["experience_archetype"] = _enum(
            obj["experience_archetype"],
            EXPERIENCE_ARCHETYPES,
            f"{path}.experience_archetype",
        )
    if "special_preset" in obj:
        result["special_preset"] = _enum(
            obj["special_preset"], SPECIAL_PRESETS, f"{path}.special_preset"
        )
    if "long_range_wave_count" in obj:
        raw_wave_count = obj["long_range_wave_count"]
        normalized_wave_count = (
            str(raw_wave_count)
            if isinstance(raw_wave_count, int) and not isinstance(raw_wave_count, bool)
            else raw_wave_count
        )
        result["long_range_wave_count"] = _enum(
            normalized_wave_count,
            LONG_RANGE_WAVE_COUNTS,
            f"{path}.long_range_wave_count",
        )
    if "online_style_reference" in obj:
        result["online_style_reference"] = _enum(
            obj["online_style_reference"],
            ONLINE_STYLE_REFERENCES,
            f"{path}.online_style_reference",
        )
    return result


def _parse_solver_partial(value: Any, path: str) -> dict[str, int]:
    obj = _object(value, path)
    _reject_unknown(obj, {"max_states", "max_path_count"}, path)
    result: dict[str, int] = {}
    if "max_states" in obj:
        result["max_states"] = _integer(obj["max_states"], f"{path}.max_states", minimum=1)
    if "max_path_count" in obj:
        result["max_path_count"] = _integer(obj["max_path_count"], f"{path}.max_path_count", minimum=1)
    return result


_GENERATION_FIELDS = {
    "automatic_group_max_difference",
    "replacement_sources", "allowed_types", "total_count_limit", "total_percent_limit", "total_count_minimum", "total_percent_minimum",
    "max_layer", "max_layer_percent", "max_structure_layer_span", "min_same_type_layer_span", "quality",
    "required_pre_gate_recycles", "first_recycle_lead_tiles",
    "candidate_count", "attempt_budget", "precision_mode",
    "assignment_attempts", "solver_limits", "unit_defaults",
}


def _parse_generation_partial(value: Any, path: str) -> dict[str, Any]:
    obj = _object(value, path)
    _reject_unknown(obj, _GENERATION_FIELDS, path)
    result: dict[str, Any] = {}
    if "replacement_sources" in obj:
        value = obj["replacement_sources"]
        if isinstance(value, str):
            source_rule = _enum(
                value,
                (MAJORITY_A_ON_TIE, PREFER_A_ELSE_B),
                f"{path}.replacement_sources",
            )
            # Preserve the exact behavior of an explicitly saved legacy plan;
            # only requests that omit this field take the new default.
            sources = (source_rule,)
        else:
            raw = _array(value, f"{path}.replacement_sources")
            sources = tuple(
                _enum(
                    item,
                    ("A", "B"),
                    f"{path}.replacement_sources[{index}]",
                )
                for index, item in enumerate(raw)
            )
        if not sources:
            raise ConfigError("empty_array", "at least one replacement source is required", f"{path}.replacement_sources")
        if len(set(sources)) != len(sources):
            raise ConfigError("duplicate_value", "replacement sources cannot repeat", f"{path}.replacement_sources")
        result["replacement_sources"] = sources
    if "allowed_types" in obj:
        result["allowed_types"] = _parse_type_list(obj["allowed_types"], f"{path}.allowed_types")
    if "total_count_limit" in obj:
        result["total_count_limit"] = _integer(obj["total_count_limit"], f"{path}.total_count_limit", minimum=0)
    if "automatic_group_max_difference" in obj:
        result["automatic_group_max_difference"] = _integer(obj["automatic_group_max_difference"], f"{path}.automatic_group_max_difference", minimum=0)
    if "total_count_minimum" in obj:
        result["total_count_minimum"] = _integer(obj["total_count_minimum"], f"{path}.total_count_minimum", minimum=0)
    if "total_percent_minimum" in obj:
        percent = _number(obj["total_percent_minimum"], f"{path}.total_percent_minimum", maximum=100)
        if percent < 0:
            raise ConfigError("number_too_small", "结构块占比下限不能为负数", f"{path}.total_percent_minimum")
        if result.get("total_count_minimum", 0) != 0:
            raise ConfigError("conflicting_minimum_modes", "结构块数量下限与占比下限互斥", path)
        result["total_percent_minimum"] = percent
    if "total_percent_limit" in obj:
        result["total_percent_limit"] = _number(obj["total_percent_limit"], f"{path}.total_percent_limit", exclusive_minimum=0, maximum=100)
    if "max_layer" in obj:
        result["max_layer"] = _integer(obj["max_layer"], f"{path}.max_layer", minimum=1)
    if "max_layer_percent" in obj:
        result["max_layer_percent"] = _number(obj["max_layer_percent"], f"{path}.max_layer_percent", exclusive_minimum=0, maximum=100)
    if "max_structure_layer_span" in obj:
        result["max_structure_layer_span"] = _integer(
            obj["max_structure_layer_span"],
            f"{path}.max_structure_layer_span",
            minimum=0,
        )
    if "min_same_type_layer_span" in obj:
        result["min_same_type_layer_span"] = _integer(obj["min_same_type_layer_span"], f"{path}.min_same_type_layer_span", minimum=0)
    if "required_pre_gate_recycles" in obj:
        result["required_pre_gate_recycles"] = _integer(
            obj["required_pre_gate_recycles"],
            f"{path}.required_pre_gate_recycles",
            minimum=0,
        )
    if "first_recycle_lead_tiles" in obj:
        result["first_recycle_lead_tiles"] = _integer(
            obj["first_recycle_lead_tiles"],
            f"{path}.first_recycle_lead_tiles",
            minimum=0,
        )
    if "candidate_count" in obj:
        result["candidate_count"] = _integer(obj["candidate_count"], f"{path}.candidate_count", minimum=1, maximum=20)
    if "precision_mode" in obj:
        if not isinstance(obj["precision_mode"], bool):
            raise ConfigError(
                "expected_boolean", "expected a boolean", f"{path}.precision_mode"
            )
        result["precision_mode"] = obj["precision_mode"]
    if "attempt_budget" in obj:
        result["attempt_budget"] = _integer(
            obj["attempt_budget"],
            f"{path}.attempt_budget",
            minimum=1,
            maximum=MAX_ATTEMPT_BUDGET,
        )
    if "assignment_attempts" in obj:
        result["assignment_attempts"] = _integer(obj["assignment_attempts"], f"{path}.assignment_attempts", minimum=1)
    if "quality" in obj:
        result["quality"] = _parse_quality_partial(obj["quality"], f"{path}.quality")
    if "solver_limits" in obj:
        result["solver_limits"] = _parse_solver_partial(obj["solver_limits"], f"{path}.solver_limits")
    if "unit_defaults" in obj:
        result["unit_defaults"] = _parse_unit_partial(obj["unit_defaults"], f"{path}.unit_defaults")
    return result


def _merge_generation(base: PlanGenerationSpec, partial: Mapping[str, Any]) -> PlanGenerationSpec:
    values = {
        "automatic_group_max_difference": base.automatic_group_max_difference,
        "replacement_sources": base.replacement_sources,
        "allowed_types": base.allowed_types,
        "total_count_limit": base.total_count_limit,
        "total_count_minimum": base.total_count_minimum,
        "total_percent_minimum": base.total_percent_minimum,
        "total_percent_limit": base.total_percent_limit,
        "max_layer": base.max_layer,
        "max_layer_percent": base.max_layer_percent,
        "max_structure_layer_span": base.max_structure_layer_span,
        "min_same_type_layer_span": base.min_same_type_layer_span,
        "required_pre_gate_recycles": base.required_pre_gate_recycles,
        "first_recycle_lead_tiles": base.first_recycle_lead_tiles,
        "quality": base.quality,
        "precision_mode": base.precision_mode,
        "candidate_count": base.candidate_count,
        "attempt_budget": base.attempt_budget,
        "assignment_attempts": base.assignment_attempts,
        "solver_limits": base.solver_limits,
        "unit_defaults": base.unit_defaults,
    }
    for key, item in partial.items():
        if key == "quality":
            values[key] = QualityOptions(**{**asdict(base.quality), **item})
        elif key == "solver_limits":
            values[key] = SolverLimits(**{**asdict(base.solver_limits), **item})
        elif key == "unit_defaults":
            values[key] = _unit_from_parts(base.unit_defaults, item)
        else:
            values[key] = item
    if "total_percent_minimum" in partial:
        values["total_count_minimum"] = 0
    elif "total_count_minimum" in partial:
        values["total_percent_minimum"] = None
    if (values["max_structure_layer_span"] is not None
            and values["min_same_type_layer_span"] > values["max_structure_layer_span"]):
        raise ConfigError("conflicting_layer_span", "同种结构块最小间隔层不能超过结构块最大层间距", "generation.min_same_type_layer_span")
    return PlanGenerationSpec(**values)


def _parse_plan(
    value: Any,
    path: str,
    generation_base: PlanGenerationSpec,
    parameter_source: str,
) -> Tuple[RawOutputPlan, PlanParameterBinding]:
    obj = _object(value, path)
    allowed = {"plan_id", "units", "layout", "tower_mode", "serial_difficulty", "generation", "tower_generation"}
    _reject_unknown(obj, allowed, path)
    if "plan_id" not in obj:
        raise ConfigError("missing_field", "plan_id is required", f"{path}.plan_id")
    plan_id = _text(obj["plan_id"], f"{path}.plan_id")
    tower_mode_text = _enum(obj.get("tower_mode", "disallow"), tuple(item.value for item in TowerMode), f"{path}.tower_mode")
    tower_mode = TowerMode(tower_mode_text)
    layout = None
    if "layout" in obj:
        layout = Layout(_enum(obj["layout"], tuple(item.value for item in Layout), f"{path}.layout"))
    serial = SerialDifficultyPolicy(
        _enum(obj.get("serial_difficulty", "harder_low"), tuple(item.value for item in SerialDifficultyPolicy), f"{path}.serial_difficulty")
    )
    generation = generation_base
    if "generation" in obj:
        generation = _merge_generation(generation, _parse_generation_partial(obj["generation"], f"{path}.generation"))

    raw_units = []
    unit_specs = []
    units_value = obj.get("units", [])
    for index, item in enumerate(_array(units_value, f"{path}.units")):
        unit_path = f"{path}.units[{index}]"
        unit_obj = _object(item, unit_path)
        _reject_unknown(unit_obj, {"unit_id", "step", "generation"}, unit_path)
        if "step" not in unit_obj:
            raise ConfigError("missing_field", "puzzle unit step is required", f"{unit_path}.step")
        unit_id = _text(unit_obj.get("unit_id", ""), f"{unit_path}.unit_id", nonempty=False)
        raw_units.append(RawPuzzleUnit(step=_parse_step(unit_obj["step"], f"{unit_path}.step"), unit_id=unit_id))
        unit_spec = generation.unit_defaults
        if "generation" in unit_obj:
            unit_spec = _unit_from_parts(unit_spec, _parse_unit_partial(unit_obj["generation"], f"{unit_path}.generation"))
        unit_specs.append(unit_spec)

    tower_spec = None
    if "tower_generation" in obj:
        tower_spec = _unit_from_parts(generation.unit_defaults, _parse_unit_partial(obj["tower_generation"], f"{path}.tower_generation"))
    elif tower_mode is TowerMode.ONLY:
        tower_spec = generation.unit_defaults
    if tower_mode is TowerMode.DISALLOW and tower_spec is not None:
        raise ConfigError("tower_generation_disallowed", "disallow mode cannot define tower_generation", f"{path}.tower_generation")

    raw_plan = RawOutputPlan(
        plan_id=plan_id,
        units=tuple(raw_units),
        layout=layout,
        tower_mode=tower_mode,
        serial_difficulty=serial,
    )
    binding = PlanParameterBinding(
        plan_id=plan_id,
        generation=generation,
        unit_specs=tuple(unit_specs),
        tower_spec=tower_spec,
        parameter_source=parameter_source,
    )
    return raw_plan, binding


def _load_pairs(pairs: Sequence[Tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        normalized = unicodedata.normalize("NFC", key)
        if normalized in result:
            raise ConfigError("duplicate_json_key", f"duplicate key {normalized!r}", "$")
        result[normalized] = value
    return result


def loads_batch_request(
    text: str,
    *,
    batch_seed_override: Optional[int] = None,
) -> ParsedBatchConfig:
    """Parse JSON text while rejecting duplicate keys, NaN and Infinity."""

    try:
        value = json.loads(
            text,
            object_pairs_hook=_load_pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ConfigError("non_finite_number", f"{token} is not valid", "$")
            ),
        )
    except ConfigError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError("invalid_json", str(exc), "$") from exc
    return parse_batch_request(value, batch_seed_override=batch_seed_override)


def load_batch_request(
    path: Path,
    *,
    batch_seed_override: Optional[int] = None,
) -> ParsedBatchConfig:
    """Read one UTF-8 JSON request.  This function never writes beside it."""

    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError("config_read_failed", str(exc), "$") from exc
    return loads_batch_request(text, batch_seed_override=batch_seed_override)


def parse_batch_request(
    value: Any,
    *,
    batch_seed_override: Optional[int] = None,
) -> ParsedBatchConfig:
    root = _object(value, "$")
    allowed = {"schema_version", "template_keys", "batch_seed", "generation_defaults", "batch_plans", "template_rules"}
    _reject_unknown(root, allowed, "$")
    version = _integer(root.get("schema_version", SCHEMA_VERSION), "$.schema_version")
    if version != SCHEMA_VERSION:
        raise ConfigError("unsupported_schema_version", "unsupported schema version", "$.schema_version")
    if "template_keys" not in root:
        raise ConfigError("missing_field", "template_keys is required", "$.template_keys")
    template_keys = tuple(
        _text(item, f"$.template_keys[{index}]")
        for index, item in enumerate(
            _bounded_array(
                root["template_keys"],
                "$.template_keys",
                maximum=MAX_BATCH_TEMPLATES,
            )
        )
    )
    if batch_seed_override is not None:
        batch_seed_override = _integer(
            batch_seed_override,
            "batch_seed_override",
            minimum=0,
            maximum=2**64 - 1,
        )
    if "batch_seed" in root and batch_seed_override is not None:
        configured_seed = _integer(
            root["batch_seed"], "$.batch_seed", minimum=0, maximum=2**64 - 1
        )
        if configured_seed != batch_seed_override:
            raise ConfigError(
                "batch_seed_conflict",
                "batch seed override conflicts with the seed in the request",
                "batch_seed_override",
            )
    batch_seed_source = (
        "provided"
        if "batch_seed" in root
        else "override"
        if batch_seed_override is not None
        else "generated"
    )
    batch_seed = _integer(
        root["batch_seed"]
        if "batch_seed" in root
        else batch_seed_override
        if batch_seed_override is not None
        else secrets.randbits(64),
        "$.batch_seed",
        minimum=0,
        maximum=2**64 - 1,
    )

    generation_base = PlanGenerationSpec()
    if "generation_defaults" in root:
        generation_base = _merge_generation(generation_base, _parse_generation_partial(root["generation_defaults"], "$.generation_defaults"))

    if "batch_plans" not in root:
        raise ConfigError("missing_field", "batch_plans is required", "$.batch_plans")
    raw_batch_plans = []
    batch_bindings = []
    for index, item in enumerate(
        _bounded_array(
            root["batch_plans"],
            "$.batch_plans",
            maximum=MAX_BATCH_PLANS,
        )
    ):
        plan, binding = _parse_plan(item, f"$.batch_plans[{index}]", generation_base, "batch")
        raw_batch_plans.append(plan)
        batch_bindings.append(binding)

    raw_rules = []
    template_bindings = []
    for index, item in enumerate(
        _bounded_array(
            root.get("template_rules", []),
            "$.template_rules",
            maximum=MAX_TEMPLATE_RULES,
        )
    ):
        path = f"$.template_rules[{index}]"
        obj = _object(item, path)
        _reject_unknown(obj, {"template_key", "mode", "plans"}, path)
        if "template_key" not in obj:
            raise ConfigError("missing_field", "template_key is required", f"{path}.template_key")
        template_key = _text(obj["template_key"], f"{path}.template_key")
        mode_text = _enum(obj.get("mode", "use_batch"), tuple(item.value for item in TemplatePlanMode), f"{path}.mode")
        mode = TemplatePlanMode(mode_text)
        plans = []
        bindings = []
        for plan_index, plan_value in enumerate(
            _bounded_array(
                obj.get("plans", []),
                f"{path}.plans",
                maximum=MAX_LOCAL_PLANS_PER_TEMPLATE,
            )
        ):
            plan, binding = _parse_plan(plan_value, f"{path}.plans[{plan_index}]", generation_base, f"template:{mode.value}")
            plans.append(plan)
            bindings.append(binding)
        raw_rules.append(TemplatePlanRule(template_key=template_key, mode=mode, plans=tuple(plans)))
        template_bindings.append((template_key, tuple(bindings)))

    plan_definition_count = len(raw_batch_plans) + sum(
        len(rule.plans) for rule in raw_rules
    )
    if plan_definition_count > MAX_PLAN_DEFINITIONS:
        raise ConfigError(
            "plan_definition_limit_exceeded",
            f"request may define at most {MAX_PLAN_DEFINITIONS} plans in total",
            "$",
        )

    request = RawBatchRequest(
        template_keys=template_keys,
        batch_plans=tuple(raw_batch_plans),
        template_rules=tuple(raw_rules),
        batch_seed=batch_seed,
    )
    semantic_config = {
        "schema_version": SCHEMA_VERSION,
        "request": request,
        "batch_bindings": tuple(batch_bindings),
        "template_bindings": tuple(template_bindings),
    }
    normalized_config_json = canonical_json(semantic_config)
    return ParsedBatchConfig(
        request=request,
        batch_bindings=tuple(batch_bindings),
        template_bindings=tuple(template_bindings),
        config_hash=config_hash(semantic_config),
        batch_seed_source=batch_seed_source,
        normalized_config_json=normalized_config_json,
    )


def _resolve_integer_choice(
    choice: IntegerChoice,
    *,
    batch_seed: int,
    template_key: str,
    plan: ResolvedOutputPlan,
    group_key: str,
    candidate_seed: Optional[int] = None,
    candidate_index: int = 1,
    allowed_domain: Optional[Tuple[int, ...]] = None,
) -> int:
    domain = allowed_domain or choice.domain(f"{group_key}.type_count")
    if len(domain) == 1:
        return domain[0]
    if candidate_seed is None:
        # ``generation_options`` on a work item remains a deterministic plan
        # preview for API compatibility.  Formal generation always supplies a
        # source-bound candidate seed through
        # ``resolve_candidate_generation_options`` below.
        seed = derive_seed(
            "type_count_preview_v2",
            batch_seed,
            template_key,
            plan.config_hash,
            group_key,
            asdict(choice),
        )
    else:
        seed = derive_seed(
            "type_count_choice_v2",
            candidate_seed,
            candidate_index,
            batch_seed,
            template_key,
            plan.config_hash,
            group_key,
            asdict(choice),
        )
    return domain[seed % len(domain)]


def _resolve_unit_spec(
    spec: UnitGenerationSpec,
    generation: PlanGenerationSpec,
    *,
    batch_seed: int,
    template_key: str,
    plan: ResolvedOutputPlan,
    group_key: str,
    candidate_seed: Optional[int] = None,
    candidate_index: int = 1,
    minimum_tile_count: int = 0,
) -> UnitGenerationOptions:
    allowed_types = spec.allowed_types or generation.allowed_types
    if set(allowed_types) - set(generation.allowed_types):
        raise ConfigError("unit_type_outside_global_pool", "unit allowed_types must be inside plan allowed_types", group_key)
    domain = spec.type_count.domain(f"{group_key}.type_count")
    if max(domain) > len(allowed_types):
        raise ConfigError("insufficient_allowed_types", "type_count range exceeds this group's allowed_types", group_key)
    if spec.groups_per_type and spec.uniform_groups_per_type is not None:
        raise ConfigError(
            "conflicting_group_modes",
            "groups_per_type and uniform_groups_per_type cannot be used together",
            group_key,
        )
    if spec.groups_per_type and len(domain) != 1:
        raise ConfigError(
            "ambiguous_groups_for_range",
            "groups_per_type requires an exact type_count",
            group_key,
        )

    def groups_for_type_count(value: int) -> Tuple[int, ...]:
        if spec.groups_per_type:
            return spec.groups_per_type
        if spec.uniform_groups_per_type is not None:
            if spec.uniform_groups_per_type == 0:
                minimum_groups = max(value, (minimum_tile_count + 2) // 3)
                groups = [1] * value
                for offset in range(minimum_groups - value):
                    groups[offset % value] += 1
                return tuple(groups)
            return (spec.uniform_groups_per_type,) * value
        return (1,) * value

    feasible_domain = tuple(
        value
        for value in domain
        if 3 * sum(groups_for_type_count(value)) >= minimum_tile_count
    )
    if not feasible_domain:
        maximum_tile_count = max(
            3 * sum(groups_for_type_count(value))
            for value in domain
        )
        raise ConfigError(
            "insufficient_tiles_for_recycles",
            (
                f"当前结构种类/数量范围最多生成 "
                f"{maximum_tile_count} 个结构块，"
                f"本谜题的门槛与回收设置至少需要 "
                f"{minimum_tile_count} 个"
            ),
            f"{group_key}.type_count",
        )
    type_count = _resolve_integer_choice(
        spec.type_count,
        batch_seed=batch_seed,
        template_key=template_key,
        plan=plan,
        group_key=group_key,
        candidate_seed=candidate_seed,
        candidate_index=candidate_index,
        allowed_domain=feasible_domain,
    )
    if spec.groups_per_type and len(spec.groups_per_type) != type_count:
        raise ConfigError("groups_length_mismatch", "groups_per_type length must equal resolved type_count", group_key)
    if spec.threshold_type is not None and spec.threshold_type not in allowed_types:
        raise ConfigError("threshold_type_not_allowed", "threshold_type is not in allowed_types", group_key)
    resolved_groups = groups_for_type_count(type_count)
    tile_count = 3 * sum(resolved_groups)
    if spec.count_limit is not None and tile_count > spec.count_limit:
        raise ConfigError("unit_count_limit_exceeded", "unit structure count exceeds count_limit", group_key)
    return UnitGenerationOptions(
        type_count=type_count,
        groups_per_type=(
            resolved_groups
            if spec.uniform_groups_per_type is not None
            else spec.groups_per_type
        ),
        threshold_type=spec.threshold_type,
        allowed_types=allowed_types,
        count_limit=spec.count_limit,
        max_layer=spec.max_layer,
        required_pre_gate_recycles=(
            generation.required_pre_gate_recycles
            if spec.required_pre_gate_recycles is None
            else spec.required_pre_gate_recycles
        ),
        first_recycle_lead_tiles=(
            generation.first_recycle_lead_tiles
            if spec.first_recycle_lead_tiles is None
            else spec.first_recycle_lead_tiles
        ),
        preferred_coords=spec.preferred_coords,
        variable_groups_per_type=spec.uniform_groups_per_type == 0,
    )


def _has_disjoint_type_allocation(groups: Sequence[UnitGenerationOptions]) -> bool:
    """Return whether every group can receive its requested mutually exclusive types."""

    ordered = sorted(groups, key=lambda item: (len(item.allowed_types), item.type_count))

    def visit(index: int, used: frozenset[str]) -> bool:
        if index == len(ordered):
            return True
        group = ordered[index]
        available = tuple(item for item in group.allowed_types if item not in used)
        if len(available) < group.type_count:
            return False
        return any(
            visit(index + 1, used.union(choice))
            for choice in itertools.combinations(available, group.type_count)
        )

    return visit(0, frozenset())


def _options_for_binding(
    binding: PlanParameterBinding,
    *,
    batch_seed: int,
    template_key: str,
    plan: ResolvedOutputPlan,
    candidate_seed: Optional[int] = None,
    candidate_index: int = 1,
) -> GenerationOptions:
    if len(binding.unit_specs) != len(plan.units):
        raise ConfigError("unit_parameter_mismatch", "generation unit count does not match resolved plan", f"templates[{template_key}].{plan.plan_id}")
    units = tuple(
        _resolve_unit_spec(
            spec,
            binding.generation,
            batch_seed=batch_seed,
            template_key=template_key,
            plan=plan,
            group_key=f"templates[{template_key}].{plan.plan_id}.units[{index}]",
            candidate_seed=candidate_seed,
            candidate_index=candidate_index,
            minimum_tile_count=(
                plan.units[index].step
                + 3
                * (
                    binding.generation.required_pre_gate_recycles
                    if spec.required_pre_gate_recycles is None
                    else spec.required_pre_gate_recycles
                )
            ),
        )
        for index, spec in enumerate(binding.unit_specs)
    )
    tower = None
    if binding.tower_spec is not None:
        tower = _resolve_unit_spec(
            binding.tower_spec,
            binding.generation,
            batch_seed=batch_seed,
            template_key=template_key,
            plan=plan,
            group_key=f"templates[{template_key}].{plan.plan_id}.tower",
            candidate_seed=candidate_seed,
            candidate_index=candidate_index,
        )
    generation = binding.generation
    if (generation.total_percent_minimum is not None
            and generation.total_percent_limit is not None
            and generation.total_percent_minimum > generation.total_percent_limit):
        raise ConfigError("structure_count_bounds", "结构块占比下限超过占比上限", "generation.total_percent_minimum")
    if (generation.total_count_limit is not None
            and generation.total_count_minimum > generation.total_count_limit // 3 * 3):
        raise ConfigError("structure_count_bounds", "结构块数量下限超过按整组取整后的上限", "generation.total_count_minimum")
    effective_allowed_types = generation.allowed_types
    if (
        generation.quality.special_preset != "all_types_long_range"
        and generation.quality.long_range_wave_count != "auto"
    ):
        raise ConfigError(
            "special_preset_variant_scope",
            "long_range_wave_count 只适用于 all_types_long_range",
            (
                f"templates[{template_key}].{plan.plan_id}.generation.quality."
                "long_range_wave_count"
            ),
        )
    if generation.quality.special_preset == "all_types_long_range":
        if (
            len(units) != 1
            or tower is not None
            or plan.layout is not Layout.SINGLE
        ):
            raise ConfigError(
                "special_preset_plan_shape",
                "all_types_long_range 仅支持单个塔外结构单元",
                f"templates[{template_key}].{plan.plan_id}.generation.quality.special_preset",
            )
        effective_allowed_types = _STRUCTURE_TYPES
        units = (
            replace(
                units[0],
                type_count=12,
                groups_per_type=(1,) * 12,
                allowed_types=_STRUCTURE_TYPES,
                required_pre_gate_recycles=0,
            ),
        )
    elif generation.quality.special_preset == "serial_mechanism":
        if len(units) != 2 or tower is not None or plan.layout is not Layout.Z:
            raise ConfigError(
                "special_preset_plan_shape",
                "serial_mechanism 仅支持 Z 向 high→low 双单元",
                f"templates[{template_key}].{plan.plan_id}.generation.quality.special_preset",
            )
    groups = units + (() if tower is None else (tower,))
    for unit_index, (resolved_unit, resolved_options) in enumerate(
        zip(plan.units, units)
    ):
        minimum_required = (
            resolved_unit.step
            + 3 * resolved_options.required_pre_gate_recycles
        )
        if resolved_options.tile_count < minimum_required:
            raise ConfigError(
                "insufficient_tiles_for_recycles",
                (
                    f"s{resolved_unit.step} 且门槛前回收 "
                    f"{resolved_options.required_pre_gate_recycles} 次，"
                    f"至少需要 {minimum_required} 个结构块；"
                    f"当前为 {resolved_options.tile_count} 个"
                ),
                (
                    f"templates[{template_key}].{plan.plan_id}."
                    f"units[{unit_index}].required_pre_gate_recycles"
                ),
            )
        latest_possible_first_recycle = (
            resolved_unit.step
            + 2 * resolved_options.required_pre_gate_recycles
        )
        if (
            resolved_options.required_pre_gate_recycles > 0
            and 3 + resolved_options.first_recycle_lead_tiles
            > latest_possible_first_recycle
        ):
            raise ConfigError(
                "first_recycle_lead_impossible",
                (
                    f"s{resolved_unit.step} 且门槛前回收 "
                    f"{resolved_options.required_pre_gate_recycles} 次时，"
                    f"首次回收最迟需在第 "
                    f"{latest_possible_first_recycle} 次点击完成；"
                    f"当前铺垫要求会延后到第 "
                    f"{3 + resolved_options.first_recycle_lead_tiles} 次"
                ),
                (
                    f"templates[{template_key}].{plan.plan_id}."
                    f"units[{unit_index}].first_recycle_lead_tiles"
                ),
            )
    required_types = sum(item.type_count for item in groups)
    if required_types > len(effective_allowed_types):
        raise ConfigError(
            "type_budget_exceeded",
            "mutually exclusive puzzle/tower groups require more types than allowed_types",
            f"templates[{template_key}].{plan.plan_id}.generation.allowed_types",
        )
    if not _has_disjoint_type_allocation(groups):
        raise ConfigError(
            "type_pool_conflict",
            "group allowed_types cannot satisfy mutually exclusive type allocation",
            f"templates[{template_key}].{plan.plan_id}.generation.allowed_types",
        )
    return GenerationOptions(
        automatic_group_max_difference=generation.automatic_group_max_difference,
        unit_options=units,
        tower_options=tower,
        replacement_sources=generation.replacement_sources,
        allowed_types=effective_allowed_types,
        total_count_limit=generation.total_count_limit,
        total_count_minimum=generation.total_count_minimum,
        total_percent_minimum=generation.total_percent_minimum,
        total_percent_limit=generation.total_percent_limit,
        max_layer=generation.max_layer,
        max_layer_percent=generation.max_layer_percent,
        max_structure_layer_span=generation.max_structure_layer_span,
        min_same_type_layer_span=generation.min_same_type_layer_span,
        quality=generation.quality,
        precision_mode=generation.precision_mode,
        candidate_count=generation.candidate_count,
        attempt_budget=generation.attempt_budget,
        assignment_attempts=generation.assignment_attempts,
        solver_limits=generation.solver_limits,
    )


def resolve_candidate_generation_options(
    item: GenerationWorkItem,
    *,
    batch_seed: int,
    candidate_seed: int,
    candidate_index: int,
    maximum_structure_count: int | None = None,
    minimum_structure_count: int | None = None,
) -> GenerationOptions:
    """Lock all range/random generation fields for one candidate slot.

    The resolved output plan remains the same, but candidate 1 and candidate 2
    may legitimately lock a different ``type_count`` from the same configured
    range.  Passing the same source-derived candidate seed and ordinal returns
    byte-for-byte equal options.
    """

    if isinstance(candidate_index, bool) or not isinstance(candidate_index, int):
        raise ConfigError(
            "invalid_candidate_index",
            "candidate index must be a positive integer",
            "candidate_index",
        )
    if candidate_index < 1:
        raise ConfigError(
            "invalid_candidate_index",
            "candidate index must be a positive integer",
            "candidate_index",
        )
    if item.parameter_binding is None:
        # Compatibility for callers that construct GenerationWorkItem objects
        # directly with already-exact options.
        options = item.generation_options
    else:
        options = _options_for_binding(
            item.parameter_binding,
            batch_seed=batch_seed,
            template_key=item.template_key,
            plan=item.plan,
            candidate_seed=candidate_seed,
            candidate_index=candidate_index,
        )
    variable_units = [unit for unit in options.unit_options if unit.variable_groups_per_type]
    if options.tower_options is not None and options.tower_options.variable_groups_per_type:
        variable_units.append(options.tower_options)
    if not variable_units:
        return options
    if maximum_structure_count is None:
        raise ConfigError(
            "missing_structure_count_limit",
            "variable groups require the effective structure count limit",
            "maximum_structure_count",
        )
    fixed_groups = sum(
        sum(unit.normalized_groups())
        for unit in options.unit_options
        if not unit.variable_groups_per_type
    )
    if options.tower_options is not None and not options.tower_options.variable_groups_per_type:
        fixed_groups += sum(options.tower_options.normalized_groups())
    available_groups = maximum_structure_count // 3 - fixed_groups
    baseline_groups = [
        value
        for unit in variable_units
        for value in unit.normalized_groups()
    ]
    max_difference = options.automatic_group_max_difference
    balance_floor = max(baseline_groups) - max_difference
    baseline_groups = [max(value, balance_floor) for value in baseline_groups]
    minimum_groups = sum(baseline_groups)
    if available_groups < minimum_groups:
        raise ConfigError(
            "insufficient_variable_group_capacity",
            f"结构块数量上限不足以满足每类最低组数及自动分配差值不超过 {max_difference} 组的要求",
            "maximum_structure_count",
        )
    if minimum_structure_count is None:
        if options.total_percent_minimum:
            raise ConfigError("missing_structure_count_minimum", "占比下限需要模板换算后的数量下限", "minimum_structure_count")
        minimum_structure_count = options.total_count_minimum
    required_extra = max(0, (minimum_structure_count + 2) // 3 - fixed_groups - minimum_groups)
    extra_capacity = available_groups - minimum_groups
    if required_extra > extra_capacity:
        raise ConfigError("structure_count_bounds", "结构块数量下限超过有效上限", "generation.total_count_minimum")
    stride = len(baseline_groups) if max_difference == 0 else 1
    required_extra = ((required_extra + stride - 1) // stride) * stride
    if required_extra > extra_capacity:
        raise ConfigError("structure_count_bounds", "数量上下限内无法满足自动组数最大差值", "generation.automatic_group_max_difference")
    extra_count = required_extra + stride * (derive_seed(
        "variable_group_total_v1",
        batch_seed,
        item.template_key,
        item.plan.signature,
        candidate_seed,
        candidate_index,
    ) % ((extra_capacity - required_extra) // stride + 1))
    slot_count = len(baseline_groups)
    start = derive_seed(
        "variable_group_distribution_v1",
        batch_seed,
        item.template_key,
        item.plan.signature,
        candidate_seed,
        candidate_index,
    ) % slot_count
    resolved_groups: list[tuple[int, ...]] = []
    flat = list(baseline_groups)
    # Fill the least populated types first, then introduce bounded diversity.
    # Preserve the chosen total; never satisfy balance by dropping groups.
    for offset in range(extra_count):
        minimum = min(flat)
        index = next((start + offset + step) % slot_count for step in range(slot_count)
                     if flat[(start + offset + step) % slot_count] == minimum)
        flat[index] += 1
    rng = random.Random(derive_seed('variable_group_shape_balanced_v3', candidate_seed, candidate_index))
    for _ in range(rng.randrange(slot_count + 1)):
        donor, receiver = rng.randrange(slot_count), rng.randrange(slot_count)
        if donor == receiver or flat[donor] <= baseline_groups[donor]:
            continue
        proposal = list(flat)
        proposal[donor] -= 1
        proposal[receiver] += 1
        if max(proposal) - min(proposal) <= max_difference:
            flat = proposal
    cursor = 0
    for unit in variable_units:
        resolved_groups.append(tuple(flat[cursor : cursor + unit.type_count]))
        cursor += unit.type_count
    group_iterator = iter(resolved_groups)
    units = tuple(
        replace(
            unit,
            groups_per_type=next(group_iterator),
            variable_groups_per_type=False,
        )
        if unit.variable_groups_per_type
        else unit
        for unit in options.unit_options
    )
    tower = options.tower_options
    if tower is not None and tower.variable_groups_per_type:
        tower = replace(
            tower,
            groups_per_type=next(group_iterator),
            variable_groups_per_type=False,
        )
    return replace(options, unit_options=units, tower_options=tower)


def _parameter_config_snapshot(
    plan: RawOutputPlan, binding: PlanParameterBinding
) -> dict[str, Any]:
    """Identify configured choices before candidate Seed resolves ranges.

    Inheritance/source labels and plan IDs describe where a configuration lives,
    not whether its values differ. Compare merged unit settings instead of the
    redundant unit-default declarations.
    """

    generation = asdict(binding.generation)
    if generation["automatic_group_max_difference"] == 3:
        generation.pop("automatic_group_max_difference")
    generation.pop("unit_defaults")
    if generation["total_percent_minimum"] is None:
        generation.pop("total_percent_minimum")
    generation["allowed_types"] = sorted(binding.generation.allowed_types)

    def unit_settings(spec: UnitGenerationSpec) -> dict[str, Any]:
        settings = asdict(spec)
        if not spec.groups_per_type:
            settings["uniform_groups_per_type"] = (
                1 if spec.uniform_groups_per_type is None
                else spec.uniform_groups_per_type
            )
        settings["allowed_types"] = sorted(
            binding.generation.allowed_types
            if spec.allowed_types is None else spec.allowed_types
        )
        return settings

    distribution_revision = (
        {"group_distribution_version": "balanced-v3"}
        if any(not spec.groups_per_type and spec.uniform_groups_per_type == 0
               for spec in (*binding.unit_specs, *([binding.tower_spec] if binding.tower_spec else [])))
        else {}
    )
    return json.loads(canonical_json({
        **distribution_revision,
        "tower_mode": plan.tower_mode,
        "layout": Layout.SINGLE if len(plan.units) == 1 else plan.layout,
        "serial_difficulty": (
            plan.serial_difficulty if plan.layout is Layout.Z else None
        ),
        "units": [
            {"unit_id": unit.unit_id or f"U{index + 1}", "step": unit.step}
            for index, unit in enumerate(plan.units)
        ],
        "generation": generation,
        "unit_settings": [unit_settings(spec) for spec in binding.unit_specs],
        "tower_settings": (
            None if binding.tower_spec is None
            else unit_settings(binding.tower_spec)
        ),
    }))


def _parameter_snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    # Preserve the explicit UI mode in snapshots while comparing effective
    # constraints: omitted, count 0, and percent 0 all impose no lower bound.
    generation = dict(snapshot["generation"])
    # Legacy snapshots omit this constraint; zero has the same effect and
    # must preserve parameter provenance when a historical task continues.
    if generation.get("min_same_type_layer_span", 0) == 0:
        generation.pop("min_same_type_layer_span", None)
    if generation.get("total_percent_minimum") in (None, 0):
        generation.pop("total_percent_minimum", None)
    return config_hash({**snapshot, "generation": generation})


def _parameter_config_hash(plan: RawOutputPlan, binding: PlanParameterBinding) -> str:
    return _parameter_snapshot_hash(_parameter_config_snapshot(plan, binding))


def resolve_batch_config(parsed: ParsedBatchConfig) -> ResolvedBatchConfig:
    """Resolve planning inheritance and attach exact search options per plan."""

    try:
        batch = resolve_batch(parsed.request)
    except PlanningError as exc:
        path = f"$.{exc.path}" if exc.path else "$"
        raise ConfigError(exc.code, exc.message, path) from exc

    batch_by_id = {item.plan_id: item for item in parsed.batch_bindings}
    batch_parameter_hashes = {
        plan.plan_id: _parameter_config_hash(plan, batch_by_id[plan.plan_id])
        for plan in parsed.request.batch_plans
    }
    batch_parameters = {
        plan.plan_id: _parameter_config_snapshot(plan, batch_by_id[plan.plan_id])
        for plan in parsed.request.batch_plans
    }
    local_by_template = {
        key: {item.plan_id: item for item in bindings}
        for key, bindings in parsed.template_bindings
    }
    local_parameter_hashes = {
        (rule.template_key, plan.plan_id): _parameter_config_hash(
            plan, local_by_template[rule.template_key][plan.plan_id],
        )
        for rule in parsed.request.template_rules for plan in rule.plans
    }
    local_parameters = {
        (rule.template_key, plan.plan_id): _parameter_config_snapshot(
            plan, local_by_template[rule.template_key][plan.plan_id],
        )
        for rule in parsed.request.template_rules for plan in rule.plans
    }
    rules = {rule.template_key: rule.mode for rule in parsed.request.template_rules}
    work_items = []
    for template in batch.templates:
        if template.skipped:
            continue
        mode = rules.get(template.template_key, TemplatePlanMode.USE_BATCH)
        for plan in template.plans:
            if len(work_items) >= MAX_RESOLVED_WORK_ITEMS:
                raise ConfigError(
                    "work_item_limit_exceeded",
                    (
                        "expanded request exceeds the hard limit of "
                        f"{MAX_RESOLVED_WORK_ITEMS} work items"
                    ),
                    "$.batch_plans",
                )
            binding = None
            if mode in (TemplatePlanMode.REPLACE, TemplatePlanMode.APPEND):
                binding = local_by_template.get(template.template_key, {}).get(plan.source_plan_id)
            if binding is None and mode in (TemplatePlanMode.USE_BATCH, TemplatePlanMode.APPEND):
                binding = batch_by_id.get(plan.source_plan_id)
            if binding is None:
                raise ConfigError("missing_parameter_binding", "resolved plan has no generation parameter binding", f"templates[{template.template_key}].{plan.plan_id}")
            parameter_hash = (
                batch_parameter_hashes[plan.source_plan_id]
                if binding.parameter_source == "batch"
                else local_parameter_hashes[(template.template_key, plan.source_plan_id)]
            )
            parameters = (
                batch_parameters[plan.source_plan_id]
                if binding.parameter_source == "batch"
                else local_parameters[(template.template_key, plan.source_plan_id)]
            )
            uniform_parameters = batch_parameters.get(plan.source_plan_id)
            if uniform_parameters is None and len(batch_parameters) == 1:
                uniform_parameters = next(iter(batch_parameters.values()))
            if uniform_parameters is None:
                compatible = [
                    snapshot for snapshot in batch_parameters.values()
                    if snapshot["tower_mode"] == parameters["tower_mode"]
                    and len(snapshot["units"]) == len(parameters["units"])
                ]
                uniform_parameters = next(
                    (snapshot for snapshot in compatible if _parameter_snapshot_hash(snapshot) == parameter_hash),
                    compatible[0] if len(compatible) == 1 else None,
                )
            work_items.append(
                GenerationWorkItem(
                    template_key=template.template_key,
                    plan=plan,
                    generation_options=_options_for_binding(
                        binding,
                        batch_seed=batch.batch_seed,
                        template_key=template.template_key,
                        plan=plan,
                    ),
                    parameter_source=binding.parameter_source,
                    parameter_binding=binding,
                    parameter_config_hash=parameter_hash,
                    uses_independent_parameters=(
                        parameter_hash not in batch_parameter_hashes.values()
                    ),
                    configured_parameters=parameters,
                    uniform_parameters=uniform_parameters,
                )
            )
    return ResolvedBatchConfig(
        batch=batch,
        work_items=tuple(work_items),
        source_config_hash=parsed.config_hash,
        batch_seed_source=parsed.batch_seed_source,
        normalized_config_json=parsed.normalized_config_json,
    )
