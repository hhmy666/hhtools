"""User-visible type changes and recovery must use concrete, safe facts."""
from types import SimpleNamespace
import unittest

from scripts.tower_reducer import api
from scripts.tower_reducer.inspection import inspect_template
from scripts.tower_reducer.template_reader import decode_template
from scripts.tests.test_rewrite import scalar, message
from scripts.tower_reducer.distribution import DistributionOptions
from scripts.tower_reducer.placement import PlacementOptions, plan_joint_layout
from scripts.tower_reducer.rewrite import RewritePlan, rewrite_template
from scripts.tower_reducer.verification import verify_output
from scripts.tests.test_api import ApiContractCase


def mixed_conversion_source():
    # Source A6/B3; the only tower contains A3, so all three become B.
    fields = [message(2, b'conversion'), scalar(4, 17), scalar(5, 19)]
    points = ((4, 4, 0), (8, 4, 0), (12, 4, 0),
              (5, 5, 1), (9, 5, 1), (13, 5, 1))
    for index, (x, y, z) in enumerate(points):
        fields.append(message(6, scalar(1, x) + scalar(2, y) + scalar(3, z)
                              + scalar(4, -11 if index < 3 else -10)))
    for z in range(3):
        fields.append(message(6, scalar(1, 2) + scalar(2, 16) + scalar(3, z) + scalar(4, -10)))
    fields.append(message(7, scalar(1, 2) + scalar(2, 16) + scalar(3, 2)))
    return b''.join(fields)


class FailureContextTests(unittest.TestCase):
    def test_summary_distinguishes_removed_and_replenished_types(self):
        inspection = inspect_template(decode_template(mixed_conversion_source()))
        distribution = SimpleNamespace(retained_counts=(0,), move_type='B', move_count=3,
                                       final_ratio=0, removed_counts=(3, 0), final_counts=(3, 6))
        result = api._plan_summary(inspection, distribution)
        self.assertEqual(result.get('source_tile_counts'), {'A': 6, 'B': 3})
        self.assertEqual(result.get('removed_tile_counts'), {'A': 3, 'B': 0})
        self.assertEqual(result.get('added_tile_counts'), {'A': 0, 'B': 3})
        self.assertEqual(result.get('result_tile_counts'), {'A': 3, 'B': 6})
        self.assertEqual(result.get('tile_count_changes'), {'A': -3, 'B': 3})

    def test_legacy_summary_does_not_invent_converted_counts(self):
        inspection = inspect_template(decode_template(mixed_conversion_source()))
        distribution = SimpleNamespace(retained_counts=(0,), move_type='A', move_count=3, final_ratio=0)
        result = api._plan_summary(inspection, distribution)
        self.assertIsNone(result.get('tile_count_changes'))

    def test_old_type_restriction_explains_retry_without_parameter_guess(self):
        result = api._public_failure({'code': 'single_move_type_unavailable'})
        self.assertIn('旧结果', result['message'])
        self.assertIn('单独调整并重新运行', ' '.join(result['recommendations']))
        self.assertIn('原参数', ' '.join(result['recommendations']))

    def test_invalid_manual_counts_explain_actual_final_counts_and_safe_recovery(self):
        summary = {'failure_context': {
            'code': 'final_type_counts_not_multiple_of_three',
            'source_counts': [6, 3], 'removed_counts': [1, 0], 'replenish_type': 'B',
            'replenish_count': 1, 'final_counts': [5, 4], 'remainders': [2, 1], 'retained_counts': [2]}}
        failure = {'code': 'final_type_counts_not_multiple_of_three',
                   'message': '/private/internal/path'}
        try:
            result = api._public_failure(failure, summary)
        except TypeError:
            result = api._public_failure(failure)
        self.assertIn('A 5', result['message'])
        self.assertIn('B 4', result['message'])
        self.assertIn('3的倍数', result['message'])
        self.assertNotIn('/private', repr(result))
        self.assertIn('已验收', ' '.join(result['recommendations']))

    def test_malformed_advice_does_not_hide_a_valid_count_error(self):
        for malformed in (['unexpected'], 1, 'unexpected'):
            with self.subTest(malformed=malformed):
                summary = {'failure_context': {'final_counts': [5, 4]}, 'parameter_advice': malformed}
                try:
                    result = api._public_failure({'code': 'final_type_counts_not_multiple_of_three'}, summary)
                except (AttributeError, TypeError):
                    result = {'message': 'unavailable'}
                self.assertIn('A 5', result['message'])


