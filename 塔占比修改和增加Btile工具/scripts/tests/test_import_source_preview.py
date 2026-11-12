"""Original-board previews are read-only capabilities over imported snapshots."""

import hashlib
import json
from unittest.mock import patch

from scripts.tests.project_fixtures import read_project_fixture
from scripts.tests.test_api import ApiContractCase
from scripts.tests.test_rewrite import synthetic
from scripts.tower_reducer.path_binding import TILE_SUFFIX


class ImportSourcePreviewTests(ApiContractCase):
    def preview_url(self, imported, row=0):
        return '/api/imports/{}/preview/{}'.format(
            imported['import_id'], imported['files'][row]['file_id'])

    def import_folder(self, imported):
        return self.runtime_root / '导入' / imported['import_id']

    def snapshot_path(self, imported, row=0):
        return self.import_folder(imported) / 'files' / (imported['files'][row]['file_id'] + '.bytes')

    def runtime_files(self):
        return {str(path.relative_to(self.runtime_root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.runtime_root.rglob('*') if path.is_file()}

    def test_ready_original_has_shared_board_contract_without_creating_jobs_or_writes(self):
        self.start_server()
        imported = self.import_files([('sample_m.bytes', synthetic()[0])])
        self.assertEqual(imported['files'][0]['status'], 'ready')
        before = self.runtime_files()
        resources = self.request('GET', '/api/bootstrap')[2]['resources']

        status, headers, payload, raw = self.request('GET', self.preview_url(imported))

        self.assertEqual(status, 200, raw)
        self.assertEqual(headers['cache-control'], 'no-store')
        self.assertEqual(payload['source']['template_id'], 'sample')
        self.assertEqual((payload['source']['width'], payload['source']['height']), (17, 19))
        self.assertEqual(payload['source']['tiles'][0], {
            'index': 0, 'x': 4, 'y': 4, 'z': 0, 'tile_id': -10,
            'changed': False, 'added': False, 'tower_index': None,
            'support_indices': [], 'support_areas': []})
        self.assertEqual(payload['source']['towers'], [
            {'index': 0, 'x': 2, 'y': 16, 'direction': 2, 'count': 3}])
        self.assertIsNone(payload['output'])
        self.assertEqual(payload['changed_count'], 0)
        self.assertEqual(payload['editor'], {'width': 17, 'height': 19,
                                           'tile_span': 2, 'tower_shift': 16 / 85})
        self.assertFalse(any(tile['changed'] or tile['added'] for tile in payload['source']['tiles']))
        self.assertEqual(self.runtime_files(), before)
        self.assertEqual(list(self.repository.root.iterdir()), [])
        self.assertEqual(self.request('GET', '/api/bootstrap')[2]['resources'], resources)

    def test_no_tower_preview_matches_existing_job_original_and_keeps_revision(self):
        self.start_server()
        imported = self.import_files([('ntxkd021_m.bytes', read_project_fixture('ntxkd021_m.bytes'))])
        self.assertEqual(imported['files'][0]['status'], 'no_tower')
        result = self.wait_job(self.start_job(imported['import_id']))
        row = self.rows(result)[0]
        existing = self.request('GET', '/api/jobs/{}/preview/{}'.format(result['job_id'], row['key']))
        before = self.runtime_files()

        status, _, payload, raw = self.request('GET', self.preview_url(imported))

        self.assertEqual(status, 200, raw)
        self.assertTrue(payload['source']['tiles'])
        self.assertEqual(payload['source']['towers'], [])
        self.assertIsNone(payload['output'])
        self.assertEqual(existing[0], 200)
        for key in ('source', 'output', 'changed_count', 'editor'):
            self.assertEqual(payload[key], existing[2][key])
        self.assertEqual(self.runtime_files(), before)
        unchanged = self.request('GET', '/api/jobs/' + result['job_id'])[2]
        self.assertEqual(unchanged['revision'], result['revision'])
        self.assertEqual(unchanged['artifacts'], [])

    def test_id_import_preview_uses_snapshot_when_external_repository_changes(self):
        self.start_server()
        root = self.discovery_root / 'work' / TILE_SUFFIX
        root.mkdir(parents=True)
        raw = synthetic()[0]
        source = root / 'sample_m.bytes'
        source.write_bytes(raw)
        binding = self.request('PUT', '/api/settings', json_body={
            'tile_root': str(root), 'tile_confirmed': True})
        self.assertEqual(binding[0], 200)
        response = self.request('POST', '/api/import-ids', json_body={
            'template_ids': ['sample'], 'source_repository': 'tile'})
        self.assertEqual(response[0], 200, response[3])
        source.write_bytes(b'external repository changed')
        before = source.read_bytes()

        status, _, payload, response_raw = self.request('GET', self.preview_url(response[2]))

        self.assertEqual(status, 200, response_raw)
        self.assertEqual(payload['source']['template_id'], 'sample')
        self.assertEqual(len(payload['source']['tiles']), 9)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(self.snapshot_path(response[2]).read_bytes(), raw)

    def test_missing_invalid_and_empty_ids_have_bounded_json_errors(self):
        self.start_server()
        imported = self.import_files([('sample_m.bytes', synthetic()[0])])
        import_id, file_id = imported['import_id'], imported['files'][0]['file_id']
        for url, status, code in (
            ('/api/imports//preview/' + file_id, 400, 'invalid_request'),
            ('/api/imports/' + import_id + '/preview/', 400, 'invalid_request'),
            ('/api/imports/bad/preview/' + file_id, 400, 'invalid_request'),
            ('/api/imports/' + import_id + '/preview/bad', 400, 'invalid_request'),
            ('/api/imports/import-' + 'f' * 32 + '/preview/' + file_id, 404, 'import_not_found'),
            ('/api/imports/' + import_id + '/preview/file-' + 'f' * 32, 404, 'template_not_found'),
            ('/api/imports/' + import_id + '/preview/%2e%2e%2fsettings.json', 404, 'not_found'),
        ):
            with self.subTest(url=url):
                self.assert_error(self.request('GET', url), status, code)

    def test_failed_rows_and_other_imports_file_ids_cannot_be_previewed(self):
        self.start_server()
        imported = self.import_files([('bad_m.bytes', b'broken'), ('sample_m.bytes', synthetic()[0])])
        self.assertEqual(imported['files'][0]['status'], 'failed')
        other = self.import_files([('other_m.bytes', synthetic()[0])])
        self.assert_error(self.request('GET', self.preview_url(imported)), 404, 'template_not_found')
        self.assert_error(self.request('GET', '/api/imports/{}/preview/{}'.format(
            imported['import_id'], other['files'][0]['file_id'])), 404, 'template_not_found')
        self.assertEqual(self.request('GET', self.preview_url(imported, 1))[0], 200)

    def test_source_sha_changes_missing_files_and_same_bytes_symlinks_are_rejected(self):
        self.start_server()
        raw = synthetic()[0]
        imported = self.import_files([('sample_m.bytes', raw)])
        snapshot = self.snapshot_path(imported)
        snapshot.chmod(0o600)
        snapshot.write_bytes(raw + b'changed')
        self.assert_error(self.request('GET', self.preview_url(imported)), 409, 'import_state_invalid')
        snapshot.unlink()
        self.assert_error(self.request('GET', self.preview_url(imported)), 409, 'import_state_invalid')
        outside = self.root / 'outside.bytes'
        outside.write_bytes(raw)
        snapshot.symlink_to(outside)
        self.assert_error(self.request('GET', self.preview_url(imported)), 409, 'import_state_invalid')
        self.assertEqual(outside.read_bytes(), raw)

    def test_symlinked_import_directory_and_manifest_are_rejected(self):
        self.start_server()
        imported = self.import_files([('sample_m.bytes', synthetic()[0])])
        folder = self.import_folder(imported)
        actual = folder.with_name('moved-import')
        folder.rename(actual)
        folder.symlink_to(actual, target_is_directory=True)
        self.assert_error(self.request('GET', self.preview_url(imported)), 409, 'import_state_invalid')
        folder.unlink()
        actual.rename(folder)
        manifest = folder / 'manifest.json'
        moved = folder / 'moved-manifest.json'
        manifest.rename(moved)
        manifest.symlink_to(moved)
        self.assert_error(self.request('GET', self.preview_url(imported)), 409, 'import_state_invalid')

    def test_manifest_identity_corruption_and_unpreviewable_status_fail_closed(self):
        self.start_server()
        imported = self.import_files([('sample_m.bytes', synthetic()[0])])
        manifest_path = self.import_folder(imported) / 'manifest.json'
        original = json.loads(manifest_path.read_text())
        cases = [
            ({**original, 'import_id': 'import-' + 'f' * 32}, 409, 'import_state_invalid'),
            ({**original, 'files': [None, original['files'][0]]}, 409, 'import_state_invalid'),
            ({**original, 'files': [original['files'][0], original['files'][0]]}, 409, 'import_state_invalid'),
            ({**original, 'files': [{**original['files'][0], 'status': 'queued'}]}, 404, 'template_not_found'),
        ]
        for manifest, status, code in cases:
            with self.subTest(manifest=manifest):
                manifest_path.write_text(json.dumps(manifest))
                self.assert_error(self.request('GET', self.preview_url(imported)), status, code)

    def test_invalid_snapshot_with_matching_sha_is_a_bounded_preview_error(self):
        self.start_server()
        imported = self.import_files([('sample_m.bytes', synthetic()[0])])
        snapshot = self.snapshot_path(imported)
        snapshot.chmod(0o600)
        snapshot.write_bytes(b'broken')
        manifest_path = self.import_folder(imported) / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['files'][0]['sha256'] = hashlib.sha256(b'broken').hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        self.assert_error(self.request('GET', self.preview_url(imported)), 409, 'preview_unavailable')

    def test_get_retains_host_guard_and_does_not_require_write_csrf(self):
        self.start_server()
        imported = self.import_files([('sample_m.bytes', synthetic()[0])])
        url = self.preview_url(imported)
        self.assert_error(self.request('GET', url, host='evil.example'), 403, 'host_rejected')
        self.assertEqual(self.request('GET', url, csrf=False, same_origin=False)[0], 200)


class IncreasePublicFailureTests(ApiContractCase):
    cases = (
        ('search_budget', 'search_budget', '尝试', '暂未'),
        ('resource_budget', 'resource_budget', '规模', '单独'),
        ('new_tower_positions_unavailable', 'new_tower_positions_unavailable', '对称位置', '区域'),
        ('existing_tower_layout_unavailable', 'existing_tower_layout_unavailable', '原塔', '平移'),
        ('stack_count_symmetry_unavailable', 'stack_count_symmetry_unavailable', '对称', '自动'),
        ('unexpected_internal_error', 'invalid_increase', '参数', '检查'),
    )

    def assert_public_failure(self, failure, code, message_fact, recovery_fact):
        self.assertEqual(failure['code'], 'increase_' + code)
        self.assertIn(message_fact, failure['message'])
        self.assertIn(recovery_fact, failure['message'] + ' '.join(failure['recommendations']))
        self.assertNotIn('预算', failure['message'] + ' '.join(failure['recommendations']))
        self.assertNotIn('无解', failure['message'] + ' '.join(failure['recommendations']))
        self.assertNotIn(str(self.root), repr(failure))

    def test_readonly_recommendation_uses_safe_specific_engine_failure_mapping(self):
        self.start_server()
        imported = self.import_files([('sample_m.bytes', synthetic()[0])])
        for engine_code, public_code, message_fact, recovery_fact in self.cases:
            with self.subTest(code=engine_code):
                engine_result = {'schemes': [], 'error': {'code': engine_code, 'message': str(self.root)}}
                with patch('scripts.tower_reducer.api.recommend_increase', return_value=engine_result):
                    response = self.request('POST', '/api/increase/preview', json_body={
                        'import_id': imported['import_id'], 'file_id': imported['files'][0]['file_id'],
                        'increase': {'target_mode': 'count', 'added_tiles': 3}})
                self.assertEqual(response[0], 200, response[3])
                self.assertEqual(response[2]['status'], 'unavailable')
                self.assertEqual(response[2]['schemes'], [])
                self.assert_public_failure(response[2]['error'], public_code, message_fact, recovery_fact)
        self.assertEqual(list(self.repository.root.iterdir()), [])

    def test_generation_uses_same_specific_public_mapping_without_exposing_engine_text(self):
        self.start_server()
        imported = self.import_files([('sample_m.bytes', synthetic()[0])])
        for engine_code, public_code, message_fact, recovery_fact in self.cases:
            with self.subTest(code=engine_code):
                engine_result = {'ok': False, 'error': {'code': engine_code, 'message': str(self.root)}}
                with patch('scripts.tower_reducer.api.build_increase', return_value=engine_result):
                    result = self.wait_job(self.start_job(imported['import_id'], defaults={
                        'mode': 'increase', 'naming_rule': 'moreT{原模板ID}',
                        'increase': {'target_mode': 'count', 'added_tiles': 3}}))
                row = self.rows(result)[0]
                self.assertEqual(row['status'], 'failed')
                self.assertEqual(result['artifacts'], [])
                self.assert_public_failure(row['error'], public_code, message_fact, recovery_fact)


if __name__ == '__main__':
    import unittest
    unittest.main()
