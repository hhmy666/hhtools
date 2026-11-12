"""Explicit reversible Tile edits and rigid transforms on verified output."""
from dataclasses import dataclass, replace
from hashlib import sha256
import re

from .geometry import overlap_area, within_bounds
from .protobuf_wire import encode_int32, encode_varint
from .template_reader import decode_template


@dataclass(frozen=True)
class ManualTile:
    x: int
    y: int
    z: int
    tile_id: int

    def __post_init__(self):
        if (any(type(v) is not int for v in (self.x,self.y,self.z,self.tile_id))
                or not 2<=self.x<=14 or not 2<=self.y<=16 or not 0<=self.z<=255
                or self.tile_id not in (-10,-11)):
            raise ValueError('新增牌需为安全区内的A/B牌，层号为1–256')


@dataclass(frozen=True)
class TowerRemovalPlan:
    """Trusted source-bound seed, never a verified deliverable by itself."""
    source_sha256: str

    def __post_init__(self):
        if type(self.source_sha256) is not str or not re.fullmatch('[0-9a-f]{64}',self.source_sha256):
            raise ValueError('删塔预览缺少源文件摘要')


def tower_removal_fields(source,plan,new_id):
    from .protobuf_wire import replace_length_delimited
    if sha256(source).hexdigest()!=plan.source_sha256:
        raise ValueError('删塔源文件已变化')
    document=decode_template(source)
    if not isinstance(new_id,str) or not new_id or new_id==document.template_id:
        raise ValueError('删塔结果需使用新的模板ID')
    anchors={(s.x,s.y) for s in document.stacks}
    removed={s.top_field_index for s in document.stacks}|{t.top_field_index for t in document.tiles if (t.x,t.y) in anchors}
    return tuple(replace_length_delimited(f,new_id.encode('utf-8')) if i==document.template_id_field_index else f.raw
                 for i,f in enumerate(document.fields) if i not in removed)


def initial_plan(plan):
    from .translation import TranslationPlan
    for _ in range(17):
        if type(plan) not in (ManualEditPlan,TranslationPlan):
            return plan
        plan=plan.base_plan
    raise ValueError('微调历史过长，请重新生成后再微调')


def source_indices(source,plan,new_id):
    """Trace surviving source occurrences through authorized revision wrappers."""
    from .translation import TranslationPlan
    from .rewrite import rewrite_template
    if type(plan) is TranslationPlan:
        return source_indices(source,plan.base_plan,new_id)
    if type(plan) is ManualEditPlan:
        previous=source_indices(source,plan.base_plan,new_id)
        return [origin for i,origin in enumerate(previous) if i not in plan.deleted_indices]+[None]*len(plan.additions)
    if type(plan) is TowerRemovalPlan:
        document=decode_template(source)
        anchors={(s.x,s.y) for s in document.stacks}
        return [t.index for t in document.tiles if (t.x,t.y) not in anchors]
    return list(range(len(decode_template(source).tiles)))+[None]*(len(decode_template(rewrite_template(source,plan,new_id)).tiles)-len(decode_template(source).tiles))


def source_transform(plan):
    """Compose caller-owned transforms for original-occurrence highlighting."""
    from .translation import TranslationPlan,rotate_vector
    if type(plan) not in (ManualEditPlan,TranslationPlan):
        return 0,0,0
    q,dx,dy=source_transform(plan.base_plan)
    turn=plan.quarter_turns if type(plan) is ManualEditPlan else 0
    extra=plan.translation if type(plan) is ManualEditPlan else (plan.dx,plan.dy)
    dx,dy=rotate_vector(dx,dy,turn)
    return (q+turn)%4,dx+extra[0],dy+extra[1]


