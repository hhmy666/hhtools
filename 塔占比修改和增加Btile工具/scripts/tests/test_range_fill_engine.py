"""Ratio ranges and user-selected replenishment must survive every engine stage."""
from dataclasses import replace
import tempfile
from pathlib import Path
import unittest

from scripts.tests.test_distribution import source
from scripts.tests.test_joint_layout import symmetric_source
from scripts.tests.test_rewrite import computed_plan, message, scalar, synthetic
from scripts.tower_reducer import inspection as inspection_module
from scripts.tower_reducer.distribution import DistributionOptions, iter_tower_distributions, plan_tower_distribution
from scripts.tower_reducer.inspection import inspect_template
from scripts.tower_reducer.parameter_advice import verified_parameter_advice
from scripts.tower_reducer.placement import PlacementOptions, SearchBudget, plan_joint_layout
from scripts.tower_reducer.rewrite import RewritePlan, rewrite_template
from scripts.tower_reducer.storage import InputSnapshot, JobRepository, atomic_write_json
from scripts.tower_reducer.template_reader import decode_template
from scripts.tower_reducer.verification import verify_output


def range_options(ratio=.25, retained_counts=None, min_ratio=0, replenish_type='auto'):
    return DistributionOptions(ratio, retained_counts, min_ratio, replenish_type)


class RangeDistributionTests(unittest.TestCase):
    def test_manual_counts_below_inclusive_lower_bound_fail(self):
        doc = source(['AAAAAA'], extra_a=18)
        result = plan_tower_distribution(doc, inspect_template(doc), range_options(.25, (4,), .2))
        self.assertFalse(result.ok, result)
        self.assertEqual(result.code, 'tower_ratio_below_minimum')

    def test_lower_bound_exactly_equal_to_actual_ratio_is_allowed(self):
        doc = source(['AAAAAA'], extra_a=18)
        result = plan_tower_distribution(doc, inspect_template(doc), range_options(.25, (3,), .125))
        self.assertTrue(result.ok, result)
        self.assertEqual(result.final_ratio, .125)

    def test_original_tower_count_below_lower_bound_fails_without_adding_towers(self):
        doc = source(['AAA'], extra_a=24)
        result = plan_tower_distribution(doc, inspect_template(doc), range_options(.25, min_ratio=.2))
        self.assertFalse(result.ok, result)
        self.assertEqual(result.code, 'tower_ratio_range_unattainable')
        self.assertIn('原', result.message)

    def test_no_discrete_tower_count_between_bounds_fails_clearly(self):
        doc = decode_template(symmetric_source())
        result = plan_tower_distribution(doc, inspect_template(doc), range_options(.22, min_ratio=.2))
        self.assertFalse(result.ok, result)
        self.assertEqual(result.code, 'tower_ratio_range_unattainable')
        self.assertIn('整数', result.message)

    def test_divisibility_cannot_silently_reduce_towers_below_minimum(self):
        doc = source(['BBBAAA'], extra_a=3, extra_b=6)
        result = plan_tower_distribution(doc, inspect_template(doc), range_options(.2, min_ratio=.1))
        self.assertFalse(result.ok, result)
        self.assertEqual(result.code, 'tower_ratio_range_unattainable')

    def test_joint_search_never_falls_below_minimum_to_solve_geometry(self):
        doc = decode_template(symmetric_source())
        options = range_options(.25, min_ratio=.2)
        candidates = list(iter_tower_distributions(doc, inspect_template(doc), options, SearchBudget(100)))
        self.assertEqual({p.retained_counts for p in candidates}, {(2,)})
        result = plan_joint_layout(doc, inspect_template(doc), options)
        self.assertFalse(result.ok, result)
        self.assertEqual(result.distribution.retained_counts, (2,))

    def test_invalid_ranges_are_rejected_and_zero_range_clears_towers(self):
        doc = source(['AAAAAA'], extra_a=18)
        for minimum, maximum in ((.2, .2), (.21, .2), (-.1, .2), (True, .2), (float('nan'), .2)):
            with self.subTest(minimum=minimum, maximum=maximum):
                result = plan_tower_distribution(doc, inspect_template(doc), range_options(maximum, min_ratio=minimum))
                self.assertFalse(result.ok, result)
                self.assertEqual(result.code, 'invalid_ratio')
        result = plan_tower_distribution(doc, inspect_template(doc), range_options(0, min_ratio=0))
        self.assertTrue(result.ok, result)
        self.assertEqual(result.retained_counts, (0,))

    def test_no_tower_source_cannot_satisfy_positive_minimum(self):
        doc = source([], extra_a=9)
        result = plan_tower_distribution(doc, inspect_template(doc), range_options(.25, min_ratio=.1))
        self.assertFalse(result.ok, result)

    def test_explicit_type_overrides_global_minority_and_still_conserves_counts(self):
        doc = source(['BBBAAA'], extra_a=3, extra_b=6)
        result = plan_tower_distribution(doc, inspect_template(doc), range_options(.25, (3,), .1, 'B'))
        self.assertTrue(result.ok, result)
        self.assertEqual(result.move_type, 'B')
        self.assertEqual(result.removed_counts, (3, 0))
        self.assertEqual(result.final_counts, (3, 12))

    def test_explicit_b_can_convert_original_a_only_source(self):
        doc = source(['AAAAAA'], extra_a=18)
        result = plan_tower_distribution(doc, inspect_template(doc), range_options(.25, min_ratio=.1, replenish_type='B'))
        self.assertTrue(result.ok, result)
        self.assertEqual(result.retained_counts, (3,))
        self.assertEqual(result.move_type, 'B')
        self.assertEqual(result.final_counts, (21, 3))
        automatic = plan_tower_distribution(doc, inspect_template(doc), DistributionOptions())
        self.assertEqual(automatic.move_type, 'A')
        self.assertEqual(automatic.retained_counts, (5,))

    def test_reduce_upper_above_source_is_not_treated_as_no_movement_success(self):
        doc = source(['AAA'], extra_a=24)
        result = plan_tower_distribution(doc, inspect_template(doc), range_options(.25, min_ratio=.1, replenish_type='B'))
        self.assertFalse(result.ok, result)
        self.assertEqual(result.code, 'reduce_ratio_above_source')

    def test_invalid_replenishment_type_is_rejected(self):
        doc = source(['AAAAAA'], extra_a=18)
        result = plan_tower_distribution(doc, inspect_template(doc), range_options(replenish_type='C'))
        self.assertFalse(result.ok, result)
        self.assertEqual(result.code, 'invalid_replenish_type')


