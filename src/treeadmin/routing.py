from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode


RouteHop = dict[str, Any]


def _get_root_id(config: Any) -> str:
    if isinstance(config, dict):
        root_id = config.get("root_id")
        if isinstance(root_id, str) and root_id.strip():
            return root_id

    root_id = getattr(config, "root_id", None)
    if isinstance(root_id, str) and root_id.strip():
        return root_id

    data = getattr(config, "data", None)
    if isinstance(data, dict):
        root_id = data.get("root_id")
        if isinstance(root_id, str) and root_id.strip():
            return root_id

    raise ValueError("Client config must contain non-empty root_id")


def _get_nodes(config: Any) -> dict[str, dict[str, Any]]:
    if isinstance(config, dict):
        nodes = config.get("nodes")
        if isinstance(nodes, dict) and nodes:
            return nodes

    nodes = getattr(config, "nodes", None)
    if isinstance(nodes, dict) and nodes:
        return nodes

    data = getattr(config, "data", None)
    if isinstance(data, dict):
        nodes = data.get("nodes")
        if isinstance(nodes, dict) and nodes:
            return nodes

    raise ValueError("Client config must contain non-empty nodes")


def _get_default_port(config: Any) -> int | None:
    if isinstance(config, dict):
        port = config.get("default_port")
        if port is None:
            return None
        if isinstance(port, int) and port > 0:
            return port
        raise ValueError("default_port must be positive integer")

    port = getattr(config, "default_port", None)
    if port is not None:
        if isinstance(port, int) and port > 0:
            return port
        raise ValueError("default_port must be positive integer")

    data = getattr(config, "data", None)
    if isinstance(data, dict):
        port = data.get("default_port")
        if port is None:
            return None
        if isinstance(port, int) and port > 0:
            return port
        raise ValueError("default_port must be positive integer")

    return None


def _find_path_dfs(
    nodes: dict[str, dict[str, Any]],
    current_id: str,
    target_id: str,
    visited: set[str] | None = None,
) -> list[str] | None:
    if visited is None:
        visited = set()

    if current_id in visited:
        return None

    if current_id not in nodes:
        raise ValueError(f"Unknown node in path search: '{current_id}'")

    visited.add(current_id)

    if current_id == target_id:
        return [current_id]

    current_node = nodes[current_id]
    children = current_node.get("children", [])

    if children is None:
        children = []

    if not isinstance(children, list):
        raise ValueError(f"children of node '{current_id}' must be list")

    for child_id in children:
        if not isinstance(child_id, str) or not child_id.strip():
            raise ValueError(f"Invalid child id in node '{current_id}'")

        path = _find_path_dfs(nodes, child_id, target_id, visited.copy())
        if path is not None:
            return [current_id] + path

    return None


def build_route_hops(client_config: Any, target_node_id: str) -> list[RouteHop]:
    if not isinstance(target_node_id, str) or not target_node_id.strip():
        raise ValueError("target_node_id must be non-empty string")

    root_id = _get_root_id(client_config)
    nodes = _get_nodes(client_config)
    default_port = _get_default_port(client_config)

    if root_id not in nodes:
        raise ValueError(f"root_id '{root_id}' not found in nodes")

    if target_node_id not in nodes:
        raise ValueError(f"Target node '{target_node_id}' not found in nodes")

    path_ids = _find_path_dfs(nodes, root_id, target_node_id)
    if path_ids is None:
        raise ValueError(f"No route from '{root_id}' to '{target_node_id}'")

    if len(path_ids) < 2:
        raise ValueError("Route must contain at least one hop after root")

    hops: list[RouteHop] = []

    for node_id in path_ids[1:]:
        node = nodes[node_id]
        host = node.get("host")
        port = node.get("port", default_port)

        if not isinstance(host, str) or not host.strip():
            raise ValueError(f"Node '{node_id}' must have non-empty host")

        if not isinstance(port, int) or port <= 0:
            raise ValueError(
                f"Node '{node_id}' must have positive port "
                f"(explicitly or via default_port)"
            )

        hops.append({
            "id": node_id,
            "host": host,
            "port": port,
        })

    if not hops:
        raise ValueError("Empty route hops")

    return hops


