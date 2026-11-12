from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.models import (
    ExpansionPolicy,
    Layout,
    PlanningError,
    RawBatchRequest,
    RawOutputPlan,
    RawPuzzleUnit,
    SerialDifficultyPolicy,
    StepKind,
    StepSpec,
    TemplatePlanMode,
    TemplatePlanRule,
    TowerMode,
    UnitRole,
)
from puzzle_core.naming import (
    naming_family,
    output_filename,
    step_signature,
    versioned_template_id,
)
from puzzle_core.planning import (
    canonical_json,
    config_hash,
    derive_candidate_seed,
    derive_candidate_seed_material,
    derive_seed,
    normalize_output_plan,
    resolve_batch,
)


def exact_unit(step: int, unit_id: str = "") -> RawPuzzleUnit:
    return RawPuzzleUnit(step=StepSpec.exact(step), unit_id=unit_id)


def exact_plan(
    plan_id: str,
    *steps: int,
    layout: Layout | None = None,
    tower_mode: TowerMode = TowerMode.DISALLOW,
    serial_difficulty: SerialDifficultyPolicy = SerialDifficultyPolicy.HARDER_LOW,
) -> RawOutputPlan:
    return RawOutputPlan(
        plan_id=plan_id,
        units=tuple(exact_unit(step) for step in steps),
        layout=layout,
        tower_mode=tower_mode,
        serial_difficulty=serial_difficulty,
    )


class PlanNormalizationTests(unittest.TestCase):
    def test_exact_single_becomes_resolved_s5(self) -> None:
        (resolved,) = normalize_output_plan(
            exact_plan("base", 5), batch_seed=7, template_key="hr0029"
        )

        self.assertEqual(resolved.plan_id, "base")
        self.assertEqual(resolved.source_plan_id, "base")
        self.assertEqual(resolved.layout, Layout.SINGLE)
        self.assertEqual(resolved.signature, "s5")
        self.assertEqual(resolved.units[0].role, UnitRole.SINGLE)
        self.assertEqual(len(resolved.config_hash), 64)

    def test_candidate_seed_keeps_full_digest_and_includes_ordinal(self) -> None:
        (resolved,) = normalize_output_plan(
            exact_plan("base", 5), batch_seed=7, template_key="hr0029"
        )
        first = derive_candidate_seed_material(
            7, "a" * 64, resolved, 1, "algorithm-v1"
        )
        replay = derive_candidate_seed_material(
            7, "a" * 64, resolved, 1, "algorithm-v1"
        )
        second = derive_candidate_seed_material(
            7, "a" * 64, resolved, 2, "algorithm-v1"
        )

        self.assertEqual(first, replay)
        self.assertEqual(first.value, derive_candidate_seed(7, "a" * 64, resolved, 1, "algorithm-v1"))
        self.assertEqual(len(first.sha256), 64)
        self.assertNotEqual(first, second)

    def test_range_choose_one_is_locked_deterministically(self) -> None:
        raw = RawOutputPlan(
            plan_id="range",
            units=(RawPuzzleUnit(StepSpec.range(4, 7)),),
        )
        first = normalize_output_plan(raw, batch_seed=917, template_key="hr0029")
        second = normalize_output_plan(raw, batch_seed=917, template_key="hr0029")

        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        self.assertIn(first[0].units[0].step, range(4, 8))

    def test_random_supports_choose_one_and_expand_each(self) -> None:
        choose = RawOutputPlan(
            plan_id="random-one",
            units=(RawPuzzleUnit(StepSpec.random(7, 9)),),
        )
        (selected,) = normalize_output_plan(
            choose, batch_seed=11, template_key="source"
        )
        self.assertIn(selected.signature, {"s7", "s8", "s9"})

        expand = RawOutputPlan(
            plan_id="random-all",
            units=(
                RawPuzzleUnit(
                    StepSpec.random(
                        7, 9, policy=ExpansionPolicy.EXPAND_EACH
                    )
                ),
            ),
        )
        resolved = normalize_output_plan(
            expand, batch_seed=11, template_key="source"
        )
        self.assertEqual([plan.signature for plan in resolved], ["s7", "s8", "s9"])

    def test_range_expand_each_creates_independent_plans(self) -> None:
        raw = RawOutputPlan(
            plan_id="difficulty",
            units=(
                RawPuzzleUnit(
                    StepSpec.range(5, 6, ExpansionPolicy.EXPAND_EACH)
                ),
            ),
        )
        resolved = normalize_output_plan(raw, batch_seed=0, template_key="hr0029")

        self.assertEqual([plan.signature for plan in resolved], ["s5", "s6"])
        self.assertEqual(
            [plan.plan_id for plan in resolved],
            ["difficulty@s5", "difficulty@s6"],
        )

    def test_two_expand_each_units_form_cartesian_product(self) -> None:
        raw = RawOutputPlan(
            plan_id="combo",
            layout=Layout.X,
            units=(
                RawPuzzleUnit(
                    StepSpec.range(4, 5, ExpansionPolicy.EXPAND_EACH)
                ),
                RawPuzzleUnit(
                    StepSpec.range(6, 7, ExpansionPolicy.EXPAND_EACH)
                ),
            ),
        )
        resolved = normalize_output_plan(raw, batch_seed=0, template_key="hr0029")

        self.assertEqual(
            [plan.signature for plan in resolved],
            ["s4s6", "s4s7", "s5s6", "s5s7"],
        )
        self.assertEqual(
            [unit.role for unit in resolved[0].units],
            [UnitRole.LEFT, UnitRole.RIGHT],
        )

    def test_z_signature_is_high_then_low_even_if_tuple_is_reversed(self) -> None:
        (resolved,) = normalize_output_plan(
            exact_plan("serial", 5, 6, layout=Layout.Z),
            batch_seed=0,
            template_key="hr0029",
        )
        reversed_units = replace(resolved, units=tuple(reversed(resolved.units)))

        self.assertEqual(resolved.signature, "s5s6")
        self.assertEqual(step_signature(reversed_units), "s5s6")
        self.assertEqual(resolved.units[-1].role, UnitRole.LOW)

    def test_z_difficulty_order_is_validated(self) -> None:
        raw = exact_plan(
            "serial",
            7,
            5,
            layout=Layout.Z,
            serial_difficulty=SerialDifficultyPolicy.HARDER_LOW,
        )
        with self.assertRaises(PlanningError) as caught:
            normalize_output_plan(raw, batch_seed=0, template_key="hr0029")
        self.assertEqual(caught.exception.code, "serial_difficulty_conflict")

    def test_tower_plan_has_no_units_and_uses_tower_family(self) -> None:
        raw = RawOutputPlan(plan_id="tower", tower_mode=TowerMode.ONLY)
        (resolved,) = normalize_output_plan(
            raw, batch_seed=0, template_key="tower0001"
        )

        self.assertEqual(resolved.signature, "tower")
        self.assertEqual(naming_family("tower0001", resolved), "aipuzzletower0001tower")
        self.assertEqual(
            output_filename("tower0001", resolved),
            "aipuzzletower0001tower_m.bytes",
        )
        self.assertEqual(
            output_filename("tower0001", resolved, 2),
            "aipuzzletower0001towerv2_m.bytes",
        )

    def test_tower_plan_rejects_static_fields(self) -> None:
        raw = RawOutputPlan(
            plan_id="bad-tower",
            units=(exact_unit(5),),
            tower_mode=TowerMode.ONLY,
        )
        with self.assertRaises(PlanningError) as caught:
            normalize_output_plan(raw, batch_seed=0, template_key="tower0001")
        self.assertEqual(caught.exception.code, "tower_has_units")