class RewriteRangeFillTests(unittest.TestCase):
    def test_no_tower_explicit_fill_rewrites_and_verifies_unchanged_tiles(self):
        points = [(4,4,0),(8,4,0),(12,4,0),(5,5,1),(9,5,1),(13,5,1),(4,4,2),(8,4,2),(2,16,0)]
        raw = message(2, b'no_movement') + scalar(4,17) + scalar(5,19)
        raw += b''.join(message(6, scalar(1,x)+scalar(2,y)+scalar(3,z)+scalar(4,-10)) for x,y,z in points)
        plan = computed_plan(raw, range_options(.25, replenish_type='B'))
        self.assertEqual(plan.distribution.move_count, 0)
        output = rewrite_template(raw, plan, 'range_fill')
        report = verify_output(raw, output, plan, 'range_fill')
        self.assertTrue(report.ok, report.errors)
        self.assertIsNone(report.facts['replenishment_type'])
        self.assertEqual(report.facts['tile_counts'], {'total': 9, 'A': 9, 'B': 0})

    def test_actual_rewrite_converts_only_moved_a_only_tiles_to_explicit_b(self):
        raw, _ = synthetic('A')
        options = range_options(0, replenish_type='B')
        plan = computed_plan(raw, options)
        output = rewrite_template(raw, plan, 'range_fill')
        self.assertEqual([t.tile_id for t in decode_template(output).tiles], [-10] * 6 + [-11] * 3)
        report = verify_output(raw, output, plan, 'range_fill')
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.facts['tile_counts'], {'total': 9, 'A': 6, 'B': 3})

    def test_independent_verifier_rejects_output_below_minimum(self):
        raw, _ = synthetic('A')
        plan = computed_plan(raw)
        output = rewrite_template(raw, plan, 'range_fill')
        forged = replace(plan, distribution_options=range_options(.25, min_ratio=.23))
        report = verify_output(raw, output, forged, 'range_fill')
        self.assertFalse(report.ok, report)
        self.assertIn('tower_ratio', {e.code for e in report.errors})

    def test_independent_verifier_rejects_mismatched_explicit_fill(self):
        raw, _ = synthetic('A')
        plan = computed_plan(raw, DistributionOptions(0))
        output = rewrite_template(raw, plan, 'range_fill')
        forged = replace(plan, distribution_options=range_options(0, replenish_type='B'))
        report = verify_output(raw, output, forged, 'range_fill')
        self.assertFalse(report.ok, report)
        self.assertIn('replenish_type', {e.code for e in report.errors})

    def test_rewriter_rejects_plan_with_different_explicit_fill(self):
        raw, _ = synthetic('A')
        plan = computed_plan(raw, DistributionOptions(0))
        forged = replace(plan, distribution_options=range_options(0, replenish_type='B'))
        with self.assertRaises(ValueError):
            rewrite_template(raw, forged, 'range_fill')


