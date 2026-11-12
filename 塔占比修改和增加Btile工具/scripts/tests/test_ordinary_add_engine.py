"""Ordinary additions preserve original occurrences and enforce layer geometry."""
import importlib.util
import unittest
from dataclasses import replace
from pathlib import Path

from scripts.tests.project_fixtures import read_project_fixture
from scripts.tower_reducer.protobuf_wire import encode_int32, encode_varint, replace_length_delimited
from scripts.tower_reducer.template_reader import decode_template


def scalar(n, value):
    return encode_varint(n << 3) + encode_int32(value)


def message(n, payload):
    return encode_varint(n << 3 | 2) + encode_varint(len(payload)) + payload


def source(points=None, towers=()):
    points = points or [(4,4,0),(8,4,0),(12,4,0),(5,5,1),(9,5,1),(13,5,1)]
    raw = message(2,b'original')+scalar(4,17)+scalar(5,19)+message(90,b'unknown')
    for x,y,z in points:
        raw += message(6,scalar(1,x)+scalar(2,y)+scalar(3,z)+scalar(4,-10)+message(91,b'keep'))
    for x,y,count,direction in towers:
        for z in range(count):
            raw += message(6,scalar(1,x)+scalar(2,y)+scalar(3,z)+scalar(4,-10)+message(92,b'tower'))
        raw += message(7,scalar(1,x)+scalar(2,y)+scalar(3,direction)+message(93,b'stack'))
    return raw


