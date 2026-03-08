import sys
import json
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from pathlib import Path
import subprocess


class ProxyHandler(BaseHTTPRequestHandler):

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text: str) -> None:
        self._send_bytes(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length > 0 else b""

    def _forward(self, host: str, port: int, method: str, path: str, body: bytes) -> None:
        # исправить после создания Agent структуры
        host = "10.88.180.24"
        port = 8080
        conn = http.client.HTTPConnection(host, port, timeout=5)
        try:
            headers = {}
            content_type = self.headers.get("Content-Type")
            if content_type:
                headers["Content-Type"] = content_type

            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            resp_body = resp.read()

            self.send_response(resp.status)
            self.send_header("Content-Type", resp.getheader("Content-Type", "text/plain; charset=utf-8"))
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
        except Exception as e:
            self._send_text(502, f"proxy error: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass


    def do_GET(self):
        self_host, self_port = self.server.server_address
        parsed = urlparse(self.path)

        if parsed.path != "/hello":
            self._send_text(404, "not found")
            return

        qs = parse_qs(parsed.query)
        if "to" not in qs:
            self._send_text(400, "missing 'to'")
            return

        try:
            to_port = int(qs["to"][0])
        except ValueError:
            self._send_text(400, "invalid 'to' port")
            return

        if to_port == int(self_port):
            msg = (qs.get("msg") or [""])[0]
            print(f"SERVER({self_port}): receive ping: {msg}")
            self._send_text(200, "Hello, Client")

            return

        print(f"PROXY({self_port}): forward ping -> next")
        self._forward(self_host, int(self_port), "GET", self.path, b"")

    def do_POST(self):
        self_host, self_port = self.server.server_address
        parsed = urlparse(self.path)

        if parsed.path != "/send_config" or "/send_command":
            self._send_text(404, "not found")
            return

        qs = parse_qs(parsed.query)
        if "to" not in qs:
            self._send_text(400, "missing 'to'")
            return

        try:
            to_port = int(qs["to"][0])
        except ValueError:
            self._send_text(400, "invalid 'to' port")
            return
    
        body = self._read_body()

        if to_port == int(self_port):
            ct = self.headers.get("Content-Type", "")
            if "application/json" not in ct:
                self._send_text(415, "expected application/json")
                return

            try:
                file = json.loads(body.decode("utf-8"))
            except Exception as e:
                self._send_text(400, f"bad json: {e}")
                return
            
            if self.path == "/send_config":
                config_dir = Path("api")
                config_dir.mkdir(exist_ok=True)
                config_file = config_dir / "config.json"

                # сохраняю в папку в корне проекта 
                with open(config_file, "w", encoding="utf-8") as f:
                    json.dump(file, f, ensure_ascii=False, indent=2)

                print(f"SERVER({self_port}): config saved to {config_file.resolve()}")
                self._send_text(200, "config received")
                return
            
            if self.path == "/send_command":
                command = file["command"]
                try:
                    output = subprocess.check_output(command, shell=True)
                    print(f"SERVER({self_port}):  execute command: {command}")
                except subprocess.CalledProcessError as e:
                    print(f"ERROR COMMAND: {e.returncode}")
                self._send_text(200, output)
                return

        print(f"PROXY({self_port}): forward config -> next")
        self._forward(self_host, int(self_port), "POST", self.path, body)

    def log_message(self, fmt, *args):
        sys.stdout.write(f"[NODE {self.client_address[0]}] {self.command} {self.path}\n")


def run_server(host="127.0.0.1", port=8000):
    httpd = ThreadingHTTPServer((host, port), ProxyHandler)
    print(f"Server started: http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")