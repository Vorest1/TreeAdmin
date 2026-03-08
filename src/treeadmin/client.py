import http.client
import json
from pathlib import Path
from urllib.parse import urlencode

def send_config(host="127.0.0.1", port=8000, to=8002):
    
    file_path = Path("api/config.json")

    if not file_path.exists():
        print("Config file not found")
        return
    
    config = json.loads(file_path.read_text(encoding="utf-8"))

    body = json.dumps(config, ensure_ascii=False).encode("utf-8")
    qs = urlencode({"to": to})
    path = f"/send_config?{qs}"

    conn = http.client.HTTPConnection(host, port, timeout=5)
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

def send_exec(host: str = "127.0.0.1", port: int = 8000, to: int = 8080):
    qs = urlencode({"to": to})
    path = f"/exec?{qs}"
    cmd = input("command> ").strip()
    command = {
        "command" : cmd,
        "timeout" : 10
    }
    body = json.dumps(command, ensure_ascii=False).encode("utf-8")

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


def run_client(host: str = "127.0.0.1", port: int = 8000) -> None:
    msg = "Hello, Server"
    to_port = int(input("Finally Server port (default 8000): ").strip() or "8000")

    qs = urlencode({"to": to_port,"msg": msg})
    path = f"/hello?{qs}"

    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", path)

    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="replace").strip()
    conn.close()

    print(f"CLIENT: sent REQUEST GET: {msg}")
    print(f"CLIENT: GET: {body} (status={resp.status})")