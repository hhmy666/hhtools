from __future__ import annotations

import base64
import errno
import io
import math
import sys
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch


SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

import app  # noqa: E402
from app import (  # noqa: E402
    APP_VERSION,
    SERVICE_ID,
    SERVICE_PROTOCOL,
    Application,
    _artifact_kind,
    _decode_csv_preview,
    _is_existing_tool,
    _loopback_host,
    _read_local_json,
    _safe_open_browser,
    serve,
)


class ArtifactPreviewTests(unittest.TestCase):
    def test_config_copy_is_not_misclassified_as_mapping_audit(self) -> None:
        self.assertEqual(
            _artifact_kind("level_list_copy_path", "config_level_list_100-199_重排副本.csv", "online"),
            "config_level_list_copy",
        )
        self.assertEqual(
            _artifact_kind("level_list_audit_path", "config_level_list_100-199_映射审计.csv", "online"),
            "config_level_list_audit",
        )

    def test_csv_preview_keeps_all_hundred_rows_searchable(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "result.csv"
            path.write_text("level_index,label\n" + "\n".join(f"{100 + i},L{i}" for i in range(100)), encoding="utf-8")
            preview = _decode_csv_preview(path)
            self.assertIsNotNone(preview)
            self.assertEqual(preview["shown_rows"], 100)
            self.assertFalse(preview["truncated"])
from services.engine_adapter import AnalysisEngineAdapter  # noqa: E402
from services.source_service import (  # noqa: E402
    SourceValidationError,
    inspect_csv_bytes,
    json_safe,
    normalize_settings,
    persist_source,
)
from services.storage import JobRepository, atomic_write_json  # noqa: E402


PLAYER_CSV = (
    "user_id,level_index,level_template_id,replay_info,level_status\n"
    "u1,100,n6209,0#1:1-2-3,pass\n"
).encode("utf-8")
ONLINE_CSV = (
    "level_index,level_id,level_template_id,进入人数,人均消耗,失败流失\n"
    "100,list100,n6209,1000,1.5,0.8%\n"
).encode("utf-8")


def source_payload(data: bytes, name: str) -> dict:
    return {
        "kind": "files",
        "files": [
            {
                "name": name,
                "size": len(data),
                "content_base64": base64.b64encode(data).decode("ascii"),
            }
        ],
    }


def fake_engine() -> AnalysisEngineAdapter:
    module = types.ModuleType("fake_analysis_engine")
    module.ENGINE_VERSION = "test"

    def preflight_player(source_files, settings, work_dir):
        return {"valid": True, "engine_marker": "player", "errors": [], "warnings": []}

    def run_player_analysis(
        source_files,
        settings,
        work_dir,
        output_dir,
        progress_callback=None,
        log_callback=None,
    ):
        if progress_callback:
            progress_callback(25, "解析回放")
        if log_callback:
            log_callback("测试分析已启动")
        output_dir = Path(output_dir)
        workbook = output_dir / "table1.xlsx"
        workbook.write_bytes(b"test workbook")
        levels = output_dir / "levels.csv"
        levels.write_text("level_index,label\n100,L2\n", encoding="utf-8")
        return {
            "workbook_path": workbook,
            "levels_csv_path": levels,
            "counts": {"levels": 1, "non_finite": math.nan},
            "first_level_label_counts": {"整体未见稳定高负担": 1},
            "coverage": {"geometry_click_share": 0.75},
            "custom": {"preserved": True},
        }

    def preflight_online(source_files, settings, player_job, work_dir):
        return {"valid": True, "engine_marker": "online", "errors": [], "warnings": []}

    def run_online_analysis(
        source_files,
        settings,
        player_job,
        work_dir,
        output_dir,
        progress_callback=None,
        log_callback=None,
    ):
        if progress_callback:
            progress_callback(70, "计算三维分类")
        output_dir = Path(output_dir)
        workbook = output_dir / "table2.xlsx"
        workbook.write_bytes(b"test workbook 2")
        recommendation = output_dir / "recommendation.csv"
        recommendation.write_text(
            "level_index,tag,recommended_index\n100,T2,109\n", encoding="utf-8"
        )
        return {
            "workbook_path": workbook,
            "recommendation_csv_path": recommendation,
            "metadata": {"classified_levels": 1},
        }

    module.preflight_player = preflight_player
    module.run_player_analysis = run_player_analysis
    module.preflight_online = preflight_online
    module.run_online_analysis = run_online_analysis
    return AnalysisEngineAdapter(module=module)


class SourceServiceTests(unittest.TestCase):
    def test_csv_inspection_and_schema(self):
        audit = inspect_csv_bytes("玩家.csv", PLAYER_CSV)
        self.assertEqual(audit["row_count"], 1)
        self.assertIn("replay_info", audit["columns"])

    def test_rejects_unsafe_filename(self):
        with TemporaryDirectory() as temporary:
            with self.assertRaises(SourceValidationError):
                persist_source(
                    {
                        "kind": "files",
                        "files": [
                            {
                                "name": "../escape.csv",
                                "size": len(PLAYER_CSV),
                                "content_base64": base64.b64encode(PLAYER_CSV).decode(),
                            }
                        ],
                    },
                    destination=Path(temporary) / "input",
                    paste_name="paste.csv",
                )

    def test_non_finite_numbers_become_null(self):
        self.assertIsNone(json_safe(float("nan")))
        self.assertIsNone(json_safe(float("inf")))

    def test_column_count_mismatch_is_blocking(self):
        malformed = (
            "user_id,level_index,level_template_id,replay_info,level_status\n"
            "u1,100,n6209,0#1:1-2-3\n"
        ).encode("utf-8")
        with TemporaryDirectory() as temporary:
            _paths, audit = persist_source(
                source_payload(malformed, "malformed.csv"),
                destination=Path(temporary) / "input",
                paste_name="paste.csv",
            )
        self.assertFalse(audit["valid"])
        self.assertIn("column_count_mismatch", {item["code"] for item in audit["errors"]})

    def test_history_mode_is_explicit_and_audited(self):
        historical = normalize_settings(
            {"history_ref": "historical"},
            default_config_path=Path("/tmp/config"),
            default_template_path=Path("/tmp/templates"),
        )
        self.assertEqual(historical["history_mode"], "historical")
        self.assertTrue(historical["level_tile_git_revision"])
        current = normalize_settings(
            {"history_ref": "current"},
            default_config_path=Path("/tmp/config"),
            default_template_path=Path("/tmp/templates"),
        )
        self.assertEqual(current["level_tile_git_revision"], "")
        with self.assertRaises(SourceValidationError):
            normalize_settings(
                {"history_ref": "unreviewed-branch"},
                default_config_path=Path("/tmp/config"),
                default_template_path=Path("/tmp/templates"),
            )


class StorageTests(unittest.TestCase):
    def test_atomic_json_has_no_partial_temp_file(self):
        with TemporaryDirectory() as temporary:
            target = Path(temporary) / "state.json"
            atomic_write_json(target, {"value": 1})
            self.assertEqual(target.read_text(encoding="utf-8").strip(), '{\n  "value": 1\n}')
            self.assertEqual(list(Path(temporary).glob("*.tmp")), [])

    def test_recovery_marks_running_job_interrupted(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs = JobRepository(root / "jobs", root / "outputs")
            job_dir = root / "jobs" / "job-20260918-120000-abc123"
            job_dir.mkdir()
            atomic_write_json(
                job_dir / "state.json",
                {
                    "job_id": job_dir.name,
                    "status": "running",
                    "updated_at": "2026-09-18T12:00:00+08:00",
                    "logs": [],
                },
            )
            recovered = jobs.discover()
            self.assertEqual(recovered[0]["status"], "interrupted")


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "config").mkdir()
        (self.root / "templates").mkdir()
        self.app = Application(tool_root=self.root, engine=fake_engine())
        self.settings = {
            "config_path": str(self.root / "config"),
            "template_path": str(self.root / "templates"),
            "history_ref": "",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def wait_for(self, job_id: str, expected_stage: str) -> dict:
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            state = self.app.jobs.load(job_id)
            if state.get("stage") == expected_stage:
                return state
            time.sleep(0.02)
        self.fail(f"job did not reach {expected_stage}")

    def test_two_stage_job_and_artifact_whitelist(self):
        player_preflight = self.app.preflight_player(
            {"source": source_payload(PLAYER_CSV, "players.csv"), "settings": self.settings}
        )
        self.assertTrue(player_preflight["audit"]["valid"])
        self.assertEqual(player_preflight["audit"]["engine_marker"], "player")
        job_id = self.app.start_player({"draft_id": player_preflight["draft_id"]})["job_id"]
        player_state = self.wait_for(job_id, "player_completed")
        self.assertEqual(player_state["status"], "succeeded")
        self.assertIsNone(player_state["summary"]["player"]["non_finite"])
        self.assertEqual(
            player_state["summary"]["player"]["first_level_label_counts"],
            {"整体未见稳定高负担": 1},
        )
        self.assertIn("behavior_workbook", {item["kind"] for item in player_state["artifacts"]})
        self.assertIn("behavior_levels", {item["kind"] for item in player_state["artifacts"]})
        self.assertGreaterEqual(len(player_state["artifacts"]), 2)

        online_preflight = self.app.attach_online_preflight(
            {
                "job_id": job_id,
                "source": source_payload(ONLINE_CSV, "online.csv"),
                "settings": self.settings,
            }
        )
        self.assertTrue(online_preflight["audit"]["valid"])
        self.assertNotIn("online", self.app.jobs.load(job_id).get("source", {}))
        self.app.start_online(job_id, {"draft_id": online_preflight["draft_id"]})
        online_state = self.wait_for(job_id, "online_completed")
        self.assertEqual(online_state["summary"]["online"]["classified_levels"], 1)
        artifact = online_state["artifacts"][0]
        path, _ = self.app.artifact_path(job_id, artifact["id"])
        self.assertTrue(path.is_file())
        with self.assertRaises(FileNotFoundError):
            self.app.artifact_path(job_id, "a-not-whitelisted")

    def test_online_preflight_rejects_busy_job(self):
        player_preflight = self.app.preflight_player(
            {"source": source_payload(PLAYER_CSV, "players.csv"), "settings": self.settings}
        )
        job_id = self.app.start_player({"draft_id": player_preflight["draft_id"]})["job_id"]
        self.wait_for(job_id, "player_completed")

        def mark_busy(state):
            state["status"] = "running"
            state["stage"] = "online"

        self.app.jobs.update(job_id, mark_busy)
        with self.assertRaises(Exception):
            self.app.preflight_online(
                {
                    "job_id": job_id,
                    "source": source_payload(ONLINE_CSV, "online.csv"),
                    "settings": self.settings,
                }
            )

    def test_invalid_preflight_cannot_start(self):
        mismatched = (
            "other,column\n1,2\n".encode("utf-8")
        )
        result = self.app.preflight_player(
            {
                "source": {
                    "kind": "files",
                    "files": [
                        source_payload(PLAYER_CSV, "a.csv")["files"][0],
                        source_payload(mismatched, "b.csv")["files"][0],
                    ],
                },
                "settings": self.settings,
            }
        )
        self.assertFalse(result["audit"]["valid"])
        with self.assertRaises(Exception):
            self.app.start_player({"draft_id": result["draft_id"]})


class CliSafetyTests(unittest.TestCase):
    def test_only_loopback_hosts_are_accepted(self):
        self.assertEqual(_loopback_host("localhost"), "127.0.0.1")
        with self.assertRaises(Exception):
            _loopback_host("0.0.0.0")
        with self.assertRaises(Exception):
            _loopback_host("::1")
        with self.assertRaises(Exception):
            _loopback_host("[::1]")

    def test_reuses_existing_tool_instead_of_binding_again(self):
        occupied = OSError(errno.EADDRINUSE, "Address already in use")
        with (
            patch("app._is_existing_tool", return_value=True),
            patch("app.Application") as application,
            patch("app.LocalThreadingHTTPServer", side_effect=occupied),
            patch("app.webbrowser.open") as open_browser,
        ):
            result = serve(host="127.0.0.1", port=8765, open_browser=True)
        self.assertEqual(result, 0)
        application.assert_not_called()
        open_browser.assert_called_once_with("http://127.0.0.1:8765/")

    def test_unrelated_port_occupant_has_friendly_error(self):
        occupied = OSError(errno.EADDRINUSE, "Address already in use")
        error_output = io.StringIO()
        with (
            patch("app._is_existing_tool", return_value=False),
            patch("app.time.sleep"),
            patch("app.Application") as application,
            patch("app.LocalThreadingHTTPServer", side_effect=occupied),
            redirect_stderr(error_output),
        ):
            result = serve(host="127.0.0.1", port=8765, open_browser=False)
        self.assertEqual(result, 2)
        application.assert_not_called()
        self.assertIn("端口 8765 已被其他程序占用", error_output.getvalue())
        self.assertNotIn("Traceback", error_output.getvalue())

    def test_existing_tool_identity_is_strict_with_narrow_legacy_support(self):
        current = {
            "status": "ready",
            "version": APP_VERSION,
            "service_id": SERVICE_ID,
            "protocol": SERVICE_PROTOCOL,
        }
        with patch("app._read_local_json", return_value=(current, "Other/1.0")):
            self.assertTrue(_is_existing_tool("127.0.0.1", 8765))
        wrong = {**current, "service_id": "another-service"}
        with patch("app._read_local_json", return_value=(wrong, "Other/1.0")):
            self.assertFalse(_is_existing_tool("127.0.0.1", 8765))
        legacy = {"status": "ready", "version": APP_VERSION}
        with patch(
            "app._read_local_json",
            return_value=(legacy, "PlayerBehaviourLocal/1.0 Python/3.9"),
        ):
            self.assertTrue(_is_existing_tool("127.0.0.1", 8765))

    def test_local_probe_rejects_redirect_html_oversize_invalid_json_and_timeout(self):
        class FakeResponse:
            def __init__(self, status, content_type, body, content_length=None):
                self.status = status
                self.body = body
                self.headers = {
                    "Content-Type": content_type,
                    "Server": "Other/1.0",
                }
                if content_length is not None:
                    self.headers["Content-Length"] = str(content_length)

            def getheader(self, name, default=None):
                return self.headers.get(name, default)

            def read(self, amount):
                return self.body[:amount]

        def probe(response=None, request_error=None):
            connection = types.SimpleNamespace(
                request=Mock(side_effect=request_error),
                getresponse=Mock(return_value=response),
                close=Mock(),
            )
            with patch("app.http.client.HTTPConnection", return_value=connection):
                result = _read_local_json(
                    "127.0.0.1",
                    8765,
                    "/api/health",
                    timeout=0.1,
                    max_bytes=4096,
                )
            connection.close.assert_called_once_with()
            return result

        self.assertIsNone(probe(FakeResponse(302, "application/json", b"{}")))
        self.assertIsNone(probe(FakeResponse(200, "text/html", b"<html></html>")))
        self.assertIsNone(
            probe(FakeResponse(200, "application/json", b"{}", content_length=5000))
        )
        self.assertIsNone(probe(FakeResponse(200, "application/json", b"not-json")))
        self.assertIsNone(probe(request_error=TimeoutError("timed out")))
        valid = probe(FakeResponse(200, "application/json", b'{"status":"ready"}'))
        self.assertEqual(valid[0], {"status": "ready"})

    def test_application_is_created_only_after_successful_bind(self):
        events = []

        class FakeServer:
            server_address = ("127.0.0.1", 8765)
            application = None

            def serve_forever(self, poll_interval):
                events.append("serve")
                raise KeyboardInterrupt

            def server_close(self):
                events.append("close")

        def bind(*_args, **_kwargs):
            events.append("bind")
            return FakeServer()

        def build_application():
            events.append("application")
            return object()

        with (
            patch("app.LocalThreadingHTTPServer", side_effect=bind),
            patch("app.Application", side_effect=build_application),
        ):
            self.assertEqual(serve(host="127.0.0.1", port=8765, open_browser=False), 0)
        self.assertEqual(events, ["bind", "application", "serve", "close"])

    def test_application_failure_closes_bound_socket(self):
        server = types.SimpleNamespace(server_close=Mock())
        with (
            patch("app.LocalThreadingHTTPServer", return_value=server),
            patch("app.Application", side_effect=RuntimeError("broken")),
            self.assertRaises(RuntimeError),
        ):
            serve(host="127.0.0.1", port=8765, open_browser=False)
        server.server_close.assert_called_once_with()

    def test_browser_open_failure_prints_manual_url_without_traceback(self):
        output = io.StringIO()
        with (
            patch("app.webbrowser.open", side_effect=OSError("no browser")),
            redirect_stdout(output),
        ):
            self.assertFalse(_safe_open_browser("http://127.0.0.1:8765/"))
        self.assertIn("请手动访问", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())


class FrontendCopyContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        frontend_root = SCRIPT_ROOT.parent / "frontend"
        cls.html = (frontend_root / "index.html").read_text(encoding="utf-8")
        cls.javascript = (frontend_root / "assets" / "app.js").read_text(encoding="utf-8")

    def test_player_import_copy_is_chinese_and_names_redash_query(self):
        self.assertIn('id="player-import-title">玩家数据导入</h2>', self.html)
        self.assertIn("请使用 Redash 查询「玩家单关 TileNum」导出数据。", self.html)
        self.assertNotIn('class="eyebrow"', self.html)

    def test_service_status_and_settings_boundary_copy(self):
        self.assertIn('state.serviceOk ? "本地服务已连接"', self.javascript)
        self.assertNotIn("本地服务已连接 · 数据不出本机", self.javascript)
        self.assertNotIn("本机运行边界", self.html)
        self.assertIn("工具服务仅绑定本机 127.0.0.1", self.html)

    def test_partial_replay_warning_is_visible_in_success_state(self):
        self.assertIn("预检通过，部分数据已自动处理", self.javascript)
        self.assertIn("skipped_empty_replay_rows", self.javascript)

    def test_behavior_result_has_one_page_title_and_actionable_guidance(self):
        self.assertEqual(self.html.count("实时行为分层已完成"), 0)
        self.assertIn('id="behavior-header-download"', self.html)
        self.assertIn('class="result-guidance"', self.html)
        self.assertIn('class="insight-copy"', self.javascript)
        self.assertIn("建议至少再补", self.javascript)
        self.assertNotIn('stages = [{ name: currentStage', self.javascript)

    def test_running_view_uses_real_backend_progress_and_full_width_logs(self):
        self.assertIn('id="player-job-progress"', self.html)
        self.assertIn("progressData.percent", self.javascript)
        self.assertIn("进度仅显示后台已确认的数据", self.javascript)

    def test_successful_supplement_preflight_invalidates_stale_results(self):
        self.assertIn('id="behavior-add-samples"', self.html)
        self.assertIn("invalidateAnalysisAfterPlayerInputChange();", self.javascript)
        self.assertIn("已合并并检查当前", self.javascript)
        self.assertIn("将按全部文件重新分析", self.javascript)
        self.assertIn("sampleSummary.players", self.javascript)


class LegacyRecommendationTests(unittest.TestCase):
    def test_public_job_backfills_instance_aware_recommendation(self):
        job_id = "job-legacy-sample"
        with TemporaryDirectory() as temp, patch("app.OUTPUT_ROOT", Path(temp)):
            output = Path(temp) / job_id
            output.mkdir(parents=True, exist_ok=True)
            levels = output / "levels.csv"
            instances = output / "instances.csv"
            levels.write_text(
                "level_index,first_level_behavior_label\n100,样本覆盖不足\n",
                encoding="utf-8",
            )
            instances.write_text(
                "level_index,attempt_phase,player_count\n"
                + "\n".join(f"100,首局,9" for _ in range(10))
                + "\n",
                encoding="utf-8",
            )
            state = {
                "job_id": job_id,
                "summary": {"player": {"players": 90}},
                "engine_results": {
                    "player": {
                        "levels_csv_path": str(levels),
                        "instances_csv_path": str(instances),
                    }
                },
            }
            recommendation = app._public_job(state)["summary"]["player"]["sample_recommendation"]
            self.assertEqual(recommendation["recommended_additional_players"], 20)
            self.assertEqual(recommendation["recommended_total_players"], 110)


if __name__ == "__main__":
    unittest.main()
