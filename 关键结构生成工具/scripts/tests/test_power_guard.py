import unittest
from unittest.mock import patch, MagicMock


class PowerGuardTests(unittest.TestCase):
    def test_cleanup_failure_does_not_discard_completed_result(self):
        import subprocess
        from services.power_guard import prevent_idle_sleep
        process = MagicMock()
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired('caffeinate', 2)
        with patch('services.power_guard.sys.platform', 'darwin'), patch(
                'services.power_guard.subprocess.Popen', return_value=process):
            @prevent_idle_sleep
            def work():
                return 'completed'
            with self.assertLogs('services.power_guard', level='WARNING'):
                self.assertEqual(work(), 'completed')
        process.kill.assert_called_once()

    def test_guard_allows_display_sleep_and_releases_on_error(self):
        from services.power_guard import prevent_idle_sleep
        process = MagicMock()
        process.poll.return_value = None
        with patch('services.power_guard.sys.platform', 'darwin'), patch(
                'services.power_guard.subprocess.Popen', return_value=process) as spawn:
            @prevent_idle_sleep
            def work():
                self.assertFalse(process.terminate.called)
                raise ValueError('failure')
            with self.assertRaisesRegex(ValueError, 'failure'):
                work()
        args = spawn.call_args.args[0]
        self.assertEqual(args[:3], ['/usr/bin/caffeinate', '-i', '-w'])
        self.assertTrue(args[3].isdigit())
        process.terminate.assert_called_once()
        process.wait.assert_called_once()

    def test_other_platform_and_missing_utility_do_not_break_generation(self):
        from services.power_guard import prevent_idle_sleep
        @prevent_idle_sleep
        def work():
            return 'finished'
        with patch('services.power_guard.sys.platform', 'linux'), patch(
                'services.power_guard.subprocess.Popen') as spawn:
            self.assertEqual(work(), 'finished')
            spawn.assert_not_called()
        with patch('services.power_guard.sys.platform', 'darwin'), patch(
                'services.power_guard.subprocess.Popen', side_effect=OSError('unavailable')):
            with self.assertLogs('services.power_guard', level='WARNING'):
                self.assertEqual(work(), 'finished')
