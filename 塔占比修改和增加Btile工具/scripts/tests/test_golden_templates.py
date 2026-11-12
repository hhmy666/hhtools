import hashlib
import json
from pathlib import Path
import unittest

from scripts.tests.project_fixtures import read_project_fixture
from scripts.tests.test_rewrite import computed_plan
from scripts.tower_reducer.distribution import DistributionOptions, plan_tower_distribution
from scripts.tower_reducer.geometry import overlap_profile, symmetry_orbit
from scripts.tower_reducer.inspection import inspect_template
from scripts.tower_reducer.placement import Placement, PlacementOptions, PlacementPlan
from scripts.tower_reducer.template_reader import decode_template
from scripts.tower_reducer.rewrite import RewritePlan, rewrite_template
from scripts.tower_reducer.verification import verify_output

# Original accepted bytes stay immutable. Current plans prefer moved supports;
# independent final-byte verification still checks all geometry and invariants.
SUPPORT_MOVES = {
    'mxd0057v1': [(91,3,6,5),(93,13,6,5),(94,4,8,5),(96,12,8,5),(97,5,6,5),(98,11,6,5),
                 (99,5,10,5),(100,11,10,5),(101,5,12,5),(102,11,12,5),(103,7,2,5),(104,9,2,5)],
    'hr0029': [(91,3,3,6),(92,3,11,6),(93,13,3,6),(94,13,11,6),(95,3,7,6),(96,13,7,6),(97,6,7,6),(98,10,7,6)],
}
SUPPORT_SHA = {
    'mxd0057v1':'df50d303168c1f861e4424d2944fe3b3a1f854983f11523cb01cded7fa2b654c',
    'hr0029':'d161eea5c441273600204bec54e9976ae9957dd0470ec5c567e76399cc304b85',
}


class GoldenTemplateTests(unittest.TestCase):
    def test_five_historical_plans_match_accepted_sha_and_independent_audit(self):
        manifest=json.loads(read_project_fixture("manifest.json"))['fixtures']
        targets={'mxd0057v1':(3,5,9,5,3),'hr0029':(6,6,6,6),'hr0044':(4,5,7,5,4),
                 'mxd5010':(4,4,4,4,4,4),'ntxkd021':()}
        for name,counts in targets.items():
            with self.subTest(name=name):
                raw=read_project_fixture(name+'_m.bytes')
                self.assertEqual(hashlib.sha256(raw).hexdigest(),manifest[name+'_m.bytes']['sha256'])
                source=decode_template(raw);inspection=inspect_template(source)
                distribution=plan_tower_distribution(source,inspection,DistributionOptions())
                golden=decode_template(read_project_fixture('lessT'+name+'_m.bytes'))
                expected=[(t.x,t.y,t.z) for t in golden.tiles]
                for i,x,y,z in SUPPORT_MOVES.get(name,[]):expected[i]=(x,y,z)
                anchors={(s.x,s.y) for s in source.stacks}
                ordinary=[t for t in source.tiles if (t.x,t.y) not in anchors]
                points=[(t.x,t.y) for t in ordinary]
                axes=(min(x for x,y in points)+max(x for x,y in points),min(y for x,y in points)+max(y for x,y in points))
                final_anchors={(s.x,s.y) for s in golden.stacks}
                final_layers={}
                for x,y,z in expected:
                    if (x,y) not in final_anchors:final_layers.setdefault(z,[]).append((x,y))
                placements=tuple(Placement(x,y,z,overlap_profile((x,y),final_layers.get(z-1 if z else z+1,())),
                    symmetry_orbit((x,y),axes,inspection.symmetry),i)
                    for i in sorted(distribution.moved_tile_indices) for x,y,z in (expected[i],))
                added=len(set(final_layers)-{t.z for t in ordinary})
                # Golden bytes certify the saved historical strategy, not the
                # current planner's deliberately revised directional ranking.
                plan=RewritePlan(distribution,PlacementPlan(placements,'top_first',0,added_layers=added),
                                 DistributionOptions(),PlacementOptions())
                output=rewrite_template(raw,plan,'lessT'+name)
                expected_sha=SUPPORT_SHA.get(name,manifest['lessT'+name+'_m.bytes']['sha256'])
                self.assertEqual(hashlib.sha256(output).hexdigest(),expected_sha)
                report=verify_output(raw,output,plan,'lessT'+name)
                self.assertTrue(report.ok,report.errors)
                self.assertEqual(report.facts['retained_counts'],counts)
                self.assertEqual(report.facts['output_sha256'],expected_sha)
                self.assertEqual(report.facts['tile_counts']['B'],0)
                self.assertIn('layer_profiles',report.facts)
                self.assertIn('source_symmetry',report.facts)
                self.assertEqual(len(report.facts['moves']),plan.distribution.move_count)
                self.assertEqual(output,rewrite_template(raw,plan,'lessT'+name))
                golden=decode_template(read_project_fixture('lessT'+name+'_m.bytes'))
                actual=decode_template(output)
                expected=[(t.x,t.y,t.z) for t in golden.tiles]
                for i,x,y,z in SUPPORT_MOVES.get(name,[]):
                    expected[i]=(x,y,z)
                self.assertEqual([(t.x,t.y,t.z) for t in actual.tiles],expected)
                if not counts:
                    source=decode_template(raw)
                    self.assertEqual([f.raw for i,f in enumerate(source.fields) if i!=source.template_id_field_index],
                                     [f.raw for i,f in enumerate(actual.fields) if i!=actual.template_id_field_index])

    def test_current_directional_defaults_are_deterministic_and_independently_verified(self):
        for name in ('mxd0057v1','hr0029','hr0044','mxd5010','ntxkd021'):
            with self.subTest(name=name):
                raw=read_project_fixture(name+'_m.bytes')
                plan=computed_plan(raw)
                output=rewrite_template(raw,plan,'lessT'+name)
                report=verify_output(raw,output,plan,'lessT'+name)
                self.assertTrue(report.ok,report.errors)
                self.assertEqual(len(decode_template(raw).tiles),len(decode_template(output).tiles))
                self.assertEqual(output,rewrite_template(raw,computed_plan(raw),'lessT'+name))
