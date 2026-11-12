"""Real failure snapshots, including a support-chain defect found in the batch."""
from dataclasses import replace
import unittest
from scripts.tests.test_rewrite import scalar, message
from scripts.tower_reducer.template_reader import decode_template, encode_template
from scripts.tower_reducer.inspection import inspect_template
from scripts.tower_reducer.distribution import DistributionOptions
from scripts.tower_reducer.placement import PlacementOptions, plan_joint_layout
from scripts.tower_reducer.rewrite import RewritePlan, rewrite_template
from scripts.tower_reducer.verification import verify_output
from scripts.tests.test_api import ApiContractCase


GEOMETRY = {
    'hr0127': ([
        [(7,3),(9,3),(7,5),(9,5),(7,7),(9,7),(7,9),(9,9)],
        [(8,2),(6,4),(10,4),(8,6),(6,8),(10,8),(8,10)],
        [(7,1),(9,1),(5,3),(11,3),(5,5),(7,5),(9,5),(11,5),(5,7),(7,7),(9,7),(11,7),(5,9),(11,9),(7,11),(9,11)],
        [(8,2),(4,4),(8,4),(12,4),(4,6),(6,6),(10,6),(12,6),(4,8),(8,8),(12,8),(8,10)],
        [(7,1),(9,1),(5,3),(11,3),(5,5),(7,5),(9,5),(11,5),(5,7),(7,7),(9,7),(11,7),(5,9),(11,9),(7,11),(9,11)],
        [(8,2),(4,4),(8,4),(12,4),(4,6),(6,6),(10,6),(12,6),(4,8),(8,8),(12,8),(8,10)],
    ], [6,8,9,8,6], 14),
    'hr0030': ([
        [(4,2),(12,2),(4,11),(12,11)],
        [(3,1),(5,1),(11,1),(13,1),(3,3),(13,3),(3,10),(13,10),(3,12),(5,12),(11,12),(13,12)],
        [(4,2),(6,2),(10,2),(12,2),(4,4),(12,4),(4,9),(12,9),(4,11),(6,11),(10,11),(12,11)],
        [(5,3),(7,3),(9,3),(11,3),(5,5),(11,5),(5,8),(11,8),(5,10),(7,10),(9,10),(11,10)],
        [(6,2),(8,2),(10,2),(6,4),(8,4),(10,4),(6,6),(8,6),(10,6),(6,8),(8,8),(10,8),(6,10),(8,10),(10,10)],
        [(7,3),(9,3),(7,5),(9,5),(7,7),(9,7),(7,9),(9,9)],
    ], [7,9,10,9,7], 15),
    'hr0128': ([
        [(7,6),(9,6),(7,8),(9,8)],
        [(6,5),(10,5),(6,9),(10,9)],
        [(5,4),(7,4),(9,4),(11,4),(6,6),(10,6),(6,8),(10,8),(5,10),(7,10),(9,10),(11,10)],
        [(4,3),(8,3),(12,3),(4,5),(12,5),(5,7),(11,7),(4,9),(12,9),(4,11),(8,11),(12,11)],
        [(3,2),(5,2),(7,2),(9,2),(11,2),(13,2),(4,4),(12,4),(5,6),(11,6),(7,7),(9,7),(5,8),(11,8),(4,10),(12,10),(3,12),(5,12),(7,12),(9,12),(11,12),(13,12)],
        [(4,3),(6,3),(8,3),(10,3),(12,3),(5,5),(11,5),(6,7),(8,7),(10,7),(5,9),(11,9),(4,11),(6,11),(8,11),(10,11),(12,11)],
    ], [5,7,7,7,5], 15),
    'nn0007v1': ([
        [(8,3),(6,5),(10,5),(7,8),(9,8)],
        [(7,4),(9,4),(5,6),(11,6),(6,9),(8,9),(10,9)],
        [(8,3),(6,5),(8,5),(10,5)],
        [(7,4),(9,4),(5,6),(7,6),(9,6),(11,6)],
        [(6,3),(8,3),(10,3),(4,5),(6,5),(8,5),(10,5),(12,5),(6,7),(8,7),(10,7)],
        [(7,4),(9,4),(5,6),(7,6),(9,6),(11,6),(5,8),(7,8),(9,8),(11,8),(7,10),(9,10)],
        [(6,3),(8,3),(10,3),(4,5),(6,5),(8,5),(10,5),(12,5),(4,7),(6,7),(8,7),(10,7),(12,7),(6,9),(8,9),(10,9)],
        [(5,4),(7,4),(9,4),(11,4),(3,6),(5,6),(7,6),(9,6),(11,6),(13,6),(5,8),(7,8),(9,8),(11,8)],
    ], [8,11,10,11,8], 14),
}


