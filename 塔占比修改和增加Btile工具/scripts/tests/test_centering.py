"""Center current verified geometry by one legal whole-template XY revision."""
import unittest
from scripts.tests import test_translation,test_api
from scripts.tests.test_increase_engine import source
from scripts.tower_reducer.api import ApiError
from scripts.tower_reducer.template_reader import decode_template
from scripts.tower_reducer.storage import JobRepository


class CenteringTests(unittest.TestCase):
    setUp=test_translation.TranslationTests.setUp
    prepare=test_translation.TranslationTests.prepare
    assert_shift=test_translation.TranslationTests.assert_shift

    def test_center_only_shifts_whole_output_and_second_call_does_not_churn_artifact(self):
        job,key,artifact,before=self.prepare();source_bytes=job.read_source(key)
        self.assertTrue(callable(getattr(self.app,'center_template',None)),'centering API missing')
        centered=self.app.center_template(job.job_id,key,dict(artifact_id=artifact['id']))
        self.assertTrue(centered['centered']);self.assertFalse(centered['skipped'])
        raw=job.read_artifact(centered['artifact_id']);delta=centered['centering']
        self.assert_shift(before,raw,delta['dx'],delta['dy'])
        self.assertEqual((delta['dx'],delta['dy']),(0,4))
        self.assertEqual(delta['residual'],{'x':.5,'y':-.5})
        self.assertEqual(job.read_source(key),source_bytes)
        snapshot=job.snapshot()
        again=self.app.center_template(job.job_id,key,dict(artifact_id=centered['artifact_id']))
        self.assertTrue(again['skipped']);self.assertFalse(again['centered'])
        self.assertEqual(again['artifact_id'],centered['artifact_id']);self.assertEqual(job.snapshot(),snapshot)
        self.assertEqual(JobRepository(self.root/'jobs').get(job.job_id).read_artifact(centered['artifact_id']),raw)
        with self.assertRaises(ApiError):self.app.center_template(job.job_id,key,dict(artifact_id=artifact['id']))

    def test_visual_tower_expansion_and_legal_clamp_are_used(self):
        from scripts.tower_reducer.translation import center_translation
        # Rightward tower makes visual center differ from the anchor center.
        doc=decode_template(source(total=9,towers=((8,8,9,3),)))
        facts=center_translation(doc)
        self.assertEqual((facts['dx'],facts['dy']),(-1,1))
        # Existing full-width source leaves no legal X movement.
        wide=decode_template(source(total=3,points=[(0,2),(7,2),(14,2)]))
        clamped=center_translation(wide)
        self.assertEqual(clamped['dx'],0)
        self.assertTrue(clamped['clamped'])

    def test_center_uses_current_translation_and_rejects_busy_requests_without_writes(self):
        job,key,artifact,_=self.prepare()
        moved=self.app.translate_template(job.job_id,key,dict(artifact_id=artifact['id'],dx=0,dy=2))
        centered=self.app.center_template(job.job_id,key,dict(artifact_id=moved['artifact_id']))
        self.assertEqual(centered['centering']['dy'],2)
        self.assertEqual(centered['translation']['dy'],4)
        self.app._workers[job.job_id]=object();before=job.snapshot()
        with self.assertRaises(ApiError):self.app.center_template(job.job_id,key,dict(artifact_id=centered['artifact_id']))
        self.assertEqual(job.snapshot(),before)


class CenteringHttpTests(test_api.ApiContractCase):
    def test_http_center_current_artifact_and_csrf_boundary(self):
        app=self.start_server()
        imported=self.import_files([('input_m.bytes',test_api.synthetic()[0])])
        job_id=self.start_job(imported['import_id'],defaults={'ratio':0,'min_ratio':0})
        row=self.rows(self.wait_job(job_id))[0];artifact=row['artifacts'][0]
        endpoint='/api/jobs/'+job_id+'/center/'+row['key']
        self.assert_error(self.request('POST',endpoint,json_body={'artifact_id':artifact['id']},csrf=False),403,'csrf_failed')
        response=self.request('POST',endpoint,json_body={'artifact_id':artifact['id']})
        self.assertEqual(response[0],200,response[3])
        self.assertIn('centering',response[2])
