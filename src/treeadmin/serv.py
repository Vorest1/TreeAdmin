import sys
import json
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from pathlib import Path
import subprocess
from datetime import datetime
import threading, uuid
from src.treeadmin.config import ServerConfig
from src.treeadmin.routing import (
    parse_route_params,
    get_current_hop,
    is_final_hop,
    build_forward_request,
    format_route,
)

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
        # host = "127.0.0.1"
        # port = 8001
        conn = http.client.HTTPConnection(host, port, timeout=15)
        try:
            headers = {}
            content_type = self.headers.get("Content-Type")
            if content_type:
                headers["Content-Type"] = content_type
            
            if body:
                headers["Content-Length"] = str(len(body))
            else:
                headers["Content-Length"] = "0"

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
    
    def _extract_extra_query(self, qs: dict[str, list[str]]) -> dict[str, str]:
        extra: dict[str, str] = {}

        for key, values in qs.items():
            if key in {"route", "hop"}:
                continue

            if not values:
                extra[key] = ""
            else:
                extra[key] = values[0]

        return extra
    
    def _resolve_route(self, parsed):
        qs = parse_qs(parsed.query, keep_blank_values=True)

        hops, hop_index = parse_route_params(qs)
        current_hop = get_current_hop(hops, hop_index)

        current_node_id = self.server.config.node_id
        expected_node_id = current_hop["id"]

        if current_node_id != expected_node_id:
            raise ValueError(
                f"route mismatch: current node is '{current_node_id}', "
                f"but route expects '{expected_node_id}'"
            )

        return qs, hops, hop_index

    def _handle_open_shell(self):
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

    def _load_json_payload(self, body: bytes) -> dict:
        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            raise ValueError(f"expected application/json: {content_type}")

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception as e:
            raise ValueError(f"bad json: {e}") from e

        if not isinstance(payload, dict):
            raise ValueError("json body must be object")

        return payload

    def _handle_send_config(self, payload: dict) -> None:
        config_dir = Path("api")
        config_dir.mkdir(exist_ok=True)
        config_file = config_dir / "config.json"

        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        print(
            f"SERVER({self.server.config.node_id}): "
            f"config saved to {config_file.resolve()}"
        )
        self._send_text(200, "config received")

    def _handle_send_command(self, payload: dict) -> None:
        session_id = payload.get("session_id")
        command = payload.get("command", "").strip()

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

            self._send_json(
                200,
                {
                    "output": output,
                    "cwd": cwd_raw,
                },
            )

        except Exception as e:
            self._send_text(500, f"command execution error: {e}")

    def _handle_close_shell(self, payload: dict) -> None:
        session_id = payload.get("session_id")
        if not session_id:
            self._send_text(400, "missing 'session_id'")
            return

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

    def do_GET(self):
        #self_host, self_port = self.server.server_address
        parsed = urlparse(self.path)

        if parsed.path != "/hello":
            self._send_text(404, "not found")
            return

        try:
            qs, hops, hop_index = self._resolve_route(parsed)
        except ValueError as e:
            self._send_text(400, str(e))
            return

        print(
            f"ROUTE({self.server.config.node_id}): "
            f"{format_route(hops)} | hop={hop_index}"
        )

        if is_final_hop(hops, hop_index):
            msg = (qs.get("msg") or [""])[0]
            print(
                f"SERVER({self.server.config.node_id}): "
                f"receive ping: {msg}"
            )
            self._send_text(200, "Hello, Client")
            return

        try:
            next_host, next_port, next_path = build_forward_request(
                endpoint_path=parsed.path,
                hops=hops,
                current_hop_index=hop_index,
                extra_query=self._extract_extra_query(qs),
            )
        except ValueError as e:
            self._send_text(500, f"route forward error: {e}")
            return
        
        print(f"PROXY({self.server.config.node_id}): forward GET -> {next_host}:{next_port} {next_path}")
        self._forward(next_host, next_port, "GET", next_path, b"")

    def do_POST(self):
        #self_host, self_port = self.server.server_address
        parsed = urlparse(self.path)

        cur_paths = {"/send_config", "/send_command", "/open_shell", "/close_shell"}

        if parsed.path not in cur_paths:
            self._send_text(404, "not found")
            return

        try:
            qs, hops, hop_index = self._resolve_route(parsed)
        except ValueError as e:
            self._send_text(400, str(e))
            return
    
        body = self._read_body()

        print(
            f"ROUTE({self.server.config.node_id}): "
            f"{format_route(hops)} | hop={hop_index}"
        )

        if not is_final_hop(hops, hop_index):
            try:
                next_host, next_port, next_path = build_forward_request(
                    endpoint_path=parsed.path,
                    hops=hops,
                    current_hop_index=hop_index,
                    extra_query=self._extract_extra_query(qs),
                )
            except ValueError as e:
                self._send_text(500, f"route forward error: {e}")
                return

            print(
                f"PROXY({self.server.config.node_id}): "
                f"forward POST -> {next_host}:{next_port} {next_path}"
            )
            self._forward(next_host, next_port, "POST", next_path, body)
            return

        if parsed.path == "/open_shell":
            self._handle_open_shell()
            return

        try:
            payload = self._load_json_payload(body)
        except ValueError as e:
            self._send_text(415 if "application/json" in str(e) else 400, str(e))
            return

        if parsed.path == "/send_config":
            self._handle_send_config(payload)
            return

        if parsed.path == "/send_command":
            self._handle_send_command(payload)
            return
            
        if parsed.path == "/close_shell":
            self._handle_close_shell(payload)
            return
        
        self._send_text(404, "not found")

    def log_message(self, fmt, *args):
        sys.stdout.write(
            f"[{datetime.now().strftime("%d.%m.%Y %H:%M:%S")}] "
            f"[NODE {self.client_address[0]}]"
            f" {self.command} {urlparse(self.path).path}\n"
        )


def run_server(config: ServerConfig | None = None):
    if config is None:
        config = ServerConfig.load()

    httpd = ThreadingHTTPServer((config.listen_host, config.listen_port), ProxyHandler)
    print(f"Server started: http://{config.listen_host}:{config.listen_port}")
    
    httpd.config = config
    httpd.shell_sessions = {}
    httpd.next_session_id = 1

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")