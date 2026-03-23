from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode


RouteHop = dict[str, Any]


def _config_to_dict(config: Any) -> dict[str, Any]:
    if isinstance(config, dict):
        return config

    data = getattr(config, "data", None)
    if isinstance(data, dict):
        return data

    raise ValueError("Config must be dict or object with .data dict")


def _get_topology(config: Any) -> dict[str, Any]:
    if isinstance(config, dict):
        topology = config.get("topology")
        if not isinstance(topology, dict):
            raise ValueError("Client config must contain topology")
        return topology

    topology = getattr(config, "topology", None)
    if isinstance(topology, dict):
        return topology

    data = getattr(config, "data", None)
    if isinstance(data, dict):
        topology = data.get("topology")
        if isinstance(topology, dict):
            return topology

    raise ValueError("Client config must contain topology")


def _get_self_node_id(config: Any) -> str:
    if isinstance(config, dict):
        self_block = config.get("self")
        if not isinstance(self_block, dict):
            raise ValueError("Config must contain self block")

        node_id = self_block.get("node_id")
        if not isinstance(node_id, str) or not node_id.strip():
            raise ValueError("self.node_id must be non-empty string")
        return node_id

    node_id = getattr(config, "node_id", None)
    if isinstance(node_id, str) and node_id.strip():
        return node_id

    data = getattr(config, "data", None)
    if isinstance(data, dict):
        self_block = data.get("self")
        if isinstance(self_block, dict):
            node_id = self_block.get("node_id")
            if isinstance(node_id, str) and node_id.strip():
                return node_id

    raise ValueError("self.node_id must be non-empty string")


def _build_node_index(topology: dict[str, Any]) -> dict[str, dict[str, Any]]:
    nodes = topology.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("topology.nodes must be non-empty list")

    index: dict[str, dict[str, Any]] = {}

    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("Each topology node must be object")

        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id.strip():
            raise ValueError("Each node must have non-empty id")

        if node_id in index:
            raise ValueError(f"Duplicate node id: {node_id}")

        index[node_id] = node

    return index


def _find_path_dfs(
    node_index: dict[str, dict[str, Any]],
    current_id: str,
    target_id: str,
    visited: set[str] | None = None,
) -> list[str] | None:
    if visited is None:
        visited = set()

    if current_id in visited:
        return None

    visited.add(current_id)

    if current_id == target_id:
        return [current_id]

    current_node = node_index.get(current_id)
    if current_node is None:
        return None

    for child_id in current_node.get("children", []):
        path = _find_path_dfs(node_index, child_id, target_id, visited.copy())
        if path is not None:
            return [current_id] + path

    return None


def build_route_hops(client_config: Any, target_node_id: str) -> list[RouteHop]:
    topology = _get_topology(client_config)

    root_id = topology.get("root_id")
    if not isinstance(root_id, str) or not root_id.strip():
        raise ValueError("topology.root_id must be non-empty string")

    node_index = _build_node_index(topology)

    if target_node_id not in node_index:
        raise ValueError(f"Target node '{target_node_id}' not found in topology")

    path_ids = _find_path_dfs(node_index, root_id, target_node_id)
    if path_ids is None:
        raise ValueError(f"No route from '{root_id}' to '{target_node_id}'")

    if len(path_ids) < 2:
        raise ValueError("Route must contain at least one server hop after client root")

    hops: list[RouteHop] = []

    for node_id in path_ids[1:]:
        node = node_index[node_id]
        role = node.get("role")

        if role != "server":
            raise ValueError(
                f"Route contains non-server node '{node_id}' after root. "
                "Only server hops are allowed after client root."
            )

        host = node.get("host")
        port = node.get("port")

        if not isinstance(host, str) or not host.strip():
            raise ValueError(f"Server node '{node_id}' must have non-empty host")

        if not isinstance(port, int) or port <= 0:
            raise ValueError(f"Server node '{node_id}' must have positive port")

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

    return json.dumps(hops, ensure_ascii=False, separators=(",", ":"))


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

        node_id = item.get("id")
        host = item.get("host")
        port = item.get("port")

        if not isinstance(node_id, str) or not node_id.strip():
            raise ValueError("Route hop id must be non-empty string")

        if not isinstance(host, str) or not host.strip():
            raise ValueError(f"Route hop '{node_id}' must have non-empty host")

        if not isinstance(port, int) or port <= 0:
            raise ValueError(f"Route hop '{node_id}' must have positive port")

        hops.append({
            "id": node_id,
            "host": host,
            "port": port,
        })

    return hops


def build_routed_path(
    endpoint_path: str,
    hops: list[RouteHop],
    hop_index: int = 0,
    extra_query: dict[str, Any] | None = None,
) -> str:
    if not endpoint_path.startswith("/"):
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


def validate_current_node(local_config: Any, hops: list[RouteHop], hop_index: int) -> None:
    current_node_id = _get_self_node_id(local_config)

    current_hop = get_current_hop(hops, hop_index)
    expected_id = current_hop["id"]

    if current_node_id != expected_id:
        raise ValueError(
            f"Route mismatch: current node is '{current_node_id}', "
            f"but route expects '{expected_id}'"
        )


def format_route(hops: list[RouteHop]) -> str:
    parts: list[str] = []
    for hop in hops:
        parts.append(f"{hop['id']}({hop['host']}:{hop['port']})")
    return " -> ".join(parts)