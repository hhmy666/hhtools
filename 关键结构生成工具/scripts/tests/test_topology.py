from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.geometry import build_static_geometry  # noqa: E402
from puzzle_core.models import (  # noqa: E402
    Layout,
    RawOutputPlan,
    RawPuzzleUnit,
    StepSpec,
    TowerMode,
    UnitRole,
)
from puzzle_core.planning import normalize_output_plan  # noqa: E402
from puzzle_core.protobuf_wire import encode_varint  # noqa: E402
from puzzle_core.search import (  # noqa: E402
    GenerationOptions,
    UnitGenerationOptions,
    search_candidates,
)
from puzzle_core.template_reader import parse_level_bytes  # noqa: E402
from puzzle_core.topology import (  # noqa: E402
    TopologyUnit,
    analyze_existing_structure_isolation,
    analyze_topology,
)
from puzzle_core.validator import validate_candidate  # noqa: E402


def _varint_field(number: int, value: int) -> bytes:
    return encode_varint(number << 3) + encode_varint(value)


def _bytes_field(number: int, value: bytes) -> bytes:
    return encode_varint((number << 3) | 2) + encode_varint(len(value)) + value


def _level(
    tiles: list[tuple[int, int, int, int]],
    *,
    width: int = 12,
    height: int = 6,
    template_id: str = "topology",
):
    payload = [
        _bytes_field(2, template_id.encode("utf-8")),
        _varint_field(4, width),
        _varint_field(5, height),
    ]
    for x, y, z, tile_id in tiles:
        tile = b"".join(
            (
                _varint_field(1, x),
                _varint_field(2, y),
                _varint_field(3, z),
                _varint_field(4, tile_id),
            )
        )
        payload.append(_bytes_field(6, tile))
    return parse_level_bytes(b"".join(payload))


def _unit(
    unit_id: str,
    role: UnitRole,
    selected: tuple[int, ...],
    step: int = 5,
) -> TopologyUnit:
    return TopologyUnit(unit_id, role, step, selected)


class ParallelTopologyTests(unittest.TestCase):
    def test_parallel_units_pass_only_with_separate_influence_and_entries(self) -> None:
        level = _level([(0, 0, 1, -10), (8, 0, 1, -11)])
        report = analyze_topology(
            level,
            build_static_geometry(level),
            Layout.X,
            (
                _unit("left", UnitRole.LEFT, (0,)),
                _unit("right", UnitRole.RIGHT, (1,)),
            ),
        )
        self.assertTrue(report.passed)
        self.assertEqual(report.influence_intersection_count, 0)
        self.assertEqual(
            tuple(unit.initial_entry_count for unit in report.unit_reports), (1, 1)
        )

    def test_shared_nested_ab_breaks_parallel_independence(self) -> None:
        # The two structures do not overlap one another, but both cover tile 2.
        level = _level(
            [(3, 0, 2, -10), (5, 0, 2, -11), (4, 0, 1, -10)], width=10
        )
        report = analyze_topology(
            level,
            build_static_geometry(level),
            Layout.X,
            (
                _unit("left", UnitRole.LEFT, (0,)),
                _unit("right", UnitRole.RIGHT, (1,)),
            ),
        )
        self.assertFalse(report.passed)
        self.assertIn("parallel_influence_overlap", report.reasons)
        self.assertEqual(report.projection_overlap_pairs, 0)


class SerialTopologyTests(unittest.TestCase):
    def test_forward_unlock_is_a_valid_serial_topology(self) -> None:
        level = _level([(0, 0, 2, -10), (1, 0, 1, -11)])
        report = analyze_topology(
            level,
            build_static_geometry(level),
            Layout.Z,
            (
                _unit("high", UnitRole.HIGH, (0,), 5),
                _unit("low", UnitRole.LOW, (1,), 6),
            ),
        )
        self.assertTrue(report.passed)
        self.assertGreater(report.direct_u1_to_u2, 0)
        self.assertEqual(report.u2_new_entries_after_u1, (1,))

    def test_independent_lower_chain_cannot_masquerade_as_serial(self) -> None:
        level = _level([(0, 0, 2, -10), (5, 0, 1, -11)])
        report = analyze_topology(
            level,
            build_static_geometry(level),
            Layout.Z,
            (
                _unit("high", UnitRole.HIGH, (0,)),
                _unit("low", UnitRole.LOW, (1,)),
            ),
        )
        self.assertFalse(report.passed)
        self.assertIn("serial_missing_forward_dependency", report.reasons)
        self.assertIn("u2_has_no_new_entry_after_u1", report.reasons)

    def test_shared_transition_ab_is_rejected_as_ambiguous_ownership(self) -> None:
        level = _level(
            [
                (0, 0, 3, -10),  # high structure
                (1, 0, 1, -11),  # low structure
                (0, 0, 2, -10),  # nested by high and blocks low
            ]
        )
        report = analyze_topology(
            level,
            build_static_geometry(level),
            Layout.Z,
            (
                _unit("high", UnitRole.HIGH, (0,)),
                _unit("low", UnitRole.LOW, (1,)),
            ),
        )
        self.assertFalse(report.passed)
        self.assertIn("serial_ambiguous_ab_ownership", report.reasons)
        self.assertIn("u2_has_no_entry_after_u1", report.reasons)

    def test_serial_difficulty_policy_is_checked_against_layer_order(self) -> None:
        level = _level([(0, 0, 2, -10), (1, 0, 1, -11)])
        report = analyze_topology(
            level,
            build_static_geometry(level),
            Layout.Z,
            (
                _unit("high", UnitRole.HIGH, (0,), 6),
                _unit("low", UnitRole.LOW, (1,), 5),
            ),
        )
        self.assertFalse(report.passed)
        self.assertIn("serial_difficulty_order_mismatch", report.reasons)


