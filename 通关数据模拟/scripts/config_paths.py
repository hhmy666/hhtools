"""Bounded local config discovery; never imports settings from sibling tools."""
import os
from pathlib import Path

REQUIRED = tuple(f'{name}.csv' for name in (
    'config_client_game', 'config_client_level_fuuu_dup', 'config_client_tile',
    'config_client_level_tile', 'config_level', 'config_level_list'))


def normalize_config(path):
    path = Path(path).expanduser().resolve()
    if (path / 'csv').is_dir():
        path /= 'csv'
    return path


def complete(path):
    return all((path / name).is_file() for name in REQUIRED)


def discover_config():
    explicit = os.environ.get('TILE_SIM_CONFIG')
    if explicit:
        return normalize_config(explicit)
    home = Path.home()
    conventional = home / 'ulinkconfig' / 'csv'
    if complete(conventional):
        return conventional
    found = {}
    # Bounded common checkout locations: do not walk the entire disk or .git.
    for base in (home, *(home / name for name in (
            'Projects', 'projects', 'repos', 'Repositories', 'work', 'Workspace',
            'Developer', 'Documents', 'Desktop'))):
        if not base.is_dir():
            continue
        for pattern in ('*ulinkconfig*', '*/*ulinkconfig*'):
            for candidate in base.glob(pattern):
                path = normalize_config(candidate)
                if complete(path):
                    stat = path.stat()
                    found[(stat.st_dev, stat.st_ino)] = path
    return next(iter(found.values())) if len(found) == 1 else None


def require_config(path=None):
    path = normalize_config(path) if path is not None else discover_config()
    if path is None:
        raise ValueError('未发现唯一可用的 ulinkconfig 仓库。请用 TILE_SIM_CONFIG 指定仓库根目录或 csv 目录后启动。')
    missing = [name for name in REQUIRED if not (path / name).is_file()]
    if missing:
        raise ValueError(f'配置目录 {path} 缺少：{"、".join(missing)}。请核对 TILE_SIM_CONFIG 指向的仓库。')
    return path
