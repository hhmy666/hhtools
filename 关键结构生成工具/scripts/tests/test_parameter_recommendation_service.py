from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from services.parameter_recommendation_service import (  # noqa: E402
    DupParam,
    STRATEGIES,
    apply_generation_parameter_changes,
    build_generation_parameter_recommendations,
    build_generation_probe_request,
    build_parameter_recommendations,
    discover_config_sets,
    discover_unlaunched_config_sets,
    lookup_unlaunched_template_parameters,
    recommend_dup_params,
    _resolve_config_source_template_id,
    validate_ulinkconfig_root,
)
from puzzle_core.config_io import loads_batch_request, resolve_batch_config  # noqa: E402
from puzzle_core.errors import InputValidationError  # noqa: E402
from services.profile_service import Profile  # noqa: E402


def _write_table(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerow(["string"] * len(header))
        writer.writerow(["说明"] * len(header))
        writer.writerows(rows)


class ParameterRecommendationTests(unittest.TestCase):
    def test_clean_source_uses_only_an_unambiguous_config_alias(self) -> None:
        self.assertEqual(
            _resolve_config_source_template_id(
                "hhmy001clean", ["hhmy001", "unrelated"]
            ),
            ("hhmy001", "trailing_clean_alias"),
        )
        self.assertEqual(
            _resolve_config_source_template_id(
                "hhmy001clean", ["hhmy001clean", "hhmy001"]
            ),
            ("hhmy001clean", "exact"),
        )
        self.assertEqual(
            _resolve_config_source_template_id(
                "hhmy001clean", ["HHMY001", "unrelated"]
            ),
            (None, "missing"),
        )
        self.assertEqual(
            _resolve_config_source_template_id("clean", [""]),
            (None, "missing"),
        )

    @staticmethod
    def _generation_request() -> dict:
        return {
            "schema_version": 1,
            "template_keys": ["first", "second"],
            "batch_seed": 17,
            "generation_defaults": {
                "candidate_count": 4,
                "attempt_budget": 200,
                "total_count_limit": 6,
                "max_layer_percent": 50,
                "max_structure_layer_span": 5,
            },
            "batch_plans": [
                {
                    "plan_id": "single-s5",
                    "tower_mode": "disallow",
                    "units": [{"step": 5}],
                }
            ],
        }

    def test_generation_recommendation_is_scoped_and_reversible_as_a_diff(self) -> None:
        request = self._generation_request()
        cards = build_generation_parameter_recommendations(
            request,
            template_key="first",
            source_plan_id="single-s5",
            tower_mode="disallow",
            error={
                "failure_kind": "parameter_conflict",
                "evidence": {"required": 9, "effective_limit": 6},
            },
        )

        self.assertGreaterEqual(len(cards), 1)
        count_card = next(
            card for card in cards if card["field"] == "total_count_limit"
        )
        self.assertEqual(count_card["before"], 6)
        self.assertEqual(count_card["after"], 9)
        self.assertFalse(count_card["verified"])
        self.assertEqual(count_card["trial_result"]["status"], "pending")

        adjusted, differences = apply_generation_parameter_changes(
            request,
            template_key="first",
            source_plan_id="single-s5",
            changes=count_card["changes"],
        )
        self.assertEqual(differences[0]["before"], 6)
        self.assertEqual(differences[0]["after"], 9)
        self.assertEqual(len(adjusted["template_rules"]), 1)
        self.assertEqual(adjusted["template_rules"][0]["template_key"], "first")
        self.assertEqual(
            adjusted["template_rules"][0]["plans"][0]["generation"][
                "total_count_limit"
            ],
            9,
        )
        self.assertNotIn("template_rules", request)

        probe = build_generation_probe_request(
            adjusted,
            template_key="first",
            source_plan_id="single-s5",
            attempt_budget=24,
        )
        self.assertEqual(probe["template_keys"], ["first"])
        self.assertEqual(probe["batch_plans"][0]["generation"]["candidate_count"], 1)
        self.assertEqual(probe["batch_plans"][0]["generation"]["attempt_budget"], 24)

    def test_budget_recommendation_uses_rejection_reason_without_claiming_success(self) -> None:
        request = self._generation_request()
        cards = build_generation_parameter_recommendations(
            request,
            template_key="first",
            source_plan_id="single-s5",
            tower_mode="disallow",
            error={
                "failure_kind": "budget_exhausted",
                "evidence": {
                    "attempt_budget": 200,
                    "rejection_counts": {"structure_layer_span": 190},
                },
            },
        )

        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["field"], "max_structure_layer_span")
        self.assertEqual(cards[0]["before"], 5)
        self.assertEqual(cards[0]["after"], 6)
        self.assertFalse(cards[0]["verified"])
        self.assertIn("层间距", cards[0]["reason"])

    def test_insufficient_recycle_tiles_recommends_smallest_safe_reduction(self) -> None:
        request = self._generation_request()
        request["generation_defaults"].update(
            {
                "total_count_limit": 30,
                "first_recycle_lead_tiles": 2,
                "unit_defaults": {"required_pre_gate_recycles": 2},
            }
        )
        cards = build_generation_parameter_recommendations(
            request,
            template_key="first",
            source_plan_id="single-s5",
            tower_mode="disallow",
            error={
                "code": "insufficient_tiles_for_recycles",
                "message": (
                    "当前结构种类/数量范围最多生成 9 个结构块，"
                    "本谜题的门槛与回收设置至少需要 11 个"
                ),
                "path": "templates[first].single-s5.units[0].type_count",
            },
        )

        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card["field"], "required_pre_gate_recycles")
        self.assertEqual(card["before"], 2)
        self.assertEqual(card["after"], 1)
        self.assertEqual(card["unit_index"], 0)
        self.assertFalse(card["verified"])
        adjusted, differences = apply_generation_parameter_changes(
            request,
            template_key="first",
            source_plan_id="single-s5",
            changes=card["changes"],
            unit_index=card["unit_index"],
        )
        self.assertEqual(differences[0]["after"], 1)
        self.assertEqual(
            adjusted["template_rules"][0]["plans"][0]["units"][0][
                "generation"
            ][
                "required_pre_gate_recycles"
            ],
            1,
        )
        probe = build_generation_probe_request(
            adjusted,
            template_key="first",
            source_plan_id="single-s5",
        )
        resolved = resolve_batch_config(
            loads_batch_request(json.dumps(probe, ensure_ascii=False))
        )
        self.assertEqual(
            resolved.work_items[0]
            .generation_options.unit_options[0]
            .required_pre_gate_recycles,
            1,
        )

    def test_impossible_first_recycle_lead_recommends_latest_feasible_value(self) -> None:
        request = self._generation_request()
        request["generation_defaults"].update(
            {
                "total_count_limit": 30,
                "required_pre_gate_recycles": 1,
                "first_recycle_lead_tiles": 5,
            }
        )
        cards = build_generation_parameter_recommendations(
            request,
            template_key="first",
            source_plan_id="single-s5",
            tower_mode="disallow",
            error={
                "code": "first_recycle_lead_impossible",
                "message": (
                    "s5 且门槛前回收 1 次时，首次回收最迟需在第 "
                    "7 次点击完成；当前铺垫要求会延后到第 8 次"
                ),
                "path": (
                    "templates[first].single-s5.units[0]."
                    "first_recycle_lead_tiles"
                ),
            },
        )

        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card["field"], "first_recycle_lead_tiles")
        self.assertEqual(card["before"], 5)
        self.assertEqual(card["after"], 4)
        self.assertIn("回收次数与 sN 门槛不变", card["impact"])

    def test_empty_ulinkconfig_path_never_falls_back_to_working_directory(self) -> None:
        with self.assertRaises(InputValidationError):
            validate_ulinkconfig_root("")

    def test_six_strategies_keep_fidelity_default_and_rebuild_rules_explicit(self) -> None:
        self.assertEqual(
            list(STRATEGIES),
            ["fidelity", "balanced", "flat", "aggressive", "steady", "gentle"],
        )
        self.assertTrue(STRATEGIES["fidelity"]["is_default"])
        self.assertEqual(
            STRATEGIES["fidelity"]["mode"], "preserve_original_gradient"
        )
        self.assertTrue(
            all(
                strategy["mode"] == "rebuild_gradient"
                for strategy_id, strategy in STRATEGIES.items()
                if strategy_id != "fidelity"
            )
        )
        first = DupParam(10, "17", "100", 6)
        balanced = recommend_dup_params(first, "balanced")
        self.assertEqual(balanced[:5], [
            "10|17|100|6", "9|17|100|6", "9|17|100|5",
            "8|17|100|5", "8|17|100|4",
        ])
        self.assertEqual(recommend_dup_params(first, "flat"), [first.render()] * 10)
        self.assertEqual(recommend_dup_params(first, "gentle")[2], "9|17|100|6")
        self.assertEqual(recommend_dup_params(first, "gentle")[3], "9|17|100|5")
        self.assertEqual(recommend_dup_params(first, "aggressive")[4], "7|17|100|4")
        self.assertEqual(recommend_dup_params(first, "steady")[4], "8|17|100|5")

    def test_fidelity_strategy_adjusts_each_original_dup_param_independently(self) -> None:
        originals = [
            DupParam(
                a=12 - index,
                seed=str(100 + index),
                luck=str(90 - index),
                b=12 - index,
            )
            for index in range(10)
        ]

        recommendations = recommend_dup_params(
            originals,
            "fidelity",
            replaced_a_type_count=2,
            replaced_b_type_count=1,
        )

        self.assertEqual(
            recommendations,
            [
                f"{original.a - 2}|{original.seed}|{original.luck}|{original.b - 1}"
                for original in originals
            ],
        )
        self.assertEqual(recommendations[1], "9|101|89|10")
        self.assertEqual(recommendations[-1], "1|109|81|2")

    def test_fidelity_strategy_refuses_single_dup_or_negative_result(self) -> None:
        with self.assertRaisesRegex(InputValidationError, "不能仅从 DupPara1"):
            recommend_dup_params(DupParam(10, "17", "100", 6), "fidelity")

        originals = [DupParam(8, str(index), "100", 4) for index in range(10)]
        originals[6] = DupParam(1, "6", "88", 0)
        with self.assertRaisesRegex(InputValidationError, "DupPara7"):
            recommend_dup_params(
                originals,
                "fidelity",
                replaced_a_type_count=2,
                replaced_b_type_count=1,
            )

    def test_fidelity_caps_deduction_at_majority_aware_floors_without_raising_old_values(self) -> None:
        originals = [DupParam(7 - min(index, 4), str(index), "100", 4) for index in range(10)]
        majority_a = recommend_dup_params(
            originals, "fidelity", replaced_a_type_count=4,
            minimum_a=4, minimum_b=3,
        )
        self.assertEqual(majority_a[0], "4|0|100|4")
        self.assertEqual(majority_a[1], "4|1|100|4")
        self.assertEqual(majority_a[-1], "3|9|100|4")
        majority_b = recommend_dup_params(
            [DupParam(4, str(index), "88", 7) for index in range(10)],
            "fidelity", replaced_b_type_count=4,
            minimum_a=3, minimum_b=4,
        )
        self.assertEqual(majority_b[0], "4|0|88|4")

    def test_decrement_floor_does_not_raise_lower_starting_values(self) -> None:
        low = DupParam(3, "0", "100", 1)
        self.assertEqual(recommend_dup_params(low, "aggressive"), [low.render()] * 10)

        exact_floor = DupParam(4, "0", "100", 3)
        self.assertEqual(
            recommend_dup_params(exact_floor, "aggressive"),
            [exact_floor.render()] * 10,
        )

        mixed = DupParam(2, "0", "100", 6)
        recommendations = recommend_dup_params(mixed, "aggressive")
        self.assertTrue(all(value.split("|")[0] == "2" for value in recommendations))
        self.assertEqual(recommendations[-1].split("|")[3], "3")

    def test_discovery_ignores_unreadable_or_unsupported_table_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unsupported = root / "temp_encrypt_csv"
            unsupported.mkdir()
            (unsupported / "config_level.csv").write_bytes(b"\xff\xfe\x00")
            (unsupported / "config_level_list.csv").write_text(
                "WrongHeader\n", encoding="utf-8"
            )
            supported = root / "tile01_ios_config"
            supported.mkdir()
            _write_table(
                supported / "config_level.csv",
                ["LevelId", "TemplateId", *(f"DupPara{i}" for i in range(1, 11))],
                [],
            )
            _write_table(
                supported / "config_level_list.csv",
                ["LevelIndex", "LevelId"],
                [],
            )

            self.assertEqual(
                [item["id"] for item in discover_config_sets(root)],
                ["tile01_ios_config"],
            )

    def test_read_only_join_filters_range_and_uses_dda_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "tile01_ios_config"
            config.mkdir()
            dup = ["10|9|100|6"] * 10
            _write_table(
                config / "config_level.csv",
                ["LevelId", "TemplateId", "StrategyGroupId", *(f"DupPara{i}" for i in range(1, 11))],
                [["level_normal", "hr0029", "1004", *dup], ["level_dda", "hr0029", "1004", *dup]],
            )
            _write_table(
                config / "config_level_list.csv",
                ["LevelIndex", "LevelId", "DDALevelId1", "DDALevelId2", "DDALevelId3", "DDAType"],
                [
                    ["210", "level_normal", "", "", "", "0"],
                    ["220", "unused", "level_dda", "", "", "1"],
                    ["350", "level_normal", "", "", "", "0"],
                ],
            )
            profile = Profile("p", "test", None, None, str(root / "out"), str(root))
            manifest = {
                "source_template_id": "hr0029",
                "output_template_id": "aipuzzlehr0029s5",
                "replacements": [
                    {"source_tile_id": -10, "structure_type": "C"},
                    {"source_tile_id": -10, "structure_type": "C"},
                    {"source_tile_id": -10, "structure_type": "D"},
                    {"source_tile_id": -11, "structure_type": "E"},
                ],
            }

            self.assertEqual(discover_config_sets(root)[0]["id"], "tile01_ios_config")
            result = build_parameter_recommendations(
                profile,
                [manifest],
                config_set_id="tile01_ios_config",
                level_start=200,
                level_end=300,
                strategy="flat",
            )

            self.assertEqual(len(result["rows"]), 1)
            row = result["rows"][0]
            self.assertEqual(row["online_positions"], [210, 220])
            self.assertEqual(row["level_ids"], ["level_dda", "level_normal"])
            self.assertEqual(row["replaced_a_type_count"], 2)
            self.assertEqual(row["replaced_b_type_count"], 1)
            self.assertEqual(row["strategy_group_id"], "1004")
            self.assertEqual(row["recommended_dup"], ["8|9|100|5"] * 10)

    def test_read_only_join_uses_clean_alias_without_changing_manifest_source_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "csv"
            config.mkdir()
            dup = ["8|0|100|5"] * 10
            _write_table(
                config / "config_level.csv",
                ["LevelId", "TemplateId", *(f"DupPara{i}" for i in range(1, 11))],
                [["level_clean", "hhmy001", *dup]],
            )
            _write_table(
                config / "config_level_list.csv",
                ["LevelIndex", "LevelId"],
                [["202", "level_clean"]],
            )
            profile = Profile("p", "test", None, None, str(root / "out"), str(root))
            manifest = {
                "source_template_id": "hhmy001clean",
                "output_template_id": "aipuzzlehhmy001cleans5",
                "source_tile_counts": {"A": 60, "B": 45},
                "replacements": [
                    {"source_tile_id": -10, "structure_type": "C"},
                    {"source_tile_id": -10, "structure_type": "C"},
                    {"source_tile_id": -10, "structure_type": "C"},
                ],
            }

            result = build_parameter_recommendations(
                profile,
                [manifest],
                config_set_id="csv",
                level_start=200,
                level_end=300,
                strategy="fidelity",
            )

            (row,) = result["rows"]
            self.assertEqual(row["source_template_id"], "hhmy001clean")
            self.assertEqual(row["config_source_template_id"], "hhmy001")
            self.assertEqual(row["config_source_match"], "trailing_clean_alias")
            self.assertIn("历史目标按 CSV 中可用的原模板别名解析", result["warnings"][0])
            self.assertIn("首次写入使用该配置源 ID", result["warnings"][0])
            self.assertIn("manifest 原模板 ID 同时保留作审计", result["warnings"][0])

    def test_independent_calibration_lookup_does_not_filter_current_config_position(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "csv"
            config.mkdir()
            dup = ["8|0|100|5"] * 10
            _write_table(
                config / "config_level.csv",
                ["LevelId", "TemplateId", *(f"DupPara{i}" for i in range(1, 11))],
                [["moved_level", "hhmy001", *dup]],
            )
            _write_table(
                config / "config_level_list.csv",
                ["LevelIndex", "LevelId"],
                [["16161", "moved_level"]],
            )
            profile = Profile("p", "test", None, None, str(root / "out"), str(root))
            manifest = {
                "source_template_id": "hhmy001clean",
                "output_template_id": "aipuzzlehhmy001cleans5",
                "source_tile_counts": {"A": 60, "B": 45},
                "replacements": [
                    {"source_tile_id": -10, "structure_type": "C"},
                    {"source_tile_id": -10, "structure_type": "C"},
                    {"source_tile_id": -10, "structure_type": "C"},
                ],
            }

            result = build_parameter_recommendations(
                profile,
                [manifest],
                config_set_id="csv",
                level_start=200,
                level_end=300,
                strategy="fidelity",
                filter_by_level_range=False,
            )

            (row,) = result["rows"]
            self.assertEqual(row["online_positions"], [16161])
            self.assertEqual(row["config_source_template_id"], "hhmy001")
            self.assertFalse(result["level_range_filter_applied"])

    def test_read_only_join_fidelity_preserves_each_duplicate_seed_and_luck(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "tile01_ios_config"
            config.mkdir()
            dup = [
                f"{12 - index}|{90 + index}|{100 - index}|{11 - index}"
                for index in range(10)
            ]
            _write_table(
                config / "config_level.csv",
                ["LevelId", "TemplateId", *(f"DupPara{i}" for i in range(1, 11))],
                [["level_normal", "hr0029", *dup]],
            )
            _write_table(
                config / "config_level_list.csv",
                ["LevelIndex", "LevelId"],
                [["210", "level_normal"]],
            )
            profile = Profile("p", "test", None, None, str(root / "out"), str(root))
            manifest = {
                "source_template_id": "hr0029",
                "output_template_id": "aipuzzlehr0029s5",
                "replacements": [
                    {"source_tile_id": -10, "structure_type": "C"},
                    {"source_tile_id": -10, "structure_type": "D"},
                    {"source_tile_id": -11, "structure_type": "E"},
                ],
            }

            result = build_parameter_recommendations(
                profile,
                [manifest],
                config_set_id="tile01_ios_config",
                level_start=200,
                level_end=300,
                strategy="fidelity",
            )

            self.assertEqual(result["strategy"]["mode"], "preserve_original_gradient")
            self.assertEqual(
                result["rows"][0]["recommended_dup"],
                [
                    f"{10 - index}|{90 + index}|{100 - index}|{10 - index}"
                    for index in range(10)
                ],
            )

    def test_read_only_join_preserves_random_seed_group_without_cross_group_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "tile01_ios_config"
            config.mkdir()
            dup = ["10|9|100|6"] * 10
            _write_table(
                config / "config_level.csv",
                [
                    "LevelId",
                    "TemplateId",
                    "randomSeed_groupID",
                    *(f"DupPara{i}" for i in range(1, 11)),
                ],
                [
                    ["legacy_level", "hr0029", "0", *dup],
                    ["structured_level", "hr0029", "37", *dup],
                ],
            )
            _write_table(
                config / "config_level_list.csv",
                ["LevelIndex", "LevelId"],
                [["210", "legacy_level"], ["211", "structured_level"]],
            )
            profile = Profile("p", "test", None, None, str(root / "out"), str(root))
            manifest = {
                "source_template_id": "hr0029",
                "output_template_id": "aipuzzlehr0029s5",
                "replacements": [],
            }

            result = build_parameter_recommendations(
                profile,
                [manifest],
                config_set_id="tile01_ios_config",
                level_start=200,
                level_end=300,
                strategy="fidelity",
            )

            self.assertEqual(len(result["rows"]), 2)
            self.assertEqual(
                [row["random_seed_group_id"] for row in result["rows"]],
                [0, 37],
            )
            self.assertEqual(result["rows"][0]["level_ids"], ["legacy_level"])
            self.assertEqual(
                result["rows"][1]["level_ids"], ["structured_level"]
            )

            unlaunched = lookup_unlaunched_template_parameters(
                profile, ["hr0029"], config_set_id="tile01_ios_config"
            )
            self.assertEqual(
                [row["random_seed_group_id"] for row in unlaunched["rows"]],
                [0, 37],
            )

    def test_read_only_join_uses_source_tile_majority_and_reports_partial_deduction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "tile01_ios_config"
            config.mkdir()
            dup = [f"{7 - min(index, 4)}|{index}|100|4" for index in range(10)]
            _write_table(
                config / "config_level.csv",
                ["LevelId", "TemplateId", *(f"DupPara{i}" for i in range(1, 11))],
                [["level_normal", "hr0029", *dup]],
            )
            _write_table(
                config / "config_level_list.csv",
                ["LevelIndex", "LevelId"], [["210", "level_normal"]],
            )
            profile = Profile("p", "test", None, None, str(root / "out"), str(root))
            manifest = {
                "source_template_id": "hr0029",
                "output_template_id": "aipuzzlehr0029s5",
                "source_tile_counts": {"A": 68, "B": 68},
                "replacements": [
                    {"source_tile_id": -10, "structure_type": structure_type}
                    for structure_type in "CDEF"
                ],
            }
            result = build_parameter_recommendations(
                profile, [manifest], config_set_id="tile01_ios_config",
                level_start=200, level_end=300, strategy="fidelity",
            )
            row = result["rows"][0]
            self.assertEqual(row["source_majority_side"], "A")
            self.assertEqual((row["minimum_a_types"], row["minimum_b_types"]), (4, 3))
            self.assertEqual(row["recommended_dup"][0], "4|0|100|4")
            self.assertEqual(row["recommended_dup"][-1], "3|9|100|4")
            self.assertEqual(row["partial_deductions"][0]["applied_a"], 3)
            self.assertEqual(row["partial_deductions"][-1]["applied_a"], 0)
            self.assertIn("少扣", row["message"])

    def test_rebuilding_gradient_keeps_each_original_seed_and_luck(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "tile01_ios_config"
            config.mkdir()
            dup = [f"10|{index}|{90 + index}|7" for index in range(10)]
            _write_table(
                config / "config_level.csv",
                ["LevelId", "TemplateId", *(f"DupPara{i}" for i in range(1, 11))],
                [["level_normal", "hr0029", *dup]],
            )
            _write_table(config / "config_level_list.csv", ["LevelIndex", "LevelId"], [["210", "level_normal"]])
            profile = Profile("p", "test", None, None, str(root / "out"), str(root))
            manifest = {
                "source_template_id": "hr0029",
                "output_template_id": "aipuzzlehr0029s5",
                "source_tile_counts": {"A": 30, "B": 60},
                "replacements": [{"source_tile_id": -11, "structure_type": "C"}],
            }
            result = build_parameter_recommendations(
                profile, [manifest], config_set_id="tile01_ios_config",
                level_start=200, level_end=300, strategy="balanced",
            )
            values = result["rows"][0]["recommended_dup"]
            self.assertEqual(result["rows"][0]["source_majority_side"], "B")
            self.assertEqual((result["rows"][0]["minimum_a_types"], result["rows"][0]["minimum_b_types"]), (3, 4))
            self.assertEqual(values[0], "10|0|90|6")
            self.assertEqual(values[-1].split("|")[1:3], ["9", "99"])

    def test_majority_floor_never_exceeds_remaining_three_tile_group_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "tile01_ios_config"
            config.mkdir()
            dup = ["7|0|100|6"] * 10
            _write_table(
                config / "config_level.csv",
                ["LevelId", "TemplateId", *(f"DupPara{i}" for i in range(1, 11))],
                [["level_normal", "hr0029", *dup]],
            )
            _write_table(
                config / "config_level_list.csv",
                ["LevelIndex", "LevelId"],
                [["210", "level_normal"]],
            )
            replacements = [
                {"source_tile_id": -10, "structure_type": structure_type}
                for structure_type in "CDEF"
                for _ in range(3)
            ]
            profile = Profile("p", "test", None, None, str(root / "out"), str(root))
            result = build_parameter_recommendations(
                profile,
                [{
                    "source_template_id": "hr0029",
                    "output_template_id": "aipuzzlehr0029s5",
                    "source_tile_counts": {"A": 21, "B": 18},
                    "replacements": replacements,
                }],
                config_set_id="tile01_ios_config",
                level_start=200,
                level_end=300,
                strategy="fidelity",
            )

            row = result["rows"][0]
            self.assertEqual(row["remaining_a_group_capacity"], 3)
            self.assertEqual(row["minimum_a_types"], 3)
            self.assertEqual(row["recommended_dup"][0], "3|0|100|6")

    def test_unlaunched_index_reads_config_level_and_marks_duplicate_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "offline_config"
            config.mkdir()
            first_dup = ["10|9|100|6"] * 10
            second_dup = ["8|7|90|5"] * 10
            _write_table(
                config / "config_level.csv",
                ["LevelId", "TemplateId", *(f"DupPara{i}" for i in range(1, 11))],
                [
                    ["level_a", "zm0105plus", *first_dup],
                    ["level_b", "zm0105plus", *second_dup],
                    ["level_c", "zm0077", *first_dup],
                ],
            )
            profile = Profile("p", "test", None, None, str(root / "out"), str(root))

            self.assertEqual(
                [item["id"] for item in discover_unlaunched_config_sets(root)],
                ["offline_config"],
            )
            result = lookup_unlaunched_template_parameters(
                profile,
                ["zm0105plus", "zm0077", "missing"],
                config_set_id="offline_config",
            )

            self.assertEqual(len(result["rows"]), 3)
            self.assertEqual(result["duplicate_template_ids"], ["zm0105plus"])
            duplicate_rows = [row for row in result["rows"] if row["template_id"] == "zm0105plus"]
            self.assertEqual([row["level_id"] for row in duplicate_rows], ["level_a", "level_b"])
            self.assertTrue(all(row["is_duplicate"] for row in duplicate_rows))
            self.assertEqual(result["missing_template_ids"], ["missing"])
            self.assertNotIn("config_level_list", result["source_tables"])

    def test_unlaunched_index_searches_all_config_levels_without_config_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dup = ["10|9|100|6"] * 10
            for name, level_id in (("first", "level_a"), ("second", "level_b")):
                config = root / name
                config.mkdir()
                _write_table(
                    config / "config_level.csv",
                    ["LevelId", "TemplateId", *(f"DupPara{i}" for i in range(1, 11))],
                    [[level_id, "zm0105plus", *dup]],
                )
            profile = Profile("p", "test", None, None, str(root / "out"), str(root))

            result = lookup_unlaunched_template_parameters(
                profile,
                ["zm0105plus"],
            )

            self.assertEqual(result["config_set_ids"], ["first", "second"])
            self.assertEqual(result["duplicate_template_ids"], ["zm0105plus"])
            self.assertEqual(
                [row["config_level_path"] for row in result["rows"]],
                ["first/config_level.csv", "second/config_level.csv"],
            )
            self.assertTrue(all(row["is_duplicate"] for row in result["rows"]))

    def test_out_of_range_warning_lists_all_actual_level_positions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "tile01_ios_config"
            config.mkdir()
            dup = ["10|9|100|6"] * 10
            _write_table(
                config / "config_level.csv",
                ["LevelId", "TemplateId", *(f"DupPara{i}" for i in range(1, 11))],
                [["level_ft", "ft0125", *dup], ["level_ft_dda", "ft0125", *dup]],
            )
            _write_table(
                config / "config_level_list.csv",
                ["LevelIndex", "LevelId", "DDALevelId1", "DDALevelId2", "DDALevelId3", "DDAType"],
                [
                    ["14201", "level_ft", "", "", "", "0"],
                    ["14202", "level_ft", "", "", "", "0"],
                    ["14203", "level_ft", "", "", "", "0"],
                    ["14510", "unused", "", "level_ft_dda", "", "1"],
                ],
            )
            profile = Profile("p", "test", None, None, str(root / "out"), str(root))
            manifest = {
                "source_template_id": "ft0125",
                "output_template_id": "aipuzzleft0125s5",
                "replacements": [],
            }

            result = build_parameter_recommendations(
                profile,
                [manifest],
                config_set_id="tile01_ios_config",
                level_start=14670,
                level_end=14679,
                strategy="balanced",
            )

            self.assertEqual(result["rows"], [])
            self.assertEqual(
                result["warnings"],
                [
                    "ft0125 在 config_level 中存在，但未用于 14670–14679 关；"
                    "实际用于：14201–14203、14510 关"
                ],
            )


if __name__ == "__main__":
    unittest.main()
