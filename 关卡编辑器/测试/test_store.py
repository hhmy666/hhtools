import tempfile
import unittest
from pathlib import Path
from store import Store, Fault
from codec.engine import analyze_bytes
from codec.repair import _message, _integer, _tile_bytes

def fixture(identifier='Old-1'):
    return (_integer(4, 16) + _integer(5, 18) + _message(2, identifier.encode()) +
            _message(3, b'm') + _integer(91, 27) + b''.join(_message(6, _tile_bytes(
                dict(x=x,y=2,z=0,tile_id=-10))) for x in [2,4,6]))

class StoreTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)/'repo'; self.repo.mkdir()
        self.out = Path(tmp.name)/'out'; self.out.mkdir()
        self.source = self.repo/'Old-1_m.bytes'; self.source.write_bytes(fixture())
        self.app = Store(self.repo, self.out)
    def test_roundtrip_and_exclusive_sync(self):
        info = self.app.load('Old-1')
        tiles = info['board']['tiles']
        tiles[0]['x'] = 3
        info = self.app.edit(info['key'], 0, tiles, info['board']['towers'])
        saved = self.app.save(info['key'], 1, 'New1')
        self.assertEqual(self.source.read_bytes(), fixture())
        out = self.out/'New1_m.bytes'
        self.assertEqual(analyze_bytes(out.read_bytes())['tiles'][0]['x'], 3)
        self.assertEqual(analyze_bytes(out.read_bytes())['template_id'], 'New1')
        self.assertIn(_integer(91, 27), out.read_bytes())
        self.assertFalse((self.repo/out.name).exists())
        self.app.sync('New1')
        self.assertEqual((self.repo/out.name).read_bytes(), out.read_bytes())
        with self.assertRaises(Fault): self.app.sync('new1')
        with self.assertRaises(Fault): self.app.sync('New1')
        with self.assertRaises(Fault): self.app.save(info['key'], 1, 'New1')
    def test_invalid_name_and_stale_edit(self):
        info = self.app.load('Old-1')
        for name in ('bad-name','a.b','a_b','../bad','a b'):
            with self.assertRaises(Fault): self.app.save(info['key'], 0, name)
        with self.assertRaises(Fault): self.app.save(info['key'], 2, 'New1')
        self.assertEqual(list(self.out.iterdir()), [])
    def test_repository_collision_blocks_save_and_late_collision_blocks_sync(self):
        info = self.app.load('Old-1')
        existing = self.repo/'Taken1_m.bytes'
        existing.write_bytes(fixture('Taken1'))
        with self.assertRaisesRegex(Fault, '已存在同名'):
            self.app.save(info['key'], 0, 'taken1')
        self.assertEqual(list(self.out.iterdir()), [])
        self.app.save(info['key'], 0, 'Later1')
        late = self.repo/'Later1_m.bytes'
        late.write_bytes(fixture('Other'))
        with self.assertRaisesRegex(Fault, '已存在同名'):
            self.app.sync('Later1')
        self.assertEqual(late.read_bytes(), fixture('Other'))
        (self.out/'Untracked1_m.bytes').write_bytes(fixture('Untracked1'))
        with self.assertRaisesRegex(Fault, '请先'):
            self.app.sync('Untracked1')
    def test_invalid_group_keeps_draft(self):
        info = self.app.load('Old-1')
        info = self.app.edit(info['key'], 0, info['board']['tiles'][:-1], [])
        with self.assertRaises(Fault): self.app.save(info['key'], 1, 'New1')
        self.assertEqual(len(self.app.snapshot(info['key'])['board']['tiles']), 2)
        self.assertEqual(len(self.app.undo(info['key'], 1)['board']['tiles']), 3)
