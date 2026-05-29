import http.client
import logging
from typing import Dict, List, Tuple, Any
from urllib.parse import parse_qs

from src.treeadmin.routing import (
    get_current_hop,
    parse_route_params,
)

logger = logging.getLogger(__name__)


class ProxySupport:
    def extract_extra_query(self, qs):
        # type: (Dict[str, List[str]]) -> Dict[str, str]
        extra = {}  # type: Dict[str, str]

        for key, values in qs.items():
            if key in {"route", "hop"}:
                continue
            extra[key] = values[0] if values else ""

        # log
        if extra:
            logger.debug(
                "extract_extra_query keys=%s",
                sorted(extra.keys()),
            )
        #

        return extra

    def resolve_route(self, parsed):
        # type: (Any) -> Tuple[Dict[str, List[str]], List[Dict[str, Any]], int]
        try:
            qs = parse_qs(parsed.query, keep_blank_values=True)
            hops, hop_index = parse_route_params(qs)

            if not hops:
                raise ValueError("empty route")

            cur_hop = get_current_hop(hops, hop_index)

            # log
            logger.debug(
                    "resolve_route path=%s hop_index=%s hops_count=%s current_host=%s current_port=%s",
                    parsed.path,
                    hop_index,
                    len(hops),
                    cur_hop.get("host"),
                    cur_hop.get("port"),
                )
            #

            return qs, hops, hop_index
        # log
        except ValueError:
            attr = getattr(parsed, "path", "")
            text = str(attr)
            if len(text) > 500:
                text = text[: 500 - 3] + "..."

            logger.warning(
                "proxy_route_resolve_failed path=%s query=%s",
                attr,
                text,
                exc_info=True,
            )
            raise
        #

    def forward(self, handler, host, port, method, path, body):
        # type: (Any, str, int, str, str, bytes) -> None
        conn = http.client.HTTPConnection(host, port, timeout=60)
        
        #log
        logger.info(
            "proxy is open: method=%s, host:port=%s:%s",
            method,
            host,
            port
            )
        #

        try:
            headers = {}  # type: Dict[str, str]

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
            logger.error("proxy error: %s", e)
            handler._send_text(502, "proxy error: {}".format(e))
        finally:
            try:
                conn.close()
                
                # log
                logger.info(
                    "proxy is close: host:port=%s:%s",
                    host,
                    port
                )
                #
            except Exception:
                #pass
                # log
                logger.debug(
                    "proxy close failed host=%s port=%s",
                    host,
                    port,
                    exc_info=True,
                )
                #