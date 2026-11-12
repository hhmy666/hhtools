from __future__ import annotations

import codecs
import csv
import io
import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.reporting import (
    AUDIT_CSV_HEADERS,
    CSV_HEADERS,
    FIXED_CSV_HEADERS,
    TOWER_STEP_TEXT,
    ReportingError,
    render_authoritative_json,
    render_batch_markdown,
    render_comparison_csv,
)


def parse_csv(payload: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(payload.decode("utf-8-sig"))))


class AuthoritativeJsonTests(unittest.TestCase):
    def test_json_is_canonical_utf8_without_bom(self) -> None:
        first = {"状态": "通过", "details": {"b": 2, "a": 1}}
        second = {"details": {"a": 1, "b": 2}, "状态": "通过"}

        rendered = render_authoritative_json(first)
        self.assertEqual(rendered, render_authoritative_json(second))
        self.assertFalse(rendered.startswith(codecs.BOM_UTF8))
        self.assertEqual(
            rendered.decode("utf-8"),
            '{"details":{"a":1,"b":2},"状态":"通过"}',
        )

    def test_json_root_must_be_mapping_or_sequence(self) -> None:
        with self.assertRaises(ReportingError):
            render_authoritative_json("not a report record")


class ComparisonCsvTests(unittest.TestCase):
    def test_first_nine_headers_are_exactly_prd_14_1_order(self) -> None:
        rendered = render_comparison_csv(())
        rows = parse_csv(rendered)

        self.assertEqual(
            FIXED_CSV_HEADERS,
            (
                "线上关卡号",
                "关卡 ID",
                "模板 ID",
                "新的模板 ID",
                "替换 A Tile 数量",
                "替换 B Tile 数量",
                "使用的结构块",
                "替换后的结构块数量",
                "谜题结构完整解首消步数",
            ),
        )
        self.assertEqual(tuple(rows[0][:9]), FIXED_CSV_HEADERS)
        self.assertEqual(tuple(rows[0][9:]), AUDIT_CSV_HEADERS)
        self.assertEqual(tuple(rows[0]), CSV_HEADERS)

    def test_csv_is_utf8_sig_and_preserves_chinese(self) -> None:
        rendered = render_comparison_csv(
            (
                {
                    "online_level_number": 300,
                    "level_id": "关卡三百",
                    "source_template_id": "hr0029",
                    "output_template_id": "aipuzzlehr0029s5",
                    "replaced_a_count": 6,
                    "replaced_b_count": 3,
                    "structure_types": ("C", "D", "E"),
                    "new_structure_count": 9,
                    "puzzle_units": ({"unit_id": "U1", "actual_structure_step": 5},),
                    "validation_status": "通过",
                },
            )
        )

        self.assertTrue(rendered.startswith(codecs.BOM_UTF8))
        text = rendered.decode("utf-8-sig")
        self.assertIn("关卡三百", text)
        self.assertIn("C、D、E", text)
        self.assertIn("通过", text)

    def test_each_formal_template_remains_one_row(self) -> None:
        rows = (
            {
                "source_template_id": "hr0029",
                "output_template_id": "aipuzzlehr0029s5",
                "puzzle_steps": 5,
            },
            {
                "source_template_id": "hr0029",
                "output_template_id": "aipuzzlehr0029s6",
                "puzzle_steps": 6,
            },
        )
        parsed = parse_csv(render_comparison_csv(rows))

        self.assertEqual(len(parsed), 3)
        output_id_index = CSV_HEADERS.index("新的模板 ID")
        self.assertEqual(
            [row[output_id_index] for row in parsed[1:]],
            ["aipuzzlehr0029s5", "aipuzzlehr0029s6"],
        )

    def test_single_multi_unit_and_tower_step_values(self) -> None:
        rows = (
            {
                "output_template_id": "aipuzzlehr0029s5",
                "puzzle_units": ({"unit_id": "U1", "actual_step": 5},),
            },
            {
                "output_template_id": "aipuzzlehr0029s5s6",
                "puzzle_units": (
                    {"unit_id": "U1", "actual_step": 5},
                    {"unit_id": "U2", "actual_step": 6},
                ),
            },
            {
                "output_template_id": "aipuzzlehr0029tower",
                "tower_mode": "only",
            },
        )
        parsed = parse_csv(render_comparison_csv(rows))
        step_index = CSV_HEADERS.index("谜题结构完整解首消步数")

        self.assertEqual(
            [row[step_index] for row in parsed[1:]],
            ["5", "U1=5；U2=6", TOWER_STEP_TEXT],
        )

    def test_common_audit_columns_are_derived(self) -> None:
        row = {
            "source_sha256": "source-hash",
            "output_sha256": "output-hash",
            "generation_seed": 123456,
            "validation_status": "失败",
            "failure_reason": "没有合格候选",
            "tower_counts": {"inside": 6, "outside": 9},
            "nested_counts": {"A": 2, "B": 1},
            "replaced_a_count": 6,
            "replaced_b_count": 3,
            "structure_types": ["C", "D", "E"],
            "puzzle_steps": 5,
            "source_total_tile_count": 99,
            "source_existing_structure_count": 6,
            "output_structure_count": 15,
            "effective_structure_count_limit": 18,
            "effective_max_structure_layer": 6,
            "highest_structure_layer": 5,
            "adjacent_single_full_count": 0,
            "cross_layer_single_full_count": 2,
            "joint_full_cover_count": 1,
            "initial_entry_count": 3,
            "external_blocker_count": 4,
            "combination_conclusion_text": "7 格背包下允许顺序均可完成",
        }
        parsed = parse_csv(render_comparison_csv((row,)))
        values = dict(zip(parsed[0], parsed[1]))

        self.assertEqual(values["源文件 SHA-256"], "source-hash")
        self.assertEqual(values["输出文件 SHA-256"], "output-hash")
        self.assertEqual(values["谜题生成种子"], "123456")
        self.assertEqual(values["塔内结构块数量"], "6")
        self.assertEqual(values["塔外结构块数量"], "9")
        self.assertEqual(values["被夹 A Tile 唯一数量"], "2")
        self.assertEqual(values["被夹 B Tile 唯一数量"], "1")
        self.assertEqual(values["替换后的结构块数量"], "9")
        self.assertEqual(values["源模板 Tile 总数量"], "99")
        self.assertEqual(values["源模板既有结构块数量"], "6")
        self.assertEqual(values["输出结构块总数量"], "15")
        self.assertEqual(values["有效结构块数量上限"], "18")
        self.assertEqual(values["有效最高层限制"], "6")
        self.assertEqual(values["结构块实际最高层"], "5")
        self.assertEqual(values["相邻层单块全遮数量"], "0")
        self.assertEqual(values["跨层单块全遮诊断数量"], "2")
        self.assertEqual(values["联合全遮数量"], "1")
        self.assertEqual(values["初始入口数量"], "3")
        self.assertEqual(values["外部阻挡 Tile 唯一数量"], "4")

    def test_nested_outputs_are_rejected_instead_of_collapsed(self) -> None:
        with self.assertRaises(ReportingError):
            render_comparison_csv(({"outputs": ({"id": "one"}, {"id": "two"})},))


