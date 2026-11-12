import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from scripts.tests.test_rewrite import synthetic
from scripts.tower_reducer.rewrite import rewrite_template
from scripts.tower_reducer.storage import FileOps, InputSnapshot, JobRepository, atomic_write_json


class HookOps(FileOps):
    def __init__(self, callback):
        self.callback = callback

    def checkpoint(self, stage, path):
        self.callback(stage, Path(path))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.raw, self.plan = synthetic()
        self.repo = JobRepository(self.root/'jobs')

    def create(self, **kwargs):
        return self.repo.create([InputSnapshot(self.raw, '输入_m.bytes', self.root/'inputs')], **kwargs)

    def test_snapshot_sha_metadata_dedup_and_parameter_ownership(self):
        params = {'options': [1,2]}
        job = self.repo.create([InputSnapshot(self.raw,'one_m.bytes'), InputSnapshot(self.raw,'two_m.bytes')], parameters=params)
        params['options'].append(3)
        state=job.snapshot()
        self.assertEqual(len(state['templates']),1)
        row=next(iter(state['templates'].values()))
        self.assertEqual(row['source_sha256'],hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(row['original_names'],['one_m.bytes','two_m.bytes'])
        self.assertEqual(row['source_template_id'],'sample')
        self.assertEqual(state['parameters'],{'options':[1,2]})
        self.assertEqual(job.read_source(row['key']),self.raw)
        self.assertEqual((job.directory/'templates'/row['key']/'source.bytes').stat().st_mode & 0o222,0)

    def test_atomic_state_failure_preserves_old_json_and_ignores_orphans(self):
        target=self.root/'state.json'
        atomic_write_json(target, {'value':1})
        for phase in ('write','fsync_file','replace'):
            def fail(stage,path, phase=phase):
                if stage==phase:
                    raise OSError('injected')
            with self.subTest(phase=phase),self.assertRaises(OSError):
                atomic_write_json(target, {'value':2}, HookOps(fail))
            self.assertEqual(json.loads(target.read_text()),{'value':1})
            self.assertFalse(list(self.root.glob('.tmp-*')))
        job=self.create()
        (job.directory/'.tmp-orphan').write_text('{invalid')
        self.assertEqual(len(JobRepository(self.repo.root).discover()),1)

    def test_paths_symlinks_unknown_ids_are_rejected(self):
        for identifier in ('../escape','/tmp/x','job-abc','中文',''):
            with self.subTest(identifier=identifier),self.assertRaises((ValueError,KeyError)):
                self.repo.get(identifier)
        with self.assertRaises(KeyError):
            self.repo.get('job-'+'a'*32)
        link=self.root/'linked'; link.symlink_to(self.repo.root,target_is_directory=True)
        with self.assertRaises(ValueError):
            JobRepository(link)
        job=self.create(); key=next(iter(job.snapshot()['templates']))
        folder=job.directory/'templates'/key
        folder.rename(job.directory/'moved')
        folder.symlink_to(job.directory/'moved',target_is_directory=True)
        with self.assertRaises(ValueError):
            job.read_source(key)

    def test_recovery_marks_active_states_and_preserves_verified_output(self):
        job=self.create(); key=next(iter(job.snapshot()['templates']))
        artifact=job.register_output(key,rewrite_template(self.raw,self.plan,'new'),self.plan,'new')
        self.assertIsNotNone(artifact)
        job.set_status('running')
        restored=JobRepository(self.repo.root).discover()[0]
        self.assertEqual(restored.snapshot()['status'],'interrupted')
        self.assertEqual(restored.snapshot()['templates'][key]['status'],'verified')
        self.assertEqual(restored.read_artifact(artifact['id']),rewrite_template(self.raw,self.plan,'new'))
        for status in ('queued','running','validating','generating','verifying','generated'):
            restored.update_template(key,status=status)
            again=JobRepository(self.repo.root).discover()[0]
            self.assertEqual(again.snapshot()['templates'][key]['status'],'interrupted')
        for status in ('succeeded','failed','no_op'):
            again.set_status(status)
            self.assertEqual(JobRepository(self.repo.root).discover()[0].snapshot()['status'],status)

    def test_recovery_preserves_output_from_pre_conversion_plan_format(self):
        job = self.create()
        key = next(iter(job.snapshot()['templates']))
        output = rewrite_template(self.raw, self.plan, 'legacy')
        artifact = job.register_output(key, output, self.plan, 'legacy')
        state = job.snapshot()
        fields = state['artifacts'][artifact['id']]['plan']['fields']['distribution']['fields']
        fields.pop('removed_counts', None)
        fields.pop('final_counts', None)
        fields.pop('replenishment_ids', None)
        atomic_write_json(job.directory / 'state.json', state)
        restored = JobRepository(self.repo.root).discover()[0]
        self.assertEqual(restored.snapshot()['templates'][key]['status'], 'verified')
        self.assertEqual(restored.read_artifact(artifact['id']), output)

    def test_unknown_conversion_plan_field_still_rejects_the_artifact(self):
        job = self.create()
        key = next(iter(job.snapshot()['templates']))
        artifact = job.register_output(key, rewrite_template(self.raw, self.plan, 'legacy'), self.plan, 'legacy')
        state = job.snapshot()
        fields = state['artifacts'][artifact['id']]['plan']['fields']['distribution']['fields']
        fields['unexpected_count_permission'] = True
        atomic_write_json(job.directory / 'state.json', state)
        restored = JobRepository(self.repo.root).discover()[0]
        self.assertEqual(restored.snapshot()['templates'][key]['status'], 'failed')
        self.assertEqual(restored.artifact_records(), ())

    def test_failed_verification_isolated_and_downloads_whitelisted(self):
        job=self.repo.create([InputSnapshot(self.raw,'a_m.bytes'),InputSnapshot(self.raw+b'\x78\x01','b_m.bytes')])
        a,b=list(job.snapshot()['templates'])
        self.assertIsNone(job.register_output(a,self.raw,self.plan,'newA'))
        valid=job.register_output(b,rewrite_template(self.raw+b'\x78\x01',self.plan,'newB'),self.plan,'newB')
        self.assertEqual(job.snapshot()['templates'][a]['status'],'failed')
        self.assertEqual(job.snapshot()['templates'][b]['status'],'verified')
        self.assertTrue(job.read_artifact(valid['id']))
        for artifact_id in ('../source.bytes','/tmp/data','中文','a/b','%2e%2e',a):
            with self.subTest(artifact_id=artifact_id),self.assertRaises((ValueError,KeyError)):
                job.read_artifact(artifact_id)

    def test_artifact_and_source_tampering_cannot_forge_verified(self):
        job=self.create(); key=next(iter(job.snapshot()['templates']))
        artifact=job.register_output(key,rewrite_template(self.raw,self.plan,'new'),self.plan,'new')
        path=job.directory/artifact['relative_path']
        path.chmod(0o600); path.write_bytes(self.raw)
        with self.assertRaises(ValueError):
            job.read_artifact(artifact['id'])
        state=json.loads((job.directory/'state.json').read_text())
        state['artifacts'][artifact['id']]['sha256']=hashlib.sha256(self.raw).hexdigest()
        atomic_write_json(job.directory/'state.json',state)
        with self.assertRaises(ValueError):
            JobRepository(self.repo.root).get(job.job_id).read_artifact(artifact['id'])

    def test_log_limit_and_protected_verified_state(self):
        job=self.create(); key=next(iter(job.snapshot()['templates']))
        for n in range(job.LOG_LIMIT+5):
            job.add_log('event '+str(n),key)
        self.assertEqual(len(job.snapshot()['logs']),job.LOG_LIMIT)
        with self.assertRaises(ValueError):
            job.update_template(key,status='verified')
        with self.assertRaises(ValueError):
            job.update_template(key,output_artifacts=['fake'])

    def test_registration_reloads_temporary_bytes_and_detects_changed_sha(self):
        job=self.create(); key=next(iter(job.snapshot()['templates']))
        def corrupt(stage,path):
            if stage=='registration_readback':
                path.write_bytes(self.raw)
        self.repo.file_ops=HookOps(corrupt)
        self.assertIsNone(job.register_output(key,rewrite_template(self.raw,self.plan,'new'),self.plan,'new'))
        self.assertEqual(job.snapshot()['templates'][key]['status'],'failed')
        self.assertEqual(job.snapshot()['artifacts'],{})

    def test_failed_verification_persists_rule_evidence(self):
        job=self.create(); key=next(iter(job.snapshot()['templates']))
        self.assertIsNone(job.register_output(key,self.raw,self.plan,'new'))
        report=job.snapshot()['templates'][key]['verification']
        self.assertIsNotNone(report)
        self.assertTrue(any(issue['code']=='template_id' for issue in report['errors']))

    def test_start_attempt_preserves_prior_result_and_records_new_parameters(self):
        job=self.create(); key=next(iter(job.snapshot()['templates']))
        artifact=job.register_output(key,rewrite_template(self.raw,self.plan,'new'),self.plan,'new')
        job.start_attempt(key, parameters={'ratio':0}, naming={'rule':'lessT{原模板ID}'})
        row=job.snapshot()['templates'][key]
        self.assertEqual(row['status'],'queued')
        self.assertEqual(row['parameters'],{'ratio':0})
        self.assertEqual(row['attempts'][0]['output_artifacts'],[artifact['id']])
        self.assertEqual(row['attempts'][0]['status'],'verified')

    def test_source_tampering_and_job_symlink_are_rejected(self):
        job=self.create(); key=next(iter(job.snapshot()['templates']))
        source=job.directory/'templates'/key/'source.bytes'
        source.chmod(0o600); source.write_bytes(b'changed')
        with self.assertRaises(ValueError): job.read_source(key)
        jobdir=job.directory; moved=self.root/'moved'; jobdir.rename(moved)
        jobdir.symlink_to(moved,target_is_directory=True)
        with self.assertRaises(ValueError): self.repo.get(job.job_id)

    def test_unknown_and_non_json_parameters_are_audit_summaries(self):
        job=self.create(parameters={'unknown':object()})
        self.assertEqual(job.snapshot()['parameters'],{'unknown':{'unsupported_type':'object'}})
        self.assertEqual(JobRepository(self.repo.root).get(job.job_id).snapshot()['parameters'],job.snapshot()['parameters'])

    def test_corrupt_state_is_diagnosed_without_blocking_other_jobs(self):
        broken=self.create(); good=self.create()
        (broken.directory/'state.json').write_text('{bad')
        repo=JobRepository(self.repo.root)
        self.assertEqual([j.job_id for j in repo.discover()],[good.job_id])
        self.assertEqual(repo.diagnostics[0]['job_id'],broken.job_id)

    def test_invalid_source_isolated_and_random_ids_are_distinct(self):
        job=self.repo.create([InputSnapshot(b'broken','bad_m.bytes'),InputSnapshot(self.raw,'good_m.bytes')])
        rows=list(job.snapshot()['templates'].values())
        self.assertEqual([r['status'] for r in rows],['failed','queued'])
        self.assertNotEqual(self.create().job_id,job.job_id)

    def test_registration_rechecks_temp_after_replace_hook(self):
        job=self.create(); key=next(iter(job.snapshot()['templates']))
        def corrupt(stage,path):
            if stage=='replace' and path.suffix=='.bytes':
                for temp in path.parent.glob('.tmp-*'):
                    temp.chmod(0o600); temp.write_bytes(self.raw)
        self.repo.file_ops=HookOps(corrupt)
        self.assertIsNone(job.register_output(key,rewrite_template(self.raw,self.plan,'new'),self.plan,'new'))
        self.assertEqual(job.snapshot()['artifacts'],{})

    def test_typed_plan_json_preserves_decimal_and_byte_evidence(self):
        from dataclasses import replace
        from decimal import Decimal
        plan=replace(self.plan, distribution_options=replace(self.plan.distribution_options,ratio=Decimal('0')),
                     placement=replace(self.plan.placement,layers=({'evidence':b'\x00\xff'},)))
        job=self.create(); key=next(iter(job.snapshot()['templates']))
        output=rewrite_template(self.raw,plan,'new')
        artifact=job.register_output(key,output,plan,'new')
        self.assertIsNotNone(artifact)
        self.assertEqual(JobRepository(self.repo.root).get(job.job_id).read_artifact(artifact['id']),output)

    def test_discover_isolates_wrong_json_root_and_nested_shapes(self):
        invalid = [[], None, 'string', 3]
        for field,value in [('templates', []), ('templates', {'tpl-'+'a'*32: []}),
                            ('logs', None), ('status', []), ('revision', 'bad')]:
            state=self.create().snapshot(); state[field]=value; invalid.append(state)
        for value in invalid:
            job=self.create()
            if isinstance(value,dict): value['job_id']=job.job_id
            atomic_write_json(job.directory/'state.json',value)
        good=self.create(); repo=JobRepository(self.repo.root)
        recovered=repo.discover()
        self.assertIn(good.job_id,[job.job_id for job in recovered])
        self.assertEqual(len(repo.diagnostics),len(invalid))

    def test_attempt_history_explicit_read_survives_failure_success_and_restart(self):
        from scripts.tower_reducer.export_service import build_zip, plan_exports
        import io
        import zipfile
        job=self.create(); key=next(iter(job.snapshot()['templates']))
        first=job.register_output(key,rewrite_template(self.raw,self.plan,'first'),self.plan,'first')
        job.start_attempt(key,parameters={'ratio':0})
        self.assertEqual(job.snapshot()['templates'][key]['output_artifacts'],[])
        self.assertEqual(job.artifact_records(),())
        self.assertEqual(plan_exports(job,self.root/'exports').rows[0]['classification'],'blocked')
        job.register_output(key,self.raw,self.plan,'second')
        loaded=JobRepository(self.repo.root).discover()[0]
        with zipfile.ZipFile(io.BytesIO(build_zip(loaded))) as archive:
            self.assertEqual(archive.namelist(),[])
        with self.assertRaises((ValueError,KeyError)): loaded.read_artifact(first['id'])
        self.assertEqual(loaded.read_artifact(first['id'],attempt_id=0),rewrite_template(self.raw,self.plan,'first'))
        loaded.start_attempt(key)
        second=loaded.register_output(key,rewrite_template(self.raw,self.plan,'second'),self.plan,'second')
        final=JobRepository(self.repo.root).discover()[0]
        self.assertEqual(final.read_artifact(first['id'],attempt_id=0),rewrite_template(self.raw,self.plan,'first'))
        self.assertEqual(final.read_artifact(second['id']),rewrite_template(self.raw,self.plan,'second'))
        self.assertEqual([record['id'] for record in final.artifact_records()],[second['id']])

    def test_recovery_revalidates_current_artifacts_per_template(self):
        for corruption in ('output_changed','output_missing','source_changed'):
            raw2=self.raw+b'\x78\x01'
            job=self.repo.create([InputSnapshot(self.raw,'a_m.bytes'),InputSnapshot(raw2,'b_m.bytes')])
            a,b=job.snapshot()['templates']
            bad=job.register_output(a,rewrite_template(self.raw,self.plan,'first'),self.plan,'first')
            good=job.register_output(b,rewrite_template(raw2,self.plan,'second'),self.plan,'second')
            job.set_status('succeeded')
            path=job.directory/bad['relative_path']
            if corruption=='source_changed': path=job.directory/'templates'/a/'source.bytes'
            if corruption=='output_missing': path.unlink()
            else: path.chmod(0o600); path.write_bytes(b'changed')
            recovered={j.job_id:j for j in JobRepository(self.repo.root).discover()}[job.job_id]
            state=recovered.snapshot()
            with self.subTest(corruption=corruption):
                self.assertEqual(state['templates'][a]['status'],'failed')
                self.assertEqual(state['templates'][a]['output_artifacts'],[])
                self.assertIsNotNone(state['templates'][a]['verification'])
                self.assertEqual(state['templates'][b]['status'],'verified')
                self.assertEqual(state['status'],'partial_failed')
                self.assertEqual([r['id'] for r in recovered.artifact_records()],[good['id']])

    def test_two_repository_instances_do_not_lose_updates(self):
        job=self.create(); key=next(iter(job.snapshot()['templates']))
        other=JobRepository(self.repo.root).get(job.job_id)
        job.add_log('first',key)
        other.add_log('second',key)
        job.update_template(key,parameters={'ratio':0})
        other.update_template(key,naming={'new_id':'next'})
        result=JobRepository(self.repo.root).get(job.job_id).snapshot()
        self.assertEqual([log['message'] for log in result['logs']],['first','second'])
        self.assertEqual(result['templates'][key]['parameters'],{'ratio':0})
        self.assertEqual(result['templates'][key]['naming'],{'new_id':'next'})

    def test_discover_limits_scan_and_orders_observed_jobs_by_update_time(self):
        jobs=[self.create() for _ in range(4)]
        # Controlled directory mtimes prove ordering independently of random IDs.
        for index,job in enumerate(jobs): os.utime(job.directory,ns=(1000000000*(index+1),)*2)
        repo=JobRepository(self.repo.root)
        self.assertEqual([j.job_id for j in repo.discover(limit=2,scan_limit=4)],[jobs[3].job_id,jobs[2].job_id])
        self.assertTrue(any(d['code']=='result_truncated' for d in repo.diagnostics))
        bounded=JobRepository(self.repo.root)
        self.assertLessEqual(len(bounded.discover(limit=20,scan_limit=2)),2)
        self.assertTrue(any(d['code']=='scan_truncated' for d in bounded.diagnostics))

    def test_ancestor_swap_between_walk_and_open_cannot_write_external_directory(self):
        from unittest.mock import patch
        from scripts.tower_reducer import storage
        ancestor=self.root/'parent'; folder=ancestor/'nested'; folder.mkdir(parents=True)
        outside=self.root/'outside'; (outside/'nested').mkdir(parents=True)
        original_open=storage.os.open
        triggered=[]
        def raced_open(path,flags,*args,**kwargs):
            fd=original_open(path,flags,*args,**kwargs)
            # Swap immediately after the final no-follow component is open.
            if path=='nested' and kwargs.get('dir_fd') is not None and not triggered:
                triggered.append(True); ancestor.rename(self.root/'original-parent')
                ancestor.symlink_to(outside,target_is_directory=True)
            return fd
        with patch.object(storage.os,'open',side_effect=raced_open):
            try: storage.atomic_write_bytes(folder/'result.bytes',b'new')
            except (ValueError,OSError): pass
        self.assertTrue(triggered)
        self.assertEqual(list((outside/'nested').iterdir()),[])

    def test_cleanup_uses_original_fd_after_parent_is_replaced(self):
        from scripts.tower_reducer.storage import prepare_file, discard_temp
        parent=self.root/'parent'; parent.mkdir()
        outside=self.root/'outside'; outside.mkdir()
        temp=prepare_file(parent/'result.bytes',b'prepared')
        external=outside/temp.name; external.write_bytes(b'external')
        parent.rename(self.root/'original-parent'); parent.symlink_to(outside,target_is_directory=True)
        discard_temp(temp)
        self.assertEqual(external.read_bytes(),b'external')
        self.assertFalse((self.root/'original-parent'/temp.name).exists())

    def test_job_creation_cannot_follow_root_ancestor_swap(self):
        from unittest.mock import patch
        ancestor=self.root/'parent'; nested=ancestor/'jobs'
        repo=JobRepository(nested)
        outside=self.root/'outside'; (outside/'jobs').mkdir(parents=True)
        original=repo._check_root
        def swap_after_check():
            original(); ancestor.rename(self.root/'original-parent'); ancestor.symlink_to(outside,target_is_directory=True)
        with patch.object(repo,'_check_root',side_effect=swap_after_check):
            with self.assertRaises((ValueError,OSError)):
                repo.create([InputSnapshot(self.raw,'input_m.bytes')])
        self.assertEqual(list((outside/'jobs').iterdir()),[])

    def test_success_to_success_keeps_old_manifest_only_in_explicit_history(self):
        job=self.create(); key=next(iter(job.snapshot()['templates']))
        first=job.register_output(key,rewrite_template(self.raw,self.plan,'first'),self.plan,'first')
        job.start_attempt(key,naming={'new_id':'second'})
        second=job.register_output(key,rewrite_template(self.raw,self.plan,'second'),self.plan,'second')
        recovered=JobRepository(self.repo.root).discover()[0]
        state=recovered.snapshot()
        self.assertEqual(set(state['artifacts']),{second['id']})
        self.assertEqual(state['history_artifacts'][first['id']],first)
        with self.assertRaises(KeyError): recovered.read_artifact(first['id'])
        self.assertEqual(recovered.read_artifact(first['id'],attempt_id=0),rewrite_template(self.raw,self.plan,'first'))
        with self.assertRaises(ValueError): recovered.read_artifact(first['id'],attempt_id=1)

    def test_recovery_invalid_plan_keeps_healthy_sibling_usable(self):
        raw2=self.raw+b'\x78\x01'
        job=self.repo.create([InputSnapshot(self.raw,'a_m.bytes'),InputSnapshot(raw2,'b_m.bytes')])
        a,b=job.snapshot()['templates']
        bad=job.register_output(a,self.raw,self.plan,'bad')
        good=job.register_output(b,rewrite_template(raw2,self.plan,'good'),self.plan,'good')
        # A structurally valid manifest with a corrupted typed plan must be
        # invalidated during recovery, not defer the failure to first download.
        artifact=job.register_output(a,rewrite_template(self.raw,self.plan,'first'),self.plan,'first')
        state=job.snapshot(); state['artifacts'][artifact['id']]['plan']={'decimal':'invalid'}
        atomic_write_json(job.directory/'state.json',state)
        recovered=JobRepository(self.repo.root).discover()[0]
        self.assertEqual(recovered.snapshot()['templates'][a]['status'],'failed')
        self.assertEqual([item['id'] for item in recovered.artifact_records()],[good['id']])

    def test_history_manifest_cannot_be_relabelled_as_current_attempt(self):
        job=self.create(); key=next(iter(job.snapshot()['templates']))
        first=job.register_output(key,rewrite_template(self.raw,self.plan,'first'),self.plan,'first')
        job.start_attempt(key)
        job.register_output(key,rewrite_template(self.raw,self.plan,'second'),self.plan,'second')
        state=job.snapshot(); state['artifacts'][first['id']]=first
        state['templates'][key]['output_artifacts']=[first['id']]
        atomic_write_json(job.directory/'state.json',state)
        with self.assertRaises(ValueError):
            JobRepository(self.repo.root).get(job.job_id).read_artifact(first['id'])

    def test_template_directory_damage_isolated_on_fresh_repository_recovery(self):
        raw2=self.raw+b'\x78\x01'
        for damage in ('missing','symlink','file'):
            with self.subTest(damage=damage):
                job=self.repo.create([InputSnapshot(self.raw,'a_m.bytes'),InputSnapshot(raw2,'b_m.bytes')])
                a,b=job.snapshot()['templates']
                bad=job.register_output(a,rewrite_template(self.raw,self.plan,'first'),self.plan,'first')
                good=job.register_output(b,rewrite_template(raw2,self.plan,'second'),self.plan,'second')
                job.set_status('succeeded')
                folder=job.directory/'templates'/a
                moved=self.root/('moved-'+damage); folder.rename(moved)
                evidence={str(path.relative_to(moved)):path.read_bytes() for path in moved.rglob('*') if path.is_file()}
                if damage=='symlink': folder.symlink_to(moved,target_is_directory=True)
                if damage=='file': folder.write_bytes(b'not a directory')
                fresh=JobRepository(self.repo.root)
                self.assertEqual(fresh.get(job.job_id).job_id,job.job_id)
                recovered={item.job_id:item for item in fresh.discover()}[job.job_id]
                state=recovered.snapshot()
                self.assertEqual(state['status'],'partial_failed')
                self.assertEqual(state['templates'][a]['status'],'failed')
                self.assertEqual(state['templates'][a]['output_artifacts'],[])
                self.assertEqual(state['templates'][a]['error']['code'],'recovery_verification_failed')
                self.assertEqual(state['templates'][a]['unavailable_artifacts'][bad['id']],bad)
                self.assertEqual(state['templates'][b]['status'],'verified')
                self.assertEqual(recovered.read_artifact(good['id']),rewrite_template(raw2,self.plan,'second'))
                self.assertEqual({str(path.relative_to(moved)):path.read_bytes() for path in moved.rglob('*') if path.is_file()},evidence)

    def test_discovery_entry_failures_are_isolated_with_bounded_diagnostics(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        from scripts.tower_reducer import storage
        good=self.create()
        class Entry:
            def __init__(self,name,error=None,name_error=False):
                self._name,self.error,self.name_error=name,error,name_error
            @property
            def name(self):
                if self.name_error: raise PermissionError('name unavailable')
                return self._name
            def stat(self,follow_symlinks=False):
                if self.error: raise self.error
                return good.directory.stat()
        failures=[Entry('job-'+format(i,'032x'), FileNotFoundError('removed') if i==0 else OSError('unreadable')) for i in range(104)]
        entries=[failures[0],Entry('job-'+'f'*32,name_error=True),Entry(good.job_id)]+failures[1:]
        @contextmanager
        def scan(_):
            yield iter(entries)
        fresh=JobRepository(self.repo.root)
        with patch.object(storage.os,'scandir',side_effect=scan):
            self.assertEqual([item.job_id for item in fresh.discover(scan_limit=200)],[good.job_id])
        self.assertLessEqual(len(fresh.diagnostics),100)
        self.assertTrue(any(item['code']=='invalid_job_entry' for item in fresh.diagnostics))
        bounded=JobRepository(self.repo.root)
        with patch.object(storage.os,'scandir',side_effect=scan):
            self.assertEqual(bounded.discover(scan_limit=2),[])
        self.assertEqual(len(bounded.diagnostics),3)
        self.assertTrue(any(item['code']=='scan_truncated' for item in bounded.diagnostics))
