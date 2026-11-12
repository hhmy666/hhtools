"""Session-scoped capabilities for opening completed delivery directories."""

import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import threading
import time


class DirectoryActionError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def open_in_file_manager(path):
    if sys.platform == 'darwin':
        command = ['/usr/bin/open', str(path)]
    elif sys.platform == 'win32':
        os.startfile(str(path))
        return
    else:
        command = ['xdg-open', str(path)]
    subprocess.run(command, check=True, timeout=5,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _identity(path):
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('invalid directory')
    for component in (path,) + tuple(path.parents):
        if stat.S_ISLNK(component.lstat().st_mode):
            raise ValueError('symlink directory')
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError('not directory')
    return info.st_dev, info.st_ino


class DirectoryActions:
    def __init__(self, *, opener=None, clock=None):
        self.opener = opener or open_in_file_manager
        self.clock = clock or time.time
        self._entries = {}
        self._lock = threading.Lock()

    def issue(self, directories):
        """Called only with server-owned paths after a proven delivery result."""
        result, seen = [], set()
        with self._lock:
            now = self.clock()
            self._entries = {k: v for k, v in self._entries.items() if v['expires'] > now}
            for label, value in directories:
                if value in seen:
                    continue
                seen.add(value)
                path = Path(value)
                try:
                    identity = _identity(path)
                except (OSError, ValueError):
                    continue
                key = secrets.token_urlsafe(32)
                while len(self._entries) >= 256:
                    self._entries.pop(next(iter(self._entries)))
                self._entries[key] = {'path': path, 'identity': identity, 'expires': now + 86400}
                result.append({'key': key, 'label': label, 'path': str(path)})
        return result

    def open(self, key):
        with self._lock:
            entry = self._entries.get(key) if type(key) is str else None
            if entry is None or entry['expires'] <= self.clock():
                raise DirectoryActionError('directory_action_expired', '打开目录凭据已失效，请根据页面路径手动打开目录。')
            path = entry['path']
        try:
            if _identity(path) != entry['identity']:
                raise ValueError('directory changed')
        except (OSError, ValueError):
            raise DirectoryActionError('directory_changed', '目录已移动、删除或替换，请核对弹窗中的路径。')
        try:
            self.opener(path)
        except Exception:
            raise DirectoryActionError('directory_open_failed', '无法打开目录，请检查目录权限后重试，或根据路径手动打开。')
        return {'opened': True}
