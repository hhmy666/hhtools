"""Real HTTP rerun scheduling, coordinated at the expensive engine boundary."""

import threading

from scripts.tests.project_fixtures import read_project_fixture
from scripts.tests.test_api import ApiContractCase
class RerunQueueTests(ApiContractCase):
    def completed_batch(self):
        imported = self.import_files([
            (name, read_project_fixture(name))
            for name in ('mxd0057v1_m.bytes', 'hr0029_m.bytes', 'hr0044_m.bytes')
        ])
        job_id = self.start_job(imported['import_id'])
        return job_id, self.wait_job(job_id)

    def rerun_row(self, job_id, key, **changes):
        return self.request('POST', '/api/jobs/{}/rerun/{}'.format(job_id, key),
                            json_body={'defaults': {'ratio': .25, **changes}})

    def test_different_rows_queue_on_one_worker_and_keep_attempts_and_unselected_output(self):
        _, api = self._modules()
        application = self.start_server(limits=api.ApiLimits(active_jobs=1))
        job_id, completed = self.completed_batch()
        keys = tuple(completed['templates'])
        before = self.repository.get(job_id).snapshot()
        export = self.request('POST', '/api/jobs/{}/export'.format(job_id),
                              json_body={'action': 'preview'})[2]
        entered = threading.Event()
        second_entered = threading.Event()
        release = threading.Event()
        release_second = threading.Event()
        self.addCleanup(release.set)
        self.addCleanup(release_second.set)
        original = application._run_template
        calls = []

        def blocked(job, key):
            calls.append((key, threading.get_ident()))
            if key == keys[0]:
                entered.set()
                self.assertTrue(release.wait(8))
            elif key == keys[1]:
                second_entered.set()
                self.assertTrue(release_second.wait(8))
            return original(job, key)

        application._run_template = blocked
        try:
            first = self.rerun_row(job_id, keys[0], seed=13, new_id='queue_first')
            self.assertEqual(first[0], 202, first[3])
            self.assertTrue(entered.wait(3))
            second = self.rerun_row(job_id, keys[1], seed=29, new_id='queue_second')
            self.assertEqual(second[0], 202, second[3])
            pending = self.request('GET', '/api/jobs/' + job_id)[2]
            self.assertEqual(pending['templates'][keys[1]]['status'], 'queued')
            self.assertEqual(pending['templates'][keys[1]]['attempt_id'], 1)
            self.assertEqual(pending['templates'][keys[1]]['parameters']['seed'], 29)
            self.assertEqual(pending['templates'][keys[1]]['artifacts'], [])
            self.assertEqual(application.bootstrap()['resources']['active_jobs'], 1)
            self.assertFalse(second_entered.is_set())
            self.assert_error(self.rerun_row(job_id, keys[0], seed=99), 409, 'template_busy')
            self.assert_error(self.rerun_row(job_id, keys[1], seed=99), 409, 'template_busy')
            old_artifact = before['templates'][keys[1]]['output_artifacts'][0]
            self.assertNotEqual(self.request('GET', '/api/jobs/{}/download/{}'.format(
                job_id, old_artifact))[0], 200)
            stale = self.request('POST', '/api/jobs/{}/export'.format(job_id), json_body={
                'action': 'commit', 'plan_id': export['plan_id'],
                'confirmation_token': export['confirmation_token'], 'conflict_decisions': {},
            })
            self.assert_error(stale, 409, 'plan_stale')
            release.set()
            self.assertTrue(second_entered.wait(5))
            midway = self.request('GET', '/api/jobs/' + job_id)[2]
            self.assertEqual(midway['status'], 'running')
            self.assertEqual(midway['progress'], {'completed_templates': 2, 'total_templates': 3})
            release_second.set()
            final = self.wait_job(job_id)
        finally:
            release.set()
            release_second.set()
        after = self.repository.get(job_id).snapshot()
        self.assertEqual(after['templates'][keys[2]], before['templates'][keys[2]])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len({thread_id for _, thread_id in calls}), 1)
        for index, seed in ((0, 13), (1, 29)):
            row = after['templates'][keys[index]]
            self.assertEqual(row['attempt_id'], 1)
            self.assertEqual(row['parameters']['seed'], seed)
            self.assertEqual(len(row['attempts']), 1)
            self.assertEqual(row['attempts'][0]['output_artifacts'],
                             before['templates'][keys[index]]['output_artifacts'])
            self.assertEqual(row['status'], 'verified')
            artifact = after['artifacts'][row['output_artifacts'][0]]
            self.assertEqual(artifact['attempt_id'], 1)
            self.assertEqual(artifact['new_id'], 'queue_first' if index == 0 else 'queue_second')
        self.assertEqual(final['progress'], {'completed_templates': 3, 'total_templates': 3})

    def test_simultaneous_requests_for_same_row_create_one_attempt(self):
        application = self.start_server()
        job_id, completed = self.completed_batch()
        key = next(iter(completed['templates']))
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        original = application._run_template

        def blocked(job, template_key):
            entered.set()
            release.wait(8)
            return original(job, template_key)

        application._run_template = blocked
        barrier = threading.Barrier(3)
        results = []

        def submit():
            barrier.wait(3)
            results.append(self.rerun_row(job_id, key))

        threads = [threading.Thread(target=submit) for _ in range(2)]
        try:
            for thread in threads:
                thread.start()
            barrier.wait(3)
            for thread in threads:
                thread.join(4)
            self.assertEqual(sorted(result[0] for result in results), [202, 409])
            self.assert_error(next(result for result in results if result[0] == 409),
                              409, 'template_busy')
            self.assertTrue(entered.wait(3))
            row = self.repository.get(job_id).snapshot()['templates'][key]
            self.assertEqual(row['attempt_id'], 1)
            self.assertEqual(len(row['attempts']), 1)
        finally:
            release.set()
            for thread in threads:
                thread.join(4)
            self.wait_job(job_id)

    def test_completed_row_can_queue_during_initial_batch_without_losing_duplicate_guard(self):
        application = self.start_server()
        imported = self.import_files([
            (name, read_project_fixture(name))
            for name in ('mxd0057v1_m.bytes', 'hr0029_m.bytes')
        ])
        initial_blocked = threading.Event()
        rerun_blocked = threading.Event()
        release_initial = threading.Event()
        release_rerun = threading.Event()
        self.addCleanup(release_initial.set)
        self.addCleanup(release_rerun.set)
        original = application._run_template
        calls = []

        def blocked(job, key):
            calls.append(key)
            if len(calls) == 2:
                initial_blocked.set()
                release_initial.wait(8)
            elif len(calls) == 3:
                rerun_blocked.set()
                release_rerun.wait(8)
            return original(job, key)

        application._run_template = blocked
        job_id = self.start_job(imported['import_id'])
        try:
            self.assertTrue(initial_blocked.wait(5))
            keys = tuple(self.repository.get(job_id).snapshot()['templates'])
            self.assertEqual(self.rerun_row(job_id, keys[0], seed=37)[0], 202)
            self.assert_error(self.rerun_row(job_id, keys[1]), 409, 'template_busy')
            release_initial.set()
            self.assertTrue(rerun_blocked.wait(5))
            self.assert_error(self.rerun_row(job_id, keys[0]), 409, 'template_busy')
        finally:
            release_initial.set()
            release_rerun.set()
            final = self.wait_job(job_id)
        self.assertEqual(len(calls), 3)
        self.assertEqual(final['templates'][keys[0]]['attempt_id'], 1)
