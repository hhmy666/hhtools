"""Regressions for paired-board features and source-family holdout training."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from services import consumption_calibration_service as service
from services.parameter_recommendation_service import DupParam
from tools.train_consumption_model import group_folds, resolve_identity, history_key


class PairedBoardFeatureTests(unittest.TestCase):
    def test_reduced_towers_and_existing_anchor_structures_are_not_lost(self):
        def tile(tile_id, layer, tower=False):
            return SimpleNamespace(tile_id=tile_id, layer=layer, is_tower_member=tower)
        source = SimpleNamespace(tiles=[tile(-10, 1), tile(-11, 2)])
        anchor = SimpleNamespace(tiles=[tile(-10, 1), tile(-11, 2), tile(-12, 3), tile(-11, 8, True)])
        self.assertTrue(hasattr(service, '_paired_board_features'))
        result = service._paired_board_features(source, anchor)
        self.assertEqual(result['source_tower_fraction'], 0)
        self.assertEqual(result['anchor_tower_fraction'], .25)
        self.assertEqual(result['anchor_structure_fraction'], .25)
        self.assertEqual(result['source_max_layer'], .2)
        self.assertAlmostEqual(result['layer_delta'], -.6)

    def test_remaining_tile_pressure_uses_only_unreplaced_a_and_b(self):
        static = dict(source_total=100, source_tile_a_count=60, source_tile_b_count=40,
                      replacement_count=15, type_count=5, replaced_a=15, replaced_b=0,
                      external_a=0, external_b=0, initial_entry=3, blocker_edges=1,
                      nonzero_masks=1, min_failure_click=7, complete_paths=1)
        features = service._structure_correction_features(
            {}, target=1000, original=DupParam(9, '1', '100', 9),
            candidate=DupParam(6, '31', '100', 3), duplicate_index=1, level_index=456,
            static_structure_features=static)
        self.assertAlmostEqual(features.get('remaining_a_pressure', -1), .18)
        self.assertAlmostEqual(features.get('remaining_b_pressure', -1), .08)

    def test_renames_are_resolved_by_level_and_actual_parameters_not_suffix(self):
        row = dict(level_id='newv1aipuzzlehr0008s5v1', level_template_id='aipuzzlehr0008s5',
                   level_dup_para_id='1', level_dup='10|1|100|4')
        config = {row['level_id']: dict(TemplateId='aipuzzlehr0008s5v1', DupPara1='10|1|100|4')}
        self.assertEqual(resolve_identity(row, config), ('aipuzzlehr0008s5v1', 'historical_config_rename'))
        self.assertEqual(resolve_identity(row, {})[0], 'aipuzzlehr0008s5')
        config[row['level_id']]['DupPara1'] = '8|1|100|4'
        with self.assertRaisesRegex(ValueError, 'historical_parameter_mismatch'):
            resolve_identity(row, config)

    def test_family_holdouts_keep_all_variants_together(self):
        groups = ['tile01g:hr0008'] * 20 + ['tile01g:hr0010'] * 30 + ['tile01g:mxd0003'] * 10
        folds = group_folds(groups, 3)
        for group in set(groups):
            self.assertEqual(len({folds[i] for i, g in enumerate(groups) if g == group}), 1)
        self.assertEqual(set(folds), {0, 1, 2})
        row = dict(user_info='tile01g', level_template_id='hr0008', level_index='432', level_dup_para_id='1')
        self.assertNotEqual(history_key(row), history_key({**row, 'level_index': '464'}))
        self.assertNotEqual(history_key(row), history_key({**row, 'user_info': 'other_product'}))

    def test_new_model_can_transfer_without_reversing_ab_difficulty(self):
        model = service._load_structure_correction_model()
        self.assertEqual(model['model_id'], 'consumption-structure-correction-20261027-v2')
        static = dict(source_total=100, source_tile_a_count=60, source_tile_b_count=40,
                      replacement_count=12, type_count=4, replaced_a=12, replaced_b=0,
                      external_a=10, external_b=2, initial_entry=3, blocker_edges=10,
                      nonzero_masks=8, min_failure_click=7, complete_paths=1000,
                      source_tower_fraction=.2, anchor_tower_fraction=.4,
                      anchor_structure_fraction=0, log_source_tiles=4.6,
                      log_tile_ratio=-.1, source_max_layer=.7, layer_delta=-.2)
        manifest = {'signature': 's5', 'validation': {'replacement_count': 12}, 'replacements': []}
        def predict(a, b, seed='1', position=1000, context=static):
            return service._consumption_prediction(target=1000, manifest=manifest,
                original=DupParam(9, '1', '100', 9), candidate=DupParam(a, seed, '100', b),
                duplicate_index=1, level_index=position, model=model, static_structure_features=context)
        low, high = predict(6, 3), predict(9, 9)
        self.assertIsNotNone(low['value'])
        self.assertLessEqual(low['value'], high['value'])
        self.assertEqual(high['estimate_mode'], 'transfer_estimate')
        self.assertEqual(high['value'], predict(9, 9, seed='31')['value'])
        self.assertEqual(predict(9, 9, position=350)['estimate_mode'], 'transfer_estimate')
        self.assertEqual(predict(9, 9, position=456)['estimate_mode'], 'in_scope_model')
        missing = {k: v for k, v in static.items() if k != 'anchor_tower_fraction'}
        self.assertIsNone(predict(9, 9, context=missing)['value'])
        for feature in model['monotone_features']:
            self.assertGreaterEqual(model['coefficients'][feature], 0)


if __name__ == '__main__':
    unittest.main()
