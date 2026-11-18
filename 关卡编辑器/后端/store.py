"""Template draft storage. Source paths are read-only; sync copies a saved snapshot."""
from collections import Counter, OrderedDict
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import os
import re
import secrets
from threading import RLock

from codec.engine import analyze_bytes
from codec.repair import rewrite_tiles, validate_board
from codec.wire import parse_fields

MAX_BYTES = 2 * 1024 * 1024
SUFFIX = '_m.bytes'
READ_ID = re.compile(r'[\w.-]{1,180}')
NEW_ID = re.compile(r'[A-Za-z0-9]{1,80}')


class Fault(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def identifier(value):
    if not isinstance(value, str) or not NEW_ID.fullmatch(value):
        raise Fault('新模板名只能使用英文字母和数字，最多 80 个字符。')
    return value


def read_regular(path):
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
            import stat
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise Fault('所选对象不是模板文件。')
            data = stream.read(MAX_BYTES + 1)
    except FileNotFoundError as error:
        raise Fault('未找到模板文件，请核对模板 ID 或目录。', 404) from error
    except OSError as error:
        raise Fault('没有权限读取模板文件，或文件是符号链接。', 403) from error
    if len(data) > MAX_BYTES:
        raise Fault('模板文件不能超过 2 MB。', 413)
    return data


def decode(raw):
    if not raw or len(raw) > MAX_BYTES:
        raise Fault('请选择非空且不超过 2 MB 的模板。')
    try:
        board = analyze_bytes(raw)
        if len(board['tiles']) > 1500 or len(board['towers']) > 1500:
            raise Fault('模板超过 1500 张牌或塔，无法打开。')
        return board
    except Fault:
        raise
    except (ValueError, TypeError) as error:
        raise Fault('无法读取模板，请选择有效的 Unity 模板文件。', 422) from error


def assert_free(folder, name):
    try:
        if any(p.name.casefold() in {name.casefold(), name[:-len(SUFFIX)].casefold()} for p in folder.iterdir()):
            raise Fault('已存在同名模板，请换一个新名称。', 409)
    except OSError as error:
        raise Fault('目标目录不可用，请检查路径和权限。', 403) from error


def create_only(folder, name, data):
    assert_free(folder, name)
    path = folder / name
    made = False
    try:
        with path.open('xb') as stream:
            made = True
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as error:
        raise Fault('已存在同名文件，请换一个新名称。', 409) from error
    except OSError as error:
        if made:
            path.unlink(missing_ok=True)
        raise Fault('无法写入目标目录，请检查目录权限和磁盘空间。', 403) from error
    return path


class Store:
    def __init__(self, repo, output):
        self.repo = Path(repo).expanduser().resolve()
        self.output = Path(output).resolve()
        self.sessions = OrderedDict()
        self.saved = {}
        self.lock = RLock()

    def read_template(self, name):
        if not isinstance(name, str) or not READ_ID.fullmatch(name) or name in ('.', '..'):
            raise Fault('请输入有效的模板 ID。')
        canonical = name if name.endswith(SUFFIX) else name + SUFFIX
        return read_regular(self.repo / canonical)

    def load(self, name):
        return self.adopt(self.read_template(name))

    def adopt(self, raw):
        board = decode(raw)
        key = secrets.token_hex(16)
        with self.lock:
            while len(self.sessions) >= 24:
                self.sessions.popitem(last=False)
            self.sessions[key] = {'raw': raw, 'original': board, 'history': [], 'revision': 0,
                                  'translation': {'dx': 0, 'dy': 0}, 'origins': list(range(len(board['tiles'])))}
            return self.snapshot(key)

    def session(self, key, revision=None):
        s = self.sessions.get(key)
        if s is None:
            raise Fault('草稿会话已结束，请重新载入模板。', 404)
        if revision is not None and revision != s['revision']:
            raise Fault('草稿已更新，请重新载入编辑界面再试。', 409)
        self.sessions.move_to_end(key)
        return s

    def snapshot(self, key):
        with self.lock:
            s = self.session(key)
            board = decode(s['raw'])
            for tile, origin in zip(board['tiles'], s['origins']):
                tile['source_index'] = origin
            board.update(can_undo=bool(s['history']), translation=s['translation'])
            return deepcopy({'key': key, 'revision': s['revision'], 'board': board, 'original': s['original']})

    def edit(self, key, revision, tiles, towers, translation=None):
        with self.lock:
            s = self.session(key, revision)
            if not isinstance(tiles, list) or len(tiles) > 1500:
                raise Fault('一份模板最多可编辑 1500 张牌。')
            try:
                if translation is not None and (not isinstance(translation, dict) or set(translation) != {'dx','dy'} or
                    any(type(x) is not int for x in translation.values())):
                    raise ValueError('平移需要整数坐标')
                raw = rewrite_tiles(s['raw'], tiles, towers=towers)
                origins = [s['origins'][t['index']] if type(t.get('index')) is int and t['index'] >= 0 else None for t in tiles]
                return self.replace(key, s, raw, origins, translation)
            except Fault:
                raise
            except (ValueError, IndexError, TypeError) as error:
                raise Fault('编辑内容无法写入：' + str(error), 422) from error

    def replace(self, key, s, raw, origins, translation=None):
        decode(raw)
        s['history'] = (s['history'] + [(s['raw'], s['origins'], s['translation'])])[-30:]
        s.update(raw=raw, origins=origins, revision=s['revision']+1)
        if translation is not None:
            s['translation'] = translation
        return self.snapshot(key)

    def advanced(self, key, revision, raw):
        with self.lock:
            s = self.session(key, revision)
            board = decode(raw)
            current = decode(s['raw'])
            if current['template_id'] != board['template_id']:
                raise Fault('编辑草稿的模板 ID 不一致。', 409)
            before, after = parse_fields(s['raw']), parse_fields(raw)
            # The game web adapter models fields 1–8 and these nested fields.
            # Preserve opaque top-level fields; reject opaque nested data rather than drop it.
            nested = {6: {1,2,3,4,5}, 7: {1,2,3}, 8: {1}}
            if any(f.number in nested and any(x.number not in nested[f.number] for x in parse_fields(f.value)) for f in before):
                raise Fault('该模板有此编辑模式未识别的扩展字段，请用坐标编辑保留源数据。', 422)
            by_coordinate = {(t['x'],t['y'],t['z']): origin for t,origin in zip(current['tiles'],s['origins'])}
            origins = [by_coordinate.get((t['x'],t['y'],t['z'])) for t in board['tiles']]
            raw = b''.join(f.raw for f in after if f.number <= 8) + b''.join(f.raw for f in before if f.number > 8)
            return self.replace(key, s, raw, origins)

    def undo(self, key, revision):
        with self.lock:
            s = self.session(key, revision)
            if not s['history']:
                raise Fault('没有可撤回的修改。')
            s['raw'], s['origins'], s['translation'] = s['history'].pop()
            s['revision'] += 1
            return self.snapshot(key)

    def save(self, key, revision, name, group=None):
        name = identifier(name)
        with self.lock:
            s = self.session(key, revision)
            assert_free(self.repo, name + SUFFIX)
            board = decode(s['raw'])
            if not board['tiles']:
                raise Fault('空关卡不能保存，请先摆牌。', 422)
            raw = rewrite_tiles(s['raw'], board['tiles'], new_id=name)
            invalid = [(kind,count) for kind,count in Counter(t['tile_id'] for t in board['tiles']).items() if count % 3]
            if invalid:
                bad = '、'.join(f'{ {-10:"A",-11:"B"}.get(kind,kind) }：{count} 张' for kind,count in invalid)
                raise Fault('每种 Tile 须三张成组：'+bad+'。当前修改仍保留。',422)
            checked = validate_board(raw)
            if not checked['valid']:
                raise Fault('；'.join(checked['errors']), 422)
            path = create_only(self.output, name + SUFFIX, raw)
            self.saved[self.output, name] = sha256(raw).digest()
            return {'name': name, 'file': path.name, 'warnings': checked['warnings']}

    def sync(self, name):
        name = identifier(name)
        filename = name + SUFFIX
        with self.lock:
            assert_free(self.repo, filename)
            digest = self.saved.get((self.output, name))
            if digest is None:
                raise Fault('请先在编辑器中另存为新模板，再同步到模板仓库。', 409)
            raw = read_regular(self.output / filename)
            if sha256(raw).digest() != digest:
                raise Fault('另存的文件已变化，请换一个新 ID 重新另存。', 409)
            if decode(raw)['template_id'] != name or not validate_board(raw)['valid']:
                raise Fault('输出文件内容与新模板名不一致，或牌数不成组。', 422)
            path = create_only(self.repo, filename, raw)
            return {'name': name, 'file': path.name}
