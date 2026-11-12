from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from services.desktop_service import open_directory  # noqa: E402


class DesktopServiceTests(unittest.TestCase):
    def test_macos_opener_uses_argument_list_without_shell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch("services.desktop_service.sys.platform", "darwin"), patch(
                "services.desktop_service.subprocess.Popen"
            ) as popen:
                opened = open_directory(directory)

        self.assertEqual(opened, directory.resolve())
        popen.assert_called_once_with(
            ["open", str(directory.resolve())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def test_missing_directory_is_rejected_before_opening(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing"
            with patch("services.desktop_service.subprocess.Popen") as popen:
                with self.assertRaises(FileNotFoundError):
                    open_directory(missing)
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
