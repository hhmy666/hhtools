"""Placement diagnostics are advisory; triplet grouping remains a hard gate."""
import io
from pathlib import Path
import unittest
import zipfile

import test_repair_api as harness
from test_repair_api import CLEAN, ISSUE, board


class AdvisoryDeliveryApiTests(unittest.TestCase):
    setUp = harness.RepairApiTests.setUp
    stop = harness.RepairApiTests.stop
    request = harness.RepairApiTests.request
    poll = harness.RepairApiTests.poll
    submit = harness.RepairApiTests.submit
    row = harness.RepairApiTests.row
    export = harness.RepairApiTests.export

    def change(self, path, index=0, **change):
        row = self.row(path, index)
        row['tiles'][0].update(change)
        code, result = self.request('POST', path + '/rows/{}/edit'.format(index),
                                    {'revision': row['revision'], 'tiles': row['tiles']})
        self.assertEqual(code, 200, result)
        return result

    def preview(self, path, output, **extra):
        code, result = self.request('POST', path + '/export/preview',
                                   dict(pattern='fixed{原模板ID}', output_dir=str(output), **extra))
        self.assertEqual(code, 202, result)
        return self.poll(path + '/export/' + result['export_id'] + '/status')

    def test_fullcovers_floating_conflicts_and_outside_coordinates_save_and_export(self):
        path = self.submit([('issue_m.bytes', ISSUE)])
        row = self.row(path)
        tiles = row['tiles'] + [dict(index=None, x=30, y=-5, z=7, tile_id=-10),
                                dict(index=None, x=2, y=2, z=0, tile_id=-10),
                                dict(index=None, x=2, y=2, z=0, tile_id=-10)]
        code, saved = self.request('POST', path + '/rows/0/edit', {'revision': 0, 'tiles': tiles})
        self.assertEqual(code, 200, saved)
        self.assertTrue(saved['exportable'])
        self.assertTrue(saved['issues'])
        self.assertTrue(saved['validation_warnings'])
        self.assertFalse(any('安全范围' in x for x in saved['validation_warnings']))
        export_id, done = self.export(path)
        self.assertEqual(done['files'], ['fixissue_m.bytes'])
        code, raw, _ = self.request('GET', path + '/export/' + export_id, raw=True)
        self.assertEqual(code, 200)
        with zipfile.ZipFile(io.BytesIO(raw)) as package:
            self.assertIn(b'preserve-this-unknown-metadata', package.read('fixissue_m.bytes'))

    def test_triplet_grouping_blocks_save_without_consuming_revision(self):
        path = self.submit([('source_m.bytes', CLEAN)])
        row = self.row(path)
        bad_variants = [row['tiles'][:-1], [dict(t, tile_id=-11) if i == 0 else t
                                         for i, t in enumerate(row['tiles'])]]
        for tiles in bad_variants:
            code, result = self.request('POST', path + '/rows/0/edit', {'revision': 0, 'tiles': tiles})
            self.assertEqual(code, 422, result)
            self.assertIn('三张成组', str(result))
            self.assertEqual(self.row(path)['revision'], 0)

    def test_empty_board_can_be_saved_and_exported(self):
        path = self.submit([('source_m.bytes', CLEAN)])
        code, saved = self.request('POST', path + '/rows/0/edit', {'revision': 0, 'tiles': []})
        self.assertEqual(code, 200, saved)
        self.assertTrue(saved['exportable'])
        self.assertEqual(self.export(path)[1]['summary']['exported'], 1)

    def test_default_exports_changed_only_and_explicit_selection_is_respected(self):
        path = self.submit([('one_m.bytes', CLEAN), ('two_m.bytes', CLEAN), ('three_m.bytes', CLEAN)])
        self.change(path, 0, z=1)
        self.change(path, 2, z=2)
        _, all_new = self.export(path)
        self.assertEqual(len(all_new['files']), 2)
        self.assertEqual({r['index'] for r in all_new['skipped']}, {1})
        _, selected = self.export(path, {'indices': [2], 'pattern': 'pick{原模板ID}'})
        self.assertEqual(len(selected['files']), 1)
        original = self.row(path, 0)['original_board']['tiles']
        code, restored = self.request('POST', path + '/rows/0/edit', {'revision': 1, 'tiles': original})
        self.assertEqual(code, 200, restored)
        self.assertFalse(restored['edited'])
        self.assertFalse(restored['exportable'])

    def test_preview_is_readonly_then_confirm_writes_same_names_to_three_targets(self):
        path = self.submit([('source_m.bytes', CLEAN)])
        self.change(path, z=1)
        output, template, tile = [self.root / name for name in ('out', 'template', 'tile')]
        preview = self.preview(path, output, sync_targets=['template', 'tile'],
                               template_dir=str(template), tile_dir=str(tile))
        self.assertEqual(preview['status'], 'succeeded', preview)
        self.assertEqual(preview['mode'], 'preview')
        self.assertFalse(output.exists())
        code, writing = self.request('POST', path + '/export/' + preview['export_id'] + '/write', {})
        self.assertEqual(code, 202, writing)
        done = self.poll(path + '/export/' + preview['export_id'] + '/status')
        self.assertEqual(done['status'], 'succeeded', done)
        self.assertEqual((done['completed'], done['total']), (3, 3))
        paths = [root / 'fixedsource_m.bytes' for root in (output, template, tile)]
        self.assertTrue(all(p.exists() for p in paths))
        self.assertEqual(len({p.read_bytes() for p in paths}), 1)
        self.assertTrue((self.root / 'clean_m.bytes').exists())

    def test_saved_change_invalidates_preview_before_any_writes(self):
        path = self.submit([('source_m.bytes', CLEAN)])
        self.change(path, z=1)
        output = self.root / 'not-created'
        preview = self.preview(path, output)
        self.change(path, x=3)
        code, result = self.request('POST', path + '/export/' + preview['export_id'] + '/write', {})
        self.assertEqual(code, 409, result)
        self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
