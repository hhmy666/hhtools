"""The independently read-back layer must retain the lower layer's overlap unit."""
import unittest

from scripts.tests.test_rewrite import scalar, message
from scripts.tower_reducer.distribution import DistributionOptions, DistributionPlan
from scripts.tower_reducer.placement import Placement, PlacementOptions, PlacementPlan
from scripts.tower_reducer.rewrite import RewritePlan, rewrite_template
from scripts.tower_reducer.verification import verify_output


class InheritedStyleTests(unittest.TestCase):
    def test_independent_verifier_rejects_half_overlap_above_quarter_style(self):
        points = [(4,4,0),(8,4,0),(12,4,0),(5,5,1),(9,5,1),(12,5,1),(6,6,2),(10,6,2),(13,6,2)]
        points += [(2,16,z) for z in range(3)]
        raw = message(2,b'style_case')+scalar(4,17)+scalar(5,19)
        raw += b''.join(message(6,scalar(1,x)+scalar(2,y)+scalar(3,z)+scalar(4,-10)) for x,y,z in points)
        raw += message(7,scalar(1,2)+scalar(2,16)+scalar(3,2))
        distribution = DistributionPlan((0,), (9,10,11), 'A',3,0,0)
        placements = tuple(Placement(x,y,3,(2,),((x,y),),index) for index,(x,y) in zip((9,10,11),((6,7),(10,7),(13,7))))
        plan = RewritePlan(distribution,PlacementPlan(placements,'top_first',0,added_layers=1),DistributionOptions(0,(0,)),PlacementOptions())
        report = verify_output(raw,rewrite_template(raw,plan,'newstyle'),plan,'newstyle')
        self.assertIn('inherited_stacking_style', {issue.code for issue in report.errors})
