from __future__ import annotations

import http.client
import json
import re
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

from src.treeadmin.config import ClientConfig
from src.treeadmin.routing import build_first_request


CONFIG_PATH = Path("config/config_client.json")
OUTPUT_FILE_RE = re.compile(r"\s+#F\[(.+?)\]F#\s*$")
EVENT_STREAM_TIMEOUT = 70
EVENT_RECONNECT_DELAY = 3


def _load_client_config() -> ClientConfig:
    return ClientConfig.load(CONFIG_PATH)


def _request(
    method: str,
    target_id: str,
    endpoint_path: str,
    *,
    payload: dict[str, Any] | None = None,
    extra_query: dict[str, Any] | None = None,
    timeout: int | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, str, dict[str, str]]:
    client_config = _load_client_config()

    host, port, url_path = build_first_request(
        client_config=client_config,
        target_node_id=target_id,
        endpoint_path=endpoint_path,
        extra_query=extra_query,
    )

    body = b""
    req_headers: dict[str, str] = dict(headers or {})

    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers["Content-Type"] = "application/json; charset=utf-8"

    req_headers["Content-Length"] = str(len(body))

    conn = http.client.HTTPConnection(
        host,
        port,
        timeout=timeout if timeout is not None else client_config.timeout,
    )

    try:
        conn.request(method, url_path, body=body, headers=req_headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        response_headers = {k: v for k, v in resp.getheaders()}
        return resp.status, raw, response_headers

    except socket.timeout:
        return 408, "request timeout: server did not respond in time", {}

    except OSError as e:
        return 503, f"connection error: {e}", {}

    except Exception as e:
        return 500, f"client error: {e}", {}

    finally:
        conn.close()


def _request_json(
    method: str,
    target_id: str,
    endpoint_path: str,
    *,
    payload: dict[str, Any] | None = None,
    extra_query: dict[str, Any] | None = None,
    timeout: int | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any] | None, str]:
    status, raw, _ = _request(
        method,
        target_id,
        endpoint_path,
        payload=payload,
        extra_query=extra_query,
        timeout=timeout,
        headers=headers,
    )

    if status < 200 or status >= 300:
        return status, None, raw

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return 502, None, f"invalid JSON from server: {raw}"

    if not isinstance(data, dict):
        return 502, None, f"invalid JSON object from server: {raw}"

    return status, data, ""


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


def ping_server(target_id: str, msg: str = "Hello, Server") -> tuple[int, str]:
    status, raw, _ = _request(
        "GET",
        target_id,
        "/hello",
        extra_query={"msg": msg},
    )
    return status, raw


def open_shell(target_id: str) -> tuple[str | None, str | None, str | None]:
    status, data, error = _request_json("POST", target_id, "/open_shell")
    if status != 200 or data is None:
        print(f"[{status}] {error}")
        return None, None, None

    session_id = data.get("session_id")
    cwd = data.get("cwd", "")
    node_id = data.get("node_id", "")

    if not isinstance(session_id, str) or not session_id.strip():
        print("[502] server returned invalid session_id")
        return None, None, None

    if not isinstance(cwd, str):
        cwd = ""
    if not isinstance(node_id, str):
        node_id = ""

    return session_id, cwd, node_id


def list_sessions(target_id: str) -> tuple[int, list[dict[str, Any]], str]:
    status, data, error = _request_json("GET", target_id, "/list_sessions")
    if status != 200 or data is None:
        return status, [], error

    sessions = data.get("sessions", [])
    if not isinstance(sessions, list):
        return 502, [], "invalid 'sessions' payload"

    normalized: list[dict[str, Any]] = []
    for item in sessions:
        if isinstance(item, dict):
            normalized.append(item)
    return 200, normalized, ""


def get_session_jobs(target_id: str, session_id: str) -> tuple[int, dict[str, Any] | None, str]:
    status, data, error = _request_json(
        "GET",
        target_id,
        "/session_jobs",
        extra_query={"session_id": session_id},
    )
    if status != 200:
        return status, None, error
    return 200, data, ""