@dataclass(frozen=True)
class ManualEditPlan:
    base_plan: object
    source_sha256: str
    deleted_indices: tuple
    additions: tuple
    translation: tuple = (0, 0)
    quarter_turns: int = 0

    def __post_init__(self):
        from .rewrite import RewritePlan
        from .increase import IncreasePlan
        from .ordinary_add import OrdinaryAddPlan
        from .translation import TranslationPlan
        if type(self.base_plan) not in (RewritePlan,IncreasePlan,OrdinaryAddPlan,TranslationPlan,TowerRemovalPlan):
            raise ValueError('手动微调需要可信生成计划')
        if type(self.source_sha256) is not str or not re.fullmatch('[0-9a-f]{64}',self.source_sha256):
            raise ValueError('手动微调缺少源文件摘要')
        if (type(self.deleted_indices) not in (tuple,list) or len(self.deleted_indices)>10000
                or any(type(n) is not int or n<0 for n in self.deleted_indices)
                or len(set(self.deleted_indices))!=len(self.deleted_indices)):
            raise ValueError('删除牌索引不合法')
        if type(self.additions) not in (tuple,list) or len(self.additions)>1000 or any(type(t) is not ManualTile for t in self.additions):
            raise ValueError('新增牌数据不合法，单次最多1000张')
        object.__setattr__(self,'deleted_indices',tuple(sorted(self.deleted_indices)))
        object.__setattr__(self,'additions',tuple(self.additions))
        if (type(self.translation) not in (tuple,list) or len(self.translation)!=2
                or any(type(n) is not int or not -32<=n<=32 for n in self.translation)):
            raise ValueError('整体平移量需为-32到32之间的整数')
        object.__setattr__(self,'translation',tuple(self.translation))
        if type(self.quarter_turns) is not int or self.quarter_turns not in range(4):
            raise ValueError('整体旋转量需为0到3之间的整数')
        initial_plan(self)


def _tile_wire(tile):
    payload=b''.join(encode_varint(n<<3)+encode_int32(v)
                     for n,v in enumerate((tile.x,tile.y,tile.z,tile.tile_id),1))
    return b'\x32'+encode_varint(len(payload))+payload


def _kept_fields(before,plan):
    deleted=set(plan.deleted_indices)
    if any(n>=len(before.tiles) for n in deleted):
        raise ValueError('删除牌已变化，请重新打开对比')
    removed={before.tiles[n].top_field_index for n in deleted}
    dx,dy=plan.translation
    from .translation import transform_point, _transform_payload
    remaining=[transform_point(t.x,t.y,plan.quarter_turns,dx,dy) for t in before.tiles if t.index not in deleted]+[(t.x,t.y) for t in plan.additions]
    for stack in before.stacks:
        if transform_point(stack.x,stack.y,plan.quarter_turns,dx,dy) not in remaining:
            removed.add(stack.top_field_index)
    from .protobuf_wire import replace_length_delimited
    items={t.top_field_index:t for t in (*before.tiles,*before.stacks)}
    return tuple(replace_length_delimited(f,_transform_payload(items[i],plan.quarter_turns,dx,dy))
                 if (dx or dy or plan.quarter_turns) and i in items else f.raw
                 for i,f in enumerate(before.fields) if i not in removed)


def rewrite_manual(source,plan,new_id):
    from .rewrite import rewrite_template
    if sha256(source).hexdigest()!=plan.source_sha256:
        raise ValueError('手动微调源文件已变化')
    baseline=rewrite_template(source,plan.base_plan,new_id)
    return b''.join(_kept_fields(decode_template(baseline),plan))+b''.join(_tile_wire(t) for t in plan.additions)


def revise_manual(source,current_plan,new_id,deleted,additions,translation=(0,0),quarter_turns=0):
    """Flatten repeated edits; incoming indices refer to the current artifact."""
    from .rewrite import rewrite_template
    additions=tuple(ManualTile(**t) for t in additions)
    if type(current_plan) is not ManualEditPlan:
        return ManualEditPlan(current_plan,sha256(source).hexdigest(),tuple(deleted),additions,translation,quarter_turns)
    baseline=decode_template(rewrite_template(source,current_plan.base_plan,new_id))
    origins=[i for i in range(len(baseline.tiles)) if i not in current_plan.deleted_indices]
    if any(type(i) is not int or not 0<=i<len(origins)+len(current_plan.additions) for i in deleted):
        raise ValueError('删除牌索引不合法')
    removed=set(current_plan.deleted_indices)|{origins[i] for i in deleted if i<len(origins)}
    dx,dy=translation
    from .translation import transform_point,rotate_vector
    kept_additions=tuple(replace(t,**dict(zip(('x','y'),transform_point(t.x,t.y,quarter_turns,dx,dy))))
                         for i,t in enumerate(current_plan.additions,len(origins)) if i not in deleted)
    total=tuple(a+b for a,b in zip(rotate_vector(*current_plan.translation,quarter_turns),translation))
    return ManualEditPlan(current_plan.base_plan,current_plan.source_sha256,tuple(removed),kept_additions+additions,total,
                          (current_plan.quarter_turns+quarter_turns)%4)


