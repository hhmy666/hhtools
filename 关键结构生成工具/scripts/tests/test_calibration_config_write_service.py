from __future__ import annotations

import csv
import errno
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from services import calibration_config_write_service as write_service
from services.calibration_config_write_service import write_calibration_config
from services.profile_service import Profile
from puzzle_core.errors import InputValidationError


class CalibrationConfigWriteServiceTests(unittest.TestCase):
    def test_appends_reviewed_row_preserving_source_and_creating_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config_root = root / "config-repo"
            config_dir = config_root / "csv"
            config_dir.mkdir(parents=True)
            header = [
                "LevelId", "TemplateId", "StrategyGroupId", "KeepField",
                *(f"DupPara{index}" for index in range(1, 11)),
            ]
            source = [
                "202", "hhmy001", "1004", "keep-me",
                *(f"{10 - min(index, 5)}|{index}|100|4" for index in range(1, 11)),
            ]
            with (config_dir / "config_level.csv").open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(header)
                writer.writerow(source)
            with (config_dir / "config_level_list.csv").open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["LevelIndex", "LevelId"])
                writer.writerow([202, 202])
            profile = Profile("p", "test", str(root), str(root), str(root / "out"), str(config_root))
            rows = [{
                "source_template_id": "hhmy001",
                "output_template_id": "aipuzzlehhmy001cleans5",
                "level_ids": ["202"],
                "strategy_group_id": "1004",
                "duplicates": [
                    {"duplicate_index": index, "recommended_dup": f"7|{index}|100|4"}
                    for index in range(1, 11)
                ],
            }]

            original_bytes = (config_dir / "config_level.csv").read_bytes()
            original_files = {path.relative_to(config_root) for path in config_root.rglob("*")}
            with patch.object(write_service, "TOOL_ROOT", root / "tool", create=True):
                result = write_calibration_config(
                    profile, config_set_id="csv", calibration_rows=rows
                )

            self.assertEqual(result["updated_count"], 1)
            self.assertEqual(result["appended_count"], 1)
            backup = Path(result["backup_path"])
            self.assertTrue(backup.is_absolute(), "Backup path must not be relative to the config repository")
            self.assertTrue(backup.is_relative_to(root / "tool" / "运行数据" / "配置备份"))
            self.assertEqual(backup.read_bytes(), original_bytes)
            self.assertEqual({path.relative_to(config_root) for path in config_root.rglob("*")}, original_files)
            with (config_dir / "config_level.csv").open("r", encoding="utf-8-sig", newline="") as stream:
                written = list(csv.DictReader(stream))
            self.assertEqual(len(written), 2)
            self.assertEqual(written[1]["LevelId"], "newv1hhmy001cleans5")
            self.assertEqual(written[1]["TemplateId"], "aipuzzlehhmy001cleans5")
            self.assertEqual(written[1]["StrategyGroupId"], "1004")
            self.assertEqual(written[1]["KeepField"], "keep-me")
            self.assertEqual(written[1]["DupPara10"], "7|10|100|4")

    def test_clean_manifest_clones_the_uniquely_resolved_config_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config_root = root / "config-repo"
            config_dir = config_root / "csv"
            config_dir.mkdir(parents=True)
            header = [
                "LevelId", "TemplateId", "StrategyGroupId", "KeepField",
                *(f"DupPara{index}" for index in range(1, 11)),
            ]
            source = [
                "202", "hhmy001", "1003", "clone-from-resolved-source",
                *(["8|0|100|5"] * 10),
            ]
            with (config_dir / "config_level.csv").open(
                "w", encoding="utf-8-sig", newline=""
            ) as stream:
                writer = csv.writer(stream)
                writer.writerow(header)
                writer.writerow(source)
            with (config_dir / "config_level_list.csv").open(
                "w", encoding="utf-8-sig", newline=""
            ) as stream:
                writer = csv.writer(stream)
                writer.writerow(["LevelIndex", "LevelId"])
                writer.writerow([202, 202])
            profile = Profile("p", "test", str(root), str(root), str(root / "out"), str(config_root))
            rows = [{
                "source_template_id": "hhmy001clean",
                "config_source_template_id": "hhmy001",
                "config_source_match": "trailing_clean_alias",
                "output_template_id": "aipuzzlehhmy001cleans5",
                "level_ids": ["202"],
                "strategy_group_id": "1004",
                "duplicates": [
                    {"duplicate_index": index, "recommended_dup": f"7|{index}|100|4"}
                    for index in range(1, 11)
                ],
            }]

            with patch.object(write_service, "TOOL_ROOT", root / "tool", create=True):
                result = write_calibration_config(
                    profile, config_set_id="csv", calibration_rows=rows
                )

            self.assertEqual(result["appended_count"], 1)
            with (config_dir / "config_level.csv").open(
                "r", encoding="utf-8-sig", newline=""
            ) as stream:
                written = list(csv.DictReader(stream))
            self.assertEqual(len(written), 2)
            self.assertEqual(written[1]["TemplateId"], "aipuzzlehhmy001cleans5")
            self.assertEqual(written[1]["KeepField"], "clone-from-resolved-source")
            self.assertEqual(written[1]["StrategyGroupId"], "1004")


class ConfigRepositoryBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config_root = self.root / "config-repo"
        config_dir = self.config_root / "csv"
        config_dir.mkdir(parents=True)
        self.level_path = config_dir / "config_level.csv"
        with self.level_path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["LevelId", "TemplateId", *(f"DupPara{i}" for i in range(1, 11))])
            writer.writerow(["202", "source", *(["9|1|100|9"] * 10)])
        (config_dir / "config_level_list.csv").write_text("LevelIndex,LevelId\n202,202\n", encoding="utf-8")
        (config_dir / "unrelated.csv").write_bytes(b"do-not-touch\r\n")
        (config_dir / "config_level.csv.bak.old").write_bytes(b"existing-backup-do-not-touch")
        self.tool_root = self.root / "tool"
        self.tool_root.mkdir()
        self.profile = Profile("p", "test", None, None, str(self.root / "out"), str(self.config_root))
        self.rows = [{"source_template_id":"source", "output_template_id":"aipuzzlesources5", "duplicates":[
            {"duplicate_index":i, "recommended_dup":"8|1|100|8"} for i in range(1, 11)
        ]}]
        self.original = self.snapshot()
        root_patch = patch.object(write_service, "TOOL_ROOT", self.tool_root, create=True)
        root_patch.start()
        self.addCleanup(root_patch.stop)

    def snapshot(self):
        return {str(path.relative_to(self.config_root)):path.read_bytes() for path in self.config_root.rglob("*") if path.is_file()}

    def write(self):
        return write_calibration_config(self.profile, config_set_id="csv", calibration_rows=self.rows)

    def test_only_confirmed_config_file_changes_and_staging_stays_in_tool(self):
        self.level_path.chmod(0o640)
        replace = write_service.os.replace
        def checked_replace(source, destination):
            self.assertTrue(Path(source).resolve().is_relative_to(self.tool_root))
            self.assertEqual(Path(destination), self.level_path)
            return replace(source, destination)
        with patch.object(write_service.os, "replace", side_effect=checked_replace):
            result = self.write()
        after = self.snapshot()
        self.assertEqual(after.keys(), self.original.keys())
        self.assertEqual([name for name in after if after[name] != self.original[name]], ["csv/config_level.csv"])
        self.assertEqual(Path(result["backup_path"]).read_bytes(), self.original["csv/config_level.csv"])
        self.assertEqual(self.level_path.stat().st_mode & 0o777, 0o640)

    def test_repeated_writes_preserve_separate_complete_backups(self):
        first = self.write()
        before_second = self.level_path.read_bytes()
        second = self.write()
        self.assertNotEqual(first["backup_path"], second["backup_path"])
        self.assertEqual(Path(first["backup_path"]).read_bytes(), self.original["csv/config_level.csv"])
        self.assertEqual(Path(second["backup_path"]).read_bytes(), before_second)
        self.assertEqual(len(list(self.config_root.rglob("*.bak.*"))), 1)

    def test_backup_failure_does_not_modify_config(self):
        with patch.object(write_service.shutil, "copy2", side_effect=PermissionError("backup denied")):
            with self.assertRaises((OSError, InputValidationError)):
                self.write()
        self.assertEqual(self.snapshot(), self.original)

    def test_cross_filesystem_replace_fails_closed_without_repo_artifacts(self):
        with patch.object(write_service.os, "replace", side_effect=OSError(errno.EXDEV, "cross-device link")):
            with self.assertRaisesRegex(InputValidationError, "同一.*磁盘|跨.*文件系统"):
                self.write()
        self.assertEqual(self.snapshot(), self.original)
        self.assertFalse(list(self.tool_root.rglob("*.tmp")))
        backups = list(self.tool_root.rglob("*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original["csv/config_level.csv"])

    def test_refuses_tool_directory_nested_inside_config_repository(self):
        with patch.object(write_service, "TOOL_ROOT", self.config_root / "tool", create=True):
            with self.assertRaisesRegex(InputValidationError, "工具.*配置|配置.*工具"):
                self.write()
        self.assertEqual(self.snapshot(), self.original)
        self.assertFalse((self.config_root / "tool").exists())

    def test_symlinked_backup_directory_cannot_redirect_into_config_repo(self):
        runtime = self.tool_root / "运行数据"
        runtime.mkdir()
        (runtime / "配置备份").symlink_to(self.config_root, target_is_directory=True)
        with self.assertRaises(InputValidationError):
            self.write()
        self.assertEqual(self.snapshot(), self.original)

    def test_symlinked_backup_directory_cannot_escape_tool_directory(self):
        runtime = self.tool_root / "运行数据"
        runtime.mkdir()
        outside = self.root / "elsewhere"
        outside.mkdir()
        (runtime / "配置备份").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(InputValidationError):
            self.write()
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.snapshot(), self.original)


if __name__ == "__main__":
    unittest.main()
