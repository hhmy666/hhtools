from __future__ import annotations

import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.template_reader import LevelTemplate, Tile, Tower, parse_level_file
from puzzle_core.tower_reader import (
    A_TILE_ID,
    B_TILE_ID,
    TowerAllocationError,
    allocate_only_mode,
    diagnose_towers,
)


_FIXTURE_ENV = "AI_PUZZLE_TEST_TEMPLATE_DIR"


def _fixture(test: unittest.TestCase, template_id: str) -> LevelTemplate:
    configured = os.environ.get(_FIXTURE_ENV)
    if not configured:
        test.skipTest(f"optional production fixtures require {_FIXTURE_ENV}")
    path = Path(configured).expanduser() / f"{template_id}_m.bytes"
    if not path.is_file():
        test.skipTest(f"read-only tile fixture is unavailable: {path}")
    return parse_level_file(path)


def _tile(
    index: int,
    x: int,
    y: int,
    z: int,
    tile_id: int = A_TILE_ID,
    *,
    tower_index: int | None = None,
) -> Tile:
    return Tile(
        index=index,
        top_field_index=index,
        x=x,
        y=y,
        z=z,
        tile_id=tile_id,
        debuff_data_index=None,
        fields=(),
        raw=b"",
        tower_index=tower_index,
    )


def _tower(index: int, x: int, y: int, direction: int = 0) -> Tower:
    return Tower(
        index=index,
        top_field_index=index,
        x=x,
        y=y,
        direction=direction,
        fields=(),
        raw=b"",
    )


def _template(
    tiles: tuple[Tile, ...],
    towers: tuple[Tower, ...],
    *,
    width: int = 8,
    height: int = 8,
) -> LevelTemplate:
    return LevelTemplate(
        data=b"",
        fields=(),
        template_id="synthetic",
        template_id_field_index=0,
        tiles=tiles,
        towers=towers,
        level_width=width,
        level_height=height,
    )


class TowerFixtureTest(unittest.TestCase):
    def test_mx0011_uses_explicit_anchor_membership(self) -> None:
        template = _fixture(self, "mx0011")
        report = diagnose_towers(template)

        self.assertEqual(report.tower_count, 3)
        self.assertEqual(
            [tower.anchor for tower in report.towers],
            [(6, 13), (8, 13), (10, 13)],
        )
        self.assertEqual(
            [tower.actual_member_count for tower in report.towers], [3, 3, 3]
        )
        self.assertEqual([tower.a_count for tower in report.towers], [3, 3, 3])
        self.assertEqual([tower.b_count for tower in report.towers], [0, 0, 0])
        self.assertEqual(len(report.tower_member_indices), 9)
        self.assertEqual(len(report.non_tower_indices), 12)

    def test_model3v2_defaults_direction_and_reports_existing_g_gap(self) -> None:
        template = _fixture(self, "model3v2")
        report = diagnose_towers(template)

        self.assertEqual(report.tower_count, 1)
        tower = report.towers[0]
        self.assertFalse(
            any(field.number == 3 for field in template.towers[0].fields)
        )
        self.assertEqual(tower.direction, 0)
        self.assertEqual(tower.z_values, (0, 1, 3))
        self.assertEqual(tower.missing_z_values, (2,))
        self.assertTrue(tower.has_z_gap)
        self.assertEqual(tower.existing_structure_counts, {"G": 3})
        self.assertIn("tower_z_gap", {warning.code for warning in report.warnings})

    def test_tower0002_has_real_capacity_and_all_four_directions(self) -> None:
        template = _fixture(self, "tower0002")
        report = diagnose_towers(template)

        self.assertEqual(report.tower_count, 6)
        self.assertEqual(
            sorted(tower.actual_member_count for tower in report.towers),
            [4, 4, 8, 8, 8, 8],
        )
        self.assertEqual({tower.direction for tower in report.towers}, {0, 1, 2, 3})

    def test_n0016_omitted_tile_z_is_read_as_zero_and_has_no_tower(self) -> None:
        template = _fixture(self, "n0016")
        report = diagnose_towers(template)

        self.assertEqual(report.tower_count, 0)
        self.assertFalse(report.tower_member_indices)
        self.assertEqual(len(report.non_tower_indices), len(template.tiles))
        omitted_z = tuple(
            tile
            for tile in template.tiles
            if not any(field.number == 3 for field in tile.fields)
        )
        self.assertGreaterEqual(len(omitted_z), 13)
        self.assertTrue(all(tile.z == 0 for tile in omitted_z))


