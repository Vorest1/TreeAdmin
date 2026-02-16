import http.client
from urllib.parse import urlencode


def run_client(host: str = "127.0.0.1", port: int = 8000) -> None:
    msg = "Hello, Server"

    qs = urlencode({"msg": msg})
    path = f"/hello?{qs}"

    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", path)

    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="replace").strip()
    conn.close()

    print(f"CLIENT: sent REQUEST GET: {msg}")
    print(f"CLIENT: GET: {body} (status={resp.status})")