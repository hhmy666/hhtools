"""Dual-mode HTTP and persistence contracts; all roots are temporary."""
import json
from pathlib import Path
import unittest

from scripts.tests.test_api import ApiContractCase
from scripts.tests.test_rewrite import message, scalar
from scripts.tower_reducer.settings import LocalSettings, load_settings, save_settings
from scripts.tower_reducer.template_reader import decode_template


def untowered():
    # Six ordinary A tiles, left/right symmetric, substantial room below.
    return b''.join([message(2, b'plain'), scalar(4, 17), scalar(5, 19)] + [
        message(6, scalar(1,x)+scalar(2,y)+scalar(3,0)+scalar(4,-10))
        for x,y in ((5,5),(8,5),(11,5),(6,7),(10,7),(8,9))])


class ModifierSettingsTests(unittest.TestCase):
    def test_legacy_settings_get_separate_increase_naming_without_losing_reduce(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'settings.json'
            path.write_text(json.dumps({'naming_rule':'low{原模板ID}'}), encoding='utf-8')
            settings = load_settings(path)
            self.assertFalse(settings.load_error)
            self.assertEqual(settings.naming_rule, 'low{原模板ID}')
            self.assertEqual(getattr(settings, 'increase_naming_rule', None), 'moreT{原模板ID}')
            settings.increase_naming_rule = 'high{原模板ID}'
            save_settings(settings, path)
            self.assertEqual(load_settings(path).increase_naming_rule, 'high{原模板ID}')


class ModifierApiTests(ApiContractCase):
    def imported_plain(self):
        return self.import_files([('plain_m.bytes', untowered())])

    @staticmethod
    def options():
        return {'target_mode':'count', 'added_tiles':6, 'regions':['bottom'],
                'stack_count':2, 'auto_translate':False}

    def test_saved_mode_naming_is_independent_and_validated(self):
        self.start_server()
        response = self.request('PUT','/api/settings',json_body={
            'increase_naming_rule':'high{原模板ID}'})
        self.assertEqual(response[0],200,response[3])
        settings = response[2]['settings']
        self.assertEqual(settings['increase_naming_rule'],'high{原模板ID}')
        self.assertEqual(settings['naming_rule'],'lessT{原模板ID}')
        self.assert_error(self.request('PUT','/api/settings',json_body={
            'increase_naming_rule':'../unsafe'}),400,'invalid_naming_rule')

    def test_increase_no_tower_generates_downloadable_restartable_artifact(self):
        application = self.start_server()
        imported = self.imported_plain()
        before = untowered()
        job_id = self.start_job(imported['import_id'],defaults={
            'mode':'increase','increase':self.options()})
        row = self.rows(self.wait_job(job_id))[0]
        self.assertEqual(row['status'],'verified',row)
        self.assertEqual(row['naming']['new_id'],'moreTplain')
        self.assertEqual(row['plan_summary']['operation'],'increase')
        artifact = row['artifacts'][0]
        response = self.request('GET','/api/jobs/'+job_id+'/download/'+artifact['id'])
        self.assertEqual(response[0],200,response[3])
        output = decode_template(response[3])
        self.assertEqual(len(output.tiles),12)
        self.assertEqual(len(output.stacks),2)
        job = application.repository.get(job_id)
        self.assertEqual(job.read_source(row['key']), before)
        from scripts.tower_reducer.storage import JobRepository
        reopened = JobRepository(self.repository.root).get(job_id)
        self.assertEqual(reopened.read_artifact(artifact['id']), response[3])
        changed, plan = reopened.renamed_output_bytes(artifact['id'],'moreTplainv2')
        self.assertEqual(decode_template(changed).template_id,'moreTplainv2')

    def test_preview_is_read_only_and_rejects_arbitrary_source_or_unknown_fields(self):
        app = self.start_server()
        imported = self.imported_plain()
        body = {'import_id':imported['import_id'],'file_id':imported['files'][0]['file_id'],
                'increase':self.options()}
        response = self.request('POST','/api/increase/preview',json_body=body)
        self.assertEqual(response[0],200,response[3])
        self.assertTrue(response[2]['schemes'],response[2])
        scheme = response[2]['schemes'][0]
        self.assertNotIn('output',scheme)
        self.assertNotIn('plan',scheme)
        self.assertEqual(len(scheme['preview']['source']['tiles']),6)
        self.assertEqual(len(scheme['preview']['output']['tiles']),12)
        self.assertEqual(len(self.repository.discover()),0)
        for invalid in ({**body,'source_path':'/tmp/private'},
                        {**body,'file_id':'file-'+'0'*32},
                        {**body,'increase':{'made_up':3}}):
            result=self.request('POST','/api/increase/preview',json_body=invalid)
            self.assertGreaterEqual(result[0],400,result[3])
        self.assert_error(self.request('POST','/api/increase/preview',json_body=body,
                                      csrf=False),403,'csrf_failed')

    def test_invalid_increase_options_do_not_create_jobs(self):
        self.start_server()
        imported = self.imported_plain()
        for values in ({'mode':'unsupported'}, {'mode':'increase','increase':{'added_tiles':True}},
                       {'mode':'increase','increase':{'max_ratio':float('inf')}},
                       {'mode':'increase','increase':{'tower_positions':[{'x':3,'y':4}]}}):
            response=self.request('POST','/api/jobs',json_body={
                'import_id':imported['import_id'],'defaults':values,'overrides':{}})
            self.assert_error(response,400,'invalid_request')
        self.assertFalse(self.repository.discover())

    def test_legacy_no_tower_still_skips_without_creating_a_name(self):
        self.start_server()
        row=self.rows(self.wait_job(self.start_job(self.imported_plain()['import_id'])))[0]
        self.assertEqual(row['status'],'no_op')
        self.assertFalse(row['naming'])
        self.assertFalse(row['artifacts'])

    def test_increase_overrides_merge_inside_batch_range(self):
        self.start_server()
        imported=self.imported_plain()
        file_id=imported['files'][0]['file_id']
        job_id=self.start_job(imported['import_id'],defaults={
            'mode':'increase','increase':{**self.options(), 'min_ratio':.3,'max_ratio':.7}},
            overrides={file_id:{'increase':{'min_ratio':.4}}})
        row=self.rows(self.wait_job(job_id))[0]
        self.assertEqual(row['status'],'verified',row)
        self.assertEqual(row['parameters']['increase']['min_ratio'],.4)
        self.assertEqual(row['parameters']['increase']['max_ratio'],.7)
        self.assertEqual(row['parameters']['increase']['stack_count'],2)

    def test_increase_zero_addition_has_no_output_and_rerun_can_change_mode(self):
        self.start_server()
        imported=self.imported_plain()
        job_id=self.start_job(imported['import_id'])
        original=self.rows(self.wait_job(job_id))[0]
        response=self.request('POST','/api/jobs/'+job_id+'/rerun/'+original['key'],
            json_body={'defaults':{'mode':'increase','increase':self.options()}})
        self.assertEqual(response[0],202,response[3])
        row=self.rows(self.wait_job(job_id))[0]
        self.assertEqual(row['status'],'verified',row)
        self.assertEqual(row['attempts'][0]['status'],'no_op')
        no_op=self.start_job(imported['import_id'],defaults={'mode':'increase',
            'increase':{**self.options(),'added_tiles':0}})
        row=self.rows(self.wait_job(no_op))[0]
        self.assertEqual(row['status'],'no_op',row)
        self.assertFalse(row['naming'])
        self.assertFalse(row['artifacts'])

    def test_increase_export_version_revalidates_typed_plan_and_original_kept(self):
        application=self.start_server()
        job_id=self.start_job(self.imported_plain()['import_id'],defaults={
            'mode':'increase','increase':self.options()})
        row=self.rows(self.wait_job(job_id))[0]
        existing=self.export_root/'moreTplain_m.bytes'
        existing.write_bytes(b'user original')
        endpoint='/api/jobs/'+job_id+'/export'
        preview=self.request('POST',endpoint,json_body={'action':'preview'})
        self.assertEqual(preview[0],200,preview[3])
        payload=preview[2]
        commit=self.request('POST',endpoint,json_body={'action':'commit',
            'plan_id':payload['plan_id'],'confirmation_token':payload['confirmation_token'],
            'conflict_decisions':{}})
        self.assertEqual(commit[0],200,commit[3])
        self.assertEqual(existing.read_bytes(),b'user original')
        written=self.export_root/'moreTplainv2_m.bytes'
        self.assertEqual(decode_template(written.read_bytes()).template_id,'moreTplainv2')
        self.assertEqual(application.repository.get(job_id).read_source(row['key']),untowered())


if __name__ == '__main__':
    unittest.main()
