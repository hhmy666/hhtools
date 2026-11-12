from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.legacy_materialization import (  # noqa: E402
    ClientTileCatalog,
    LEGACY_EVIDENCE_LEVEL,
    LEGACY_MATERIALIZATION_VERSION,
    LevelTilePoolRange,
    materialize_legacy_ab_from_config,
    parse_client_tile_config,
    parse_level_tile_config,
    select_legacy_pattern_pool,
    select_level_tile_range,
)


LEVEL_ROWS = (
    LevelTilePoolRange(1, 0, (11, 12), (12, 13)),
    LevelTilePoolRange(2, 10, (21,), (22, 23)),
    LevelTilePoolRange(3, 20, (), (31, 32, 33)),
)
TILE_CATALOG = ClientTileCatalog(
    available_tile_ids=(11, 12, 13, 21, 22, 23, 31, 32, 33, 34, 35),
    normal_tile_ids=(11, 12, 13, 21, 22, 23, 31, 32, 33, 34, 35),
)


class LegacySeedReplayTest(unittest.TestCase):
    def test_client_algorithm_golden_pool_and_board(self) -> None:
        result = materialize_legacy_ab_from_config(
            level_index=11,
            level_tile_rows=LEVEL_ROWS,
            tile_catalog=TILE_CATALOG,
            a_positions=range(100, 112),
            b_positions=range(200, 209),
            dup_a_types=4,
            dup_b_types=3,
            dup_seed=7,
        )
        self.assertEqual(result.pattern_pool, (21, 22, 23, 11))
        self.assertEqual(
            tuple(tile_id for _, tile_id in result.a_assignments),
            (22, 23, 21, 11, 23, 11, 22, 21, 23, 22, 11, 21),
        )
        self.assertEqual(
            tuple(tile_id for _, tile_id in result.b_assignments),
            (23, 22, 22, 21, 21, 23, 22, 23, 21),
        )
        self.assertEqual(result.version, LEGACY_MATERIALIZATION_VERSION)
        self.assertEqual(result.evidence_level, LEGACY_EVIDENCE_LEVEL)
        self.assertEqual(result.selected_level_tile_id, 2)
        self.assertEqual(result.selected_level_start, 10)
        self.assertEqual(len(result.configuration_fingerprint), 64)
        self.assertTrue(result.client_source_refs)

    def test_level_segment_uses_highest_start_not_row_id_or_input_order(self) -> None:
        shuffled = (LEVEL_ROWS[2], LEVEL_ROWS[0], LEVEL_ROWS[1])
        self.assertEqual(select_level_tile_range(0, shuffled).row_id, 1)
        self.assertEqual(select_level_tile_range(19, shuffled).row_id, 2)
        self.assertEqual(select_level_tile_range(20, shuffled).row_id, 3)
        with self.assertRaisesRegex(ValueError, "no level-tile row"):
            select_level_tile_range(-1, shuffled)

    def test_csv_parsers_skip_ulink_metadata_and_keep_config_order(self) -> None:
        level_csv = """Id,StartLevel,TileList1,TileList2
int,int,array,array
id,第几关开始,Tile列表1,Tile列表2
1,0,,1003|1008
2,6,1006,1003|1008
"""
        tile_csv = """TileId,Des,IconType,Board,Icon
int,string,int,string,string
Tile id,备注,类型,底板,图案
1304,茄子,1,tile_bg,a
1000,蘑菇,0,tile_bg,b
1001,太阳,0,tile_bg,c
2200,活动,0,tile_bg,d
"""
        rows = parse_level_tile_config(level_csv)
        catalog = parse_client_tile_config(tile_csv)
        self.assertEqual(rows[1], LevelTilePoolRange(2, 6, (1006,), (1003, 1008)))
        self.assertEqual(catalog.available_tile_ids, (1304, 1000, 1001, 2200))
        self.assertEqual(catalog.normal_tile_ids, (1304, 1000, 1001))

    def test_pool_selection_deduplicates_and_fails_when_unique_pool_is_short(self) -> None:
        rows = (LevelTilePoolRange(1, 0, (11, 11), (11, 12)),)
        selected = select_legacy_pattern_pool(
            level_index=0,
            dup_a_types=2,
            dup_b_types=1,
            dup_seed=0,
            level_tile_rows=rows,
            available_tile_ids=(11, 12),
            normal_tile_ids=(11, 12),
        )
        self.assertEqual(set(selected.pattern_pool), {11, 12})
        self.assertEqual(len(selected.pattern_pool), 2)
        with self.assertRaisesRegex(ValueError, "fewer than required"):
            select_legacy_pattern_pool(
                level_index=0,
                dup_a_types=3,
                dup_b_types=1,
                dup_seed=0,
                level_tile_rows=rows,
                available_tile_ids=(11, 12),
                normal_tile_ids=(11, 12),
            )

    def test_iap_reduces_only_a_after_pool_selection(self) -> None:
        result = materialize_legacy_ab_from_config(
            level_index=11,
            level_tile_rows=LEVEL_ROWS,
            tile_catalog=TILE_CATALOG,
            a_positions=range(12),
            b_positions=range(100, 112),
            dup_a_types=4,
            dup_b_types=2,
            dup_seed=7,
            iap_protect_cut_a_count=3,
        )
        self.assertEqual(len(result.pattern_pool), 4)  # original max(A, B)
        self.assertEqual(result.effective_a_types, 1)
        self.assertEqual(result.effective_b_types, 2)
        self.assertEqual(len({tile_id for _, tile_id in result.a_assignments}), 1)
        self.assertEqual(len({tile_id for _, tile_id in result.b_assignments}), 2)
        self.assertEqual(result.iap_protect_cut_a_count, 3)

    def test_a_and_b_restart_rng_and_all_inputs_are_deterministic(self) -> None:
        kwargs = dict(
            level_index=11,
            level_tile_rows=LEVEL_ROWS,
            tile_catalog=TILE_CATALOG,
            a_positions=range(0, 6),
            b_positions=range(100, 106),
            dup_a_types=2,
            dup_b_types=2,
            dup_seed=3,
        )
        first = materialize_legacy_ab_from_config(**kwargs)
        second = materialize_legacy_ab_from_config(**kwargs)
        self.assertEqual(first, second)
        self.assertEqual(
            tuple(tile_id for _, tile_id in first.a_assignments),
            tuple(tile_id for _, tile_id in first.b_assignments),
        )
        changed = materialize_legacy_ab_from_config(**(kwargs | {"dup_seed": 4}))
        self.assertNotEqual(first.configuration_fingerprint, changed.configuration_fingerprint)

    def test_structured_seed_group_is_rejected_before_replay(self) -> None:
        with self.assertRaisesRegex(ValueError, "structured randomSeed_groupID"):
            materialize_legacy_ab_from_config(
                level_index=11,
                level_tile_rows=LEVEL_ROWS,
                tile_catalog=TILE_CATALOG,
                a_positions=(0, 1, 2),
                b_positions=(3, 4, 5),
                dup_a_types=1,
                dup_b_types=1,
                dup_seed=0,
                random_seed_group_id=1003,
            )


if __name__ == "__main__":
    unittest.main()
