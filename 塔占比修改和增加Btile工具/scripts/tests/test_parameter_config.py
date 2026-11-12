"""Hand-checked parameter calculations and append-only local CSV transactions."""
import csv
import importlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
import zipfile


FIELDS = ['LevelId', 'Des', 'TemplateId', 'RandomType'] + [f'DupPara{i}' for i in range(1, 11)] + ['StrategyGroupId', 'randomSeed_groupID', 'FutureColumn']
LIST_FIELDS = ['LevelIndex', 'DDAType', 'LevelId', 'DDALevelId1', 'DDALevelId2', 'DDALevelId3']


def csv_bytes(fields, rows, *, bom=True, newline='\r\n'):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator=newline)
    writer.writerow(fields)
    writer.writerow(['string'] * len(fields))
    writer.writerow(['字段说明'] * len(fields))
    writer.writerows(rows)
    return (b'\xef\xbb\xbf' if bom else b'') + stream.getvalue().encode('utf-8')


def source_row(level_id='old1', template_id='tpl', parameters=None, description='原备注,含"引号"中文'):
    return [level_id, description, template_id, '1'] + (parameters or ['11|0|100|6', '10|7|99|5'] + ['9|8|80|4'] * 8) + ['17', '23', '未知,字段']


def entry(source=(69, 39), result=(51, 57), **kwargs):
    return {'original_template_id': 'tpl', 'new_template_id': 'lessTtpl',
            'source_tile_counts': dict(zip(('A', 'B'), source)),
            'result_tile_counts': dict(zip(('A', 'B'), result)), 'status': 'verified', **kwargs}


class ParameterConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config'
        self.config.mkdir()
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()
        try:
            self.module = importlib.import_module('scripts.tower_reducer.parameter_config')
        except ModuleNotFoundError:
            self.fail('ParameterConfigService is not implemented yet')
        self.service = self.module.ParameterConfigService(self.runtime)
        self.write_config([source_row()])

    def write_config(self, rows, list_rows=None, **kwargs):
        self.raw = csv_bytes(FIELDS, rows, **kwargs)
        (self.config / 'config_level.csv').write_bytes(self.raw)
        list_rows = list_rows or [['20', '0', 'old1', '', '', '']]
        (self.config / 'config_level_list.csv').write_bytes(csv_bytes(LIST_FIELDS, list_rows))

    def calculate(self, entries=None, **kwargs):
        return self.service.calculate(self.config, entries or [entry()], **kwargs)

    def preview(self, calculated=None, **kwargs):
        calculated = calculated or self.calculate()
        return self.service.preview(calculated['calculation_id'], [r['row_id'] for r in calculated['rows']], config_root=self.config, **kwargs)

    def test_conversion_uses_first_delta_for_all_duplicates_and_preserves_seed_luck(self):
        result = self.calculate()
        row = result['rows'][0]
        self.assertEqual(row['new_parameters'], ['8|0|100|9', '7|7|99|8', '6|8|80|7']
            + ['6|8|80|6'] * 2 + ['6|8|80|5'] * 2 + ['6|8|80|4'] * 2 + ['6|8|80|3'])
        self.assertEqual(row['delta'], {'A': -3, 'B': 3})
        self.assertEqual(row['status'], 'ready')
        self.assertEqual(row['new_level_id'], 'lessTold1')
        self.assertEqual(row['description'], '原备注,含"引号"中文')
        self.assertEqual(row['RandomType'], '1')
        self.assertEqual(row['StrategyGroupId'], '17')
        self.assertEqual(row['randomSeed_groupID'], '23')
        self.assertEqual(len(result['comparisons']), 2)
        self.assertEqual([r['version'] for r in result['comparisons']], ['原参数', '新参数'])
        self.assertEqual((self.config / 'config_level.csv').read_bytes(), self.raw)

    def test_recalculated_b_decreases_every_two_duplicates_starting_at_four(self):
        self.write_config([source_row(parameters=['7|0|100|5'] * 10)])
        row = self.calculate([entry(source=(60, 60), result=(72, 48))])['rows'][0]
        self.assertEqual(row['new_parameters'], ['8|0|100|4'] * 3 + ['8|0|100|3'] * 7)
        self.write_config([source_row(parameters=['10|7|99|5'] * 10)])
        row = self.calculate([entry(source=(60, 60), result=(48, 72))])['rows'][0]
        self.assertEqual(row['new_parameters'], ['8|7|99|6'] * 3 + ['8|7|99|5'] * 2
            + ['8|7|99|4'] * 2 + ['8|7|99|3'] * 3)

    def test_b_taper_does_not_raise_low_values_or_change_an_unchanged_b_side(self):
        for b in (1, 2):
            self.write_config([source_row(parameters=[f'7|0|100|{b}'] * 10)])
            row = self.calculate([entry(source=(60, 60), result=(72, 48))])['rows'][0]
            self.assertEqual(row['new_parameters'], [f'8|0|100|{b}'] * 10)
        self.write_config([source_row(parameters=['7|0|100|5'] * 10)])
        row = self.calculate([entry(source=(60, 60), result=(72, 60), operation='increase')])['rows'][0]
        self.assertEqual(row['new_parameters'], ['8|0|100|5'] * 10)

    def test_b_taper_is_shared_by_reduce_increase_manual_and_ordinary_add(self):
        self.write_config([source_row(parameters=['10|0|100|5'] * 10)])
        for operation, mode in [('reduce', 'reduce'), ('increase', 'increase'),
                ('increase', 'ordinary_add'), ('manual', 'manual')]:
            with self.subTest(mode=mode):
                counts = (48, 72) if operation == 'reduce' else (60, 72)
                row = self.calculate([entry(source=(60, 60), result=counts,
                    operation=operation, template_mode=mode)])['rows'][0]
                self.assertEqual(row['status'], 'ready', row['message'])
                self.assertEqual([int(value.split('|')[3]) for value in row['new_parameters']],
                    [6, 6, 6, 5, 5, 4, 4, 3, 3, 3])

    def test_same_type_move_keeps_all_ten_verbatim_and_can_append_copy(self):
        values = ['011|000|100|006'] * 10
        self.write_config([source_row(parameters=values)])
        calculated = self.calculate([entry(result=(69, 39))])
        self.assertEqual(calculated['rows'][0]['new_parameters'], values)
        self.assertEqual(calculated['rows'][0]['status'], 'unchanged')
        self.assertEqual(calculated['comparisons'], [])
        plan = self.preview(calculated)
        self.assertEqual(plan['status'], 'ready')
        self.assertEqual(plan['summary']['append_count'], 1)

    def test_preserved_level_prefix_uses_each_original_row_and_new_template(self):
        self.write_config([
            source_row('list482r1v3zm0078v2plus1', 'zm0078v2plus1'),
            source_row('list483r2zm0078v2plus1', 'zm0078v2plus1')])
        calculated = self.calculate([entry(original_template_id='zm0078v2plus1',
            new_template_id='newTemplate')], preserve_level_prefix=True)
        self.assertEqual([r['new_level_id'] for r in calculated['rows']],
            ['list482r1v3newTemplate', 'list483r2newTemplate'])
        plan = self.preview(calculated)
        self.assertEqual([r['new_level_id'] for r in plan['rows']],
            ['list482r1v3newTemplate', 'list483r2newTemplate'])
        self.assertEqual(plan['summary']['append_count'], 2)
        default_plan = self.preview(calculated, preserve_level_prefix=False)
        self.assertEqual(default_plan['rows'][0]['new_level_id'], 'lessTlist482r1v3zm0078v2plus1')
        self.assertEqual((self.config / 'config_level.csv').read_bytes(), self.raw)

    def test_preserved_level_prefix_versions_existing_and_batch_collisions(self):
        self.write_config([source_row('listAoldA', 'oldA'), source_row('listAoldB', 'oldB'),
            source_row('listAnew', 'unrelated'), source_row('LISTAnewv2', 'unrelated')])
        calculated = self.calculate([entry(original_template_id='oldA', new_template_id='new'),
            entry(original_template_id='oldB', new_template_id='new')], preserve_level_prefix=True)
        self.assertEqual([r['new_level_id'] for r in calculated['rows']], ['listAnewv3', 'listAnewv4'])
        plan = self.preview(calculated)
        self.assertEqual([r['new_level_id'] for r in plan['rows']], ['listAnewv3', 'listAnewv4'])
        self.assertEqual(plan['summary']['append_count'], 2)

    def test_preserved_list_prefix_allows_historical_template_version_in_level_id(self):
        self.write_config([
            source_row('list417r1v3zm0078v2plus1', 'zm0078v2plus6v1'),
            source_row('list150v2zm0078v2plus1', 'zm0078v2plus6v1')])
        calculated = self.calculate([entry(original_template_id='zm0078v2plus6v1',
            new_template_id='newTemplate')], preserve_level_prefix=True)
        self.assertEqual([r['status'] for r in calculated['rows']], ['ready', 'ready'])
        self.assertEqual([r['new_level_id'] for r in calculated['rows']],
            ['list417r1v3newTemplate', 'list150v2newTemplate'])
        self.assertEqual([r['new_level_id'] for r in self.preview(calculated)['rows']],
            ['list417r1v3newTemplate', 'list150v2newTemplate'])

    def test_preserved_list_prefix_rejects_empty_or_invalid_template_tail(self):
        for original_level in ('list417r1v3', 'list150v2bad!template', 'customOldVersion'):
            self.write_config([source_row(original_level)])
            row = self.calculate(preserve_level_prefix=True)['rows'][0]
            self.assertEqual(row['status'], 'failed', original_level)

    def test_preserve_prefix_can_be_switched_in_preview_and_invalid_suffix_is_explained(self):
        self.write_config([source_row('listAtpl')])
        calculated = self.calculate()
        self.assertEqual(calculated['rows'][0]['new_level_id'], 'lessTlistAtpl')
        self.assertEqual(self.preview(calculated, preserve_level_prefix=True)['rows'][0]['new_level_id'],
            'listAlessTtpl')
        self.write_config([source_row('unrelated')])
        row = self.calculate(preserve_level_prefix=True)['rows'][0]
        self.assertEqual(row['status'], 'failed')
        self.assertIn('原模板ID', row['message'])

    def test_preserve_prefix_requires_boolean(self):
        with self.assertRaises(ValueError):
            self.calculate(preserve_level_prefix='true')
        with self.assertRaises(ValueError):
            self.preview(preserve_level_prefix=1)

    def test_same_type_move_keeps_inactive_side_kind_values_verbatim(self):
        values = ['011|000|100|006', '50|7|99|6'] + ['9|8|80|6'] * 8
        self.write_config([source_row(parameters=values)])
        row = self.calculate([entry(source=(69, 0), result=(69, 0))])['rows'][0]
        self.assertEqual(row['status'], 'unchanged')
        self.assertEqual(row['new_parameters'], values)
        plan = self.preview(self.calculate([entry(source=(69, 0), result=(69, 0))]))
        self.assertEqual(plan['summary']['append_count'], 1)

    def test_conversion_validates_new_capacity_without_declaring_old_kinds_invalid(self):
        self.write_config([source_row(parameters=['4|0|100|2'] * 10)])
        row = self.calculate([entry(source=(9, 9), result=(3, 15))])['rows'][0]
        self.assertEqual(row['status'], 'ready')
        self.assertEqual(row['new_parameters'], ['1|0|100|3'] * 10)
        self.assertEqual(row['delta'], {'A': -3, 'B': 1})

    def test_half_up_is_exact_and_reverse_conversion_supported(self):
        self.write_config([source_row(parameters=['3|2|77|3'] * 10)])
        row = self.calculate([entry(source=(18, 18), result=(15, 21))])['rows'][0]
        self.assertEqual(row['new_parameters'], ['3|2|77|4'] * 3 + ['3|2|77|3'] * 7)
        self.write_config([source_row(parameters=['8|3|90|9'] * 10)])
        row = self.calculate([entry(source=(51, 57), result=(69, 39))])['rows'][0]
        self.assertEqual(row['new_parameters'], ['11|3|90|6'] * 3 + ['11|3|90|5'] * 2
            + ['11|3|90|4'] * 2 + ['11|3|90|3'] * 3)
        self.assertEqual(row['delta'], {'A': 3, 'B': -3})

    def test_links_every_level_and_uses_only_active_list_branches(self):
        self.write_config([source_row('old1'), source_row('old2')], [
            ['20', '0', 'old1|old2', 'unused', '', ''],
            ['21', '1', 'inactive', 'old1; old2', 'old1', 'old2'],
            ['21', '1', '', 'old1', '', ''],
            ['22', '0', 'unrelated', 'old1', '', '']])
        rows = self.calculate()['rows']
        self.assertEqual(len(rows), 2)
        self.assertEqual([r['level_indexes'] for r in rows], [[20, 21], [20, 21]])
        filtered = self.calculate(level_start=21, level_end=21)['rows']
        self.assertEqual([r['level_indexes'] for r in filtered], [[21], [21]])
        self.assertEqual(self.calculate(level_start=22, level_end=22)['rows'], [])

    def test_illegal_parameters_report_row_failure_without_clamping(self):
        self.write_config([source_row('good'), source_row('bad', parameters=['11|0|100|6'] + ['1|0|100|1'] * 9)])
        rows = self.calculate()['rows']
        self.assertEqual([r['status'] for r in rows], ['ready', 'failed'])
        self.assertEqual(rows[1]['new_parameters'], [])
        self.assertIn('DupPara2', rows[1]['message'])
        self.assertIn('负', rows[1]['message'])

    def test_zero_source_a_nonempty_output_and_capacity_fail_explicitly(self):
        self.write_config([source_row(parameters=['0|0|100|8'] * 10)])
        row = self.calculate([entry(source=(0, 24), result=(3, 21))])['rows'][0]
        self.assertEqual(row['status'], 'failed')
        self.assertIn('原 A', row['message'])
        self.write_config([source_row(parameters=['24|0|100|6'] * 10)])
        row = self.calculate()['rows'][0]
        self.assertEqual(row['status'], 'failed')
        self.assertIn('容量', row['message'])

    def test_new_b_uses_new_a_groups_per_pattern_ceiling_minus_one_with_minimum_three(self):
        self.write_config([source_row(parameters=['19|0|100|0'] * 10)])
        row = self.calculate([entry(source=(144, 0), result=(114, 30))])['rows'][0]
        self.assertEqual(row['status'], 'ready')
        self.assertEqual(row['new_parameters'], ['15|0|100|3'] * 10)
        row = self.calculate([entry(source=(144, 0), result=(84, 60))])['rows'][0]
        self.assertEqual(row['status'], 'ready')
        self.assertEqual(row['new_parameters'], ['11|0|100|7'] * 3 + ['11|0|100|6'] * 2
            + ['11|0|100|5'] * 2 + ['11|0|100|4'] * 2 + ['11|0|100|3'])
        self.write_config([source_row(parameters=['4|0|100|0'] * 10)])
        row = self.calculate([entry(source=(108, 0), result=(78, 30))])['rows'][0]
        self.assertEqual(row['new_parameters'], ['3|0|100|3'] * 10)

    def test_new_b_rule_applies_to_every_mode_and_minimum_to_every_duplicate(self):
        self.write_config([source_row(parameters=['19|0|100|5'] + ['19|0|100|1'] * 9)])
        for operation, mode in [('reduce', 'reduce'), ('increase', 'increase'),
                ('increase', 'ordinary_add'), ('manual', 'manual')]:
            counts = (114, 30) if operation != 'increase' else (144, 30)
            row = self.calculate([entry(source=(144, 0), result=counts,
                operation=operation, template_mode=mode)])['rows'][0]
            self.assertEqual(row['status'], 'ready', row['message'])
            self.assertTrue(all(int(value.split('|')[3]) >= 3 for value in row['new_parameters']))
            self.assertEqual(row['new_parameters'][0].split('|')[3], '3')

    def test_new_b_minimum_requires_nine_tiles_and_new_a_basis(self):
        self.write_config([source_row(parameters=['10|0|100|0'] * 10)])
        row = self.calculate([entry(source=(108, 0), result=(102, 6))])['rows'][0]
        self.assertEqual(row['status'], 'failed')
        self.assertIn('至少需要9', row['message'])
        row = self.calculate([entry(source=(108, 0), result=(0, 108))])['rows'][0]
        self.assertEqual(row['status'], 'failed')
        self.assertIn('新A', row['message'])

    def test_nonconserved_or_nonmultiple_tile_counts_are_failures(self):
        for source, result in [((69, 39), (51, 54)), ((70, 38), (51, 57))]:
            with self.subTest(source=source, result=result):
                row = self.calculate([entry(source=source, result=result)])['rows'][0]
                self.assertEqual(row['status'], 'failed')
                self.assertEqual(row['new_parameters'], [])

    def test_no_tower_failed_and_unmapped_have_no_invented_parameters(self):
        result = self.calculate([entry(status='no_tower'), entry(status='failed', message='验收失败'), entry(original_template_id='missing')])
        self.assertEqual([r['status'] for r in result['rows']], ['no_tower', 'failed', 'failed'])
        self.assertEqual(result['rows'][2]['original_parameters'], [])
        plan = self.preview(result)
        self.assertEqual(plan['summary']['append_count'], 0)
        self.assertEqual((self.config / 'config_level.csv').read_bytes(), self.raw)

    def test_duplicate_source_level_is_blocking_and_never_chooses_one(self):
        self.write_config([source_row(), source_row(parameters=['12|0|100|6'] * 10)])
        rows = self.calculate()['rows']
        self.assertTrue(rows)
        self.assertTrue(all(r['status'] == 'failed' for r in rows))
        self.assertTrue(all('重复' in r['message'] for r in rows))

    def test_preview_rule_des_override_and_unknown_fields_copy(self):
        calculated = self.calculate()
        plan = self.preview(calculated, level_id_rule='pre{原关卡ID}_{新模板ID}_post', description='')
        self.assertEqual(plan['rows'][0]['new_level_id'], 'preold1_lessTtpl_post')
        self.assertEqual(plan['rows'][0]['description'], '')
        self.assertEqual((self.config / 'config_level.csv').read_bytes(), self.raw)
        result = self.service.commit(plan['plan_id'], config_root=self.config)
        self.assertEqual(result['status'], 'committed')
        table = list(csv.reader(io.StringIO((self.config / 'config_level.csv').read_text(encoding='utf-8-sig'), newline='')))
        self.assertEqual(table[-1][0:4], ['preold1_lessTtpl_post', '', 'lessTtpl', '1'])
        self.assertEqual(table[-1][-3:], ['17', '23', '未知,字段'])

    def test_commit_preserves_old_bytes_bom_crlf_backup_and_retry(self):
        plan = self.preview()
        list_raw = (self.config / 'config_level_list.csv').read_bytes()
        result = self.service.commit(plan['plan_id'], config_root=self.config)
        raw = (self.config / 'config_level.csv').read_bytes()
        self.assertEqual(result['status'], 'committed')
        self.assertTrue(raw.startswith(self.raw))
        self.assertEqual(Path(result['backup_path']).read_bytes(), self.raw)
        self.assertTrue(Path(result['backup_path']).is_relative_to(self.runtime))
        self.assertEqual((self.config / 'config_level_list.csv').read_bytes(), list_raw)
        self.assertEqual(self.service.commit(plan['plan_id'], config_root=self.config)['status'], 'committed')
        self.assertEqual((self.config / 'config_level.csv').read_bytes(), raw)

    def test_existing_identical_skips_and_conflicting_or_colliding_ids_block(self):
        original = source_row()
        same = source_row('lessTold1', 'lessTtpl', ['8|0|100|9', '7|7|99|8', '6|8|80|7']
            + ['6|8|80|6'] * 2 + ['6|8|80|5'] * 2 + ['6|8|80|4'] * 2 + ['6|8|80|3'])
        self.write_config([original, same])
        plan = self.preview()
        self.assertEqual(plan['summary']['skip_count'], 1)
        self.assertEqual(plan['summary']['append_count'], 0)
        same[1] = 'different'
        self.write_config([original, same])
        plan = self.preview()
        self.assertEqual(plan['status'], 'blocked')
        self.assertEqual(self.service.commit(plan['plan_id'], config_root=self.config)['status'], 'error')
        self.write_config([source_row('old1'), source_row('old2', description='different')])
        plan = self.preview(level_id_rule='{新模板ID}')
        self.assertEqual(plan['status'], 'blocked')
        self.assertTrue(all(r['status'] == 'blocked' for r in plan['rows']))

    def test_commit_rejects_changed_file_root_identity_and_symlink(self):
        plan = self.preview()
        (self.config / 'config_level.csv').write_bytes(self.raw + b'\r\n')
        self.assertEqual(self.service.commit(plan['plan_id'], config_root=self.config)['status'], 'error')
        self.write_config([source_row()])
        plan = self.preview()
        alternate = self.root / 'alternate'
        alternate.mkdir()
        (alternate / 'config_level.csv').write_bytes(self.raw)
        (alternate / 'config_level_list.csv').write_bytes((self.config / 'config_level_list.csv').read_bytes())
        self.assertEqual(self.service.commit(plan['plan_id'], config_root=alternate)['status'], 'error')
        moved = self.root / 'moved'
        self.config.rename(moved)
        self.config.mkdir()
        self.write_config([source_row()])
        self.assertEqual(self.service.commit(plan['plan_id'], config_root=self.config)['status'], 'error')
        (self.config / 'config_level.csv').unlink()
        (self.config / 'config_level.csv').symlink_to(moved / 'config_level.csv')
        with self.assertRaises(ValueError):
            self.calculate()

    def test_root_csv_selection_export_and_restart_preserve_plan(self):
        wrapper = self.root / 'repository'
        wrapper.mkdir()
        self.config.rename(wrapper / 'csv')
        self.config = wrapper
        calculated = self.calculate()
        self.assertEqual(calculated['config_path'], str(wrapper / 'csv' / 'config_level.csv'))
        payload, mime, filename = self.service.export(calculated['calculation_id'])
        self.assertEqual(mime, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        self.assertTrue(filename.endswith('.xlsx'))
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertIn('xl/worksheets/sheet1.xml', archive.namelist())
            self.assertIn('xl/worksheets/sheet2.xml', archive.namelist())
            self.assertIn('原模板ID', archive.read('xl/worksheets/sheet1.xml').decode())
            self.assertIn('DupPara10', archive.read('xl/worksheets/sheet2.xml').decode())
            self.assertIn('思源黑体', archive.read('xl/styles.xml').decode())
        csv_payload, csv_mime, _ = self.service.export(calculated['calculation_id'], format='csv')
        self.assertTrue(csv_payload.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(csv_mime, 'text/csv; charset=utf-8')
        plan = self.preview(calculated)
        restarted = self.module.ParameterConfigService(self.runtime)
        self.assertEqual(restarted.commit(plan['plan_id'], config_root=self.config)['status'], 'committed')

    def test_post_write_fsync_failure_reports_written_and_retry_recovers(self):
        plan = self.preview()
        original_fsync = os.fsync

        def fail_after_replace(fd):
            # Real files and writes; only the filesystem failure is injected.
            if (self.config / 'config_level.csv').read_bytes() != self.raw:
                raise OSError('simulated directory fsync failure')
            return original_fsync(fd)

        with patch.object(self.module.os, 'fsync', side_effect=fail_after_replace):
            failed = self.service.commit(plan['plan_id'], config_root=self.config)
        self.assertEqual(failed['status'], 'error')
        self.assertTrue(failed['written'])
        once = (self.config / 'config_level.csv').read_bytes()
        recovered = self.service.commit(plan['plan_id'], config_root=self.config)
        self.assertEqual(recovered['status'], 'committed')
        self.assertEqual((self.config / 'config_level.csv').read_bytes(), once)

    def test_directory_replaced_after_write_reports_real_write_and_identity_error(self):
        plan = self.preview()
        original_replace = os.replace
        displaced = self.root / 'displaced'

        def replace_and_move(source, target, **kwargs):
            result = original_replace(source, target, **kwargs)
            if target == 'config_level.csv':
                self.config.rename(displaced)
                self.config.mkdir()
                self.write_config([source_row()])
            return result

        with patch.object(self.module.os, 'replace', side_effect=replace_and_move):
            failed = self.service.commit(plan['plan_id'], config_root=self.config)
        self.assertEqual(failed['status'], 'error')
        self.assertTrue(failed['written'])
        self.assertIn('身份', failed['message'])
        self.assertNotEqual((displaced / 'config_level.csv').read_bytes(), self.raw)

    def test_lf_without_bom_or_trailing_newline_preserves_exact_prefix(self):
        self.write_config([source_row()], bom=False, newline='\n')
        self.raw = self.raw.rstrip(b'\n')
        (self.config / 'config_level.csv').write_bytes(self.raw)
        plan = self.preview()
        result = self.service.commit(plan['plan_id'], config_root=self.config)
        self.assertEqual(result['status'], 'committed')
        raw = (self.config / 'config_level.csv').read_bytes()
        self.assertTrue(raw.startswith(self.raw + b'\n'))
        self.assertNotIn(b'\r', raw)
        self.assertFalse(raw.startswith(b'\xef\xbb\xbf'))

    def test_calculation_file_changes_block_preview_and_list_changes_block_commit(self):
        calculated = self.calculate()
        (self.config / 'config_level.csv').write_bytes(self.raw + b'\r\n')
        with self.assertRaises(ValueError):
            self.preview(calculated)
        self.write_config([source_row()])
        plan = self.preview()
        list_path = self.config / 'config_level_list.csv'
        list_path.write_bytes(list_path.read_bytes() + b'\r\n')
        self.assertEqual(self.service.commit(plan['plan_id'], config_root=self.config)['status'], 'error')
        self.assertEqual((self.config / 'config_level.csv').read_bytes(), self.raw)

    def test_nonempty_side_zero_kinds_and_fractional_counts_are_not_guessed(self):
        self.write_config([source_row(parameters=['0|0|100|6'] * 10)])
        row = self.calculate()['rows'][0]
        self.assertEqual(row['status'], 'failed')
        self.assertIn('不能为0', row['message'])
        self.write_config([source_row()])
        row = self.calculate([entry(source=(69.0, 39))])['rows'][0]
        self.assertEqual(row['status'], 'failed')

    def test_blank_original_level_id_is_never_given_new_default_id(self):
        self.write_config([source_row(level_id='')])
        row = self.calculate()['rows'][0]
        self.assertEqual(row['status'], 'failed')
        self.assertIn('LevelId', row['message'])

    def test_no_tower_without_output_id_retains_source_information_and_is_excluded(self):
        result = self.calculate([entry(status='no_tower', new_template_id=None)])
        row = result['rows'][0]
        self.assertEqual(row['status'], 'no_tower')
        self.assertEqual(row['original_level_id'], 'old1')
        self.assertEqual(row['original_template_id'], 'tpl')
        self.assertEqual(row['new_template_id'], '')
        self.assertEqual(row['new_level_id'], '')
        self.assertEqual(row['new_parameters'], [])
        plan = self.preview(result)
        self.assertEqual(plan['status'], 'empty')
        self.assertEqual(plan['summary']['append_count'], 0)

    def test_unreleased_template_without_index_table_calculates_and_appends_exact_match(self):
        (self.config / 'config_level_list.csv').unlink()
        result = self.calculate()
        self.assertEqual(result['rows'][0]['status'], 'ready')
        self.assertEqual(result['rows'][0]['level_indexes'], [])
        plan = self.preview(result)
        self.assertEqual(self.service.commit(plan['plan_id'], config_root=self.config)['status'], 'committed')
        self.assertFalse((self.config / 'config_level_list.csv').exists())

    def test_missing_index_with_requested_range_has_specific_actionable_error(self):
        (self.config / 'config_level_list.csv').unlink()
        with self.assertRaisesRegex(ValueError, '补齐.*索引|清空.*范围'):
            self.calculate(level_start=20)

    def test_index_file_added_or_deleted_invalidates_existing_binding(self):
        list_path = self.config / 'config_level_list.csv'
        list_raw = list_path.read_bytes()
        list_path.unlink()
        calculated = self.calculate()
        list_path.write_bytes(list_raw)
        with self.assertRaisesRegex(ValueError, 'config_level_list'):
            self.preview(calculated)
        plan = self.preview()
        list_path.unlink()
        failed = self.service.commit(plan['plan_id'], config_root=self.config)
        self.assertEqual(failed['status'], 'error')
        self.assertIn('config_level_list', failed['message'])
        self.assertEqual((self.config / 'config_level.csv').read_bytes(), self.raw)

    def test_preview_null_description_keeps_csv_original_after_calculation_override(self):
        calculated = self.calculate(description='计算页临时备注')
        plan = self.preview(calculated, description=None)
        self.assertEqual(plan['rows'][0]['description'], '原备注,含"引号"中文')

    def test_exports_use_chinese_statuses_and_level_number_label(self):
        result = self.calculate([entry(), entry(result=(69, 39)),
                                 entry(status='no_tower', new_template_id=None), entry(status='failed')])
        payload, _, _ = self.service.export(result['calculation_id'], format='csv')
        table = list(csv.reader(io.StringIO(payload.decode('utf-8-sig'), newline='')))
        self.assertEqual(table[0][0], '关卡号')
        status_column = table[0].index('状态')
        self.assertEqual([r[status_column] for r in table[1:]], ['可追加', '参数不变', '无塔', '失败'])
        payload, _, _ = self.service.export(result['calculation_id'])
        ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
            rows = sheet.findall('s:sheetData/s:row', ns)
            values = [[c.find('s:is/s:t', ns).text for c in r.findall('s:c', ns) if c.find('s:is/s:t', ns) is not None] for r in rows]
            self.assertEqual(values[0][0], '关卡号')
            self.assertTrue(all(label in str(values) for label in ('可追加', '参数不变', '无塔', '失败')))

    def test_xlsx_real_styles_fit_ids_parameters_notes_and_distinguish_comparison_rows(self):
        self.write_config([source_row(description='较长备注需要能够自动换行完整阅读，' * 12)])
        result = self.calculate()
        payload, _, _ = self.service.export(result['calculation_id'])
        ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            styles = ET.fromstring(archive.read('xl/styles.xml'))
            xfs = styles.findall('s:cellXfs/s:xf', ns)
            fills = styles.findall('s:fills/s:fill', ns)
            for name in ('xl/worksheets/sheet1.xml', 'xl/worksheets/sheet2.xml'):
                sheet = ET.fromstring(archive.read(name))
                table_rows = sheet.findall('s:sheetData/s:row', ns)
                headers = [c.find('s:is/s:t', ns).text for c in table_rows[0].findall('s:c', ns)]
                widths = {}
                for col in sheet.findall('s:cols/s:col', ns):
                    for index in range(int(col.attrib['min']), int(col.attrib['max']) + 1):
                        widths[headers[index - 1]] = float(col.attrib['width'])
                for heading in ('原关卡ID', '新关卡ID', '原模板ID', '新模板ID'):
                    self.assertEqual(widths[heading], 32)
                self.assertIn(widths['DupPara1'], (18, 20))
                self.assertNotEqual(len(set(widths.values())), 1)
                self.assertEqual(sheet.find('s:sheetViews/s:sheetView/s:pane', ns).attrib['state'], 'frozen')
                self.assertEqual(sheet.find('s:sheetViews/s:sheetView/s:pane', ns).attrib['ySplit'], '1')
                self.assertIsNotNone(sheet.find('s:autoFilter', ns))
                header_style = xfs[int(table_rows[0].find('s:c', ns).attrib['s'])]
                header_fill = fills[int(header_style.attrib['fillId'])].find('s:patternFill', ns)
                self.assertEqual(header_fill.attrib['patternType'], 'solid')
                self.assertIsNotNone(header_fill.find('s:fgColor', ns))
                self.assertGreaterEqual(float(table_rows[0].attrib['ht']), 28)
                if name.endswith('sheet1.xml'):
                    self.assertGreaterEqual(widths['说明'], 48)
                    note_style = xfs[int(table_rows[1].findall('s:c', ns)[12].attrib['s'])]
                    self.assertEqual(note_style.find('s:alignment', ns).attrib['wrapText'], '1')
                    self.assertGreater(float(table_rows[1].attrib['ht']), 28)
                else:
                    old_style = xfs[int(table_rows[1].find('s:c', ns).attrib['s'])]
                    new_style = xfs[int(table_rows[2].find('s:c', ns).attrib['s'])]
                    self.assertNotEqual(old_style.attrib['fillId'], new_style.attrib['fillId'])
                    self.assertEqual(headers[0], '关卡号')


if __name__ == '__main__':
    unittest.main()
