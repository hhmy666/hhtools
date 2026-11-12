"""Wire-level contracts use synthetic bytes as well as real planner inputs."""

from collections import deque, UserList, UserDict
from dataclasses import FrozenInstanceError, dataclass, replace
import unittest

from scripts.tower_reducer.distribution import DistributionOptions, DistributionPlan, plan_tower_distribution
from scripts.tower_reducer.inspection import inspect_template
from scripts.tower_reducer.placement import Placement, PlacementOptions, PlacementPlan, plan_placements
from scripts.tower_reducer.protobuf_wire import encode_int32, encode_varint, parse_fields, replace_length_delimited
from scripts.tower_reducer.template_reader import decode_template
from scripts.tower_reducer.rewrite import RewritePlan, rewrite_template


def scalar(number, value):
    return encode_varint(number << 3) + encode_int32(value)


def message(number, payload, noncanonical=False):
    size = encode_varint(len(payload))
    if noncanonical:
        size = size[:-1] + bytes((size[-1] | 128, 0))
    return encode_varint(number << 3 | 2) + size + payload


# Unknown wire 0/1/2/5, including a noncanonical tag and value.
UNKNOWN = bytes.fromhex('f800aa00810101020304050607088a010278798d0109080706')


def synthetic(kind='mixed', second_tower=False, missing_z=False):
    """A-only/B-only/AB; three quarter-overlap columns with no source symmetry."""
    base_id = -11 if kind == 'B' else -10
    tower_id = -10 if kind == 'A' else -11
    points = [(4,4,0),(8,4,0),(12,4,0),(5,5,1),(9,5,1),(13,5,1)]
    if second_tower:
        points += [(4,4,2),(8,4,2),(12,4,2)]
    parts = [message(2,b'old'), scalar(1,1), message(2,b'sample',True),
             message(3,b'dupe'), scalar(4,17), scalar(5,19), UNKNOWN]
    for x,y,z in points:
        parts.append(message(6,scalar(1,x)+scalar(2,y)+scalar(3,z)+scalar(4,base_id)+UNKNOWN))
    for z in range(3):
        body = scalar(1,7)+scalar(2,8)+b'\x88\x00\x82\x00'+b'\x90\x00\x90\x00'
        if not missing_z or z:
            body += scalar(3,9)+b'\x98\x00'+bytes((z|128,0))
        body += scalar(4,tower_id)+scalar(5,0)+UNKNOWN
        parts.append(message(6,body,True))
    # Effective anchor is (2,16), earlier duplicated values must not bind it.
    parts.append(message(7,scalar(1,9)+scalar(2,9)+scalar(1,2)+scalar(2,16)+scalar(3,2)+UNKNOWN,True))
    if second_tower:
        for z in range(3):
            parts.append(message(6,scalar(1,14)+scalar(2,16)+scalar(3,z)+scalar(4,base_id)))
        parts.append(message(7,scalar(1,14)+scalar(2,16)+scalar(3,2)+UNKNOWN,True))
    parts += [message(8,b'\x08\x01'), UNKNOWN]
    raw = b''.join(parts)
    start = len(points)
    retained = (0,3) if second_tower else (0,)
    ratio = .25 if second_tower else 0
    cap = 3 if second_tower else 0
    distribution = DistributionPlan(retained,tuple(range(start,start+3)), 'A' if kind=='A' else 'B',3,cap,3/15 if second_tower else 0)
    z = 3 if second_tower else 2
    destinations = ((5,5),(9,5),(13,5)) if second_tower else ((4,4),(8,4),(12,4))
    placements = tuple(Placement(x,y,z,(1,),((x,y),),start+i) for i,(x,y) in enumerate(destinations))
    plan = RewritePlan(distribution,PlacementPlan(placements,'top_first',0,added_layers=1),
                       DistributionOptions(ratio=ratio,retained_counts=retained),PlacementOptions())
    return raw, plan


def computed_plan(raw, distribution_options=DistributionOptions(), placement_options=PlacementOptions()):
    doc = decode_template(raw)
    inspection = inspect_template(doc)
    distribution = plan_tower_distribution(doc, inspection, distribution_options)
    placement = plan_placements(doc, inspection, distribution, placement_options)
    return RewritePlan(distribution,placement,distribution_options,placement_options)


