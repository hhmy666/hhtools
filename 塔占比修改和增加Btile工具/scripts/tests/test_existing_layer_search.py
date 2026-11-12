"""Regression: sparse original middle layers must participate in replenishment."""
import unittest
from collections import Counter
from unittest.mock import patch

from scripts.tests.test_ordinary_add_engine import source
from scripts.tower_reducer.distribution import DistributionOptions
from scripts.tower_reducer.inspection import inspect_template
from scripts.tower_reducer.placement import PlacementOptions, plan_joint_layout
from scripts.tower_reducer.rewrite import RewritePlan, rewrite_template
from scripts.tower_reducer.template_reader import decode_template
from scripts.tower_reducer.verification import verify_output
from scripts.tower_reducer import placement


def sparse_middle_source():
    layers=[[(6,6),(10,6)],[(6,5),(10,5),(6,7),(10,7)],[(6,4),(10,4),(6,8),(10,8)],
            [(6,5),(10,5),(6,7),(10,7)],[(6,4),(10,4),(6,6),(10,6),(6,8),(10,8)],
            [(5,3),(11,3),(7,5),(9,5),(7,7),(9,7),(5,9),(11,9)],
            [(4,4),(6,4),(8,4),(10,4),(12,4),(6,6),(10,6),(4,8),(6,8),(8,8),(10,8),(12,8)],
            [(5,3),(7,3),(9,3),(11,3),(5,5),(11,5),(5,7),(11,7),(5,9),(7,9),(9,9),(11,9)],
            [(6,2),(8,2),(10,2),(6,4),(10,4),(6,6),(10,6),(6,8),(10,8),(6,10),(8,10),(10,10)],
            [(7,3),(9,3),(7,5),(9,5),(7,7),(9,7),(7,9),(9,9)]]
    return source([(x,y,z) for z,ps in enumerate(layers) for x,y in ps],
                  [(x,14,9,0) for x in (4,6,8,10,12)])


class ExistingLayerSearchTests(unittest.TestCase):
    def test_rejected_final_profiles_never_escape_as_a_complete_incumbent(self):
        state=placement.PlacementState((17,19),{0:[(4,4),(10,4)],1:[(5,5),(10,5)]},{},
                                       layer_profiles={1:((1,),(2,))})
        def candidates(current,z):
            return [placement.Placement(6,4,z,(1,),((6,4),))] if z==0 else [placement.Placement(6,3,z,(2,),((6,3),))]
        with patch.object(placement,'enumerate_legal_positions',side_effect=candidates):
            result=placement._search_all_layers(state,2,PlacementOptions('top_first',3,0),placement.SearchBudget())
        # The upper addition changes the bottom's final profile from (1,) to
        # mixed units (2,1). Final validation rejects it; it is not a solution.
        self.assertLess(len(result[0]),2)

    def test_random_seeds_can_use_existing_middle_layers(self):
        raw=sparse_middle_source();doc=decode_template(raw);inspection=inspect_template(doc);dist=DistributionOptions(0)
        for strategy in ('seeded_random',):
            for seed in ((0,1,2,3) if strategy=='seeded_random' else (1,)):
                with self.subTest(strategy=strategy,seed=seed):
                    options=PlacementOptions(strategy,21,4,seed)
                    result=plan_joint_layout(doc,inspection,dist,options)
                    self.assertTrue(result.ok,result.placement)
                    plan=RewritePlan(result.distribution,result.placement,dist,options)
                    report=verify_output(raw,rewrite_template(raw,plan,'new'),plan,'new')
                    self.assertTrue(report.ok,report.errors)
                    self.assertTrue(any(0<p.z<9 for p in result.placement.placements))
                    self.assertEqual(result.placement.added_layers,0)

    def test_sparse_middle_clears_all_forty_five_with_original_parameters(self):
        raw=sparse_middle_source();doc=decode_template(raw);inspection=inspect_template(doc)
        dist=DistributionOptions(0);opt=PlacementOptions('seeded_random',21,4,1)
        result=plan_joint_layout(doc,inspection,dist,opt)
        self.assertTrue(result.ok,result.placement)
        plan=RewritePlan(result.distribution,result.placement,dist,opt)
        output=rewrite_template(raw,plan,'new')
        report=verify_output(raw,output,plan,'new')
        self.assertTrue(report.ok,report.errors)
        self.assertEqual(len(result.placement.placements),45)
        self.assertEqual(len(decode_template(output).tiles),117)
        self.assertFalse(decode_template(output).stacks)
        self.assertLessEqual(result.placement.added_layers,4)
        self.assertTrue(all(n<=21 for z,n in Counter(t.z for t in decode_template(output).tiles).items()))
