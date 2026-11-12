"""Independent append-only read-back audit; never invokes the tower planner.

Every old wire occurrence is aligned by position and checked against the small
coordinate/ID whitelist. Newly appended TileData must be ordinary canonical A/B
records, followed by new StackData. Geometry uses both editor and client shift.
"""
from collections import Counter
from decimal import Decimal
from hashlib import sha256
from math import gcd

from .protobuf_wire import encode_int32, encode_varint, parse_fields
from .template_reader import decode_template
from .increase_geometry import rectangle,inside,separated,SHIFTS


def verify_increase(source_bytes,output_bytes,plan,expected_template_id):
    from .increase import IncreasePlan,TowerAddition,normalise_increase_options
    from .verification import VerificationReport,VerificationIssue
    errors=[]; warnings=[]; facts={'operation':'increase','source_sha256':None,'output_sha256':None}
    def error(code,message,*evidence): errors.append(VerificationIssue(code,message,tuple(evidence)))
    def warning(code,message,*evidence): warnings.append(VerificationIssue(code,message,tuple(evidence)))
    try:
        if type(source_bytes) is not bytes or type(output_bytes) is not bytes: raise ValueError('bytes required')
        facts.update(source_sha256=sha256(source_bytes).hexdigest(),output_sha256=sha256(output_bytes).hexdigest())
        if type(plan) is not IncreasePlan or plan.source_sha256!=facts['source_sha256']: raise ValueError('原模板证据与增加计划不匹配')
        if plan.symmetry_version not in ('source-v2','legacy-x-v1'): raise ValueError('unknown increase symmetry version')
        source=decode_template(source_bytes); output=decode_template(output_bytes)
        options=normalise_increase_options(plan.options)
        if not isinstance(expected_template_id,str) or not expected_template_id or expected_template_id==source.template_id:
            raise ValueError('新的模板 ID 无效')
        if output.template_id!=expected_template_id: error('template_id','输出 ID 不符合期望')
        if plan.original_tile_count!=len(source.tiles): error('source_count','原牌量证据不符')
        if len(plan.translation)!=2 or any(type(n) is not int for n in plan.translation): raise ValueError('平移必须是整数网格')
        tx,ty=plan.translation
        if not options['auto_translate'] and (tx,ty)!=(options['translate_x'],options['translate_y']):
            error('translation','未授权的自动平移')
        towers=plan.towers
        if len(towers)<len(source.stacks) or len(output.stacks)!=len(towers): raise ValueError('不能删除原塔，塔数需与计划一致')
        anchors={(s.x,s.y):s.index for s in source.stacks}
        if len(anchors)!=len(source.stacks): raise ValueError('源塔锚点重复')
        old_counts=[]
        for s in source.stacks:
            members=sorted((t for t in source.tiles if (t.x,t.y)==(s.x,s.y)),key=lambda t:t.z)
            if not members or [t.z for t in members]!=list(range(len(members))) or any(t.tile_id not in (-10,-11) for t in members):
                raise ValueError('原塔不是普通 A/B 连续层')
            old_counts.append(len(members))
        directions={r['index']:r['direction'] for r in options['tower_directions']}
        positions={r['index']:(r['x'],r['y']) for r in options['tower_positions']}
        if any(i>=len(towers) for i in list(directions)+list(positions)+options['locked_towers']): raise ValueError('单塔覆盖索引不存在')
        if options['stack_count'] is not None and options['operation']!='existing' and len(towers)-len(old_counts)!=options['stack_count']:
            error('stack_count','新塔数量与选项不符')
        for i,t in enumerate(towers):
            if type(t) is not TowerAddition or t.index!=i: raise ValueError('塔记录顺序不符')
            if any(type(n) is not int or not -(2**31)<=n<2**31 for n in (t.x,t.y,t.direction,t.original_count,t.final_count)):
                raise ValueError('塔记录数值无效')
            if t.direction not in range(4) or t.original_count!=(old_counts[i] if i<len(old_counts) else 0) or t.final_count<max(1,t.original_count):
                error('tower_count','不能减少原塔牌数或创建空塔',i)
            if len(t.tile_ids)!=t.final_count-t.original_count or any(type(n) is not int or n not in (-10,-11) for n in t.tile_ids):
                error('added_tile_type','新增牌必须是普通 A/B 且数量符合计划',i)
            old=source.stacks[i] if i<len(source.stacks) else None
            expected_direction=directions.get(i,old.direction if old else options['direction'])
            if t.direction!=expected_direction: error('tower_direction','方向与单塔覆盖/原塔方向不符',i)
            if i in positions and (t.x,t.y)!=positions[i]: error('tower_position','单塔位置覆盖未落实',i)
            if old and i not in positions and (t.x,t.y)!=(old.x+tx,old.y+ty) and not positions:
                error('tower_position','原塔发生未授权位置改变',i)
            if i in options['locked_towers']:
                locked_count=options['tower_counts'][i] if i<len(options['tower_counts']) else t.original_count
                if t.final_count!=locked_count or (old and (t.x,t.y,t.direction)!=(old.x+tx,old.y+ty,old.direction)):
                    error('locked_tower','锁定塔被改变',i)
            if old and options['operation']=='new' and t.final_count!=t.original_count: error('operation','仅新建模式不能给原塔加层')
            if not old and options['operation']=='existing': error('operation','原塔加层模式不能新建塔')
            s=output.stacks[i]
            if (s.x,s.y,s.direction)!=(t.x,t.y,t.direction): error('stack_plan','StackData 不符合计划',i)
        added=sum(len(t.tile_ids) for t in towers)
        if len(output.tiles)!=len(source.tiles)+added or added<0: error('total_count','输出总量不满足仅追加约束')
        if len({(t.x,t.y) for t in towers})!=len(towers): error('duplicate_stack','输出塔锚点重复')
        totals=tuple(sum(t.tile_id==n for t in output.tiles) for n in (-10,-11))
        if len(output.tiles)%3 or any(n%3 for n in totals): error('groups_of_three','最终总牌量及 A/B 侧都应是 3 的倍数')
        before=tuple(sum(t.tile_id==n for t in source.tiles) for n in (-10,-11))
        fill=options['fill_type'] if options['fill_type']!='auto' else ('A' if before[0]>=before[1] else 'B')
        wanted_id=-10 if fill=='A' else -11
        if any(n!=wanted_id for t in towers for n in t.tile_ids): error('fill_type','新增牌不符合补牌类型/原模板多数类型')
        if options['target_mode']=='ratio':
            ratio=Decimal(sum(t.final_count for t in towers))/Decimal(len(output.tiles))
            if not Decimal(str(options['min_ratio']))<=ratio<Decimal(str(options['max_ratio'])): error('ratio','最终占比不在含下限、不含上限的目标范围')
        if options['target_mode']=='manual' or options['height_mode']=='manual':
            if len(towers)!=len(options['tower_counts']): error('manual_counts','逐塔最终张数条目不齐全')
        # Ordered occurrence whitelist: ID, original XY, authorized directions.
        old_tiles={t.top_field_index:t for t in source.tiles}
        old_stacks={s.top_field_index:s for s in source.stacks}
        expected_extra=added+len(towers)-len(source.stacks)
        if len(output.fields)!=len(source.fields)+expected_extra: error('wire_count','顶层字段并非原字段加授权追加')
        def inspect_changed(before_field,after_field,values,original,label):
            if before_field.tag_raw!=after_field.tag_raw or after_field.wire_type!=2:
                error('wire_tag','原字段标识被修改',label); return
            wanted_length=before_field.length_raw if len(before_field.payload)==len(after_field.payload) else encode_varint(len(after_field.payload))
            if after_field.length_raw!=wanted_length: error('wire_length','原长度编码未按白名单保留',label)
            old=parse_fields(before_field.payload); new=parse_fields(after_field.payload)
            last={f.number:j for j,f in enumerate(old) if f.number in values}
            appended=[n for n in values if n not in last and values[n]!=original[n]]
            if len(new)!=len(old)+len(appended): error('wire_nested_count','原子字段数量改变',label); return
            for j,(a,b) in enumerate(zip(old,new)):
                expected=a.tag_raw+encode_int32(values[a.number]) if a.number in values and last[a.number]==j and values[a.number]!=original[a.number] else a.raw
                if b.raw!=expected: error('wire_original','原字段字节超出授权 XY/方向白名单',label,j)
            for n,f in zip(appended,new[len(old):]):
                if f.raw!=encode_varint(n<<3)+encode_int32(values[n]): error('wire_append','授权缺省字段追加不符',label)
        for i,(a,b) in enumerate(zip(source.fields,output.fields)):
            if i==source.template_id_field_index:
                wanted_len=a.length_raw if len(a.payload)==len(expected_template_id.encode('utf-8')) else encode_varint(len(expected_template_id.encode('utf-8')))
                if b.raw!=a.tag_raw+wanted_len+expected_template_id.encode('utf-8'): error('wire_id','ID 字段白名单不符')
            elif i in old_tiles:
                tile=old_tiles[i]; ti=anchors.get((tile.x,tile.y))
                x,y=(towers[ti].x,towers[ti].y) if ti is not None else (tile.x+tx,tile.y+ty)
                inspect_changed(a,b,{1:x,2:y},{1:tile.x,2:tile.y},('tile',tile.index))
            elif i in old_stacks:
                s=old_stacks[i]; t=towers[s.index]
                inspect_changed(a,b,{1:t.x,2:t.y,3:t.direction},{1:s.x,2:s.y,3:s.direction},('stack',s.index))
            elif a.raw!=b.raw: error('wire_unknown','原顶层未知字段/元数据被改变',i)
        appended=output.fields[len(source.fields):]; expected=[]
        for t in towers:
            for z,tile_id in enumerate(t.tile_ids,t.original_count):
                expected.append((6,((1,t.x),(2,t.y),(3,z),(4,tile_id))))
        for t in towers[len(source.stacks):]: expected.append((7,((1,t.x),(2,t.y),(3,t.direction))))
        for field,(number,values) in zip(appended,expected):
            payload=b''.join(encode_varint(n<<3)+encode_int32(v) for n,v in values)
            raw=encode_varint(number<<3|2)+encode_varint(len(payload))+payload
            if field.raw!=raw: error('wire_new','追加字段含特殊效果、未知字段或顺序不符')
        # Independent geometric read-back, including all non-tower layers.
        ordinary={(t.x+tx,t.y+ty) for t in source.tiles if (t.x,t.y) not in anchors}
        if ordinary and 'auto' not in options['regions']:
            lx,hx=min(x for x,y in ordinary),max(x for x,y in ordinary)
            ly,hy=min(y for x,y in ordinary),max(y for x,y in ordinary)
            for t in towers[len(source.stacks):]:
                regions=[]
                if t.y+2<=ly: regions.append('top')
                if t.y>=hy+2: regions.append('bottom')
                if t.x+2<=lx: regions.append('left')
                if t.x>=hx+2: regions.append('right')
                if lx<=t.x<=hx and ly<=t.y<=hy: regions.append('center')
                if not set(regions)&set(options['regions']): error('tower_region','新塔位置不在请求区域内',t.index)
        for tower in towers:
            members=sorted(t.z for t in output.tiles if (t.x,t.y)==(tower.x,tower.y))
            if members!=list(range(tower.final_count)):
                error('tower_members','读回塔的真实数量/层序不符，或新塔吞并原非塔牌',tower.index)
        active=[i>=len(source.stacks) or (t.x,t.y,t.direction,t.final_count)!=(source.stacks[i].x,source.stacks[i].y,source.stacks[i].direction,old_counts[i]) for i,t in enumerate(towers)]
        for i,t in enumerate(towers):
            if active[i] and not (2<=t.x<=14 and 2<=t.y<=16):
                error('safe_bounds','新增、移动、改向或加高的塔超出固定锚点安全区 X=2–14、Y=2–16',i,t.x,t.y)
        for t in output.tiles:
            unchanged=t.index<len(source.tiles) and (t.x,t.y)==(source.tiles[t.index].x,source.tiles[t.index].y)
            if not (2<=t.x<=14 and 2<=t.y<=16):
                (warning if unchanged else error)(
                    'existing_safe_bounds' if unchanged else 'safe_bounds',
                    '保留源牌的既有安全区外锚点' if unchanged else '新增或移动牌超出固定锚点安全区 X=2–14、Y=2–16',
                    t.index,t.x,t.y)
            if not inside(rectangle(t.x,t.y),output.width,output.height):
                (warning if unchanged else error)('logical_bounds','逻辑牌占位超出棋盘（未改源牌仅记录）',t.index)
        for shift,label in ((0,'logical'),(SHIFTS[0],'editor'),(SHIFTS[1],'client')):
            for x,y in ordinary:
                if not inside(rectangle(x,y,shift=shift),output.width,output.height):
                    (error if tx or ty else warning)(label+'_ordinary_bounds','主阵型可见牌身外框越界（未改源牌仅记录）',x,y)
            rectangles=[rectangle(t.x,t.y,t.final_count,t.direction,shift) for t in towers]
            for i,rect in enumerate(rectangles):
                report=error if active[i] else warning
                if not inside(rect,output.width,output.height): report(label+'_bounds','完整展开外框越界（未改源塔仅记录）',i)
                if any(not separated(rect,rectangle(x,y,shift=shift),options['formation_gap']) for x,y in ordinary): report(label+'_formation_overlap','塔完整外框与主阵型占用/间距冲突（未改源塔仅记录）',i)
                for j,other in enumerate(rectangles[i+1:],i+1):
                    if not separated(rect,other,options['spacing']):
                        (error if active[i] or active[j] else warning)(label+'_tower_overlap','塔完整外框之间冲突（未改源塔仅记录）',i,j)
            facts.setdefault('geometry',{})[label]=[list(r) for r in rectangles]
        # Recover symmetry from original non-tower layers, independently.
        original_positions=Counter((t.x+tx,t.y+ty,t.z) for t in source.tiles if (t.x,t.y) not in anchors)
        if not original_positions: original_positions=Counter((t.x+tx,t.y+ty,t.z) for t in source.tiles)
        if original_positions:
            lx,hx=min(p[0] for p in original_positions),max(p[0] for p in original_positions)
            ly,hy=min(p[1] for p in original_positions),max(p[1] for p in original_positions)
            if plan.symmetry_version=='legacy-x-v1':
                transforms=[lambda x,y:(lx+hx-x,y)]
                if ly!=hy and Counter((x,ly+hy-y,z) for x,y,z in original_positions.elements())==original_positions:
                    transforms.append(lambda x,y:(x,ly+hy-y))
            else:
                transforms=[]
                if Counter((lx+hx-x,y,z) for x,y,z in original_positions.elements())==original_positions:
                    transforms.append(lambda x,y:(lx+hx-x,y))
                if ly!=hy and Counter((x,ly+hy-y,z) for x,y,z in original_positions.elements())==original_positions:
                    transforms.append(lambda x,y:(x,ly+hy-y))
                if lx!=hx and ly!=hy and Counter((lx+hx-x,ly+hy-y,z) for x,y,z in original_positions.elements())==original_positions:
                    transforms.append(lambda x,y:(lx+hx-x,ly+hy-y))
                if not transforms:
                    transforms.append(lambda x,y:(lx+hx-x,ly+hy-y))
            newpoints={(t.x,t.y) for t in towers[len(source.stacks):]}
            for transform in transforms:
                if {transform(x,y) for x,y in newpoints}!=newpoints: error('position_symmetry','新塔位置破坏原阵型位置对称')
            for i,s in enumerate(source.stacks):
                if i in positions or (towers[i].x,towers[i].y)==(s.x+tx,s.y+ty): continue
                allowed=False
                for j,p in positions.items():
                    if j>=len(source.stacks): continue
                    original=source.stacks[j]
                    orbit={(original.x+tx,original.y+ty,p[0],p[1])}
                    for transform in transforms:
                        orbit|={(*transform(x,y),*transform(nx,ny)) for x,y,nx,ny in tuple(orbit)}
                    if (s.x+tx,s.y+ty,towers[i].x,towers[i].y) in orbit: allowed=True
                if not allowed: error('tower_position','原塔移动既非直接授权也非对称联动',i)
            # Recover platform groups from source height and actual mirror
            # positions, rather than trusting the plan's per-tower assertions.
            groups=[]; grouped=set()
            for i,tower in enumerate(towers):
                if i in grouped: continue
                orbit={(tower.x,tower.y)}
                for transform in transforms: orbit|={transform(x,y) for x,y in tuple(orbit)}
                group=tuple(j for j,t in enumerate(towers) if (t.x,t.y) in orbit and
                            ((i<len(old_counts) and j<len(old_counts) and old_counts[i]==old_counts[j]) or
                             (i>=len(old_counts) and j>=len(old_counts))))
                groups.append(group); grouped.update(group)
                if len({towers[j].final_count for j in group})!=1:
                    error('mirror_height','同一镜像平台的最终高度不同',*group)
            manual=options['target_mode']=='manual' or options['height_mode']=='manual'
            if not manual and any(old_counts[i]<old_counts[j] and towers[i].final_count>towers[j].final_count
                                  for i in range(len(old_counts)) for j in range(len(old_counts))):
                error('platform_order','原有高低平台关系被反转')
            if manual and len(options['tower_counts'])==len(towers):
                requested=options['tower_counts']
                current=[t.final_count for t in towers]
                def legal_manual_counts(values):
                    if sum(values)!=sum(current) or any(n<max(1,t.original_count) for n,t in zip(values,towers)): return False
                    if any(len({values[j] for j in group})!=1 for group in groups): return False
                    if any(values[j]!=(requested[j] if j<len(requested) else towers[j].original_count) for j in options['locked_towers']): return False
                    if options['operation']=='new' and values[:len(old_counts)]!=old_counts: return False
                    altered=[i>=len(source.stacks) or (t.x,t.y,t.direction,n)!=(source.stacks[i].x,source.stacks[i].y,source.stacks[i].direction,old_counts[i])
                             for i,(t,n) in enumerate(zip(towers,values))]
                    if any(altered[i] and not (2<=t.x<=14 and 2<=t.y<=16)
                           for i,t in enumerate(towers)): return False
                    for shift in (0,)+SHIFTS:
                        rects=[rectangle(t.x,t.y,n,t.direction,shift) for t,n in zip(towers,values)]
                        if any(altered[i] and (not inside(r,output.width,output.height) or
                               any(not separated(r,rectangle(x,y,shift=shift),options['formation_gap']) for x,y in ordinary))
                               for i,r in enumerate(rects)): return False
                        if any((altered[i] or altered[j]) and not separated(r,s,options['spacing'])
                               for i,r in enumerate(rects) for j,s in enumerate(rects) if j>i): return False
                    return True
                # Exact requested values take precedence whenever jointly legal.
                # Otherwise check equal-total symmetric exchanges against the
                # requested per-tower values, preserving nearest legal rounding.
                current_cost=sum((n-r)**2 for n,r in zip(current,requested))
                if current!=requested and legal_manual_counts(requested):
                    error('manual_intent','合法的逐塔最终数量未按用户输入落实')
                else:
                    adjustable=[g for g in groups if not any(j in options['locked_towers'] for j in g)
                                and (options['operation']!='new' or g[0]>=len(old_counts))]
                    found=False
                    for gi,left in enumerate(adjustable):
                        for right in adjustable[gi+1:]:
                            divisor=gcd(len(left),len(right))
                            for sign in (-1,1):
                                candidate=list(current)
                                for j in left: candidate[j]+=sign*len(right)//divisor
                                for j in right: candidate[j]-=sign*len(left)//divisor
                                if sum((n-r)**2 for n,r in zip(candidate,requested))<current_cost and legal_manual_counts(candidate):
                                    error('manual_intent','存在更接近逐塔输入且总量不变的合法镜像平台分配')
                                    found=True; break
                            if found: break
                        if found: break
            # A quantity certificate must not certify an obviously farther
            # result while a closer legal symmetric increment exists at these
            # exact anchors. This is an independent bounded neighbourhood audit,
            # not a call back into the layout/quantity planner.
            mode=options['target_mode']
            desired=(Decimal(str(options['added_tiles'])) if mode=='count' else
                     Decimal(len(source.tiles))*(Decimal(str(options['total_multiplier']))-1) if mode=='multiplier' else
                     Decimal(sum(options['tower_counts'])-sum(old_counts)) if mode=='manual' else None)
            if desired is not None:
                current_score=(abs(Decimal(added)-desired),-added)
                checked=set()
                for i,tower in enumerate(towers):
                    if i in checked: continue
                    orbit={(tower.x,tower.y)}
                    for transform in transforms: orbit|={transform(x,y) for x,y in tuple(orbit)}
                    group=tuple(j for j,t in enumerate(towers) if (t.x,t.y) in orbit and
                                ((i<len(old_counts) and j<len(old_counts) and old_counts[i]==old_counts[j]) or
                                 (i>=len(old_counts) and j>=len(old_counts))))
                    checked.update(group)
                    if any(j in options['locked_towers'] for j in group) or (options['operation']=='new' and i<len(old_counts)): continue
                    step=3//gcd(3,len(group))
                    for delta in (-step,step):
                        alternative=added+delta*len(group)
                        if alternative<0 or (abs(Decimal(alternative)-desired),-alternative)>=current_score: continue
                        counts=[t.final_count+(delta if j in group else 0) for j,t in enumerate(towers)]
                        if any(c<max(1,t.original_count) for c,t in zip(counts,towers)): continue
                        if any(old_counts[a]<old_counts[b] and counts[a]>counts[b] for a in range(len(old_counts)) for b in range(len(old_counts))): continue
                        valid=True
                        if any((active[j] or j in group) and not (2<=t.x<=14 and 2<=t.y<=16)
                               for j,t in enumerate(towers)): continue
                        for shift in (0,)+SHIFTS:
                            rects=[rectangle(t.x,t.y,c,t.direction,shift) for t,c in zip(towers,counts)]
                            altered=[active[j] or j in group for j in range(len(towers))]
                            if any(altered[j] and (not inside(r,output.width,output.height) or any(not separated(r,rectangle(x,y,shift=shift),options['formation_gap']) for x,y in ordinary)) for j,r in enumerate(rects)): valid=False; break
                            if any((altered[j] or altered[k]) and not separated(r,s,options['spacing']) for j,r in enumerate(rects) for k,s in enumerate(rects) if k>j): valid=False; break
                        if valid: error('quantity_target','方案数量并非这些位置、对称与锁定约束下更接近目标的合法数量')
        facts.update(original_total=len(source.tiles),final_total=len(output.tiles),added_tiles=added,
                     original_counts=before,final_counts=totals,tower_tiles=sum(t.final_count for t in towers))
    except (ValueError,TypeError,IndexError,KeyError,AttributeError,ZeroDivisionError,OverflowError) as exc:
        error('invalid_increase_evidence',str(exc))
    from .verification import enforce_output_structure
    return enforce_output_structure(VerificationReport(facts,tuple(errors),tuple(warnings)), output_bytes,source_bytes=source_bytes,plan=plan)
