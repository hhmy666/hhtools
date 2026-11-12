"""Manual draft commits remain verified artifact revisions, never raw uploads."""
from dataclasses import replace
from scripts.tests.test_translation import TranslationTests
from scripts.tower_reducer.api import ApiError
from scripts.tower_reducer.storage import JobRepository, _pack, _unpack
from scripts.tower_reducer.template_reader import decode_template
from scripts.tower_reducer.verification import verify_output


class ManualEditTests(TranslationTests):
    def test_export_naming_keeps_manual_geometry_and_current_artifact_unchanged(self):
        import io,zipfile
        job,key,artifact,_=self.prepare()
        manual=self.edit(job,key,artifact,[dict(x=x,y=12,z=0,tile_id=-11) for x in (4,8,12)])
        artifact_id=manual['artifact_id'];before=job.read_artifact(artifact_id)
        original=decode_template(before)
        preview=self.app.export(job.job_id,{'action':'preview','export_root':str(self.root/'out'),'naming_rule':'noT{原模板ID}'})
        expected='noT'+decode_template(job.read_source(key)).template_id
        self.assertEqual(preview['items'][0]['filename'],expected+'_m.bytes')
        self.app.export(job.job_id,{'action':'commit','plan_id':preview['plan_id'],'confirmation_token':preview['confirmation_token'],'conflict_decisions':{}})
        exported=decode_template((self.root/'out'/(expected+'_m.bytes')).read_bytes())
        self.assertEqual(exported.template_id,expected)
        self.assertEqual([t.fields for t in exported.tiles],[t.fields for t in original.tiles])
        self.assertEqual(exported.stacks,original.stacks)
        self.assertEqual(job.read_artifact(artifact_id),before)
        self.assertEqual(job.snapshot()['templates'][key]['output_artifacts'],[artifact_id])
        zipped,_,_=self.app.download_artifact(job.job_id,'bundle',naming_rule='zip{原模板ID}')
        with zipfile.ZipFile(io.BytesIO(zipped)) as archive:
            raw=archive.read('zip'+decode_template(job.read_source(key)).template_id+'_m.bytes')
            self.assertEqual([t.fields for t in decode_template(raw).tiles],[t.fields for t in original.tiles])

    def edit(self, job, key, artifact, additions=(), deleted=(), translation=None):
        payload={'artifact_id':artifact['id'], 'deleted_indices':list(deleted),'additions':list(additions)}
        if translation is not None:payload['translation']=translation
        return self.app.edit_template(job.job_id,key,payload)

    def test_translation_and_edge_additions_commit_in_final_coordinates(self):
        job,key,artifact,baseline=self.prepare()
        extra=[dict(x=x,y=16,z=0,tile_id=-11) for x in (2,6,10)]
        preview=self.edit(job,key,artifact,extra,translation=dict(dx=0,dy=1))
        output=job.read_artifact(preview['artifact_id']);doc=decode_template(output)
        self.assertTrue(job.snapshot()['templates'][key]['plan_summary']['adjusted'])
        self.assertEqual([(t.x,t.y) for t in doc.tiles[-3:]],[(2,16),(6,16),(10,16)])
        self.assertEqual([(t.x,t.y) for t in doc.tiles[:9]],
                         [(t.x,t.y+1) for t in decode_template(baseline).tiles])
        # A subsequent edit translates old additions too, preserving flattening.
        second=self.edit(job,key,preview['artifact'],translation=dict(dx=0,dy=-1))
        raw=job.read_artifact(second['artifact_id'])
        self.assertEqual([(t.x,t.y) for t in decode_template(raw).tiles[-3:]],[(2,15),(6,15),(10,15)])
        self.assertEqual(JobRepository(self.root/'jobs').get(job.job_id).read_artifact(second['artifact_id']),raw)

    def test_delete_boundary_tiles_before_atomic_translation(self):
        job,key,artifact,_=self.prepare()
        preview=self.edit(job,key,artifact,[dict(x=x,y=12,z=0,tile_id=-11) for x in (4,8,14)])
        # X=14 would block this movement if checked before the deletions.
        moved=self.edit(job,key,preview['artifact'],deleted=(9,10,11),translation=dict(dx=1,dy=0))
        self.assertEqual(len(moved['output']['tiles']),9)
        self.assertEqual(max(t['x'] for t in moved['output']['tiles']),14)

    def test_old_manual_plan_defaults_to_no_translation_and_invalid_drafts_preserve_artifact(self):
        job,key,artifact,baseline=self.prepare()
        current=self.edit(job,key,artifact)
        record=job.snapshot()['artifacts'][current['artifact_id']]
        packed=record['plan'].copy();packed['fields']=dict(packed['fields'])
        del packed['fields']['translation']
        legacy=_unpack(packed)
        self.assertEqual(legacy.translation,(0,0))
        self.assertEqual(_unpack(_pack(legacy)),legacy)
        for delta in (dict(dx=True,dy=0),dict(dx=1.5,dy=0),dict(dx=0,dy=33),dict(dx=2,dy=0)):
            with self.subTest(delta=delta),self.assertRaises(ApiError):
                self.edit(job,key,current['artifact'],translation=delta)
            self.assertEqual(job.read_artifact(current['artifact_id']),baseline)

    def test_atomic_translation_moves_tower_anchor_and_members_uniformly(self):
        from scripts.tests.test_increase_engine import source
        from scripts.tower_reducer.increase import build_increase
        raw=source(total=6,towers=((8,8,3,0),),points=[(6,4),(8,4),(10,4)])
        result=build_increase(raw,{'target_mode':'count','added_tiles':3,'operation':'existing'},'new')
        self.assertTrue(result['ok'],result.get('error'))
        job,key,artifact,baseline=self.prepare(raw,result['plan'])
        preview=self.edit(job,key,artifact,translation=dict(dx=1,dy=-1))
        output=job.read_artifact(preview['artifact_id'])
        self.assert_shift(baseline,output,1,-1)
        self.assertEqual([(s.x,s.y,s.direction) for s in decode_template(output).stacks],[(9,7,0)])

    def test_add_three_persists_and_download_reverifies_after_restart(self):
        job,key,artifact,baseline=self.prepare()
        self.assertTrue(callable(getattr(self.app,'edit_template',None)), 'manual edit commit API is missing')
        extra=[dict(x=x,y=12,z=0,tile_id=-11) for x in (4,8,12)]
        preview=self.edit(job,key,artifact,extra)
        self.assertNotEqual(preview['artifact_id'],artifact['id'])
        raw=job.read_artifact(preview['artifact_id'])
        self.assertEqual(len(decode_template(raw).tiles),12)
        reopened=JobRepository(self.root/'jobs').get(job.job_id)
        self.assertEqual(reopened.read_artifact(preview['artifact_id']),raw)
        current=job.snapshot()['artifacts'][preview['artifact_id']]
        self.assertTrue(verify_output(job.read_source(key),raw,_unpack(current['plan']),'new').ok)
        second=self.edit(job,key,preview['artifact'],deleted=(9,10,11))
        self.assertEqual(job.read_artifact(second['artifact_id']),baseline)
        with self.assertRaises(ApiError):self.edit(job,key,artifact,extra)

    def test_rejected_edits_preserve_old_verified_artifact(self):
        job,key,artifact,baseline=self.prepare()
        self.assertTrue(callable(getattr(self.app,'edit_template',None)), 'manual edit commit API is missing')
        for extra in ([dict(x=4,y=12,z=2,tile_id=-11)]*3,
                      [dict(x=4,y=4,z=1,tile_id=-11)]*3,
                      [dict(x=1,y=12,z=0,tile_id=-11)]*3,
                      [dict(x=4,y=12,z=0,tile_id=-11)]):
            with self.subTest(extra=extra), self.assertRaises(ApiError):self.edit(job,key,artifact,extra)
            self.assertEqual(job.read_artifact(artifact['id']),baseline)
            self.assertEqual(job.snapshot()['templates'][key]['status'],'verified')

    def test_delete_original_tiles_uses_survivor_origins_not_shifted_indices(self):
        job,key,artifact,baseline=self.prepare()
        preview=self.edit(job,key,artifact,deleted=(6,7,8))
        self.assertEqual(len(preview['output']['tiles']),6)
        self.assertEqual(preview['deleted_count'],3)
        self.assertEqual([t['source_index'] for t in preview['output']['tiles']],list(range(6)))
        self.assertEqual(job.read_source(key),self.repo.get(job.job_id).read_source(key))
        from scripts.tower_reducer.parameter_flow import ParameterFlow
        row=job.snapshot()['artifacts'][preview['artifact_id']]
        entry,_=ParameterFlow(self.app)._entry(job,row)
        self.assertEqual(entry['operation'],'manual')
        self.assertEqual(entry['source_tile_counts'],{'A':6,'B':3})
        self.assertEqual(entry['result_tile_counts'],{'A':6,'B':0})

    def test_delete_support_fails_and_manual_revision_can_translate(self):
        job,key,artifact,baseline=self.prepare()
        with self.assertRaises(ApiError):self.edit(job,key,artifact,deleted=(0,1,2))
        preview=self.edit(job,key,artifact,[dict(x=x,y=12,z=0,tile_id=-11) for x in (4,8,12)])
        moved=self.move(job,key,preview['artifact'],dx=1,dy=0)
        self.assertTrue(moved['output'])
        self.assertEqual(len(decode_template(job.read_artifact(moved['artifact']['id'])).tiles),12)
