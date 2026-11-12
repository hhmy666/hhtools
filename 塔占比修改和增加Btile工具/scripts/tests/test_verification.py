from dataclasses import replace
from hashlib import sha256
import unittest
from unittest.mock import patch

from scripts.tests.project_fixtures import read_project_fixture
from scripts.tests.test_rewrite import synthetic, mixed_replenishment, message, scalar, UNKNOWN, computed_plan
from scripts.tower_reducer.protobuf_wire import parse_fields, replace_length_delimited
from scripts.tower_reducer.template_reader import decode_template, encode_template
from scripts.tower_reducer.rewrite import rewrite_template
from scripts.tower_reducer.placement import PlacementOptions
from scripts.tower_reducer.distribution import DistributionOptions
from scripts.tower_reducer.verification import verify_output


def mutate_tile(raw,index,change):
    doc=decode_template(raw)
    top=doc.tiles[index].top_field_index
    return b''.join(replace_length_delimited(f,change(f.payload)) if i==top else f.raw for i,f in enumerate(doc.fields))


class VerificationTests(unittest.TestCase):
    def test_ratio_above_25_percent_is_verified_against_source_bound(self):
        raw, _ = synthetic(second_tower=True)
        plan = computed_plan(raw, DistributionOptions(ratio=.27, retained_counts=(3, 0)))
        output = rewrite_template(raw, plan, 'new')
        report = verify_output(raw, output, plan, 'new')
        self.assertTrue(report.ok, report.errors)
        forged = replace(plan, distribution_options=replace(plan.distribution_options, ratio=.41))
        report = verify_output(raw, output, forged, 'new')
        self.assertFalse(report.ok, report.facts)
        self.assertTrue(any('exceeds source' in str(issue.evidence) for issue in report.errors), report.errors)

    def test_unchanged_source_history_needs_support_but_retains_explicit_inherited_cover_warnings(self):
        from scripts.tests.test_ordinary_add_engine import source
        for points, code in [([(4,4,0),(8,4,0),(12,4,0),(4,12,2),(8,12,2),(12,12,2)],'unsupported'),
                             ([(4,4,0),(8,4,0),(12,4,0),(4,4,1),(8,4,1),(12,4,1)],'full_cover')]:
            raw=source(points)
            plan=computed_plan(raw)
            output=rewrite_template(raw,plan,'new')
            report=verify_output(raw,output,plan,'new')
            if code=='full_cover':
                self.assertTrue(report.ok,report.errors)
                self.assertEqual(len([w for w in report.warnings if w.code=='inherited_full_cover']),3)
                self.assertTrue(report.facts['strict_structure']['no_new_full_cover'])
                continue
            self.assertFalse(report.ok,report.facts)
            self.assertTrue(any(e.code==code for e in report.errors),report.errors)

    def test_mixed_suffix_replenishment_is_independently_verified(self):
        raw, plan = mixed_replenishment()
        # Build the expected recoloring separately from the production rewriter.
        doc = decode_template(rewrite_template(raw, plan, 'new'))
        for tile in doc.tiles[6:]:
            tile.tile_id = -11
        expected = encode_template(doc)
        report = verify_output(raw, expected, plan, 'new')
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.facts['tile_counts'], {'total': 9, 'A': 6, 'B': 3})
        self.assertEqual(report.facts.get('source_tile_counts'), {'total': 9, 'A': 8, 'B': 1})
        self.assertEqual(report.facts.get('type_count_changes'), {'A': -2, 'B': 2})

    def test_replenishment_does_not_authorize_unmoved_ids_or_wrong_moved_type(self):
        raw, plan = mixed_replenishment()
        output = rewrite_template(raw, plan, 'new')
        for index, before, after in ((0, -10, -11), (6, -11, -10)):
            altered = mutate_tile(output, index,
                                  lambda payload: payload.replace(scalar(4, before), scalar(4, after)))
            report = verify_output(raw, altered, plan, 'new')
            self.assertFalse(report.ok, report.facts)
        for changes in ({'removed_counts': (1, 2)}, {'final_counts': (9, 0)},
                        {'removed_counts': None}, {'move_type': 'A'}):
            forged = replace(plan, distribution=replace(plan.distribution, **changes))
            self.assertFalse(verify_output(raw, output, forged, 'new').ok)

    def test_missing_z_verifier_accepts_append_or_default_and_rejects_reordered_append(self):
        raw,plan=synthetic(missing_z=True)
        output=rewrite_template(raw,plan,'new')
        self.assertTrue(verify_output(raw,output,plan,'new').ok)
        self.assertEqual(decode_template(output).tiles[6].fields[-1].raw,b'\x18\x02')
        removed=mutate_tile(output,6,lambda p:p[:-2])
        reordered=mutate_tile(output,6,lambda p:p[-2:]+p[:-2])
        for bad in (removed,reordered):
            self.assertFalse(verify_output(raw,bad,plan,'new').ok)
        raw=plain_template([(4,4,0),(6,4,0),(10,4,0),(5,5,1),(11,5,1)],1)
        raw=mutate_tile(raw,5,lambda p:b''.join(f.raw for f in parse_fields(p) if f.number!=3))
        plan=computed_plan(raw,DistributionOptions(0),PlacementOptions(strategy='bottom_first'))
        self.assertEqual(plan.placement.placements[0].z,0)
        output=rewrite_template(raw,plan,'new')
        self.assertFalse(any(f.number==3 for f in decode_template(output).tiles[5].fields))
        self.assertTrue(verify_output(raw,output,plan,'new').ok)

    def test_all_tower_source_is_rejected_even_with_a_successful_forged_plan(self):
        raw=plain_template([],3)
        _,base=synthetic()
        distribution=replace(base.distribution,moved_tile_indices=(0,1,2),move_type='A')
        xyz=((4,4,0),(5,5,1),(4,4,2))
        placements=tuple(replace(p,tile_index=i,x=x,y=y,z=z) for i,(p,(x,y,z)) in enumerate(zip(base.placement.placements,xyz)))
        plan=replace(base,distribution=distribution,placement=replace(base.placement,placements=placements,added_layers=3),
                     placement_options=PlacementOptions(max_added_layers=3))
        report=verify_output(raw,rewrite_template(raw,plan,'new'),plan,'new')
        self.assertTrue(any(e.code=='source_no_surface' for e in report.errors),report.errors)

    def test_source_center_tower_parity_is_independently_checked(self):
        raw=read_project_fixture('mxd0057v1_m.bytes')
        plan=computed_plan(raw)
        doc=decode_template(raw)
        center=doc.stacks[2]
        extra=next(t.index for t in doc.tiles if (t.x,t.y,t.z)==(center.x,center.y,8))
        counts=(3,5,8,5,3)
        distribution=replace(plan.distribution,retained_counts=counts,
                             moved_tile_indices=plan.distribution.moved_tile_indices+(extra,),
                             move_count=21,final_ratio=24/105,removed_counts=(21,0))
        extra_placement=replace(plan.placement.placements[0],tile_index=extra,x=8,y=2,z=6)
        bad=replace(plan,distribution=distribution,
                    placement=replace(plan.placement,placements=plan.placement.placements+(extra_placement,)))
        report=verify_output(raw,rewrite_template(raw,bad,'new'),bad,'new')
        self.assertTrue(any(e.code=='tower_center_parity' for e in report.errors),report.errors)

    def test_invalid_byte_inputs_return_structured_parse_errors(self):
        raw,plan=synthetic()
        output=rewrite_template(raw,plan,'new')
        for invalid in (None,'text',bytearray(raw),memoryview(raw),[1,2]):
            for side in ('source','output'):
                with self.subTest(value=type(invalid).__name__,side=side):
                    report=verify_output(invalid if side=='source' else raw,
                                         invalid if side=='output' else output,plan,'new')
                    self.assertFalse(report.ok)
                    self.assertTrue(any(e.code=='parse' for e in report.errors),report.errors)
                    self.assertIsNone(report.facts[side+'_sha256'])

    def test_ratio_extreme_integer_returns_structured_plan_error(self):
        raw,plan=synthetic()
        output=rewrite_template(raw,plan,'new')
        for ratio in (10**1000,-10**1000):
            bad=replace(plan,distribution_options=replace(plan.distribution_options,ratio=ratio))
            report=verify_output(raw,output,bad,'new')
            self.assertFalse(report.ok)
            self.assertTrue(any(e.code=='plan' for e in report.errors),report.errors)

    def test_b_only_ab_debuff_and_all_unknown_wire_types_pass_with_audit(self):
        for kind in ('A','B','mixed'):
            for second in (False,True):
                with self.subTest(kind=kind,second=second):
                    raw,plan=synthetic(kind,second)
                    output=rewrite_template(raw,plan,'new')
                    report=verify_output(raw,output,plan,'new')
                    self.assertTrue(report.ok,report.errors)
                    self.assertEqual(report.facts['source_sha256'],sha256(raw).hexdigest())
                    self.assertEqual(report.facts['moves'][0]['from'],(2,16,0))
                    self.assertEqual(report.facts['moves'][0]['to'],(5,5,3) if second else (4,4,2))

    def test_whitelist_rejects_every_unauthorized_field_and_occurrence_change(self):
        raw,plan=synthetic()
        output=rewrite_template(raw,plan,'new')
        d=decode_template(output)
        mutations={
            'unknown_top':output.replace(UNKNOWN,UNKNOWN[:-1]+b'\x07',1),
            'version':output.replace(scalar(1,1),scalar(1,2),1),
            'width':output.replace(scalar(4,17),scalar(4,18),1),
            'height':output.replace(scalar(5,19),scalar(5,20),1),
            'duplicate_id':output.replace(b'dupe',b'DUPE',1),
            'debuff_message':output.replace(message(8,b'\x08\x01'),message(8,b'\x08\x02')),
            'early_template_id':output.replace(message(2,b'old'),message(2,b'OLD'),1),
            'tile_id':mutate_tile(output,6,lambda p:p.replace(scalar(4,-11),scalar(4,-10))),
            'debuff_index':mutate_tile(output,6,lambda p:p.replace(scalar(5,0),scalar(5,1))),
            'early_x':mutate_tile(output,6,lambda p:p.replace(scalar(1,7),scalar(1,8),1)),
            'early_y':mutate_tile(output,6,lambda p:p.replace(scalar(2,8),scalar(2,7),1)),
            'early_z':mutate_tile(output,6,lambda p:p.replace(scalar(3,9),scalar(3,8),1)),
            'tile_unknown':mutate_tile(output,6,lambda p:p.replace(UNKNOWN,UNKNOWN[::-1])),
            'coordinate_tag':mutate_tile(output,6,lambda p:p.replace(b'\x88\x00',b'\x08',1)),
            'reencoded_same_z':mutate_tile(output,8,lambda p:p.replace(b'\x98\x00\x82\x00',b'\x98\x00\x02',1)),
            'unmoved_x':mutate_tile(output,0,lambda p:p.replace(scalar(1,4),scalar(1,3),1)),
            'nested_order':mutate_tile(output,6,lambda p:b''.join(f.raw for f in reversed(parse_fields(p)))),
            'top_order':b''.join(f.raw for f in reversed(d.fields)),
            'tile_order':b''.join(d.fields[d.tiles[1].top_field_index].raw if i==d.tiles[0].top_field_index else
                                  d.fields[d.tiles[0].top_field_index].raw if i==d.tiles[1].top_field_index else f.raw for i,f in enumerate(d.fields)),
            'missing_deletion':output+decode_template(raw).fields[decode_template(raw).stacks[0].top_field_index].raw,
            'truncated':output[:-1],
        }
        for reason,broken in mutations.items():
            with self.subTest(reason=reason):
                report=verify_output(raw,broken,plan,'new')
                self.assertFalse(report.ok,reason)
                self.assertTrue(report.errors)

    def test_each_unknown_wire_type_and_length_encoding_is_protected(self):
        raw,plan=synthetic()
        output=rewrite_template(raw,plan,'new')
        for field in parse_fields(UNKNOWN):
            changed=field.raw[:-1]+bytes((field.raw[-1]^1,))
            for scope in ('top','nested'):
                with self.subTest(wire=field.wire_type,scope=scope):
                    broken=(output.replace(field.raw,changed,1) if scope=='top' else
                            mutate_tile(output,6,lambda p:p.replace(field.raw,changed,1)))
                    self.assertFalse(verify_output(raw,broken,plan,'new').ok)
        for number in (2,6):
            fields=decode_template(output).fields
            index=next(i for i,f in enumerate(fields) if f.number==number and (number!=2 or f.payload==b'new'))
            field=fields[index]
            broken=b''.join(message(f.number,f.payload,True) if i==index else f.raw for i,f in enumerate(fields))
            self.assertFalse(verify_output(raw,broken,plan,'new').ok)

    def test_retained_stack_direction_and_unknown_bytes_cannot_change(self):
        raw,plan=synthetic(second_tower=True)
        output=rewrite_template(raw,plan,'new')
        doc=decode_template(output)
        index=doc.stacks[0].top_field_index
        stack=doc.fields[index]
        for body in (stack.payload.replace(scalar(3,2),scalar(3,1)),stack.payload.replace(UNKNOWN,UNKNOWN[:-1]+b'\x07')):
            broken=b''.join(replace_length_delimited(f,body) if i==index else f.raw for i,f in enumerate(doc.fields))
            self.assertFalse(verify_output(raw,broken,plan,'new').ok)
        broken=b''.join(f.raw for i,f in enumerate(doc.fields) if i!=index)
        self.assertFalse(verify_output(raw,broken,plan,'new').ok)

    def test_expected_id_is_required_and_cannot_be_inferred_from_output(self):
        raw,plan=synthetic()
        output=rewrite_template(raw,plan,'new')
        self.assertFalse(verify_output(raw,output,plan,'different').ok)
        with self.assertRaises(TypeError):
            verify_output(raw,output,plan)

    def test_independence_from_planners_and_rewriter(self):
        raw,plan=synthetic()
        output=rewrite_template(raw,plan,'new')
        with patch('scripts.tower_reducer.distribution.plan_tower_distribution',side_effect=AssertionError('planner called')), \
             patch('scripts.tower_reducer.placement.plan_placements',side_effect=AssertionError('planner called')), \
             patch('scripts.tower_reducer.rewrite.rewrite_template',side_effect=AssertionError('rewriter called')):
            self.assertTrue(verify_output(raw,output,plan,'new').ok)

    def test_illegal_geometry_is_rejected_even_when_output_matches_tampered_plan(self):
        raw,plan=synthetic()
        for destination,code in (((1,4,2),'safe_bounds'),((16,4,2),'board_bounds'),((4,4,1),'same_layer_collision'),
                                 ((5,5,2),'full_cover'),((10,12,2),'unsupported'),((4,5,2),'profile'),
                                 ((4,4,4),'non_tower_z_gap')):
            with self.subTest(destination=destination):
                p=replace(plan.placement.placements[0],x=destination[0],y=destination[1],z=destination[2])
                broken=replace(plan,placement=replace(plan.placement,placements=(p,)+plan.placement.placements[1:]))
                report=verify_output(raw,rewrite_template(raw,broken,'new'),broken,'new')
                self.assertFalse(report.ok)
                self.assertTrue(any(code in e.code for e in report.errors),report.errors)

    def test_ratio_cap_type_and_option_metadata_cannot_self_certify(self):
        raw,plan=synthetic(second_tower=True)
        output=rewrite_template(raw,plan,'new')
        for changes in ({'cap':999},{'final_ratio':0},{'move_type':'A'},{'move_count':1}):
            broken=replace(plan,distribution=replace(plan.distribution,**changes))
            self.assertFalse(verify_output(raw,output,broken,'new').ok)
        for option in (replace(plan.distribution_options,ratio=.20),replace(plan.distribution_options,ratio=0)):
            self.assertFalse(verify_output(raw,output,replace(plan,distribution_options=option),'new').ok)
        for option in (PlacementOptions(max_tiles_per_layer=2),PlacementOptions(max_added_layers=0)):
            self.assertFalse(verify_output(raw,output,replace(plan,placement_options=option),'new').ok)

    def test_configurable_layer_limits_and_preserved_historical_safe_bounds(self):
        raw=read_project_fixture('mxd5010_m.bytes')
        plan=computed_plan(raw,placement_options=PlacementOptions(max_tiles_per_layer=40,max_added_layers=4))
        output=rewrite_template(raw,plan,'new')
        report=verify_output(raw,output,plan,'new')
        self.assertTrue(report.ok,report.errors)
        raw=read_project_fixture('hr0029_m.bytes')
        plan=computed_plan(raw)
        report=verify_output(raw,rewrite_template(raw,plan,'new'),plan,'new')
        self.assertTrue(report.ok,report.errors)
        self.assertTrue(any(w.code=='existing_safe_bounds' for w in report.warnings))

    def test_symmetry_is_recomputed_using_source_axes(self):
        raw=read_project_fixture('hr0029_m.bytes')
        plan=computed_plan(raw)
        first=plan.placement.placements[0]
        p=replace(first,x=first.x+1)
        broken=replace(plan,placement=replace(plan.placement,placements=(p,)+plan.placement.placements[1:]))
        report=verify_output(raw,rewrite_template(raw,broken,'new'),broken,'new')
        self.assertTrue(any(e.code=='source_symmetry' for e in report.errors),report.errors)

    def test_zero_tower_remnant_wrong_prefix_and_unchanged_moved_tile_are_rejected(self):
        raw,plan=synthetic()
        p=replace(plan.placement.placements[0],x=2,y=16,z=0)
        broken=replace(plan,placement=replace(plan.placement,placements=(p,)+plan.placement.placements[1:]))
        report=verify_output(raw,rewrite_template(raw,broken,'new'),broken,'new')
        self.assertFalse(report.ok)
        self.assertTrue(any(e.code=='moved_indices' for e in report.errors),report.errors)

    def test_tower_mirror_heights_are_independently_checked(self):
        raw=read_project_fixture('hr0029_m.bytes')
        plan=computed_plan(raw)
        doc=decode_template(raw)
        counts=(5,7,6,6)
        moved=[]
        for s,n in zip(doc.stacks,counts):
            group=sorted((t for t in doc.tiles if (t.x,t.y)==(s.x,s.y)),key=lambda t:t.z)
            moved.extend(t.index for t in group[n:])
        distribution=replace(plan.distribution,retained_counts=counts,moved_tile_indices=tuple(moved))
        placements=tuple(replace(p,tile_index=i) for p,i in zip(plan.placement.placements,sorted(moved)))
        broken=replace(plan,distribution=distribution,placement=replace(plan.placement,placements=placements))
        report=verify_output(raw,rewrite_template(raw,broken,'new'),broken,'new')
        self.assertFalse(report.ok)
        self.assertTrue(any(e.code=='tower_symmetry' for e in report.errors),report.errors)

    def test_all_four_strategies_have_independently_valid_golden_geometry(self):
        for name in ('mxd0057v1','hr0029','hr0044','mxd5010'):
            raw=read_project_fixture(name+'_m.bytes')
            for strategy in ('top_first','bottom_first','balanced','seeded_random'):
                with self.subTest(name=name,strategy=strategy):
                    plan=computed_plan(raw,placement_options=PlacementOptions(strategy=strategy,seed=42))
                    output=rewrite_template(raw,plan,'new')
                    report=verify_output(raw,output,plan,'new')
                    self.assertTrue(report.ok,report.errors)

    def test_malformed_plan_evidence_returns_a_failed_report(self):
        raw,plan=synthetic()
        output=rewrite_template(raw,plan,'new')
        for bad in (None,replace(plan,placement_options=None)):
            with self.subTest(bad=bad):
                self.assertFalse(verify_output(raw,output,bad,'new').ok)
        # Ordered model fields are now rejected earlier by the snapshot boundary.
        with self.assertRaises(ValueError):
            replace(plan,placement=replace(plan.placement,placements=(replace(plan.placement.placements[0],profile=None),)+plan.placement.placements[1:]))

    def test_moving_away_records_old_collision_without_rejecting_unchanged_board(self):
        raw,plan=synthetic()
        doc=decode_template(raw)
        for tile in doc.tiles[6:]:
            tile.x,tile.y=3,4
        # Encoder deliberately cannot edit StackData: replace its wire explicitly.
        source=encode_template(doc)
        fields=decode_template(source).fields
        raw=b''.join(message(7,scalar(1,3)+scalar(2,4)) if f.number==7 else f.raw for f in fields)
        output=rewrite_template(raw,plan,'new')
        report=verify_output(raw,output,plan,'new')
        self.assertTrue(report.ok, report.errors)
        self.assertTrue(any(e.code=='source_same_layer_collision' for e in report.warnings),report.warnings)
        self.assertEqual(decode_template(output).tiles[:6], decode_template(raw).tiles[:6])

    def test_deleted_stack_anchor_may_be_reused_by_an_ordinary_moved_tile(self):
        raw=plain_template([(4,14,0),(3,15,1)],1)
        plan=computed_plan(raw,DistributionOptions(ratio=0))
        p=replace(plan.placement.placements[0],x=2,y=16,z=2,profile=(1,))
        plan=replace(plan,placement=replace(plan.placement,placements=(p,),added_layers=1))
        # The old tower anchor (2,16) now holds a non-tower Tile at Z=2.
        output=rewrite_template(raw,plan,'new')
        self.assertEqual(decode_template(output).stacks,[])
        report=verify_output(raw,output,plan,'new')
        self.assertTrue(report.ok,report.errors)
        self.assertEqual(report.facts['retained_counts'],(0,))

    def test_single_area_profiles_quarter_half_and_one_on_four(self):
        cases=[([(4,4,0),(5,5,1)],1,(1,)),
               ([(4,4,0),(4,5,1)],1,(2,)),
               ([(4,4,0),(6,4,0),(4,6,0),(6,6,0),
                 (10,4,0),(12,4,0),(10,6,0),(12,6,0),(5,5,1)],3,(1,1,1,1))]
        for points,count,profile in cases:
            with self.subTest(profile=profile):
                raw=plain_template(points,count)
                plan=computed_plan(raw)
                self.assertTrue(plan.placement.ok,plan.placement)
                output=rewrite_template(raw,plan,'new')
                report=verify_output(raw,output,plan,'new')
                self.assertTrue(report.ok,report.errors)
                self.assertEqual(report.facts['source_profiles'],(profile,))
                self.assertTrue(all(p.profile==profile for p in plan.placement.placements))

    def test_more_than_two_new_layers_are_allowed_by_options(self):
        raw=plain_template([(4,4,0),(5,5,1)],7)
        plan=computed_plan(raw,DistributionOptions(0),PlacementOptions(max_tiles_per_layer=1,max_added_layers=7))
        report=verify_output(raw,rewrite_template(raw,plan,'new'),plan,'new')
        self.assertTrue(report.ok,report.errors)
        self.assertEqual(report.facts['added_layers'],7)

    def test_new_layer_mixed_profiles_and_affected_upper_profile_are_rejected(self):
        raw=plain_template([(4,4,0),(6,4,0),(10,4,0),(5,5,1),(11,5,1)],4)
        plan=computed_plan(raw,DistributionOptions(0),PlacementOptions(max_tiles_per_layer=2,max_added_layers=2))
        p=replace(plan.placement.placements[0],x=5,y=4,z=2,profile=(2,))
        bad=replace(plan,placement=replace(plan.placement,placements=(p,)+plan.placement.placements[1:]))
        report=verify_output(raw,rewrite_template(raw,bad,'new'),bad,'new')
        self.assertTrue(any(e.code=='new_layer_profile' for e in report.errors),report.errors)
        raw,plan=synthetic()
        p=replace(plan.placement.placements[0],x=6,y=4,z=0)
        bad=replace(plan,placement=replace(plan.placement,placements=(p,)+plan.placement.placements[1:]))
        report=verify_output(raw,rewrite_template(raw,bad,'new'),bad,'new')
        self.assertTrue(any(e.code=='upper_profile' for e in report.errors),report.errors)


def plain_template(points,tower_count):
    """Literal geometry with a bottom-contiguous A-only tower at (2,16)."""
    xyz=points+[(2,16,z) for z in range(tower_count)]
    return (message(2,b'plain')+scalar(4,17)+scalar(5,19)
            +b''.join(message(6,scalar(1,x)+scalar(2,y)+scalar(3,z)+scalar(4,-10)) for x,y,z in xyz)
            +message(7,scalar(1,2)+scalar(2,16)))
