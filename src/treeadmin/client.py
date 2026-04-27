from __future__ import annotations

import http.client
import json
import os
import re
import socket
import threading
from pathlib import Path
from typing import Any

from src.treeadmin.config import ClientConfig
from src.treeadmin.routing import build_first_request

CONFIG_PATH = Path("config/config_client.json")
OUTPUT_FILE_RE = re.compile(r"\s+#F\[(.+?)\]F#\s*$")

ACTIVE_POLL_INTERVAL_SECONDS = float(os.getenv("TREEADMIN_ACTIVE_POLL_INTERVAL", "0.5"))
BACKGROUND_POLL_INTERVAL_SECONDS = float(os.getenv("TREEADMIN_BACKGROUND_POLL_INTERVAL", "1.5"))

_PRINT_LOCK = threading.RLock()
_POLLERS_LOCK = threading.RLock()
_CWD_LOCK = threading.RLock()
_PULL_LOCKS_LOCK = threading.RLock()
_SEEN_RESPONSES_LOCK = threading.RLock()

_RESULT_POLLERS: dict[tuple[str, str], threading.Thread] = {}
_RESULT_POLL_STOP_EVENTS: dict[tuple[str, str], threading.Event] = {}
_RESULT_POLL_MODES: dict[tuple[str, str], str] = {}
_SESSION_LAST_CWD: dict[tuple[str, str], str] = {}

_PULL_LOCKS: dict[tuple[str, str], threading.RLock] = {}
_SEEN_RESPONSE_IDS: set[tuple[str, str]] = set()


def _safe_print(*args, **kwargs) -> None:
    with _PRINT_LOCK:
        print(*args, **kwargs)


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


def _set_session_cwd(target_id: str, session_id: str, cwd: str) -> None:
    if not cwd:
        return

    with _CWD_LOCK:
        _SESSION_LAST_CWD[(target_id, session_id)] = cwd


def _get_session_cwd(target_id: str, session_id: str, fallback: str = "") -> str:
    with _CWD_LOCK:
        return _SESSION_LAST_CWD.get((target_id, session_id), fallback)


def _pull_lock_key(target_id: str, session_id: str | None) -> tuple[str, str]:
    return target_id, session_id or ""


def _get_pull_lock(target_id: str, session_id: str | None) -> threading.RLock:
    key = _pull_lock_key(target_id, session_id)

    with _PULL_LOCKS_LOCK:
        lock = _PULL_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PULL_LOCKS[key] = lock
        return lock


def _response_seen_key(target_id: str, response_id: str) -> tuple[str, str]:
    return target_id, response_id


def _is_response_seen(target_id: str, response_id: str) -> bool:
    if not response_id:
        return False

    with _SEEN_RESPONSES_LOCK:
        return _response_seen_key(target_id, response_id) in _SEEN_RESPONSE_IDS


def _mark_response_seen(target_id: str, response_id: str) -> None:
    if not response_id:
        return

    with _SEEN_RESPONSES_LOCK:
        _SEEN_RESPONSE_IDS.add(_response_seen_key(target_id, response_id))


def _print_response_payload(payload: dict[str, Any]) -> None:
    node_id = str(payload.get("node_id", "unknown-node"))
    session_id = str(payload.get("session_id", ""))
    job_id = str(payload.get("job_id", ""))
    command = str(payload.get("command", ""))
    status = str(payload.get("status", ""))
    output = str(payload.get("output", ""))
    cwd = str(payload.get("cwd", ""))
    returncode = payload.get("returncode")
    error = payload.get("error")

    _safe_print(f"\n[from {node_id}][session {session_id}][job {job_id}] {command}")

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


def _ack_responses(
    target_id: str,
    response_ids: list[str],
    *,
    quiet: bool = False,
) -> bool:
    clean_ids = [str(item).strip() for item in response_ids if str(item).strip()]
    if not clean_ids:
        return True

    ack_status, ack_raw = _request(
        "POST",
        target_id,
        "/ack_response",
        payload={"response_ids": clean_ids},
    )

    if ack_status != 200:
        if not quiet:
            _safe_print(f"[{ack_status}] failed to ack pulled results: {ack_raw}")
        return False

    return True


def _pull_pending_results(
    target_id: str,
    session_id: str | None = None,
    *,
    quiet: bool = False,
) -> int:
    pull_lock = _get_pull_lock(target_id, session_id)

    acquired = pull_lock.acquire(blocking=False)
    if not acquired:
        return 0

    try:
        query = {"session_id": session_id} if session_id else None

        status, raw = _request(
            "GET",
            target_id,
            "/pull_pending_results",
            extra_query=query,
        )

        if status != 200:
            if not quiet:
                _safe_print(f"[{status}] failed to pull pending results: {raw}")
            return 0

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            if not quiet:
                _safe_print(f"[502] invalid JSON from server: {raw}")
            return 0

        responses = data.get("responses", [])
        if not isinstance(responses, list):
            return 0

        response_ids_to_ack: list[str] = []
        printed_count = 0

        for item in responses:
            if not isinstance(item, dict):
                continue

            response_id = str(item.get("response_id", "")).strip()
            if not response_id:
                continue

            response_ids_to_ack.append(response_id)

            if _is_response_seen(target_id, response_id):
                continue

            _mark_response_seen(target_id, response_id)
            _print_response_payload(item)
            printed_count += 1

            response_session_id = str(item.get("session_id", "")).strip()
            response_cwd = str(item.get("cwd", "")).strip()

            if response_session_id and response_cwd:
                _set_session_cwd(target_id, response_session_id, response_cwd)

        if response_ids_to_ack:
            _ack_responses(target_id, response_ids_to_ack, quiet=quiet)

        return printed_count

    finally:
        pull_lock.release()


