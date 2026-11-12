"""Independent read-back verification: recompute facts, never rerun a planner.

Wire checks align ordered occurrences, not field-number dictionaries or Counters.
Only the source parser, value encoders and pure footprint geometry are shared
with generation. No rewriter or planning helper is used to certify the output.
"""

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from hashlib import sha256
from typing import Tuple

from .distribution import DistributionOptions, DistributionPlan
from .geometry import overlap_area, overlap_profile
from .placement import Placement, PlacementOptions, PlacementPlan
from .protobuf_wire import encode_int32, encode_varint, parse_fields
from .template_reader import decode_template


@dataclass(frozen=True)
class VerificationIssue:
    code: str
    message: str
    evidence: Tuple[object, ...] = ()


@dataclass(frozen=True)
class VerificationReport:
    facts: dict
    errors: Tuple[VerificationIssue, ...]
    warnings: Tuple[VerificationIssue, ...]

    @property
    def ok(self):
        return not self.errors


def verify_tower_removal_seed(source_bytes, seed_bytes, source_sha256, new_id):
    """Certify a preview whitelist only; this does not certify final counts/structure."""
    from .protobuf_wire import replace_length_delimited
    if sha256(source_bytes).hexdigest()!=source_sha256:
        raise ValueError('删塔源文件摘要不匹配')
    source,seed=decode_template(source_bytes),decode_template(seed_bytes)
    if not new_id or new_id==source.template_id or seed.template_id!=new_id:
        raise ValueError('删塔模板ID不符合计划')
    anchors={(s.x,s.y) for s in source.stacks}
    retained_tiles={t.top_field_index for t in source.tiles if (t.x,t.y) not in anchors}
    removed_records={s.top_field_index for s in source.stacks}|{t.top_field_index for t in source.tiles if t.top_field_index not in retained_tiles}
    expected=[]
    for i,field in enumerate(source.fields):
        if i in removed_records: continue
        expected.append(replace_length_delimited(field,new_id.encode('utf-8')) if i==source.template_id_field_index else field.raw)
    if tuple(expected)!=tuple(field.raw for field in seed.fields):
        raise ValueError('删塔种子包含白名单外改动')
    return VerificationReport({'source_sha256':source_sha256,'manual_recovery':True},(),())


def _positions(tiles):
    return Counter((t.x, t.y, t.z) for t in tiles)


def _symmetries(tiles):
    if not tiles:
        return (0, 0), ()
    axes = (min(t.x for t in tiles)+max(t.x for t in tiles), min(t.y for t in tiles)+max(t.y for t in tiles))
    points = _positions(tiles)
    transforms = (('horizontal', lambda x, y: (axes[0]-x, y)),
                  ('vertical', lambda x, y: (x, axes[1]-y)),
                  ('rotation', lambda x, y: (axes[0]-x, axes[1]-y)))
    return axes, tuple(name for name, fn in transforms if Counter((*fn(x, y), z) for x, y, z in points.elements()) == points)


def _layers(tiles):
    result = defaultdict(list)
    for t in tiles:
        result[t.z].append((t.x, t.y))
    return dict(result)


def _source_origins(source,plan):
    """Independent occurrence lineage, with no call to a generator/rewriter."""
    from .manual_edit import ManualEditPlan,TowerRemovalPlan
    from .translation import TranslationPlan
    from .increase import IncreasePlan
    from .ordinary_add import OrdinaryAddPlan
    from .rewrite import RewritePlan
    if type(plan) is TranslationPlan:
        return _source_origins(source,plan.base_plan)
    if type(plan) is ManualEditPlan:
        previous=_source_origins(source,plan.base_plan)
        return [origin for i,origin in enumerate(previous) if i not in plan.deleted_indices]+[None]*len(plan.additions)
    if type(plan) is TowerRemovalPlan:
        anchors={(s.x,s.y) for s in source.stacks}
        return [t.index for t in source.tiles if (t.x,t.y) not in anchors]
    if type(plan) is RewritePlan:
        return list(range(len(source.tiles)))
    if type(plan) is IncreasePlan:
        return list(range(len(source.tiles)))+[None]*sum(t.final_count-t.original_count for t in plan.towers)
    if type(plan) is OrdinaryAddPlan:
        return list(range(len(source.tiles)))+[None]*len(plan.additions)
    raise ValueError('原牌继承需要可信的完整计划')


def verify_manual_transform(before,after,plan):
    """Independent scalar whitelist for surviving manual-edit wire records."""
    def point(item):
        x,y=item.x,item.y
        q=plan.quarter_turns
        x,y=((x,y),(17-y,x+1),(16-x,18-y),(y-1,17-x))[q]
        return x+plan.translation[0],y+plan.translation[1]

    retained=[tile for tile in before.tiles if tile.index not in plan.deleted_indices]
    if len(after.tiles)!=len(retained)+len(plan.additions):
        raise ValueError('手动微调保留牌数量不符')
    output_anchors={(tile.x,tile.y) for tile in after.tiles}
    kept_stacks=[stack for stack in before.stacks if point(stack) in output_anchors]
    if len(kept_stacks)!=len(after.stacks):
        raise ValueError('手动微调保留塔数量不符')
    for old,new in list(zip(retained,after.tiles))+list(zip(kept_stacks,after.stacks)):
        x,y=point(old)
        wanted={1:x,2:y}
        old_values={1:old.x,2:old.y}
        if hasattr(old,'direction'):
            wanted[3]=(old.direction-plan.quarter_turns)%4
            old_values[3]=old.direction
        last={f.number:i for i,f in enumerate(old.fields) if f.number in wanted}
        appended=[n for n in wanted if n not in last and wanted[n]!=old_values[n]]
        if len(new.fields)!=len(old.fields)+len(appended):
            raise ValueError('手动微调改写了非授权嵌套字段')
        for i,(left,right) in enumerate(zip(old.fields,new.fields)):
            n=left.number
            if n in wanted and i==last[n] and wanted[n]!=old_values[n]:
                if right.raw!=left.tag_raw+encode_int32(wanted[n]):
                    raise ValueError('手动微调整体变换不一致')
            elif left.raw!=right.raw:
                raise ValueError('手动微调改写了原始字段')
        for number,field in zip(appended,new.fields[len(old.fields):]):
            if field.raw!=encode_varint(number<<3)+encode_int32(wanted[number]):
                raise ValueError('手动微调新增了非授权字段')


