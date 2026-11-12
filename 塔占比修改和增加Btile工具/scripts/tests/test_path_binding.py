"""Bounded local path discovery and persisted confirmation."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.tower_reducer.path_binding import (
    TEMPLATE_SUFFIX, TILE_SUFFIX, discover_template_roots, validate_binding,
)
from scripts.tower_reducer.settings import LocalSettings, load_settings, save_settings


class PathBindingTests(unittest.TestCase):
    def test_default_sync_choices_persist_without_confirming_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary).resolve() / 'settings.json'
            self.assertEqual(load_settings(store).default_sync_targets, [])
            for targets in ([], ['template'], ['tile'], ['template', 'tile']):
                save_settings(LocalSettings(default_sync_targets=targets), store)
                loaded = load_settings(store)
                self.assertEqual(loaded.default_sync_targets, targets)
                self.assertFalse(loaded.sync_ready)
                self.assertFalse(loaded.tile_confirmed)

    def test_legacy_settings_and_invalid_sync_choices(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary).resolve() / 'settings.json'
            store.write_text('{"naming_rule":"lessT{原模板ID}"}', encoding='utf-8')
            self.assertEqual(load_settings(store).default_sync_targets, [])
            for invalid in ('tile', ['other'], ['tile', 'tile'], [True]):
                with self.assertRaises(ValueError):
                    save_settings(LocalSettings(default_sync_targets=invalid), store)

    def test_new_settings_default_to_tool_output_and_naming_rule(self):
        with tempfile.TemporaryDirectory() as temporary:
            defaults = load_settings(Path(temporary).resolve() / "missing.json")
            self.assertEqual(Path(defaults.export_root).name, "输出模板")
            self.assertEqual(defaults.naming_rule, "lessT{原模板ID}")

    def test_discovers_both_fixed_suffixes_without_assuming_user_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "arbitrary-person"
            tile = root / TILE_SUFFIX
            template = root / TEMPLATE_SUFFIX
            tile.mkdir(parents=True)
            template.mkdir(parents=True)
            found = discover_template_roots(root, max_depth=7, deadline=time.monotonic() + 3)
            self.assertEqual(found.tile_roots, (tile,))
            self.assertEqual(found.template_roots, (template,))
            self.assertTrue(found.requires_confirmation)

    def test_direct_home_suffixes_are_found_before_noisy_search_hits_entry_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "another-person"
            root.mkdir()
            for number in range(8):
                (root / ("noise-" + str(number))).mkdir()
            tile = root / TILE_SUFFIX
            template = root / TEMPLATE_SUFFIX
            tile.mkdir(parents=True)
            template.mkdir(parents=True)

            found = discover_template_roots(
                root,
                max_depth=9,
                deadline=time.monotonic() + 3,
                max_entries=1,
            )

            self.assertEqual(found.tile_roots, (tile,))
            self.assertEqual(found.template_roots, (template,))
            self.assertFalse(found.timed_out)
            self.assertFalse(found.limit_reached)

    def test_direct_home_suffixes_still_respect_max_depth(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "depth-bounded-person"
            tile = root / TILE_SUFFIX
            template = root / TEMPLATE_SUFFIX
            tile.mkdir(parents=True)
            template.mkdir(parents=True)

            found = discover_template_roots(
                root,
                max_depth=1,
                deadline=time.monotonic() + 3,
            )

            self.assertEqual(found.tile_roots, ())
            self.assertEqual(found.template_roots, ())

    def test_direct_home_suffixes_do_not_bypass_expired_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "deadline-bounded-person"
            (root / TILE_SUFFIX).mkdir(parents=True)
            (root / TEMPLATE_SUFFIX).mkdir(parents=True)

            found = discover_template_roots(
                root,
                max_depth=9,
                deadline=time.monotonic() - 1,
            )

            self.assertEqual(found.tile_roots, ())
            self.assertEqual(found.template_roots, ())
            self.assertTrue(found.timed_out)

    def test_multiple_and_missing_candidates_remain_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "one" / TILE_SUFFIX).mkdir(parents=True)
            (root / "two" / TILE_SUFFIX).mkdir(parents=True)
            found = discover_template_roots(root, max_depth=8, deadline=time.monotonic() + 3)
            self.assertEqual(len(found.tile_roots), 2)
            self.assertEqual(found.template_roots, ())
            self.assertTrue(found.requires_confirmation)

    def test_depth_and_deadline_bound_search(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "nested" / TILE_SUFFIX).mkdir(parents=True)
            self.assertEqual(discover_template_roots(root, 5, time.monotonic() + 3).tile_roots, ())
            self.assertEqual(discover_template_roots(root, 9, time.monotonic() - 1).tile_roots, ())

    def test_validation_requires_exact_directory_suffix_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            good = root / TILE_SUFFIX
            good.mkdir(parents=True)
            self.assertTrue(validate_binding(good, TILE_SUFFIX).valid)
            self.assertFalse(validate_binding(good, TEMPLATE_SUFFIX).valid)
            self.assertFalse(validate_binding(root / "missing" / TILE_SUFFIX, TILE_SUFFIX).valid)
            link_parent = root / "linked"
            link_parent.mkdir()
            alias = link_parent / "tile"
            alias.symlink_to(root / "tile", target_is_directory=True)
            result = validate_binding(alias / "Tile" / "Assets" / "Resources" /
                                      "TileMatchLevelData", TILE_SUFFIX)
            self.assertFalse(result.valid)
            self.assertEqual(result.reason, "symlink_path")

    def test_discovery_does_not_follow_symlink_outside_user_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "home"
            outside = Path(temporary).resolve() / "outside"
            (outside / TILE_SUFFIX).mkdir(parents=True)
            root.mkdir()
            (root / "alias").symlink_to(outside, target_is_directory=True)
            found = discover_template_roots(root, 10, time.monotonic() + 3)
            self.assertEqual(found.tile_roots, ())

    def test_discovery_rejects_symlink_in_search_root_ancestor(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            actual_home = base / "actual" / "home"
            (actual_home / TILE_SUFFIX).mkdir(parents=True)
            (base / "alias").symlink_to(base / "actual", target_is_directory=True)
            found = discover_template_roots(base / "alias" / "home", 7, time.monotonic() + 3)
            self.assertEqual(found.tile_roots, ())
            self.assertEqual(found.template_roots, ())

    def test_single_directory_enumeration_stops_at_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            visited = []

            class Entry:
                def __init__(self, number):
                    self.path = str(root / ("child-" + str(number)))

                def is_dir(self, follow_symlinks=False):
                    return True

                def is_symlink(self):
                    return False

            class Entries:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def __iter__(self):
                    for number in range(100):
                        visited.append(number)
                        yield Entry(number)

            tick = iter(range(1000))
            with patch("scripts.tower_reducer.path_binding.os.scandir", return_value=Entries()), \
                    patch("scripts.tower_reducer.path_binding.time.monotonic", side_effect=lambda: next(tick)):
                found = discover_template_roots(root, 2, deadline=4)
            self.assertTrue(found.timed_out)
            self.assertLess(len(visited), 10)

    def test_entry_limit_returns_explicit_partial_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for number in range(10):
                (root / ("child-" + str(number))).mkdir()
            found = discover_template_roots(root, 2, time.monotonic() + 3, max_entries=3)
            self.assertTrue(found.limit_reached)
            self.assertFalse(found.timed_out)

    def test_saved_bindings_require_confirmation_and_reguide_when_stale(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            tile = root / TILE_SUFFIX
            template = root / TEMPLATE_SUFFIX
            tile.mkdir(parents=True)
            template.mkdir(parents=True)
            settings_path = root / "运行数据" / "settings.json"
            original = LocalSettings(tile_root=str(tile), template_root=str(template),
                                     tile_confirmed=True, template_confirmed=True,
                                     export_root=str(root / "out"), naming_rule="lessT{原模板ID}")
            save_settings(original, settings_path)
            loaded = load_settings(settings_path)
            self.assertTrue(loaded.sync_ready)
            self.assertEqual(loaded.naming_rule, "lessT{原模板ID}")
            template.rmdir()
            stale = load_settings(settings_path)
            self.assertFalse(stale.sync_ready)
            self.assertFalse(stale.template_confirmed)
            self.assertTrue(stale.tile_confirmed)

    def test_string_false_confirmation_cannot_enable_sync(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            tile = root / TILE_SUFFIX
            template = root / TEMPLATE_SUFFIX
            tile.mkdir(parents=True)
            template.mkdir(parents=True)
            store = root / "settings.json"
            store.write_text(json.dumps({"tile_root": str(tile), "template_root": str(template),
                                         "tile_confirmed": "false", "template_confirmed": "false",
                                         "export_root": str(root / "out"),
                                         "naming_rule": "lessT{原模板ID}"}), encoding="utf-8")
            loaded = load_settings(store)
            self.assertFalse(loaded.sync_ready)
            self.assertFalse(loaded.tile_confirmed)
            self.assertFalse(loaded.template_confirmed)
            self.assertTrue(loaded.load_error)

    def test_invalid_settings_types_fail_closed_on_load_and_save(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            store = root / "settings.json"
            for bad in ({"tile_root": 123}, {"template_root": ["path"]},
                        {"export_root": None}, {"naming_rule": 42},
                        {"tile_confirmed": 1}, {"template_confirmed": "true"}):
                with self.subTest(bad=bad):
                    store.write_text(json.dumps(bad), encoding="utf-8")
                    loaded = load_settings(store)
                    self.assertFalse(loaded.sync_ready)
                    self.assertTrue(loaded.load_error)
            with self.assertRaises(ValueError):
                save_settings(LocalSettings(tile_confirmed="false"), store)
            with self.assertRaises(ValueError):
                save_settings(LocalSettings(export_root=123), store)

    def test_corrupt_json_returns_error_without_replacing_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            store = root / "settings.json"
            original = "{broken JSON"
            store.write_text(original, encoding="utf-8")
            loaded = load_settings(store)
            self.assertFalse(loaded.sync_ready)
            self.assertTrue(loaded.load_error)
            with self.assertRaises(ValueError):
                save_settings(loaded, store)
            self.assertEqual(store.read_text(encoding="utf-8"), original)

    def test_settings_keep_only_local_defaults_and_reject_symlink_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            store = root / "settings.json"
            save_settings(LocalSettings(naming_rule="pre{原模板ID}"), store)
            raw = json.loads(store.read_text(encoding="utf-8"))
            self.assertEqual(set(raw), {"tile_root", "template_root", "tile_confirmed",
                                         "template_confirmed", "export_root", "naming_rule", "default_sync_targets",
                                         "config_root", "increase_naming_rule"})
            alias = root / "alias.json"
            alias.symlink_to(store)
            with self.assertRaises(ValueError):
                save_settings(LocalSettings(), alias)


if __name__ == "__main__":
    unittest.main()
