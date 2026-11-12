"""Export copies never overwrite existing names, including identical bytes."""
import unittest
import os
from unittest.mock import patch
from scripts.tests import test_export_service as fixtures
from scripts.tower_reducer.export_service import export_outputs, plan_exports
from scripts.tower_reducer.template_reader import decode_template


class VersionedExportTests(unittest.TestCase):
    setUp = fixtures.ExportTests.setUp
    def preview(self):
        return plan_exports(self.job,self.target,auto_version=True)

    def export(self):
        return export_outputs(self.job,self.target,auto_version=True)

    def test_existing_name_uses_v2_in_filename_and_internal_id_without_overwriting(self):
        self.target.mkdir(); old=self.target/'new_m.bytes'; old.write_bytes(b'keep this')
        preview=self.preview()
        self.assertEqual(preview.rows[0]['relative_name'],'newv2_m.bytes')
        self.assertFalse((self.target/'newv2_m.bytes').exists(),'preview is read-only')
        result=self.export()
        self.assertEqual(result.rows[0]['status'],'exported')
        self.assertEqual(old.read_bytes(),b'keep this')
        self.assertEqual(decode_template((self.target/'newv2_m.bytes').read_bytes()).template_id,'newv2')
        self.assertEqual(self.job.artifact_records()[0]['new_id'],'newv2','later download/sync use the exported ID')

    def test_identical_existing_bytes_still_create_next_version_and_notify(self):
        self.target.mkdir(); (self.target/'new_m.bytes').write_bytes(self.output)
        (self.target/'newv2_m.bytes').write_bytes(b'existing v2')
        result=self.export()
        self.assertEqual(result.rows[0]['relative_name'],'newv3_m.bytes')
        self.assertTrue(result.rows[0].get('renamed'))
        self.assertEqual((self.target/'new_m.bytes').read_bytes(),self.output)
        again=self.export()
        self.assertEqual(again.rows[0]['relative_name'],'newv4_m.bytes')

    def test_version_name_already_reserved_by_another_batch_template_is_not_reused(self):
        raw2=self.raw+b'\x78\x01'
        from scripts.tower_reducer.storage import InputSnapshot
        from scripts.tower_reducer.rewrite import rewrite_template
        job=self.repo.create([InputSnapshot(self.raw,'a_m.bytes'),InputSnapshot(raw2,'b_m.bytes')])
        a,b=list(job.snapshot()['templates'])
        job.register_output(a,self.output,self.plan,'new')
        job.register_output(b,rewrite_template(raw2,self.plan,'newv2'),self.plan,'newv2')
        self.target.mkdir(); (self.target/'new_m.bytes').write_bytes(b'old')
        report=plan_exports(job,self.target,auto_version=True)
        self.assertEqual([row['relative_name'] for row in report.rows],['newv3_m.bytes','newv2_m.bytes'])

    def test_name_taken_after_final_check_is_not_overwritten(self):
        real_link = os.link
        def concurrent_link(src, dst, **kwargs):
            # Another process creates the destination after our last evidence
            # check but before the atomic no-clobber publication.
            (self.target / dst).write_bytes(b'concurrent owner')
            return real_link(src, dst, **kwargs)
        with patch('scripts.tower_reducer.storage.os.link', side_effect=concurrent_link):
            result = self.export()
        self.assertEqual(result.rows[0]['status'], 'failed')
        self.assertEqual(result.rows[0]['error']['code'], 'target_changed')
        self.assertFalse(result.rows[0]['applied'])
        self.assertEqual((self.target/'new_m.bytes').read_bytes(), b'concurrent owner')
        self.assertEqual(list(self.target.glob('.tmp-*')), [])

    def test_name_occupied_after_preview_requires_a_new_preview(self):
        from scripts.tower_reducer.export_service import TargetChanged
        preview = self.preview()
        self.target.mkdir()
        (self.target/'new_m.bytes').write_bytes(b'concurrent owner')
        with self.assertRaises(TargetChanged):
            export_outputs(self.job, self.target, auto_version=True, expected_rows=preview.rows)
        self.assertFalse((self.target/'newv2_m.bytes').exists())
        self.assertEqual(self.preview().rows[0]['relative_name'], 'newv2_m.bytes')

if __name__=='__main__': unittest.main()
