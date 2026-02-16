import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote


class HelloHandler(BaseHTTPRequestHandler):

    def _send_text(self, status: int, text: str) -> None:
        body = (text + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path != "/hello":
            self._send_text(404, "not found")
            return

        qs = parse_qs(parsed.query)
        msg = (qs.get("msg") or [""])[0]

        print(f"SERVER: receive get message: {msg}")

        self._send_text(200, "Hello, Client")

    def log_message(self, fmt, *args):
        # убираем стандартные логи BaseHTTPRequestHandler
        sys.stdout.write(f"[{self.client_address[0]}] {self.command} {self.path}\n")


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    httpd = ThreadingHTTPServer((host, port), HelloHandler)
    print(f"Server started: http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")