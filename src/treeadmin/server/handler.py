import json
import logging
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict
from urllib.parse import urlparse

from src.treeadmin.routing import build_forward_request, format_route, is_final_hop

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("treeadmin.audit")

SILENT_LOG_PATHS = {
    "/pull_pending_results",
    "/ack_response",
    "/results_summary",
}


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle_one_request(self):
        self._request_path = ""
        self._suppress_access_log = False

        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            # log
            logger.debug(
                "http connection closed : client_ip=%s",
                self.client_address[0] if self.client_address else "",
                exc_info=True
            )
            #
            self.close_connection = True

    @staticmethod
    def _is_silent_path(path):
        # type: (str) -> bool
        return path in SILENT_LOG_PATHS

    def _prepare_request_logging_state(self, path):
        # type: (str) -> bool
        self._request_path = path
        self._suppress_access_log = self._is_silent_path(path)
        return self._suppress_access_log

    def _send_bytes(self, status, body, content_type):
        # type: (int, bytes, str) -> None
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status, text):
        # type: (int, str) -> None
        self._send_bytes(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _send_json(self, status, data):
        # type: (int, Dict[str, Any]) -> None
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send_bytes(body=body, status=status, content_type="application/json; charset=utf-8")

    def _read_body(self):
        # type: () -> bytes
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length > 0 else b""

    def _load_json_payload(self, body):
        # type: (bytes) -> Dict[str, Any]
        if not body:
            return {}

        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            raise ValueError("expected application/json: {}".format(content_type))

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception as e:
            raise ValueError("bad json: {}".format(e))

        if not isinstance(payload, dict):
            raise ValueError("json body must be object")

        return payload

    def do_GET(self):
        parsed = urlparse(self.path)
        silent = self._prepare_request_logging_state(parsed.path)
        client_ip = self.client_address[0] if self.client_address else ""

        if parsed.path not in {
            "/hello",
            "/list_sessions",
            "/session_jobs",
            "/pull_pending_results",
            "/results_summary",
        }:
            # log
            logger.warning(
                "http get not found : path=%s client_ip=%s",
                parsed.path,
                client_ip
            )
            #
            self._send_text(404, "not found")
            return

        try:
            qs, hops, hop_index = self.server.proxy.resolve_route(parsed)
        except ValueError as e:
            # log
            logger.warning(
                "http get route resolve failed : path=%s client_ip=%s error=%s",
                parsed.path,
                client_ip,
                e
            )
            #
            self._send_text(400, str(e))
            return

        if not silent:
            # log
            logger.debug(
                "http get received : path=%s client_ip=%s",
                parsed.path,
                client_ip,
            )
            #
            print("ROUTE: {} | hop={}".format(format_route(hops), hop_index))

        if not is_final_hop(hops, hop_index):
            try:
                next_host, next_port, next_path = build_forward_request(
                    endpoint_path=parsed.path,
                    hops=hops,
                    current_hop_index=hop_index,
                    extra_query=self.server.proxy.extract_extra_query(qs),
                )
            except ValueError as e:
                # log
                logger.exception(
                    "http get build forward request failed : path=%s client_ip=%s hop_index=%s",
                    parsed.path,
                    client_ip,
                    hop_index
                )
                #
                self._send_text(500, "route forward error: {}".format(e))
                return
            # log
            logger.debug(
                "http get forwarding : path=%s client_ip=%s next_host=%s:%s hop_index=%s",
                parsed.path,
                client_ip,
                next_host,
                next_port,
                hop_index
            )
            #
            if not silent:
                print(
                    "PROXY: forward GET -> {}:{} {}".format(
                        next_host,
                        next_port,
                        next_path
                    )
                )

            self.server.proxy.forward(self, next_host, next_port, "GET", next_path, b"")
            return

        if parsed.path == "/hello":
            msg = (qs.get("msg") or [""])[0]
            # log
            logger.info(
                "ping received : client_ip=%s msg_len=%s",
                client_ip,
                len(msg),
            )
            #
            print("SERVER: receive ping: {}".format(msg))
            self._send_text(200, "Hello, Client")
            return

        if parsed.path == "/list_sessions":
            # log
            sessions = self.server.sessions.list_sessions()

            logger.debug(
                "sessions list requested : client_ip=%s count=%s",
                client_ip,
                len(sessions)
            )
            #
            self._send_json(200, {"sessions": self.server.sessions.list_sessions()})
            return

        if parsed.path == "/session_jobs":
            session_id = (qs.get("session_id") or [""])[0].strip()
            if not session_id:
                # log
                logger.warning(
                    "session jobs bad request missing session_id : client_ip=%s",
                    client_ip
                )
                #
                self._send_text(400, "missing query parameter 'session_id'")
                return
            # log
            logger.debug(
                "session jobs requested : client_ip=%s session_id=%s",
                client_ip,
                session_id
            )
            #
            self._send_json(200, self.server.jobs.get_session_jobs(session_id))
            return

        if parsed.path == "/results_summary":
            session_id = (qs.get("session_id") or [""])[0].strip() or None
            # log
            logger.debug(
                "results summary requested : client_ip=%s session_id=%s",
                client_ip,
                session_id,
            )
            #
            self._send_json(200, self.server.jobs.get_results_summary(session_id=session_id))
            return

        if parsed.path == "/pull_pending_results":
            session_id = (qs.get("session_id") or [""])[0].strip() or None

            if session_id and hasattr(self.server.sessions, "touch_session"):
                self.server.sessions.touch_session(session_id)

            items = self.server.jobs.list_pending_responses(session_id=session_id)
            # log
            logger.debug(
                "pending results pulled : client_ip=%s session_id=%s count=%s",
                client_ip,
                session_id,
                len(items)
            )
            #
            self._send_json(200, {"responses": items})
            return

        self._send_text(404, "not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        silent = self._prepare_request_logging_state(parsed.path)
        client_ip = self.client_address[0] if self.client_address else ""

        valid_paths = {
            "/open_shell",
            "/send_command",
            "/close_shell",
            "/ack_response",
        }

        if parsed.path not in valid_paths:
            # log
            logger.warning(
                "HTTP POST not found : path=%s client_ip=%s",
                parsed.path,
                client_ip
            )
            #
            self._send_text(404, "not found")
            return

        try:
            qs, hops, hop_index = self.server.proxy.resolve_route(parsed)
        except ValueError as e:
            # log
            logger.warning(
                "HTTP POST route resolve failed : path=%s client_ip=%s error=%s",
                parsed.path,
                client_ip,
                e
            )
            #
            self._send_text(400, str(e))
            return

        try:
            body = self._read_body()
        except Exception:
            # log
            logger.exception(
                "HTTP POST read body failed : path=%s client_ip=%s",
                parsed.path,
                client_ip
            )
            #
            self._send_text(400, "failed to read request body")
            return

        if not silent:
            # log
            logger.debug(
                "HTTP POST received : path=%s client_ip=%s",
                parsed.path,
                client_ip,
            )
            #
            print("ROUTE: {} | hop={}".format(format_route(hops), hop_index))

        if not is_final_hop(hops, hop_index):
            try:
                next_host, next_port, next_path = build_forward_request(
                    endpoint_path=parsed.path,
                    hops=hops,
                    current_hop_index=hop_index,
                    extra_query=self.server.proxy.extract_extra_query(qs),
                )
            except ValueError as e:
                # log
                logger.exception(
                    "HTTP POST build forward request failed : path=%s client_ip=%s hop_index=%s",
                    parsed.path,
                    client_ip,
                    hop_index
                )
                #
                self._send_text(500, "route forward error: {}".format(e))
                return
            
            logger.debug(
                "HTTP POST forwarding : path=%s client_ip=%s next_host=%s:%s "
                "hop_index=%s body_size=%s",
                parsed.path,
                client_ip,
                next_host,
                next_port,
                hop_index,
                len(body)
            )

            if not silent:
                print(
                    "PROXY: forward POST -> {}:{} {}".format(
                        next_host,
                        next_port,
                        next_path
                    )
                )

            self.server.proxy.forward(self, next_host, next_port, "POST", next_path, body)
            return

        try:
            payload = self._load_json_payload(body)
        except ValueError as e:
            # log
            logger.warning(
                "HTTP POST invalid json : path=%s client_ip=%s body_size=%s error=%s",
                parsed.path,
                client_ip,
                len(body),
                e,
            )
            #
            self._send_text(415 if "expected application/json" in str(e) else 400, str(e))
            return

        if parsed.path == "/open_shell":
            try:
                # log
                logger.info("open_shell requested : client_ip=%s", client_ip)
                #

                session_info = self.server.sessions.open_session()
                self.server.workers.ensure_worker(str(session_info["session_id"]))

                # log
                logger.info(
                    "open_shell done : client_ip=%s session_id=%s cwd=%s node_id=%s",
                    client_ip,
                    session_info.get("session_id"),
                    session_info.get("cwd"),
                    session_info.get("node_id")
                )

                audit_logger.info(
                    "session opened : client_ip=%s session_id=%s node_id=%s",
                    client_ip,
                    session_info.get("session_id"),
                    session_info.get("node_id")
                )
                #

                self._send_json(200, session_info)
            except Exception as e:
                # log
                logger.exception("open_shell_failed client_ip=%s", client_ip)
                #
                self._send_text(500, "open shell error: {}".format(e))
            return

        if parsed.path == "/send_command":
            session_id = str(payload.get("session_id", "")).strip()
            command = str(payload.get("command", "")).strip()

            output_storage_format = str(
                payload.get("output_storage_format", "base64")
            ).strip().lower()

            if output_storage_format not in {"text", "base64"}:
                output_storage_format = "base64"

            if not session_id:
                # log
                logger.warning(
                    "send_command bad request[missing session_id] : client_ip=%s",
                    client_ip
                )
                #
                self._send_text(400, "missing 'session_id'")
                return

            if not command:
                # log
                logger.warning(
                    "send_command bad request [missing command] : client_ip=%s session_id=%s",
                    client_ip,
                    session_id
                )
                #
                self._send_text(400, "missing 'command'")
                return

            session = self.server.sessions.get_session(session_id)
            if not session:
                # log
                logger.warning(
                    "send_command session not found : client_ip=%s session_id=%s command_len=%s",
                    client_ip,
                    session_id,
                    len(command)
                )
                #
                self._send_text(404, "session not found")
                return

            try:
                result = self.server.jobs.enqueue_command(
                    session_id=session_id,
                    command=command,
                    cwd=str(session.get("cwd", "")),
                    output_storage_format=output_storage_format,
                )
            except Exception:
                # log
                logger.exception(
                    "send_command enqueue failed : client_ip=%s session_id=%s command_len=%s",
                    client_ip,
                    session_id,
                    len(command)
                )
                #
                self._send_text(500, "failed to enqueue command")
                return
            
            # log
            logger.info(
                "send_command queued : client_ip=%s session_id=%s job_id=%s command_len=%s cwd=%s",
                client_ip,
                session_id,
                result.get("job_id"),
                len(command),
                result.get("cwd")
            )

            audit_logger.info(
                "command queued : client_ip=%s session_id=%s job_id=%s command_len=%s",
                client_ip,
                session_id,
                result.get("job_id"),
                len(command)
            )
            #

            self._send_json(202, result)
            return

        if parsed.path == "/close_shell":
            session_id = str(payload.get("session_id", "")).strip()
            if not session_id:
                # log
                logger.warning(
                    "close_shell bad request [missing session_id] : client_ip=%s",
                    client_ip
                )
                #
                self._send_text(400, "missing 'session_id'")
                return

            session = self.server.sessions.close_session(session_id)
            if session is None:
                logger.warning(
                    "close_shell session not found : client_ip=%s session_id=%s",
                    client_ip,
                    session_id
                )
                self._send_text(404, "session not found")
                return

            try:
                self.server.jobs.cancel_queued_for_session(session_id, "session closed")
            except Exception:
                # log
                logger.exception(
                    "close_shell cancel queued failed : client_ip=%s session_id=%s",
                    client_ip,
                    session_id
                )
                #
                self._send_text(500, "shell closed, but failed to cancel queued jobs")
                return
            
            # log
            logger.info(
                "close_shell done : client_ip=%s session_id=%s platform=%s cwd=%s",
                client_ip,
                session_id,
                session.get("platform"),
                session.get("cwd")
            )

            audit_logger.info(
                "session closed : client_ip=%s session_id=%s",
                client_ip,
                session_id
            )
            #

            self._send_text(200, "shell closed")
            return

        if parsed.path == "/ack_response":
            response_ids = payload.get("response_ids")

            if isinstance(response_ids, list):
                ids = [str(item) for item in response_ids]
            else:
                single = str(payload.get("response_id", "")).strip()
                ids = [single] if single else []

            try:
                removed = self.server.jobs.ack_responses(ids)
            except Exception:
                # log
                logger.exception(
                    "ack_response failed : client_ip=%s ids_count=%s",
                    client_ip,
                    len(ids)
                )
                #
                self._send_text(500, "failed to ack response")
                return
            # log
            logger.debug(
                "ack_response done : client_ip=%s ids_count=%s acknowledged=%s",
                client_ip,
                len(ids),
                removed
            )
            #

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
            "[{}] [NODE {}] {} {}\n".format(
                datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                self.client_address[0],
                self.command,
                path,
            )
        )