def source(name):
    layers, counts, y = GEOMETRY[name]
    parts = [message(2,name.encode()), scalar(4,17), scalar(5,19)]
    for z, points in enumerate(layers):
        for x, yy in points:
            kind = -11 if name == 'hr0127' and z >= 3 and (x,yy,z) != (4,4,3) else -10
            parts.append(message(6,scalar(1,x)+scalar(2,yy)+scalar(3,z)+scalar(4,kind)))
    for index, (x, count) in enumerate(zip((4,6,8,10,12), counts)):
        for z in range(count):
            parts.append(message(6,scalar(1,x)+scalar(2,y)+scalar(3,z)+scalar(4,-10)+scalar(5,index)))
        parts.append(message(7,scalar(1,x)+scalar(2,y)+scalar(3,2)))
    return b''.join(parts)


class ReportedRandomRecoveryTests(unittest.TestCase):
    def test_hr0127_top_first_accepts_minor_a_near_original_a_across_layers(self):
        # Geometry and ordinary A/B types transcribed from actual hr0127_m.bytes.
        raw = source('hr0127')
        doc = decode_template(raw)
        dopt = DistributionOptions(0,None,0,'majority_B')
        popt = PlacementOptions('top_first',20,4,1)
        result = plan_joint_layout(doc,inspect_template(doc),dopt,popt)
        self.assertTrue(result.ok,result.placement)
        plan = RewritePlan(result.distribution,result.placement,dopt,popt)
        report = verify_output(raw,rewrite_template(raw,plan,'fixedhr0127'),plan,'fixedhr0127')
        self.assertTrue(report.ok,report.errors)
        self.assertEqual(report.facts['removed_tile_counts'], {'A':37,'B':0})
        self.assertEqual(report.facts['added_tile_counts'], {'A':1,'B':36})
        self.assertEqual(report.facts['tile_counts'], {'total':108,'A':33,'B':75})
        anchors = {(s.x,s.y) for s in doc.stacks}
        originals = [t for t in doc.tiles if t.tile_id == -10 and (t.x,t.y) not in anchors]
        correction = next(p for p in result.placement.placements
                          if dict(zip(sorted(result.distribution.moved_tile_indices),
                                      result.distribution.replenishment_ids))[p.tile_index] == -10)
        self.assertLessEqual(min((correction.x-t.x)**2+(correction.y-t.y)**2+
                                (correction.z-t.z)**2 for t in originals), 8)
        self.assertTrue(report.facts['strict_structure']['supported'])
        self.assertTrue(report.facts['strict_structure']['no_full_cover'])

    def test_nn0007_support_repair_prefers_pairs_over_a_central_single_tile_stem(self):
        raw=source('nn0007v1')
        doc=decode_template(raw)
        dopt=DistributionOptions(0,None,0,'majority_B')
        popt=PlacementOptions('top_first',20,3,1)
        result=plan_joint_layout(doc,inspect_template(doc),dopt,popt)
        self.assertTrue(result.ok,result.placement)
        for z in range(4):
            added=[p for p in result.placement.placements if p.z==z]
            self.assertNotEqual(len(added),1,(z,added))
        plan=RewritePlan(result.distribution,result.placement,dopt,popt)
        report=verify_output(raw,rewrite_template(raw,plan,'paired'),plan,'paired')
        self.assertTrue(report.ok,report.errors)

    def test_directional_modes_repair_real_source_gaps_before_replenishment(self):
        for name, moved in [('hr0128',31), ('nn0007v1',48), ('hr0030',42)]:
            for strategy in ('top_first','balanced'):
                with self.subTest(template=name,strategy=strategy):
                    raw=source(name)
                    doc=decode_template(raw)
                    dopt=DistributionOptions(0,None,0,'majority_B')
                    popt=PlacementOptions(strategy,20,3,1)
                    result=plan_joint_layout(doc,inspect_template(doc),dopt,popt)
                    self.assertTrue(result.ok,result.placement)
                    self.assertEqual(len(result.placement.placements),moved)
                    plan=RewritePlan(result.distribution,result.placement,dopt,popt)
                    report=verify_output(raw,rewrite_template(raw,plan,'fixed'+name),plan,'fixed'+name)
                    self.assertTrue(report.ok,report.errors)
                    self.assertTrue(report.facts['strict_structure']['supported'])
                    self.assertTrue(report.facts['strict_structure']['no_full_cover'])

    def test_directional_support_exception_does_not_authorize_unrelated_lower_tiles(self):
        raw=source('hr0128')
        doc=decode_template(raw)
        dopt=DistributionOptions(0,None,0,'majority_B')
        popt=PlacementOptions('top_first',20,3,1)
        result=plan_joint_layout(doc,inspect_template(doc),dopt,popt)
        self.assertTrue(result.ok,result.placement)
        placements=list(result.placement.placements)
        placements[0]=replace(placements[0],x=2,y=2,z=0)
        plan=RewritePlan(result.distribution,replace(result.placement,placements=tuple(placements)),dopt,popt)
        report=verify_output(raw,rewrite_template(raw,plan,'bad'),plan,'bad')
        self.assertIn('strategy_direction',[issue.code for issue in report.errors])

    def test_directional_recovery_preserves_all_original_symmetries(self):
        raw=source('hr0128')
        doc=decode_template(raw)
        dopt=DistributionOptions(0,None,0,'majority_B')
        popt=PlacementOptions('top_first',20,3,1)
        result=plan_joint_layout(doc,inspect_template(doc),dopt,popt)
        self.assertTrue(result.ok,result.placement)
        plan=RewritePlan(result.distribution,result.placement,dopt,popt)
        report=verify_output(raw,rewrite_template(raw,plan,'good'),plan,'good')
        self.assertTrue(report.ok,report.errors)
        self.assertEqual(report.facts['required_output_symmetry'],('horizontal','vertical','rotation'))
        placements=list(result.placement.placements)
        placements[-1]=replace(placements[-1],x=3)
        plan=replace(plan,placement=replace(result.placement,placements=tuple(placements)))
        report=verify_output(raw,rewrite_template(raw,plan,'bad'),plan,'bad')
        self.assertIn('source_symmetry',[issue.code for issue in report.errors])

    def test_incomplete_advice_branch_does_not_hide_verified_alternative_or_claim_complete(self):
        from scripts.tests.test_rewrite import synthetic
        from scripts.tower_reducer.parameter_advice import verified_parameter_advice
        raw,_=synthetic('A')
        advice=verified_parameter_advice(raw,DistributionOptions(),PlacementOptions(
            max_tiles_per_layer=3,max_added_layers=0),'retry')
        self.assertEqual(len(advice['schemes']),2)
        self.assertEqual(advice['status'],'limited')

    def test_random_repairs_support_and_replenishes_all_removed_tower_tiles(self):
        for name, moved in [('hr0128',31), ('nn0007v1',48), ('hr0030',42)]:
            with self.subTest(template=name):
                raw=source(name)
                doc=decode_template(raw)
                dopt=DistributionOptions(0,None,0,'majority_B')
                popt=PlacementOptions('seeded_random',20,4,1)
                result=plan_joint_layout(doc,inspect_template(doc),dopt,popt)
                self.assertTrue(result.ok,result.placement)
                self.assertEqual(len(result.placement.placements),moved)
                plan=RewritePlan(result.distribution,result.placement,dopt,popt)
                report=verify_output(raw,rewrite_template(raw,plan,'fixed'+name),plan,'fixed'+name)
                self.assertTrue(report.ok,report.errors)
                self.assertTrue(report.facts['strict_structure']['supported'])
                self.assertTrue(report.facts['strict_structure']['no_full_cover'])
                self.assertEqual(report.facts['tile_counts']['B'],moved-moved%3)
                self.assertEqual(encode_template(doc),raw)

    def test_general_partial_overlap_plans_still_reject_floating_and_full_cover(self):
        raw=source('hr0030')
        doc=decode_template(raw)
        dopt=DistributionOptions(0,None,0,'majority_B')
        popt=PlacementOptions('seeded_random',20,4,1)
        result=plan_joint_layout(doc,inspect_template(doc),dopt,popt)
        self.assertTrue(result.ok,result.placement)
        for x,y,z,code in [(14,16,12,'unsupported'),(4,2,1,'full_cover')]:
            with self.subTest(defect=code):
                placements=list(result.placement.placements)
                placements[0]=replace(placements[0],x=x,y=y,z=z)
                bad=replace(result.placement,placements=tuple(placements))
                plan=RewritePlan(result.distribution,bad,dopt,popt)
                report=verify_output(raw,rewrite_template(raw,plan,'bad'),plan,'bad')
                self.assertFalse(report.ok)
                self.assertIn(code,[issue.code for issue in report.errors])

    def test_removed_tower_ground_anchor_can_be_reused_as_ordinary_tile(self):
        raw=source('hr0030')
        doc=decode_template(raw)
        dopt=DistributionOptions(0,None,0,'majority_B')
        popt=PlacementOptions('seeded_random',20,4,7)
        result=plan_joint_layout(doc,inspect_template(doc),dopt,popt)
        self.assertTrue(result.ok,result.placement)
        plan=RewritePlan(result.distribution,result.placement,dopt,popt)
        report=verify_output(raw,rewrite_template(raw,plan,'reused'),plan,'reused')
        self.assertTrue(report.ok,report.errors)
        self.assertEqual(report.facts['tile_counts'],{'total':105,'A':63,'B':42})


class ReportedRandomHttpTests(ApiContractCase):
    def test_reported_batch_is_exportable_through_the_real_generation_api(self):
        self.start_server()
        names=('hr0128','nn0007v1','hr0030')
        imported=self.import_files([(name+'_m.bytes',source(name)) for name in names])
        options={'ratio':0,'min_ratio':0,'replenish_type':'majority_B',
                 'strategy':'seeded_random','max_tiles_per_layer':20,
                 'max_added_layers':4,'seed':1}
        result=self.wait_job(self.start_job(imported['import_id'],defaults=options))
        self.assertEqual(len(self.rows(result)),3)
        self.assertTrue(all(row['status']=='verified' for row in self.rows(result)),self.rows(result))
        self.assertEqual(len(result['artifacts']),3)
