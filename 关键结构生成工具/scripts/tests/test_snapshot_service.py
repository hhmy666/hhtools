from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.errors import InputValidationError  # noqa: E402
from puzzle_core.protobuf_wire import encode_varint  # noqa: E402
from services.import_service import load_template_id  # noqa: E402
from services.profile_service import Profile  # noqa: E402
from services.snapshot_service import SourceSnapshotStore  # noqa: E402


def _varint_field(number: int, value: int) -> bytes:
    return encode_varint(number << 3) + encode_varint(value)


def _bytes_field(number: int, value: bytes) -> bytes:
    return encode_varint((number << 3) | 2) + encode_varint(len(value)) + value


def _template_bytes(template_id: str) -> bytes:
    tile = b"".join(
        (
            _varint_field(1, 1),
            _varint_field(2, 1),
            _varint_field(3, 0),
            _varint_field(4, -10),
        )
    )
    return b"".join(
        (
            _bytes_field(2, template_id.encode()),
            _varint_field(4, 2),
            _varint_field(5, 2),
            _bytes_field(6, tile),
        )
    )


class SnapshotServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.app_data = self.root / "app-data"
        self.output = self.root / "output"
        self.output.mkdir()
        self.profile = Profile("p1", "test", None, None, str(self.output))
        self.source = self.root / "hr0029_m.bytes"
        self.data = _template_bytes("hr0029")
        self.source.write_bytes(self.data)
        self.normalized = '{"locked":true}'
        self.config_hash = hashlib.sha256(self.normalized.encode()).hexdigest()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_snapshot_survives_original_move_without_leaking_absolute_path(self) -> None:
        store = SourceSnapshotStore(self.app_data)
        created = store.create(
            {"hr0029": self.source},
            batch_seed=123,
            source_config_hash=self.config_hash,
            normalized_config_json=self.normalized,
            profile=self.profile,
        )
        moved = self.root / "moved_m.bytes"
        self.source.rename(moved)

        loaded = store.load(
            created.snapshot_set_id,
            expected_source_config_hash=self.config_hash,
            profile=self.profile,
        )
        self.assertEqual(loaded.sources["hr0029"].level.data, self.data)
        self.assertEqual(loaded.sources["hr0029"].input_mode, "file_snapshot")
        self.assertEqual(loaded.batch_seed, 123)
        manifest = (
            self.app_data
            / "source-snapshots"
            / created.snapshot_set_id
            / "manifest.json"
        ).read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), manifest)
        self.assertNotIn(str(moved), manifest)

    def test_snapshot_rejects_tampered_source_and_wrong_config(self) -> None:
        store = SourceSnapshotStore(self.app_data)
        created = store.create(
            {"hr0029": self.source},
            batch_seed=123,
            source_config_hash=self.config_hash,
            normalized_config_json=self.normalized,
            profile=self.profile,
        )
        with self.assertRaises(InputValidationError):
            store.load(
                created.snapshot_set_id,
                expected_source_config_hash="0" * 64,
                profile=self.profile,
            )

        digest = hashlib.sha256(self.data).hexdigest()
        snapshot_file = (
            self.app_data
            / "source-snapshots"
            / created.snapshot_set_id
            / f"{digest}_m.bytes"
        )
        snapshot_file.write_bytes(self.data + b"tampered")
        with self.assertRaises(InputValidationError):
            store.load(created.snapshot_set_id, profile=self.profile)

    def test_snapshot_store_cannot_be_inside_formal_output(self) -> None:
        store = SourceSnapshotStore(self.output)
        with self.assertRaises(InputValidationError):
            store.create(
                {"hr0029": self.source},
                batch_seed=123,
                source_config_hash=self.config_hash,
                normalized_config_json=self.normalized,
                profile=self.profile,
            )


class TemplateIdRevalidationTests(unittest.TestCase):
    def test_lookup_revalidates_repo_containment_each_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "tile"
            repo.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (outside / "hr0029_m.bytes").write_bytes(_template_bytes("hr0029"))
            profile = Profile("p1", "test", str(repo), str(outside), str(root / "out"))
            with self.assertRaises(InputValidationError):
                load_template_id(profile, "hr0029")

    def test_lookup_revalidates_readability_each_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "tile"
            templates = repo / "Tile" / "Assets" / "Resources" / "TileMatchLevelData"
            templates.mkdir(parents=True)
            (templates / "hr0029_m.bytes").write_bytes(_template_bytes("hr0029"))
            profile = Profile("p1", "test", str(repo), str(templates), str(root / "out"))
            with (
                mock.patch("services.path_service.os.access", return_value=False),
                self.assertRaises(InputValidationError),
            ):
                load_template_id(profile, "hr0029")


if __name__ == "__main__":
    unittest.main()
