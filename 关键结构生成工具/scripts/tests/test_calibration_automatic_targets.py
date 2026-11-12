import sys
import unittest
from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.consumption_calibration_service import (
    _automatic_structure_search_bounds, _candidate_space, _non_increasing_online_targets,
)
from services.parameter_recommendation_service import DupParam, manifest_replacement_type_counts
from puzzle_core.errors import InputValidationError


class AutomaticCalibrationTests(unittest.TestCase):
    def test_twelve_minus_five_caps_at_seven_but_five_and_other_seeds_remain_candidates(self):
        manifest = {"replacements": [
            {"source_tile_id": -10, "structure_type": label}
            for label in 'CDEFG' for _ in range(3)
        ]}
        counts = manifest_replacement_type_counts(manifest)
        self.assertEqual(counts, (5, 0))
        original = DupParam(12, '1', 100, 4)
        baseline = DupParam(7, '1', 100, 4)
        upper, lower, evidence = _automatic_structure_search_bounds([original]*10, [baseline]*10, *counts)
        self.assertEqual((upper[0].a, upper[0].b), (7, 4))
        self.assertEqual((lower[0].a, lower[0].b), (4, 4))
        candidates = _candidate_space(upper[0], lower[0], seeds=('1', '5', '12'))
        values = {p.render() for p in candidates}
        self.assertTrue({'7|1|100|4','7|5|100|4','5|1|100|4','7|12|100|4'} <= values)
        self.assertTrue(all(p.a <= 7 and p.b == 4 for p in candidates))
        self.assertEqual(original.render(), '12|1|100|4')
        self.assertEqual(evidence['replaced_a_type_count'], 5)

    def test_a_and_b_caps_are_separate_and_small_sources_are_never_raised(self):
        original = DupParam(5, '3', 80, 4)
        upper, lower, _ = _automatic_structure_search_bounds([original], [DupParam(3,'3',80,3)], 2, 1)
        self.assertEqual(upper[0].render(), '3|3|80|3')
        self.assertLessEqual(lower[0].a, upper[0].a)
        with self.assertRaises(InputValidationError):
            _automatic_structure_search_bounds([original], [original], 6, 0)

    def test_online_increases_are_flattened_and_raw_data_remains_unchanged(self):
        values = [2000, 1800, 1500, 1142.5, 1521.69, 1400, 1000, None, 1200, 1100]
        records = [{'value':value,'source':'source_template_online','source_observed_value':value} for value in values]
        before = deepcopy(records)
        adjusted = _non_increasing_online_targets(records)
        self.assertEqual([r['value'] for r in adjusted], [2000,1800,1500,1142.5,1142.5,1142.5,1000,None,1200,1100])
        self.assertEqual(adjusted[4]['source_observed_value'], 1521.69)
        self.assertEqual(adjusted[4]['adjustment']['previous_duplicate_index'], 4)
        self.assertEqual(adjusted[5]['adjustment']['anchor_duplicate_index'], 4)
        self.assertEqual(records, before)

    def test_zero_targets_are_valid_and_manual_values_are_not_lowered(self):
        result = _non_increasing_online_targets([
            {'value':0,'source':'source_template_online'},
            {'value':1,'source':'source_template_online'},
            {'value':3000,'source':'user_override'},
        ])
        self.assertEqual([r['value'] for r in result], [0,0,3000])
        self.assertNotIn('adjustment',result[2])

    def test_adjusted_online_targets_reach_prediction_and_manual_override_wins(self):
        from services import consumption_calibration_service as service
        from services.profile_service import Profile
        from scripts.tests.test_consumption_calibration_service import _history, HEADER
        from scripts.tests.test_difficulty import _level
        level = _level()
        manifest = {'source_template_id':'source','output_template_id':'output',
                    'source_sha256':level.sha256,'signature':'s5','replacements':[]}
        values = [2000,1800,1500,1142.5,1521.69,1400,1000,900,800,700]
        data = _history([['source','7|1|100|4',i,1003,'',100,value,250]
                         for i,value in enumerate(values,start=1)], header=[*HEADER,'level_index'])
        baseline = {'rows':[{'status':'ready','source_template_id':'source',
            'config_source_template_id':'source','output_template_id':'output',
            'before_dup':['7|1|100|4']*10,'recommended_dup':['7|1|100|4']*10,
            'online_positions':[250],'strategy_group_id':'1003','random_seed_group_id':0}],
            'warnings':[],'source_tables':{}}
        profile = Profile('p','test',None,None,'/tmp/out','/tmp/config')
        with patch.object(service,'build_parameter_recommendations',return_value=baseline), \
             patch.object(service,'load_template_id',return_value=SimpleNamespace(level=level)), \
             patch.object(service,'_consumption_prediction',wraps=service._consumption_prediction) as predict:
            result = service.calibrate_consumption_parameters(profile,[manifest],config_set_id='csv',
                history_data=data,history_filename='history.csv',target_overrides={'6':1600})
        duplicates = result['rows'][0]['duplicates']
        fifth = duplicates[4]['target_consumption']
        self.assertEqual(fifth['value'],1142.5)
        self.assertEqual(fifth['source_observed_value'],1521.69)
        self.assertEqual(duplicates[5]['target_consumption']['value'],1600)
        self.assertEqual(duplicates[5]['target_consumption']['source'],'user_override')
        self.assertTrue(any(c.kwargs['duplicate_index']==5 and c.kwargs['target']==1142.5 for c in predict.call_args_list))


if __name__ == '__main__':
    unittest.main()
