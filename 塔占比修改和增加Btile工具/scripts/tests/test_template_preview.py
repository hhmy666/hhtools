"""Preview is a read-only capability over source and independently verified bytes."""
from scripts.tests.project_fixtures import read_project_fixture
from scripts.tests.test_api import ApiContractCase
from scripts.tests.test_rewrite import synthetic
from scripts.tower_reducer.template_reader import decode_template


class TemplatePreviewTests(ApiContractCase):
    def test_repeated_finished_progress_does_not_create_a_false_revision(self):
        app = self.start_server()
        imported = self.import_files([('a_m.bytes', synthetic()[0])])
        result = self.wait_job(self.start_job(imported['import_id']))
        job = app.repository.get(result['job_id'])
        with job.lock:
            app._refresh_job_progress(job)
            before = job.snapshot()['revision']
            app._refresh_job_progress(job)
            self.assertEqual(job.snapshot()['revision'], before)

    def test_plan_summary_exposes_non_tower_layer_renumbering(self):
        from types import SimpleNamespace
        from scripts.tower_reducer.api import _plan_summary
        from scripts.tower_reducer.inspection import inspect_template
        raw, _ = synthetic()
        summary = _plan_summary(inspect_template(decode_template(raw)), placement=SimpleNamespace(
            placements=(), added_layers=2, original_z_offset=2, strategy='bottom_first', seed=0))
        self.assertEqual(summary['original_z_offset'], 2)

    def test_comparison_contains_real_coordinates_and_marks_exactly_moved_indices(self):
        self.start_server()
        raw,_=synthetic()
        imported=self.import_files([('a_m.bytes',raw)])
        job_id=self.start_job(imported['import_id'])
        result=self.wait_job(job_id)
        row=self.rows(result)[0]
        url='/api/jobs/{}/preview/{}'.format(job_id,row['key'])
        status,_,payload,_=self.request('GET',url)
        self.assertEqual(status,200)
        source=decode_template(raw)
        self.assertEqual(payload['source']['template_id'],source.template_id)
        self.assertEqual(payload['source']['tiles'][0]['x'],4)
        self.assertEqual(payload['source']['tiles'][0]['y'],4)
        moved={t['index'] for t in payload['output']['tiles'] if t['changed']}
        self.assertEqual(moved,{move['tile_index'] for move in row['verification']['facts']['moves']})
        self.assertEqual({t['index'] for t in payload['source']['tiles'] if t['changed']},moved)
        self.assertEqual(payload['revision'],result['revision'])
        unchanged=self.request('GET','/api/jobs/'+job_id)[2]
        self.assertEqual(unchanged['revision'],result['revision'],'preview never writes a new task state')

    def test_no_tower_preview_has_original_only_and_no_output(self):
        self.start_server()
        imported=self.import_files([('ntxkd021_m.bytes',read_project_fixture('ntxkd021_m.bytes'))])
        result=self.wait_job(self.start_job(imported['import_id']))
        row=self.rows(result)[0]
        status,_,payload,_=self.request('GET','/api/jobs/{}/preview/{}'.format(result['job_id'],row['key']))
        self.assertEqual(status,200)
        self.assertIsNone(payload['output'])
        self.assertTrue(payload['source']['tiles'])
        self.assertEqual(result['artifacts'],[])

    def test_unknown_template_key_cannot_read_another_source_or_arbitrary_file(self):
        self.start_server()
        imported=self.import_files([('a_m.bytes',synthetic()[0])])
        result=self.wait_job(self.start_job(imported['import_id']))
        status,_,_,_=self.request('GET','/api/jobs/{}/preview/tpl-{}'.format(result['job_id'],'f'*32))
        self.assertEqual(status,404)
