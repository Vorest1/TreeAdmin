from __future__ import annotations

import json
from pathlib import Path
from typing import Any


API_DIR = Path("api")
SERVER_CONFIG_PATH = API_DIR / "config.json"
CLIENT_CONFIG_PATH = API_DIR / "config_client.json"


class BaseConfig:
    DEFAULT_PATH: Path | None = None

    def __init__(self, data: dict[str, Any], path: str | Path | None = None) -> None:
        if not isinstance(data, dict):
            raise ValueError("Config must be JSON object")

        self.data = data
        self.path = Path(path) if path is not None else self.DEFAULT_PATH

    @staticmethod
    def _load_json_object(path: str | Path) -> dict[str, Any]:
        path = Path(path)

        if not path.exists():
            return {}

        raw_text = path.read_text(encoding="utf-8").strip()
        if not raw_text:
            return {}

        data = json.loads(raw_text)

        if data is None:
            return {}

        if not isinstance(data, dict):
            raise ValueError(f"Config root must be JSON object: {path}")

        return data

    @staticmethod
    def _save_json_object(data: dict[str, Any], path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path | None = None) -> BaseConfig:
        final_path = Path(path) if path is not None else cls.DEFAULT_PATH
        if final_path is None:
            raise ValueError("Path is not specified")

        data = cls._load_json_object(final_path)
        if not data:
            raise ValueError(f"Config is empty: {final_path}")

        obj = cls(data, final_path)
        obj.validate()
        return obj

    def save(self, path: str | Path | None = None) -> None:
        final_path = Path(path) if path is not None else self.path
        if final_path is None:
            raise ValueError("Path is not specified")

        self.validate()
        self._save_json_object(self.data, final_path)
        self.path = final_path

    def validate(self) -> None:
        raise NotImplementedError

    @property
    def role(self) -> str:
        return self.data["self"]["role"]

    @property
    def node_id(self) -> str:
        return self.data["self"]["node_id"]

    @property
    def listen_host(self) -> str:
        return self.data["network"]["listen_host"]

    @property
    def listen_port(self) -> int:
        return int(self.data["network"]["listen_port"])

    @property
    def timeout(self) -> int:
        return int(self.data["network"]["timeout"])


class ServerConfig(BaseConfig):
    DEFAULT_PATH = SERVER_CONFIG_PATH

    @classmethod
    def build(
        cls,
        *,
        node_id: str,
        listen_host: str,
        listen_port: int,
        timeout: int = 10,
        parent_id: str | None = None,
        path: str | Path | None = None,
    ) -> ServerConfig:
        data = {
            "self": {
                "node_id": node_id,
                "role": "server",
            },
            "network": {
                "listen_host": listen_host,
                "listen_port": listen_port,
                "timeout": timeout,
            },
            "routing": {
                "parent_id": parent_id,
            },
        }
        obj = cls(data, path)
        obj.validate()
        return obj

    @classmethod
    def build_basic(cls) -> ServerConfig:
        return cls.build(
            node_id="pc2",
            listen_host="127.0.0.1",
            listen_port=8000,
            timeout=10,
            parent_id=None,
        )

    def validate(self) -> None:
        config = self.data

        self_block = config.get("self")
        if not isinstance(self_block, dict):
            raise ValueError("Missing object 'self'")

        node_id = self_block.get("node_id")
        role = self_block.get("role")

        if not isinstance(node_id, str) or not node_id.strip():
            raise ValueError("self.node_id must be non-empty string")

        if role != "server":
            raise ValueError("self.role must be 'server'")

        network = config.get("network")
        if not isinstance(network, dict):
            raise ValueError("Missing object 'network'")

        listen_host = network.get("listen_host")
        listen_port = network.get("listen_port")
        timeout = network.get("timeout")

        if not isinstance(listen_host, str) or not listen_host.strip():
            raise ValueError("network.listen_host must be non-empty string")

        if not isinstance(listen_port, int) or listen_port <= 0:
            raise ValueError("network.listen_port must be positive integer")

        if not isinstance(timeout, int) or timeout <= 0:
            raise ValueError("network.timeout must be positive integer")

        routing = config.get("routing")
        if not isinstance(routing, dict):
            raise ValueError("Server config must contain object 'routing'")

        parent_id = routing.get("parent_id")
        if parent_id is not None and not isinstance(parent_id, str):
            raise ValueError("routing.parent_id must be string or null")

    @property
    def parent_id(self) -> str | None:
        return self.data.get("routing", {}).get("parent_id")


