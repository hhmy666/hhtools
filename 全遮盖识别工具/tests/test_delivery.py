"""Delivery operates only in temporary directories and never overwrites."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from covercheck.engine import analyze_bytes
from covercheck.wire import parse_fields, scalar
from test_engine import fixture, message

try:
    from covercheck import delivery
except ImportError:
    delivery = None


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(delivery, "safe local delivery is not implemented")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def entries(self, *ids):
        return [{"index": i, "name": name + "_m.bytes", "template_id": name,
                 "raw": fixture([(-2147483648, 2147483647, -5, -10)], template_id=name)
                         + message(100, b"preserved metadata")}
                for i, name in enumerate(ids or ("original",))]

    def test_preview_is_read_only_and_names_match_all_targets_and_internal_id(self):
        targets = {"output": str(self.root / "new" / "output"), "template": str(self.root / "sync")}
        plan = delivery.plan_delivery(self.entries(), targets, "fixed{原模板ID}")
        self.assertFalse((self.root / "new").exists())
        self.assertFalse((self.root / "sync").exists())
        json.dumps(plan["rows"])
        row = plan["rows"][0]
        self.assertEqual(row["filename"], "fixedoriginal_m.bytes")
        self.assertEqual(row["new_id"], "fixedoriginal")
        self.assertEqual([t["status"] for t in row["targets"]], ["create", "create"])
        raw = plan["files"]["fixedoriginal_m.bytes"]
        self.assertEqual(analyze_bytes(raw)["template_id"], "fixedoriginal")
        self.assertEqual(analyze_bytes(raw)["tiles"][0]["x"], -2147483648)
        self.assertEqual(scalar(parse_fields(raw), 100, 2), b"preserved metadata")

    def test_original_ids_source_names_and_batch_duplicates_are_reserved(self):
        entries = self.entries("one", "one_v2", "one")
        entries[1]["name"] = "one_v3_m.bytes"
        plan = delivery.plan_delivery(entries, {"output": str(self.root)}, "{id}")
        self.assertEqual([r["new_id"] for r in plan["rows"]], ["one_v4", "one_v2_v2", "one_v5"])

    def test_unselected_original_ids_are_reserved_without_exporting_other_entries(self):
        plan = delivery.plan_delivery(self.entries("one"), {"output": str(self.root)}, "fixed{id}",
                                      reserved_ids=("FIXEDone", "ｆｉｘｅｄone_v2"))
        self.assertEqual([row["new_id"] for row in plan["rows"]], ["fixedone_v3"])
        self.assertEqual(list(plan["files"]), ["fixedone_v3_m.bytes"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_collision_in_any_target_casefold_and_nfkc_allocates_one_version(self):
        left, right = self.root / "left", self.root / "right"
        left.mkdir(); right.mkdir()
        (right / "ＦＩＸＥＤoriginal_m.bytes").write_bytes(b"occupied")
        (left / "fixedORIGINAL_v2_m.bytes").write_bytes(b"occupied")
        plan = delivery.plan_delivery(self.entries(), {"output": str(left), "tile": str(right)}, "fixed{id}")
        self.assertEqual(plan["rows"][0]["filename"], "fixedoriginal_v3_m.bytes")

    def test_commit_creates_complete_files_and_reports_every_target_progress(self):
        targets = {"output": str(self.root / "out"), "tile": str(self.root / "nested" / "tile")}
        plan = delivery.plan_delivery(self.entries("one", "two"), targets, "fixed{id}")
        events = []
        report = delivery.commit_delivery(plan, events.append)
        self.assertTrue(report["ok"], report)
        self.assertEqual((report["completed"], report["total"]), (4, 4))
        self.assertEqual([e["completed"] for e in events if e["phase"] == "writing"], [1, 2, 3, 4])
        for target in targets.values():
            self.assertEqual(sorted(p.name for p in Path(target).iterdir()), ["fixedone_m.bytes", "fixedtwo_m.bytes"])
            for filename, raw in plan["files"].items():
                self.assertEqual((Path(target) / filename).read_bytes(), raw)

    def test_new_occupied_leaf_fails_only_affected_target_without_overwrite(self):
        left, right = self.root / "left", self.root / "right"
        left.mkdir(); right.mkdir()
        plan = delivery.plan_delivery(self.entries(), {"output": str(left), "tile": str(right)}, "fixed{id}")
        conflict = right / "fixedoriginal_m.bytes"
        conflict.write_bytes(b"keep")
        report = delivery.commit_delivery(plan)
        self.assertFalse(report["ok"])
        self.assertEqual([t["status"] for t in report["rows"][0]["targets"]], ["written", "failed"])
        self.assertEqual(conflict.read_bytes(), b"keep")
        self.assertEqual(report["completed"], 2)

    def test_symlink_directory_ancestor_and_unsafe_names_rejected_without_writes(self):
        (self.root / "alias").symlink_to(self.root, target_is_directory=True)
        for target in (str(self.root / "alias" / "out"), str(self.root / ".." / "escape"), "relative"):
            with self.subTest(target=target), self.assertRaises((ValueError, OSError)):
                delivery.plan_delivery(self.entries(), {"output": target}, "fixed{id}")
        for pattern in ("../{id}", "ｆｉｘ／{id}", "CON", "{unknown}", "a.bytes", "a\x00{id}"):
            with self.subTest(pattern=pattern), self.assertRaises(ValueError):
                delivery.plan_delivery(self.entries(), {"output": str(self.root)}, pattern)
        self.assertEqual([p.name for p in self.root.iterdir()], ["alias"])

    def test_directory_replaced_after_preview_is_rejected(self):
        target = self.root / "out"
        target.mkdir()
        plan = delivery.plan_delivery(self.entries(), {"output": str(target)}, "fixed{id}")
        target.rename(self.root / "old")
        target.mkdir()
        report = delivery.commit_delivery(plan)
        self.assertFalse(report["ok"])
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(list((self.root / "old").iterdir()), [])

    def test_missing_root_created_by_someone_else_requires_new_preview(self):
        target = self.root / "missing"
        plan = delivery.plan_delivery(self.entries(), {"output": str(target)}, "fixed{id}")
        target.mkdir()
        report = delivery.commit_delivery(plan)
        self.assertFalse(report["ok"])
        self.assertEqual(list(target.iterdir()), [])

    def test_casefold_race_never_reports_success_or_overwrites(self):
        plan = delivery.plan_delivery(self.entries(), {"output": str(self.root)}, "fixed{id}")
        real_link = os.link
        alias = self.root / "FIXEDoriginal_m.bytes"
        def race(*args, **kwargs):
            alias.write_bytes(b"other process")
            return real_link(*args, **kwargs)
        with patch.object(delivery.os, "link", side_effect=race):
            report = delivery.commit_delivery(plan)
        self.assertFalse(report["ok"])
        self.assertEqual(alias.read_bytes(), b"other process")
        self.assertNotIn("fixedoriginal_m.bytes", os.listdir(self.root))
        self.assertFalse(any(p.name.startswith(".delivery-") for p in self.root.iterdir()))

    def test_leaf_symlink_race_is_not_followed(self):
        victim = self.root / "victim"
        victim.write_bytes(b"keep")
        plan = delivery.plan_delivery(self.entries(), {"output": str(self.root)}, "fixed{id}")
        (self.root / "fixedoriginal_m.bytes").symlink_to(victim)
        report = delivery.commit_delivery(plan)
        self.assertFalse(report["ok"])
        self.assertEqual(victim.read_bytes(), b"keep")
        self.assertTrue((self.root / "fixedoriginal_m.bytes").is_symlink())

    def test_nfkc_collision_created_at_publish_is_rolled_back(self):
        plan = delivery.plan_delivery(self.entries(), {"output": str(self.root)}, "fixed{id}")
        real_link = os.link
        alias = self.root / "ｆｉｘｅｄoriginal_m.bytes"
        def race(*args, **kwargs):
            alias.write_bytes(b"concurrent")
            return real_link(*args, **kwargs)
        with patch.object(delivery.os, "link", side_effect=race):
            report = delivery.commit_delivery(plan)
        self.assertFalse(report["ok"])
        self.assertFalse(report["rows"][0]["targets"][0]["applied"])
        self.assertEqual(alias.read_bytes(), b"concurrent")
        self.assertNotIn("fixedoriginal_m.bytes", os.listdir(self.root))

    def test_directory_swap_during_publish_cannot_write_to_replacement(self):
        target, moved, other = self.root / "out", self.root / "moved", self.root / "other"
        target.mkdir(); other.mkdir()
        plan = delivery.plan_delivery(self.entries(), {"output": str(target)}, "fixed{id}")
        real_link = os.link
        def race(*args, **kwargs):
            target.rename(moved)
            target.symlink_to(other, target_is_directory=True)
            return real_link(*args, **kwargs)
        with patch.object(delivery.os, "link", side_effect=race):
            report = delivery.commit_delivery(plan)
        self.assertFalse(report["ok"])
        self.assertFalse(report["rows"][0]["targets"][0]["applied"])
        self.assertEqual(list(moved.iterdir()), [])
        self.assertEqual(list(other.iterdir()), [])

    def test_failed_directory_fsync_reports_already_applied_complete_file(self):
        plan = delivery.plan_delivery(self.entries(), {"output": str(self.root)}, "fixed{id}")
        real_fsync = os.fsync
        def fail_directory(fd):
            import stat
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("directory durability failed")
            return real_fsync(fd)
        with patch.object(delivery.os, "fsync", side_effect=fail_directory):
            report = delivery.commit_delivery(plan)
        self.assertFalse(report["ok"])
        self.assertTrue(report["rows"][0]["targets"][0]["applied"])
        self.assertEqual((self.root / "fixedoriginal_m.bytes").read_bytes(), plan["files"]["fixedoriginal_m.bytes"])

    def test_failed_temp_cleanup_preserves_applied_status(self):
        plan = delivery.plan_delivery(self.entries(), {"output": str(self.root)}, "fixed{id}")
        real_unlink = os.unlink
        def fail_cleanup(path, *args, **kwargs):
            if str(path).startswith(".delivery-"):
                raise OSError("temporary cleanup blocked")
            return real_unlink(path, *args, **kwargs)
        with patch.object(delivery.os, "unlink", side_effect=fail_cleanup):
            report = delivery.commit_delivery(plan)
        self.assertFalse(report["ok"])
        self.assertTrue(report["rows"][0]["targets"][0]["applied"])
        self.assertEqual((self.root / "fixedoriginal_m.bytes").read_bytes(), plan["files"]["fixedoriginal_m.bytes"])

    def test_output_target_required_and_bad_wire_rejected(self):
        with self.assertRaises(ValueError):
            delivery.plan_delivery(self.entries(), {"tile": str(self.root)}, "fixed{id}")
        entries = self.entries()
        entries[0]["raw"] = b"\x32\xff"
        with self.assertRaises(ValueError):
            delivery.plan_delivery(entries, {"output": str(self.root)}, "fixed{id}")


if __name__ == "__main__":
    unittest.main()
