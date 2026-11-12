"""Apply the TemplateId / moved XYZ and TileId / zero-tower wire whitelist.

This module performs no I/O. Geometry is checked independently by verification;
callers must verify the read-back bytes before publishing them.
"""

from dataclasses import dataclass, fields, replace
from decimal import Decimal
from types import MappingProxyType
from typing import get_args, get_origin

from .distribution import DistributionFailure, DistributionOptions, DistributionPlan, ratio_tile_bounds, REPLENISH_TYPES, MIXED_TYPES
from .inspection import InspectionIssue
from .placement import LayerCapacity, Placement, PlacementFailure, PlacementOptions, PlacementPlan
from .protobuf_wire import encode_int32, replace_length_delimited
from .template_reader import decode_template


_SNAPSHOT_RECORDS = (DistributionPlan, DistributionOptions, DistributionFailure,
                     Placement, PlacementPlan, PlacementOptions, PlacementFailure,
                     LayerCapacity, InspectionIssue)


def _ordered_annotation(annotation):
    return (annotation in (tuple, list) or get_origin(annotation) in (tuple, list)
            or any(_ordered_annotation(arg) for arg in get_args(annotation)))


def _snapshot(value):
    # Exact types are intentional: subclass protocols and unknown containers
    # can retain caller-owned state even when the outer dataclass is frozen.
    if type(value) in _SNAPSHOT_RECORDS:
        copied = {}
        for f in fields(value):
            item = getattr(value, f.name)
            optional_none = item is None and type(None) in get_args(f.type)
            if _ordered_annotation(f.type) and not optional_none and type(item) not in (tuple, list):
                raise ValueError('{} must be a built-in tuple or list'.format(f.name))
            copied[f.name] = _snapshot(item)
        return replace(value, **copied)
    if type(value) in (tuple, list):
        return tuple(_snapshot(item) for item in value)
    if type(value) in (dict, MappingProxyType):
        return MappingProxyType({_snapshot(key): _snapshot(item) for key, item in value.items()})
    if type(value) in (type(None), bool, int, float, str, bytes, Decimal):
        return value
    raise ValueError('unsupported plan snapshot type: {}'.format(type(value).__name__))


@dataclass(frozen=True)
class RewritePlan:
    distribution: DistributionPlan
    placement: PlacementPlan
    distribution_options: DistributionOptions
    placement_options: PlacementOptions

    def __post_init__(self):
        # Frozen dataclasses alone do not freeze a caller-supplied list inside
        # their annotated tuple fields. Own a snapshot of all nested evidence.
        for item in fields(self):
            object.__setattr__(self, item.name, _snapshot(getattr(self, item.name)))


