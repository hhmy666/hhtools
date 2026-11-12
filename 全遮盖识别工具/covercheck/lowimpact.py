"""Independent, source-identity-based limits for low-disturbance machine repair.

These are geometry/change-budget checks, never a claim of equal gameplay data.
Tower footprints may grow; ordinary outline and exposed faces may not change.
"""

from collections import Counter

from .engine import analyze_bytes
from .repair import (_covered, _geometry, _integer, _location, _message,
                     _overlaps, _tile_bytes, _visible_signature, _new_unsupported, rewrite_tiles,
                     validate_board)


MAX_HIDDEN_CHANGES = 3
MAX_TOWER_CHANGES = 6
MAX_COUNT_CHANGE = 3


def _same_position(left, right):
    return all(left[key] == right[key] for key in ('x', 'y', 'z', 'tile_id'))


def _same_union(before, after):
    return (all(_covered(item, after) for item in before)
            and all(_covered(item, before) for item in after))


def _layout_raw(original, tiles):
    fields = [_message(2, original['template_id'].encode('utf-8'))]
    fields.extend(_message(6, _tile_bytes(tile)) for tile in tiles)
    fields.extend(_message(7, b''.join(_integer(n, tower[key])
                                    for n, key in [(1, 'x'), (2, 'y'), (3, 'direction')]))
                  for tower in original['towers'])
    return b''.join(fields)


def _relations(geometry):
    supports, blocks = set(), set()
    names = {id(item): 's' + str(item[0]['index']) if item[0]['index'] is not None
             else 'n' + str(i) for i, item in enumerate(geometry)}
    for lower in geometry:
        for upper in geometry:
            if upper[0]['z'] > lower[0]['z'] and _overlaps(lower, upper):
                edge = names[id(lower)], names[id(upper)]
                blocks.add(edge)
                if upper[0]['z'] == lower[0]['z'] + 1:
                    supports.add(edge)
    return supports, blocks


def _compressed_exposed(original_geometry, retained, problem_ids):
    """Infer only vacated whole-problem-layer renumbering, never arbitrary Z moves.

    Prefix constraints avoid exponential subset enumeration. Empty source gaps
    cannot be selected; their original layer numbers are not collapsed.
    """
    by_z, constraints = {}, {}
    for item in original_geometry:
        tile = item[0]
        by_z.setdefault(tile['z'], []).append(tile)
        if tile['tower_index'] is not None or tile['index'] in problem_ids:
            continue
        if _covered(item, [p for p in original_geometry if p[0]['z'] > tile['z']]):
            continue
        current = retained.get(tile['index'])
        if current is None or any(tile[k] != current[k] for k in ('x', 'y', 'tile_id')):
            continue
        delta = tile['z'] - current['z']
        if delta < 0 or tile['z'] in constraints and constraints[tile['z']] != delta:
            return [], set()
        constraints[tile['z']] = delta
    vacated = [z for z, layer in by_z.items() if all(t['index'] in problem_ids and
               (t['index'] not in retained or not _same_position(t, retained[t['index']])) for t in layer)]
    chosen, previous_z, previous_delta = [], -1, 0
    for z, delta in sorted(constraints.items()):
        eligible = sorted(layer for layer in vacated if previous_z <= layer < z)
        needed = delta - previous_delta
        if needed < 0 or needed > len(eligible):
            return [], set()
        chosen.extend(eligible[:needed])
        previous_z, previous_delta = z, delta
    allowed = {item[0]['index'] for item in original_geometry
               if item[0]['index'] in retained and item[0]['index'] not in problem_ids
               and all(item[0][k] == retained[item[0]['index']][k] for k in ('x', 'y', 'tile_id'))
               and retained[item[0]['index']]['z'] == item[0]['z'] - sum(z < item[0]['z'] for z in chosen)}
    return chosen, allowed if chosen else set()


