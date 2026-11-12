"""Bounded discovery and validation of the two local synchronization roots."""

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple, Union


TILE_SUFFIX = Path("tile/Tile/Assets/Resources/TileMatchLevelData")
TEMPLATE_SUFFIX = Path("tileleveltemplate/Template")
_SUFFIXES = (TILE_SUFFIX, TEMPLATE_SUFFIX)
DEFAULT_MAX_DISCOVERY_ENTRIES = 20000


@dataclass(frozen=True)
class DiscoveryCandidates:
    tile_roots: Tuple[Path, ...]
    template_roots: Tuple[Path, ...]
    timed_out: bool = False
    limit_reached: bool = False

    @property
    def requires_confirmation(self) -> bool:
        return True


@dataclass(frozen=True)
class BindingResult:
    valid: bool
    path: Path
    reason: str = ""


def _has_suffix(path: Path, suffix: Path) -> bool:
    return len(path.parts) >= len(suffix.parts) and path.parts[-len(suffix.parts):] == suffix.parts


def _symlink_components(path: Path) -> bool:
    current = path
    while current != current.parent:
        if current.is_symlink():
            return True
        current = current.parent
    return False


def validate_binding(path: Union[str, Path], expected_suffix: Union[str, Path]) -> BindingResult:
    candidate = Path(path).expanduser().absolute()
    suffix = Path(expected_suffix)
    if suffix not in _SUFFIXES:
        return BindingResult(False, candidate, "unsupported_suffix")
    if not _has_suffix(candidate, suffix):
        return BindingResult(False, candidate, "suffix_mismatch")
    if _symlink_components(candidate):
        return BindingResult(False, candidate, "symlink_path")
    if not candidate.is_dir():
        return BindingResult(False, candidate, "missing_directory")
    return BindingResult(True, candidate)


def discover_template_roots(user_root: Union[str, Path], max_depth: int, deadline: float,
                            max_entries: int = DEFAULT_MAX_DISCOVERY_ENTRIES) -> DiscoveryCandidates:
    """Search only beneath user_root; deadline is an absolute monotonic timestamp."""
    root = Path(user_root).expanduser().absolute()
    if max_depth < 0 or max_entries < 1 or not root.is_dir() or _symlink_components(root):
        return DiscoveryCandidates((), ())
    canonical_root = root.resolve()
    tiles, templates = set(), set()

    # The normal team checkout keeps both fixed suffixes directly below the
    # current user's home directory.  Resolve those deterministic candidates
    # before the bounded fallback walk so a large Library or cache tree cannot
    # consume the entry budget first.
    direct_candidates = (
        (TILE_SUFFIX, tiles),
        (TEMPLATE_SUFFIX, templates),
    )
    for suffix, destination in direct_candidates:
        if time.monotonic() >= deadline:
            return DiscoveryCandidates(
                tuple(sorted(tiles)), tuple(sorted(templates)), timed_out=True
            )
        if max_depth >= len(suffix.parts):
            direct = root / suffix
            if validate_binding(direct, suffix).valid:
                destination.add(direct)
    if tiles and templates:
        return DiscoveryCandidates(tuple(sorted(tiles)), tuple(sorted(templates)))

    pending = [(root, 0)]
    timed_out = False
    limit_reached = False
    visited_entries = 0
    while pending:
        if time.monotonic() >= deadline:
            timed_out = True
            break
        directory, depth = pending.pop()
        try:
            if directory.is_symlink() or not directory.resolve().is_relative_to(canonical_root):
                continue
        except OSError:
            continue
        if depth:
            if _has_suffix(directory, TILE_SUFFIX):
                tiles.add(directory)
            if _has_suffix(directory, TEMPLATE_SUFFIX):
                templates.add(directory)
        if depth >= max_depth:
            continue
        try:
            with os.scandir(directory) as entries:
                iterator = iter(entries)
                while True:
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
                    if visited_entries >= max_entries:
                        limit_reached = True
                        break
                    try:
                        entry = next(iterator)
                    except StopIteration:
                        break
                    visited_entries += 1
                    if entry.is_dir(follow_symlinks=False) and not entry.is_symlink():
                        pending.append((Path(entry.path), depth + 1))
        except (OSError, PermissionError):
            continue
        if timed_out or limit_reached:
            break
    return DiscoveryCandidates(tuple(sorted(tiles)), tuple(sorted(templates)), timed_out, limit_reached)
