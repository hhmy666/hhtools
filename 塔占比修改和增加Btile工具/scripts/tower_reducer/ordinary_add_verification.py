"""Independent ordinary-add read-back audit; never calls a writer or solver."""
from collections import Counter, defaultdict
from hashlib import sha256

from .geometry import overlap_area, overlap_profile, symmetry_orbit, within_bounds
from .increase_geometry import SHIFTS, rectangle, separated
from .ordinary_add import OrdinaryAddPlan, OrdinaryAddition, normalize_ordinary_add_options
from .protobuf_wire import encode_int32, encode_varint, parse_fields
from .template_reader import decode_template
from .verification import VerificationIssue, VerificationReport


def verify_ordinary_add(source_bytes,output_bytes,plan,expected_template_id):
    facts={}
    errors=[]
    warnings=[]
    def error(code,message,*evidence):
        errors.append(VerificationIssue(code,message,tuple(evidence)))
    try:
        if type(source_bytes) is not bytes or type(output_bytes) is not bytes:
            raise ValueError('模板必须是 bytes')
        if type(plan) is not OrdinaryAddPlan:
            raise ValueError('需要 OrdinaryAddPlan')
        source=decode_template(source_bytes)
        output=decode_template(output_bytes)
        anchors={(s.x,s.y) for s in source.stacks}
        facts.update(source_sha256=sha256(source_bytes).hexdigest(),output_sha256=sha256(output_bytes).hexdigest())
        options=normalize_ordinary_add_options(plan.options)
        if facts['source_sha256']!=plan.source_sha256 or plan.original_tile_count!=len(source.tiles):
            error('source_mismatch','计划与原模板不匹配')
        if not isinstance(expected_template_id,str) or not expected_template_id or expected_template_id==source.template_id:
            raise ValueError('期望 ID 必须非空且不同于源 ID')
        expected_template_id.encode('utf-8')
        if output.template_id!=expected_template_id:
            error('template_id','输出模板 ID 不符')
        offset=plan.original_z_offset
        additions=plan.additions
        wanted_id=-10 if options['tile_type']=='A' else -11
        quantity_ok=(len(additions)==options['added_tiles'] if options['target_mode']=='count' else
                     options['min_total_tiles']<=len(source.tiles)+len(additions)<=options['max_total_tiles'])
        if (not additions or not quantity_ok or any(type(t) is not OrdinaryAddition or t.tile_id!=wanted_id for t in additions)):
            error('quantity_or_type','新增数量或 A/B 类型不符合输入')
        if len(output.tiles)!=len(source.tiles)+len(additions):
            error('tile_count','输出牌数不是原牌加新增牌')
        if len(output.fields)!=len(source.fields)+len(additions):
            error('wire_top_count','原字段 occurrence 数量或新增字段数量不符')
        by_top={t.top_field_index:t for t in source.tiles}
        for index,before in enumerate(source.fields):
            if index>=len(output.fields):
                break
            after=output.fields[index]
            tile=by_top.get(index)
            if index==source.template_id_field_index:
                payload=expected_template_id.encode('utf-8')
                wanted_length=before.length_raw if len(payload)==len(before.payload) else encode_varint(len(payload))
                if after.tag_raw!=before.tag_raw or after.payload!=payload or after.length_raw!=wanted_length:
                    error('wire_template_id','仅允许改写生效模板 ID，保留原 tag 和可复用长度编码')
            elif tile is not None and offset and (tile.x,tile.y) not in anchors:
                nested=parse_fields(after.payload)
                last=next((i for i in range(len(tile.fields)-1,-1,-1) if tile.fields[i].number==3),None)
                expected=[]
                for i,field in enumerate(tile.fields):
                    expected.append(field.tag_raw+encode_int32(tile.z+offset) if i==last else field.raw)
                if last is None:
                    expected.append(b'\x18'+encode_int32(tile.z+offset))
                body=b''.join(expected)
                length=before.length_raw if len(body)==len(before.payload) else encode_varint(len(body))
                if (after.tag_raw!=before.tag_raw or after.length_raw!=length or [f.raw for f in nested]!=expected):
                    error('wire_original_tile','原牌只允许统一偏移生效 Z；其他字段及 occurrence 顺序须原样保留',tile.index)
            elif after.raw!=before.raw:
                error('wire_unauthorized','未经授权的原字段或原牌字节发生变化',index,before.number)
        for index,tile in enumerate(additions):
            top_index=len(source.fields)+index
            if top_index>=len(output.fields):
                break
            field=output.fields[top_index]
            values=(tile.x,tile.y,tile.z,tile.tile_id)
            payload=b''.join(encode_varint(n << 3)+encode_int32(v) for n,v in enumerate(values,1))
            expected=b'\x32'+encode_varint(len(payload))+payload
            if field.raw!=expected:
                error('wire_append','新增 occurrence 仅允许无特殊字段的普通 A/B TileData',index)
        if len(output.stacks)!=len(source.stacks):
            error('tower_count','普通牌增加不能增加或删除塔')
        for before,after in zip(source.tiles,output.tiles):
            tile_offset=0 if (before.x,before.y) in anchors else offset
            if (after.x,after.y,after.z,after.tile_id,after.debuff_data_index)!=(before.x,before.y,before.z+tile_offset,before.tile_id,before.debuff_data_index):
                error('original_tile','原牌 XY、类型、效果或相对层关系改变',before.index)
        counts=[sum(t.tile_id==kind for t in output.tiles) for kind in (-10,-11)]
        if len(output.tiles)%3 or any(n%3 for n in counts):
            error('grouping','最终 A/B 张数及含特殊牌的总牌数必须分别为 3 的倍数',counts,len(output.tiles))
        ordinary=[t for t in source.tiles if (t.x,t.y) not in anchors]
        if not ordinary:
            raise ValueError('源模板没有非塔承接层')
        low=min(t.z for t in ordinary)+offset
        high=max(t.z for t in ordinary)+offset
        new_levels={t.z for t in additions if t.z<low or t.z>high}
        actual_layers=len(new_levels)
        minimum=0 if options['layer_mode']=='auto' else options['min_added_layers']
        if not minimum<=actual_layers<=options['max_added_layers']:
            error('added_layers','实际新增层数不符合输入',actual_layers)
        if any(low<t.z<high for t in additions):
            error('interior_layer','只允许在非塔最顶层、最底层或外部连续新增层增加牌')
        if ((options['placement_strategy']=='top_first' and any(t.z<high for t in additions))
                or (options['placement_strategy']=='bottom_first' and any(t.z>low for t in additions))):
            error('placement_side','新增牌超出所选最顶层或最底层一侧')
        if options['placement_strategy']=='both':
            top_count=sum(t.z>=high for t in additions)
            bottom_count=sum(t.z<=low for t in additions)
            if (not top_count or not bottom_count or (low==high and any(t.z==low for t in additions))):
                error('placement_side','上下同时补入须在两侧各有新牌；单层原阵型须在其上方和下方分别加层')
            if options['split_mode']=='custom' and (top_count,bottom_count)!=(options['top_tiles'],options['bottom_tiles']):
                error('split_quantity','上下实际张数不符合自定义分配',top_count,bottom_count)
        if new_levels:
            expected_levels=set(range(min(new_levels|{low}),low))|set(range(high+1,max(new_levels|{high})+1))
            if new_levels!=expected_levels:
                error('layer_gap','新增外层须逐层连续，不能跳过空层')
        expected_offset=max(0,-min([t.z for t in ordinary]+[t.z-offset for t in additions]))
        if offset!=expected_offset:
            error('z_offset','原非塔牌 Z 偏移只能是使非塔层号非负的最小统一偏移',offset,expected_offset)
        if any(t.z<0 for t in output.tiles):
            error('negative_z','输出层号不能为负')
        towers=[]
        for stack in source.stacks:
            count=sum((t.x,t.y)==(stack.x,stack.y) for t in source.tiles)
            if not count or stack.direction not in range(4):
                raise ValueError('源塔信息无效')
            towers.append((stack.x,stack.y,count,stack.direction))
        layers=defaultdict(list)
        for tile in ordinary:
            layers[tile.z+offset].append((tile.x,tile.y))
        original_layers={z:list(points) for z,points in layers.items()}
        for i,tile in enumerate(additions):
            point=(tile.x,tile.y)
            if not within_bounds(point,(output.width,output.height)):
                error('safe_bounds','新增牌完整 2×2 footprint 超出安全区或棋盘',i)
            if any(overlap_area(point,p) for p in layers.get(tile.z,())):
                error('same_layer_collision','新增牌造成同层重叠',i)
            if any(not separated(rectangle(*point,shift=shift),rectangle(*tower,shift=shift))
                   for tower in towers for shift in (0,)+SHIFTS):
                error('tower_visual_collision','新增普通牌与原塔实际展开 footprint 相交',i)
            layers[tile.z].append(point)
        added_by_z=defaultdict(list)
        for tile in additions:
            added_by_z[tile.z].append((tile.x,tile.y))
            if any(overlap_area((tile.x,tile.y),p)==4 for z in (tile.z-1,tile.z+1) for p in layers.get(z,())):
                error('full_cover','新增牌与相邻层形成一对一完整重叠')
            if len(layers)>1 and not any(overlap_area((tile.x,tile.y),p) for z in (tile.z-1,tile.z+1) for p in layers.get(z,())):
                error('unsupported','新增牌未接触相邻非塔层')
        floor=min(layers)
        for z,points in layers.items():
            # Every newly introduced lower surface must support ALL of the
            # immediately upper layer, including previously bottommost originals.
            required=points if z-1<low and z-1 in new_levels else added_by_z.get(z,())
            if z>floor and any(not overlap_profile(p,layers.get(z-1,())) for p in required):
                error('unsupported_upper','下方新增层未承接全部上层牌，存在悬空 Tile',z)
        source_units={area for z,points in original_layers.items() for p in points
                      for area in overlap_profile(p,original_layers.get(z-1,())) if area in (1,2)}
        units=source_units
        for z,new_points in added_by_z.items():
            direction=1 if options['placement_strategy']=='bottom_first' or options['placement_strategy']=='both' and z<=low else -1
            adjacent=layers.get(z+direction,())
            new_profiles=[overlap_profile(p,adjacent) for p in new_points]
            selected_units={area for profile in new_profiles for area in profile}
            if len(selected_units)>1 or 4 in selected_units:
                error('layer_stacking_style','同一新增层须使用统一重叠单位；承接张数可以不同',z)
            reference_units={area for p in original_layers.get(z,())
                             for area in overlap_profile(p,original_layers.get(z+direction,())) if area in (1,2)}
            if len(reference_units)==1 and not selected_units.issubset(reference_units):
                error('inherited_stacking_style','新增牌未使用同层原牌的重叠单位',z)
            neighbor_units={area for p in adjacent for area in overlap_profile(p,layers.get(z+2*direction,())) if area in (1,2)}
            if len(neighbor_units)==1 and not selected_units.issubset(neighbor_units):
                error('inherited_stacking_style','新增牌未继承紧邻层的重叠单位',z)
        for z,points in layers.items():
            lower=layers.get(z-1,())
            if not lower or not (z in added_by_z or z-1 in added_by_z):
                continue
            changed_profiles=[overlap_profile(p,lower) for p in added_by_z.get(z,())]
            changed_profiles += [overlap_profile(p,lower) for p in points
                                 if any(overlap_area(p,new) for new in added_by_z.get(z-1,()))]
            if units and any(not set(profile).issubset(units) for profile in changed_profiles):
                error('inherited_stacking_style','新增相邻层没有继承原层的重叠单位',z)
            for upper in original_layers.get(z,()):
                before=overlap_profile(upper,original_layers.get(z-1,()))
                incoming={overlap_area(upper,p) for p in added_by_z.get(z-1,()) if overlap_area(upper,p)}
                if before and not incoming.issubset(set(before)):
                    error('inherited_stacking_style','新增承接牌改变了原牌已有的重叠单位',z,upper)
        axes=(min(t.x for t in ordinary)+max(t.x for t in ordinary),min(t.y for t in ordinary)+max(t.y for t in ordinary))
        positions=Counter((t.x,t.y,t.z) for t in ordinary)
        transforms=(('horizontal',lambda x,y:(axes[0]-x,y)),('vertical',lambda x,y:(x,axes[1]-y)),
                    ('rotation',lambda x,y:(axes[0]-x,axes[1]-y)))
        symmetries=tuple(name for name,fn in transforms if Counter((*fn(x,y),z) for x,y,z in positions.elements())==positions)
        for z,points in added_by_z.items():
            counts_by_point=Counter(points)
            for point in points:
                if any(counts_by_point[p]!=counts_by_point[point] for p in symmetry_orbit(point,axes,symmetries)):
                    error('symmetry','新增牌未保留原非塔层实际镜像或旋转对称',z,point)
                    break
        facts.update(original_total=len(source.tiles),final_total=len(output.tiles),added_tiles=len(additions),
                     original_z_offset=offset,added_layers=actual_layers,final_counts=counts)
    except (ValueError,TypeError,OverflowError,IndexError,KeyError,AttributeError,UnicodeError) as exc:
        error('invalid_ordinary_add_evidence',str(exc))
    from .verification import enforce_output_structure
    return enforce_output_structure(VerificationReport(facts,tuple(errors),tuple(warnings)), output_bytes,source_bytes=source_bytes,plan=plan)
