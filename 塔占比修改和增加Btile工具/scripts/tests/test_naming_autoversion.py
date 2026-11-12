"""Duplicate source IDs and delivery rules receive safe unique output names."""
import io
import unittest
import zipfile

from scripts.tests.test_api import ApiContractCase
from scripts.tests import test_export_service as fixtures
from scripts.tests.test_rewrite import synthetic
from scripts.tower_reducer.export_service import build_zip, export_outputs, plan_exports
from scripts.tower_reducer.rewrite import rewrite_template
from scripts.tower_reducer.storage import InputSnapshot
from scripts.tower_reducer.template_reader import decode_template


class GenerationNamesTests(ApiContractCase):
    def test_same_internal_ids_generate_and_rerun_with_unique_names(self):
        self.start_server()
        raw, _ = synthetic()
        imported = self.import_files([('a_m.bytes', raw), ('b_m.bytes', raw+b'\x78\x01'),
                                      ('c_m.bytes', raw+b'\x78\x02')])
        overrides = {imported['files'][2]['file_id']: {'new_id':'sharedv2'}}
        job_id = self.start_job(imported['import_id'], defaults={'ratio':0, 'naming_rule':'shared'},
                                overrides=overrides)
        completed = self.wait_job(job_id)
        self.assertEqual(completed['status'], 'succeeded', self.rows(completed))
        self.assertEqual([r['new_id'] for r in completed['artifacts']], ['shared','sharedv3','sharedv2'])
        row = self.rows(completed)[0]
        result = self.request('POST', f'/api/jobs/{job_id}/rerun/{row["key"]}',
                              json_body={'defaults': {'ratio':0, 'new_id':'SHAREDV2'}})
        self.assertEqual(result[0], 202)
        rerun = self.wait_job(job_id)
        self.assertEqual(rerun['status'], 'succeeded', self.rows(rerun))
        self.assertEqual(next(r for r in rerun['artifacts'] if r['template_key']==row['key'])['new_id'], 'SHAREDV2v2')


class DeliveryNamesTests(unittest.TestCase):
    setUp = fixtures.ExportTests.setUp

    def duplicate_id_job(self):
        job = self.repo.create([InputSnapshot(self.raw, 'a_m.bytes'),
                                InputSnapshot(self.raw+b'\x78\x01', 'b_m.bytes')])
        for key, new_id in zip(job.snapshot()['templates'], ['first', 'second']):
            raw = job.read_source(key)
            self.assertIsNotNone(job.register_output(key, rewrite_template(raw, self.plan, new_id), self.plan, new_id))
        return job

    def test_edited_export_rule_versions_batch_and_existing_files_without_overwrite(self):
        job = self.duplicate_id_job()
        self.target.mkdir()
        (self.target/'copy_m.bytes').write_bytes(b'keep')
        (self.target/'COPYv2_m.bytes').write_bytes(b'keep v2')
        preview = plan_exports(job, self.target, auto_version=True, naming_rule='copy')
        self.assertEqual([r['relative_name'] for r in preview.rows], ['copyv3_m.bytes','copyv4_m.bytes'])
        result = export_outputs(job, self.target, auto_version=True, naming_rule='copy', expected_rows=preview.rows)
        self.assertTrue(result.ok, result.rows)
        for row in result.rows:
            self.assertEqual(decode_template((self.target/row['relative_name']).read_bytes()).template_id,
                             row['relative_name'].removesuffix('_m.bytes'))
        self.assertEqual((self.target/'copy_m.bytes').read_bytes(), b'keep')
        self.assertEqual((self.target/'COPYv2_m.bytes').read_bytes(), b'keep v2')
        self.assertEqual([r['new_id'] for r in job.artifact_records()], ['first','second'])

    def test_zip_rule_versions_same_source_id_and_preserves_originals_if_unedited(self):
        job = self.duplicate_id_job()
        for rule, expected in [(None, ['first_m.bytes','second_m.bytes']),
                               ('copy{原模板ID}', ['copysample_m.bytes','copysamplev2_m.bytes'])]:
            with zipfile.ZipFile(io.BytesIO(build_zip(job, naming_rule=rule))) as archive:
                self.assertEqual(archive.namelist(), expected)
                for name in expected:
                    self.assertEqual(decode_template(archive.read(name)).template_id, name.removesuffix('_m.bytes'))


if __name__ == '__main__':
    unittest.main()
