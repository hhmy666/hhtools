import unittest

from scripts.tower_reducer.naming import render_template_id, validate_batch_names


class NamingTests(unittest.TestCase):
    def test_default_custom_fixed_and_override(self):
        self.assertEqual(render_template_id('lessT{原模板ID}', 'old'), 'lessTold')
        self.assertEqual(render_template_id('a_{原模板ID}-v2', 'old'), 'a_old-v2')
        self.assertEqual(render_template_id('fixed', 'old'), 'fixed')
        self.assertEqual(render_template_id('lessT{原模板ID}', 'old', 'manual'), 'manual')

    def test_invalid_rules_and_ids(self):
        for rule in ('{原模板ID}{原模板ID}', '{other}', '{原模板ID', 'x}', '{{原模板ID}}', ''):
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                render_template_id(rule, 'old')
        for name in ('', 'old', '中文', '../other', '/other', 'a\\b', 'a.b', 'a b', '\x00', 'x'*181):
            with self.subTest(name=name), self.assertRaises(ValueError):
                render_template_id('lessT{原模板ID}', 'old', name)
        self.assertEqual(render_template_id('x'*180, 'old'), 'x'*180)

    def test_fixed_batch_name_is_versioned_even_with_overrides(self):
        report = validate_batch_names([{'original_id':'a','override':'newA'}, {'original_id':'b','override':'newB'}], 'fixed')
        self.assertTrue(report.ok)
        self.assertEqual([r['new_id'] for r in report.rows], ['newA', 'newB'])
        fixed = validate_batch_names([{'original_id':'a'}, {'original_id':'b'}], 'fixed')
        self.assertEqual([r['new_id'] for r in fixed.rows], ['fixed', 'fixedv2'])

    def test_casefold_collisions_are_versioned_without_taking_an_explicit_v2(self):
        rows = [{'original_id':'a','override':'NEW'}, {'original_id':'b','override':'new'},
                {'original_id':'c','override':'NEW'}, {'original_id':'d','override':'newv2'}]
        report = validate_batch_names(rows, 'lessT{原模板ID}')
        self.assertTrue(report.ok)
        self.assertEqual([r['new_id'] for r in report.rows], ['NEW','newv3','NEWv4','newv2'])

    def test_long_duplicate_names_leave_room_for_the_version_suffix(self):
        report = validate_batch_names([{'original_id':'a'}, {'original_id':'b'}], 'x'*180)
        self.assertTrue(report.ok)
        self.assertEqual([r['new_id'] for r in report.rows], ['x'*180, 'x'*178+'v2'])

    def test_single_fixed_and_row_failure_are_independent(self):
        self.assertTrue(validate_batch_names([{'original_id':'old'}], 'fixed').ok)
        result = validate_batch_names([{'original_id':'a','override':'bad/id'}, {'original_id':'b'}], 'lessT{原模板ID}')
        self.assertEqual([r['ok'] for r in result.rows], [False,True])
