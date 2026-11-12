"""Persisted legacy symmetry never changes the new planner's source policy."""
import unittest
from dataclasses import replace
from hashlib import sha256

from scripts.tests.test_increase_engine import source
from scripts.tower_reducer.increase import IncreasePlan, TowerAddition, rewrite_increase
from scripts.tower_reducer.increase_verification import verify_increase
from scripts.tower_reducer.storage import _pack, _unpack


class IncreaseSymmetryCompatibilityTests(unittest.TestCase):
    def fixture(self):
        raw = source(points=[(6, 6), (10, 10), (6, 8), (10, 8)])
        towers = (TowerAddition(0, 3, 12, 0, 0, 6, (-10,) * 6),
                  TowerAddition(1, 13, 12, 0, 0, 6, (-10,) * 6))
        plan = IncreasePlan(sha256(raw).hexdigest(), {
            'target_mode': 'count', 'added_tiles': 12, 'stack_count': 2,
            'auto_translate': False,
        }, towers, (0, 0), 60)
        old_record = _pack(plan)
        old_record['fields'].pop('symmetry_version', None)
        return raw, plan, old_record

    def audit(self, raw, plan, output=None):
        return verify_increase(raw, output or rewrite_increase(raw, plan, 'moreToriginal'),
                               plan, 'moreToriginal')

    def test_old_record_missing_version_keeps_accepted_horizontal_pair(self):
        raw, new_plan, old_record = self.fixture()
        old_plan = _unpack(old_record)
        report = self.audit(raw, old_plan)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(_pack(old_plan)['fields'].get('symmetry_version'), 'legacy-x-v1')

    def test_new_plan_persists_source_policy_and_rejects_same_horizontal_pair(self):
        raw, plan, old_record = self.fixture()
        self.assertEqual(_pack(plan)['fields'].get('symmetry_version'), 'source-v2')
        report = self.audit(raw, _unpack(_pack(plan)))
        self.assertFalse(report.ok)
        self.assertIn('position_symmetry', {issue.code for issue in report.errors})

    def test_unknown_version_and_incomplete_old_record_are_rejected(self):
        raw, plan, old_record = self.fixture()
        unknown = _pack(plan)
        unknown['fields']['symmetry_version'] = 'unknown-v3'
        with self.assertRaises(ValueError):
            _unpack(unknown)
        old_record['fields'].pop('translation')
        with self.assertRaises(ValueError):
            _unpack(old_record)

    def test_legacy_policy_keeps_current_safe_bounds_and_unknown_wire_protection(self):
        raw, new_plan, old_record = self.fixture()
        plan = _unpack(old_record)
        outer = replace(plan, towers=(replace(plan.towers[0], x=1),
                                      replace(plan.towers[1], x=15)))
        report = self.audit(raw, outer)
        self.assertFalse(report.ok)
        self.assertIn('safe_bounds', {issue.code for issue in report.errors})
        output = rewrite_increase(raw, plan, 'moreToriginal').replace(b'unknown', b'changed', 1)
        report = self.audit(raw, plan, output)
        self.assertFalse(report.ok)
        self.assertIn('wire_unknown', {issue.code for issue in report.errors})

    def test_renaming_preserves_legacy_symmetry_policy(self):
        raw, new_plan, old_record = self.fixture()
        plan = _unpack(old_record)
        renamed_plan = replace(plan)
        output = rewrite_increase(raw, renamed_plan, 'moreToriginalv2')
        report = verify_increase(raw, output, renamed_plan, 'moreToriginalv2')
        self.assertTrue(report.ok, report.errors)


if __name__ == '__main__':
    unittest.main()
