import http.client
import json, socket
from pathlib import Path
from urllib.parse import urlencode

def send_config(host="127.0.0.1", port=8000, to=8000):
    
    file_path = Path("api/config.json")

    if not file_path.exists():
        print("Config file not found")
        return
    
    config = json.loads(file_path.read_text(encoding="utf-8"))

    body = json.dumps(config, ensure_ascii=False).encode("utf-8")
    qs = urlencode({"to": to})
    path = f"/send_config?{qs}"

    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.request(
        "POST",
        path,
        body=body,
        headers={"Content-Type": "application/json; charset=utf-8", 
                 "Content-Length": str(len(body))
        }
    )

    resp = conn.getresponse()
    print(resp.status, resp.read().decode("utf-8", errors="replace"))
    conn.close()

def open_shell(host: str = "127.0.0.1", port: int = 8000, to: int = 8000) -> tuple[str, str] | tuple[None, None]:
    qs = urlencode({"to": to})
    path = f"/open_shell?{qs}"

    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request("POST", path, body=b"", headers={"Content-Length": "0"})
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
    host: str,
    port: int,
    to: int,
    session_id: str,
    command: str
) -> tuple[int, dict]:
    qs = urlencode({"to": to})
    path = f"/send_command?{qs}"

    payload = {
        "session_id": session_id,
        "command": command
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    conn = http.client.HTTPConnection(host, port, timeout=15)
    try:
        conn.request(
            "POST",
            path,
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


def close_shell(host: str, port: int, to: int, session_id: str) -> tuple[int, str]:
    qs = urlencode({"to": to})
    path = f"/close_shell?{qs}"

    payload = {"session_id": session_id}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request(
            "POST",
            path,
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

def interactive_shell(host: str = "127.0.0.1", port: int = 8000, to: int = 8000):
    session_id, current_dir = open_shell(host, port, to)
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

            status, result = send_exec(host, port, to, session_id, cmd)

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
        status, response = close_shell(host, port, to, session_id)
        if status != 200:
            print(f"[{status}] {response}")

def run_client(host: str = "127.0.0.1", port: int = 8000) -> None:
    msg = "Hello, Server"
    to_port = int(input("Finally Server port (default 8000): ").strip() or "8000")

    qs = urlencode({"to": to_port,"msg": msg})
    path = f"/hello?{qs}"

    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.request("GET", path)

    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="replace").strip()
    conn.close()

    print(f"CLIENT: sent REQUEST GET: {msg}")
    print(f"CLIENT: GET: {body} (status={resp.status})")