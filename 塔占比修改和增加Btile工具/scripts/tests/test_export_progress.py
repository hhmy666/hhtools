"""Real export progress remains observable while a durable write is blocked."""
import threading
import time
import json
from unittest.mock import patch

from scripts.tests.test_api import ApiContractCase
from scripts.tests.test_rewrite import synthetic
from scripts.tests.test_storage import HookOps
from scripts.tower_reducer.rewrite import rewrite_template
from scripts.tower_reducer.storage import InputSnapshot


class ExportProgressTests(ApiContractCase):
    def prepare(self, count=2):
        self.application = self.start_server()
        raw, plan = synthetic()
        sources = [raw + b'\x78' + bytes([index]) for index in range(count)]
        job = self.repository.create([InputSnapshot(value, f'{index}_m.bytes')
                                      for index, value in enumerate(sources)])
        for index, key in enumerate(job.snapshot()['templates']):
            name = f'output{index}'
            job.register_output(key, rewrite_template(sources[index], plan, name), plan, name)
        self.job = job
        self.path = f'/api/jobs/{job.job_id}/export'
        self.plan = self.request('POST', self.path, json_body={'action': 'preview'})[2]
        self.payload = {'action': 'start', 'plan_id': self.plan['plan_id'],
                        'confirmation_token': self.plan['confirmation_token'], 'conflict_decisions': {}}

    def status(self, task_id):
        response = self.request('POST', self.path, json_body={'action': 'status', 'task_id': task_id})
        self.assertEqual(response[0], 200, response[3])
        return response[2]

    def finish(self, task_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.status(task_id)
            if result['status'] != 'running':
                return result
            time.sleep(.01)
        self.fail('export task did not finish')

    def test_progress_counts_real_completed_files_and_duplicate_click_cannot_write_again(self):
        self.prepare()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def block_second_commit(stage, path):
            if stage == 'export_before_commit' and path.name == 'output1_m.bytes':
                entered.set()
                release.wait(5)
        self.repository.file_ops = HookOps(block_second_commit)
        started = self.request('POST', self.path, json_body=self.payload)
        self.assertEqual(started[0], 200, started[3])
        self.assertTrue(entered.wait(3))
        task_id = started[2]['task_id']
        first = self.status(task_id)
        self.assertEqual((first['completed'], first['total'], first['phase']), (1, 2, 'writing'))
        self.assertTrue((self.export_root / 'output0_m.bytes').exists())
        self.assertFalse((self.export_root / 'output1_m.bytes').exists())
        self.assertEqual(self.status(task_id)['completed'], 1, 'waiting does not invent progress')
        duplicate = self.request('POST', self.path, json_body=self.payload)
        self.assertEqual(duplicate[0], 200, duplicate[3])
        self.assertEqual(duplicate[2]['task_id'], task_id)
        self.assert_error(self.request('POST', self.path,
                                      json_body=dict(self.payload, action='commit')), 409, 'plan_expired')
        release.set()
        done = self.finish(task_id)
        self.assertEqual((done['status'], done['completed'], done['total']), ('succeeded', 2, 2))
        self.assertTrue(done['result']['ok'])
        self.assertIsNone(done['error'])
        self.assertEqual(sorted(path.name for path in self.export_root.iterdir()),
                         ['output0_m.bytes', 'output1_m.bytes'])
        recovered = self.request('POST', self.path, json_body=self.payload)
        self.assertEqual(recovered[0], 200, recovered[3])
        self.assertEqual(recovered[2]['task_id'], task_id)
        self.assertEqual(recovered[2]['status'], 'succeeded')
        self.assertNotIn(self.payload['confirmation_token'], json.dumps(recovered[2]))
        self.assertNotIn('token_hash', recovered[2])
        self.assert_error(self.request('POST', self.path, json_body=dict(
            self.payload, confirmation_token='forged')), 409, 'plan_expired')
        self.assert_error(self.request('POST', self.path, json_body=dict(
            self.payload, conflict_decisions={'extra': 'skip'})), 409, 'plan_expired')

    def test_target_changed_after_preview_fails_without_writing_another_version(self):
        self.prepare(1)
        (self.export_root / 'output0_m.bytes').write_bytes(b'concurrent owner')
        started = self.request('POST', self.path, json_body=self.payload)
        self.assertEqual(started[0], 200, started[3])
        done = self.finish(started[2]['task_id'])
        self.assertEqual(done['status'], 'failed')
        self.assertEqual(done['error']['code'], 'target_changed')
        self.assertEqual(done['completed'], 0)
        self.assertEqual((self.export_root / 'output0_m.bytes').read_bytes(), b'concurrent owner')
        self.assertFalse((self.export_root / 'output0v2_m.bytes').exists())

    def test_io_failure_is_terminal_and_keeps_actual_partial_outcomes(self):
        self.prepare(1)
        def fail(stage, path):
            if stage == 'export_before_commit':
                raise OSError('private storage detail')
        self.repository.file_ops = HookOps(fail)
        started = self.request('POST', self.path, json_body=self.payload)
        self.assertEqual(started[0], 200, started[3])
        done = self.finish(started[2]['task_id'])
        self.assertEqual((done['status'], done['completed'], done['total']), ('failed', 1, 1))
        self.assertFalse(done['result']['ok'])
        self.assertEqual(done['result']['items'][0]['status'], 'failed')
        self.assertNotIn('private storage detail', str(done))
        self.assertFalse(list(self.export_root.iterdir()))

    def test_forged_confirmation_cannot_launch_a_writer_and_status_is_job_scoped(self):
        self.prepare(1)
        self.assert_error(self.request('POST', self.path, json_body=dict(
            self.payload, confirmation_token='forged')), 409, 'plan_expired')
        self.assertFalse(list(self.export_root.iterdir()))
        started = self.request('POST', self.path, json_body=self.payload)
        self.assertEqual(started[0], 200, started[3])
        done = self.finish(started[2]['task_id'])
        self.assertEqual(done['status'], 'succeeded')
        wrong_job_path = '/api/jobs/job-' + '0' * 32 + '/export'
        self.assert_error(self.request('POST', wrong_job_path, json_body={
            'action': 'status', 'task_id': started[2]['task_id']}), 404, 'export_task_not_found')
        self.assert_error(self.request('POST', self.path, json_body={
            'action': 'status', 'task_id': 'unknown'}), 404, 'export_task_not_found')

    def block_second_verification(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.job.read_artifact
        second = self.job.artifact_records()[1]['id']
        def read(artifact_id):
            if artifact_id == second:
                entered.set()
                release.wait(5)
            return original(artifact_id)
        blocker = patch.object(self.job, 'read_artifact', side_effect=read)
        blocker.start()
        self.addCleanup(blocker.stop)
        return entered, release

    def test_preview_returns_before_verification_and_reports_real_items_without_writing(self):
        self.prepare()
        before = self.job.snapshot()
        entered, release = self.block_second_verification()
        request = {'action': 'preview_start', 'request_id': 'preview-request-one'}
        started = self.request('POST', self.path, json_body=request)
        self.assertEqual(started[0], 200, started[3])
        self.assertTrue(entered.wait(3))
        snapshot = self.status(started[2]['task_id'])
        self.assertEqual((snapshot['kind'], snapshot['phase'], snapshot['verified'],
                          snapshot['verification_total'], snapshot['completed']),
                         ('preview', 'verifying', 1, 2, 0))
        self.assertEqual(snapshot['current_filename'], 'output1_m.bytes')
        duplicate = self.request('POST', self.path, json_body=request)
        self.assertEqual(duplicate[0], 200, duplicate[3])
        self.assertEqual(duplicate[2]['task_id'], snapshot['task_id'])
        self.assertEqual(self.status(snapshot['task_id'])['verified'], 1)
        self.assertFalse(list(self.export_root.iterdir()))
        release.set()
        done = self.finish(snapshot['task_id'])
        self.assertEqual((done['status'], done['verified']), ('succeeded', 2))
        self.assertEqual(len(done['result']['items']), 2)
        self.assertTrue(done['result']['confirmation_token'])
        self.assertEqual(self.job.snapshot(), before, 'preview only creates an in-memory confirmation')
        recovered = self.request('POST', self.path, json_body=request)
        self.assertEqual(recovered[2]['task_id'], snapshot['task_id'])
        self.assertEqual(recovered[2]['result']['plan_id'], done['result']['plan_id'])
        self.assert_error(self.request('POST', self.path, json_body=dict(
            request, export_root=str(self.root / 'different'))), 409, 'preview_request_changed')

    def test_write_preflight_reports_each_verified_item_before_any_output_is_written(self):
        self.prepare()
        entered, release = self.block_second_verification()
        started = self.request('POST', self.path, json_body=self.payload)
        self.assertEqual(started[0], 200, started[3])
        self.assertTrue(entered.wait(3))
        current = self.status(started[2]['task_id'])
        self.assertEqual((current['phase'], current['verified'], current['verification_total'],
                          current['completed'], current['total']), ('preflight', 1, 2, 0, 2))
        self.assertFalse(list(self.export_root.iterdir()))
        release.set()
        self.assertEqual(self.finish(current['task_id'])['status'], 'succeeded')

    def test_async_preview_failure_is_terminal_and_sanitized(self):
        self.prepare(1)
        started = self.request('POST', self.path, json_body={
            'action': 'preview_start', 'request_id': 'invalid-target-one', 'export_root': 'relative'})
        self.assertEqual(started[0], 200, started[3])
        done = self.finish(started[2]['task_id'])
        self.assertEqual((done['status'], done['error']['code']), ('failed', 'invalid_export_root'))
        self.assertIsNone(done['result'])
        self.assertFalse(list(self.export_root.iterdir()))

    def test_async_preview_selection_reports_the_requested_scope(self):
        self.prepare()
        key = list(self.job.snapshot()['templates'])[1]
        started = self.request('POST', self.path, json_body={
            'action': 'preview_start', 'request_id': 'selected-preview', 'template_keys': [key]})
        self.assertEqual(started[0], 200, started[3])
        done = self.finish(started[2]['task_id'])
        self.assertEqual((done['verified'], done['verification_total']), (1, 1))
        self.assertEqual([row['template_key'] for row in done['result']['items']], [key])