class ClientConfig(BaseConfig):
    DEFAULT_PATH = CLIENT_CONFIG_PATH

    @classmethod
    def build(
        cls,
        *,
        node_id: str,
        listen_host: str,
        listen_port: int,
        nodes: list[dict[str, Any]],
        timeout: int = 10,
        root_id: str | None = None,
        path: str | Path | None = None,
    ) -> ClientConfig:
        if root_id is None:
            root_id = node_id

        data = {
            "self": {
                "node_id": node_id,
                "role": "client",
            },
            "network": {
                "listen_host": listen_host,
                "listen_port": listen_port,
                "timeout": timeout,
            },
            "topology": {
                "root_id": root_id,
                "nodes": nodes,
            },
        }
        obj = cls(data, path)
        obj.validate()
        return obj

    @classmethod
    def build_basic(cls) -> ClientConfig:
        return cls.build(
            node_id="pc1",
            listen_host="127.0.0.1",
            listen_port=8001,
            timeout=10,
            nodes=[
                {
                    "id": "pc1",
                    "role": "client",
                    "children": ["pc2"],
                },
                {
                    "id": "pc2",
                    "role": "server",
                    "host": "127.0.0.1",
                    "port": 8000,
                    "children": [],
                },
            ],
        )

    def validate(self) -> None:
        config = self.data

        self_block = config.get("self")
        if not isinstance(self_block, dict):
            raise ValueError("Missing object 'self'")

        node_id = self_block.get("node_id")
        role = self_block.get("role")

        if not isinstance(node_id, str) or not node_id.strip():
            raise ValueError("self.node_id must be non-empty string")

        if role != "client":
            raise ValueError("self.role must be 'client'")

        network = config.get("network")
        if not isinstance(network, dict):
            raise ValueError("Missing object 'network'")

        listen_host = network.get("listen_host")
        listen_port = network.get("listen_port")
        timeout = network.get("timeout")

        if not isinstance(listen_host, str) or not listen_host.strip():
            raise ValueError("network.listen_host must be non-empty string")

        if not isinstance(listen_port, int) or listen_port <= 0:
            raise ValueError("network.listen_port must be positive integer")

        if not isinstance(timeout, int) or timeout <= 0:
            raise ValueError("network.timeout must be positive integer")

        topology = config.get("topology")
        if not isinstance(topology, dict):
            raise ValueError("Missing object 'topology'")

        root_id = topology.get("root_id")
        nodes = topology.get("nodes")

        if not isinstance(root_id, str) or not root_id.strip():
            raise ValueError("topology.root_id must be non-empty string")

        if not isinstance(nodes, list) or not nodes:
            raise ValueError("topology.nodes must be non-empty list")

        ids: set[str] = set()

        for node in nodes:
            if not isinstance(node, dict):
                raise ValueError("Each topology node must be object")

            current_id = node.get("id")
            current_role = node.get("role")
            children = node.get("children", [])

            if not isinstance(current_id, str) or not current_id.strip():
                raise ValueError("Each node must have non-empty 'id'")

            if current_id in ids:
                raise ValueError(f"Duplicate node id: {current_id}")
            ids.add(current_id)

            if current_role not in {"client", "server"}:
                raise ValueError(f"Invalid role for node '{current_id}'")

            if not isinstance(children, list):
                raise ValueError(f"children of node '{current_id}' must be list")

            for child_id in children:
                if not isinstance(child_id, str) or not child_id.strip():
                    raise ValueError(f"Invalid child id in node '{current_id}'")

            if current_role == "server":
                host = node.get("host")
                port = node.get("port")

                if not isinstance(host, str) or not host.strip():
                    raise ValueError(f"Server node '{current_id}' must have non-empty host")

                if not isinstance(port, int) or port <= 0:
                    raise ValueError(f"Server node '{current_id}' must have positive port")

        if root_id not in ids:
            raise ValueError(f"topology.root_id '{root_id}' not found in topology.nodes")

        for node in nodes:
            current_id = node["id"]
            for child_id in node.get("children", []):
                if child_id not in ids:
                    raise ValueError(f"Node '{current_id}' references unknown child '{child_id}'")

        self_node = None
        for node in nodes:
            if node["id"] == node_id:
                self_node = node
                break

        if self_node is None:
            raise ValueError(f"Client node '{node_id}' not found in topology")

        if self_node["role"] != "client":
            raise ValueError(f"Topology node '{node_id}' must have role 'client'")

        if root_id != node_id:
            raise ValueError(
                f"topology.root_id '{root_id}' must match local client node_id '{node_id}'"
            )

    @property
    def topology(self) -> dict[str, Any]:
        return self.data["topology"]

    @property
    def root_id(self) -> str:
        return self.data["topology"]["root_id"]

    @property
    def nodes(self) -> list[dict[str, Any]]:
        return self.data["topology"]["nodes"]