import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts.tests.test_rewrite import synthetic
from scripts.tests.test_storage import HookOps
from scripts.tower_reducer.rewrite import rewrite_template
from scripts.tower_reducer.storage import InputSnapshot, JobRepository
from scripts.tower_reducer.export_service import build_zip, export_outputs, plan_exports


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.raw,self.plan=synthetic()
        self.repo=JobRepository(self.root/'jobs')
        self.job=self.repo.create([InputSnapshot(self.raw,'a_m.bytes',self.root/'inputs')])
        self.key=next(iter(self.job.snapshot()['templates']))
        self.output=rewrite_template(self.raw,self.plan,'new')
        self.artifact=self.job.register_output(self.key,self.output,self.plan,'new')
        self.target=self.root/'out'

    def decide(self,sha):
        return {self.key:{'job_id':self.job.job_id,'relative_name':'new_m.bytes','expected_old_sha':sha,'action':'backup_and_replace'}}

    def test_create_and_same_sha_skip(self):
        report=export_outputs(self.job,self.target,{})
        self.assertEqual(report.rows[0]['status'],'exported')
        self.assertEqual(report.rows[0]['classification'],'create')
        target=self.target/'new_m.bytes'
        self.assertEqual(target.read_bytes(),self.output)
        inode=target.stat().st_ino
        self.assertEqual(export_outputs(self.job,self.target,{}).rows[0]['status'],'skipped')
        self.assertEqual(target.stat().st_ino,inode)

    def test_conflict_requires_bound_confirmation_and_durable_backup(self):
        self.target.mkdir(); target=self.target/'new_m.bytes'; target.write_bytes(b'old')
        sha=hashlib.sha256(b'old').hexdigest()
        self.assertEqual(plan_exports(self.job,self.target).rows[0]['classification'],'conflict')
        for decisions in ({},self.decide('0'*64),{self.key:'replace'}):
            report=export_outputs(self.job,self.target,decisions)
            self.assertEqual(report.rows[0]['status'],'blocked')
            self.assertEqual(target.read_bytes(),b'old')
        report=export_outputs(self.job,self.target,self.decide(sha))
        self.assertEqual(report.rows[0]['status'],'exported')
        self.assertEqual(target.read_bytes(),self.output)
        self.assertEqual((self.job.directory/report.rows[0]['backup']['relative_path']).read_bytes(),b'old')
        self.assertEqual(report.rows[0]['backup']['sha256'],sha)

    def test_sha_bound_skip_is_a_persisted_audit_outcome_without_writing(self):
        self.target.mkdir(); target=self.target/'new_m.bytes'; target.write_bytes(b'old')
        sha=hashlib.sha256(b'old').hexdigest()
        decision={self.key:{'job_id':self.job.job_id,'relative_name':'new_m.bytes',
                            'expected_old_sha':sha,'action':'skip'}}
        report=export_outputs(self.job,self.target,decision)
        self.assertEqual(report.rows[0]['status'],'skipped')
        self.assertEqual(report.rows[0]['classification'],'conflict')
        self.assertEqual(target.read_bytes(),b'old')
        persisted=self.job.snapshot()['exports'][-1]
        self.assertEqual(persisted[0]['status'],'skipped')
        self.assertNotIn('error',persisted[0])

    def test_changed_target_after_plan_is_never_overwritten(self):
        def change(stage,path):
            if stage=='export_before_commit':
                path.write_bytes(b'concurrent')
        self.repo.file_ops=HookOps(change)
        report=export_outputs(self.job,self.target,{})
        self.assertEqual(report.rows[0]['error']['code'],'target_changed')
        self.assertEqual((self.target/'new_m.bytes').read_bytes(),b'concurrent')

    def test_write_fsync_replace_failures_leave_no_partial_formal_file(self):
        for phase in ('write','fsync_file','replace'):
            def fail(stage,path,phase=phase):
                if stage==phase and path.parent==self.target:
                    raise OSError('injected')
            self.repo.file_ops=HookOps(fail)
            with self.subTest(phase=phase):
                result=export_outputs(self.job,self.target,{})
                self.assertEqual(result.rows[0]['status'],'failed')
                self.assertFalse((self.target/'new_m.bytes').exists())
                self.assertFalse(list(self.target.glob('.tmp-*')))

    def test_symlink_directory_readonly_and_special_targets_blocked(self):
        self.target.mkdir()
        for kind in ('symlink','directory','fifo'):
            file=self.target/'new_m.bytes'
            if kind=='symlink': file.symlink_to(self.root/'missing')
            elif kind=='directory': file.mkdir()
            else: os.mkfifo(file)
            with self.subTest(kind=kind):
                self.assertEqual(export_outputs(self.job,self.target,{}).rows[0]['status'],'blocked')
            if kind=='directory': file.rmdir()
            else: file.unlink()
        self.target.chmod(0o500)
        try:
            self.assertEqual(export_outputs(self.job,self.target,{}).rows[0]['status'],'blocked')
        finally:
            self.target.chmod(0o700)
        link=self.root/'link'; link.symlink_to(self.target,target_is_directory=True)
        for target in (link,link/'child',Path('relative')):
            with self.subTest(target=target):
                self.assertEqual(export_outputs(self.job,target,{}).rows[0]['status'],'blocked')

    def test_source_output_recursive_relationships_blocked(self):
        for target in (self.root,self.root/'inputs',self.root/'inputs'/'nested'):
            with self.subTest(target=target):
                self.assertEqual(export_outputs(self.job,target,{}).rows[0]['status'],'blocked')

    def test_zip_only_whitelisted_verified_artifacts(self):
        (self.job.directory/'unregistered.bytes').write_bytes(b'not verified')
        with zipfile.ZipFile(io.BytesIO(build_zip(self.job))) as zipped:
            self.assertEqual(zipped.namelist(),['new_m.bytes'])
            self.assertEqual(zipped.read('new_m.bytes'),self.output)

    def test_one_failed_export_does_not_block_other_template(self):
        raw2=self.raw+b'\x78\x01'
        job=self.repo.create([InputSnapshot(self.raw,'a_m.bytes'),InputSnapshot(raw2,'b_m.bytes')])
        a,b=list(job.snapshot()['templates'])
        job.register_output(a,self.output,self.plan,'new')
        job.register_output(b,rewrite_template(raw2,self.plan,'second'),self.plan,'second')
        self.target.mkdir(); (self.target/'new_m.bytes').mkdir()
        report=export_outputs(job,self.target,{})
        self.assertEqual([r['status'] for r in report.rows],['blocked','exported'])
        self.assertTrue((self.target/'second_m.bytes').is_file())

    def test_preview_does_not_create_missing_directory(self):
        result=plan_exports(self.job,self.target/'deep')
        self.assertEqual(result.rows[0]['classification'],'create')
        self.assertFalse(self.target.exists())

    def test_readback_corruption_cannot_be_published(self):
        for event in ('export_readback','export_before_commit','replace'):
            def corrupt(stage,path,event=event):
                if stage==event and path.parent==self.target:
                    for temp in path.parent.glob('.tmp-*'):
                        temp.write_bytes(self.raw)
            self.repo.file_ops=HookOps(corrupt)
            result=export_outputs(self.job,self.target,{})
            with self.subTest(event=event):
                self.assertEqual(result.rows[0]['status'],'failed')
                self.assertFalse((self.target/'new_m.bytes').exists())

    def test_backup_failure_preserves_old_target(self):
        self.target.mkdir(); file=self.target/'new_m.bytes'; file.write_bytes(b'old')
        sha=hashlib.sha256(b'old').hexdigest()
        def fail(stage,path):
            if stage=='fsync_file' and 'backups' in path.parts:
                raise OSError('backup failed')
        self.repo.file_ops=HookOps(fail)
        result=export_outputs(self.job,self.target,self.decide(sha))
        self.assertEqual(result.rows[0]['status'],'failed')
        self.assertEqual(file.read_bytes(),b'old')

    def test_directory_fsync_failure_reports_complete_applied_bytes(self):
        def fail(stage,path):
            if stage=='fsync_dir' and path==self.target:
                raise OSError('directory fsync failed')
        self.repo.file_ops=HookOps(fail)
        result=export_outputs(self.job,self.target,{})
        self.assertEqual(result.rows[0]['status'],'failed')
        self.assertTrue(result.rows[0]['applied'])
        self.assertEqual((self.target/'new_m.bytes').read_bytes(),self.output)

    def test_zip_excludes_failed_generation_and_rejects_forged_names(self):
        job=self.repo.create([InputSnapshot(self.raw,'bad_m.bytes')])
        key=next(iter(job.snapshot()['templates']))
        job.register_output(key,self.raw,self.plan,'bad')
        with zipfile.ZipFile(io.BytesIO(build_zip(job))) as zipped:
            self.assertEqual(zipped.namelist(),[])
        import json
        from scripts.tower_reducer.storage import atomic_write_json
        original=self.job.snapshot()
        for name in ('../escape','中文','x/y','x\\y','x'*181):
            state=self.job.snapshot()
            record=state['artifacts'][self.artifact['id']]
            record.update(new_id=name,filename=name+'_m.bytes')
            atomic_write_json(self.job.directory/'state.json',state)
            loaded=JobRepository(self.repo.root).get(self.job.job_id)
            with self.subTest(name=name),self.assertRaises(ValueError):
                build_zip(loaded)
        atomic_write_json(self.job.directory/'state.json',original)

    def test_target_symlink_injected_immediately_before_commit_is_not_followed(self):
        outside=self.root/'outside'; outside.write_bytes(b'outside')
        def change(stage,path):
            if stage=='replace' and path.parent==self.target:
                path.symlink_to(outside)
        self.repo.file_ops=HookOps(change)
        result=export_outputs(self.job,self.target,{})
        self.assertEqual(result.rows[0]['status'],'failed')
        self.assertEqual(outside.read_bytes(),b'outside')

    def test_directory_swap_has_structured_failure_and_never_cleans_external_files(self):
        outside=self.root/'outside'; outside.mkdir()
        def swap(stage,path):
            if stage=='export_before_commit':
                self.target.rename(self.root/'old-target')
                self.target.symlink_to(outside,target_is_directory=True)
        self.repo.file_ops=HookOps(swap)
        result=export_outputs(self.job,self.target,{})
        self.assertEqual(result.rows[0]['status'],'failed')
        self.assertFalse(list(outside.iterdir()))

    def test_source_parent_changed_to_symlink_is_rejected(self):
        self.target.mkdir()
        (self.root/'inputs').symlink_to(self.target,target_is_directory=True)
        result=export_outputs(self.job,self.target,{})
        self.assertEqual(result.rows[0]['status'],'blocked')
        self.assertFalse((self.target/'new_m.bytes').exists())

    def test_corrupted_backup_never_authorizes_replacement(self):
        self.target.mkdir(); target=self.target/'new_m.bytes'; target.write_bytes(b'old')
        def corrupt(stage,path):
            if stage=='replace' and path.name=='original.bytes':
                for temp in path.parent.glob('.tmp-*'):
                    temp.chmod(0o600); temp.write_bytes(b'corrupt')
        self.repo.file_ops=HookOps(corrupt)
        result=export_outputs(self.job,self.target,self.decide(hashlib.sha256(b'old').hexdigest()))
        self.assertEqual(result.rows[0]['status'],'failed')
        self.assertEqual(target.read_bytes(),b'old')

    def test_conflict_target_changed_after_backup_preserves_concurrent_content(self):
        self.target.mkdir(); target=self.target/'new_m.bytes'; target.write_bytes(b'old')
        def change(stage,path):
            if stage=='export_before_commit': path.write_bytes(b'concurrent')
        self.repo.file_ops=HookOps(change)
        result=export_outputs(self.job,self.target,self.decide(hashlib.sha256(b'old').hexdigest()))
        self.assertEqual(result.rows[0]['error']['code'],'target_changed')
        self.assertEqual(target.read_bytes(),b'concurrent')
        self.assertEqual((self.job.directory/result.rows[0]['backup']['relative_path']).read_bytes(),b'old')

    def test_malformed_typed_plan_blocks_only_its_template(self):
        from scripts.tower_reducer.storage import atomic_write_json
        raw2=self.raw+b'\x78\x01'
        job=self.repo.create([InputSnapshot(self.raw,'a_m.bytes'),InputSnapshot(raw2,'b_m.bytes')])
        a,b=job.snapshot()['templates']
        bad=job.register_output(a,self.output,self.plan,'new')
        job.register_output(b,rewrite_template(raw2,self.plan,'second'),self.plan,'second')
        original=job.snapshot()
        invalid=[{'decimal':'invalid'}, {'decimal':[]}, {'tuple':'abc'}, {'mapping':None},
                 {'record':[],'fields':{}}, {'record':'RewritePlan','fields':['distribution','placement','distribution_options','placement_options']},
                 {'bytes_hex':3}, {'mapping':[[{'tuple':[1]},2]]}, {'unknown':1}]
        for value in invalid:
            from copy import deepcopy
            state=deepcopy(original); state['artifacts'][bad['id']]['plan']=value
            atomic_write_json(job.directory/'state.json',state)
            loaded=JobRepository(self.repo.root).get(job.job_id)
            rows={row['template_key']:row for row in plan_exports(loaded,self.target).rows}
            with self.subTest(value=value):
                self.assertEqual(rows[a]['classification'],'blocked')
                self.assertEqual(rows[b]['classification'],'create')

    def test_containment_uses_physical_ancestry_for_case_insensitive_aliases(self):
        from unittest.mock import patch
        from scripts.tower_reducer import storage
        source=self.root/'Inputs'; source.mkdir()
        job=self.repo.create([InputSnapshot(self.raw,'input_m.bytes',source)])
        key=next(iter(job.snapshot()['templates']))
        job.register_output(key,self.output,self.plan,'new')
        real_stat=storage.os.stat; real_open=storage.os.open
        # Deterministic filesystem adapter: only this existing component is
        # case-insensitive; actual dev/ino comes from the real Inputs directory.
        def folded(name): return 'Inputs' if name=='inputs' else name
        def case_stat(name,*args,**kwargs): return real_stat(folded(name),*args,**kwargs)
        def case_open(name,*args,**kwargs): return real_open(folded(name),*args,**kwargs)
        with patch.object(storage.os,'stat',side_effect=case_stat), patch.object(storage.os,'open',side_effect=case_open):
            for target in (self.root/'inputs', self.root/'inputs'/'nested'):
                with self.subTest(target=target):
                    result=plan_exports(job,target)
                    self.assertEqual(result.rows[0]['classification'],'blocked')
                    self.assertEqual(result.rows[0]['error']['code'],'invalid_target_directory')
                    self.assertIn('contain',result.rows[0]['error']['message'])
        self.assertFalse((source/'nested').exists())
        nested=source/'child'; nested.mkdir()
        descendant=self.repo.create([InputSnapshot(self.raw,'nested_m.bytes',nested)])
        child_key=next(iter(descendant.snapshot()['templates']))
        descendant.register_output(child_key,self.output,self.plan,'new')
        with patch.object(storage.os,'stat',side_effect=case_stat), patch.object(storage.os,'open',side_effect=case_open):
            report=plan_exports(descendant,self.root/'inputs')
            self.assertEqual(report.rows[0]['classification'],'blocked')
            self.assertIn('contain',report.rows[0]['error']['message'])

    def test_containment_distinguishes_missing_siblings_on_case_sensitive_volume(self):
        result=plan_exports(self.job,self.root/'other-output'/'nested')
        self.assertEqual(result.rows[0]['classification'],'create')

    def test_missing_tails_inherit_proven_case_insensitive_parent_semantics(self):
        from unittest.mock import patch
        from scripts.tower_reducer import storage
        parent=self.root/'Inputs'; parent.mkdir()
        job=self.repo.create([InputSnapshot(self.raw,'input_m.bytes',parent/'Missing')])
        key=next(iter(job.snapshot()['templates']))
        job.register_output(key,self.output,self.plan,'new')
        real_stat=storage.os.stat; real_open=storage.os.open
        def folded(name):
            return 'Inputs' if isinstance(name,str) and name.casefold()=='inputs' else name
        def case_stat(name,*args,**kwargs): return real_stat(folded(name),*args,**kwargs)
        def case_open(name,*args,**kwargs): return real_open(folded(name),*args,**kwargs)
        with patch.object(storage.os,'stat',side_effect=case_stat), patch.object(storage.os,'open',side_effect=case_open):
            result=plan_exports(job,parent/'missing'/'nested')
            self.assertEqual(result.rows[0]['classification'],'blocked')
            self.assertIn('contain',result.rows[0]['error']['message'])
