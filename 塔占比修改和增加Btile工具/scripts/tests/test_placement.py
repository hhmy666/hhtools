import copy
import subprocess
import sys
import unittest
from pathlib import Path

from scripts.tests.project_fixtures import read_project_fixture
from scripts.tower_reducer.distribution import DistributionOptions, DistributionPlan, plan_tower_distribution
from scripts.tower_reducer.inspection import inspect_template
from scripts.tower_reducer.models import TemplateDocument, Tile, Stack
from scripts.tower_reducer.template_reader import decode_template
from scripts.tower_reducer.placement import (
    PlacementState, PlacementOptions, PlacementFailure, enumerate_legal_positions, plan_placements,
)

def sample(points, moved=1):
    tiles = [Tile(i, i, x, y, z, -10, None, (), (x, y, z, -10, None))
             for i, (x, y, z) in enumerate(points)]
    n = len(tiles)
    for z in range(moved):
        tiles.append(Tile(n+z, n+z, 2, 16, z, -10, None, (), (2, 16, z, -10, None)))
    doc = TemplateDocument(b'', (), 'sample', 0, tiles, [Stack(0, n, 2, 16, 2, ())], [], width=17, height=19)
    return doc, inspect_template(doc), DistributionPlan((0,), tuple(range(n, n+moved)), 'A', moved, 0, 0)


class CandidateTests(unittest.TestCase):
    def test_missing_immediate_lower_layer_is_not_supported_by_upper_layer(self):
        state=PlacementState((17,19),{1:[(4,4)],2:[(5,5)]},{},layer_profiles={2:((1,),(1,1))})
        self.assertEqual(enumerate_legal_positions(state,1),[])

    def test_new_layer_prefers_newly_added_support_over_original_support(self):
        from scripts.tower_reducer.placement import _append, _layer_choice, SearchBudget
        state = PlacementState((17,19), {0:[(4,4),(8,8)],1:[(3,3)]}, {},
                               layer_profiles={1:((1,),)}, single_profile=(1,), stacking_unit=1)
        first = next(p for p in enumerate_legal_positions(state,1) if p.orbit==((9,9),))
        _append(state,1,[first],[])
        choice,_ = _layer_choice(state,2,1,PlacementOptions(),SearchBudget(),new=True)
        self.assertEqual([(p.x,p.y) for p in choice],[(8,8)])
        self.assertEqual(choice[0].profile,(1,))

    def test_new_layer_cannot_change_quarter_overlap_to_half_overlap(self):
        state = PlacementState((17,19), {0:[(4,4)]}, {},
                               layer_profiles={1:((1,),(1,1))}, stacking_unit=1)
        # Different support counts of quarter-overlaps remain quarter style.
        state.layer_profiles = {}
        self.assertFalse(enumerate_legal_positions(state,1,(2,)))
        self.assertTrue(enumerate_legal_positions(state,1,(1,)))

    def test_new_layer_inherits_immediate_lower_layer_not_other_source_layers(self):
        state = PlacementState((17,19), {0:[(4,4),(10,4)], 1:[(5,5),(10,5)], 2:[(6,6)]}, {})
        self.assertFalse(enumerate_legal_positions(state,3,(2,)))
        self.assertTrue(enumerate_legal_positions(state,3,(1,)))

    def test_new_upper_layer_can_use_a_tile_just_added_to_original_top(self):
        from scripts.tower_reducer.placement import _append, _layer_choice, SearchBudget
        state = PlacementState((17,19), {0:[(4,4)],1:[(5,5)]}, {},
                               layer_profiles={1:((1,),)}, single_profile=(1,), stacking_unit=1)
        first = next(p for p in enumerate_legal_positions(state,1) if p.orbit==((3,3),))
        destinations=[]
        _append(state,1,[first],destinations)
        choice,_ = _layer_choice(state,2,1,PlacementOptions(),SearchBudget(),new=True)
        self.assertEqual([(p.x,p.y) for p in choice],[(2,2)])

    def test_same_layer_and_internal_orbit_collision(self):
        state = PlacementState((17, 19), {0: [(4,4)], 1: [(5,5)]}, {})
        points = {p.orbit[0] for p in enumerate_legal_positions(state, 1)}
        self.assertNotIn((4,5), points)
        self.assertNotIn((5,4), points)
        state = PlacementState((17, 19), {0: [(4,4),(7,4)], 1: []}, {}, (11,8), ('horizontal',))
        candidates = enumerate_legal_positions(state, 1)
        self.assertTrue(all((5,5) not in p.orbit for p in candidates))
        self.assertGreater(state.rejected.get('orbit_collision', 0), 0)

    def test_tower_footprint_collision_support_and_full_cover_filter(self):
        state = PlacementState((17, 19), {0: [(4, 4)], 1: []}, {1: [(2, 2)]})
        candidates = enumerate_legal_positions(state, 1, None)
        points = {p.orbit[0] for p in candidates}
        self.assertIn((5, 5), points)
        self.assertIn((4, 5), points)
        self.assertNotIn((4, 4), points)
        self.assertNotIn((3, 3), points)
        self.assertNotIn((10, 10), points)
        self.assertGreater(state.rejected.get('tower_collision', 0), 0)
        self.assertGreater(state.rejected.get('full_cover', 0), 0)

    def test_orbit_is_never_split_when_mirror_is_outside_board(self):
        state = PlacementState((8, 19), {0: [(4, 4)], 1: []}, {}, (16, 18), ('horizontal',))
        self.assertEqual(enumerate_legal_positions(state, 1, None), [])

    def test_bottom_addition_revalidates_affected_upper_profile(self):
        state = PlacementState((17, 19), {0: [(4, 4)], 1: [(5, 5)]}, {},
                               layer_profiles={1: ((1,),)}, single_profile=(1,))
        self.assertFalse(enumerate_legal_positions(state, 0, None))
        state.single_profile = None
        state.layer_profiles[1] = ((1,), (1, 1))
        self.assertIn((6, 4), {p.orbit[0] for p in enumerate_legal_positions(state, 0, None)})


