from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path
from typing import Any

from src.treeadmin.routing import build_first_request
from src.treeadmin.config import ClientConfig


CONFIG_PATH = Path("api/config_client.json")


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


def open_shell(target_id: str) -> tuple[str | None, str | None]:
    status, raw = _request(
        "POST",
        target_id,
        "/open_shell",
    )

    if status != 200:
        print(f"[{status}] {raw}")
        return None, None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[502] invalid JSON from server: {raw}")
        return None, None

    session_id = data.get("session_id")
    cwd = data.get("cwd", "")

    if not isinstance(session_id, str) or not session_id.strip():
        print("[502] server returned invalid session_id")
        return None, None

    if not isinstance(cwd, str):
        cwd = ""

    return session_id, cwd


def send_exec(target_id: str, session_id: str, command: str) -> tuple[int, dict[str, str]]:
    status, raw = _request(
        "POST",
        target_id,
        "/send_command",
        payload={
            "session_id": session_id,
            "command": command,
        },
        timeout=15,
    )

    if status != 200:
        return status, {
            "output": raw,
            "cwd": "",
        }

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return 502, {
            "output": f"invalid JSON from server: {raw}",
            "cwd": "",
        }

    output = data.get("output", "")
    cwd = data.get("cwd", "")

    if not isinstance(output, str):
        output = str(output)

    if not isinstance(cwd, str):
        cwd = ""

    return 200, {
        "output": output,
        "cwd": cwd,
    }


def close_shell(target_id: str, session_id: str) -> tuple[int, str]:
    return _request(
        "POST",
        target_id,
        "/close_shell",
        payload={"session_id": session_id},
    )


def clean_output(raw: str) -> str:
    return raw.replace("__PROMPT__", "").strip()


def interactive_shell(target_id: str) -> None:
    session_id, current_dir = open_shell(target_id)
    if not session_id:
        print("Failed to open shell session")
        return

    current_dir = current_dir or ""

    try:
        while True:
            cmd = input(f"command: {current_dir} > ").strip()

            if not cmd:
                continue

            if cmd.lower() in {"exit", "quit"}:
                break

            status, result = send_exec(target_id, session_id, cmd)

            if status != 200:
                print(f"[{status}] {result.get('output', '')}")
                continue

            output = result.get("output", "").strip()
            new_cwd = result.get("cwd", "").strip()

            if new_cwd:
                current_dir = new_cwd

            if output and output != "[empty output]":
                print(output)

    except KeyboardInterrupt:
        print("\nExecute command stopped")

    finally:
        status, response = close_shell(target_id, session_id)
        if status != 200:
            print(f"[{status}] {response}")


def ping_server(target_id: str, msg: str = "Hello, Server") -> tuple[int, str]:
    return _request(
        "GET",
        target_id,
        "/hello",
        extra_query={"msg": msg},
    )


def run_client() -> None:
    target_id = input("Target server node id (for example pc2): ").strip() or "pc2"

    status, body = ping_server(target_id)

    print("CLIENT: sent REQUEST GET: Hello, Server")
    print(f"CLIENT: GET: {body.strip()} (status={status})")