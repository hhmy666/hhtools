"""Persistent per-user Profile settings for first-run and later reuse."""

from __future__ import annotations

import json
import os
import platform
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from puzzle_core.errors import InputValidationError

from .path_service import (
    inspect_path,
    validate_output_root,
    validate_source_roots,
)


SETTINGS_SCHEMA_VERSION = 1
APP_DIR_NAME = "关键结构生成工具"


@dataclass(frozen=True)
class Profile:
    profile_id: str
    name: str
    tile_repo_root: str | None
    template_dir: str | None
    output_root: str
    ulinkconfig_root: str | None = None

    @property
    def source_configured(self) -> bool:
        return bool(self.tile_repo_root and self.template_dir)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_app_data_dir() -> Path:
    override = os.environ.get("AI_PUZZLE_APP_DATA")
    if override:
        return Path(override).expanduser().resolve(strict=False)
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    if system == "Windows":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
        return base / APP_DIR_NAME
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "ai-puzzle-tool"


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


class ProfileService:
    def __init__(self, settings_path: Path | None = None) -> None:
        app_dir = default_app_data_dir()
        self.settings_path = settings_path or app_dir / "settings.json"

    def _empty(self) -> dict[str, Any]:
        return {
            "schema_version": SETTINGS_SCHEMA_VERSION,
            "active_profile_id": None,
            "profiles": {},
        }

    def load(self) -> dict[str, Any]:
        if not self.settings_path.exists():
            return self._empty()
        try:
            raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InputValidationError(
                "Profile 配置无法读取", evidence={"path": str(self.settings_path)}
            ) from exc
        if raw.get("schema_version") != SETTINGS_SCHEMA_VERSION:
            raise InputValidationError(
                "Profile 配置版本不受支持",
                evidence={"schema_version": raw.get("schema_version")},
            )
        if not isinstance(raw.get("profiles"), dict):
            raise InputValidationError("Profile 配置格式错误")
        return raw

    def _save(self, settings: dict[str, Any]) -> None:
        _atomic_json_write(self.settings_path, settings)

    def initialize(self, tool_root: Path, *, name: str = "默认环境") -> Profile:
        settings = self.load()
        active = settings.get("active_profile_id")
        if active and active in settings["profiles"]:
            return Profile(**settings["profiles"][active])
        default_output = tool_root.resolve(strict=False) / "输出关卡模板"
        validate_output_root(default_output, create=True)
        profile = Profile(
            profile_id=f"profile_{uuid.uuid4().hex}",
            name=name,
            tile_repo_root=None,
            template_dir=None,
            output_root=str(default_output),
            ulinkconfig_root=None,
        )
        settings["profiles"][profile.profile_id] = profile.as_dict()
        settings["active_profile_id"] = profile.profile_id
        self._save(settings)
        return profile

    def active_profile(self) -> Profile | None:
        settings = self.load()
        active = settings.get("active_profile_id")
        value = settings["profiles"].get(active)
        return Profile(**value) if value else None

    def get(self, profile_id: str) -> Profile:
        settings = self.load()
        try:
            return Profile(**settings["profiles"][profile_id])
        except KeyError as exc:
            raise InputValidationError(
                "Profile 不存在", evidence={"profile_id": profile_id}
            ) from exc

    def list_profiles(self) -> tuple[Profile, ...]:
        settings = self.load()
        return tuple(Profile(**value) for value in settings["profiles"].values())

    def activate(self, profile_id: str) -> Profile:
        settings = self.load()
        if profile_id not in settings["profiles"]:
            raise InputValidationError("Profile 不存在")
        settings["active_profile_id"] = profile_id
        self._save(settings)
        return Profile(**settings["profiles"][profile_id])

    def create(self, name: str, output_root: Path) -> Profile:
        clean_name = str(name).strip()
        if not clean_name:
            raise InputValidationError("Profile 名称不能为空")
        output = validate_output_root(output_root, create=True)
        settings = self.load()
        profile = Profile(
            profile_id=f"profile_{uuid.uuid4().hex}",
            name=clean_name,
            tile_repo_root=None,
            template_dir=None,
            output_root=str(output),
            ulinkconfig_root=None,
        )
        settings["profiles"][profile.profile_id] = profile.as_dict()
        settings["active_profile_id"] = profile.profile_id
        self._save(settings)
        return profile

    def configure_source(
        self,
        profile_id: str,
        tile_repo_root: Path,
        template_dir: Path | None = None,
    ) -> Profile:
        current = self.get(profile_id)
        repo, templates = validate_source_roots(tile_repo_root, template_dir)
        validate_output_root(
            current.output_root, tile_repo_root=repo, template_dir=templates
        )
        changed = Profile(
            profile_id=current.profile_id,
            name=current.name,
            tile_repo_root=str(repo),
            template_dir=str(templates),
            output_root=current.output_root,
            ulinkconfig_root=current.ulinkconfig_root,
        )
        self._replace(changed)
        return changed

    def configure_output(self, profile_id: str, output_root: Path) -> Profile:
        current = self.get(profile_id)
        output = validate_output_root(
            output_root,
            tile_repo_root=current.tile_repo_root,
            template_dir=current.template_dir,
            create=True,
        )
        changed = Profile(
            profile_id=current.profile_id,
            name=current.name,
            tile_repo_root=current.tile_repo_root,
            template_dir=current.template_dir,
            output_root=str(output),
            ulinkconfig_root=current.ulinkconfig_root,
        )
        self._replace(changed)
        return changed

    def configure_ulinkconfig(self, profile_id: str, ulinkconfig_root: Path) -> Profile:
        """Persist one read-only ulinkconfig root for parameter recommendations."""

        current = self.get(profile_id)
        root = ulinkconfig_root.expanduser().resolve(strict=True)
        if not root.is_dir() or not os.access(root, os.R_OK):
            raise InputValidationError(
                "ulinkconfig 目录不存在或不可读",
                evidence={"ulinkconfig_root": str(root)},
            )
        changed = Profile(
            profile_id=current.profile_id,
            name=current.name,
            tile_repo_root=current.tile_repo_root,
            template_dir=current.template_dir,
            output_root=current.output_root,
            ulinkconfig_root=str(root),
        )
        self._replace(changed)
        return changed

    def _replace(self, profile: Profile) -> None:
        settings = self.load()
        if profile.profile_id not in settings["profiles"]:
            raise InputValidationError("Profile 不存在")
        settings["profiles"][profile.profile_id] = profile.as_dict()
        self._save(settings)

    def status(self, profile_id: str) -> dict[str, Any]:
        profile = self.get(profile_id)
        return {
            "profile": profile.as_dict(),
            "source_required_for_template_id": not profile.source_configured,
            "tile_repo_root": inspect_path(profile.tile_repo_root).as_dict(),
            "template_dir": inspect_path(profile.template_dir).as_dict(),
            "output_root": inspect_path(profile.output_root).as_dict(),
            "ulinkconfig_root": inspect_path(profile.ulinkconfig_root).as_dict(),
        }
