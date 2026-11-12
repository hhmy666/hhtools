"""Canonical template naming for resolved puzzle plans."""

from __future__ import annotations

import re
from typing import Dict, Tuple

from .models import (
    Layout,
    PlanningError,
    ResolvedOutputPlan,
    ResolvedPuzzleUnit,
    TowerMode,
    UnitRole,
)


_TEMPLATE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

_ROLE_ORDER: Dict[Layout, Tuple[UnitRole, ...]] = {
    Layout.SINGLE: (UnitRole.SINGLE,),
    Layout.X: (UnitRole.LEFT, UnitRole.RIGHT),
    Layout.Y: (UnitRole.TOP, UnitRole.BOTTOM),
    # Z is intentionally high-to-low: the lowest unit is always last.
    Layout.Z: (UnitRole.HIGH, UnitRole.LOW),
}


def ordered_units_for_signature(
    plan: ResolvedOutputPlan,
) -> Tuple[ResolvedPuzzleUnit, ...]:
    if plan.tower_mode is TowerMode.ONLY:
        if plan.units:
            raise PlanningError(
                "tower_has_units",
                "tower-only plans cannot contain PuzzleUnit entries",
                "units",
            )
        if plan.layout is not None:
            raise PlanningError(
                "tower_has_layout",
                "tower-only plans cannot contain a puzzle layout",
                "layout",
            )
        return ()

    if plan.layout is None:
        raise PlanningError(
            "missing_layout", "static plans require a resolved layout", "layout"
        )

    expected_roles = _ROLE_ORDER[plan.layout]
    by_role = {unit.role: unit for unit in plan.units}
    if len(by_role) != len(plan.units):
        raise PlanningError(
            "duplicate_unit_role", "resolved unit roles must be unique", "units"
        )
    if set(by_role) != set(expected_roles):
        expected = ", ".join(role.value for role in expected_roles)
        raise PlanningError(
            "invalid_unit_roles",
            f"layout {plan.layout.value} requires roles: {expected}",
            "units",
        )
    return tuple(by_role[role] for role in expected_roles)


def step_signature(plan: ResolvedOutputPlan) -> str:
    if plan.tower_mode is TowerMode.ONLY:
        ordered_units_for_signature(plan)
        return "tower"

    units = ordered_units_for_signature(plan)
    for index, unit in enumerate(units):
        if not 4 <= unit.step <= 9:
            raise PlanningError(
                "step_out_of_range",
                "resolved structure step must be between 4 and 9",
                f"units[{index}].step",
            )
    # Every segment is lowercase by product definition, including combinations.
    return "".join(f"s{unit.step}" for unit in units)


def naming_family(source_template_id: str, plan: ResolvedOutputPlan) -> str:
    if not source_template_id:
        raise PlanningError(
            "missing_template_id", "source template ID is required", "template_id"
        )
    if not _TEMPLATE_ID_PATTERN.fullmatch(source_template_id):
        raise PlanningError(
            "invalid_template_id",
            "template ID may contain only letters, digits, underscore, and hyphen",
            "template_id",
        )
    return f"aipuzzle{source_template_id}{step_signature(plan)}"


def versioned_template_id(
    source_template_id: str, plan: ResolvedOutputPlan, version: int = 1
) -> str:
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise PlanningError(
            "invalid_version", "version must be an integer >= 1", "version"
        )
    family = naming_family(source_template_id, plan)
    return family if version == 1 else f"{family}v{version}"


def output_filename(
    source_template_id: str, plan: ResolvedOutputPlan, version: int = 1
) -> str:
    return f"{versioned_template_id(source_template_id, plan, version)}_m.bytes"
