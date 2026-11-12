"""Real HTTP edit/repair/export contracts; all template inputs remain read-only."""

import base64
from collections import Counter
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from covercheck import server
from covercheck.engine import analyze_bytes
from covercheck import app as launcher


def varint(value):
    value &= (1 << 64) - 1
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def scalar(number, value):
    return varint(number << 3) + varint(value)


def message(number, value):
    return varint((number << 3) | 2) + varint(len(value)) + value


def board(identifier, coordinates):
    raw = scalar(1, 1) + message(2, identifier.encode()) + scalar(4, 10) + scalar(5, 10)
    for coordinate in coordinates:
        x, y, z = coordinate[:3]
        tile_id = coordinate[3] if len(coordinate) > 3 else -10
        raw += message(6, scalar(1, x) + scalar(2, y) + scalar(3, z) + scalar(4, tile_id))
    return raw + message(90, b"preserve-this-unknown-metadata")


CLEAN = board("source", [(2, 2, 0), (6, 2, 0), (10, 2, 0)])
ISSUE = board("issue", [(2, 2, 0), (2, 2, 1), (10, 2, 0)])
REPAIRABLE = board("repairable", [(2, 2, 0), (2, 2, 1), (5, 5, 0), (11, 5, 0),
    (5, 11, 0), (11, 11, 0), (8, 7, 0), (6, 6, 1), (10, 6, 1), (6, 10, 1),
    (10, 10, 1), (7, 7, 2), (9, 7, 2), (7, 9, 2), (9, 9, 2)])


class RepairApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "clean_m.bytes").write_bytes(CLEAN)
        (self.root / "issue_m.bytes").write_bytes(ISSUE)
        self.httpd = server.create_server(("127.0.0.1", 0), template_root=self.root)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.port = self.httpd.server_port
        self.origin = "http://127.0.0.1:{}".format(self.port)
        self.context = self.request("GET", "/api/context")[1]

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(2)

    def request(self, method, path, payload=None, headers=None, raw=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        actual = {"Origin": self.origin}
        if hasattr(self, "context"):
            actual["X-CSRF-Token"] = self.context["csrf_token"]
        actual.update(headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            actual["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=actual)
        response = connection.getresponse()
        content = response.read()
        result = (response.status, content, dict(response.getheaders())) if raw else (response.status, json.loads(content))
        connection.close()
        return result

    def poll(self, path):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            code, result = self.request("GET", path)
            self.assertEqual(code, 200, result)
            if result["status"] != "running":
                return result
            time.sleep(.01)
        self.fail("后台任务未完成")

    def submit(self, files):
        code, result = self.request("POST", "/api/tasks", {"mode": "files", "files": [
            {"name": name, "base64": base64.b64encode(raw).decode()} for name, raw in files]})
        self.assertEqual(code, 202)
        task_id = result["task_id"]
        self.poll("/api/tasks/" + task_id)
        return "/api/tasks/" + task_id

    def row(self, path, index=0):
        code, result = self.request("GET", path + "/rows/{}".format(index))
        self.assertEqual(code, 200)
        return result

    def export(self, path, payload=None):
        code, result = self.request("POST", path + "/export", payload or {"pattern": "fix{原模板ID}"})
        self.assertEqual(code, 202, result)
        status = self.poll(path + "/export/" + result["export_id"] + "/status")
        return result["export_id"], status

    def mark_changed(self, path, index=0):
        row = self.row(path, index)
        tiles = row['tiles']
        tiles[0]['x'] += 1
        code, saved = self.request('POST', path + '/rows/{}/edit'.format(index),
                                   {'tiles': tiles, 'revision': row['revision']})
        self.assertEqual(code, 200, saved)
        return saved

    def test_detail_keeps_original_board_and_scan_never_leaks_binary_state(self):
        path = self.submit([("clean_m.bytes", CLEAN)])
        detail = self.row(path)
        self.assertEqual(detail.get("revision"), 0)
        self.assertFalse(detail["edited"])
        self.assertEqual(detail["original_board"]["tiles"], detail["tiles"])
        self.assertEqual(detail["repair_status"], "not_run")
        code, snapshot = self.request("GET", path)
        self.assertEqual(code, 200)
        self.assertNotIn("original_board", snapshot["rows"][0])
        self.assertNotIn("tiles", snapshot["rows"][0])
        self.assertNotIn("states", snapshot)
        self.assertNotIn("raw", json.dumps(snapshot))

    def test_saved_edit_can_be_undone_after_reopening_without_revision_reuse(self):
        path = self.submit([("clean_m.bytes", CLEAN)])
        original = self.row(path)
        self.assertFalse(original.get("can_undo"))
        moved = json.loads(json.dumps(original["tiles"]))
        moved[0]["x"] += 1
        code, saved = self.request("POST", path + "/rows/0/edit", {
            "tiles": moved, "revision": 0, "translation": {"dx": 1, "dy": 0}})
        self.assertEqual(code, 200, saved)
        self.assertEqual(self.row(path).get("translation"), {"dx": 1, "dy": 0})
        self.assertTrue(self.row(path).get("can_undo"))
        code, stale = self.request("POST", path + "/rows/0/undo", {"revision": 0})
        self.assertEqual(code, 409, stale)
        code, undone = self.request("POST", path + "/rows/0/undo", {"revision": saved["revision"]})
        self.assertEqual(code, 200, undone)
        self.assertEqual(undone["tiles"], original["tiles"])
        self.assertEqual(undone["revision"], 2)
        self.assertFalse(undone["can_undo"])
        self.assertFalse(undone["edited"])
        self.assertEqual(undone["translation"], {"dx": 0, "dy": 0})
        self.assertEqual(self.request("POST", path + "/rows/0/undo", {"revision": 2})[0], 409)

    def test_edit_tower_coordinates_and_direction_are_saved_and_undone_together(self):
        raw = board("tower", [(6, 6, 0), (6, 6, 1), (6, 6, 2)])
        raw += message(7, scalar(1, 6) + scalar(2, 6) + scalar(3, 0) + message(99, b"tower-meta"))
        path = self.submit([("tower_m.bytes", raw)])
        original = self.row(path)
        for tile in original["tiles"]:
            tile["x"] += 1
        towers = [dict(original["towers"][0], x=7, direction=1)]
        code, saved = self.request("POST", path + "/rows/0/edit", {
            "tiles": original["tiles"], "towers": towers, "revision": 0})
        self.assertEqual(code, 200, saved)
        self.assertEqual((saved["towers"][0]["x"], saved["towers"][0]["direction"]), (7, 1))
        self.assertTrue(all(t["tower_index"] == 0 for t in saved["tiles"]))
        code, undone = self.request("POST", path + "/rows/0/undo", {"revision": saved["revision"]})
        self.assertEqual(code, 200, undone)
        self.assertEqual((undone["towers"][0]["x"], undone["towers"][0]["direction"]), (6, 0))
        self.assertTrue(all(t["x"] == 6 and t["tower_index"] == 0 for t in undone["tiles"]))

    def test_saved_undo_invalidates_previous_export_and_respects_busy_job(self):
        path = self.submit([("clean_m.bytes", CLEAN)])
        tiles = self.row(path)["tiles"]
        tiles[0]["x"] += 1
        self.assertEqual(self.request("POST", path + "/rows/0/edit", {"tiles": tiles, "revision": 0})[0], 200)
        export_id, _ = self.export(path)
        self.assertIsNotNone(self.request("GET", path)[1]["operation"])
        code, undone = self.request("POST", path + "/rows/0/undo", {"revision": 1})
        self.assertEqual(code, 200, undone)
        self.assertIsNone(self.request("GET", path)[1]["operation"])
        self.assertEqual(self.request("GET", path + "/export/" + export_id, raw=True)[0], 409)

    def test_source_identity_survives_delete_add_save_reindex_and_undo(self):
        path = self.submit([("clean_m.bytes", CLEAN)])
        original = self.row(path)
        changed = original["tiles"][1:] + [{"index": -1, "x": 14, "y": 2, "z": 0, "tile_id": -10}]
        code, saved = self.request("POST", path + "/rows/0/edit", {"tiles": changed, "revision": 0})
        self.assertEqual(code, 200, saved)
        self.assertEqual([t.get("source_index", "missing") for t in saved["tiles"]], [1, 2, None])
        for tile in saved["tiles"]:
            tile["y"] += 1
        code, shifted = self.request("POST", path + "/rows/0/edit", {"tiles": saved["tiles"], "revision": 1})
        self.assertEqual(code, 200, shifted)
        self.assertEqual([t["source_index"] for t in shifted["tiles"]], [1, 2, None])
        code, undone = self.request("POST", path + "/rows/0/undo", {"revision": 2})
        self.assertEqual(code, 200, undone)
        self.assertEqual([t["source_index"] for t in undone["tiles"]], [1, 2, None])
        self.assertEqual([t["source_index"] for t in undone["original_board"]["tiles"]], [0, 1, 2])

    def test_auto_repair_can_be_undone_to_the_original_problem_board(self):
        path = self.submit([("repairable_m.bytes", REPAIRABLE)])
        original = self.row(path)
        self.assertEqual(self.request("POST", path + "/repair", {})[0], 202)
        self.poll(path + "/repair")
        repaired = self.row(path)
        self.assertTrue(repaired.get("can_undo"))
        self.assertEqual(sorted(t.get("source_index", -1) for t in repaired["tiles"]), list(range(len(original["tiles"]))))
        code, undone = self.request("POST", path + "/rows/0/undo", {"revision": repaired["revision"]})
        self.assertEqual(code, 200, undone)
        self.assertEqual(undone["tiles"], original["tiles"])
        self.assertFalse(undone["exportable"])
        self.assertFalse(undone["edited"])

    def test_lowimpact_grouped_count_decrease_is_saved_exportable_and_undoable(self):
        from covercheck.repair import rewrite_tiles
        points = [(t['x'], t['y'], t['z'], t['tile_id']) for t in analyze_bytes(REPAIRABLE)['tiles']]
        raw = board('small', points + [(2, 2, 2), (2, 2, 3), (14, 2, 0)])
        analysis = analyze_bytes(raw)
        kept = [t for t in analysis['tiles'] if not (t['x'] == t['y'] == 2 and t['z'] > 0)]
        candidate = rewrite_tiles(raw, kept)
        path = self.submit([('small_m.bytes', raw)])
        result = dict(status='repaired', raw=candidate, origin_indices=[t['index'] for t in kept],
                      removed_count=3, added_count=0, reason='test count decrease')
        with patch('covercheck.repair.auto_repair', return_value=result):
            self.request('POST', path + '/repair', {})
            self.poll(path + '/repair')
        repaired = self.row(path)
        self.assertEqual(repaired['repair_status'], 'repaired', repaired['repair_reason'])
        self.assertEqual(len(repaired['tiles']), 15)
        self.assertEqual(repaired['repair_metrics']['tile_delta'], -3)
        self.assertTrue(repaired['exportable'])
        code, undone = self.request('POST', path + '/rows/0/undo', {'revision': repaired['revision']})
        self.assertEqual(code, 200)
        self.assertEqual(len(undone['tiles']), 18)
        self.assertTrue(undone['issues'])

    def test_service_rejects_same_count_result_that_changes_exposed_surface(self):
        from covercheck.repair import rewrite_tiles
        tiles = analyze_bytes(CLEAN)['tiles']; tiles[0]['x'] += 1
        path = self.submit([('clean_m.bytes', CLEAN)])
        before = self.row(path)
        result = dict(status='repaired', raw=rewrite_tiles(CLEAN, tiles), origin_indices=[0, 1, 2],
                      reason='claimed safe', metrics={'ordinary_outline_unchanged': True})
        with patch('covercheck.repair.auto_repair', return_value=result):
            self.request('POST', path + '/repair', {})
            self.poll(path + '/repair')
        after = self.row(path)
        self.assertEqual(after['repair_status'], 'unrepairable')
        self.assertEqual(after['tiles'], before['tiles'])
        self.assertEqual(after['revision'], 0)

    def test_new_tile_origins_are_supported_without_dropping_undo_and_metadata(self):
        from test_lowimpact import tower_case
        from test_engine import fixture
        from covercheck.repair import rewrite_tiles
        original, tiles = tower_case(1)
        raw = fixture([(t['x'], t['y'], t['z'], t['tile_id']) for t in original['tiles']],
                      [(t['x'], t['y'], t['direction']) for t in original['towers']])
        raw += message(90, b'unchanged')
        path = self.submit([('tower_m.bytes', raw)])
        result = dict(status='repaired', raw=rewrite_tiles(raw, tiles),
                      origin_indices=[t['index'] for t in tiles], removed_count=3, added_count=3)
        with patch('covercheck.repair.auto_repair', return_value=result):
            self.request('POST', path + '/repair', {})
            self.poll(path + '/repair')
        repaired = self.row(path)
        self.assertEqual(repaired['repair_status'], 'repaired', repaired['repair_reason'])
        self.assertEqual(sum(t['source_index'] is None for t in repaired['tiles']), 3)
        self.assertEqual(repaired['repair_metrics']['tower_changed_count'], 3)
        self.assertEqual(self.export(path)[1]['summary']['exported'], 1)
        code, undone = self.request('POST', path + '/rows/0/undo', {'revision': repaired['revision']})
        self.assertEqual(code, 200)
        self.assertEqual([t['source_index'] for t in undone['tiles']], list(range(21)))

    def test_machine_budget_is_cumulative_from_import_not_reset_by_repeated_repair(self):
        from test_lowimpact import hidden_clusters
        from covercheck.repair import rewrite_tiles
        raw = board('hidden', hidden_clusters())
        path = self.submit([('hidden_m.bytes', raw)])
        manual = self.row(path)['tiles']
        for index in (0, 3, 6):
            manual[index]['index'] = -index - 1
        code, saved = self.request('POST', path + '/rows/0/edit', {'tiles': manual, 'revision': 0})
        self.assertEqual(code, 200, saved)
        current_raw = rewrite_tiles(raw, manual)
        next_tiles = analyze_bytes(current_raw)['tiles']; next_tiles[9]['index'] = None
        mapping = list(range(12)); mapping[9] = None
        result = dict(status='repaired', raw=rewrite_tiles(current_raw, next_tiles), origin_indices=mapping)
        with patch('covercheck.repair.auto_repair', return_value=result):
            self.request('POST', path + '/repair', {})
            self.poll(path + '/repair')
        after = self.row(path)
        self.assertEqual(after['repair_status'], 'unrepairable', after['repair_reason'])
        self.assertIn('3张', after['repair_reason'])
        self.assertEqual(after['revision'], 1)
        self.assertEqual(after['tiles'], saved['tiles'])

    def test_floating_upper_is_repaired_and_can_be_exported(self):
        tiles = analyze_bytes(REPAIRABLE)["tiles"]
        coordinates = [(t["x"], t["y"], 2 if t["index"] == 1 else t["z"], t["tile_id"])
                       for t in tiles]
        raw = board("floating", coordinates)
        path = self.submit([("floating_m.bytes", raw)])
        self.assertEqual(self.row(path)["floating_count"], 1)
        code, operation = self.request("POST", path + "/repair", {})
        self.assertEqual(code, 202, operation)
        self.assertEqual(self.poll(path + "/repair")["status"], "succeeded")
        repaired = self.row(path)
        self.assertEqual(repaired["repair_status"], "repaired")
        self.assertTrue(repaired["exportable"])
        self.assertEqual(repaired["issues"], [])
        self.assertEqual(repaired["original_board"]["floating_count"], 1)
        export_id, status = self.export(path)
        self.assertEqual(status["summary"]["exported"], 1)
        code, zipped, _ = self.request("GET", path + "/export/" + export_id, raw=True)
        self.assertEqual(code, 200)
        with zipfile.ZipFile(io.BytesIO(zipped)) as archive:
            self.assertEqual(analyze_bytes(archive.read(archive.namelist()[0]))["issues"], [])

    def test_edit_updates_current_analysis_and_revision_without_touching_source(self):
        before = (self.root / "clean_m.bytes").read_bytes()
        code, created = self.request("POST", "/api/tasks", {"mode": "ids", "ids": ["clean"]})
        self.assertEqual(code, 202)
        path = "/api/tasks/" + created["task_id"]
        self.poll(path)
        tiles = self.row(path)["tiles"]
        tiles[0]["x"] = 3
        code, edited = self.request("POST", path + "/rows/0/edit", {"tiles": tiles, "revision": 0})
        self.assertEqual(code, 200, edited)
        self.assertEqual((edited["revision"], edited["edited"], edited["tiles"][0]["x"]), (1, True, 3))
        self.assertEqual(edited["original_board"]["tiles"][0]["x"], 2)
        self.assertTrue(edited["exportable"])
        self.assertEqual(before, (self.root / "clean_m.bytes").read_bytes())
        code, conflict = self.request("POST", path + "/rows/0/edit", {"tiles": tiles, "revision": 0})
        self.assertEqual((code, conflict["error"]["code"]), (409, "revision_conflict"))
        self.assertEqual(self.row(path)["revision"], 1)

    def test_semantic_edits_and_remaining_fullcovers_are_saved_and_exportable(self):
        path = self.submit([("clean_m.bytes", CLEAN)])
        original = self.row(path)
        for version, transform in enumerate((lambda tiles: tiles[0].update(y=-1), lambda tiles: tiles[0].update(x=1),
                          lambda tiles: tiles[1].update(x=3), lambda tiles: tiles[0].update(z=1))):
            tiles = json.loads(json.dumps(self.row(path)["tiles"]))
            transform(tiles)
            code, result = self.request("POST", path + "/rows/0/edit", {"tiles": tiles, "revision": version})
            self.assertEqual(code, 200, result)
            self.assertTrue(result['exportable'])
            self.assertFalse(result['validation_errors'])
            self.assertEqual(self.row(path)["revision"], version + 1)
        tiles = self.row(path)["tiles"]
        tiles[0].update(x=2, y=2, z=0)
        tiles[1].update(x=2, y=2, z=1)
        code, draft = self.request("POST", path + "/rows/0/edit", {"tiles": tiles, "revision": 4})
        self.assertEqual(code, 200, draft)
        self.assertEqual((draft["revision"], draft["status"]), (5, "issue"))
        self.assertTrue(draft["exportable"])
        export_id, status = self.export(path)
        self.assertEqual(status["status"], "succeeded")
        self.assertEqual(status["summary"], {"total": 1, "exported": 1, "skipped": 0})
        self.assertEqual(self.request("GET", path + "/export/" + export_id, raw=True)[0], 200)

    def test_edit_requires_revision_and_strict_json_tile_shapes(self):
        path = self.submit([("clean_m.bytes", CLEAN)])
        tiles = self.row(path)["tiles"]
        for payload in ({"tiles": tiles}, {"tiles": tiles, "revision": True},
                        {"tiles": [None], "revision": 0}, {"tiles": "bad", "revision": 0},
                        {"tiles": [{"x": True, "y": 2, "z": 0, "tile_id": -10}], "revision": 0}):
            code, _ = self.request("POST", path + "/rows/0/edit", payload)
            self.assertEqual(code, 400)
        self.assertEqual(self.row(path)["revision"], 0)

    def test_clean_invalid_import_is_not_exportable(self):
        invalid = board("invalid", [(2, 2, 0)])
        path = self.submit([("clean_m.bytes", CLEAN), ("invalid_m.bytes", invalid)])
        self.assertFalse(self.row(path, 1)["exportable"])
        self.assertTrue(self.row(path, 1)["validation_errors"])

    def test_noop_save_still_consumes_revision(self):
        path = self.submit([("clean_m.bytes", CLEAN)])
        code, result = self.request("POST", path + "/rows/0/edit", {"tiles": self.row(path)["tiles"], "revision": 0})
        self.assertEqual(code, 200)
        self.assertEqual(result["revision"], 1)
        self.assertFalse(result["edited"])

    def test_auto_repair_isolates_unrepairable_and_corrupt_rows_and_exports_success(self):
        path = self.submit([("repairable_m.bytes", REPAIRABLE), ("issue_m.bytes", ISSUE), ("bad.bytes", b"broken")])
        before = self.row(path, 1)
        code, created = self.request("POST", path + "/repair", {})
        self.assertEqual(code, 202, created)
        done = self.poll(path + "/repair")
        self.assertEqual((done["status"], done["completed"], done["total"]), ("succeeded", 3, 3))
        self.assertEqual([row["status"] for row in done["rows"]], ["repaired", "unrepairable", "unrepairable"])
        self.assertTrue(done["rows"][1]["reason"])
        repaired = self.row(path)
        self.assertEqual((repaired["revision"], repaired["status"]), (1, "clean"))
        self.assertEqual((repaired["original_board"]["adjacent_count"], repaired["adjacent_count"]), (1, 0))
        self.assertEqual(Counter(t["tile_id"] for t in repaired["tiles"]), {-10: 15})
        failed = self.row(path, 1)
        self.assertEqual((failed["revision"], failed["tiles"], failed["issues"]), (0, before["tiles"], before["issues"]))
        export_id, exported = self.export(path)
        self.assertEqual(exported["files"], ["fixrepairable_m.bytes"])
        self.assertEqual(exported["summary"], {"total": 3, "exported": 1, "skipped": 2})
        code, zipped, headers = self.request("GET", path + "/export/" + export_id, raw=True)
        self.assertEqual((code, headers["Content-Type"]), (200, "application/zip"))
        with zipfile.ZipFile(io.BytesIO(zipped)) as package:
            self.assertEqual(package.namelist(), ["fixrepairable_m.bytes"])
            self.assertEqual(analyze_bytes(package.read(package.namelist()[0]))["template_id"], "fixrepairable")

    def test_export_names_are_case_insensitive_protect_all_sources_and_preserve_metadata(self):
        path = self.submit([("source_m.bytes", CLEAN), ("SOURCE_m.bytes", CLEAN), ("other_m.bytes", CLEAN)])
        for index in range(3):
            self.mark_changed(path, index)
        export_id, status = self.export(path, {"pattern": "source", "indices": [0, 1, 2]})
        self.assertEqual(status["files"], ["source_v2_m.bytes", "source_v3_m.bytes", "source_v4_m.bytes"])
        code, raw, _ = self.request("GET", path + "/export/" + export_id, raw=True)
        self.assertEqual(code, 200)
        with zipfile.ZipFile(io.BytesIO(raw)) as package:
            for name in package.namelist():
                data = package.read(name)
                self.assertIn(b"preserve-this-unknown-metadata", data)
                analysis = analyze_bytes(data)
                self.assertEqual(analysis["template_id"], name[:-8])
                self.assertEqual(Counter(t["tile_id"] for t in analysis["tiles"]), {-10: 3})
        self.assertEqual(self.row(path)["template_id"], "source")

    def test_indices_and_names_reject_paths_unknown_placeholders_and_boolean_indices(self):
        path = self.submit([("clean_m.bytes", CLEAN)])
        for pattern in ("../bad", "a/b", "a\\b", "bad\nname", "{unknown}", "..", "CON", "", "a_m.bytes", "\ud800"):
            code, _ = self.request("POST", path + "/export", {"pattern": pattern})
            self.assertEqual(code, 400, pattern)
        for indices in ([True], [1], [-1], [0, 0], "all", []):
            for action in ("repair", "export"):
                code, _ = self.request("POST", path + "/" + action, {"indices": indices, "pattern": "fix{原模板ID}"})
                self.assertEqual(code, 400, (indices, action))

    def test_bad_internal_id_only_skips_its_row_in_named_export(self):
        malicious = board("../unsafe", [(2, 2, 0), (6, 2, 0), (10, 2, 0)])
        path = self.submit([("clean_m.bytes", CLEAN), ("unsafe_m.bytes", malicious)])
        self.mark_changed(path, 0)
        self.mark_changed(path, 1)
        export_id, result = self.export(path)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["files"], ["fixsource_m.bytes"])
        self.assertEqual(result["skipped"][0]["index"], 1)
        self.assertEqual(self.request("GET", path + "/export/" + export_id, raw=True)[0], 200)

    def test_new_mutations_enforce_csrf_and_do_not_leak_exports_in_snapshot(self):
        path = self.submit([("clean_m.bytes", CLEAN)])
        for action, payload in (("repair", {}), ("export", {"pattern": "fix{原模板ID}"}),
                                ("rows/0/edit", {"tiles": self.row(path)["tiles"], "revision": 0})):
            code, _ = self.request("POST", path + "/" + action, payload, headers={"X-CSRF-Token": ""})
            self.assertEqual(code, 403)
        export_id, _ = self.export(path)
        snapshot = self.request("GET", path)[1]
        self.assertNotIn("exports", snapshot)
        self.assertNotIn("states", snapshot)
        self.assertEqual(self.request("GET", path + "/export/" + "f" * 24 + "/status")[0], 404)

    def test_running_repair_locks_edits_exports_and_duplicate_repair(self):
        path = self.submit([("clean_m.bytes", CLEAN)])
        from covercheck.repair import auto_repair
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def delayed(raw):
            entered.set()
            release.wait(3)
            return auto_repair(raw)

        with patch("covercheck.repair.auto_repair", side_effect=delayed):
            self.assertEqual(self.request("POST", path + "/repair", {})[0], 202)
            self.assertTrue(entered.wait(2))
            for action, payload in (("repair", {}), ("export", {"pattern": "fix{原模板ID}"}),
                                    ("rows/0/edit", {"tiles": self.row(path)["tiles"], "revision": 0})):
                code, result = self.request("POST", path + "/" + action, payload)
                self.assertEqual((code, result["error"]["code"]), (409, "repair_running"))
            release.set()
            self.assertEqual(self.poll(path + "/repair")["status"], "succeeded")

    def test_running_export_reports_progress_and_locks_all_current_board_mutations(self):
        path = self.submit([("clean_m.bytes", CLEAN), ("second_m.bytes", CLEAN)])
        self.mark_changed(path, 0)
        self.mark_changed(path, 1)
        from covercheck.repair import validate_board
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def delayed(raw):
            entered.set()
            release.wait(3)
            return validate_board(raw)

        with patch("covercheck.repair.validate_board", side_effect=delayed):
            code, created = self.request("POST", path + "/export", {"pattern": "fix{原模板ID}"})
            self.assertEqual(code, 202)
            self.assertTrue(entered.wait(2))
            export_path = path + "/export/" + created["export_id"]
            code, progress = self.request("GET", export_path + "/status")
            self.assertEqual((code, progress["status"], progress["total"], progress["completed"]), (200, "running", 2, 0))
            self.assertTrue(progress["current"])
            for action, payload in (("repair", {}), ("export", {"pattern": "fix{原模板ID}"}),
                                    ("rows/0/edit", {"tiles": self.row(path)["tiles"], "revision": 0})):
                code, result = self.request("POST", path + "/" + action, payload)
                self.assertEqual((code, result["error"]["code"]), (409, "export_running"))
            self.assertEqual(self.request("GET", export_path, raw=True)[0], 409)
            release.set()
            done = self.poll(export_path + "/status")
        self.assertEqual((done["status"], done["completed"], len(done["files"])), ("succeeded", 2, 2))

    def test_launcher_only_reuses_matching_api_version_and_ignores_malformed_health(self):
        health = {"service_id": server.SERVICE_ID, "instance_id": server.INSTANCE_ID, "version": "1.0.0"}

        class Health(BaseHTTPRequestHandler):
            def do_GET(self):
                raw = json.dumps(health).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        old = ThreadingHTTPServer(("127.0.0.1", 0), Health)
        worker = threading.Thread(target=old.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(old.server_close)
        self.addCleanup(old.shutdown)
        for payload, want in ((health, False),
                              ({"service_id": server.SERVICE_ID, "instance_id": server.INSTANCE_ID, "version": server.VERSION}, True),
                              ([], False)):
            with self.subTest(payload=payload):
                health = payload
                self.assertEqual(launcher.is_our_server(old.server_port), want)


if __name__ == "__main__":
    unittest.main()
