import http.client
import json, socket
from pathlib import Path
from urllib.parse import urlencode

from src.treeadmin.routing import build_first_request
from src.treeadmin.config import ClientConfig

CONFIG_PATH = Path("api/config_client.json")

def send_config(target_id: str):
    file_path = Path("api/config.json")

    if not file_path.exists():
        print("Config file not found")
        return
    
    config = json.loads(file_path.read_text(encoding="utf-8"))

    body = json.dumps(config, ensure_ascii=False).encode("utf-8")
    # qs = urlencode({"to": to})
    # path = f"/send_config?{qs}"
    client_config = ClientConfig.load(CONFIG_PATH)
    host, port, url_path = build_first_request(
        client_config=client_config,
        target_node_id=target_id,
        endpoint_path="/send_config",
    )

    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request(
            "POST",
            url_path,
            body=body,
            headers={"Content-Type": "application/json; charset=utf-8", 
                    "Content-Length": str(len(body))
            }
        )

        resp = conn.getresponse()
        print(resp.status, resp.read().decode("utf-8", errors="replace"))
    finally:
        conn.close()

def open_shell(target_id: str) -> tuple[str, str] | tuple[None, None]:
    client_config = ClientConfig.load(CONFIG_PATH)
    host, port, url_path = build_first_request(
        client_config=client_config,
        target_node_id=target_id,
        endpoint_path="/open_shell",
    )

    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request("POST", url_path, body=b"", headers={"Content-Length": "0"})
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")

        if resp.status != 200:
            print(f"{resp.status} {raw}")
            return None, None

        data = json.loads(raw)
        return data["session_id"], data["cwd"]
    finally:
        conn.close()

def send_exec(
    target_id: str,
    session_id: str,
    command: str
) -> tuple[int, dict]:
    client_config = ClientConfig.load(CONFIG_PATH)
    host, port, url_path = build_first_request(
        client_config=client_config,
        target_node_id=target_id,
        endpoint_path="/send_command",
    )

    payload = {
        "session_id": session_id,
        "command": command
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    conn = http.client.HTTPConnection(host, port, timeout=15)
    try:
        conn.request(
            "POST",
            url_path,
            body=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": str(len(body))
            }
        )

        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")

        if resp.status != 200:
            return resp.status, {
                "output": raw,
                "cwd": ""
            }

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return 502, {
                "output": f"invalid JSON from server: {raw}",
                "cwd": ""
            }

        data.setdefault("output", "")
        data.setdefault("cwd", "")
        return resp.status, data

    except socket.timeout:
        return 408, {
            "output": "request timeout: server did not respond in time",
            "cwd": ""
        }

    except ConnectionError as e:
        return 503, {
            "output": f"connection error: {e}",
            "cwd": ""
        }

    except Exception as e:
        return 500, {
            "output": f"client error while sending command: {e}",
            "cwd": ""
        }

    finally:
        conn.close()


def close_shell(target_id: str, session_id: str) -> tuple[int, str]:
    client_config = ClientConfig.load(CONFIG_PATH)
    host, port, url_path = build_first_request(
        client_config=client_config,
        target_node_id=target_id,
        endpoint_path="/close_shell",
    )

    payload = {"session_id": session_id}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request(
            "POST",
            url_path,
            body=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": str(len(body))
            }
        )
        resp = conn.getresponse()
        data = resp.read().decode("utf-8", errors="replace")
        return resp.status, data
    finally:
        conn.close()

def clean_output(raw: str) -> str:
    return raw.replace("__PROMPT__", "").strip()

def interactive_shell(target_id: str):
    session_id, current_dir = open_shell(target_id)
    if not session_id:
        print("Failed to open shell session")
        return

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
            current_dir = result.get("cwd", current_dir).strip()

            if output and output != "[empty output]":
                print(output)

    except KeyboardInterrupt:
        print("\nExecute command stopped")

    finally:
        status, response = close_shell(target_id, session_id)
        if status != 200:
            print(f"[{status}] {response}")

def run_client() -> None:
    msg = "Hello, Server"
    target_id = input("Target server node id (for example pc2): ").strip() or "pc2"

    client_config = ClientConfig.load(CONFIG_PATH)
    host, port, url_path = build_first_request(
        client_config=client_config,
        target_node_id=target_id,
        endpoint_path="/hello",
        extra_query={"msg": msg},
    )

    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request("GET", url_path)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace").strip()

        print(f"CLIENT: sent REQUEST GET: {msg}")
        print(f"CLIENT: GET: {body} (status={resp.status})")
    finally:
        conn.close()