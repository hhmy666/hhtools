"""Whole-board quarter turns retain trusted field and occurrence identities."""
from dataclasses import replace
import unittest

from scripts.tests import test_translation,test_api
from scripts.tests import test_inherited_geometry
from scripts.tower_reducer.api import ApiError
from scripts.tower_reducer.rewrite import rewrite_template
from scripts.tower_reducer.storage import JobRepository, _pack, _unpack
from scripts.tower_reducer.template_reader import decode_template
from scripts.tower_reducer.verification import verify_output


class RotationTests(unittest.TestCase):
    setUp = test_translation.TranslationTests.setUp
    prepare = test_translation.TranslationTests.prepare

    def rotate(self, job, key, artifact, direction='cw'):
        return self.app.rotate_template(job.job_id,key,dict(artifact_id=artifact['id'],direction=direction))

    def test_clockwise_rotation_preserves_unknown_bytes_and_four_turns_reset_exactly(self):
        job,key,artifact,baseline=self.prepare()
        before=decode_template(baseline)
        preview=self.rotate(job,key,artifact)
        output=job.read_artifact(preview['artifact_id']);after=decode_template(output)
        self.assertEqual((after.tiles[0].x,after.tiles[0].y),(13,5))
        self.assertEqual([(t.x,t.y,t.z,t.tile_id) for t in after.tiles],
                         [(17-t.y,t.x+1,t.z,t.tile_id) for t in before.tiles])
        for old,new in zip(before.tiles,after.tiles):
            self.assertEqual([f.raw for f in old.fields if f.number not in (1,2)],
                             [f.raw for f in new.fields if f.number not in (1,2)])
        self.assertEqual(preview['changed_count'],3)
        self.assertEqual([t['source_index'] for t in preview['output']['tiles']],list(range(9)))
        self.assertEqual(JobRepository(self.repo.root).get(job.job_id).read_artifact(preview['artifact_id']),output)
        record=job.snapshot()['artifacts'][preview['artifact_id']];plan=_unpack(record['plan'])
        self.assertFalse(verify_output(job.read_source(key),output.replace(b'xy',b'zz',1),plan,'new').ok)
        self.assertFalse(verify_output(job.read_source(key),output,replace(plan,quarter_turns=3),'new').ok)
        for _ in range(3):preview=self.rotate(job,key,preview['artifact'])
        self.assertEqual(job.read_artifact(preview['artifact_id']),baseline)

    def test_atomic_rotation_translation_deletion_and_final_coordinate_additions(self):
        job,key,artifact,baseline=self.prepare()
        saved=self.app.edit_template(job.job_id,key,dict(artifact_id=artifact['id'],deleted_indices=[6,7,8],
            additions=[dict(x=x,y=12,z=0,tile_id=-11) for x in (4,7,10)],
            transform=dict(quarter_turns=1,dx=-1,dy=0)))
        doc=decode_template(job.read_artifact(saved['artifact_id']))
        self.assertEqual([(t.x,t.y) for t in doc.tiles[:3]],[(12,5),(12,9),(12,13)])
        self.assertEqual([(t.x,t.y) for t in doc.tiles[-3:]],[(4,12),(7,12),(10,12)])
        # Rotating back also rotates the old translation vector: (-1,0)->(0,1).
        returned=self.rotate(job,key,saved['artifact'],'ccw')
        final=decode_template(job.read_artifact(returned['artifact_id']))
        self.assertEqual([(t.x,t.y) for t in final.tiles[:3]],[(4,5),(8,5),(12,5)])
        self.assertEqual([(t.x,t.y) for t in final.tiles[-3:]],[(11,13),(11,10),(11,7)])
        plan=_unpack(job.snapshot()['artifacts'][returned['artifact_id']]['plan'])
        self.assertEqual(plan.translation,(0,1));self.assertEqual(plan.quarter_turns,0)
        self.assertEqual(_unpack(_pack(plan)),plan)

    def test_rotation_changes_tower_direction_without_changing_z(self):
        from scripts.tests.test_increase_engine import source
        from scripts.tower_reducer.increase import build_increase
        raw=source(total=6,towers=((8,8,3,0),),points=[(6,4),(8,4),(10,4)])
        result=build_increase(raw,{'target_mode':'count','added_tiles':3,'operation':'existing'},'new')
        job,key,artifact,baseline=self.prepare(raw,result['plan'])
        preview=self.rotate(job,key,artifact)
        doc=decode_template(job.read_artifact(preview['artifact_id']))
        self.assertEqual([(s.x,s.y,s.direction) for s in doc.stacks],[(9,9,3)])
        self.assertEqual([t.z for t in doc.tiles],[t.z for t in decode_template(baseline).tiles])
        inverse=self.rotate(job,key,preview['artifact'],'ccw')
        self.assertEqual(job.read_artifact(inverse['artifact_id']),baseline)

    def test_rejected_rotation_keeps_current_artifact_and_source_pair_inheritance(self):
        fixture=test_inherited_geometry.InheritedGeometryTests();fixture.__dict__.update(self.__dict__)
        job,key,_,draft=fixture.draft();saved=fixture.save(job,key,draft)
        rotated=self.rotate(job,key,saved['artifact'])
        self.assertEqual([(w['x'],w['y']) for w in rotated['output_geometry_warnings']],[(13,5)])
        before=job.snapshot()
        replacements=[{name:t[name] for name in ('x','y','z','tile_id')} for t in rotated['output']['tiles'][3:6]]
        with self.assertRaises(ApiError):
            self.app.edit_template(job.job_id,key,dict(artifact_id=rotated['artifact_id'],deleted_indices=[3,4,5],additions=replacements))
        self.assertEqual(job.snapshot(),before)
        with self.assertRaises(ApiError):self.rotate(job,key,rotated['artifact'],'invalid')
        self.assertEqual(job.snapshot(),before)
        with self.assertRaises(ApiError):
            self.app.edit_template(job.job_id,key,dict(artifact_id=rotated['artifact_id'],deleted_indices=[],additions=[],
                transform=dict(quarter_turns=1,dx=20,dy=0)))
        self.assertEqual(job.snapshot(),before)

    def test_unknown_and_duplicate_tower_direction_fields_survive_rotation(self):
        from scripts.tests.test_increase_engine import source
        from scripts.tests.test_rewrite import scalar,UNKNOWN
        from scripts.tower_reducer.protobuf_wire import replace_length_delimited
        raw=source(total=6,towers=((8,8,3,0),),points=[(4,4),(8,4),(12,4)])
        document=decode_template(raw);stack=document.stacks[0]
        # A duplicate earlier direction and opaque fields are immutable;
        # omitted effective direction defaults to up and must become explicit.
        for payload in (scalar(1,8)+scalar(2,8)+UNKNOWN,
                        scalar(1,8)+scalar(2,8)+scalar(3,2)+UNKNOWN+scalar(3,0)):
            candidate=b''.join(replace_length_delimited(f,payload) if i==stack.top_field_index else f.raw
                               for i,f in enumerate(document.fields))
            # Keep the tower unchanged in this baseline by using an increase plan.
            from scripts.tower_reducer.increase import build_increase
            result=build_increase(candidate,{'target_mode':'count','added_tiles':3,'operation':'existing'},'new')
            job,key,artifact,baseline=self.prepare(candidate,result['plan'])
            rotated=self.rotate(job,key,artifact)
            before=decode_template(baseline).stacks[0]
            after=decode_template(job.read_artifact(rotated['artifact_id'])).stacks[0]
            self.assertEqual(after.direction,3)
            self.assertEqual([f.raw for f in before.fields if f.number not in (1,2,3)],
                             [f.raw for f in after.fields if f.number not in (1,2,3)])
            self.assertEqual([f.value for f in after.fields if f.number==3],
                             [2,3] if len([f for f in before.fields if f.number==3])==2 else [3])


class RotationHttpTests(test_api.ApiContractCase):
    def test_rotation_route_saves_verified_bytes_and_rejects_stale_or_csrf_requests(self):
        self.start_server()
        imported=self.import_files([('input_m.bytes',test_api.synthetic()[0])])
        job_id=self.start_job(imported['import_id'],defaults={'ratio':0,'min_ratio':0})
        row=self.rows(self.wait_job(job_id))[0];artifact=row['artifacts'][0]
        endpoint='/api/jobs/'+job_id+'/rotate/'+row['key']
        payload=dict(artifact_id=artifact['id'],direction='cw')
        self.assert_error(self.request('POST',endpoint,json_body=payload,csrf=False),403,'csrf_failed')
        rotated=self.request('POST',endpoint,json_body=payload)
        self.assertEqual(rotated[0],200,rotated[3])
        self.assertEqual(rotated[2]['transform']['quarter_turns'],1)
        self.assert_error(self.request('POST',endpoint,json_body=payload),409,'stale_artifact')
        inverse=self.request('POST',endpoint,json_body=dict(artifact_id=rotated[2]['artifact_id'],direction='ccw'))
        self.assertEqual(inverse[0],200,inverse[3])
        self.assertEqual(inverse[2]['transform']['quarter_turns'],0)
