#!/usr/bin/env python3
"""Loopback-only HTTP adapter for the tower ratio reducer."""

from __future__ import annotations

import argparse
import errno
from email.parser import BytesParser
from email.policy import default as EMAIL_POLICY
import http.client
import hashlib
import json
import mimetypes
from pathlib import Path
import re
import secrets
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple


TOOL_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_ROOT = TOOL_ROOT / "frontend"
SERVICE_ID = "tile-family.tower-ratio-reducer-tool"
APP_VERSION = "0.9.2"
HOST = "127.0.0.1"
DEFAULT_PORT = 8766
LAST_PORT = 8775
PROTOCOL_VERSION = 1
TOOL_INSTANCE_ID = hashlib.sha256(str(TOOL_ROOT.resolve()).encode("utf-8")).hexdigest()[:24]


def _api_types():
    try:
        from scripts.tower_reducer.api import ApiError, Application
    except ImportError:
        from tower_reducer.api import ApiError, Application
    return ApiError, Application


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler_or_application=None):
        application = None
        handler = RequestHandler
        if isinstance(handler_or_application, type) and issubclass(handler_or_application, BaseHTTPRequestHandler):
            handler = handler_or_application
        elif handler_or_application is not None:
            application = handler_or_application
        super().__init__(server_address, handler)
        self.application = application


def create_server(address, application):
    """Create the public DI server; public binds are intentionally refused."""
    host, port = address
    if host not in (HOST, "localhost"):
        raise ValueError("local service may bind only to loopback")
    return LocalServer((HOST, port), application)


class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_PUT(self): self._handle("PUT")
    def do_PATCH(self): self._handle("PATCH")
    def do_DELETE(self): self._handle("DELETE")
    def do_OPTIONS(self): self._handle("OPTIONS")

    @property
    def application(self):
        return getattr(self.server, "application", None)

    def _handle(self, method):
        ApiError, _ = _api_types()
        request_id = "req-" + secrets.token_urlsafe(12)
        try:
            self._validate_host()
            path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
            if method in {"POST", "PUT", "PATCH", "DELETE"}:
                self._validate_write_request()
            if path.startswith("/api/") or path == "/api":
                status, value, headers = self._dispatch_api(method, path)
                if isinstance(value, bytes):
                    headers = dict(headers)
                    content_type = headers.pop("Content-Type", "application/octet-stream")
                    self._respond(status, value, content_type, headers)
                else:
                    self._json(status, value, headers)
                return
            if method != "GET":
                raise ApiError(405, "method_not_allowed", "该路径不支持此请求方法")
            self._serve_static(path)
        except ApiError as error:
            payload = {"error": {"protocol": PROTOCOL_VERSION, "code": error.code,
                                 "message": error.message, "request_id": request_id}}
            if error.details is not None:
                payload["error"]["details"] = error.details
            self._json(error.status, payload)
        except Exception:
            payload = {"error": {"protocol": PROTOCOL_VERSION, "code": "internal_error",
                                 "message": "本地服务处理失败，请重试", "request_id": request_id}}
            self._json(500, payload)

    def _validate_host(self):
        ApiError, _ = _api_types()
        allowed = {"127.0.0.1:{}".format(self.server.server_port),
                   "localhost:{}".format(self.server.server_port)}
        if self.headers.get("Host") not in allowed:
            raise ApiError(403, "host_rejected", "请从当前本地服务地址访问")

    def _validate_write_request(self):
        ApiError, _ = _api_types()
        app = self.application
        if app is None:
            raise ApiError(503, "service_not_ready", "本地服务尚未就绪")
        if self.headers.get("Sec-Fetch-Site", "").lower() in ("cross-site", "none"):
            raise ApiError(403, "origin_rejected", "已拒绝跨站请求")
        origins = {"http://127.0.0.1:{}".format(self.server.server_port),
                   "http://localhost:{}".format(self.server.server_port)}
        origin = self.headers.get("Origin")
        if origin is not None:
            if origin not in origins:
                raise ApiError(403, "origin_rejected", "请求来源不是当前本地服务")
        else:
            referer = self.headers.get("Referer", "")
            if not any(referer == item or referer.startswith(item + "/") for item in origins):
                raise ApiError(403, "origin_rejected", "请求缺少同源依据")
        supplied = self.headers.get("X-CSRF-Token", "")
        if not isinstance(supplied, str) or not secrets.compare_digest(supplied, app.csrf_token):
            raise ApiError(403, "csrf_failed", "页面令牌无效，请刷新后重试")

    def _dispatch_api(self, method, path):
        ApiError, _ = _api_types()
        app = self.application
        if path == "/api/health" and method == "GET":
            ready = app is not None and bool(getattr(app, "ready", True))
            return 200, {"service_id": SERVICE_ID, "version": APP_VERSION,
                         "protocol": PROTOCOL_VERSION,
                         "instance_id": TOOL_INSTANCE_ID,
                         "status": "ready" if ready else "starting",
                         "local_only": True, "port": self.server.server_port}, {}
        if app is None:
            raise ApiError(503, "service_not_ready", "本地服务尚未就绪")
        if path == "/api/bootstrap" and method == "GET":
            return 200, app.bootstrap(), {}
        if path == "/api/settings" and method == "GET":
            return 200, app.get_settings(), {}
        if path == "/api/settings" and method == "PUT":
            return 200, app.update_settings(self._read_json()), {}
        if path == "/api/import" and method == "POST":
            return 200, app.import_files(self._read_multipart()), {}
        if path == "/api/import-ids" and method == "POST":
            return 200, app.import_ids(self._read_json()), {}
        if path == "/api/imports/merge" and method == "POST":
            return 200, app.merge_imports(self._read_json()), {}
        if path == "/api/import-rejections" and method == "POST":
            return 200, app.import_rejections(self._read_json()), {}
        match = re.fullmatch(r"/api/imports/([^/]*)/preview/([^/]*)", path)
        if match and method == "GET":
            return 200, app.import_source_preview(match.group(1), match.group(2)), {}
        if path == "/api/increase/preview" and method == "POST":
            return 200, app.increase_preview(self._read_json()), {}
        if path == "/api/ordinary-add/preview" and method == "POST":
            return 200, app.ordinary_add_preview(self._read_json()), {}
        if path == "/api/parameters/context" and method == "GET":
            return 200, app.parameter_flow.context(), {}
        if path == "/api/parameters/upload" and method == "POST":
            return 200, app.import_files(self._read_multipart(), _inspect=False), {}
        if path == "/api/parameters/start" and method == "POST":
            return 200, app.parameter_flow.start(self._read_json()), {}
        match = re.fullmatch(r"/api/parameters/tasks/(parameter-task-[0-9a-f]{32})", path)
        if match and method == "GET":
            return 200, app.parameter_flow.task_status(match.group(1)), {}
        if path in ("/api/parameters/resolve", "/api/parameters/calculate", "/api/parameters/preview", "/api/parameters/commit") and method == "POST":
            return 200, getattr(app.parameter_flow, path.rsplit('/', 1)[1])(self._read_json()), {}
        match = re.fullmatch(r"/api/parameters/(calc-[0-9a-f]{32})/download", path)
        if match and method == "GET":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            raw, content_type, filename = app.parameter_flow.download(match.group(1), query.get('format', ['xlsx'])[0])
            return 200, raw, {"Content-Type": content_type, "Content-Disposition":
                'attachment; filename="parameter-report.' + ('csv' if filename.endswith('.csv') else 'xlsx') +
                '"; filename*=UTF-8\'\'' + urllib.parse.quote(filename)}
        if path == "/api/jobs" and method == "POST":
            return 202, app.create_job(self._read_json()), {}
        if path == "/api/sync/plan" and method == "POST":
            return 200, app.sync_plan(self._read_json()), {}
        if path == "/api/sync/commit" and method == "POST":
            return 200, app.sync_commit(self._read_json()), {}
        if path == "/api/open-directory" and method == "POST":
            return 200, app.open_directory(self._read_json()), {}
        match = re.fullmatch(r"/api/jobs/(job-[0-9a-f]{32})", path)
        if match and method == "GET":
            return 200, app.job_payload(match.group(1)), {}
        match = re.fullmatch(r"/api/jobs/(job-[0-9a-f]{32})/preview/(tpl-[0-9a-f]{32})", path)
        if match and method == "GET":
            return 200, app.template_preview(match.group(1), match.group(2)), {}
        match = re.fullmatch(r"/api/jobs/(job-[0-9a-f]{32})/rerun/(tpl-[0-9a-f]{32})", path)
        if match and method == "POST":
            return 202, app.rerun(match.group(1), match.group(2), self._read_json()), {}
        match = re.fullmatch(r"/api/jobs/(job-[0-9a-f]{32})/translate/(tpl-[0-9a-f]{32})", path)
        if match and method == "POST":
            return 200, app.translate_template(match.group(1), match.group(2), self._read_json()), {}
        match = re.fullmatch(r"/api/jobs/(job-[0-9a-f]{32})/undo/(tpl-[0-9a-f]{32})", path)
        if match and method == "POST":
            return 200, app.undo_template(match.group(1), match.group(2), self._read_json()), {}
        match = re.fullmatch(r"/api/jobs/(job-[0-9a-f]{32})/center/(tpl-[0-9a-f]{32})", path)
        if match and method == "POST":
            return 200, app.center_template(match.group(1), match.group(2), self._read_json()), {}
        match = re.fullmatch(r"/api/jobs/(job-[0-9a-f]{32})/rotate/(tpl-[0-9a-f]{32})", path)
        if match and method == "POST":
            return 200, app.rotate_template(match.group(1), match.group(2), self._read_json()), {}
        match = re.fullmatch(r"/api/jobs/(job-[0-9a-f]{32})/edit/(tpl-[0-9a-f]{32})", path)
        if match and method == "POST":
            return 200, app.edit_template(match.group(1), match.group(2), self._read_json()), {}
        match = re.fullmatch(r"/api/jobs/(job-[0-9a-f]{32})/manual-recovery/(tpl-[0-9a-f]{32})", path)
        if match and method == "POST":
            return 200, app.manual_recovery(match.group(1), match.group(2), self._read_json()), {}
        match = re.fullmatch(r"/api/jobs/(job-[0-9a-f]{32})/export", path)
        if match and method == "POST":
            return 200, app.export(match.group(1), self._read_json()), {}
        match = re.fullmatch(r"/api/jobs/(job-[0-9a-f]{32})/download/([^/]+)", path)
        if match and method == 'POST' and match.group(2)=='bundle':
            payload=self._read_json()
            if type(payload) is not dict or set(payload)-{'template_keys','naming_rule'}:
                raise ApiError(400,'invalid_request','ZIP请求只接受模板选择')
            raw, filename, content_type=app.download_artifact(match.group(1),'bundle',payload.get('template_keys'),payload.get('naming_rule'))
            return 200,raw,{'Content-Type':content_type,'Content-Disposition':'attachment; filename="{}"'.format(filename)}
        if match and method == "GET":
            raw, filename, content_type = app.download_artifact(match.group(1), match.group(2))
            return 200, raw, {"Content-Type": content_type,
                              "Content-Disposition": "attachment; filename=\"{}\"".format(filename)}
        # A traversal-looking suffix must stay inside the artifact capability
        # namespace and receive the same non-enumerating failure as any other
        # unknown artifact.  It must never fall through to static routing.
        if method == "GET" and re.match(r"/api/jobs/job-[0-9a-f]{32}/download(?:/|$)", path):
            raise ApiError(404, "artifact_not_found", "未找到可下载的已验证产物")
        if method not in ("GET", "POST", "PUT"):
            raise ApiError(405, "method_not_allowed", "该路径不支持此请求方法")
        raise ApiError(404, "not_found", "未找到该本地接口")

    def _content_length(self):
        ApiError, _ = _api_types()
        try:
            length = int(self.headers.get("Content-Length"))
        except (TypeError, ValueError):
            raise ApiError(411, "length_required", "请求需提供内容长度")
        if length < 0:
            raise ApiError(400, "invalid_request", "内容长度不合法")
        if length > self.application.limits.request_bytes:
            self.close_connection = True
            raise ApiError(413, "body_too_large", "请求总大小超过上限")
        return length

    def _read_exact(self, length):
        ApiError, _ = _api_types()
        raw = self.rfile.read(length)
        if len(raw) != length:
            self.close_connection = True
            raise ApiError(400, "truncated_body", "请求内容未完整传输")
        return raw

    def _read_json(self):
        ApiError, _ = _api_types()
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json" or self.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise ApiError(415, "unsupported_media_type", "该接口只接受 JSON")
        raw = self._read_exact(self._content_length())
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise ApiError(400, "invalid_json", "JSON 内容无法解析")
        if type(value) is not dict:
            raise ApiError(400, "invalid_json_root", "JSON 根节点必须是对象")
        return value

    def _read_multipart(self):
        ApiError, _ = _api_types()
        content_type = self.headers.get("Content-Type", "")
        if (not content_type.lower().startswith("multipart/form-data")
                or self.headers.get("Content-Encoding", "identity").lower() != "identity"):
            raise ApiError(415, "unsupported_media_type", "导入接口只接受 multipart/form-data")
        match = re.search(r"(?:^|;)\s*boundary=(?:\"([^\"]+)\"|([^;\s]+))", content_type,
                          flags=re.IGNORECASE)
        if not match:
            raise ApiError(400, "invalid_multipart", "multipart 缺少有效边界")
        boundary = match.group(1) or match.group(2)
        if not boundary or len(boundary) > 70 or any(ord(ch) < 33 or ord(ch) > 126 for ch in boundary):
            raise ApiError(400, "invalid_multipart", "multipart 边界不合法")
        length = self._content_length()
        app = self.application
        app.acquire_upload()
        try:
            raw = self._read_exact(length)
            envelope = ("MIME-Version: 1.0\r\nContent-Type: {}\r\n\r\n".format(content_type)).encode("ascii") + raw
            message = BytesParser(policy=EMAIL_POLICY).parsebytes(envelope)
            if not message.is_multipart() or message.defects:
                raise ApiError(400, "invalid_multipart", "multipart 内容无法解析")
            files = []
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                filename = part.get_filename()
                if name != "files" or filename is None or part.is_multipart():
                    raise ApiError(400, "invalid_request", "导入表单只允许 files 文件字段")
                payload = part.get_payload(decode=True)
                if payload is None:
                    raise ApiError(400, "invalid_multipart", "文件内容无法解析")
                files.append((filename, payload))
            if len(files) > app.limits.file_count:
                raise ApiError(413, "too_many_files", "内部上传批次过大，请刷新页面使用自动分批导入")
            return files
        finally:
            app.release_upload()

    def _serve_static(self, path):
        ApiError, _ = _api_types()
        root = self.application.frontend_root if self.application is not None else FRONTEND_ROOT.resolve()
        target = (root / ("index.html" if path == "/" else path.lstrip("/"))).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise ApiError(404, "not_found", "未找到该页面")
        if not target.is_file():
            raise ApiError(404, "not_found", "未找到该页面")
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix in (".html", ".css", ".js", ".svg"):
            content_type += "; charset=utf-8"
        self._respond(200, target.read_bytes(), content_type)

    def _security_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
                         "script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")

    def _respond(self, status, body, content_type, headers=None):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            for name, value in (headers or {}).items(): self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _json(self, status, value, headers=None):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
        self._respond(status, body, "application/json; charset=utf-8", headers)

    def log_message(self, format, *args):
        pass


def is_same_service(port: int) -> bool:
    connection = http.client.HTTPConnection(HOST, port, timeout=0.3)
    try:
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        if response.status != 200: return False
        payload = json.loads(response.read())
        return (
            payload.get("service_id") == SERVICE_ID
            and payload.get("version") == APP_VERSION
            and payload.get("protocol") == PROTOCOL_VERSION
            and payload.get("instance_id") == TOOL_INSTANCE_ID
            and payload.get("status") == "ready"
            and payload.get("local_only") is True
        )
    except (OSError, ValueError, json.JSONDecodeError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def select_server(start_port=DEFAULT_PORT, end_port=LAST_PORT) -> Tuple[int, Optional[LocalServer], bool]:
    for candidate in range(start_port, end_port + 1):
        if candidate > 0 and is_same_service(candidate): return candidate, None, True
    for candidate in range(start_port, end_port + 1):
        try:
            server = LocalServer((HOST, candidate), RequestHandler)
            return server.server_port, server, False
        except OSError as error:
            if error.errno != errno.EADDRINUSE: raise
            if is_same_service(candidate): return candidate, None, True
    raise RuntimeError("8766–8775 端口均不可用，未启动本地服务。")


def _default_application():
    _, Application = _api_types()
    try:
        from scripts.tower_reducer.storage import JobRepository
        from scripts.tower_reducer.migration import migrate_default_export_path
    except ImportError:
        from tower_reducer.storage import JobRepository
        from tower_reducer.migration import migrate_default_export_path
    runtime = TOOL_ROOT / "运行数据"
    migrate_default_export_path(TOOL_ROOT, runtime / 'settings.json')
    return Application(tool_root=TOOL_ROOT, repository=JobRepository(runtime / "任务"),
                       settings_path=runtime / "settings.json", discovery_root=Path.home())


def main():
    parser = argparse.ArgumentParser(description="塔占比修改及增加Btile工具本地服务")
    parser.add_argument("--open", action="store_true", help="在浏览器打开工具")
    args = parser.parse_args()
    port, server, reused = select_server()
    address = "http://{}:{}/".format(HOST, port)
    if reused:
        print("已复用本地服务：{}".format(address), flush=True)
        if args.open:
            webbrowser.open(address)
        return 0
    try:
        # Recovery and discovery finish before the page can observe this
        # process.  A failed initialization closes the reserved socket.
        server.application = _default_application()
    except Exception:
        server.server_close()
        raise
    print("本地服务已启动：{}".format(address), flush=True)
    if args.open:
        webbrowser.open(address)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
