import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import server

class PortablePathTest(unittest.TestCase):
    def test_clean_home_starts_without_project_data(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); (root/'配置').mkdir()
            defaults=root/'配置/目录.json'; defaults.write_text('{"game_editor_data":"","template_repository":""}')
            with patch.object(server,'ROOT',root),patch.object(Path,'home',return_value=root/'home'):
                app=server.create_server(config_path=defaults)
                self.addCleanup(app.server_close)
                self.assertFalse(app.store.repo.is_dir())
                self.assertEqual(app.config.get()['templates'],{})
                self.assertEqual(json.loads(defaults.read_text())['template_repository'],'')
    def test_discovers_current_home_only(self):
        with tempfile.TemporaryDirectory() as raw:
            home=Path(raw); repo=home/'Projects/tileleveltemplate/Template'; repo.mkdir(parents=True)
            game=home/'Projects/tileleveltemplate/WebEditor'; game.mkdir()
            with patch.object(Path,'home',return_value=home):
                self.assertEqual(server.discover_game_data(),game)
                self.assertEqual(server.discover_repository(game),repo.resolve())
