"""Increase parameters use server-proven source/output bytes and temporary CSVs."""
import csv
import io
from pathlib import Path
import tempfile
import unittest

from scripts.tests.test_parameter_config import FIELDS, LIST_FIELDS, csv_bytes, source_row, entry
from scripts.tests.test_api import ApiContractCase
from scripts.tests.test_rewrite import message, scalar, synthetic
from scripts.tower_reducer.parameter_config import ParameterConfigService
from scripts.tower_reducer.settings import load_settings, save_settings
from scripts.tower_reducer.template_reader import decode_template, encode_template


class IncreaseParameterCalculationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / 'config'
        self.config.mkdir()
        self.service = ParameterConfigService(self.root)
        self.write_parameters()

    def write_parameters(self, values=None):
        self.original = csv_bytes(FIELDS, [source_row(parameters=values)])
        (self.config / 'config_level.csv').write_bytes(self.original)
        (self.config / 'config_level_list.csv').write_bytes(csv_bytes(
            LIST_FIELDS, [['20', '0', 'old1', '', '', '']]))

    def calculate(self, source=(69, 39), result=(87, 39), **fields):
        return self.service.calculate(self.config, [entry(source, result,
            new_template_id='moreTtpl', operation='increase', **fields)])

    def test_increase_translates_all_ten_duplicates_from_first_half_up_delta(self):
        result = self.calculate()
        row = result['rows'][0]
        self.assertEqual(row['status'], 'ready', row)
        self.assertEqual(row['new_parameters'], ['14|0|100|6', '13|7|99|5'] + ['12|8|80|4'] * 8)
        self.assertEqual(row['delta'], {'A': 3, 'B': 0})
        self.assertEqual(row['operation'], 'increase')
        self.assertEqual((row['original_total'], row['final_total'], row['added_tiles']), (108, 126, 18))
        self.assertEqual(row['new_level_id'], 'moreTold1')
        self.assertEqual(len(result['comparisons']), 2)
        self.assertEqual((self.config / 'config_level.csv').read_bytes(), self.original)

    def test_unchanged_inactive_side_keeps_history_and_original_strings(self):
        values = ['011|000|+100|006', '10|+07|099|+005'] + ['9|008|80|004'] * 8
        self.write_parameters(values)
        row = self.calculate(source=(69, 0), result=(87, 0))['rows'][0]
        self.assertEqual(row['status'], 'ready', row)
        self.assertEqual(row['new_parameters'], ['14|000|+100|006', '13|+07|099|+005'] + ['12|008|80|004'] * 8)
        self.assertEqual(row['delta'], {'A': 3, 'B': 0})

    def test_half_up_rounding_and_both_sides_increase(self):
        self.write_parameters(['3|001|+100|3'] * 10)
        row = self.calculate(source=(18, 18), result=(21, 21))['rows'][0]
        self.assertEqual(row['new_parameters'], ['4|001|+100|4'] * 3 + ['4|001|+100|3'] * 7)

    def test_added_tiles_with_zero_kind_delta_explains_rounding_without_claiming_same_counts(self):
        row = self.calculate(result=(72, 39))['rows'][0]
        self.assertEqual(row['status'], 'unchanged')
        self.assertIn('种类数不变', row['message'])
        self.assertNotIn('A/B 数量不变', row['message'])

    def test_new_B_below_minimum_pattern_capacity_fails_explicitly(self):
        self.write_parameters(['11|0|100|6'] * 10)
        row = self.calculate(source=(69, 0), result=(69, 3))['rows'][0]
        self.assertEqual(row['status'], 'failed')
        self.assertIn('B参数最低为3', row['message'])
        self.assertIn('至少需要9张B牌', row['message'])

    def test_legacy_reduce_and_unknown_operation_cannot_relax_conservation(self):
        for fields in ({}, {'operation':'reduce'}, {'operation':'pretend-increase'}):
            result = self.service.calculate(self.config, [entry(result=(87, 39), **fields)])
            self.assertEqual(result['rows'][0]['status'], 'failed')
        row = self.calculate(result=(66, 45))['rows'][0]
        self.assertEqual(row['status'], 'failed')

    def test_increase_export_preview_append_backup_and_retry_keep_source_prefix(self):
        calculated = self.calculate()
        plan = self.service.preview(calculated['calculation_id'],
            [calculated['rows'][0]['row_id']], config_root=self.config)
        self.assertEqual(plan['summary']['append_count'], 1, plan)
        self.assertIn('+moreTold1', plan['diff'])
        exported, _, _ = self.service.export(calculated['calculation_id'], 'csv')
        table = list(csv.reader(io.StringIO(exported.decode('utf-8-sig'))))
        self.assertEqual(dict(zip(table[0], table[1]))['新增牌数'], '18')
        result = self.service.commit(plan['plan_id'], config_root=self.config)
        self.assertEqual(result['status'], 'committed', result)
        written = (self.config / 'config_level.csv').read_bytes()
        self.assertTrue(written.startswith(self.original))
        self.assertEqual(Path(result['backup_path']).read_bytes(), self.original)
        self.assertEqual(self.service.commit(plan['plan_id'], config_root=self.config)['status'], 'committed')
        self.assertEqual((self.config / 'config_level.csv').read_bytes(), written)


