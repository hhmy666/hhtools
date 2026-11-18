"""Canonical level-data model and revisioned filesystem repository.

parse_csv_bundle(texts) -> {"entities": {collection: {id: row}}, "files":
{filename: {"sha256": str, "rowCount": int}}}.
sync_csv_bundle(store, parsed, actor, now) -> (new_store, report), without
mutating either input. Report integer totals: added, updated, unchanged,
restored, markedMissing; warnings is a list of strings; changedKeys maps
collections to changed ID lists; entities maps collections to category/ID lists.
validate_store(store) returns warning strings or raises ValueError.
render_csv_bundle(store) returns the five UTF-8 CSV texts keyed by filename.
LevelDataRepository owns persistence, locking, and revision publication.
Nullable pipe arrays (ddaMatrix, stageParam, tileKindNum) use None for an
empty CSV cell; [] is rejected because it cannot round-trip distinctly.
Strategy lists use [] for the CSV 0 sentinel; the ambiguous [0] is rejected.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CSV_HEADERS = {'config_level_randomSeed.csv': [['seedID',
                                  'GroupID',
                                  'ChokeTile_num',
                                  'ChokeTile_startLayer',
                                  'ChokeTile_param',
                                  'NormalTile_param',
                                  'Stage_param',
                                  'TileKind_num'],
                                 ['int', 'int', 'int', 'int', 'int', 'int', 'array', 'array'],
                                 ['随机种子ID',
                                  '随机种子组ID',
                                  '卡点数量',
                                  '卡点图案分配的上限层',
                                  '卡点层间隔',
                                  '非卡点层间隔',
                                  '非卡点图案分组比例',
                                  '非卡点图案连续层（从下到上）']],
 'config_level_list.csv': [['LevelIndex',
                            'LevelType',
                            'LevelCoin',
                            'RewardCoinMultiple',
                            'LevelId',
                            'DDALevelId1',
                            'DDALevelId2',
                            'DDALevelId3',
                            'DDAType',
                            'DDAMatrix',
                            'LevelDuration',
                            'LevelCoinPercentage',
                            'LevelFuuuDup'],
                           ['int',
                            'int',
                            'int',
                            'int',
                            'array',
                            'array',
                            'array',
                            'array',
                            'int',
                            'array',
                            'int',
                            'int',
                            'int'],
                           ['第几关',
                            '关卡类型，0=普通，1=Hard，2=SuperHard，3=金币',
                            '基础金币奖励值。结算金币数=0时不显示。',
                            '用户看完广告后获得的金币倍数',
                            '关卡 id',
                            '关卡 id',
                            '关卡 id',
                            '关卡 id',
                            'DDA 类型，0=不走DDA（取 LevelId 列），1=走DDA，2=考试关（取 LevelId 列）',
                            'DDA 参数',
                            '金币关的关卡时长(单位秒)，最高100分钟，即6000s',
                            '金币tile的比例（单位为%，取值0-100）',
                            '关卡FUUU副本']],
 'config_level_randomSeed_group.csv': [['GroupID',
                                        'DupPara1',
                                        'DupPara2',
                                        'DupPara3',
                                        'DupPara4',
                                        'DupPara5',
                                        'DupPara6',
                                        'DupPara7',
                                        'DupPara8',
                                        'DupPara9',
                                        'DupPara10'],
                                       ['int',
                                        'int',
                                        'int',
                                        'int',
                                        'int',
                                        'int',
                                        'int',
                                        'int',
                                        'int',
                                        'int',
                                        'int'],
                                       ['随机种子组ID',
                                        '副本1使用的随机种子ID',
                                        '副本2使用的随机种子ID',
                                        '副本3使用的随机种子ID',
                                        '副本4使用的随机种子ID',
                                        '副本5使用的随机种子ID',
                                        '副本6使用的随机种子ID',
                                        '副本7使用的随机种子ID',
                                        '副本8使用的随机种子ID',
                                        '副本9使用的随机种子ID',
                                        '副本10使用的随机种子ID']],
 'config_level_strategy_group.csv': [['StrategyGroupId',
                                      'InstanceId1',
                                      'InstanceId2',
                                      'InstanceId3',
                                      'InstanceId4',
                                      'InstanceId5',
                                      'InstanceId6',
                                      'InstanceId7',
                                      'InstanceId8',
                                      'InstanceId9',
                                      'InstanceId10'],
                                     ['int',
                                      'array',
                                      'array',
                                      'array',
                                      'array',
                                      'array',
                                      'array',
                                      'array',
                                      'array',
                                      'array',
                                      'array'],
                                     ['策略组合ID',
                                      '副本1(策略应用规则 | 使用的策略数量 | 策略列表）',
                                      '副本2(策略应用规则 | 使用的策略数量 | 策略列表）',
                                      '副本3(策略应用规则 | 使用的策略数量 | 策略列表）',
                                      '副本4(策略应用规则 | 使用的策略数量 | 策略列表）',
                                      '副本5(策略应用规则 | 使用的策略数量 | 策略列表）',
                                      '副本6(策略应用规则 | 使用的策略数量 | 策略列表）',
                                      '副本7(策略应用规则 | 使用的策略数量 | 策略列表）',
                                      '副本8(策略应用规则 | 使用的策略数量 | 策略列表）',
                                      '副本9(策略应用规则 | 使用的策略数量 | 策略列表）',
                                      '副本10(策略应用规则 | 使用的策略数量 | 策略列表）']],
 'config_level.csv': [['LevelId',
                       'Des',
                       'TemplateId',
                       'RandomType',
                       'DupPara1',
                       'DupPara2',
                       'DupPara3',
                       'DupPara4',
                       'DupPara5',
                       'DupPara6',
                       'DupPara7',
                       'DupPara8',
                       'DupPara9',
                       'DupPara10',
                       'StrategyGroupId',
                       'randomSeed_groupID'],
                      ['string',
                       'string',
                       'string',
                       'int',
                       'array',
                       'array',
                       'array',
                       'array',
                       'array',
                       'array',
                       'array',
                       'array',
                       'array',
                       'array',
                       'int',
                       'int'],
                      ['关卡id',
                       '备注',
                       '模板 id',
                       '0=固定按第n次玩来取副本，1=随机取',
                       '第1次玩(ATile种类数 | 随机种子 | 塔副本幸运值 | BTile种类数)',
                       '第2次玩(ATile种类数 | 随机种子 | 塔副本幸运值 | BTile种类数)',
                       '第3次玩(ATile种类数 | 随机种子 | 塔副本幸运值 | BTile种类数)',
                       '第4次玩(ATile种类数 | 随机种子 | 塔副本幸运值 | BTile种类数)',
                       '第5次玩(ATile种类数 | 随机种子 | 塔副本幸运值 | BTile种类数)',
                       '第6次玩(ATile种类数 | 随机种子 | 塔副本幸运值 | BTile种类数)',
                       '第7次玩(ATile种类数 | 随机种子 | 塔副本幸运值 | BTile种类数)',
                       '第8次玩(ATile种类数 | 随机种子 | 塔副本幸运值 | BTile种类数)',
                       '第9次玩(ATile种类数 | 随机种子 | 塔副本幸运值 | BTile种类数)',
                       '第10次及之后玩(ATile种类数 | 随机种子 | 塔副本幸运值 | BTile种类数)',
                       '策略组合ID',
                       '随机种子组ID']]}

# Collection, primary-key field, and CSV-mapped fields in their wire order.
TABLES = {
    'config_level_list.csv': ('levelIndexes', 'levelIndex', (
        'levelIndex', 'levelType', 'levelCoin', 'rewardCoinMultiple', 'levelId',
        'ddaLevelIds', 'ddaType', 'ddaMatrix', 'levelDuration', 'levelCoinPercentage', 'levelFuuuDup')),
    'config_level_randomSeed_group.csv': ('randomSeedGroups', 'groupId', ('groupId', 'seedIds')),
    'config_level_randomSeed.csv': ('randomSeeds', 'seedId', (
        'seedId', 'groupId', 'chokeTileCount', 'chokeTileStartLayer', 'chokeTileParam',
        'normalTileParam', 'stageParam', 'tileKindNum')),
    'config_level_strategy_group.csv': ('strategyGroups', 'strategyGroupId', ('strategyGroupId', 'slots')),
    'config_level.csv': ('levels', 'levelId', (
        'levelId', 'des', 'templateId', 'randomType', 'dupParams', 'strategyGroupId', 'randomSeedGroupId')),
}
CSV_FILES = tuple(TABLES)
COLLECTIONS = tuple(table[0] for table in TABLES.values())
DUP_FIELDS = ('aTileKinds', 'seed', 'towerLucky', 'bTileKinds')


def new_store(now: str, actor: str) -> dict[str, Any]:
    """Create an empty revision-zero store, ready for its first import."""
    return dict(schemaVersion=1, revision=0, createdAt=now, updatedAt=now,
                updatedBy=actor, csvSync=None, templates={}, **{name: {} for name in COLLECTIONS})


def _int(cell: str, nullable: bool = False) -> int | None:
    cell = cell.strip()
    if nullable and not cell:
        return None
    if not re.fullmatch(r'[+-]?[0-9]+', cell):
        raise ValueError(f'expected integer, got {cell!r}')
    return int(cell)


def _ints(cell: str) -> list[int] | None:
    return [_int(part) for part in cell.split('|')] if cell.strip() else None


def _parse_row(name: str, cells: list[str]) -> dict[str, Any]:
    if name == 'config_level.csv':
        dup = []
        for cell in cells[4:14]:
            values = _ints(cell)
            if values is not None and len(values) != 4:
                raise ValueError('DupPara requires four integers')
            dup.append(dict(zip(DUP_FIELDS, values)) if values is not None else None)
        return dict(levelId=cells[0].strip(), des=cells[1], templateId=cells[2].strip(),
                    randomType=_int(cells[3]), dupParams=dup,
                    strategyGroupId=_int(cells[14], True), randomSeedGroupId=_int(cells[15], True))
    if name == 'config_level_list.csv':
        return dict(levelIndex=_int(cells[0]), levelType=_int(cells[1]), levelCoin=_int(cells[2]),
                    rewardCoinMultiple=_int(cells[3]), levelId=cells[4].strip(),
                    ddaLevelIds=[cell.strip() for cell in cells[5:8]], ddaType=_int(cells[8]),
                    ddaMatrix=_ints(cells[9]), levelDuration=_int(cells[10], True),
                    levelCoinPercentage=_int(cells[11], True), levelFuuuDup=_int(cells[12]))
    if name == 'config_level_randomSeed_group.csv':
        return dict(groupId=_int(cells[0]), seedIds=[_int(cell, True) for cell in cells[1:]])
    if name == 'config_level_randomSeed.csv':
        fields = TABLES[name][2]
        return dict(zip(fields, [_int(cells[0]), _int(cells[1]), _int(cells[2]), _int(cells[3]),
                                 _int(cells[4]), _int(cells[5], True), _ints(cells[6]), _ints(cells[7])]))
    slots = []
    for cell in cells[1:]:
        if not cell.strip():
            slots.append(None)
            continue
        parts = cell.split('|')
        if len(parts) != 3:
            raise ValueError('strategy slot requires useType|targetCount|strategyIds')
        ids = [] if parts[2].strip() in ('', '0') else [_int(part) for part in parts[2].split(';')]
        slots.append(dict(useType=_int(parts[0]), targetCount=_int(parts[1]), strategyIds=ids))
    return dict(strategyGroupId=_int(cells[0]), slots=slots)


def parse_csv_bundle(files: dict[str, str]) -> dict[str, Any]:
    """Parse exactly five text files and reject the entire bundle on any error."""
    if not isinstance(files, dict) or set(files) != set(CSV_FILES):
        raise ValueError('CSV bundle must contain exactly the five required files')
    entities, metadata = {}, {}
    for name, (collection, primary, _) in TABLES.items():
        text = files[name]
        if not isinstance(text, str):
            raise ValueError(f'{name}: expected UTF-8 text')
        try:
            encoded = text.encode('utf-8')
            rows = list(csv.reader(io.StringIO(text.lstrip('\ufeff'), newline=''), strict=True))
        except (UnicodeError, csv.Error) as error:
            raise ValueError(f'{name}: invalid UTF-8 CSV: {error}') from error
        if rows[:3] != CSV_HEADERS[name]:
            raise ValueError(f'{name}: invalid three-row header')
        records = {}
        for line, cells in enumerate(rows[3:], 4):
            if not cells or not any(cells):
                continue
            try:
                if len(cells) != len(CSV_HEADERS[name][0]):
                    raise ValueError('incorrect column count')
                record = _parse_row(name, cells)
                key = str(record[primary])
                if key in records:
                    raise ValueError(f'duplicate primary key {key!r}')
                records[key] = record
            except ValueError as error:
                raise ValueError(f'{name}:{line}: {error}') from error
        entities[collection] = records
        metadata[name] = dict(sha256=hashlib.sha256(encoded).hexdigest(), rowCount=len(records))
    _validate_entities(entities)
    return dict(entities=entities, files=metadata)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_template_id(value: Any) -> None:
    """Use the same filename-safe template ID contract at every entry point."""
    _require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_]+', value) is not None,
             'templateId: expected only ASCII letters, digits, or underscores')


def _integer(value: Any, where: str, nullable: bool = False) -> None:
    _require((nullable and value is None) or type(value) is int, f'{where}: expected integer')


def _array(value: Any, where: str, length: int | None = None, nullable: bool = False) -> bool:
    if value is None and nullable:
        return False
    _require(isinstance(value, list), f'{where}: expected array')
    _require(length is None or len(value) == length, f'{where}: expected {length} slots')
    return True


def _int_array(value: Any, where: str, length: int | None = None, nullable: bool = False,
               nullable_items: bool = False) -> None:
    if _array(value, where, length, nullable):
        # A blank pipe-array CSV cell means null, never an empty list.
        _require(not nullable or bool(value), f'{where}: use null instead of an empty array')
        for item in value:
            _integer(item, where, nullable_items)


def _active(row: dict[str, Any]) -> bool:
    return row.get('sourceStatus', 'active') == 'active'


def _validate_entities(entities: dict[str, Any]) -> list[str]:
    warnings = []
    for name, (collection, primary, fields) in TABLES.items():
        records = entities.get(collection)
        _require(isinstance(records, dict), f'{collection}: expected dictionary')
        for key, row in records.items():
            _require(isinstance(row, dict), f'{collection}/{key}: expected record')
            _require(all(field in row for field in fields), f'{collection}/{key}: missing CSV field')
            value = row[primary]
            if collection == 'levels':
                _require(isinstance(value, str) and bool(value.strip()), 'levelId: expected nonempty string')
            else:
                _integer(value, primary)
            _require(isinstance(key, str) and key == str(value), f'{collection}/{key}: primary key mismatch')
            if collection == 'levels':
                _require(isinstance(row['des'], str), 'des: expected string')
                validate_template_id(row['templateId'])
                _integer(row['randomType'], 'randomType')
                _integer(row['strategyGroupId'], 'strategyGroupId', True)
                _integer(row['randomSeedGroupId'], 'randomSeedGroupId', True)
                _array(row['dupParams'], 'dupParams', 10)
                for slot in row['dupParams']:
                    if slot is not None:
                        _require(isinstance(slot, dict) and set(slot) == set(DUP_FIELDS), 'invalid dupParams slot')
                        for field in DUP_FIELDS:
                            _integer(slot[field], field)
            elif collection == 'levelIndexes':
                for field in ('levelType', 'levelCoin', 'rewardCoinMultiple', 'ddaType', 'levelFuuuDup'):
                    _integer(row[field], field)
                for field in ('levelDuration', 'levelCoinPercentage'):
                    _integer(row[field], field, True)
                _require(isinstance(row['levelId'], str), 'levelId: expected string')
                _array(row['ddaLevelIds'], 'ddaLevelIds', 3)
                _require(all(isinstance(v, str) for v in row['ddaLevelIds']), 'ddaLevelIds: expected strings')
                _int_array(row['ddaMatrix'], 'ddaMatrix', nullable=True)
            elif collection == 'strategyGroups':
                _array(row['slots'], 'slots', 10)
                for slot in row['slots']:
                    if slot is not None:
                        _require(isinstance(slot, dict) and set(slot) == {'useType', 'targetCount', 'strategyIds'},
                                 'invalid strategy slot')
                        _integer(slot['useType'], 'useType')
                        _integer(slot['targetCount'], 'targetCount')
                        _int_array(slot['strategyIds'], 'strategyIds')
                        _require(slot['strategyIds'] != [0],
                                 'strategyIds: use [] for the reserved CSV 0 sentinel')
            elif collection == 'randomSeedGroups':
                _int_array(row['seedIds'], 'seedIds', 10, nullable_items=True)
            else:
                for field in ('groupId', 'chokeTileCount', 'chokeTileStartLayer', 'chokeTileParam'):
                    _integer(row[field], field)
                _integer(row['normalTileParam'], 'normalTileParam', True)
                _int_array(row['stageParam'], 'stageParam', nullable=True)
                _int_array(row['tileKindNum'], 'tileKindNum', nullable=True)
    for key, row in entities['levels'].items():
        if not _active(row):
            continue
        for field, target in [('strategyGroupId', 'strategyGroups'), ('randomSeedGroupId', 'randomSeedGroups')]:
            ref = row[field]
            _require(ref is None or (str(ref) in entities[target] and _active(entities[target][str(ref)])),
                     f'levels/{key}: unknown {field} {ref}')
    groups, seeds = entities['randomSeedGroups'], entities['randomSeeds']
    for key, row in groups.items():
        if not _active(row):
            continue
        ids = [seed for seed in row['seedIds'] if seed is not None]
        _require(len(ids) == len(set(ids)), f'randomSeedGroups/{key}: duplicate seed ID')
        for seed_id in ids:
            seed = seeds.get(str(seed_id))
            _require(seed is not None and _active(seed) and seed['groupId'] == row['groupId'],
                     f'randomSeedGroups/{key}: inconsistent seed {seed_id}')
    for key, row in seeds.items():
        if not _active(row):
            continue
        group = groups.get(str(row['groupId']))
        _require(group is not None and _active(group) and row['seedId'] in group['seedIds'],
                 f'randomSeeds/{key}: inconsistent group {row["groupId"]}')
    for key, row in entities['levelIndexes'].items():
        if _active(row):
            for ref in dict.fromkeys([row['levelId']] + row['ddaLevelIds']):
                if ref and ref not in entities['levels']:
                    warnings.append(f'levelIndexes/{key}: unknown levelId {ref}')
    return warnings


def validate_store(store: dict[str, Any]) -> list[str]:
    """Validate typed canonical records and references; return historical warnings."""
    _require(isinstance(store, dict), 'store: expected object')
    _require(store.get('schemaVersion') == 1, 'unsupported schemaVersion')
    _integer(store.get('revision'), 'revision')
    _require(store['revision'] >= 0, 'revision must be non-negative')
    warnings = _validate_entities(store)
    templates = store.get('templates')
    _require(isinstance(templates, dict), 'templates: expected dictionary')
    for key, template in templates.items():
        _require(isinstance(template, dict) and isinstance(key, str) and bool(key) and
                 template.get('templateId') == key, f'templates/{key}: primary key mismatch')
        validate_template_id(key)
        for field in ('name', 'author', 'createdAt', 'createdBy', 'updatedAt', 'updatedBy'):
            _require(isinstance(template.get(field), str), f'templates/{key}: invalid {field}')
        _require(isinstance(template.get('bytes'), dict) and type(template['bytes'].get('exists')) is bool,
                 f'templates/{key}: invalid bytes metadata')
    for key, level in store['levels'].items():
        _require(level['templateId'] in templates, f'levels/{key}: missing template {level["templateId"]}')
    for collection in COLLECTIONS:
        for key, row in store[collection].items():
            _require(row.get('sourceStatus') in ('active', 'missingFromCsv'), f'{collection}/{key}: invalid sourceStatus')
            _require(row.get('managedBy') in ('csv', 'json'), f'{collection}/{key}: invalid managedBy')
            for field in ('sourceFile', 'firstSeenAt', 'lastSeenAt', 'createdAt', 'createdBy', 'updatedAt', 'updatedBy'):
                _require(isinstance(row.get(field), str), f'{collection}/{key}: invalid {field}')
    return warnings


def template_stats_stale(stats: dict[str, Any] | None, source_sha256: str | None) -> bool:
    """Compare against live bytes metadata; protobuf stays a TypeScript concern."""
    return bool(stats and stats.get('sourceSha256') != source_sha256)


def _ensure_templates(store: dict[str, Any], actor: str, now: str) -> None:
    for level in store['levels'].values():
        template_id = level['templateId']
        if template_id not in store['templates']:
            store['templates'][template_id] = dict(
                templateId=template_id, name=level['des'] or template_id, author=actor,
                createdAt=now, createdBy=actor, updatedAt=now, updatedBy=actor,
                bytes=dict(fileName=f'{template_id}_m.bytes', exists=False, size=0, sha256=None, modifiedAt=None),
                stats=None)


def sync_csv_bundle(store: dict[str, Any], parsed: dict[str, Any], actor: str,
                    now: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Non-destructive upsert, returning a new revision and change report."""
    _validate_entities(parsed['entities'])
    result = copy.deepcopy(store)
    categories = ('added', 'updated', 'unchanged', 'restored', 'markedMissing')
    report = {category: 0 for category in categories}
    report.update(warnings=[], changedKeys={}, entities={})
    for name, (collection, _, fields) in TABLES.items():
        existing, incoming = result[collection], parsed['entities'][collection]
        changes = {category: [] for category in categories}
        for key, values in incoming.items():
            old = existing.get(key)
            if old is None:
                existing[key] = dict(copy.deepcopy(values), sourceStatus='active', managedBy='csv', sourceFile=name,
                                     firstSeenAt=now, lastSeenAt=now, createdAt=now, createdBy=actor,
                                     updatedAt=now, updatedBy=actor)
                changes['added'].append(key)
                continue
            changed = any(old.get(field) != values[field] for field in fields)
            restored = old.get('sourceStatus') != 'active'
            old.update({field: copy.deepcopy(values[field]) for field in fields})
            old.update(sourceStatus='active', sourceFile=name, lastSeenAt=now)
            if changed or restored:
                old.update(updatedAt=now, updatedBy=actor)
            if restored:
                changes['restored'].append(key)
            if changed:
                changes['updated'].append(key)
            if not changed and not restored:
                changes['unchanged'].append(key)
        for key, old in existing.items():
            if (key not in incoming and old.get('managedBy') == 'csv' and
                    old.get('sourceFile') == name and old.get('sourceStatus') != 'missingFromCsv'):
                old.update(sourceStatus='missingFromCsv', updatedAt=now, updatedBy=actor)
                changes['markedMissing'].append(key)
        for category in categories:
            report[category] += len(changes[category])
        report['entities'][collection] = changes
        report['changedKeys'][collection] = list(dict.fromkeys(
            key for category in categories if category != 'unchanged' for key in changes[category]))
    _ensure_templates(result, actor, now)
    result.update(revision=store['revision'] + 1, updatedAt=now, updatedBy=actor)
    result['csvSync'] = dict(batchId=f'{now}-{actor}', syncedAt=now, syncedBy=actor, files=copy.deepcopy(parsed['files']))
    report['warnings'] = validate_store(result)
    return result, report


