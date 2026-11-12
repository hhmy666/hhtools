#!/usr/bin/env python3
"""Launch the standalone checker without replacing another local process."""

import argparse
import errno
import http.client
import json
import sys
import webbrowser

from covercheck.server import (
    DEFAULT_PORT, DEFAULT_TEMPLATE_ROOT, HOST, INSTANCE_ID, SERVICE_ID, VERSION, create_server,
)


def is_our_server(port):
    connection = http.client.HTTPConnection(HOST, port, timeout=1)
    try:
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        payload = json.loads(response.read(4096))
        return (response.status == 200 and isinstance(payload, dict)
                and payload.get("service_id") == SERVICE_ID
                and payload.get("instance_id") == INSTANCE_ID
                and payload.get("version") == VERSION)
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="全遮盖识别工具（本地只读服务）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--template-root", default=DEFAULT_TEMPLATE_ROOT)
    parser.add_argument("--open", action="store_true", help="启动后打开默认浏览器")
    options = parser.parse_args(argv)
    if not 0 <= options.port <= 65535:
        parser.error("端口需要在0至65535之间")
    try:
        server = create_server((HOST, options.port), template_root=options.template_root)
    except OSError as error:
        if error.errno != errno.EADDRINUSE:
            raise
        if is_our_server(options.port):
            url = "http://{}:{}/".format(HOST, options.port)
            print("全遮盖识别工具已在运行：" + url, flush=True)
            if options.open:
                webbrowser.open(url)
            return 0
        server = create_server((HOST, 0), template_root=options.template_root)
        print("默认端口已被其他程序或旧版本占用，已选用空闲端口。", flush=True)
    url = "http://{}:{}/".format(HOST, server.server_port)
    print("全遮盖识别工具：" + url, flush=True)
    print("模板只读识别；关闭此窗口或按Control-C可停止服务。", flush=True)
    if options.open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\n服务已停止。", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