class MarkdownSummaryTests(unittest.TestCase):
    def test_summary_lists_every_output_and_step_mode(self) -> None:
        rows = (
            {
                "source_template_id": "hr0029",
                "output_template_id": "aipuzzlehr0029s5s6",
                "puzzle_units": (
                    {"unit_id": "U1", "step": 5},
                    {"unit_id": "U2", "step": 6},
                ),
                "validation_status": "通过",
            },
            {
                "source_template_id": "tower0001",
                "output_template_id": "aipuzzletower0001tower",
                "signature": "tower",
                "validation_status": "失败",
                "failure_reason": "空塔|无法分配\n需要检查",
            },
        )

        markdown = render_batch_markdown(
            {"batch_id": "20260909", "status": "部分失败"}, rows
        )

        self.assertIn("- 正式模板：2", markdown)
        self.assertIn("- 校验通过：1", markdown)
        self.assertIn("- 校验失败：1", markdown)
        self.assertIn("- 人工确认导出（非推荐）：0", markdown)
        self.assertIn("- 仅塔内：1", markdown)
        self.assertIn("U1=5；U2=6", markdown)
        self.assertIn(TOWER_STEP_TEXT, markdown)
        self.assertIn("空塔\\|无法分配<br>需要检查", markdown)
        self.assertIn("aipuzzlehr0029s5s6", markdown)
        self.assertIn("aipuzzletower0001tower", markdown)

    def test_manual_override_status_is_not_counted_as_validation_pass(self) -> None:
        markdown = render_batch_markdown(
            {"batch_id": "manual-review"},
            (
                {
                    "source_template_id": "hr0029",
                    "output_template_id": "aipuzzlehr0029s5",
                    "validation_status": "规则结论未通过（用户确认导出）",
                },
            ),
        )

        self.assertIn("- 校验通过：0", markdown)
        self.assertIn("- 校验失败：0", markdown)
        self.assertIn("- 人工确认导出（非推荐）：1", markdown)


if __name__ == "__main__":
    unittest.main()