def _cell(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, list):
        return '|'.join(str(item) for item in value)
    return str(value)


def _row_cells(name: str, row: dict[str, Any]) -> list[str]:
    if name == 'config_level.csv':
        return [row['levelId'], row['des'], row['templateId'], _cell(row['randomType']),
                *['|'.join(str(slot[field]) for field in DUP_FIELDS) if slot is not None else ''
                  for slot in row['dupParams']], _cell(row['strategyGroupId']), _cell(row['randomSeedGroupId'])]
    if name == 'config_level_list.csv':
        return [_cell(row[field]) for field in ('levelIndex', 'levelType', 'levelCoin', 'rewardCoinMultiple', 'levelId')] + row['ddaLevelIds'] + [
            _cell(row[field]) for field in ('ddaType', 'ddaMatrix', 'levelDuration', 'levelCoinPercentage', 'levelFuuuDup')]
    if name == 'config_level_randomSeed_group.csv':
        return [_cell(row['groupId']), *[_cell(seed) for seed in row['seedIds']]]
    if name == 'config_level_strategy_group.csv':
        return [_cell(row['strategyGroupId']), *[
            f'{slot["useType"]}|{slot["targetCount"]}|{";".join(map(str, slot["strategyIds"])) or "0"}'
            if slot is not None else '' for slot in row['slots']]]
    return [_cell(row[field]) for field in TABLES[name][2]]