class ValidationTests(unittest.TestCase):
    def assert_plan_error(self, code: str, raw: RawOutputPlan) -> None:
        with self.assertRaises(PlanningError) as caught:
            normalize_output_plan(raw, batch_seed=0, template_key="hr0029")
        self.assertEqual(caught.exception.code, code)

    def test_missing_required_fields_are_explicit(self) -> None:
        self.assert_plan_error("missing_plan_id", exact_plan("", 5))
        self.assert_plan_error(
            "missing_step", RawOutputPlan("missing-step", (RawPuzzleUnit(None),))
        )
        self.assert_plan_error(
            "missing_layout",
            exact_plan("two-without-layout", 5, 6),
        )
        self.assert_plan_error(
            "missing_step_range",
            RawOutputPlan(
                "bad-range",
                (
                    RawPuzzleUnit(
                        StepSpec(kind=StepKind.RANGE, minimum=5)
                    ),
                ),
            ),
        )

    def test_third_unit_is_rejected(self) -> None:
        self.assert_plan_error(
            "too_many_units",
            exact_plan("three", 4, 5, 6, layout=Layout.X),
        )

    def test_invalid_step_bounds_are_rejected(self) -> None:
        self.assert_plan_error("step_out_of_range", exact_plan("too-low", 3))
        self.assert_plan_error(
            "invalid_step_range",
            RawOutputPlan("reverse", (RawPuzzleUnit(StepSpec.range(7, 5)),)),
        )

    def test_duplicate_unit_id_is_rejected_after_unicode_normalization(self) -> None:
        raw = RawOutputPlan(
            plan_id="duplicate-units",
            layout=Layout.X,
            units=(
                exact_unit(5, "e\u0301"),
                exact_unit(6, "\u00e9"),
            ),
        )
        self.assert_plan_error("duplicate_unit_id", raw)