def mixed_replenishment():
    """Source A=8/B=1; move ABA to B, yielding A=6/B=3.

    Repeated TileId occurrences on the recolored records prove only the
    effective last scalar changes, while earlier IDs and unknowns stay raw.
    """
    raw, plan = synthetic()
    source = decode_template(raw)
    changed_tops = {source.tiles[i].top_field_index for i in (6, 8)}
    raw = b''.join(replace_length_delimited(f, f.payload + scalar(4, -10))
                   if i in changed_tops else f.raw for i, f in enumerate(source.fields))
    plan = replace(plan, distribution=replace(plan.distribution,
                   removed_counts=(2, 1), final_counts=(6, 3)))
    return raw, plan


class RewriteTests(unittest.TestCase):
    def test_mixed_suffix_recolors_only_effective_moved_tile_ids(self):
        raw, plan = mixed_replenishment()
        source = decode_template(raw)
        result = decode_template(rewrite_template(raw, plan, 'new'))
        self.assertEqual([t.tile_id for t in result.tiles], [-10] * 6 + [-11] * 3)
        for before, after in zip(source.tiles[:6], result.tiles[:6]):
            self.assertEqual(before.fields, after.fields)
        for i in (6, 8):
            before, after = source.tiles[i], result.tiles[i]
            self.assertEqual([f.raw for f in before.fields if f.number not in (1, 2, 3, 4)],
                             [f.raw for f in after.fields if f.number not in (1, 2, 3, 4)])
            self.assertEqual([f.raw for f in after.fields if f.number == 4],
                             [scalar(4, -11), scalar(4, -11)])

    def test_inaccurate_population_metadata_and_invalid_final_counts_fail_closed(self):
        raw, plan = mixed_replenishment()
        for changes in ({'removed_counts': (1, 2)}, {'final_counts': (7, 2)},
                        {'removed_counts': None}, {'move_type': 'A'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                rewrite_template(raw, replace(plan, distribution=replace(plan.distribution, **changes)), 'new')

    def test_last_occurrences_only_preserve_tags_prefixes_unknowns_and_same_values(self):
        raw, plan = synthetic()
        output = rewrite_template(raw,plan,'result') # Same ID payload length preserves overlong length.
        source, result = decode_template(raw), decode_template(output)
        self.assertEqual(result.template_id,'result')
        self.assertEqual(result.fields[0].raw,source.fields[0].raw)
        self.assertEqual(result.fields[2].length_raw,b'\x86\x00')
        self.assertEqual(len(result.stacks),0)
        for i in range(6):
            self.assertEqual(result.tiles[i].fields,source.tiles[i].fields)
        for i, expected in zip(range(6,9),((4,4,2),(8,4,2),(12,4,2))):
            before, after = source.tiles[i],result.tiles[i]
            self.assertEqual((after.x,after.y,after.z),expected)
            self.assertEqual([f.raw for f in after.fields[:2]],[f.raw for f in before.fields[:2]])
            self.assertEqual(after.fields[4].raw,before.fields[4].raw) # Earlier Z.
            self.assertEqual([f.raw for f in after.fields[6:]],[f.raw for f in before.fields[6:]])
            self.assertEqual(after.fields[2].tag_raw,b'\x88\x00')
        self.assertEqual(result.tiles[8].fields[5].raw,b'\x98\x00\x82\x00') # Z unchanged.

    def test_missing_z_appends_only_when_nonzero(self):
        raw,plan=synthetic(missing_z=True)
        result=decode_template(rewrite_template(raw,plan,'new'))
        self.assertEqual(result.tiles[6].fields[-1].raw,b'\x18\x02')
        bottom=replace(plan.placement.placements[0],x=6,y=4,z=0)
        plan=replace(plan,placement=replace(plan.placement,placements=(bottom,)+plan.placement.placements[1:]))
        result=decode_template(rewrite_template(raw,plan,'new'))
        self.assertFalse(any(f.number==3 for f in result.tiles[6].fields))

    def test_delete_only_effectively_matched_zero_tower_and_preserve_other_stack_raw(self):
        raw,plan=synthetic(second_tower=True)
        source=decode_template(raw)
        result=decode_template(rewrite_template(raw,plan,'new'))
        self.assertEqual([(s.x,s.y) for s in result.stacks],[(14,16)])
        self.assertEqual(result.fields[result.stacks[0].top_field_index].raw,source.fields[source.stacks[1].top_field_index].raw)

    def test_failed_incomplete_duplicate_and_non_suffix_plans_fail_closed(self):
        raw,plan=synthetic()
        bad_distributions=[replace(plan.distribution,ok=False),replace(plan.distribution,retained_counts=()),
                           replace(plan.distribution,retained_counts=(True,)),replace(plan.distribution,retained_counts=(4,)),
                           replace(plan.distribution,moved_tile_indices=(6,6,8)),replace(plan.distribution,moved_tile_indices=(0,7,8)),
                           replace(plan.distribution,moved_tile_indices=(6,7,999)),replace(plan.distribution,move_count=2)]
        bad_placements=[replace(plan.placement,ok=False),replace(plan.placement,placements=plan.placement.placements[:2]),
                        replace(plan.placement,placements=(plan.placement.placements[0],)*3),
                        replace(plan.placement,placements=(replace(plan.placement.placements[0],x=True),)+plan.placement.placements[1:])]
        for bad in [replace(plan,distribution=d) for d in bad_distributions]+[replace(plan,placement=p) for p in bad_placements]:
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                rewrite_template(raw,bad,'new')

    def test_missing_ambiguous_and_discontinuous_towers_are_rejected(self):
        raw,plan=synthetic()
        doc=decode_template(raw)
        stack=doc.fields[doc.stacks[0].top_field_index].raw
        for broken in (raw.replace(stack,b''),raw+stack,raw.replace(b'\x98\x00\x81\x00',b'\x98\x00\x89\x00')):
            with self.subTest(broken=broken),self.assertRaises(ValueError):
                rewrite_template(broken,plan,'new')

    def test_new_id_must_be_nonempty_different_utf8(self):
        raw,plan=synthetic()
        for name in ('','sample','\ud800',None):
            with self.subTest(name=name),self.assertRaises(ValueError):
                rewrite_template(raw,plan,name)

    def test_plan_snapshots_lists_in_frozen_nested_records(self):
        raw,plan=synthetic()
        moved=list(plan.distribution.moved_tile_indices)
        placements=list(plan.placement.placements)
        retained=list(plan.distribution.retained_counts)
        snapshot=RewritePlan(replace(plan.distribution,moved_tile_indices=moved,retained_counts=retained),
                             replace(plan.placement,placements=placements),DistributionOptions(0,retained),plan.placement_options)
        moved.clear(); placements.clear(); retained.clear()
        self.assertEqual(rewrite_template(raw,snapshot,'new'),rewrite_template(raw,plan,'new'))
        with self.assertRaises(FrozenInstanceError):
            snapshot.placement_options=PlacementOptions()

    def test_plan_rejects_deque_instead_of_sharing_mutable_destinations(self):
        _,plan=synthetic()
        caller_placements=deque(plan.placement.placements)
        with self.assertRaises(ValueError):
            replace(plan,placement=replace(plan.placement,placements=caller_placements))

    def test_plan_rejects_unknown_unordered_and_single_use_containers(self):
        _,plan=synthetic()
        class MutableList(list):
            pass
        @dataclass
        class MutableRecord:
            value: list
        cases=(UserList(plan.placement.placements),MutableList(plan.placement.placements),
               set(plan.placement.placements),frozenset(plan.placement.placements),
               iter(plan.placement.placements),{p:None for p in plan.placement.placements})
        for container in cases:
            with self.subTest(container=type(container).__name__),self.assertRaises(ValueError):
                replace(plan,placement=replace(plan.placement,placements=container))
        for value in (UserList([1]),UserDict({'value':[1]}),MutableRecord([1]),object()):
            with self.subTest(value=type(value).__name__),self.assertRaises(ValueError):
                replace(plan,placement=replace(plan.placement,layers=({'nested':value},)))

    def test_plan_freezes_dictionary_evidence_and_recopies_readonly_views(self):
        _,plan=synthetic()
        nested=[1,2]
        metadata={'values':nested}
        snapshot=replace(plan,placement=replace(plan.placement,layers=(metadata,)))
        nested.append(3); metadata['added']=4
        self.assertEqual(dict(snapshot.placement.layers[0]),{'values':(1,2)})
        with self.assertRaises(TypeError):
            snapshot.placement.layers[0]['values']=(9,)
        second=replace(snapshot)
        self.assertEqual(dict(second.placement.layers[0]),{'values':(1,2)})