def send_exec(target_id: str, session_id: str, command: str) -> tuple[int, dict[str, Any]]:
    status, data, error = _request_json(
        "POST",
        target_id,
        "/send_command",
        payload={
            "session_id": session_id,
            "command": command,
        },
        timeout=15,
    )

    if status != 202 or data is None:
        return status, {
            "error": error,
            "job_id": None,
            "status": None,
            "cwd": "",
            "node_id": "",
        }

    return 202, {
        "job_id": data.get("job_id"),
        "status": data.get("status"),
        "cwd": data.get("cwd", ""),
        "node_id": data.get("node_id", ""),
    }


def close_shell(target_id: str, session_id: str) -> tuple[int, str]:
    status, raw, _ = _request(
        "POST",
        target_id,
        "/close_shell",
        payload={"session_id": session_id},
    )
    return status, raw


def ack_event(target_id: str, event_id: str) -> tuple[int, str]:
    status, raw, _ = _request(
        "POST",
        target_id,
        "/ack_event",
        payload={"event_id": event_id},
        timeout=15,
    )
    return status, raw


class EventStreamWorker:
    def __init__(
        self,
        *,
        target_id: str,
        session_id: str,
        cwd_state: dict[str, str],
        pending_files: dict[str, str],
        print_lock: threading.Lock,
    ) -> None:
        self.target_id = target_id
        self.session_id = session_id
        self.cwd_state = cwd_state
        self.pending_files = pending_files
        self.print_lock = print_lock
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"events-{session_id}")
        self.last_event_id: str | None = None

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=1.5)

    def _open_stream(self) -> tuple[http.client.HTTPConnection | None, http.client.HTTPResponse | None, str | None]:
        client_config = _load_client_config()
        host, port, url_path = build_first_request(
            client_config=client_config,
            target_node_id=self.target_id,
            endpoint_path="/events",
            extra_query={"session_id": self.session_id},
        )

        headers: dict[str, str] = {"Accept": "text/event-stream"}
        if self.last_event_id:
            headers["Last-Event-ID"] = self.last_event_id

        conn = http.client.HTTPConnection(host, port, timeout=EVENT_STREAM_TIMEOUT)
        try:
            conn.request("GET", url_path, headers=headers)
            resp = conn.getresponse()
            if resp.status != 200:
                body = resp.read().decode("utf-8", errors="replace")
                conn.close()
                return None, None, f"[{resp.status}] {body}"
            return conn, resp, None
        except Exception as e:
            try:
                conn.close()
            except Exception:
                pass
            return None, None, str(e)

    def _ack(self, event_id: str) -> None:
        status, raw = ack_event(self.target_id, event_id)
        if status not in {200, 404}:
            with self.print_lock:
                print(f"[events] ack failed for event {event_id}: [{status}] {raw}")

    def _handle_event(self, event: dict[str, Any]) -> None:
        event_id = str(event.get("event_id", "")).strip()
        if event_id:
            self.last_event_id = event_id

        command = str(event.get("command", "")).strip()
        job_id = str(event.get("job_id", "")).strip()
        status = str(event.get("status", "")).strip()
        output = str(event.get("output", "") or "")
        cwd = str(event.get("cwd", "") or "")
        node_id = str(event.get("node_id", "") or self.target_id)
        returncode = event.get("returncode")
        error = event.get("error")

        if cwd:
            self.cwd_state["cwd"] = cwd

        file_path = self.pending_files.pop(job_id, None) if job_id else None
        if file_path:
            try:
                _save_command_output(file_path, output + ("\n" if output else ""))
            except Exception as e:
                with self.print_lock:
                    print(f"[events] failed to save output to '{file_path}': {e}")

        with self.print_lock:
            print("")
            print(f"[from {node_id}][session {self.session_id}][job {job_id}] {command}")
            if status:
                print(f"[status={status}]")
            if output:
                print(output)
            if error:
                print(error)
            if isinstance(returncode, int) and returncode != 0:
                print(f"[returncode={returncode}]")
            if cwd:
                print(f"[cwd={cwd}]")
            if file_path:
                print(f"[saved to {file_path}]")

        if event_id:
            self._ack(event_id)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            conn: http.client.HTTPConnection | None = None
            resp: http.client.HTTPResponse | None = None
            conn, resp, error = self._open_stream()
            if error is not None or conn is None or resp is None:
                if not self.stop_event.is_set():
                    with self.print_lock:
                        print(f"[events] reconnect after error: {error}")
                    self.stop_event.wait(EVENT_RECONNECT_DELAY)
                continue

            event_id = ""
            data_lines: list[str] = []

            try:
                while not self.stop_event.is_set():
                    raw_line = resp.fp.readline()  # type: ignore[union-attr]
                    if not raw_line:
                        raise ConnectionResetError("event stream closed")

                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")

                    if not line:
                        if data_lines:
                            payload = "\n".join(data_lines)
                            try:
                                event = json.loads(payload)
                            except json.JSONDecodeError:
                                event = {"event_id": event_id, "command": "", "output": payload}
                            if event_id and "event_id" not in event:
                                event["event_id"] = event_id
                            if str(event.get("session_id", "") or self.session_id) == self.session_id:
                                self._handle_event(event)
                        event_id = ""
                        data_lines = []
                        continue

                    if line.startswith(":"):
                        continue
                    if line.startswith("id:"):
                        event_id = line[3:].strip()
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                        continue
            except (socket.timeout, ConnectionError, OSError, http.client.HTTPException) as e:
                if not self.stop_event.is_set():
                    with self.print_lock:
                        print(f"[events] reconnect after error: {e}")
                    self.stop_event.wait(EVENT_RECONNECT_DELAY)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass


