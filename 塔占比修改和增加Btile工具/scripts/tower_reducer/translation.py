"""Verified whole-output XY revisions, relative to the initial generated plan."""
from dataclasses import dataclass
from hashlib import sha256
import math
import re

from .increase_geometry import rectangle, SHIFTS
from .protobuf_wire import encode_int32, encode_varint, replace_length_delimited
from .template_reader import decode_template


@dataclass(frozen=True)
class TranslationPlan:
    base_plan: object
    source_sha256: str
    dx: int
    dy: int

    def __post_init__(self):
        from .rewrite import RewritePlan
        from .increase import IncreasePlan
        from .ordinary_add import OrdinaryAddPlan
        from .manual_edit import ManualEditPlan, initial_plan
        if type(self.base_plan) not in (RewritePlan, IncreasePlan, OrdinaryAddPlan, ManualEditPlan):
            raise ValueError('translation requires an initial typed plan')
        initial_plan(self)
        if type(self.source_sha256) is not str or not re.fullmatch('[0-9a-f]{64}', self.source_sha256):
            raise ValueError('translation requires source SHA256')
        if any(type(value) is not int or not -32 <= value <= 32 for value in (self.dx, self.dy)):
            raise ValueError('translation offsets must be integers from -32 to 32')


def translation_bounds(document):
    if type(document.width) is not int or type(document.height) is not int:
        raise ValueError('translation requires board dimensions')
    min_dx = min_dy = -32
    max_dx = max_dy = 32
    for item in (*document.tiles, *document.stacks):
        # Historical source anchors may already occupy the display margins.
        # Preserve those margins without allowing their overflow to worsen;
        # otherwise opposite margin tiles produce an empty interval and block
        # even a valid movement along the other axis (e.g. xkd068 X=1/15).
        # Physical board bounds and full tower projections remain strict.
        min_dx = max(min_dx, min(0, 2 - item.x), -item.x)
        max_dx = min(max_dx, max(0, 14 - item.x), document.width - 2 - item.x)
        min_dy = max(min_dy, min(0, 2 - item.y), -item.y)
        max_dy = min(max_dy, max(0, 16 - item.y), document.height - 2 - item.y)
    for stack in document.stacks:
        count = sum((tile.x, tile.y) == (stack.x, stack.y) for tile in document.tiles)
        if not count or stack.direction not in range(4):
            raise ValueError('invalid tower projection')
        for shift in (0,) + SHIFTS:
            left, top, right, bottom = rectangle(stack.x, stack.y, count, stack.direction, shift)
            min_dx = max(min_dx, math.ceil(-left - 1e-9))
            max_dx = min(max_dx, math.floor(document.width - right + 1e-9))
            min_dy = max(min_dy, math.ceil(-top - 1e-9))
            max_dy = min(max_dy, math.floor(document.height - bottom + 1e-9))
    return dict(min_dx=min_dx, max_dx=max_dx, min_dy=min_dy, max_dy=max_dy)


def validate_translation(document, dx, dy):
    bounds = translation_bounds(document)
    if not bounds['min_dx'] <= dx <= bounds['max_dx'] or not bounds['min_dy'] <= dy <= bounds['max_dy']:
        raise ValueError('translation_out_of_bounds')
    return bounds


