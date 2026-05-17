from __future__ import annotations

from typing import Any

from src.treeadmin.client import (
    get_unread_notification_count,
    interactive_shell,
    list_sessions,
    ping_server,
    refresh_dashboard,
    refresh_notifications,
    restore_background_pollers_from_state,
    show_notifications,
    ui_input,
    ui_print,
)
from src.treeadmin.config import ClientConfig
from src.treeadmin.serv import run_server
from src.treeadmin.logging_config import setup_logging

def _print_topology(config: ClientConfig) -> None:
    lines: list[str] = [
        "\nCurrent topology:",
        f"root_id: {config.root_id}",
        f"timeout: {config.timeout}",
    ]

    default_port = config.data.get("default_port")
    if default_port is not None:
        lines.append(f"default_port: {default_port}")

    lines.append("nodes:")
    for node_id, node in config.nodes.items():
        host = node.get("host", "-")
        port = node.get("port", config.data.get("default_port", "-"))
        children = node.get("children", [])
        lines.append(f"  {node_id}: host={host}, port={port}, children={children}")

    ui_print("\n".join(lines))


def _reset_client_config() -> None:
    config = ClientConfig.build_basic()
    config.save()
    ui_print(f"Client config reset: {config.path}")


def _ping_menu() -> None:
    target_id = _choose_server()
    if not target_id:
        return

    status, body = ping_server(target_id)
    ui_print("CLIENT: sent REQUEST GET: Hello, Server")
    ui_print(f"CLIENT: GET: {body.strip()} (status={status})")


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
        ui_print(f"Failed to load client config: {e}")
        return None

    server_candidates: list[str] = []
    for node_id, node in config.nodes.items():
        host = node.get("host")
        port = node.get("port")
        if isinstance(host, str) and host.strip() and isinstance(port, int) and port > 0:
            server_candidates.append(node_id)

    if not server_candidates:
        ui_print("No servers found in client config")
        return None

    if len(server_candidates) == 1:
        target_id = server_candidates[0]
        ui_print(f"Connecting to only configured server: {target_id}")
        return target_id

    lines = ["\nChoose server:"]
    for index, node_id in enumerate(server_candidates, start=1):
        lines.append(f"{index}) {node_id}")
    lines.append("0) Back")
    ui_print("\n".join(lines))

    raw = ui_input("> ").strip()
    if raw == "0":
        return None

    try:
        index = int(raw)
    except ValueError:
        ui_print("Invalid menu item")
        return None

    if index < 1 or index > len(server_candidates):
        ui_print("Invalid menu item")
        return None

    return server_candidates[index - 1]


def _print_sessions_brief(sessions: list[dict[str, Any]]) -> None:
    if not sessions:
        ui_print("No active sessions on server")
        return

    lines = ["\nActive sessions:"]
    for index, item in enumerate(sessions, start=1):
        session_id = str(item.get("session_id", ""))
        cwd = str(item.get("cwd", ""))
        platform_name = str(item.get("platform", ""))
        lines.append(f"{index}) session {session_id} | platform={platform_name} | cwd={cwd}")

    ui_print("\n".join(lines))


def _connect_to_server_menu() -> None:
    target_id = _choose_server()
    if not target_id:
        return

    raw_sessions = list_sessions(target_id)
    sessions = _normalize_sessions(raw_sessions)

    if not sessions:
        ui_print(f"\nNo active session on {target_id}.")
        ui_print("Opening new session...")
        interactive_shell(target_id)
        return

    if len(sessions) == 1:
        session = sessions[0]
        session_id = str(session.get("session_id", ""))
        cwd = str(session.get("cwd", ""))
        ui_print(f"\nFound active session {session_id} on {target_id}.")
        ui_print("Connecting...")
        interactive_shell(
            target_id,
            existing_session_id=session_id,
            existing_cwd=cwd,
        )
        return

    ui_print(
        f"\nServer {target_id} has multiple sessions:\n"
        f"1) Continue latest session [{sessions[0].get('session_id')}]\n"
        "2) Choose session manually\n"
        "3) Open new session\n"
        "0) Back"
    )

    choice = ui_input("> ").strip()

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
        ui_print("0) Back")

        raw = ui_input("Session number: ").strip()
        if raw == "0":
            return

        try:
            index = int(raw)
        except ValueError:
            ui_print("Invalid menu item")
            return

        if index < 1 or index > len(sessions):
            ui_print("Invalid menu item")
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

    ui_print("Unknown menu item")


def _show_topology_menu() -> None:
    try:
        config = ClientConfig.load()
    except Exception as e:
        ui_print(f"Failed to load client config: {e}")
        return

    _print_topology(config)


def _client_menu() -> None:
    try:
        restore_background_pollers_from_state()

        ui_print("\nChecking command-history servers for unread responses...")
        refresh_dashboard()

        while True:
            # Quiet refresh. The dashboard itself is printed only once at startup.
            snapshot = refresh_notifications()
            unread_count = get_unread_notification_count(snapshot)

            ui_print(
                "\nClient menu:\n"
                "1) Connect to server\n"
                f"2) View notifications [{unread_count}]\n"
                "3) Ping server\n"
                "4) Show topology\n"
                "5) Reset config\n"
                "0) Back"
            )

            choice = ui_input("> ").strip()

            if choice == "1":
                _connect_to_server_menu()
            elif choice == "2":
                show_notifications()
            elif choice == "3":
                _ping_menu()
            elif choice == "4":
                _show_topology_menu()
            elif choice == "5":
                _reset_client_config()
            elif choice == "0":
                break
            else:
                ui_print("Unknown menu item")
    except KeyboardInterrupt:
        ui_print("Close programm!\n")


def main() -> None:
    while True:
        ui_print(
            "\nSelect mode:\n"
            "1) Server\n"
            "2) Client\n"
            "0) Exit"
        )

        choice = ui_input("> ").strip()

        try:
            if choice == "1":
                setup_logging("server")
                run_server()
                return

            if choice == "2":
                setup_logging("client")
                _client_menu()
                return

            if choice == "0":
                ui_print("Exit")
                return

            ui_print("Unknown menu item")

        except KeyboardInterrupt:
            ui_print("Close programm!\n")
            return