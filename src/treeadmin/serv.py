import sys
import json
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from pathlib import Path
import subprocess
from datetime import datetime
import threading, uuid
from src.treeadmin.config import Config

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
    
    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _forward(self, host: str, port: int, method: str, path: str, body: bytes) -> None:
        # исправить после создания config структуры
        conf = Config()
        next_hop = conf.next_hop[0]
        host = next_hop["ip"]
        port = next_hop["port"]
        conn = http.client.HTTPConnection(host, port, timeout=15)
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

    def _read_until_marker(self, process, marker: str) -> str:
        lines = []
        while True:
            line = process.stdout.readline()
            if not line:
                break
            if marker in line:
                break
            lines.append(line)
        return "".join(lines)

    def _get_shell_cwd(self, process) -> str:
        marker = f"__CWD_END__{uuid.uuid4().hex}__"
        process.stdin.write("cd\n")
        process.stdin.write(f"echo {marker}\n")
        process.stdin.flush()

        raw = self._read_until_marker(process, marker)
        raw = raw.replace("__PROMPT__", "").strip()

        if ">" in raw:
            raw = raw.split(">")[-1].strip()

        return raw

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

        cur_paths = {"/send_config", "/send_command", "/open_shell", "/close_shell"}

        if parsed.path not in cur_paths:
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

            if parsed.path == "/open_shell":
                session_id = str(self.server.next_session_id)
                self.server.next_session_id += 1

                process = subprocess.Popen(
                    ["cmd.exe", "/Q", "/K"],
                    cwd="C:/Users/user/Desktop",
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="cp866",
                    bufsize=1
                )

                process.stdin.write("prompt __PROMPT__$\n")
                process.stdin.write("cd\n")
                process.stdin.write("echo __OPEN_END__\n")
                process.stdin.flush()

                lines = []
                while True:
                    line = process.stdout.readline()
                    if not line:
                        break
                    if "__OPEN_END__" in line:
                        break
                    lines.append(line)

                cwd_raw = "".join(lines).replace("__PROMPT__", "").strip()
                if ">" in cwd_raw:
                    cwd_raw = cwd_raw.split(">")[-1].strip()

                self.server.shell_sessions[session_id] = {
                    "process": process,
                    "lock": threading.Lock()
                }

                self._send_json(200, {
                    "session_id": session_id,
                    "cwd": cwd_raw
                })
                return

            ct = self.headers.get("Content-Type", "")
            if "application/json" not in ct:
                self._send_text(415, f"expected application/json : {ct}")
                print
                return

            try:
                file = json.loads(body.decode("utf-8"))
            except Exception as e:
                self._send_text(400, f"bad json: {e}")
                return

            if parsed.path == "/send_config":
                config_dir = Path("api")
                config_dir.mkdir(exist_ok=True)
                config_file = config_dir / "config.json"

                # сохраняю в папку в корне проекта 
                with open(config_file, "w", encoding="utf-8") as f:
                    json.dump(file, f, ensure_ascii=False, indent=2)

                print(f"SERVER({self_port}): config saved to {config_file.resolve()}")
                self._send_text(200, "config received")
                return

            if parsed.path == "/send_command":
                session_id = file.get("session_id")
                command = file.get("command", "").strip()

                if not session_id:
                    self._send_text(400, "missing 'session_id'")
                    return

                if not command:
                    self._send_text(400, "missing 'command'")
                    return

                session = self.server.shell_sessions.get(session_id)
                if not session:
                    self._send_text(404, "session not found")
                    return

                process = session["process"]
                lock = session["lock"]

                if process.poll() is not None:
                    self._send_text(500, "shell process already terminated")
                    return

                cmd_marker = f"__END__{uuid.uuid4().hex}__"
                cwd_marker = f"__CWD_END__{uuid.uuid4().hex}__"

                try:
                    with lock:
                        # выполнить команду
                        process.stdin.write(command + "\n")
                        process.stdin.write(f"echo {cmd_marker}\n")
                        process.stdin.flush()

                        output_lines = []
                        while True:
                            line = process.stdout.readline()
                            if not line:
                                break
                            if cmd_marker in line:
                                break
                            output_lines.append(line)

                        # сразу получить cwd в этой же сессии
                        process.stdin.write("cd\n")
                        process.stdin.write(f"echo {cwd_marker}\n")
                        process.stdin.flush()

                        cwd_lines = []
                        while True:
                            line = process.stdout.readline()
                            if not line:
                                break
                            if cwd_marker in line:
                                break
                            cwd_lines.append(line)

                    output = "".join(output_lines).replace("__PROMPT__", "").strip()
                    cwd_raw = "".join(cwd_lines).replace("__PROMPT__", "").strip()

                    if ">" in cwd_raw:
                        cwd_raw = cwd_raw.split(">")[-1].strip()

                    self._send_json(200, {
                        "output": output,
                        "cwd": cwd_raw
                    })
                    return

                except Exception as e:
                    self._send_text(500, f"command execution error: {e}")
                    return
                
            
            if parsed.path == "/close_shell":
                session_id = file.get("session_id")
                session = self.server.shell_sessions.pop(session_id, None)

                if not session:
                    self._send_text(404, "session not found")
                    return

                process = session["process"]

                try:
                    if process.poll() is None:
                        process.stdin.write("exit\n")
                        process.stdin.flush()
                        process.wait(timeout=3)
                except Exception:
                    process.kill()

                self._send_text(200, "shell closed")
                return

        print(f"path?query: {parsed.path}?{parsed.query}")
        print(f"PROXY({self_port}): forward config -> next")
        self._forward(self_host, int(self_port), "POST", self.path, body)

    def log_message(self, fmt, *args):
        sys.stdout.write(f"[{datetime.now().strftime("%d.%m.%Y %H:%M:%S")}] [NODE {self.client_address[0]}] {self.command} {self.path}\n")


def run_server(host="127.0.0.1", port=8000):
    httpd = ThreadingHTTPServer((host, port), ProxyHandler)
    print(f"Server started: http://{host}:{port}")
    
    httpd.shell_sessions = {}
    httpd.next_session_id = 1

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")