class OrdinaryAddTests(unittest.TestCase):
    def test_automatic_two_sided_search_reaches_later_layer_allocations(self):
        # 1540296te geometry: dense early allocations must not starve later ones.
        rows=[[(4,3),(6,3),(8,3),(10,3),(12,3),(12,5),(12,7),(4,9),(6,9),(8,9),(10,9),(12,9),(4,11),(4,13),(4,15),(6,15),(8,15),(10,15),(12,15)],
              [(5,2),(7,2),(9,2),(11,2),(11,4),(11,6),(5,8),(7,8),(9,8),(11,8),(5,10),(7,10),(9,10),(11,10),(5,12),(5,14),(5,16),(7,16),(9,16),(11,16)],
              [(6,3),(8,3),(10,3),(10,5),(6,7),(8,7),(10,7),(6,11),(8,11),(10,11),(6,13),(6,15),(8,15),(10,15)],
              [(5,4),(7,4),(9,4),(5,6),(7,6),(9,6),(7,12),(9,12),(11,12),(7,14),(9,14),(11,14)],
              [(6,5),(10,5),(6,13),(10,13)]]
        result,_=self.successful(source([(x,y,z) for z,ps in enumerate(rows) for x,y in ps]),
            target_mode='total_range',min_total_tiles=90,max_total_tiles=99,placement_strategy='both')
        self.assertGreater(result['summary']['top_added_tiles'],0)
        self.assertGreater(result['summary']['bottom_added_tiles'],0)
        self.assertTrue(90<=result['summary']['final_total']<=99)

    def build(self, raw=None, **options):
        self.assertIsNotNone(importlib.util.find_spec('scripts.tower_reducer.ordinary_add'),
                             'ordinary add engine is required')
        from scripts.tower_reducer.ordinary_add import build_ordinary_add
        return build_ordinary_add(raw or source(), options, 'moreBoriginal')

    def successful(self, raw=None, **options):
        raw = raw or source()
        result = self.build(raw, **options)
        self.assertTrue(result['ok'], result.get('error'))
        from scripts.tower_reducer.ordinary_add_verification import verify_ordinary_add
        report = verify_ordinary_add(raw,result['output'],result['plan'],'moreBoriginal')
        self.assertTrue(report.ok,report.errors)
        return result, decode_template(result['output'])

    def test_adds_exact_b_without_changing_original_types_or_xy(self):
        raw = source()
        result, output = self.successful(raw, added_tiles=3, placement_strategy='top_first',
                                        layer_mode='exact',min_added_layers=1,max_added_layers=1)
        self.assertEqual(len(output.tiles),9)
        self.assertEqual([t.tile_id for t in output.tiles],[-10]*6+[-11]*3)
        self.assertEqual([t.fields for t in output.tiles[:6]], [t.fields for t in decode_template(raw).tiles])
        self.assertEqual(result['summary']['added_layers'],1)
        self.assertFalse(output.stacks)

    def test_bottom_shifts_only_original_non_tower_z_and_preserves_tower_wire(self):
        raw = source(towers=((2,16,3,2),))
        result, output = self.successful(raw, added_tiles=3, layer_mode='exact',
                                        min_added_layers=1,max_added_layers=1)
        original = decode_template(raw)
        self.assertEqual(result['plan'].original_z_offset,1)
        for before,after in zip(original.tiles,output.tiles):
            tower=(before.x,before.y)==(2,16)
            self.assertEqual((after.x,after.y,after.tile_id,after.z),(before.x,before.y,before.tile_id,before.z+(0 if tower else 1)))
            if tower:
                self.assertEqual(after.fields,before.fields)
            self.assertEqual([f.raw for f in after.fields if f.number!=3], [f.raw for f in before.fields if f.number!=3])
        self.assertEqual([s.fields for s in output.stacks],[s.fields for s in original.stacks])

    def test_explicit_a_and_exact_zero_new_layers(self):
        # There are free quarter-overlap positions on the original upper layer.
        points=[(4,4,0),(8,4,0),(12,4,0),(5,5,1),(9,5,1),(13,5,1)]
        result, output = self.successful(source(points),added_tiles=3,tile_type='A',
            placement_strategy='top_first',layer_mode='exact',min_added_layers=0,max_added_layers=0)
        self.assertEqual(result['summary']['added_layers'],0)
        self.assertEqual(len(output.tiles),9)
        self.assertTrue(all(t.tile_id==-10 for t in output.tiles))

    def test_invalid_quantity_bool_unknown_and_exact_mismatch_fail(self):
        for options in ({'added_tiles':0},{'added_tiles':4},{'added_tiles':True},
                        {'added_tiles':3.0},{'mystery':1},{'max_added_layers':True},
                        {'placement_strategy':'balanced'},
                        {'layer_mode':'exact','min_added_layers':0,'max_added_layers':1}):
            with self.subTest(options=options):
                self.assertFalse(self.build(**options)['ok'])

    def test_fails_if_source_type_groups_cannot_be_preserved(self):
        self.assertEqual(self.build(source([(4,4,0)]),added_tiles=3)['error']['code'],'grouping_conflict')

    def test_source_total_including_special_tiles_must_also_be_multiple_of_three(self):
        raw=source()+message(6,scalar(1,2)+scalar(2,10)+scalar(3,0)+scalar(4,7))
        result=self.build(raw,added_tiles=3)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'],'grouping_conflict')

    def test_independent_audit_rejects_nonmultiple_total_despite_valid_ab_groups(self):
        raw=source()+message(6,scalar(1,2)+scalar(2,10)+scalar(3,0)+scalar(4,7))
        from scripts.tower_reducer.ordinary_add import OrdinaryAddition, OrdinaryAddPlan, rewrite_ordinary_add
        from scripts.tower_reducer.ordinary_add_verification import verify_ordinary_add
        from hashlib import sha256
        additions=tuple(OrdinaryAddition(x,6,2,-11) for x in (6,10,14))
        plan=OrdinaryAddPlan(sha256(raw).hexdigest(),{'added_tiles':3,'placement_strategy':'top_first',
            'layer_mode':'exact','min_added_layers':1,'max_added_layers':1},additions,0,7)
        report=verify_ordinary_add(raw,rewrite_ordinary_add(raw,plan,'moreBoriginal'),plan,'moreBoriginal')
        self.assertFalse(report.ok)
        self.assertIn('grouping',{issue.code for issue in report.errors})

    def test_impossible_layer_count_fails_without_partial_bytes(self):
        result=self.build(added_tiles=3,layer_mode='exact',min_added_layers=4,max_added_layers=4)
        self.assertFalse(result['ok'])
        self.assertIsNone(result.get('output'))

    def test_independent_audit_rejects_recoloring_original_and_changed_unknown(self):
        raw=source()
        result,output=self.successful(raw,added_tiles=3)
        from scripts.tower_reducer.ordinary_add_verification import verify_ordinary_add
        forged=result['output'].replace(b'unknown',b'changed',1)
        self.assertFalse(verify_ordinary_add(raw,forged,result['plan'],'moreBoriginal').ok)
        old=output.tiles[0]
        forged=b''.join(replace_length_delimited(f, f.payload+scalar(4,-11)) if i==old.top_field_index else f.raw
                        for i,f in enumerate(output.fields))
        self.assertFalse(verify_ordinary_add(raw,forged,result['plan'],'moreBoriginal').ok)

    def test_independent_audit_rejects_forged_full_cover_plan(self):
        raw=source()
        result,_=self.successful(raw,added_tiles=3,placement_strategy='top_first',
                                  layer_mode='exact',min_added_layers=1,max_added_layers=1)
        from scripts.tower_reducer.ordinary_add import rewrite_ordinary_add
        from scripts.tower_reducer.ordinary_add_verification import verify_ordinary_add
        additions=result['plan'].additions
        forged=replace(result['plan'],additions=(replace(additions[0],x=5,y=5),)+additions[1:])
        output=rewrite_ordinary_add(raw,forged,'moreBoriginal')
        self.assertFalse(verify_ordinary_add(raw,output,forged,'moreBoriginal').ok)

    def test_rotation_only_source_keeps_rotation_without_inventing_mirrors(self):
        points=[(4,4,0),(7,5,0),(5,8,0),(12,12,0),(9,11,0),(11,8,0)]
        raw=source(points)
        result,_=self.successful(raw,added_tiles=6,placement_strategy='top_first',
            layer_mode='exact',min_added_layers=1,max_added_layers=1)
        by_z={}
        for tile in result['plan'].additions:
            by_z.setdefault(tile.z,set()).add((tile.x,tile.y))
        for positions in by_z.values():
            self.assertEqual(positions,{(16-x,16-y) for x,y in positions})

    def test_default_eighteen_stays_exact_and_no_added_tile_leaves_safe_bounds(self):
        result,output=self.successful()
        self.assertEqual(len(output.tiles),24)
        self.assertEqual(result['summary']['added_tiles'],18)
        self.assertLessEqual(result['summary']['added_layers'],3)
        for tile in result['plan'].additions:
            self.assertTrue(2<=tile.x<=14 and 2<=tile.y<=16)

    def test_independent_audit_rejects_tower_visual_collision_outside_anchor(self):
        raw=source(towers=((2,16,3,2),))
        result,_=self.successful(raw,added_tiles=3)
        from scripts.tower_reducer.ordinary_add import rewrite_ordinary_add
        from scripts.tower_reducer.ordinary_add_verification import verify_ordinary_add
        additions=result['plan'].additions
        forged=replace(result['plan'],additions=(replace(additions[0],x=2,y=15),)+additions[1:])
        report=verify_ordinary_add(raw,rewrite_ordinary_add(raw,forged,'moreBoriginal'),forged,'moreBoriginal')
        self.assertIn('tower_visual_collision',{issue.code for issue in report.errors})

    def test_extreme_valid_layer_request_stops_before_allocating_unbounded_slots(self):
        result=self.build(added_tiles=2_147_483_646,layer_mode='exact',
                          min_added_layers=2_147_483_646,max_added_layers=2_147_483_646)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'],'search_limit')

    def test_bottom_selection_never_fills_original_top_or_adds_upper_layers(self):
        result,_=self.successful(added_tiles=18,placement_strategy='bottom_first')
        self.assertTrue(all(t.z-result['plan'].original_z_offset<=0 for t in result['plan'].additions))

    def test_independent_audit_rejects_plan_which_changes_selected_outer_side(self):
        raw=source()
        result,_=self.successful(raw,added_tiles=3,layer_mode='exact',min_added_layers=1,max_added_layers=1)
        from scripts.tower_reducer.ordinary_add_verification import verify_ordinary_add
        forged=replace(result['plan'],options={**result['plan'].options,'placement_strategy':'top_first'})
        self.assertFalse(verify_ordinary_add(raw,result['output'],forged,'moreBoriginal').ok)

    def test_quarter_overlap_may_touch_one_or_two_supports_on_same_layer(self):
        points=[(4,4,0),(6,4,0),(10,4,0),(5,5,1),(9,5,1),(11,5,1)]
        raw=source(points)
        result,_=self.successful(raw,added_tiles=3,placement_strategy='top_first',
            layer_mode='exact',min_added_layers=0,max_added_layers=0)
        from scripts.tower_reducer.ordinary_add import OrdinaryAddition, rewrite_ordinary_add
        from scripts.tower_reducer.ordinary_add_verification import verify_ordinary_add
        # (3,5)/(7,5) each touch one corner; (5,3) touches two corners.
        additions=tuple(OrdinaryAddition(x,y,1,-11) for x,y in ((3,5),(7,5),(5,3)))
        plan=replace(result['plan'],additions=additions)
        report=verify_ordinary_add(raw,rewrite_ordinary_add(raw,plan,'moreBoriginal'),plan,'moreBoriginal')
        self.assertTrue(report.ok,report.errors)

    def test_new_layer_cannot_mix_quarter_and_half_units_even_with_mixed_history(self):
        raw=source([(4,4,0),(8,4,0),(12,4,0),(5,5,1),(8,5,1),(13,5,1)])
        from scripts.tower_reducer.ordinary_add import OrdinaryAddition, OrdinaryAddPlan, rewrite_ordinary_add
        from scripts.tower_reducer.ordinary_add_verification import verify_ordinary_add
        from hashlib import sha256
        # New layer units are quarter, half, quarter respectively.
        additions=tuple(OrdinaryAddition(x,y,2,-11) for x,y in ((6,6),(8,6),(14,6)))
        plan=OrdinaryAddPlan(sha256(raw).hexdigest(),{'added_tiles':3,'placement_strategy':'top_first',
            'layer_mode':'exact','min_added_layers':1,'max_added_layers':1},additions,0,6)
        report=verify_ordinary_add(raw,rewrite_ordinary_add(raw,plan,'moreBoriginal'),plan,'moreBoriginal')
        self.assertFalse(report.ok)
        self.assertIn('layer_stacking_style',{issue.code for issue in report.errors})

    def test_exact_two_layers_search_keeps_known_real_layouts_reachable(self):
        # The former mxd0057v1 +18/bottom fixture left originals floating.
        # +24 has complete support on both introduced layers.
        for name,side,count in (('ntxkd021','bottom_first',18),('mxd0057v1','bottom_first',24),('mxd0057v1','top_first',18)):
            with self.subTest(name=name,side=side):
                result,_=self.successful(read_project_fixture(name+'_m.bytes'),added_tiles=count,
                    placement_strategy=side,layer_mode='exact',min_added_layers=2,max_added_layers=2)
                self.assertEqual(result['summary']['added_layers'],2)

    def test_target_total_range_selects_actual_grouped_quantity(self):
        points=[(x,y,z) for z in (0,1) for x in range(3+z,12+z,2) for y in range(3+z,14+z,2)]
        raw=source(points)
        self.assertEqual(len(decode_template(raw).tiles),60)
        result,output=self.successful(raw,target_mode='total_range',min_total_tiles=90,max_total_tiles=99)
        self.assertTrue(90<=len(output.tiles)<=99)
        self.assertEqual(len(output.tiles)%3,0)
        self.assertEqual(result['summary']['final_counts'],[60,len(output.tiles)-60])
        self.assertEqual(result['plan'].options['target_mode'],'total_range')

    def test_both_sides_custom_counts_and_multiple_bottom_layers(self):
        result,output=self.successful(added_tiles=12,placement_strategy='both',split_mode='custom',
            top_tiles=3,bottom_tiles=9,layer_mode='exact',min_added_layers=3,max_added_layers=3)
        self.assertEqual(result['summary']['top_added_tiles'],3)
        self.assertEqual(result['summary']['bottom_added_tiles'],9)
        self.assertEqual(result['summary']['added_layers'],3)
        self.assertGreaterEqual(result['summary']['bottom_added_layers'],2)
        from scripts.tower_reducer.geometry import overlap_area
        floor=min(t.z for t in output.tiles)
        for tile in output.tiles:
            if tile.z>floor:
                self.assertTrue(any(t.z==tile.z-1 and overlap_area((t.x,t.y),(tile.x,tile.y)) in (1,2)
                                    for t in output.tiles),(tile.x,tile.y,tile.z))

    def test_both_sides_auto_keeps_both_nonempty(self):
        result,_=self.successful(added_tiles=18,placement_strategy='both')
        self.assertGreater(result['summary']['top_added_tiles'],0)
        self.assertGreater(result['summary']['bottom_added_tiles'],0)
        self.assertEqual(result['summary']['top_added_tiles']+result['summary']['bottom_added_tiles'],18)

    def test_range_and_custom_split_conflicts_are_explained(self):
        self.assertEqual(self.build(target_mode='total_range',min_total_tiles=6,max_total_tiles=12)['error']['code'],'already_in_range')
        for options in ({'target_mode':'total_range','min_total_tiles':3,'max_total_tiles':5},
                        {'target_mode':'total_range','min_total_tiles':10,'max_total_tiles':11},
                        {'target_mode':'total_range','min_total_tiles':18,'max_total_tiles':21,
                         'placement_strategy':'both','split_mode':'custom','top_tiles':3,'bottom_tiles':3}):
            self.assertEqual(self.build(**options)['error']['code'],'target_total_unattainable')

    def test_independent_audit_rejects_floating_originals_above_new_bottom(self):
        from hashlib import sha256
        from scripts.tower_reducer.ordinary_add import OrdinaryAddition,OrdinaryAddPlan,rewrite_ordinary_add
        from scripts.tower_reducer.ordinary_add_verification import verify_ordinary_add
        raw=source()
        additions=tuple(OrdinaryAddition(x,y,0,-11) for x,y in ((3,3),(5,3),(3,5)))
        plan=OrdinaryAddPlan(sha256(raw).hexdigest(),{'added_tiles':3},additions,1,6)
        report=verify_ordinary_add(raw,rewrite_ordinary_add(raw,plan,'moreBoriginal'),plan,'moreBoriginal')
        self.assertIn('unsupported_upper',{issue.code for issue in report.errors})

    def test_independent_audit_checks_total_range_and_custom_split(self):
        from scripts.tower_reducer.ordinary_add_verification import verify_ordinary_add
        result,_=self.successful(added_tiles=6,placement_strategy='both',split_mode='custom',top_tiles=3,bottom_tiles=3)
        for changes,code in (({'target_mode':'total_range','min_total_tiles':90,'max_total_tiles':99},'quantity_or_type'),
                             ({'top_tiles':4,'bottom_tiles':2},'split_quantity')):
            plan=replace(result['plan'],options={**result['plan'].options,**changes})
            report=verify_ordinary_add(source(),result['output'],plan,'moreBoriginal')
            self.assertIn(code,{issue.code for issue in report.errors})


if __name__=='__main__':
    unittest.main()
