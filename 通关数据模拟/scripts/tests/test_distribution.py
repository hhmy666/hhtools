"""The deliverable must run without sibling tools, home configs or preferences."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]


class DistributionTests(unittest.TestCase):
    def test_isolated_copy_can_load_models_and_simulate_1000_styles(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / '独立副本'
            from build_release import build
            archive = build(Path(directory) / 'release.zip')
            with ZipFile(archive) as zipped:
                names = zipped.namelist()
                self.assertFalse(any('运行数据/' in name or '__pycache__' in name for name in names))
                self.assertTrue(any('resources/models/online-player-model.json' in name for name in names))
                zipped.extractall(Path(directory) / 'unpacked')
            shutil.move(str(Path(directory) / 'unpacked' / ROOT.name), package)
            config = Path(directory) / 'Projects' / 'ulinkconfig-team' / 'csv'
            config.mkdir(parents=True)
            fixtures = {
                'config_client_game.csv': 'Key,Value\ntower_help_game_progress_param,100|100|100|100\ntower_help_bag_tile_param,100|100|100|100|100|100|100\n',
                'config_client_level_fuuu_dup.csv': 'LevelFuuuDup,TowerHelpGameProgressParam,TowerHelpBagTileParam\n1,100|100,100|100|100|100|100|100|100\n',
                'config_client_tile.csv': 'TileId\n1001\n',
                'config_client_level_tile.csv': 'Id,StartLevel,TileList1,TileList2\n1,1,1001,1001\n',
                'config_level.csv': 'LevelId,TemplateId\n',
                'config_level_list.csv': 'LevelIndex,LevelId,LevelFuuuDup\n',
            }
            for name, content in fixtures.items():
                (config / name).write_text(content)
            program = r'''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import app, sources, simulation, fuuu
assert sources.default_repository() == ''
assert sources.model_path().is_relative_to(sources.ROOT / 'resources' / 'models')
assert simulation.CONFIG.is_relative_to(Path.home().resolve())
assert simulation.CONFIG.parent.name == 'ulinkconfig-team'
assert fuuu.CONFIG == simulation.CONFIG
model = app.player_model()
assert len(model['player_policies']) >= 1000
engine = simulation.build_engine(model)
from sim_core.protobuf_wire import encode_varint
from sim_core.template_reader import parse_level_bytes
from scenario_inputs import Entry, Parameters
v = lambda k, n: encode_varint(k << 3) + encode_varint(n)
b = lambda k, x: encode_varint((k << 3) | 2) + encode_varint(len(x)) + x
payload = b(2, b'portable') + v(4, 9) + v(5, 4)
for i in range(3):
    payload += b(6, v(1, i) + v(2, 1) + v(3, i * 3) + v(4, 1001))
board = parse_level_bytes(payload, source_name='portable_m.bytes')
result = engine(board, Entry('portable', 1, None), Parameters(1, 0, 100, 0), 1000, 42)
assert result['player_styles'] >= 1000, result
assert .9 < result['naked_win_rate'] <= 1, result
assert result['naked_wins'] / 1000 == result['naked_win_rate']
for name, module in list(sys.modules.items()):
    path = getattr(module, '__file__', None)
    if name.startswith(('puzzle_core', 'sim_core')) and path:
        assert Path(path).resolve().is_relative_to(sources.ROOT), path
print('isolated simulation passed: 1000 styles / 1000 games')
'''
            env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'TILE_SIM_CONFIG')}
            env['HOME'] = directory
            run = subprocess.run([sys.executable, '-I', '-B', '-c', program, str(package / 'scripts')],
                                 env=env, cwd=directory, capture_output=True, text=True, timeout=90)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


class ConfigDiscoveryTests(unittest.TestCase):
    def test_missing_ambiguous_and_explicit_config(self):
        from config_paths import REQUIRED, discover_config, require_config
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            with patch('config_paths.Path.home', return_value=Path(tmp)):
                self.assertIsNone(discover_config())
                with self.assertRaisesRegex(ValueError, 'TILE_SIM_CONFIG'):
                    require_config()
                one = Path(tmp) / 'repos' / 'ulinkconfig-one' / 'csv'
                two = Path(tmp) / 'Projects' / 'ulinkconfig-two' / 'csv'
                for directory in (one, two):
                    directory.mkdir(parents=True)
                    for name in REQUIRED:
                        (directory / name).touch()
                self.assertIsNone(discover_config())
                os.environ['TILE_SIM_CONFIG'] = str(one.parent)
                self.assertEqual(require_config(), one.resolve())
                (one / REQUIRED[0]).unlink()
                with self.assertRaisesRegex(ValueError, REQUIRED[0]):
                    require_config()  # Explicit failure must not fall back to two.


if __name__ == '__main__':
    unittest.main()
