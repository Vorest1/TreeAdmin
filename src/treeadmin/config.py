from __future__ import annotations

import json
from pathlib import Path
from typing import Any


CONFIG_DIR = Path("config")
CLIENT_CONFIG_PATH = CONFIG_DIR / "config_client.json"


class ClientConfig:
    DEFAULT_PATH = CLIENT_CONFIG_PATH

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

    @staticmethod
    def _normalize_output_format(value: Any, default: str = "base64") -> str:
        normalized = str(value or default).strip().lower()

        if normalized in {"base64", "b64"}:
            return "base64"

        if normalized in {"text", "plain", "human", "readable"}:
            return "text"

        return default

    @classmethod
    def load(cls, path: str | Path | None = None) -> ClientConfig:
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

    @classmethod
    def build(
        cls,
        *,
        root_id: str,
        nodes: dict[str, dict[str, Any]],
        timeout: int = 10,
        path: str | Path | None = None,
    ) -> ClientConfig:
        data = {
            "timeout": timeout,
            "root_id": root_id,
            "nodes": nodes,
        }
        obj = cls(data, path)
        obj.validate()
        return obj

    @classmethod
    def build_basic(cls) -> ClientConfig:
        return cls.build(
            {
                "timeout": 10,
                "root_id": "pc1",
                "nodes": {
                    "pc1": {
                        "children": ["pc2"],
                    },
                    "pc2": {
                        "host": "127.0.0.1",
                        "port": 8000,
                    },
                },
                "storage": {
                    "output_format": "base64",
                },
            }
        )

    def validate(self) -> None:
        timeout = self.data.get("timeout")
        root_id = self.data.get("root_id")
        nodes = self.data.get("nodes")

        if not isinstance(timeout, int) or timeout <= 0:
            raise ValueError("timeout must be positive integer")

        if not isinstance(root_id, str) or not root_id.strip():
            raise ValueError("root_id must be non-empty string")

        if not isinstance(nodes, dict) or not nodes:
            raise ValueError("nodes must be non-empty object")

        for node_id, node in nodes.items():
            if not isinstance(node_id, str) or not node_id.strip():
                raise ValueError("Each node key must be non-empty string")

            if not isinstance(node, dict):
                raise ValueError(f"Node '{node_id}' must be object")

            children = node.get("children", [])
            if not isinstance(children, list):
                raise ValueError(f"children of node '{node_id}' must be list")

            for child_id in children:
                if not isinstance(child_id, str) or not child_id.strip():
                    raise ValueError(f"Invalid child id in node '{node_id}'")

            host = node.get("host")
            port = node.get("port")

            has_host = host is not None
            has_port = port is not None

            if has_host != has_port:
                raise ValueError(
                    f"Node '{node_id}' must define both 'host' and 'port' together"
                )

            if has_host:
                if not isinstance(host, str) or not host.strip():
                    raise ValueError(f"Node '{node_id}' must have non-empty host")

                if not isinstance(port, int) or port <= 0:
                    raise ValueError(f"Node '{node_id}' must have positive port")

        if root_id not in nodes:
            raise ValueError(f"root_id '{root_id}' not found in nodes")

        for node_id, node in nodes.items():
            for child_id in node.get("children", []):
                if child_id not in nodes:
                    raise ValueError(
                        f"Node '{node_id}' references unknown child '{child_id}'"
                    )
        
        storage = self.data.get("storage")
        if storage is not None:
            if not isinstance(storage, dict):
                raise ValueError("storage must be object")

            output_format = self._normalize_output_format(
                storage.get("output_format"),
                default="base64",
            )
            storage["output_format"] = output_format

    @property
    def timeout(self) -> int:
        return int(self.data["timeout"])

    @property
    def root_id(self) -> str:
        return self.data["root_id"]

    @property
    def nodes(self) -> dict[str, dict[str, Any]]:
        return self.data["nodes"]

    @property
    def preferred_storage_output_format(self) -> str:
        storage = self.data.get("storage", {})

        if not isinstance(storage, dict):
            return "base64"

        return self._normalize_output_format(
            storage.get("output_format"),
            default="base64",
        )