def render_csv_bundle(store: dict[str, Any]) -> dict[str, str]:
    """Render active canonical rows using the established three-row CSV headers."""
    validate_store(store)
    files = {}
    for name, (collection, _, _) in TABLES.items():
        output = io.StringIO(newline='')
        writer = csv.writer(output, lineterminator='\n')
        writer.writerows(CSV_HEADERS[name])
        writer.writerows(_row_cells(name, row) for row in store[collection].values() if _active(row))
        files[name] = output.getvalue()
    return files


class RevisionConflict(ValueError):
    """A stale writer, including the authoritative state for an HTTP 409."""

    def __init__(self, current_store: dict[str, Any]):
        self.current_store = copy.deepcopy(current_store)
        self.current_revision = current_store['revision']
        super().__init__(f'revision conflict: current revision is {self.current_revision}')


class LevelDataRepository:
    """Single-process repository; share one instance across request threads.

    root is the WebEditor directory, containing csv/ and level_data.json.
    A durable journal precedes CSV replacement; JSON replacement commits the
    revision. Recovery always renders the revision present in JSON. The lock
    deliberately covers reads too, so no request observes an unfinished write.
    """

    def __init__(self, root: str | Path, template_dir: str | Path):
        self.root = Path(root).resolve()
        self.template_dir = Path(template_dir).resolve()
        self.csv_dir = self.root / 'csv'
        self.path = self.root / 'level_data.json'
        self.journal_path = self.root / 'level_data.transaction.json'
        self.backup_dir = self.root / 'backups' / 'level_data'
        self._lock = threading.Lock()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')

    @staticmethod
    def _json_bytes(value: Any) -> bytes:
        return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')

    @staticmethod
    def _sync_dir(directory: Path) -> None:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _stage(self, target: Path, data: bytes) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f'.{target.name}.', suffix='.tmp', dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return temporary

    def _replace(self, temporary: Path, target: Path) -> None:
        os.replace(temporary, target)
        self._sync_dir(target.parent)

    def _atomic_write(self, target: Path, data: bytes) -> None:
        temporary = self._stage(target, data)
        try:
            self._replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _load(self) -> dict[str, Any]:
        store = json.loads(self.path.read_text(encoding='utf-8'))
        validate_store(store)
        return store

    def load(self) -> dict[str, Any]:
        with self._lock:
            self._recover()
            return self._load()

    def initialize_from_csv(self, actor: str) -> dict[str, Any]:
        """Idempotently create revision one without changing any existing CSV."""
        with self._lock:
            self._recover()
            if self.path.exists():
                return self._load()
            files = {name: (self.csv_dir / name).read_bytes().decode('utf-8') for name in CSV_FILES}
            now = self._now()
            store, _ = sync_csv_bundle(new_store(now, actor), parse_csv_bundle(files), actor, now)
            self._publish(None, store, {})
            return store

    @staticmethod
    def _check_revision(current: dict[str, Any], expected: int) -> None:
        _integer(expected, 'expected_revision')
        if current['revision'] != expected:
            raise RevisionConflict(current)

    def commit(self, expected_revision: int, next_store: dict[str, Any],
               csv_bundle: dict[str, str]) -> dict[str, Any]:
        """Publish a validated next revision and its exact five-file projection."""
        with self._lock:
            self._recover()
            current = self._load()
            self._check_revision(current, expected_revision)
            candidate = copy.deepcopy(next_store)
            validate_store(candidate)
            _require(candidate['revision'] == expected_revision + 1, 'next revision must increment by one')
            _require(csv_bundle == render_csv_bundle(candidate), 'CSV bundle must match next store projection')
            self._publish(current, candidate, csv_bundle)
            return candidate

    def import_csv(self, files: dict[str, str], expected_revision: int,
                   actor: str) -> tuple[dict[str, Any], dict[str, Any]]:
        with self._lock:
            self._recover()
            current = self._load()
            self._check_revision(current, expected_revision)
            candidate, report = sync_csv_bundle(current, parse_csv_bundle(files), actor, self._now())
            self._publish(current, candidate, render_csv_bundle(candidate), import_csv=True)
            return candidate, report

    def _publish(self, current: dict[str, Any] | None, candidate: dict[str, Any],
                 csv_bundle: dict[str, str], import_csv: bool = False) -> None:
        # Materialize and encode the entire transaction before any filesystem write.
        data = [(self.csv_dir / name, text.encode('utf-8')) for name, text in csv_bundle.items()]
        data.append((self.path, self._json_bytes(candidate)))
        old_revision = current['revision'] if current is not None else 0
        artifacts = []
        try:
            json_backup = None
            if current is not None:
                json_backup = self.backup_dir / f'revision-{old_revision}.json'
                self._atomic_write(json_backup, self.path.read_bytes())
            batch = self.backup_dir / f'import-{old_revision}-to-{candidate["revision"]}'
            for target, content in data:
                backup = json_backup if target == self.path else None
                if import_csv and target != self.path:
                    backup = batch / target.name
                    self._atomic_write(backup, target.read_bytes())
                temporary = self._stage(target, content)
                artifacts.append(dict(target=str(target), temporary=str(temporary),
                                      backup=str(backup) if backup is not None else None))
            journal = dict(oldRevision=old_revision, newRevision=candidate['revision'], artifacts=artifacts)
            self._atomic_write(self.journal_path, self._json_bytes(journal))
            for artifact in artifacts:
                self._replace(Path(artifact['temporary']), Path(artifact['target']))
        except Exception:
            if self.journal_path.exists():
                self._recover()
                # An error after the JSON replacement is already committed. Finish
                # projection recovery and report its authoritative successful state.
                if self.path.exists() and self._load()['revision'] == candidate['revision']:
                    return
            else:
                for artifact in artifacts:
                    Path(artifact['temporary']).unlink(missing_ok=True)
            raise
        self._cleanup_transaction(journal)

    def _cleanup_transaction(self, journal: dict[str, Any]) -> None:
        # Projection publication/recovery is already complete. Housekeeping must
        # not turn that authoritative outcome into an apparent failed write.
        # Prune before deleting the journal so unfinished work remains retryable
        # through recover() and load(), even if a filesystem error persists.
        try:
            self._prune_backups()
            self._finish_transaction(journal)
        except OSError:
            return

    def _finish_transaction(self, journal: dict[str, Any]) -> None:
        for artifact in journal['artifacts']:
            Path(artifact['temporary']).unlink(missing_ok=True)
        self.journal_path.unlink()
        self._sync_dir(self.root)

    def _prune_backups(self) -> None:
        backups = sorted(self.backup_dir.glob('revision-*.json'),
                         key=lambda path: int(path.stem.split('-')[1]))
        for backup in backups[:-20]:
            backup.unlink()
        if backups:
            self._sync_dir(self.backup_dir)

    def recover(self) -> None:
        """Complete/roll back an interrupted transaction using the JSON commit point."""
        with self._lock:
            self._recover()

    def _recover(self) -> None:
        if not self.journal_path.exists():
            return
        journal = json.loads(self.journal_path.read_text(encoding='utf-8'))
        if self.path.exists():
            current = self._load()
            _require(current['revision'] in (journal['oldRevision'], journal['newRevision']),
                     'transaction journal does not match current JSON revision')
            # Initial migration only creates JSON, and must never rewrite CSV.
            if journal['oldRevision'] != 0:
                for name, text in render_csv_bundle(current).items():
                    self._atomic_write(self.csv_dir / name, text.encode('utf-8'))
        else:
            _require(journal['oldRevision'] == 0, 'cannot recover transaction without canonical JSON')
        self._cleanup_transaction(journal)