class LegacyIsolationTests(unittest.TestCase):
    def test_existing_cn_and_new_unit_may_not_share_ab_influence(self) -> None:
        level = _level(
            [
                (0, 0, 2, -12),  # existing C
                (2, 0, 2, -10),  # selected replacement
                (1, 0, 1, -11),  # nested by both groups
            ]
        )
        report = analyze_existing_structure_isolation(
            level,
            build_static_geometry(level),
            (_unit("single", UnitRole.SINGLE, (1,)),),
            existing_structure_indices=(0,),
        )
        self.assertFalse(report.passed)
        conflict = report.unit_conflicts[0]
        self.assertEqual(conflict.projection_overlap_pairs, 0)
        self.assertEqual(conflict.shared_ab_count, 1)
        self.assertGreater(conflict.influence_intersection_count, 0)


class SearchTopologyIntegrationTests(unittest.TestCase):
    @staticmethod
    def _serial_chain_level(*, connected: bool):
        tiles = []
        for offset in range(9):
            tiles.append((offset, 1, 9 + offset, -10 if offset % 2 == 0 else -11))
        low_x = (lambda offset: 9 - offset) if connected else (lambda offset: 24 - offset)
        for offset in range(9):
            tiles.append((low_x(offset), 1, offset, -10 if offset % 2 == 0 else -11))
        return _level(tiles, width=32, height=4, template_id="serialchain")

    @staticmethod
    def _serial_plan():
        return normalize_output_plan(
            RawOutputPlan(
                plan_id="s4s4",
                units=(
                    RawPuzzleUnit(StepSpec.exact(4), "U1"),
                    RawPuzzleUnit(StepSpec.exact(4), "U2"),
                ),
                layout=Layout.Z,
                tower_mode=TowerMode.DISALLOW,
            ),
            batch_seed=1,
            template_key="serialchain",
        )[0]

    @staticmethod
    def _serial_options(level):
        return GenerationOptions(
            unit_options=(
                UnitGenerationOptions(
                    type_count=3,
                    preferred_coords=tuple(
                        (tile.x, tile.y, tile.z) for tile in level.tiles[:9]
                    ),
                ),
                UnitGenerationOptions(
                    type_count=3,
                    preferred_coords=tuple(
                        (tile.x, tile.y, tile.z) for tile in level.tiles[9:]
                    ),
                ),
            ),
            replacement_sources=("A", "B"),
            attempt_budget=3,
            assignment_attempts=600,
        )

    def test_search_and_validator_accept_forward_unlock_serial_candidate(self) -> None:
        level = self._serial_chain_level(connected=True)
        plan = self._serial_plan()
        options = self._serial_options(level)
        candidate = search_candidates(
            level, plan, options, candidate_seed=456
        ).candidates[0]
        # This fixture isolates serial topology; formal-export experience proof is
        # covered by the export integration tests with exact DupPara1-10 data.
        self.assertTrue(
            validate_candidate(
                level,
                plan,
                options,
                candidate,
                require_exportable_experience=False,
            ).passed
        )

    def test_independent_low_chain_remains_reviewable_but_not_recommended(self) -> None:
        level = self._serial_chain_level(connected=False)
        plan = self._serial_plan()
        options = self._serial_options(level)
        searched = search_candidates(
            level,
            plan,
            options,
            candidate_seed=456,
        )
        report = validate_candidate(
            level,
            plan,
            options,
            searched.candidates[0],
            require_exportable_experience=False,
        )

        self.assertGreaterEqual(
            searched.rejection_counts[
                "rule_warning_topology_serial_missing_forward_dependency"
            ],
            1,
        )
        self.assertFalse(report.passed)
        self.assertTrue(report.technical_integrity_passed)
        self.assertTrue(report.manual_export_eligible)
        self.assertFalse(report.auto_recommendation_eligible)
        self.assertIn(
            "unit_topology",
            {check.code for check in report.rule_failures},
        )


if __name__ == "__main__":
    unittest.main()
