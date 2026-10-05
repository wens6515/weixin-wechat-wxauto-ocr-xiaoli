# -*- coding: utf-8 -*-
"""webui 开发预览服务器：静态服务 + no-store 响应头（改完刷新即生效）。

仅用于浏览器直开调试（mock 模式）；pywebview 壳用自带内置服务器，不经过
本文件。用法：python tools/webui_dev_server.py [端口] [目录]
"""
import http.server
import os
import sys


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8731
    root = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webui")
    os.chdir(root)
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), NoCacheHandler) as httpd:
        print(f"webui dev server: http://127.0.0.1:{port}/  (root={root})")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