def verify_manual(source,output,plan,new_id):
    from .rewrite import rewrite_template
    from .verification import verify_output,VerificationReport,VerificationIssue,enforce_output_structure
    errors=[]
    facts={}
    try:
        if sha256(source).hexdigest()!=plan.source_sha256:
            raise ValueError('源文件摘要不匹配')
        baseline=rewrite_template(source,plan.base_plan,new_id)
        if type(plan.base_plan) is TowerRemovalPlan:
            # A recovery seed may have non-triple counts or unsupported retained
            # tiles. Prove its exact field whitelist before checking the final draft.
            from .verification import verify_tower_removal_seed
            base=verify_tower_removal_seed(source,baseline,plan.base_plan.source_sha256,new_id)
        else:
            base=verify_output(source,baseline,plan.base_plan,new_id)
        if not base.ok:return base
        before,after=decode_template(baseline),decode_template(output)
        # Validate only retained anchors, after deletions. Additions already
        # carry final coordinates; checking them before the shift would reject
        # valid atomic drafts such as a newly inserted bottom-edge tile.
        from .translation import validate_translation,transform_point
        retained=[t for t in before.tiles if t.index not in plan.deleted_indices]
        if plan.quarter_turns:
            final_points=[transform_point(t.x,t.y,plan.quarter_turns,*plan.translation) for t in retained]
            if any(not 2<=x<=14 or not 2<=y<=16 for x,y in final_points):
                raise ValueError('整体旋转超出安全区域')
        else:
            validate_translation(replace(before,tiles=retained,stacks=[]),*plan.translation)
        # Ordered whitelist: effective X/Y and tower direction may transform;
        # unknowns, earlier duplicate scalars, Tile Z and other values stay exact.
        expected=_kept_fields(before,plan)
        if tuple(f.raw for f in after.fields)!=expected+tuple(_tile_wire(t) for t in plan.additions):
            raise ValueError('输出存在增删白名单以外的字段改动')
        from .verification import verify_manual_transform
        verify_manual_transform(before,after,plan)
        if after.template_id!=new_id:raise ValueError('新模板ID不符')
        counts=tuple(sum(t.tile_id==kind for t in after.tiles) for kind in (-10,-11))
        if not after.tiles or len(after.tiles)%3 or any(n%3 for n in counts):
            errors.append(VerificationIssue('manual_counts','保存时总牌量及A/B数量各自需为3的倍数',counts))
        anchors={(s.x,s.y):s for s in after.stacks}
        if len(anchors)!=len(after.stacks):raise ValueError('塔锚点重复')
        for t in plan.additions:
            if not within_bounds((t.x,t.y),(after.width,after.height)):
                errors.append(VerificationIssue('safe_bounds','新增牌超出合法范围',(t.x,t.y,t.z)))
        by_z={}
        for t in after.tiles:by_z.setdefault(t.z,[]).append(t)
        for z,tiles in by_z.items():
            for i,t in enumerate(tiles):
                if any(overlap_area((t.x,t.y),(p.x,p.y)) for p in tiles[:i]):
                    errors.append(VerificationIssue('same_layer_overlap','同层牌不能重叠',(t.index,z)))
        from .increase_geometry import rectangle,SHIFTS,inside
        for xy,stack in anchors.items():
            members=sorted(t.z for t in after.tiles if (t.x,t.y)==xy)
            if members!=list(range(len(members))) or stack.direction not in range(4):
                errors.append(VerificationIssue('tower_gap','只能增删塔顶；塔内层号需从第1层连续'))
            if any(not inside(rectangle(*xy,len(members),stack.direction,shift),after.width,after.height) for shift in (0,)+SHIFTS):
                errors.append(VerificationIssue('tower_bounds','塔展开超出合法范围'))
        facts={**base.facts,'output_sha256':sha256(output).hexdigest(),
               'tile_counts':dict(total=len(after.tiles),A=counts[0],B=counts[1]),
               'manual_edit':dict(deleted=len(plan.deleted_indices),added=len(plan.additions),
                                  translation=dict(dx=plan.translation[0],dy=plan.translation[1]),
                                  quarter_turns=plan.quarter_turns)}
        return enforce_output_structure(VerificationReport(facts,tuple(errors),()),output,source_bytes=source,plan=plan)
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError) as exc:
        errors.append(VerificationIssue('invalid_manual_edit',str(exc)))
    return VerificationReport(facts,tuple(errors),())
