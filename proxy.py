"""
Локальный CORS-прокси для Anthropic-совместимых API (Zenoid и т.п.).

Запуск:
    py proxy.py                # слушает http://localhost:8787
    py proxy.py 9000           # слушает http://localhost:9000
    py proxy.py 8787 https://api.zenoid.space   # сменить upstream

Использование в браузере (test-api.html и index.html):
    Endpoint: http://localhost:8787
    (приложение само допишет /v1/messages)

Прокси прозрачно прокидывает методы/пути/тело/заголовки на upstream
и добавляет на ответ заголовки Access-Control-Allow-* — браузер пускает.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import http.client
import sys
import ssl

UPSTREAM = "https://api.zenoid.space"
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length", "origin", "referer",
}

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS, PATCH",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Expose-Headers": "*",
    "Access-Control-Max-Age": "86400",
}


def make_handler(upstream_url):
    parts = urlsplit(upstream_url)
    upstream_host = parts.hostname
    upstream_port = parts.port or (443 if parts.scheme == "https" else 80)
    upstream_scheme = parts.scheme

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            sys.stderr.write("[proxy] " + (fmt % args) + "\n")

        def _send_cors(self):
            for k, v in CORS_HEADERS.items():
                self.send_header(k, v)

        def do_OPTIONS(self):
            self.send_response(204)
            self._send_cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _forward(self):
            length = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(length) if length else None

            fwd_headers = {}
            for k, v in self.headers.items():
                if k.lower() in HOP_BY_HOP:
                    continue
                fwd_headers[k] = v
            fwd_headers["Host"] = upstream_host

            if upstream_scheme == "https":
                ctx = ssl.create_default_context()
                conn = http.client.HTTPSConnection(upstream_host, upstream_port, context=ctx, timeout=120)
            else:
                conn = http.client.HTTPConnection(upstream_host, upstream_port, timeout=120)

            try:
                conn.request(self.command, self.path, body=body, headers=fwd_headers)
                resp = conn.getresponse()
                data = resp.read()
            except Exception as e:
                msg = f'{{"error":"proxy upstream failed: {e}"}}'.encode("utf-8")
                self.send_response(502)
                self._send_cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return
            finally:
                conn.close()

            self.send_response(resp.status, resp.reason)
            self._send_cors()
            for k, v in resp.getheaders():
                if k.lower() in HOP_BY_HOP:
                    continue
                # CORS-заголовки уже выставили — не дублируем
                if k.lower().startswith("access-control-"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _forward

    return Handler


def main():
    port = 8787
    upstream = UPSTREAM
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    if len(sys.argv) > 2:
        upstream = sys.argv[2].rstrip("/")
    handler = make_handler(upstream)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"CORS proxy -> {upstream}")
    print(f"Listening on http://localhost:{port}")
    print(f"In browser app set Endpoint = http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