class IncreaseParameterEvidenceTests(ApiContractCase):
    def prepare(self, *, changed_original=False, new_unknown=False, added_side=-10):
        from scripts.tower_reducer.api import Application
        from scripts.tower_reducer.parameter_flow import ParameterFlow
        self.application = Application(tool_root=self.tool_root, repository=self.repository,
            settings_path=self.settings_path, discovery_root=self.discovery_root,
            sync_service=self.sync_service, csrf_token=self.csrf)
        self.flow = ParameterFlow(self.application)
        config = self.root / 'config'
        config.mkdir(exist_ok=True)
        (config / 'config_level.csv').write_bytes(csv_bytes(FIELDS, [source_row(
            'level-source', 'sample', ['4|' + str(i) + '|100|1' for i in range(10)])]))
        raw = synthetic(second_tower=True)[0]
        tile_root = self.root / 'tile/Tile/Assets/Resources/TileMatchLevelData'
        tile_root.mkdir(parents=True, exist_ok=True)
        (tile_root / 'sample_m.bytes').write_bytes(raw)
        settings = load_settings(self.settings_path)
        settings.tile_root, settings.tile_confirmed = str(tile_root), True
        settings.config_root = str(config)
        save_settings(settings, self.settings_path)
        doc = decode_template(raw)
        doc.template_id = 'manualIncrease'
        if changed_original:
            doc.tiles[0].tile_id = -11
        output = encode_template(doc)
        output += b''.join(message(6, scalar(1, 6) + scalar(2, 16) + scalar(3, z) +
            scalar(4, added_side) + (scalar(9, 1) if new_unknown else b'')) for z in range(3))
        output += message(7, scalar(1, 6) + scalar(2, 16) + scalar(3, 0))
        (self.export_root / 'manualIncrease_m.bytes').write_bytes(output)

    def selection(self):
        return self.flow.resolve({'mode':'ids', 'source':'output',
            'template_ids':['manualIncrease'], 'source_map':{'manualIncrease':'sample'}})

    def test_explicit_source_new_mapping_proves_append_only_increase(self):
        self.prepare()
        selection = self.selection()
        self.assertEqual(selection['entries'][0].get('operation'), 'increase')
        calculated = self.flow.calculate({'selection_id':selection['selection_id']})
        self.assertEqual(calculated['rows'][0]['new_parameters'][0], '5|0|100|1')

    def test_unauthorized_original_changes_or_added_unknown_fields_cannot_claim_increase(self):
        for changes in ({'changed_original':True}, {'new_unknown':True}, {'added_side':42}):
            with self.subTest(changes=changes):
                self.prepare(**changes)
                selection = self.selection()
                self.assertEqual(selection['entries'][0]['status'], 'failed', selection)

    def test_browser_cannot_supply_operation_to_calculation_or_resolution(self):
        self.prepare()
        selection = self.selection()
        from scripts.tower_reducer.api import ApiError
        with self.assertRaises(ApiError):
            self.flow.calculate({'selection_id':selection['selection_id'], 'operation':'increase'})
        with self.assertRaises(ApiError):
            self.flow.resolve({'mode':'ids', 'template_ids':['manualIncrease'], 'operation':'increase'})

    def test_explicit_mapping_binds_selected_output_and_original_before_calculation(self):
        self.prepare()
        selected = self.selection()
        output = self.export_root / 'manualIncrease_m.bytes'
        output.write_bytes(output.read_bytes() + scalar(31, 1))
        from scripts.tower_reducer.api import ApiError
        with self.assertRaises(ApiError):
            self.flow.calculate({'selection_id':selected['selection_id']})

    def test_new_tower_on_original_without_towers_is_not_mislabeled_no_tower(self):
        self.prepare()
        source_path = Path(load_settings(self.settings_path).tile_root) / 'sample_m.bytes'
        original = decode_template(source_path.read_bytes())
        source_raw = b''.join(field.raw for field in original.fields if field.number != 7)
        source_path.write_bytes(source_raw)
        output_doc = decode_template(source_raw)
        output_doc.template_id = 'manualIncrease'
        output = encode_template(output_doc)
        output += b''.join(message(6, scalar(1, 6) + scalar(2, 16) + scalar(3, z) +
            scalar(4, -10)) for z in range(3))
        output += message(7, scalar(1, 6) + scalar(2, 16) + scalar(3, 0))
        (self.export_root / 'manualIncrease_m.bytes').write_bytes(output)
        selected = self.selection()
        self.assertEqual(selected['entries'][0]['status'], 'verified', selected)
        self.assertEqual(selected['entries'][0]['operation'], 'increase')

    def test_job_mode_claim_cannot_turn_a_reduce_artifact_into_increase_evidence(self):
        self.prepare()
        from scripts.tower_reducer.storage import InputSnapshot
        from scripts.tower_reducer.rewrite import rewrite_template
        raw, plan = synthetic(second_tower=True)
        job = self.repository.create([InputSnapshot(raw, 'sample_m.bytes')],
            parameters={'mode':'increase'})
        key = next(iter(job.snapshot()['templates']))
        artifact = job.register_output(key, rewrite_template(raw, plan, 'legacy'), plan, 'legacy')
        self.assertIsNotNone(artifact)
        selected = self.flow.resolve({'mode':'job', 'job_id':job.job_id})
        self.assertEqual(selected['entries'][0]['operation'], 'reduce')

    def test_registered_increase_plan_drives_calculation_and_historical_export(self):
        self.prepare()
        from scripts.tests.test_increase_engine import source
        from scripts.tower_reducer.increase import build_increase
        from scripts.tower_reducer.storage import InputSnapshot
        raw = source(total=108, b=39)
        built = build_increase(raw, {'target_mode':'count', 'added_tiles':18, 'fill_type':'A'}, 'moreToriginal')
        self.assertTrue(built['ok'], built.get('error'))
        job = self.repository.create([InputSnapshot(raw, 'original_m.bytes')])
        # The HTTP job creator also adds its job to the application's recent
        # registry, which is the provenance search scope for exported files.
        self.application.recent_jobs.insert(0, job)
        key = next(iter(job.snapshot()['templates']))
        artifact = job.register_output(key, built['output'], built['plan'], 'moreToriginal')
        self.assertIsNotNone(artifact, job.snapshot()['templates'][key])
        config = self.root / 'config' / 'config_level.csv'
        config.write_bytes(csv_bytes(FIELDS, [source_row('old1', 'original')]))
        selected = self.flow.resolve({'mode':'job', 'job_id':job.job_id})
        self.assertEqual(selected['entries'][0]['operation'], 'increase', selected)
        calculated = self.flow.calculate({'selection_id':selected['selection_id']})
        self.assertEqual(calculated['rows'][0]['new_parameters'][0], '14|0|100|6')
        old_output = self.export_root / 'moreToriginal_m.bytes'
        old_output.write_bytes(built['output'])
        renamed = job.activate_renamed_output(artifact['id'], 'moreToriginalv2', 'moreToriginal')
        self.assertIsNotNone(renamed)
        selected = self.flow.resolve({'mode':'ids', 'source':'output', 'template_ids':['moreToriginal']})
        self.assertEqual(selected['entries'][0]['operation'], 'increase', selected)
        calculated = self.flow.calculate({'selection_id':selected['selection_id']})
        self.assertEqual(calculated['rows'][0]['new_parameters'][0], '14|0|100|6')


if __name__ == '__main__':
    unittest.main()
