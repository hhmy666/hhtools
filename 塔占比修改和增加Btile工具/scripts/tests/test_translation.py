"""Translation revisions preserve every wire field outside effective X/Y."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from scripts.tests.test_api import FakeSyncService
from scripts.tests.test_increase_engine import source
from scripts.tests.test_rewrite import synthetic
from scripts.tower_reducer.api import Application, ApiError
from scripts.tower_reducer.export_service import export_outputs, plan_exports, TargetChanged
from scripts.tower_reducer.path_binding import TEMPLATE_SUFFIX
from scripts.tower_reducer.increase import build_increase
from scripts.tower_reducer.ordinary_add import build_ordinary_add
from scripts.tower_reducer.preview import comparison
from scripts.tower_reducer.rewrite import rewrite_template
from scripts.tower_reducer.settings import LocalSettings, save_settings
from scripts.tower_reducer.storage import InputSnapshot, JobRepository, _pack, _unpack
from scripts.tower_reducer.sync_service import SyncService
from scripts.tower_reducer.template_reader import decode_template
from scripts.tower_reducer.translation import (TranslationPlan, translate_bytes, translation_bounds,
                                               verify_translation)
from scripts.tower_reducer.verification import verify_output


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.tool = self.root / 'tool'
        self.tool.mkdir()
        self.settings = self.root / 'settings.json'
        save_settings(LocalSettings(), self.settings)
        self.repo = JobRepository(self.root / 'jobs')
        self.app = Application(tool_root=self.tool, repository=self.repo,
            settings_path=self.settings, discovery_root=self.root, sync_service=FakeSyncService())

    def prepare(self, raw=None, plan=None):
        if raw is None:
            raw, plan = synthetic()
        job = self.repo.create([InputSnapshot(raw, 'input_m.bytes')])
        key = next(iter(job.snapshot()['templates']))
        baseline = rewrite_template(raw, plan, 'new')
        artifact = job.register_output(key, baseline, plan, 'new')
        self.assertIsNotNone(artifact, job.snapshot())
        job.update_template(key, plan_summary={'operation': 'reduce', 'new_id': 'new', 'custom': 'preserved'})
        job.set_status('succeeded')
        self.app.recent_jobs.append(job)
        return job, key, artifact, baseline

    def move(self, job, key, artifact, dx=1, dy=1):
        return self.app.translate_template(job.job_id, key, dict(artifact_id=artifact['id'], dx=dx, dy=dy))

    def assert_shift(self, before, after, dx, dy):
        original, shifted = decode_template(before), decode_template(after)
        for left, right in zip((*original.tiles, *original.stacks), (*shifted.tiles, *shifted.stacks)):
            self.assertEqual((right.x, right.y), (left.x + dx, left.y + dy))
            for name in ('z', 'tile_id', 'direction'):
                self.assertEqual(getattr(left, name, None), getattr(right, name, None))
            self.assertEqual([f.raw for f in left.fields if f.number not in (1, 2)],
                             [f.raw for f in right.fields if f.number not in (1, 2)])

    def test_revision_stales_previous_capability_and_reset_is_exact(self):
        job, key, artifact, baseline = self.prepare()
        original_source = job.read_source(key)
        preview = self.move(job, key, artifact)
        current = preview['artifact']
        self.assertNotEqual(current['id'], artifact['id'])
        self.assertEqual(preview['artifact_id'], current['id'])
        self.assertEqual(preview['translation']['dx'], 1)
        self.assert_shift(baseline, job.read_artifact(current['id']), 1, 1)
        with self.assertRaises(KeyError):
            job.read_artifact(artifact['id'])
        with self.assertRaises(ApiError) as rejected:
            self.move(job, key, artifact)
        self.assertEqual(rejected.exception.code, 'stale_artifact')
        reset = self.move(job, key, current, 0, 0)
        self.assertEqual(job.read_artifact(reset['artifact']['id']), baseline)
        self.assertTrue(job.snapshot()['templates'][key]['plan_summary']['adjusted'])
        self.assertEqual(job.read_source(key), original_source)
        self.assertEqual(job.snapshot()['templates'][key]['plan_summary']['custom'], 'preserved')

    def test_bottom_reduction_preview_normalizes_original_z_through_manual_wrapper(self):
        from scripts.tests.test_ordinary_add_engine import source as ordinary_source
        from scripts.tower_reducer.distribution import DistributionOptions, plan_tower_distribution
        from scripts.tower_reducer.inspection import inspect_template
        from scripts.tower_reducer.manual_edit import ManualEditPlan
        from scripts.tower_reducer.placement import PlacementOptions, plan_placements
        from scripts.tower_reducer.rewrite import RewritePlan
        raw=ordinary_source([(4+z%2,4+z%2,z) for z in range(14)],[(12,14,4,0)])
        doc=decode_template(raw);inspection=inspect_template(doc)
        dopt=DistributionOptions(.20,retained_counts=(3,));popt=PlacementOptions('bottom_first',1,1)
        dist=plan_tower_distribution(doc,inspection,dopt)
        placement=plan_placements(doc,inspection,dist,popt,include_advice=False)
        self.assertTrue(placement.ok,placement)
        plan=RewritePlan(dist,placement,dopt,popt)
        for wrapped in (plan,ManualEditPlan(plan,sha256(raw).hexdigest(),(),())):
            job,key,_,_=self.prepare(raw,wrapped)
            preview=self.app.template_preview(job.job_id,key)
            self.assertEqual(preview['layer_offset'],1)
            self.assertEqual(preview['changed_count'],1)
            self.assertFalse(any(t['changed'] for t in preview['output']['tiles'][:14]))

    def test_preview_marks_intrinsic_edits_and_reports_actual_output_destinations(self):
        job, key, artifact, baseline = self.prepare()
        before = self.app.template_preview(job.job_id, key)
        preview = self.move(job, key, artifact)
        self.assertEqual(preview['changed_count'], before['changed_count'])
        self.assertEqual(preview['changed_count'], 3)
        self.assertLess(preview['changed_count'], len(preview['output']['tiles']))
        self.assertEqual(preview['source'], before['source'])
        self.assertEqual([tile['changed'] for tile in preview['output']['tiles']],
                         [tile['changed'] for tile in before['output']['tiles']])
        report = job.snapshot()['templates'][key]['verification']['facts']
        old_report = verify_output(job.read_source(key), baseline, _unpack(artifact['plan']), 'new').facts
        for old, new in zip(old_report['moves'], report['moves']):
            self.assertEqual(tuple(new['from']), old['from'])
            self.assertEqual(tuple(new['to']), (old['to'][0] + 1, old['to'][1] + 1, old['to'][2]))
        self.assertEqual(tuple(report['source_symmetry_axes']), old_report['symmetry_axes'])
        self.assertEqual(tuple(report['symmetry_axes']), tuple(value + 2 for value in old_report['symmetry_axes']))

    def test_invalid_or_failed_revision_preserves_previous_verified_artifact(self):
        job, key, artifact, baseline = self.prepare()
        before = job.snapshot()
        for value in (True, 1.5, '1', 33):
            with self.assertRaises(ApiError) as rejected:
                self.move(job, key, artifact, value, 0)
            self.assertEqual(rejected.exception.code, 'invalid_translation')
        with self.assertRaises(ApiError) as rejected:
            self.move(job, key, artifact, 2, 0)
        self.assertEqual(rejected.exception.code, 'translation_out_of_bounds')
        self.assertEqual(job.snapshot(), before)
        def reject(*args, **kwargs):
            job.update_template(key, status='failed')
            return None
        with patch.object(job, 'register_output', side_effect=reject):
            # _job obtains a fresh handle sharing the same repository lock.
            with patch.object(self.app, '_job', return_value=job), self.assertRaises(ApiError):
                self.move(job, key, artifact)
        self.assertEqual(job.snapshot()['templates'][key]['status'], 'verified')
        self.assertEqual(job.read_artifact(artifact['id']), baseline)

    def test_concurrent_same_artifact_request_only_one_wins(self):
        job, key, artifact, baseline = self.prepare()
        def attempt():
            try:
                return self.move(job, key, artifact)['artifact_id']
            except ApiError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))
        self.assertEqual(results.count('stale_artifact'), 1)
        self.assertEqual(len(job.artifact_records()), 1)

    def test_all_modes_serialize_recover_rename_and_export_translated_geometry(self):
        raw = source(total=3)
        modes = [synthetic()]
        for builder in (build_increase, build_ordinary_add):
            result = builder(raw, {'target_mode': 'count', 'added_tiles': 3}, 'new')
            self.assertTrue(result['ok'], result.get('error'))
            modes.append((raw, result['plan']))
        for index, (raw, plan) in enumerate(modes):
            with self.subTest(mode=type(plan).__name__):
                job, key, artifact, baseline = self.prepare(raw, plan)
                preview = self.move(job, key, artifact, 0, 1)
                record = job.artifact_records()[0]
                translated = _unpack(record['plan'])
                self.assertIs(type(translated), TranslationPlan)
                self.assertEqual(_pack(_unpack(_pack(translated))), _pack(translated))
                output = job.read_artifact(record['id'])
                offset = getattr(plan, 'original_z_offset', 0)
                base_preview = comparison(raw, baseline, original_z_offset=offset)
                shifted_preview = self.app.template_preview(job.job_id, key)
                self.assertEqual(shifted_preview['changed_count'], base_preview['changed_count'])
                self.assertEqual(shifted_preview['source'], base_preview['source'])
                self.assertEqual(sum(tile['added'] for tile in shifted_preview['output']['tiles']),
                                 sum(tile['added'] for tile in base_preview['output']['tiles']))
                self.assert_shift(baseline, output, 0, 1)
                self.assertTrue(verify_output(raw, output, translated, 'new').ok)
                self.assertEqual(verify_translation(raw, output, translated, 'new').facts['output_sha256'], sha256(output).hexdigest())
                self.assertEqual(JobRepository(self.repo.root).get(job.job_id).read_artifact(record['id']), output)
                renamed, same_plan = job.renamed_output_bytes(record['id'], 'new_2')
                self.assert_shift(output, renamed, 0, 0)
                self.assertEqual(same_plan, translated)
                exported = export_outputs(job, self.root / ('exports' + str(index)))
                self.assertTrue(exported.ok, exported)
                self.assertEqual((self.root / ('exports' + str(index)) / 'new_m.bytes').read_bytes(), output)

    def test_unknown_wire_and_nonuniform_or_source_tampering_fail(self):
        raw, plan = synthetic()
        translated = TranslationPlan(plan, sha256(raw).hexdigest(), 1, 1)
        output = rewrite_template(raw, translated, 'new')
        self.assertTrue(verify_translation(raw, output, translated, 'new').ok)
        # Fixture unknown length-delimited bytes must survive exactly.
        self.assertFalse(verify_translation(raw, output.replace(b'xy', b'zz', 1), translated, 'new').ok)
        self.assertFalse(verify_translation(raw, output, replace(translated, dx=0), 'new').ok)
        self.assertFalse(verify_translation(raw + b'\xa0\x06\x01', output, translated, 'new').ok)

    def test_full_tower_projection_can_tighten_safe_anchor_bounds(self):
        raw = source(total=12, towers=((8, 4, 12, 0),), points=[(6, 8)])
        bounds = translation_bounds(decode_template(raw))
        # Upward twelve-card unfolding cannot be placed at anchor Y=2.
        self.assertGreater(bounds['min_dy'], 2 - 4)
        with self.assertRaisesRegex(ValueError, 'translation_out_of_bounds'):
            translate_bytes(raw, 0, -2)

    def test_historical_horizontal_margin_tiles_allow_vertical_translation(self):
        # xkd068 retains source tiles at X=1 and X=15 on its 17×19 board.
        # They may keep their existing margins, but neither side may worsen.
        raw = source(total=3, points=[(1, 3), (15, 13), (8, 8)])
        self.assertEqual(translation_bounds(decode_template(raw)),
                         dict(min_dx=0, max_dx=0, min_dy=-1, max_dy=3))
        self.assert_shift(raw, translate_bytes(raw, 0, 3), 0, 3)
        for dx in (-1, 1):
            with self.assertRaisesRegex(ValueError, 'translation_out_of_bounds'):
                translate_bytes(raw, dx, 0)

    def test_historical_vertical_margin_tiles_allow_horizontal_translation(self):
        raw = source(total=3, points=[(3, 1), (13, 17), (8, 8)])
        self.assertEqual(translation_bounds(decode_template(raw)),
                         dict(min_dx=-1, max_dx=1, min_dy=0, max_dy=0))
        self.assert_shift(raw, translate_bytes(raw, 1, 0), 1, 0)

    def test_safe_margin_tiles_cannot_be_translated_outside_safe_region(self):
        raw = source(total=3, points=[(2, 2), (14, 16), (8, 8)])
        self.assertEqual(translation_bounds(decode_template(raw)),
                         dict(min_dx=0, max_dx=0, min_dy=0, max_dy=0))
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            with self.assertRaisesRegex(ValueError, 'translation_out_of_bounds'):
                translate_bytes(raw, dx, dy)

    def test_http_route_uses_translation_service(self):
        from scripts.app import RequestHandler
        handler = RequestHandler.__new__(RequestHandler)
        app = SimpleNamespace(translate_template=lambda job, key, payload: dict(job=job, key=key, payload=payload))
        handler.server = SimpleNamespace(application=app)
        payload = dict(artifact_id='art-' + 'a' * 32, dx=1, dy=-1)
        handler._read_json = lambda: payload
        job_id, key = 'job-' + 'b' * 32, 'tpl-' + 'c' * 32
        status, response, headers = handler._dispatch_api('POST', '/api/jobs/' + job_id + '/translate/' + key)
        self.assertEqual(status, 200)
        self.assertEqual(response, dict(job=job_id, key=key, payload=payload))

    def test_old_export_and_sync_plans_reject_revision_and_new_sync_uses_shifted_bytes(self):
        job, key, artifact, baseline = self.prepare()
        target = self.root / 'template-checkout' / TEMPLATE_SUFFIX
        target.mkdir(parents=True)
        save_settings(LocalSettings(template_root=str(target), template_confirmed=True), self.settings)
        sync = SyncService(self.repo, self.settings)
        old_sync = sync.create_sync_plan(job, ('template',))
        old_export = plan_exports(job, self.root / 'export')
        preview = self.move(job, key, artifact)
        output = job.read_artifact(preview['artifact_id'])
        with self.assertRaises(TargetChanged):
            export_outputs(job, self.root / 'export', expected_rows=old_export.rows)
        stale_sync = sync.commit_sync(old_sync.plan_id, {})
        self.assertEqual(stale_sync.rows[0]['status'], 'failed')
        self.assertFalse((target / 'new_m.bytes').exists())
        plan = sync.create_sync_plan(job, ('template',))
        report = sync.commit_sync(plan.plan_id, {})
        self.assertTrue(report.ok, report)
        self.assertEqual((target / 'new_m.bytes').read_bytes(), output)
        self.assert_shift(baseline, output, 1, 1)
