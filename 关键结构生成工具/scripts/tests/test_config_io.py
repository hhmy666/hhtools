from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.config_io import (
    ConfigError,
    load_batch_request,
    loads_batch_request,
    parse_batch_request,
    resolve_candidate_generation_options,
    resolve_batch_config,
)
from puzzle_core.models import Layout, TowerMode, UnitRole
from puzzle_core.planning import derive_candidate_seed_material
from puzzle_core.search import (
    ALGORITHM_VERSION,
    MAJORITY_A_ON_TIE,
    PREFER_A_ELSE_B,
)
from puzzle_core.limits import (
    MAX_BATCH_PLANS,
    MAX_BATCH_TEMPLATES,
    MAX_LOCAL_PLANS_PER_TEMPLATE,
    MAX_PLAN_DEFINITIONS,
    MAX_RESOLVED_WORK_ITEMS,
    MAX_TEMPLATE_RULES,
)


def plan(plan_id: str, step: int) -> dict[str, object]:
    return {"plan_id": plan_id, "units": [{"step": step}]}


def request(*templates: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "template_keys": list(templates),
        "batch_seed": 42,
        "batch_plans": [plan("default-s5", 5)],
    }


class BatchConfigTests(unittest.TestCase):
    def test_percent_minimum_roundtrip_mode_switch_and_validation(self):
        raw = request("one")
        raw["generation_defaults"] = {"total_count_minimum": 15}
        raw["batch_plans"][0]["generation"] = {"total_percent_minimum": 0}
        parsed = parse_batch_request(raw)
        item = resolve_batch_config(parsed).work_items[0]
        self.assertEqual(item.generation_options.total_count_minimum, 0)
        self.assertEqual(item.generation_options.total_percent_minimum, 0)
        restored = resolve_batch_config(loads_batch_request(json.dumps(raw))).work_items[0]
        self.assertEqual(restored.generation_options, item.generation_options)
        raw["generation_defaults"] = {"total_percent_minimum": 40}
        raw["batch_plans"][0]["generation"] = {"total_count_minimum": 0}
        none = resolve_batch_config(parse_batch_request(raw)).work_items[0].generation_options
        self.assertIsNone(none.total_percent_minimum)
        self.assertEqual(none.total_count_minimum, 0)
        raw["batch_plans"][0]["generation"] = {"total_count_minimum": 9}
        count = resolve_batch_config(parse_batch_request(raw)).work_items[0].generation_options
        self.assertIsNone(count.total_percent_minimum)
        self.assertEqual(count.total_count_minimum, 9)
        for value in (-1, 101, True, float("nan")):
            raw["generation_defaults"] = {"total_percent_minimum": value}
            with self.assertRaises(ConfigError):
                parse_batch_request(raw)
        raw["generation_defaults"] = {"total_count_minimum": 3, "total_percent_minimum": 20}
        with self.assertRaisesRegex(ConfigError, "互斥"):
            parse_batch_request(raw)
        raw["generation_defaults"] = {"total_percent_minimum": 35, "total_percent_limit": 30}
        raw["batch_plans"][0].pop("generation")
        with self.assertRaisesRegex(ConfigError, "下限超过"):
            resolve_batch_config(parse_batch_request(raw))

    def test_percent_minimum_requires_population_and_bounds_seeded_groups(self):
        raw = request("one")
        raw["generation_defaults"] = {"total_percent_minimum": 40, "unit_defaults": {"uniform_groups_per_type": 0}}
        item = resolve_batch_config(parse_batch_request(raw)).work_items[0]
        with self.assertRaisesRegex(ConfigError, "下限"):
            resolve_candidate_generation_options(item, batch_seed=42, candidate_seed=1, candidate_index=1, maximum_structure_count=18)
        for seed in range(10):
            options = resolve_candidate_generation_options(item, batch_seed=42, candidate_seed=seed, candidate_index=1, maximum_structure_count=18, minimum_structure_count=13)
            self.assertIn(options.unit_options[0].tile_count, (15, 18))
            self.assertEqual(options.total_percent_minimum, 40)
        with self.assertRaisesRegex(ConfigError, "下限"):
            resolve_candidate_generation_options(item, batch_seed=42, candidate_seed=1, candidate_index=1, maximum_structure_count=12, minimum_structure_count=13)

    def test_structure_minimum_bounds_seeded_allocation_and_preserves_old_defaults(self):
        old_request = parse_batch_request(request("one"))
        explicit_zero = request("one")
        explicit_zero["generation_defaults"] = {"total_count_minimum": 0}
        self.assertEqual(parse_batch_request(explicit_zero).config_hash, old_request.config_hash)
        legacy = resolve_batch_config(old_request).work_items[0]
        self.assertNotIn("total_percent_minimum", legacy.configured_parameters["generation"])
        self.assertEqual(legacy.generation_options.total_count_minimum, 0)
        self.assertEqual(legacy.generation_options.unit_options[0].normalized_groups(), (1, 1, 1))
        raw = request("one")
        raw["generation_defaults"] = {
            "total_count_minimum": 13,
            "unit_defaults": {"uniform_groups_per_type": 0},
        }
        config = resolve_batch_config(parse_batch_request(raw))
        for seed in range(20):
            resolved = resolve_candidate_generation_options(
                config.work_items[0], batch_seed=42, candidate_seed=seed,
                candidate_index=1, maximum_structure_count=18,
            )
            self.assertIn(resolved.unit_options[0].tile_count, (15, 18))
        with self.assertRaisesRegex(ConfigError, "下限"):
            resolve_candidate_generation_options(
                config.work_items[0], batch_seed=42, candidate_seed=1,
                candidate_index=1, maximum_structure_count=12,
            )

    def test_structure_minimum_rejects_invalid_and_inverted_bounds(self):
        for minimum in (-1, True, 1.5):
            raw = request("one")
            raw["generation_defaults"] = {"total_count_minimum": minimum}
            with self.assertRaises(ConfigError):
                parse_batch_request(raw)
        raw = request("one")
        raw["generation_defaults"] = {"total_count_minimum": 13, "total_count_limit": 14}
        with self.assertRaisesRegex(ConfigError, "下限"):
            resolve_batch_config(parse_batch_request(raw))

    def test_precision_mode_resolves_without_changing_normal_mode_identity(self) -> None:
        normal = request("hr0029")
        original = parse_batch_request(normal)
        explicit_normal = request("hr0029")
        explicit_normal["generation_defaults"] = {"precision_mode": False}
        parsed_normal = parse_batch_request(explicit_normal)
        precise = request("hr0029")
        precise["generation_defaults"] = {"precision_mode": True}
        parsed_precise = parse_batch_request(precise)

        self.assertEqual(parsed_normal.config_hash, original.config_hash)
        self.assertFalse(
            resolve_batch_config(parsed_normal).work_items[0].generation_options.precision_mode
        )
        self.assertTrue(
            resolve_batch_config(parsed_precise).work_items[0].generation_options.precision_mode
        )
        self.assertNotEqual(parsed_precise.config_hash, original.config_hash)

    def test_precision_mode_requires_json_boolean(self) -> None:
        invalid = request("hr0029")
        invalid["generation_defaults"] = {"precision_mode": "true"}

        with self.assertRaises(ConfigError):
            parse_batch_request(invalid)

    def test_experience_quality_options_and_long_range_preset_resolve(self) -> None:
        raw = request("hr0029")
        raw["generation_defaults"] = {
            "quality": {
                "experience_archetype": "board_reveal",
                "special_preset": "all_types_long_range",
                "long_range_wave_count": 3,
                "online_style_reference": "level_200_300_v1",
            }
        }
        resolved = resolve_batch_config(parse_batch_request(raw))
        options = resolved.work_items[0].generation_options
        self.assertEqual(options.quality.experience_archetype, "board_reveal")
        self.assertEqual(options.quality.special_preset, "all_types_long_range")
        self.assertEqual(options.quality.long_range_wave_count, "3")
        self.assertEqual(options.unit_options[0].type_count, 12)
        self.assertEqual(options.unit_options[0].groups_per_type, (1,) * 12)
        self.assertEqual(len(options.allowed_types), 12)

    def test_unknown_experience_quality_option_fails_closed(self) -> None:
        raw = request("hr0029")
        raw["generation_defaults"] = {
            "quality": {"experience_archetype": "mystery"}
        }
        with self.assertRaises(ConfigError):
            parse_batch_request(raw)

    def test_serial_special_preset_requires_z_high_low_plan(self) -> None:
        valid = request("hr0029")
        valid["batch_plans"] = [
            {
                "plan_id": "serial-s5s5",
                "layout": "z",
                "units": [{"step": 5}, {"step": 5}],
            }
        ]
        valid["generation_defaults"] = {
            "quality": {"special_preset": "serial_mechanism"}
        }
        resolved = resolve_batch_config(parse_batch_request(valid))
        self.assertEqual(
            resolved.work_items[0].generation_options.quality.special_preset,
            "serial_mechanism",
        )
        self.assertEqual(resolved.work_items[0].plan.layout, Layout.Z)

        invalid = request("hr0029")
        invalid["generation_defaults"] = {
            "quality": {"special_preset": "serial_mechanism"}
        }
        with self.assertRaises(ConfigError) as caught:
            resolve_batch_config(parse_batch_request(invalid))
        self.assertEqual(caught.exception.code, "special_preset_plan_shape")

    def test_long_range_wave_count_is_strict_and_scoped_to_its_preset(self) -> None:
        invalid_value = request("hr0029")
        invalid_value["generation_defaults"] = {
            "quality": {
                "special_preset": "all_types_long_range",
                "long_range_wave_count": 5,
            }
        }
        with self.assertRaises(ConfigError):
            parse_batch_request(invalid_value)

        wrong_scope = request("hr0029")
        wrong_scope["generation_defaults"] = {
            "quality": {
                "special_preset": "none",
                "long_range_wave_count": "4",
            }
        }
        with self.assertRaises(ConfigError) as caught:
            resolve_batch_config(parse_batch_request(wrong_scope))
        self.assertEqual(caught.exception.code, "special_preset_variant_scope")

    def test_attempt_budget_has_safe_local_upper_bound(self) -> None:
        valid = request("hr0029")
        valid["generation_defaults"] = {"attempt_budget": 100_000}
        resolved = resolve_batch_config(parse_batch_request(valid))
        self.assertEqual(
            resolved.work_items[0].generation_options.attempt_budget,
            100_000,
        )

        invalid = request("hr0029")
        invalid["generation_defaults"] = {"attempt_budget": 100_001}
        with self.assertRaises(ConfigError):
            parse_batch_request(invalid)

    def test_replacement_source_majority_mode_and_manual_arrays_coexist(self) -> None:
        default_resolved = resolve_batch_config(
            parse_batch_request(request("hr0029"))
        )
        self.assertEqual(
            default_resolved.work_items[0].generation_options.replacement_sources,
            (MAJORITY_A_ON_TIE,),
        )

        majority = request("hr0029")
        majority["generation_defaults"] = {
            "replacement_sources": MAJORITY_A_ON_TIE
        }
        resolved = resolve_batch_config(parse_batch_request(majority))
        self.assertEqual(
            resolved.work_items[0].generation_options.replacement_sources,
            (MAJORITY_A_ON_TIE,),
        )

        legacy_alias = request("hr0029")
        legacy_alias["generation_defaults"] = {
            "replacement_sources": PREFER_A_ELSE_B
        }
        resolved_alias = resolve_batch_config(parse_batch_request(legacy_alias))
        self.assertEqual(
            resolved_alias.work_items[0].generation_options.replacement_sources,
            (PREFER_A_ELSE_B,),
        )

        manual_both = request("hr0029")
        manual_both["generation_defaults"] = {"replacement_sources": ["A", "B"]}
        resolved_manual_both = resolve_batch_config(
            parse_batch_request(manual_both)
        )
        self.assertEqual(
            resolved_manual_both.work_items[0].generation_options.replacement_sources,
            ("A", "B"),
        )

        invalid = request("hr0029")
        invalid["generation_defaults"] = {
            "replacement_sources": [MAJORITY_A_ON_TIE]
        }
        with self.assertRaises(ConfigError):
            parse_batch_request(invalid)

    def test_uniform_plan_creates_one_work_item_per_template(self) -> None:
        resolved = resolve_batch_config(parse_batch_request(request("one", "two")))

        self.assertEqual(
            [(item.template_key, item.plan.signature) for item in resolved.work_items],
            [("one", "s5"), ("two", "s5")],
        )
        self.assertTrue(all(item.parameter_source == "batch" for item in resolved.work_items))
        self.assertTrue(all(len(item.generation_options.unit_options) == 1 for item in resolved.work_items))

    def test_one_template_can_replace_the_batch_step(self) -> None:
        raw = request("first", "second", "third")
        raw["template_rules"] = [
            {
                "template_key": "second",
                "mode": "replace",
                "plans": [plan("second-s6", 6)],
            }
        ]

        resolved = resolve_batch_config(parse_batch_request(raw))

        self.assertEqual(
            [(item.template_key, item.plan.signature) for item in resolved.work_items],
            [("first", "s5"), ("second", "s6"), ("third", "s5")],
        )
        self.assertEqual(resolved.work_items[1].parameter_source, "template:replace")

    def test_same_template_can_expand_to_separate_s5_and_s6_outputs(self) -> None:
        raw = request("hr0029")
        raw["batch_plans"] = [
            {
                "plan_id": "difficulty",
                "units": [
                    {
                        "step": {
                            "kind": "range",
                            "minimum": 5,
                            "maximum": 6,
                            "policy": "expand_each",
                        }
                    }
                ],
            }
        ]

        resolved = resolve_batch_config(parse_batch_request(raw))

        self.assertEqual([item.plan.signature for item in resolved.work_items], ["s5", "s6"])
        self.assertEqual(
            [item.plan.plan_id for item in resolved.work_items],
            ["difficulty@s5", "difficulty@s6"],
        )

    def test_append_produces_batch_and_local_outputs(self) -> None:
        raw = request("hr0029")
        raw["template_rules"] = [
            {
                "template_key": "hr0029",
                "mode": "append",
                "plans": [plan("extra-s6", 6)],
            }
        ]

        resolved = resolve_batch_config(parse_batch_request(raw))

        self.assertEqual([item.plan.signature for item in resolved.work_items], ["s5", "s6"])
        self.assertEqual(
            [item.parameter_source for item in resolved.work_items],
            ["batch", "template:append"],
        )

    def test_two_units_keep_role_order_and_separate_generation(self) -> None:
        raw = request("xyz009")
        raw["generation_defaults"] = {"allowed_types": ["C", "D", "E", "F", "G", "H"]}
        raw["batch_plans"] = [
            {
                "plan_id": "left-right",
                "layout": "x",
                "units": [
                    {"unit_id": "left", "step": 5, "generation": {"allowed_types": ["C", "D", "E"]}},
                    {"unit_id": "right", "step": 6, "generation": {"allowed_types": ["F", "G", "H"]}},
                ],
            }
        ]

        (item,) = resolve_batch_config(parse_batch_request(raw)).work_items

        self.assertEqual(item.plan.layout, Layout.X)
        self.assertEqual(item.plan.signature, "s5s6")
        self.assertEqual(tuple(unit.role for unit in item.plan.units), (UnitRole.LEFT, UnitRole.RIGHT))
        self.assertEqual(item.generation_options.unit_options[0].allowed_types, ("C", "D", "E"))
        self.assertEqual(item.generation_options.unit_options[1].allowed_types, ("F", "G", "H"))

    def test_tower_only_has_no_step_and_has_tower_generation(self) -> None:
        raw = request("tower0001")
        raw["batch_plans"] = [
            {
                "plan_id": "tower",
                "tower_mode": "only",
                "tower_generation": {
                    "type_count": 3,
                    "groups_per_type": [1, 1, 2],
                },
            }
        ]

        (item,) = resolve_batch_config(parse_batch_request(raw)).work_items

        self.assertEqual(item.plan.tower_mode, TowerMode.ONLY)
        self.assertEqual(item.plan.signature, "tower")
        self.assertEqual(item.generation_options.unit_options, ())
        self.assertEqual(item.generation_options.tower_options.type_count, 3)
        self.assertEqual(item.generation_options.tower_options.tile_count, 12)

    def test_generation_range_is_locked_and_directly_consumable(self) -> None:
        raw = request("hr0029")
        raw["generation_defaults"] = {
            "allowed_types": ["C", "D", "E", "F", "G"],
            "replacement_sources": ["A"],
            "max_layer": 5,
            "max_layer_percent": 50,
            "total_count_limit": 30,
            "total_percent_limit": 25,
            "candidate_count": 4,
            "quality": {"density": "dispersed"},
            "unit_defaults": {
                "type_count": {"kind": "range", "minimum": 3, "maximum": 5}
            },
        }
        first = resolve_batch_config(parse_batch_request(raw))
        second = resolve_batch_config(parse_batch_request(raw))

        options = first.work_items[0].generation_options
        self.assertEqual(first.work_items[0].generation_options, second.work_items[0].generation_options)
        self.assertIn(options.unit_options[0].type_count, (3, 4, 5))
        self.assertEqual(options.replacement_sources, ("A",))
        self.assertEqual(options.max_layer, 5)
        self.assertEqual(options.max_layer_percent, 50.0)
        self.assertEqual(options.total_count_limit, 30)
        self.assertEqual(options.total_percent_limit, 25.0)
        self.assertEqual(options.candidate_count, 4)
        self.assertEqual(options.quality.density, "dispersed")

    def test_uniform_groups_per_type_expands_for_exact_type_count(self) -> None:
        raw = request("hr0029")
        raw["generation_defaults"] = {
            "unit_defaults": {
                "type_count": 3,
                "uniform_groups_per_type": 2,
            }
        }

        (item,) = resolve_batch_config(parse_batch_request(raw)).work_items
        unit = item.generation_options.unit_options[0]

        self.assertEqual(unit.type_count, 3)
        self.assertEqual(unit.groups_per_type, (2, 2, 2))
        self.assertEqual(unit.tile_count, 18)

    def test_uniform_groups_per_type_expands_after_range_lock(self) -> None:
        raw = request("hr0029")
        raw["generation_defaults"] = {
            "allowed_types": ["C", "D", "E", "F", "G"],
            "unit_defaults": {
                "type_count": {"kind": "range", "minimum": 3, "maximum": 5},
                "uniform_groups_per_type": 2,
            },
        }
        config = resolve_batch_config(parse_batch_request(raw))
        item = config.work_items[0]

        for candidate_index in range(1, 5):
            resolved = resolve_candidate_generation_options(
                item,
                batch_seed=config.batch.batch_seed,
                candidate_seed=1000 + candidate_index,
                candidate_index=candidate_index,
            )
            unit = resolved.unit_options[0]
            self.assertIn(unit.type_count, (3, 4, 5))
            self.assertEqual(unit.groups_per_type, (2,) * unit.type_count)
            self.assertEqual(unit.tile_count, 6 * unit.type_count)

    def test_omitted_uniform_groups_preserves_current_config_hash(self) -> None:
        parsed = parse_batch_request(request("hr0029"))

        self.assertEqual(
            parsed.config_hash,
            "b8554ae9e26159cf3aaefdbabc2d50383ababdbbeeb1f54aa20235397d89f6ec",
        )

    def test_batch_request_schema_documents_uniform_group_mode(self) -> None:
        schema = json.loads(
            (SCRIPTS / "config" / "batch-request.schema.json").read_text(
                encoding="utf-8"
            )
        )
        unit_schema = schema["$defs"]["unitGeneration"]

        self.assertEqual(
            unit_schema["properties"]["uniform_groups_per_type"]["minimum"],
            0,
        )
        self.assertEqual(
            unit_schema["not"]["required"],
            ["groups_per_type", "uniform_groups_per_type"],
        )

    def test_recycle_and_structure_layer_span_options_resolve(self) -> None:
        raw = request("hr0029")
        raw["generation_defaults"] = {
            "max_structure_layer_span": 5,
            "min_same_type_layer_span": 2,
            "required_pre_gate_recycles": 1,
            "first_recycle_lead_tiles": 2,
        }

        (item,) = resolve_batch_config(parse_batch_request(raw)).work_items
        unit = item.generation_options.unit_options[0]
        self.assertEqual(unit.required_pre_gate_recycles, 1)
        self.assertEqual(unit.first_recycle_lead_tiles, 2)
        self.assertEqual(item.generation_options.max_structure_layer_span, 5)
        self.assertEqual(item.generation_options.min_same_type_layer_span, 2)

    def test_legacy_request_gets_zero_recycles_and_unlimited_layer_span(self) -> None:
        (item,) = resolve_batch_config(
            parse_batch_request(request("hr0029"))
        ).work_items
        unit = item.generation_options.unit_options[0]

        self.assertEqual(unit.required_pre_gate_recycles, 0)
        self.assertEqual(unit.first_recycle_lead_tiles, 2)
        self.assertIsNone(item.generation_options.max_structure_layer_span)
        self.assertEqual(item.generation_options.min_same_type_layer_span, 0)

    def test_same_type_span_rejects_invalid_values_and_max_conflicts(self) -> None:
        for value in (-1, True, 1.5):
            with self.subTest(value=value):
                raw = request("hr0029")
                raw["generation_defaults"] = {"min_same_type_layer_span": value}
                with self.assertRaises(ConfigError):
                    parse_batch_request(raw)
        raw = request("hr0029")
        raw["generation_defaults"] = {"min_same_type_layer_span": 2, "max_structure_layer_span": 1}
        with self.assertRaises(ConfigError) as caught:
            parse_batch_request(raw)
        self.assertEqual(caught.exception.code, "conflicting_layer_span")

    def test_recycle_precheck_reports_required_structure_count(self) -> None:
        raw = request("hr0029")
        raw["generation_defaults"] = {
            "unit_defaults": {
                "type_count": 3,
                "required_pre_gate_recycles": 2,
            }
        }

        with self.assertRaises(ConfigError) as caught:
            resolve_batch_config(parse_batch_request(raw))
        self.assertEqual(caught.exception.code, "insufficient_tiles_for_recycles")
        self.assertIn("至少需要 11 个", caught.exception.message)

    def test_recycle_precheck_filters_only_infeasible_range_values(self) -> None:
        raw = request("hr0029")
        raw["generation_defaults"] = {
            "required_pre_gate_recycles": 2,
            "unit_defaults": {
                "type_count": {"kind": "range", "minimum": 3, "maximum": 5}
            },
        }

        config = resolve_batch_config(parse_batch_request(raw))
        item = config.work_items[0]
        for candidate_index in range(1, 5):
            resolved = resolve_candidate_generation_options(
                item,
                batch_seed=config.batch.batch_seed,
                candidate_seed=1000 + candidate_index,
                candidate_index=candidate_index,
            )
            self.assertIn(resolved.unit_options[0].type_count, (4, 5))

    def test_impossible_first_recycle_lead_is_rejected_early(self) -> None:
        raw = request("hr0029")
        raw["generation_defaults"] = {
            "required_pre_gate_recycles": 1,
            "first_recycle_lead_tiles": 5,
        }

        with self.assertRaises(ConfigError) as caught:
            resolve_batch_config(parse_batch_request(raw))
        self.assertEqual(caught.exception.code, "first_recycle_lead_impossible")
        self.assertIn("延后到第 8 次", caught.exception.message)

    def test_type_count_range_is_locked_per_candidate_seed(self) -> None:
        raw = request("hr0029")
        raw["batch_seed"] = 0
        raw["generation_defaults"] = {
            "candidate_count": 4,
            "allowed_types": ["C", "D", "E", "F", "G"],
            "unit_defaults": {
                "type_count": {"kind": "range", "minimum": 3, "maximum": 5}
            },
        }
        config = resolve_batch_config(parse_batch_request(raw))
        item = config.work_items[0]

        locked = []
        for candidate_index in range(1, 5):
            material = derive_candidate_seed_material(
                config.batch.batch_seed,
                "a" * 64,
                item.plan,
                candidate_index,
                ALGORITHM_VERSION,
            )
            first = resolve_candidate_generation_options(
                item,
                batch_seed=config.batch.batch_seed,
                candidate_seed=material.value,
                candidate_index=candidate_index,
            )
            second = resolve_candidate_generation_options(
                item,
                batch_seed=config.batch.batch_seed,
                candidate_seed=material.value,
                candidate_index=candidate_index,
            )
            self.assertEqual(first, second)
            locked.append(first.unit_options[0].type_count)

        self.assertTrue(set(locked).issubset({3, 4, 5}))
        self.assertGreater(len(set(locked)), 1)

    def test_omitted_batch_seed_is_securely_generated_and_snapshotted(self) -> None:
        raw = request("hr0029")
        del raw["batch_seed"]
        with mock.patch("puzzle_core.config_io.secrets.randbits", return_value=987654321):
            parsed = parse_batch_request(raw)
        resolved = resolve_batch_config(parsed)

        self.assertEqual(parsed.request.batch_seed, 987654321)
        self.assertEqual(parsed.batch_seed_source, "generated")
        self.assertEqual(resolved.batch_seed_source, "generated")
        snapshot = json.loads(resolved.normalized_config_json)
        self.assertEqual(snapshot["request"]["batch_seed"], 987654321)

    def test_seed_override_replays_seedless_request_and_rejects_conflict(self) -> None:
        raw = request("hr0029")
        del raw["batch_seed"]
        first = resolve_batch_config(
            parse_batch_request(raw, batch_seed_override=123456789)
        )
        second = resolve_batch_config(
            parse_batch_request(raw, batch_seed_override=123456789)
        )
        self.assertEqual(first, second)
        self.assertEqual(first.batch_seed_source, "override")

        raw["batch_seed"] = 11
        with self.assertRaises(ConfigError) as caught:
            parse_batch_request(raw, batch_seed_override=12)
        self.assertEqual(caught.exception.code, "batch_seed_conflict")

    def test_skip_has_no_work_item(self) -> None:
        raw = request("one", "two")
        raw["template_rules"] = [{"template_key": "two", "mode": "skip"}]
        resolved = resolve_batch_config(parse_batch_request(raw))
        self.assertEqual([item.template_key for item in resolved.work_items], ["one"])
        self.assertTrue(resolved.batch.templates[1].skipped)


