from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.errors import InputValidationError
from services.path_service import resolve_template_path, validate_template_id
from services.profile_service import ProfileService


class ProfileServiceTest(unittest.TestCase):
    def test_first_run_persists_and_reuses_default_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tool = root / "tool"
            tool.mkdir()
            service = ProfileService(root / "app-data" / "settings.json")
            first = service.initialize(tool)
            second = ProfileService(service.settings_path).initialize(tool)
            self.assertEqual(first, second)
            self.assertEqual(Path(first.output_root), (tool / "输出关卡模板").resolve())

    def test_source_is_optional_until_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tool = root / "tool"
            tool.mkdir()
            service = ProfileService(root / "settings.json")
            profile = service.initialize(tool)
            self.assertTrue(service.status(profile.profile_id)["source_required_for_template_id"])

            repo = root / "tile"
            templates = repo / "Tile" / "Assets" / "Resources" / "TileMatchLevelData"
            templates.mkdir(parents=True)
            (templates / "hr0029_m.bytes").write_bytes(b"fixture")
            configured = service.configure_source(profile.profile_id, repo)
            self.assertEqual(Path(configured.template_dir), templates.resolve())
            self.assertEqual(resolve_template_path(templates, "hr0029").read_bytes(), b"fixture")
            reloaded = ProfileService(service.settings_path).active_profile()
            self.assertEqual(reloaded, configured)

    def test_output_cannot_be_inside_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tool = root / "tool"
            tool.mkdir()
            service = ProfileService(root / "settings.json")
            profile = service.initialize(tool)
            repo = root / "tile"
            templates = repo / "Tile" / "Assets" / "Resources" / "TileMatchLevelData"
            templates.mkdir(parents=True)
            configured = service.configure_source(profile.profile_id, repo)
            with self.assertRaises(InputValidationError):
                service.configure_output(configured.profile_id, repo / "outputs")

    def test_ulinkconfig_root_is_persisted_as_read_only_lookup_preference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tool = root / "tool"
            tool.mkdir()
            ulink = root / "ulinkconfig"
            ulink.mkdir()
            service = ProfileService(root / "settings.json")
            profile = service.initialize(tool)

            configured = service.configure_ulinkconfig(profile.profile_id, ulink)

            self.assertEqual(Path(configured.ulinkconfig_root), ulink.resolve())
            self.assertEqual(
                ProfileService(service.settings_path).active_profile(), configured
            )
            self.assertTrue(
                service.status(configured.profile_id)["ulinkconfig_root"]["readable"]
            )

    def test_template_id_rejects_paths(self) -> None:
        for value in ("../hr0029", "a/b", "a\\b", ""):
            with self.subTest(value=value), self.assertRaises(InputValidationError):
                validate_template_id(value)

    def test_settings_contains_no_tool_install_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tool = root / "tool"
            tool.mkdir()
            path = root / "app-data" / "settings.json"
            ProfileService(path).initialize(tool)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(len(payload["profiles"]), 1)


if __name__ == "__main__":
    unittest.main()
