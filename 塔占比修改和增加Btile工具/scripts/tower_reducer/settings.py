"""Machine-local defaults; synchronized paths are never taken from request payloads."""

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from .path_binding import TEMPLATE_SUFFIX, TILE_SUFFIX, validate_binding


DEFAULT_SETTINGS_PATH = Path(__file__).resolve().parents[2] / "运行数据" / "settings.json"
_STORED_FIELDS = ("tile_root", "template_root", "tile_confirmed", "template_confirmed",
                  "export_root", "naming_rule", "increase_naming_rule", "default_sync_targets", "config_root")


@dataclass
class LocalSettings:
    tile_root: str = ""
    template_root: str = ""
    tile_confirmed: bool = False
    template_confirmed: bool = False
    export_root: str = str(Path(__file__).resolve().parents[2] / "输出模板")
    naming_rule: str = "lessT{原模板ID}"
    increase_naming_rule: str = "moreT{原模板ID}"
    default_sync_targets: list = field(default_factory=list)
    config_root: str = ""
    load_error: str = field(default="", init=False, repr=False)

    @property
    def sync_ready(self) -> bool:
        return (self.tile_confirmed is True and self.template_confirmed is True
                and validate_binding(self.tile_root, TILE_SUFFIX).valid
                and validate_binding(self.template_root, TEMPLATE_SUFFIX).valid)


def _validate_fields(settings: LocalSettings) -> None:
    targets = settings.default_sync_targets
    if (type(targets) is not list or any(type(t) is not str or t not in ('tile', 'template') for t in targets)
            or len(set(targets)) != len(targets)):
        raise ValueError('default_sync_targets must contain unique tile/template choices')
    for name in ("tile_root", "template_root", "export_root", "naming_rule", "increase_naming_rule", "config_root"):
        if type(getattr(settings, name)) is not str:
            raise ValueError("{} must be a string".format(name))
    for name in ("tile_confirmed", "template_confirmed"):
        if type(getattr(settings, name)) is not bool:
            raise ValueError("{} must be a JSON boolean".format(name))
    for name in ("tile_root", "template_root", "export_root", "config_root"):
        value = getattr(settings, name)
        if value and not Path(value).is_absolute():
            raise ValueError("{} must be an absolute path".format(name))


def _ensure_no_symlink(path: Path) -> None:
    current = path
    while current != current.parent:
        if current.is_symlink():
            raise ValueError("settings path traverses a symbolic link")
        current = current.parent


def load_settings(settings_path: Optional[Union[str, Path]] = None) -> LocalSettings:
    path = Path(settings_path) if settings_path is not None else DEFAULT_SETTINGS_PATH
    try:
        _ensure_no_symlink(path)
        if not path.exists():
            return LocalSettings()
        stored = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(stored, dict):
            raise ValueError("settings must be a JSON object")
        unknown = set(stored) - set(_STORED_FIELDS)
        if unknown:
            raise ValueError("unknown settings fields: {}".format(", ".join(sorted(unknown))))
        settings = LocalSettings(**stored)
        _validate_fields(settings)
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        settings = LocalSettings()
        settings.load_error = "无法读取本机设置：{}".format(error)
        return settings
    if settings.tile_confirmed and not validate_binding(settings.tile_root, TILE_SUFFIX).valid:
        settings.tile_confirmed = False
    if settings.template_confirmed and not validate_binding(settings.template_root, TEMPLATE_SUFFIX).valid:
        settings.template_confirmed = False
    return settings


def save_settings(settings: LocalSettings, settings_path: Optional[Union[str, Path]] = None) -> None:
    path = Path(settings_path) if settings_path is not None else DEFAULT_SETTINGS_PATH
    _validate_fields(settings)
    if settings.load_error:
        raise ValueError("unresolved settings load error; explicit recovery required")
    _ensure_no_symlink(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if settings.tile_confirmed and not validate_binding(settings.tile_root, TILE_SUFFIX).valid:
        raise ValueError("confirmed tile binding is invalid")
    if settings.template_confirmed and not validate_binding(settings.template_root, TEMPLATE_SUFFIX).valid:
        raise ValueError("confirmed template binding is invalid")
    encoded = (json.dumps({name: getattr(settings, name) for name in _STORED_FIELDS},
                          ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".settings-", suffix=".tmp", dir=path.parent,
                                         delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        _ensure_no_symlink(path)
        os.replace(temporary, path)
        temporary = None
        parent_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
