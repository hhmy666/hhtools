"""Task-scoped macOS idle-sleep assertion; never keeps the display awake."""
from functools import wraps
import logging
import os
import subprocess
import sys


def prevent_idle_sleep(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        process = None
        if sys.platform == 'darwin':
            try:
                process = subprocess.Popen(
                    ['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                logging.getLogger(__name__).warning(
                    '无法启用任务防自动睡眠；电脑睡眠期间计算会暂停。')
        try:
            return function(*args, **kwargs)
        finally:
            if process is not None:
                try:
                    try:
                        if process.poll() is None:
                            process.terminate()
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    logging.getLogger(__name__).warning('任务防睡眠进程已退出或无法释放。')
    return guarded
