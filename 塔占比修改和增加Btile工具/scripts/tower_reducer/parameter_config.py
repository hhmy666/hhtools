"""Calculate DupPara changes and append reviewed rows to a local CSV only.

The source is the selected machine's config, not a claim about online values.
Calculation and preview never modify it. Durable plans bind both CSV digests
and directory identities; commits preserve the complete original byte prefix.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import csv
import difflib
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import unicodedata
import zipfile
from xml.sax.saxutils import escape

from .naming import allocate_template_id


DUP_COLUMNS = tuple(f'DupPara{i}' for i in range(1, 11))
_COMMIT_LOCK = threading.RLock()
_INTEGER = re.compile(r'[+-]?\d+\Z')
_ID = re.compile(r'(?:calc|plan)-[a-f0-9]{32}\Z')


def _digest(raw):
    return sha256(raw).hexdigest()


def _absolute(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('目录必须是无路径穿越的绝对路径')
    return path


@contextmanager
def _directory(value, create=False):
    """Open each component without following links, retaining the final fd."""
    path = _absolute(value)
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                info = os.stat(part, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    raise ValueError(f'目录不存在：{path}')
                os.mkdir(part, 0o700, dir_fd=fd)
                info = os.stat(part, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError(f'拒绝符号链接或非真实目录：{path}')
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            if _identity(child) != [info.st_dev, info.st_ino]:
                os.close(child)
                raise ValueError('打开期间目录身份变化')
            os.close(fd)
            fd = child
        yield fd
    except OSError as exc:
        raise ValueError(f'目录访问失败：{path}（{exc}）') from exc
    finally:
        os.close(fd)


def _identity(fd):
    info = os.fstat(fd)
    return [info.st_dev, info.st_ino]


def _read_file(fd, name):
    try:
        child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        with os.fdopen(child, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f'{name} 不是普通文件')
            if info.st_size > 64 * 1024 * 1024:
                raise ValueError(f'{name} 超过读取大小限制')
            return stream.read(), info
    except OSError as exc:
        raise ValueError(f'拒绝符号链接或无法读取 {name}（{exc}）') from exc


def _table(raw, name, required):
    try:
        rows = list(csv.reader(io.StringIO(raw.decode('utf-8-sig'), newline=''), strict=True))
    except (UnicodeError, csv.Error) as exc:
        raise ValueError(f'{name} 不是合法 UTF-8 CSV：{exc}') from exc
    if len(rows) < 3:
        raise ValueError(f'{name} 缺少字段、类型、描述三行表头')
    fields = rows[0]
    if len(set(fields)) != len(fields):
        raise ValueError(f'{name} 有重复字段名')
    missing = set(required).difference(fields)
    if missing:
        raise ValueError(f'{name} 缺少字段：' + '、'.join(sorted(missing)))
    data = []
    for line, row in enumerate(rows[3:], 4):
        if not row or not any(row):
            continue
        if len(row) > len(fields):
            raise ValueError(f'{name} 第{line}行列数超过表头')
        data.append(row + [''] * (len(fields) - len(row)))
    return fields, data


def _snapshot(config_root):
    selected = _absolute(config_root)
    with _directory(selected) as root_fd:
        root_identity = _identity(root_fd)
        try:
            os.stat('config_level.csv', dir_fd=root_fd, follow_symlinks=False)
            directory = selected
        except FileNotFoundError:
            directory = selected / 'csv'
    with _directory(directory) as fd:
        raw, info = _read_file(fd, 'config_level.csv')
        try:
            os.stat('config_level_list.csv', dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            list_raw = None
        else:
            list_raw, _ = _read_file(fd, 'config_level_list.csv')
        identity = _identity(fd)
    fields, data = _table(raw, 'config_level.csv', ('LevelId', 'TemplateId', *DUP_COLUMNS))
    list_fields, list_data = ([], []) if list_raw is None else _table(
        list_raw, 'config_level_list.csv', ('LevelIndex', 'LevelId'))
    return {
        'selected_root': str(selected), 'root_identity': root_identity,
        'directory': str(directory), 'directory_identity': identity,
        'config_path': str(directory / 'config_level.csv'),
        'config_sha256': _digest(raw), 'list_sha256': None if list_raw is None else _digest(list_raw),
        'mode': stat.S_IMODE(info.st_mode), 'raw': raw,
        'fields': fields, 'data': data, 'list_fields': list_fields, 'list_data': list_data,
    }


def _binding(snapshot):
    return {key: snapshot[key] for key in ('selected_root', 'root_identity', 'directory',
            'directory_identity', 'config_path', 'config_sha256', 'list_sha256', 'mode')}


def _check_binding(current, expected, *, check_hash=True):
    for key in ('selected_root', 'root_identity', 'directory', 'directory_identity'):
        if current[key] != expected[key]:
            raise ValueError('所选配置目录或目录身份已变化，请重新计算和预览')
    if current['list_sha256'] != expected['list_sha256']:
        raise ValueError('config_level_list.csv 已变化，请重新计算和预览')
    if check_hash and current['config_sha256'] != expected['config_sha256']:
        raise ValueError('config_level.csv 整文件摘要已变化，请重新计算和预览')


def _indexes(snapshot, start, end):
    if not snapshot['list_fields']:
        if start is not None or end is not None:
            raise ValueError('缺少 config_level_list.csv，请补齐关卡索引表或清空关卡范围')
        return {}
    columns = {name: i for i, name in enumerate(snapshot['list_fields'])}
    result = {}
    for row in snapshot['list_data']:
        try:
            index = int(row[columns['LevelIndex']])
        except ValueError:
            raise ValueError('config_level_list.csv 存在非法 LevelIndex')
        if (start is not None and index < start) or (end is not None and index > end):
            continue
        dda = row[columns['DDAType']].strip() if 'DDAType' in columns else '0'
        names = ('DDALevelId1', 'DDALevelId2', 'DDALevelId3') if dda == '1' else ('LevelId',)
        if any(name not in columns for name in names):
            raise ValueError('DDAType=1 的关卡索引缺少 DDALevelId1–3')
        for name in names:
            for level_id in re.split(r'[|;\s]+', row[columns[name]].strip()):
                if level_id:
                    result.setdefault(level_id, set()).add(index)
    return {key: sorted(value) for key, value in result.items()}


def _counts(value, label):
    if not isinstance(value, dict):
        raise ValueError(f'{label} Tile 数缺失')
    result = {}
    for side in ('A', 'B'):
        count = value.get(side)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f'{label} {side} Tile 数必须是非负整数')
        if count % 3:
            raise ValueError(f'{label} {side} Tile 数不是3的倍数')
        result[side] = count
    return result


def _kind_check(kinds, counts, label, unchanged=()):
    for side, value in zip(('A', 'B'), kinds):
        if side in unchanged:
            continue
        if value < 0:
            raise ValueError(f'{label} {side} 种类数为负数')
        if counts[side] > 0 and value == 0:
            raise ValueError(f'{label} 非空 {side} 侧种类数不能为0')
        if value > counts[side] // 3:
            raise ValueError(f'{label} {side} 种类数超过侧组容量 {counts[side] // 3}')


def _parameters(original, source, result, operation='reduce', *, ordinary_add=False):
    source = _counts(source, '原')
    result = _counts(result, '新')
    if operation not in ('reduce', 'increase', 'manual'):
        raise ValueError('模板操作类型不合法')
    if operation == 'increase':
        if any(result[side] < source[side] for side in ('A', 'B')) or sum(result.values()) <= sum(source.values()):
            raise ValueError('增加模式只允许追加 A/B Tile，且新总数应大于原总数')
    elif operation == 'reduce' and sum(source.values()) != sum(result.values()):
        raise ValueError('原、新 Tile 总数不守恒')
    parsed = []
    for index, value in enumerate(original, 1):
        pieces = value.split('|')
        if len(pieces) != 4 or any(not _INTEGER.fullmatch(piece) for piece in pieces):
            raise ValueError(f'DupPara{index} 应含四个以 | 分隔的整数')
        kinds = (int(pieces[0]), int(pieces[3]))
        parsed.append((kinds, pieces))
    if source == result:
        # Existing configs may retain kinds for an inactive side or repeated
        # patterns. A same-type move is an exact copy, not a config migration.
        return list(original), {'A': 0, 'B': 0}
    first = parsed[0][0]
    adjusted = []
    unchanged = {side for side in ('A', 'B') if source[side] == result[side]}
    for side, kinds in zip(('A', 'B'), first):
        if side in unchanged:
            adjusted.append(kinds)
        elif source[side] == 0:
            if result[side] > 0:
                if side == 'B':
                    if result['A'] <= 0 or adjusted[0] <= 0:
                        raise ValueError('新A缺少有效的每种图案组数基准，无法计算新增B种类数')
                    if result['B'] < 9:
                        raise ValueError('新增B牌数不足：B参数最低为3，至少需要9张B牌')
                    # gA = (new A / 3) / new A kinds. Calculate
                    # ceil((new B / 3) / gA) - 1 exactly, with floor 3.
                    numerator = result['B'] * adjusted[0]
                    adjusted.append(max(3, (numerator + result['A'] - 1) // result['A'] - 1))
                    continue
                raise ValueError(f'原 {side} 侧 Tile 数为0，新侧非空，无法按比例计算种类数')
            adjusted.append(0)
        else:
            # Exact rational half-up: floor(n/d + 1/2), no binary floating point.
            adjusted.append((2 * kinds * result[side] + source[side]) // (2 * source[side]))
    delta = {'A': adjusted[0] - first[0], 'B': adjusted[1] - first[1]}
    third_b = parsed[min(2, len(parsed) - 1)][0][1] + delta['B']
    if source['B'] == 0 and result['B'] > 0:
        third_b = max(3, third_b)
    output = []
    for index, (kinds, pieces) in enumerate(parsed, 1):
        new_kinds = (kinds[0] + delta['A'], kinds[1] + delta['B'])
        if source['B'] == 0 and result['B'] > 0:
            new_kinds = (new_kinds[0], max(3, new_kinds[1]))
        if 'B' not in unchanged and result['B'] > 0 and index >= 4:
            # Starting with duplicate 4, each pair reduces the duplicate-3
            # baseline once. Never raise an already lower computed B value.
            ceiling = max(3, third_b - (index - 2) // 2)
            new_kinds = (new_kinds[0], min(new_kinds[1], ceiling))
        _kind_check(new_kinds, result, f'新 DupPara{index}', unchanged)
        output.append('|'.join((pieces[0] if delta['A'] == 0 else str(new_kinds[0]),
            pieces[1], pieces[2], pieces[3] if new_kinds[1] == kinds[1] else str(new_kinds[1]))))
    return output, delta


def _level_id(rule, original, template):
    if not isinstance(rule, str) or not rule:
        raise ValueError('关卡ID规则不能为空')
    remaining = rule.replace('{原关卡ID}', '').replace('{新模板ID}', '')
    if '{' in remaining or '}' in remaining:
        raise ValueError('关卡ID规则只支持 {原关卡ID} 和 {新模板ID}')
    result = rule.replace('{原关卡ID}', original).replace('{新模板ID}', template)
    if not result or len(result) > 180 or any(c in result for c in '\r\n\0'):
        raise ValueError('规则生成了非法新关卡ID')
    return result


def _default_level_rule(row):
    if row.get('template_mode') == 'ordinary_add':
        return 'moreB{原关卡ID}'
    return 'moreT{原关卡ID}' if row.get('operation') == 'increase' else 'lessT{原关卡ID}'


def _new_level_id(row, rule, preserve_level_prefix, occupied):
    if preserve_level_prefix:
        original, template = row['original_level_id'], row['original_template_id']
        if template and original.endswith(template):
            prefix = original[:-len(template)]
        else:
            # A level can retain an older template version in its name after
            # its TemplateId changes. Only the established list/r/v prefix is
            # recognizable without guessing a custom level naming scheme.
            match = re.match(r'list\d+(?:r\d+)?(?:v\d+)*', original)
            prefix = match.group(0) if match else ''
            tail = original[len(prefix):]
            if not prefix or not re.fullmatch(r'[A-Za-z0-9_-]{1,180}', tail):
                raise ValueError('原关卡ID未以原模板ID结尾，且不含可识别的list关卡前缀与合法模板名，无法可靠提取前缀')
        base = prefix + row['new_template_id']
        return allocate_template_id(base, occupied)
    return _level_id(_default_level_rule(row) if rule is None else rule,
        row['original_level_id'], row['new_template_id'])


def _summary(rows):
    statuses = ('ready', 'unchanged', 'no_tower', 'failed', 'append', 'skip', 'blocked', 'excluded')
    return {'row_count': len(rows), **{f'{status}_count': sum(r['status'] == status for r in rows)
            for status in statuses}, 'changed_count': sum(bool(r.get('new_parameters')) and
            r['original_parameters'] != r['new_parameters'] for r in rows)}


def _addition(raw, fields, rows):
    newline = '\r\n' if b'\r\n' in raw else ('\r' if b'\r' in raw and b'\n' not in raw else '\n')
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator=newline)
    writer.writerows(rows)
    separator = b'' if not rows or not raw or raw.endswith((b'\r', b'\n')) else newline.encode()
    return separator + stream.getvalue().encode('utf-8')


def _diff(path, raw, addition):
    return ''.join(difflib.unified_diff(raw.decode('utf-8-sig').splitlines(keepends=True),
        (raw + addition).decode('utf-8-sig').splitlines(keepends=True),
        fromfile=path, tofile=path + '（追加预览）'))


class ParameterConfigService:
    def __init__(self, runtime_root):
        self.runtime_root = _absolute(runtime_root)
        with _directory(self.runtime_root):
            pass
        self.state_root = self.runtime_root / 'parameter_config'

    def _save(self, identity, value):
        if not _ID.fullmatch(identity):
            raise ValueError('非法计算或计划ID')
        payload = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        with _directory(self.state_root, create=True) as fd:
            name = '.' + identity + '-' + secrets.token_hex(8) + '.tmp'
            child = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            try:
                with os.fdopen(child, 'wb') as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, identity + '.json', src_dir_fd=fd, dst_dir_fd=fd)
                os.fsync(fd)
            finally:
                try:
                    os.unlink(name, dir_fd=fd)
                except FileNotFoundError:
                    pass

    def _load(self, identity, prefix):
        if not isinstance(identity, str) or not _ID.fullmatch(identity) or not identity.startswith(prefix + '-'):
            raise ValueError('非法计算或计划ID')
        with _directory(self.state_root) as fd:
            raw, _ = _read_file(fd, identity + '.json')
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise ValueError('计算或计划记录损坏') from exc

    def calculate(self, config_root, entries, *, level_start=None, level_end=None,
                  level_id_rule=None, description=None, preserve_level_prefix=False):
        if type(preserve_level_prefix) is not bool:
            raise ValueError('保留原关卡ID前缀必须是布尔值')
        if description is not None and not isinstance(description, str):
            raise ValueError('备注必须是字符串或 null')
        for value in (level_start, level_end):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError('关卡范围必须是非负整数')
        if level_start is not None and level_end is not None and level_start > level_end:
            raise ValueError('关卡范围起点不能大于终点')
        if not isinstance(entries, (list, tuple)):
            raise ValueError('模板结果必须是数组')
        snapshot = _snapshot(config_root)
        fields = snapshot['fields']
        columns = {name: i for i, name in enumerate(fields)}
        positions = _indexes(snapshot, level_start, level_end)
        by_template, by_level = {}, {}
        for data in snapshot['data']:
            by_template.setdefault(data[columns['TemplateId']], []).append(data)
            by_level.setdefault(data[columns['LevelId']], []).append(data)
        rows, sources, comparisons = [], {}, []
        occupied = set(by_level)
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError('模板结果行必须是对象')
            original_id = str(item.get('original_template_id') or '')
            new_id = str(item.get('new_template_id') or '')
            matches = by_template.get(original_id, [])
            for source in matches or [None]:
                level_id = source[columns['LevelId']] if source is not None else ''
                indexes = positions.get(level_id, [])
                if source is not None and (level_start is not None or level_end is not None) and not indexes:
                    continue
                row = {'row_id': 'row-' + secrets.token_hex(16), 'level_indexes': indexes,
                    # Entries are built by ParameterFlow after server-side proof;
                    # the HTTP API never accepts entries or an operation override.
                    'operation': item.get('operation', 'reduce'),
                    'template_mode': item.get('template_mode', item.get('operation', 'reduce')),
                    'original_level_id': level_id, 'original_template_id': original_id,
                    'new_template_id': new_id, 'original_parameters': [] if source is None else
                    [source[columns[name]] for name in DUP_COLUMNS], 'new_parameters': [],
                    'source_tile_counts': deepcopy(item.get('source_tile_counts')),
                    'result_tile_counts': deepcopy(item.get('result_tile_counts')),
                    'delta': {'A': None, 'B': None}, 'status': 'failed', 'message': '',
                    'new_level_id': '', 'description': description if description is not None else
                    (source[columns['Des']] if source is not None and 'Des' in columns else ''),
                    **{name: source[columns[name]] if source is not None and name in columns else None
                       for name in ('RandomType', 'StrategyGroupId', 'randomSeed_groupID')}}
                for name in ('source_sha256', 'artifact_sha256'):
                    if name in item:
                        row[name] = item[name]
                if item.get('status') == 'no_tower' and original_id:
                    row['status'], row['message'] = 'no_tower', '无塔模板，无需追加关卡配置'
                    rows.append(row)
                    continue
                try:
                    if not original_id or not new_id:
                        raise ValueError('缺少原模板ID或新模板ID')
                    if source is None:
                        raise ValueError(f'本机 config_level.csv 未找到模板 {original_id} 的关卡配置')
                    if not level_id.strip():
                        raise ValueError('原关卡 LevelId 为空，无法建立可靠映射')
                    sources[row['row_id']] = source
                    if len(by_level[level_id]) != 1:
                        raise ValueError(f'原 LevelId {level_id} 重复，未自动选择配置')
                    row['new_level_id'] = _new_level_id(row, level_id_rule, preserve_level_prefix, occupied)
                    status = item.get('status')
                    if status != 'verified':
                        raise ValueError(item.get('message') or '模板转换未通过验收')
                    else:
                        row['new_parameters'], row['delta'] = _parameters(row['original_parameters'],
                            row['source_tile_counts'], row['result_tile_counts'], row['operation'],
                            ordinary_add=row.get('template_mode') == 'ordinary_add')
                        row['original_total'] = sum(row['source_tile_counts'][side] for side in ('A', 'B'))
                        row['final_total'] = sum(row['result_tile_counts'][side] for side in ('A', 'B'))
                        row['added_tiles'] = row['final_total'] - row['original_total']
                        row['status'] = 'ready' if row['new_parameters'] != row['original_parameters'] else 'unchanged'
                        if preserve_level_prefix:
                            occupied.add(row['new_level_id'])
                        row['message'] = ('参数计算完成' if row['status'] == 'ready' else
                            ('A/B 数量不变，10个副本参数原样复制'
                             if row['source_tile_counts'] == row['result_tile_counts'] else
                             '按比例取整后种类数不变，10个副本参数原样复制'))
                        if row['source_tile_counts']['B'] == 0 and row['result_tile_counts']['B'] > 0:
                            row['parameter_basis'] = {'B':'new_A_groups_per_pattern_ceiling_minus_one_min_three'}
                            row['message'] += '；原B为空，按新A每种图案组数计算新增B首副本种类数：向上取整后减1，最低3'
                        if row['source_tile_counts']['B'] != row['result_tile_counts']['B'] and row['result_tile_counts']['B'] > 0:
                            row['message'] += '；B从第4副本起每两个副本减1，最低3，已低于3的不抬高'
                except ValueError as exc:
                    row['status'], row['message'], row['new_parameters'] = 'failed', str(exc), []
                rows.append(row)
                if row['status'] == 'ready':
                    for version, parameters in (('原参数', row['original_parameters']), ('新参数', row['new_parameters'])):
                        comparisons.append({'row_id': row['row_id'], 'level_indexes': indexes,
                            'original_level_id': level_id, 'new_level_id': row['new_level_id'],
                            'original_template_id': original_id, 'new_template_id': new_id,
                            'version': version, 'parameters': parameters,
                            **dict(zip(DUP_COLUMNS, parameters))})
        identity = 'calc-' + secrets.token_hex(16)
        result = {'calculation_id': identity, 'rows': rows, 'comparisons': comparisons,
            'summary': _summary(rows), 'config_path': snapshot['config_path'],
            'config_sha256': snapshot['config_sha256'], 'preserve_level_prefix': preserve_level_prefix}
        self._save(identity, {'result': result, 'binding': _binding(snapshot), 'sources': sources})
        return deepcopy(result)

    def preview(self, calculation_id, selected_row_ids, *, config_root,
                level_id_rule=None, description=None, preserve_level_prefix=None):
        if preserve_level_prefix is not None and type(preserve_level_prefix) is not bool:
            raise ValueError('保留原关卡ID前缀必须是布尔值')
        if description is not None and not isinstance(description, str):
            raise ValueError('备注必须是字符串或 null')
        if not isinstance(selected_row_ids, (list, tuple)) or any(not isinstance(r, str) for r in selected_row_ids):
            raise ValueError('所选关卡行必须是ID数组')
        calculation = self._load(calculation_id, 'calc')
        if preserve_level_prefix is None:
            preserve_level_prefix = calculation['result'].get('preserve_level_prefix', False)
        snapshot = _snapshot(config_root)
        _check_binding(snapshot, calculation['binding'])
        selected = set(selected_row_ids)
        available = {row['row_id']: row for row in calculation['result']['rows']}
        if selected.difference(available):
            raise ValueError('选择中包含未知关卡行')
        columns = {name: i for i, name in enumerate(snapshot['fields'])}
        existing = {}
        for row in snapshot['data']:
            existing.setdefault(row[columns['LevelId']], []).append(row)
        occupied = set(existing)
        rows, candidates = [], {}
        for original in calculation['result']['rows']:
            if original['row_id'] not in selected:
                continue
            row = deepcopy(original)
            if row['status'] not in ('ready', 'unchanged'):
                row['status'] = 'excluded'
                row['message'] = '未选择写入：' + row['message']
                rows.append(row)
                continue
            try:
                row['new_level_id'] = _new_level_id(row, level_id_rule, preserve_level_prefix, occupied)
                source = list(calculation['sources'][row['row_id']])
                row['description'] = (source[columns['Des']] if 'Des' in columns else '') if description is None else description
                source[columns['LevelId']] = row['new_level_id']
                source[columns['TemplateId']] = row['new_template_id']
                if 'Des' in columns:
                    source[columns['Des']] = row['description']
                elif description is not None:
                    raise ValueError('config_level.csv 缺少 Des，无法覆盖备注')
                for name, value in zip(DUP_COLUMNS, row['new_parameters']):
                    source[columns[name]] = value
                if preserve_level_prefix:
                    occupied.add(row['new_level_id'])
                candidates.setdefault(row['new_level_id'], []).append((row, source))
                current = existing.get(row['new_level_id'], [])
                if len(current) > 1:
                    row['status'], row['message'] = 'blocked', '目标 LevelId 重复，无法自动选择'
                elif current and current[0] == source:
                    row['status'], row['message'] = 'skip', '同ID内容完全相同，跳过追加'
                elif current:
                    row['status'], row['message'] = 'blocked', '目标 LevelId 已存在且内容不同'
                else:
                    row['status'], row['message'] = 'append', '将追加新关卡配置'
            except ValueError as exc:
                row['status'], row['message'] = 'blocked', str(exc)
            rows.append(row)
        additions = []
        for target, group in candidates.items():
            if len(group) > 1 and any(data != group[0][1] for _, data in group[1:]):
                for row, _ in group:
                    row['status'], row['message'] = 'blocked', f'所选多行生成相同 LevelId {target} 且内容不同'
            else:
                for index, (row, data) in enumerate(group):
                    if row['status'] == 'append':
                        if index == 0:
                            additions.append(data)
                        else:
                            row['status'], row['message'] = 'skip', '所选行生成相同ID和内容，去重追加'
        addition = _addition(snapshot['raw'], snapshot['fields'], additions)
        identity = 'plan-' + secrets.token_hex(16)
        result = {'plan_id': identity, 'status': 'blocked' if any(r['status'] == 'blocked' for r in rows) else
            ('ready' if additions else 'empty'), 'rows': rows, 'summary': _summary(rows),
            'diff': _diff(snapshot['config_path'], snapshot['raw'], addition), 'config_path': snapshot['config_path'],
            'preserve_level_prefix': preserve_level_prefix}
        self._save(identity, {'result': result, 'binding': _binding(snapshot), 'additions': additions,
            'target_sha256': _digest(snapshot['raw'] + addition), 'write_started': False})
        return deepcopy(result)

    def _backup(self, plan_id, raw):
        path = self.state_root / 'backups' / (plan_id + '-config_level.csv')
        with _directory(path.parent, create=True) as fd:
            try:
                child = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            except FileExistsError:
                previous, _ = _read_file(fd, path.name)
                if previous != raw:
                    raise ValueError('运行数据中的既有备份与原配置不一致')
            else:
                with os.fdopen(child, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.fsync(fd)
        return str(path)

    def commit(self, plan_id, *, config_root):
        with _COMMIT_LOCK:
            plan, written, backup_path = None, False, None
            try:
                plan = self._load(plan_id, 'plan')
                result = plan['result']
                if result['status'] == 'blocked':
                    raise ValueError('预览存在冲突，未写入任何配置')
                current = _snapshot(config_root)
                _check_binding(current, plan['binding'], check_hash=False)
                backup_path = plan.get('backup_path')
                if plan['write_started'] and current['config_sha256'] == plan['target_sha256']:
                    written = bool(plan['additions'])
                    committed = {'status': 'committed', 'rows': result['rows'], 'diff': result['diff'],
                        'summary': result['summary'], 'backup_path': backup_path,
                        'config_path': result['config_path'], 'written': written}
                    plan['committed'] = committed
                    self._save(plan_id, plan)
                    return deepcopy(committed)
                _check_binding(current, plan['binding'])
                if plan['additions']:
                    if not current['mode'] & 0o222:
                        raise ValueError('config_level.csv 没有写入权限')
                    backup_path = self._backup(plan_id, current['raw'])
                    plan['backup_path'], plan['write_started'] = backup_path, True
                    self._save(plan_id, plan)
                    addition = _addition(current['raw'], current['fields'], plan['additions'])
                    target = current['raw'] + addition
                    directory = Path(current['directory'])
                    with _directory(directory) as fd:
                        name = '.config_level.' + secrets.token_hex(16) + '.tmp'
                        child = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                        current['mode'], dir_fd=fd)
                        try:
                            with os.fdopen(child, 'wb') as stream:
                                stream.write(target)
                                stream.flush()
                                os.fsync(stream.fileno())
                            fresh = _snapshot(config_root)
                            _check_binding(fresh, plan['binding'])
                            if _identity(fd) != fresh['directory_identity']:
                                raise ValueError('写入期间目录身份变化')
                            os.replace(name, 'config_level.csv', src_dir_fd=fd, dst_dir_fd=fd)
                            written = True
                            os.fsync(fd)
                            check, _ = _read_file(fd, 'config_level.csv')
                            if check != target:
                                raise ValueError('原子写入后读回内容不一致')
                            final = _snapshot(config_root)
                            _check_binding(final, plan['binding'], check_hash=False)
                            if final['config_sha256'] != plan['target_sha256']:
                                raise ValueError('写入后所选路径的配置摘要不一致')
                        finally:
                            try:
                                os.unlink(name, dir_fd=fd)
                            except FileNotFoundError:
                                pass
                committed = {'status': 'committed', 'rows': result['rows'], 'diff': result['diff'],
                    'summary': result['summary'], 'backup_path': backup_path,
                    'config_path': result['config_path'], 'written': written}
                plan['committed'] = committed
                self._save(plan_id, plan)
                return deepcopy(committed)
            except Exception as exc:
                return {'status': 'error', 'message': str(exc), 'written': written,
                    'rows': [] if plan is None else plan['result']['rows'],
                    'diff': '' if plan is None else plan['result']['diff'],
                    'backup_path': backup_path,
                    'config_path': None if plan is None else plan['result']['config_path']}

    def export(self, calculation_id, format='xlsx'):
        result = self._load(calculation_id, 'calc')['result']
        labels = {'ready': '可追加', 'unchanged': '参数不变', 'no_tower': '无塔', 'failed': '失败'}
        mapping = [['关卡号', '原关卡ID', '新关卡ID', '原模板ID', '新模板ID', '原A数量', '原B数量',
                    '新A数量', '新B数量', '操作模式', '原总牌数', '新总牌数', '新增牌数',
                    'A种类变化', 'B种类变化', '状态', '说明', '备注',
                    'RandomType', 'StrategyGroupId', 'randomSeed_groupID', *DUP_COLUMNS]]
        for row in result['rows']:
            source, target = row.get('source_tile_counts') or {}, row.get('result_tile_counts') or {}
            mapping.append(['|'.join(map(str, row['level_indexes'])), row['original_level_id'],
                row['new_level_id'], row['original_template_id'], row['new_template_id'],
                source.get('A'), source.get('B'), target.get('A'), target.get('B'),
                {'increase':'增加', 'manual':'微调'}.get(row.get('operation'), '减少'),
                row.get('original_total'), row.get('final_total'), row.get('added_tiles'),
                row['delta']['A'], row['delta']['B'], labels[row['status']], row['message'], row['description'],
                row['RandomType'], row['StrategyGroupId'], row['randomSeed_groupID'],
                *(row['new_parameters'] if row['new_parameters'] else [''] * 10)])
        comparison = [['关卡号', '原关卡ID', '新关卡ID', '原模板ID', '新模板ID', '参数版本', *DUP_COLUMNS]]
        for row in result['comparisons']:
            comparison.append(['|'.join(map(str, row['level_indexes'])), row['original_level_id'],
                row['new_level_id'], row['original_template_id'], row['new_template_id'],
                row['version'], *row['parameters']])
        if format == 'csv':
            stream = io.StringIO(newline='')
            csv.writer(stream, lineterminator='\r\n').writerows(mapping)
            return b'\xef\xbb\xbf' + stream.getvalue().encode('utf-8'), 'text/csv; charset=utf-8', calculation_id + '.csv'
        if format != 'xlsx':
            raise ValueError('只支持 xlsx 或 csv 导出')
        return _xlsx((('关卡模板映射', mapping), ('变动参数对照', comparison))), \
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', calculation_id + '.xlsx'


def _xml(value):
    text = str(value) if value is not None else ''
    return escape(re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text))


def _cell_name(index):
    result = ''
    while index:
        index, rem = divmod(index - 1, 26)
        result = chr(65 + rem) + result
    return result


def _column_width(heading):
    if heading in ('原关卡ID', '新关卡ID', '原模板ID', '新模板ID'):
        return 32
    if heading in DUP_COLUMNS or heading == '关卡号':
        return 18
    return {'说明': 60, '备注': 48, 'StrategyGroupId': 20, 'randomSeed_groupID': 24,
            'A种类变化': 16, 'B种类变化': 16}.get(heading, 14)


def _row_height(row, widths):
    lines = 1
    for value, width in zip(row, widths):
        text = '' if value is None else str(value)
        wrapped = 0
        for part in text.split('\n'):
            units = sum(2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1 for char in part)
            wrapped += max(1, (units + width - 3) // (width - 2))
        lines = max(lines, wrapped)
    return min(409, max(28, lines * 16 + 8))


def _xlsx_styles(namespace):
    fills = '<fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
    for color in ('FFE8ECCD', 'FFF8F9F6', 'FFF0F3E8'):
        fills += f'<fill><patternFill patternType="solid"><fgColor rgb="{color}"/><bgColor indexed="64"/></patternFill></fill>'
    xfs = []
    for font, fill, border, horizontal in ((0, 0, 0, 'left'), (1, 2, 1, 'center'),
                                           (0, 3, 0, 'left'), (0, 4, 0, 'left')):
        xfs.append(f'<xf numFmtId="0" fontId="{font}" fillId="{fill}" borderId="{border}" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="{horizontal}" vertical="center" wrapText="1"/></xf>')
    return f'<styleSheet xmlns="{namespace}"><fonts count="2"><font><sz val="11"/><color rgb="FF263026"/><name val="思源黑体"/></font><font><b/><sz val="11"/><color rgb="FF414B28"/><name val="思源黑体"/></font></fonts><fills count="5">' + fills + '</fills><borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border><border><left/><right/><top/><bottom style="thin"><color rgb="FFCBD4B4"/></bottom><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="4">' + ''.join(xfs) + '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'


def _xlsx(sheets):
    """Minimal genuine OpenXML workbook using inline strings, no dependencies."""
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    prefix = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        overrides = ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(sheets) + 1))
        archive.writestr('[Content_Types].xml', prefix + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' + overrides + '</Types>')
        archive.writestr('_rels/.rels', prefix + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="' + rel + '/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        sheet_xml = ''.join(f'<sheet name="{_xml(name)}" sheetId="{i}" r:id="rId{i}"/>' for i, (name, _) in enumerate(sheets, 1))
        archive.writestr('xl/workbook.xml', prefix + f'<workbook xmlns="{ns}" xmlns:r="{rel}"><sheets>{sheet_xml}</sheets></workbook>')
        sheet_rels = ''.join(f'<Relationship Id="rId{i}" Type="{rel}/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets) + 1))
        archive.writestr('xl/_rels/workbook.xml.rels', prefix + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + sheet_rels + f'<Relationship Id="rId{len(sheets)+1}" Type="{rel}/styles" Target="styles.xml"/></Relationships>')
        archive.writestr('xl/styles.xml', prefix + _xlsx_styles(ns))
        for index, (_, table) in enumerate(sheets, 1):
            rows = []
            widths = [_column_width(heading) for heading in table[0]]
            version_column = table[0].index('参数版本') if '参数版本' in table[0] else None
            for row_index, row in enumerate(table, 1):
                cells = []
                style_index = 1 if row_index == 1 else (2 if version_column is not None and row[version_column] == '原参数'
                    else (3 if version_column is not None and row[version_column] == '新参数' else 0))
                for column, value in enumerate(row, 1):
                    address = f'{_cell_name(column)}{row_index}'
                    style = f' s="{style_index}"'
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        cells.append(f'<c r="{address}"{style}><v>{value}</v></c>')
                    else:
                        cells.append(f'<c r="{address}" t="inlineStr"{style}><is><t xml:space="preserve">{_xml(value)}</t></is></c>')
                height = max(32, _row_height(row, widths)) if row_index == 1 else _row_height(row, widths)
                rows.append(f'<row r="{row_index}" ht="{height}" customHeight="1">' + ''.join(cells) + '</row>')
            cols = '<cols>' + ''.join(f'<col min="{column}" max="{column}" width="{width}" customWidth="1"/>'
                for column, width in enumerate(widths, 1)) + '</cols>'
            region = f'A1:{_cell_name(len(table[0]))}{len(table)}'
            archive.writestr(f'xl/worksheets/sheet{index}.xml', prefix + f'<worksheet xmlns="{ns}"><dimension ref="{region}"/><sheetViews><sheetView workbookViewId="0" showGridLines="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>{cols}<sheetData>' + ''.join(rows) + f'</sheetData><autoFilter ref="{region}"/></worksheet>')
    return stream.getvalue()
