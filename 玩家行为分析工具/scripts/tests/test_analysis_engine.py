from __future__ import annotations

import csv
import hashlib
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import analysis_engine as engine
from xlsx_writer import inspect_xlsx, write_stage1_workbook, write_stage2_workbook


def player_row(user_id: str, event_time: str, level_index: int) -> dict[str, str]:
    row = {column: "" for column in engine.PLAYER_REQUIRED_COLUMNS}
    row.update(
        {
            "cohort_player_rank": "1",
            "user_id": user_id,
            "event_time": event_time,
            "client_version": "2.08.0",
            "platform": "android",
            "level_index": str(level_index),
            "level_id": f"list{level_index}",
            "level_template_id": "n0001",
            "level_play_count": "1",
            "level_dup_para_id": "1",
            "level_dup_para": "9|4|100|2",
            "level_status": "pass",
            "level_duration_sec": "12.5",
            "bag_tile_change_curve": "0,1,2",
            "surface_normal_tile_change_curve": "6,5,4",
            "replay_info": "0#2:1-2-3|500#1:4",
        }
    )
    return row


class PlayerInputAuditTests(unittest.TestCase):
    @staticmethod
    def _resource_settings(root: Path) -> dict[str, Path]:
        config, templates, reader = root / "config", root / "templates", root / "reader"
        config.mkdir(); templates.mkdir(); reader.mkdir()
        for name in engine.REQUIRED_CONFIG_FILES:
            (config / name).write_text("id\n", encoding="utf-8")
        (templates / "n0001_m.bytes").write_bytes(b"test")
        return {"config_dir": config, "template_dir": templates, "template_reader_dir": reader}

    def test_uid_overlap_is_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, second = root / "first.csv", root / "second.csv"
            rows_a = [player_row("u1", "2026-01-01T00:00:00Z", 100), player_row("u2", "2026-01-01T00:01:00Z", 101)]
            rows_b = [dict(rows_a[0]), player_row("u1", "2026-01-01T00:02:00Z", 102)]
            columns = sorted(engine.PLAYER_REQUIRED_COLUMNS)
            for path, rows in ((first, rows_a), (second, rows_b)):
                with path.open("w", encoding="utf-8-sig", newline="") as target:
                    writer = csv.DictWriter(target, fieldnames=columns)
                    writer.writeheader()
                    writer.writerows(rows)
            audit = engine.preflight_player(
                [first, second],
                self._resource_settings(root),
                root / "work",
            )
            self.assertEqual(audit["status"], "error")
            self.assertEqual(audit["raw_rows"], 4)
            self.assertTrue(any("重复UID" in message for message in audit["errors"]))
            self.assertEqual(audit["uid_overlaps"][0]["overlap_player_count"], 1)

    def test_exact_duplicate_file_is_ignored_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, second = root / "first.csv", root / "copy.csv"
            rows = [player_row("u1", "2026-01-01T00:00:00Z", 100), player_row("u2", "2026-01-01T00:01:00Z", 101)]
            columns = sorted(engine.PLAYER_REQUIRED_COLUMNS)
            with first.open("w", encoding="utf-8-sig", newline="") as target:
                writer = csv.DictWriter(target, fieldnames=columns)
                writer.writeheader(); writer.writerows(rows)
            second.write_bytes(first.read_bytes())
            audit = engine.preflight_player([first, second], self._resource_settings(root), root / "work")
            self.assertEqual(audit["status"], "warning")
            self.assertEqual(audit["effective_source_file_count"], 1)
            self.assertEqual(audit["merged_rows"], 2)
            self.assertTrue(any("完全相同" in message for message in audit["warnings"]))

    def test_rejects_noninteger_levels_and_blank_instance_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "invalid.csv"
            rows = [
                player_row("u1", "2026-01-01T00:00:00Z", 100),
                player_row("u2", "2026-01-01T00:01:00Z", 101),
            ]
            rows[0]["level_index"] = "100.5"
            rows[0]["level_template_id"] = ""
            rows[1]["replay_info"] = ""
            columns = sorted(engine.PLAYER_REQUIRED_COLUMNS)
            with path.open("w", encoding="utf-8-sig", newline="") as target:
                writer = csv.DictWriter(target, fieldnames=columns)
                writer.writeheader(); writer.writerows(rows)
            audit = engine.preflight_player(
                [path], self._resource_settings(root), root / "work"
            )
            self.assertFalse(audit["valid"])
            self.assertTrue(any("level_index" in message and "非整数" in message for message in audit["errors"]))
            self.assertTrue(any("level_template_id" in message for message in audit["errors"]))
            self.assertFalse(any("replay_info为空" in message for message in audit["errors"]))
            self.assertEqual(audit["skipped_empty_replay_rows"], 1)

    def test_skips_blank_normal_replay_and_keeps_other_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "partly_usable.csv"
            rows = [
                player_row("u1", "2026-01-01T00:00:00Z", 100),
                player_row("u2", "2026-01-01T00:01:00Z", 101),
            ]
            rows[1]["replay_info"] = ""
            columns = sorted(engine.PLAYER_REQUIRED_COLUMNS)
            with path.open("w", encoding="utf-8-sig", newline="") as target:
                writer = csv.DictWriter(target, fieldnames=columns)
                writer.writeheader(); writer.writerows(rows)
            audit = engine.preflight_player(
                [path], self._resource_settings(root), root / "work"
            )
            self.assertTrue(audit["valid"])
            self.assertEqual(audit["status"], "warning")
            self.assertEqual(audit["rows_after_deduplication"], 2)
            self.assertEqual(audit["merged_rows"], 1)
            self.assertEqual(audit["normal_outcome_rows_before_replay_filter"], 2)
            self.assertEqual(audit["normal_outcome_rows"], 1)
            self.assertEqual(audit["skipped_empty_replay_rows"], 1)
            self.assertTrue(any("已跳过" in message for message in audit["warnings"]))
            merged = pd.read_csv(audit["merged_csv_path"], dtype=str, keep_default_na=False)
            self.assertEqual(merged["user_id"].tolist(), ["u1"])

    def test_rejects_when_every_normal_replay_is_blank(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "no_replay.csv"
            rows = [
                player_row("u1", "2026-01-01T00:00:00Z", 100),
                player_row("u2", "2026-01-01T00:01:00Z", 101),
            ]
            for row in rows:
                row["replay_info"] = ""
            columns = sorted(engine.PLAYER_REQUIRED_COLUMNS)
            with path.open("w", encoding="utf-8-sig", newline="") as target:
                writer = csv.DictWriter(target, fieldnames=columns)
                writer.writeheader(); writer.writerows(rows)
            audit = engine.preflight_player(
                [path], self._resource_settings(root), root / "work"
            )
            self.assertFalse(audit["valid"])
            self.assertEqual(audit["skipped_empty_replay_rows"], 2)
            self.assertTrue(any("没有可分析" in message for message in audit["errors"]))

    def test_rejects_all_exit_or_unknown_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "exit_only.csv"
            rows = [
                player_row("u1", "2026-01-01T00:00:00Z", 100),
                player_row("u2", "2026-01-01T00:01:00Z", 101),
            ]
            rows[0]["level_status"] = "exit"
            rows[1]["level_status"] = "unknown"
            columns = sorted(engine.PLAYER_REQUIRED_COLUMNS)
            with path.open("w", encoding="utf-8-sig", newline="") as target:
                writer = csv.DictWriter(target, fieldnames=columns)
                writer.writeheader(); writer.writerows(rows)
            audit = engine.preflight_player(
                [path], self._resource_settings(root), root / "work"
            )
            self.assertFalse(audit["valid"])
            self.assertEqual(audit["normal_outcome_rows"], 0)
            self.assertTrue(any("全为exit/unknown" in message for message in audit["errors"]))

    def test_historical_snapshot_never_mixes_current_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            config = repo / "csv"
            config.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            for name in engine.REQUIRED_CONFIG_FILES:
                (config / name).write_text(f"{name},A\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "csv"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "A"], check=True)
            revision_a = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
            ).strip()
            for name in engine.REQUIRED_CONFIG_FILES:
                (config / name).write_text(f"{name},B\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "csv"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "B"], check=True)
            revision_b = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
            ).strip()
            settings = {
                "history_mode": "historical",
                "config_level_git_revision": revision_a,
                "level_tile_git_revision": revision_b,
            }
            git_audit = engine._git_revision_audit(config, settings)
            snapshot, manifest = engine._materialize_config_snapshot(
                config, settings, root / "work", git_audit
            )
            self.assertEqual((snapshot / "config_level.csv").read_text(), "config_level.csv,A\n")
            for name in engine.REQUIRED_CONFIG_FILES:
                expected_revision = revision_a if name == "config_level.csv" else revision_b
                expected_suffix = "A\n" if name == "config_level.csv" else "B\n"
                self.assertTrue((snapshot / name).read_text().endswith(expected_suffix))
                self.assertEqual(manifest["files"][name]["revision"], expected_revision)
                self.assertEqual(
                    manifest["files"][name]["sha256"],
                    hashlib.sha256((snapshot / name).read_bytes()).hexdigest(),
                )


class RobustClassificationTests(unittest.TestCase):
    @staticmethod
    def _sequence_frame() -> pd.DataFrame:
        rows = []
        for offset, level in enumerate(range(100, 200)):
            tag_number = min(7, offset // 12)
            rows.append(
                {
                    "level_index": level,
                    "level_template_id": f"n{(offset // 3) % 7:04d}",
                    "tag_code": f"T{tag_number}",
                    "pressure_order": min(4, tag_number // 2),
                    "consume_z": (offset % 12 - 6) / 6,
                    "fail_z": ((offset * 3) % 11 - 5) / 5,
                }
            )
        return pd.DataFrame(rows)

    def test_center_scale_and_band_boundaries(self) -> None:
        center, scale = engine.robust_center_scale([1, 2, 3, 4, 100])
        self.assertEqual(center, 3.0)
        self.assertAlmostEqual(scale, 1.4826, places=4)
        self.assertEqual(engine.robust_band(-2), "极低")
        self.assertEqual(engine.robust_band(-1), "偏低")
        self.assertEqual(engine.robust_band(0.999), "正常")
        self.assertEqual(engine.robust_band(1), "偏高")
        self.assertEqual(engine.robust_band(2), "极高")

    def test_all_named_tags(self) -> None:
        cases = {
            (0, "正常", "正常"): "T0",
            (0, "偏高", "正常"): "T1",
            (1, "正常", "正常"): "T2",
            (0, "正常", "偏高"): "T3",
            (2, "正常", "正常"): "T4",
            (2, "偏高", "正常"): "T5",
            (2, "正常", "偏高"): "T6",
            (2, "偏高", "偏高"): "T7",
        }
        for arguments, expected in cases.items():
            self.assertEqual(engine.named_tag(*arguments), expected)

    def test_planner_tags_collapse_t2_t3_and_reserve_px_for_extreme_loss(self) -> None:
        base = {
            "pressure_order": 0,
            "first_any_friction_share": 0.0,
            "first_compound_high_burden_share": 0.0,
            "first_L1_share": 0.0,
            "first_L4_share": 0.0,
            "first_L3_share": 0.0,
            "consume_z": 0.0,
            "first_level_behavior_label": "整体未见稳定高负担",
        }
        self.assertEqual(engine.planner_pressure_tag({**base, "tag_code": "T2", "fail_z": 0.0}), "P2")
        self.assertEqual(engine.planner_pressure_tag({**base, "tag_code": "T3", "fail_z": 1.2}), "P2")
        self.assertEqual(engine.planner_pressure_tag({**base, "tag_code": "T3", "fail_z": 2.0}), "P2")
        high_pressure = {
            **base,
            "tag_code": "T6",
            "pressure_order": 4,
            "first_any_friction_share": 0.62,
            "first_compound_high_burden_share": 0.52,
            "first_L4_share": 0.30,
            "first_L3_share": 0.27,
        }
        self.assertEqual(engine.planner_pressure_tag({**high_pressure, "fail_z": 2.0}), "P5")
        self.assertEqual(engine.planner_pressure_tag({**high_pressure, "fail_z": 3.0}), "PX")
        self.assertIn("正常排位", engine.planner_risk_note({**high_pressure, "fail_z": 2.0}, "P5"))

    def test_slot_assignment_reserves_5_and_9_for_harder_buckets(self) -> None:
        rows = []
        for level in range(100, 200):
            tag = f"T{min(7, level // 13 - 7)}"
            rows.append(
                {
                    "level_index": level,
                    "tag_code": tag,
                    "pressure_order": int(tag[1:]) // 2,
                    "consume_z": (level - 150) / 20,
                    "fail_z": (level - 150) / 25,
                }
            )
        output = engine.assign_recommended_positions(pd.DataFrame(rows))
        self.assertEqual(output["recommended_level_index"].nunique(), 100)
        score_by_digit = output.groupby(output["recommended_level_index"] % 10)["sequence_score"].mean()
        self.assertGreater(score_by_digit[9], score_by_digit[5])
        self.assertGreater(score_by_digit[5], score_by_digit.drop([5, 9]).max())

    def test_all_sequence_strategies_are_unique_reproducible_permutations(self) -> None:
        frame = self._sequence_frame()
        for code, spec in engine.SEQUENCE_STRATEGIES.items():
            first = engine.assign_recommended_positions(frame, code)
            second = engine.assign_recommended_positions(frame, code)
            self.assertEqual(set(first["recommended_level_index"]), set(range(100, 200)))
            self.assertEqual(first["recommended_level_index"].tolist(), second["recommended_level_index"].tolist())
            for bucket, digit in enumerate(spec["bucket_to_digit"]):
                assigned_digits = set(
                    first.loc[first["sequence_risk_bucket"].eq(bucket), "recommended_level_index"] % 10
                )
                self.assertEqual(assigned_digits, {digit})

    def test_dual_peak_default_mapping_regression(self) -> None:
        frame = self._sequence_frame()
        implicit = engine.assign_recommended_positions(frame)
        explicit = engine.assign_recommended_positions(frame, "dual_peak")
        self.assertEqual(
            implicit.sort_values("level_index")["recommended_level_index"].tolist(),
            explicit.sort_values("level_index")["recommended_level_index"].tolist(),
        )
        ranked = explicit.sort_values(["sequence_score", "level_index"], kind="stable").reset_index(drop=True)
        expected: dict[int, int] = {}
        for bucket, digit in enumerate([0, 1, 2, 6, 3, 7, 4, 8, 5, 9]):
            source_levels = sorted(
                ranked.iloc[bucket * 10 : (bucket + 1) * 10]["level_index"].astype(int)
            )
            for source, target in zip(source_levels, range(100 + digit, 200, 10)):
                expected[source] = target
        actual = dict(zip(explicit["level_index"], explicit["recommended_level_index"]))
        self.assertEqual(actual, expected)

    def test_spiral_flow_uses_requested_position_roles(self) -> None:
        output = engine.assign_recommended_positions(self._sequence_frame(), "spiral_flow")
        expected_roles = ["爽", "过渡", "轻谜题", "轻谜题", "多谜题", "高压挑战", "过渡", "过渡", "多谜题", "超难高危"]
        roles = (
            output[["sequence_slot_digit", "sequence_role"]]
            .drop_duplicates().sort_values("sequence_slot_digit")["sequence_role"].tolist()
        )
        self.assertEqual(roles, expected_roles)
        self.assertEqual(engine.SEQUENCE_STRATEGIES["spiral_flow"]["bucket_to_digit"], [0, 1, 6, 7, 2, 3, 4, 8, 5, 9])

    def test_spiral_flow_fills_tail_zero_then_earliest_overflow_in_source_order(self) -> None:
        frame = self._sequence_frame()
        frame["planner_tag_code"] = "P1"
        p0_levels = [101, 105, 112, 120, 133, 140, 151, 162, 173, 184, 195]
        frame.loc[frame["level_index"].isin(p0_levels), "planner_tag_code"] = "P0"
        output = engine.assign_recommended_positions(frame, "spiral_flow")
        mapping = output.set_index("level_index")["recommended_level_index"].to_dict()
        self.assertEqual([mapping[level] for level in sorted(p0_levels)[:10]], list(range(100, 200, 10)))
        self.assertEqual(mapping[max(p0_levels)], 101)

    def test_dominant_duplicate_method_records_source_duplicate(self) -> None:
        levels = pd.DataFrame([{
            "level_index": 100,
            "enter_users": 1000,
            "planner_tag_code": "P2",
            "planner_tag": "P2 轻度卡顿型",
        }])
        duplicates = [
            {"level_index": 100, "attempt_phase": "首局", "level_dup_para_id": "1", "level_dup_para": "a", "enter_users": 300, "duplicate_planner_code": "P0"},
            {"level_index": 100, "attempt_phase": "首局", "level_dup_para_id": "2", "level_dup_para": "b", "enter_users": 700, "duplicate_planner_code": "P4"},
        ]
        output = engine.apply_level_label_method(levels, duplicates, "dominant_online_duplicate")
        self.assertEqual(output.iloc[0]["planner_tag_code"], "P4")
        self.assertEqual(output.iloc[0]["dominant_duplicate_id"], "2")
        self.assertAlmostEqual(output.iloc[0]["dominant_duplicate_enter_share"], 0.7)

    def test_diversity_guard_never_worsens_requested_adjacencies(self) -> None:
        frame = self._sequence_frame()
        baseline = engine.assign_recommended_positions(frame, "dual_peak")
        protected = engine.assign_recommended_positions(frame, "diversity_guard")
        before = engine.sequence_adjacency_metrics(baseline)
        after = engine.sequence_adjacency_metrics(protected)
        self.assertLessEqual(after["adjacent_same_template_count"], before["adjacent_same_template_count"])
        self.assertLessEqual(after["adjacent_t6_t7_count"], before["adjacent_t6_t7_count"])
        baseline_digit = baseline.set_index("level_index")["sequence_slot_digit"].to_dict()
        protected_digit = protected.set_index("level_index")["sequence_slot_digit"].to_dict()
        self.assertEqual(baseline_digit, protected_digit)

    def test_real_regression_when_fixture_is_available(self) -> None:
        online_path = Path.home() / "Downloads" / "Tile关卡副本数据_2026_09_18.csv"
        labels_path = SCRIPT_DIR.parents[1] / "输出/玩家单关_TileNum_2026_09_18_550人实时行为分层_v3/15_550人关卡号完整分层表.csv"
        if not (online_path.is_file() and labels_path.is_file()):
            self.skipTest("local regression fixture not present")
        online, audit = engine._load_online_sources([online_path])
        self.assertFalse(audit["errors"])
        payload = engine.analyze_online_levels(online, engine._read_csv(labels_path))
        metadata = payload["metadata"]
        self.assertAlmostEqual(metadata["consume_center"], 1003.5310138738165)
        self.assertAlmostEqual(metadata["consume_scale"], 239.0421040052856)
        self.assertAlmostEqual(metadata["fail_loss_center"], 0.006688943525839764)
        self.assertAlmostEqual(metadata["fail_loss_scale"], 0.0020895532056039385)
        counts = {item["code"]: item["count"] for item in metadata["tag_definitions"]}
        self.assertEqual(counts, {"T0": 50, "T1": 2, "T2": 12, "T3": 9, "T4": 12, "T5": 3, "T6": 5, "T7": 7})


class SampleRecommendationTests(unittest.TestCase):
    def test_pending_levels_receive_buffered_player_recommendation(self) -> None:
        instances = pd.DataFrame(
            [
                {
                    "level_index": 100,
                    "attempt_phase": "首局",
                    "player_count": 9,
                    "behavior_layer": "P_待补样",
                }
                for _ in range(10)
            ]
        )
        levels = pd.DataFrame(
            [{"level_index": 100, "first_level_behavior_label": "样本覆盖不足"}]
        )

        recommendation = engine.estimate_sample_recommendation(instances, levels, 90)

        self.assertEqual(recommendation["next_action"], "add_player_sample")
        self.assertEqual(recommendation["pending_level_count"], 1)
        self.assertEqual(recommendation["recommended_additional_players"], 20)
        self.assertEqual(recommendation["recommended_total_players"], 110)

    def test_fully_classified_levels_continue_to_online_metrics(self) -> None:
        instances = pd.DataFrame(
            [{"level_index": 100, "attempt_phase": "首局", "player_count": 20}]
        )
        levels = pd.DataFrame(
            [{"level_index": 100, "first_level_behavior_label": "整体未见稳定高负担"}]
        )

        recommendation = engine.estimate_sample_recommendation(instances, levels, 100)

        self.assertEqual(recommendation["next_action"], "online_metrics")
        self.assertEqual(recommendation["recommended_additional_players"], 0)
        self.assertEqual(recommendation["recommended_total_players"], 100)

    def test_player_result_exposes_recommendation_through_summary(self) -> None:
        source = Path(__file__).resolve().parents[2] / "scripts" / "analysis_engine.py"
        self.assertIn('"summary": summary', source.read_text(encoding="utf-8"))


class LevelListExportTests(unittest.TestCase):
    def test_complete_copy_preserves_metadata_and_only_permutes_four_id_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "config_level_list.csv"
            rows = [["LevelIndex", *engine.LEVEL_LIST_ID_COLUMNS, "Other"]]
            rows.append(["int", "string", "string", "string", "string", "string"])
            rows.append(["关卡号", "关卡ID", "DDA1", "DDA2", "DDA3", "其他说明"])
            for level in range(95, 205):
                rows.append(
                    [str(level), f"L{level}", f"A{level}", f"B{level}", f"C{level}", f"keep-{level}"]
                )
            with source.open("w", encoding="utf-8-sig", newline="") as target:
                csv.writer(target).writerows(rows)
            source_before = source.read_bytes()
            mappings = [
                {"original_level_index": level, "recommended_level_index": 299 - level}
                for level in range(100, 200)
            ]
            result = engine.build_level_list_reorder_copy(
                source, mappings, root / "output", "single_ramp"
            )
            copy_path = Path(result["level_list_copy_path"])
            audit_path = Path(result["level_list_audit_path"])
            with source.open(encoding="utf-8-sig", newline="") as handle:
                source_rows = list(csv.reader(handle))
            with copy_path.open(encoding="utf-8-sig", newline="") as handle:
                copied_rows = list(csv.reader(handle))
            self.assertEqual(source.read_bytes(), source_before)
            self.assertEqual(copied_rows[:3], source_rows[:3])
            self.assertEqual(len(copied_rows), len(source_rows))
            header = source_rows[0]
            level_col = header.index("LevelIndex")
            id_cols = [header.index(column) for column in engine.LEVEL_LIST_ID_COLUMNS]
            other_col = header.index("Other")
            source_by_level = {int(row[level_col]): row for row in source_rows[3:]}
            copy_by_level = {int(row[level_col]): row for row in copied_rows[3:]}
            for original, target in ((level, 299 - level) for level in range(100, 200)):
                self.assertEqual(
                    [copy_by_level[target][index] for index in id_cols],
                    [source_by_level[original][index] for index in id_cols],
                )
            for level in range(95, 205):
                self.assertEqual(copy_by_level[level][other_col], source_by_level[level][other_col])
            with audit_path.open(encoding="utf-8-sig", newline="") as handle:
                self.assertEqual(sum(1 for _ in csv.DictReader(handle)), 100)
            self.assertTrue(result["metadata"]["metadata_rows_preserved"])
            self.assertTrue(result["metadata"]["id_multisets_conserved"])
            self.assertEqual(result["metadata"]["source_sha256"], hashlib.sha256(source_before).hexdigest())

    def test_partial_copy_keeps_problem_positions_blank(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "config_level_list.csv"
            rows = [["LevelIndex", *engine.LEVEL_LIST_ID_COLUMNS]]
            rows.append(["int", "string", "string", "string", "string"])
            rows.append(["关卡号", "关卡ID", "DDA1", "DDA2", "DDA3"])
            for level in range(100, 200):
                rows.append([str(level), f"L{level}", f"A{level}", f"B{level}", f"C{level}"])
            with source.open("w", encoding="utf-8-sig", newline="") as target:
                csv.writer(target).writerows(rows)
            mappings = []
            for level in range(100, 200):
                mappings.append({
                    "original_level_index": level,
                    "recommended_level_index": None if level == 109 else level,
                    "planner_tag": "PX 负面体验突出" if level == 109 else "P1 平淡过渡型",
                })
            result = engine.build_level_list_reorder_copy(
                source, mappings, root / "output", "spiral_flow"
            )
            with Path(result["level_list_copy_path"]).open(encoding="utf-8-sig", newline="") as handle:
                copied = list(csv.reader(handle))
            header = copied[0]
            level_col = header.index("LevelIndex")
            id_cols = [header.index(column) for column in engine.LEVEL_LIST_ID_COLUMNS]
            by_level = {int(row[level_col]): row for row in copied[3:]}
            self.assertEqual([by_level[109][index] for index in id_cols], ["", "", "", ""])
            self.assertEqual(result["metadata"]["blank_positions"], [109])
            self.assertEqual(result["metadata"]["mapped_level_count"], 99)


class OnlineInputAuditTests(unittest.TestCase):
    @staticmethod
    def _write_online(path: Path, levels: list[int], **overrides: object) -> None:
        rows = []
        for index, level in enumerate(levels):
            row = {
                "level_index": level,
                "level_id": f"list{level}",
                "level_template_id": f"n{level}",
                "进入人数": 100,
                "人均消耗": 1.5,
                "失败流失": "1%",
                "user_info": "tile01g",
                "strategy_group_id": "all",
                "strategy_effective_list": "all",
                "strategy_failed_list": "all",
            }
            for key, value in overrides.items():
                row[key] = value(index, level) if callable(value) else value
            rows.append(row)
        pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")

    def test_rejects_invalid_ranges_and_mixed_aggregation_slices(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "online.csv"
            self._write_online(
                path,
                [100, 101],
                人均消耗=lambda index, _level: -1 if index == 0 else 2,
                失败流失=lambda index, _level: "120%" if index == 0 else "1%",
                strategy_group_id=lambda index, _level: "all" if index == 0 else "U1_0",
            )
            _frame, audit = engine._load_online_sources([path])
            self.assertFalse(audit["valid"])
            self.assertTrue(any("人均消耗小于0" in message for message in audit["errors"]))
            self.assertTrue(any("失败流失不在0–1" in message for message in audit["errors"]))
            self.assertTrue(any("混入2种聚合口径" in message for message in audit["errors"]))

    def test_preflight_rejects_non_contiguous_hundred_levels(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            levels = list(range(100, 199)) + [200]
            online_path = root / "online.csv"
            labels_path = root / "levels.csv"
            self._write_online(online_path, levels)
            pd.DataFrame(
                {
                    "level_index": levels,
                    "first_level_behavior_label": ["整体未见稳定高负担"] * 100,
                }
            ).to_csv(labels_path, index=False, encoding="utf-8-sig")
            audit = engine.preflight_online(
                [online_path],
                {},
                {"levels_csv_path": str(labels_path)},
                root / "work",
            )
            self.assertFalse(audit["valid"])
            self.assertTrue(any("关卡号不连续" in message for message in audit["errors"]))

    def test_preflight_rejects_stage1_levels_that_still_need_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            levels = list(range(100, 200))
            online_path = root / "online.csv"
            labels_path = root / "levels.csv"
            self._write_online(online_path, levels)
            labels = ["整体未见稳定高负担"] * 100
            labels[7] = "样本覆盖不足"
            pd.DataFrame(
                {
                    "level_index": levels,
                    "first_level_behavior_label": labels,
                }
            ).to_csv(labels_path, index=False, encoding="utf-8-sig")
            audit = engine.preflight_online(
                [online_path],
                {},
                {"levels_csv_path": str(labels_path)},
                root / "work",
            )
            self.assertFalse(audit["valid"])
            self.assertEqual(audit["unclassified_stage1_label_counts"], {"样本覆盖不足": 1})
            self.assertTrue(any("补充互不重复的玩家批次" in message for message in audit["errors"]))


class XlsxWriterTests(unittest.TestCase):
    def test_stage2_has_planner_duplicate_mapping_and_audit_sheets(self) -> None:
        frame = pd.DataFrame(
            {
                "level_index": range(100, 200),
                "level_id": [f"list{x}" for x in range(100, 200)],
                "level_template_id": ["n0001"] * 100,
                "first_level_behavior_label": ["整体未见稳定高负担"] * 100,
                "pressure_order": [0] * 100,
                "consume": np.linspace(0, 10, 100),
                "fail_loss": np.linspace(0, 0.02, 100),
            }
        )
        consume_center, consume_scale = engine.robust_center_scale(frame["consume"])
        fail_center, fail_scale = engine.robust_center_scale(frame["fail_loss"])
        frame["consume_z"] = (frame["consume"] - consume_center) / consume_scale
        frame["fail_z"] = (frame["fail_loss"] - fail_center) / fail_scale
        frame["consume_band"] = frame["consume_z"].map(engine.robust_band)
        frame["fail_band"] = frame["fail_z"].map(engine.robust_band)
        frame["tag_code"] = [engine.named_tag(0, a, b) for a, b in zip(frame["consume_band"], frame["fail_band"])]
        frame["named_tag"] = frame["tag_code"].map(lambda code: f"{code} {engine.TAG_DEFINITIONS[code][0]}")
        frame["planner_tag_code"] = "P1"
        frame["planner_tag"] = "P1 平淡过渡型"
        frame["slot_guidance"] = "过渡位"
        frame["feature_summary"] = "synthetic"
        frame["action"] = frame["tag_code"].map(engine.TAG_ACTIONS)
        frame["classification_label"] = "synthetic"
        frame["enter_users"] = 100
        frame = engine.assign_recommended_positions(frame)
        payload = {
            "metadata": {
                "level_range": "100-199", "level_count": 100,
                "consume_center": consume_center, "consume_scale": consume_scale,
                "fail_loss_center": fail_center, "fail_loss_scale": fail_scale,
                "raw_combination_count": 4, "named_tag_count": 8,
                "band_rule": "test", "sequence_rule": "test",
                "tag_definitions": [
                    {"code": code, "name": value[0], "definition": value[1], "count": int((frame["tag_code"] == code).sum())}
                    for code, value in engine.TAG_DEFINITIONS.items()
                ],
                "planner_tag_definitions": [
                    {"code": code, "name": value[0], "definition": value[1], "count": int(code == "P1") * 100}
                    for code, value in engine.PLANNER_TAG_DEFINITIONS.items()
                ],
            },
            "rows": [
                {
                    "original_level_index": int(row.level_index), "level_id": row.level_id,
                    "level_template_id": row.level_template_id, "named_tag": row.named_tag,
                    "planner_tag": row.planner_tag, "planner_tag_code": row.planner_tag_code,
                    "slot_guidance": row.slot_guidance,
                    "tag_code": row.tag_code, "feature_summary": row.feature_summary,
                    "action": row.action, "recommended_level_index": int(row.recommended_level_index),
                }
                for row in frame.itertuples(index=False)
            ],
            "audit_rows": frame.to_dict(orient="records"),
        }
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "stage2.xlsx"
            write_stage2_workbook(target, payload)
            report = inspect_xlsx(target)
            self.assertEqual(report["sheet_names"], ["原序列与推荐序列", "关卡副本标签", "配置重排对照", "内部计算审计"])
            self.assertEqual(report["table_count"], 2)
            with zipfile.ZipFile(target) as archive:
                first = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
                self.assertIn('state="frozen"', first)
                self.assertIn("<tableParts", first)
                self.assertIn("<autoFilter", first)

    def test_stage1_workbook_contract(self) -> None:
        summary = {
            "sample": {"players": 12, "valid_outcome_attempts": 1200, "decision_actions": 5000, "date_min": "2026-01-01", "date_max": "2026-01-31", "product": "tile01g", "platform": "android"},
            "business_instances": 100,
            "phase_layer_counts": [],
            "first_level_label_counts": {},
            "formal_signal_stability": {},
        }
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "stage1.xlsx"
            write_stage1_workbook(target, levels=[], instances=[], metrics=[], summary=summary)
            report = inspect_xlsx(target)
            self.assertEqual(report["sheet_names"], ["结论总览", "关卡号分层", "实例分层", "行为维度", "方法与边界"])
            self.assertEqual(report["table_count"], 3)


if __name__ == "__main__":
    unittest.main()
