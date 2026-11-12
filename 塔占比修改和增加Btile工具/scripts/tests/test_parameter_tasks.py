"""Background parameter work uses temporary fixtures and never writes real CSVs."""
import threading
import time
import json
from unittest.mock import patch

from scripts.tests.test_parameter_flow_api import ParameterFlowApiTests
from scripts.tests.test_rewrite import synthetic
from scripts.tower_reducer.parameter_flow import ParameterFlow


class ParameterTaskTests(ParameterFlowApiTests):
    def start_task(self, action, payload, request_id='parameter-test-0001'):
        response = self.request('POST', '/api/parameters/start', json_body={
            'request_id': request_id, 'action': action, 'payload': payload})
        self.assertEqual(response[0], 200, response[3])
        return response[2]

    def task_done(self, task):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            response = self.request('GET', '/api/parameters/tasks/' + task['task_id'])
            self.assertEqual(response[0], 200, response[3])
            if response[2]['status'] != 'running':
                return response[2]
            time.sleep(.01)
        self.fail('parameter task did not finish')

    def test_calculate_prepares_source_in_background_and_preserves_config(self):
        self.start_server()
        root, before = self.config()
        job_id = self.verified_job()
        task = self.start_task('calculate', {'source': {'mode': 'job', 'job_id': job_id}})
        done = self.task_done(task)
        self.assertEqual(done['status'], 'succeeded', done)
        self.assertEqual(done['selection']['entries'][0]['new_template_id'], 'lessTsample')
        self.assertEqual(done['result']['rows'][0]['new_parameters'][0], '3|0|100|2')
        self.assertEqual((root / 'config_level.csv').read_bytes(), before)
        self.assertEqual(done['phase'], 'completed')

    def test_whole_calculation_progress_does_not_finish_when_source_or_verification_finishes(self):
        app = self.start_server()
        self.config()
        job_id = self.verified_job()
        reached, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = app.parameter_flow.service.calculate
        def delayed_calculation(*args, **kwargs):
            reached.set()
            release.wait(3)
            return original(*args, **kwargs)
        with patch.object(app.parameter_flow.service, 'calculate', side_effect=delayed_calculation):
            task = self.start_task('calculate', {'source': {'mode': 'job', 'job_id': job_id}})
            self.assertTrue(reached.wait(1))
            running = self.request('GET', '/api/parameters/tasks/' + task['task_id'])[2]
            self.assertEqual(running['phase'], 'calculating')
            progress = running.get('overall_progress', {})
            self.assertGreater(progress.get('completed', 0), 0)
            self.assertLess(progress['completed'], progress['total'])
            self.assertEqual(progress.get('stage_count'), 3)
            release.set()
            done = self.task_done(task)
        self.assertEqual(done['overall_progress']['completed'], done['overall_progress']['total'])
        saved = json.loads((app.parameter_flow.task_root / (task['task_id'] + '.json')).read_text())
        self.assertEqual(saved['task']['overall_progress'], done['overall_progress'])

    def test_failed_calculation_keeps_partial_overall_progress_in_recovery_record(self):
        app = self.start_server()
        self.config()
        job_id = self.verified_job()
        with patch.object(app.parameter_flow.service, 'calculate', side_effect=ValueError('invalid fixture')):
            done = self.task_done(self.start_task('calculate', {'source': {'mode': 'job', 'job_id': job_id}}))
        self.assertEqual(done['status'], 'failed')
        progress = done.get('overall_progress', {})
        self.assertGreater(progress.get('completed', 0), 0)
        self.assertLess(progress['completed'], progress['total'])
        app.parameter_flow = ParameterFlow(app)
        self.assertEqual(app.parameter_flow.task_status(done['task_id'])['overall_progress'], progress)

    def test_overall_progress_never_rewinds_when_local_phase_counters_restart(self):
        app = self.start_server()
        self.config()
        job_id = self.verified_job()
        snapshots = []
        original = app.parameter_flow.resolve
        def observe_source(payload, progress=None):
            def observe(**update):
                progress(**update)
                current = next(iter(app.parameter_flow.tasks.values()))['task']
                snapshots.append(dict(current.get('overall_progress', {})))
            result = original(payload, progress=observe)
            # A renamed-artifact lookup can revisit an index inside resolving.
            observe(phase='indexing', completed=0, total=635)
            observe(phase='resolving', completed=0, total=1)
            observe(completed=1)
            return result
        with patch.object(app.parameter_flow, 'resolve', side_effect=observe_source):
            done = self.task_done(self.start_task('calculate', {'source': {'mode': 'job', 'job_id': job_id}}))
        values = [row.get('completed', -1) for row in snapshots]
        self.assertGreater(max(values), 0)
        self.assertEqual(values, sorted(values))
        self.assertTrue(all(row['completed'] < row['total'] for row in snapshots))
        self.assertEqual(done['overall_progress']['completed'], done['overall_progress']['total'])

    def test_calculating_an_existing_selection_does_not_count_source_lookup_again(self):
        self.start_server()
        self.config()
        selected = self.task_done(self.start_task('resolve', {'mode': 'job', 'job_id': self.verified_job()}))
        done = self.task_done(self.start_task('calculate', {'selection_id': selected['result']['selection_id']}, 'parameter-selected-0001'))
        self.assertEqual(done['status'], 'succeeded', done)
        self.assertEqual(done.get('overall_progress', {}).get('stage_count'), 2)

    def test_slow_worker_never_blocks_start_status_or_duplicate_request(self):
        app = self.start_server()
        self.config()
        job_id = self.verified_job()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = app.parameter_flow._entry
        def slow(*args, **kwargs):
            entered.set()
            release.wait(3)
            return original(*args, **kwargs)
        with patch.object(app.parameter_flow, '_entry', side_effect=slow):
            started = time.monotonic()
            task = self.start_task('calculate', {'source': {'mode': 'job', 'job_id': job_id}})
            self.assertLess(time.monotonic() - started, .5)
            self.assertTrue(entered.wait(1))
            duplicate = self.start_task('calculate', {'source': {'mode': 'job', 'job_id': job_id}})
            self.assertEqual(duplicate['task_id'], task['task_id'])
            self.assertEqual(duplicate['status'], 'running')
            status = self.request('GET', '/api/parameters/tasks/' + task['task_id'])[2]
            self.assertEqual(status['phase'], 'resolving')
            self.assertEqual(status['total'], 1)
            self.assertEqual(status['completed'], 0)
            changed = self.request('POST', '/api/parameters/start', json_body={
                'request_id': 'parameter-test-0001', 'action': 'calculate',
                'payload': {'source': {'mode': 'ids', 'template_ids': ['other']}}})
            self.assert_error(changed, 409, 'parameter_request_changed')
            release.set()
            self.assertEqual(self.task_done(task)['status'], 'succeeded')

    def test_parameter_upload_only_stages_bytes_and_invalid_parse_is_per_file(self):
        app = self.start_server()
        with patch('scripts.tower_reducer.api.inspect_template', side_effect=AssertionError('analysis on upload')):
            imported = app.import_files([('sample_m.bytes', synthetic()[0]), ('broken_m.bytes', b'bad')], _inspect=False)
        self.config()
        task = self.start_task('calculate', {'source': {'mode': 'files', 'import_id': imported['import_id']}})
        done = self.task_done(task)
        self.assertEqual(done['status'], 'succeeded', done)
        self.assertEqual(len(done['selection']['entries']), 2)
        self.assertEqual(done['selection']['entries'][1]['status'], 'failed')

    def test_restart_recovers_an_uncertain_commit_using_the_same_plan(self):
        app = self.start_server()
        root, before = self.config()
        calc = self.task_done(self.start_task('calculate', {'source': {'mode': 'job', 'job_id': self.verified_job()}}))
        rows = calc['result']['rows']
        preview = self.task_done(self.start_task('preview', {'calculation_id': calc['result']['calculation_id'],
            'selected_row_ids': [rows[0]['row_id']]}, 'parameter-preview-0001'))
        commit_body = {'plan_id': preview['result']['plan_id']}
        commit = self.task_done(self.start_task('commit', commit_body, 'parameter-commit-0001'))
        updated = (root / 'config_level.csv').read_bytes()
        self.assertNotEqual(updated, before)
        journal = app.parameter_flow.task_root / (commit['task_id'] + '.json')
        saved = json.loads(journal.read_text())
        saved['task'].update(status='running', phase='committing', result=None)
        journal.write_text(json.dumps(saved))
        app.parameter_flow = ParameterFlow(app)
        recovered = self.task_done(commit)
        self.assertEqual(recovered['status'], 'succeeded', recovered)
        self.assertEqual(recovered['result']['status'], 'committed')
        self.assertEqual(recovered['overall_progress']['completed'], recovered['overall_progress']['total'])
        self.assertEqual(recovered['overall_progress']['stage_count'], 2)
        self.assertEqual((root / 'config_level.csv').read_bytes(), updated)

    def test_corrupt_recovery_journals_cannot_register_invalid_task_ids(self):
        app = self.start_server()
        path = app.parameter_flow.task_root / ('parameter-task-' + 'f' * 32 + '.json')
        path.write_text(json.dumps({'task': {'task_id': '../../outside', 'status': 'failed'}}))
        app.parameter_flow = ParameterFlow(app)
        self.assertNotIn('../../outside', app.parameter_flow.tasks)
        task = self.start_task('resolve', {'mode': 'ids', 'template_ids': ['missing']})
        self.assertEqual(self.task_done(task)['status'], 'succeeded')
