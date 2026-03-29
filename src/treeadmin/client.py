from __future__ import annotations

import http.client
import json
import re
import socket
from pathlib import Path
from typing import Any

from src.treeadmin.config import ClientConfig
from src.treeadmin.routing import build_first_request


CONFIG_PATH = Path("api/config_client.json")
OUTPUT_FILE_RE = re.compile(r"\s+#F\[(.+?)\]F#\s*$")


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


def open_shell(target_id: str) -> tuple[str | None, str | None]:
    status, raw = _request("POST", target_id, "/open_shell")

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


def send_exec(
    target_id: str,
    session_id: str,
    command: str,
) -> tuple[int, dict[str, Any]]:
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
            "returncode": None,
        }

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return 502, {
            "output": f"invalid JSON from server: {raw}",
            "cwd": "",
            "returncode": None,
        }

    output = data.get("output", "")
    cwd = data.get("cwd", "")
    returncode = data.get("returncode")

    if not isinstance(output, str):
        output = str(output)

    if not isinstance(cwd, str):
        cwd = ""

    if not isinstance(returncode, int):
        returncode = None

    return 200, {
        "output": output,
        "cwd": cwd,
        "returncode": returncode,
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


def ping_server(target_id: str, msg: str = "Hello, Server") -> tuple[int, str]:
    return _request(
        "GET",
        target_id,
        "/hello",
        extra_query={"msg": msg},
    )


def interactive_shell(target_id: str) -> None:
    session_id, current_dir = open_shell(target_id)
    if not session_id:
        print("Failed to open shell session")
        return

    current_dir = current_dir or ""

    try:
        while True:
            raw_cmd = input(f"command: {current_dir} > ").strip()

            if not raw_cmd:
                continue

            if raw_cmd.lower() in {"exit", "quit"}:
                break

            cmd, output_file = _extract_output_file(raw_cmd)

            if not cmd:
                print("Empty command")
                continue

            status, result = send_exec(target_id, session_id, cmd)

            if status == 404 and "session not found" in str(result.get("output", "")).lower():
                print("Remote session expired or was closed on server")
                break

            if status != 200:
                print(f"[{status}] {result.get('output', '')}")
                continue

            output = str(result.get("output", "")).strip()
            new_cwd = str(result.get("cwd", "")).strip()
            returncode = result.get("returncode")

            if new_cwd:
                current_dir = new_cwd

            if output:
                print(output)

            if output_file:
                try:
                    _save_command_output(output_file, output + ("\n" if output else ""))
                    print(f"Output saved to: {output_file}")
                except Exception as e:
                    print(f"Failed to save output to file '{output_file}': {e}")

            if isinstance(returncode, int) and returncode != 0:
                print(f"[returncode={returncode}]")

    except KeyboardInterrupt:
        print("\nExecute command stopped")

    finally:
        status, response = close_shell(target_id, session_id)

        if status == 404 and "session not found" in response.lower():
            print("Remote session was already closed")
        elif status != 200:
            print(f"[{status}] {response}")


def run_client() -> None:
    target_id = input("Target server node id (for example pc2): ").strip() or "pc2"

    try:
        status, body = ping_server(target_id)
    except Exception as e:
        print(f"Ping failed: {e}")
        return

    print("CLIENT: sent REQUEST GET: Hello, Server")
    print(f"CLIENT: GET: {body.strip()} (status={status})")