from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import NamedTemporaryFile
from threading import RLock, Timer
from urllib.parse import parse_qs, unquote, urlsplit
import webbrowser

from store import Store, Fault, MAX_BYTES, decode, identifier
from level_data_store import TABLES, parse_csv_bundle, sync_csv_bundle, validate_store, new_store

ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_SUFFIX = Path('tileleveltemplate/Template')


def discover_game_data():
    home = Path.home()
    roots = (home, home/'Projects', home/'Documents', home/'Desktop', ROOT.parent)
    return next((base/'tileleveltemplate/WebEditor' for base in roots
                 if (base/'tileleveltemplate/WebEditor').is_dir()), None)


def discover_repository(game):
    home = Path.home()
    candidates = [game.parent / 'Template', home / REPOSITORY_SUFFIX]
    candidates.extend(parent / REPOSITORY_SUFFIX for parent in ROOT.parents)
    candidates.extend(home / folder / REPOSITORY_SUFFIX
                      for folder in ('Documents', 'Desktop', 'Downloads', 'Projects', 'Developer', 'Work', '07data'))
    return next((path.resolve() for path in candidates if path.is_dir()), None)


def save_directory_settings(path, settings):
    with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(settings, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def choose_repository_folder(current, prompt="选择模板仓库目录"):
    if sys.platform != 'darwin':
        raise Fault('当前系统无法打开目录选择器，请手写或粘贴目录路径。')
    script = '''on run argv
try
    set startFolder to (POSIX file (item 1 of argv)) as alias
    set selectedFolder to choose folder with prompt (item 2 of argv) default location startFolder
    return POSIX path of selectedFolder
on error number -128
    return ""
end try
end run'''
    try:
        result = subprocess.run(['/usr/bin/osascript', '-e', script, str(current), prompt],
                                capture_output=True, text=True, timeout=300, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise Fault('未能打开系统目录选择器，请手写或粘贴目录路径。') from error
    return result.stdout.rstrip('\n')


def existing_directory(value, label):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise Fault(f'请输入{label}的完整目录路径。')
    folder = Path(value.strip()).expanduser()
    if not folder.is_absolute() or not folder.is_dir():
        raise Fault(f'{label}不存在，请检查路径或重新选取目录。')
    return folder.resolve()


class LocalConfig:
    def __init__(self, source, local):
        self.source, self.local = source, local
        self.lock = RLock()
        self.data = None

    def get(self):
        with self.lock:
            if self.data is None:
                path = self.local if self.local.exists() else self.source
                self.data = (json.loads(path.read_bytes()) if path.is_file() else
                             new_store(datetime.now(timezone.utc).isoformat(), '本机编辑器'))
            return self.data

    def change(self, body, importing):
        from copy import deepcopy
        with self.lock:
            old = self.get()
            if body.get('baseRevision') != old['revision']:
                raise Fault('本地配置已变化，请刷新后重试。', 409)
            now = datetime.now(timezone.utc).isoformat()
            actor = '本机编辑器'
            report = None
            if importing:
                candidate, report = sync_csv_bundle(old, parse_csv_bundle(body['files']), actor, now)
            else:
                candidate = deepcopy(old)
                for op in body['operations']:
                    collection, key = op['entity'], op['key']
                    if collection not in {'templates', *(t[0] for t in TABLES.values())}:
                        raise Fault('不支持的配置类型。')
                    rows = candidate[collection]
                    if op['op'] == 'add' and key not in rows:
                        if collection == 'templates':
                            rows[key] = dict(createdAt=now, createdBy=actor, stats=None,
                                             bytes=dict(fileName=key+'_m.bytes', exists=False, size=0, sha256=None, modifiedAt=None))
                        else:
                            rows[key] = dict(createdAt=now, createdBy=actor, sourceStatus='active', managedBy='json',
                                             sourceFile='', firstSeenAt=now, lastSeenAt=now)
                    elif op['op'] != 'update' or key not in rows:
                        raise Fault('配置已存在或已变更。', 409)
                    rows[key].update(op.get('values', {}), updatedAt=now, updatedBy=actor)
                candidate.update(revision=old['revision']+1, updatedAt=now, updatedBy=actor)
                validate_store(candidate)
            self.local.parent.mkdir(parents=True, exist_ok=True)
            temp = self.local.with_suffix('.tmp')
            temp.write_text(json.dumps(candidate, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
            os.replace(temp, self.local)
            self.data = candidate
            return dict(ok=True, revision=candidate['revision'], **({'report':report} if report else {}))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, content, kind, status=200, **headers):
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        for k,v in headers.items():
            self.send_header(k.replace('_','-'), v)
        self.end_headers()
        self.wfile.write(content)

    def json(self, data, status=200):
        self.send(json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode(), 'application/json; charset=utf-8', status)

    def body(self, limit):
        size = int(self.headers.get('Content-Length', '0'))
        if not 0 < size <= limit:
            raise Fault('请求为空或超过大小限制。', 413)
        return self.rfile.read(size)

    def do_GET(self):
        self.route('GET')

    def do_POST(self):
        self.route('POST')

    def do_PATCH(self):
        self.route('PATCH')

    def route(self, method):
        try:
            host = f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != host:
                raise Fault('请通过本机启动器打开。', 403)
            path = unquote(urlsplit(self.path).path)
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            arg = lambda key: query.get(key, [''])[0]
            store = self.server.store
            if method != 'GET':
                if (self.headers.get('Origin') != 'http://' + host or
                        (self.headers.get('X-Level-Editor') != 'local' and path != '/api/level-data' and path != '/api/level-data/import-csv')):
                    raise Fault('请求必须来自本机关卡编辑器。', 403)
            if method == 'GET' and path.startswith('/templates/'):
                name = path.removeprefix('/templates/')
                if not name.endswith('_m.bytes') or '/' in name:
                    raise Fault('模板名无效。')
                if arg("preview") == "1":
                    raw = store.read_template(name)
                    decode(raw)
                    return self.send(raw, "application/octet-stream")
                info = store.load(name)
                return self.send(store.session(info['key'])['raw'], 'application/octet-stream', X_Source=info['key'])
            if method == 'GET' and path == '/api/templates':
                ids = sorted(p.name[:-8] for p in store.repo.glob('*_m.bytes') if p.is_file() and not p.is_symlink())
                return self.json({'ids':ids})
            if method == 'GET' and path == '/api/status':
                return self.json({'repository':str(store.repo) if store.repo.is_dir() else '',
                                  'output':str(store.output), 'ready':store.repo.is_dir(),
                                  'game':str(self.server.game) if self.server.game.is_dir() else ''})
            if method == 'POST' and path == '/api/repository/pick':
                target = arg('target') if arg('target') in {'output', 'game'} else 'repository'
                current = {'output':store.output, 'game':self.server.game, 'repository':store.repo}[target]
                prompt = {'output':'选择输出模板目录', 'game':'选择 WebEditor 资源目录', 'repository':'选择模板仓库目录'}[target]
                return self.json({target: choose_repository_folder(current if current.is_dir() else Path.home(), prompt)})
            if method == 'POST' and path == '/api/repository':
                body = json.loads(self.body(32 * 1024))
                with store.lock:
                    repository = existing_directory(body['repository'], '模板仓库目录') if body.get('repository') else store.repo
                    game = existing_directory(body['game'], 'WebEditor 资源目录') if body.get('game') else self.server.game
                    output = existing_directory(body.get('output', str(store.output)), '输出模板目录')
                    if repository.is_relative_to(output) or output.is_relative_to(repository):
                        raise Fault('模板仓库与「输出模板」不能使用相同或互相包含的目录。')
                    changed = output != store.output
                    settings = {**self.server.directory_settings, 'template_repository':str(repository) if repository.is_dir() else '',
                                'template_output':str(output), 'game_editor_data':str(game) if game.is_dir() else ''}
                    save_directory_settings(self.server.directory_settings_path, settings)
                    self.server.directory_settings = settings
                    store.repo, store.output = repository, output
                    self.server.game = game
                    self.server.config.source = game/'level_data.json'
                    self.server.config.data = None
                return self.json({'repository':str(repository) if repository.is_dir() else '', 'output':str(output),'ready':repository.is_dir(),'outputChanged':changed,'game':str(game) if game.is_dir() else ''})
            if path == '/api/level-data':
                if method == 'GET':
                    return self.json(self.server.config.get())
                if method == 'PATCH':
                    return self.json(self.server.config.change(json.loads(self.body(16*1024**2)),False))
            if method == 'POST' and path == '/api/template-screenshot/open-folder':
                directory = self.server.screenshot_output
                if not directory.is_dir():
                    raise Fault('截图文件夹尚未创建，请先生成截图。', 404)
                try:
                    subprocess.run(['/usr/bin/open', str(directory.resolve())], check=True, timeout=10)
                except (OSError, subprocess.SubprocessError) as error:
                    raise Fault('无法在 Finder 中打开截图文件夹。', 500) from error
                return self.json({'ok': True})
            if method == 'POST' and path == '/api/template-screenshot':
                name = arg('name')
                if not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9._-]{0,171}(?:_\d{1,3}\|-?\d{1,10}\|\d{1,3}\|\d{1,3})?', name):
                    raise Fault('截图文件名无效。')
                png = self.body(8 * 1024**2)
                if not png.startswith(b'\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR') or len(png) < 45:
                    raise Fault('截图必须是 PNG 文件。')
                directory = self.server.screenshot_output
                directory.mkdir(parents=True, exist_ok=True)
                filename = f'{name}.png'
                with NamedTemporaryFile(dir=directory, prefix='.screenshot-', delete=False) as stream:
                    temporary = Path(stream.name)
                    try:
                        stream.write(png)
                        stream.flush()
                        os.replace(temporary, directory / filename)
                    finally:
                        temporary.unlink(missing_ok=True)
                return self.json({'file': filename, 'directory': str(directory)})
            if method == 'POST' and path == '/api/level-data/import-csv':
                return self.json(self.server.config.change(json.loads(self.body(16*1024**2)),True))
            if method == 'POST' and path == '/api/upload':
                raw = self.body(MAX_BYTES)
                if arg('preview') == '1':
                    decode(raw)
                    return self.json({'ok': True})
                return self.json(store.adopt(raw))
            if method == 'POST' and path == '/api/save':
                raw = self.body(MAX_BYTES)
                name = identifier(arg('name'))
                source = arg('source')
                with store.lock:
                    if source:
                        s = store.session(source)
                        if s['raw'] == raw:
                            key, revision = source, s['revision']
                        else:
                            # Work on a transient copy: failed saves must not change the loaded source.
                            tmp = store.adopt(s['raw'])
                            key = tmp['key']
                            revision = store.advanced(key, 0, raw)['revision']
                    else:
                        key, revision = store.adopt(raw)['key'], 0
                    return self.json(store.save(key, revision, name))
            if method == 'POST' and path == '/api/sync':
                return self.json(store.sync(arg('name')))
            if method == 'GET':
                if path == '/api/tile-icons':
                    directory = (self.server.game / 'icon').resolve()
                    icons = sorted(file.stem for file in directory.glob('*.png')
                                   if file.is_file() and file.resolve().is_relative_to(directory)
                                   and re.fullmatch(r'[\w. -]+', file.stem))
                    return self.json({'icons': icons})
                if path == '/':
                    candidate, parent = ROOT/'前端/index.html', ROOT/'前端'
                elif re.fullmatch(r'/(?:assets|modules)/[\w./-]+', path):
                    parent = ROOT/'前端'
                    candidate = parent / path[1:].replace('modules/', '模块/', 1)
                elif path == '/csv/config_client_tile.csv' and not (self.server.game/'csv/config_client_tile.csv').is_file():
                    catalog = 'TileId,Des,IconType,Board,Icon,ColorTag,ThemeTag\nint,string,int,string,string,string,string\n,,,,,,\n'
                    catalog += ''.join(f'{i},图案{i-1000},0,,,,\n' for i in range(1001,1031))
                    return self.send(catalog.encode(), 'text/csv; charset=utf-8')
                elif re.fullmatch(r'/(?:csv|icon|image)/[\w. -]+',path):
                    parent = self.server.game
                    candidate = parent / path[1:]
                else:
                    raise Fault('没有这个页面。', 404)
                if not candidate.resolve().is_relative_to(parent.resolve()) or not candidate.is_file():
                    raise Fault('没有这个文件。', 404)
                return self.send(candidate.read_bytes(), mimetypes.guess_type(candidate.name)[0] or 'application/octet-stream')
            raise Fault('没有这个操作。', 404)
        except Fault as error:
            self.json({'ok':False,'error':str(error)},error.status)
        except (ValueError, KeyError, TypeError) as error:
            self.json({'ok':False,'error':'请求或配置无效：'+str(error)},400)
        except OSError:
            self.json({'ok':False,'error':'本机文件无法访问，请检查配置路径和权限。'},500)


def create_server(*, port=0, config_path=ROOT/'配置/目录.json', repo=None, output=None, screenshot_output=None):
    config_path = Path(config_path)
    defaults = json.loads(config_path.read_text(encoding='utf-8'))
    # Tracked defaults contain no personal paths; local choices stay in ignored runtime data.
    settings_path = ROOT/'运行数据/目录.json' if config_path == ROOT/'配置/目录.json' else config_path
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings = {**defaults, **(json.loads(settings_path.read_text(encoding='utf-8'))
                              if settings_path != config_path and settings_path.is_file() else {})}
    saved_game = Path(settings['game_editor_data']).expanduser() if settings.get('game_editor_data') else None
    game = saved_game if saved_game and saved_game.is_dir() else (discover_game_data() or ROOT/'资源')
    configured = Path(settings['template_repository']).expanduser() if settings.get('template_repository') else None
    if repo is None:
        repo = configured if configured and configured.is_dir() else discover_repository(game)
    settings.update(game_editor_data=str(game) if game.is_dir() else '',
                    template_repository=str(repo) if repo else '')
    save_directory_settings(settings_path, settings)
    server = ThreadingHTTPServer(('127.0.0.1',port), Handler)
    output = output or settings.get('template_output') or ROOT/'输出模板'
    server.store = Store(repo or configured or Path.home() / REPOSITORY_SUFFIX, Path(output).expanduser())
    server.screenshot_output = Path(screenshot_output) if screenshot_output else ROOT/'模板截图'
    server.directory_settings, server.directory_settings_path = settings, settings_path
    server.game = game
    server.config = LocalConfig(game/'level_data.json', ROOT/'运行数据/关卡配置.json')
    return server


if __name__ == '__main__':
    with create_server(port=int(os.environ.get('LEVEL_EDITOR_PORT', '0'))) as server:
        url = f'http://127.0.0.1:{server.server_port}/'
        print('关卡编辑器：'+url+'（关闭终端可停止工具）',flush=True)
        if '--no-open' not in sys.argv:
            Timer(0.3, lambda: webbrowser.open(url)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
