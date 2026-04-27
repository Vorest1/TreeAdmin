from __future__ import annotations

import json
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

from src.treeadmin.routing import build_forward_request, format_route, is_final_hop


SILENT_LOG_PATHS = {
    "/pull_pending_results",
    "/ack_response",
}


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle_one_request(self):
        self._request_path = ""
        self._suppress_access_log = False

        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            self.close_connection = True

    @staticmethod
    def _is_silent_path(path: str) -> bool:
        return path in SILENT_LOG_PATHS

    def _prepare_request_logging_state(self, path: str) -> bool:
        self._request_path = path
        self._suppress_access_log = self._is_silent_path(path)
        return self._suppress_access_log

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

    def _load_json_payload(self, body: bytes) -> dict:
        if not body:
            return {}

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

    def do_GET(self):
        parsed = urlparse(self.path)
        silent = self._prepare_request_logging_state(parsed.path)

        if parsed.path not in {
            "/hello",
            "/list_sessions",
            "/session_jobs",
            "/pull_pending_results",
        }:
            self._send_text(404, "not found")
            return

        try:
            qs, hops, hop_index = self.server.proxy.resolve_route(parsed)
        except ValueError as e:
            self._send_text(400, str(e))
            return

        if not silent:
            print(f"ROUTE: {format_route(hops)} | hop={hop_index}")

        if not is_final_hop(hops, hop_index):
            try:
                next_host, next_port, next_path = build_forward_request(
                    endpoint_path=parsed.path,
                    hops=hops,
                    current_hop_index=hop_index,
                    extra_query=self.server.proxy.extract_extra_query(qs),
                )
            except ValueError as e:
                self._send_text(500, f"route forward error: {e}")
                return

            if not silent:
                print(f"PROXY: forward GET -> {next_host}:{next_port} {next_path}")

            self.server.proxy.forward(self, next_host, next_port, "GET", next_path, b"")
            return

        if parsed.path == "/hello":
            msg = (qs.get("msg") or [""])[0]
            print(f"SERVER: receive ping: {msg}")
            self._send_text(200, "Hello, Client")
            return

        if parsed.path == "/list_sessions":
            self._send_json(200, {"sessions": self.server.sessions.list_sessions()})
            return

        if parsed.path == "/session_jobs":
            session_id = (qs.get("session_id") or [""])[0].strip()
            if not session_id:
                self._send_text(400, "missing query parameter 'session_id'")
                return

            self._send_json(200, self.server.jobs.get_session_jobs(session_id))
            return

        if parsed.path == "/pull_pending_results":
            session_id = (qs.get("session_id") or [""])[0].strip() or None

            if session_id and hasattr(self.server.sessions, "touch_session"):
                self.server.sessions.touch_session(session_id)

            items = self.server.jobs.list_pending_responses(session_id=session_id)
            self._send_json(200, {"responses": items})
            return

        self._send_text(404, "not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        silent = self._prepare_request_logging_state(parsed.path)

        valid_paths = {
            "/open_shell",
            "/send_command",
            "/close_shell",
            "/ack_response",
        }

        if parsed.path not in valid_paths:
            self._send_text(404, "not found")
            return

        try:
            qs, hops, hop_index = self.server.proxy.resolve_route(parsed)
        except ValueError as e:
            self._send_text(400, str(e))
            return

        body = self._read_body()

        if not silent:
            print(f"ROUTE: {format_route(hops)} | hop={hop_index}")

        if not is_final_hop(hops, hop_index):
            try:
                next_host, next_port, next_path = build_forward_request(
                    endpoint_path=parsed.path,
                    hops=hops,
                    current_hop_index=hop_index,
                    extra_query=self.server.proxy.extract_extra_query(qs),
                )
            except ValueError as e:
                self._send_text(500, f"route forward error: {e}")
                return

            if not silent:
                print(f"PROXY: forward POST -> {next_host}:{next_port} {next_path}")

            self.server.proxy.forward(self, next_host, next_port, "POST", next_path, body)
            return

        try:
            payload = self._load_json_payload(body)
        except ValueError as e:
            self._send_text(415 if "expected application/json" in str(e) else 400, str(e))
            return

        if parsed.path == "/open_shell":
            try:
                session_info = self.server.sessions.open_session()
                self.server.workers.ensure_worker(str(session_info["session_id"]))
                self._send_json(200, session_info)
            except Exception as e:
                self._send_text(500, f"open shell error: {e}")
            return

        if parsed.path == "/send_command":
            session_id = str(payload.get("session_id", "")).strip()
            command = str(payload.get("command", "")).strip()

            if not session_id:
                self._send_text(400, "missing 'session_id'")
                return

            if not command:
                self._send_text(400, "missing 'command'")
                return

            session = self.server.sessions.get_session(session_id)
            if not session:
                self._send_text(404, "session not found")
                return

            result = self.server.jobs.enqueue_command(
                session_id=session_id,
                command=command,
                cwd=str(session.get("cwd", "")),
            )
            self._send_json(202, result)
            return

        if parsed.path == "/close_shell":
            session_id = str(payload.get("session_id", "")).strip()
            if not session_id:
                self._send_text(400, "missing 'session_id'")
                return

            session = self.server.sessions.close_session(session_id)
            if session is None:
                self._send_text(404, "session not found")
                return

            self.server.jobs.cancel_queued_for_session(session_id, "session closed")
            self._send_text(200, "shell closed")
            return

        if parsed.path == "/ack_response":
            response_ids = payload.get("response_ids")

            if isinstance(response_ids, list):
                ids = [str(item) for item in response_ids]
            else:
                single = str(payload.get("response_id", "")).strip()
                ids = [single] if single else []

            removed = self.server.jobs.ack_responses(ids)
            self._send_json(200, {"acknowledged": removed})
            return

        self._send_text(404, "not found")

    def log_request(self, code="-", size="-"):
        if getattr(self, "_suppress_access_log", False):
            return

        super().log_request(code, size)

    def log_message(self, fmt, *args):
        path = getattr(self, "_request_path", "") or urlparse(self.path).path

        if self._is_silent_path(path):
            return

        sys.stdout.write(
            f'[{datetime.now().strftime("%d.%m.%Y %H:%M:%S")}] '
            f'[NODE {self.client_address[0]}] '
            f'{self.command} {path}\n'
        )