"""Pure ordinary A/B addition, with lossless original occurrences and bounded packing."""
from collections import Counter, defaultdict
from dataclasses import dataclass
from hashlib import sha256
from time import monotonic
from types import MappingProxyType
from typing import Tuple

from .geometry import overlap_area, overlap_profile, symmetry_orbit, within_bounds
from .increase_geometry import SHIFTS, rectangle, separated
from .inspection import inspect_template
from .placement import Placement, SearchBudget, SearchLimit, _choose_orbits
from .protobuf_wire import encode_int32, encode_varint, replace_length_delimited
from .template_reader import decode_template


DEFAULT_ORDINARY_ADD_OPTIONS = {
    'tile_type':'B', 'added_tiles':18, 'placement_strategy':'bottom_first',
    'layer_mode':'auto', 'min_added_layers':0, 'max_added_layers':3,
    'target_mode':'count', 'min_total_tiles':90, 'max_total_tiles':99,
    'split_mode':'auto', 'top_tiles':9, 'bottom_tiles':9,
}


def normalize_ordinary_add_options(value):
    if type(value) not in (dict, MappingProxyType):
        raise ValueError('ordinary_add 必须是对象')
    unknown = set(value)-set(DEFAULT_ORDINARY_ADD_OPTIONS)
    if unknown:
        raise ValueError('未知普通牌增加选项: '+', '.join(sorted(map(str,unknown))))
    result = {**DEFAULT_ORDINARY_ADD_OPTIONS, **value}
    if result['target_mode']=='total_range': result['added_tiles']=DEFAULT_ORDINARY_ADD_OPTIONS['added_tiles']
    else:
        result['min_total_tiles']=90
        result['max_total_tiles']=99
    if result['placement_strategy']!='both' or result['split_mode']=='auto':
        result['top_tiles']=9
        result['bottom_tiles']=9
    if result['tile_type'] not in ('A','B'):
        raise ValueError('tile_type 必须是 A 或 B')
    if result['placement_strategy'] not in ('bottom_first','top_first','both'):
        raise ValueError('placement_strategy 无效')
    if result['layer_mode'] not in ('auto','range','exact'):
        raise ValueError('layer_mode 无效')
    if result['target_mode'] not in ('count','total_range') or result['split_mode'] not in ('auto','custom'):
        raise ValueError('数量目标或上下分配方式无效')
    for name in ('added_tiles','min_added_layers','max_added_layers','min_total_tiles','max_total_tiles','top_tiles','bottom_tiles'):
        if type(result[name]) is not int or not 0 <= result[name] < 2**31:
            raise ValueError(name+' 必须是非负 int32 整数')
    if not result['added_tiles'] or result['added_tiles'] % 3:
        raise ValueError('增加数量必须是正的 3 的倍数，试算不自动取整')
    if not 0 < result['min_total_tiles'] <= result['max_total_tiles']:
        raise ValueError('目标总 Tile 数范围须为正整数且下限不大于上限')
    if result['placement_strategy']=='both' and result['split_mode']=='custom':
        total=result['top_tiles']+result['bottom_tiles']
        if min(result['top_tiles'],result['bottom_tiles'])<=0 or total%3 or total>=2**31:
            raise ValueError('上下张数须分别大于0，合计为3的倍数')
        if result['target_mode']=='count' and total!=result['added_tiles']:
            raise ValueError('上层与下层张数合计须等于新增张数')
    if result['min_added_layers'] > result['max_added_layers']:
        raise ValueError('新增层数下限不能超过上限')
    if result['layer_mode']=='exact' and result['min_added_layers']!=result['max_added_layers']:
        raise ValueError('指定新增层数时，上下限必须相同')
    return result


# Match the British spelling used by the older increase engine as well.
normalise_ordinary_add_options = normalize_ordinary_add_options


@dataclass(frozen=True)
class OrdinaryAddition:
    x: int
    y: int
    z: int
    tile_id: int