class BatchResolutionTests(unittest.TestCase):
    def test_use_batch_replace_append_and_skip(self) -> None:
        batch = exact_plan("batch-s5", 5)
        s6 = exact_plan("local-s6", 6)
        request = RawBatchRequest(
            template_keys=("use", "replace", "append", "skip"),
            batch_plans=(batch,),
            template_rules=(
                TemplatePlanRule("use", TemplatePlanMode.USE_BATCH),
                TemplatePlanRule("replace", TemplatePlanMode.REPLACE, (s6,)),
                TemplatePlanRule("append", TemplatePlanMode.APPEND, (s6,)),
                TemplatePlanRule("skip", TemplatePlanMode.SKIP),
            ),
            batch_seed=99,
        )

        resolved = resolve_batch(request)
        by_template = {item.template_key: item for item in resolved.templates}
        self.assertEqual(
            [plan.signature for plan in by_template["use"].plans], ["s5"]
        )
        self.assertEqual(
            [plan.signature for plan in by_template["replace"].plans], ["s6"]
        )
        self.assertEqual(
            [plan.signature for plan in by_template["append"].plans],
            ["s5", "s6"],
        )
        self.assertTrue(by_template["skip"].skipped)
        self.assertEqual(by_template["skip"].plans, ())

    def test_duplicate_plan_ids_are_rejected(self) -> None:
        duplicate = exact_plan("same-id", 5)
        request = RawBatchRequest(
            template_keys=("hr0029",),
            batch_plans=(duplicate, duplicate),
        )
        with self.assertRaises(PlanningError) as caught:
            resolve_batch(request)
        self.assertEqual(caught.exception.code, "duplicate_plan_id")

    def test_semantically_duplicate_plans_are_rejected(self) -> None:
        request = RawBatchRequest(
            template_keys=("hr0029",),
            batch_plans=(exact_plan("first", 5),),
            template_rules=(
                TemplatePlanRule(
                    "hr0029",
                    TemplatePlanMode.APPEND,
                    (exact_plan("second", 5),),
                ),
            ),
        )
        with self.assertRaises(PlanningError) as caught:
            resolve_batch(request)
        self.assertEqual(caught.exception.code, "duplicate_plan")

    def test_disabled_serial_field_cannot_hide_a_duplicate(self) -> None:
        first = exact_plan(
            "first", 5, serial_difficulty=SerialDifficultyPolicy.HARDER_LOW
        )
        second = exact_plan(
            "second", 5, serial_difficulty=SerialDifficultyPolicy.UNRESTRICTED
        )
        request = RawBatchRequest(
            template_keys=("hr0029",),
            batch_plans=(first, second),
        )
        with self.assertRaises(PlanningError) as caught:
            resolve_batch(request)
        self.assertEqual(caught.exception.code, "duplicate_plan")

    def test_rule_for_unknown_template_is_rejected(self) -> None:
        request = RawBatchRequest(
            template_keys=("hr0029",),
            batch_plans=(exact_plan("batch", 5),),
            template_rules=(TemplatePlanRule("missing"),),
        )
        with self.assertRaises(PlanningError) as caught:
            resolve_batch(request)
        self.assertEqual(caught.exception.code, "unknown_template_rule")


class CanonicalizationAndNamingTests(unittest.TestCase):
    def test_canonical_json_is_order_and_unicode_stable(self) -> None:
        decomposed = {"z": [2, 1], "e\u0301": {"b": 2, "a": 1}}
        composed = {"\u00e9": {"a": 1, "b": 2}, "z": [2, 1]}

        self.assertEqual(canonical_json(decomposed), canonical_json(composed))
        self.assertEqual(config_hash(decomposed), config_hash(composed))

    def test_seed_and_candidate_seed_are_stable_and_scoped(self) -> None:
        self.assertEqual(
            derive_seed("seed", {"b": 2, "a": 1}),
            derive_seed("seed", {"a": 1, "b": 2}),
        )
        (plan,) = normalize_output_plan(
            exact_plan("p", 5), batch_seed=12, template_key="hr0029"
        )
        first = derive_candidate_seed(12, "abc", plan, 0, "algorithm-v1")
        repeated = derive_candidate_seed(12, "abc", plan, 0, "algorithm-v1")
        next_candidate = derive_candidate_seed(12, "abc", plan, 1, "algorithm-v1")
        self.assertEqual(first, repeated)
        self.assertNotEqual(first, next_candidate)

    def test_static_and_combination_names_are_lowercase(self) -> None:
        (single,) = normalize_output_plan(
            exact_plan("single", 5), batch_seed=0, template_key="hr0029"
        )
        (combo,) = normalize_output_plan(
            exact_plan("combo", 5, 6, layout=Layout.Y),
            batch_seed=0,
            template_key="hr0029",
        )

        self.assertEqual(naming_family("hr0029", single), "aipuzzlehr0029s5")
        self.assertEqual(naming_family("hr0029", combo), "aipuzzlehr0029s5s6")
        self.assertEqual(
            versioned_template_id("hr0029", combo, 3),
            "aipuzzlehr0029s5s6v3",
        )
        self.assertEqual(
            output_filename("hr0029", combo, 3),
            "aipuzzlehr0029s5s6v3_m.bytes",
        )

    def test_invalid_template_id_and_version_are_rejected(self) -> None:
        (plan,) = normalize_output_plan(
            exact_plan("p", 5), batch_seed=0, template_key="hr0029"
        )
        with self.assertRaises(PlanningError) as caught:
            naming_family("../hr0029", plan)
        self.assertEqual(caught.exception.code, "invalid_template_id")
        with self.assertRaises(PlanningError) as caught:
            versioned_template_id("hr0029", plan, 0)
        self.assertEqual(caught.exception.code, "invalid_version")


if __name__ == "__main__":
    unittest.main()
