from collections import Counter
from dataclasses import replace
import unittest

from scripts.tests.test_distribution import source
from scripts.tests.test_rewrite import synthetic, scalar
from scripts.tower_reducer.distribution import DistributionOptions, plan_tower_distribution
from scripts.tower_reducer.inspection import inspect_template
from scripts.tower_reducer.placement import Placement
from scripts.tower_reducer.rewrite import rewrite_template
from scripts.tower_reducer.template_reader import decode_template
from scripts.tower_reducer.verification import verify_output
from scripts.tower_reducer.protobuf_wire import replace_length_delimited


class MixedReplenishmentTests(unittest.TestCase):
    def test_minority_assignment_uses_euclidean_distance_across_layers(self):
        from scripts.tower_reducer.placement import assign_replenishment_positions
        for mode, minority in [('majority_B', -10), ('majority_A', -11)]:
            with self.subTest(mode=mode):
                doc = source(['AAA'], extra_a=3, extra_b=3)
                dist = plan_tower_distribution(doc, inspect_template(doc), DistributionOptions(ratio=0))
                # One original ordinary anchor; the other kind cannot anchor a correction.
                for tile in doc.tiles[3:]:
                    tile.tile_id = -11 if minority == -10 else -10
                doc.tiles[3].tile_id = minority
                dist = replace(dist, replenishment_ids=(minority, -11 if minority == -10 else -10,
                                                       -11 if minority == -10 else -10), move_type=mode)
                # Anchor (2,2,0): squared XYZ distances 6, 8, 9 respectively.
                for xyz, accepted in [((3,3,2), True), ((4,2,2), True), ((4,3,2), False),
                                      ((2,2,3), False)]:
                    places = tuple(Placement(*point, (), (), i) for i, point in enumerate(
                        (xyz, (12,14,0), (10,14,0))))
                    assigned = assign_replenishment_positions(doc, dist, places)
                    self.assertEqual(assigned is not None, accepted, (mode, xyz))
                    if accepted:
                        chosen = next(p for p in assigned if p.tile_index == 0)
                        self.assertEqual((chosen.x, chosen.y, chosen.z), xyz)

    def test_independent_verifier_accepts_cross_layer_neighbor_and_rejects_far_minorities(self):
        raw, combined = synthetic(kind='A')
        doc = decode_template(raw)
        # Two ground A anchors, four ordinary B, three tower A: replace with 1A/2B.
        changed = {doc.tiles[i].top_field_index for i in (2,3,4,5)}
        raw = b''.join(replace_length_delimited(f, f.payload + scalar(4,-11)) if i in changed else f.raw
                       for i,f in enumerate(doc.fields))
        dist = replace(combined.distribution, move_type='majority_B', removed_counts=(3,0),
                       final_counts=(3,6), replenishment_ids=(-10,-11,-11))
        combined = replace(combined, distribution=dist,
            distribution_options=replace(combined.distribution_options, replenish_type='majority_B'))
        report = verify_output(raw, rewrite_template(raw, combined, 'near'), combined, 'near')
        # A at (4,4,2) is two layers above original A at (4,4,0).
        self.assertTrue(report.ok, report.errors)
        for xyz in ((4,4,3), (12,4,2)):
            places = (replace(combined.placement.placements[0], x=xyz[0], y=xyz[1], z=xyz[2]),
                      *combined.placement.placements[1:])
            bad = replace(combined, placement=replace(combined.placement, placements=places))
            report = verify_output(raw, rewrite_template(raw,bad,'far'),bad,'far')
            self.assertIn('minority_proximity', [e.code for e in report.errors], xyz)

    def test_majority_b_keeps_one_a_out_of_31_for_final_triples(self):
        doc = source(['A' * 34], extra_a=2, extra_b=6)
        plan = plan_tower_distribution(doc, inspect_template(doc),
            DistributionOptions(retained_counts=(3,), replenish_type='majority_B'))
        self.assertTrue(plan.ok, plan)
        self.assertEqual(Counter(plan.replenishment_ids), {-11: 30, -10: 1})
        self.assertEqual(plan.final_counts, (6, 36))

    def test_automatic_mixed_mode_does_not_filter_valid_mixed_residue(self):
        doc = source(['BBBAAA'], extra_b=6)
        plan = plan_tower_distribution(doc, inspect_template(doc),
            DistributionOptions(replenish_type='majority_A'))
        self.assertTrue(plan.ok, plan)
        self.assertEqual(plan.retained_counts, (2,))
        self.assertEqual(plan.final_counts, (3, 9))

    def test_preserve_uses_source_ids_in_sorted_moved_index_order(self):
        doc = source(['ABBAAB'], extra_a=3, extra_b=3)
        plan = plan_tower_distribution(doc, inspect_template(doc),
            DistributionOptions(ratio=0, replenish_type='preserve'))
        self.assertTrue(plan.ok, plan)
        self.assertEqual(plan.replenishment_ids, (-10, -11, -11, -10, -10, -11))
        self.assertEqual(plan.final_counts, (6, 6))

    def test_automatic_mixed_skips_heights_with_too_few_correction_slots(self):
        doc = source(['BAA'], extra_a=6)
        doc.tiles[3].tile_id = -11
        plan = plan_tower_distribution(doc,inspect_template(doc),DistributionOptions(replenish_type='majority_A'))
        self.assertTrue(plan.ok,plan)
        self.assertEqual(plan.retained_counts,(1,))
        self.assertEqual(plan.final_counts,(6,6))

    def test_minority_gets_real_neighbor_and_missing_neighbor_is_reported(self):
        from scripts.tower_reducer.placement import assign_replenishment_positions
        doc = source(['AAA'], extra_a=3, extra_b=3)
        dist = plan_tower_distribution(doc, inspect_template(doc),
            DistributionOptions(ratio=0, replenish_type='majority_B'))
        # Force an auditable 1A/2B assignment for this geometry-only contract.
        dist = replace(dist, replenishment_ids=(-10, -11, -11), move_type='majority_B')
        places = tuple(Placement(x,y,0,(),(),i) for i,(x,y) in enumerate(((12,14),(4,2),(10,14))))
        result = assign_replenishment_positions(doc, dist, places)
        a = next(p for p in result if p.tile_index == 0)
        self.assertEqual((a.x,a.y,a.z), (4,2,0))
        distant = tuple(replace(p, y=14) for p in places)
        self.assertIsNone(assign_replenishment_positions(doc, dist, distant))

    def test_mixed_wire_rewrite_and_independent_verification(self):
        raw, combined = synthetic(kind='A')
        source_doc = decode_template(raw)
        changed = source_doc.tiles[5].top_field_index
        raw = b''.join(replace_length_delimited(f, f.payload + scalar(4,-11)) if i == changed else f.raw
                       for i,f in enumerate(source_doc.fields))
        dist = replace(combined.distribution, move_type='majority_B', removed_counts=(3,0),
                       final_counts=(6,3), replenishment_ids=(-10,-11,-11))
        combined = replace(combined, distribution=dist,
                           distribution_options=replace(combined.distribution_options, replenish_type='majority_B'))
        from scripts.tower_reducer.placement import plan_placements
        planned = plan_placements(decode_template(raw), inspect_template(decode_template(raw)), dist)
        self.assertTrue(planned.ok, planned)
        output = rewrite_template(raw, combined, 'new')
        self.assertEqual([t.tile_id for t in decode_template(output).tiles], [-10]*5+[-11,-10,-11,-11])
        report = verify_output(raw, output, combined, 'new')
        self.assertTrue(report.ok, report.errors)
        with self.assertRaises(ValueError):
            rewrite_template(raw,replace(combined,distribution=replace(dist,removed_counts=None,final_counts=None)),'new')
        # Keep population, but move the correction beyond every original A's radius.
        places = combined.placement.placements
        bad_places = (replace(places[0],tile_index=8), places[1], replace(places[2],tile_index=6,z=4))
        bad_plan = replace(combined, placement=replace(combined.placement, placements=bad_places))
        bad_output = rewrite_template(raw, bad_plan, 'new')
        bad_report = verify_output(raw, bad_output, bad_plan, 'new')
        self.assertIn('minority_proximity', [e.code for e in bad_report.errors])

    def test_missing_source_minority_is_reported_without_claiming_proximity(self):
        raw, combined = synthetic(kind='B')
        # Keep two A tower members so one A correction is necessary, while all
        # ordinary source Tiles are B. The source count need not be divisible.
        doc = decode_template(raw)
        tops = {doc.tiles[i].top_field_index for i in (6,7,8)}
        raw = b''.join(replace_length_delimited(f,f.payload+scalar(4,-10)) if i in tops else f.raw
                       for i,f in enumerate(doc.fields))
        # Retained tower A=1 forces two A corrections; no ordinary A exists.
        dist = replace(combined.distribution, retained_counts=(1,), moved_tile_indices=(7,8),
                       move_count=2, cap=2, final_ratio=1/9, move_type='majority_B',
                       removed_counts=(2,0), final_counts=(3,6), replenishment_ids=(-10,-10))
        from scripts.tower_reducer.placement import assign_replenishment_positions
        places = tuple(replace(p,tile_index=i) for i,p in zip((7,8),combined.placement.placements[:2]))
        self.assertIsNone(assign_replenishment_positions(decode_template(raw),dist,places))
        combined = replace(combined, distribution=dist, placement=replace(combined.placement,placements=places),
                           distribution_options=DistributionOptions(ratio=.25,retained_counts=(1,),replenish_type='majority_B'))
        output = rewrite_template(raw,combined,'new')
        report = verify_output(raw,output,combined,'new')
        self.assertFalse(report.ok)
        self.assertIn('minority_source_type_absent', [e.code for e in report.errors])