def _format_sessions(sessions: list[dict[str, Any]]) -> None:
    if not sessions:
        print("No active server sessions")
        return

    print("\nActive server sessions:")
    for item in sessions:
        print(
            f"  session_id={item.get('session_id')} | "
            f"cwd={item.get('cwd', '')} | "
            f"platform={item.get('platform', '')} | "
            f"node_id={item.get('node_id', '')}"
        )


def _print_session_jobs(data: dict[str, Any]) -> None:
    print(f"\nSession {data.get('session_id')}")

    queue = data.get("queue", [])
    history = data.get("history", [])
    responses = data.get("responses", [])

    print("Queue:")
    if isinstance(queue, list) and queue:
        for item in queue:
            print(
                f"  job {item.get('job_id')}: {item.get('command')} "
                f"[status={item.get('status')}]"
            )
    else:
        print("  <empty>")

    print("History:")
    if isinstance(history, list) and history:
        for item in history[-10:]:
            print(
                f"  job {item.get('job_id')}: {item.get('command')} "
                f"[status={item.get('status')}]"
            )
    else:
        print("  <empty>")

    print("Pending responses:")
    if isinstance(responses, list) and responses:
        for item in responses:
            print(
                f"  event {item.get('event_id')} -> job {item.get('job_id')}: "
                f"{item.get('command')}"
            )
    else:
        print("  <empty>")