@dataclass(frozen=True)
class OrdinaryAddPlan:
    source_sha256: str
    options: dict
    additions: Tuple[OrdinaryAddition, ...]
    original_z_offset: int
    original_tile_count: int

    def __post_init__(self):
        object.__setattr__(self,'options',MappingProxyType(normalize_ordinary_add_options(self.options)))
        if type(self.additions) not in (list,tuple) or any(type(t) is not OrdinaryAddition for t in self.additions):
            raise ValueError('OrdinaryAddition records required')
        object.__setattr__(self,'additions',tuple(self.additions))
        for name in ('original_z_offset','original_tile_count'):
            if type(getattr(self,name)) is not int or not 0 <= getattr(self,name) < 2**31:
                raise ValueError(name+' 必须是非负 int32 整数')
        for t in self.additions:
            if any(type(n) is not int or not -(2**31) <= n < 2**31 for n in (t.x,t.y,t.z,t.tile_id)):
                raise ValueError('新增牌字段必须是 int32 整数')


def _scalar(number,value):
    return encode_varint(number << 3)+encode_int32(value)


def _message(number,payload):
    return encode_varint(number << 3 | 2)+encode_varint(len(payload))+payload


def rewrite_ordinary_add(source_bytes,plan,new_id):
    """Append the plan; shift only original non-tower Z and effective ID."""
    if type(plan) is not OrdinaryAddPlan or sha256(source_bytes).hexdigest()!=plan.source_sha256:
        raise ValueError('普通牌增加计划与原模板不匹配')
    source=decode_template(source_bytes)
    if not isinstance(new_id,str) or not new_id or new_id==source.template_id:
        raise ValueError('新的模板 ID 无效')
    new_id.encode('utf-8')
    if plan.original_tile_count!=len(source.tiles):
        raise ValueError('普通牌增加计划的源牌数不匹配')
    tiles={t.top_field_index:t for t in source.tiles}
    anchors={(s.x,s.y) for s in source.stacks}
    parts=[]
    for index,field in enumerate(source.fields):
        if index==source.template_id_field_index:
            parts.append(replace_length_delimited(field,new_id.encode('utf-8')))
        elif index in tiles and plan.original_z_offset and (tiles[index].x,tiles[index].y) not in anchors:
            tile=tiles[index]
            z=tile.z+plan.original_z_offset
            last=next((i for i in range(len(tile.fields)-1,-1,-1) if tile.fields[i].number==3),None)
            body=b''.join(f.tag_raw+encode_int32(z) if i==last else f.raw for i,f in enumerate(tile.fields))
            if last is None:
                body+=_scalar(3,z)
            parts.append(replace_length_delimited(field,body))
        else:
            parts.append(field.raw)
    for tile in plan.additions:
        parts.append(_message(6,b''.join(_scalar(n,v) for n,v in enumerate((tile.x,tile.y,tile.z,tile.tile_id),1))))
    return b''.join(parts)


def _tower_records(document):
    records=[]
    seen=set()
    for stack in document.stacks:
        anchor=(stack.x,stack.y)
        if anchor in seen or stack.direction not in range(4):
            raise ValueError('原塔锚点重复或方向无效')
        seen.add(anchor)
        members=[t for t in document.tiles if (t.x,t.y)==anchor]
        if not members:
            raise ValueError('原塔没有对应牌')
        records.append((stack.x,stack.y,len(members),stack.direction))
    return tuple(records)


def _tower_clear(point,towers):
    return all(separated(rectangle(*point,shift=shift),rectangle(*tower,shift=shift))
               for tower in towers for shift in (0,)+SHIFTS)


def _styles_valid(layers,added,units,original_layers,side):
    """Overlap unit is style; support count may vary. Preserve mixed history."""
    added_by_z=defaultdict(list)
    for x,y,z in added:
        added_by_z[z].append((x,y))
    direction=1 if side=='bottom' else -1
    for z,points in added_by_z.items():
        adjacent=layers.get(z+direction,())
        profiles=[overlap_profile(p,adjacent) for p in points]
        new_units={area for profile in profiles for area in profile}
        if len(new_units)>1 or 4 in new_units or (units and not new_units.issubset(units)):
            return False
        reference_units={area for p in original_layers.get(z,())
                         for area in overlap_profile(p,original_layers.get(z+direction,())) if area in (1,2)}
        if len(reference_units)==1 and not new_units.issubset(reference_units):
            return False
    for z,points in layers.items():
        lower=layers.get(z-1,())
        if not lower:
            continue
        affected=any(az in (z,z-1) for _,_,az in added)
        if not affected:
            continue
        for x,y,az in added:
            if az==z:
                profile=overlap_profile((x,y),lower)
                if not profile or 4 in profile or (units and not set(profile).issubset(units)):
                    return False
            elif az==z-1:
                for upper in points:
                    if overlap_area((x,y),upper):
                        profile=overlap_profile(upper,lower)
                        if 4 in profile or (units and not set(profile).issubset(units)):
                            return False
                        before=overlap_profile(upper,original_layers.get(z-1,())) if upper in original_layers.get(z,()) else ()
                        if before and overlap_area((x,y),upper) not in before:
                            return False
    return True


