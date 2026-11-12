"""Failed reduction recovery is a read-only draft until independently verified."""
import unittest
from scripts.tests import test_translation
from scripts.tests import test_api
from scripts.tests.test_increase_engine import source
from scripts.tower_reducer.storage import InputSnapshot, JobRepository, _unpack
from scripts.tower_reducer.template_reader import decode_template
from scripts.tower_reducer.api import ApiError
from scripts.tower_reducer.verification import verify_output


class ManualRecoveryTests(unittest.TestCase):
    setUp=test_translation.TranslationTests.setUp
    def failed(self, ordinary=3):
        raw=source(total=ordinary+3,towers=((12,12,3,0),),points=[(4,4),(8,4),(12,4)])
        job=self.repo.create([InputSnapshot(raw,'input_m.bytes')])
        key=next(iter(job.snapshot()['templates']))
        job.update_template(key,status='failed',stage='placement',options={'distribution':{},'placement':{}},
                            naming={'new_id':'new'},plan_summary={'operation':'reduce'})
        job.set_status('failed'); self.app.recent_jobs.append(job)
        return job,key,raw

    def test_preview_is_read_only_and_valid_removal_only_can_save(self):
        job,key,raw=self.failed()
        job.update_template(key,plan_summary={'operation':'reduce','move_count':30,'added_layers':3,
            'destinations':[{'x':6,'y':6,'z':1}], 'added_tile_counts':{'A':30,'B':0},
            'removed_tile_counts':{'A':30,'B':0},'placement_failure':{'requested':30}})
        before=job.snapshot()
        preview=self.app.manual_recovery(job.job_id,key,{})
        self.assertEqual(job.snapshot(),before)
        self.assertTrue(preview['manual_recovery']); self.assertIsNone(preview['artifact_id'])
        self.assertEqual(preview['output']['towers'],[])
        self.assertEqual([t['source_index'] for t in preview['output']['tiles']],[0,1,2])
        saved=self.app.manual_recovery(job.job_id,key,dict(recovery_token=preview['recovery_token'],deleted_indices=[],additions=[]))
        output=job.read_artifact(saved['artifact_id']);doc=decode_template(output)
        self.assertEqual([t.fields for t in doc.tiles],[t.fields for t in decode_template(raw).tiles[:3]])
        self.assertEqual(job.read_source(key),raw)
        self.assertEqual(JobRepository(self.root/'jobs').get(job.job_id).read_artifact(saved['artifact_id']),output)
        row=job.snapshot()['templates'][key]
        self.assertTrue(row['plan_summary']['adjusted'])
        self.assertEqual(row['plan_summary']['final_total'],3)
        self.assertEqual(row['plan_summary']['removed_tile_counts'],{'A':3,'B':0})
        self.assertEqual(row['plan_summary']['added_tile_counts'],{'A':0,'B':0})
        self.assertEqual(row['plan_summary']['move_count'],0)
        self.assertEqual(row['plan_summary']['added_layers'],0)
        self.assertEqual(row['plan_summary']['destinations'],[])
        self.assertIsNone(row['plan_summary']['placement_failure'])
        from scripts.tower_reducer.parameter_flow import ParameterFlow
        entry,_=ParameterFlow(self.app)._entry(job,job.snapshot()['artifacts'][saved['artifact_id']])
        self.assertEqual(entry['operation'],'manual')
        renamed,_=job.renamed_output_bytes(saved['artifact_id'],'renamed')
        self.assertEqual(decode_template(renamed).template_id,'renamed')
        # Existing edit/translation APIs continue from the persisted recovery plan.
        edited=self.app.edit_template(job.job_id,key,dict(artifact_id=saved['artifact_id'],deleted_indices=[],
            additions=[dict(x=x,y=12,z=0,tile_id=-11) for x in (4,8,12)]))
        self.assertEqual(job.snapshot()['templates'][key]['plan_summary']['added_tile_counts'],{'A':0,'B':3})
        shifted=self.app.translate_template(job.job_id,key,dict(artifact_id=edited['artifact_id'],dx=1,dy=0))
        self.assertEqual(len(decode_template(job.read_artifact(shifted['artifact_id'])).tiles),6)
        with self.assertRaises(ApiError):
            self.app.manual_recovery(job.job_id,key,dict(recovery_token=preview['recovery_token'],deleted_indices=[],additions=[]))

    def test_incomplete_seed_rejects_save_without_state_change_then_accepts_replenishment(self):
        job,key,raw=self.failed(ordinary=2); preview=self.app.manual_recovery(job.job_id,key,{})
        before=job.snapshot()
        with self.assertRaises(ApiError):
            self.app.manual_recovery(job.job_id,key,dict(recovery_token=preview['recovery_token'],deleted_indices=[],additions=[]))
        self.assertEqual(job.snapshot(),before)
        saved=self.app.manual_recovery(job.job_id,key,dict(recovery_token=preview['recovery_token'],deleted_indices=[],
            additions=[dict(x=12,y=4,z=0,tile_id=-10)]))
        output=job.read_artifact(saved['artifact_id']); record=job.snapshot()['artifacts'][saved['artifact_id']]
        plan=_unpack(record['plan'])
        self.assertTrue(verify_output(raw,output,plan,'new').ok)
        self.assertFalse(verify_output(raw,output+b'\x98\x06\x01',plan,'new').ok)

    def test_stale_attempt_and_busy_or_wrong_mode_rejected(self):
        job,key,_=self.failed(); preview=self.app.manual_recovery(job.job_id,key,{})
        state=job.snapshot();state['templates'][key]['attempt_id']=2;job._save(state)
        before=job.snapshot()
        with self.assertRaises(ApiError):
            self.app.manual_recovery(job.job_id,key,dict(recovery_token=preview['recovery_token'],deleted_indices=[],additions=[]))
        self.assertEqual(job.snapshot(),before)
        job.update_template(key,options={'mode':'increase'})
        with self.assertRaises(ApiError): self.app.manual_recovery(job.job_id,key,{})

    def test_busy_invalid_and_duplicate_requests_preserve_failed_state(self):
        job,key,_=self.failed(); preview=self.app.manual_recovery(job.job_id,key,{})
        self.app._workers[job.job_id]=object()
        before=job.snapshot()
        with self.assertRaises(ApiError):self.app.manual_recovery(job.job_id,key,{})
        self.assertEqual(job.snapshot(),before)
        self.app._workers.pop(job.job_id)
        for fields in ({'deleted_indices':[0,0],'additions':[]},
                       {'deleted_indices':[False],'additions':[]},
                       {'deleted_indices':[99],'additions':[]},
                       {'deleted_indices':[],'additions':[dict(x=4,y=4,z=0,tile_id=-10)]*3}):
            with self.subTest(fields=fields),self.assertRaises(ApiError):
                self.app.manual_recovery(job.job_id,key,dict(recovery_token=preview['recovery_token'],**fields))
            self.assertEqual(job.snapshot(),before)

    def test_seed_not_publishable_and_independent_whitelist_rejects_bad_rewriter(self):
        from hashlib import sha256
        from unittest.mock import patch
        from scripts.tower_reducer.manual_edit import TowerRemovalPlan,ManualEditPlan,tower_removal_fields
        from scripts.tower_reducer.rewrite import rewrite_template
        job,key,raw=self.failed()
        seed=TowerRemovalPlan(sha256(raw).hexdigest())
        baseline=rewrite_template(raw,seed,'new')
        self.assertFalse(verify_output(raw,baseline,seed,'new').ok)
        plan=ManualEditPlan(seed,sha256(raw).hexdigest(),(),())
        corrupted=tower_removal_fields(raw,seed,'new')+(b'\x98\x06\x01',)
        with patch('scripts.tower_reducer.manual_edit.tower_removal_fields',return_value=corrupted):
            output=rewrite_template(raw,plan,'new')
            self.assertFalse(verify_output(raw,output,plan,'new').ok)

    def test_reduce_search_limit_expands_search_before_publishing_failure(self):
        from unittest.mock import patch
        from scripts.tower_reducer.api import plan_joint_layout
        from scripts.tower_reducer.placement import JointLayout, PlacementFailure
        job,key,_=self.failed()
        job.update_template(key,parameters={'ratio':0,'min_ratio':0})
        attempts=[]
        def initially_limited(*args,**kwargs):
            attempts.append(kwargs.get('search_budget'))
            if len(attempts)==1:
                from scripts.tower_reducer.distribution import plan_tower_distribution
                return JointLayout(plan_tower_distribution(args[0],args[1],args[2]),
                    PlacementFailure('search_limit','bounded',3,0,3,(),(),(),'top_first',0),1,False)
            self.assertEqual(job.snapshot()['templates'][key]['status'],'generating')
            return plan_joint_layout(*args,**kwargs)
        with patch('scripts.tower_reducer.api.plan_joint_layout',side_effect=initially_limited):
            self.app._run_template(job,key)
        row=job.snapshot()['templates'][key]
        self.assertEqual(row['status'],'verified',row)
        self.assertGreaterEqual(len(attempts),2)
        self.assertTrue(any('扩大搜索' in log['message'] for log in row['logs']))

    def test_inherited_source_pair_remains_verified_with_exact_warning_coordinates(self):
        from scripts.tests.test_ordinary_add_engine import source as ordinary_source
        raw=ordinary_source([(4,4,0),(4,4,1),(8,4,0)],[(12,12,3,0)])
        job=self.repo.create([InputSnapshot(raw,'input_m.bytes')]);key=next(iter(job.snapshot()['templates']))
        job.update_template(key,parameters={'ratio':0,'min_ratio':0});job.set_status('running')
        self.app._run_template(job,key)
        row=job.snapshot()['templates'][key]
        self.assertEqual(row['status'],'verified',row)
        self.assertIsNone(row['error'])
        self.assertEqual(len(row['plan_summary']['layout_search']['attempts']),1)
        warning=row['verification']['facts']['source_geometry_warnings'][0]
        self.assertEqual((warning['x'],warning['y'],warning['lower_z'],warning['upper_z']),(4,4,0,1))