class AdviceAndInspectionTests(unittest.TestCase):
    def test_manual_advice_keeps_lower_bound_and_explicit_fill(self):
        raw, _ = synthetic('A', second_tower=True)
        options = range_options(.25, (0, 0), .1, 'B')
        advice = verified_parameter_advice(raw, options, PlacementOptions(max_added_layers=0), 'range_fill')
        self.assertTrue(advice['schemes'], advice)
        for scheme in advice['schemes']:
            parameters = scheme['parameters']
            self.assertEqual(parameters.get('min_ratio'), .1)
            self.assertEqual(parameters.get('replenish_type'), 'B')
            self.assertGreaterEqual(scheme['facts']['tower_ratio'], .1)
            rebuilt = computed_plan(raw, DistributionOptions(parameters['ratio'],
                tuple(parameters['retained_counts']), parameters['min_ratio'], parameters['replenish_type']),
                PlacementOptions(parameters['strategy'], parameters['max_tiles_per_layer'],
                                 parameters['max_added_layers'], parameters['seed']))
            output = rewrite_template(raw, rebuilt, 'range_fill')
            report = verify_output(raw, output, rebuilt, 'range_fill')
            self.assertTrue(report.ok, report.errors)
            self.assertEqual(report.facts['replenishment_type'], 'B')

    def test_advice_has_no_success_below_an_unattainable_minimum(self):
        raw, _ = synthetic('A')
        advice = verified_parameter_advice(raw, range_options(.25, min_ratio=.23),
                                           PlacementOptions(max_added_layers=0), 'range_fill')
        self.assertEqual(advice['schemes'], [], advice)

    def test_original_non_tower_counts_drive_recommendation(self):
        doc = source(['B' * 12], extra_a=9, extra_b=3)
        result = inspect_template(doc)
        self.assertEqual((getattr(result, 'non_tower_a_count', None),
                          getattr(result, 'non_tower_b_count', None)), (9, 3))
        recommendation = getattr(inspection_module, 'recommend_replenish_type', lambda _: None)
        self.assertEqual(recommendation(result), 'A')
        self.assertEqual(recommendation(inspect_template(source(['A'*12], extra_b=9))), 'B')
        self.assertEqual(recommendation(inspect_template(source([], extra_a=3, extra_b=3))), 'A')
        self.assertIsNone(recommendation(inspect_template(source(['AAA']))))

    def test_old_persisted_options_receive_defaults_without_losing_artifact(self):
        raw, plan = synthetic('A')
        with tempfile.TemporaryDirectory() as temporary:
            repo = JobRepository(Path(temporary).resolve() / 'jobs')
            job = repo.create([InputSnapshot(raw, 'sample_m.bytes')])
            key = next(iter(job.snapshot()['templates']))
            output = rewrite_template(raw, plan, 'legacy_range')
            artifact = job.register_output(key, output, plan, 'legacy_range')
            state = job.snapshot()
            stored = state['artifacts'][artifact['id']]['plan']['fields']['distribution_options']['fields']
            stored.pop('min_ratio', None)
            stored.pop('replenish_type', None)
            atomic_write_json(job.directory / 'state.json', state)
            restored = JobRepository(repo.root).discover()[0]
            self.assertEqual(restored.snapshot()['templates'][key]['status'], 'verified')
            self.assertEqual(restored.read_artifact(artifact['id']), output)

    def test_range_and_explicit_fill_survive_persisted_output_revalidation(self):
        raw, _ = synthetic('A', second_tower=True)
        options = range_options(.25, (0, 3), .1, 'B')
        plan = computed_plan(raw, options)
        with tempfile.TemporaryDirectory() as temporary:
            repo = JobRepository(Path(temporary).resolve() / 'jobs')
            job = repo.create([InputSnapshot(raw, 'sample_m.bytes')])
            key = next(iter(job.snapshot()['templates']))
            output = rewrite_template(raw, plan, 'range_fill')
            artifact = job.register_output(key, output, plan, 'range_fill')
            self.assertIsNotNone(artifact)
            restored = JobRepository(repo.root).discover()[0]
            self.assertEqual(restored.read_artifact(artifact['id']), output)
            self.assertEqual(restored.snapshot()['templates'][key]['verification']['facts']['options']['distribution'],
                             {'ratio': .25, 'retained_counts': [0, 3], 'min_ratio': .1, 'replenish_type': 'B'})


if __name__ == '__main__':
    unittest.main()
