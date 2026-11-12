"""Ordinary additions cross the same artifact trust boundary as tower edits."""
from pathlib import Path
import io
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from scripts.tests.project_fixtures import read_project_fixture
from scripts.tests.test_api import FakeSyncService
from scripts.tests.test_increase_engine import source
from scripts.tests.test_parameter_config import FIELDS, LIST_FIELDS, csv_bytes, source_row
from scripts.tower_reducer.api import Application, ApiError
from scripts.tower_reducer.parameter_flow import ParameterFlow
from scripts.tower_reducer.parameter_config import ParameterConfigService
from scripts.tower_reducer.export_service import export_outputs
from scripts.tower_reducer.path_binding import TILE_SUFFIX, TEMPLATE_SUFFIX
from scripts.tower_reducer.settings import LocalSettings, save_settings, load_settings
from scripts.tower_reducer.storage import JobRepository, _unpack
from scripts.tower_reducer.template_reader import decode_template


class OrdinaryAddApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tool = self.root / 'tool'
        self.tool.mkdir()
        self.output = self.root / 'output'
        self.output.mkdir()
        self.config = self.root / 'config'
        self.config.mkdir()
        self.settings = self.root / 'settings.json'
        save_settings(LocalSettings(export_root=str(self.output), config_root=str(self.config)), self.settings)
        self.repository = JobRepository(self.root / 'jobs')
        self.app = Application(tool_root=self.tool, repository=self.repository,
            settings_path=self.settings, discovery_root=self.root,
            sync_service=FakeSyncService(), csrf_token='temporary-csrf-token')
        self.raw = source(total=12, b=6)
        (self.config / 'config_level.csv').write_bytes(csv_bytes(FIELDS, [source_row(
            'level-source', 'original', ['1|0|100|1'] * 10)]))
        (self.config / 'config_level_list.csv').write_bytes(csv_bytes(
            LIST_FIELDS, [['20', '0', 'level-source', '', '', '']]))

    def create(self, options=None, overrides=None):
        imported = self.app.import_files([('original_m.bytes', self.raw)])
        self.assertEqual(imported['files'][0]['status'], 'no_tower')
        response = self.app.create_job({'import_id': imported['import_id'],
            'defaults': {'mode': 'ordinary_add', 'ordinary_add': options or {'added_tiles': 3}},
            'overrides': {} if overrides is None else {imported['files'][0]['file_id']: overrides}})
        return response['job_id']

    def finished(self, job_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            # Reruns publish queued state before returning to callers.
            payload = self.app.job_payload(job_id)
            if payload['status'] in ('succeeded', 'partial_failed', 'failed'):
                return payload, next(iter(payload['templates'].values()))
            time.sleep(.01)
        self.fail('ordinary addition did not terminate')

    def test_no_tower_addition_is_verified_downloadable_and_recovers_as_typed_plan(self):
        job_id = self.create()
        payload, row = self.finished(job_id)
        self.assertEqual(row['status'], 'verified', row)
        self.assertEqual(row['naming']['new_id'], 'moreBoriginal')
        self.assertEqual(row['parameters']['ordinary_add']['added_tiles'], 3)
        self.assertEqual(row['plan_summary']['operation'], 'ordinary_add')
        self.assertIn('普通B', row['placement_summary'])
        record = self.repository.get(job_id).artifact_records()[0]
        self.assertEqual(type(_unpack(record['plan'])).__name__, 'OrdinaryAddPlan')
        output = self.repository.get(job_id).read_artifact(record['id'])
        doc = decode_template(output)
        self.assertEqual((len(doc.tiles), len(doc.stacks)), (15, 0))
        self.assertEqual(sum(t.tile_id == -11 for t in doc.tiles), 9)
        recovered = JobRepository(self.repository.root)
        recovered.discover()
        self.assertEqual(recovered.get(job_id).read_artifact(record['id']), output)
        renamed, plan = recovered.get(job_id).renamed_output_bytes(record['id'], 'moreBoriginal_2')
        self.assertEqual(decode_template(renamed).template_id, 'moreBoriginal_2')
        self.assertEqual(type(plan).__name__, 'OrdinaryAddPlan')

    def test_total_already_in_range_skips_without_an_output_and_keeps_source_counts(self):
        job_id=self.create({'target_mode':'total_range','min_total_tiles':12,'max_total_tiles':21})
        payload,row=self.finished(job_id)
        self.assertEqual(row['status'],'no_op')
        self.assertEqual(row['plan_summary']['original_total'],12)
        self.assertEqual(row['plan_summary']['final_total'],12)
        self.assertTrue(row['plan_summary']['recommendations'])
        self.assertFalse(self.repository.get(job_id).artifact_records())

    def test_failed_addition_retains_source_counts(self):
        job_id=self.create({'added_tiles':300,'layer_mode':'exact','min_added_layers':0,'max_added_layers':0})
        _,row=self.finished(job_id)
        self.assertEqual(row['status'],'failed')
        self.assertEqual(row['plan_summary']['original_total'],12)
        self.assertEqual(row['plan_summary']['original_counts'],[6,6])
        legacy=dict(row['plan_summary'])
        for field in ('original_total','original_counts','original_tower_tiles'): legacy.pop(field)
        self.repository.get(job_id).update_template(row['key'],plan_summary=legacy)
        recovered=self.app.job_payload(job_id)['templates'][row['key']]['plan_summary']
        self.assertEqual(recovered['original_total'],12)
        self.assertEqual(recovered['original_counts'],[6,6])

    def test_failed_bottom_plan_offers_verified_options_and_reruns_only_on_request(self):
        from scripts.tests.test_ordinary_add_engine import source as ordinary_source
        self.raw=ordinary_source([(x,y,z) for z in range(6) for x in
            ([4,6,10,12] if z%2==0 else [5,11]) for y in [4,7,10,13]])
        job_id=self.create({'added_tiles':18})
        _,row=self.finished(job_id)
        self.assertEqual(row['status'],'failed')
        advice=row['plan_summary']['parameter_advice']
        self.assertEqual(len(advice['schemes']),2)
        self.assertEqual(row['parameters']['ordinary_add']['placement_strategy'],'bottom_first')
        self.assertFalse(self.repository.get(job_id).artifact_records())
        self.app.rerun(job_id,row['key'],{'defaults':advice['schemes'][0]['parameters']})
        _,rerun=self.finished(job_id)
        self.assertEqual(rerun['status'],'verified',rerun)
        self.assertEqual(rerun['plan_summary']['added_tiles'],18)

    def test_total_range_is_kept_as_job_intent_through_preview_generation_and_recovery(self):
        options={'target_mode':'total_range','min_total_tiles':18,'max_total_tiles':21}
        imported=self.app.import_files([('original_m.bytes',self.raw)])
        preview=self.app.ordinary_add_preview({'import_id':imported['import_id'],
            'file_id':imported['files'][0]['file_id'],'ordinary_add':options})
        self.assertTrue(preview['ok'],preview)
        job_id=self.create(options)
        _,row=self.finished(job_id)
        self.assertEqual(row['status'],'verified',row)
        self.assertEqual(row['parameters']['ordinary_add']['target_mode'],'total_range')
        record=self.repository.get(job_id).artifact_records()[0]
        output=self.repository.get(job_id).read_artifact(record['id'])
        self.assertTrue(18<=len(decode_template(output).tiles)<=21)
        recovered=JobRepository(self.repository.root);recovered.discover()
        self.assertEqual(recovered.get(job_id).read_artifact(record['id']),output)

    def test_custom_top_bottom_counts_survive_artifact_registration(self):
        from scripts.tests.test_ordinary_add_engine import source as ordinary_source
        self.raw=ordinary_source()
        job_id=self.create({'added_tiles':12,'placement_strategy':'both','split_mode':'custom',
            'top_tiles':3,'bottom_tiles':9,'layer_mode':'exact','min_added_layers':3,'max_added_layers':3})
        _,row=self.finished(job_id)
        self.assertEqual(row['status'],'verified',row)
        self.assertEqual((row['plan_summary']['top_added_tiles'],row['plan_summary']['bottom_added_tiles']),(3,9))
        record=self.repository.get(job_id).artifact_records()[0]
        self.assertEqual(len(decode_template(self.repository.get(job_id).read_artifact(record['id'])).tiles),18)

    def test_successful_row_can_be_rerun_with_fresh_ordinary_add_options(self):
        job_id = self.create()
        _, original = self.finished(job_id)
        self.assertEqual(original['status'], 'verified', original)
        self.app.rerun(job_id, original['key'], {'defaults': {'mode': 'ordinary_add',
            'ordinary_add': {'added_tiles': 6}, 'new_id': 'moreBsecond'}})
        _, row = self.finished(job_id)
        self.assertEqual(row['status'], 'verified', row)
        self.assertEqual(row['plan_summary']['added_tiles'], 6)
        self.assertEqual(row['attempts'][0]['parameters']['ordinary_add']['added_tiles'], 3)
        self.assertEqual(len(self.repository.get(job_id).snapshot()['history_artifacts']), 1)

    def test_successful_batch_rerun_preserves_sibling_and_revokes_old_download(self):
        sibling = source(total=18, b=6).replace(b'original', b'sibling0', 1)
        imported = self.app.import_files([('original_m.bytes', self.raw), ('sibling0_m.bytes', sibling)])
        response = self.app.create_job({'import_id': imported['import_id'],
            'defaults': {'mode':'ordinary_add', 'ordinary_add':{'added_tiles':3}}, 'overrides':{}})
        job_id = response['job_id']
        payload, _ = self.finished(job_id)
        job = self.repository.get(job_id)
        before = job.snapshot()
        first, second = list(before['templates'])
        self.assertEqual(before['templates'][first]['status'], 'verified', payload)
        self.assertEqual(before['templates'][second]['status'], 'verified', payload)
        old_artifact = before['templates'][first]['output_artifacts'][0]
        self.app.rerun(job_id, first, {'defaults': {'mode':'ordinary_add',
            'ordinary_add':{'added_tiles':6}, 'new_id':'moreBsecond'}})
        self.finished(job_id)
        self.assertEqual(job.snapshot()['templates'][second], before['templates'][second])
        self.assertIn(old_artifact, job.snapshot()['history_artifacts'])
        with self.assertRaises((KeyError, ValueError)):
            job.read_artifact(old_artifact)

    def test_nested_overrides_validate_merged_layer_limits_before_creating_job(self):
        with self.assertRaises(ApiError) as error:
            self.create({'layer_mode': 'range', 'min_added_layers': 1, 'max_added_layers': 2},
                        {'ordinary_add': {'min_added_layers': 3}})
        self.assertEqual(error.exception.code, 'invalid_request')
        self.assertEqual(len(self.repository.discover()), 0)

    def test_partial_layer_override_is_validated_against_actual_batch_limits(self):
        from scripts.tower_reducer.api import _effective_options, _validate_options
        defaults = _validate_options({'mode':'ordinary_add', 'ordinary_add':{
            'layer_mode':'range', 'max_added_layers':5}}, partial=False)
        try:
            overrides = _validate_options({'ordinary_add':{'min_added_layers':4}})
        except ApiError as error:
            self.fail('partial minimum must merge with the actual maximum first: ' + error.message)
        effective = _effective_options(defaults, overrides)
        self.assertEqual(effective['ordinary_add']['min_added_layers'], 4)
        self.assertEqual(effective['ordinary_add']['max_added_layers'], 5)

    def test_parameter_flow_proves_counts_and_rejects_new_b_below_confirmed_minimum(self):
        job_id = self.create({'added_tiles': 6})
        _, row = self.finished(job_id)
        self.assertEqual(row['status'], 'verified', row)
        flow = ParameterFlow(self.app)
        selected = flow.resolve({'mode': 'job', 'job_id': job_id})
        self.assertEqual(selected['entries'][0]['operation'], 'increase')
        self.assertEqual(selected['entries'][0]['source_tile_counts'], {'A': 6, 'B': 6})
        self.assertEqual(selected['entries'][0]['result_tile_counts'], {'A': 6, 'B': 12})
        calculated = flow.calculate({'selection_id': selected['selection_id']})
        calculated_row = calculated['rows'][0]
        self.assertEqual(calculated_row['status'], 'ready', calculated_row)
        self.assertEqual(calculated_row['new_parameters'], ['1|0|100|2'] * 10)
        self.assertEqual(calculated_row['new_level_id'], 'moreBlevel-source')
        (self.config / 'config_level.csv').write_bytes(csv_bytes(FIELDS, [source_row(
            'level-source', 'original', ['4|0|100|0'] * 10)]))
        self.raw = source(total=12)
        zero_id = self.create()
        _, row = self.finished(zero_id)
        self.assertEqual(row['status'], 'verified', row)
        selected = flow.resolve({'mode': 'job', 'job_id': zero_id})
        calculated = flow.calculate({'selection_id': selected['selection_id']})
        self.assertEqual(calculated['rows'][0]['status'], 'failed', calculated)
        self.assertIn('至少需要9', calculated['rows'][0]['message'])

    def test_new_b_uses_confirmed_new_a_rule_for_all_modes(self):
        values = ['10|0|100|0', '9|7|100|0'] + ['8|8|99|0'] * 8
        (self.config / 'config_level.csv').write_bytes(csv_bytes(FIELDS, [source_row(
            'level-source', 'original', values)]))
        item = {'original_template_id':'original', 'new_template_id':'moreBoriginal',
            'source_tile_counts':{'A':60, 'B':0}, 'result_tile_counts':{'A':60, 'B':18},
            'operation':'increase', 'template_mode':'ordinary_add', 'status':'verified'}
        service = ParameterConfigService(self.root)
        row = service.calculate(self.config, [item])['rows'][0]
        self.assertEqual(row['status'], 'ready', row)
        self.assertEqual(row['new_parameters'], ['10|0|100|3', '9|7|100|3'] + ['8|8|99|3'] * 8)
        self.assertEqual(row['delta'], {'A':0, 'B':3})
        self.assertEqual(row['parameter_basis']['B'], 'new_A_groups_per_pattern_ceiling_minus_one_min_three')
        for mode in ('reduce', 'increase'):
            with self.subTest(mode=mode):
                converted = service.calculate(self.config, [{**item, 'template_mode':mode}])['rows'][0]
                self.assertEqual(converted['status'], 'ready', converted)
                self.assertEqual(converted['new_parameters'], row['new_parameters'])

    def test_versioned_export_zip_sync_and_explicit_file_selection_reuse_verified_plan(self):
        from scripts.tower_reducer.export_service import build_zip
        from scripts.tower_reducer.sync_service import SyncService
        job_id = self.create({'added_tiles':6})
        _, row = self.finished(job_id)
        self.assertEqual(row['status'], 'verified', row)
        job = self.repository.get(job_id)
        (self.output / 'moreBoriginal_m.bytes').write_bytes(b'prior export')
        exported = export_outputs(job, self.output, auto_version=True)
        self.assertEqual(exported.rows[0]['status'], 'exported', exported)
        path = self.output / 'moreBoriginalv2_m.bytes'
        self.assertEqual(decode_template(path.read_bytes()).template_id, 'moreBoriginalv2')
        self.assertEqual((self.output / 'moreBoriginal_m.bytes').read_bytes(), b'prior export')
        with zipfile.ZipFile(io.BytesIO(build_zip(job))) as archive:
            self.assertEqual(archive.namelist(), ['moreBoriginalv2_m.bytes'])
            self.assertEqual(archive.read(archive.namelist()[0]), path.read_bytes())
        settings = load_settings(self.settings)
        tile = self.root / 'tile-checkout' / TILE_SUFFIX
        template = self.root / 'template-checkout' / TEMPLATE_SUFFIX
        tile.mkdir(parents=True)
        template.mkdir(parents=True)
        settings.tile_root, settings.template_root = str(tile), str(template)
        settings.tile_confirmed = settings.template_confirmed = True
        save_settings(settings, self.settings)
        sync = SyncService(self.repository, self.settings)
        plan = sync.create_sync_plan(job, ('tile', 'template'))
        committed = sync.commit_sync(plan.plan_id, {})
        self.assertTrue(committed.ok, committed)
        self.assertEqual((tile / path.name).read_bytes(), path.read_bytes())
        self.assertEqual((template / path.name).read_bytes(), path.read_bytes())
        flow = ParameterFlow(self.app)
        for selection in ({'mode':'ids', 'source':'output', 'template_ids':['moreBoriginalv2'],
                'source_map':{'moreBoriginalv2':'original'}},
                {'mode':'files', 'import_id':self.app.import_files([(path.name, path.read_bytes())])['import_id'],
                 'source_map':{'moreBoriginalv2':'original'}}):
            selected = flow.resolve(selection)
            entry = selected['entries'][0]
            self.assertEqual((entry['status'], entry['operation'], entry['template_mode']),
                             ('verified', 'increase', 'ordinary_add'))
            calculated = flow.calculate({'selection_id':selected['selection_id']})
            self.assertEqual(calculated['rows'][0]['new_parameters'], ['1|0|100|2'] * 10)
        self.app.rerun(job_id, row['key'], {'defaults': {'mode':'ordinary_add',
            'ordinary_add':{'added_tiles':3}, 'new_id':'moreBrerun'}})
        self.finished(job_id)
        selected = flow.resolve({'mode':'ids', 'source':'output', 'template_ids':['moreBoriginalv2']})
        self.assertEqual(selected['entries'][0]['operation'], 'increase')
        self.assertEqual(selected['entries'][0]['template_mode'], 'ordinary_add')

    def test_reduce_mode_continues_to_skip_original_without_towers(self):
        imported = self.app.import_files([('original_m.bytes', self.raw)])
        response = self.app.create_job({'import_id':imported['import_id'], 'defaults':{}, 'overrides':{}})
        _, row = self.finished(response['job_id'])
        self.assertEqual(row['status'], 'no_op')
        self.assertEqual(row['artifacts'], [])

    def test_failed_addition_on_towerless_source_is_not_mislabeled_no_tower_in_parameter_flow(self):
        job_id = self.create({'added_tiles':300, 'layer_mode':'exact',
                              'min_added_layers':0, 'max_added_layers':0})
        _, row = self.finished(job_id)
        self.assertEqual(row['status'], 'failed', row)
        selected = ParameterFlow(self.app).resolve({'mode':'job', 'job_id':job_id})
        self.assertEqual(selected['entries'][0]['status'], 'failed', selected)
        self.assertIn('尚无已验收新产物', selected['entries'][0]['message'])

    def test_preview_reads_import_snapshot_without_registering_any_artifact(self):
        imported = self.app.import_files([('original_m.bytes', self.raw)])
        request = {'import_id': imported['import_id'], 'file_id': imported['files'][0]['file_id'],
                   'ordinary_add': {'added_tiles': 3}}
        result = self.app.ordinary_add_preview(request)
        self.assertTrue(result['ok'], result)
        self.assertEqual((len(result['source']['tiles']), len(result['output']['tiles'])), (12, 15))
        self.assertEqual(result['options']['tile_type'], 'B')
        self.assertEqual(result['summary']['added_tiles'], 3)
        self.assertGreaterEqual(result['changed_count'], 3)
        self.assertEqual(self.repository.discover(), [])
        self.assertEqual(self.app.import_source_preview(request['import_id'], request['file_id'])['source']['template_id'],
                         'original')

    def test_preview_treats_verified_global_z_offset_as_layer_renumbering(self):
        self.raw = read_project_fixture("ntxkd021_m.bytes")
        imported = self.app.import_files([('original_m.bytes', self.raw)])
        result = self.app.ordinary_add_preview({'import_id':imported['import_id'],
            'file_id':imported['files'][0]['file_id'], 'ordinary_add':{'added_tiles':18,
            'layer_mode':'exact', 'min_added_layers':2, 'max_added_layers':2}})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['changed_count'], 18)
        self.assertEqual(result['layer_offset'], 2)
        self.assertFalse(any(tile['changed'] for tile in result['source']['tiles']))
        self.assertTrue(all(not tile['changed'] for tile in result['output']['tiles'][:102]))

    def test_saved_preview_uses_verified_plan_offset_instead_of_public_summary(self):
        self.raw = read_project_fixture("ntxkd021_m.bytes")
        job_id = self.create({'added_tiles':18, 'layer_mode':'exact',
                              'min_added_layers':2, 'max_added_layers':2})
        _, row = self.finished(job_id)
        self.assertEqual(row['status'], 'verified', row)
        job = self.repository.get(job_id)
        job.update_template(row['key'], plan_summary={**row['plan_summary'], 'original_z_offset':99})
        revision = job.snapshot()['revision']
        preview = self.app.template_preview(job_id, row['key'])
        self.assertEqual(preview['layer_offset'], 2)
        self.assertEqual(preview['changed_count'], 18)
        self.assertEqual(job.snapshot()['revision'], revision)

    def test_comparison_without_trusted_offset_keeps_real_xyz_and_type_changes_visible(self):
        from scripts.tower_reducer.ordinary_add import build_ordinary_add
        from scripts.tower_reducer.preview import comparison
        from scripts.tower_reducer.template_reader import encode_template
        self.raw = read_project_fixture("ntxkd021_m.bytes")
        built = build_ordinary_add(self.raw, {'added_tiles':18, 'layer_mode':'exact',
            'min_added_layers':2, 'max_added_layers':2}, 'moreBoriginal')
        self.assertTrue(built['ok'], built)
        self.assertEqual(comparison(self.raw, built['output'])['changed_count'], 120)
        changed = decode_template(self.raw)
        changed.tiles[0].tile_id = -11
        self.assertEqual(comparison(self.raw, encode_template(changed))['changed_count'], 1)

    def test_trusted_non_tower_offset_does_not_hide_original_tower_z_changes(self):
        from scripts.tower_reducer.preview import comparison
        from scripts.tower_reducer.template_reader import encode_template
        raw = source(total=15, b=6, towers=((3,14,3,0),))
        changed = decode_template(raw)
        for tile in changed.tiles:
            if (tile.x, tile.y) != (3,14):
                tile.z += 2
        shifted = encode_template(changed)
        self.assertEqual(comparison(raw, shifted)['changed_count'], 12)
        self.assertEqual(comparison(raw, shifted, original_z_offset=2)['changed_count'], 0)
        for tile in changed.tiles:
            if (tile.x, tile.y) == (3,14):
                tile.z += 1
        self.assertEqual(comparison(raw, encode_template(changed), original_z_offset=2)['changed_count'], 3)

    def test_preview_does_not_trust_forged_success_from_the_builder(self):
        from scripts.tower_reducer.ordinary_add import build_ordinary_add
        from scripts.tests.test_increase_engine import scalar
        imported = self.app.import_files([('original_m.bytes', self.raw)])
        built = build_ordinary_add(self.raw, {'added_tiles':3}, 'moreBoriginal')
        self.assertTrue(built['ok'], built)
        forged = {**built, 'output':built['output'] + scalar(88, 1)}
        with patch('scripts.tower_reducer.api.build_ordinary_add', return_value=forged):
            result = self.app.ordinary_add_preview({'import_id': imported['import_id'],
                'file_id': imported['files'][0]['file_id'], 'ordinary_add': {'added_tiles':3}})
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'ordinary_add_verification_failed')
        self.assertIsNone(result['output'])
        self.assertEqual(self.repository.discover(), [])

    def test_preview_does_not_adopt_a_verified_plan_for_different_requested_options(self):
        from scripts.tower_reducer.ordinary_add import build_ordinary_add
        imported = self.app.import_files([('original_m.bytes', self.raw)])
        built = build_ordinary_add(self.raw, {'added_tiles':6}, 'moreBoriginal')
        self.assertTrue(built['ok'], built)
        with patch('scripts.tower_reducer.api.build_ordinary_add', return_value=built):
            result = self.app.ordinary_add_preview({'import_id':imported['import_id'],
                'file_id':imported['files'][0]['file_id'], 'ordinary_add':{'added_tiles':3}})
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'ordinary_add_verification_failed')