class ConversionAdviceTests(unittest.TestCase):
    def test_invalid_manual_conversion_gets_verified_exact_retained_numbers(self):
        raw = mixed_conversion_source()
        dopt = DistributionOptions(.25, (2,))
        popt = PlacementOptions()
        advice = api.verified_parameter_advice(raw, dopt, popt, 'lessTconversion')
        self.assertTrue(advice['schemes'], 'Users need a tested correction for the invalid manual count')
        first = advice['schemes'][0]
        self.assertEqual(first['parameters']['retained_counts'], [0])
        self.assertEqual(first['parameters']['ratio'], .25)
        self.assertEqual(first['parameters']['strategy'], 'top_first')
        self.assertEqual(first['parameters']['max_tiles_per_layer'], 20)
        self.assertEqual(first['parameters']['max_added_layers'], 2)
        doc = decode_template(raw)
        inspection = inspect_template(doc)
        for scheme in advice['schemes']:
            parameters = scheme['parameters']
            self.assertLessEqual(parameters['ratio'], .25)
            corrected = DistributionOptions(parameters['ratio'], tuple(parameters['retained_counts']))
            placement = PlacementOptions(parameters['strategy'], parameters['max_tiles_per_layer'],
                                         parameters['max_added_layers'], parameters['seed'])
            joint = plan_joint_layout(doc, inspection, corrected, placement)
            self.assertTrue(joint.placement.ok)
            plan = RewritePlan(joint.distribution, joint.placement, corrected, placement)
            result = verify_output(raw, rewrite_template(raw, plan, 'lessTconversion'), plan, 'lessTconversion')
            self.assertTrue(result.ok, result.errors)
            self.assertEqual(result.facts['tile_counts'], {'total': 9, 'A': 3, 'B': 6})


class ConversionHttpTests(ApiContractCase):
    def test_conversion_counts_survive_registration_download_and_restart(self):
        self.start_server()
        raw = mixed_conversion_source()
        imported = self.import_files([('conversion_m.bytes', raw)])
        job_id = self.start_job(imported['import_id'])
        result = self.wait_job(job_id)
        row = self.rows(result)[0]
        self.assertEqual(row['status'], 'verified')
        self.assertEqual(row['plan_summary']['tile_count_changes'], {'A': -3, 'B': 3})
        self.assertEqual(row['plan_summary']['removed_tile_counts'], {'A': 3, 'B': 0})
        self.assertEqual(row['plan_summary']['added_tile_counts'], {'A': 0, 'B': 3})
        artifact = result['artifacts'][0]
        job = self.repository.get(job_id)
        output = job.read_artifact(artifact['id'])
        restored = type(self.repository)(self.repository.root).discover()[0]
        self.assertEqual(restored.read_artifact(artifact['id']), output)
        self.assertEqual(restored.read_source(row['key']), raw)
        doc = decode_template(output)
        self.assertEqual(len(doc.tiles), 9)
        self.assertEqual(sum(tile.tile_id == -10 for tile in doc.tiles), 3)
        self.assertEqual(sum(tile.tile_id == -11 for tile in doc.tiles), 6)

    def test_invalid_manual_count_returns_concrete_error_and_working_correction(self):
        self.start_server()
        imported = self.import_files([('conversion_m.bytes', mixed_conversion_source())])
        job_id = self.start_job(imported['import_id'], defaults={'retained_counts': [2]})
        result = self.wait_job(job_id)
        row = self.rows(result)[0]
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['error']['code'], 'final_type_counts_not_multiple_of_three')
        self.assertIn('A 5', row['error']['message'])
        self.assertIn('B 4', row['error']['message'])
        scheme = row['plan_summary']['parameter_advice']['schemes'][0]
        self.assertEqual(scheme['parameters']['retained_counts'], [0])
        response = self.request('POST', '/api/jobs/' + job_id + '/rerun/' + row['key'],
                                json_body={'defaults': scheme['parameters']})
        self.assertEqual(response[0], 202)
        self.assertEqual(self.rows(self.wait_job(job_id))[0]['status'], 'verified')


if __name__ == '__main__':
    unittest.main()
