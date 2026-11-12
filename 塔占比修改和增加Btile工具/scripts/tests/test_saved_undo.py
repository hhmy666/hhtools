"""Saved micro edits undo only their current verified direct predecessor."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.tests import test_translation
from scripts.tower_reducer.api import ApiError
from scripts.tower_reducer.storage import JobRepository


class SavedUndoTests(unittest.TestCase):
    setUp = test_translation.TranslationTests.setUp
    prepare = test_translation.TranslationTests.prepare

    def undo(self, job, key, artifact):
        self.assertTrue(callable(getattr(self.app, 'undo_template', None)), 'saved undo API is missing')
        return self.app.undo_template(job.job_id, key, {'artifact_id': artifact['id']})

    def test_saved_layer_deletion_rotation_translation_undo_in_order_after_restart(self):
        job, key, original, baseline = self.prepare()
        self.assertFalse(self.app.template_preview(job.job_id, key)['can_undo'])
        moved = self.app.translate_template(job.job_id, key, dict(artifact_id=original['id'], dx=1, dy=0))
        moved_raw = job.read_artifact(moved['artifact_id'])
        rotated = self.app.rotate_template(job.job_id, key, dict(artifact_id=moved['artifact_id'], direction='cw'))
        rotated_raw = job.read_artifact(rotated['artifact_id'])
        deleted = self.app.edit_template(job.job_id, key, dict(artifact_id=rotated['artifact_id'],
            deleted_indices=[6, 7, 8], additions=[]))
        self.assertEqual(len(deleted['output']['tiles']), 6)
        self.assertTrue(deleted['can_undo'])
        self.app.repository = JobRepository(self.repo.root)
        current = deleted['artifact']
        for expected in (rotated_raw, moved_raw, baseline):
            previous_id = current['id']
            result = self.undo(job, key, current)
            current = result['artifact']
            self.assertNotIn(current['id'], (original['id'], moved['artifact_id'], rotated['artifact_id'], deleted['artifact_id']))
            self.assertEqual(job.read_artifact(current['id']), expected)
            with self.assertRaises(KeyError): job.read_artifact(previous_id)
        self.assertFalse(result['can_undo'])
        summary = job.snapshot()['templates'][key]['plan_summary']
        self.assertEqual(summary['custom'], 'preserved')
        self.assertNotIn('manual_edit', summary)
        before = job.snapshot()
        with self.assertRaises(ApiError) as rejected: self.undo(job, key, current)
        self.assertEqual(rejected.exception.code, 'undo_unavailable')
        self.assertEqual(job.snapshot(), before)

    def test_saved_additions_restore_exact_previous_bytes(self):
        job, key, original, baseline = self.prepare()
        saved = self.app.edit_template(job.job_id, key, dict(artifact_id=original['id'], deleted_indices=[],
            additions=[dict(x=x, y=12, z=0, tile_id=-11) for x in (4, 8, 12)]))
        self.assertEqual(len(saved['output']['tiles']), 12)
        restored = self.undo(job, key, saved['artifact'])
        self.assertEqual(job.read_artifact(restored['artifact_id']), baseline)

    def test_stale_identity_and_client_selected_history_cannot_activate_artifacts(self):
        job, key, original, _ = self.prepare()
        moved = self.app.translate_template(job.job_id, key, dict(artifact_id=original['id'], dx=1, dy=0))
        before = job.snapshot()
        with self.assertRaises(ApiError) as rejected: self.undo(job, key, original)
        self.assertEqual(rejected.exception.code, 'stale_artifact')
        with self.assertRaises(ApiError):
            self.app.undo_template(job.job_id, key, dict(artifact_id=moved['artifact_id'], previous_artifact_id=original['id']))
        self.assertEqual(job.snapshot(), before)

    def test_corrupt_predecessor_and_failed_registration_preserve_current_capability(self):
        job, key, original, _ = self.prepare()
        moved = self.app.translate_template(job.job_id, key, dict(artifact_id=original['id'], dx=1, dy=0))
        current_bytes = job.read_artifact(moved['artifact_id'])
        before = job.snapshot()
        def reject(stage, path):
            if stage == 'registration_readback': raise OSError('simulated disk failure')
        with patch.object(self.repo.file_ops, 'checkpoint', side_effect=reject), self.assertRaises(ApiError):
            self.undo(job, key, moved['artifact'])
        self.assertEqual(job.snapshot()['templates'][key], before['templates'][key])
        self.assertEqual(job.read_artifact(moved['artifact_id']), current_bytes)
        predecessor = job.directory / before['history_artifacts'][original['id']]['relative_path']
        predecessor.chmod(0o600)
        predecessor.write_bytes(b'corrupt')
        with self.assertRaises(ApiError): self.undo(job, key, moved['artifact'])
        self.assertEqual(job.read_artifact(moved['artifact_id']), current_bytes)
        self.assertFalse(self.app.template_preview(job.job_id, key)['can_undo'])

    def test_concurrent_undo_with_same_current_identity_has_only_one_winner(self):
        job, key, original, baseline = self.prepare()
        moved = self.app.translate_template(job.job_id, key, dict(artifact_id=original['id'], dx=1, dy=0))
        def attempt(_):
            try: return self.undo(job, key, moved['artifact'])['artifact_id']
            except ApiError as exc: return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(attempt, range(2)))
        self.assertEqual(results.count('stale_artifact'), 1)
        self.assertEqual(len(job.artifact_records()), 1)
        self.assertEqual(job.read_artifact(job.artifact_records()[0]['id']), baseline)

    def test_malformed_previous_summary_cannot_replace_current_artifact(self):
        job, key, original, _ = self.prepare()
        moved = self.app.translate_template(job.job_id, key, dict(artifact_id=original['id'], dx=1, dy=0))
        corrupt = job.snapshot()
        corrupt['history_artifacts'][original['id']]['plan_summary'] = ['corrupt']
        job._save(corrupt)
        before = job.snapshot()
        with self.assertRaises(ApiError): self.undo(job, key, moved['artifact'])
        self.assertEqual(job.snapshot(), before)
        self.assertEqual(job.snapshot()['templates'][key]['output_artifacts'], [moved['artifact_id']])

    def test_new_attempt_cannot_undo_into_previous_attempt(self):
        job, key, original, _ = self.prepare()
        moved = self.app.translate_template(job.job_id, key, dict(artifact_id=original['id'], dx=1, dy=0))
        job.start_attempt(key)
        self.assertFalse(self.app.template_preview(job.job_id, key)['can_undo'])
        with self.assertRaises(ApiError): self.undo(job, key, moved['artifact'])

    def test_http_undo_route_restores_verified_bytes(self):
        from scripts.app import RequestHandler
        job, key, original, baseline = self.prepare()
        moved = self.app.translate_template(job.job_id, key, dict(artifact_id=original['id'], dx=1, dy=0))
        handler = RequestHandler.__new__(RequestHandler)
        handler.server = SimpleNamespace(application=self.app)
        handler._read_json = lambda: {'artifact_id': moved['artifact_id']}
        status, result, _ = handler._dispatch_api('POST', '/api/jobs/' + job.job_id + '/undo/' + key)
        self.assertEqual(status, 200)
        self.assertFalse(result['can_undo'])
        self.assertEqual(job.read_artifact(result['artifact_id']), baseline)