def source_geometry_warnings(source_bytes,output_bytes=None,plan=None):
    """Source pair warnings; final pairs additionally require trusted lineage."""
    source=decode_template(source_bytes)
    anchors={(s.x,s.y) for s in source.stacks}
    ordinary=[t for t in source.tiles if (t.x,t.y) not in anchors]
    lower_by_xyz={(t.x,t.y,t.z):t for t in ordinary}
    pairs=[(lower_by_xyz[(t.x,t.y,t.z-1)],t) for t in ordinary if (t.x,t.y,t.z-1) in lower_by_xyz]
    output=decode_template(output_bytes) if output_bytes is not None else source
    if output_bytes is not None:
        origins=_source_origins(source,plan)
        if len(origins)!=len(output.tiles):raise ValueError('原牌继承索引与输出不符')
        by_origin={origin:output.tiles[i] for i,origin in enumerate(origins) if origin is not None}
    else:
        by_origin={t.index:t for t in source.tiles}
    output_anchors={(s.x,s.y) for s in output.stacks}
    result=[]
    for original_lower,original_upper in pairs:
        lower,upper=by_origin.get(original_lower.index),by_origin.get(original_upper.index)
        if (lower is None or upper is None or upper.z!=lower.z+1 or (upper.x,upper.y)!=(lower.x,lower.y)
                or (upper.x,upper.y) in output_anchors):continue
        result.append({'code':'inherited_full_cover','x':upper.x,'y':upper.y,
                       'lower_z':lower.z,'upper_z':upper.z,'lower_index':lower.index,'upper_index':upper.index})
    return result


def enforce_output_structure(report, output_bytes, *, source_bytes=None,plan=None):
    """Every final ordinary Tile needs an immediate lower support, including history.

    StackData members intentionally share XY in the wire format: their direction
    unfolds them in the editor. Source-bound plans may inherit original
    ordinary pairs; replacing either member never inherits this exception.
    """
    if not report.ok:
        return report
    try:
        document = decode_template(output_bytes)
    except (TypeError, ValueError, UnicodeError):
        return VerificationReport(report.facts, report.errors + (VerificationIssue('parse', '输出结构无法解析'),), report.warnings)
    anchors = {(s.x, s.y) for s in document.stacks}
    inherited=source_geometry_warnings(source_bytes,output_bytes,plan) if source_bytes is not None and plan is not None else []
    inherited_pairs={(item['lower_index'],item['upper_index']) for item in inherited}
    ordinary = [t for t in document.tiles if (t.x, t.y) not in anchors]
    by_z = defaultdict(list)
    all_by_z = defaultdict(list)
    for t in ordinary:
        by_z[t.z].append(t)
    for t in document.tiles:
        all_by_z[t.z].append(t)
    errors = []
    for t in ordinary:
        if t.z < 0:
            errors.append(VerificationIssue('negative_z', '普通牌层号不能为负', (t.index, t.x, t.y, t.z)))
        elif t.z > 0 and not any(overlap_area((t.x,t.y),(p.x,p.y)) for p in by_z.get(t.z-1, ())):
            errors.append(VerificationIssue('unsupported', f'普通牌 Tile {t.index} 悬空：第 {t.z+1} 层 ({t.x}, {t.y}) 未接触第 {t.z} 层；不能以上层压住代替底部承接', (t.index,t.x,t.y,t.z)))
    for z, tiles in all_by_z.items():
        lower = {(p.x,p.y):p for p in all_by_z.get(z-1, ())}
        for t in tiles:
            p = lower.get((t.x,t.y))
            if p is not None and (t.x,t.y) not in anchors and (p.index,t.index) not in inherited_pairs:
                errors.append(VerificationIssue('full_cover', f'普通牌 Tile {t.index} 与 Tile {p.index} 在第 {z+1}/{z} 层 ({t.x}, {t.y}) 一压一完全遮盖', (t.index,p.index,t.x,t.y,z)))
    facts = {**report.facts,'source_geometry_warnings':inherited,
             'strict_structure': {'ordinary_tiles':len(ordinary), 'supported':not any(e.code=='unsupported' for e in errors),
                                  'no_full_cover':not inherited and not any(e.code=='full_cover' for e in errors),
                                  'no_new_full_cover':not any(e.code=='full_cover' for e in errors)}}
    warnings = tuple(w for w in report.warnings if w.code not in ('source_unsupported','inherited_full_cover'))
    warnings+=tuple(VerificationIssue('inherited_full_cover','继承原普通牌完全覆盖：第{}→{}层 ({}, {})'.format(
        item['lower_z']+1,item['upper_z']+1,item['x'],item['y']),(item['lower_index'],item['upper_index'],item['x'],item['y'],item['lower_z'],item['upper_z'])) for item in inherited)
    return VerificationReport(facts, report.errors + tuple(errors), warnings)