def _candidates(document,layers,z,side,axes,symmetries,towers,units):
    adjacent=layers.get(z+(1 if side=='bottom' else -1),())
    reference_units={area for p in layers.get(z,()) for area in overlap_profile(p,adjacent) if area in (1,2)}
    direction=1 if side=='bottom' else -1
    inherited_units={area for p in adjacent for area in overlap_profile(p,layers.get(z+2*direction,())) if area in (1,2)}
    groups={}
    seen=set()
    for y in range(2,17):
        for x in range(2,15):
            orbit=symmetry_orbit((x,y),axes,symmetries)
            if orbit in seen:
                continue
            seen.add(orbit)
            if any(not within_bounds(p,(document.width,document.height)) or not _tower_clear(p,towers)
                   or any(overlap_area(p,q) for q in layers.get(z,())) for p in orbit):
                continue
            if any(overlap_area(p,q) for i,p in enumerate(orbit) for q in orbit[:i]):
                continue
            signatures=[overlap_profile(p,adjacent) for p in orbit]
            signature_units=[frozenset(signature) for signature in signatures]
            if len(set(signature_units))!=1:
                continue
            signature=signatures[0]
            chosen_units=signature_units[0]
            if adjacent and (not signature or len(chosen_units)!=1 or 4 in chosen_units
                             or (units and not chosen_units.issubset(units))):
                continue
            if not adjacent and z not in layers:
                continue
            if len(reference_units)==1 and not chosen_units.issubset(reference_units):
                continue
            if len(inherited_units)==1 and not chosen_units.issubset(inherited_units):
                continue
            if any(overlap_area(p,q)==4 for p in orbit for near in (z-1,z+1) for q in layers.get(near,())):
                continue
            groups.setdefault(tuple(chosen_units),[]).append(Placement(*orbit[0],z,signature,orbit))
    return [groups[p] for p in sorted(groups,key=lambda p:(len(p),sum(p),p))]


def _choose_covering(candidates,count,upper,budget,accept):
    """Choose a full support surface, branching on the least-supported upper tile."""
    full=(1<<len(upper))-1
    covers=[sum(1<<i for i,p in enumerate(upper) if any(overlap_area(p,q) for q in c.orbit)) for c in candidates]
    conflicts=[sum(1<<j for j,d in enumerate(candidates)
                   if any(overlap_area(p,q) for p in c.orbit for q in d.orbit)) for c in candidates]
    def visit(mask,left,uncovered,chosen):
        budget.use()
        reachable=1
        for i,c in enumerate(candidates):
            if mask>>i&1: reachable|=reachable<<len(c.orbit)
        if not reachable>>left&1: return None
        if not uncovered:
            if left==0:
                return chosen if accept(chosen) else None
            rest=[c for i,c in enumerate(candidates) if mask>>i&1]
            extra=_choose_orbits(rest,left,budget,lambda tail:accept(chosen+tail))
            return None if extra is None else chosen+extra
        available=[i for i,c in enumerate(candidates) if mask>>i&1 and len(c.orbit)<=left]
        union=0
        for i in available: union|=covers[i]
        if uncovered & ~union: return None
        targets=[[i for i in available if covers[i]>>u&1] for u in range(len(upper)) if uncovered>>u&1]
        choices=min(targets,key=len)
        choices.sort(key=lambda i:(-bin(covers[i]&uncovered).count('1'),len(candidates[i].orbit),i))
        for i in choices:
            result=visit(mask & ~conflicts[i],left-len(candidates[i].orbit),uncovered & ~covers[i],chosen+[candidates[i]])
            if result is not None: return result
        return None
    return visit((1<<len(candidates))-1,count,full,[])