def center_translation(document, *, baseline=None,current=(0,0)):
    """Nearest legal integer shift of the actual 18×20 editor footprint.

    Coordinates render as 2×2 Tiles; declared towers fan out by 16/85 per Z.
    Safety still uses all existing editor/client tower-envelope constraints.
    ``current`` is the absolute TranslationPlan offset from ``baseline``.
    """
    if not document.tiles:raise ValueError('空模板无法居中')
    bounds=translation_bounds(baseline if baseline is not None else document)
    towers={(s.x,s.y):s for s in document.stacks}
    points=[]
    for tile in document.tiles:
        tower=towers.get((tile.x,tile.y))
        vx,vy=((0,-1),(-1,0),(0,1),(1,0))[tower.direction] if tower is not None else (0,0)
        points.append((tile.x+vx*SHIFTS[0]*tile.z,tile.y+vy*SHIFTS[0]*tile.z))
    box={'left':min(x for x,y in points),'top':min(y for x,y in points),
         'right':max(x for x,y in points)+2,'bottom':max(y for x,y in points)+2}
    center=((box['left']+box['right'])/2,(box['top']+box['bottom'])/2)
    target=(9.0,10.0)
    absolute=[];clamped=False
    for axis,position,wanted,previous in zip(('dx','dy'),center,target,current):
        lo,hi=bounds['min_'+axis],bounds['max_'+axis]
        if lo>hi:raise ValueError('当前模板没有合法的整体平移范围')
        ideal=previous+wanted-position
        chosen=min(range(lo,hi+1),key=lambda value:(round(abs(value-ideal),12),abs(value-previous),value))
        unconstrained=min(abs(math.floor(ideal)-ideal),abs(math.ceil(ideal)-ideal))
        clamped=clamped or abs(chosen-ideal)>unconstrained+1e-9
        absolute.append(chosen)
    dx,dy=tuple(value-old for value,old in zip(absolute,current))
    return {'dx':dx,'dy':dy,'absolute_dx':absolute[0],'absolute_dy':absolute[1],
            'target':{'x':target[0],'y':target[1]},'before':box,
            'after':{name:value+(dx if name in ('left','right') else dy) for name,value in box.items()},
            'residual':{'x':round(center[0]+dx-target[0],12),'y':round(center[1]+dy-target[1],12)},
            'clamped':clamped}


def _shift_payload(item, dx, dy):
    last = {field.number: index for index, field in enumerate(item.fields) if field.number in (1, 2)}
    wanted = {1: (item.x, dx), 2: (item.y, dy)}
    parts = []
    for index, field in enumerate(item.fields):
        if field.number in wanted and index == last[field.number] and wanted[field.number][1]:
            coordinate, delta = wanted[field.number]
            parts.append(field.tag_raw + encode_int32(coordinate + delta))
        else:
            parts.append(field.raw)
    return b''.join(parts)


def rotate_vector(x, y, quarter_turns):
    for _ in range(quarter_turns % 4):
        x, y = -y, x
    return x, y


def transform_point(x, y, quarter_turns=0, dx=0, dy=0):
    """Rotate 2×2 Tile anchors around the fixed 18×20 editor canvas."""
    x, y = rotate_vector(x - 8, y - 9, quarter_turns)
    return x + 8 + dx, y + 9 + dy


def _transform_payload(item, quarter_turns, dx, dy):
    """Change effective coordinates/direction only, preserving duplicate fields."""
    x, y = transform_point(item.x, item.y, quarter_turns, dx, dy)
    wanted = {1: (item.x, x), 2: (item.y, y)}
    if hasattr(item, 'direction'):
        wanted[3] = (item.direction, (item.direction - quarter_turns) % 4)
    last = {f.number: i for i, f in enumerate(item.fields) if f.number in wanted}
    parts = []
    for i, field in enumerate(item.fields):
        change = wanted.get(field.number)
        parts.append(field.tag_raw + encode_int32(change[1])
                     if change and change[0] != change[1] and i == last[field.number] else field.raw)
    for number, (old, new) in wanted.items():
        if number not in last and old != new:
            parts.append(encode_varint(number << 3) + encode_int32(new))
    return b''.join(parts)


def translate_bytes(raw, dx, dy):
    document = decode_template(raw)
    validate_translation(document, dx, dy)
    items = {item.top_field_index: item for item in (*document.tiles, *document.stacks)}
    return b''.join(replace_length_delimited(field, _shift_payload(items[index], dx, dy))
                    if index in items and (dx or dy) else field.raw
                    for index, field in enumerate(document.fields))


