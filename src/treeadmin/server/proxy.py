from __future__ import annotations

import http.client
from urllib.parse import parse_qs

from src.treeadmin.routing import (
    get_current_hop,
    parse_route_params,
)


class ProxySupport:
    def extract_extra_query(self, qs: dict[str, list[str]]) -> dict[str, str]:
        extra: dict[str, str] = {}

        for key, values in qs.items():
            if key in {"route", "hop"}:
                continue
            extra[key] = values[0] if values else ""

        return extra

    def resolve_route(self, parsed):
        qs = parse_qs(parsed.query, keep_blank_values=True)
        hops, hop_index = parse_route_params(qs)

        if not hops:
            raise ValueError("empty route")

        get_current_hop(hops, hop_index)
        return qs, hops, hop_index

    def forward(self, handler, host: str, port: int, method: str, path: str, body: bytes) -> None:
        conn = http.client.HTTPConnection(host, port, timeout=60)

        try:
            headers: dict[str, str] = {}

            content_type = handler.headers.get("Content-Type")
            if content_type:
                headers["Content-Type"] = content_type

            headers["Content-Length"] = str(len(body)) if body else "0"

            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            response_content_type = resp.getheader("Content-Type", "text/plain; charset=utf-8")

            resp_body = resp.read()
            handler.send_response(resp.status)
            handler.send_header("Content-Type", response_content_type)
            handler.send_header("Content-Length", str(len(resp_body)))
            handler.end_headers()
            handler.wfile.write(resp_body)

        except Exception as e:
            handler._send_text(502, f"proxy error: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass