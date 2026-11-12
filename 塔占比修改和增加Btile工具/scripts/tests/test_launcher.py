"""Launcher-facing server identity and loopback port selection."""

from __future__ import annotations

import http.client
import importlib
import json
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


TOOL_ROOT = Path(__file__).resolve().parents[2]


def app_module():
    return importlib.import_module("scripts.app")


class LauncherTests(unittest.TestCase):
    def test_launcher_is_executable_and_invokes_local_app(self):
        launcher = TOOL_ROOT / "启动塔占比修改及增加Btile工具.command"
        self.assertTrue(launcher.is_file())
        self.assertTrue(launcher.stat().st_mode & 0o111)
        content = launcher.read_text(encoding="utf-8")
        self.assertIn("scripts/app.py", content)
        self.assertIn("--open", content)

    def test_health_identifies_service_and_selected_port_on_loopback(self):
        app = app_module()
        port, server, reused = app.select_server(start_port=0, end_port=0)
        self.assertFalse(reused)
        self.assertIsNotNone(server)
        self.assertEqual(server.server_address[0], "127.0.0.1")
        server.application = object()
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            connection.request("GET", "/api/health")
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["service_id"], app.SERVICE_ID)
            self.assertEqual(payload["version"], app.APP_VERSION)
            self.assertEqual(payload["protocol"], 1)
            self.assertEqual(payload["status"], "ready")
            self.assertIs(payload["local_only"], True)
            self.assertEqual(payload["port"], port)
            selected_port, another_server, reused = app.select_server(start_port=port, end_port=port)
            self.assertEqual(selected_port, port)
            self.assertIsNone(another_server)
            self.assertTrue(reused)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_unrelated_listener_causes_port_fallback(self):
        app = app_module()
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        blocked_port = blocker.getsockname()[1]
        try:
            selected_port, server, reused = app.select_server(
                start_port=blocked_port,
                end_port=min(blocked_port + 10, 65535),
            )
            self.assertFalse(reused)
            self.assertGreater(selected_port, blocked_port)
            self.assertLessEqual(selected_port, min(blocked_port + 10, 65535))
            self.assertEqual(server.server_address[0], "127.0.0.1")
            server.server_close()
        finally:
            blocker.close()

    def test_reuses_higher_port_service_even_when_lower_port_is_free(self):
        app = app_module()
        for _ in range(20):
            existing = app.LocalServer((app.HOST, 0), app.RequestHandler)
            higher_port = existing.server_port
            lower_port = higher_port - 1
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                    probe.bind((app.HOST, lower_port))
            except OSError:
                existing.server_close()
                continue
            break
        else:
            self.fail("Could not find adjacent free loopback ports for regression test")

        worker = threading.Thread(target=existing.serve_forever, daemon=True)
        existing.application = object()
        worker.start()
        created = None
        try:
            selected_port, created, reused = app.select_server(
                start_port=lower_port, end_port=higher_port
            )
            self.assertEqual(selected_port, higher_port)
            self.assertIsNone(created)
            self.assertTrue(reused)
        finally:
            if created is not None:
                created.server_close()
            existing.shutdown()
            existing.server_close()
            worker.join(timeout=2)

    def test_default_port_window_is_8766_through_8775(self):
        app = app_module()
        self.assertEqual((app.DEFAULT_PORT, app.LAST_PORT), (8766, 8775))

    def test_old_protocol_version_or_other_tool_instance_is_never_reused(self):
        app = app_module()

        class HealthHandler(BaseHTTPRequestHandler):
            payload = {}

            def do_GET(self):
                raw = json.dumps(self.payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, format, *args):
                pass

        valid = {"service_id": app.SERVICE_ID, "version": app.APP_VERSION,
                 "protocol": app.PROTOCOL_VERSION, "instance_id": app.TOOL_INSTANCE_ID,
                 "status": "ready", "local_only": True}
        variants = (
            dict(valid, version="0.1.0"),
            dict(valid, protocol=app.PROTOCOL_VERSION + 1),
            dict(valid, instance_id="different-copy"),
            dict(valid, status="starting"),
        )
        for payload in variants:
            with self.subTest(payload=payload):
                HealthHandler.payload = payload
                server = ThreadingHTTPServer((app.HOST, 0), HealthHandler)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    self.assertFalse(app.is_same_service(server.server_port))
                finally:
                    server.shutdown(); server.server_close(); worker.join(timeout=2)

    def test_main_initializes_and_attaches_application_before_opening_browser(self):
        app = app_module()
        events = []

        class FakeServer:
            application = None

            def serve_forever(self):
                events.append(("serve", self.application))

            def server_close(self):
                events.append(("close", self.application))

        server = FakeServer()
        application = object()

        def initialize():
            events.append(("initialized", None))
            return application

        def opened(address):
            events.append(("open", server.application))
            return True

        with patch.object(app, "select_server", return_value=(8766, server, False)), \
                patch.object(app, "_default_application", side_effect=initialize), \
                patch.object(app.webbrowser, "open", side_effect=opened), \
                patch.object(sys, "argv", ["app.py", "--open"]):
            self.assertEqual(app.main(), 0)
        self.assertEqual(events[0][0], "initialized")
        self.assertIs(events[1][1], application)
        self.assertEqual(events[1][0], "open")
        self.assertEqual(events[2][0], "serve")

    def test_main_does_not_open_browser_when_application_initialization_fails(self):
        app = app_module()

        class FakeServer:
            application = None
            closed = False

            def server_close(self):
                self.closed = True

        server = FakeServer()
        with patch.object(app, "select_server", return_value=(8766, server, False)), \
                patch.object(app, "_default_application", side_effect=RuntimeError("init failed")), \
                patch.object(app.webbrowser, "open") as opened, \
                patch.object(sys, "argv", ["app.py", "--open"]):
            with self.assertRaises(RuntimeError):
                app.main()
        self.assertTrue(server.closed)
        opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