def rewrite_translation(source, plan, new_id):
    from .rewrite import rewrite_template
    if sha256(source).hexdigest() != plan.source_sha256:
        raise ValueError('translation source SHA mismatch')
    return translate_bytes(rewrite_template(source, plan.base_plan, new_id), plan.dx, plan.dy)


def verify_translation(source, output, plan, new_id):
    """Check original engine evidence and the complete translated wire envelope."""
    from .rewrite import rewrite_template
    from .verification import verify_output, VerificationIssue, VerificationReport
    errors = []
    facts = {'translation': {'dx': plan.dx, 'dy': plan.dy}}
    try:
        if sha256(source).hexdigest() != plan.source_sha256:
            raise ValueError('source SHA mismatch')
        baseline = rewrite_template(source, plan.base_plan, new_id)
        report = verify_output(source, baseline, plan.base_plan, new_id)
        if not report.ok:
            return report
        before, after = decode_template(baseline), decode_template(output)
        validate_translation(before, plan.dx, plan.dy)
        if len(before.fields) != len(after.fields):
            raise ValueError('top-level field count changed')
        items = {item.top_field_index: item for item in (*before.tiles, *before.stacks)}
        for index, (left, right) in enumerate(zip(before.fields, after.fields)):
            item = items.get(index)
            if item is None or not (plan.dx or plan.dy):
                if left.raw != right.raw:
                    raise ValueError('unrelated wire field changed')
                continue
            # Independent whitelist: only the effective X/Y varint may change;
            # duplicate fields, Z, type, direction and unknown bytes stay exact.
            from .protobuf_wire import parse_fields, decode_int32
            fields = parse_fields(right.payload)
            if (left.tag_raw != right.tag_raw or len(item.fields) != len(fields)
                    or right.raw != replace_length_delimited(left, right.payload)):
                raise ValueError('coordinate envelope changed')
            last = {field.number: i for i, field in enumerate(item.fields) if field.number in (1, 2)}
            for i, (old, new) in enumerate(zip(item.fields, fields)):
                delta = plan.dx if old.number == 1 else plan.dy if old.number == 2 else 0
                if old.number in last and last[old.number] == i and delta:
                    if (old.tag_raw != new.tag_raw or new.wire_type != 0
                            or new.raw != old.tag_raw + encode_int32(decode_int32(old.value) + delta)):
                        raise ValueError('nonuniform translation')
                elif old.raw != new.raw:
                    raise ValueError('non-coordinate wire bytes changed')
        facts.update(report.facts)
        facts['output_sha256'] = sha256(output).hexdigest()
        facts['translation'] = {'dx': plan.dx, 'dy': plan.dy}
        def shifted(point):
            return (point[0] + plan.dx, point[1] + plan.dy, *point[2:])
        if 'moves' in facts:
            facts['moves'] = tuple({**move, 'to': shifted(move['to'])} for move in facts['moves'])
        if 'overlaps' in facts:
            facts['overlaps'] = tuple((index, z, tuple((shifted(point), area) for point, area in neighbours))
                                      for index, z, neighbours in facts['overlaps'])
        if 'symmetry_axes' in facts:
            axes = facts['symmetry_axes']
            facts['source_symmetry_axes'] = axes
            # Axes are stored as min+max coordinates, twice their grid position.
            facts['symmetry_axes'] = (axes[0] + 2 * plan.dx, axes[1] + 2 * plan.dy)
        if 'geometry' in facts:
            facts['geometry'] = {name: [[value + (plan.dx if i % 2 == 0 else plan.dy)
                                        for i, value in enumerate(rect)] for rect in rectangles]
                                 for name, rectangles in facts['geometry'].items()}
        return VerificationReport(facts, (), report.warnings)
    except (ValueError, TypeError, OverflowError, AttributeError) as exc:
        errors.append(VerificationIssue('invalid_translation', str(exc)))
    return VerificationReport(facts, tuple(errors), ())
