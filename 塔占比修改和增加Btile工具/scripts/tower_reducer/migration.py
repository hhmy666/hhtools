"""Narrow, idempotent metadata migration after the product directory rename."""
from pathlib import Path

from .settings import load_settings, save_settings
from .storage import atomic_write_bytes, digest, read_regular, safe_directory


def migrate_default_export_path(tool_root, settings_path):
    """Only move the exact legacy default; never reinterpret a custom path.

    Templates, jobs and outputs move with the containing tool directory. Their
    contents remain untouched. Preserve the original settings bytes before the
    one field update, including for older versions missing new default fields.
    """
    root=Path(tool_root)
    path=Path(settings_path)
    if root.name != '塔占比修改工具' or not path.exists():
        return False
    settings=load_settings(path)
    legacy=root.parent/'塔占比减少工具'/'输出模板'
    if settings.load_error or settings.export_root != str(legacy):
        return False
    original=read_regular(path)
    backup_root=safe_directory(path.parent/'迁移备份',create=True)
    backup=backup_root/('settings-'+digest(original)+'.json')
    if backup.exists():
        if read_regular(backup) != original:
            raise ValueError('settings migration backup changed')
    else:
        atomic_write_bytes(backup,original)
    if read_regular(path) != original:
        raise ValueError('settings changed during migration')
    settings.export_root=str(root/'输出模板')
    save_settings(settings,path)
    return True