class PlanningTests(unittest.TestCase):
    def test_capacity_advice_includes_existing_tiles_outside_new_tile_safe_region(self):
        # Each source layer has one safe Tile plus 56 preserved historical
        # Tiles outside the new-Tile safety region, all inside a wider board.
        points = [(4,4,0),(5,5,1)]
        points += [(x,4,0) for x in range(20,132,2)]
        points += [(x,5,1) for x in range(21,133,2)]
        doc, _, distribution = sample(points,3)
        doc.width = 135
        inspected = inspect_template(doc)
        self.assertTrue(inspected.can_optimize,inspected.issues)
        failure = plan_placements(doc,inspected,distribution,PlacementOptions(max_tiles_per_layer=57,max_added_layers=0))
        self.assertIsInstance(failure,PlacementFailure)
        evidence = dict(failure.evidence)
        self.assertIn(('max_tiles_per_layer',60,3),evidence['recommendation_checks'])
        self.assertIn(('max_tiles_per_layer',58,113),evidence['recommendation_search']['requested_ranges'])
        self.assertIn(('max_tiles_per_layer',58,60),evidence['recommendation_search']['checked_ranges'])
        verified = plan_placements(doc,inspected,distribution,PlacementOptions(max_tiles_per_layer=60,max_added_layers=0))
        self.assertTrue(verified.ok,verified)
        self.assertEqual([(p.x,p.y,p.z) for p in verified.placements],[(3,3,1),(3,5,1),(5,3,1)])

    def test_incomplete_advice_records_checked_ranges_and_pending_trial(self):
        from scripts.tower_reducer.placement import _parameter_advice, SearchBudget
        state = PlacementState((17,19),{0:[(4,4)],1:[(5,5)]},{},
                               layer_profiles={1:((1,),)},single_profile=(1,))
        advice, checks, status = _parameter_advice(state,7,PlacementOptions(max_tiles_per_layer=1,max_added_layers=0),SearchBudget(1))
        self.assertFalse(status['complete'])
        self.assertEqual(status['requested_ranges'],(('max_tiles_per_layer',2,57),('max_added_layers',1,7)))
        self.assertEqual(status['checked_ranges'],())
        self.assertEqual(status['interrupted_trial'],('max_tiles_per_layer',2))
        self.assertEqual(checks,())
        self.assertTrue(any('未完成' in a and '已检查范围' in a for a in advice))

    def test_advice_can_validate_more_than_four_additional_layers(self):
        args = sample([(4,4,0),(5,5,1)],7)
        options = PlacementOptions(max_tiles_per_layer=1,max_added_layers=0)
        failure = plan_placements(*args,options)
        self.assertIsInstance(failure,PlacementFailure)
        self.assertIn(('max_added_layers',7,7),dict(failure.evidence)['recommendation_checks'])
        verified = plan_placements(*args,PlacementOptions(max_tiles_per_layer=1,max_added_layers=7))
        self.assertTrue(verified.ok,verified)
        self.assertEqual(verified.added_layers,7)

    def test_advice_recomputes_and_searches_all_strategy_splits(self):
        args = sample([(4,4,0),(6,4,0),(10,4,0),(5,5,1),(11,5,1)],4)
        # Original cap3 has no bottom space; cap5 enables one bottom + three
        # top placements. Keeping the original target0 incorrectly needs cap6.
        for strategy in ('balanced','seeded_random'):
            with self.subTest(strategy=strategy):
                options = PlacementOptions(strategy=strategy,seed=2,max_tiles_per_layer=3,max_added_layers=0)
                failure = plan_placements(*args,options)
                self.assertIsInstance(failure,PlacementFailure)
                self.assertIn(('max_tiles_per_layer',5,4),dict(failure.evidence)['recommendation_checks'])
                self.assertTrue(any('max_tiles_per_layer >= 5' in a for a in failure.recommendations))
                verified = plan_placements(*args,PlacementOptions(strategy=strategy,seed=2,max_tiles_per_layer=5,max_added_layers=0))
                self.assertTrue(verified.ok,verified)
                self.assertGreaterEqual(sum(p.z==0 for p in verified.placements),1)

    def test_seed_does_not_change_the_new_layer_inherited_overlap_unit(self):
        args = sample([(4,4,0),(6,4,0),(10,4,0),(5,5,1),(11,5,1)],4)
        profiles = set()
        for seed in range(6):
            options = PlacementOptions(strategy='seeded_random',seed=seed,max_tiles_per_layer=2,max_added_layers=2)
            choice = plan_placements(*args,options)
            again = plan_placements(*args,options)
            self.assertTrue(choice.ok,choice)
            self.assertEqual(choice,again)
            original={z+choice.original_z_offset for z in args[1].non_tower_layers}
            new_tiles=[p for p in choice.placements if p.z not in original]
            self.assertTrue(new_tiles)
            self.assertTrue(all(area==1 for p in new_tiles for area in p.profile))
            profiles.update(area for p in new_tiles for area in p.profile)
        self.assertEqual(profiles,{1})

    def test_every_strategy_preserves_final_geometry_and_layer_profiles(self):
        for name in ('mxd0057v1','hr0029','hr0044','mxd5010'):
            doc = decode_template(read_project_fixture(name+'_m.bytes'))
            inspected = inspect_template(doc)
            distribution = plan_tower_distribution(doc, inspected, DistributionOptions())
            for strategy in ('top_first','bottom_first','balanced','seeded_random'):
                with self.subTest(name=name,strategy=strategy):
                    result = plan_placements(doc, inspected, distribution, PlacementOptions(strategy=strategy,seed=42))
                    self.assertTrue(result.ok,result)
                    anchors={(s.x,s.y) for s in doc.stacks}
                    final = [(t.x,t.y,t.z+(result.original_z_offset if (t.x,t.y) not in anchors else 0)) for t in doc.tiles if t.index not in distribution.moved_tile_indices]
                    for p in result.placements:
                        self.assertTrue(2 <= p.x <= 14 and 2 <= p.y <= 16)
                        self.assertFalse(any(z == p.z and abs(x-p.x)<2 and abs(y-p.y)<2 for x,y,z in final))
                        final.append((p.x,p.y,p.z))
                    for z in set(p.z for p in result.placements if p.z > max(inspected.non_tower_layers)):
                        self.assertEqual(len({area for p in result.placements if p.z == z for area in p.profile}), 1)
                    self.assertEqual(len({p.tile_index for p in result.placements}), distribution.move_count)

    def test_seed_changes_only_legal_selection_and_reproduces_same_seed(self):
        args = sample([(4,4,0),(6,4,0),(10,4,0),(5,5,1),(11,5,1)],4)
        first = plan_placements(*args, PlacementOptions(strategy='seeded_random',seed=1))
        second = plan_placements(*args, PlacementOptions(strategy='seeded_random',seed=2))
        self.assertTrue(first.ok and second.ok)
        self.assertNotEqual(first.placements,second.placements)
        self.assertEqual(first,plan_placements(*args,PlacementOptions(strategy='seeded_random',seed=1)))

    def test_very_large_layer_limit_is_bounded_by_geometry(self):
        args = sample([(4,4,0),(5,5,1)],4)
        result = plan_placements(*args, PlacementOptions(max_tiles_per_layer=10**9,max_added_layers=10**9))
        self.assertTrue(result.ok,result)

    def test_four_goldens_preserve_moved_indices_source_and_new_directional_geometry(self):
        for name in ('mxd0057v1', 'hr0029', 'hr0044', 'mxd5010'):
            with self.subTest(name=name):
                doc = decode_template(read_project_fixture(name+'_m.bytes'))
                before = copy.deepcopy(doc)
                inspected = inspect_template(doc)
                distribution = plan_tower_distribution(doc, inspected, DistributionOptions())
                result = plan_placements(doc, inspected, distribution, PlacementOptions())
                self.assertTrue(result.ok, result)
                self.assertEqual([p.tile_index for p in result.placements],sorted(distribution.moved_tile_indices))
                from scripts.tower_reducer.rewrite import RewritePlan,rewrite_template
                from scripts.tower_reducer.verification import verify_output
                raw=read_project_fixture(name+'_m.bytes')
                plan=RewritePlan(distribution,result,DistributionOptions(),PlacementOptions())
                report=verify_output(raw,rewrite_template(raw,plan,'new'),plan,'new')
                self.assertTrue(report.ok,report.errors)
                self.assertEqual(doc, before)

    def test_no_tower_is_successful_empty_plan(self):
        doc = decode_template(read_project_fixture('ntxkd021_m.bytes'))
        inspected = inspect_template(doc)
        result = plan_placements(doc, inspected, plan_tower_distribution(doc, inspected, DistributionOptions()), PlacementOptions())
        self.assertTrue(result.ok)
        self.assertEqual(result.placements, ())

    def test_bottom_and_balanced_use_first_layer_without_negative_z(self):
        args = sample([(4,4,0), (6,4,0), (10,4,0), (5,5,1), (11,5,1)], 4)
        for strategy in ('bottom_first', 'balanced', 'top_first', 'seeded_random'):
            result = plan_placements(*args, PlacementOptions(strategy=strategy, seed=123))
            self.assertTrue(result.ok, result)
            self.assertEqual(len(result.placements), 4)
            self.assertTrue(all(p.z >= 0 for p in result.placements))
            bottom = sum(p.z == 0 for p in result.placements)
            if strategy == 'bottom_first': self.assertGreater(bottom, 0)
            # The first upper Tile already covers two lower Tiles; only the
            # second upper Tile can gain one more lower Tile. Bottom capacity=1.
            if strategy == 'balanced': self.assertEqual(bottom, 2)
            if strategy == 'top_first': self.assertEqual(bottom, 0)
            self.assertEqual(result, plan_placements(*args, PlacementOptions(strategy=strategy, seed=123)))

    def test_single_profile_new_layers_keep_exact_signature(self):
        args = sample([(4,4,0), (5,5,1)], 4)
        result = plan_placements(*args, PlacementOptions(max_tiles_per_layer=1, max_added_layers=5))
        self.assertTrue(result.ok, result)
        self.assertTrue(all(p.profile == (1,) for p in result.placements))
        self.assertGreater(max(p.z for p in result.placements), 3)

    def test_configurable_limits_above_defaults_and_capacity_failure(self):
        doc = decode_template(read_project_fixture('mxd5010_m.bytes'))
        inspected = inspect_template(doc)
        distribution = plan_tower_distribution(doc, inspected, DistributionOptions())
        result = plan_placements(doc, inspected, distribution, PlacementOptions(max_tiles_per_layer=40, max_added_layers=4))
        self.assertTrue(result.ok, result)
        original_layers = plan_placements(doc, inspected, distribution, PlacementOptions(strategy='seeded_random',max_added_layers=0))
        self.assertTrue(original_layers.ok,original_layers)
        self.assertEqual(original_layers.added_layers,0)
        failure = plan_placements(doc, inspected, distribution, PlacementOptions(max_tiles_per_layer=4,max_added_layers=0))
        self.assertIsInstance(failure, PlacementFailure)
        self.assertGreater(failure.shortage, 0)
        self.assertTrue(failure.layers)
        self.assertTrue(failure.rejected)
        self.assertTrue(failure.recommendations)
        self.assertTrue(any('max_added_layers' in advice and '验证' in advice
                            for advice in failure.recommendations), failure.recommendations)

    def test_invalid_options_are_structured_failures(self):
        args = sample([(4,4,0), (5,5,1)], 1)
        for kwargs in ({'strategy':'unknown'}, {'max_tiles_per_layer':0}, {'max_tiles_per_layer':True},
                       {'max_tiles_per_layer':2.5}, {'max_added_layers':-1}, {'max_added_layers':False}, {'seed':1.2}):
            self.assertEqual(plan_placements(*args, PlacementOptions(**kwargs)).code, 'invalid_options')

    def test_invalid_distribution_does_not_slice_with_invalid_retained_count(self):
        from dataclasses import replace
        doc, inspected, distribution = sample([(4,4,0), (5,5,1)], 1)
        for count in (1.5, True, -1, 9):
            result = plan_placements(doc, inspected, replace(distribution, retained_counts=(count,)))
            self.assertEqual(result.code, 'invalid_distribution')
        for count in ('1', None, True):
            result = plan_placements(doc, inspected, replace(distribution, move_count=count))
            self.assertEqual(result.code, 'invalid_distribution')

    def test_bottom_combination_can_complete_a_three_overlap_profile(self):
        # Upper (5,5) has profile (1,1,1); upper (11,5) has (1,).
        # Two bottom additions under (11,5) jointly restore a source profile.
        args = sample([(4,4,0),(6,4,0),(4,6,0),(10,4,0), (5,5,1),(11,5,1),
                       (12,10,0),(14,10,0)], 4)
        result = plan_placements(*args, PlacementOptions(strategy='balanced'))
        self.assertTrue(result.ok, result)
        self.assertEqual(sum(p.z == 0 for p in result.placements), 2)

    def test_dense_search_has_explicit_runtime_boundary(self):
        script = '''from scripts.tower_reducer.placement import *
s=PlacementState((17,19),{0:[(x,y) for x in range(2,15,2) for y in range(2,17,2)],1:[]},{})
c=enumerate_legal_positions(s,1,None)
assert len(c)>50
from scripts.tower_reducer.placement import _choose_orbits, SearchBudget, SearchLimit
try:
    _choose_orbits(c,48,SearchBudget(30000))
except SearchLimit:
    pass
'''
        run = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)
