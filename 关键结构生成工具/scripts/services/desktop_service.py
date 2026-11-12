"""Small, explicit bridges from the loopback app to the local desktop."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from puzzle_core.errors import InputValidationError


def open_directory(path: Path) -> Path:
    """Open an existing directory with the operating system's file browser."""

    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise FileNotFoundError("输出目录不存在") from exc
    if not resolved.is_dir():
        raise InputValidationError("导出记录对应的路径不是目录")

    try:
        if sys.platform == "darwin":
            subprocess.Popen(
                ["open", str(resolved)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        elif os.name == "nt":
            os.startfile(str(resolved))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(
                ["xdg-open", str(resolved)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InputValidationError("当前系统无法打开输出目录") from exc
    return resolved


__all__ = ["open_directory"]
