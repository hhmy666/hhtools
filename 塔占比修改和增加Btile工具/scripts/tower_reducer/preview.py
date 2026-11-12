"""Read-only board facts for the editor-coordinate comparison renderer."""
from .template_reader import decode_template
from .geometry import overlap_area


def comparison(source_bytes, output_bytes=None, *, original_z_offset=0, original_xy_offset=(0, 0), output_origins=None, original_quarter_turns=0):
    """Compare actual edits, optionally normalizing verified non-tower numbering.

    Offsets are caller-owned plan evidence, never inferred from output bytes.
    XY offsets normalize highlights only; both boards retain their real coordinates.
    Tower Z affects its real unfolded position and is always compared directly.
    """
    if type(original_z_offset) is not int or original_z_offset < 0:
        raise ValueError('original_z_offset must be a nonnegative integer')
    if (type(original_xy_offset) not in (tuple, list) or len(original_xy_offset) != 2
            or any(type(value) is not int for value in original_xy_offset)):
        raise ValueError('original_xy_offset must contain two integers')
    dx, dy = original_xy_offset
    if type(original_quarter_turns) is not int or original_quarter_turns not in range(4):
        raise ValueError('invalid original quarter turns')
    from .translation import transform_point
    def original_values(tile):
        return (*transform_point(tile.x,tile.y,original_quarter_turns,dx,dy),
                tile.z+(original_z_offset if (tile.x,tile.y) not in source_anchors else 0),tile.tile_id)
    source = decode_template(source_bytes)
    output = decode_template(output_bytes) if output_bytes is not None else None
    if output and output_origins is None and len(output.tiles) < len(source.tiles):
        raise ValueError('comparison cannot remove original Tile occurrences')
    source_anchors = {(stack.x, stack.y) for stack in source.stacks}
    changed = {a.index for a,b in zip(source.tiles, output.tiles)
               if original_values(a)
                  != (b.x,b.y,b.z,b.tile_id)} if output else set()
    added = set(range(len(source.tiles),len(output.tiles))) if output else set()
    changed |= added
    source_changed=changed
    deleted_count=0
    if output is not None and output_origins is not None:
        if len(output_origins)!=len(output.tiles):raise ValueError('invalid preview origins')
        added={i for i,origin in enumerate(output_origins) if origin is None}
        changed={i for i,origin in enumerate(output_origins) if origin is None or
                 original_values(source.tiles[origin])
                 !=(output.tiles[i].x,output.tiles[i].y,output.tiles[i].z,output.tiles[i].tile_id)}
        survivors={origin for origin in output_origins if origin is not None}
        source_changed={output_origins[i] for i in changed if output_origins[i] is not None}|(set(range(len(source.tiles)))-survivors)
        deleted_count=len(source.tiles)-len(survivors)

    def board(document,is_output=False):
        stack_by_xy = {(s.x,s.y):s.index for s in document.stacks}
        supports = {t.index:[p for p in document.tiles if p.z==t.z-1 and overlap_area((t.x,t.y),(p.x,p.y))]
                    for t in document.tiles}
        return {'template_id': document.template_id,
                'width': document.width, 'height': document.height,
                'tiles': [{'index':t.index, 'x':t.x, 'y':t.y, 'z':t.z,
                           'tile_id':t.tile_id, 'changed':t.index in (changed if is_output else source_changed), 'added':is_output and t.index in added,
                           **({'source_index':output_origins[t.index]} if is_output and output_origins is not None else {}),
                           'support_indices':[p.index for p in supports[t.index]],
                           'support_areas':[overlap_area((t.x,t.y),(p.x,p.y)) for p in supports[t.index]],
                           'tower_index':stack_by_xy.get((t.x,t.y))} for t in document.tiles],
                'towers': [{'index':s.index, 'x':s.x, 'y':s.y, 'direction':s.direction,
                            'count':sum((t.x,t.y)==(s.x,s.y) for t in document.tiles)} for s in document.stacks]}

    return {'source':board(source), 'output':board(output,True) if output else None,'deleted_count':deleted_count,
            'changed_count':len(changed), 'layer_offset':original_z_offset,
            'editor':{'width':17,'height':19,'tile_span':2,'tower_shift':16/85}}