class TowerDiagnosticTest(unittest.TestCase):
    def test_duplicate_anchor_is_last_wins_and_warned(self) -> None:
        towers = (_tower(0, 2, 3), _tower(1, 2, 3, direction=2))
        # Deliberately stale reader associations prove diagnose_towers
        # recomputes membership from the explicit anchors.
        tiles = (
            _tile(0, 2, 3, 0, tower_index=0),
            _tile(1, 2, 3, 1, tile_id=B_TILE_ID, tower_index=0),
            _tile(2, 6, 6, 0),
        )
        report = diagnose_towers(_template(tiles, towers))

        self.assertTrue(report.towers[0].is_empty)
        self.assertEqual(report.towers[1].member_indices, (0, 1))
        self.assertEqual(report.towers[1].a_count, 1)
        self.assertEqual(report.towers[1].b_count, 1)
        self.assertEqual(report.tower_index_for_tile(0), 1)
        codes = [warning.code for warning in report.warnings]
        self.assertIn("duplicate_stackdata_anchor", codes)
        self.assertIn("reader_membership_mismatch", codes)

    def test_empty_gap_out_of_bounds_and_existing_types_are_exposed(self) -> None:
        towers = (_tower(0, 1, 1), _tower(1, 8, 1), _tower(2, 4, 4))
        tiles = (
            _tile(0, 1, 1, 0),
            _tile(1, 1, 1, 2, tile_id=-12),
            _tile(2, 1, 1, 3, tile_id=-23),
            _tile(3, 8, 1, 0),
        )
        report = diagnose_towers(_template(tiles, towers))

        self.assertEqual(report.towers[0].missing_z_values, (1,))
        self.assertEqual(
            report.towers[0].existing_structure_counts, {"C": 1, "N": 1}
        )
        self.assertTrue(report.towers[1].is_out_of_bounds)
        self.assertTrue(report.towers[2].is_empty)
        codes = {warning.code for warning in report.warnings}
        self.assertTrue(
            {"tower_z_gap", "tower_anchor_out_of_bounds", "empty_tower"}
            <= codes
        )


class TowerOnlyAllocationTest(unittest.TestCase):
    def test_balances_counts_and_spreads_each_type_across_towers(self) -> None:
        template = _fixture(self, "mx0011")
        report = diagnose_towers(template)
        selected = tuple(sorted(report.tower_member_indices))

        allocation = allocate_only_mode(
            template,
            selected,
            {"C": 3, "D": 3, "E": 3},
        )

        self.assertEqual(
            [bucket.assigned_count for bucket in allocation.towers], [3, 3, 3]
        )
        self.assertTrue(allocation.strict_balance_feasible)
        self.assertTrue(allocation.balanced_within_one)
        self.assertEqual(allocation.max_tower_count_difference, 0)
        self.assertEqual(
            allocation.structure_type_tower_spread, {"C": 3, "D": 3, "E": 3}
        )
        self.assertEqual(set(allocation.assigned_indices), set(selected))
        self.assertFalse(allocation.calculates_steps)
        self.assertIsNone(allocation.structure_step_count)
        self.assertIsNone(allocation.complete_solution_count)
        self.assertEqual(allocation.step_status, "N/A（塔内实时调控）")

    def test_capacity_saturation_reports_unavoidable_imbalance(self) -> None:
        towers = (_tower(0, 0, 0), _tower(1, 1, 0), _tower(2, 2, 0))
        tiles = tuple(
            [_tile(0, 0, 0, 0), _tile(1, 1, 0, 0)]
            + [_tile(index, 2, 0, index - 2) for index in range(2, 9)]
        )
        template = _template(tiles, towers)

        allocation = allocate_only_mode(
            template,
            range(len(tiles)),
            {"C": 3, "D": 3},
        )

        self.assertEqual(
            [bucket.assigned_count for bucket in allocation.towers], [1, 1, 4]
        )
        self.assertFalse(allocation.strict_balance_feasible)
        self.assertFalse(allocation.balanced_within_one)
        self.assertEqual(allocation.max_tower_count_difference, 3)
        self.assertIsNotNone(allocation.imbalance_reason)

    def test_rejects_non_multiple_counts_and_non_tower_selection(self) -> None:
        template = _template(
            (_tile(0, 1, 1, 0), _tile(1, 2, 2, 0)),
            (_tower(0, 1, 1),),
        )
        with self.assertRaises(TowerAllocationError) as count_error:
            allocate_only_mode(template, (0,), {"C": 2})
        self.assertEqual(
            count_error.exception.reason,
            "structure_count_not_multiple_of_three",
        )

        with self.assertRaises(TowerAllocationError) as member_error:
            allocate_only_mode(template, (0, 1, 1), {"C": 3})
        self.assertEqual(member_error.exception.reason, "duplicate_selected_index")

        enough = replace(
            template,
            tiles=(
                _tile(0, 1, 1, 0),
                _tile(1, 1, 1, 1),
                _tile(2, 2, 2, 0),
            ),
        )
        with self.assertRaises(TowerAllocationError) as non_tower_error:
            allocate_only_mode(enough, (0, 1, 2), {"C": 3})
        self.assertEqual(
            non_tower_error.exception.reason, "selected_tile_not_in_tower"
        )

    def test_explicit_minus_one_is_a_valid_no_debuff_sentinel(self) -> None:
        towers = (_tower(0, 1, 1),)
        tiles = tuple(
            replace(_tile(index, 1, 1, index), debuff_data_index=-1)
            for index in range(3)
        )
        template = _template(tiles, towers)

        report = diagnose_towers(template)
        allocation = allocate_only_mode(template, (0, 1, 2), {"C": 3})

        self.assertEqual(report.towers[0].replaceable_ab_indices, (0, 1, 2))
        self.assertEqual(allocation.assigned_indices, (0, 1, 2))


if __name__ == "__main__":
    unittest.main()
