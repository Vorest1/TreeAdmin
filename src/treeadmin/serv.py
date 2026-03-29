from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import http.client

from src.treeadmin.routing import (
    build_forward_request,
    format_route,
    get_current_hop,
    is_final_hop,
    parse_route_params,
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

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length > 0 else b""

    def _forward(self, host: str, port: int, method: str, path: str, body: bytes) -> None:
        conn = http.client.HTTPConnection(host, port, timeout=15)
        try:
            headers = {}
            content_type = self.headers.get("Content-Type")
            if content_type:
                headers["Content-Type"] = content_type

            headers["Content-Length"] = str(len(body)) if body else "0"

            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            resp_body = resp.read()

            self.send_response(resp.status)
            self.send_header(
                "Content-Type",
                resp.getheader("Content-Type", "text/plain; charset=utf-8"),
            )
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

    def _read_until_marker(self, process: subprocess.Popen, marker: str) -> str:
        lines: list[str] = []
        while True:
            line = process.stdout.readline()
            if not line:
                break
            if marker in line:
                break
            lines.append(line)
        return "".join(lines)

    def _extract_extra_query(self, qs: dict[str, list[str]]) -> dict[str, str]:
        extra: dict[str, str] = {}
        for key, values in qs.items():
            if key in {"route", "hop"}:
                continue
            extra[key] = values[0] if values else ""
        return extra

    def _resolve_route(self, parsed):
        qs = parse_qs(parsed.query, keep_blank_values=True)
        hops, hop_index = parse_route_params(qs)

        if not hops:
            raise ValueError("empty route")

        get_current_hop(hops, hop_index)
        return qs, hops, hop_index

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

    def _get_platform_name(self) -> str:
        system_name = platform.system().lower()

        if system_name.startswith("win"):
            return "windows"

        if system_name in {"linux", "darwin"}:
            return "posix"

        return "posix"

    def _handle_open_shell(self) -> None:
        session_id = str(self.server.next_session_id)
        self.server.next_session_id += 1

        platform_name = self._get_platform_name()

        if platform_name == "windows":
            start_cwd = "C:/Users/user/Desktop"

            process = subprocess.Popen(
                ["cmd.exe", "/Q", "/K"],
                cwd=start_cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="cp866",
                bufsize=1,
            )

            process.stdin.write("prompt __PROMPT__$\n")
            process.stdin.write("cd\n")
            process.stdin.write("echo __OPEN_END__\n")
            process.stdin.flush()

            lines: list[str] = []
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
                "platform": "windows",
                "process": process,
                "lock": threading.Lock(),
                "cwd": cwd_raw or start_cwd,
            }

            self._send_json(
                200,
                {
                    "session_id": session_id,
                    "cwd": cwd_raw or start_cwd,
                },
            )
            return

        start_cwd = str(Path.home())

        self.server.shell_sessions[session_id] = {
            "platform": "posix",
            "process": None,
            "lock": threading.Lock(),
            "cwd": start_cwd,
        }

        self._send_json(
            200,
            {
                "session_id": session_id,
                "cwd": start_cwd,
            },
        )

    def _run_posix_command(self, command: str, cwd: str) -> tuple[str, str, int]:
        if not isinstance(cwd, str) or not cwd.strip():
            cwd = str(Path.home())

        cmd = command.strip()

        if not cmd:
            return "", cwd, 0

        if cmd == "cd":
            new_cwd = str(Path.home())
            return "", new_cwd, 0

        if cmd.startswith("cd "):
            target = cmd[3:].strip()

            if (target.startswith('"') and target.endswith('"')) or (
                target.startswith("'") and target.endswith("'")
            ):
                target = target[1:-1]

            if not target:
                new_cwd = str(Path.home())
                return "", new_cwd, 0

            if os.path.isabs(target):
                candidate = os.path.abspath(target)
            else:
                candidate = os.path.abspath(os.path.join(cwd, target))

            if not os.path.isdir(candidate):
                return f"cd: no such directory: {target}", cwd, 1

            return "", candidate, 0

        completed = subprocess.run(
            ["/bin/bash", "-lc", command],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )

        output = completed.stdout or ""
        return output, cwd, completed.returncode

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

        platform_name = session.get("platform")
        lock = session["lock"]

        try:
            with lock:
                if platform_name == "windows":
                    process = session["process"]

                    if process is None:
                        self._send_text(500, "windows shell process is missing")
                        return

                    if process.poll() is not None:
                        self._send_text(500, "shell process already terminated")
                        return

                    cmd_marker = f"__END__{uuid.uuid4().hex}__"
                    cwd_marker = f"__CWD_END__{uuid.uuid4().hex}__"

                    process.stdin.write(command + "\n")
                    process.stdin.write(f"echo {cmd_marker}\n")
                    process.stdin.flush()

                    output_lines: list[str] = []
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

                    cwd_lines: list[str] = []
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

                    session["cwd"] = cwd_raw or session.get("cwd", "")

                    self._send_json(
                        200,
                        {
                            "output": output,
                            "cwd": session["cwd"],
                        },
                    )
                    return

                cwd = session.get("cwd", str(Path.home()))
                output, new_cwd, returncode = self._run_posix_command(command, cwd)
                session["cwd"] = new_cwd

                self._send_json(
                    200,
                    {
                        "output": output.strip(),
                        "cwd": new_cwd,
                        "returncode": returncode,
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

        platform_name = session.get("platform")
        process = session.get("process")

        if platform_name == "windows" and process is not None:
            try:
                if process.poll() is None:
                    process.stdin.write("exit\n")
                    process.stdin.flush()
                    process.wait(timeout=3)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

        self._send_text(200, "shell closed")

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path != "/hello":
            self._send_text(404, "not found")
            return

        try:
            qs, hops, hop_index = self._resolve_route(parsed)
        except ValueError as e:
            self._send_text(400, str(e))
            return

        print(f"ROUTE: {format_route(hops)} | hop={hop_index}")

        if is_final_hop(hops, hop_index):
            msg = (qs.get("msg") or [""])[0]
            print(f"SERVER: receive ping: {msg}")
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

        print(f"PROXY: forward GET -> {next_host}:{next_port} {next_path}")
        self._forward(next_host, next_port, "GET", next_path, b"")

    def do_POST(self):
        parsed = urlparse(self.path)

        cur_paths = {"/send_command", "/open_shell", "/close_shell"}
        if parsed.path not in cur_paths:
            self._send_text(404, "not found")
            return

        try:
            qs, hops, hop_index = self._resolve_route(parsed)
        except ValueError as e:
            self._send_text(400, str(e))
            return

        body = self._read_body()

        print(f"ROUTE: {format_route(hops)} | hop={hop_index}")

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

            print(f"PROXY: forward POST -> {next_host}:{next_port} {next_path}")
            self._forward(next_host, next_port, "POST", next_path, body)
            return

        if parsed.path == "/open_shell":
            try:
                self._handle_open_shell()
            except Exception as e:
                self._send_text(500, f"open shell error: {e}")
            return

        try:
            payload = self._load_json_payload(body)
        except ValueError as e:
            self._send_text(415 if "expected application/json" in str(e) else 400, str(e))
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
            f'[{datetime.now().strftime("%d.%m.%Y %H:%M:%S")}] '
            f'[NODE {self.client_address[0]}] '
            f'{self.command} {urlparse(self.path).path}\n'
        )


def run_server(host: str = "0.0.0.0", port: int = 8000):
    httpd = ThreadingHTTPServer((host, port), ProxyHandler)
    print(f"Server started: http://{host}:{port}")

    httpd.shell_sessions = {}
    httpd.next_session_id = 1

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")