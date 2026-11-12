"""Product rename preserves customized paths and byte-for-byte backup."""
import tempfile
import unittest
from pathlib import Path

from scripts.tower_reducer.settings import LocalSettings, load_settings, save_settings


class ModifierMigrationTests(unittest.TestCase):
    def fixture(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        parent=Path(temporary.name).resolve()
        root=parent/'塔占比修改工具'
        settings=root/'运行数据'/'settings.json'
        save_settings(LocalSettings(export_root=str(parent/'塔占比减少工具'/'输出模板')),
                      settings)
        return root, settings

    def test_old_default_is_migrated_only_once_with_exact_original_backup(self):
        from scripts.tower_reducer.migration import migrate_default_export_path
        root, path=self.fixture()
        original=path.read_bytes()
        self.assertTrue(migrate_default_export_path(root,path))
        self.assertEqual(load_settings(path).export_root,str(root/'输出模板'))
        backups=list((path.parent/'迁移备份').glob('settings-*.json'))
        self.assertEqual(len(backups),1)
        self.assertEqual(backups[0].read_bytes(),original)
        self.assertFalse(migrate_default_export_path(root,path))
        self.assertEqual(len(list((path.parent/'迁移备份').glob('*'))),1)

    def test_custom_export_path_is_not_changed(self):
        from scripts.tower_reducer.migration import migrate_default_export_path
        root,path=self.fixture()
        settings=load_settings(path)
        settings.export_root=str(root.parent/'my exports')
        save_settings(settings,path)
        original=path.read_bytes()
        self.assertFalse(migrate_default_export_path(root,path))
        self.assertEqual(path.read_bytes(),original)

    def test_unreadable_settings_are_not_replaced(self):
        from scripts.tower_reducer.migration import migrate_default_export_path
        root,path=self.fixture()
        path.write_bytes(b'{invalid')
        self.assertFalse(migrate_default_export_path(root,path))
        self.assertEqual(path.read_bytes(),b'{invalid')