def _validate(document, plan, new_id):
    if not isinstance(new_id, str) or not new_id or new_id == document.template_id:
        raise ValueError('new_id must be a nonempty string different from the source ID')
    try:
        new_id.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError('new_id must be valid UTF-8') from exc
    if not isinstance(plan, RewritePlan):
        raise ValueError('an immutable RewritePlan is required')
    distribution, placement = plan.distribution, plan.placement
    if not isinstance(distribution, DistributionPlan) or not isinstance(placement, PlacementPlan):
        raise ValueError('successful distribution and placement plans are required')
    if distribution.ok is not True or placement.ok is not True:
        raise ValueError('both plans must be successful')
    retained = distribution.retained_counts
    options = plan.distribution_options
    minimum, cap = ratio_tile_bounds(len(document.tiles), options.ratio, options.min_ratio)
    if (options.replenish_type not in REPLENISH_TYPES or sum(retained) < minimum
            or sum(retained) > cap or (options.min_ratio > 0 and not document.tiles)):
        raise ValueError('tower population or replenishment options violate the requested range')
    if distribution.moved_tile_indices and options.replenish_type != 'auto' and distribution.move_type != options.replenish_type:
        raise ValueError('replenishment type does not match the explicit selection')
    if len(retained) != len(document.stacks):
        raise ValueError('retained_counts does not match source StackData occurrences')
    anchors = [(s.x, s.y) for s in document.stacks]
    if len(set(anchors)) != len(anchors):
        raise ValueError('ambiguous StackData anchor')
    expected, deleted = [], set()
    for stack, count in zip(document.stacks, retained):
        members = sorted((t for t in document.tiles if (t.x, t.y) == (stack.x, stack.y)), key=lambda t: (t.z, t.index))
        if not members or [t.z for t in members] != list(range(len(members))):
            raise ValueError('source tower must contain a continuous Z prefix starting at zero')
        if any(t.tile_id not in (-10, -11) for t in members):
            raise ValueError('source tower contains a special Tile')
        if type(count) is not int or not 0 <= count <= len(members):
            raise ValueError('invalid retained count')
        expected.extend(t.index for t in members[count:])
        if not count:
            deleted.add(stack.top_field_index)
    moved = distribution.moved_tile_indices
    if (type(distribution.move_count) is not int or distribution.move_count != len(moved)
            or any(type(i) is not int or not 0 <= i < len(document.tiles) for i in moved)
            or len(set(moved)) != len(moved) or set(moved) != set(expected)):
        raise ValueError('moved indices must exactly match the continuous source tower top suffixes')
    replenish_id = None
    replenishment_ids = distribution.replenishment_ids
    if replenishment_ids is not None:
        if (type(replenishment_ids) is not tuple or len(replenishment_ids) != len(moved)
                or any(type(i) is not int or i not in (-10,-11) for i in replenishment_ids)
                or options.replenish_type not in MIXED_TYPES
                or distribution.removed_counts is None or distribution.final_counts is None):
            raise ValueError('invalid mixed replenishment TileId sequence')
        replenish_id = dict(zip(sorted(moved), replenishment_ids))
        if options.replenish_type == 'preserve':
            if any(replenish_id[i] != document.tiles[i].tile_id for i in moved):
                raise ValueError('preserve must keep every moved TileId')
        else:
            other = -11 if options.replenish_type == 'majority_A' else -10
            base = sum(t.tile_id == other for t in document.tiles if t.index not in moved)
            if replenishment_ids.count(other) != (-base) % 3:
                raise ValueError('majority mode must maximize requested type with only modulo-three corrections')
    elif options.replenish_type in MIXED_TYPES:
        raise ValueError('mixed modes require explicit per-Tile replenishment IDs')
    if distribution.removed_counts is not None or distribution.final_counts is not None:
        for name in ('removed_counts', 'final_counts'):
            population = getattr(distribution, name)
            if (type(population) is not tuple or len(population) != 2
                    or any(type(n) is not int or n < 0 for n in population)):
                raise ValueError('{} must contain nonnegative integer A/B counts'.format(name))
        original_counts = tuple(sum(t.tile_id == tile_id for t in document.tiles) for tile_id in (-10, -11))
        removed = tuple(sum(document.tiles[i].tile_id == tile_id for i in moved) for tile_id in (-10, -11))
        if distribution.removed_counts != removed:
            raise ValueError('removed_counts does not match source tower suffixes')
        if moved:
            if distribution.move_type not in ('A', 'B') + MIXED_TYPES:
                raise ValueError('moved Tiles require an A/B replenishment type')
            a, b = original_counts
            required = 'A' if not b or (a and a < b) else 'B' if not a or b < a else None
            if options.replenish_type == 'auto' and required is not None and distribution.move_type != required:
                raise ValueError('replenishment type must be the sole or less numerous source type')
            if replenishment_ids is None:
                replenish_id = -10 if distribution.move_type == 'A' else -11
        elif distribution.move_type is not None:
            raise ValueError('an empty movement must not record a replenishment type')
        final = tuple(original - removed_count + (replenishment_ids.count(tile_id) if replenishment_ids is not None else len(moved) if tile_id == replenish_id else 0)
                      for original, removed_count, tile_id in zip(original_counts, removed, (-10, -11)))
        if distribution.final_counts != final or any(n % 3 for n in final):
            raise ValueError('final_counts must match recoloring and each be divisible by three')
    destinations = {}
    for p in placement.placements:
        index = p.tile_index
        if type(index) is not int or index in destinations or not 0 <= index < len(document.tiles):
            raise ValueError('placement Tile indices must be valid and unique')
        if any(type(n) is not int or not -(1 << 31) <= n < (1 << 31) for n in (p.x, p.y, p.z)):
            raise ValueError('placement XYZ must be signed int32 integers')
        destinations[index] = (p.x, p.y, p.z)
    if set(destinations) != set(moved):
        raise ValueError('destinations must exactly match moved indices')
    offset=placement.original_z_offset
    if (type(offset) is not int or not 0<=offset<=plan.placement_options.max_added_layers
            or (offset and (placement.strategy_version not in ('orbit-v4-directional-bands','orbit-v5-partial-support','orbit-v6-directional-support','orbit-v7-surface-support','orbit-v8-inherited-source-minority')
                            or plan.placement_options.strategy=='top_first'))):
        raise ValueError('invalid original ordinary Z offset')
    if offset:
        for tile in document.tiles:
            if tile.index not in moved and (tile.x,tile.y) not in set(anchors):
                destinations[tile.index]=(tile.x,tile.y,tile.z+offset)
    return destinations, deleted, replenish_id


