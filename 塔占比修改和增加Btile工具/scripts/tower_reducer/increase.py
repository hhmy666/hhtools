"""Append-only ordinary tower planner and lossless writer (no filesystem I/O)."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from hashlib import sha256
from math import isfinite, gcd
from time import monotonic
from types import MappingProxyType
from typing import Tuple

from . import increase_geometry as geo
from .protobuf_wire import encode_int32, encode_varint, replace_length_delimited
from .template_reader import decode_template

DEFAULT_INCREASE_OPTIONS = {
    'target_mode':'ratio','added_tiles':30,'total_multiplier':1.5,
    'min_ratio':.20,'max_ratio':.25,'operation':'new','stack_count':None,
    'regions':['auto'],'fill_type':'auto','direction':0,'spread':'compact',
    'spacing':0,'formation_gap':0,'translate_x':0,'translate_y':0,
    'auto_translate':True,'height_mode':'difference','tower_counts':[],
    'tower_positions':[],'tower_directions':[],'locked_towers':[],
}


def normalise_increase_options(value):
    if not isinstance(value,(dict,MappingProxyType)):
        raise ValueError('increase 必须是对象')
    unknown=set(value)-set(DEFAULT_INCREASE_OPTIONS)
    if unknown: raise ValueError('未知增加选项: '+', '.join(sorted(unknown)))
    result={**DEFAULT_INCREASE_OPTIONS,**value}
    for name,choices in (
        ('target_mode',('ratio','count','multiplier','manual')),
        ('operation',('new','existing','both')),('fill_type',('auto','A','B')),
        ('spread',('compact','distributed')),('height_mode',('difference','proportion','manual'))):
        if result[name] not in choices: raise ValueError(name+' 值无效')
    for name in ('added_tiles','total_multiplier','min_ratio','max_ratio'):
        n=result[name]
        if type(n) not in (int,float) or not isfinite(n): raise ValueError(name+' 必须是有限数值')
    if result['added_tiles']<0 or result['total_multiplier']<1:
        raise ValueError('增加数量不能为负，总牌量倍数不能小于 1')
    if not 0<=result['min_ratio']<result['max_ratio']<=1:
        raise ValueError('占比范围必须满足 0 ≤ 下限 < 上限 ≤ 1')
    for name in ('direction','spacing','formation_gap','translate_x','translate_y'):
        if type(result[name]) is not int or abs(result[name])>=2**31: raise ValueError(name+' 必须是 int32 整数')
    if result['direction'] not in range(4) or result['spacing']<0 or result['formation_gap']<0:
        raise ValueError('方向或间距无效')
    if type(result['auto_translate']) is not bool: raise ValueError('auto_translate 必须是布尔值')
    if type(result['stack_count']) is int and result['stack_count']==0:
        result['stack_count']=None
    if result['stack_count'] is not None and (type(result['stack_count']) is not int or result['stack_count']<1):
        raise ValueError('新增堆数应为 0（自动）、留空或正整数')
    for name in ('regions','tower_counts','tower_positions','tower_directions','locked_towers'):
        if type(result[name]) not in (list,tuple): raise ValueError(name+' 必须是数组')
        if len(result[name])>4096: raise ValueError(name+' 超出单次可处理数量')
        result[name]=list(result[name])
    if not result['regions'] or any(r not in ('auto','top','bottom','left','right','center') for r in result['regions']):
        raise ValueError('regions 无效')
    for name in ('tower_counts','locked_towers'):
        if any(type(n) is not int or n<(1 if name=='tower_counts' else 0) for n in result[name]):
            raise ValueError(name+' 必须包含有效整数')
    if len(set(result['locked_towers']))!=len(result['locked_towers']): raise ValueError('锁定塔索引重复')
    for name,keys in (('tower_positions',{'index','x','y'}),('tower_directions',{'index','direction'})):
        seen=set(); rows=[]
        for row in result[name]:
            if not isinstance(row,(dict,MappingProxyType)) or set(row)!=keys or any(type(v) is not int or abs(v)>=2**31 for v in row.values()):
                raise ValueError(name+' 条目无效')
            if row['index']<0 or row['index'] in seen: raise ValueError(name+' 索引无效或重复')
            if 'direction' in row and row['direction'] not in range(4): raise ValueError('单塔方向无效')
            seen.add(row['index']); rows.append(dict(row))
        result[name]=rows
    return result


def _freeze(value):
    if type(value) in (dict,MappingProxyType): return MappingProxyType({k:_freeze(v) for k,v in value.items()})
    if type(value) in (list,tuple): return tuple(_freeze(v) for v in value)
    if type(value) in (str,int,float,bool,type(None)): return value
    raise ValueError('invalid immutable plan value')


@dataclass(frozen=True)
class TowerAddition:
    index: int
    x: int
    y: int
    direction: int
    original_count: int
    final_count: int
    tile_ids: Tuple[int,...]

    def __post_init__(self):
        object.__setattr__(self,'tile_ids',tuple(self.tile_ids))


@dataclass(frozen=True)
class IncreasePlan:
    source_sha256: str
    options: dict
    towers: Tuple[TowerAddition,...]
    translation: Tuple[int,int]
    original_tile_count: int
    symmetry_version: str = 'source-v2'

    def __post_init__(self):
        if type(self.symmetry_version) is not str or self.symmetry_version not in ('source-v2','legacy-x-v1'):
            raise ValueError('unknown increase symmetry version')
        object.__setattr__(self,'options',_freeze(normalise_increase_options(self.options)))
        object.__setattr__(self,'towers',tuple(self.towers))
        object.__setattr__(self,'translation',tuple(self.translation))
        if any(type(t) is not TowerAddition for t in self.towers): raise ValueError('TowerAddition records required')


def _scalar(n,v): return encode_varint(n<<3)+encode_int32(v)
def _message(n,p): return encode_varint(n<<3|2)+encode_varint(len(p))+p


def _changed_payload(fields,values,original):
    last={f.number:i for i,f in enumerate(fields) if f.number in values}
    parts=[]
    for i,f in enumerate(fields):
        parts.append(f.tag_raw+encode_int32(values[f.number])
                     if f.number in values and i==last[f.number] and values[f.number]!=original[f.number]
                     else f.raw)
    for n in values:
        if n not in last and values[n]!=original[n]: parts.append(_scalar(n,values[n]))
    return b''.join(parts)


def rewrite_increase(source_bytes,plan,new_id):
    if type(plan) is not IncreasePlan or sha256(source_bytes).hexdigest()!=plan.source_sha256:
        raise ValueError('增加计划与原模板不匹配')
    document=decode_template(source_bytes)
    if not isinstance(new_id,str) or not new_id or new_id==document.template_id: raise ValueError('新的模板 ID 无效')
    new_id.encode('utf-8')
    by_top={t.top_field_index:t for t in document.tiles}
    stacks={s.top_field_index:s for s in document.stacks}
    old_xy={(s.x,s.y):plan.towers[i] for i,s in enumerate(document.stacks)}
    tx,ty=plan.translation; parts=[]
    for i,f in enumerate(document.fields):
        if i==document.template_id_field_index:
            parts.append(replace_length_delimited(f,new_id.encode('utf-8')))
        elif i in by_top:
            t=by_top[i]; tower=old_xy.get((t.x,t.y))
            x,y=(tower.x,tower.y) if tower else (t.x+tx,t.y+ty)
            payload=_changed_payload(t.fields,{1:x,2:y},{1:t.x,2:t.y})
            parts.append(replace_length_delimited(f,payload))
        elif i in stacks:
            s=stacks[i]; t=plan.towers[s.index]
            payload=_changed_payload(s.fields,{1:t.x,2:t.y,3:t.direction},{1:s.x,2:s.y,3:s.direction})
            parts.append(replace_length_delimited(f,payload))
        else: parts.append(f.raw)
    # All original occurrences remain in their original order. Appends are stable.
    for t in plan.towers:
        for z,tile_id in enumerate(t.tile_ids,t.original_count):
            parts.append(_message(6,_scalar(1,t.x)+_scalar(2,t.y)+_scalar(3,z)+_scalar(4,tile_id)))
    for t in plan.towers[len(document.stacks):]:
        parts.append(_message(7,_scalar(1,t.x)+_scalar(2,t.y)+_scalar(3,t.direction)))
    return b''.join(parts)


def _source_counts(document):
    seen=set(); counts=[]
    for s in document.stacks:
        if (s.x,s.y) in seen or s.direction not in range(4): raise ValueError('源塔锚点重复或方向无效')
        seen.add((s.x,s.y))
        members=sorted((t for t in document.tiles if (t.x,t.y)==(s.x,s.y)),key=lambda t:t.z)
        if not members or [t.z for t in members]!=list(range(len(members))) or any(t.tile_id not in (-10,-11) for t in members):
            raise ValueError('源塔必须是从 Z=0 连续排列的普通 A/B 牌')
        counts.append(len(members))
    return counts


def _targets(document,options,old_count,physical_limit):
    total=len(document.tiles); mode=options['target_mode']
    if mode=='manual': target=Decimal(sum(options['tower_counts'])-old_count)
    elif mode=='count': target=Decimal(str(options['added_tiles']))
    elif mode=='multiplier': target=Decimal(total)*(Decimal(str(options['total_multiplier']))-1)
    else:
        lower=Decimal(str(options['min_ratio'])); upper=Decimal(str(options['max_ratio']))
        target=max(Decimal(0),(lower*total-old_count)/(1-lower))
        low=max(0,int(target.to_integral_value(rounding=ROUND_CEILING)))
        if upper==1:
            high=physical_limit if old_count<total else -1
        else:
            high=min(physical_limit,int(((upper*total-old_count)/(1-upper)).to_integral_value(rounding=ROUND_CEILING))-1)
    if mode!='ratio': low,high=0,physical_limit
    first=low+(-total-low)%3
    last=high-(total+high)%3
    def nearest():
        if first>last: return
        center=max(first,min(last,int(target)//3*3+(-total)%3))
        while center>target and center>first: center-=3
        left=center; right=center+3
        while left>=first or right<=last:
            if right<=last and (left<first or abs(Decimal(right)-target)<=abs(Decimal(left)-target)):
                yield right; right+=3
            else:
                yield left; left-=3
    return nearest(),target


def _allocation(records,groups,target,document,options,points,old_counts):
    """Bounded integer allocation over symmetric platforms, with final counts."""
    caps=[]
    for i,(x,y,direction) in enumerate(records):
        lo,hi=0,int(max(document.width,document.height)/max(geo.SHIFTS))+1
        while lo<hi:
            mid=(lo+hi+1)//2
            if geo.tower_fits(x,y,mid,direction,points,document.width,document.height,options['formation_gap']): lo=mid
            else: hi=mid-1
        unchanged=i<len(old_counts) and (x,y,direction)==(document.stacks[i].x,document.stacks[i].y,document.stacks[i].direction)
        caps.append(max(lo,old_counts[i]) if unchanged else lo)
    base=old_counts+[1]*(len(records)-len(old_counts))
    if True:
        # New platforms cycle through the source low/high profile. Equal-height
        # mirrored originals stay together, distinct platforms keep their order.
        profile=sorted(set(old_counts)) or [1]
        seeds=list(old_counts)
        for gi,group in enumerate(groups):
            for i in group:
                if i>=len(seeds): seeds.extend([profile[gi%len(profile)]]*(i-len(seeds)+1))
        manual=options['target_mode']=='manual' or options['height_mode']=='manual'
        if manual:
            if len(options['tower_counts'])!=len(records): return None
            if any(n<b for n,b in zip(options['tower_counts'],base)): return None
            seeds=list(options['tower_counts'])
        available=[g for g in groups if not any(i in options['locked_towers'] for i in g)
                   and (options['operation']!='new' or g[0]>=len(old_counts))]
        locked={i:(options['tower_counts'][i] if i<len(options['tower_counts']) else base[i]) for i in options['locked_towers']}
        fixed=sum(locked.get(i,base[i]) for g in groups if g not in available for i in g)
        available_seed=sum(seeds[i] for g in available for i in g)
        members=sum(len(g) for g in available)
        offset=(target-fixed-available_seed)/members if members else 0
        factor=(target-fixed)/available_seed if available_seed else 1
        states={0:(0,())}
        for group in groups:
            lo=max(base[i] for i in group); hi=min(caps[i] for i in group)
            lockvalues={locked[i] for i in group if i in locked}
            if len(lockvalues)>1: return None
            if lockvalues:
                value=next(iter(lockvalues))
                if value<lo or value>hi: return None
                lo=hi=value
            elif group not in available:
                if hi<lo: return None
                hi=lo
            ideal=(sum(seeds[i] for i in group)/len(group) if manual else
                   seeds[group[0]]*factor if options['height_mode']=='proportion' else seeds[group[0]]+offset)
            next_states={}
            for subtotal,(cost,chosen) in states.items():
                for n in range(lo,min(hi,(target-subtotal)//len(group))+1):
                    newtotal=subtotal+n*len(group); newcost=cost+len(group)*(n-ideal)**2
                    if newtotal not in next_states or newcost<next_states[newtotal][0]:
                        next_states[newtotal]=(newcost,chosen+(n,))
            states=next_states
            if not states: return None
        if target not in states: return None
        counts=[0]*len(records)
        for group,n in zip(groups,states[target][1]):
            for i in group: counts[i]=n
        # Do not invert established source high/low platforms.
        if not manual and any(seeds[i]<seeds[j] and counts[i]>counts[j] for i in range(len(records)) for j in range(len(records))):
            return None
    geometries=[(x,y,c,d) for (x,y,d),c in zip(records,counts)]
    active=[i>=len(old_counts) or geometry!=(document.stacks[i].x,document.stacks[i].y,old_counts[i],document.stacks[i].direction) for i,geometry in enumerate(geometries)]
    if any(active[i] and not geo.tower_fits(*a,points,document.width,document.height,options['formation_gap']) for i,a in enumerate(geometries)): return None
    if any((active[i] or active[j]) and not geo.towers_separate(a,b,options['spacing'])
           for i,a in enumerate(geometries) for j,b in enumerate(geometries) if j>i): return None
    return counts


def _precheck_new_positions(options):
    # Explicit positions and locked/manual plans retain the existing exact
    # placement path; this check only reasons about ordinary generated anchors.
    return (options['operation']!='existing' and not options['tower_positions']
            and not options['locked_towers'] and options['target_mode']!='manual'
            and options['height_mode']!='manual')


def _layout(document,options,old_counts,added,translation,budget,cache):
    budget[0]-=1
    if budget[0]<0 or monotonic()>budget[1]: raise RuntimeError('search_budget')
    tx,ty=translation; points=geo.source_points(document,translation)
    if (tx or ty) and any(not geo.within_bounds(point,(document.width,document.height)) for point in points): return None
    if (tx or ty) and any(not geo.inside(geo.rectangle(x,y,shift=shift),document.width,document.height)
                         for x,y in points for shift in geo.SHIFTS): return None
    axes,names=geo.symmetry(document,tx,ty)
    pos={r['index']:(r['x'],r['y']) for r in options['tower_positions']}
    dirs={r['index']:r['direction'] for r in options['tower_directions']}
    # Moving one old tower carries its geometric mirrors; explicit conflicting
    # overrides are rejected instead of silently overriding the other edit.
    original=[(s.x+tx,s.y+ty) for s in document.stacks]
    propagated=dict(pos)
    for index,new in pos.items():
        if index>=len(original): continue
        old=original[index]
        linked=geo.linked_orbit(old,new,axes,names)
        for j,point in enumerate(original):
            targets={after for before,after in linked if before==point}
            if not targets: continue
            if len(targets)!=1: return None
            target=next(iter(targets))
            if j in propagated and propagated[j]!=target: return None
            propagated[j]=target
    records=[(*propagated.get(s.index,(s.x+tx,s.y+ty)),dirs.get(s.index,s.direction)) for s in document.stacks]
    # Every changed old tower must fit with at least its original cards.
    # Adding cards only expands its rectangle, so no new layout can repair
    # this failure. Check final overrides before spending the search budget.
    if any(record!=(s.x,s.y,s.direction) and not geo.tower_fits(
            *record[:2],old_counts[i],record[2],points,document.width,document.height,options['formation_gap'])
           for i,(record,s) in enumerate(zip(records,document.stacks))):
        cache['invalid_old_layout']=True
        return None
    cache['admissible_old_layout']=True
    groups=[]; handled=set()
    for i,s in enumerate(document.stacks):
        if i in handled: continue
        mirrors=set(geo.orbit(records[i][:2],axes,names))
        group=tuple(j for j in range(i,len(records)) if records[j][:2] in mirrors and old_counts[j]==old_counts[i])
        groups.append(group); handled.update(group)
    for i in options['locked_towers']:
        if i>=len(records):
            if i>=len(options['tower_counts']): return None
            continue
        s=document.stacks[i]
        if records[i]!=(s.x+tx,s.y+ty,s.direction): return None
    if options['operation']=='existing' or added==0: numbers=[0]
    elif options['target_mode']=='manual' or options['height_mode']=='manual': numbers=[len(options['tower_counts'])-len(records)]
    elif options['stack_count'] is not None: numbers=[options['stack_count']]
    else:
        max_stacks=min(added,(document.width//2)*(document.height//2)-len(records))
        numbers=range(1,max_stacks+1)
    precheck=_precheck_new_positions(options)
    if translation not in cache:
        choices=geo.candidates(document,options,translation)
        if precheck:
            # A one-card clash with an existing anchor cannot be repaired by
            # trying a different height or target quantity at these positions.
            choices=[orbit for orbit in choices if all(
                geo.towers_separate((x,y,1,options['direction']),(a,b,1,e),options['spacing'])
                for x,y in orbit for a,b,e in records)]
            # Subset sums are a necessary condition only: overlapping groups
            # may still fail the unchanged exact geometry search below.
            # Existing historical towers can overlap. Their record count is
            # not a proof of how many distinct new positions remain occupied.
            limit=(document.width//2)*(document.height//2)
            reachable=1; mask=(1<<(limit+1))-1
            for orbit in choices: reachable=(reachable|(reachable<<len(orbit)))&mask
            cache[('reachable',translation)]=reachable
        cache[translation]=choices
    choices=cache[translation]
    if precheck:
        reachable=cache[('reachable',translation)]
        requested=options['stack_count']
        cache['new_anchors_found']=cache.get('new_anchors_found',False) or bool(choices)
        possible=bool(choices) if requested is None else bool((reachable>>requested)&1)
        cache['new_placement_possible']=cache.get('new_placement_possible',False) or possible
        numbers=(n for n in numbers if (reachable>>n)&1)
    old_len=len(records)
    for number in numbers:
        if number<0: continue
        if any(i>=old_len+number for i in list(pos)+list(dirs)+options['locked_towers']): continue
        divisor=0
        for group in groups: divisor=gcd(divisor,len(group))
        for group in choices:
            if len(group)<=number: divisor=gcd(divisor,len(group))
        if divisor and (sum(old_counts)+added)%divisor: continue
        def finish():
            budget[0]-=1
            if budget[0]<0 or monotonic()>budget[1]: raise RuntimeError('search_budget')
            newpoints={r[:2] for r in records[old_len:]}
            if any(set(geo.orbit(p,axes,names))-newpoints for p in newpoints): return None
            counts=_allocation(records,groups,sum(old_counts)+added,document,options,points,old_counts)
            if counts is not None: return tuple(records),tuple(counts)
            return None
        if number and all(i in pos for i in range(old_len,old_len+number)):
            for i in range(old_len,old_len+number): records.append((*pos[i],dirs.get(i,options['direction'])))
            handled=set()
            original_group_count=len(groups)
            for i in range(old_len,len(records)):
                if i in handled: continue
                mirrors=set(geo.orbit(records[i][:2],axes,names))
                group=tuple(j for j in range(i,len(records)) if records[j][:2] in mirrors)
                groups.append(group); handled.update(group)
            result=finish()
            del records[old_len:]; del groups[original_group_count:]
            if result: return result
            continue
        def search(start,left):
            budget[0]-=1
            if budget[0]<0 or monotonic()>budget[1]: raise RuntimeError('search_budget')
            if left==0: return finish()
            for ci in range(start,len(choices)):
                orbit=choices[ci]
                if len(orbit)>left: continue
                candidate=[]; index=len(records)
                origin=next((j for j in range(len(orbit)) if index+j in pos),None)
                linked=geo.linked_orbit(orbit[origin],pos[index+origin],axes,names) if origin is not None else ()
                for j,p in enumerate(orbit):
                    if origin is not None:
                        targets={after for before,after in linked if before==p}
                        if len(targets)!=1: break
                        inferred=next(iter(targets))
                    else: inferred=p
                    xy=pos.get(index+j,inferred)
                    candidate.append((*xy,dirs.get(index+j,options['direction'])))
                if len(candidate)!=len(orbit): continue
                if any(not geo.towers_separate((x,y,1,d),(a,b,1,e),options['spacing'])
                       for x,y,d in candidate for a,b,e in records): continue
                if any(not geo.tower_fits(x,y,1,d,points,document.width,document.height,options['formation_gap']) for x,y,d in candidate): continue
                records.extend(candidate); groups.append(tuple(range(index,index+len(candidate))))
                result=search(ci+1,left-len(candidate))
                if result: return result
                del records[index:]; groups.pop()
            return None
        result=search(0,number)
        if result: return result
    return None


def _error(code,message):
    return {'ok':False,'plan':None,'output':None,'summary':None,
            'error':{'code':code,'message':message,'recommendations':['将新增堆数设为 0 自动，或调整增加方式、摆放区域','检查间距、方向，或允许原阵型整数网格平移']}}


def build_increase(source_bytes,options,new_id):
    return _build_increase(source_bytes,options,new_id,monotonic()+4)


def _build_increase(source_bytes,options,new_id,deadline):
    try:
        options=normalise_increase_options(options)
        document=decode_template(source_bytes)
        if not document.width or not document.height or document.width<2 or document.height<2:
            return _error('invalid_source','原模板缺少有效棋盘宽高')
        # Resource bounds guard work, not product height or ratio policy.
        if document.width*document.height>4096 or len(document.tiles)>100000:
            return _error('resource_budget','模板规模较大，本次未能完成试算；请单独处理并缩小摆放区域')
        old_counts=_source_counts(document); total=len(document.tiles)
        if not total: return _error('invalid_source','空模板没有可建立推荐的原阵型')
        if options['target_mode']=='ratio' and Decimal(sum(old_counts))>=Decimal(str(options['max_ratio']))*total:
            result=_error('ratio_unattainable','原塔占比已达到或超过目标上限；追加塔牌只会提高占比，请提高目标范围或改用减少模式')
            result['error']['evidence']={'original_ratio':sum(old_counts)/total,'min_ratio':options['min_ratio'],'max_ratio':options['max_ratio']}
            result['error']['recommendations']=['提高目标占比范围','改用减少塔模式']
            return result
        if options['operation']=='existing' and not old_counts: return _error('no_existing_towers','原模板无塔，请选择新建或混合模式')
        a=sum(t.tile_id==-10 for t in document.tiles); b=sum(t.tile_id==-11 for t in document.tiles)
        fill=options['fill_type'] if options['fill_type']!='auto' else ('A' if a>=b else 'B')
        fill_id=-10 if fill=='A' else -11
        untouched=b if fill=='A' else a
        if untouched%3: return _error('grouping_conflict','未补牌的 A/B 侧不是 3 的倍数，单一补牌类型无法修复')
        physical_limit=(document.width//2)*(document.height//2)*int(max(document.width,document.height)/min(geo.SHIFTS))
        targets,requested=_targets(document,options,sum(old_counts),physical_limit)
        if requested is not None and requested>physical_limit:
            return _error('quantity_outside_geometry','请求数量超过完整展开边界内的几何容量')
        budget=[20000,min(monotonic()+4,deadline)]; cache={}
        for added in targets:
            if added<0 or (total+added)%3 or ((a if fill=='A' else b)+added)%3: continue
            if added==0:
                if requested is not None and abs(requested)>Decimal('1.5'): continue
                if options['target_mode']=='manual' and options['tower_counts']!=old_counts: continue
                editing=options['tower_positions'] or options['tower_directions'] or options['translate_x'] or options['translate_y']
                if not editing:
                    return {'ok':True,'plan':None,'output':None,'summary':{'operation':'increase','no_op':True,'added_tiles':0,'original_total':total,'final_total':total},'error':None}
            # Very distant counts are not quietly selected after budget exhaustion.
            for translation in geo.translations(document,options):
                layout=_layout(document,options,old_counts,added,translation,budget,cache)
                if not layout: continue
                records,counts=layout
                towers=tuple(TowerAddition(i,x,y,d,old_counts[i] if i<len(old_counts) else 0,c,
                    (fill_id,)*(c-(old_counts[i] if i<len(old_counts) else 0))) for i,((x,y,d),c) in enumerate(zip(records,counts)))
                plan=IncreasePlan(sha256(source_bytes).hexdigest(),options,towers,translation,total)
                output=rewrite_increase(source_bytes,plan,new_id)
                from .increase_verification import verify_increase
                report=verify_increase(source_bytes,output,plan,new_id)
                if not report.ok: return _error('verification_failed','; '.join(e.message for e in report.errors))
                summary={'operation':'increase','no_op':False,'original_total':total,'final_total':total+added,
                    'total_tiles_before':total,'total_tiles_after':total+added,'added_tiles':added,
                    'original_tower_tiles':sum(old_counts),'final_tower_tiles':sum(counts),
                    'original_ratio':sum(old_counts)/total,'final_ratio':sum(counts)/(total+added),
                    'fill_type':fill,'translation':list(translation),'adjustment':float(Decimal(added)-requested) if requested is not None else 0,
                    'original_counts':[a,b],'final_counts':[a+(added if fill=='A' else 0),b+(added if fill=='B' else 0)],
                    'towers':[{'index':t.index,'x':t.x,'y':t.y,'direction':t.direction,'original_count':t.original_count,'final_count':t.final_count,'added_tiles':len(t.tile_ids),'new':t.index>=len(old_counts)} for t in towers],
                    'geometry':report.facts.get('geometry',{})}
                return {'ok':True,'plan':plan,'output':output,'summary':summary,'error':None}
            if cache.get('invalid_old_layout',False) and not cache.get('admissible_old_layout',False):
                return _error('existing_tower_layout_unavailable',
                    '原塔移动或改向后超出固定安全区，或与原阵型占用冲突；请调整平移、方向或逐塔位置设置')
            if added>0 and _precheck_new_positions(options) and not cache.get('new_placement_possible',False):
                if cache.get('new_anchors_found',False):
                    return _error('stack_count_symmetry_unavailable',
                        '指定新增堆数无法组成当前区域中的完整对称布局；可设为 0 自动，或调整增加方式、区域')
                return _error('new_tower_positions_unavailable',
                    '当前区域与原塔位置下没有可用的新增塔对称位置；请调整增加方式或摆放区域')
        return _error('no_feasible_layout','所选数量、区域、锁定与完整展开边界没有共同合法方案')
    except RuntimeError as exc:
        return _error('search_budget','本次试算尚未找到可用摆法；可将新增堆数设为 0 自动，或调整增加方式、区域后重试')
    except (ValueError,TypeError,OverflowError,KeyError) as exc:
        return _error('invalid_increase',str(exc))


def recommend_increase(source_bytes,options,new_id):
    try: options=normalise_increase_options(options)
    except (ValueError,TypeError) as exc: return {'schemes':[],'error':_error('invalid_increase',str(exc))['error']}
    variants=[('当前设置',options)]
    if options['regions']==['auto']:
        variants += [(label,{**options,'regions':regions}) for label,regions in (
            ('上下区域',['top','bottom']),('左右区域',['left','right']),('内部空白',['center']))]
    variants.append(('分散布局' if options['spread']=='compact' else '紧凑布局',
                     {**options,'spread':'distributed' if options['spread']=='compact' else 'compact'}))
    schemes=[]; seen=set(); last_error=None; deadline=monotonic()+8
    for label,variant in variants:
        if len(schemes)>=3 or monotonic()>=deadline: break
        result=_build_increase(source_bytes,variant,new_id,deadline)
        if not result['ok']: last_error=result['error']; continue
        if result['output'] is None:
            if not schemes: schemes.append({'label':'已满足目标，无需增加','options':variant,**{k:result[k] for k in ('summary','plan','output')}})
            continue
        key=sha256(result['output']).hexdigest()
        if key in seen: continue
        seen.add(key)
        if result['plan'].translation!=(0,0): label+='（原阵型平移 {}, {}）'.format(*result['plan'].translation)
        plan=result['plan']
        resolved={**variant,'translate_x':plan.translation[0],'translate_y':plan.translation[1],
                  'tower_counts':[t.final_count for t in plan.towers],
                  'tower_positions':[{'index':t.index,'x':t.x,'y':t.y} for t in plan.towers],
                  'tower_directions':[{'index':t.index,'direction':t.direction} for t in plan.towers]}
        new_count=sum(t.original_count==0 for t in plan.towers)
        if new_count: resolved['stack_count']=new_count
        schemes.append({'label':label,'options':resolved,**{k:result[k] for k in ('summary','plan','output')}})
    return {'schemes':schemes,'error':None if schemes else last_error}