def _find_layout(document,options,towers,budget):
    anchors={(s.x,s.y) for s in document.stacks}
    layers=defaultdict(list)
    for tile in document.tiles:
        if (tile.x,tile.y) not in anchors:
            layers[tile.z].append((tile.x,tile.y))
    if not layers:
        return None
    layers=dict(layers)
    points=[p for layer in layers.values() for p in layer]
    axes=(min(p[0] for p in points)+max(p[0] for p in points),min(p[1] for p in points)+max(p[1] for p in points))
    symmetries=inspect_template(document).symmetry
    areas={area for z,layer in layers.items() for p in layer for area in overlap_profile(p,layers.get(z-1,())) if area in (1,2)}
    units=areas
    original_layers={z:list(points) for z,points in layers.items()}
    low,high=min(layers),max(layers)
    minimum=options['min_added_layers'] if options['layer_mode']!='auto' else 0
    maximum=min(options['max_added_layers'],options['added_tiles'])
    if minimum>maximum:
        return None
    needed=options['added_tiles']
    # At most 56 non-overlapping 2x2 footprints fit in the fixed 13x15 anchors.
    if needed>56*(maximum+(2 if options['placement_strategy']=='both' and low!=high else 1)):
        return None
    if minimum>budget.remaining or needed>56*(budget.remaining+2):
        raise SearchLimit('requested layout cannot be visited within the state budget')
    result=None
    candidate_cache={}

    def candidates(current,z,side):
        # Different quantity/layer splits often reach the same local surface.
        # Changes on the distant opposite side cannot affect these candidates;
        # the target, both neighbours and inherited-style layer fully determine
        # them. Keep absence distinct from an explicitly present empty layer.
        direction=1 if side=='bottom' else -1
        levels=sorted({z-1,z,z+1,z+2*direction})
        key=(z,side,tuple((level,tuple(current[level]) if level in current else None) for level in levels))
        if key not in candidate_cache:
            candidate_cache[key]=_candidates(document,current,z,side,axes,symmetries,towers,units)
        return candidate_cache[key]

    def valid_styles(current,placed):
        return all(_styles_valid(current,[p for p in placed if p[2]<=low] if side=='bottom'
                                 else [p for p in placed if p[2]>=high],units,original_layers,side)
                   for side in ('bottom','top'))

    def fill(slots,index,current,placed,remaining):
        nonlocal result
        budget.use()
        if index==len(slots):
            supported=(len(current)==1 or all(any(overlap_area((x,y),p)
                       for near in (z-1,z+1) for p in current.get(near,())) for x,y,z in placed))
            if remaining==0 and supported and valid_styles(current,placed):
                result=placed
                return True
            return False
        z,side,mandatory=slots[index]
        future=sum(s[2] for s in slots[index+1:])
        side_left=side_targets[side]-sum(1 for _,_,az in placed if (az<=low if side=='bottom' else az>=high)) if side_targets else remaining
        maximum_count=min(56,remaining-future,side_left)
        minimum_count=1 if mandatory else 0
        if side_targets and not any(s[1]==side for s in slots[index+1:]):
            minimum_count=max(minimum_count,side_left)
        if maximum_count<minimum_count:
            return False
        groups=candidates(current,z,side)
        counts=list(range(maximum_count,minimum_count-1,-1))
        if future:
            # Exact new layers need real support surfaces. Balanced quantities
            # leave enough cards for each surface before trying dense prefixes.
            counts.sort(key=lambda count:(abs(count*(len(slots)-index)-remaining),-count))
            if not mandatory and (low==high or side=='bottom' and any(s[1]=='bottom' for s in slots[index+1:])):
                counts.remove(0)
                counts.insert(0,0)
        for count in counts:
            if count==0:
                if fill(slots,index+1,current,placed,remaining):
                    return True
                continue
            for group in groups:
                def accept(chosen):
                    additions=[(x,y,z) for candidate in chosen for x,y in candidate.orbit]
                    updated={level:list(ps) for level,ps in current.items()}
                    updated.setdefault(z,[]).extend((x,y) for x,y,_ in additions)
                    combined=placed+additions
                    if not valid_styles(updated,combined):
                        return False
                    return fill(slots,index+1,updated,combined,remaining-count)
                chosen=(_choose_covering(group,count,current[z+1],budget,accept) if side=='bottom' and z<low
                        else _choose_orbits(group,count,budget,accept))
                if chosen is not None:
                    return True
        return False

    parent_budget=budget
    incomplete=False
    def balanced(lowest,highest):
        center=(lowest+highest+1)//2
        for distance in range(highest-lowest+1):
            for n in ((center,) if distance==0 else (center-distance,center+distance)):
                if lowest<=n<=highest: yield n
    def configurations():
        for added_layers in range(minimum,maximum+1):
            if options['placement_strategy']=='both':
                for bottom_layers in balanced(0,added_layers):
                    top_layers=added_layers-bottom_layers
                    if low==high and (not bottom_layers or not top_layers): continue
                    splits=(options['top_tiles'],) if options['split_mode']=='custom' else balanced(1,needed-1)
                    for top_count in splits:
                        parent_budget.use()
                        if top_layers>top_count or bottom_layers>needed-top_count: continue
                        yield bottom_layers,top_layers,{'top':top_count,'bottom':needed-top_count}
            else:
                yield (added_layers,0,None) if options['placement_strategy']=='bottom_first' else (0,added_layers,None)
    # Explore every automatic split shallowly before spending the remaining
    # search on dense branches. Early layer counts must not starve later ones.
    caps=(300,20000) if options['placement_strategy']=='both' and options['split_mode']=='auto' else (20000,)
    trials=((cap,configuration) for cap in caps for configuration in configurations())
    for cap,(bottom_layers,top_layers,side_targets) in trials:
        parent_budget.use()
        if bottom_layers+top_layers>parent_budget.remaining:
            raise SearchLimit('layer construction exceeds remaining state budget')
        slots=[]
        if options['placement_strategy'] in ('bottom_first','both'):
            slots=([(low,'bottom',False)] if low!=high or not side_targets else [])+[(low-n,'bottom',True) for n in range(1,bottom_layers+1)]
        if options['placement_strategy'] in ('top_first','both'):
            slots+=([(high,'top',False)] if low!=high or not side_targets else [])+[(high+n,'top',True) for n in range(1,top_layers+1)]
        # An inconclusive dense low-layer trial must not consume every state
        # before a requested larger layer count gets a chance to place exactly.
        budget=SearchBudget(min(cap,parent_budget.remaining),parent_budget.deadline)
        available=budget.remaining
        try:
            if fill(slots,0,layers,[],needed):
                return result
        except SearchLimit:
            incomplete=True
        finally:
            parent_budget.remaining-=available-budget.remaining
    if incomplete:
        raise SearchLimit('one or more layer-count trials remain inconclusive')
    return None


