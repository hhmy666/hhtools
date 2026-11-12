"""Failed increases offer only source-bound, independently audited replays."""
from copy import deepcopy
from hashlib import sha256
import unittest
from unittest.mock import patch

from scripts.tests.test_api import ApiContractCase
from scripts.tests.test_increase_engine import source
from scripts.tower_reducer import api
from scripts.tower_reducer.increase import (
    DEFAULT_INCREASE_OPTIONS, build_increase, normalise_increase_options,
)
from scripts.tower_reducer.increase_verification import verify_increase


def extension_source():
    return source(towers=((3, 10, 6, 0), (13, 10, 6, 0)),
                  points=[(6, 1), (8, 1), (10, 1)])


def blocked_new_options():
    return normalise_increase_options({
        'target_mode': 'count', 'added_tiles': 6, 'operation': 'both',
        'regions': ['top'], 'auto_translate': False,
    })


class VerifiedIncreaseRecoveryTests(unittest.TestCase):
    def advice(self, raw, options, **kwargs):
        function = getattr(api, 'verified_increase_parameter_advice', None)
        self.assertTrue(callable(function), 'Increase failures need verified parameter advice')
        return function(raw, options, 'moreToriginal', **kwargs)

    def test_existing_recovery_keeps_requested_quantity_and_exactly_replays(self):
        # A missing operation diff would let a both request silently become existing.
        raw, options = extension_source(), blocked_new_options()
        self.assertFalse(build_increase(raw, options, 'moreToriginal')['ok'])
        advice = self.advice(raw, options)
        self.assertEqual(advice['operation'], 'increase')
        self.assertEqual(advice['status'], 'ready')
        self.assertTrue(advice['schemes'])
        first = advice['schemes'][0]
        self.assertEqual(first['options']['operation'], 'existing')
        self.assertEqual(first['options']['added_tiles'], 6)
        self.assertIn({'field': 'operation', 'from': 'both', 'to': 'existing'}, first['changes'])
        self.assertEqual(first['facts']['tower_counts'], [9, 9])
        self.assertEqual(first['facts']['new_tower_count'], 0)
        self.assertEqual(first['facts']['extended_tower_count'], 2)
        self.assertEqual(first['facts']['added_tiles'], 6)
        self.assertEqual(first['facts']['final_total'], 66)
        for scheme in advice['schemes']:
            self.assertTrue(scheme['verified'])
            self.assertEqual(set(scheme['options']), set(DEFAULT_INCREASE_OPTIONS))
            self.assertEqual(scheme['parameters'], {'mode': 'increase', 'increase': scheme['options']})
            replay = build_increase(raw, scheme['options'], 'moreToriginal')
            self.assertTrue(replay['ok'], replay['error'])
            self.assertTrue(verify_increase(raw, replay['output'], replay['plan'], 'moreToriginal').ok)
            self.assertEqual(scheme['source_sha256'], sha256(raw).hexdigest())
            self.assertEqual(scheme['output_sha256'], sha256(replay['output']).hexdigest())
            for change in scheme['changes']:
                self.assertEqual(change['from'], options[change['field']])
                self.assertEqual(change['to'], scheme['options'][change['field']])

    def test_exhausted_trial_budget_never_claims_template_is_impossible(self):
        # Treating a work limit as a proof of infeasibility is a wrong branch.
        advice = self.advice(extension_source(), blocked_new_options(), max_trials=0)
        self.assertEqual(advice['schemes'], [])
        self.assertEqual(advice['status'], 'limited')
        self.assertEqual(advice['checked_trials'], 0)

    def test_smaller_actual_quantity_is_a_visible_requested_quantity_change(self):
        # The planner may find only 18 while asked for 30; adopting 30 hides the compromise.
        raw = source(towers=((3, 3, 3, 0), (13, 3, 3, 0)),
                     points=[(6, 1), (8, 1), (10, 1)])
        options = {**blocked_new_options(), 'added_tiles': 30}
        advice = self.advice(raw, options)
        self.assertTrue(advice['schemes'])
        first = advice['schemes'][0]
        self.assertEqual(first['facts']['added_tiles'], 18)
        self.assertEqual(first['options']['added_tiles'], 18)
        self.assertIn({'field': 'added_tiles', 'from': 30, 'to': 18}, first['changes'])
        replay = build_increase(raw, first['options'], 'moreToriginal')
        self.assertTrue(replay['ok'], replay['error'])
        self.assertEqual(replay['summary']['added_tiles'], 18)

    def test_unattainable_multiplier_becomes_an_explicit_count_request(self):
        # Keeping a nominal 1.5 multiplier on a 60→78 result hides the target change.
        raw = source(towers=((3, 3, 3, 0), (13, 3, 3, 0)),
                     points=[(6, 1), (8, 1), (10, 1)])
        options = {**blocked_new_options(), 'target_mode': 'multiplier', 'total_multiplier': 1.5}
        advice = self.advice(raw, options)
        self.assertTrue(advice['schemes'])
        first = advice['schemes'][0]
        self.assertEqual(first['facts']['final_total'], 78)
        self.assertEqual(first['options']['target_mode'], 'count')
        self.assertEqual(first['options']['added_tiles'], 18)
        self.assertIn({'field': 'target_mode', 'from': 'multiplier', 'to': 'count'}, first['changes'])
        self.assertIn({'field': 'added_tiles', 'from': 6, 'to': 18}, first['changes'])

    def test_forged_builder_success_cannot_become_verified_advice(self):
        # Even a successful replay must pass an independent read-back audit.
        raw, options = extension_source(), blocked_new_options()
        built = build_increase(raw, {**options, 'operation': 'existing'}, 'moreToriginal')
        self.assertTrue(built['ok'], built['error'])
        forged = {**built, 'output': built['output'].replace(b'unknown', b'changed', 1)}
        with patch('scripts.tower_reducer.api._build_increase', return_value=forged, create=True):
            advice = self.advice(raw, options, max_trials=1)
        self.assertEqual(advice['schemes'], [])

    def test_replay_plan_must_bind_the_exact_offered_options(self):
        # A valid output for a different option set is not an audited offered replay.
        raw, options = extension_source(), blocked_new_options()
        built = build_increase(raw, {**options, 'operation': 'existing'}, 'moreToriginal')
        self.assertTrue(built['ok'], built['error'])
        with patch('scripts.tower_reducer.api._build_increase', return_value=built):
            advice = self.advice(raw, options, max_trials=1)
        self.assertEqual(advice['schemes'], [])

    def test_displayed_actual_counts_come_from_independent_read_back(self):
        # A stale planner summary must not misstate a verified byte stream's result.
        real_build = api._build_increase

        def stale_summary(raw, options, new_id, deadline):
            result = real_build(raw, options, new_id, deadline)
            if result['ok'] and result['output'] is not None:
                return {**result, 'summary': {**result['summary'], 'final_total': 999,
                    'final_tower_tiles': 999, 'final_ratio': .99, 'final_counts': [999, 0]}}
            return result

        with patch('scripts.tower_reducer.api._build_increase', side_effect=stale_summary):
            advice = self.advice(extension_source(), blocked_new_options())
        self.assertTrue(advice['schemes'])
        facts = advice['schemes'][0]['facts']
        self.assertEqual(facts['final_total'], 66)
        self.assertEqual(facts['final_tower_tiles'], 18)
        self.assertEqual(facts['tower_ratio'], 18 / 66)
        self.assertEqual(facts['final_counts'], [66, 0])