def audit_layout(original, tiles):
    """Audit candidate tiles with original input indices; None means new tile.

    Counts and identity limits are derived here, not trusted from search results.
    Deleting/recreating a nonproblem tile still consumes its hidden-change limit.
    """
    errors, metrics = [], {}
    if not isinstance(tiles, list) or not tiles:
        return {'valid': False, 'errors': ['修复结果没有有效牌数组'], 'metrics': metrics}
    source = {tile['index']: tile for tile in original['tiles']}
    tower_xy = {}
    for tower in original['towers']:
        xy = tower['x'], tower['y']
        if xy in tower_xy:
            errors.append('塔描述坐标重复：{}'.format(xy))
        tower_xy.setdefault(xy, tower['index'])
    normalized, retained = [], {}
    for tile in tiles:
        if (not isinstance(tile, dict) or any(type(tile.get(k)) is not int
                or not -(1 << 31) <= tile[k] < (1 << 31) for k in ('x', 'y', 'z', 'tile_id'))):
            return {'valid': False, 'errors': ['修复结果坐标或牌型不是int32整数'], 'metrics': metrics}
        index = tile.get('index')
        if index is not None and (type(index) is not int or index not in source or index in retained):
            return {'valid': False, 'errors': ['修复结果牌来源不存在或重复'], 'metrics': metrics}
        copy = dict(tile, index=index, tower_index=tower_xy.get((tile['x'], tile['y'])))
        normalized.append(copy)
        if index is not None:
            retained[index] = copy
            if copy['tile_id'] != source[index]['tile_id']:
                errors.append('不允许改写保留牌的牌型：Tile {}'.format(index))

    original_geometry, geometry = _geometry(original), _geometry({'tiles': normalized, 'towers': original['towers']})
    source_geometry = {item[0]['index']: item for item in original_geometry}
    current_geometry = {item[0]['index']: item for item in geometry if item[0]['index'] is not None}
    problem_ids = {issue[key] for issue in original['issues'] for key in ('lower_index', 'upper_index')}
    upper_ids = {issue['upper_index'] for issue in original['issues']}
    changed = {index for index, tile in source.items()
               if index not in retained or not _same_position(tile, retained[index])}
    compressed_layers, compressed_ids = _compressed_exposed(original_geometry, retained, problem_ids)
    hidden_changed = {index for index in changed if index not in problem_ids and source[index]['tower_index'] is None}
    tower_changed = {index for index in changed if source[index]['tower_index'] is not None
                     or index in retained and retained[index]['tower_index'] is not None}
    tower_new = sum(tile['index'] is None and tile['tower_index'] is not None for tile in normalized)
    before_counts, after_counts = Counter(t['tile_id'] for t in original['tiles']), Counter(t['tile_id'] for t in normalized)
    if len(normalized) % 3:
        errors.append('全部牌共{}张，未按三张成组（含塔牌）'.format(len(normalized)))
    for tile_id, name in ((-10, 'A'), (-11, 'B')):
        if after_counts[tile_id] % 3:
            errors.append('{}牌共{}张，未按三张成组（含塔牌）'.format(name, after_counts[tile_id]))
    for position, left in enumerate(geometry):
        for right in geometry[position + 1:]:
            if left[0]['z'] == right[0]['z'] and _overlaps(left, right):
                errors.append('同层正面积冲突：{} 与 {}'.format(_location(left[0]), _location(right[0])))
    differences = {str(key): after_counts[key] - before_counts[key]
                   for key in before_counts.keys() | after_counts.keys() if after_counts[key] != before_counts[key]}
    distances = [sum(abs(source[i][key] - retained[i][key]) for key in ('x', 'y', 'z'))
                 for i in changed if i in retained]
    metrics.update(hidden_changed_count=len(hidden_changed), tower_changed_count=len(tower_changed) + tower_new,
                   problem_changed_count=len(changed & problem_ids), changed_source_count=len(changed),
                   source_deleted_count=len(source.keys() - retained.keys()),
                   new_tile_count=sum(t['index'] is None for t in normalized),
                   tile_delta=len(tiles) - len(source), type_deltas=differences,
                   displacement_total=sum(distances), displacement_max=max(distances, default=0))
    metrics['compressed_problem_layers'] = [z + 1 for z in compressed_layers]
    if len(hidden_changed) > MAX_HIDDEN_CHANGES:
        errors.append('其他普通牌改动{}张，超过3张限制'.format(len(hidden_changed)))
    if metrics['tower_changed_count'] > MAX_TOWER_CHANGES:
        errors.append('塔牌改动{}张，超过6张限制'.format(metrics['tower_changed_count']))
    if abs(metrics['tile_delta']) > MAX_COUNT_CHANGE or sum(abs(v) for v in differences.values()) > MAX_COUNT_CHANGE:
        errors.append('牌量或逐牌型数量变化超过3张限制')
    for index in hidden_changed:
        item = source_geometry[index]
        if index not in compressed_ids and not _covered(item, [other for other in original_geometry if other[0]['z'] > item[0]['z']]):
            errors.append('不能改动非问题露出牌：Tile {}'.format(index))

    old_heights = Counter(t['tower_index'] for t in original['tiles'] if t['tower_index'] is not None)
    new_heights = Counter(t['tower_index'] for t in normalized if t['tower_index'] is not None)
    for index in range(len(original['towers'])):
        if bool(old_heights[index]) != bool(new_heights[index]):
            errors.append('不能新建或清空原塔：塔 {}'.format(index + 1))
        for other in range(index):
            old_relation = (old_heights[index] > old_heights[other]) - (old_heights[index] < old_heights[other])
            new_relation = (new_heights[index] > new_heights[other]) - (new_heights[index] < new_heights[other])
            if old_relation != new_relation:
                errors.append('塔的等高组或高低关系发生变化')
    metrics['tower_counts_before'] = [old_heights[i] for i in range(len(original['towers']))]
    metrics['tower_counts_after'] = [new_heights[i] for i in range(len(original['towers']))]

    ordinary_before = [item for item in original_geometry if item[0]['tower_index'] is None]
    ordinary_after = [item for item in geometry if item[0]['tower_index'] is None]
    outline_same = _same_union(ordinary_before, ordinary_after)
    metrics['ordinary_outline_unchanged'] = outline_same
    if not outline_same:
        errors.append('非塔区域外轮廓改变（不能只比较包围盒）')

    surface_same = True
    allowed_exposed = set()
    for item in ordinary_before:
        tile, index = item[0], item[0]['index']
        higher = [other for other in original_geometry if other[0]['z'] > tile['z']]
        if _covered(item, higher):
            continue
        wanted = _visible_signature(item, original_geometry)
        current = current_geometry.get(index)
        unchanged_face = (current is not None and (_same_position(tile, current[0]) or index in compressed_ids)
                          and _visible_signature(current, geometry) == wanted)
        if unchanged_face:
            allowed_exposed.add(index)
            continue
        # Only source full-cover uppers may yield to the same XY/type face.
        replacements = []
        if index in upper_ids:
            replacements = [candidate for candidate in geometry
                            if candidate[0]['index'] in source
                            and (candidate[0]['index'] == index or source[candidate[0]['index']]['z'] < tile['z'])
                            and all(candidate[0][k] == tile[k] for k in ('x', 'y', 'tile_id'))
                            and candidate[0]['tower_index'] is None
                            and _visible_signature(candidate, geometry) == wanted]
        if replacements:
            allowed_exposed.update(p[0]['index'] for p in replacements)
        else:
            surface_same = False
            errors.append('原露出牌面或表面类型改变：{}'.format(_location(tile)))
    for item in ordinary_after:
        index = item[0]['index']
        # Unchanged originally-hidden tiles must stay hidden too: shrinking a
        # tower or moving another blocker must not reveal a new ordinary face.
        if index not in allowed_exposed and not _covered(item, [p for p in geometry if p[0]['z'] > item[0]['z']]):
            surface_same = False
            errors.append('原表面之外出现普通牌露出面：{}'.format(_location(item[0])))
    metrics['ordinary_surface_unchanged'] = surface_same

    try:
        checked = validate_board(_layout_raw(original, normalized))
        errors.extend(checked['errors'])
        if checked['analysis'] and checked['analysis']['issues']:
            errors.append('修复后仍有全遮盖')
    except (ValueError, TypeError, OverflowError) as error:
        errors.append('修复结果不能解析：{}'.format(error))
    for tile in _new_unsupported(geometry, original):
        errors.append('新增悬空：{}没有紧邻下层承接'.format(_location(tile)))
    old_support, old_blocks = _relations(original_geometry)
    new_support, new_blocks = _relations(geometry)
    metrics.update(support_edges_added=len(new_support - old_support), support_edges_removed=len(old_support - new_support),
                   blocking_edges_added=len(new_blocks - old_blocks), blocking_edges_removed=len(old_blocks - new_blocks))
    return {'valid': not errors, 'errors': list(dict.fromkeys(errors)), 'metrics': metrics}


def audit_repair(original_raw, candidate_raw, origin_indices):
    """Independently audit final wire bytes and caller-provided source mapping."""
    try:
        original, candidate = analyze_bytes(original_raw), analyze_bytes(candidate_raw)
        if not isinstance(origin_indices, list) or len(origin_indices) != len(candidate['tiles']):
            raise ValueError('修复结果牌来源映射不完整')
        tiles = [dict(tile, index=origin) for tile, origin in zip(candidate['tiles'], origin_indices)]
        result = audit_layout(original, tiles)
        if candidate_raw != rewrite_tiles(original_raw, tiles):
            result['errors'].append('修复结果未知字段、塔描述或来源记录被改写')
            result['valid'] = False
        return result
    except (ValueError, TypeError, KeyError, IndexError, OverflowError) as error:
        return {'valid': False, 'errors': ['修复结果来源核验失败：{}'.format(error)], 'metrics': {}}
