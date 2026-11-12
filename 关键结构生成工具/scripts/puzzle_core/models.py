"""Domain models for headless puzzle-plan normalization.

The models deliberately contain no filesystem or UI behaviour.  Raw models may
still contain ranges or random choices; resolved models are immutable inputs to
the search/generation layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple


class StepKind(str, Enum):
    EXACT = "exact"
    RANGE = "range"
    RANDOM = "random"


class ExpansionPolicy(str, Enum):
    CHOOSE_ONE = "choose_one"
    EXPAND_EACH = "expand_each"


class Layout(str, Enum):
    SINGLE = "single"
    X = "x"
    Y = "y"
    Z = "z"


class UnitRole(str, Enum):
    SINGLE = "single"
    LEFT = "left"
    RIGHT = "right"
    TOP = "top"
    BOTTOM = "bottom"
    HIGH = "high"
    LOW = "low"


class TowerMode(str, Enum):
    DISALLOW = "disallow"
    ALLOW = "allow"
    ONLY = "only"


class TemplatePlanMode(str, Enum):
    USE_BATCH = "use_batch"
    REPLACE = "replace"
    APPEND = "append"
    SKIP = "skip"


class SerialDifficultyPolicy(str, Enum):
    HARDER_LOW = "harder_low"
    EASIER_LOW = "easier_low"
    UNRESTRICTED = "unrestricted"


class PlanningError(ValueError):
    """A stable, machine-readable planning validation error."""

    def __init__(self, code: str, message: str, path: str = "") -> None:
        self.code = code
        self.message = message
        self.path = path
        location = f" at {path}" if path else ""
        super().__init__(f"{code}{location}: {message}")


@dataclass(frozen=True)
class StepSpec:
    kind: StepKind
    value: Optional[int] = None
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    policy: ExpansionPolicy = ExpansionPolicy.CHOOSE_ONE

    @classmethod
    def exact(cls, value: int) -> "StepSpec":
        return cls(kind=StepKind.EXACT, value=value)

    @classmethod
    def range(
        cls,
        minimum: int,
        maximum: int,
        policy: ExpansionPolicy = ExpansionPolicy.CHOOSE_ONE,
    ) -> "StepSpec":
        return cls(
            kind=StepKind.RANGE,
            minimum=minimum,
            maximum=maximum,
            policy=policy,
        )

    @classmethod
    def random(
        cls,
        minimum: int = 4,
        maximum: int = 9,
        policy: ExpansionPolicy = ExpansionPolicy.CHOOSE_ONE,
    ) -> "StepSpec":
        return cls(
            kind=StepKind.RANDOM,
            minimum=minimum,
            maximum=maximum,
            policy=policy,
        )


@dataclass(frozen=True)
class RawPuzzleUnit:
    step: Optional[StepSpec]
    unit_id: str = ""


@dataclass(frozen=True)
class RawOutputPlan:
    plan_id: str
    units: Tuple[RawPuzzleUnit, ...] = field(default_factory=tuple)
    layout: Optional[Layout] = None
    tower_mode: TowerMode = TowerMode.DISALLOW
    serial_difficulty: SerialDifficultyPolicy = SerialDifficultyPolicy.HARDER_LOW


@dataclass(frozen=True)
class ResolvedPuzzleUnit:
    unit_id: str
    role: UnitRole
    step: int


@dataclass(frozen=True)
class ResolvedOutputPlan:
    plan_id: str
    source_plan_id: str
    tower_mode: TowerMode
    layout: Optional[Layout]
    serial_difficulty: SerialDifficultyPolicy
    units: Tuple[ResolvedPuzzleUnit, ...]
    signature: str
    config_hash: str


@dataclass(frozen=True)
class TemplatePlanRule:
    template_key: str
    mode: TemplatePlanMode = TemplatePlanMode.USE_BATCH
    plans: Tuple[RawOutputPlan, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RawBatchRequest:
    template_keys: Tuple[str, ...]
    batch_plans: Tuple[RawOutputPlan, ...]
    template_rules: Tuple[TemplatePlanRule, ...] = field(default_factory=tuple)
    batch_seed: int = 0


@dataclass(frozen=True)
class ResolvedTemplatePlans:
    template_key: str
    plans: Tuple[ResolvedOutputPlan, ...]
    skipped: bool = False


@dataclass(frozen=True)
class ResolvedBatch:
    batch_seed: int
    templates: Tuple[ResolvedTemplatePlans, ...]
    config_hash: str
