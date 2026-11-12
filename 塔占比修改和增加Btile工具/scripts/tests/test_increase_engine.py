"""Increase contracts: real protobuf, quantity, geometry and hostile read-back."""
import importlib.util
import unittest
from dataclasses import replace

from scripts.tower_reducer.protobuf_wire import encode_int32, encode_varint
from scripts.tower_reducer.template_reader import decode_template
from scripts.tower_reducer.preview import comparison


def scalar(n, v):
    return encode_varint(n << 3) + encode_int32(v)


def msg(n, v):
    return encode_varint(n << 3 | 2) + encode_varint(len(v)) + v


def source(total=60, towers=(), b=0, points=None):
    parts = [msg(2, b'original'), scalar(4, 17), scalar(5, 19), msg(90, b'unknown')]
    ordinary = total - sum(t[2] for t in towers)
    positions = points or [(6, 8), (8, 8), (10, 8)]
    # Ordinary fixtures must have real immediate support without exact XY
    # copies on adjacent layers. Tower members below intentionally share XY.
    cx = (min(x for x, _ in positions) + max(x for x, _ in positions)) / 2
    cy = (min(y for _, y in positions) + max(y for _, y in positions)) / 2
    def stagger(x, y, z):
        if z % 2:
            # Move toward the same bbox center so actual mirror/rotation axes
            # survive staggered support layers rather than drifting by a row.
            if y != cy:
                y += 1 if y < cy else -1
            elif x != cx:
                x += 1 if x < cx else -1
            else:
                y += 1 if y < 16 else -1
        return x, y, z
    records = [stagger(*positions[i % len(positions)], i // len(positions)) for i in range(ordinary)]
    records += [(x, y, z) for x, y, count, direction in towers for z in range(count)]
    for i, (x, y, z) in enumerate(records):
        parts.append(msg(6, scalar(1, x)+scalar(2, y)+scalar(3, z)+scalar(4, -11 if i < b else -10)+msg(91,b'keep')))
    for x, y, count, direction in towers:
        parts.append(msg(7,scalar(1,x)+scalar(2,y)+scalar(3,direction)+msg(92,b'stack')))
    return b''.join(parts)


class IncreaseTests(unittest.TestCase):
    def build(self, raw=None, **options):
        self.assertIsNotNone(importlib.util.find_spec('scripts.tower_reducer.increase'),
                             'increase engine must implement the approved builder')
        from scripts.tower_reducer.increase import build_increase
        return build_increase(raw or source(), options, 'moreToriginal')

    def successful(self, raw=None, **options):
        raw = raw or source()
        result = self.build(raw, **options)
        self.assertTrue(result['ok'], result.get('error'))
        from scripts.tower_reducer.verification import verify_output
        report = verify_output(raw, result['output'], result['plan'], 'moreToriginal')
        self.assertTrue(report.ok, report.errors)
        return result, decode_template(result['output'])

    def test_multiplier_creates_towers_and_60_becomes_90(self):
        result, doc = self.successful(target_mode='multiplier', total_multiplier=1.5)
        self.assertEqual(len(doc.tiles), 90)
        self.assertTrue(doc.stacks)
        self.assertEqual(result['summary']['added_tiles'], 30)

    def test_twenty_rounds_to_twenty_one_and_keeps_original_wire(self):
        raw = source()
        result, doc = self.successful(raw, target_mode='count', added_tiles=20)
        self.assertEqual(len(doc.tiles),81)
        self.assertEqual([t.fields for t in doc.tiles[:60]], [t.fields for t in decode_template(raw).tiles])
        self.assertEqual(sum(t.tile_id == -10 for t in doc.tiles),81)
        self.assertEqual(comparison(raw,result['output'])['changed_count'],21)

    def test_ratio_uses_new_denominator_and_no_quarter_ceiling(self):
        result, doc = self.successful(target_mode='ratio', min_ratio=.4, max_ratio=.45)
        count = sum(t.final_count for t in result['plan'].towers)
        self.assertGreaterEqual(count / len(doc.tiles),.4)
        self.assertLess(count / len(doc.tiles),.45)

    def test_existing_towers_keep_direction_and_add_only(self):
        raw=source(towers=((3,10,6,0),(13,10,6,0)))
        result, doc=self.successful(raw,target_mode='count',added_tiles=12,operation='existing')
        self.assertEqual(len(doc.stacks),2)
        self.assertEqual([t.final_count for t in result['plan'].towers],[12,12])
        self.assertEqual([s.direction for s in doc.stacks],[0,0])

    def test_manual_final_counts_and_lock_conflict(self):
        raw=source(towers=((3,10,6,0),(13,10,6,0)))
        result, doc=self.successful(raw,target_mode='manual',operation='existing',tower_counts=[9,9])
        self.assertEqual(len(doc.tiles),66)
        self.assertFalse(self.build(raw,target_mode='manual',operation='existing',tower_counts=[5,5],locked_towers=[0])['ok'])

    def test_majority_fill_and_final_groups_of_three(self):
        result, doc=self.successful(source(b=39),target_mode='count',added_tiles=20)
        self.assertEqual(sum(t.tile_id == -11 for t in doc.tiles),60)
        self.assertEqual(sum(t.tile_id == -10 for t in doc.tiles),21)

    def test_per_tower_direction_override(self):
        for direction in range(4):
            with self.subTest(direction=direction):
                result,doc=self.successful(target_mode='count',added_tiles=6,stack_count=1,
                    regions=['bottom'],tower_directions=[{'index':0,'direction':direction}])
                self.assertEqual(doc.stacks[0].direction,direction)

    def test_tampering_original_unknown_bytes_fails(self):
        raw=source()
        result,doc=self.successful(raw,target_mode='count',added_tiles=6)
        from scripts.tower_reducer.verification import verify_output
        forged=result['output'].replace(b'unknown',b'changed',1)
        self.assertFalse(verify_output(raw,forged,result['plan'],'moreToriginal').ok)

    def test_invalid_and_nonfinite_options_fail_cleanly(self):
        for options in ({'added_tiles':float('nan')},{'mystery':True},{'direction':4},{'spacing':-1}):
            self.assertFalse(self.build(**options)['ok'])

    def test_recommendations_are_real_verified_outputs(self):
        self.build(target_mode='count',added_tiles=6)
        from scripts.tower_reducer.increase import recommend_increase
        from scripts.tower_reducer.verification import verify_output
        raw=source()
        result=recommend_increase(raw,{'target_mode':'count','added_tiles':12},'moreToriginal')
        self.assertGreaterEqual(len(result['schemes']),2)
        for scheme in result['schemes']:
            self.assertTrue(verify_output(raw,scheme['output'],scheme['plan'],'moreToriginal').ok)

    def test_center_hole_uses_all_layers_and_preserves_symmetry(self):
        ring=[(4,4),(8,4),(12,4),(4,8),(12,8),(4,12),(8,12),(12,12)]
        raw=source(total=48,points=ring)
        result,doc=self.successful(raw,target_mode='count',added_tiles=6,stack_count=1,regions=['center'])
        self.assertEqual((doc.stacks[0].x,doc.stacks[0].y),(8,8))
        # A high-layer occupied card in the same hole is still a blocker.
        blocker=msg(6,scalar(1,8)+scalar(2,8)+scalar(3,20)+scalar(4,-10))
        blocked=raw+blocker+blocker+blocker
        result=self.build(blocked,target_mode='count',added_tiles=6,stack_count=1,regions=['center'],auto_translate=False)
        self.assertFalse(result['ok'])

    def test_difference_and_proportion_preserve_distinct_platforms(self):
        raw=source(towers=((2,12,3,0),(14,12,6,0)))
        result,doc=self.successful(raw,target_mode='count',added_tiles=6,operation='existing',height_mode='difference')
        self.assertEqual([t.final_count for t in result['plan'].towers],[6,9])
        result,doc=self.successful(raw,target_mode='count',added_tiles=9,operation='existing',height_mode='proportion')
        self.assertEqual([t.final_count for t in result['plan'].towers],[6,12])

    def test_both_extends_existing_and_adds_new_towers(self):
        raw=source(towers=((2,12,3,0),(14,12,6,0)))
        result,doc=self.successful(raw,target_mode='count',added_tiles=18,operation='both',stack_count=2,regions=['bottom'])
        self.assertEqual(len(doc.stacks),4)
        self.assertGreater(sum(t.final_count for t in result['plan'].towers[:2]),9)

    def test_manual_nearest_legal_keeps_mirrored_heights(self):
        raw=source(towers=((3,10,6,0),(13,10,6,0)))
        result,doc=self.successful(raw,target_mode='manual',operation='existing',tower_counts=[8,8])
        self.assertEqual([t.final_count for t in result['plan'].towers],[9,9])
        self.assertEqual(len(doc.tiles),66)

    def test_new_tower_lock_uses_adopted_height_not_one(self):
        result,doc=self.successful(target_mode='manual',operation='new',stack_count=1,regions=['bottom'],
            tower_counts=[6],locked_towers=[0])
        self.assertEqual(result['plan'].towers[0].final_count,6)

    def test_existing_position_moves_mirrored_partner(self):
        raw=source(towers=((3,12,6,0),(13,12,6,0)))
        result,doc=self.successful(raw,target_mode='count',operation='existing',added_tiles=6,
            tower_positions=[{'index':0,'x':2,'y':12}])
        self.assertEqual([(s.x,s.y) for s in doc.stacks],[(2,12),(14,12)])

    def test_client_outer_frame_rejects_editor_only_fit(self):
        from scripts.tower_reducer.increase_geometry import tower_fits
        # 17 cards at y=3 pass editor: 3 - 16*16/85 > 0,
        # but client offset is .2: 3 - 16*.2 < 0.
        self.assertFalse(tower_fits(8,3,17,0,(),17,19))

    def test_explicit_translation_preserves_other_tile_fields(self):
        raw=source()
        result,doc=self.successful(raw,target_mode='count',added_tiles=6,translate_x=1,translate_y=2,auto_translate=False)
        self.assertEqual(result['plan'].translation,(1,2))
        self.assertEqual((doc.tiles[0].x,doc.tiles[0].y),(7,10))
        self.assertEqual(doc.tiles[0].tile_id,-10)

    def test_geometry_height_above_twenty_five_is_supported(self):
        result,doc=self.successful(source(points=[(6,6),(8,6),(10,6)]),target_mode='count',added_tiles=30,stack_count=1,regions=['bottom'])
        self.assertEqual(result['plan'].towers[0].final_count,30)

    def test_asymmetric_formation_still_gets_symmetric_new_positions(self):
        raw=source(total=60,points=[(4,8),(8,8),(10,10)])
        result,doc=self.successful(raw,target_mode='count',added_tiles=12,stack_count=2,regions=['top','bottom'])
        positions={(s.x,s.y) for s in doc.stacks}
        self.assertEqual(positions,{(14-x,18-y) for x,y in positions})

    def test_frozen_plan_does_not_alias_callers_mutable_options(self):
        from scripts.tower_reducer.increase import build_increase
        options={'target_mode':'count','added_tiles':6,'regions':['bottom']}
        result=build_increase(source(),options,'moreToriginal')
        self.assertTrue(result['ok'])
        options['regions'][0]='top'
        self.assertEqual(result['plan'].options['regions'],('bottom',))

    def test_impossible_quantity_and_huge_numeric_requests_are_bounded(self):
        result=self.build(target_mode='count',added_tiles=1e300)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'],'quantity_outside_geometry')

    def test_zero_quantity_still_applies_explicit_direction(self):
        raw=source(towers=((3,10,6,0),(13,10,6,0)))
        result,doc=self.successful(raw,target_mode='count',added_tiles=0,
            tower_directions=[{'index':0,'direction':3}])
        self.assertEqual(len(doc.tiles),60)
        self.assertEqual(doc.stacks[0].direction,3)

    def test_independent_audit_rejects_quantity_option_tampering(self):
        raw=source()
        result,doc=self.successful(raw,target_mode='count',added_tiles=6)
        from scripts.tower_reducer.verification import verify_output
        forged=replace(result['plan'],options={**result['plan'].options,'added_tiles':900})
        self.assertFalse(verify_output(raw,result['output'],forged,'moreToriginal').ok)

    def test_pure_tower_source_cannot_bypass_mirror_movement_authority(self):
        raw=source(total=12,towers=((3,10,6,0),(13,10,6,0)))
        result,doc=self.successful(raw,target_mode='count',added_tiles=6,operation='existing')
        from scripts.tower_reducer.verification import verify_output
        from scripts.tower_reducer.rewrite import rewrite_template
        forged=replace(result['plan'],options={**result['plan'].options,'tower_positions':[{'index':0,'x':2,'y':10}]},
            towers=(replace(result['plan'].towers[0],x=2),replace(result['plan'].towers[1],x=10)))
        output=rewrite_template(raw,forged,'moreToriginal')
        self.assertFalse(verify_output(raw,output,forged,'moreToriginal').ok)

    def test_new_anchor_cannot_swallow_original_non_tower_tiles(self):
        raw=source()
        result,doc=self.successful(raw,target_mode='count',added_tiles=6,stack_count=1,regions=['bottom'])
        from scripts.tower_reducer.verification import verify_output
        from scripts.tower_reducer.rewrite import rewrite_template
        forged=replace(result['plan'],towers=(replace(result['plan'].towers[0],x=8,y=8),))
        output=rewrite_template(raw,forged,'moreToriginal')
        self.assertFalse(verify_output(raw,output,forged,'moreToriginal').ok)

    def test_ratio_below_source_explains_monotonic_impossibility(self):
        result=self.build(source(towers=((3,10,6,0),(13,10,6,0))),
            target_mode='ratio',min_ratio=.1,max_ratio=.2)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'],'ratio_unattainable')

    def test_recommendation_options_capture_layout_for_exact_replay(self):
        from scripts.tower_reducer.increase import recommend_increase,build_increase
        raw=source()
        response=recommend_increase(raw,{'target_mode':'count','added_tiles':21},'moreToriginal')
        for scheme in response['schemes']:
            self.assertEqual(len(scheme['options']['tower_counts']),len(scheme['plan'].towers))
            replay=build_increase(raw,scheme['options'],'moreToriginal')
            self.assertTrue(replay['ok'],replay.get('error'))
            self.assertEqual(replay['output'],scheme['output'])

    def test_independent_audit_rejects_new_tower_outside_requested_region(self):
        raw=source()
        result,doc=self.successful(raw,target_mode='count',added_tiles=6,regions=['bottom'])
        from scripts.tower_reducer.verification import verify_output
        forged=replace(result['plan'],options={**result['plan'].options,'regions':['top']})
        self.assertFalse(verify_output(raw,result['output'],forged,'moreToriginal').ok)

    def test_editor_and_client_outer_frames_include_actual_card_body(self):
        from scripts.tower_reducer.increase_geometry import rectangle
        editor=rectangle(8,17,1,0,16/85)
        client=rectangle(8,17,1,0,.2)
        self.assertGreater(editor[3],19.2)
        self.assertGreater(client[3],19.2)
        self.assertLess(client[1],17)

    def test_unchanged_source_tower_overflow_is_history_not_repaired(self):
        raw=source(towers=((2,0,6,0),(14,0,6,0)))
        result,doc=self.successful(raw,target_mode='count',added_tiles=6,operation='new',regions=['bottom'])
        self.assertEqual([(s.x,s.y) for s in doc.stacks[:2]],[(2,0),(14,0)])
        from scripts.tower_reducer.verification import verify_output
        self.assertTrue(verify_output(raw,result['output'],result['plan'],'moreToriginal').warnings)

    def test_translated_formation_must_fit_visible_body_not_only_anchor(self):
        result=self.build(source(points=[(6,0),(8,0),(10,0)]),target_mode='count',added_tiles=6,
                          translate_x=1,translate_y=0,auto_translate=False,regions=['bottom'])
        self.assertFalse(result['ok'])

    def test_independent_audit_rejects_manual_mirrored_height_forgery(self):
        raw=source(towers=((3,10,6,0),(13,10,6,0)))
        result,doc=self.successful(raw,target_mode='manual',operation='existing',tower_counts=[9,9])
        from scripts.tower_reducer.verification import verify_output
        from scripts.tower_reducer.increase import rewrite_increase
        plan=result['plan']
        forged=replace(plan,towers=(replace(plan.towers[0],final_count=6,tile_ids=()),
                                  replace(plan.towers[1],final_count=12,tile_ids=(-10,)*6)))
        output=rewrite_increase(raw,forged,'moreToriginal')
        self.assertFalse(verify_output(raw,output,forged,'moreToriginal').ok)

    def test_independent_audit_rejects_original_platform_order_inversion(self):
        raw=source(towers=((2,12,3,0),(14,12,6,0)))
        result,doc=self.successful(raw,target_mode='count',added_tiles=6,operation='existing',height_mode='difference')
        from scripts.tower_reducer.verification import verify_output
        from scripts.tower_reducer.increase import rewrite_increase
        plan=result['plan']
        forged=replace(plan,towers=(replace(plan.towers[0],final_count=9,tile_ids=(-10,)*6),
                                  replace(plan.towers[1],final_count=6,tile_ids=())))
        output=rewrite_increase(raw,forged,'moreToriginal')
        self.assertFalse(verify_output(raw,output,forged,'moreToriginal').ok)

    def test_independent_audit_rejects_manual_intent_shift_between_unequal_platforms(self):
        raw=source(towers=((2,12,3,0),(14,12,6,0)))
        result,doc=self.successful(raw,target_mode='manual',operation='existing',tower_counts=[6,9])
        from scripts.tower_reducer.verification import verify_output
        from scripts.tower_reducer.increase import rewrite_increase
        plan=result['plan']
        forged=replace(plan,towers=(replace(plan.towers[0],final_count=3,tile_ids=()),
                                  replace(plan.towers[1],final_count=12,tile_ids=(-10,)*6)))
        output=rewrite_increase(raw,forged,'moreToriginal')
        self.assertFalse(verify_output(raw,output,forged,'moreToriginal').ok)


if __name__ == '__main__':
    unittest.main()
