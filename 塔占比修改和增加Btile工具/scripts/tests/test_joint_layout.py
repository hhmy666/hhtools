"""A ratio is an upper bound, not a fixed target tower population."""
import unittest

from scripts.tests.test_rewrite import scalar, message
from scripts.tests.test_api import ApiContractCase
from scripts.tower_reducer import placement, distribution
from scripts.tower_reducer.distribution import DistributionOptions, plan_tower_distribution
from scripts.tower_reducer.inspection import inspect_template
from scripts.tower_reducer.placement import PlacementOptions, SearchBudget, plan_placements
from scripts.tower_reducer.rewrite import RewritePlan, rewrite_template
from scripts.tower_reducer.template_reader import decode_template, encode_template
from scripts.tower_reducer.verification import verify_output


def symmetric_source():
    # Nine A tiles, three in one tower. All legal new positions come in
    # left/right pairs: moving one cannot work, moving two can work.
    points = [(4,4,0),(8,4,0),(12,4,0),(16,4,0),(5,5,1),(15,5,1)]
    points += [(10,16,z) for z in range(3)]
    parts = [message(2,b'joint_case'), scalar(4,20), scalar(5,19)]
    for i, (x,y,z) in enumerate(points):
        body = scalar(1,x)+scalar(2,y)+scalar(3,z)+scalar(4,-10)
        parts.append(message(6,body+(scalar(5,0) if i >= 6 else b'')))
    parts.append(message(7,scalar(1,10)+scalar(2,16)+scalar(3,2)))
    return b''.join(parts)


def xkd090_geometry_source():
    """Portable snapshot of the real template's 94 surface/44 tower tiles.

    Only geometry needed for this regression is encoded; the live template
    and its unrelated wire fields are not copied or modified by this fixture.
    """
    layers = [
        [(4,5),(8,5),(12,5),(6,7),(10,7),(4,9),(8,9),(12,9)],
        [(3,4),(7,4),(9,4),(13,4),(5,6),(7,6),(9,6),(11,6),(5,8),(7,8),(9,8),(11,8),(3,10),(7,10),(9,10),(13,10)],
        [(6,3),(8,3),(10,3),(4,5),(8,5),(12,5),(4,7),(6,7),(8,7),(10,7),(12,7),(4,9),(8,9),(12,9),(6,11),(8,11),(10,11)],
        [(7,2),(9,2),(3,4),(7,4),(9,4),(13,4),(5,6),(7,6),(9,6),(11,6),(5,8),(7,8),(9,8),(11,8),(3,10),(7,10),(9,10),(13,10),(7,12),(9,12)],
        [(6,3),(8,3),(10,3),(4,5),(6,5),(8,5),(10,5),(12,5),(4,7),(6,7),(8,7),(10,7),(12,7),(4,9),(6,9),(8,9),(10,9),(12,9),(6,11),(8,11),(10,11)],
        [(7,4),(9,4),(5,6),(7,6),(9,6),(11,6),(5,8),(7,8),(9,8),(11,8),(7,10),(9,10)],
    ]
    parts = [message(2,b'xkd090'), scalar(4,17), scalar(5,19)]
    for z, points in enumerate(layers):
        for x,y in points:
            parts.append(message(6,scalar(1,x)+scalar(2,y)+scalar(3,z)+scalar(4,-10)))
    for index,(x,y,count) in enumerate(((3,14,12),(6,15,10),(10,15,10),(13,14,12))):
        for z in range(count):
            parts.append(message(6,scalar(1,x)+scalar(2,y)+scalar(3,z)+scalar(4,-10)+scalar(5,index)))
        parts.append(message(7,scalar(1,x)+scalar(2,y)+scalar(3,2)))
    return b''.join(parts)