def encode_route(hops: list[RouteHop]) -> str:
    if not isinstance(hops, list) or not hops:
        raise ValueError("Route hops must be non-empty list")

    normalized: list[RouteHop] = []

    for hop in hops:
        if not isinstance(hop, dict):
            raise ValueError("Each route hop must be object")

        host = hop.get("host")
        port = hop.get("port")
        node_id = hop.get("id")

        if not isinstance(host, str) or not host.strip():
            raise ValueError("Route hop must have non-empty host")

        if not isinstance(port, int) or port <= 0:
            raise ValueError("Route hop must have positive port")

        item: RouteHop = {
            "host": host,
            "port": port,
        }

        if isinstance(node_id, str) and node_id.strip():
            item["id"] = node_id

        normalized.append(item)

    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def decode_route(raw: str) -> list[RouteHop]:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("route query parameter is empty")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid route JSON: {e}") from e

    if not isinstance(data, list) or not data:
        raise ValueError("Decoded route must be non-empty list")

    hops: list[RouteHop] = []

    for item in data:
        if not isinstance(item, dict):
            raise ValueError("Each route hop must be object")

        host = item.get("host")
        port = item.get("port")
        node_id = item.get("id")

        if not isinstance(host, str) or not host.strip():
            raise ValueError("Route hop must have non-empty host")

        if not isinstance(port, int) or port <= 0:
            raise ValueError("Route hop must have positive port")

        hop: RouteHop = {
            "host": host,
            "port": port,
        }

        if isinstance(node_id, str) and node_id.strip():
            hop["id"] = node_id

        hops.append(hop)

    return hops


def build_routed_path(
    endpoint_path: str,
    hops: list[RouteHop],
    hop_index: int = 0,
    extra_query: dict[str, Any] | None = None,
) -> str:
    if not isinstance(endpoint_path, str) or not endpoint_path.startswith("/"):
        raise ValueError("endpoint_path must start with '/'")

    if not isinstance(hop_index, int) or hop_index < 0:
        raise ValueError("hop_index must be non-negative integer")

    if hop_index >= len(hops):
        raise ValueError("hop_index is out of route range")

    query: dict[str, Any] = {}
    if extra_query:
        query.update(extra_query)

    query["route"] = encode_route(hops)
    query["hop"] = hop_index

    qs = urlencode(query)
    return f"{endpoint_path}?{qs}"


def build_first_request(
    client_config: Any,
    target_node_id: str,
    endpoint_path: str,
    extra_query: dict[str, Any] | None = None,
) -> tuple[str, int, str]:
    hops = build_route_hops(client_config, target_node_id)

    first_hop = hops[0]
    host = first_hop["host"]
    port = int(first_hop["port"])
    path = build_routed_path(
        endpoint_path=endpoint_path,
        hops=hops,
        hop_index=0,
        extra_query=extra_query,
    )
    return host, port, path


def parse_route_params(query_params: dict[str, list[str]]) -> tuple[list[RouteHop], int]:
    route_raw = query_params.get("route", [""])
    hop_raw = query_params.get("hop", [""])

    route_value = route_raw[0] if route_raw else ""
    hop_value = hop_raw[0] if hop_raw else ""

    hops = decode_route(route_value)

    if not hop_value:
        raise ValueError("Missing query parameter 'hop'")

    try:
        hop_index = int(hop_value)
    except ValueError as e:
        raise ValueError(f"Invalid hop value: {hop_value}") from e

    if hop_index < 0 or hop_index >= len(hops):
        raise ValueError("hop is out of route range")

    return hops, hop_index


def get_current_hop(hops: list[RouteHop], hop_index: int) -> RouteHop:
    if hop_index < 0 or hop_index >= len(hops):
        raise ValueError("hop_index is out of route range")
    return hops[hop_index]


def get_next_hop(hops: list[RouteHop], hop_index: int) -> RouteHop | None:
    next_index = hop_index + 1
    if next_index >= len(hops):
        return None
    return hops[next_index]


def is_final_hop(hops: list[RouteHop], hop_index: int) -> bool:
    return hop_index == len(hops) - 1


def build_forward_request(
    endpoint_path: str,
    hops: list[RouteHop],
    current_hop_index: int,
    extra_query: dict[str, Any] | None = None,
) -> tuple[str, int, str]:
    next_hop = get_next_hop(hops, current_hop_index)
    if next_hop is None:
        raise ValueError("Current hop is already final")

    host = next_hop["host"]
    port = int(next_hop["port"])
    path = build_routed_path(
        endpoint_path=endpoint_path,
        hops=hops,
        hop_index=current_hop_index + 1,
        extra_query=extra_query,
    )
    return host, port, path


def format_route(hops: list[RouteHop]) -> str:
    parts: list[str] = []

    for index, hop in enumerate(hops):
        host = hop["host"]
        port = hop["port"]
        node_id = hop.get("id")

        if isinstance(node_id, str) and node_id.strip():
            parts.append(f"{node_id}({host}:{port})")
        else:
            parts.append(f"hop{index}({host}:{port})")

    return " -> ".join(parts)