"""Immutable backend snapshots for direct-file review and later export.

Direct input paths are user-interface handles, not stable source identities.
This store copies only the reviewed source bytes into application data, binds
them to the request configuration hash, and exposes an opaque set ID.  Formal
output directories and read-only Tile roots are explicitly excluded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from puzzle_core.errors import InputValidationError, SourceParseError
from puzzle_core.limits import MAX_BATCH_TEMPLATES
from puzzle_core.planning import canonical_json
from puzzle_core.template_reader import TemplateParseError, parse_level_bytes

from .import_service import LoadedTemplate, MAX_SOURCE_BYTES, load_template_file
from .path_service import is_within, real_path
from .profile_service import Profile


SNAPSHOT_FORMAT_VERSION = 1
SNAPSHOT_DIRECTORY = "source-snapshots"
MAX_SNAPSHOT_SET_BYTES = 512 * 1024 * 1024
MAX_SNAPSHOT_MANIFEST_BYTES = 1024 * 1024

_SNAPSHOT_ID = re.compile(r"^snapshot_[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class SourceSnapshotSet:
    snapshot_set_id: str
    sources: Mapping[str, LoadedTemplate]
    batch_seed: int
    source_config_hash: str
    normalized_config_json: str


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_exclusive(path: Path, data: bytes) -> None:
    descriptor = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def _duplicate_rejecting_object(
    pairs: Sequence[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InputValidationError("源快照清单包含重复字段")
        result[key] = value
    return result


class SourceSnapshotStore:
    """Content-verifying store rooted outside source and formal output trees."""

    def __init__(self, app_data_dir: Path) -> None:
        self.root = real_path(app_data_dir) / SNAPSHOT_DIRECTORY

    def _validate_location(self, profile: Profile) -> Path:
        root = real_path(self.root)
        protected = (
            profile.tile_repo_root,
            profile.template_dir,
            profile.output_root,
        )
        for value in protected:
            if value is not None and is_within(root, real_path(value)):
                raise InputValidationError(
                    "源快照目录不得位于只读源或正式输出目录内"
                )
        return root

    def create(
        self,
        file_inputs: Mapping[str, Path],
        *,
        batch_seed: int,
        source_config_hash: str,
        normalized_config_json: str,
        profile: Profile,
    ) -> SourceSnapshotSet:
        if not file_inputs:
            raise InputValidationError("源快照集至少需要一个直接文件")
        if len(file_inputs) > MAX_BATCH_TEMPLATES:
            raise InputValidationError(
                "源快照文件数超过批次模板上限",
                evidence={"maximum": MAX_BATCH_TEMPLATES},
            )

        loaded: dict[str, LoadedTemplate] = {}
        total_bytes = 0
        for template_key, path in sorted(file_inputs.items()):
            source = load_template_file(path, expected_template_id=template_key)
            total_bytes += len(source.level.data)
            if total_bytes > MAX_SNAPSHOT_SET_BYTES:
                raise InputValidationError(
                    "单个源快照集超过安全容量上限",
                    evidence={"maximum_bytes": MAX_SNAPSHOT_SET_BYTES},
                )
            loaded[template_key] = source

        root = self._validate_location(profile)
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise InputValidationError("源快照根目录不可用")

        snapshot_id = f"snapshot_{uuid.uuid4().hex}"
        temporary = Path(tempfile.mkdtemp(prefix=".creating-", dir=str(root)))
        final = root / snapshot_id
        try:
            written_hashes: set[str] = set()
            entries = []
            for template_key, source in sorted(loaded.items()):
                digest = source.level.sha256
                if digest not in written_hashes:
                    _write_exclusive(temporary / f"{digest}_m.bytes", source.level.data)
                    written_hashes.add(digest)
                entries.append(
                    {
                        "template_key": template_key,
                        "template_id": source.level.template_id,
                        "source_filename": source.source_filename,
                        "sha256": digest,
                        "byte_size": len(source.level.data),
                    }
                )
            manifest = {
                "format_version": SNAPSHOT_FORMAT_VERSION,
                "snapshot_set_id": snapshot_id,
                "batch_seed": batch_seed,
                "source_config_hash": source_config_hash,
                "normalized_config_sha256": _sha256(
                    normalized_config_json.encode("utf-8")
                ),
                "sources": entries,
            }
            if _sha256(normalized_config_json.encode("utf-8")) != source_config_hash:
                raise InputValidationError("规范化配置与配置哈希不一致")
            _write_exclusive(
                temporary / "normalized-config.json",
                normalized_config_json.encode("utf-8"),
            )
            _write_exclusive(
                temporary / "manifest.json",
                canonical_json(manifest).encode("utf-8"),
            )
            os.replace(temporary, final)
            directory_fd = os.open(str(root), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return SourceSnapshotSet(
            snapshot_id,
            loaded,
            batch_seed,
            source_config_hash,
            normalized_config_json,
        )

    def load(
        self,
        snapshot_set_id: str,
        *,
        expected_source_config_hash: str | None = None,
        profile: Profile,
    ) -> SourceSnapshotSet:
        if not isinstance(snapshot_set_id, str) or not _SNAPSHOT_ID.fullmatch(
            snapshot_set_id
        ):
            raise InputValidationError("源快照 ID 格式不合法")
        root = self._validate_location(profile)
        directory = root / snapshot_set_id
        manifest_path = directory / "manifest.json"
        if directory.is_symlink() or manifest_path.is_symlink():
            raise InputValidationError("源快照不得使用符号链接")
        try:
            manifest_bytes = manifest_path.read_bytes()
        except OSError as exc:
            raise InputValidationError(
                "源快照不存在或不可读",
                evidence={"snapshot_set_id": snapshot_set_id},
            ) from exc
        if not manifest_bytes or len(manifest_bytes) > MAX_SNAPSHOT_MANIFEST_BYTES:
            raise InputValidationError("源快照清单大小不在安全范围内")
        try:
            manifest = json.loads(
                manifest_bytes.decode("utf-8"),
                object_pairs_hook=_duplicate_rejecting_object,
                parse_constant=lambda _token: (_ for _ in ()).throw(
                    InputValidationError("源快照清单包含非法数字")
                ),
            )
        except InputValidationError:
            raise
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise InputValidationError("源快照清单无法解析") from exc
        if not isinstance(manifest, dict) or set(manifest) != {
            "format_version",
            "snapshot_set_id",
            "batch_seed",
            "source_config_hash",
            "normalized_config_sha256",
            "sources",
        }:
            raise InputValidationError("源快照清单字段不完整或含未知字段")
        if manifest["format_version"] != SNAPSHOT_FORMAT_VERSION:
            raise InputValidationError("源快照版本不受支持")
        if manifest["snapshot_set_id"] != snapshot_set_id:
            raise InputValidationError("源快照 ID 与清单不一致")
        batch_seed = manifest["batch_seed"]
        source_config_hash = manifest["source_config_hash"]
        normalized_config_sha256 = manifest["normalized_config_sha256"]
        if (
            isinstance(batch_seed, bool)
            or not isinstance(batch_seed, int)
            or batch_seed < 0
            or batch_seed > 2**64 - 1
            or not isinstance(source_config_hash, str)
            or not _SHA256.fullmatch(source_config_hash)
            or not isinstance(normalized_config_sha256, str)
            or not _SHA256.fullmatch(normalized_config_sha256)
        ):
            raise InputValidationError("源快照的锁定配置身份不合法")
        if (
            expected_source_config_hash is not None
            and source_config_hash != expected_source_config_hash
        ):
            raise InputValidationError("源快照不属于当前批次配置")

        normalized_path = directory / "normalized-config.json"
        if normalized_path.is_symlink():
            raise InputValidationError("规范化配置快照不得为符号链接")
        try:
            normalized_bytes = normalized_path.read_bytes()
            normalized_config_json = normalized_bytes.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise InputValidationError("规范化配置快照不可读") from exc
        if (
            not normalized_bytes
            or len(normalized_bytes) > MAX_SNAPSHOT_MANIFEST_BYTES
            or _sha256(normalized_bytes) != normalized_config_sha256
            or normalized_config_sha256 != source_config_hash
        ):
            raise InputValidationError("规范化配置快照 SHA-256 校验失败")

        entries = manifest["sources"]
        if (
            not isinstance(entries, list)
            or not entries
            or len(entries) > MAX_BATCH_TEMPLATES
        ):
            raise InputValidationError("源快照源文件列表不合法")
        result: dict[str, LoadedTemplate] = {}
        total_bytes = 0
        expected_entry_fields = {
            "template_key",
            "template_id",
            "source_filename",
            "sha256",
            "byte_size",
        }
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != expected_entry_fields:
                raise InputValidationError("源快照文件项格式不合法")
            template_key = entry["template_key"]
            template_id = entry["template_id"]
            filename = entry["source_filename"]
            digest = entry["sha256"]
            size = entry["byte_size"]
            if (
                not isinstance(template_key, str)
                or not template_key
                or template_key in result
                or not isinstance(template_id, str)
                or template_id != template_key
                or not isinstance(filename, str)
                or not filename
                or Path(filename).name != filename
                or not isinstance(digest, str)
                or not _SHA256.fullmatch(digest)
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size <= 0
                or size > MAX_SOURCE_BYTES
            ):
                raise InputValidationError("源快照文件项身份不合法")
            data_path = directory / f"{digest}_m.bytes"
            if data_path.is_symlink():
                raise InputValidationError("源快照源文件不得为符号链接")
            try:
                data = data_path.read_bytes()
            except OSError as exc:
                raise InputValidationError(
                    "源快照源文件不存在或不可读",
                    evidence={"sha256": digest},
                ) from exc
            total_bytes += len(data)
            if len(data) != size or len(data) > MAX_SOURCE_BYTES:
                raise InputValidationError("源快照源文件大小校验失败")
            if _sha256(data) != digest:
                raise InputValidationError("源快照源文件 SHA-256 校验失败")
            if total_bytes > MAX_SNAPSHOT_SET_BYTES:
                raise InputValidationError("源快照集超过安全容量上限")
            try:
                level = parse_level_bytes(data, source_name=filename)
            except (TemplateParseError, ValueError) as exc:
                raise SourceParseError("源快照 protobuf 解析失败") from exc
            if level.template_id != template_id or level.sha256 != digest:
                raise SourceParseError("源快照内部模板身份校验失败")
            result[template_key] = LoadedTemplate(
                level=level,
                input_mode="file_snapshot",
                source_locator=f"sha256:{digest}",
                source_filename=filename,
            )
        return SourceSnapshotSet(
            snapshot_set_id,
            result,
            batch_seed,
            source_config_hash,
            normalized_config_json,
        )


__all__ = [
    "MAX_SNAPSHOT_SET_BYTES",
    "SNAPSHOT_DIRECTORY",
    "SNAPSHOT_FORMAT_VERSION",
    "SourceSnapshotSet",
    "SourceSnapshotStore",
]
