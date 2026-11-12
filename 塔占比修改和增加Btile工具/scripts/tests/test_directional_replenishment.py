"""Directional intent survives complete packing and lower-layer normalization."""
import unittest
from collections import Counter
from dataclasses import replace

from scripts.tests.test_placement import sample
from scripts.tower_reducer.placement import PlacementOptions, plan_placements
from scripts.tower_reducer import placement


class DirectionalReplenishmentTests(unittest.TestCase):
    def test_unfolded_tower_direction_blocks_moved_ordinary_candidates(self):
        state=placement.PlacementState((17,19),{0:[(4,8)]},{1:[(8,8)]},
            tower_envelopes=((8,8,15,1),))
        positions={p.orbit[0] for p in placement.enumerate_legal_positions(state,1)}
        self.assertNotIn((5,7),positions)
        self.assertIn((3,7),positions)

    def test_lower_extension_roundtrips_wire_and_keeps_retained_towers_fixed(self):
        from scripts.tests.test_ordinary_add_engine import source
        from scripts.tower_reducer.template_reader import decode_template
        from scripts.tower_reducer.inspection import inspect_template
        from scripts.tower_reducer.distribution import DistributionOptions,plan_tower_distribution
        from scripts.tower_reducer.rewrite import RewritePlan,rewrite_template
        from scripts.tower_reducer.verification import verify_output
        from scripts.tower_reducer.storage import _pack,_unpack
        raw=source([(4+z%2,4+z%2,z) for z in range(14)],[(12,14,4,0)])
        doc=decode_template(raw)
        dopt=DistributionOptions(.20,retained_counts=(3,))
        dist=plan_tower_distribution(doc,inspect_template(doc),dopt)
        opt=PlacementOptions('bottom_first',1,1)
        result=plan_placements(doc,inspect_template(doc),dist,opt,include_advice=False)
        self.assertTrue(result.ok,result)
        self.assertEqual(result.original_z_offset,1)
        plan=RewritePlan(dist,result,dopt,opt)
        output=rewrite_template(raw,plan,'new')
        report=verify_output(raw,output,plan,'new')
        self.assertTrue(report.ok,report.errors)
        final=decode_template(output)
        self.assertEqual([t.z for t in final.tiles[:14]],list(range(1,15)))
        self.assertEqual([t.z for t in final.tiles[14:17]],[0,1,2])
        self.assertEqual(final.tiles[-1].z,0)
        self.assertEqual(_unpack(_pack(plan)),plan)
        # A claimed shift cannot authorize XY, type, or unknown payload edits.
        for before,after in zip(doc.tiles[:17],final.tiles[:17]):
            self.assertEqual((before.x,before.y,before.tile_id),(after.x,after.y,after.tile_id))
        legacy=_pack(replace(result,original_z_offset=0,strategy_version='orbit-v3-all-layers-unit-backtrack'))
        legacy['fields'].pop('original_z_offset')
        self.assertEqual(_unpack(legacy).original_z_offset,0)

    def test_three_lower_layers_keep_original_types_during_uniform_shift(self):
        from scripts.tests.test_ordinary_add_engine import source,scalar
        from scripts.tower_reducer.template_reader import decode_template
        from scripts.tower_reducer.inspection import inspect_template
        from scripts.tower_reducer.distribution import DistributionOptions,plan_tower_distribution
        from scripts.tower_reducer.rewrite import RewritePlan,rewrite_template
        from scripts.tower_reducer.verification import verify_output
        raw=source([(4+z%2,4+z%2,z) for z in range(15)],[(12,14,6,0)])
        raw=raw.replace(scalar(4,-10),scalar(4,-11),3)
        doc=decode_template(raw);inspected=inspect_template(doc)
        dopt=DistributionOptions(.25,retained_counts=(3,))
        dist=plan_tower_distribution(doc,inspected,dopt)
        opt=PlacementOptions('bottom_first',1,3)
        result=plan_placements(doc,inspected,dist,opt,include_advice=False)
        self.assertTrue(result.ok,result)
        self.assertEqual(result.original_z_offset,3)
        plan=RewritePlan(dist,result,dopt,opt);output=rewrite_template(raw,plan,'new')
        report=verify_output(raw,output,plan,'new')
        self.assertTrue(report.ok,report.errors)
        final=decode_template(output)
        self.assertEqual([t.tile_id for t in final.tiles[:18]],[t.tile_id for t in doc.tiles[:18]])
        self.assertEqual([t.z for t in final.tiles[-3:]],[2,1,0])

    def test_single_layer_balanced_has_unique_nonnegative_destinations(self):
        result=plan_placements(*sample([(4,4,0)],4),PlacementOptions('balanced',6,1),include_advice=False)
        self.assertTrue(result.ok,result)
        coordinates={(p.x,p.y,p.z) for p in result.placements}
        self.assertEqual(len(coordinates),4)
        self.assertTrue(all(z>=0 for x,y,z in coordinates))
    def test_top_first_fills_sparse_top_band_before_adding_layers(self):
        points = [(4,4,0),(6,4,0),(8,4,0),(10,4,0),
                  (5,5,1),(7,5,1),(9,5,1),(11,5,1),(6,6,2),(7,7,3)]
        result = plan_placements(*sample(points,3), PlacementOptions('top_first',8,2),include_advice=False)
        self.assertTrue(result.ok,result)
        self.assertEqual(result.added_layers,0)
        self.assertTrue(all(p.z in (2,3) for p in result.placements))
        self.assertTrue(any(p.z==2 for p in result.placements))

    def test_top_first_reports_failure_instead_of_falling_back_to_bottom(self):
        # Top layer is already at its limit; bottom is sparse and can accept a
        # supported corner. A complete bottom packing must not change intent.
        points=[(6,4,0),(5,5,1),(7,5,1)]
        result=plan_placements(*sample(points,1),PlacementOptions('top_first',2,0),include_advice=False)
        self.assertFalse(result.ok,result)

    def test_bottom_extension_normalizes_existing_ordinary_layers(self):
        result=plan_placements(*sample([(4,4,0),(5,5,1)],1),
                               PlacementOptions('bottom_first',1,1),include_advice=False)
        self.assertTrue(result.ok,result)
        self.assertEqual(result.original_z_offset,1)
        self.assertEqual(result.added_layers,1)
        self.assertEqual([p.z for p in result.placements],[0])

    def test_balanced_splits_tile_counts_across_bands(self):
        points=[(4,4,0),(8,4,0),(12,4,0),(5,5,1),(9,5,1),(13,5,1),
                (6,6,2),(10,6,2),(12,6,2),(7,7,3),(11,7,3)]
        result=plan_placements(*sample(points,4),PlacementOptions('balanced',8,0),include_advice=False)
        self.assertTrue(result.ok,result)
        counts=Counter('bottom' if p.z<=1 else 'top' for p in result.placements)
        self.assertEqual(counts,{'bottom':2,'top':2})

    def test_sparse_band_depth_adapts_to_three_layers(self):
        state=placement.PlacementState((17,19),{0:[(2,2)]*8,1:[(2,2)]*9,
            2:[(2,2)]*3,3:[(2,2)]*2,4:[(2,2)]},{})
        bottom,top=placement._end_bands(state)
        self.assertEqual(top,(4,3,2))
