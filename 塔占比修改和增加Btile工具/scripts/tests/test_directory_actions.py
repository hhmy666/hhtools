import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.tower_reducer.directory_actions import DirectoryActions, DirectoryActionError, open_in_file_manager


class DirectoryActionTests(unittest.TestCase):
    def test_only_issued_success_directory_can_be_opened_repeatedly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            opened = []
            actions = DirectoryActions(opener=opened.append)
            issued = actions.issue([('输出目录', str(root)), ('输出目录', str(root))])
            self.assertEqual(len(issued), 1)
            self.assertEqual(issued[0]['path'], str(root))
            self.assertEqual(opened, [], 'issuing buttons must not launch Finder')
            for _ in range(2):
                actions.open(issued[0]['key'])
            self.assertEqual(opened, [root, root])
            for invalid in ('bogus', str(root), None, {'path': str(root)}):
                with self.assertRaises(DirectoryActionError):
                    actions.open(invalid)
            self.assertEqual(len(opened), 2)

    def test_replaced_symlink_expired_and_missing_directories_never_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            dest = root / 'output'
            dest.mkdir()
            opened = []
            clock = [100.0]
            actions = DirectoryActions(opener=opened.append, clock=lambda: clock[0])
            issued = actions.issue([('输出目录', str(dest))])[0]
            dest.rename(root / 'old')
            dest.symlink_to(root / 'old', target_is_directory=True)
            with self.assertRaises(DirectoryActionError):
                actions.open(issued['key'])
            dest.unlink()
            dest.mkdir()
            with self.assertRaises(DirectoryActionError):
                actions.open(issued['key'])
            issued = actions.issue([('输出目录', str(dest))])[0]
            clock[0] += 86401
            with self.assertRaises(DirectoryActionError):
                actions.open(issued['key'])
            self.assertEqual(opened, [])

    def test_finder_command_is_argument_list_not_shell_and_handles_spaces(self):
        with patch('scripts.tower_reducer.directory_actions.sys.platform', 'darwin'), \
                patch('scripts.tower_reducer.directory_actions.subprocess.run') as run:
            open_in_file_manager(Path('/private/tmp/有 空格; test'))
            run.assert_called_once_with(['/usr/bin/open', '/private/tmp/有 空格; test'],
                                        check=True, timeout=5, stdout=-3, stderr=-3)

    def test_launcher_failure_does_not_consume_retry_or_modify_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            def broken(_path):
                raise OSError('do not leak internal details')
            actions = DirectoryActions(opener=broken)
            issued = actions.issue([('输出目录', str(root))])[0]
            for _ in range(2):
                with self.assertRaises(DirectoryActionError) as caught:
                    actions.open(issued['key'])
                self.assertEqual(caught.exception.code, 'directory_open_failed')
                self.assertNotIn('leak', str(caught.exception))
            self.assertEqual(list(root.iterdir()), [])
