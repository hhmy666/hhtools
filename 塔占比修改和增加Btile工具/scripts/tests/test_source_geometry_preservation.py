"""Wire history remains immutable; every final Tile obeys structural rules."""

from dataclasses import replace
import unittest

from scripts.tests.test_rewrite import synthetic, computed_plan
from scripts.tower_reducer.distribution import DistributionOptions, plan_tower_distribution
from scripts.tower_reducer.inspection import inspect_template
from scripts.tower_reducer.rewrite import rewrite_template
from scripts.tower_reducer.template_reader import decode_template, encode_template
from scripts.tower_reducer.verification import verify_output


def historical_source(kind):
    raw, _ = synthetic()
    document = decode_template(raw)
    if kind == "cross_layer":
        document.tiles[3].z = 3  # Covers Z=0, not the empty immediate Z=2.
    elif kind == "same_layer_overlap":
        document.tiles[1].x = 5  # Original lower pair overlaps; neither is moved.
    elif kind == "outside_board":
        document.tiles[0].x = 16
    elif kind == "negative_z":
        document.tiles[0].z = -1
    return encode_template(document)


def historical_rewrite_plan(raw):
    # Intentionally supply a plan from the valid geometry to test the rewrite
    # and independent verifier, even when the planner now rejects this history.
    base, _ = synthetic()
    plan = computed_plan(base)
    document = decode_template(raw)
    distribution = plan_tower_distribution(document, inspect_template(document), DistributionOptions())
    return replace(plan, distribution=distribution, placement=replace(plan.placement,
        strategy_version='orbit-v2-added-support-sha256'))


class PreserveSourceGeometryTests(unittest.TestCase):
    def test_rename_only_does_not_require_new_position_board_information(self):
        raw, _ = synthetic()
        document = decode_template(raw)
        raw = b"".join(field.raw for field in document.fields if field.number not in (4, 5, 7))
        plan = computed_plan(raw)
        self.assertTrue(plan.placement.ok, plan.placement)
        self.assertEqual(plan.distribution.move_count, 0)
        output = rewrite_template(raw, plan, "lessTsample")
        report = verify_output(raw, output, plan, "lessTsample")
        # Source-only one-on-one pairs may remain after the user's approved
        # baseline exception; renaming must not disguise them as new geometry.
        self.assertTrue(report.ok, report.errors)
        self.assertIn('inherited_full_cover',{issue.code for issue in report.warnings})
        self.assertTrue(report.facts['strict_structure']['no_new_full_cover'])
        self.assertEqual([tile.fields for tile in decode_template(raw).tiles],
                         [tile.fields for tile in decode_template(output).tiles])

    def test_existing_non_tower_geometry_does_not_block_tower_planning(self):
        for kind in ("cross_layer", "same_layer_overlap", "outside_board", "negative_z"):
            with self.subTest(kind=kind):
                raw = historical_source(kind)
                document = decode_template(raw)
                inspection = inspect_template(document)
                plan = plan_tower_distribution(document, inspection, DistributionOptions())
                self.assertTrue(plan.ok, plan)
                self.assertEqual(plan.moved_tile_indices, (8,))  # 9 total: strict 25% retains 2 of 3.
                self.assertEqual(encode_template(document), raw)

    def test_complete_generation_preserves_every_unmoved_wire_occurrence(self):
        for kind in ("cross_layer", "same_layer_overlap", "outside_board", "negative_z"):
            with self.subTest(kind=kind):
                raw = historical_source(kind)
                plan = historical_rewrite_plan(raw)
                self.assertTrue(plan.placement.ok, plan.placement)
                output = rewrite_template(raw, plan, "lessTsample")
                report = verify_output(raw, output, plan, "lessTsample")
                self.assertFalse(report.ok)
                self.assertTrue({'unsupported','negative_z'} & {issue.code for issue in report.errors})
                before, after = decode_template(raw), decode_template(output)
                for original, result in zip(before.tiles[:6], after.tiles[:6]):
                    self.assertEqual(original.fields, result.fields)
                self.assertEqual(report.facts["tile_counts"], {"total": 9, "A": 6, "B": 3})

    def test_inherited_geometry_does_not_excuse_illegal_new_geometry(self):
        raw = historical_source("cross_layer")
        plan = historical_rewrite_plan(raw)
        self.assertTrue(plan.placement.ok, plan.placement)
        for xyz, error_code in (((1, 4, 4), "safe_bounds"),
                                ((16, 4, 4), "board_bounds"),
                                ((5, 5, 4), "full_cover"),
                                ((10, 12, 4), "unsupported"),
                                ((4, 4, 6), "non_tower_z_gap")):
            with self.subTest(xyz=xyz):
                point = replace(plan.placement.placements[0], x=xyz[0], y=xyz[1], z=xyz[2])
                bad = replace(plan, placement=replace(plan.placement,
                              placements=(point,) + plan.placement.placements[1:]))
                report = verify_output(raw, rewrite_template(raw, bad, "lessTsample"), bad, "lessTsample")
                self.assertFalse(report.ok)
                self.assertIn(error_code, {issue.code for issue in report.errors}, report.errors)

    def test_existing_tile_mutation_is_still_unauthorized(self):
        raw = historical_source("cross_layer")
        plan = historical_rewrite_plan(raw)
        self.assertTrue(plan.placement.ok, plan.placement)
        document = decode_template(rewrite_template(raw, plan, "lessTsample"))
        document.tiles[3].z = 1
        report = verify_output(raw, encode_template(document), plan, "lessTsample")
        self.assertFalse(report.ok)
        self.assertIn("wire_unauthorized", {issue.code for issue in report.errors})