def interactive_shell(
    target_id: str,
    session_id: str | None = None,
    current_dir: str | None = None,
) -> None:
    if not session_id:
        session_id, current_dir, _ = open_shell(target_id)
        if not session_id:
            print("Failed to open shell session")
            return

    cwd_state = {"cwd": current_dir or ""}
    pending_files: dict[str, str] = {}
    print_lock = threading.Lock()
    stream = EventStreamWorker(
        target_id=target_id,
        session_id=session_id,
        cwd_state=cwd_state,
        pending_files=pending_files,
        print_lock=print_lock,
    )
    stream.start()

    try:
        while True:
            prompt_cwd = cwd_state.get("cwd", "")
            raw_cmd = input(f"command[{session_id}]: {prompt_cwd} > ").strip()

            if not raw_cmd:
                continue

            if raw_cmd.lower() in {"exit", "quit", ":leave"}:
                print(f"Left session {session_id}. Remote session is still active.")
                break

            if raw_cmd.lower() == ":close":
                status, response = close_shell(target_id, session_id)
                if status != 200:
                    print(f"[{status}] {response}")
                else:
                    print(f"Remote session {session_id} closed")
                break

            if raw_cmd.lower() == ":jobs":
                status, data, error = get_session_jobs(target_id, session_id)
                if status != 200 or data is None:
                    print(f"[{status}] {error}")
                else:
                    _print_session_jobs(data)
                continue

            if raw_cmd.lower() == ":sessions":
                status, sessions, error = list_sessions(target_id)
                if status != 200:
                    print(f"[{status}] {error}")
                else:
                    _format_sessions(sessions)
                continue

            cmd, output_file = _extract_output_file(raw_cmd)
            if not cmd:
                print("Empty command")
                continue

            status, result = send_exec(target_id, session_id, cmd)
            if status == 404 and "session not found" in str(result.get("error", "")).lower():
                print("Remote session expired or was closed on server")
                break
            if status != 202:
                print(f"[{status}] {result.get('error', '')}")
                continue

            job_id = result.get("job_id")
            if isinstance(job_id, str) and output_file:
                pending_files[job_id] = output_file

            queued_cwd = str(result.get("cwd", "") or "")
            if queued_cwd:
                cwd_state["cwd"] = queued_cwd

            print(f"Queued job {job_id} on session {session_id}: {cmd}")

    except KeyboardInterrupt:
        print(f"\nLeft session {session_id}. Remote session is still active.")
    finally:
        stream.stop()


def _attach_existing_session_menu(target_id: str) -> None:
    status, sessions, error = list_sessions(target_id)
    if status != 200:
        print(f"[{status}] {error}")
        return

    _format_sessions(sessions)
    if not sessions:
        return

    session_id = input("Session id to attach: ").strip()
    if not session_id:
        print("Empty session id")
        return

    cwd = ""
    for item in sessions:
        if str(item.get("session_id")) == session_id:
            cwd = str(item.get("cwd", "") or "")
            break

    interactive_shell(target_id, session_id=session_id, current_dir=cwd)


def run_client() -> None:
    target_id = input("Target server node id [pc2]: ").strip() or "pc2"

    while True:
        print("\nEnter client function:")
        print("1) Ping server")
        print("2) Open new shell session")
        print("3) Attach to existing shell session")
        print("4) List active server sessions")
        print("5) Close remote session")
        print("0) Back")

        choice = input("> ").strip()

        if choice == "1":
            status, body = ping_server(target_id)
            print("CLIENT: sent REQUEST GET: Hello, Server")
            print(f"CLIENT: GET: {body.strip()} (status={status})")
        elif choice == "2":
            interactive_shell(target_id)
        elif choice == "3":
            _attach_existing_session_menu(target_id)
        elif choice == "4":
            status, sessions, error = list_sessions(target_id)
            if status != 200:
                print(f"[{status}] {error}")
            else:
                _format_sessions(sessions)
        elif choice == "5":
            session_id = input("Session id to close: ").strip()
            if not session_id:
                print("Empty session id")
                continue
            status, response = close_shell(target_id, session_id)
            if status != 200:
                print(f"[{status}] {response}")
            else:
                print(f"Remote session {session_id} closed")
        elif choice == "0":
            break
        else:
            print("Unknown menu item")


__all__ = [
    "ping_server",
    "open_shell",
    "list_sessions",
    "get_session_jobs",
    "send_exec",
    "close_shell",
    "interactive_shell",
    "run_client",
]