class StrictValidationTests(unittest.TestCase):
    def assert_config_error(self, code: str, raw: dict[str, object]) -> ConfigError:
        with self.assertRaises(ConfigError) as caught:
            resolve_batch_config(parse_batch_request(raw))
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_automatic_groups_explore_bounded_variations(self):
        raw = request('one')
        raw['generation_defaults'] = {'unit_defaults': {'type_count': 5, 'uniform_groups_per_type': 0}}
        item = resolve_batch_config(parse_batch_request(raw)).work_items[0]
        groups = [resolve_candidate_generation_options(item, batch_seed=42,
                    candidate_seed=seed, candidate_index=1,
                    maximum_structure_count=45, minimum_structure_count=45
                  ).unit_options[0].normalized_groups() for seed in range(40)]
        self.assertTrue(all(sum(g) == 15 and min(g) >= 1 for g in groups))
        self.assertTrue(any(max(g) - min(g) > 1 for g in groups), groups)
        self.assertTrue(all(max(g) - min(g) <= 3 for g in groups), groups)

    def test_unrestricted_distribution_revision_is_recorded_and_hashed(self):
        from puzzle_core.config_io import _parameter_snapshot_hash
        raw = request('one')
        raw['generation_defaults'] = {'unit_defaults': {'type_count': 5, 'uniform_groups_per_type': 0}}
        item = resolve_batch_config(parse_batch_request(raw)).work_items[0]
        snapshot = item.configured_parameters
        self.assertEqual(snapshot.get('group_distribution_version'), 'balanced-v3')
        legacy = {key: value for key, value in snapshot.items() if key != 'group_distribution_version'}
        self.assertNotEqual(item.parameter_config_hash, _parameter_snapshot_hash(legacy))
        repeated = resolve_batch_config(parse_batch_request(raw)).work_items[0]
        self.assertEqual(item.parameter_config_hash, repeated.parameter_config_hash)

    def test_unknown_fields_fail_closed_at_every_level(self) -> None:
        raw = request("one")
        raw["surprise"] = True
        with self.assertRaises(ConfigError) as caught:
            parse_batch_request(raw)
        self.assertEqual(caught.exception.code, "unknown_field")
        self.assertEqual(caught.exception.path, "$.surprise")

        raw = request("one")
        raw["batch_plans"][0]["units"][0]["mystery"] = 1
        with self.assertRaises(ConfigError) as caught:
            parse_batch_request(raw)
        self.assertEqual(caught.exception.path, "$.batch_plans[0].units[0].mystery")

    def test_invalid_step_and_type_ranges_are_rejected(self) -> None:
        raw = request("one")
        raw["batch_plans"][0]["units"][0]["step"] = {
            "kind": "range", "minimum": 7, "maximum": 5
        }
        with self.assertRaises(ConfigError) as caught:
            parse_batch_request(raw)
        self.assertEqual(caught.exception.code, "invalid_range")

        raw = request("one")
        raw["generation_defaults"] = {
            "unit_defaults": {
                "type_count": {"kind": "range", "minimum": 6, "maximum": 3}
            }
        }
        with self.assertRaises(ConfigError) as caught:
            parse_batch_request(raw)
        self.assertEqual(caught.exception.code, "invalid_range")

    def test_bool_is_not_accepted_as_integer(self) -> None:
        raw = request("one")
        raw["batch_seed"] = True
        with self.assertRaises(ConfigError) as caught:
            parse_batch_request(raw)
        self.assertEqual(caught.exception.code, "expected_integer")

    def test_structure_type_case_and_object_keys_are_strict(self) -> None:
        raw = request("one")
        raw["generation_defaults"] = {"allowed_types": ["c", "D", "E"]}
        with self.assertRaises(ConfigError) as caught:
            parse_batch_request(raw)
        self.assertEqual(caught.exception.code, "invalid_structure_type")

        with self.assertRaises(ConfigError) as caught:
            parse_batch_request({1: "not-json"})
        self.assertEqual(caught.exception.code, "invalid_object_key")

    def test_type_budget_and_groups_length_are_checked_after_locking(self) -> None:
        raw = request("one")
        raw["generation_defaults"] = {"allowed_types": ["C", "D", "E", "F", "G"]}
        raw["batch_plans"] = [
            {"plan_id": "two", "layout": "y", "units": [{"step": 5}, {"step": 6}]}
        ]
        caught = self.assert_config_error("type_budget_exceeded", raw)
        self.assertIn("generation.allowed_types", caught.path)

        raw = request("one")
        raw["batch_plans"][0]["units"][0]["generation"] = {
            "type_count": 4, "groups_per_type": [1, 1, 1]
        }
        self.assert_config_error("groups_length_mismatch", raw)

        raw = request("one")
        raw["batch_plans"][0]["units"][0]["generation"] = {
            "type_count": {"kind": "range", "minimum": 3, "maximum": 5},
            "groups_per_type": [1, 1, 1],
        }
        self.assert_config_error("ambiguous_groups_for_range", raw)

        raw = request("one")
        raw["generation_defaults"] = {
            "allowed_types": ["C", "D", "E", "F", "G", "H"]
        }
        raw["batch_plans"] = [
            {
                "plan_id": "overlap",
                "layout": "x",
                "units": [
                    {"step": 5, "generation": {"allowed_types": ["C", "D", "E"]}},
                    {"step": 6, "generation": {"allowed_types": ["C", "D", "E"]}},
                ],
            }
        ]
        self.assert_config_error("type_pool_conflict", raw)

    def test_uniform_and_custom_group_modes_are_mutually_exclusive(self) -> None:
        raw = request("one")
        raw["generation_defaults"] = {
            "unit_defaults": {
                "type_count": 3,
                "uniform_groups_per_type": 2,
                "groups_per_type": [2, 1, 1],
            }
        }

        with self.assertRaises(ConfigError) as caught:
            parse_batch_request(raw)

        self.assertEqual(caught.exception.code, "conflicting_group_modes")
        self.assertEqual(caught.exception.path, "$.generation_defaults.unit_defaults")

    def test_zero_uniform_groups_enables_seeded_variable_allocation(self) -> None:
        raw = request("one")
        raw["generation_defaults"] = {
            "unit_defaults": {"uniform_groups_per_type": 0}
        }
        config = resolve_batch_config(parse_batch_request(raw))
        item = config.work_items[0]

        first = resolve_candidate_generation_options(
            item,
            batch_seed=config.batch.batch_seed,
            candidate_seed=101,
            candidate_index=1,
            maximum_structure_count=18,
        )
        repeated = resolve_candidate_generation_options(
            item,
            batch_seed=config.batch.batch_seed,
            candidate_seed=101,
            candidate_index=1,
            maximum_structure_count=18,
        )
        variants = {
            resolve_candidate_generation_options(
                item,
                batch_seed=config.batch.batch_seed,
                candidate_seed=seed,
                candidate_index=index,
                maximum_structure_count=18,
            ).unit_options[0].groups_per_type
            for index, seed in enumerate(range(101, 109), start=1)
        }

        self.assertEqual(first, repeated)
        self.assertTrue(first.unit_options[0].variable_groups_per_type is False)
        self.assertTrue(all(value >= 1 for value in first.unit_options[0].groups_per_type))
        self.assertLessEqual(first.unit_options[0].tile_count, 18)
        self.assertGreater(len(variants), 1)

    def test_automatic_groups_remain_balanced_across_seeds(self) -> None:
        raw = request("one")
        raw["generation_defaults"] = {"unit_defaults": {"type_count": 5, "uniform_groups_per_type": 0}}
        config = resolve_batch_config(parse_batch_request(raw))
        for seed in range(100):
            options = resolve_candidate_generation_options(config.work_items[0],
                batch_seed=config.batch.batch_seed, candidate_seed=seed, candidate_index=1,
                minimum_structure_count=45, maximum_structure_count=60)
            groups = options.unit_options[0].groups_per_type
            self.assertLessEqual(max(groups)-min(groups), 3, (seed, groups))
            self.assertGreaterEqual(sum(groups)*3, 45)
            self.assertLessEqual(sum(groups)*3, 60)

    def test_automatic_group_difference_is_configurable_and_roundtrips(self) -> None:
        for difference in (0, 2, 3, 10):
            raw = request("one")
            raw["generation_defaults"] = {"automatic_group_max_difference": difference,
                "unit_defaults": {"type_count": 5, "uniform_groups_per_type": 0}}
            config = resolve_batch_config(loads_batch_request(json.dumps(raw)))
            item = config.work_items[0]
            self.assertEqual(item.generation_options.automatic_group_max_difference, difference)
            self.assertEqual(item.configured_parameters['generation'].get('automatic_group_max_difference',3), difference)
            for seed in range(30):
                options = resolve_candidate_generation_options(item, batch_seed=42,
                    candidate_seed=seed, candidate_index=1, minimum_structure_count=45, maximum_structure_count=60)
                groups = options.unit_options[0].groups_per_type
                self.assertLessEqual(max(groups)-min(groups), difference)
                self.assertTrue(45 <= sum(groups)*3 <= 60)
        for invalid in (-1, 1.5, True):
            raw['generation_defaults']['automatic_group_max_difference'] = invalid
            with self.assertRaises(ConfigError):
                parse_batch_request(raw)

    def test_automatic_balance_respects_two_unit_minimums(self) -> None:
        raw = request("one")
        raw["generation_defaults"] = {"unit_defaults": {"type_count": 2, "uniform_groups_per_type": 0}}
        raw["batch_plans"] = [{"plan_id":"two", "layout":"x", "units":[
            {"step":5,"generation":{"required_pre_gate_recycles":10}},
            {"step":5,"generation":{"required_pre_gate_recycles":0}}]}]
        item = resolve_batch_config(parse_batch_request(raw)).work_items[0]
        with self.assertRaises(ConfigError):
            resolve_candidate_generation_options(item, batch_seed=42, candidate_seed=1,
                candidate_index=1, minimum_structure_count=42, maximum_structure_count=42)
        options = resolve_candidate_generation_options(item, batch_seed=42, candidate_seed=1,
            candidate_index=1, minimum_structure_count=54, maximum_structure_count=54)
        groups = [g for unit in options.unit_options for g in unit.groups_per_type]
        self.assertLessEqual(max(groups)-min(groups), 3)
        self.assertEqual(sum(groups)*3, 54)

    def test_uniform_groups_per_type_rejects_boolean(self) -> None:
        raw = request("one")

        raw["generation_defaults"] = {
            "unit_defaults": {"uniform_groups_per_type": True}
        }
        with self.assertRaises(ConfigError) as caught:
            parse_batch_request(raw)
        self.assertEqual(caught.exception.code, "expected_integer")

    def test_zero_uniform_groups_preserves_recycle_minimum_before_random_expansion(self) -> None:
        raw = request("one")
        raw["generation_defaults"] = {
            "required_pre_gate_recycles": 2,
            "unit_defaults": {"type_count": 3, "uniform_groups_per_type": 0},
        }
        config = resolve_batch_config(parse_batch_request(raw))

        resolved = resolve_candidate_generation_options(
            config.work_items[0],
            batch_seed=config.batch.batch_seed,
            candidate_seed=202,
            candidate_index=1,
            maximum_structure_count=18,
        )

        self.assertGreaterEqual(sum(resolved.unit_options[0].groups_per_type), 4)
        self.assertLessEqual(resolved.unit_options[0].tile_count, 18)

    def test_explicit_custom_groups_cannot_be_empty(self) -> None:
        raw = request("one")
        raw["generation_defaults"] = {
            "unit_defaults": {"type_count": 3, "groups_per_type": []}
        }

        with self.assertRaises(ConfigError) as caught:
            parse_batch_request(raw)

        self.assertEqual(caught.exception.code, "empty_array")

    def test_unsupported_schema_version_has_stable_error(self) -> None:
        raw = request("one")
        raw["schema_version"] = 2
        with self.assertRaises(ConfigError) as caught:
            parse_batch_request(raw)
        self.assertEqual(caught.exception.code, "unsupported_schema_version")
        self.assertEqual(caught.exception.path, "$.schema_version")

    def test_duplicate_json_keys_and_nonfinite_numbers_are_rejected(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            loads_batch_request('{"schema_version":1,"schema_version":1}')
        self.assertEqual(caught.exception.code, "duplicate_json_key")

        with self.assertRaises(ConfigError) as caught:
            loads_batch_request('{"schema_version":NaN}')
        self.assertEqual(caught.exception.code, "non_finite_number")

    def test_semantically_duplicate_plans_are_rejected(self) -> None:
        raw = request("one")
        raw["batch_plans"] = [plan("first", 5), plan("second", 5)]
        self.assert_config_error("duplicate_plan", raw)

    def test_batch_resource_limits_fail_before_work_begins(self) -> None:
        raw = request(*[f"t{index}" for index in range(MAX_BATCH_TEMPLATES + 1)])
        with self.assertRaises(ConfigError) as caught:
            parse_batch_request(raw)
        self.assertEqual(caught.exception.code, "array_too_large")
        self.assertEqual(caught.exception.path, "$.template_keys")

        raw = request("one")
        raw["batch_plans"] = [
            plan(f"p{index}", 4 + index % 6)
            for index in range(MAX_BATCH_PLANS + 1)
        ]
        with self.assertRaises(ConfigError) as caught:
            parse_batch_request(raw)
        self.assertEqual(caught.exception.code, "array_too_large")
        self.assertEqual(caught.exception.path, "$.batch_plans")

        raw = request("one")
        raw["template_rules"] = [
            {"template_key": "one", "mode": "skip"}
            for index in range(MAX_TEMPLATE_RULES + 1)
        ]
        with self.assertRaises(ConfigError) as caught:
            parse_batch_request(raw)
        self.assertEqual(caught.exception.code, "array_too_large")
        self.assertEqual(caught.exception.path, "$.template_rules")

        raw = request("one")
        raw["template_rules"] = [
            {
                "template_key": "one",
                "mode": "replace",
                "plans": [
                    plan(f"local{index}", 4 + index % 6)
                    for index in range(MAX_LOCAL_PLANS_PER_TEMPLATE + 1)
                ],
            }
        ]
        with self.assertRaises(ConfigError) as caught:
            parse_batch_request(raw)
        self.assertEqual(caught.exception.code, "array_too_large")
        self.assertEqual(caught.exception.path, "$.template_rules[0].plans")

    def test_total_plan_and_expanded_work_item_limits(self) -> None:
        raw = request("one", "two", "three", "four")
        raw["batch_plans"] = [plan(f"batch{index}", 4) for index in range(1)]
        raw["template_rules"] = []
        remaining = MAX_PLAN_DEFINITIONS
        for template_key in ("one", "two", "three", "four"):
            count = min(MAX_LOCAL_PLANS_PER_TEMPLATE, remaining)
            raw["template_rules"].append(
                {
                    "template_key": template_key,
                    "mode": "replace",
                    "plans": [plan(f"{template_key}-{index}", 4) for index in range(count)],
                }
            )
            remaining -= count
        self.assertEqual(remaining, 0)
        with self.assertRaises(ConfigError) as caught:
            parse_batch_request(raw)
        self.assertEqual(caught.exception.code, "plan_definition_limit_exceeded")

        raw = request(*[f"t{index}" for index in range(29)])
        raw["batch_plans"] = [
            {
                "plan_id": "expanded",
                "layout": "x",
                "units": [
                    {
                        "step": {
                            "kind": "range",
                            "minimum": 4,
                            "maximum": 9,
                            "policy": "expand_each",
                        }
                    },
                    {
                        "step": {
                            "kind": "range",
                            "minimum": 4,
                            "maximum": 9,
                            "policy": "expand_each",
                        }
                    },
                ],
            }
        ]
        with self.assertRaises(ConfigError) as caught:
            resolve_batch_config(parse_batch_request(raw))
        self.assertEqual(caught.exception.code, "work_item_limit_exceeded")
        self.assertEqual(MAX_RESOLVED_WORK_ITEMS, 1000)


class ArtifactTests(unittest.TestCase):
    def test_schema_defaults_match_omitted_generation_defaults(self) -> None:
        schema = json.loads(
            (SCRIPTS / "config" / "batch-request.schema.json").read_text(
                encoding="utf-8"
            )
        )
        generation = schema["$defs"]["generation"]["properties"]

        self.assertEqual(generation["candidate_count"]["default"], 6)
        self.assertEqual(generation["attempt_budget"]["default"], 2000)

    def test_checked_in_schema_and_example_are_valid_json(self) -> None:
        scripts = Path(__file__).resolve().parents[1]
        schema = json.loads((scripts / "config" / "batch-request.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        parsed = load_batch_request(scripts / "examples" / "batch-request.example.json")
        resolved = resolve_batch_config(parsed)
        self.assertEqual(
            [(item.template_key, item.plan.signature) for item in resolved.work_items],
            [("hr0029", "s5"), ("xyz009", "s5s6"), ("tower0001", "tower")],
        )
        auto = load_batch_request(
            scripts / "examples" / "batch-request.auto-seed.example.json",
            batch_seed_override=123,
        )
        self.assertEqual(auto.batch_seed_source, "override")
        self.assertEqual(auto.request.batch_seed, 123)

        self.assertEqual(schema["properties"]["template_keys"]["maxItems"], MAX_BATCH_TEMPLATES)
        self.assertEqual(schema["properties"]["batch_plans"]["maxItems"], MAX_BATCH_PLANS)
        self.assertEqual(schema["properties"]["template_rules"]["maxItems"], MAX_TEMPLATE_RULES)
        self.assertEqual(
            schema["$defs"]["templateRule"]["properties"]["plans"]["maxItems"],
            MAX_LOCAL_PLANS_PER_TEMPLATE,
        )


if __name__ == "__main__":
    unittest.main()