def _poll_interval_for_mode(mode: str) -> float:
    if mode == "active":
        return ACTIVE_POLL_INTERVAL_SECONDS
    return BACKGROUND_POLL_INTERVAL_SECONDS


def _result_poller_loop(
    target_id: str,
    session_id: str,
    stop_event: threading.Event,
) -> None:
    key = (target_id, session_id)

    while not stop_event.is_set():
        try:
            _pull_pending_results(target_id, session_id, quiet=True)
        except Exception:
            pass

        with _POLLERS_LOCK:
            mode = _RESULT_POLL_MODES.get(key, "background")

        stop_event.wait(_poll_interval_for_mode(mode))


def _start_or_update_result_poller(target_id: str, session_id: str, mode: str) -> None:
    if mode not in {"active", "background"}:
        raise ValueError("poller mode must be 'active' or 'background'")

    key = (target_id, session_id)

    with _POLLERS_LOCK:
        _RESULT_POLL_MODES[key] = mode

        thread = _RESULT_POLLERS.get(key)
        if thread is not None and thread.is_alive():
            return

        stop_event = threading.Event()
        thread = threading.Thread(
            target=_result_poller_loop,
            args=(target_id, session_id, stop_event),
            daemon=True,
            name=f"result-poller-{target_id}-{session_id}",
        )

        _RESULT_POLL_STOP_EVENTS[key] = stop_event
        _RESULT_POLLERS[key] = thread
        thread.start()


def _stop_result_poller(target_id: str, session_id: str) -> None:
    key = (target_id, session_id)

    with _POLLERS_LOCK:
        stop_event = _RESULT_POLL_STOP_EVENTS.pop(key, None)
        _RESULT_POLLERS.pop(key, None)
        _RESULT_POLL_MODES.pop(key, None)

    if stop_event is not None:
        stop_event.set()


def _stop_all_result_pollers() -> None:
    with _POLLERS_LOCK:
        items = list(_RESULT_POLL_STOP_EVENTS.items())
        _RESULT_POLL_STOP_EVENTS.clear()
        _RESULT_POLLERS.clear()
        _RESULT_POLL_MODES.clear()

    for _, stop_event in items:
        stop_event.set()


def ensure_client_background_services(target_id: str = "pc2") -> None:
    return None


def open_shell(target_id: str) -> tuple[str | None, str | None]:
    status, raw = _request(
        "POST",
        target_id,
        "/open_shell",
        payload={},
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

    _set_session_cwd(target_id, session_id, cwd)
    _start_or_update_result_poller(target_id, session_id, "active")
    _pull_pending_results(target_id, session_id, quiet=True)

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

    matched_session: dict[str, Any] | None = None

    for item in sessions:
        if str(item.get("session_id", "")) == session_id:
            matched_session = item
            break

    if matched_session is None:
        _safe_print(f"Session not found on server: {session_id}")
        return False

    cwd = str(matched_session.get("cwd", ""))
    _set_session_cwd(target_id, session_id, cwd)

    _start_or_update_result_poller(target_id, session_id, "active")
    _pull_pending_results(target_id, session_id, quiet=True)

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
                f"job={item.get('job_id')} :: {item.get('command')}"
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
        _safe_print(f"  session_id={session_id} platform={platform_name} cwd={cwd}")


def _print_help() -> None:
    _safe_print("\nAvailable commands:")
    _safe_print("  help      show this help")
    _safe_print("  jobs      show queued and completed commands in this session")
    _safe_print("  sessions  show active sessions on current server")
    _safe_print("  pull      fetch undelivered results from server now")
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
        _set_session_cwd(target_id, session_id, current_dir)

    if not session_id:
        _safe_print("Failed to open shell session")
        return

    current_dir = current_dir or ""
    close_remote_on_exit = False

    _start_or_update_result_poller(target_id, session_id, "active")

    _safe_print("Commands are queued automatically. Results are fetched from server in background.")
    _safe_print("Type 'help' to show available commands.")

    while True:
        try:
            prompt_dir = _get_session_cwd(target_id, session_id, current_dir)
            raw_cmd = input(f"[{target_id}][session {session_id}] {prompt_dir} > ").strip()
        except KeyboardInterrupt:
            _start_or_update_result_poller(target_id, session_id, "background")
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
            count = _pull_pending_results(target_id, session_id, quiet=False)
            _safe_print(f"Pulled responses: {count}")
            continue

        if lowered in {"exit", "quit", ":leave", "leave"}:
            _start_or_update_result_poller(target_id, session_id, "background")
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
        _stop_result_poller(target_id, session_id)

        if status != 200:
            _safe_print(f"[{status}] {response}")
        else:
            _safe_print(f"Closed remote session: {session_id}")


def run_client() -> None:
    target_id = input("Target server node id (for example pc2): ").strip() or "pc2"

    try:
        status, body = ping_server(target_id)
    except Exception as e:
        _safe_print(f"Ping failed: {e}")
        return

    _safe_print("CLIENT: sent REQUEST GET: Hello, Server")
    _safe_print(f"CLIENT: GET: {body.strip()} (status={status})")