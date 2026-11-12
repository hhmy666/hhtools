"""Resolve renamed inputs from real sources without changing validation authority."""
from dataclasses import replace
from unittest.mock import patch

from scripts.tests.test_parameter_flow_api import ParameterFlowApiTests
from scripts.tests.test_rewrite import synthetic
from scripts.tower_reducer.settings import load_settings, save_settings
from scripts.tower_reducer.template_reader import decode_template, encode_template


class ParameterSourceMatchingTests(ParameterFlowApiTests):
    def library(self, identities=('sample',), raw=None):
        root = self.root / 'tile/Tile/Assets/Resources/TileMatchLevelData'
        root.mkdir(parents=True, exist_ok=True)
        raw = raw or synthetic(second_tower=True)[0]
        for identity in identities:
            document = decode_template(raw)
            document.template_id = identity
            (root / (identity + '_m.bytes')).write_bytes(encode_template(document))
        settings = load_settings(self.settings_path)
        settings.tile_root, settings.tile_confirmed = str(root), True
        save_settings(settings, self.settings_path)
        return root

    def flat_output(self, new_id, raw=None, a_count=9):
        document = decode_template(raw or synthetic(second_tower=True)[0])
        document.template_id = new_id
        document.tiles = [replace(tile, x=index * 3, y=0, z=0, tile_id=-10 if index < a_count else -11)
                          for index, tile in enumerate(document.tiles)]
        output = self.export_root / (new_id + '_m.bytes')
        output.write_bytes(encode_template(document))
        return output

    def test_noT_copy_matches_verified_artifact_after_only_internal_id_changed(self):
        app = self.start_server()
        self.config()
        job = app.repository.get(self.verified_job())
        artifact = next(iter(job.snapshot()['artifacts'].values()))
        original = decode_template(job.read_artifact(artifact['id']))
        original.template_id = 'noTsample'
        (self.export_root / 'noTsample_m.bytes').write_bytes(encode_template(original))
        selection = self.call('resolve', {'mode': 'ids', 'template_ids': ['noTsample']})
        entry = selection['entries'][0]
        self.assertEqual(entry['status'], 'verified', entry)
        self.assertEqual(entry['original_template_id'], 'sample')
        self.assertEqual(entry['new_template_id'], 'noTsample')
        self.assertEqual(entry['match_kind'], 'renamed_artifact')
        result = self.call('calculate', {'selection_id': selection['selection_id']})
        self.assertEqual(result['rows'][0]['new_template_id'], 'noTsample')

    def test_auto_matching_happens_before_towerless_classification(self):
        self.start_server()
        self.config()
        self.library()
        self.flat_output('noTsample')
        selection = self.call('resolve', {'mode': 'ids', 'template_ids': ['noTsample']})
        entry = selection['entries'][0]
        self.assertEqual(entry['status'], 'verified', entry)
        self.assertEqual(entry['original_template_id'], 'sample')
        self.assertEqual(entry['source_tile_counts'], {'A': 12, 'B': 3})
        self.assertEqual(entry['result_tile_counts'], {'A': 9, 'B': 6})
        result = self.call('calculate', {'selection_id': selection['selection_id']})
        self.assertEqual(result['rows'][0]['status'], 'ready', result)

    def test_native_version_suffix_is_preserved_when_that_source_exists(self):
        self.start_server()
        self.library(('sample', 'samplev2'))
        self.flat_output('noTsamplev2')
        selection = self.call('resolve', {'mode': 'ids', 'template_ids': ['noTsamplev2']})
        self.assertEqual(selection['entries'][0]['original_template_id'], 'samplev2')

    def test_explicit_stripping_removes_whole_ends_for_files(self):
        self.start_server()
        self.library(('sample', 'samplev2'))
        output = self.flat_output('noTsamplev2_final')
        imported = self.import_files([(output.name, output.read_bytes())])
        selection = self.call('resolve', {'mode': 'files', 'import_id': imported['import_id'],
            'strip_prefix': 'noT', 'strip_suffix': '_final'})
        self.assertEqual(selection['entries'][0]['status'], 'verified', selection)
        self.assertEqual(selection['entries'][0]['original_template_id'], 'samplev2')

    def test_explicit_prefix_never_removes_matching_text_from_the_middle(self):
        self.start_server()
        self.library()
        self.flat_output('XnoTsample')
        selection = self.call('resolve', {'mode': 'ids', 'template_ids': ['XnoTsample'], 'strip_prefix': 'noT'})
        self.assertEqual(selection['entries'][0]['status'], 'failed')
        self.assertIn('前缀', selection['entries'][0]['message'])

    def test_explicit_prefix_is_checked_even_when_an_artifact_already_matches(self):
        self.start_server()
        job_id = self.verified_job()
        app = self.server.application
        job = app.repository.get(job_id)
        artifact = next(iter(job.snapshot()['artifacts'].values()))
        (self.export_root / 'lessTsample_m.bytes').write_bytes(job.read_artifact(artifact['id']))
        selection = self.call('resolve', {'mode': 'ids', 'template_ids': ['lessTsample'], 'strip_prefix': 'wrong'})
        self.assertEqual(selection['entries'][0]['status'], 'failed', selection)
        self.assertIn('前缀', selection['entries'][0]['message'])

    def test_ambiguous_sources_are_returned_for_user_selection_and_explicit_map_resolves(self):
        self.start_server()
        self.library(('sample', 'other'))
        self.flat_output('sample_other')
        payload = {'mode': 'ids', 'template_ids': ['sample_other']}
        selection = self.call('resolve', payload)
        entry = selection['entries'][0]
        self.assertEqual(entry['status'], 'failed', entry)
        self.assertEqual({row['original_template_id'] for row in entry['source_candidates']}, {'sample', 'other'})
        chosen = self.call('resolve', dict(payload, source_map={'sample_other': 'sample'}))
        self.assertEqual(chosen['entries'][0]['original_template_id'], 'sample')
        self.assertEqual(chosen['entries'][0]['status'], 'verified')

    def test_auto_source_evidence_still_detects_later_changes(self):
        self.start_server()
        self.config()
        root = self.library()
        self.flat_output('noTsample')
        selection = self.call('resolve', {'mode': 'ids', 'template_ids': ['noTsample']})
        source = root / 'sample_m.bytes'
        source.write_bytes(source.read_bytes() + b'\xf8\x01\x01')
        self.assert_error(self.request('POST', '/api/parameters/calculate', json_body={
            'selection_id': selection['selection_id']}), 409, 'parameter_evidence_changed')

    def test_auto_matching_does_not_grant_a_registered_manual_operation(self):
        self.start_server()
        self.config()
        raw = synthetic(kind='A', second_tower=True)[0]
        self.library(raw=raw)
        self.flat_output('noTsample', raw=raw, a_count=12)
        selection = self.call('resolve', {'mode': 'ids', 'template_ids': ['noTsample']})
        self.assertEqual(selection['entries'][0]['status'], 'verified', selection)
        self.assertEqual(selection['entries'][0]['operation'], 'reduce')
        self.assertEqual(selection['entries'][0]['match_kind'], 'automatic_source')

    def test_one_new_template_can_bind_multiple_real_sources_independently(self):
        self.start_server()
        root, _ = self.config()
        self.library(('sample', 'other'))
        self.flat_output('shared')
        config = root / 'config_level.csv'
        config.write_bytes(config.read_bytes() + b'level-other,other,other,1,' +
            b','.join(('4|' + str(i) + '|100|1').encode() for i in range(10)) + b',1003,7,keep\r\n')
        selection = self.call('resolve', {'mode': 'ids', 'template_ids': ['shared'],
            'source_map': {'shared': ['sample', 'other']}})
        self.assertEqual({row['original_template_id'] for row in selection['entries']}, {'sample', 'other'})
        self.assertTrue(all(row['status'] == 'verified' for row in selection['entries']), selection)
        result = self.call('calculate', {'selection_id': selection['selection_id']})
        self.assertEqual(len(result['rows']), 2)
        self.assertEqual({row['new_template_id'] for row in result['rows']}, {'shared'})
        self.assertEqual(len({row['new_level_id'] for row in result['rows']}), 2)

    def test_each_explicit_source_binding_has_independent_change_detection(self):
        self.start_server()
        self.config()
        library = self.library(('sample', 'other'))
        self.flat_output('shared')
        selection = self.call('resolve', {'mode': 'ids', 'template_ids': ['shared'],
            'source_map': {'shared': ['sample', 'other']}})
        source = library / 'other_m.bytes'
        source.write_bytes(source.read_bytes() + b'\xf8\x01\x01')
        self.assert_error(self.request('POST', '/api/parameters/calculate', json_body={
            'selection_id': selection['selection_id']}), 409, 'parameter_evidence_changed')

    def test_a_second_explicit_source_cannot_borrow_the_first_sources_manual_plan(self):
        app = self.start_server()
        self.library(('other',))
        job = app.repository.get(self.verified_job())
        state = job.snapshot()
        artifact = next(iter(state['artifacts'].values()))
        app.edit_template(job.job_id, artifact['template_key'], {'artifact_id':artifact['id'],
            'deleted_indices':[], 'additions':[]})
        artifact = next(iter(job.snapshot()['artifacts'].values()))
        (self.export_root / 'lessTsample_m.bytes').write_bytes(job.read_artifact(artifact['id']))
        selection = self.call('resolve', {'mode':'ids', 'template_ids':['lessTsample'],
            'source_map':{'lessTsample':['sample','other']}})
        rows = {row['original_template_id']:row for row in selection['entries']}
        self.assertEqual(rows['sample']['operation'], 'manual', rows)
        self.assertEqual(rows['other']['operation'], 'reduce', rows)