class JointLayoutTests(unittest.TestCase):
    def solve(self, doc, inspection, dopt=DistributionOptions(), popt=PlacementOptions(), **kwargs):
        function = getattr(placement, 'plan_joint_layout', None)
        if function:
            return function(doc, inspection, dopt, popt, **kwargs)
        # Exercise the old single-distribution flow for a meaningful RED.
        d = plan_tower_distribution(doc, inspection, dopt)
        p = plan_placements(doc, inspection, d, popt, include_advice=False)
        class Result:
            distribution = d
            placement = p
            ok = p.ok
            checked_distributions = 1
            complete = True
        return Result()

    def test_auto_mode_finds_a_valid_lower_population_without_changing_any_user_parameter(self):
        raw = symmetric_source()
        doc = decode_template(raw)
        inspection = inspect_template(doc)
        result = self.solve(doc, inspection)
        self.assertTrue(result.ok, result.placement)
        self.assertEqual(result.distribution.retained_counts, (1,))
        self.assertEqual(result.distribution.move_count, 2)
        self.assertGreater(result.checked_distributions, 1)
        plan = RewritePlan(result.distribution, result.placement, DistributionOptions(), PlacementOptions())
        report = verify_output(raw, rewrite_template(raw, plan, 'lessTjoint_case'), plan, 'lessTjoint_case')
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.facts['options']['distribution'],
                         {'ratio': .25, 'retained_counts': None, 'min_ratio': 0.0, 'replenish_type': 'auto'})
        self.assertEqual(report.facts['tile_counts'], {'total': 9, 'A': 9, 'B': 0})
        self.assertEqual(encode_template(doc), raw, 'Planning must not mutate the source')

    def test_explicit_per_tower_counts_are_not_silently_changed(self):
        doc = decode_template(symmetric_source())
        result = self.solve(doc, inspect_template(doc), DistributionOptions(.25,(2,)))
        self.assertFalse(result.ok)
        self.assertEqual(result.distribution.retained_counts, (2,))
        self.assertEqual(result.checked_distributions, 1)

    def test_budget_exhaustion_is_inconclusive_not_capacity_proof(self):
        doc = decode_template(symmetric_source())
        result = self.solve(doc, inspect_template(doc), search_budget=SearchBudget(0))
        self.assertFalse(result.ok)
        self.assertFalse(result.complete)
        self.assertEqual(result.placement.code, 'search_limit')
        self.assertEqual(dict(result.placement.evidence)['available_states_at_start'], 0)

    def test_candidate_iterator_covers_legal_counts_below_original_cap(self):
        doc = decode_template(symmetric_source())
        function = getattr(distribution, 'iter_tower_distributions', None)
        candidates = list(function(doc, inspect_template(doc), DistributionOptions(), SearchBudget(100))) if function else []
        self.assertEqual({candidate.retained_counts for candidate in candidates}, {(2,), (1,), (0,)})
        self.assertEqual(len(candidates), 3)
        self.assertTrue(all(candidate.cap == 2 and candidate.final_ratio < .25 for candidate in candidates))

    def test_iterator_covers_all_two_group_symmetric_populations_not_just_a_lower_ratio_target(self):
        from dataclasses import replace
        from scripts.tests.test_distribution import source
        doc = source(['A'*12,'A'*10,'A'*10,'A'*12], extra_a=94)
        inspection = replace(inspect_template(doc), symmetry=('horizontal',))
        function = getattr(distribution, 'iter_tower_distributions', None)
        candidates = list(function(doc, inspection, DistributionOptions(), SearchBudget(10000))) if function else []
        expected = {(a,b,b,a) for a in range(13) for b in range(11) if a+b <= 17}
        self.assertEqual({candidate.retained_counts for candidate in candidates}, expected)
        self.assertEqual(len(candidates), len(expected))
        self.assertTrue(all(candidate.cap == 34 for candidate in candidates))


class JointLayoutHttpTests(ApiContractCase):
    def test_xkd090_original_25_percent_top_first_20_tiles_two_layers_succeeds(self):
        self.start_server()
        imported = self.import_files([('xkd090_m.bytes', xkd090_geometry_source())])
        options = {'ratio': .25, 'retained_counts': None, 'strategy': 'top_first',
                   'max_tiles_per_layer': 20, 'max_added_layers': 2, 'seed': 1}
        result = self.wait_job(self.start_job(imported['import_id'], defaults=options))
        row = self.rows(result)[0]
        self.assertEqual(row['status'], 'verified')
        self.assertEqual(row['parameters']['ratio'], .25)
        self.assertIsNone(row['parameters']['retained_counts'])
        # Keep the first legal tower population, but honour top-first instead
        # of silently filling arbitrary middle layers as the old fallback did.
        self.assertEqual(row['plan_summary']['retained_counts'], [9,8,8,9])
        self.assertEqual(row['plan_summary']['move_count'], 10)
        self.assertEqual(row['plan_summary']['added_layers'], 1)
        self.assertEqual(row['verification']['facts']['tile_counts'], {'total':138,'A':138,'B':0})

    def test_original_auto_options_succeed_without_asking_user_to_reduce_the_ceiling(self):
        self.start_server()
        imported = self.import_files([('joint_case_m.bytes', symmetric_source())])
        result = self.wait_job(self.start_job(imported['import_id']))
        row = self.rows(result)[0]
        self.assertEqual(row['status'], 'verified')
        self.assertEqual(row['parameters']['ratio'], .25)
        self.assertIsNone(row['parameters']['retained_counts'])
        self.assertEqual(row['plan_summary']['retained_counts'], [1])
        self.assertEqual(len(result['artifacts']), 1)

    def test_failed_manual_layout_explains_the_unplaced_tile_count(self):
        self.start_server()
        imported = self.import_files([('joint_case_m.bytes', symmetric_source())])
        result = self.wait_job(self.start_job(imported['import_id'], defaults={
            'ratio': .25, 'retained_counts': [2], 'strategy': 'top_first',
            'max_tiles_per_layer': 20, 'max_added_layers': 2, 'seed': 0}))
        row = self.rows(result)[0]
        self.assertEqual(row['status'], 'failed')
        self.assertRegex(row['error']['message'], r'需要摆放\s*1\s*张.*排下\s*0\s*张.*1\s*张')
        self.assertNotIn('容量不足', row['error']['message'])