def _error(code,message):
    return {'ok':False,'output':None,'plan':None,'summary':None,'error':{'code':code,'message':message}}


def build_ordinary_add(source_bytes,options,new_id, *, deadline=None):
    try:
        options=normalize_ordinary_add_options(options)
        document=decode_template(source_bytes)
        if not isinstance(new_id,str) or not new_id or new_id==document.template_id:
            raise ValueError('新的模板 ID 无效')
        new_id.encode('utf-8')
        if type(document.width) is not int or type(document.height) is not int or min(document.width,document.height)<2:
            raise ValueError('模板缺少有效棋盘宽高')
        counts=[sum(t.tile_id==kind for t in document.tiles) for kind in (-10,-11)]
        if len(document.tiles)%3 or any(n%3 for n in counts):
            return _error('grouping_conflict','保留原牌类型时，原 A/B 数量及含特殊牌的总牌数必须分别是 3 的倍数')
        anchors={(s.x,s.y) for s in document.stacks}
        ordinary=[t for t in document.tiles if (t.x,t.y) not in anchors]
        if not ordinary:
            return _error('no_non_tower_surface','没有可承接普通牌的非塔层')
        towers=_tower_records(document)
        total=len(document.tiles)
        if options['target_mode']=='total_range':
            if options['min_total_tiles']<=total<=options['max_total_tiles']:
                return _error('already_in_range','原模板总数已在目标范围内，无需新增；如仍需补牌，请改用新增张数')
            first=max(3,((options['min_total_tiles']-total+2)//3)*3)
            last=options['max_total_tiles']-total
            counts_to_try=range(first,last+1,3)
            if not counts_to_try:
                return _error('target_total_unattainable','目标总数范围内没有高于原模板、且满足3的倍数的总数')
        else:
            counts_to_try=(options['added_tiles'],)
        if options['placement_strategy']=='both' and options['split_mode']=='custom':
            custom_count=options['top_tiles']+options['bottom_tiles']
            if custom_count not in counts_to_try:
                return _error('target_total_unattainable','自定义上下张数合计不符合当前模板的目标总数范围')
            counts_to_try=(custom_count,)
        budget=SearchBudget(120000,min(monotonic()+6, deadline) if deadline is not None else monotonic()+6)
        layout=None
        incomplete=False
        for needed in counts_to_try:
            budget.use()
            trial=SearchBudget(min(30000,budget.remaining),budget.deadline)
            available=trial.remaining
            try:
                layout=_find_layout(document,{**options,'added_tiles':needed},towers,trial)
                if layout is not None: break
            except SearchLimit:
                incomplete=True
            finally:
                budget.remaining-=available-trial.remaining
        if layout is None and incomplete: raise SearchLimit('quantity trials incomplete')
        if layout is None:
            return _error('no_feasible_layout','当前参数下未找到满足数量、层数、对称和堆叠风格的完整方案；不是全局无解证明')
        offset=max(0,-min([t.z for t in ordinary]+[p[2] for p in layout]))
        tile_id=-10 if options['tile_type']=='A' else -11
        additions=tuple(OrdinaryAddition(x,y,z+offset,tile_id) for x,y,z in layout)
        plan=OrdinaryAddPlan(sha256(source_bytes).hexdigest(),options,additions,offset,len(document.tiles))
        output=rewrite_ordinary_add(source_bytes,plan,new_id)
        from .ordinary_add_verification import verify_ordinary_add
        report=verify_ordinary_add(source_bytes,output,plan,new_id)
        if not report.ok:
            return _error('verification_failed','; '.join(issue.message for issue in report.errors))
        tower_count=sum(t[2] for t in towers)
        total=len(document.tiles)
        final_counts=[n+(len(additions) if kind==tile_id else 0) for n,kind in zip(counts,(-10,-11))]
        added_layers=len({z for _,_,z in layout if z<min(t.z for t in ordinary) or z>max(t.z for t in ordinary)})
        summary={'operation':'ordinary_add','no_op':False,'original_total':total,'final_total':total+len(additions),
                 'total_tiles_before':total,'total_tiles_after':total+len(additions),'added_tiles':len(additions),
                 'original_counts':counts,'final_counts':final_counts,'tile_type':options['tile_type'],
                 'fill_type':options['tile_type'],'added_layers':added_layers,'original_z_offset':offset,
                 'z_offset_scope':'non_tower',
                 'top_added_tiles':sum(z>=max(t.z for t in ordinary) for _,_,z in layout) if options['placement_strategy']!='bottom_first' else 0,
                 'bottom_added_tiles':sum(z<=min(t.z for t in ordinary) for _,_,z in layout) if options['placement_strategy']!='top_first' else 0,
                 'top_added_layers':len({z for _,_,z in layout if z>max(t.z for t in ordinary)}),
                 'bottom_added_layers':len({z for _,_,z in layout if z<min(t.z for t in ordinary)}),
                 'original_tower_tiles':tower_count,'final_tower_tiles':tower_count,
                 'original_ratio':tower_count/total if total else 0,'final_ratio':tower_count/(total+len(additions))}
        return {'ok':True,'output':output,'plan':plan,'summary':summary,'error':None}
    except SearchLimit:
        return _error('search_limit','本次有界搜索未完成，不能断言无解；请调整数量、摆放优先级或新增层数后重试')
    except (ValueError,TypeError,OverflowError,KeyError,UnicodeError) as exc:
        return _error('invalid_ordinary_add',str(exc))
