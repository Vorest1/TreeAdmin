from __future__ import annotations

from typing import Any

from src.treeadmin.client import (
    interactive_shell,
    list_sessions,
    ping_server,
)
from src.treeadmin.config import ClientConfig
from src.treeadmin.serv import run_server


def _print_topology(config: ClientConfig) -> None:
    print("\nCurrent topology:")
    print(f"root_id: {config.root_id}")
    print(f"timeout: {config.timeout}")

    default_port = config.data.get("default_port")
    if default_port is not None:
        print(f"default_port: {default_port}")

    print("nodes:")
    for node_id, node in config.nodes.items():
        host = node.get("host", "-")
        port = node.get("port", config.data.get("default_port", "-"))
        children = node.get("children", [])
        print(f"  {node_id}: host={host}, port={port}, children={children}")


def _reset_client_config() -> None:
    config = ClientConfig.build_basic()
    config.save()
    print(f"Client config reset: {config.path}")


def _ping_menu() -> None:
    target_id = _choose_server()
    if not target_id:
        return

    status, body = ping_server(target_id)
    print("CLIENT: sent REQUEST GET: Hello, Server")
    print(f"CLIENT: GET: {body.strip()} (status={status})")


def _normalize_sessions(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []

    if isinstance(raw, dict):
        if isinstance(raw.get("sessions"), list):
            raw = raw["sessions"]
        else:
            items: list[dict[str, Any]] = []
            for key, value in raw.items():
                if isinstance(value, dict):
                    item = dict(value)
                    item.setdefault("session_id", str(key))
                    items.append(item)
                else:
                    items.append({"session_id": str(key)})
            return items

    if not isinstance(raw, list):
        return []

    result: list[dict[str, Any]] = []

    for item in raw:
        if isinstance(item, dict):
            normalized = dict(item)
            if "session_id" in normalized:
                normalized["session_id"] = str(normalized["session_id"])
            result.append(normalized)
        elif isinstance(item, (int, str)):
            result.append({"session_id": str(item)})
        else:
            result.append({"session_id": str(item)})

    return result


def _choose_server() -> str | None:
    try:
        config = ClientConfig.load()
    except Exception as e:
        print(f"Failed to load client config: {e}")
        return None

    server_candidates: list[str] = []
    for node_id, node in config.nodes.items():
        host = node.get("host")
        port = node.get("port")
        if isinstance(host, str) and host.strip() and isinstance(port, int) and port > 0:
            server_candidates.append(node_id)

    if not server_candidates:
        print("No servers found in client config")
        return None

    if len(server_candidates) == 1:
        target_id = server_candidates[0]
        print(f"Connecting to only configured server: {target_id}")
        return target_id

    print("\nChoose server:")
    for index, node_id in enumerate(server_candidates, start=1):
        print(f"{index}) {node_id}")
    print("0) Back")

    raw = input("> ").strip()
    if raw == "0":
        return None

    try:
        index = int(raw)
    except ValueError:
        print("Invalid menu item")
        return None

    if index < 1 or index > len(server_candidates):
        print("Invalid menu item")
        return None

    return server_candidates[index - 1]


def _print_sessions_brief(sessions: list[dict[str, Any]]) -> None:
    if not sessions:
        print("No active sessions on server")
        return

    print("\nActive sessions:")
    for index, item in enumerate(sessions, start=1):
        session_id = str(item.get("session_id", ""))
        cwd = str(item.get("cwd", ""))
        platform_name = str(item.get("platform", ""))
        print(f"{index}) session {session_id} | platform={platform_name} | cwd={cwd}")


def _connect_to_server_menu() -> None:
    target_id = _choose_server()
    if not target_id:
        return

    raw_sessions = list_sessions(target_id)
    sessions = _normalize_sessions(raw_sessions)

    if not sessions:
        print(f"\nNo active session on {target_id}.")
        print("Opening new session...")
        interactive_shell(target_id)
        return

    if len(sessions) == 1:
        session = sessions[0]
        session_id = str(session.get("session_id", ""))
        cwd = str(session.get("cwd", ""))
        print(f"\nFound active session {session_id} on {target_id}.")
        print("Connecting...")
        interactive_shell(
            target_id,
            existing_session_id=session_id,
            existing_cwd=cwd,
        )
        return

    print(f"\nServer {target_id} has multiple sessions:")
    print(f"1) Continue latest session [{sessions[0].get('session_id')}]")
    print("2) Choose session manually")
    print("3) Open new session")
    print("0) Back")

    choice = input("> ").strip()

    if choice == "0":
        return

    if choice == "1":
        session = sessions[0]
        interactive_shell(
            target_id,
            existing_session_id=str(session.get("session_id", "")),
            existing_cwd=str(session.get("cwd", "")),
        )
        return

    if choice == "2":
        _print_sessions_brief(sessions)
        print("0) Back")

        raw = input("Session number: ").strip()
        if raw == "0":
            return

        try:
            index = int(raw)
        except ValueError:
            print("Invalid menu item")
            return

        if index < 1 or index > len(sessions):
            print("Invalid menu item")
            return

        session = sessions[index - 1]
        interactive_shell(
            target_id,
            existing_session_id=str(session.get("session_id", "")),
            existing_cwd=str(session.get("cwd", "")),
        )
        return

    if choice == "3":
        interactive_shell(target_id)
        return

    print("Unknown menu item")


def _show_topology_menu() -> None:
    try:
        config = ClientConfig.load()
    except Exception as e:
        print(f"Failed to load client config: {e}")
        return

    _print_topology(config)


def _client_menu() -> None:
    try:
        while True:
            print("\nClient menu:")
            print("1) Connect to server")
            print("2) Ping server")
            print("3) Show topology")
            print("4) Reset config")
            print("0) Back")

            choice = input("> ").strip()

            if choice == "1":
                _connect_to_server_menu()
            elif choice == "2":
                _ping_menu()
            elif choice == "3":
                _show_topology_menu()
            elif choice == "4":
                _reset_client_config()
            elif choice == "0":
                break
            else:
                print("Unknown menu item")
    except KeyboardInterrupt:
        print("Close programm!\n")


def main() -> None:
    while True:
        print("\nSelect mode:")
        print("1) Server")
        print("2) Client")
        print("0) Exit")

        choice = input("> ").strip()

        try:
            if choice == "1":
                run_server()
                return
            if choice == "2":
                _client_menu()
                return
            if choice == "0":
                print("Exit")
                return

            print("Unknown menu item")
        except KeyboardInterrupt:
            print("Close programm!\n")
            return