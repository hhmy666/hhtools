"""Real HTTP contracts for ratio ranges and per-template fill choices."""
import math

from scripts.tests.test_api import ApiContractCase
from scripts.tests.test_rewrite import message, synthetic
from scripts.tower_reducer.template_reader import decode_template


class RangeFillApiTests(ApiContractCase):
    def imported(self):
        return self.import_files([('sample_m.bytes',synthetic(second_tower=True)[0])])

    def test_ratio_above_25_percent_uses_current_template_source_bound(self):
        self.start_server()
        imported = self.imported()
        job_id = self.start_job(imported['import_id'], defaults={'ratio':.27, 'retained_counts':[3,0]})
        row = self.rows(self.wait_job(job_id))[0]
        self.assertEqual(row['status'], 'verified', row)
        self.assertEqual(row['parameters']['ratio'], .27)
        response = self.request('POST', '/api/jobs', json_body={
            'import_id': imported['import_id'], 'defaults': {'ratio':.41}, 'overrides': {}})
        self.assert_error(response, 400, 'reduce_ratio_above_source')
        self.assertIn('sample', response[2]['error']['message'])
        self.assertIn('40', response[2]['error']['message'])

    def test_each_template_uses_its_effective_ratio_without_clamping_batch_defaults(self):
        self.start_server()
        lower = synthetic()[0] + message(2, b'lower-source')
        imported = self.import_files([('sample_m.bytes', synthetic(second_tower=True)[0]),
                                     ('lower-source_m.bytes', lower)])
        response = self.request('POST', '/api/jobs', json_body={
            'import_id': imported['import_id'], 'defaults': {'ratio': .35}, 'overrides': {}})
        self.assert_error(response, 400, 'reduce_ratio_above_source')
        self.assertIn('lower-source', response[2]['error']['message'])
        self.assertEqual(len(self.repository.discover()), 0)
        lower_id = next(row['file_id'] for row in imported['files'] if row['inspection']['template_id'] == 'lower-source')
        job_id = self.start_job(imported['import_id'], defaults={'ratio': .35},
                                overrides={lower_id: {'ratio': .3}})
        rows = {row['source_template_id']: row for row in self.rows(self.wait_job(job_id))}
        self.assertEqual(rows['sample']['parameters']['ratio'], .35)
        self.assertEqual(rows['lower-source']['parameters']['ratio'], .3)

    def test_rerun_rejects_ratio_above_source_without_creating_attempt(self):
        app = self.start_server()
        job_id = self.start_job(self.imported()['import_id'], defaults={'retained_counts':[3,0]})
        original = self.rows(self.wait_job(job_id))[0]
        revision = app.repository.get(job_id).snapshot()['revision']
        response = self.request('POST', '/api/jobs/' + job_id + '/rerun/' + original['key'],
                                json_body={'defaults': {'ratio': .41}})
        self.assert_error(response, 400, 'reduce_ratio_above_source')
        self.assertEqual(app.repository.get(job_id).snapshot()['revision'], revision)

    def test_import_counts_only_non_tower_tiles_for_recommendation(self):
        self.start_server()
        imported = self.imported()
        inspection = imported['files'][0]['inspection']
        self.assertEqual(inspection.get('non_tower_a_count'),9)
        self.assertEqual(inspection.get('non_tower_b_count'),0)

    def test_legacy_job_exposes_source_non_tower_counts_without_changing_revision(self):
        app = self.start_server()
        job_id = self.start_job(self.imported()['import_id'],defaults={'retained_counts':[3,0]})
        row = self.rows(self.wait_job(job_id))[0]
        job = app.repository.get(job_id)
        summary = dict(row['plan_summary'])
        summary.pop('non_tower_a_count',None)
        summary.pop('non_tower_b_count',None)
        job.update_template(row['key'],plan_summary=summary)
        revision = job.snapshot()['revision']
        payload = app.job_payload(job_id)
        old = self.rows(payload)[0]
        self.assertEqual(old.get('non_tower_a_count'),9)
        self.assertEqual(old.get('non_tower_b_count'),0)
        self.assertEqual(job.snapshot()['revision'],revision)

    def test_explicit_a_overrides_auto_minor_type_and_lower_bound_is_inclusive(self):
        app = self.start_server()
        imported = self.imported()
        job_id = self.start_job(imported['import_id'],defaults={
            'ratio':.25,'min_ratio':.20,'replenish_type':'A','retained_counts':[3,0],
            'max_tiles_per_layer':20,'max_added_layers':2})
        finished = self.wait_job(job_id)
        row = self.rows(finished)[0]
        self.assertEqual(row['status'],'verified',row)
        self.assertEqual(row['parameters']['min_ratio'],.20)
        self.assertEqual(row['parameters']['replenish_type'],'A')
        self.assertEqual(row['plan_summary']['result_tower_ratio'],.20)
        self.assertEqual(row['plan_summary']['added_tile_counts'],{'A':3,'B':0})
        job = app.repository.get(job_id)
        artifact = next(iter(job.snapshot()['artifacts']))
        doc = decode_template(job.read_artifact(artifact))
        self.assertEqual(sum(t.tile_id==-10 for t in doc.tiles),12)
        self.assertEqual(sum(t.tile_id==-11 for t in doc.tiles),3)

    def test_cross_field_invalid_range_rejected_before_job_creation(self):
        self.start_server()
        imported = self.imported()
        for values in ({'min_ratio':.25,'ratio':.25}, {'min_ratio':.2,'ratio':.1},
                       {'min_ratio':-0.1}, {'min_ratio':True}, {'min_ratio':math.inf},
                       {'replenish_type':'C'}):
            with self.subTest(values=values):
                self.assert_error(self.request('POST','/api/jobs',json_body={
                    'import_id':imported['import_id'],'defaults':values,'overrides':{}}),400,'invalid_request')
        self.assertEqual(len(self.repository.discover()),0)

    def test_partial_override_range_is_checked_against_batch_defaults(self):
        self.start_server()
        imported = self.imported()
        file_id = imported['files'][0]['file_id']
        response = self.request('POST','/api/jobs',json_body={
            'import_id':imported['import_id'],'defaults':{'ratio':.15},
            'overrides':{file_id:{'min_ratio':.20}}})
        self.assert_error(response,400,'invalid_request')
        self.assertEqual(len(self.repository.discover()),0)

    def test_legacy_defaults_are_zero_minimum_and_auto_fill(self):
        self.start_server()
        job_id = self.start_job(self.imported()['import_id'],defaults={'retained_counts':[3,0]})
        row = self.rows(self.wait_job(job_id))[0]
        self.assertEqual(row['status'],'verified',row)
        self.assertEqual(row['parameters'].get('min_ratio'),0)
        self.assertEqual(row['parameters'].get('replenish_type'),'auto')
        self.assertEqual(row['plan_summary']['added_tile_counts'],{'A':0,'B':3})

    def test_rerun_validates_range_and_replaces_fill_option_for_only_this_attempt(self):
        self.start_server()
        job_id = self.start_job(self.imported()['import_id'],defaults={'retained_counts':[3,0]})
        original = self.rows(self.wait_job(job_id))[0]
        key = original['key']
        self.assert_error(self.request('POST','/api/jobs/'+job_id+'/rerun/'+key,
            json_body={'defaults':{'ratio':.2,'min_ratio':.2}}),400,'invalid_request')
        response = self.request('POST','/api/jobs/'+job_id+'/rerun/'+key,
            json_body={'defaults':{'ratio':.25,'min_ratio':.2,'retained_counts':[3,0],
            'replenish_type':'A'}})
        self.assertEqual(response[0],202,response[3])
        row = self.rows(self.wait_job(job_id))[0]
        self.assertEqual(row['status'],'verified',row)
        self.assertEqual(row['plan_summary']['added_tile_counts'],{'A':3,'B':0})
        self.assertEqual(row['attempts'][0]['parameters']['replenish_type'],'auto')
