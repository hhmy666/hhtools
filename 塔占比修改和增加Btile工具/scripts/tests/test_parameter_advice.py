"""Recommendations must be executable and independently verified, not guesses."""
import unittest
from unittest.mock import patch

from scripts.tests.test_rewrite import synthetic, computed_plan
from scripts.tower_reducer import api
from scripts.tower_reducer.distribution import DistributionOptions
from scripts.tower_reducer.placement import PlacementOptions
from scripts.tower_reducer.rewrite import rewrite_template
from scripts.tower_reducer.verification import verify_output, VerificationReport, VerificationIssue
from scripts.tests.test_api import ApiContractCase


class VerifiedParameterAdviceTests(unittest.TestCase):
    def advice(self, raw, placement, ratio=.25, **kwargs):
        function = getattr(api, 'verified_parameter_advice', None)
        if function is None:
            return {'schemes': [], 'status': 'unavailable'}
        return function(raw, DistributionOptions(ratio), placement, 'lessTsample', **kwargs)

    def test_failed_layout_gets_two_distinct_executable_schemes(self):
        raw, _ = synthetic('A')
        original = PlacementOptions(max_tiles_per_layer=3, max_added_layers=0)
        self.assertFalse(computed_plan(raw, placement_options=original).placement.ok)
        advice = self.advice(raw, original)
        self.assertEqual(len(advice['schemes']), 2)
        self.assertNotEqual(advice['schemes'][0]['parameters'], advice['schemes'][1]['parameters'])
        for scheme in advice['schemes']:
            p = scheme['parameters']
            self.assertLessEqual(p['ratio'], .25)
            from scripts.tower_reducer.placement import plan_joint_layout
            from scripts.tower_reducer.inspection import inspect_template
            from scripts.tower_reducer.template_reader import decode_template
            from scripts.tower_reducer.rewrite import RewritePlan
            # Advice and actual reruns use joint automatic tower population
            # search, not the initial single-population test helper.
            doc=decode_template(raw)
            dopt=DistributionOptions(ratio=p['ratio'],retained_counts=p['retained_counts'],
                min_ratio=p['min_ratio'],replenish_type=p['replenish_type'])
            popt=PlacementOptions(p['strategy'],p['max_tiles_per_layer'],p['max_added_layers'],p['seed'])
            joint=plan_joint_layout(doc,inspect_template(doc),dopt,popt)
            plan=RewritePlan(joint.distribution,joint.placement,dopt,popt)
            self.assertTrue(plan.placement.ok)
            report = verify_output(raw, rewrite_template(raw, plan, 'lessTsample'), plan, 'lessTsample')
            self.assertTrue(report.ok, report.errors)
            self.assertTrue(scheme['changes'])
            self.assertEqual(scheme['facts']['tower_ratio'], report.facts['tower_ratio'])
            self.assertEqual(scheme['source_sha256'], report.facts['source_sha256'])

    def test_keeps_current_strategy_when_adding_one_layer_solves_it(self):
        raw, _ = synthetic('A')
        advice = self.advice(raw, PlacementOptions(max_tiles_per_layer=1, max_added_layers=0))
        self.assertTrue(advice['schemes'])
        first = advice['schemes'][0]['parameters']
        self.assertEqual(first['strategy'], 'top_first')
        self.assertEqual(first['max_added_layers'], 1)
        self.assertEqual(first['max_tiles_per_layer'], 1)
        self.assertEqual(first['ratio'], .25)

    def test_lower_initial_ceiling_and_actual_ratio_are_not_raised_by_any_scheme(self):
        raw, _ = synthetic('A')
        advice = self.advice(raw, PlacementOptions(max_tiles_per_layer=1, max_added_layers=0), ratio=.20)
        self.assertTrue(advice['schemes'])
        for scheme in advice['schemes']:
            self.assertLessEqual(scheme['parameters']['ratio'], .20)
            self.assertLessEqual(scheme['facts']['tower_ratio'], 1 / 9)

    def test_no_trial_is_presented_as_verified_when_verifier_rejects_it(self):
        raw, _ = synthetic('A')
        rejected = VerificationReport({}, (VerificationIssue('test_rejection', 'rejected'),), ())
        with patch('scripts.tower_reducer.verification.verify_output', return_value=rejected):
            advice = self.advice(raw, PlacementOptions(max_tiles_per_layer=1, max_added_layers=0))
        self.assertEqual(advice['schemes'], [])

    def test_budget_exhaustion_reports_no_guessed_schemes(self):
        raw, _ = synthetic('A')
        advice = self.advice(raw, PlacementOptions(max_tiles_per_layer=1, max_added_layers=0), max_states=0)
        self.assertEqual(advice['schemes'], [])
        self.assertEqual(advice['status'], 'limited')

    def test_distribution_failure_never_suggests_increasing_ratio(self):
        from scripts.tests.test_distribution import source
        from scripts.tower_reducer.distribution import plan_tower_distribution
        from scripts.tower_reducer.inspection import inspect_template
        doc = source(['BBBAAA'], extra_a=3, extra_b=6)
        # The former homogeneous-type failure is now permitted. This manual
        # suffix instead leaves A8/B7, so divisibility genuinely blocks it.
        result = plan_tower_distribution(doc, inspect_template(doc), DistributionOptions(.20, (1,)))
        self.assertFalse(result.ok)
        self.assertEqual(result.code, 'final_type_counts_not_multiple_of_three')
        advice = ' '.join(result.recommendations)
        self.assertNotIn('至少调至', advice)
        self.assertNotIn('提高塔占比', advice)


class ParameterAdviceHttpTests(ApiContractCase):
    def test_failure_advice_survives_history_and_selected_parameters_rerun_successfully(self):
        self.start_server()
        raw, _ = synthetic('A')
        imported = self.import_files([('sample_m.bytes', raw)])
        job_id = self.start_job(imported['import_id'], defaults={
            'ratio': .25, 'max_tiles_per_layer': 3, 'max_added_layers': 0})
        result = self.wait_job(job_id)
        row = self.rows(result)[0]
        self.assertEqual(row['status'], 'failed')
        advice = row['plan_summary'].get('parameter_advice', {'schemes': []})
        self.assertEqual(len(advice['schemes']), 2)
        self.assertEqual(result['artifacts'], [], 'Trial outputs must not become downloadable artifacts')
        recovered = self.request('GET', '/api/jobs/' + job_id)[2]
        self.assertEqual(self.rows(recovered)[0]['plan_summary']['parameter_advice'], advice)
        response = self.request('POST', '/api/jobs/' + job_id + '/rerun/' + row['key'],
                                json_body={'defaults': advice['schemes'][0]['parameters']})
        self.assertEqual(response[0], 202)
        final = self.wait_job(job_id)
        self.assertEqual(self.rows(final)[0]['status'], 'verified')
        self.assertEqual(len(final['artifacts']), 1)


if __name__ == '__main__':
    unittest.main()