class ManualRecoveryHttpTests(test_api.ApiContractCase):
    def test_http_preview_save_and_csrf_boundary(self):
        app=self.start_server()
        raw=source(total=6,towers=((12,12,3,0),),points=[(4,4),(8,4),(12,4)])
        job=self.repository.create([InputSnapshot(raw,'input_m.bytes')]);key=next(iter(job.snapshot()['templates']))
        job.update_template(key,status='failed',stage='placement',parameters={'ratio':0,'min_ratio':0})
        job.set_status('failed');app.recent_jobs.append(job)
        endpoint='/api/jobs/'+job.job_id+'/manual-recovery/'+key
        before=job.snapshot()
        self.assert_error(self.request('POST',endpoint,json_body={},csrf=False),403,'csrf_failed')
        response=self.request('POST',endpoint,json_body={})
        self.assertEqual(response[0],200,response[3]);self.assertEqual(job.snapshot(),before)
        draft=response[2]
        result=self.request('POST',endpoint,json_body=dict(recovery_token=draft['recovery_token'],deleted_indices=[],additions=[]))
        self.assertEqual(result[0],200,result[3])
        downloaded=self.request('GET','/api/jobs/'+job.job_id+'/download/'+result[2]['artifact_id'])
        self.assertEqual(downloaded[0],200,downloaded[3])
        self.assertEqual(len(decode_template(downloaded[3]).tiles),3)
