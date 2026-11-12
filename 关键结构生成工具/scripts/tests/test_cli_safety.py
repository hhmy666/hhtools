from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cli  # noqa: E402
from puzzle_core.config_io import ConfigError  # noqa: E402
from services.profile_service import Profile  # noqa: E402


class _GenerationResult:
    failed = 0

    def as_dict(self):
        return {"status": "generated"}


class _ExportResult:
    def as_dict(self):
        return {"state": "completed"}


class CliSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.profile = Profile("profile-1", "测试环境", None, None, str(self.output))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_generate_report_cannot_enter_formal_output_root(self) -> None:
        report = self.output / "candidate-review.json"
        args = argparse.Namespace(
            config="unused.json",
            file_input=[],
            report=str(report),
        )
        with (
            mock.patch.object(cli, "_load_resolved_config", return_value=object()),
            mock.patch.object(cli, "ProfileService", return_value=object()),
            mock.patch.object(cli, "_active_profile", return_value=self.profile),
            mock.patch.object(cli, "generate_batch", return_value=_GenerationResult()),
        ):
            with self.assertRaisesRegex(ConfigError, "report_inside_output"):
                cli._command_generate(args)
        self.assertFalse(report.exists())

    def test_explicit_empty_idempotency_key_is_rejected_before_generation(self) -> None:
        args = argparse.Namespace(
            config="does-not-exist.json",
            selection="does-not-exist.json",
            file_input=[],
            idempotency_key="",
            duplicate_as_new_version=False,
        )
        with self.assertRaisesRegex(ConfigError, "empty_idempotency_key"):
            cli._command_export(args)

    def test_automatic_key_is_emitted_before_export_service_runs(self) -> None:
        key = "12345678-1234-4234-8234-123456789abc"
        events = []
        args = argparse.Namespace(
            config="unused.json",
            selection="unused-selection.json",
            file_input=[],
            idempotency_key=None,
            duplicate_as_new_version=False,
        )

        def capture_emit(payload, *, stream=sys.stdout):
            events.append(("emit", payload, stream))

        def capture_export(request):
            events.append(("export", request.idempotency_key, None))
            return _ExportResult()

        with (
            mock.patch.object(cli, "_load_resolved_config", return_value=object()),
            mock.patch.object(cli, "ProfileService", return_value=object()),
            mock.patch.object(cli, "_active_profile", return_value=self.profile),
            mock.patch.object(cli, "load_selection_payload", return_value=()),
            mock.patch.object(cli, "generate_batch", return_value=object()),
            mock.patch.object(cli, "resolve_export_selections", return_value=()),
            mock.patch.object(cli, "new_idempotency_key", return_value=key),
            mock.patch.object(cli, "export_selected", side_effect=capture_export),
            mock.patch.object(cli, "_emit", side_effect=capture_emit),
        ):
            self.assertEqual(cli._command_export(args), 0)

        self.assertEqual(events[0][0], "emit")
        self.assertEqual(
            events[0][1],
            {"event": "export_idempotency_key", "idempotency_key": key},
        )
        self.assertIs(events[0][2], sys.stderr)
        self.assertEqual(events[1], ("export", key, None))
        self.assertEqual(events[2][0], "emit")

    def test_export_uses_review_snapshot_seed_and_loaded_sources(self) -> None:
        snapshot = SimpleNamespace(
            snapshot_set_id="snapshot_" + "1" * 32,
            sources={"hr0029": object()},
            batch_seed=987,
            source_config_hash="a" * 64,
            normalized_config_json='{"locked":true}',
        )
        config = SimpleNamespace(
            batch_seed_source="override",
            source_config_hash=snapshot.source_config_hash,
            normalized_config_json=snapshot.normalized_config_json,
        )
        service = SimpleNamespace(settings_path=self.root / "app-data" / "settings.json")
        captured = {}
        args = argparse.Namespace(
            config="request.json",
            selection="selection.json",
            file_input=[],
            batch_seed=None,
            review_snapshot=snapshot.snapshot_set_id,
            idempotency_key="12345678-1234-4234-8234-123456789abc",
            duplicate_as_new_version=False,
        )

        def load_config(_path, *, batch_seed_override=None):
            captured["seed_override"] = batch_seed_override
            return config

        def generate(_config, _profile, **kwargs):
            captured.update(kwargs)
            return object()

        with (
            mock.patch.object(cli, "ProfileService", return_value=service),
            mock.patch.object(cli, "_active_profile", return_value=self.profile),
            mock.patch.object(cli, "_load_resolved_config", side_effect=load_config),
            mock.patch.object(
                cli.SourceSnapshotStore,
                "load",
                return_value=snapshot,
            ),
            mock.patch.object(cli, "load_selection_payload", return_value=()),
            mock.patch.object(cli, "generate_batch", side_effect=generate),
            mock.patch.object(cli, "resolve_export_selections", return_value=()),
            mock.patch.object(cli, "export_selected", return_value=_ExportResult()),
            mock.patch.object(cli, "_emit"),
        ):
            self.assertEqual(cli._command_export(args), 0)

        self.assertEqual(captured["seed_override"], snapshot.batch_seed)
        self.assertEqual(captured["loaded_inputs"], snapshot.sources)
        self.assertEqual(captured["source_snapshot_id"], snapshot.snapshot_set_id)
        self.assertEqual(captured["file_inputs"], {})


if __name__ == "__main__":
    unittest.main()
