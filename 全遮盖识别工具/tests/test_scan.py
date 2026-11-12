"""Synthetic three-header config snapshots test the actual scan/export path."""

import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from test_engine import fixture

try:
    from covercheck.scan import scan_sources
except ModuleNotFoundError:
    scan_sources = None


def write_csv(path, headers, rows, type_name="string"):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerow([type_name] * len(headers))
        writer.writerow(["字段说明"] * len(headers))
        writer.writerows(rows)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(scan_sources, "config scanner is not implemented")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.templates = self.root / "templates"
        self.templates.mkdir()
        self.list_path, self.level_path = self.root / "list.csv", self.root / "level.csv"
        self.output = self.root / "reports"

    def configs(self, lists, levels):
        write_csv(self.list_path, ["LevelIndex", "LevelId", "DDALevelId1", "DDALevelId2", "DDALevelId3", "DDAType"], lists, "int")
        write_csv(self.level_path, ["LevelId", "TemplateId"], levels)

    def test_dda_selection_exact_split_and_dedup_exports(self):
        self.configs([["1", "inactive", " a |b ", "a", "c", "1"], ["2", "d", "inactive", "inactive", "inactive", "2"]], [["a", "hit"], ["b", "clean"], ["c", "hit"], ["d", "hit"]])
        (self.templates / "hit_m.bytes").write_bytes(fixture([(0, 0, 0, -10), (0, 0, 1, -10)], template_id="hit"))
        (self.templates / "clean_m.bytes").write_bytes(fixture([(0, 0, 0, -10)], template_id="clean"))
        source_hash = hashlib.sha256(self.list_path.read_bytes()).hexdigest()
        value = scan_sources(self.list_path, self.level_path, self.templates, self.output)
        self.assertEqual(value["totals"]["active_level_references"], 4)
        self.assertEqual(value["totals"]["scanned_templates"], 2)
        self.assertEqual(value["totals"]["hit_level_rows"], 3)
        self.assertEqual(value["totals"]["adjacent_pairs"], 1)
        self.assertEqual(value["sources"]["level_list"]["sha256"], source_hash)
        report = self.output / value["reports"]["hit_levels"]
        rows = list(csv.reader(io.StringIO(report.read_text(encoding="utf-8-sig"))))
        self.assertEqual(rows[0][:5], ["模板名称", "LevelIndex", "LevelId", "非塔重叠坐标组数", "模板另含塔"])
        self.assertEqual(sorted((r[1], r[2]) for r in rows[1:]), [("1", "a"), ("1", "c"), ("2", "d")])
        self.assertEqual(hashlib.sha256(self.list_path.read_bytes()).hexdigest(), source_hash)
        persisted = json.loads((self.output / "来源与口径.json").read_text())
        self.assertEqual(persisted["totals"], value["totals"])

    def test_missing_conflict_mismatch_and_bad_wire_are_explicit_exceptions(self):
        self.configs([["1", "missing_cfg|conflict|missing_file|mismatch|bad|good", "", "", "", "0"]], [["conflict", "one"], ["conflict", "two"], ["missing_file", "absent"], ["mismatch", "expected"], ["bad", "bad"], ["good", "good"]])
        (self.templates / "expected_m.bytes").write_bytes(fixture([], template_id="wrong"))
        (self.templates / "bad_m.bytes").write_bytes(b"\x32\xff")
        (self.templates / "good_m.bytes").write_bytes(fixture([], template_id="good"))
        value = scan_sources(self.list_path, self.level_path, self.templates, self.output)
        self.assertEqual(value["totals"]["scanned_templates"], 1)
        self.assertEqual(value["totals"]["exception_count"], 5)
        kinds = {a["kind"] for a in value["exceptions"]}
        self.assertEqual(kinds, {"missing_level", "conflicting_level", "missing_template", "template_id_mismatch", "decode_error"})
        self.assertEqual(value["totals"]["clean_templates"], 1)

    def test_duplicate_levelindex_conflict_does_not_silently_choose_mapping(self):
        self.configs([["1", "a", "", "", "", "0"], ["1", "b", "", "", "", "0"]], [["a", "a"], ["b", "b"]])
        value = scan_sources(self.list_path, self.level_path, self.templates, self.output)
        self.assertEqual(value["totals"]["scanned_templates"], 0)
        self.assertEqual([a["kind"] for a in value["exceptions"]], ["conflicting_level_index"])

    def test_same_name_symlink_cannot_escape_template_root(self):
        self.configs([["1", "a", "", "", "", "0"]], [["a", "outside"]])
        outside = self.root / "outside_m.bytes"
        outside.write_bytes(fixture([], template_id="outside"))
        (self.templates / outside.name).symlink_to(outside)
        value = scan_sources(self.list_path, self.level_path, self.templates, self.output)
        self.assertEqual([a["kind"] for a in value["exceptions"]], ["unsafe_template_path"])


if __name__ == "__main__":
    unittest.main()
