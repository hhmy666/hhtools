"""Grandfather only immutable original ordinary pairs, never replacement tiles."""
import unittest
from scripts.tests import test_translation
from scripts.tests.test_ordinary_add_engine import source
from scripts.tower_reducer.storage import InputSnapshot,JobRepository,_unpack
from scripts.tower_reducer.template_reader import decode_template
from scripts.tower_reducer.verification import verify_output,VerificationReport,enforce_output_structure
from scripts.tower_reducer.api import ApiError


class InheritedGeometryTests(unittest.TestCase):
    setUp=test_translation.TranslationTests.setUp

    def draft(self):
        raw=source([(4,4,0),(8,4,0),(12,4,0),(4,4,1),(9,5,1),(13,5,1)],[(12,12,3,0)])
        job=self.repo.create([InputSnapshot(raw,'input_m.bytes')]);key=next(iter(job.snapshot()['templates']))
        job.update_template(key,status='failed',stage='placement');job.set_status('failed')
        self.app.recent_jobs.append(job)
        preview=self.app.manual_recovery(job.job_id,key,{})
        return job,key,raw,preview

    def save(self,job,key,preview,deleted=(),additions=()):
        return self.app.manual_recovery(job.job_id,key,dict(recovery_token=preview['recovery_token'],
            deleted_indices=list(deleted),additions=list(additions)))

    def test_source_pair_survives_save_restart_export_translation_with_warning(self):
        job,key,raw,preview=self.draft()
        self.assertEqual(len(preview['source_geometry_warnings']),1)
        self.assertEqual(preview['source_geometry_warnings'][0]['upper_z'],1)
        self.assertEqual([t['index'] for t in preview['output']['tiles'] if t.get('inherited_full_cover')],[0,3])
        saved=self.save(job,key,preview)
        artifact=saved['artifact_id'];output=job.read_artifact(artifact)
        self.assertEqual(JobRepository(self.root/'jobs').get(job.job_id).read_artifact(artifact),output)
        renamed,_=job.renamed_output_bytes(artifact,'exported')
        self.assertEqual(decode_template(renamed).template_id,'exported')
        record=job.snapshot()['artifacts'][artifact]
        report=verify_output(raw,output,_unpack(record['plan']),record['new_id'])
        self.assertTrue(report.ok,report)
        self.assertEqual(report.warnings[0].code,'inherited_full_cover')
        moved=self.app.translate_template(job.job_id,key,dict(artifact_id=artifact,dx=1,dy=0))
        self.assertEqual(moved['output_geometry_warnings'][0]['x'],5)
        self.assertEqual(self.app.job_payload(job.job_id)['templates'][key]['source_geometry_warnings'][0]['x'],4)

    def test_deleted_old_upper_cannot_grandfather_replacement_addition(self):
        job,key,_,preview=self.draft();before=job.snapshot()
        added=[dict(x=x,y=y,z=1,tile_id=-10) for x,y in ((4,4),(9,5),(13,5))]
        with self.assertRaises(ApiError):self.save(job,key,preview,(3,4,5),added)
        self.assertEqual(job.snapshot(),before)
        repaired=self.save(job,key,preview,(3,4,5))
        self.assertEqual(repaired['output_geometry_warnings'],[])
        self.assertFalse(any(t.get('inherited_full_cover') for t in repaired['output']['tiles']))

    def test_unbound_structure_verification_keeps_full_cover_strict(self):
        _,_,raw,_=self.draft()
        report=enforce_output_structure(VerificationReport({},(),()),raw)
        self.assertFalse(report.ok)
        self.assertTrue(any(e.code=='full_cover' for e in report.errors))

    def test_new_tile_can_reuse_nonadjacent_xy_with_quarter_support(self):
        job,key,raw,preview=self.draft()
        additions=[dict(x=x,y=y,z=z,tile_id=-10) for x,y,z in ((5,5,2),(4,4,3),(10,6,2))]
        saved=self.save(job,key,preview,additions=additions)
        output=job.read_artifact(saved['artifact_id'])
        self.assertEqual([(t.x,t.y,t.z) for t in decode_template(output).tiles[-3:]],[(5,5,2),(4,4,3),(10,6,2)])
        self.assertEqual(job.read_source(key),raw)
        self.assertEqual(JobRepository(self.root/'jobs').get(job.job_id).read_artifact(saved['artifact_id']),output)

    def test_new_adjacent_exact_xy_is_still_rejected_at_save(self):
        job,key,_,preview=self.draft();before=job.snapshot()
        additions=[dict(x=x,y=y,z=z,tile_id=-10) for x,y,z in ((4,4,2),(8,12,0),(12,12,0))]
        with self.assertRaises(ApiError):self.save(job,key,preview,additions=additions)
        self.assertEqual(job.snapshot(),before)

    def test_user_can_delete_special_tile_occurrences_when_final_structure_and_counts_are_valid(self):
        from scripts.tests.test_rewrite import computed_plan
        from scripts.tower_reducer.protobuf_wire import parse_fields,replace_length_delimited,encode_int32
        raw=source([(x,y,0) for y in (4,8,12) for x in (4,8,12)])
        document=decode_template(raw)
        specials={t.top_field_index for t in document.tiles[6:]}
        raw=b''.join(replace_length_delimited(field,b''.join(
            f.tag_raw+encode_int32(-12) if f.number==4 else f.raw for f in parse_fields(field.payload)))
            if i in specials else field.raw for i,field in enumerate(document.fields))
        job,key,artifact,_=test_translation.TranslationTests.prepare(self,raw,computed_plan(raw))
        before=job.snapshot()
        with self.assertRaises(ApiError):
            self.app.edit_template(job.job_id,key,dict(artifact_id=artifact['id'],deleted_indices=[6],additions=[]))
        self.assertEqual(job.snapshot(),before)
        saved=self.app.edit_template(job.job_id,key,dict(artifact_id=artifact['id'],deleted_indices=[6,7,8],additions=[]))
        output=job.read_artifact(saved['artifact_id'])
        self.assertEqual([t.fields for t in decode_template(output).tiles],[t.fields for t in decode_template(raw).tiles[:6]])
        self.assertEqual(job.read_source(key),raw)