def _wire_check(source, output, moved, destinations, deleted, expected_id, replenish_id, error):
    remaining = [(i, f) for i, f in enumerate(source.fields) if i not in deleted]
    if len(remaining) != len(output.fields):
        error('wire_top_count', '顶层 occurrence 数量与白名单不符', len(remaining), len(output.fields))
        return
    by_top = {t.top_field_index: t for t in source.tiles}

    def envelope(before, after, location):
        if before.tag_raw != after.tag_raw or before.wire_type != after.wire_type:
            error('wire_tag', '授权字段的 tag raw 被改写', location)
        if before.wire_type == after.wire_type == 2:
            wanted = before.length_raw if len(before.payload) == len(after.payload) else encode_varint(len(after.payload))
            if after.length_raw != wanted:
                error('wire_length', '长度未变化时必须保留原编码；变化时使用新长度', location)

    for (source_index, before), after in zip(remaining, output.fields):
        if source_index == source.template_id_field_index:
            envelope(before, after, ('TemplateId', source_index))
            if after.wire_type != 2 or after.payload != expected_id.encode('utf-8'):
                error('template_id', '生效 TemplateId 与调用方期望不符', expected_id, output.template_id)
            continue
        tile = by_top.get(source_index)
        if tile is None or tile.index not in moved:
            if before.raw != after.raw:
                error('wire_unauthorized', '未经授权的顶层 occurrence 字节或顺序改变', source_index, before.number)
            continue
        envelope(before, after, ('TileData', tile.index))
        if after.number != 6 or after.wire_type != 2:
            error('wire_tile_order', 'TileData occurrence 顺序被改变', tile.index)
            continue
        nested = parse_fields(after.payload)
        last = {f.number: i for i, f in enumerate(tile.fields) if f.number in (1, 2, 3, 4)}
        xyz = destinations[tile.index]
        effective_id = replenish_id.get(tile.index) if isinstance(replenish_id, dict) else replenish_id
        wanted_values = xyz + (tile.tile_id if effective_id is None else effective_id,)
        append_z = 3 not in last and xyz[2] != 0
        if len(nested) != len(tile.fields) + int(append_z):
            error('wire_nested_count', 'TileData 子字段数量与白名单不符', tile.index)
            continue
        original_values = (tile.x, tile.y, tile.z, tile.tile_id)
        for j, (old, new) in enumerate(zip(tile.fields, nested)):
            changed = old.number in last and j == last[old.number] and original_values[old.number-1] != wanted_values[old.number-1]
            wanted = old.tag_raw + encode_int32(wanted_values[old.number-1]) if changed else old.raw
            if new.raw != wanted:
                error('wire_tile_field', 'TileData occurrence 不满足搬移坐标与补牌类型改写白名单', tile.index, j, old.number)
        if append_z and nested[-1].raw != b'\x18'+encode_int32(xyz[2]):
            error('wire_append_z', '缺省 Z 只能在末尾追加目标值', tile.index)


