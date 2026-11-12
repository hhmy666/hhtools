import io
import zipfile
from scripts.tests.project_fixtures import read_project_fixture
from scripts.tests.test_api import ApiContractCase
class SelectedDeliveryTests(ApiContractCase):
    def test_delivery_naming_changes_only_selected_file_identity_and_rejects_invalid_names(self):
        from scripts.tower_reducer.template_reader import decode_template
        job,completed,keys=self.batch()
        chosen=next(item for item in completed['artifacts'] if item['template_key']==keys[0])
        original=self.request('GET',f"/api/jobs/{job}/download/{chosen['id']}")[3]
        # Source identity is authoritative, not the generated output prefix.
        expected='export'+completed['templates'][keys[0]]['source_template_id']
        status,_,preview,_=self.request('POST',f'/api/jobs/{job}/export',json_body={'action':'preview','template_keys':[keys[0]],'naming_rule':'export{原模板ID}'})
        self.assertEqual(status,200,preview)
        self.assertEqual([item['filename'] for item in preview['items']],[expected+'_m.bytes'])
        result=self.request('POST',f'/api/jobs/{job}/download/bundle',json_body={'template_keys':[keys[0]],'naming_rule':'export{原模板ID}'})
        self.assertEqual(result[0],200,result[3])
        with zipfile.ZipFile(io.BytesIO(result[3])) as archive:
            self.assertEqual(archive.namelist(),[expected+'_m.bytes'])
            renamed=decode_template(archive.read(expected+'_m.bytes'))
            self.assertEqual(renamed.template_id,expected)
            self.assertEqual([t.fields for t in renamed.tiles],[t.fields for t in decode_template(original).tiles])
        self.assertEqual(self.request('GET',f"/api/jobs/{job}/download/{chosen['id']}")[3],original)
        for rule in ('','../bad','{原模板ID}'):
            self.assert_error(self.request('POST',f'/api/jobs/{job}/export',json_body={'action':'preview','naming_rule':rule}),400,'invalid_naming_rule')
        fixed=self.request('POST',f'/api/jobs/{job}/export',json_body={'action':'preview','naming_rule':'same'})
        self.assertEqual(fixed[0],200,fixed[3])
        self.assertEqual([item['filename'] for item in fixed[2]['items']],['same_m.bytes','samev2_m.bytes'])
        fixed_zip=self.request('POST',f'/api/jobs/{job}/download/bundle',json_body={'naming_rule':'same'})
        self.assertEqual(fixed_zip[0],200,fixed_zip[3])
        with zipfile.ZipFile(io.BytesIO(fixed_zip[3])) as archive:
            self.assertEqual(archive.namelist(),['same_m.bytes','samev2_m.bytes'])
            for name in archive.namelist():
                self.assertEqual(decode_template(archive.read(name)).template_id,name.removesuffix('_m.bytes'))

    def batch(self):
        self.start_server()
        imported=self.import_files([(name,read_project_fixture(name)) for name in ('mxd0057v1_m.bytes','hr0029_m.bytes')])
        job_id=self.start_job(imported['import_id'])
        completed=self.wait_job(job_id)
        return job_id,completed,list(completed['templates'])

    def test_selected_export_preview_and_commit_never_write_unselected_template(self):
        job,completed,keys=self.batch()
        status,_,preview,_=self.request('POST',f'/api/jobs/{job}/export',json_body={'action':'preview','template_keys':[keys[1]]})
        self.assertEqual(status,200,preview)
        self.assertEqual([item['template_key'] for item in preview['items']],[keys[1]])
        result=self.request('POST',f'/api/jobs/{job}/export',json_body={'action':'commit','plan_id':preview['plan_id'],'confirmation_token':preview['confirmation_token'],'conflict_decisions':{}})
        self.assertEqual(result[0],200,result[3])
        self.assertEqual(len(list(self.export_root.glob('*.bytes'))),1)
        self.assertEqual(result[2]['items'][0]['template_key'],keys[1])

    def test_selected_zip_contains_only_selected_verified_bytes_and_rejects_bad_scope(self):
        job,completed,keys=self.batch()
        result=self.request('POST',f'/api/jobs/{job}/download/bundle',json_body={'template_keys':[keys[0]]})
        self.assertEqual(result[0],200,result[3])
        with zipfile.ZipFile(io.BytesIO(result[3])) as archive:
            expected=next(item['filename'] for item in completed['artifacts'] if item['template_key']==keys[0])
            self.assertEqual(archive.namelist(),[expected])
        for scope in ([],['unknown'],[keys[0],keys[0]],'all'):
            self.assert_error(self.request('POST',f'/api/jobs/{job}/export',json_body={'action':'preview','template_keys':scope}),400,'invalid_selection')
