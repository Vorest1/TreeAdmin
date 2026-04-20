from __future__ import annotations

import http.client
import json
import os
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from src.treeadmin.config import ClientConfig
from src.treeadmin.routing import build_first_request

CONFIG_PATH = Path("config/config_client.json")
OUTPUT_FILE_RE = re.compile(r"\s+#F\[(.+?)\]F#\s*$")

CALLBACK_BIND_HOST = os.getenv("TREEADMIN_CALLBACK_BIND_HOST", "0.0.0.0")
CALLBACK_PORT = int(os.getenv("TREEADMIN_CALLBACK_PORT", "9005"))
CALLBACK_PATH = os.getenv("TREEADMIN_CALLBACK_PATH", "/deliver_result")
CALLBACK_HOST_OVERRIDE = os.getenv("TREEADMIN_CALLBACK_HOST", "").strip()

_PRINT_LOCK = threading.RLock()
_CALLBACK_SERVER_LOCK = threading.RLock()
_CALLBACK_SERVER = None
_CALLBACK_THREAD: threading.Thread | None = None
_CALLBACK_STARTED = False


def _safe_print(*args, **kwargs) -> None:
    with _PRINT_LOCK:
        print(*args, **kwargs)


class _ClientCallbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, handler_cls, callback_path: str):
        super().__init__(server_address, handler_cls)
        self.callback_path = callback_path


class _ClientCallbackHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_text(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != self.server.callback_path:
            self._send_text(404, "not found")
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length > 0 else b""

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception as e:
            self._send_text(400, f"bad json: {e}")
            return

        if not isinstance(payload, dict):
            self._send_text(400, "json body must be object")
            return

        _print_response_payload(payload, source="callback")
        self._send_text(200, "received")

    def log_message(self, fmt, *args):
        return


def _start_background_callback_server() -> None:
    global _CALLBACK_SERVER, _CALLBACK_THREAD, _CALLBACK_STARTED

    with _CALLBACK_SERVER_LOCK:
        if _CALLBACK_STARTED:
            return

        server = _ClientCallbackHTTPServer(
            (CALLBACK_BIND_HOST, CALLBACK_PORT),
            _ClientCallbackHandler,
            CALLBACK_PATH,
        )
        thread = threading.Thread(
            target=server.serve_forever,
            daemon=True,
            name="client-callback-server",
        )
        thread.start()

        _CALLBACK_SERVER = server
        _CALLBACK_THREAD = thread
        _CALLBACK_STARTED = True

        _safe_print(
            f"[callback] background server started on "
            f"{CALLBACK_BIND_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"
        )


def _load_client_config() -> ClientConfig:
    return ClientConfig.load(CONFIG_PATH)


def _guess_callback_host(target_id: str) -> str:
    if CALLBACK_HOST_OVERRIDE:
        return CALLBACK_HOST_OVERRIDE

    try:
        client_config = _load_client_config()
        host, port, _ = build_first_request(
            client_config=client_config,
            target_node_id=target_id,
            endpoint_path="/hello",
            extra_query={"msg": "discover"},
        )

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, port))
            ip = s.getsockname()[0]
            if ip:
                return ip
    except Exception:
        pass

    return "127.0.0.1"


def _get_callback_info(target_id: str) -> dict[str, Any]:
    _start_background_callback_server()
    return {
        "host": _guess_callback_host(target_id),
        "port": CALLBACK_PORT,
        "path": CALLBACK_PATH,
    }


def ensure_client_background_services(target_id: str = "pc2") -> dict[str, Any]:
    callback = _get_callback_info(target_id)
    _safe_print(
        f"[callback] background server active; advertised as "
        f"{callback['host']}:{callback['port']}{callback['path']}"
    )
    return callback


