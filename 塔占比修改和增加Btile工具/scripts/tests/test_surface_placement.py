import unittest
from dataclasses import replace

from scripts.tests.test_placement import sample
from scripts.tower_reducer.placement import PlacementOptions, plan_placements
from scripts.tower_reducer.inspection import inspect_template


class SurfacePlacementTests(unittest.TestCase):
    def test_inherited_column_allows_far_reuse_but_rejects_new_adjacent_full_cover(self):
        from scripts.tower_reducer.placement import PlacementState,enumerate_legal_positions
        state=PlacementState((17,19),{0:[(4,4)],1:[(4,4)],3:[(5,5)]},{},
            inherited_full_cover=((4,4,1),),relaxed_profiles=True)
        self.assertIn((4,4),{p.orbit[0] for p in enumerate_legal_positions(state,4)})
        self.assertNotIn((4,4),{p.orbit[0] for p in enumerate_legal_positions(state,2)})

    def test_immutable_source_full_cover_is_inherited_without_new_full_cover(self):
        doc, inspection, distribution = sample([(4,4,0), (4,4,1)], moved=2)
        result = plan_placements(doc, inspection, distribution,
            PlacementOptions(max_added_layers=1), include_advice=False)
        self.assertTrue(result.ok,result)
        self.assertEqual([(t.x,t.y,t.z) for t in doc.tiles[:2]],[(4,4,0),(4,4,1)])
        self.assertTrue(all((p.x,p.y)!=(4,4) for p in result.placements))

    def test_visible_symmetric_top_guides_additions_despite_hidden_asymmetry(self):
        doc, inspection, distribution = sample(
            [(4, 4, 0), (8, 4, 0), (4, 6, 0),
             (5, 5, 1), (7, 5, 1)], moved=2)
        self.assertEqual(inspection.symmetry, ())
        result = plan_placements(doc, inspection, distribution,
            PlacementOptions(max_added_layers=1), include_advice=False)
        self.assertTrue(result.ok, result)
        points = {(p.x, p.y, p.z) for p in result.placements}
        self.assertEqual(points, {(12-x, y, z) for x, y, z in points})

    def test_minority_correction_searches_past_first_distant_legal_layout(self):
        doc, _, distribution = sample([(4, 4, 0), (8, 4, 0), (5, 5, 1)], moved=1)
        doc.tiles[1] = replace(doc.tiles[1], tile_id=-11)
        distribution = replace(distribution, move_type='majority_A', replenishment_ids=(-11,))
        result = plan_placements(doc, inspect_template(doc), distribution,
            PlacementOptions(max_added_layers=0), include_advice=False)
        self.assertTrue(result.ok, result)
        point = result.placements[0]
        self.assertLessEqual((point.x-8)**2+(point.y-4)**2+point.z**2, 8)

    def test_only_required_minority_correction_can_use_original_lower_layer(self):
        doc, _, distribution = sample([(4,4+z%2,z) for z in range(8)], moved=1)
        doc.tiles[0] = replace(doc.tiles[0], tile_id=-11)
        distribution = replace(distribution, move_type='majority_A', replenishment_ids=(-11,))
        from scripts.tower_reducer.placement import SearchBudget
        budget=SearchBudget(10000)
        result=plan_placements(doc,inspect_template(doc),distribution,
            PlacementOptions(max_added_layers=0),include_advice=False,search_budget=budget)
        self.assertTrue(result.ok,result)
        self.assertEqual(len(result.placements),1)
        self.assertLessEqual(result.placements[0].z,2)

    def test_majority_tiles_stay_at_selected_top_after_low_minority_prefix(self):
        doc, _, distribution = sample([(4,4+z%2,z) for z in range(8)], moved=3)
        doc.tiles[0] = replace(doc.tiles[0],tile_id=-11)
        distribution=replace(distribution,move_type='majority_A',replenishment_ids=(-11,-10,-10))
        result=plan_placements(doc,inspect_template(doc),distribution,
            PlacementOptions(max_added_layers=1),include_advice=False)
        self.assertTrue(result.ok,result)
        by_index={p.tile_index:p for p in result.placements}
        self.assertLessEqual(by_index[8].z,2)
        self.assertTrue(all(by_index[i].z>=7 for i in (9,10)))

    def test_minority_prefix_budget_exhaustion_is_reported_as_incomplete(self):
        doc, _, distribution=sample([(4,4+z%2,z) for z in range(8)],moved=1)
        doc.tiles[0]=replace(doc.tiles[0],tile_id=-11)
        distribution=replace(distribution,move_type='majority_A',replenishment_ids=(-11,))
        from scripts.tower_reducer.placement import SearchBudget
        result=plan_placements(doc,inspect_template(doc),distribution,
            PlacementOptions(max_added_layers=0),include_advice=False,search_budget=SearchBudget(1))
        self.assertFalse(result.ok)
        self.assertEqual(result.code,'search_limit')

    def test_top_rotation_pattern_does_not_invent_left_right_reflection(self):
        doc, inspection, distribution = sample(
            [(4,4,0), (8,5,0), (4,6,0), (8,7,0), (4,8,0),
             (5,5,1), (7,6,1)], moved=2)
        result = plan_placements(doc, inspection, distribution,
            PlacementOptions(max_added_layers=1), include_advice=False)
        self.assertTrue(result.ok, result)
        points = {(p.x,p.y,p.z) for p in result.placements}
        self.assertEqual(points, {(12-x,11-y,z) for x,y,z in points})


if __name__ == '__main__':
    unittest.main()