class IncreaseRecoveryHttpTests(ApiContractCase):
    def test_invalid_existing_tower_translation_is_not_reported_as_no_new_positions(self):
        # Moving old towers to Y=17 is the cause even when new anchors remain available.
        self.start_server()
        raw = source(towers=((3, 14, 3, 0), (13, 14, 3, 0)))
        imported = self.import_files([('original_m.bytes', raw)])
        job_id = self.start_job(imported['import_id'], defaults={
            'mode': 'increase', 'increase': {
                'target_mode': 'count', 'added_tiles': 6, 'operation': 'both',
                'translate_y': 3, 'auto_translate': False,
            },
        })
        row = self.rows(self.wait_job(job_id))[0]
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['error']['code'], 'increase_existing_tower_layout_unavailable')
        self.assertIn('原塔', row['error']['message'])
        self.assertIn('X=2–14', row['error']['message'])
        self.assertIn('Y=2–16', row['error']['message'])
        self.assertIn('平移', ' '.join(row['error']['recommendations']))
        self.assertNotIn('新增塔对称位置', row['error']['message'])

    def test_failed_increase_persists_advice_without_registering_trial_artifacts(self):
        # Omitting advice at the job boundary used to leave increase rows unactionable.
        self.start_server()
        imported = self.import_files([('original_m.bytes', extension_source())])
        job_id = self.start_job(imported['import_id'], defaults={
            'mode': 'increase', 'increase': blocked_new_options(),
        })
        result = self.wait_job(job_id)
        row = self.rows(result)[0]
        self.assertEqual(row['status'], 'failed')
        advice = row['plan_summary'].get('parameter_advice', {'schemes': []})
        self.assertTrue(advice['schemes'], row)
        self.assertEqual(result['artifacts'], [])
        recovered = self.request('GET', '/api/jobs/' + job_id)[2]
        self.assertEqual(self.rows(recovered)[0]['plan_summary']['parameter_advice'], advice)
        response = self.request('POST', '/api/jobs/' + job_id + '/rerun/' + row['key'],
                                json_body={'defaults': advice['schemes'][0]['parameters']})
        self.assertEqual(response[0], 202)
        self.assertEqual(self.rows(self.wait_job(job_id))[0]['status'], 'verified')

    def test_historical_raw_search_budget_guidance_is_only_normalized_in_public_payload(self):
        # Safe error mapping alone did not hide the legacy plan recommendation.
        application = self.start_server()
        from scripts.tower_reducer.storage import InputSnapshot
        job = self.repository.create([InputSnapshot(extension_source(), 'original_m.bytes')],
                                     parameters={'mode': 'increase'})
        key = next(iter(job.snapshot()['templates']))
        summary = {'operation': 'increase', 'recommendations': ['搜索预算已耗尽，请放宽边界'],
                   'non_tower_a_count': 48, 'non_tower_b_count': 0}
        job.update_template(key, status='failed', stage='placement', plan_summary=summary,
                            error={'code': 'increase_search_budget', 'message': '搜索预算已耗尽',
                                   'recommendations': ['放宽边界']})
        before = deepcopy(job.snapshot())
        payload = application.job_payload(job.job_id)
        row = self.rows(payload)[0]
        shown = ' '.join([row['error']['message'], *row['error']['recommendations'],
                          row.get('recommendation', ''), *row['plan_summary'].get('recommendations', [])])
        self.assertNotIn('搜索预算', shown)
        self.assertNotIn('放宽边界', shown)
        self.assertIn('尚未判断', ' '.join(row['error']['recommendations']))
        self.assertEqual(job.snapshot(), before, 'Reading old jobs must not rewrite their history')


if __name__ == '__main__':
    unittest.main()