def _request(
    method: str,
    target_id: str,
    endpoint_path: str,
    *,
    payload: dict[str, Any] | None = None,
    extra_query: dict[str, Any] | None = None,
    timeout: int | None = None,
) -> tuple[int, str]:
    client_config = _load_client_config()

    host, port, url_path = build_first_request(
        client_config=client_config,
        target_node_id=target_id,
        endpoint_path=endpoint_path,
        extra_query=extra_query,
    )

    body = b""
    headers: dict[str, str] = {}

    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    headers["Content-Length"] = str(len(body))

    conn = http.client.HTTPConnection(
        host,
        port,
        timeout=timeout if timeout is not None else client_config.timeout,
    )

    try:
        conn.request(method, url_path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        return resp.status, raw
    except socket.timeout:
        return 408, "request timeout: server did not respond in time"
    except OSError as e:
        return 503, f"connection error: {e}"
    except Exception as e:
        return 500, f"client error: {e}"
    finally:
        conn.close()


def _extract_output_file(raw_command: str) -> tuple[str, str | None]:
    match = OUTPUT_FILE_RE.search(raw_command)
    if not match:
        return raw_command.strip(), None

    output_path = match.group(1).strip()
    cleaned_command = raw_command[:match.start()].strip()
    return cleaned_command, output_path


def _save_command_output(output_path: str, text: str) -> None:
    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _print_response_payload(payload: dict[str, Any], source: str = "callback") -> None:
    node_id = str(payload.get("node_id", "unknown-node"))
    session_id = str(payload.get("session_id", ""))
    job_id = str(payload.get("job_id", ""))
    command = str(payload.get("command", ""))
    status = str(payload.get("status", ""))
    output = str(payload.get("output", ""))
    cwd = str(payload.get("cwd", ""))
    returncode = payload.get("returncode")
    error = payload.get("error")

    prefix = f"\n[from {node_id}][session {session_id}][job {job_id}] {command}"
    if source == "pull":
        prefix += " [pulled]"
    _safe_print(prefix)

    if status:
        _safe_print(f"[status={status}]")
    if output:
        _safe_print(output)
    if cwd:
        _safe_print(f"[cwd={cwd}]")
    if isinstance(returncode, int):
        _safe_print(f"[returncode={returncode}]")
    if error:
        _safe_print(f"[error={error}]")


def _pull_pending_results(target_id: str, session_id: str | None = None) -> int:
    query = {"session_id": session_id} if session_id else None

    status, raw = _request(
        "GET",
        target_id,
        "/pull_pending_results",
        extra_query=query,
    )

    if status != 200:
        _safe_print(f"[{status}] failed to pull pending results: {raw}")
        return 0

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _safe_print(f"[502] invalid JSON from server: {raw}")
        return 0

    responses = data.get("responses", [])
    if not isinstance(responses, list):
        return 0

    response_ids: list[str] = []

    for item in responses:
        if not isinstance(item, dict):
            continue
        _print_response_payload(item, source="pull")
        response_id = str(item.get("response_id", "")).strip()
        if response_id:
            response_ids.append(response_id)

    if response_ids:
        ack_status, ack_raw = _request(
            "POST",
            target_id,
            "/ack_response",
            payload={"response_ids": response_ids},
        )
        if ack_status != 200:
            _safe_print(f"[{ack_status}] failed to ack pulled results: {ack_raw}")

    return len(response_ids)


def _register_callback(target_id: str, session_id: str) -> bool:
    callback = _get_callback_info(target_id)

    status, raw = _request(
        "POST",
        target_id,
        "/register_callback",
        payload={
            "session_id": session_id,
            "client_callback": callback,
        },
    )

    if status != 200:
        _safe_print(f"[{status}] failed to register callback: {raw}")
        return False

    return True


def open_shell(target_id: str) -> tuple[str | None, str | None]:
    callback = _get_callback_info(target_id)

    status, raw = _request(
        "POST",
        target_id,
        "/open_shell",
        payload={"client_callback": callback},
    )

    if status != 200:
        _safe_print(f"[{status}] {raw}")
        return None, None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _safe_print(f"[502] invalid JSON from server: {raw}")
        return None, None

    session_id = data.get("session_id")
    cwd = data.get("cwd", "")

    if not isinstance(session_id, str) or not session_id.strip():
        _safe_print("[502] server returned invalid session_id")
        return None, None

    if not isinstance(cwd, str):
        cwd = ""

    _pull_pending_results(target_id, session_id)
    return session_id, cwd


def list_sessions(target_id: str) -> list[dict[str, Any]] | None:
    status, raw = _request("GET", target_id, "/list_sessions")
    if status != 200:
        _safe_print(f"[{status}] {raw}")
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _safe_print(f"[502] invalid JSON from server: {raw}")
        return None

    sessions = data.get("sessions", [])
    if not isinstance(sessions, list):
        return None

    result: list[dict[str, Any]] = []
    for item in sessions:
        if isinstance(item, dict):
            result.append(item)

    return result


def attach_shell(target_id: str, session_id: str) -> bool:
    sessions = list_sessions(target_id)
    if sessions is None:
        return False

    if session_id not in {str(item.get("session_id", "")) for item in sessions}:
        _safe_print(f"Session not found on server: {session_id}")
        return False

    if not _register_callback(target_id, session_id):
        return False

    _pull_pending_results(target_id, session_id)
    return True


def send_queued_command(target_id: str, session_id: str, command: str) -> tuple[int, dict[str, Any]]:
    status, raw = _request(
        "POST",
        target_id,
        "/send_command",
        payload={"session_id": session_id, "command": command},
    )

    if status not in {200, 202}:
        return status, {"error": raw}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return 502, {"error": f"invalid JSON from server: {raw}"}

    return status, data


def get_session_jobs(target_id: str, session_id: str) -> dict[str, Any] | None:
    status, raw = _request(
        "GET",
        target_id,
        "/session_jobs",
        extra_query={"session_id": session_id},
    )
    if status != 200:
        _safe_print(f"[{status}] {raw}")
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _safe_print(f"[502] invalid JSON from server: {raw}")
        return None

    return data


def close_shell(target_id: str, session_id: str) -> tuple[int, str]:
    return _request(
        "POST",
        target_id,
        "/close_shell",
        payload={"session_id": session_id},
    )


def ping_server(target_id: str, msg: str = "Hello, Server") -> tuple[int, str]:
    return _request(
        "GET",
        target_id,
        "/hello",
        extra_query={"msg": msg},
    )


def _print_session_jobs(data: dict[str, Any]) -> None:
    _safe_print(f"\nSession {data.get('session_id')} queue:")

    queue_items = data.get("queue", [])
    if not queue_items:
        _safe_print("  <empty>")
    else:
        for item in queue_items:
            _safe_print(
                f"  [{item.get('job_id')}] {item.get('status')} :: {item.get('command')}"
            )

    _safe_print("History:")
    history_items = data.get("history", [])
    if not history_items:
        _safe_print("  <empty>")
    else:
        for item in history_items[-10:]:
            _safe_print(
                f"  [{item.get('job_id')}] {item.get('status')} "
                f"rc={item.get('returncode')} :: {item.get('command')}"
            )

    _safe_print("Pending responses:")
    response_items = data.get("responses", [])
    if not response_items:
        _safe_print("  <empty>")
    else:
        for item in response_items:
            _safe_print(
                f"  [response {item.get('response_id')}] "
                f"status={item.get('delivery_status')} "
                f"attempts={item.get('delivery_attempts')} "
                f":: {item.get('command')}"
            )


def _print_sessions(sessions: list[dict[str, Any]]) -> None:
    if not sessions:
        _safe_print("No active sessions on server")
        return

    _safe_print("\nActive sessions:")
    for item in sessions:
        session_id = str(item.get("session_id", ""))
        platform_name = str(item.get("platform", ""))
        cwd = str(item.get("cwd", ""))
        callback = item.get("client_callback")
        callback_text = ""
        if isinstance(callback, dict):
            callback_text = (
                f" callback={callback.get('host')}:{callback.get('port')}{callback.get('path')}"
            )
        _safe_print(
            f"  session_id={session_id} platform={platform_name} cwd={cwd}{callback_text}"
        )


def _print_help() -> None:
    _safe_print("\nAvailable commands:")
    _safe_print("  help      show this help")
    _safe_print("  jobs      show queued and completed commands in this session")
    _safe_print("  sessions  show active sessions on current server")
    _safe_print("  pull      fetch undelivered results from server")
    _safe_print("  exit      leave this shell window, keep remote session alive")
    _safe_print("  close     close remote session and leave")


def interactive_shell(
    target_id: str,
    existing_session_id: str | None = None,
    existing_cwd: str | None = None,
) -> None:
    if existing_session_id is None:
        session_id, current_dir = open_shell(target_id)
    else:
        if not attach_shell(target_id, existing_session_id):
            return
        session_id = existing_session_id
        current_dir = existing_cwd or ""

    if not session_id:
        _safe_print("Failed to open shell session")
        return

    current_dir = current_dir or ""
    close_remote_on_exit = False

    _safe_print("Commands are queued automatically. Results arrive via background callback server.")
    _safe_print("Type 'help' to show available commands.")

    while True:
        try:
            raw_cmd = input(f"[{target_id}][session {session_id}] {current_dir} > ").strip()
        except KeyboardInterrupt:
            _safe_print(f"\nLeft session {session_id}. Remote session is still active.")
            return

        if not raw_cmd:
            continue

        lowered = raw_cmd.lower()

        if lowered in {"help", ":help"}:
            _print_help()
            continue

        if lowered in {"jobs", ":jobs"}:
            data = get_session_jobs(target_id, session_id)
            if data is not None:
                _print_session_jobs(data)
            continue

        if lowered in {"sessions", ":sessions"}:
            sessions = list_sessions(target_id)
            if sessions is not None:
                _print_sessions(sessions)
            continue

        if lowered in {"pull", ":pull"}:
            count = _pull_pending_results(target_id, session_id)
            _safe_print(f"Pulled responses: {count}")
            continue

        if lowered in {"exit", "quit", ":leave", "leave"}:
            _safe_print(f"Left session {session_id}. Remote session is still active.")
            return

        if lowered in {"close", ":close"}:
            close_remote_on_exit = True
            break

        cmd, output_file = _extract_output_file(raw_cmd)
        if not cmd:
            _safe_print("Empty command")
            continue

        status, result = send_queued_command(target_id, session_id, cmd)
        if status not in {200, 202}:
            _safe_print(f"[{status}] {result.get('error', '')}")
            continue

        _safe_print(f"Queued job {result.get('job_id')} on session {session_id}: {cmd}")

        if output_file:
            _safe_print(
                "#F[...]F# is not applied automatically in queued mode. "
                "Save the result manually after it arrives."
            )

    if close_remote_on_exit:
        status, response = close_shell(target_id, session_id)
        if status != 200:
            _safe_print(f"[{status}] {response}")
        else:
            _safe_print(f"Closed remote session: {session_id}")


def run_client() -> None:
    target_id = input("Target server node id (for example pc2): ").strip() or "pc2"

    try:
        ensure_client_background_services(target_id)
    except Exception as e:
        _safe_print(f"[callback] failed to start background callback server: {e}")

    try:
        status, body = ping_server(target_id)
    except Exception as e:
        _safe_print(f"Ping failed: {e}")
        return

    _safe_print("CLIENT: sent REQUEST GET: Hello, Server")
    _safe_print(f"CLIENT: GET: {body.strip()} (status={status})")