def _tile_payload(tile, destination, replenish_id):
    original = (tile.x, tile.y, tile.z, tile.tile_id)
    wanted = destination + (tile.tile_id if replenish_id is None else replenish_id,)
    last = {f.number: i for i, f in enumerate(tile.fields) if f.number in (1, 2, 3, 4)}
    parts = []
    for i, f in enumerate(tile.fields):
        number = f.number
        if number in last and i == last[number] and original[number-1] != wanted[number-1]:
            parts.append(f.tag_raw + encode_int32(wanted[number-1]))
        else:
            parts.append(f.raw)
    if 3 not in last and destination[2] != 0:
        parts.append(b'\x18' + encode_int32(destination[2]))
    return b''.join(parts)


def rewrite_template(source_bytes: bytes, combined_plan: RewritePlan, new_id: str) -> bytes:
    """Return a complete new byte string or raise ValueError; never a partial result."""
    from .manual_edit import ManualEditPlan, rewrite_manual, TowerRemovalPlan, tower_removal_fields
    if type(combined_plan) is TowerRemovalPlan:
        return b''.join(tower_removal_fields(source_bytes,combined_plan,new_id))
    if type(combined_plan) is ManualEditPlan:
        return rewrite_manual(source_bytes, combined_plan, new_id)
    from .translation import TranslationPlan, rewrite_translation
    if type(combined_plan) is TranslationPlan:
        return rewrite_translation(source_bytes, combined_plan, new_id)
    from .increase import IncreasePlan, rewrite_increase
    if type(combined_plan) is IncreasePlan:
        return rewrite_increase(source_bytes, combined_plan, new_id)
    from .ordinary_add import OrdinaryAddPlan, rewrite_ordinary_add
    if type(combined_plan) is OrdinaryAddPlan:
        return rewrite_ordinary_add(source_bytes, combined_plan, new_id)
    document = decode_template(source_bytes)
    destinations, deleted, replenish_id = _validate(document, combined_plan, new_id)
    tiles = {tile.top_field_index: tile for tile in document.tiles}
    parts = []
    for i, f in enumerate(document.fields):
        if i in deleted:
            continue
        if i == document.template_id_field_index:
            parts.append(replace_length_delimited(f, new_id.encode('utf-8')))
        elif i in tiles and tiles[i].index in destinations:
            tile_id = ((replenish_id.get(tiles[i].index) if isinstance(replenish_id, dict) else replenish_id)
                       if tiles[i].index in combined_plan.distribution.moved_tile_indices else None)
            parts.append(replace_length_delimited(f, _tile_payload(tiles[i], destinations[tiles[i].index], tile_id)))
        else:
            parts.append(f.raw)
    return b''.join(parts)
