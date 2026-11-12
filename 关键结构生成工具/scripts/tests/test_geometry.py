"""Tests for 2x2 footprints, blocker closure, and nesting metrics."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.geometry import (  # noqa: E402
    GeometryError,
    audit_structure_coverage,
    build_geometry,
    build_static_geometry,
    direct_lower_full_cover_relations,
    footprint,
    layer,
    layer_for_z,
    nesting_stats,
    overlap_area,
    overlap_fraction,
    tower_external_full_cover_relations,
)
from puzzle_core.protobuf_wire import encode_varint  # noqa: E402
from puzzle_core.template_reader import parse_level_bytes  # noqa: E402


def varint_field(number: int, value: int) -> bytes:
    return encode_varint(number << 3) + encode_varint(value)


def bytes_field(number: int, payload: bytes) -> bytes:
    return encode_varint((number << 3) | 2) + encode_varint(len(payload)) + payload


def tile_payload(x: int, y: int, z: int, tile_id: int) -> bytes:
    return b"".join(
        (
            varint_field(1, x),
            varint_field(2, y),
            varint_field(3, z),
            varint_field(4, tile_id),
        )
    )


def tower_payload(x: int, y: int, direction: int = 0) -> bytes:
    return b"".join(
        (varint_field(1, x), varint_field(2, y), varint_field(3, direction))
    )


def make_level(
    tiles: list[tuple[int, int, int, int]],
    towers: list[tuple[int, int, int]] | None = None,
):
    parts = [bytes_field(2, b"geometry_fixture")]
    parts.extend(bytes_field(6, tile_payload(*tile)) for tile in tiles)
    parts.extend(bytes_field(7, tower_payload(*tower)) for tower in (towers or []))
    return parse_level_bytes(b"".join(parts))


class FootprintTests(unittest.TestCase):
    def test_standard_tile_uses_four_cells_and_exact_overlap_areas(self) -> None:
        level = make_level(
            [
                (0, 0, 0, -10),
                (0, 0, 1, -10),
                (1, 0, 1, -10),
                (1, 1, 1, -10),
                (2, 0, 1, -10),
            ]
        )
        self.assertEqual(
            footprint(level.tiles[0]),
            frozenset(((0, 0), (1, 0), (0, 1), (1, 1))),
        )
        self.assertEqual(overlap_area(level.tiles[0], level.tiles[1]), 4)
        self.assertEqual(overlap_area(level.tiles[0], level.tiles[2]), 2)
        self.assertEqual(overlap_area(level.tiles[0], level.tiles[3]), 1)
        self.assertEqual(overlap_area(level.tiles[0], level.tiles[4]), 0)
        self.assertEqual(overlap_fraction(level.tiles[0], level.tiles[3]), 0.25)

    def test_product_layer_is_z_plus_one(self) -> None:
        level = make_level([(1, 1, 6, -10)])
        self.assertEqual(layer_for_z(6), 7)
        self.assertEqual(layer(level.tiles[0]), 7)
        self.assertEqual(level.tiles[0].layer, 7)


class BlockerGraphTests(unittest.TestCase):
    def test_direct_and_transitive_blockers_include_intermediate_tile(self) -> None:
        # 0 overlaps 1, and 1 overlaps 2, while 0 does not overlap 2.
        level = make_level(
            [
                (0, 0, 0, -10),
                (1, 0, 1, -11),
                (2, 0, 2, -10),
                (0, 0, 0, -11),  # same Z never blocks tile 0
            ]
        )
        graph = build_geometry(level)
        self.assertEqual(graph.direct_blockers[0], frozenset((1,)))
        self.assertEqual(graph.direct_blockers[1], frozenset((2,)))
        self.assertEqual(graph.transitive_blockers[0], frozenset((1, 2)))
        self.assertNotIn(3, graph.direct_blockers[0])
        self.assertIn(0, graph.direct_blocked_tiles[1])

    def test_static_graph_excludes_every_relation_involving_tower_member(self) -> None:
        level = make_level(
            [
                (0, 0, 0, -10),
                (0, 0, 1, -11),
                (5, 5, 0, -10),
                (5, 5, 1, -11),
            ],
            towers=[(5, 5, 0)],
        )
        complete = build_geometry(level)
        static = build_static_geometry(level)
        self.assertEqual(complete.direct_blockers[2], frozenset((3,)))
        self.assertEqual(static.direct_blockers[2], frozenset())
        self.assertEqual(static.included_indices, frozenset((0, 1)))


class NestingTests(unittest.TestCase):
    def test_unique_ab_counts_are_distinct_from_relation_counts(self) -> None:
        level = make_level(
            [
                (0, 0, 0, -10),  # nested A
                (1, 0, 0, -11),  # nested B
                (1, 1, 2, -10),  # selected: quarter over A, half over B
                (0, 0, 3, -11),  # selected: full over A, half over B
                (2, 1, 4, -10),  # A above selected index 2 (reverse blocker)
            ]
        )
        stats = nesting_stats(level, (2, 3))
        self.assertEqual(stats.nested_a_indices, frozenset((0,)))
        self.assertEqual(stats.nested_b_indices, frozenset((1,)))
        self.assertEqual(stats.nested_a_count, 1)
        self.assertEqual(stats.nested_b_count, 1)
        self.assertEqual(stats.quarter_relation_count, 1)
        self.assertEqual(stats.half_relation_count, 2)
        self.assertEqual(stats.full_relation_count, 1)
        self.assertEqual(stats.blocking_a_indices, frozenset((4,)))
        self.assertEqual(stats.blocking_a_count, 1)
        # A single selected tile already fully covers A; this is not reported
        # again as a multi-tile joint full cover.
        self.assertNotIn(0, stats.joint_full_lower_indices)

    def test_combined_full_cover_is_reported_but_not_a_single_full_relation(self) -> None:
        level = make_level(
            [
                (0, 0, 0, -10),  # lower A
                (-1, 0, 1, -11),  # selected left half
                (1, 0, 1, -11),  # selected right half
            ]
        )
        stats = nesting_stats(level, (1, 2))
        self.assertEqual(stats.full_relation_count, 0)
        self.assertEqual(stats.half_relation_count, 2)
        self.assertEqual(stats.joint_full_lower_indices, frozenset((0,)))

    def test_tower_external_single_full_cover_rule_excludes_tower_pairs(self) -> None:
        level = make_level(
            [
                (0, 0, 0, -10),
                (0, 0, 1, -11),
                (5, 5, 0, -10),
                (5, 5, 1, -11),
            ],
            towers=[(5, 5, 2)],
        )
        relations = tower_external_full_cover_relations(level)
        self.assertEqual(len(relations), 1)
        self.assertEqual((relations[0].upper_index, relations[0].lower_index), (1, 0))
        self.assertEqual(
            tower_external_full_cover_relations(level, selected_indices=(1,)),
            relations,
        )
        with self.assertRaisesRegex(GeometryError, "tower member"):
            tower_external_full_cover_relations(level, selected_indices=(3,))

    def test_forbidden_full_cover_is_bidirectional_and_next_layer_only(self) -> None:
        level = make_level(
            [
                (0, 0, 0, -10),
                (0, 0, 1, -10),  # selected; directly covers index 0
                (3, 0, 0, -10),
                (3, 0, 2, -10),  # selected; two layers above index 2
                (6, 0, 2, -10),
                (6, 0, 3, -11),  # covers selected index 4 from above
            ]
        )
        direct = direct_lower_full_cover_relations(level, (1, 3, 4))
        self.assertEqual(
            [(item.upper_index, item.lower_index) for item in direct],
            [(1, 0), (5, 4)],
        )

    def test_nesting_rejects_tower_member_in_static_unit(self) -> None:
        level = make_level(
            [(5, 5, 0, -10), (5, 5, 1, -11)], towers=[(5, 5, 0)]
        )
        with self.assertRaisesRegex(GeometryError, "tower member"):
            nesting_stats(level, (1,))

    def test_formal_coverage_audit_has_relation_endpoints_and_categories(self) -> None:
        level = make_level(
            [
                (0, 0, 0, -10),
                (-1, 0, 1, -10),  # selected C: half over index 0
                (1, 0, 1, -11),  # selected D: half over index 0
                (4, 0, 0, -10),
                (4, 0, 2, -11),  # selected E: cross-layer single full
            ]
        )

        audit = audit_structure_coverage(level, {1: "C", 2: "D", 4: "E"})

        self.assertEqual(audit.adjacent_single_full_count, 0)
        self.assertEqual(audit.cross_layer_single_full_count, 1)
        self.assertEqual(audit.joint_full_cover_count, 1)
        relation = audit.cross_layer_single_full_relations[0]
        self.assertEqual(relation.upper_index, 4)
        self.assertEqual(relation.lower_index, 3)
        self.assertEqual(relation.upper_effective_type, "E")
        self.assertEqual(relation.lower_effective_type, "A")
        self.assertEqual(relation.upper_xyz, (4, 0, 2))
        self.assertEqual(relation.lower_xyz, (4, 0, 0))
        self.assertEqual(relation.z_delta, 2)
        self.assertEqual(relation.overlap_fraction, 1.0)
        joint = audit.joint_full_covers[0]
        self.assertEqual(joint.lower_index, 0)
        self.assertEqual(joint.contributor_indices, (1, 2))
        self.assertEqual(
            {item.category for item in joint.contributor_relations},
            {"joint_full_contributor"},
        )

    def test_formal_coverage_audit_records_tower_full_as_diagnostic(self) -> None:
        level = make_level(
            [(5, 5, 0, -10), (5, 5, 1, -11)], towers=[(5, 5, 0)]
        )
        audit = audit_structure_coverage(level, {1: "C"})

        self.assertEqual(audit.selected_static_structure_indices, ())
        self.assertEqual(audit.selected_tower_structure_indices, (1,))
        self.assertEqual(audit.adjacent_single_full_count, 0)
        self.assertEqual(len(audit.tower_initial_full_relations), 1)
        self.assertEqual(
            audit.tower_initial_full_relations[0].category,
            "tower_initial_single_full",
        )


if __name__ == "__main__":
    unittest.main()