def verify_output(source_bytes: bytes, output_bytes: bytes, combined_plan, expected_template_id: str) -> VerificationReport:
    """Audit source/output bytes against an explicit plan and caller-owned ID.

    Malformed templates or plans yield a failed report with evidence. All final
    ordinary Tiles, including unchanged history, must obey structural rules.
    """
    from .increase import IncreasePlan
    from .manual_edit import ManualEditPlan, verify_manual
    if type(combined_plan) is ManualEditPlan:
        return verify_manual(source_bytes, output_bytes, combined_plan, expected_template_id)
    from .translation import TranslationPlan, verify_translation
    if type(combined_plan) is TranslationPlan:
        return enforce_output_structure(verify_translation(source_bytes, output_bytes, combined_plan, expected_template_id), output_bytes,source_bytes=source_bytes,plan=combined_plan)
    if type(combined_plan) is IncreasePlan:
        from .increase_verification import verify_increase
        return verify_increase(source_bytes, output_bytes, combined_plan, expected_template_id)
    from .ordinary_add import OrdinaryAddPlan
    if type(combined_plan) is OrdinaryAddPlan:
        from .ordinary_add_verification import verify_ordinary_add
        return verify_ordinary_add(source_bytes, output_bytes, combined_plan, expected_template_id)
    facts = {'source_sha256': None, 'output_sha256': None}
    errors, warnings = [], []
    def error(code, message, *evidence):
        errors.append(VerificationIssue(code, message, tuple(evidence)))
    def warning(code, message, *evidence):
        warnings.append(VerificationIssue(code, message, tuple(evidence)))
    def report():
        return enforce_output_structure(VerificationReport(facts, tuple(errors), tuple(warnings)), output_bytes,source_bytes=source_bytes,plan=combined_plan)
    for name, raw in (('source', source_bytes), ('output', output_bytes)):
        if type(raw) is not bytes:
            error('parse', '模板输入必须是不可变bytes', name, type(raw).__name__)
        else:
            facts[name+'_sha256'] = sha256(raw).hexdigest()
    if errors:
        return report()
    try:
        source, output = decode_template(source_bytes), decode_template(output_bytes)
    except (TypeError, ValueError, UnicodeError) as exc:
        error('parse', '模板不能完整解析', str(exc))
        return report()
    facts.update(source_template_id=source.template_id, output_template_id=output.template_id)
    if not isinstance(expected_template_id, str) or not expected_template_id or expected_template_id == source.template_id:
        error('expected_template_id', '期望ID必须非空且不同于源ID', expected_template_id)
        return report()
    try:
        expected_template_id.encode('utf-8')
    except UnicodeError:
        error('expected_template_id', '期望ID不是合法UTF-8')
        return report()
    if output.template_id != expected_template_id:
        error('template_id', '输出ID不同于调用方期望', expected_template_id, output.template_id)

    # Re-derive membership from the source bytes, independently of inspection
    # and the rewriter's entrance validation.
    try:
        distribution, placement = combined_plan.distribution, combined_plan.placement
        dopt, popt = combined_plan.distribution_options, combined_plan.placement_options
        if not (isinstance(distribution, DistributionPlan) and isinstance(placement, PlacementPlan)
                and isinstance(dopt, DistributionOptions) and isinstance(popt, PlacementOptions)):
            raise ValueError('typed plans and option snapshots are required')
        if distribution.ok is not True or placement.ok is not True:
            raise ValueError('both plans must be successful')
        retained = tuple(distribution.retained_counts)
        moved = tuple(distribution.moved_tile_indices)
        if len(retained) != len(source.stacks):
            raise ValueError('retained_counts/StackData length mismatch')
        anchors = [(s.x, s.y) for s in source.stacks]
        if len(set(anchors)) != len(anchors):
            raise ValueError('ambiguous source StackData anchors')
        members, expected, deleted = [], [], set()
        for stack, n in zip(source.stacks, retained):
            group = sorted((t for t in source.tiles if (t.x,t.y)==(stack.x,stack.y)), key=lambda t:(t.z,t.index))
            if not group or [t.z for t in group] != list(range(len(group))):
                raise ValueError('source tower is empty or Z is not a continuous prefix')
            if any(t.tile_id not in (-10,-11) for t in group):
                raise ValueError('source tower contains special Tiles')
            if type(n) is not int or not 0 <= n <= len(group):
                raise ValueError('invalid retained count')
            members.append(group)
            expected.extend(t.index for t in group[n:])
            if n == 0:
                deleted.add(stack.top_field_index)
        if (type(distribution.move_count) is not int or len(moved) != distribution.move_count
                or any(type(i) is not int or not 0 <= i < len(source.tiles) for i in moved)
                or len(set(moved)) != len(moved) or set(moved) != set(expected)):
            raise ValueError('moved indices are not exactly the source tower top suffixes')
        destinations = {}
        for p in placement.placements:
            if not isinstance(p, Placement) or not isinstance(p.profile, (tuple,list)) or any(type(n) is not int or not 1 <= n <= 4 for n in p.profile):
                raise ValueError('invalid placement/profile record')
            if type(p.tile_index) is not int or p.tile_index in destinations or not 0 <= p.tile_index < len(source.tiles):
                raise ValueError('invalid or duplicate destination index')
            if any(type(v) is not int or not -(1 << 31) <= v < (1 << 31) for v in (p.x,p.y,p.z)):
                raise ValueError('destination is not int32 XYZ')
            destinations[p.tile_index] = (p.x,p.y,p.z)
        if set(destinations) != set(moved):
            raise ValueError('destination/moved index sets differ')
        if (popt.strategy not in ('top_first','bottom_first','balanced','seeded_random')
                or type(popt.max_tiles_per_layer) is not int or popt.max_tiles_per_layer < 1
                or type(popt.max_added_layers) is not int or popt.max_added_layers < 0
                or type(popt.seed) is not int):
            raise ValueError('invalid placement options')
        if placement.strategy != popt.strategy or placement.seed != popt.seed:
            raise ValueError('placement/options strategy or seed mismatch')
        offset=placement.original_z_offset
        if (type(offset) is not int or not 0<=offset<=popt.max_added_layers
                or (offset and (placement.strategy_version not in ('orbit-v4-directional-bands','orbit-v5-partial-support','orbit-v6-directional-support','orbit-v7-surface-support','orbit-v8-inherited-source-minority')
                                or popt.strategy=='top_first'))):
            raise ValueError('invalid ordinary Z offset')
        if type(dopt.ratio) not in (int,float,Decimal) or not 0 <= dopt.ratio <= 1:
            raise ValueError('invalid ratio option')
        ratio = Decimal(str(dopt.ratio))
        source_tower_count = sum(len(group) for group in members)
        source_ratio = source_tower_count / len(source.tiles) if source.tiles else 0
        if members and dopt.ratio > source_ratio:
            raise ValueError('reduce ratio upper bound exceeds source template tower ratio')
        if (type(dopt.min_ratio) not in (int,float,Decimal)
                or not Decimal(str(dopt.min_ratio)).is_finite()
                or not 0 <= dopt.min_ratio <= 1
                or dopt.min_ratio > dopt.ratio
                or (dopt.min_ratio == dopt.ratio and dopt.ratio != 0)):
            raise ValueError('invalid ratio range option')
        minimum_ratio = Decimal(str(dopt.min_ratio))
        minimum = int((Decimal(len(source.tiles))*minimum_ratio).to_integral_value(rounding=ROUND_CEILING))
        if dopt.replenish_type not in ('auto', 'A', 'B', 'majority_A', 'majority_B', 'preserve'):
            raise ValueError('invalid replenishment type option')
        cap = max(0,int((Decimal(len(source.tiles))*ratio).to_integral_value(rounding=ROUND_CEILING))-1) if ratio else 0
        if members and dopt.ratio == source_ratio:
            cap = min(cap, source_tower_count - 1)
        if type(distribution.cap) is not int or distribution.cap != cap:
            error('distribution_cap', '计划cap与总量和严格比例上限不一致', distribution.cap, cap)
        if dopt.retained_counts is not None and tuple(dopt.retained_counts) != retained:
            error('manual_counts', '手动保留数快照与计划不一致', dopt.retained_counts, retained)
    except (AttributeError, TypeError, ValueError, InvalidOperation) as exc:
        error('plan', '计划或源塔关联不合法', str(exc))
        return report()

    facts['options'] = {'distribution': asdict(dopt), 'placement': asdict(popt)}
    legacy = distribution.removed_counts is None and distribution.final_counts is None
    wanted_kind = -10 if distribution.move_type == 'A' else -11 if distribution.move_type == 'B' else None
    ids = distribution.replenishment_ids
    mixed = dopt.replenish_type in ('majority_A', 'majority_B', 'preserve')
    if ids is not None:
        if (type(ids) is not tuple or len(ids) != len(moved)
                or any(type(i) is not int or i not in (-10,-11) for i in ids) or not mixed or legacy):
            error('replenishment_ids', '混合补牌序列必须与排序后的搬移索引逐项对应且仅含A/B')
            return report()
        wanted_ids = dict(zip(sorted(moved), ids))
    else:
        wanted_ids = {i: source.tiles[i].tile_id if legacy else wanted_kind for i in moved}
        if mixed:
            error('replenishment_ids', '混合补牌缺少逐牌类型序列')
    shifted={t.index:(t.x,t.y,t.z+offset) for t in source.tiles
             if offset and t.index not in moved and (t.x,t.y) not in set(anchors)}
    _wire_check(source, output, set(moved)|set(shifted), {**destinations,**shifted}, deleted, expected_template_id,
                None if legacy else wanted_ids, error)
    if len(source.tiles) != len(output.tiles):
        error('tile_count', 'TileData 数量变化', len(source.tiles), len(output.tiles))
        return report()
    src_counts = Counter(t.tile_id for t in source.tiles)
    out_counts = Counter(t.tile_id for t in output.tiles)
    facts['tile_counts'] = {'total':len(output.tiles), 'A':out_counts[-10], 'B':out_counts[-11]}
    removed = tuple(sum(source.tiles[i].tile_id == kind for i in moved) for kind in (-10, -11))
    added = Counter(wanted_ids.values())
    final = tuple(src_counts[kind] - count + added[kind]
                  for kind, count in zip((-10, -11), removed))
    facts.update(source_tile_counts={'total':len(source.tiles), 'A':src_counts[-10], 'B':src_counts[-11]},
                 removed_tile_counts=dict(zip(('A', 'B'), removed)),
                 added_tile_counts={'A':added[-10], 'B':added[-11]},
                 type_count_changes={'A':out_counts[-10]-src_counts[-10], 'B':out_counts[-11]-src_counts[-11]},
                 replenishment_type=distribution.move_type)
    if legacy:
        if [t.tile_id for t in source.tiles] != [t.tile_id for t in output.tiles] or src_counts != out_counts:
            error('tile_ids', '历史计划的TileId序列或类型总数变化')
        if any(src_counts[k] % 3 for k in (-10, -11)):
            error('ab_multiple_of_three', '历史计划源A/B数量必须各为3的倍数', dict(src_counts))
    else:
        for name, expected in (('removed_counts', removed), ('final_counts', final)):
            actual = getattr(distribution, name)
            if (type(actual) is not tuple or len(actual) != 2
                    or any(type(n) is not int or n < 0 for n in actual) or actual != expected):
                error('distribution_'+name, '计划类型数量与独立复算结果不符', name, actual, expected)
        if final != (out_counts[-10], out_counts[-11]):
            error('tile_ids', '输出A/B数量与源后缀移出及单类型补牌复算不符', final, dict(out_counts))
        for i, (before, after) in enumerate(zip(source.tiles, output.tiles)):
            expected_id = wanted_ids[i] if i in moved else before.tile_id
            if after.tile_id != expected_id:
                error('tile_ids', 'TileId改变不符合只重染搬移记录的规则', i, before.tile_id, after.tile_id, expected_id)
    if any(out_counts[k] % 3 for k in (-10,-11)):
        error('ab_multiple_of_three', '输出A/B数量必须各为3的倍数', dict(out_counts))
    kinds = {source.tiles[i].tile_id for i in moved}
    if (moved and ((wanted_kind is None and not mixed) or (legacy and kinds != {wanted_kind}))) or (not moved and distribution.move_type is not None):
        error('move_type', '计划补牌类型不合法或与历史搬移类型不符', distribution.move_type, tuple(sorted(kinds)))
    if moved:
        a, b = src_counts[-10], src_counts[-11]
        preferred = -10 if not b or (a and a < b) else -11 if not a or b < a else None
        if dopt.replenish_type != 'auto' and distribution.move_type != dopt.replenish_type:
            error('replenish_type', '实际补牌类型与用户指定的A/B类型不符', dopt.replenish_type, distribution.move_type)
        if dopt.replenish_type == 'auto' and preferred is not None and wanted_kind != preferred:
            error('move_type_priority', '补牌应使用源模板唯一类型或数量较少的一类', a, b)
        if mixed:
            if dopt.replenish_type == 'preserve':
                if any(wanted_ids[i] != source.tiles[i].tile_id for i in moved):
                    error('preserve_type', '与原搬出牌类型一致模式改变了搬移牌类型')
            else:
                other = -11 if dopt.replenish_type == 'majority_A' else -10
                base = src_counts[other] - removed[1 if other == -11 else 0]
                if added[other] != (-base) % 3:
                    error('majority_type_count', '以指定类型为主时少数补位应只补足最终三倍数', added[other], (-base)%3)
                originals = [replace(t,z=t.z+offset) for t in source.tiles if t.tile_id == other and (t.x,t.y) not in set(anchors)]
                minor = [output.tiles[i] for i in moved if output.tiles[i].tile_id == other]
                if minor and not originals:
                    error('minority_source_type_absent', '原非塔区没有少数补位的同类牌，无法满足邻近同类要求', other, len(minor))
                for t in minor if originals else ():
                    # Independently recompute XYZ distance; sqrt(8) retains the
                    # previous same-layer (2,2) maximum neighborhood radius.
                    if not any((t.x-p.x)**2+(t.y-p.y)**2+(t.z-p.z)**2 <= 8 for p in originals):
                        error('minority_proximity', '少数补位没有邻近原同类普通牌：XYZ欧氏距离平方需≤8', t.index,t.x,t.y,t.z,other)
    changed = {i for i,(a,b) in enumerate(zip(source.tiles,output.tiles)) if (a.x,a.y,a.z)!=(b.x,b.y,b.z)}
    expected_changes=set(moved)|set(shifted)
    # Once a tower is removed, a legal ordinary ground destination can happen
    # to equal that Tile's former XYZ. It is still part of the authorized
    # replenishment (and its A/B conversion is checked separately).
    coordinates_valid=(changed<=expected_changes and set(shifted)<=changed
                       if placement.strategy_version in ('orbit-v5-partial-support','orbit-v6-directional-support','orbit-v7-surface-support','orbit-v8-inherited-source-minority')
                       else changed==expected_changes)
    if not coordinates_valid:
        error('moved_indices', '实际坐标变化索引与搬移及原普通牌统一Z偏移不完全一致', tuple(sorted(changed)), tuple(sorted(set(moved)|set(shifted))))
    moves=[]
    for i,(a,b) in enumerate(zip(source.tiles,output.tiles)):
        if a.debuff_data_index != b.debuff_data_index:
            error('debuff', 'Tile Debuff引用改变', i)
        if i in destinations:
            moves.append({'tile_index':i,'tile_id':a.tile_id,'from_tile_id':a.tile_id,'to_tile_id':b.tile_id,
                          'from':(a.x,a.y,a.z),'to':(b.x,b.y,b.z)})
            if (b.x,b.y,b.z) != destinations[i]:
                error('destination', '输出坐标与计划目的地不同', i, destinations[i], (b.x,b.y,b.z))
    facts['moves']=tuple(moves)

    output_anchors=[(s.x,s.y) for s in output.stacks]
    expected_anchors=[anchor for anchor,n in zip(anchors,retained) if n]
    if output_anchors != expected_anchors or len(set(output_anchors)) != len(output_anchors):
        error('stack_occurrences', '输出StackData不是保留塔的有序子序列', expected_anchors,output_anchors)
    actual_counts=[]
    for anchor,n,group in zip(anchors,retained,members):
        # Once StackData is removed this XY is ordinary board space. The
        # suffix/index/destination checks above prove every old member moved;
        # a legal placement can reuse the former anchor at a different Z.
        if n == 0:
            actual_counts.append(0)
            continue
        actual=sorted((t for t in output.tiles if (t.x,t.y)==anchor),key=lambda t:(t.z,t.index))
        actual_counts.append(len(actual))
        if [t.index for t in actual] != [t.index for t in group[:n]]:
            error('tower_prefix', '输出塔成员不是源塔保留前缀',anchor,n,tuple(t.index for t in actual))
        if [t.z for t in actual] != list(range(len(actual))):
            error('tower_z_gap', '输出塔Z不连续',anchor,tuple(t.z for t in actual))
    for anchor in output_anchors:
        if not any((t.x,t.y)==anchor for t in output.tiles):
            error('empty_tower', '输出保留了空StackData',anchor)
    tower_total=sum(1 for t in output.tiles if (t.x,t.y) in set(output_anchors))
    actual_ratio=tower_total/len(output.tiles) if output.tiles else 0
    facts.update(source_tower_counts=tuple(len(g) for g in members), retained_counts=tuple(actual_counts),
                 tower_tile_count=tower_total, tower_ratio=actual_ratio, cap=cap)
    if (tower_total != sum(retained) or tower_total > cap or tower_total < minimum
            or (minimum_ratio > 0 and not output.tiles) or (ratio==0 and tower_total)
            or (ratio and Decimal(tower_total)>=Decimal(len(output.tiles))*ratio and output.tiles)):
        error('tower_ratio', '实际塔数量或塔占比范围不满足',tower_total,minimum,cap,str(minimum_ratio),str(ratio))
    if type(distribution.final_ratio) not in (int,float) or distribution.final_ratio != actual_ratio:
        error('distribution_ratio', '计划最终占比与输出复算结果不符',distribution.final_ratio,actual_ratio)

    src_non=[t for t in source.tiles if (t.x,t.y) not in set(anchors)]
    out_non=[t for t in output.tiles if (t.x,t.y) not in set(output_anchors)]
    source_layers, output_layers=_layers(src_non),_layers(out_non)
    source_layers={z+offset:points for z,points in source_layers.items()}
    if offset and (not output_layers or min(output_layers)!=0
                   or not any(p.z<min(source_layers) for p in placement.placements)):
        error('z_offset','原普通牌统一Z偏移必须是新增底部牌保持非负层号所需的最小值',offset)
    facts['original_z_offset']=offset
    if placement.strategy_version in ('orbit-v4-directional-bands','orbit-v5-partial-support','orbit-v6-directional-support','orbit-v7-surface-support','orbit-v8-inherited-source-minority') and source_layers:
        # Independent reconstruction of the source occupancy core. Historical
        # plans retain their original strategy interpretation and byte policy.
        ordered=sorted(source_layers)
        peak=max(len(ps) for ps in source_layers.values())
        def sparse_end(order):
            result=[]
            for z in order:
                if result and len(source_layers[z])>=peak:
                    break
                result.append(z)
            return result
        lower,upper=sparse_end(ordered),sparse_end(list(reversed(ordered)))
        common=set(lower)&set(upper)
        if len(ordered)>1 and common:
            midpoint=(ordered[0]+ordered[-1])/2
            lower=[z for z in lower if z not in common or z<=midpoint]
            upper=[z for z in upper if z not in common or z>midpoint]
        repairs=set()
        if placement.strategy_version in ('orbit-v6-directional-support','orbit-v7-surface-support','orbit-v8-inherited-source-minority'):
            # Independently trace actual output supports from source gaps down
            # to ground. Only these prerequisites may cross a chosen end band.
            gaps=[(t.x,t.y,t.z+offset) for t in src_non if t.z>0 and
                  not any(overlap_area((t.x,t.y),point)
                          for point in source_layers.get(t.z+offset-1,()))]
            pending=list(gaps)
            while pending:
                x,y,z=pending.pop()
                for tile in out_non:
                    if (tile.index in moved and tile.index not in repairs and tile.z==z-1
                            and overlap_area((x,y),(tile.x,tile.y))):
                        repairs.add(tile.index)
                        pending.append((tile.x,tile.y,tile.z))
            facts['support_repair_indices']=tuple(sorted(repairs))
        for p in placement.placements:
            if p.tile_index in repairs:
                continue
            if (placement.strategy_version=='orbit-v8-inherited-source-minority'
                    and distribution.move_type in ('majority_A','majority_B')):
                minor_id=-11 if distribution.move_type=='majority_A' else -10
                actual=output.tiles[p.tile_index]
                originals=[t for t in src_non if t.tile_id==minor_id]
                if (actual.tile_id==minor_id and p.z in source_layers and
                        any((p.x-t.x)**2+(p.y-t.y)**2+(p.z-t.z-offset)**2<=8 for t in originals)):
                    continue  # Count/type already proved by the majority modulo-three whitelist.
            if ((popt.strategy=='top_first' and p.z not in upper and p.z<=ordered[-1])
                    or (popt.strategy=='bottom_first' and p.z not in lower and p.z>=ordered[0])):
                error('strategy_direction','补牌落点超出所选方向的原稀疏端部及同侧新增层',p.tile_index,p.z,popt.strategy,tuple(lower),tuple(upper))
    source_by_z=defaultdict(list)
    if source.width is None or source.height is None or source.width<2 or source.height<2:
        warning('source_board_bounds', '保留源棋盘既有宽高；搬移牌另行验证边界',source.width,source.height)
    if source.stacks and not src_non:
        error('source_no_surface', '纯塔源模板没有非塔承接面')
    for t in source.tiles:
        source_by_z[t.z].append(t)
        if (source.width is None or source.height is None or not 0 <= t.x <= source.width-2
                or not 0 <= t.y <= source.height-2 or t.z<0):
            warning('source_board_bounds', '记录源Tile既有边界或Z；不调整原牌',t.index,t.x,t.y,t.z)
    for z,tiles in source_by_z.items():
        for j,a in enumerate(tiles):
            for b in tiles[j+1:]:
                if overlap_area((a.x,a.y),(b.x,b.y)):
                    warning('source_same_layer_collision', '记录源同层既有重叠；补牌不得新增重叠',z,a.index,b.index)
    if any(z!=i for i,z in enumerate(sorted(source_layers))):
        warning('source_non_tower_z_gap', '保留源非塔层既有层号与间隔',tuple(sorted(source_layers)))
    for t in src_non:
        if t.z>0 and not overlap_profile((t.x,t.y),source_layers.get(t.z+offset-1,())):
            warning('source_unsupported', '保留未接触紧邻下层的原有Tile',t.index,t.z)
    all_layers=defaultdict(list)
    directional=placement.strategy_version in ('orbit-v4-directional-bands','orbit-v5-partial-support','orbit-v6-directional-support','orbit-v7-surface-support','orbit-v8-inherited-source-minority')
    if directional:
        from .increase_geometry import SHIFTS,rectangle,separated
        retained_envelopes=[(s.x,s.y,n,s.direction) for s,n in zip(source.stacks,retained) if n]
    for t in output.tiles:
        all_layers[t.z].append(t)
        if t.index in moved and (output.width is None or output.height is None or not 0 <= t.x <= output.width-2 or not 0 <= t.y <= output.height-2):
            error('board_bounds', '搬移后Tile占位超出棋盘或缺少宽高',t.index,t.x,t.y,output.width,output.height)
        if t.index in moved and t.z < 0:
            error('negative_z', 'Tile Z不能为负',t.index,t.z)
        if directional and t.index in moved and any(
                not separated(rectangle(t.x,t.y,shift=shift),rectangle(*tower,shift=shift))
                for tower in retained_envelopes for shift in (0,)+SHIFTS):
            error('tower_unfolded_collision','补牌进入保留塔完整展开后的实际包围区',t.index,t.x,t.y,t.z)
        if not (2 <= t.x <= 14 and 2 <= t.y <= 16):
            if t.index in moved:
                error('safe_bounds', '搬移后Tile不在新增牌安全区',t.index,t.x,t.y)
            else:
                warning('existing_safe_bounds', '保留源Tile的既有安全区外锚点',t.index,t.x,t.y)
    for z,tiles in all_layers.items():
        for j,a in enumerate(tiles):
            for b in tiles[j+1:]:
                if (a.index in moved or b.index in moved) and overlap_area((a.x,a.y),(b.x,b.y)):
                    error('same_layer_collision', '同层Tile footprint重叠',z,a.index,b.index)
                    if ((a.x,a.y) in output_anchors or (b.x,b.y) in output_anchors):
                        error('tower_collision', '同层补牌与保留塔冲突',z,a.index,b.index)
    new_layers=set(output_layers)-set(source_layers)
    for z in new_layers:
        if z != min(output_layers) and z-1 not in output_layers:
            error('non_tower_z_gap', '新增非塔层与下层不连续',z,tuple(sorted(output_layers)))
    if len(new_layers)>popt.max_added_layers:
        error('added_layers', '实际新增层数超过参数',len(new_layers),popt.max_added_layers)
    if type(placement.added_layers) is not int or placement.added_layers != len(new_layers):
        error('added_layers_metadata', '计划新增层数与输出不一致',placement.added_layers,len(new_layers))
    affected_layers={output.tiles[i].z for i in moved}
    for z,points in output_layers.items():
        if len(points)>popt.max_tiles_per_layer:
            if z in affected_layers:
                error('layer_capacity', '补牌层数量超过参数',z,len(points),popt.max_tiles_per_layer)
            else:
                warning('existing_layer_capacity', '保留未补牌层的既有数量',z,len(points),popt.max_tiles_per_layer)
    facts.update(layer_counts=tuple((z,len(p)) for z,p in sorted(output_layers.items())),added_layers=len(new_layers))

    # Area multiset signatures deliberately ignore directional offsets.
    source_profiles={z:{overlap_profile(p,source_layers.get(z-1,[])) for p in points}
                     for z,points in source_layers.items() if z-1 in source_layers}
    valid_profiles={p for profiles in source_profiles.values() for p in profiles if p and 4 not in p}
    single=next(iter(valid_profiles)) if len(valid_profiles)==1 else None
    final_profiles={z:tuple(sorted({overlap_profile(p,output_layers.get(z-1,[])) for p in points}))
                    for z,points in output_layers.items() if z>0}
    facts['layer_profiles']=tuple(sorted(final_profiles.items()))
    facts['source_profiles']=tuple(sorted(valid_profiles))
    new_profiles=defaultdict(set)
    relaxed=placement.strategy_version in ('orbit-v5-partial-support','orbit-v6-directional-support','orbit-v7-surface-support','orbit-v8-inherited-source-minority')
    flexible=placement.strategy_version in ('orbit-v3-all-layers-unit-backtrack','orbit-v4-directional-bands','orbit-v5-partial-support','orbit-v6-directional-support','orbit-v7-surface-support','orbit-v8-inherited-source-minority')
    def allowed_profile(profile, choices):
        if relaxed:
            return bool(profile) and not set(profile)-{1,2}
        if flexible:
            units=set(profile)
            reference={n for candidate in choices for n in candidate}
            return bool(profile) and not units-{1,2} and (units.issubset(reference) if reference else len(units)==1)
        return profile in choices
    placements={p.tile_index:p for p in placement.placements}
    overlaps=[]
    for tile in out_non:
        point=(tile.x,tile.y)
        if tile.index not in moved:
            # Bottom additions may change an existing upper Tile's signature.
            if tile.z>0 and any(output.tiles[i].z==tile.z-1 and overlap_area(point,(output.tiles[i].x,output.tiles[i].y)) for i in moved):
                profile=overlap_profile(point,output_layers.get(tile.z-1,[]))
                allowed={single} if single else source_profiles.get(tile.z,set())
                inherited_profile=(placement.strategy_version=='orbit-v8-inherited-source-minority'
                                   and 4 in overlap_profile(point,source_layers.get(tile.z-1,[])))
                profile_valid=(bool(profile) and not set(profile)-{1,2,4}) if inherited_profile else (
                    bool(profile) and 4 not in profile and allowed_profile(profile,allowed))
                if not profile_valid:
                    error('upper_profile', '补底牌改变了既有上层堆叠形式',tile.index,profile,tuple(sorted(allowed)))
            continue
        adjacent_z=tile.z-1 if tile.z-1 in output_layers else tile.z+1
        adjacent=output_layers.get(adjacent_z,[])
        profile=overlap_profile(point,adjacent)
        overlaps.append((tile.index,adjacent_z,tuple((p,overlap_area(point,p)) for p in adjacent if overlap_area(point,p))))
        if not profile and not (relaxed and tile.z==0):
            error('unsupported', '补牌必须与相邻非塔层至少有1/4遮挡',tile.index,tile.z)
        if any(overlap_area(point,(other.x,other.y))==4 for z in (tile.z-1,tile.z+1) for other in all_layers.get(z,())):
            error('full_cover', '补牌与相邻层Tile形成完整遮盖',tile.index)
        if relaxed and tile.z==0 and not profile:
            pass  # Ground Tiles need no upper contact; only consumers need support.
        elif single is not None and not allowed_profile(profile,{single}):
            error('profile', '补牌与源唯一面积多重集profile不符',tile.index,profile,single)
        elif single is None and tile.z in source_profiles and not allowed_profile(profile,source_profiles[tile.z]):
            error('profile', '补入已有层的profile不属于该层形式',tile.index,tile.z,profile)
        if tuple(placements[tile.index].profile) != profile:
            error('profile_metadata', '计划profile与实际重叠面积不符',tile.index,placements[tile.index].profile,profile)
        if tile.z in new_layers:
            new_profiles[tile.z].add(profile)
            # Recompute from final bytes, not planner metadata: the immediate
            # lower layer includes both original Tiles and newly moved Tiles.
            inherited = {area for lower_point in output_layers.get(tile.z-1, ())
                         for area in overlap_profile(lower_point, output_layers.get(tile.z-2, ()))
                         if area in (1,2)}
            if not inherited:
                source_units = {area for p in valid_profiles for area in p if area in (1,2)}
                if len(source_units) == 1:
                    inherited = source_units
            if inherited and not relaxed and not set(profile).issubset(inherited):
                error('inherited_stacking_style', '新增层未延续紧邻下层的堆叠形式',tile.index,tile.z,profile,tuple(sorted(inherited)))
    for z,profiles in new_profiles.items():
        if not relaxed and (len({n for p in profiles for n in p})>1 if flexible else len(profiles)>1):
            error('new_layer_profile', '新建层内部出现多种面积多重集profile',z,tuple(sorted(profiles)))
    facts['overlaps']=tuple(overlaps)
    axes,symmetry=_symmetries(src_non)
    facts.update(source_symmetry=symmetry,symmetry_axes=axes)
    points=_positions(out_non)
    required_symmetry=(('horizontal',) if placement.strategy_version=='orbit-v6-directional-support'
                       and 'horizontal' in symmetry else symmetry)
    facts['required_output_symmetry']=required_symmetry
    for name in required_symmetry:
        reflected=Counter(((axes[0]-x if name in ('horizontal','rotation') else x),
                           (axes[1]-y if name in ('vertical','rotation') else y),z) for x,y,z in points.elements())
        if reflected != points:
            error('source_symmetry', '输出未保持源非塔区对称及原轴',name,axes)
    # Towers use their own source anchor axes. Preserve only transformations
    # that actually mapped all source tower heights, as distribution specifies.
    if anchors:
        tower_axes=(min(x for x,y in anchors)+max(x for x,y in anchors),
                    min(y for x,y in anchors)+max(y for x,y in anchors))
        anchor_index={anchor:i for i,anchor in enumerate(anchors)}
        pairs,centered=set(),set()
        for name in symmetry:
            reflected=[anchor_index.get((tower_axes[0]-x if name in ('horizontal','rotation') else x,
                                         tower_axes[1]-y if name in ('vertical','rotation') else y)) for x,y in anchors]
            if any(j is None or len(members[i])!=len(members[j]) for i,j in enumerate(reflected)):
                continue
            for i,j in enumerate(reflected):
                if i==j:
                    centered.add(i)
                else:
                    pairs.add(tuple(sorted((i,j))))
                    if actual_counts[i]!=actual_counts[j]:
                        error('tower_symmetry', '源镜像塔的输出高度不同',name,i,j,actual_counts[i],actual_counts[j])
        linked={i for pair in pairs for i in pair}
        if pairs:
            for i in centered-linked:
                if actual_counts[i] and actual_counts[i]%2!=len(members[i])%2:
                    error('tower_center_parity', '轴上独立塔的源高度奇偶未保留',i,len(members[i]),actual_counts[i])
        facts['tower_symmetry_pairs']=tuple(sorted(pairs))
    return report()
