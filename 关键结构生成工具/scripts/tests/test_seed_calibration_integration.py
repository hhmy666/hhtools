from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.difficulty import (  # noqa: E402
    DuplicateParameter,
    compare_duplicate_difficulty_pair,
)
from puzzle_core.legacy_materialization import (  # noqa: E402
    ClientTileCatalog,
    LevelTilePoolRange,
)
from services.consumption_calibration_service import (  # noqa: E402
    _legacy_seed_config_context,
    _whole_board_labels,
)
from services.parameter_recommendation_service import DupParam  # noqa: E402
from services.profile_service import Profile  # noqa: E402
from scripts.tests.test_difficulty import _level  # noqa: E402


class SeedCalibrationIntegrationTest(unittest.TestCase):
    def test_seed_changes_materialized_board_and_pair_evidence(self) -> None:
        level = _level()
        context = {
            "level_tile_rows": (
                LevelTilePoolRange(1, 0, (), tuple(range(1000, 1010))),
            ),
            "tile_catalog": ClientTileCatalog(
                tuple(range(1000, 1010)), tuple(range(1000, 1010))
            ),
            "source_paths": [
                "csv/config_client_level_tile.csv",
                "csv/config_client_tile.csv",
            ],
        }
        source_labels, source_meta = _whole_board_labels(
            level,
            (),
            DupParam(2, "0", "100", 2),
            level_index=200,
            config_context=context,
            candidate=False,
        )
        candidate_labels, candidate_meta = _whole_board_labels(
            level,
            (),
            DupParam(2, "1", "100", 2),
            level_index=200,
            config_context=context,
            candidate=True,
        )
        self.assertNotEqual(source_labels, candidate_labels)
        self.assertEqual(source_meta["mode"], "current_local_config_simulation")
        self.assertEqual(source_meta["random_seed_group_id"], 0)
        pair = compare_duplicate_difficulty_pair(
            level,
            (),
            source_duplicate=DuplicateParameter(2, "0", "100", 2),
            candidate_duplicate=DuplicateParameter(2, "1", "100", 2),
            duplicate_index=1,
            source_materialized_labels=source_labels,
            candidate_materialized_labels=candidate_labels,
            materialization_evidence={
                "mode": "current_local_config_simulation",
                "strategy_group_id": "1003",
                "random_seed_group_id": 0,
                "strategy_group_is_not_random_seed_group": True,
                "source": source_meta,
                "candidate": candidate_meta,
            },
        )
        evidence = pair["evidence"]
        self.assertEqual(
            evidence["materialization_fidelity"],
            "current_local_config_simulation",
        )
        self.assertNotEqual(
            evidence["source_label_sha256"], evidence["candidate_label_sha256"]
        )
        self.assertEqual(
            evidence["materialization"]["strategy_group_id"], "1003"
        )
        self.assertEqual(
            evidence["materialization"]["random_seed_group_id"], 0
        )
        self.assertTrue(
            evidence["materialization"][
                "strategy_group_is_not_random_seed_group"
            ]
        )

    def test_whole_board_materialization_forwards_structured_seed_group(self) -> None:
        level = _level()
        context = {
            "level_tile_rows": (
                LevelTilePoolRange(1, 0, (), tuple(range(1000, 1010))),
            ),
            "tile_catalog": ClientTileCatalog(
                tuple(range(1000, 1010)), tuple(range(1000, 1010))
            ),
            "source_paths": [],
        }

        with self.assertRaisesRegex(
            ValueError, "structured randomSeed_groupID"
        ):
            _whole_board_labels(
                level,
                (),
                DupParam(2, "0", "100", 2),
                level_index=200,
                config_context=context,
                candidate=False,
                random_seed_group_id=37,
            )

    def test_selected_config_directory_is_loaded_without_version_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = root / "csv"
            selected.mkdir()
            for name, header in (
                (
                    "config_level.csv",
                    ["LevelId", "TemplateId", *(f"DupPara{i}" for i in range(1, 11))],
                ),
                ("config_level_list.csv", ["LevelIndex", "LevelId"]),
            ):
                with (selected / name).open("w", encoding="utf-8", newline="") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(header)
                    writer.writerow(["string"] * len(header))
                    writer.writerow(["说明"] * len(header))
            (selected / "config_client_level_tile.csv").write_text(
                "Id,StartLevel,TileList1,TileList2\n"
                "int,int,array,array\n"
                "id,起点,列表1,列表2\n"
                "1,0,,1000|1001\n",
                encoding="utf-8",
            )
            (selected / "config_client_tile.csv").write_text(
                "TileId,Des,IconType,Board,Icon\n"
                "int,string,int,string,string\n"
                "Tile id,备注,类型,底板,图案\n"
                "1000,a,0,bg,a\n1001,b,0,bg,b\n",
                encoding="utf-8",
            )
            profile = Profile(
                "p", "test", str(root), str(root), str(root / "out"), str(root)
            )
            context, reason = _legacy_seed_config_context(profile, "csv")
            self.assertIsNone(reason)
            self.assertIsNotNone(context)
            self.assertEqual(
                context["source_paths"],
                [
                    "csv/config_client_level_tile.csv",
                    "csv/config_client_tile.csv",
                ],
            )

    def test_missing_seed_tables_return_fallback_reason_instead_of_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = root / "csv"
            selected.mkdir()
            for name, header in (
                (
                    "config_level.csv",
                    ["LevelId", "TemplateId", *(f"DupPara{i}" for i in range(1, 11))],
                ),
                ("config_level_list.csv", ["LevelIndex", "LevelId"]),
            ):
                with (selected / name).open("w", encoding="utf-8", newline="") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(header)
                    writer.writerow(["string"] * len(header))
                    writer.writerow(["说明"] * len(header))
            profile = Profile(
                "p", "test", str(root), str(root), str(root / "out"), str(root)
            )
            context, reason = _legacy_seed_config_context(profile, "csv")
            self.assertIsNone(context)
            self.assertIn("config_client_level_tile.csv", str(reason))

    def test_seed_config_os_error_does_not_expose_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = root / "csv"
            selected.mkdir()
            for name, header in (
                (
                    "config_level.csv",
                    ["LevelId", "TemplateId", *(f"DupPara{i}" for i in range(1, 11))],
                ),
                ("config_level_list.csv", ["LevelIndex", "LevelId"]),
            ):
                with (selected / name).open("w", encoding="utf-8", newline="") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(header)
                    writer.writerow(["string"] * len(header))
                    writer.writerow(["说明"] * len(header))
            for name in ("config_client_level_tile.csv", "config_client_tile.csv"):
                (selected / name).write_text("placeholder", encoding="utf-8")
            profile = Profile(
                "p", "test", str(root), str(root), str(root / "out"), str(root)
            )
            private_path = "/Users/example/private/config_client_level_tile.csv"
            with patch.object(
                Path,
                "read_text",
                side_effect=OSError(13, "Permission denied", private_path),
            ):
                context, reason = _legacy_seed_config_context(profile, "csv")

            self.assertIsNone(context)
            self.assertEqual(
                reason,
                "seed_config_read_failed: Seed 配置目录或文件不可读",
            )
            self.assertNotIn(private_path, str(reason))


if __name__ == "__main__":
    unittest.main()
